"""Export capacity table: vCPU headroom is measured at the TARGET ratio.

The deck's "Headroom" cell multiplied the proposed cores by the ratio the
cluster achieves, which is the projected demand by definition, so it always
read "+0 vCPU capacity". The DOCX "fits within projected growth" sentence
made the same comparison, so its CPU side always passed.

Run: .venv/bin/python -m pytest tests/test_export_headroom.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from export_format import spare_vcpus  # noqa: E402


def _rec(n1_cores, total_cores, required_cores, full=False):
    return {"sized_full_cluster": full,
            "n_minus_1": {"cores": n1_cores}, "totals": {"cores": total_cores},
            "vcpu_ratio": 2.61,
            "utilization": {"cpu": {"abs": {"total": required_cores}}}}


def test_spare_vcpus_at_the_target_ratio():
    # 60 vCPUs at 3:1 need 20 cores; 23 at N-1 leave 3 cores = 9 vCPUs.
    assert spare_vcpus(_rec(23, 46, 20), 60) == 9
    # Exactly at target (14 required, 14 usable at N-1): genuinely 0.
    assert spare_vcpus(_rec(14, 21, 14), 42) == 0
    # Sized across the full cluster: the basis is every node's cores.
    assert spare_vcpus(_rec(23, 46, 20, full=True), 60) == 78


def test_older_results_fall_back():
    rec = _rec(23, 46, 20)
    del rec["utilization"]
    assert spare_vcpus(rec, 60) is None
