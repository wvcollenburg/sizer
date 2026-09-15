""""Size CPU for full cluster" relaxes CPU only.

RAM is still sized so the workload fits with a node down (N-1), so the RAM
utilization bar must keep showing the capacity held back for failover. It used
to drop to zero with the CPU band, which read as if RAM had lost N-1 too.

Run: .venv/bin/python -m pytest tests/test_full_cluster_ha.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import test_perf_sizing_e2e as e2e  # noqa: E402
from database import db  # noqa: E402
from recommend import generate_recommendations  # noqa: E402


def _recs(size_full_cluster):
    application = e2e._build_app()
    with application.app_context():
        db.create_all()
        e2e._seed_catalog()
        e2e._set(perf_scaling=0)
        return generate_recommendations(
            dict(e2e._summary(), total_vcpus=400, total_vm_provisioned_memory_gb=600),
            3.0, growth_pct=0, snapshot_pct=0, years=1,
            max_day_one_storage_pct=100, max_day_one_ram_pct=100,
            size_full_cluster=size_full_cluster)["recommendations"]


def test_full_cluster_keeps_the_ram_failover_band():
    for rec in _recs(True):
        u = rec["utilization"]
        n = rec["hci_node_count"]
        clusters = rec["num_clusters"]
        # CPU is sized on all nodes: nothing held back for it.
        assert u["cpu"]["ha_reserve"] == 0
        # RAM still holds one node per cluster back.
        assert u["ram"]["ha_reserve"] == round(clusters / n * 100), rec["node_count"]
        assert u["ram"]["ha_reserve"] > 0


def test_n1_sizing_shows_both_failover_bands():
    for rec in _recs(False):
        u = rec["utilization"]
        assert u["cpu"]["ha_reserve"] > 0 and u["ram"]["ha_reserve"] > 0
