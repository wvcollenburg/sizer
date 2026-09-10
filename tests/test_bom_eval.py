"""Acceptance harness: the 26 real BOMs from the SC//Design archive, checked
against a catalog built from OUR scraped HCL snapshot, compared with the
human verdicts in their eval-manifest.json.

The rules-port parity test (test_bom_rules.py) proves the engine reproduces
SC//Design's output on SC//Design's catalog. This test asks the question that
matters for shipping: with the catalog we actually have (scraped from
hcl.scalecomputing.com, not their internal HCLdev repo), do the human
reviewers' verdicts still come out? Differences are listed with the finding
codes so the owner can judge each one; the ones judged acceptable are pinned
in KNOWN_DIFFERENCES with the reason.

Archive-optional: skips when _archive/ is absent.
Run: .venv/bin/python -m pytest tests/test_bom_eval.py -q -s
"""
import json
import os
import re
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

from database import db  # noqa: E402
import auth_models  # noqa: F401,E402
import project_models  # noqa: F401,E402
import hcl_models  # noqa: F401,E402
from bom.normalize import NormalizedBOM  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ARCHIVE = os.path.join(ROOT, "_archive", "SC-Sizing-main", "tests", "fixtures", "bom")
HCL_FIXTURES = os.path.join(ROOT, "tests", "fixtures", "hcl")

pytestmark = pytest.mark.skipif(not os.path.isdir(ARCHIVE), reason="archive fixtures not present")

# original filename -> (expected verdict override or None, reason). A verdict
# override of None means "any verdict, but the finding codes must include the
# listed ones" — used when the human verdict depends on catalog data the
# public HCL does not publish.
KNOWN_DIFFERENCES = {
    # filled in from the first run; each entry needs a reason the owner agreed with
}


def _safe(name):
    return re.sub(r"[^\w.\-]", "_", name)


def _snapshot():
    from bom.hcl_scrape import scrape_all, ScrapeError
    manifest = json.load(open(os.path.join(HCL_FIXTURES, "manifest.json")))
    files = {p["url"]: p["file"] for p in manifest["pages"] if p.get("file")}

    def fetch(path, **_):
        fn = files.get(path)
        if not fn:
            raise ScrapeError("no fixture for %s" % path)
        with open(os.path.join(HCL_FIXTURES, fn), encoding="utf-8") as fh:
            return fh.read()

    snap = scrape_all(fetch=fetch, sleep=lambda s: None, delay=0)
    assert snap["complete"], snap["errors"]
    return snap


@pytest.fixture(scope="module")
def catalog_app():
    """A SQLite catalog populated the way production will be: a scrape run
    whose pending changes a super admin bulk-approves."""
    application = Flask(__name__)
    application.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    application.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(application)
    with application.app_context():
        db.drop_all()
        db.create_all()
        from bom import hcl_sync
        run = hcl_sync.build_run(_snapshot(), user=None, source="import")
        assert run.summary["pending_total"] > 0
        hcl_sync.bulk([], "approve", None, all_pending=True)
        yield application


def _cases():
    manifest = json.load(open(os.path.join(ARCHIVE, "eval-manifest.json")))
    for original, meta in sorted(manifest.items()):
        path = os.path.join(ARCHIVE, "normalized", _safe(original) + ".json")
        if os.path.exists(path):
            yield original, meta["verdict"], meta.get("notes", ""), path


def test_human_verdicts_against_our_catalog(catalog_app):
    from bom.check import run_check
    rows = []
    mismatches = []
    with catalog_app.app_context():
        for original, human, notes, path in _cases():
            bom = NormalizedBOM.from_dict(json.load(open(path)))
            result = run_check(bom, None)
            ours = result["technical"]["verdict"]
            codes = sorted({f["code"] for cr in result["technical"]["config_results"]
                            for f in cr["findings"] if f["severity"] != "info"})
            platforms = sorted({m for cr in result["technical"]["config_results"]
                                for m in ((cr.get("platform") or {}).get("sc_models") or [])})
            ok = ours == human
            known = KNOWN_DIFFERENCES.get(original)
            if not ok and known:
                want, reason = known
                ok = (want is None) or (ours == want)
            rows.append("%-4s %-12s %-12s %-40s %s | platforms: %s" % (
                "ok" if ok else "DIFF", human, ours, original[:40], ",".join(codes), ",".join(platforms)))
            if not ok:
                mismatches.append((original, human, ours, codes, notes))
    print("\n" + "\n".join(rows))
    assert not mismatches, "\n".join(
        "%s: human %s, ours %s, codes %s — %s" % m for m in mismatches)
