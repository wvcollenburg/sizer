"""End-to-end test of perf-based sizing through generate_recommendations().

Builds a minimal in-memory (SQLite) catalog — one all-NVMe HCI model with a
single CPU option — and drives the real engine so the active compute floor,
utilization scaling, the GHz/benchmark balance, the determinant, and the
projection summary are all exercised together. No Postgres/seed needed.

Run:  .venv/bin/python tests/test_perf_sizing_e2e.py
  or  .venv/bin/python -m pytest tests/test_perf_sizing_e2e.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from flask import Flask  # noqa: E402
from database import db  # noqa: E402
import orm_models as om  # noqa: E402
from tunables import DEFAULTS  # noqa: E402
from recommend import generate_recommendations  # noqa: E402


def _build_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


def _seed_catalog():
    # All tunables present so refresh_from_db() reads known values; overridden
    # per-test via _set().
    for k, v in DEFAULTS.items():
        db.session.add(om.SizingSetting(key=k, value=float(v)))

    cpu = om.CpuCatalog(description="Xeon Test 32C", cores=32, threads=64,
                        ghz=2.5, specrate_int=150.0)
    nvme = om.DriveCatalog(drive_type="NVMe", size_tb=8.0)
    nic = om.NicCatalog(description="Test 25GbE", ports=2, speed="25GbE")
    db.session.add_all([cpu, nvme, nic])
    db.session.flush()

    model = om.Model(name="TEST-HCI", status="Active", category="Test",
                     form_factor="1U", chassis="Test", min_nodes=2,
                     cost_tier=5.0, validated_only=False)
    db.session.add(model)
    db.session.flush()

    # 2 sockets -> per node: 64 cores, 128 threads, perf_index 300, ghz/node 160.
    db.session.add(om.ModelCpuOption(model_id=model.id, cpu_id=cpu.id, quantity=2))
    db.session.add(om.RamOption(model_id=model.id, size_gb=512))
    db.session.add(om.ModelNicOption(model_id=model.id, nic_id=nic.id, quantity=1))

    sc = om.StorageConfig(model_id=model.id, storage_type="nvme_only",
                          drives_per_node=6)
    db.session.add(sc)
    db.session.flush()
    db.session.add(om.StorageConfigDrive(storage_config_id=sc.id, drive_id=nvme.id))
    db.session.commit()


def _set(**kv):
    for k, v in kv.items():
        row = om.SizingSetting.query.filter_by(key=k).first()
        row.value = float(v)
    db.session.commit()


# A light workload: core/RAM/storage floors all land at the 2-node minimum, so
# anything above 2 nodes is driven by the perf compute floor alone.
def _summary(total_host_ghz=400, peak_cpu_ghz=200):
    return {
        "total_vcpus": 64,
        "total_vm_provisioned_memory_gb": 200,
        "datastore_used_tb": 5,
        "nic_speed_mbps": 25000,
        "total_host_ghz": total_host_ghz,
        "peak_cpu_ghz": peak_cpu_ghz,
        "max_vm_ram_gb": 0,
        "max_vm_cores": 0,
        "active_vms": 10,
        "p95_iops": 0,
        "total_avg_iops": 0,
    }


def _rec(app, summary=None, **kwargs):
    with app.app_context():
        return generate_recommendations(
            summary or _summary(),
            # years=1, growth=0 -> growth_factor 1, so floors are exact.
            growth_pct=0, snapshot_pct=0, years=1,
            max_day_one_storage_pct=100, max_day_one_ram_pct=100,
            **kwargs)


def run():
    app = _build_app()
    with app.app_context():
        db.create_all()
        _seed_catalog()

    # ── 1. Floor OFF: sizing unchanged, no compute_floor on recs ──────────────
    with app.app_context():
        _set(perf_scaling=0)
    off = _rec(app)
    base_nodes = off["recommendations"][0]["node_count"]
    assert base_nodes == 2, f"expected 2-node minimum when floor off, got {base_nodes}"
    assert off["projection"]["compute_floor"]["active"] is False
    assert off["recommendations"][0]["compute_floor"] is None
    print(f"PASS floor-off: {base_nodes} nodes, advisory only")

    # ── 2. Floor ON, pure GHz (balance 0): utilized GHz pushes node count up ──
    # required_ghz = 400 * 0.5 util * 1 = 200; node ghz/compute-node = 160.
    # N-1: n=2 -> 1 compute -> 0.8 cov; n=3 -> 2 compute -> 1.6 cov -> 3 nodes.
    with app.app_context():
        _set(perf_scaling=1, perf_ghz_balance=0)
    ghz = _rec(app, source_perf_index=300, source_perf_type="specrate")
    top = ghz["recommendations"][0]
    assert top["node_count"] == 3, f"expected GHz floor to force 3 nodes, got {top['node_count']}"
    assert top["determinant"]["resource"] == "Compute"
    cf = top["compute_floor"]
    assert cf is not None and cf["coverage_pct"] >= 100
    assert cf["ghz_pct"] is not None and cf["perf_pct"] is not None
    assert ghz["projection"]["compute_floor"]["active"] is True
    print(f"PASS floor-on (pure GHz): {top['node_count']} nodes, "
          f"coverage {cf['coverage_pct']}% (determinant={top['determinant']['resource']})")

    # ── 3. Floor ON, pure benchmark (balance 1): strong SPECrate needs fewer ──
    # required_perf = 300 * 0.5 * 1 = 150; node perf = 300. n=2 -> 1 compute ->
    # cov 2.0 -> 2 nodes (back to minimum).
    with app.app_context():
        _set(perf_scaling=1, perf_ghz_balance=1)
    bench = _rec(app, source_perf_index=300, source_perf_type="specrate")
    bn = bench["recommendations"][0]["node_count"]
    assert bn == 2, f"expected benchmark-led floor to fit in 2 nodes, got {bn}"
    print(f"PASS floor-on (pure benchmark): {bn} nodes (vs {ghz['recommendations'][0]['node_count']} GHz-led)")

    # ── 4. Balance blends the two (0.5 still fits 2 nodes here) ───────────────
    with app.app_context():
        _set(perf_scaling=1, perf_ghz_balance=0.5)
    blend = _rec(app, source_perf_index=300, source_perf_type="specrate")
    bln = blend["recommendations"][0]["node_count"]
    # blended at n=2,comp1 = 0.5*0.8 + 0.5*2.0 = 1.4 >= 1 -> 2 nodes.
    assert bln == 2, f"expected blended floor to fit 2 nodes, got {bln}"
    print(f"PASS floor-on (balanced 0.5): {bln} nodes")

    # ── 5. Utilization scaling: lower measured peak -> lower floor ────────────
    # Pure GHz again, but source peak only 25% utilized (peak_cpu_ghz=100):
    # required_ghz = 400*0.25 = 100; n=2 -> 1 compute -> 160/100 = 1.6 -> 2 nodes.
    with app.app_context():
        _set(perf_scaling=1, perf_ghz_balance=0)
    low_util = _rec(app, summary=_summary(total_host_ghz=400, peak_cpu_ghz=100))
    lun = low_util["recommendations"][0]["node_count"]
    assert lun == 2, f"expected low-utilization to relax floor to 2 nodes, got {lun}"
    assert low_util["projection"]["compute_floor"]["source_cpu_util_pct"] == 25.0
    print(f"PASS utilization scaling: 25%-utilized source -> {lun} nodes "
          f"(vs {ghz['recommendations'][0]['node_count']} at 50%)")

    # ── 6. Advisory comparison is benchmark-vs-benchmark, NOT util-scaled ──────
    # It's a "in a benchmark" display, so both sides are rated SPECrate: the
    # source's measured utilization must NOT discount it here (that belongs in the
    # floor). nameplate 300 -> ratio = target/300, regardless of the 50% util.
    pc = bench["perf_comparison"]
    assert pc is not None
    assert pc["source_index_specrate"] == 300.0
    assert "source_index_utilized" not in pc and "source_cpu_util_pct" not in pc
    expected_ratio = round(pc["target_index"] / 300.0, 2)
    assert pc["ratio"] == expected_ratio, f"{pc['ratio']} != {expected_ratio}"
    print(f"PASS advisory benchmark-vs-benchmark: source {pc['source_index_specrate']}, "
          f"target {pc['target_index']}, ratio {pc['ratio']}× (util ignored here)")

    print("\nALL E2E CHECKS PASSED")


if __name__ == "__main__":
    run()
