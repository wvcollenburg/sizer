"""The "raise the ratio" hint under an unreachable node-count target.

The hint names the lowest vCPU:core ratio at which SOME configuration fits the
target. It was computed only from the de-duplicated result list, which keeps
one CPU option per product per node count — so when a smaller CPU won the
ranking at the next node count, the bigger CPU that would actually fit the
target at a lower ratio was already gone, and the hint overstated the ratio
(seen: "raise to 4.5:1" where a 2 x 32C box fits 3 nodes from 3.6:1).

Run: .venv/bin/python -m pytest tests/test_target_hint.py -q
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import test_perf_sizing_e2e as e2e  # noqa: E402
import orm_models as om  # noqa: E402
from database import db  # noqa: E402
from recommend import generate_recommendations  # noqa: E402


def test_hint_uses_the_cpu_option_that_fits_not_the_one_that_ranked():
    application = e2e._build_app()
    with application.app_context():
        db.create_all()
        e2e._seed_catalog()
        # Give TEST-HCI a smaller CPU as well. With the OS reserving one core
        # per node: 2 x 32C = 63 usable, 2 x 24C = 47 usable.
        model = om.Model.query.filter_by(name="TEST-HCI").one()
        cpu32 = om.CpuCatalog.query.one()
        cpu32.cores, cpu32.threads = 32, 64
        cpu24 = om.CpuCatalog(description="Xeon Test 24C", cores=24, threads=48, ghz=2.5)
        db.session.add(cpu24)
        db.session.flush()
        db.session.add(om.ModelCpuOption(model_id=model.id, cpu_id=cpu24.id, quantity=2))
        db.session.commit()
        e2e._set(perf_scaling=0)

        # 448 vCPUs at 3.5:1 = 128 cores at N-1. Three nodes leave two in
        # service: 2 x 63 = 126 falls 2 cores short, so 3 nodes is infeasible,
        # and the 32C box needs 448 / 126 = 3.56 -> 3.75:1 (0.25 steps).
        # The 24C box would need 448 / 94 = 4.77 -> 5:1.
        summary = dict(e2e._summary(), total_vcpus=448,
                       total_vm_provisioned_memory_gb=300, datastore_used_tb=2)
        result = generate_recommendations(
            summary, 3.5, growth_pct=0, snapshot_pct=0, years=1,
            max_day_one_storage_pct=100, max_day_one_ram_pct=100, target_nodes=3)

    assert all(r["node_count"] > 3 for r in result["recommendations"])
    hints = [re.search(r"ratio to ([\d.]+):1", w) for w in result["warnings"]]
    ratios = [float(m.group(1)) for m in hints if m]
    assert ratios == [3.75], result["warnings"]
