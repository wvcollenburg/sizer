"""Unit tests for the perf-based active compute floor (recommend.py).

These exercise the pure helpers that implement perf-based sizing — utilization
derivation, blended GHz/benchmark coverage, and the resulting node floor — so
the core math is covered without standing up Flask/DB. Run from the repo root:

    .venv/bin/python -m pytest tests/test_compute_floor.py -q

or directly:

    .venv/bin/python tests/test_compute_floor.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from recommend import (  # noqa: E402
    _source_cpu_util, _compute_coverage, _compute_floor_nodes,
)


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


# ── _source_cpu_util ─────────────────────────────────────────────────────────

def test_util_from_peak_ghz_ratio():
    # Import path: peak_cpu_ghz / total_host_ghz is the measured utilization.
    u = _source_cpu_util({"peak_cpu_ghz": 250, "total_host_ghz": 1000})
    assert approx(u, 0.25)


def test_util_from_peak_pct_when_no_peak_ghz():
    # Manual path carries peak_cpu_pct but no peak_cpu_ghz.
    u = _source_cpu_util({"total_host_ghz": 1000, "peak_cpu_pct": 40})
    assert approx(u, 0.40)


def test_util_defaults_to_full_when_unknown():
    # No measured signal -> conservative 100% (nameplate).
    assert approx(_source_cpu_util({}), 1.0)


def test_util_clamped_to_one():
    u = _source_cpu_util({"peak_cpu_ghz": 1200, "total_host_ghz": 1000})
    assert approx(u, 1.0)


# ── _compute_coverage (the blend) ────────────────────────────────────────────

def test_coverage_blends_ghz_and_perf_equally():
    # balance 0.5: blended = 0.5*ghz_cov + 0.5*perf_cov.
    # ghz: 100*4/1000 = 0.4 ; perf: 50*4/100 = 2.0 ; blended = 1.2
    cov = _compute_coverage(ghz_per_node=100, node_perf=50,
                            req_ghz=1000, req_perf=100,
                            balance=0.5, comp_nodes=4)
    assert approx(cov["ghz"], 0.4)
    assert approx(cov["perf"], 2.0)
    assert approx(cov["blended"], 1.2)
    assert approx(cov["w_ghz"], 0.5) and approx(cov["w_perf"], 0.5)


def test_balance_zero_is_pure_ghz():
    cov = _compute_coverage(100, 50, 1000, 100, balance=0.0, comp_nodes=4)
    assert approx(cov["blended"], 0.4)  # only the GHz side counts


def test_balance_one_is_pure_benchmark():
    cov = _compute_coverage(100, 50, 1000, 100, balance=1.0, comp_nodes=4)
    assert approx(cov["blended"], 2.0)  # only the benchmark side counts


def test_missing_benchmark_degrades_to_ghz():
    # No source benchmark demand -> weight renormalises onto GHz despite balance.
    cov = _compute_coverage(100, node_perf=50, req_ghz=1000, req_perf=0,
                            balance=0.7, comp_nodes=4)
    assert cov["perf"] is None
    assert approx(cov["w_ghz"], 1.0) and approx(cov["w_perf"], 0.0)
    assert approx(cov["blended"], 0.4)


def test_missing_node_perf_degrades_to_ghz():
    cov = _compute_coverage(100, node_perf=None, req_ghz=1000, req_perf=100,
                            balance=0.7, comp_nodes=4)
    assert cov["perf"] is None
    assert approx(cov["blended"], 0.4)


def test_no_demand_returns_none():
    assert _compute_coverage(100, 50, 0, 0, balance=0.5, comp_nodes=4) is None


# ── _compute_floor_nodes ─────────────────────────────────────────────────────

def test_floor_finds_smallest_n_meeting_blended_coverage_full_cluster():
    # Pure GHz (balance 0): need ghz_per_node*n >= req_ghz, full cluster (comp=n).
    # 100*n >= 1000 -> n=10, but min_nodes raises the floor when below.
    n = _compute_floor_nodes(ghz_per_node=100, node_perf=None,
                             req_ghz=1000, req_perf=0, balance=0.0,
                             min_nodes=2, full_cluster=True)
    assert n == 10


def test_floor_accounts_for_n_minus_one():
    # N-1 sizing: a 2-node cluster has 1 compute node, so it needs more nodes
    # than the full-cluster case for the same demand.
    full = _compute_floor_nodes(100, None, 500, 0, 0.0,
                                min_nodes=2, full_cluster=True)
    n1 = _compute_floor_nodes(100, None, 500, 0, 0.0,
                              min_nodes=2, full_cluster=False)
    assert full == 5         # 100*5 = 500
    assert n1 > full         # one node held back per cluster


def test_floor_zero_when_no_demand():
    assert _compute_floor_nodes(100, 50, 0, 0, 0.5,
                                min_nodes=2, full_cluster=True) == 0


def test_benchmark_lowers_node_count_vs_pure_ghz():
    # A modern CPU with strong benchmark/core lets the benchmark side carry the
    # floor, needing fewer nodes than pure-GHz sizing would.
    pure_ghz = _compute_floor_nodes(100, 200, req_ghz=1000, req_perf=400,
                                    balance=0.0, min_nodes=2, full_cluster=True)
    leaning_bench = _compute_floor_nodes(100, 200, req_ghz=1000, req_perf=400,
                                         balance=1.0, min_nodes=2, full_cluster=True)
    assert leaning_bench < pure_ghz


if __name__ == "__main__":
    import traceback
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for f in funcs:
        try:
            f()
            print(f"PASS {f.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {f.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
