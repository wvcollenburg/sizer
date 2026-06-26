"""Smoke test: the PPTX and DOCX exporters render a perf-floor-driven
recommendation without error, and emit the compute-floor copy.

Reuses the e2e harness to produce a real recommendation whose node count was
driven by the active compute floor (determinant == "Compute"), then runs both
exporters end-to-end.

Run:  .venv/bin/python tests/test_export_compute_floor.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import test_perf_sizing_e2e as e2e  # noqa: E402
from export_gauges import compute_floor_sentence  # noqa: E402


def _compute_driven_rec():
    """A recommendation sized up by the compute floor (pure-GHz balance), plus
    its projection and a synthetic source_perf payload for the benchmark slide."""
    app = e2e._build_app()
    from database import db
    with app.app_context():
        db.create_all()
        e2e._seed_catalog()
        e2e._set(perf_scaling=1, perf_ghz_balance=0)
    res = e2e._rec(app, source_perf_index=300, source_perf_type="specrate")
    top = res["recommendations"][0]
    assert top["determinant"]["resource"] == "Compute", top["determinant"]
    assert top.get("compute_floor"), "expected compute_floor on the rec"
    source_perf = {"total_specrate": 300.0,
                   "cpus": [{"model": "Xeon Test 32C", "sockets": 2,
                             "type": "specrate", "score": 150, "total": 300.0}]}
    return e2e._summary(), top, res["projection"], source_perf


def test_pptx_renders_with_compute_floor():
    from export_pptx import generate_proposal
    summary, rec, projection, source_perf = _compute_driven_rec()
    buf = generate_proposal(summary, rec, projection, source_perf)
    data = buf.getvalue()
    assert data[:2] == b"PK" and len(data) > 5000, "expected a non-trivial .pptx"
    print(f"PASS pptx render: {len(data):,} bytes")


def test_docx_renders_with_compute_floor():
    from export_docx import build_proposal_docx
    summary, rec, projection, source_perf = _compute_driven_rec()
    buf = build_proposal_docx(summary, rec, projection, source_perf)
    data = buf.getvalue()
    assert data[:2] == b"PK" and len(data) > 5000, "expected a non-trivial .docx"
    print(f"PASS docx render: {len(data):,} bytes")


def test_compute_floor_sentence_shape():
    _, rec, _, _ = _compute_driven_rec()
    s = compute_floor_sentence(rec)
    assert s and "CPU performance floor" in s and "%" in s
    # Off / absent -> None.
    assert compute_floor_sentence({"compute_floor": None}) is None
    assert compute_floor_sentence({}) is None
    print(f"PASS sentence: {s}")


if __name__ == "__main__":
    import traceback
    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for f in funcs:
        try:
            f()
        except Exception:
            failed += 1
            print(f"FAIL {f.__name__}")
            traceback.print_exc()
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
