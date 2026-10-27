"""mcp_presenter: engine output as the MCP tools return it
(docs/mcp/01-tool-design.md §2.4, §2.5).

Locks in:
  - every option carries the capacity block, the determinant statement in the
    guide's format, and all ten checks in order
  - capacity headroom equals the engine's determinant headroom for the
    determining resource (same formula, same inputs)
  - checks read the engine's numbers: a largest VM near a node's RAM warns,
    an IOPS shortfall fails, a single node makes N-1 not applicable
  - nothing price-shaped and no score reaches the output
  - catalog_service lists what /api/models lists

Run: .venv/bin/python -m pytest tests/test_mcp_presenter.py -q
"""
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.pop("SESSION_COOKIE_SECURE", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

import app as appmod  # noqa: E402
import catalog_service  # noqa: E402
import mcp_presenter as pres  # noqa: E402
import sizing_service as svc  # noqa: E402
from test_security import _offending_keys  # noqa: E402
from test_sizing_service import SUMMARY, client  # noqa: E402,F401

PARAMS = {"vcpu_ratio": 3.0, "max_day_one_storage_pct": None, "max_day_one_ram_pct": None}


def _run(summary=SUMMARY, **kwargs):
    with appmod.app.app_context():
        result = svc.run_recommendation(summary, **kwargs)
        params = dict(PARAMS, vcpu_ratio=kwargs.get("vcpu_ratio") or 3.0)
        return result, pres.present_result(result, summary, params, max_options=8)


def test_option_shape_and_checks(client):
    result, out = _run()
    assert out["options"], "fixture catalog must fit"
    assert out["options_total"] == len(result["recommendations"])
    top = out["options"][0]
    assert top["rank"] == 1 and top["model"] == "TEST-HCI"
    assert set(top["capacity"]) == {"cpu", "ram", "storage", "iops"}
    assert [c["id"] for c in top["checks"]] == list(pres.CHECK_IDS)
    assert all(c["status"] in ("pass", "warn", "fail", "not_applicable") for c in top["checks"])
    assert "score" not in top and "network_svg" not in top
    assert out["projection"]["base"]["vcpus"] == 148
    assert out["projection"]["at_horizon"]["vcpus"] == result["projection"]["projected_vcpus"]


def test_headroom_matches_the_engine_determinant(client):
    result, out = _run()
    for opt, rec in zip(out["options"], result["recommendations"]):
        det = opt["determinant"]
        key = {"CPU": "cpu", "RAM": "ram", "Storage": "storage"}.get(det["resource"])
        if key:
            assert opt["capacity"][key]["headroom_pct"] == pytest.approx(det["headroom_pct"], abs=0.11)
            assert det["statement"].startswith("Determined by %s: " % det["resource"])
            assert "%s%% headroom" % pres._fmt(det["headroom_pct"]) in det["statement"]
        elif det["resource"] == "minimum":
            assert det["statement"].startswith("Determined by the minimum cluster size")


def test_determinant_statement_format():
    rec = {"determinant": {"resource": "CPU", "required": 80.0, "achieved": 94,
                           "unit": "cores", "headroom_pct": 17.5}}
    assert pres.present_determinant(rec)["statement"] == \
        "Determined by CPU: 80 cores required vs 94 cores usable at N-1 (17.5% headroom)."
    rec["sized_full_cluster"] = True
    assert "usable across the full cluster" in pres.present_determinant(rec)["statement"]
    rec = {"determinant": {"resource": "Storage", "required": 15.9, "achieved": 30.1,
                           "unit": "TB", "headroom_pct": 89.3}}
    assert pres.present_determinant(rec)["statement"] == \
        "Determined by Storage: 15.9 TB required vs 30.1 TB usable (89.3% headroom)."
    rec = {"determinant": {"resource": "Compute", "required": 100.0, "achieved": 141.0,
                           "unit": "%", "headroom_pct": 41.0}}
    assert pres.present_determinant(rec)["statement"] == \
        "Determined by compute throughput: 141% of the source's utilised, grown compute demand (41% headroom)."


def test_checks_read_the_numbers(client):
    # A largest VM at 95% of usable node RAM warns; an IOPS demand the
    # fixture's 10 NVMe drives cannot meet fails; no IOPS input -> n/a.
    _, out = _run(dict(SUMMARY, max_vm_ram_gb=360, p95_iops=50_000_000))
    checks = {c["id"]: c for c in out["options"][0]["checks"]}
    assert checks["largest_vm_ram"]["status"] == "warn"
    assert "HA" in checks["largest_vm_ram"]["detail"]
    assert checks["iops"]["status"] == "fail"
    assert checks["largest_vm_cpu"]["status"] == "pass"
    assert checks["day_one_storage"]["status"] == "pass"
    assert checks["day_one_ram"]["status"] == "pass"
    assert checks["cluster_size"]["status"] == "pass"
    assert checks["n_minus_1"]["status"] == "pass"
    assert checks["hybrid_flash"]["status"] == "not_applicable"
    _, out = _run({k: v for k, v in SUMMARY.items() if k not in ("p95_iops", "max_vm_cores", "max_vm_ram_gb")})
    checks = {c["id"]: c for c in out["options"][0]["checks"]}
    assert checks["iops"]["status"] == "not_applicable"
    assert checks["largest_vm_cpu"]["status"] == "not_applicable"
    assert checks["largest_vm_ram"]["status"] == "not_applicable"


def test_vm_density_applies_to_every_option(client):
    _, out = _run(dict(SUMMARY, active_vms=5000))
    for opt in out["options"]:
        c = next(c for c in opt["checks"] if c["id"] == "vm_density")
        per_node = 5000 / opt["hci_node_count"]
        assert c["status"] == ("warn" if per_node > 100 else "pass")


def test_nothing_price_shaped_in_the_output(client):
    _, out = _run()
    assert _offending_keys(out) == []
    text = pres.summary_text(out, {"defaults_applied": ["nic_speed_gbe"]})
    assert text.startswith("Top option: ")
    assert "Determined by" in text
    assert "Defaults applied: nic_speed_gbe." in text


def test_no_fit_summary():
    text = pres.summary_text({"options": [], "options_total": 0,
                              "warnings": ["No SC// configuration fits this workload."]})
    assert text.splitlines()[0] == "No configuration fits this workload."
    assert text.splitlines()[1].startswith("Warning: No SC//")


def test_catalog_service_matches_the_models_route(client):
    res = client.get("/api/models", query_string={"sizing": "certified"})
    assert res.status_code == 200
    with appmod.app.app_context():
        models = catalog_service.appliance_models(for_sizing=True)
    assert res.get_json() == models
    assert "TEST-HCI" in models and "cost_tier" not in models["TEST-HCI"]
