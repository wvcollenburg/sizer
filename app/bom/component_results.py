"""Every BOM line with its own check result, for the "full validation results"
view (feature request: see each component's pass/fail next to its quantity).

Built on the fly from what a check already stores — the normalised BOM and
the technical result — so checks made before this view get it too, and the
rules stay the single source of the verdicts. A finding names its part by the
part's description (rules.finding / enrich / part_notes); a finding naming a
group ("Storage", "NIC", "Vendor", the platform) belongs to the configuration
as a whole and is listed with it.

Status per line:
  fail         an error finding names it
  warning      a warning finding names it (and no error)
  pass         only info findings, or none, in a category the rules check
  not_checked  a category the rules do not look at (chassis, other), or an
               absence placeholder ("No HDD")
"""
from typing import Dict, List, Optional

from bom import rules
from bom.normalize import BOMComponent, BOMConfig, NormalizedBOM

# Categories with checks of their own (rules.validate_config, enrich, notes).
CHECKED = {"cpu", "memory", "storage", "controller", "nic", "gpu", "boss"}
_RANK = {"error": 0, "warning": 1, "info": 2}


def _status(component: BOMComponent, findings: List[Dict]) -> str:
    sev = {str(f.get("severity") or "").lower() for f in findings}
    if "error" in sev:
        return "fail"
    if "warning" in sev:
        return "warning"
    if findings:
        return "pass"
    if component.category not in CHECKED or rules.is_absence_indicator(component):
        return "not_checked"
    return "pass"


def _names(finding: Dict, description: str) -> bool:
    comp = str(finding.get("component") or "")
    if comp == description:
        return True
    # "Multiple controllers" names every controller in one comma-joined string.
    return finding.get("code") == "controller_multiple" and description in comp


def _per_node(quantity: int, nodes: Optional[int]) -> Optional[int]:
    if not nodes or nodes < 1 or quantity % nodes:
        return None
    return quantity // nodes


def _finding_out(f: Dict) -> Dict:
    return {"severity": str(f.get("severity") or "info").lower(),
            "issue": f.get("issue") or "", "remediation": f.get("remediation") or "",
            "code": f.get("code")}


def config_component_results(config: BOMConfig, config_result: Dict) -> Dict:
    findings = list(config_result.get("findings") or [])
    nodes = config.node_count or config_result.get("node_count")
    used = set()
    lines = []
    for comp in config.components:
        mine = [(i, f) for i, f in enumerate(findings) if _names(f, comp.description)]
        used.update(i for i, _f in mine)
        own = sorted((f for _i, f in mine),
                     key=lambda f: _RANK.get(str(f.get("severity")).lower(), 9))
        qty = int(comp.quantity or 1)
        lines.append({
            "part_number": comp.part_number, "description": comp.description,
            "category": comp.category, "quantity": qty, "per_node": _per_node(qty, nodes),
            "status": _status(comp, own), "findings": [_finding_out(f) for f in own],
        })
    general = sorted((f for i, f in enumerate(findings) if i not in used),
                     key=lambda f: _RANK.get(str(f.get("severity")).lower(), 9))
    counts = {s: sum(1 for l in lines if l["status"] == s)
              for s in ("pass", "warning", "fail", "not_checked")}
    return {
        "config_name": config_result.get("config_name") or config_result.get("configName")
        or config.name,
        "verdict": config_result.get("verdict"),
        "node_count": nodes,
        "components": lines,
        "config_findings": [dict(_finding_out(f), component=f.get("component") or "")
                            for f in general],
        "counts": counts,
    }


def component_results(normalized: Optional[Dict], result: Optional[Dict]) -> List[Dict]:
    """One entry per checked (hardware) configuration, in the order the check
    ran them, which is the order ``technical.config_results`` holds."""
    if not normalized or not result:
        return []
    bom = NormalizedBOM.from_dict(normalized)
    hardware = [c for c in bom.configs if rules.is_hardware_config(c)]
    checked = ((result.get("technical") or {}).get("config_results")
               or (result.get("technical") or {}).get("configResults") or [])
    return [config_component_results(c, r) for c, r in zip(hardware, checked)]
