"""Catalog -> ``rules.HclData`` adapter and small lookup helpers (build spec §4).

``bom.rules`` is a verbatim port of SC//Design's validator and expects the
flat hcl-snapshot shape it was written against (lists of HBAs, NICs, CPUs,
GPUs with a handful of string fields). Our catalog is normalised — one
component row per part, links to platforms, structured ``attrs`` — so this
module flattens it back. Two things are deliberate:

* Delisted parts are *included* by default with ``eol=True``. The rules
  layer turns that into "supported but end-of-life" rather than "not in the
  HCL", which is the right message for a BOM quoting a part that was on the
  list last quarter. Callers wanting a strict view pass
  ``include_delisted=False``.

Blocked vendors live in an AppSetting (comma list) so an admin can flip
"we are not certifying HPE right now" over HTTP, not in an env file.
"""
from typing import Iterable, List

from sqlalchemy.orm import joinedload

from bom.rules import HclCpu, HclData, HclGpu, HclHba, HclNic
from database import db
from hcl_models import (
    HclComponent, HclDevice, HclPlatform, HclPlatformComponent, ORIGIN_PREVIEW,
    STATUS_ACTIVE, STATUS_DELISTED,
)

BLOCKED_VENDORS_SETTING = "bom_blocked_vendors"


# ---------------------------------------------------------------------------
# per-part mapping (shared by the DB and the snapshot path)
# ---------------------------------------------------------------------------

def _speed_str(attrs: dict) -> str:
    speed = (attrs or {}).get("speed_gbe")
    if speed is None:
        return ""
    try:
        return "%dGbE" % int(speed)
    except (TypeError, ValueError):
        return "%sGbE" % speed


def _hba(part: str, description: str, attrs: dict, delisted: bool) -> HclHba:
    return HclHba(
        part=part,
        type=str((attrs or {}).get("model") or "SAS"),
        description=description or "",
        eol=delisted,
        supported=True,
    )


def _nic(part: str, description: str, attrs: dict, platforms=None) -> HclNic:
    attrs = attrs or {}
    return HclNic(
        part=part,
        type=str(attrs.get("family") or ""),
        speed=_speed_str(attrs),
        description=description or "",
        form_factor=str(attrs.get("form_factor") or ""),
        platforms=platforms,
    )


def _cpu(description: str, attrs: dict, socket: str, platforms=None) -> HclCpu:
    return HclCpu(
        model=str((attrs or {}).get("model") or description or ""),
        description=description or "",
        socket=socket or "",
        platforms=platforms,
    )


def _gpu(part: str, description: str, attrs: dict, delisted: bool, platforms=None) -> HclGpu:
    attrs = attrs or {}
    vram = attrs.get("vram_gb") or attrs.get("vram") or 0
    try:
        vram = int(vram or 0)
    except (TypeError, ValueError):
        vram = 0
    return HclGpu(
        part=part,
        model=str(attrs.get("model") or description or ""),
        description=description or "",
        vram=vram,
        eol=delisted,
        supported=True,
        platforms=platforms,
    )


def _build(parts: Iterable[dict], blocked: List[str]) -> HclData:
    """``parts`` are dicts {kind, part_number, description, attrs, delisted,
    socket, platforms?}; order is preserved because rules.py's Array.find
    semantics return the first match. ``platforms`` (absent/None for every
    scraped part) scopes description-based matching — see
    rules._platform_scope_allows."""
    data = HclData(blocked_vendors=list(blocked))
    for p in parts:
        kind = p["kind"]
        scope = p.get("platforms")
        if kind == "hba":
            data.hbas.append(_hba(p["part_number"], p["description"], p["attrs"], p["delisted"]))
        elif kind == "nic":
            data.nics.append(_nic(p["part_number"], p["description"], p["attrs"],
                                  platforms=scope))
        elif kind == "cpu":
            data.cpus.append(_cpu(p["description"], p["attrs"], p.get("socket") or "",
                                  platforms=scope))
        elif kind == "gpu":
            data.gpus.append(_gpu(p["part_number"], p["description"], p["attrs"], p["delisted"],
                                  platforms=scope))
    return data


# ---------------------------------------------------------------------------
# blocked vendors
# ---------------------------------------------------------------------------

def _parse_vendors(value) -> List[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = value.split(",")
    out = []
    for v in value:
        v = str(v).strip().lower()
        if v and v not in out:
            out.append(v)
    return out


def blocked_vendors() -> List[str]:
    from auth import get_setting
    return _parse_vendors(get_setting(BLOCKED_VENDORS_SETTING, ""))


def set_blocked_vendors(vendors) -> List[str]:
    """Normalise + store; the caller commits (same contract as set_setting)."""
    from auth import set_setting
    cleaned = _parse_vendors(vendors)
    set_setting(BLOCKED_VENDORS_SETTING, ",".join(cleaned))
    return cleaned


# ---------------------------------------------------------------------------
# DB -> HclData
# ---------------------------------------------------------------------------

def _first_socket(comp: HclComponent) -> str:
    for link in sorted(comp.links, key=lambda l: l.id or 0):
        if link.platform is not None and link.platform.socket:
            return link.platform.socket
    return ""


def load_hcl_data(include_delisted: bool = True) -> HclData:
    q = HclComponent.query.filter(HclComponent.kind.in_(("hba", "nic", "cpu", "gpu")))
    if not include_delisted:
        q = q.filter(HclComponent.status == STATUS_ACTIVE)
    else:
        # A delisted SCRAPED part stays matchable on purpose: it was validated
        # once, so an old check still resolves and enrich adds the "removed
        # from the HCL on <date>" warning. A delisted PRE-PUBLICATION part is
        # the opposite case — withdrawn by an admin, or superseded by the
        # published part it stood in for — and was never validated by anyone,
        # so it must stop matching outright.
        q = q.filter(db.or_(HclComponent.origin != ORIGIN_PREVIEW,
                            HclComponent.status == STATUS_ACTIVE))
    q = q.options(joinedload(HclComponent.links).joinedload(HclPlatformComponent.platform))
    rows = q.order_by(HclComponent.kind, HclComponent.id).all()
    parts = []
    for comp in rows:
        # Scoping is filled ONLY for preview-origin parts: their identity may
        # be nothing more than a verbatim description, so description-based
        # matches must stay confined to the platforms they were accepted for
        # (empty tuple = accepted with no platform, matches only BOMs with no
        # identified platform). Scraped parts stay None = unrestricted.
        scope = None
        if comp.origin == ORIGIN_PREVIEW:
            scope = tuple(sorted(
                l.platform.key for l in comp.links
                if l.status == STATUS_ACTIVE and l.platform is not None
                and l.platform.status == STATUS_ACTIVE))
        parts.append({
            "kind": comp.kind,
            "part_number": comp.part_number,
            "description": comp.description,
            "attrs": comp.attrs or {},
            "delisted": comp.status == STATUS_DELISTED,
            "socket": _first_socket(comp) if comp.kind == "cpu" else "",
            "platforms": scope,
        })
    return _build(parts, blocked_vendors())


def catalog_counts() -> dict:
    def _by_status(model):
        active = model.query.filter_by(status=STATUS_ACTIVE).count()
        delisted = model.query.filter_by(status=STATUS_DELISTED).count()
        return {"active": active, "delisted": delisted}

    components = _by_status(HclComponent)
    by_kind = {}
    for comp in HclComponent.query.filter_by(status=STATUS_ACTIVE).with_entities(
            HclComponent.kind).all():
        by_kind[comp[0]] = by_kind.get(comp[0], 0) + 1
    components["by_kind"] = by_kind
    return {
        "platforms": _by_status(HclPlatform),
        "components": components,
        "devices": _by_status(HclDevice),
    }
