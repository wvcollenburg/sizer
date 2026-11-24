"""Every BOM line with its own result (bom.component_results), the data behind
the "See full validation results" view.

Locks in:
  - a finding naming a part's description marks that part (error -> fail,
    warning -> warning, info only -> pass); "Multiple controllers" marks
    every controller it names
  - a checked category with no finding passes; chassis/other lines and
    absence placeholders ("No HDD") are not checked
  - group findings ("Storage", "NIC", the platform) go to the configuration
  - per node divides evenly over the stated node count, else stays empty
  - configs pair with the result in check order; non-hardware configs skip
  - the API's full check carries it, also for a check stored before it

Run: .venv/bin/python -m pytest tests/test_bom_component_results.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from bom.component_results import component_results  # noqa: E402
from bom.normalize import BOMComponent, BOMConfig, NormalizedBOM  # noqa: E402


def _c(cat, desc, qty=1, part=None):
    return BOMComponent(part_number=part, description=desc, quantity=qty, category=cat)


def _f(sev, comp, issue="x", code=None):
    return {"severity": sev, "component": comp, "issue": issue, "remediation": "fix " + issue,
            "code": code}


CONFIG = BOMConfig(name="Prod", server_model="R760", node_count=3, components=[
    _c("chassis", "PowerEdge R760", 3, "210-BDZY"),
    _c("cpu", "Xeon Gold 6526Y", 6, "338-CPBV"),
    _c("memory", "32GB RDIMM", 96),
    _c("storage", "8TB HDD SAS", 27),
    _c("storage", "No HDD", 3),
    _c("controller", "HBA355i", 3),
    _c("controller", "PERC H755", 3),
    _c("nic", "E810 25GbE OCP", 3),
    _c("other", "Rails", 3),
    _c("memory", "16GB odd", 4),
])
FINDINGS = [
    _f("error", "HBA355i, PERC H755", "Multiple controllers detected", "controller_multiple"),
    _f("info", "HBA355i", "Supported controller — HBA mode", "controller_hba_default"),
    _f("warning", "E810 25GbE OCP", "Only 2 ports per node"),
    _f("info", "Storage", "SATA HDDs detected"),
    _f("info", "Platform", "Platform not matched"),
]


def _run(configs=None, findings=None, extra=()):
    bom = NormalizedBOM(vendor="Dell", configs=configs or [CONFIG])
    result = {"technical": {"verdict": "FAIL", "config_results": [
        {"config_name": "Prod", "verdict": "FAIL", "findings": findings or FINDINGS, "node_count": 3}
    ] + list(extra)}}
    return component_results(bom.to_dict(), result)


def _by_desc(cr):
    return {l["description"]: l for l in cr["components"]}


def test_each_line_gets_its_own_result():
    cr = _run()[0]
    lines = _by_desc(cr)
    assert lines["HBA355i"]["status"] == "fail"           # error outranks its info note
    assert [f["severity"] for f in lines["HBA355i"]["findings"]] == ["error", "info"]
    assert lines["PERC H755"]["status"] == "fail"         # named in the joined string
    assert lines["E810 25GbE OCP"]["status"] == "warning"
    assert lines["Xeon Gold 6526Y"]["status"] == "pass" and lines["Xeon Gold 6526Y"]["findings"] == []
    assert lines["PowerEdge R760"]["status"] == "not_checked"
    assert lines["Rails"]["status"] == "not_checked"
    assert lines["No HDD"]["status"] == "not_checked"
    assert cr["counts"] == {"pass": 4, "warning": 1, "fail": 2, "not_checked": 3}


def test_group_findings_belong_to_the_configuration():
    cr = _run()[0]
    assert [f["component"] for f in cr["config_findings"]] == ["Storage", "Platform"]
    assert cr["config_name"] == "Prod" and cr["verdict"] == "FAIL" and cr["node_count"] == 3


def test_per_node_only_when_it_divides_evenly():
    lines = _by_desc(_run()[0])
    assert lines["Xeon Gold 6526Y"]["per_node"] == 2
    assert lines["32GB RDIMM"]["per_node"] == 32
    assert lines["16GB odd"]["per_node"] is None           # 4 over 3 nodes
    nodeless = BOMConfig(name="Prod", server_model=None, components=[_c("cpu", "Xeon", 2)])
    bom = NormalizedBOM(vendor="Dell", configs=[nodeless])
    cr = component_results(bom.to_dict(), {"technical": {"config_results": [
        {"config_name": "Prod", "verdict": "PASS", "findings": []}]}})[0]
    assert cr["node_count"] is None and cr["components"][0]["per_node"] is None


def test_nothing_without_a_result():
    assert component_results(None, None) == []
    assert component_results(NormalizedBOM(vendor="Dell").to_dict(), {"technical": {}}) == []
