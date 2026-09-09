"""Second pass over a technical validation, using what the scraped catalog
knows beyond SC//Design's rule set.

Three things happen here, all additive (the rules port stays a faithful
1:1 port so its fixture parity holds):

* delisted parts — a BOM line that matches a component the HCL has since
  dropped gets a warning with the delist date. The rules engine only sees the
  live lists; this is where "it used to be validated" surfaces.
* swap suggestions — for "not in the HCL" findings on a config whose platform
  we identified, offer that platform's own validated parts of the same kind
  (NICs at the same or higher speed, same form factor preferred). Constrained
  to the identified platform so the suggestion is something the vendor will
  actually configure on that server.
* flag reasons — which findings need a human (the admin review queue). Only
  the uncertain cases: INCONCLUSIVE verdicts, "not found" lookups where the
  catalog may simply be incomplete, and delisted parts. Well-known hard
  failures (BOSS, pre-Scalable CPU, diskless) go straight to the user.
"""
import re
from typing import Dict, List, Optional

from bom.normalize import BOMComponent, BOMConfig, ConfigResult, Finding, normalize_part
from bom.hcl_scrape import component_attrs
from hcl_models import (HclComponent, HclPlatform, HclPlatformComponent,
                        STATUS_ACTIVE, STATUS_DELISTED)

# finding code -> component kind in the catalog
SUGGESTABLE = {
    "nic_not_in_hcl": "nic",
    "controller_not_in_hcl": "hba",
    "gpu_not_in_hcl": "gpu",
}

# Codes whose presence sends a check to the admin review queue.
REVIEW_CODES = frozenset([
    "controller_not_in_hcl", "nic_not_in_hcl", "gpu_not_in_hcl", "cpu_unknown",
    "component_delisted", "controller_unsupported", "gpu_unsupported",
])

CODE_DELISTED = "component_delisted"
CODE_PLATFORM_IDENTIFIED = "platform_identified"
CODE_PLATFORM_UNKNOWN = "platform_unknown"

_CATEGORY_KINDS = {"nic": ("nic",), "controller": ("hba",), "gpu": ("gpu",),
                   "storage": ("hdd", "ssd"), "cpu": ("cpu",)}


def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _matches_component(c: BOMComponent, comp: HclComponent) -> bool:
    """A BOM line names a catalog component when the part numbers agree (with
    Dell's '-P' suffix tolerated) or the descriptions are identical — the
    Lenovo DCSC case, where feature codes carry no HCL key but the
    descriptions are copied verbatim from the same configurator."""
    if c.part_number and comp.part_number:
        a = _norm(normalize_part(c.part_number))
        b = _norm(normalize_part(comp.part_number))
        if a and a == b:
            return True
    return bool(c.description) and _norm(c.description) == _norm(comp.description)


def delisted_findings(config: BOMConfig,
                      delisted: List[HclComponent]) -> List[Finding]:
    out = []  # type: List[Finding]
    for c in config.components:
        kinds = _CATEGORY_KINDS.get(c.category)
        if not kinds:
            continue
        for comp in delisted:
            if comp.kind in kinds and _matches_component(c, comp):
                when = comp.delisted_at.date().isoformat() if comp.delisted_at else "an earlier date"
                out.append(Finding(
                    severity="warning",
                    component=c.description,
                    issue="Component was removed from the Hardware Compatibility List on %s" % when,
                    remediation="This part (%s) was validated once but is no longer listed. "
                                "Confirm with the product team before quoting, or pick a currently "
                                "listed alternative." % comp.part_number,
                    code=CODE_DELISTED,
                ))
                break
    return out


def platform_finding(platforms: List[HclPlatform]) -> Finding:
    if platforms:
        models = ", ".join(sorted({p.sc_model for p in platforms}))
        first = platforms[0]
        return Finding(
            severity="info",
            component=first.server or models,
            issue="Platform identified: %s (%s)" % (models, first.server or first.brand),
            remediation="Component checks and swap suggestions use the validated part list of "
                        "this platform.",
            code=CODE_PLATFORM_IDENTIFIED,
        )
    return Finding(
        severity="info",
        component="Platform",
        issue="Server platform could not be matched to an HC-Ready platform",
        remediation="Checks run against the whole HCL; no platform-specific swap suggestions "
                    "are possible. Confirm the server model is on the HC-Ready list.",
        code=CODE_PLATFORM_UNKNOWN,
    )


def _platform_parts(platforms: List[HclPlatform], kind: str) -> List[Dict]:
    """Active components of ``kind`` validated for any of the platforms, with
    the per-platform TCE flag folded to 'any'."""
    seen = {}  # type: Dict[int, Dict]
    for p in platforms:
        for link in p.links:
            comp = link.component
            if link.status != STATUS_ACTIVE or comp.status != STATUS_ACTIVE or comp.kind != kind:
                continue
            entry = seen.setdefault(comp.id, {
                "part_number": comp.part_number,
                "description": comp.description,
                "tce": False,
                "attrs": comp.attrs or {},
            })
            entry["tce"] = entry["tce"] or bool(link.tce)
    return list(seen.values())


def suggestions_for(config: BOMConfig, result: ConfigResult,
                    platforms: List[HclPlatform], limit: int = 4) -> List[Dict]:
    """Swap candidates for each suggestable finding of a config."""
    if not platforms:
        return []
    out = []  # type: List[Dict]
    for f in result.findings:
        kind = SUGGESTABLE.get(f.code or "")
        if not kind:
            continue
        offending = next((c for c in config.components
                          if c.description == f.component), None)
        off_attrs = component_attrs(kind, offending.description) if offending else {}
        pool = _platform_parts(platforms, kind)
        if kind == "nic":
            need = off_attrs.get("speed_gbe")
            ff = off_attrs.get("form_factor")
            ports = off_attrs.get("ports")
            if need:
                pool = [p for p in pool if (p["attrs"].get("speed_gbe") or 0) >= need] or pool
            # Nearest-equivalent first: same slot type, lowest speed that still
            # covers the offending part, closest port count (a 2-port asked
            # for gets the 2-port offered before the 4-port).
            pool.sort(key=lambda p: (
                0 if ff and p["attrs"].get("form_factor") == ff else 1,
                p["attrs"].get("speed_gbe") or 0,
                abs((p["attrs"].get("ports") or 0) - ports) if ports else 0,
                -(p["attrs"].get("ports") or 0),
                p["part_number"],
            ))
        else:
            pool.sort(key=lambda p: (p["description"], p["part_number"]))
        if not pool:
            continue
        out.append({
            "config_name": config.name,
            "component": f.component,
            "code": f.code,
            "kind": kind,
            "candidates": [{
                "part_number": p["part_number"],
                "description": p["description"],
                "tce": p["tce"],
                "speed_gbe": p["attrs"].get("speed_gbe"),
                "ports": p["attrs"].get("ports"),
                "form_factor": p["attrs"].get("form_factor"),
            } for p in pool[:limit]],
        })
    return out


def flag_reasons(verdict: str, config_results: List[ConfigResult]) -> List[str]:
    reasons = []  # type: List[str]
    if verdict == "INCONCLUSIVE":
        reasons.append("inconclusive")
    for r in config_results:
        for f in r.findings:
            if f.code in REVIEW_CODES and f.code not in reasons:
                reasons.append(f.code)
    return reasons


def load_delisted() -> List[HclComponent]:
    return HclComponent.query.filter_by(status=STATUS_DELISTED).all()
