"""Scrape snapshot <-> catalog diff and the approval queue (docs/bom-checker-build.md §3).

The HCL site has gone stale for months at a time and its markup is not ours,
so nothing a scrape produces may land in the live catalog on its own. This
module turns a ``hcl_scrape.scrape_all`` snapshot into *reviewable*
``HclPendingChange`` rows and applies them only when a super admin approves.
The three concerns are kept apart on purpose:

* ``diff_snapshot`` reads the DB and returns plain change dicts — no rows, no
  commit — so a test (or the import route) can inspect what a snapshot would
  do before anything is recorded.
* ``record_changes`` persists them for one run and supersedes older pending
  rows for the same (entity, kind, field): two scrapes in a row must not
  leave two competing "description changed" rows for one part.
* ``apply_change`` mutates the catalog and is idempotent, because an admin
  can bulk-approve in any order (a platform add creates the parts it lists,
  so the parts' own add rows become no-ops) and a double click on Approve
  must not duplicate anything.

Rules that follow from the plan:

* Delist only from a *complete* snapshot. One dropped detail page would
  otherwise read as 80 parts vanishing from the HCL.
* Delist never deletes: ``status='delisted'`` + ``delisted_at``, so an old
  BOM check still resolves its findings and a re-check can say when the
  part disappeared. A part that reappears gets a ``relist`` change.
* ``touch_seen`` bumps ``last_seen`` without approval — it is bookkeeping
  (when did we last see this on the site), not catalog content.
* Audit entries are written by the routes, not here: the sync layer is
  also driven by a background thread with no request context.
"""
import json
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from sqlalchemy import func

from auth_models import _utcnow
from database import db
from bom.hcl_scrape import synthetic_part_number
from hcl_models import (
    APPROVED, CHANGE_ADD, CHANGE_DELIST, CHANGE_RELIST, CHANGE_UPDATE,
    HclComponent, HclDevice, HclPendingChange, HclPlatform,
    HclPlatformComponent, HclScrapeRun, PENDING, REJECTED, RUN_FAILED,
    RUN_RUNNING, RUN_SUCCEEDED, STATUS_ACTIVE, STATUS_DELISTED, SUPERSEDED,
)

ENTITY_PLATFORM = "platform"
ENTITY_COMPONENT = "component"
ENTITY_DEVICE = "device"
# Pseudo-field on a platform update: the sorted list of "kind/part" keys the
# detail page lists. One row for the whole list keeps the queue readable.
FIELD_COMPONENTS = "components"

LAST_SCRAPE_SETTING = "hcl_last_scrape_at"

# Column widths from hcl_models — a scraped value is trimmed to fit *before*
# the comparison, otherwise an over-long description would diff forever.
_PLATFORM_LIMITS = {"server": 200, "form_factor": 8, "socket": 20, "memory_type": 30}
_COMPONENT_LIMITS = {"part_number": 80, "description": 300}
_DEVICE_LIMITS = {"ven_id": 8, "dev_id": 8, "description": 200, "driver": 40,
                  "dev_type": 16, "since": 20}


class ChangeStateError(ValueError):
    """The change is in a state where the requested decision makes no sense
    (approving a superseded row, rejecting one that is already applied)."""


# ---------------------------------------------------------------------------
# snapshot -> normalised records
# ---------------------------------------------------------------------------

def _fit(value, limit):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit] if limit else text


def _canon(value):
    """JSON round-trip so tuples/lists and dict key order compare equal."""
    return json.loads(json.dumps(value, sort_keys=True, default=str))


def _same(a, b):
    return _canon(a) == _canon(b)


def _synthetic_part(kind: str, description: str, attrs: dict) -> Optional[str]:
    """Same synthetic key the scraper's snapshot index uses (see
    hcl_scrape.synthetic_part_number), trimmed to the column width."""
    part = synthetic_part_number(kind, description, attrs)
    return part[:_COMPONENT_LIMITS["part_number"]] if part else None


def component_record(comp: dict) -> Optional[dict]:
    """One snapshot component into the shape stored on hcl_components."""
    kind = (comp.get("kind") or "").strip().lower()
    part = _fit(comp.get("part_number"), _COMPONENT_LIMITS["part_number"])
    if kind and not part:
        part = _synthetic_part(kind, comp.get("description") or "", comp.get("attrs") or {})
    if not kind or not part:
        return None
    return {
        "key": "%s/%s" % (kind, part),
        "kind": kind,
        "part_number": part,
        "description": _fit(comp.get("description"), _COMPONENT_LIMITS["description"]) or "",
        "tce": bool(comp.get("tce")),
        "attrs": _canon(dict(comp.get("attrs") or {})),
    }


def platform_record(platform: dict) -> Optional[dict]:
    """One snapshot platform (listing card merged with its detail page) into
    the tracked fields plus a de-duplicated, sorted component list.

    The card and the detail page state some facts twice under different
    names (``cpu_count``/``sockets``, ``memory_type``/``ram_type``); the
    detail page wins when present, the card fills the gap.
    """
    brand = (platform.get("brand") or "").strip().lower()
    sc_model = _fit(platform.get("sc_model"), 40)
    if not brand or not sc_model:
        return None
    sockets = platform.get("sockets")
    if sockets is None:
        sockets = platform.get("cpu_count")
    rec = {
        "key": "%s/%s" % (brand, sc_model),
        "brand": brand,
        "sc_model": sc_model,
        "server": _fit(platform.get("server"), _PLATFORM_LIMITS["server"]),
        "form_factor": _fit(platform.get("form_factor"), _PLATFORM_LIMITS["form_factor"]),
        "socket": _fit(platform.get("socket"), _PLATFORM_LIMITS["socket"]),
        "sockets": sockets,
        "max_cores": platform.get("max_cores"),
        "memory_type": _fit(platform.get("memory_type") or platform.get("ram_type"),
                            _PLATFORM_LIMITS["memory_type"]),
        "ram_slots": platform.get("ram_slots"),
        "max_ram_gb": platform.get("max_ram_gb"),
        "power_supply": _fit(platform.get("power_supply"), None),
        "cooling": _fit(platform.get("cooling"), None),
        "tpm": _fit(platform.get("tpm"), None),
        "risers": _fit(platform.get("risers"), None),
        "oob_license": _fit(platform.get("oob_license"), None),
        "hdd_max": platform.get("hdd_max"),
        "ssd_max": platform.get("ssd_max"),
        "nic_listed": bool(platform["nic_listed"]) if "nic_listed" in platform else True,
    }
    comps = {}  # type: Dict[str, dict]
    for comp in platform.get("components") or []:
        crec = component_record(comp)
        if crec is None:
            continue
        # The site lists a few parts twice on one page; one link, TCE if any.
        if crec["key"] in comps:
            comps[crec["key"]]["tce"] = comps[crec["key"]]["tce"] or crec["tce"]
        else:
            comps[crec["key"]] = crec
    rec["components"] = [comps[k] for k in sorted(comps)]
    return rec


def device_record(device: dict) -> Optional[dict]:
    ven = _fit((device.get("ven_id") or "").lower(), _DEVICE_LIMITS["ven_id"])
    dev = _fit((device.get("dev_id") or "").lower(), _DEVICE_LIMITS["dev_id"])
    if not ven or not dev:
        return None
    return {
        "key": "%s/%s" % (ven, dev),
        "ven_id": ven,
        "dev_id": dev,
        "description": _fit(device.get("description"), _DEVICE_LIMITS["description"]),
        "driver": _fit(device.get("driver"), _DEVICE_LIMITS["driver"]),
        "dev_type": _fit(device.get("dev_type") or device.get("type"), _DEVICE_LIMITS["dev_type"]),
        "supported": bool(device.get("supported", True)),
        "since": _fit(device.get("since"), _DEVICE_LIMITS["since"]),
    }


def snapshot_records(snapshot: dict) -> dict:
    """{platforms: {key: rec}, components: {key: rec}, devices: {key: rec}}.

    Components are collected across platforms; ``tce`` is True when any
    platform flags the part (the per-platform flag lives on the link).
    """
    platforms = {}  # type: Dict[str, dict]
    components = {}  # type: Dict[str, dict]
    devices = {}  # type: Dict[str, dict]
    for p in snapshot.get("platforms") or []:
        rec = platform_record(p)
        if rec is None:
            continue
        platforms[rec["key"]] = rec
        for crec in rec["components"]:
            entry = components.get(crec["key"])
            if entry is None:
                components[crec["key"]] = dict(crec)
            elif crec["tce"]:
                entry["tce"] = True
    for d in snapshot.get("devices") or []:
        rec = device_record(d)
        if rec is not None:
            devices[rec["key"]] = rec
    return {"platforms": platforms, "components": components, "devices": devices}


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def _change(entity_type, entity_key, change_kind, field=None, old_value=None,
            new_value=None, payload=None, label=""):
    return {
        "entity_type": entity_type,
        "entity_key": entity_key,
        "change_kind": change_kind,
        "field": field,
        "old_value": old_value,
        "new_value": new_value,
        "payload": payload,
        "label": (label or "")[:200],
    }


def _platform_label(rec):
    return "%s (%s)" % (rec["key"], rec.get("server") or "?")


def _component_label(rec):
    return "%s %s" % (rec["kind"], rec["part_number"])


def _active_link_keys(platform: HclPlatform) -> List[str]:
    return sorted(l.component.key for l in platform.links if l.status == STATUS_ACTIVE)


def diff_snapshot(snapshot: dict) -> List[dict]:
    """Compare a snapshot with the catalog; return change dicts, write nothing.

    Order is component, device, platform (alphabetical on entity type, then
    key) — the same order the queue lists and bulk-applies them in.
    """
    complete = bool(snapshot.get("complete"))
    recs = snapshot_records(snapshot)
    changes = []  # type: List[dict]

    # -- platforms ----------------------------------------------------------
    db_platforms = {p.key: p for p in HclPlatform.query.all()}
    for key, rec in recs["platforms"].items():
        row = db_platforms.get(key)
        payload = dict(rec)
        if row is None:
            changes.append(_change(
                ENTITY_PLATFORM, key, CHANGE_ADD, payload=payload,
                label="New platform %s, %d parts" % (_platform_label(rec), len(rec["components"]))))
            continue
        if row.status == STATUS_DELISTED:
            changes.append(_change(
                ENTITY_PLATFORM, key, CHANGE_RELIST, payload=payload,
                label="Platform %s is listed again" % _platform_label(rec)))
            continue
        for field in HclPlatform.TRACKED:
            old, new = getattr(row, field), rec.get(field)
            if not _same(old, new):
                changes.append(_change(
                    ENTITY_PLATFORM, key, CHANGE_UPDATE, field=field,
                    old_value=old, new_value=new,
                    label="Platform %s: %s changed" % (key, field)))
        old_list = _active_link_keys(row)
        new_list = [c["key"] for c in rec["components"]]
        if old_list != new_list:
            added = len(set(new_list) - set(old_list))
            removed = len(set(old_list) - set(new_list))
            changes.append(_change(
                ENTITY_PLATFORM, key, CHANGE_UPDATE, field=FIELD_COMPONENTS,
                old_value=old_list, new_value=new_list,
                payload={"components": rec["components"]},
                label="Platform %s: part list changed (+%d / -%d)" % (key, added, removed)))
    if complete:
        for key, row in db_platforms.items():
            if key not in recs["platforms"] and row.status == STATUS_ACTIVE:
                changes.append(_change(
                    ENTITY_PLATFORM, key, CHANGE_DELIST,
                    label="Platform %s (%s) no longer listed on the HCL" % (key, row.server or "?")))

    # -- components ---------------------------------------------------------
    db_components = {c.key: c for c in HclComponent.query.all()}
    for key, rec in recs["components"].items():
        row = db_components.get(key)
        payload = {k: rec[k] for k in ("kind", "part_number", "description", "tce", "attrs")}
        if row is None:
            changes.append(_change(
                ENTITY_COMPONENT, key, CHANGE_ADD, payload=payload,
                label="New %s: %s" % (_component_label(rec), rec["description"])))
            continue
        if row.status == STATUS_DELISTED:
            changes.append(_change(
                ENTITY_COMPONENT, key, CHANGE_RELIST, payload=payload,
                label="%s is listed again: %s" % (_component_label(rec), rec["description"])))
            continue
        for field in HclComponent.TRACKED:
            old, new = getattr(row, field), rec.get(field)
            if field == "attrs":
                old = old or {}
            if not _same(old, new):
                changes.append(_change(
                    ENTITY_COMPONENT, key, CHANGE_UPDATE, field=field,
                    old_value=old, new_value=new,
                    label="%s: %s changed" % (_component_label(rec), field)))
    if complete:
        for key, row in db_components.items():
            if key not in recs["components"] and row.status == STATUS_ACTIVE:
                changes.append(_change(
                    ENTITY_COMPONENT, key, CHANGE_DELIST,
                    label="%s %s no longer listed on the HCL: %s" % (
                        row.kind, row.part_number, row.description)))

    # -- devices ------------------------------------------------------------
    db_devices = {d.key: d for d in HclDevice.query.all()}
    for key, rec in recs["devices"].items():
        row = db_devices.get(key)
        payload = {k: rec[k] for k in ("ven_id", "dev_id", "description", "driver",
                                       "dev_type", "supported", "since")}
        if row is None:
            changes.append(_change(
                ENTITY_DEVICE, key, CHANGE_ADD, payload=payload,
                label="New device %s: %s" % (key, rec["description"] or "?")))
            continue
        if row.status == STATUS_DELISTED:
            changes.append(_change(
                ENTITY_DEVICE, key, CHANGE_RELIST, payload=payload,
                label="Device %s is listed again: %s" % (key, rec["description"] or "?")))
            continue
        for field in HclDevice.TRACKED:
            old, new = getattr(row, field), rec.get(field)
            if not _same(old, new):
                changes.append(_change(
                    ENTITY_DEVICE, key, CHANGE_UPDATE, field=field,
                    old_value=old, new_value=new,
                    label="Device %s (%s): %s changed" % (key, row.description or "?", field)))
    if complete:
        for key, row in db_devices.items():
            if key not in recs["devices"] and row.status == STATUS_ACTIVE:
                changes.append(_change(
                    ENTITY_DEVICE, key, CHANGE_DELIST,
                    label="Device %s (%s) no longer listed on the HCL" % (key, row.description or "?")))

    changes.sort(key=lambda c: (c["entity_type"], c["entity_key"], c["field"] or ""))
    return changes


# ---------------------------------------------------------------------------
# recording + bookkeeping
# ---------------------------------------------------------------------------

def _pending_same(entity_key, change_kind, field):
    q = HclPendingChange.query.filter_by(
        entity_key=entity_key, change_kind=change_kind, status=PENDING)
    if field is None:
        q = q.filter(HclPendingChange.field.is_(None))
    else:
        q = q.filter(HclPendingChange.field == field)
    return q


def record_changes(run: HclScrapeRun, changes: List[dict]) -> int:
    """Persist change dicts as pending rows of ``run``; older pending rows for
    the same (entity_key, change_kind, field) become 'superseded'. Flushes,
    does not commit."""
    n = 0
    for ch in changes:
        older = _pending_same(ch["entity_key"], ch["change_kind"], ch["field"]).filter(
            HclPendingChange.run_id != run.id).all()
        for old in older:
            old.status = SUPERSEDED
            old.decided_at = _utcnow()
            old.note = "superseded by run %s" % run.id
        db.session.add(HclPendingChange(
            run_id=run.id,
            entity_type=ch["entity_type"],
            entity_key=ch["entity_key"],
            change_kind=ch["change_kind"],
            field=ch["field"],
            old_value=ch["old_value"],
            new_value=ch["new_value"],
            payload=ch["payload"],
            label=ch["label"],
            status=PENDING,
        ))
        n += 1
    db.session.flush()
    return n


def touch_seen(snapshot: dict, seen_at: Optional[datetime] = None) -> int:
    """Bump ``last_seen`` on every active row (and link) the snapshot
    contains. No approval needed: it records observation, not content."""
    seen_at = seen_at or _utcnow()
    recs = snapshot_records(snapshot)
    n = 0
    for platform in HclPlatform.query.filter_by(status=STATUS_ACTIVE).all():
        rec = recs["platforms"].get(platform.key)
        if rec is None:
            continue
        platform.last_seen = seen_at
        n += 1
        listed = set(c["key"] for c in rec["components"])
        for link in platform.links:
            if link.status == STATUS_ACTIVE and link.component.key in listed:
                link.last_seen = seen_at
    for comp in HclComponent.query.filter_by(status=STATUS_ACTIVE).all():
        if comp.key in recs["components"]:
            comp.last_seen = seen_at
            n += 1
    for dev in HclDevice.query.filter_by(status=STATUS_ACTIVE).all():
        if dev.key in recs["devices"]:
            dev.last_seen = seen_at
            n += 1
    db.session.flush()
    return n


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------

def _split_key(key):
    head, _, tail = (key or "").partition("/")
    return head, tail


def _auto_approve(entity_type, entity_key, kinds, note, user, now, result):
    """Pending rows for an entity that an apply just created/relisted are
    decided by that apply — the admin approved the platform that lists it."""
    rows = HclPendingChange.query.filter(
        HclPendingChange.entity_type == entity_type,
        HclPendingChange.entity_key == entity_key,
        HclPendingChange.change_kind.in_(kinds),
        HclPendingChange.status == PENDING).all()
    for row in rows:
        row.status = APPROVED
        row.decided_at = now
        row.decided_by_user_id = user.id if user else None
        row.note = note
        result["auto_approved"].append(row.id)


def _ensure_component(rec, now, user, via, result):
    """Component row for ``rec`` (create or relist); existing active rows are
    left alone — their field changes are separate queue rows."""
    comp = HclComponent.query.filter_by(kind=rec["kind"], part_number=rec["part_number"]).first()
    if comp is None:
        comp = HclComponent(
            kind=rec["kind"], part_number=rec["part_number"],
            description=rec.get("description") or "", attrs=rec.get("attrs") or {},
            tce=bool(rec.get("tce")), status=STATUS_ACTIVE,
            first_seen=now, last_seen=now)
        db.session.add(comp)
        db.session.flush()
        result["created"].append(comp.key)
        if via:
            _auto_approve(ENTITY_COMPONENT, comp.key, (CHANGE_ADD,), "via platform %s" % via,
                          user, now, result)
    elif comp.status == STATUS_DELISTED:
        comp.status = STATUS_ACTIVE
        comp.delisted_at = None
        comp.last_seen = now
        comp.description = rec.get("description") or comp.description
        comp.attrs = rec.get("attrs") or comp.attrs
        result["created"].append(comp.key)
        if via:
            _auto_approve(ENTITY_COMPONENT, comp.key, (CHANGE_RELIST,), "via platform %s" % via,
                          user, now, result)
    if rec.get("tce") and not comp.tce:
        comp.tce = True
    return comp


def _sync_links(platform, comp_records, now, user, result):
    """Make the platform's links match ``comp_records``: delist links not in
    the list, add or relist the rest, carry the per-link TCE flag."""
    wanted = {}
    for rec in comp_records:
        comp = _ensure_component(rec, now, user, platform.key, result)
        wanted[comp.id] = (comp, bool(rec.get("tce")))
    existing = {l.component_id: l for l in platform.links}
    for comp_id, link in existing.items():
        if comp_id in wanted:
            link.tce = wanted[comp_id][1]
            link.status = STATUS_ACTIVE
            link.last_seen = now
        elif link.status == STATUS_ACTIVE:
            link.status = STATUS_DELISTED
    for comp_id, (comp, tce) in wanted.items():
        if comp_id not in existing:
            db.session.add(HclPlatformComponent(
                platform=platform, component=comp, tce=tce,
                status=STATUS_ACTIVE, first_seen=now, last_seen=now))
    db.session.flush()


def _set_platform_fields(platform, rec):
    for field in HclPlatform.TRACKED:
        if field in rec:
            setattr(platform, field, rec[field])


def _apply_platform(change, now, user, result):
    brand, sc_model = _split_key(change.entity_key)
    platform = HclPlatform.query.filter_by(brand=brand, sc_model=sc_model).first()
    kind = change.change_kind

    if kind in (CHANGE_ADD, CHANGE_RELIST):
        rec = change.payload or {}
        if platform is None:
            platform = HclPlatform(brand=brand, sc_model=sc_model, status=STATUS_ACTIVE,
                                   first_seen=now, last_seen=now)
            _set_platform_fields(platform, rec)
            db.session.add(platform)
            db.session.flush()
            result["created"].append(platform.key)
        else:
            _set_platform_fields(platform, rec)
            if platform.status != STATUS_ACTIVE:
                result["created"].append(platform.key)
            platform.status = STATUS_ACTIVE
            platform.delisted_at = None
            platform.last_seen = now
        if "components" in rec:
            _sync_links(platform, rec.get("components") or [], now, user, result)
        result["applied"] = True
        return

    if platform is None:
        result["reason"] = "platform %s does not exist in the catalog" % change.entity_key
        return

    if kind == CHANGE_UPDATE:
        if change.field == FIELD_COMPONENTS:
            records = (change.payload or {}).get("components")
            if records is None:
                # Old-style row without payload: keep whatever we already know.
                by_key = {c.key: c for c in HclComponent.query.all()}
                records = []
                for key in change.new_value or []:
                    comp = by_key.get(key)
                    if comp is not None:
                        records.append({"kind": comp.kind, "part_number": comp.part_number,
                                        "description": comp.description, "attrs": comp.attrs,
                                        "tce": False})
            _sync_links(platform, records, now, user, result)
        elif change.field in HclPlatform.TRACKED:
            setattr(platform, change.field, change.new_value)
        else:
            result["reason"] = "unknown platform field %r" % change.field
            return
        platform.last_seen = now
        result["applied"] = True
        return

    if kind == CHANGE_DELIST:
        if platform.status != STATUS_DELISTED:
            platform.status = STATUS_DELISTED
            platform.delisted_at = now
        for link in platform.links:
            if link.status == STATUS_ACTIVE:
                link.status = STATUS_DELISTED
        result["applied"] = True
        return

    result["reason"] = "unknown change kind %r" % kind


def _apply_component(change, now, user, result):
    kind_, part = _split_key(change.entity_key)
    comp = HclComponent.query.filter_by(kind=kind_, part_number=part).first()
    kind = change.change_kind

    if kind in (CHANGE_ADD, CHANGE_RELIST):
        rec = dict(change.payload or {})
        rec.setdefault("kind", kind_)
        rec.setdefault("part_number", part)
        if comp is None or comp.status == STATUS_DELISTED:
            _ensure_component(rec, now, user, None, result)
        else:
            # Already there (a platform apply created it): treat as an update.
            comp.description = rec.get("description") or comp.description
            comp.attrs = rec.get("attrs") or comp.attrs
            if rec.get("tce"):
                comp.tce = True
            comp.last_seen = now
        result["applied"] = True
        return

    if comp is None:
        result["reason"] = "component %s does not exist in the catalog" % change.entity_key
        return

    if kind == CHANGE_UPDATE:
        if change.field not in HclComponent.TRACKED:
            result["reason"] = "unknown component field %r" % change.field
            return
        value = change.new_value
        if change.field == "description":
            value = _fit(value, _COMPONENT_LIMITS["description"]) or ""
        elif change.field == "tce":
            value = bool(value)
        setattr(comp, change.field, value)
        comp.last_seen = now
        result["applied"] = True
        return

    if kind == CHANGE_DELIST:
        if comp.status != STATUS_DELISTED:
            comp.status = STATUS_DELISTED
            comp.delisted_at = now
        result["applied"] = True
        return

    result["reason"] = "unknown change kind %r" % kind


def _apply_device(change, now, user, result):
    ven, dev = _split_key(change.entity_key)
    device = HclDevice.query.filter_by(ven_id=ven, dev_id=dev).first()
    kind = change.change_kind

    if kind in (CHANGE_ADD, CHANGE_RELIST):
        rec = dict(change.payload or {})
        if device is None:
            device = HclDevice(ven_id=ven, dev_id=dev, status=STATUS_ACTIVE,
                               first_seen=now, last_seen=now)
            db.session.add(device)
            result["created"].append(device.key)
        elif device.status == STATUS_DELISTED:
            result["created"].append(device.key)
        for field in HclDevice.TRACKED:
            if field in rec:
                setattr(device, field, rec[field])
        device.status = STATUS_ACTIVE
        device.delisted_at = None
        device.last_seen = now
        db.session.flush()
        result["applied"] = True
        return

    if device is None:
        result["reason"] = "device %s does not exist in the catalog" % change.entity_key
        return

    if kind == CHANGE_UPDATE:
        if change.field not in HclDevice.TRACKED:
            result["reason"] = "unknown device field %r" % change.field
            return
        value = change.new_value
        if change.field == "supported":
            value = bool(value)
        setattr(device, change.field, value)
        device.last_seen = now
        result["applied"] = True
        return

    if kind == CHANGE_DELIST:
        if device.status != STATUS_DELISTED:
            device.status = STATUS_DELISTED
            device.delisted_at = now
        result["applied"] = True
        return

    result["reason"] = "unknown change kind %r" % kind


def apply_change(change: HclPendingChange, user=None) -> dict:
    """Apply one change to the catalog (no status bookkeeping, no commit).

    Returns ``{applied, created: [keys], auto_approved: [change ids], reason?}``.
    Idempotent: an add for an entity that already exists is an update, a
    delist of a delisted row is a no-op.
    """
    now = _utcnow()
    result = {"applied": False, "created": [], "auto_approved": []}
    if change.entity_type == ENTITY_PLATFORM:
        _apply_platform(change, now, user, result)
    elif change.entity_type == ENTITY_COMPONENT:
        _apply_component(change, now, user, result)
    elif change.entity_type == ENTITY_DEVICE:
        _apply_device(change, now, user, result)
    else:
        result["reason"] = "unknown entity type %r" % change.entity_type
    db.session.flush()
    return result


# ---------------------------------------------------------------------------
# decisions
# ---------------------------------------------------------------------------

def _decide(change, status, user, note=None):
    change.status = status
    change.decided_at = _utcnow()
    change.decided_by_user_id = user.id if user else None
    if note is not None:
        change.note = note


def approve(change: HclPendingChange, user=None, note=None) -> dict:
    """Apply + mark approved; commits. Approving an approved row re-applies
    (harmless) so a double click never errors."""
    if change.status == SUPERSEDED:
        raise ChangeStateError("change %s was superseded by a newer scrape" % change.id)
    result = apply_change(change, user)
    _decide(change, APPROVED, user, note)
    if not result["applied"] and result.get("reason") and not change.note:
        change.note = result["reason"]
    db.session.commit()
    result["change"] = change.to_dict()
    return result


def reject(change: HclPendingChange, user=None, note=None) -> dict:
    """Mark rejected; the catalog is untouched. Commits."""
    if change.status == APPROVED:
        raise ChangeStateError("change %s is already approved and applied" % change.id)
    if change.status == SUPERSEDED:
        raise ChangeStateError("change %s was superseded by a newer scrape" % change.id)
    _decide(change, REJECTED, user, note)
    db.session.commit()
    return {"applied": False, "change": change.to_dict()}


def _pending_query(filters=None):
    q = HclPendingChange.query.filter_by(status=PENDING)
    filters = filters or {}
    if filters.get("kind"):
        q = q.filter(HclPendingChange.change_kind == filters["kind"])
    if filters.get("entity_type"):
        q = q.filter(HclPendingChange.entity_type == filters["entity_type"])
    if filters.get("run_id"):
        q = q.filter(HclPendingChange.run_id == int(filters["run_id"]))
    return q


def bulk(ids, action, user=None, all_pending=False, filters=None) -> dict:
    """Approve or reject many changes in one commit.

    Component rows come first (alphabetical entity type), so a platform add
    processed later finds its parts already there; a part created *by* a
    platform apply has its own add row auto-approved and is counted as
    approved, not skipped — the admin asked for it either way.
    """
    if action not in ("approve", "reject"):
        raise ValueError("action must be 'approve' or 'reject'")
    if all_pending:
        rows = _pending_query(filters).all()
        requested = len(rows)
    else:
        wanted = [int(i) for i in (ids or [])]
        rows = HclPendingChange.query.filter(HclPendingChange.id.in_(wanted or [-1])).all() \
            if wanted else []
        requested = len(wanted)
    rows.sort(key=lambda c: (c.entity_type, c.entity_key, c.field or "", c.id))

    counts = {"approved": 0, "rejected": 0, "skipped": requested - len(rows)}
    auto = set()
    for change in rows:
        if change.id in auto:
            counts["approved"] += 1
            continue
        if change.status != PENDING:
            counts["skipped"] += 1
            continue
        if action == "approve":
            result = apply_change(change, user)
            _decide(change, APPROVED, user)
            if not result["applied"] and result.get("reason"):
                change.note = result["reason"]
            auto.update(result["auto_approved"])
            counts["approved"] += 1
        else:
            _decide(change, REJECTED, user)
            counts["rejected"] += 1
    db.session.commit()
    return counts


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

def _parse_when(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def build_run(snapshot: dict, user=None, source: str = "scrape",
              run: Optional[HclScrapeRun] = None) -> HclScrapeRun:
    """Diff + record + touch for one snapshot on a (new or existing) run row.

    Commits. Raises on an unexpected error after marking the run failed, so
    the caller (scrape thread, import route) can report it.
    """
    if run is None:
        run = HclScrapeRun(status=RUN_RUNNING, source=source,
                           triggered_by_user_id=user.id if user else None)
        db.session.add(run)
        db.session.flush()
    try:
        run.status = RUN_RUNNING
        if snapshot.get("pages_total"):
            run.pages_total = int(snapshot["pages_total"])
        if snapshot.get("pages_done"):
            run.pages_done = int(snapshot["pages_done"])
        run.complete = bool(snapshot.get("complete"))
        run.errors = list(snapshot.get("errors") or [])

        changes = diff_snapshot(snapshot)
        record_changes(run, changes)
        seen_at = _parse_when(snapshot.get("scraped_at")) or _utcnow()
        touch_seen(snapshot, seen_at)

        recs = snapshot_records(snapshot)
        kinds = {CHANGE_ADD: 0, CHANGE_UPDATE: 0, CHANGE_DELIST: 0, CHANGE_RELIST: 0}
        for ch in changes:
            kinds[ch["change_kind"]] = kinds.get(ch["change_kind"], 0) + 1
        run.summary = {
            "platforms": len(recs["platforms"]),
            "components": len(recs["components"]),
            "devices": len(recs["devices"]),
            "changes": kinds,
            "pending_total": HclPendingChange.query.filter_by(status=PENDING).count(),
        }
        run.status = RUN_SUCCEEDED
        run.finished_at = _utcnow()
        from auth import set_setting
        set_setting(LAST_SCRAPE_SETTING, seen_at.isoformat())
        db.session.commit()
    except Exception as exc:  # noqa: BLE001 - recorded on the run, then re-raised
        db.session.rollback()
        run = db.session.get(HclScrapeRun, run.id) if run.id else run
        if run is not None:
            run.status = RUN_FAILED
            run.error = str(exc)[:2000]
            run.finished_at = _utcnow()
            db.session.commit()
        raise
    return run


def catalog_stamp() -> str:
    """'run<last succeeded run>:change<max approved change>' — what a BOM
    check records so a later re-check can say whether the catalog moved."""
    run_id = db.session.query(func.max(HclScrapeRun.id)).filter(
        HclScrapeRun.status == RUN_SUCCEEDED).scalar() or 0
    change_id = db.session.query(func.max(HclPendingChange.id)).filter(
        HclPendingChange.status == APPROVED).scalar() or 0
    return "run%d:change%d" % (run_id, change_id)
