"""Exports name the datastores left out of the storage basis
(summary["excluded_datastores"], set by the exclusion maths): the docx
current-environment table and assumptions, the multi-site docx per site, and
the pptx workload slide. Nothing is added when none were left out.

Run:  .venv/bin/python -m pytest tests/test_export_excluded_datastores.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import test_perf_sizing_e2e as e2e  # noqa: E402
from test_export_storage_growth import _both, _docx_text, _sized, app  # noqa: E402,F401

EXCLUDED = [{"name": "ISO", "used_tb": 0.74}, {"name": "NCS-CameraArchive", "used_tb": 642.37}]


def _summary(excluded=EXCLUDED):
    s = e2e._summary()
    if excluded:
        s["excluded_datastores"] = excluded
    return s


def test_proposal_names_the_excluded_datastores(app):
    rec, proj = _sized(app)
    ppt, doc = _both(_summary(), rec, proj)
    listed = "NCS-CameraArchive (642.4 TB), ISO (0.74 TB)"
    assert "Left out of the sizing" in doc and listed in doc
    assert ("Left out of the storage sizing because their data is not moving to the new "
            "cluster: %s (643.1 TB in total)." % listed) in doc
    assert "Left out of the sizing" in ppt and "2 datastores · 643.1 TB" in ppt


def test_single_excluded_datastore_reads_by_name_on_the_slide(app):
    rec, proj = _sized(app)
    ppt, _ = _both(_summary(EXCLUDED[1:]), rec, proj)
    assert "NCS-CameraArchive · 642.4 TB" in ppt


def test_nothing_added_without_exclusions(app):
    rec, proj = _sized(app)
    ppt, doc = _both(_summary(None), rec, proj)
    assert "Left out of the" not in doc and "Left out of the" not in ppt


def test_multisite_docx_notes_it_per_site(app):
    from export_docx import build_bundle_proposal_docx
    from export_pptx import generate_bundle_proposal
    rec, proj = _sized(app)
    site = {"name": "HQ", "summary": _summary(), "recommendation": rec, "projection": proj,
            "source_perf": None, "replicates_to": ""}
    other = dict(site, name="Branch", summary=_summary(None))
    doc = _docx_text(build_bundle_proposal_docx([site, other]))
    assert doc.count("Left out of the storage sizing") == 1
    assert generate_bundle_proposal([site, other]).getvalue()     # still renders
