"""Pre-publication accepts: parts the HCL team has verified but not yet
published on hcl.scalecomputing.com.

Real BOMs arrive quoting parts the HCL team already calls good while the
public HCL still lags (the site has gone stale for months). After talking to
the HCL team, a super admin accepts those parts straight from the failed BOM
check so the very next opportunity passes; the HCL team can then pull the
accepted set from us (``preview_feed``) instead of us waiting on them.

Ground rules, all enforced here or in hcl_sync:

* Every catalog mutation still exists as an *approved* ``HclPendingChange``
  row on a run of ``source='preview'`` — the one audited path. Accepting is
  approving, just with the admin as the diff's author instead of a scrape.
* Accepted entities carry ``origin='preview'``: the scrape's delist logic
  ignores them (their absence from the site is expected) until a complete
  snapshot lists them, at which point they silently become ordinary
  ``origin='scrape'`` entries (hcl_sync.touch_seen).
* Idempotent: accepting the same key twice skips instead of duplicating.
"""
import re
from typing import Dict, List, Optional

from auth_models import _iso, _utcnow
from database import db
from bom import hcl_sync
from bom.hcl_scrape import component_attrs, synthetic_part_number
from bom.normalize import NormalizedBOM
from hcl_models import (
    CHANGE_ADD, CHANGE_RELIST, CHANGE_UPDATE, HclComponent, HclPendingChange,
    HclPlatform, HclScrapeRun, ORIGIN_PREVIEW, PENDING, RUN_SUCCEEDED,
    STATUS_ACTIVE, STATUS_DELISTED,
)

# AppSetting key holding the bearer token for the machine-to-machine pull
# feed (/api/hcl/preview-feed). Empty/absent = feed off.
PREVIEW_FEED_TOKEN_SETTING = "hcl_preview_feed_token"

# finding code -> component kind a super admin may accept ahead of
# publication. component_delisted is handled separately: the part is already
# in the catalog, accepting it means RELIST.
CODE_KINDS = {
    "nic_not_in_hcl": "nic",
    "controller_not_in_hcl": "hba",
    "gpu_not_in_hcl": "gpu",
    "cpu_unknown": "cpu",
}
CODE_DELISTED = "component_delisted"

_PART_LIMIT = hcl_sync._COMPONENT_LIMITS["part_number"]
_DESC_LIMIT = hcl_sync._COMPONENT_LIMITS["description"]


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------

def _bom_component(config, description):
    """The normalized BOM line a finding points at: findings carry the line's
    description verbatim (rules.py + enrich.py both use c.description)."""
    if config is None:
        return None
    for c in config.components:
        if c.description == description:
            return c
    return None


def _matching_delisted(bom_comp) -> Optional[HclComponent]:
    """The delisted catalog component a component_delisted finding matched —
    same matching rule enrich.delisted_findings used to raise it."""
    from bom import enrich
    kinds = enrich._CATEGORY_KINDS.get(bom_comp.category)
    if not kinds:
        return None
    for comp in HclComponent.query.filter_by(status=STATUS_DELISTED).all():
        if comp.kind in kinds and enrich._matches_component(bom_comp, comp):
            return comp
    return None


def candidates_for(check) -> Dict:
    """What the super admin could accept from this check's findings.

    Walks the stored technical result: findings whose code names a part the
    HCL may simply not list yet become candidates keyed ``kind/part``;
    the identified platforms of the result become link targets.
    """
    tech = (check.result or {}).get("technical") or {}
    bom = NormalizedBOM.from_dict(check.normalized or {})
    configs = {c.name: c for c in bom.configs}
    candidates = {}  # type: Dict[str, dict]
    platform_ids = []  # type: List[int]
    for cr in tech.get("config_results") or []:
        config_name = cr.get("config_name") or cr.get("configName") or ""
        config = configs.get(config_name)
        for pid in (cr.get("platform") or {}).get("platform_ids") or []:
            if pid not in platform_ids:
                platform_ids.append(pid)
        for f in cr.get("findings") or []:
            code = f.get("code")
            bom_comp = _bom_component(config, f.get("component") or "")
            if bom_comp is None:
                continue
            if code in CODE_KINDS:
                kind = CODE_KINDS[code]
                attrs = component_attrs(kind, bom_comp.description)
                part = hcl_sync._fit(bom_comp.part_number, _PART_LIMIT) \
                    or synthetic_part_number(kind, bom_comp.description, attrs)
                if not part:
                    continue
                description = (bom_comp.description or "").strip()[:_DESC_LIMIT]
            elif code == CODE_DELISTED:
                delisted = _matching_delisted(bom_comp)
                if delisted is None:
                    continue
                kind, part = delisted.kind, delisted.part_number
                description = delisted.description
                attrs = component_attrs(kind, bom_comp.description)
            else:
                continue
            key = "%s/%s" % (kind, part)
            if key in candidates:
                continue
            active = HclComponent.query.filter_by(
                kind=kind, part_number=part, status=STATUS_ACTIVE).first()
            candidates[key] = {
                "key": key,
                "kind": kind,
                "part_number": part,
                "description": description,
                "attrs": attrs,
                "from_code": code,
                "config_name": config_name,
                "already_in_catalog": active is not None,
            }
    platforms = []
    for pid in platform_ids:
        p = db.session.get(HclPlatform, pid)
        if p is not None and p.status == STATUS_ACTIVE:
            platforms.append({"id": p.id, "key": p.key, "brand": p.brand,
                              "sc_model": p.sc_model, "server": p.server})
    result = {"candidates": list(candidates.values()), "platforms": platforms}
    if not platforms:
        # No platform identified: seed the "create it as pre-publication"
        # sub-form so accepted parts can land linked (and scoped) instead of
        # unlinked. The SC model is the one fact only the admin knows.
        server = next((c.server_model for c in bom.configs if c.server_model), None)
        result["platform_suggestion"] = {
            "brand": (bom.vendor or "").strip().lower() or None,
            "sc_model": _model_name_from_server(server),
            "server": server,
            "form_factor": None,
        }
    return result


_VENDOR_PREFIX_RE = re.compile(
    r"^(lenovo|dell(?:\s+emc)?|hpe|hewlett[- ]packard(?:\s+enterprise)?|supermicro|super\s+micro)\s+",
    re.I)


def _model_name_from_server(server: Optional[str]) -> Optional[str]:
    """Suggested platform name from the BOM's server model.

    These builds are software-only — validated, not certified — so the
    platform is named after the manufacturer's model ("ThinkEdge SE160 Gen 1"),
    not an SC appliance number: there is no HE/HC model to quote. The vendor
    word is dropped because the brand is already a separate field.
    """
    if not server:
        return None
    name = _VENDOR_PREFIX_RE.sub("", server.strip()).strip()
    name = name[:_PLATFORM_NAME_LIMIT].strip()
    return name or None


# ---------------------------------------------------------------------------
# accept
# ---------------------------------------------------------------------------

_SERVER_LIMIT = hcl_sync._PLATFORM_LIMITS["server"]
# HclPlatform.sc_model is String(40); the form must not let a longer
# manufacturer model be typed only to be refused on submit.
_PLATFORM_NAME_LIMIT = 40


def _validated_platform_spec(spec) -> Dict:
    """Normalise + validate the optional {brand, sc_model, server} dict of an
    accept. Brand is any lower-case token up to the column width (the four
    known brands are just the common case — a new vendor must not need a
    deploy); '/' is refused in the key halves because 'brand/sc_model' is the
    entity key everywhere. Raises ValueError (route answers 400)."""
    if not isinstance(spec, dict):
        raise ValueError("platform must be an object with brand and sc_model")
    brand = (spec.get("brand") or "").strip().lower()
    if not brand or len(brand) > 20 or "/" in brand or any(ch.isspace() for ch in brand):
        raise ValueError("platform brand must be a single token of at most 20 characters")
    sc_model = (spec.get("sc_model") or "").strip()
    if not sc_model or len(sc_model) > 40 or "/" in sc_model:
        raise ValueError("platform sc_model must be 1-40 characters (no '/')")
    server = (spec.get("server") or "").strip() or None
    if server is not None and len(server) > _SERVER_LIMIT:
        raise ValueError("platform server must be at most %d characters" % _SERVER_LIMIT)
    return {"brand": brand, "sc_model": sc_model, "server": server}


def accept_parts(check, keys, user, note=None, platform_spec=None) -> Dict:
    """Accept selected candidates of ``check`` into the catalog as
    origin='preview' components, linked to the check's identified platforms.

    ``platform_spec`` ({brand, sc_model, server}) may create — or reuse — a
    pre-publication platform when the check identified none, so the parts
    land linked (and description-scoped) instead of unlinked; the created
    platform is origin='preview' like the parts, with the same delist
    immunity and publication flip.

    Everything goes through the queue-and-approve machinery: a ``preview``
    run collects one approved pending row per mutation, so the audit trail
    and catalog_stamp move exactly as they would for a scrape approval.
    Raises ValueError for unknown keys / a bad platform_spec (route answers
    400).
    """
    info = candidates_for(check)
    by_key = {c["key"]: c for c in info["candidates"]}
    wanted = []
    seen = set()
    for k in keys or []:
        k = (k or "").strip()
        if k not in by_key:
            raise ValueError("unknown candidate key %r" % k)
        if k not in seen:
            seen.add(k)
            wanted.append(by_key[k])
    if not wanted:
        raise ValueError("no candidate keys selected")

    # Validate the platform spec BEFORE any row is written: a refused accept
    # must leave no run behind.
    spec = None
    if platform_spec is not None:
        if info["platforms"]:
            raise ValueError("a platform was identified; acceptance links to it")
        spec = _validated_platform_spec(platform_spec)
        existing = HclPlatform.query.filter_by(
            brand=spec["brand"], sc_model=spec["sc_model"]).first()
        if existing is not None and existing.status == STATUS_ACTIVE \
                and existing.origin != ORIGIN_PREVIEW:
            raise ValueError("platform already exists")

    run = HclScrapeRun(status=RUN_SUCCEEDED, source="preview", complete=False,
                       pages_total=0, pages_done=0, finished_at=_utcnow(),
                       triggered_by_user_id=getattr(user, "id", None))
    db.session.add(run)
    db.session.flush()

    approve_note = "pre-publication accept from BOM check #%s" % check.id
    if note:
        approve_note = "%s: %s" % (approve_note, note)

    platforms = []
    for pd in info["platforms"]:
        p = db.session.get(HclPlatform, pd["id"])
        if p is not None and p.status == STATUS_ACTIVE:
            platforms.append(p)

    platform_created = None
    if spec is not None:
        # Create (or reuse) the pre-publication platform the admin described,
        # through the same audited path as everything else: an approved
        # platform 'add' row (payload origin preview, no components list — a
        # targeted FIELD_LINK row per accepted part follows below).
        platform = HclPlatform.query.filter_by(
            brand=spec["brand"], sc_model=spec["sc_model"]).first()
        if platform is None or platform.status != STATUS_ACTIVE:
            key = "%s/%s" % (spec["brand"], spec["sc_model"])
            change = HclPendingChange(
                run_id=run.id, entity_type=hcl_sync.ENTITY_PLATFORM,
                entity_key=key, change_kind=CHANGE_ADD,
                payload={"brand": spec["brand"], "sc_model": spec["sc_model"],
                         "server": spec["server"], "origin": ORIGIN_PREVIEW},
                status=PENDING,
                label="Pre-publication platform %s (%s)" % (key, spec["server"] or "?"))
            db.session.add(change)
            db.session.flush()
            hcl_sync.approve(change, user, note=approve_note)
            platform = HclPlatform.query.filter_by(
                brand=spec["brand"], sc_model=spec["sc_model"]).first()
        platforms.append(platform)
        platform_created = platform.key

    created, linked, relisted, skipped = [], [], [], []
    for cand in wanted:
        kind, part = cand["kind"], cand["part_number"]
        payload = {"kind": kind, "part_number": part,
                   "description": cand["description"], "attrs": cand["attrs"],
                   "tce": False, "origin": ORIGIN_PREVIEW}
        comp = HclComponent.query.filter_by(kind=kind, part_number=part).first()
        if comp is not None and comp.status == STATUS_ACTIVE:
            skipped.append(cand["key"])
        else:
            change_kind = CHANGE_RELIST if comp is not None else CHANGE_ADD
            change = HclPendingChange(
                run_id=run.id, entity_type=hcl_sync.ENTITY_COMPONENT,
                entity_key=cand["key"], change_kind=change_kind,
                payload=payload, status=PENDING,
                label="Pre-publication %s %s: %s" % (kind, part, cand["description"]))
            db.session.add(change)
            db.session.flush()
            hcl_sync.approve(change, user, note=approve_note)
            (relisted if change_kind == CHANGE_RELIST else created).append(cand["key"])
            comp = HclComponent.query.filter_by(kind=kind, part_number=part).first()
        for platform in platforms:
            link = next((l for l in platform.links if l.component_id == comp.id), None)
            if link is not None and link.status == STATUS_ACTIVE:
                continue
            change = HclPendingChange(
                run_id=run.id, entity_type=hcl_sync.ENTITY_PLATFORM,
                entity_key=platform.key, change_kind=CHANGE_UPDATE,
                field=hcl_sync.FIELD_LINK,
                payload={"component": payload, "tce": False,
                         "link_origin": ORIGIN_PREVIEW},
                status=PENDING,
                label="Pre-publication link %s -> %s" % (cand["key"], platform.key))
            db.session.add(change)
            db.session.flush()
            hcl_sync.approve(change, user, note=approve_note)
            linked.append("%s -> %s" % (platform.key, cand["key"]))

    result = {"run_id": run.id, "created": created, "linked": linked,
              "relisted": relisted, "skipped": skipped,
              "platform_created": platform_created}
    if not platforms:
        # No identified platform on the check: the parts still enter the
        # catalog (load_hcl_data reads components regardless of links) but
        # unlinked, and the caller's UI should say so.
        result["unlinked"] = True
        result["note"] = ("no platform identified on this check; "
                          "components were created without platform links")
    run.summary = dict(result, check_id=check.id)
    db.session.commit()
    return result


# ---------------------------------------------------------------------------
# feed
# ---------------------------------------------------------------------------

def preview_feed() -> Dict:
    """The accepted-but-unpublished set, shaped for the HCL team's pull.

    Only origin='preview' active components appear — once a scrape sees a
    part published, its origin flips to 'scrape' and it drops out of the
    feed on its own. ``accepted_at`` is the row's creation time (first_seen
    is set at accept time for preview parts)."""
    rows = (HclComponent.query
            .filter_by(origin=ORIGIN_PREVIEW, status=STATUS_ACTIVE)
            .order_by(HclComponent.kind, HclComponent.part_number).all())
    components = []
    for c in rows:
        live_links = [l for l in c.links
                      if l.status == STATUS_ACTIVE and l.platform is not None
                      and l.platform.status == STATUS_ACTIVE]
        live_links.sort(key=lambda l: (l.platform.brand, l.platform.sc_model))
        platforms = [{"brand": l.platform.brand, "sc_model": l.platform.sc_model,
                      "server": l.platform.server} for l in live_links]
        components.append({
            "kind": c.kind,
            "part_number": c.part_number,
            "description": c.description,
            "attrs": c.attrs or {},
            "tce": c.tce,
            "accepted_at": _iso(c.first_seen),
            "platforms": platforms,
        })
    # Pre-publication PLATFORMS the admin created (accept_parts platform_spec)
    # — the HCL team needs to know about the missing server card too, not
    # just its parts. Same lifecycle: publication flips origin, dropping it.
    plat_rows = (HclPlatform.query
                 .filter_by(origin=ORIGIN_PREVIEW, status=STATUS_ACTIVE)
                 .order_by(HclPlatform.brand, HclPlatform.sc_model).all())
    plats = [{"brand": p.brand, "sc_model": p.sc_model, "server": p.server,
              "form_factor": p.form_factor, "accepted_at": _iso(p.first_seen)}
             for p in plat_rows]
    return {"generated_at": _utcnow().isoformat(), "components": components,
            "platforms": plats}
