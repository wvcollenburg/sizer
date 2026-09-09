"""/admin/api/hcl/* — the HTTP face of the HCL catalog (build spec §6).

Routine catalog management has to work over HTTP (no CLI for day-to-day
ops), so every admin action is exercised here through the blueprint on a
bare Flask + SQLite app: the super-admin gate, the scrape trigger with its
lock/timeout, the queue, the catalog views, blocked vendors and the
no-network snapshot import. The scrape "thread" is run inline with a fake
fetch serving the HTML fixtures.

Run: .venv/bin/python -m pytest tests/test_hcl_admin.py -q
"""
import io
import json
import os
import sys
import types
from datetime import timedelta

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

from database import db  # noqa: E402
import orm_models  # noqa: F401,E402  - complete the mapper registry
import auth_models  # noqa: F401,E402
import project_models  # noqa: F401,E402
import bom_models  # noqa: F401,E402
import auth  # noqa: E402
import admin_routes as ar  # noqa: E402
import hcl_admin_routes as har  # noqa: E402
import hcl_models as hm  # noqa: E402
from auth_models import AdminAuditLog, _utcnow  # noqa: E402
from bom import hcl_scrape as hs  # noqa: E402
from bom import hcl_sync as sync  # noqa: E402
from extensions import limiter  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "hcl")
EXPECTED_PLATFORMS = 62
EXPECTED_DEVICES = 64

SUPER_ADMIN = types.SimpleNamespace(is_super_admin=True, id=1, email="admin@test")


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def fixture_fetch(failing=()):
    manifest = json.loads(_read("manifest.json"))
    by_url = {p["url"]: p["file"] for p in manifest["pages"]}

    def fetch(path):
        if path in failing:
            raise hs.ScrapeError("GET %s failed after 4 attempts: IncompleteRead" % path)
        if path not in by_url:
            raise hs.ScrapeError("GET %s failed: HTTP 404" % path)
        return _read(by_url[path])
    return fetch


@pytest.fixture(scope="module")
def snapshot():
    return hs.scrape_all(fetch=fixture_fetch(), sleep=lambda s: None)


@pytest.fixture()
def app():
    application = Flask(__name__)
    application.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    application.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    application.config["TESTING"] = True
    application.register_blueprint(ar.admin_bp)
    application.register_blueprint(har.hcl_admin_bp)
    db.init_app(application)
    limiter.enabled = False
    with application.app_context():
        db.drop_all()
        db.create_all()
        yield application


@pytest.fixture()
def client(app, monkeypatch):
    monkeypatch.setattr(ar, "current_user", lambda: SUPER_ADMIN)
    monkeypatch.setattr(har, "current_user", lambda: SUPER_ADMIN)
    monkeypatch.setattr(auth, "current_user", lambda: SUPER_ADMIN)
    return app.test_client()


@pytest.fixture()
def inline_scrape(app, monkeypatch):
    """Run the scrape thread body synchronously with the fixture fetch."""
    calls = []

    def start(app_obj, run_id, fetch=fixture_fetch()):
        calls.append(run_id)
        har.run_scrape(app_obj, run_id, fetch=fetch, delay=0)
    monkeypatch.setattr(har, "_start_scrape_thread", start)
    return calls


def _seed(client, snapshot):
    """Import the fixture snapshot and approve everything over HTTP."""
    resp = _import(client, snapshot)
    assert resp.status_code == 201, resp.get_json()
    resp = client.post("/admin/api/hcl/pending/bulk", json={"all": True, "action": "approve"})
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _import(client, payload, filename="snapshot.json"):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    return client.post("/admin/api/hcl/import-snapshot",
                       data={"file": (io.BytesIO(raw), filename)},
                       content_type="multipart/form-data")


def _audit_actions():
    return [r.action for r in AdminAuditLog.query.order_by(AdminAuditLog.id).all()]


# ---------------------------------------------------------------- auth

def test_anonymous_gets_403(app):
    anon = app.test_client()  # current_user() not patched -> None
    assert anon.get("/admin/api/hcl/stats").status_code == 403
    assert anon.post("/admin/api/hcl/scrape").status_code == 403
    assert anon.get("/admin/api/hcl/pending").status_code == 403
    assert anon.post("/admin/api/hcl/pending/bulk", json={"all": True, "action": "approve"}).status_code == 403
    assert anon.get("/admin/api/hcl/platforms").status_code == 403
    assert anon.put("/admin/api/hcl/settings", json={"blocked_vendors": []}).status_code == 403
    assert anon.post("/admin/api/hcl/import-snapshot").status_code == 403
    assert anon.get("/admin/api/hcl/stats").get_json() == {"error": "Super admin access required"}


def test_non_super_admin_gets_403(app, monkeypatch):
    user = types.SimpleNamespace(is_super_admin=False, id=2, email="u@test")
    monkeypatch.setattr(ar, "current_user", lambda: user)
    monkeypatch.setattr(har, "current_user", lambda: user)
    assert app.test_client().get("/admin/api/hcl/stats").status_code == 403


# ---------------------------------------------------------------- stats / empty

def test_stats_on_empty_catalog(client):
    resp = client.get("/admin/api/hcl/stats")
    assert resp.status_code == 200
    d = resp.get_json()
    assert d["platforms"] == {"active": 0, "delisted": 0}
    assert d["components"] == {"active": 0, "delisted": 0, "by_kind": {}}
    assert d["devices"] == {"active": 0, "delisted": 0}
    assert d["pending"] == 0 and d["last_run"] is None and d["running"] is False
    assert d["blocked_vendors"] == [] and d["catalog_stamp"] == "run0:change0"
    assert client.get("/admin/api/hcl/scrape/status").get_json() == {"running": False, "last": None}
    assert client.get("/admin/api/hcl/runs").get_json() == []


# ---------------------------------------------------------------- scrape

def test_scrape_trigger_runs_and_queues(client, inline_scrape, snapshot):
    resp = client.post("/admin/api/hcl/scrape")
    assert resp.status_code == 202, resp.get_json()
    run = resp.get_json()["run"]
    assert run["status"] == "queued" and run["source"] == "scrape"
    assert inline_scrape == [run["id"]]

    status = client.get("/admin/api/hcl/scrape/status").get_json()
    assert status["running"] is False
    last = status["last"]
    assert last["id"] == run["id"] and last["status"] == "succeeded"
    assert last["complete"] is True and last["errors"] == []
    assert last["pages_total"] == EXPECTED_PLATFORMS + 2 == last["pages_done"]
    assert last["progress"] == 100
    assert last["summary"]["platforms"] == EXPECTED_PLATFORMS
    assert last["summary"]["devices"] == EXPECTED_DEVICES
    assert last["summary"]["changes"]["add"] == last["summary"]["pending_total"] > 0
    assert last["triggered_by_user_id"] == SUPER_ADMIN.id

    runs = client.get("/admin/api/hcl/runs").get_json()
    assert [r["id"] for r in runs] == [run["id"]]
    stats = client.get("/admin/api/hcl/stats").get_json()
    assert stats["pending"] == last["summary"]["pending_total"]
    assert stats["last_run"]["id"] == run["id"]
    assert "hcl_scrape_start" in _audit_actions()
    # nothing in the catalog until approval
    assert stats["platforms"]["active"] == 0


def test_scrape_409_while_running_and_timeout_recovery(client, inline_scrape):
    live = hm.HclScrapeRun(status=hm.RUN_RUNNING, source="scrape")
    db.session.add(live)
    db.session.commit()
    resp = client.post("/admin/api/hcl/scrape")
    assert resp.status_code == 409
    assert "already running" in resp.get_json()["error"]
    assert inline_scrape == []
    status = client.get("/admin/api/hcl/scrape/status").get_json()
    assert status["running"] is True and status["last"]["id"] == live.id
    assert client.get("/admin/api/hcl/stats").get_json()["running"] is True

    # a run stuck for over 30 minutes is written off and the button works again
    live.started_at = _utcnow() - timedelta(minutes=31)
    db.session.commit()
    resp = client.post("/admin/api/hcl/scrape")
    assert resp.status_code == 202
    stuck = db.session.get(hm.HclScrapeRun, live.id)
    assert stuck.status == hm.RUN_FAILED and stuck.error == "timed out"
    assert inline_scrape == [resp.get_json()["run"]["id"]]


def test_scrape_failure_lands_on_the_run(app, client, monkeypatch):
    def start(app_obj, run_id):
        har.run_scrape(app_obj, run_id, fetch=fixture_fetch(failing={hs.LISTING_PATH}), delay=0)
    monkeypatch.setattr(har, "_start_scrape_thread", start)
    resp = client.post("/admin/api/hcl/scrape")
    assert resp.status_code == 202
    last = client.get("/admin/api/hcl/scrape/status").get_json()["last"]
    # scrape_all never raises: the run succeeds but is flagged incomplete
    assert last["status"] == "succeeded" and last["complete"] is False
    assert [e["path"] for e in last["errors"]] == [hs.LISTING_PATH]
    assert last["summary"]["changes"]["delist"] == 0

    # a crash inside the diff is recorded, never raised out of the thread
    def boom(*args, **kwargs):
        raise RuntimeError("diff exploded")
    monkeypatch.setattr(sync, "diff_snapshot", boom)
    resp = client.post("/admin/api/hcl/scrape")
    assert resp.status_code == 202
    last = client.get("/admin/api/hcl/scrape/status").get_json()["last"]
    assert last["status"] == "failed" and "diff exploded" in last["error"]
    assert client.get("/admin/api/hcl/scrape/status").get_json()["running"] is False


def test_run_scrape_progress_commits(app, monkeypatch):
    run = hm.HclScrapeRun(status=hm.RUN_QUEUED, source="scrape")
    db.session.add(run)
    db.session.commit()
    seen = []
    real = hs.scrape_all

    def spy(fetch, progress, delay):
        def wrapped(done, total, label):
            progress(done, total, label)
            seen.append((done, total))
        return real(fetch=fetch, progress=wrapped, delay=delay, sleep=lambda s: None)
    monkeypatch.setattr(hs, "scrape_all", spy)
    out = har.run_scrape(app, run.id, fetch=fixture_fetch(), delay=0)
    assert out == run.id
    db.session.expire_all()
    assert db.session.get(hm.HclScrapeRun, run.id).status == hm.RUN_SUCCEEDED
    assert seen[-1] == (64, 64) and len(seen) == 64
    assert har.run_scrape(app, 424242, fetch=fixture_fetch(), delay=0) is None


# ---------------------------------------------------------------- pending queue

def test_pending_list_and_filters(client, snapshot):
    resp = _import(client, snapshot)
    assert resp.status_code == 201
    run_id = resp.get_json()["run"]["id"]
    rows = client.get("/admin/api/hcl/pending").get_json()
    assert rows and all(r["status"] == "pending" for r in rows)
    assert rows == sorted(rows, key=lambda r: (r["entity_type"], r["entity_key"], r["field"] or ""))
    assert {"id", "run_id", "entity_type", "entity_key", "change_kind", "field", "old", "new",
            "label", "status", "created_at", "decided_at", "decided_by_user_id", "note"} <= set(rows[0])

    platforms = client.get("/admin/api/hcl/pending?entity_type=platform").get_json()
    assert len(platforms) == EXPECTED_PLATFORMS
    assert all(r["change_kind"] == "add" for r in platforms)
    assert client.get("/admin/api/hcl/pending?kind=delist").get_json() == []
    assert client.get("/admin/api/hcl/pending?run_id=%d&entity_type=device" % run_id).get_json()
    assert client.get("/admin/api/hcl/pending?run_id=%d" % (run_id + 1)).get_json() == []
    assert len(client.get("/admin/api/hcl/pending?limit=7").get_json()) == 7
    assert len(client.get("/admin/api/hcl/pending?limit=0").get_json()) == 1
    total = client.get("/admin/api/hcl/stats").get_json()["pending"]
    assert len(client.get("/admin/api/hcl/pending?limit=99999").get_json()) == min(total, 2000)
    assert client.get("/admin/api/hcl/pending?status=approved").get_json() == []
    assert len(client.get("/admin/api/hcl/pending?status=all&limit=2000").get_json()) == min(total, 2000)


def test_approve_and_reject_single(client, snapshot):
    _import(client, snapshot)
    rows = client.get("/admin/api/hcl/pending?entity_type=platform").get_json()
    hc1350 = next(r for r in rows if r["entity_key"] == "lenovo/HC1350")
    resp = client.post("/admin/api/hcl/pending/%d/approve" % hc1350["id"], json={"note": "seed"})
    assert resp.status_code == 200, resp.get_json()
    d = resp.get_json()
    assert d["message"] == "Change applied"
    assert d["change"]["status"] == "approved" and d["change"]["note"] == "seed"
    assert d["change"]["decided_by_user_id"] == SUPER_ADMIN.id
    assert "lenovo/HC1350" in d["created"]
    platforms = client.get("/admin/api/hcl/platforms").get_json()
    assert [p["key"] for p in platforms] == ["lenovo/HC1350"]
    assert platforms[0]["component_count"] > 0
    # the parts it listed are now in the catalog and their add rows decided
    stats = client.get("/admin/api/hcl/stats").get_json()
    assert stats["components"]["active"] == platforms[0]["component_count"]
    via = client.get("/admin/api/hcl/pending?status=approved&entity_type=component").get_json()
    assert via and all(r["note"] == "via platform lenovo/HC1350" for r in via)

    other = next(r for r in rows if r["entity_key"] == "lenovo/HC1450D")
    resp = client.post("/admin/api/hcl/pending/%d/reject" % other["id"], json={"note": "later"})
    assert resp.status_code == 200
    assert resp.get_json()["change"]["status"] == "rejected"
    assert resp.get_json()["change"]["note"] == "later"
    assert len(client.get("/admin/api/hcl/platforms").get_json()) == 1

    # state errors + unknown ids
    resp = client.post("/admin/api/hcl/pending/%d/reject" % hc1350["id"])
    assert resp.status_code == 409 and "already approved" in resp.get_json()["error"]
    assert client.post("/admin/api/hcl/pending/999999/approve").status_code == 404
    assert client.post("/admin/api/hcl/pending/999999/reject").status_code == 404
    # approving twice is harmless
    resp = client.post("/admin/api/hcl/pending/%d/approve" % hc1350["id"])
    assert resp.status_code == 200
    assert len(client.get("/admin/api/hcl/platforms").get_json()) == 1
    actions = _audit_actions()
    assert "hcl_change_approve" in actions and "hcl_change_reject" in actions


def test_bulk_ids_and_all(client, snapshot):
    _import(client, snapshot)
    devices = client.get("/admin/api/hcl/pending?entity_type=device").get_json()
    ids = [r["id"] for r in devices[:5]]
    resp = client.post("/admin/api/hcl/pending/bulk", json={"ids": ids + [999999], "action": "approve"})
    assert resp.status_code == 200
    assert resp.get_json() == {"approved": 5, "rejected": 0, "skipped": 1}
    assert len(client.get("/admin/api/hcl/devices").get_json()) == 5

    resp = client.post("/admin/api/hcl/pending/bulk",
                       json={"all": True, "action": "reject", "filter": {"entity_type": "device"}})
    assert resp.status_code == 200
    assert resp.get_json() == {"approved": 0, "rejected": EXPECTED_DEVICES - 5, "skipped": 0}
    assert client.get("/admin/api/hcl/pending?entity_type=device").get_json() == []

    resp = client.post("/admin/api/hcl/pending/bulk", json={"all": True, "action": "approve"})
    d = resp.get_json()
    assert d["rejected"] == 0 and d["skipped"] == 0 and d["approved"] > 0
    assert client.get("/admin/api/hcl/pending").get_json() == []
    stats = client.get("/admin/api/hcl/stats").get_json()
    assert stats["platforms"]["active"] == EXPECTED_PLATFORMS
    assert stats["devices"]["active"] == 5
    assert stats["components"]["by_kind"] == {"cpu": 71, "nic": 32, "hba": 12, "hdd": 30, "ssd": 52}
    assert _audit_actions().count("hcl_bulk") == 3

    # validation
    assert client.post("/admin/api/hcl/pending/bulk", json={"ids": [], "action": "approve"}).status_code == 400
    assert client.post("/admin/api/hcl/pending/bulk", json={"ids": [1], "action": "nope"}).status_code == 400
    assert client.post("/admin/api/hcl/pending/bulk", json={"ids": ["x"], "action": "approve"}).status_code == 400
    assert client.post("/admin/api/hcl/pending/bulk", data="nope").status_code == 400


# ---------------------------------------------------------------- catalog views

def test_platform_views(client, snapshot):
    _seed(client, snapshot)
    platforms = client.get("/admin/api/hcl/platforms").get_json()
    assert len(platforms) == EXPECTED_PLATFORMS
    assert platforms == sorted(platforms, key=lambda p: (p["brand"], p["sc_model"]))
    assert "components" not in platforms[0]
    hc1350 = next(p for p in platforms if p["key"] == "lenovo/HC1350")
    assert hc1350["server_key"] == "sr630v2" and hc1350["status"] == "active"

    detail = client.get("/admin/api/hcl/platforms/%d" % hc1350["id"]).get_json()
    assert detail["key"] == "lenovo/HC1350"
    assert len(detail["components"]) == hc1350["component_count"]
    nic = next(c for c in detail["components"] if c["part_number"] == "4XC7A80566")
    assert nic["tce"] is True and nic["link_status"] == "active" and nic["kind"] == "nic"
    assert client.get("/admin/api/hcl/platforms/999999").status_code == 404

    assert client.get("/admin/api/hcl/platforms?status=delisted").get_json() == []
    assert len(client.get("/admin/api/hcl/platforms?status=all").get_json()) == EXPECTED_PLATFORMS
    # delist one via the queue, then the filters split
    snap = json.loads(json.dumps(snapshot))
    snap["platforms"] = [p for p in snap["platforms"]
                         if not (p["brand"] == "lenovo" and p["sc_model"] == "HC1350")]
    _import(client, snap)
    client.post("/admin/api/hcl/pending/bulk",
                json={"all": True, "action": "approve", "filter": {"kind": "delist", "entity_type": "platform"}})
    delisted = client.get("/admin/api/hcl/platforms?status=delisted").get_json()
    assert [p["key"] for p in delisted] == ["lenovo/HC1350"] and delisted[0]["delisted_at"]
    assert len(client.get("/admin/api/hcl/platforms").get_json()) == EXPECTED_PLATFORMS - 1
    assert len(client.get("/admin/api/hcl/platforms?status=all").get_json()) == EXPECTED_PLATFORMS
    assert client.get("/admin/api/hcl/stats").get_json()["platforms"] == {
        "active": EXPECTED_PLATFORMS - 1, "delisted": 1}


def test_component_search_and_devices(client, snapshot):
    _seed(client, snapshot)
    all_nics = client.get("/admin/api/hcl/components?kind=nic").get_json()
    assert len(all_nics) == 32 and all(c["kind"] == "nic" for c in all_nics)
    hits = client.get("/admin/api/hcl/components?q=57504").get_json()
    assert hits and all("57504" in (c["part_number"] + c["description"]) for c in hits)
    hits = client.get("/admin/api/hcl/components?q=4xc7a80566").get_json()
    assert [c["part_number"] for c in hits] == ["4XC7A80566"]
    hits = client.get("/admin/api/hcl/components?q=broadcom&kind=hba").get_json()
    assert hits == []
    assert client.get("/admin/api/hcl/components?q=zzzz-nothing").get_json() == []
    everything = client.get("/admin/api/hcl/components?status=all").get_json()
    assert len(everything) == 197
    devices = client.get("/admin/api/hcl/devices").get_json()
    assert len(devices) == EXPECTED_DEVICES
    assert devices == sorted(devices, key=lambda d: (d["ven_id"], d["dev_id"]))
    assert client.get("/admin/api/hcl/devices?status=delisted").get_json() == []


# ---------------------------------------------------------------- settings

def test_settings_round_trip_and_validation(client):
    assert client.get("/admin/api/hcl/settings").get_json() == {"blocked_vendors": []}
    resp = client.put("/admin/api/hcl/settings", json={"blocked_vendors": [" HPE", "Acme", "hpe"]})
    assert resp.status_code == 200
    assert resp.get_json() == {"blocked_vendors": ["hpe", "acme"]}
    assert client.get("/admin/api/hcl/settings").get_json() == {"blocked_vendors": ["hpe", "acme"]}
    assert client.get("/admin/api/hcl/stats").get_json()["blocked_vendors"] == ["hpe", "acme"]
    assert "hcl_settings" in _audit_actions()

    assert client.put("/admin/api/hcl/settings", json={}).status_code == 400
    assert client.put("/admin/api/hcl/settings", json={"blocked_vendors": "hpe"}).status_code == 400
    assert client.put("/admin/api/hcl/settings", json={"blocked_vendors": [1]}).status_code == 400
    assert client.put("/admin/api/hcl/settings", json={"blocked_vendors": ["x" * 41]}).status_code == 400
    assert client.put("/admin/api/hcl/settings", data="nope").status_code == 400
    # unchanged after the bad requests
    assert client.get("/admin/api/hcl/settings").get_json() == {"blocked_vendors": ["hpe", "acme"]}
    resp = client.put("/admin/api/hcl/settings", json={"blocked_vendors": []})
    assert resp.get_json() == {"blocked_vendors": []}


# ---------------------------------------------------------------- import

def test_import_snapshot_happy_path(client, snapshot):
    resp = _import(client, snapshot, filename="hcl-2026-09-08.json")
    assert resp.status_code == 201, resp.get_json()
    run = resp.get_json()["run"]
    assert run["status"] == "succeeded" and run["source"] == "import"
    assert run["complete"] is True and run["progress"] == 100
    assert run["pages_total"] == 64 and run["pages_done"] == 64
    assert run["summary"]["platforms"] == EXPECTED_PLATFORMS
    assert run["summary"]["changes"]["add"] == run["summary"]["pending_total"]
    assert run["triggered_by_user_id"] == SUPER_ADMIN.id
    assert client.get("/admin/api/hcl/stats").get_json()["pending"] == run["summary"]["pending_total"]
    assert "hcl_import" in _audit_actions()

    # importing again with 'complete' missing: treated as incomplete, still no delists
    minimal = {"platforms": snapshot["platforms"][:3]}
    resp = _import(client, minimal)
    assert resp.status_code == 201
    run2 = resp.get_json()["run"]
    assert run2["complete"] is False and run2["summary"]["changes"]["delist"] == 0
    assert run2["summary"]["platforms"] == 3


def test_import_snapshot_bad_files(client):
    assert client.post("/admin/api/hcl/import-snapshot").status_code == 400
    assert _import(client, b"").status_code == 400
    assert _import(client, b"not json {").status_code == 400
    assert _import(client, {"devices": []}).status_code == 400
    assert _import(client, {"platforms": "nope"}).status_code == 400
    assert _import(client, {"platforms": [], "devices": "nope"}).status_code == 400
    assert _import(client, [1, 2, 3]).status_code == 400
    too_big = b'{"platforms": [], "pad": "' + b"x" * har.IMPORT_MAX_BYTES + b'"}'
    resp = _import(client, too_big)
    assert resp.status_code == 400 and "8 MB" in resp.get_json()["error"]
    for resp_body in (client.post("/admin/api/hcl/import-snapshot").get_json(),):
        assert set(resp_body) == {"error"}
    assert client.get("/admin/api/hcl/runs").get_json() == []
