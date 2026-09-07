"""The admin pricebook upload -> diff -> apply flow (docs/pricebook-plan.md §4.4).

Day-to-day management must work over HTTP — the CLI tool is a convenience, not
the path. These tests drive the actual routes: the dry run writes nothing, the
diff is returned for review, apply installs exactly once (idempotent by file
hash), and the whole area stays super-admin-only.

Run: .venv/bin/python -m pytest tests/test_pricebook_admin.py -q
"""
import os
import sys
import types

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

from database import db  # noqa: E402
import orm_models as om  # noqa: E402
import auth_models  # noqa: F401,E402  - complete the mapper registry
import project_models  # noqa: F401,E402
import admin_routes as ar  # noqa: E402
import pricebook_import  # noqa: E402

ARCHIVE = os.path.join(os.path.dirname(__file__), "..", "_archive")
PRICELIST = os.path.join(ARCHIVE, "Scale Computing Q4 2025 EUR Master Price List.xlsx")

pytestmark = pytest.mark.skipif(
    not os.path.exists(PRICELIST),
    reason="archived price list not present")

SUPER_ADMIN = types.SimpleNamespace(is_super_admin=True, id=1,
                                    email="admin@test")


@pytest.fixture()
def app():
    application = Flask(__name__)
    application.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    application.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    application.register_blueprint(ar.admin_bp)
    db.init_app(application)
    with application.app_context():
        db.drop_all()
        db.create_all()
        yield application


@pytest.fixture()
def client(app, monkeypatch):
    monkeypatch.setattr(ar, "current_user", lambda: SUPER_ADMIN)
    return app.test_client()


def _post(client, apply_it=False, path=PRICELIST, **form):
    data = {"file": (open(path, "rb"), os.path.basename(path))}
    if apply_it:
        data["apply"] = "1"
    data.update(form)
    return client.post("/admin/api/import-pricebook", data=data,
                       content_type="multipart/form-data")


def test_requires_super_admin(app):
    client = app.test_client()   # anonymous: current_user() is not patched
    assert client.get("/admin/api/pricebook").status_code == 403
    assert _post(client).status_code == 403


def test_dry_run_writes_nothing(client):
    resp = _post(client)
    assert resp.status_code == 200
    d = resp.get_json()
    assert d["applied"] is False
    # Golden counts from the archived list (see test_licensing).
    assert d["counts"] == {"license_rows": 370, "banded": 360, "flat": 9,
                           "unmatched": 1}
    assert d["unmatched"][0]["sku"] == "HCOS-S-POC"
    assert d["diff"]["current"] is None      # fresh DB: first feed
    assert om.CatalogFeed.query.count() == 0


def test_apply_installs_and_audits(client):
    resp = _post(client, apply_it=True, label="Q4 2025 EUR")
    d = resp.get_json()
    assert d["applied"] is True
    assert d["feed"]["label"] == "Q4 2025 EUR"

    feed = om.CatalogFeed.query.one()
    assert feed.is_current and feed.region == "EMEA"
    assert len(feed.bands) == 360 and len(feed.flats) == 9

    from auth_models import AdminAuditLog
    entry = AdminAuditLog.query.filter_by(action="pricebook_import").one()
    assert "360 bands" in entry.detail

    # The sizer actually sees the feed.
    book = om.load_license_book("EMEA")
    assert book and book.flat_price("PE", 5) == 19944.00


def test_reapply_same_file_is_a_noop(client):
    _post(client, apply_it=True)
    resp = _post(client, apply_it=True)
    d = resp.get_json()
    assert d["applied"] is False and d["already_current"] is True
    assert om.CatalogFeed.query.count() == 1


def test_second_preview_diffs_against_installed_feed(client):
    _post(client, apply_it=True, label="current")
    d = _post(client).get_json()
    diff = d["diff"]
    assert diff["current"]["label"] == "current"
    # Same file against itself: nothing added, removed, or moved.
    assert diff["added"] == [] and diff["removed"] == [] and diff["moved"] == []


def test_diff_reports_a_moved_price(app):
    """diff_feed flags a changed price with old/new/pct — the reviewable core
    of the flow."""
    parsed = pricebook_import.parse_pricebook(PRICELIST)
    pricebook_import.seed_feed_from_file(PRICELIST, label="baseline")

    bumped = next(b for b in parsed["bands"]
                  if b["price"] == 17339.00 and b["core_band"] == 16)
    bumped["price"] = 18000.00
    diff = pricebook_import.diff_feed(parsed, "EMEA")
    assert diff["current"]["label"] == "baseline"
    (m,) = diff["moved"]
    assert m["old"] == 17339.00 and m["new"] == 18000.00
    assert m["kind"] == "band" and m["pct"] == pytest.approx(3.8, abs=0.05)


def test_rejects_non_xlsx(client):
    import io
    resp = client.post("/admin/api/import-pricebook",
                       data={"file": (io.BytesIO(b"not a zip"), "list.xlsx")},
                       content_type="multipart/form-data")
    assert resp.status_code == 400


def test_status_endpoint_lists_current_feed(client):
    assert client.get("/admin/api/pricebook").get_json() == {"feeds": []}
    _post(client, apply_it=True, label="Q4 2025 EUR")
    (feed,) = client.get("/admin/api/pricebook").get_json()["feeds"]
    assert feed["region"] == "EMEA" and feed["label"] == "Q4 2025 EUR"
    assert feed["bands"] == 360 and feed["flats"] == 9 and feed["unmatched"] == 1
    # Provenance keeps the UPLOADED name, not the server's anonymous temp file.
    assert feed["source_filename"] == os.path.basename(PRICELIST)
