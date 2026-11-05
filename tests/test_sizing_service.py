"""sizing_service: one code path for the browser and the MCP server
(docs/mcp/01-tool-design.md §7, docs/plan_enableMCP_sizingtool.md Phase 2a).

Locks in:
  - /api/recommend == sizing_service.recommend == generate_recommendations for
    the same inputs (parity), with the route's defaults and error replies
    unchanged by the extraction
  - the import route sizes through the same function (its warnings carry the
    input caveats)
  - /api/cpu-perf == sizing_service.lookup_cpu_perf
  - source_perf_index reproduces the browser's computeSourcePerf arithmetic
  - the MCP workload <-> engine summary mapping round-trips, never invents a
    measured value, and reports the one default it applies (NIC speed)
  - validate_workload names missing/invalid fields and flags implausible
    figures without blocking
  - sizing_service stays out of the engine fingerprint

Run: .venv/bin/python -m pytest tests/test_sizing_service.py -q
"""
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.pop("SESSION_COOKIE_SECURE", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402
import app as appmod  # noqa: E402
import orm_models as om  # noqa: E402
import sizing_service as svc  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from fingerprint import ENGINE_MODULES  # noqa: E402
from recommend import generate_recommendations  # noqa: E402
from tunables import DEFAULTS  # noqa: E402

SUMMARY = {"total_vcpus": 148, "total_vm_provisioned_memory_gb": 423,
           "datastore_used_tb": 8.2, "nic_speed_mbps": 10000, "active_vms": 59,
           "p95_iops": 16000, "max_vm_cores": 16, "max_vm_ram_gb": 64,
           "total_host_cores": 72, "total_host_ghz": 151.2}

WORKLOAD = {"total_vcpus": 148, "provisioned_ram_gb": 423, "used_storage_tb": 8.2,
            "largest_vm_vcpus": 16, "largest_vm_ram_gb": 64, "active_vms": 59,
            "p95_iops": 16000, "total_host_cores": 72, "total_host_ghz": 151.2}


def _seed_catalog():
    for k, v in DEFAULTS.items():
        db.session.add(om.SizingSetting(key=k, value=float(v)))
    cpu = om.CpuCatalog(description="Xeon Test 24C", cores=24, threads=48,
                        ghz=2.4, specrate_int=120.0)
    nvme = om.DriveCatalog(drive_type="NVMe", size_tb=1.92)
    nic = om.NicCatalog(description="Test 10GbE", ports=2, speed="10GbE")
    db.session.add_all([cpu, nvme, nic])
    db.session.flush()
    model = om.Model(name="TEST-HCI", status="Active", category="Test",
                     form_factor="1U", chassis="Test", min_nodes=2,
                     cost_tier=5.0, validated_only=False)
    db.session.add(model)
    db.session.flush()
    db.session.add(om.ModelCpuOption(model_id=model.id, cpu_id=cpu.id, quantity=2))
    for gb in (256, 384, 512):
        db.session.add(om.RamOption(model_id=model.id, size_gb=gb))
    db.session.add(om.ModelNicOption(model_id=model.id, nic_id=nic.id, quantity=1))
    sc = om.StorageConfig(model_id=model.id, storage_type="nvme_only",
                          drives_per_node=10)
    db.session.add(sc)
    db.session.flush()
    db.session.add(om.StorageConfigDrive(storage_config_id=sc.id, drive_id=nvme.id))
    db.session.add(om.DriveTypeIops(drive_type="NVMe", iops=100000))
    db.session.commit()


@pytest.fixture()
def client():
    app = appmod.app
    app.config["TESTING"] = True
    app.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    with app.app_context():
        db.drop_all()
        db.create_all()
        _seed_catalog()
    c = app.test_client()
    c.post("/api/auth/signup", json={"email": "alice@examplecorp.com",
                                     "password": "Abcdef1!xy",
                                     "accept_privacy": True})
    return c


def _strip_svg(result):
    for rec in result["recommendations"]:
        rec.pop("network_svg", None)
    return result


# ── parity: route == service == engine ───────────────────────────────────────

@pytest.mark.parametrize("params", [
    {},
    {"vcpu_ratio": 4, "growth_pct": 15, "snapshot_pct": 10, "years": 3},
    {"target_nodes": 4, "size_full_cluster": True, "storage_pref": "flash"},
    {"storage_growth_pct": 25, "max_day_one_storage_pct": 60,
     "max_day_one_ram_pct": 70, "license_term_years": 3,
     "guest_licensing": "windows_db"},
    {"source_perf_index": 412.4, "source_perf_type": "specrate"},
    {"growth_pct": None, "snapshot_pct": None},   # explicit nulls: (x or 0)
])
def test_route_service_and_engine_agree(client, params):
    body = dict(params, summary=SUMMARY)
    res = client.post("/api/recommend", json=body)
    assert res.status_code == 200
    via_route = res.get_json()
    with appmod.app.app_context():
        via_service = _strip_svg(svc.recommend(body))
        summary, kwargs = svc.parse_recommend_request(body)
        via_engine = generate_recommendations(summary, **kwargs)
        via_engine["warnings"] += svc.input_quality_warnings(summary)
        via_engine = _strip_svg(via_engine)
    _strip_svg(via_route)
    assert via_route == via_service == via_engine
    assert via_route["recommendations"], "the fixture catalog must produce a fit"


def test_parse_applies_route_defaults_only_when_absent():
    summary, kwargs = svc.parse_recommend_request({"summary": SUMMARY})
    assert summary is SUMMARY
    assert kwargs["growth_pct"] == 10 and kwargs["snapshot_pct"] == 20
    assert kwargs["years"] == 5 and kwargs["sizing_mode"] == "certified"
    assert kwargs["replication_compute_mode"] == "reserved"
    assert kwargs["vcpu_ratio"] is None and kwargs["storage_growth_pct"] is None
    _, explicit = svc.parse_recommend_request({"summary": SUMMARY, "years": None})
    assert explicit["years"] is None
    assert set(kwargs) == set(svc.RECOMMEND_DEFAULTS)


@pytest.mark.parametrize("body, message", [
    (None, "Invalid request body"),
    ([], "Invalid request body"),
    ({}, "No summary provided"),
    ({"summary": {}}, "No summary provided"),
    ({"summary": "148 vCPUs"}, "No summary provided"),
])
def test_bad_bodies_keep_their_replies(client, body, message):
    res = client.post("/api/recommend", json=body)
    assert res.status_code == 400
    assert res.get_json() == {"error": message}
    with pytest.raises(svc.SizingRequestError) as exc:
        svc.parse_recommend_request(body)
    assert str(exc.value) == message and exc.value.code == "bad_request"


def test_explicit_null_years_still_rejected(client):
    # An explicit null is passed to the engine as given, where min(None, 5)
    # raises: the route answered 400 "malformed" before the extraction and
    # must keep doing so (a default only applies to an ABSENT key).
    res = client.post("/api/recommend", json={"summary": SUMMARY, "years": None})
    assert res.status_code == 400
    assert res.get_json() == {"error": svc.MALFORMED_INPUT_MESSAGE}


def test_malformed_figures_keep_their_reply(client):
    res = client.post("/api/recommend",
                      json={"summary": dict(SUMMARY, total_vcpus="many")})
    assert res.status_code == 400
    assert res.get_json() == {"error": svc.MALFORMED_INPUT_MESSAGE}
    with appmod.app.app_context():
        with pytest.raises(svc.SizingRequestError) as exc:
            svc.recommend({"summary": dict(SUMMARY, total_vcpus="many")})
    assert exc.value.code == "invalid_input" and exc.value.status == 400


def test_catalog_error_is_mapped(client, monkeypatch):
    from storage_layout import StorageLayoutError

    def boom(*a, **k):
        raise StorageLayoutError("TEST-HCI has no drive count")
    monkeypatch.setattr(svc, "generate_recommendations", boom)
    res = client.post("/api/recommend", json={"summary": SUMMARY})
    assert res.status_code == 400
    assert res.get_json() == {"error": "TEST-HCI has no drive count"}
    with appmod.app.app_context():
        with pytest.raises(svc.SizingRequestError) as exc:
            svc.recommend({"summary": SUMMARY})
    assert exc.value.code == "catalog_error"


def test_run_recommendation_appends_input_caveats(client):
    with appmod.app.app_context():
        result = svc.run_recommendation(
            {k: v for k, v in SUMMARY.items() if not k.startswith("max_vm")})
    assert any(w.startswith("Largest VM vCPUs and RAM unknown")
               for w in result["warnings"])


def test_not_fingerprinted():
    assert "sizing_service.py" not in ENGINE_MODULES


# ── cpu lookup and source perf ───────────────────────────────────────────────

def test_cpu_perf_route_uses_the_service(client):
    for q in ("Intel Xeon Silver 4110", "Xeon Gold 6338", "no such cpu zz"):
        res = client.get("/api/cpu-perf", query_string={"q": q})
        assert res.status_code == 200
        assert res.get_json() == svc.lookup_cpu_perf(q)


def test_lookup_cpu_perf_resolves_a_source_cpu():
    hit = svc.lookup_cpu_perf("Intel Xeon Silver 4110")
    assert hit["found"] and hit["perf_index"] > 0
    assert hit["source"] in ("catalog", "spec-cpu2017")
    assert svc.lookup_cpu_perf("") == {"found": False}


def test_source_perf_index_matches_the_browser_arithmetic():
    # computeSourcePerf: (score, PassMark × 0.00386) × sockets, summed, 0.1.
    def lookup(q):
        return {"Xeon A": {"found": True, "perf_index": 100.0,
                           "perf_type": "specrate", "source": "catalog"},
                "Desk B": {"found": True, "perf_index": 20000.0,
                           "perf_type": "passmark", "source": "catalog"}
                }.get(q, {"found": False})
    out = svc.source_perf_index([
        {"model": "Xeon A", "sockets": 6},
        {"model": "Desk B", "sockets": 1},
        {"model": "Given C", "sockets": 2, "perf_index": 50, "perf_type": "specrate"},
        {"model": "Unknown D", "sockets": 2},
    ], lookup=lookup)
    assert out["total_specrate"] == pytest.approx(600 + 77.2 + 100, abs=0.05)
    assert out["unresolved"] == ["Unknown D"]
    assert [c["source"] for c in out["cpus"]] == ["catalog", "catalog", "given"]
    assert svc.source_perf_index([], lookup=lookup)["total_specrate"] is None
    assert svc.source_perf_index([{"model": "Unknown D", "sockets": 1}],
                                 lookup=lookup)["total_specrate"] is None


# ── workload mapping ─────────────────────────────────────────────────────────

def test_workload_to_summary_maps_and_defaults_only_the_nic():
    summary, defaults = svc.workload_to_summary(WORKLOAD)
    assert defaults == ["nic_speed_gbe"]
    assert summary["nic_speed_mbps"] == 10000
    assert summary["total_vm_provisioned_memory_gb"] == 423
    assert summary["max_vm_cores"] == 16 and summary["max_vm_ram_gb"] == 64
    assert summary["vcpu_per_core_ratio"] == round(148 / 72, 2)
    # Absent optionals stay absent (the engine's .get defaults apply), never 0.
    assert "total_avg_iops" not in summary and "peak_cpu_ghz" not in summary
    explicit, defaults = svc.workload_to_summary(dict(WORKLOAD, nic_speed_gbe=25))
    assert defaults == [] and explicit["nic_speed_mbps"] == 25000


def test_workload_summary_round_trip():
    summary, _ = svc.workload_to_summary(dict(
        WORKLOAD, nic_speed_gbe=25, guest_licensing="windows_db",
        source_cpus=[{"model": "Xeon Silver 4110", "sockets": 6}]))
    back = svc.summary_to_workload(summary)
    for field, value in WORKLOAD.items():
        assert back[field] == value
    assert back["nic_speed_gbe"] == 25
    assert back["guest_licensing"] == "windows_db"
    assert back["source_cpus"] == [{"model": "Xeon Silver 4110", "sockets": 6}]


def test_workload_sizes_like_the_equivalent_summary(client):
    summary, _ = svc.workload_to_summary(WORKLOAD)
    with appmod.app.app_context():
        a = _strip_svg(svc.run_recommendation(summary))
        b = _strip_svg(svc.run_recommendation(SUMMARY))
    assert a == b


def test_validate_workload_missing_and_invalid():
    out = svc.validate_workload({"total_vcpus": 148, "provisioned_ram_gb": 423})
    assert out["missing"] == ["used_storage_tb", "largest_vm_vcpus", "largest_vm_ram_gb"]
    out = svc.validate_workload(dict(
        WORKLOAD, largest_vm_vcpus=0, total_vcpus=10.5, peak_cpu_pct=120,
        guest_licensing="linux", cluster_name=3, avg_iops="lots",
        source_cpus=[{"sockets": 2}]))
    problems = {i["field"]: i["problem"] for i in out["invalid"]}
    assert problems["largest_vm_vcpus"] == "must be greater than 0"
    assert problems["total_vcpus"] == "must be a whole number"
    assert problems["peak_cpu_pct"] == "must be between 0 and 100"
    assert problems["guest_licensing"].startswith("must be one of")
    assert problems["cluster_name"] == "must be text"
    assert problems["avg_iops"] == "must be a number"
    assert problems["source_cpus[0].model"] == "must name the CPU"
    assert out["missing"] == [] and out["plausibility"] == []


def test_validate_workload_plausibility_is_advisory():
    assert svc.validate_workload(WORKLOAD) == {
        "missing": [], "invalid": [], "plausibility": []}
    out = svc.validate_workload(dict(
        WORKLOAD, provisioned_ram_gb=148 * 40, largest_vm_ram_gb=148 * 41,
        used_ram_gb=148 * 50, datastore_total_tb=5, provisioned_storage_tb=6, active_vms=200,
        host_count=4, total_host_ghz=2.4))
    codes = {f["code"] for f in out["plausibility"]}
    assert codes == {"ram_per_vcpu_high", "largest_vm_exceeds_total",
                     "used_gt_provisioned_ram", "storage_gt_datastore",
                     "used_gt_provisioned_storage", "vcpus_lt_vms", "ghz_looks_per_cpu"}
    assert out["missing"] == [] and out["invalid"] == []
