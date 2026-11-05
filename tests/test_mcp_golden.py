"""Golden test: the worked example of the "How to Size a Cluster" field guide
(§13, _archive/how-to-size-a-cluster.pdf), sized through the MCP create_sizing
tool (docs/plan_enableMCP_sizingtool.md Phase 5.2).

The guide's inputs: 148 vCPUs, 423 GB RAM, 8.2 TB used storage, 59 active VMs,
~16,000 P95 IOPS, 6 sockets of Xeon Silver 4110, defaults (3:1, 10%/yr for 5
years, 20% snapshots). Its node: 48 cores, 384 GB, ten 1.92 TB NVMe, ~425k net
IOPS, ~357 SPECrate per node (1071 for three). The catalog below is that node,
so every figure the guide derives can be held against the engine's.

Where the engine and the guide's arithmetic part ways, the engine is right and
the test says why:
  - storage: the engine grows the snapshot reserve with the data (20% today,
    32.2% at year 5), the guide keeps it flat, so 17.46 TB, not ~16 TB;
  - RAM: the guide's own 50% day-one RAM cap (constants, §15) rules out the
    384 GB node it picks in step 3 (423 GB is 56.6% of 2 x 374 GB), so at
    defaults the engine takes 512 GB; with the cap relaxed it is 384 GB, and
    RAM (9.8% headroom) then out-tightens CPU (17.5%) as the determinant.

Run: .venv/bin/python -m pytest tests/test_mcp_golden.py -q
"""
import math
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
import orm_models as om  # noqa: E402
from auth_models import User  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from test_mcp_server import call  # noqa: E402
from tunables import DEFAULTS  # noqa: E402

GUIDE = {"total_vcpus": 148, "provisioned_ram_gb": 423, "used_storage_tb": 8.2,
         "active_vms": 59, "p95_iops": 16000,
         "largest_vm_vcpus": 16, "largest_vm_ram_gb": 64,
         "source_cpus": [{"model": "Intel Xeon Silver 4110", "sockets": 6}]}


def _seed_guide_node():
    for k, v in DEFAULTS.items():
        db.session.add(om.SizingSetting(key=k, value=float(v)))
    # 178.5 SPECrate per socket: 2 sockets x 3 nodes = the guide's 1071.
    cpu = om.CpuCatalog(description="Xeon Guide 24C", cores=24, threads=48,
                        ghz=2.4, specrate_int=178.5)
    nvme = om.DriveCatalog(drive_type="NVMe", size_tb=1.92)
    nic = om.NicCatalog(description="Guide 25GbE", ports=2, speed="25GbE")
    db.session.add_all([cpu, nvme, nic])
    db.session.flush()
    model = om.Model(name="GUIDE-48C", status="Active", category="Guide",
                     form_factor="1U", chassis="Guide", min_nodes=2,
                     cost_tier=5.0, validated_only=False)
    db.session.add(model)
    db.session.flush()
    db.session.add(om.ModelCpuOption(model_id=model.id, cpu_id=cpu.id, quantity=2))
    for gb in (384, 512):
        db.session.add(om.RamOption(model_id=model.id, size_gb=gb))
    db.session.add(om.ModelNicOption(model_id=model.id, nic_id=nic.id, quantity=1))
    sc = om.StorageConfig(model_id=model.id, storage_type="nvme_only", drives_per_node=10)
    db.session.add(sc)
    db.session.flush()
    db.session.add(om.StorageConfigDrive(storage_config_id=sc.id, drive_id=nvme.id))
    # 85k per drive: ten drives net the guide's ~425k per node.
    db.session.add(om.DriveTypeIops(drive_type="NVMe", iops=85000))
    db.session.commit()


@pytest.fixture()
def client():
    app = appmod.app
    app.config["TESTING"] = True
    app.config["RATELIMIT_ENABLED"] = False
    app.config.pop("MCP_TEST_USER_ID", None)
    limiter.enabled = False
    with app.app_context():
        db.drop_all()
        db.create_all()
        _seed_guide_node()
    c = app.test_client()
    c.post("/api/auth/signup", json={"email": "alice@examplecorp.com",
                                     "password": "Abcdef1!xy", "accept_privacy": True})
    with app.app_context():
        app.config["MCP_TEST_USER_ID"] = User.query.filter_by(email="alice@examplecorp.com").one().id
    yield c
    app.config.pop("MCP_TEST_USER_ID", None)


def _size(client, params=None):
    args = {"workload": GUIDE}
    if params:
        args["params"] = params
    r = call(client, "create_sizing", args)
    assert r["isError"] is False, r
    return r["structuredContent"]


def test_guide_worked_example(client):
    s = _size(client)
    assert s["inputs_used"]["params"]["vcpu_ratio"] == 3.0
    assert s["inputs_used"]["params"]["snapshot_pct"] == 20

    # Step 1: growth factor 1.1^5 = 1.61 -> ~238 vCPUs, ~680 GB.
    horizon = s["projection"]["at_horizon"]
    assert horizon["vcpus"] == math.ceil(148 * 1.1 ** 5)               # 238.4 -> 239
    assert horizon["ram_gb"] == pytest.approx(681.2, abs=0.1)
    # The snapshot reserve compounds with storage: 8.2 x 1.61 x (1 + 0.2 x 1.61).
    assert horizon["snapshot_pct"] == pytest.approx(32.2, abs=0.1)
    assert horizon["storage_tb"] == pytest.approx(17.46, abs=0.01)

    top = s["options"][0]
    assert (top["model"], top["node_count"]) == ("GUIDE-48C", 3)

    # Step 2: 238 / 3.0 -> 80 cores; 47 usable per node, 94 at N-1, 17.5%.
    assert top["node"]["cores"] == 48 and top["node"]["usable_cores"] == 47
    cpu = top["capacity"]["cpu"]
    assert (cpu["required"], cpu["available"], cpu["basis"]) == (80, 94, "n_minus_1")
    assert cpu["headroom_pct"] == pytest.approx(17.5, abs=0.05)

    # Step 4: (57.6 - 1.92) / 2 = 27.84 TB usable, under the 50% day-one cap.
    stor = top["capacity"]["storage"]
    assert stor["available"] == pytest.approx(27.84, abs=0.01)
    assert stor["available"] >= stor["required"]
    assert {c["id"]: c["status"] for c in top["checks"]}["day_one_storage"] == "pass"

    # Step 5: ~425k net per node; two at N-1 ~850k against 16k.
    iops = top["capacity"]["iops"]
    assert iops["available_n_minus_1"] == pytest.approx(850000, rel=0.01)
    assert iops["required"] == 16000

    # Step 6: CPU set the node count.
    assert top["determinant"]["resource"] == "CPU"
    assert top["determinant"]["statement"].startswith("Determined by CPU: 80 cores")

    # Step 7: 1071 vs 222 -> ~4.8x.
    pc = s["perf_comparison"]
    assert pc["source_specrate"] == pytest.approx(222, abs=0.5)
    assert pc["target_specrate"] == pytest.approx(1071, abs=0.5)
    assert pc["ratio"] == pytest.approx(4.8, abs=0.05)


def test_guide_ram_step(client):
    # At defaults the 50% day-one RAM cap (guide §15) needs 2 x 502 GB at N-1.
    top = _size(client)["options"][0]
    assert top["node"]["ram_gb"] == 512 and top["node"]["usable_ram_gb"] == 502
    checks = {c["id"]: c for c in top["checks"]}
    assert checks["day_one_ram"]["status"] == "pass"
    # Step 3 as the guide does it, cap aside: 680 / 2 = 340 GB per node at
    # N-1; a 384 GB node (374 usable) clears it.
    top = _size(client, {"max_day_one_ram_pct": 60})["options"][0]
    assert top["node"]["ram_gb"] == 384 and top["node"]["usable_ram_gb"] == 374
    ram = top["capacity"]["ram"]
    assert ram["available"] == 748 and ram["required"] / 2 <= 374
    # Still three nodes, but 681.2 of 748 GB (9.8% headroom) is now tighter
    # than CPU's 17.5%, and the engine names the tightest resource.
    assert top["node_count"] == 3
    assert ram["headroom_pct"] == pytest.approx(9.8, abs=0.05)
    assert top["determinant"]["resource"] == "RAM"


def test_rest_route_agrees(client):
    s = _size(client)
    summary = {"total_vcpus": 148, "total_vm_provisioned_memory_gb": 423,
               "datastore_used_tb": 8.2, "nic_speed_mbps": 10000, "active_vms": 59,
               "p95_iops": 16000, "max_vm_cores": 16, "max_vm_ram_gb": 64}
    via_route = client.post("/api/recommend", json={
        "summary": summary, "source_perf_index": 222.0,
        "source_perf_type": "specrate"}).get_json()
    rec = via_route["recommendations"][0]
    top = s["options"][0]
    assert (rec["model"], rec["node_count"]) == (top["model"], top["node_count"])
    assert rec["determinant"]["resource"] == "CPU"
    assert via_route["perf_comparison"]["ratio"] == pytest.approx(s["perf_comparison"]["ratio"])
