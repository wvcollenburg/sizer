"""Unit tests for import_checks.build_import_warnings — the data-quality caveats
shown on the guided-import wizard's Environment step.

Run from the repo root:
    .venv/bin/python -m pytest tests/test_import_warnings.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from import_checks import build_import_warnings  # noqa: E402


def codes(data, file_type="liveoptics"):
    return {w["code"] for w in build_import_warnings(data, file_type)}


def _full_summary(**over):
    """A 'clean' import summary that trips no warnings; override to break it."""
    s = {
        "total_avg_iops": 5000, "total_peak_iops": 9000,
        "peak_cpu_ghz": 120.0, "avg_cpu_ghz": 60.0,
        "datastore_total_tb": 40.0, "local_total_tb": 0.0,
        "vcpu_per_core_ratio": 3.0,
    }
    s.update(over)
    return s


def test_clean_import_has_no_warnings():
    data = {"summary": _full_summary(), "hosts": [{"cluster": "A"}, {"cluster": "A"}]}
    assert codes(data) == set()


def test_no_iops_flagged():
    data = {"summary": _full_summary(total_avg_iops=0, total_peak_iops=0), "hosts": []}
    assert "no_iops" in codes(data)


def test_no_perf_flagged():
    data = {"summary": _full_summary(peak_cpu_ghz=0, avg_cpu_ghz=0), "hosts": []}
    assert "no_perf" in codes(data)


def test_assumed_ratio_flagged_with_rounded_ratio():
    data = {"summary": _full_summary(vcpu_ratio_assumed=True, vcpu_per_core_ratio=3.0), "hosts": []}
    ws = build_import_warnings(data)
    ar = [w for w in ws if w["code"] == "assumed_ratio"]
    assert ar and ar[0]["params"]["ratio"] == 3


def test_mixed_unclustered_hosts_flagged_with_count():
    data = {"summary": _full_summary(),
            "hosts": [{"cluster": "A"}, {"cluster": ""}, {"cluster": None}]}
    ws = build_import_warnings(data)
    uc = [w for w in ws if w["code"] == "unclustered_hosts"]
    assert uc and uc[0]["params"]["count"] == 2


def test_all_unclustered_is_not_flagged():
    # A physical/general scan where no host has a cluster is normal, not a caveat.
    data = {"summary": _full_summary(), "hosts": [{"cluster": ""}, {"cluster": ""}]}
    assert "unclustered_hosts" not in codes(data)


def test_local_only_storage_flagged():
    data = {"summary": _full_summary(datastore_total_tb=0.0, local_total_tb=12.0), "hosts": []}
    assert "local_only_storage" in codes(data)


def test_local_storage_not_flagged_when_cluster_storage_present():
    data = {"summary": _full_summary(datastore_total_tb=40.0, local_total_tb=12.0), "hosts": []}
    assert "local_only_storage" not in codes(data)


def test_robust_on_empty_and_partial_data():
    assert build_import_warnings({}) == [] or isinstance(build_import_warnings({}), list)
    # Missing summary keys must not raise.
    build_import_warnings({"summary": {}, "hosts": [{}]})


if __name__ == "__main__":
    import traceback
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for f in funcs:
        try:
            f(); print(f"PASS {f.__name__}")
        except Exception:
            failed += 1; print(f"FAIL {f.__name__}"); traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
