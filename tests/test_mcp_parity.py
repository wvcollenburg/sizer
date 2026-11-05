"""Parity over a set of fixtures (docs/plan_enableMCP_sizingtool.md Phase 5.1):
MCP create_sizing, POST /api/recommend and generate_recommendations return
the same recommendations for the same inputs.

The catalog has three models (all-flash, hybrid, a small single-socket box)
so ranking and storage filters have something to choose between. Manual
workloads run under several parameter sets; assessment files (RVTools and
Live Optics, built by merge_fixtures) run with exclusion sets through
from_upload and are held against the browser's own path, import_service's
adjust_summary fed to /api/recommend.

Compared per option, in rank order: model, node count, CPU, RAM, storage,
determinant and every capacity figure. MCP may add warnings of its own (the
assumed NIC speed, unmeasured fields), never drop the engine's.

Run: .venv/bin/python -m pytest tests/test_mcp_parity.py -q
"""
import io
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
import import_service  # noqa: E402
import orm_models as om  # noqa: E402
import sizing_service as svc  # noqa: E402
from auth_models import User  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from mcp_tools import PARAM_TO_ENGINE  # noqa: E402
from merge_fixtures import host, liveoptics, rvtools, vm  # noqa: E402
from recommend import generate_recommendations  # noqa: E402
from test_mcp_server import call  # noqa: E402
from test_sizing_service import _seed_catalog  # noqa: E402

WORKLOADS = {
    "guide": {"total_vcpus": 148, "provisioned_ram_gb": 423, "used_storage_tb": 8.2,
              "largest_vm_vcpus": 16, "largest_vm_ram_gb": 64, "active_vms": 59,
              "p95_iops": 16000, "nic_speed_gbe": 10},
    "small": {"total_vcpus": 24, "provisioned_ram_gb": 96, "used_storage_tb": 1.5,
              "largest_vm_vcpus": 4, "largest_vm_ram_gb": 16, "active_vms": 9,
              "nic_speed_gbe": 1},
    "storage_heavy": {"total_vcpus": 60, "provisioned_ram_gb": 256, "used_storage_tb": 40,
                      "largest_vm_vcpus": 8, "largest_vm_ram_gb": 32, "active_vms": 20,
                      "p95_iops": 4000, "nic_speed_gbe": 25},
    "big": {"total_vcpus": 900, "provisioned_ram_gb": 3600, "used_storage_tb": 30,
            "largest_vm_vcpus": 32, "largest_vm_ram_gb": 256, "active_vms": 310,
            "p95_iops": 60000, "nic_speed_gbe": 25,
            "total_host_cores": 320, "total_host_ghz": 768},
}
PARAMS = [
    {},
    {"vcpu_ratio": 4, "growth_pct": 15, "snapshot_pct": 10, "years": 3},
    {"storage_preference": "hybrid"},
    {"target_nodes": 4, "size_cpu_for_full_cluster": True},
    {"storage_growth_pct": 25, "max_day_one_storage_pct": 60, "max_day_one_ram_pct": 70,
     "license_term_years": 3},
    {"allow_storage_only_nodes": True, "storage_preference": "flash"},
]


def _seed_more():
    """A hybrid 2U and a single-socket 1U next to _seed_catalog's TEST-HCI."""
    cpu16 = om.CpuCatalog(description="Xeon Test 16C", cores=16, threads=32,
                          ghz=2.8, specrate_int=95.0)
    hdd = om.DriveCatalog(drive_type="HDD", size_tb=8.0)
    ssd = om.DriveCatalog(drive_type="SSD", size_tb=1.92)
    db.session.add_all([cpu16, hdd, ssd])
    db.session.flush()
    nic = om.NicCatalog.query.first()
    cpu24 = om.CpuCatalog.query.filter_by(description="Xeon Test 24C").one()
    nvme = om.DriveCatalog.query.filter_by(drive_type="NVMe").one()

    def model(name, cpu, qty, rams, storage_type, drives, tier, **counts):
        m = om.Model(name=name, status="Active", category="Test", form_factor="1U",
                     chassis="Test", min_nodes=1, cost_tier=tier, validated_only=False)
        db.session.add(m)
        db.session.flush()
        db.session.add(om.ModelCpuOption(model_id=m.id, cpu_id=cpu.id, quantity=qty))
        for gb in rams:
            db.session.add(om.RamOption(model_id=m.id, size_gb=gb))
        db.session.add(om.ModelNicOption(model_id=m.id, nic_id=nic.id, quantity=1))
        sc = om.StorageConfig(model_id=m.id, storage_type=storage_type, **counts)
        db.session.add(sc)
        db.session.flush()
        for d in drives:
            db.session.add(om.StorageConfigDrive(storage_config_id=sc.id, drive_id=d.id))

    model("TEST-HYB", cpu24, 2, (256, 512, 768), "hybrid", (hdd, ssd), 4.0,
          hdd_count=9, ssd_count=3)
    model("TEST-1S", cpu16, 1, (128, 256), "nvme_only", (nvme,), 3.0, drives_per_node=4)
    db.session.add_all([om.DriveTypeIops(drive_type="HDD", iops=250),
                        om.DriveTypeIops(drive_type="SSD", iops=60000)])
    db.session.commit()


@pytest.fixture(scope="module")
def client():
    app = appmod.app
    app.config["TESTING"] = True
    app.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    with app.app_context():
        db.drop_all()
        db.create_all()
        _seed_catalog()
        _seed_more()
    c = app.test_client()
    c.post("/api/auth/signup", json={"email": "alice@examplecorp.com",
                                     "password": "Abcdef1!xy", "accept_privacy": True})
    with app.app_context():
        app.config["MCP_TEST_USER_ID"] = User.query.filter_by(email="alice@examplecorp.com").one().id
    yield c
    app.config.pop("MCP_TEST_USER_ID", None)


def _engine_kwargs(params):
    kwargs = dict(svc.RECOMMEND_DEFAULTS)
    for field, value in params.items():
        kwargs[PARAM_TO_ENGINE[field]] = value
    if kwargs["storage_pref"] == "auto":
        kwargs["storage_pref"] = None
    return kwargs


def _rec_key(rec):
    return (rec["model"], rec["node_count"], rec["cpu"], rec["ram_per_node_gb"],
            (rec.get("storage_config") or {}).get("desc"), rec["determinant"]["resource"])


def _opt_key(opt):
    n = opt["node"]
    return (opt["model"], opt["node_count"], n["cpu"], n["ram_gb"], n["storage"],
            opt["determinant"]["resource"])


def _assert_same(mcp, route, engine):
    recs = route["recommendations"]
    assert [_rec_key(r) for r in recs] == [_rec_key(r) for r in engine["recommendations"]]
    assert mcp["options_total"] == len(recs)
    assert [_opt_key(o) for o in mcp["options"]] == [_rec_key(r) for r in recs[:len(mcp["options"])]]
    for opt, rec in zip(mcp["options"], recs):
        d = rec["determinant"]
        assert opt["determinant"]["required"] == d["required"]
        assert opt["determinant"]["achieved"] == d["achieved"]
        assert opt["capacity"]["storage"]["available"] == pytest.approx(
            rec["totals"]["usable_storage_tb"], abs=0.01)
        assert opt["capacity"]["ram"]["available"] == rec["n_minus_1"]["ram_gb"]
    for w in route["warnings"]:
        assert w in mcp["warnings"], w


@pytest.mark.parametrize("params", PARAMS, ids=lambda p: ",".join(p) or "defaults")
@pytest.mark.parametrize("name", sorted(WORKLOADS))
def test_manual_workload_parity(client, name, params):
    workload = WORKLOADS[name]
    r = call(client, "create_sizing", {"workload": workload, "params": params, "max_options": 8})
    assert r["isError"] is False, r["content"][0]["text"]
    summary = svc.workload_to_summary(workload)[0]
    kwargs = _engine_kwargs(params)
    route = client.post("/api/recommend", json=dict(kwargs, summary=summary)).get_json()
    with appmod.app.app_context():
        engine = generate_recommendations(summary, **kwargs)
    _assert_same(r["structuredContent"], route, engine)


# ── assessment files through from_upload ─────────────────────────────────────

HOSTS = [host("esx1", "Prod", cores=24), host("esx2", "Prod", cores=24),
         host("esx3", "Dev", cores=16)]
VMS = [vm("web01", "esx1", "Prod", vcpus=4, ram_gb=16, used_gb=120),
       vm("web02", "esx1", "Prod", vcpus=4, ram_gb=16, used_gb=110),
       vm("db01", "esx2", "Prod", vcpus=16, ram_gb=128, used_gb=900),
       vm("app01", "esx2", "Prod", vcpus=8, ram_gb=32, used_gb=300),
       vm("old01", "esx2", "Prod", vcpus=2, ram_gb=8, used_gb=50, on=False),
       vm("dev01", "esx3", "Dev", vcpus=6, ram_gb=24, used_gb=200),
       vm("dev02", "esx3", "Dev", vcpus=2, ram_gb=4, used_gb=40)]
DATASTORES = [("ds-prod", 8192, 2048), ("ds-dev", 2048, 512)]
EXCLUSIONS = [((), ()), ((2,), (2,)), ((0, 5), ()), ((), (3, 4)), ((1, 3, 6), (1, 6))]


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("parity")
    return {"rvtools": rvtools(str(d / "rv.xlsx"), HOSTS, VMS, DATASTORES),
            "liveoptics": liveoptics(str(d / "lo.xlsx"), HOSTS, VMS,
                                     [("ds-prod", 8192, 2048, ["esx1", "esx2"]),
                                      ("ds-dev", 2048, 512, ["esx3"])])}


def _upload(client, path):
    s = call(client, "start_upload", {"kind": "assessment"})["structuredContent"]
    token = s["upload_url"].split("/mcp/upload/", 1)[1]
    with open(path, "rb") as fh:
        r = client.post("/mcp/upload/" + token,
                        data={"file": (io.BytesIO(fh.read()), os.path.basename(path))},
                        content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    return s["upload_id"]


def _browser_source_perf(client, summary):
    """What the browser sends as source_perf_index for an import: each
    source CPU looked up on /api/cpu-perf, PassMark scaled to SPECrate, times
    its sockets, summed (app.js computeSourcePerf)."""
    total = 0.0
    for cpu in summary.get("source_cpus") or []:
        hit = client.get("/api/cpu-perf", query_string={"q": cpu["model"]}).get_json()
        assert hit.get("found"), cpu["model"]
        score = hit["perf_index"] * (svc.PASSMARK_TO_SPECRATE if hit["perf_type"] == "passmark" else 1)
        total += score * (cpu.get("sockets") or 1)
    return round(total, 1) or None


@pytest.mark.parametrize("excl", EXCLUSIONS, ids=lambda e: "c%s-s%s" % e)
@pytest.mark.parametrize("kind", ["rvtools", "liveoptics"])
def test_upload_parity(client, files, kind, excl):
    compute, storage = excl
    uid = _upload(client, files[kind])
    r = call(client, "create_sizing", {"from_upload": {
        "upload_id": uid, "exclude_compute": list(compute),
        "exclude_storage": list(storage)}, "max_options": 8})
    assert r["isError"] is False, r["content"][0]["text"]
    data = import_service.parse_assessment(files[kind], os.path.basename(files[kind]))
    summary = import_service.adjust_summary(data["summary"], data["vms"], compute, storage)
    kwargs = dict(svc.RECOMMEND_DEFAULTS, guest_licensing=summary.get("guest_licensing"),
                  source_perf_index=_browser_source_perf(client, summary),
                  source_perf_type="specrate")
    route = client.post("/api/recommend", json=dict(kwargs, summary=summary)).get_json()
    with appmod.app.app_context():
        engine = generate_recommendations(summary, **kwargs)
    _assert_same(r["structuredContent"], route, engine)
