"""Run one BOM check: technical validation + enrichment + optional sizing fit.

This is the single orchestration point used by the upload route, the re-check
route and the tests, so every path produces the same stored result shape
(docs/bom-checker-build.md §5). It deliberately does no I/O of its own beyond
reading the catalog: the route owns the upload, the parser owns the file.

Order matters: platforms are identified *before* the rules run because the
platform's form factor decides the DWPD threshold (1U → 0.2, else 0.3); the
plain rules port only has a string heuristic for that.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

from bom import rules
from bom.normalize import ConfigResult, NormalizedBOM, ValidationResult
from bom import enrich, platform_match


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _catalog_stamp() -> str:
    try:
        from bom.hcl_sync import catalog_stamp
        return catalog_stamp()
    except Exception:  # sync layer absent or no DB — the stamp is informational
        return ""


def _load_hcl():
    from bom.hcl_data import load_hcl_data
    return load_hcl_data()


def _fit_section(bom: NormalizedBOM, sizing) -> Optional[Dict]:
    """The fit block, or None when no sizing was chosen. Never raises: a fit
    problem must not hide the technical verdict."""
    if sizing is None:
        return None
    try:
        from bom import fit
        reqs = fit.sizing_requirements(sizing)
        base = {"sizing": {"id": sizing.id, "name": sizing.name,
                           "mode": (reqs or {}).get("kind")}}
        if reqs is None:
            base.update({"verdict": "unknown", "dimensions": [], "notes": [
                "The selected sizing has no computed result yet - open it once so it "
                "is recalculated, then re-check."]})
            return base
        config = fit.pick_config_for_sizing(bom, reqs)
        if config is None:
            base.update({"verdict": "unknown", "dimensions": [], "notes": [
                "The BOM has no hardware configuration to compare."]})
            return base
        result = fit.compare(config, reqs)
        result.setdefault("config_name", config.name)
        result.update(base)
        return result
    except Exception as exc:  # pragma: no cover - defensive
        return {"verdict": "unknown", "dimensions": [],
                "sizing": {"id": getattr(sizing, "id", None),
                           "name": getattr(sizing, "name", None), "mode": None},
                "notes": ["Fit comparison failed: %s" % exc]}


def run_check(bom: NormalizedBOM, sizing=None, hcl=None, platforms=None) -> Dict:
    """``hcl``/``platforms`` may be injected (tests, eval harness); by default
    both come from the database."""
    hcl = hcl if hcl is not None else _load_hcl()
    if platforms is None:
        from hcl_models import HclPlatform, STATUS_ACTIVE
        platforms = HclPlatform.query.filter_by(status=STATUS_ACTIVE).all()
    try:
        delisted = enrich.load_delisted()
    except Exception:
        delisted = []

    config_results = []  # type: List[ConfigResult]
    config_dicts = []    # type: List[Dict]
    suggestions = []     # type: List[Dict]
    for config in bom.configs:
        if not rules.is_hardware_config(config):
            continue
        matched = platform_match.identify(config, bom.vendor, platforms)
        form_factor = matched[0].form_factor if matched else None
        result = rules.validate_config(config, bom.vendor, hcl, form_factor=form_factor)
        extra = enrich.delisted_findings(config, delisted)
        result.findings.extend(extra)
        result.findings.append(enrich.platform_finding(matched))
        result.verdict = rules.determine_verdict(result.findings)
        config_results.append(result)
        suggestions.extend(enrich.suggestions_for(config, result, matched))
        d = result.to_dict()
        d["config_name"] = result.config_name
        d["platform"] = platform_match.platform_summary(matched)
        d["node_count"] = config.node_count
        config_dicts.append(d)

    verdict = "PASS"
    if not config_results:
        verdict = "INCONCLUSIVE"
    elif any(r.verdict == "FAIL" for r in config_results):
        verdict = "FAIL"
    elif any(r.verdict == "INCONCLUSIVE" for r in config_results):
        verdict = "INCONCLUSIVE"

    return {
        "technical": {"verdict": verdict, "config_results": config_dicts},
        "suggestions": suggestions,
        "fit": _fit_section(bom, sizing),
        "flag_reasons": enrich.flag_reasons(verdict, config_results),
        "catalog_stamp": _catalog_stamp(),
        "checked_at": _now_iso(),
    }
