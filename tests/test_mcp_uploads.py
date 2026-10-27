"""Files over MCP: the upload link, the page, and sizing from an upload
(docs/mcp/01-tool-design.md §2.7, §3.10, §3.11, §3.11a, §3.11b).

Locks in:
  - start_upload mints a one-time link for the caller; the page needs that
    user's session (another user or anonymous: no page), takes one file, and
    closes
  - get_upload reports waiting/ready/failed/expired and the parsed inputs;
    an upload is readable by its owner only, and is gone after retention
  - list_upload_vms pages and flags the UI's suggested exclusions
  - create_sizing from_upload applies the exclusion maths (same numbers as
    adjust_summary) and refuses out-of-range indices
  - save_sizing from_upload writes the UI's import payload (VM list,
    exclusions, sources, provenance) and get_sizing reads it back complete
  - merge_uploads merges two files with an overlap into a target the same
    tools accept
  - a BOM upload needs an owned project and records the check the project
    route created

Run: .venv/bin/python -m pytest tests/test_mcp_uploads.py -q
"""
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.pop("SESSION_COOKIE_SECURE", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

import app as appmod  # noqa: E402
import import_service  # noqa: E402
import mcp_uploads  # noqa: E402
from auth_models import Configuration, User  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from merge_fixtures import host, rvtools, vm  # noqa: E402
from test_mcp_server import call  # noqa: E402
from test_security import _offending_keys  # noqa: E402
from test_sizing_service import WORKLOAD, _seed_catalog  # noqa: E402

PW = "Abcdef1!xy"
HOSTS = [host("esx1", "Prod"), host("esx2", "Prod")]
VMS = [vm("web01", "esx1", "Prod", vcpus=4, ram_gb=16, used_gb=100, uuid="u-web01"),
       vm("db01", "esx2", "Prod", vcpus=8, ram_gb=32, used_gb=200, uuid="u-db01"),
       vm("old01", "esx2", "Prod", vcpus=2, ram_gb=8, used_gb=50, on=False, uuid="u-old01"),
       vm("vCLS-9a1", "esx1", "Prod", vcpus=1, ram_gb=0.25, used_gb=2, uuid="u-vcls")]
DATASTORES = [("ds1", 2048, 1024)]
FIX = os.path.join(os.path.dirname(__file__), "fixtures", "bom", "synthetic_template.xlsx")


def _signup(client, email):
    client.post("/api/auth/signup", json={"email": email, "password": PW, "accept_privacy": True})
    with appmod.app.app_context():
        return User.query.filter_by(email=email).one().id


def _login(client, email):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.get_json()


@pytest.fixture()
def world(tmp_path):
    app = appmod.app
    app.config["TESTING"] = True
    app.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    with app.app_context():
        db.drop_all()
        db.create_all()
        _seed_catalog()
    c = app.test_client()
    ids = {"alice": _signup(c, "alice@partnerco.example"),
           "bob": _signup(c, "bob@partnerco.example")}
    _login(c, "alice@partnerco.example")
    app.config["MCP_TEST_USER_ID"] = ids["alice"]
    files = {"a": rvtools(str(tmp_path / "a.xlsx"), HOSTS, VMS, DATASTORES),
             "b": rvtools(str(tmp_path / "b.xlsx"), [host("esx3", "Prod")],
                          [vm("web01", "esx3", "Prod", vcpus=4, ram_gb=16, used_gb=100, uuid="u-web01"),
                           vm("app02", "esx3", "Prod", vcpus=6, ram_gb=24, used_gb=150, uuid="u-app02")],
                          [("ds2", 2048, 512)], date="2026/10/01 10:00:00")}
    yield c, ids, files
    app.config.pop("MCP_TEST_USER_ID", None)


def _err(r):
    assert r["isError"], r
    return json.loads(r["content"][0]["text"])["error"]


def _start(c, kind="assessment", **extra):
    s = call(c, "start_upload", dict(kind=kind, **extra))["structuredContent"]
    assert s["upload_id"].startswith("u_") and "/mcp/upload/" in s["upload_url"]
    return s["upload_id"], s["upload_url"].split("/mcp/upload/", 1)[1]


def _post_file(c, token, path):
    with open(path, "rb") as fh:
        body = fh.read()
    return c.post("/mcp/upload/" + token, data={"file": (io.BytesIO(body), os.path.basename(path))},
                  content_type="multipart/form-data")


def _upload(c, path):
    uid, token = _start(c)
    r = _post_file(c, token, path)
    assert r.status_code == 200, r.get_json()
    return uid, token


# ── link and page ────────────────────────────────────────────────────────────

def test_link_is_single_use_and_bound_to_the_user(world):
    c, ids, files = world
    uid, token = _start(c)
    s = call(c, "get_upload", {"upload_id": uid})["structuredContent"]
    assert s["status"] == "waiting" and s["assessment"] is None
    # The page: the owner sees it; another user and an anonymous visitor do not.
    r = c.get("/mcp/upload/" + token)
    assert r.status_code == 200 and b"environment assessment" in r.data
    _login(c, "bob@partnerco.example")
    assert c.get("/mcp/upload/" + token).status_code == 404
    assert _post_file(c, token, files["a"]).status_code == 404
    c.post("/api/auth/logout")
    assert c.get("/mcp/upload/" + token).status_code in (302, 401)
    _login(c, "alice@partnerco.example")
    # A wrong file keeps the link open; the right one closes it.
    r = c.post("/mcp/upload/" + token, data={"file": (io.BytesIO(b"nope"), "x.xlsx")},
               content_type="multipart/form-data")
    assert r.status_code == 400
    assert c.get("/mcp/upload/" + token).status_code == 200
    r = _post_file(c, token, files["a"])
    assert r.status_code == 200 and r.get_json()["file_type"] == "rvtools"
    assert c.get("/mcp/upload/" + token).status_code == 410
    assert _post_file(c, token, files["a"]).status_code == 410
    # Not readable by another account over MCP either.
    appmod.app.config["MCP_TEST_USER_ID"] = ids["bob"]
    assert _err(call(c, "get_upload", {"upload_id": uid}))["code"] == "not_found"
    appmod.app.config["MCP_TEST_USER_ID"] = ids["alice"]
    assert _err(call(c, "get_upload", {"upload_id": "u_nonsense"}))["code"] == "not_found"


def test_expiry_and_purge(world):
    c, ids, files = world
    uid, token = _start(c)
    with appmod.app.app_context():
        row = mcp_uploads.McpUpload.query.filter_by(token=token).one()
        row.link_expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.session.commit()
    assert c.get("/mcp/upload/" + token).status_code == 410
    s = call(c, "get_upload", {"upload_id": uid})["structuredContent"]
    assert s["status"] == "expired"
    with appmod.app.app_context():
        row = mcp_uploads.McpUpload.query.filter_by(token=token).one()
        row.retention_until = datetime.now(timezone.utc) - timedelta(hours=1)
        db.session.commit()
    assert _err(call(c, "get_upload", {"upload_id": uid}))["code"] == "upload_expired"
    with appmod.app.app_context():
        assert mcp_uploads.purge_expired() == 1
        assert mcp_uploads.McpUpload.query.count() == 0
    assert _err(call(c, "get_upload", {"upload_id": uid}))["code"] == "not_found"


# ── get_upload and the VM list ───────────────────────────────────────────────

def test_get_upload_and_list_vms(world):
    c, ids, files = world
    uid, _ = _upload(c, files["a"])
    r = call(c, "get_upload", {"upload_id": uid})
    s = r["structuredContent"]
    assert s["status"] == "ready" and s["kind"] == "assessment"
    a = s["assessment"]
    assert a["source"]["file_type"] == "rvtools" and a["source"]["vm_count"] == 4
    assert a["inputs"]["workload"]["total_vcpus"] == 13 and a["missing_for_sizing"] == []
    assert a["largest_vms"][0]["name"] == "db01" and a["clusters"] == []
    assert all(isinstance(w["text"], str) for w in a["import_warnings"])
    assert _offending_keys(s) == []
    assert "rvtools: 2 hosts, 4 VMs" in r["content"][0]["text"]

    s = call(c, "list_upload_vms", {"upload_id": uid})["structuredContent"]
    assert [v["name"] for v in s["vms"]] == ["db01", "web01", "old01", "vCLS-9a1"]
    assert s["estate"] == {"vms": 4, "powered_on": 3, "templates": 0, "suggested_exclusions": 1}
    flagged = next(v for v in s["vms"] if v["name"] == "vCLS-9a1")
    assert flagged["suggested_exclusion"] == "vmware_infrastructure" and flagged["vm_index"] == 3
    s = call(c, "list_upload_vms", {"upload_id": uid, "suggested_exclusions_only": True})["structuredContent"]
    assert [v["name"] for v in s["vms"]] == ["vCLS-9a1"]
    s = call(c, "list_upload_vms", {"upload_id": uid, "powered_on": True, "min_vcpus": 4,
                                    "sort": "name", "descending": False, "limit": 1})["structuredContent"]
    assert [v["name"] for v in s["vms"]] == ["db01"] and s["total_matching"] == 2
    assert s["next_offset"] == 1
    s = call(c, "list_upload_vms", {"upload_id": uid, "name_contains": "WEB"})["structuredContent"]
    assert [v["name"] for v in s["vms"]] == ["web01"]


# ── sizing from an upload ────────────────────────────────────────────────────

def test_create_sizing_from_upload_with_exclusions(world):
    c, ids, files = world
    uid, _ = _upload(c, files["a"])
    r = call(c, "create_sizing", {"from_upload": {"upload_id": uid, "exclude_compute": [3],
                                                   "exclude_storage": [3, 2]}})
    assert r["isError"] is False, r["content"][0]["text"]
    s = r["structuredContent"]
    used = s["inputs_used"]
    assert used["workload"]["total_vcpus"] == 12 and used["workload"]["largest_vm_vcpus"] == 8
    assert used["from_upload"]["upload_id"] == uid
    assert used["from_upload"]["exclusions"]["compute"] == 1
    assert used["from_upload"]["exclusions"]["storage"] == 2
    assert used["from_upload"]["exclusions"]["vcpus_removed"] == 1
    assert "nic_speed_gbe" not in used["defaults_applied"]       # the file had NICs
    assert "_upload_ctx" not in used
    # RVTools measures no IOPS or used RAM: unknown, not zero.
    for field in ("p95_iops", "avg_iops", "peak_iops", "used_ram_gb"):
        assert used["workload"][field] is None, field
    assert any("no measurement for p95_iops" in w for w in s["warnings"])
    assert all(c["status"] == "not_applicable" for o in s["options"]
               for c in o["checks"] if c["id"] == "iops")
    # Same numbers as the exclusion maths applied by hand.
    data = import_service.parse_assessment(files["a"], "a.xlsx")
    expect = import_service.adjust_summary(data["summary"], data["vms"], [3], [3, 2])
    assert used["workload"]["used_storage_tb"] == expect["datastore_used_tb"]
    assert s["options"], s["warnings"]
    # Both ways of giving inputs at once, and an index past the VM list.
    r = call(c, "create_sizing", {"from_upload": {"upload_id": uid}, "workload": WORKLOAD})
    assert _err(r)["invalid"][0]["field"] == "from_upload"
    r = call(c, "create_sizing", {"from_upload": {"upload_id": uid, "exclude_compute": [99]}})
    assert _err(r)["invalid"][0]["field"] == "from_upload.exclude_compute"
    r = call(c, "create_sizing", {"from_upload": {"upload_id": uid, "source_cluster": "Nope"}})
    assert _err(r)["invalid"][0]["field"] == "from_upload.source_cluster"
    v = call(c, "validate_inputs", {"from_upload": {"upload_id": uid}})["structuredContent"]
    assert v["valid"] is True and v["inputs_used"]["workload"]["total_vcpus"] == 13


def test_save_sizing_from_upload_writes_the_import_payload(world):
    c, ids, files = world
    uid, token = _upload(c, files["a"])
    r = call(c, "save_sizing", {"name": "From LO", "from_upload": {"upload_id": uid,
                                                                     "exclude_compute": [3],
                                                                     "exclude_storage": [3]},
                                "new_project_name": "ACME"})
    assert r["isError"] is False, r["content"][0]["text"]
    s = r["structuredContent"]
    assert _offending_keys(s) == []
    with appmod.app.app_context():
        cfg = db.session.get(Configuration, s["sizing"]["id"])
        p = cfg.payload
        assert p["version"] == 4 and p["mode"] == "import"
        im = p["import"]
        assert [v["name"] for v in im["importVms"]] == ["web01", "db01", "old01", "vCLS-9a1"]
        assert im["exclCompute"] == [3] and im["exclStorage"] == [3]
        assert im["importVms"][0]["src"] == im["sources"][0]["id"]
        assert "ref" not in im["importVms"][0]
        assert im["originalImportSummary"]["total_vcpus"] == 13
        assert im["importSummary"]["total_vcpus"] == 12
        assert im["selectedRec"] == {"import": 0} and im["wizardStep"] is None
        assert im["sources"][0]["file_type"] == "rvtools"
        assert len(im["sources"][0]["hosts"]) == 2 and im["sources"][0]["summary"]["total_vcpus"] == 13
        assert im["lastProjection"]["years"] == 5
        assert p["fields"]["ratio-slider"] == "3" and "man-vcpus" not in p["fields"]
        assert p["canonical"]["from_upload"]["exclude_compute"] == [3]
        assert cfg.source_meta["file_type"] == "rvtools" and cfg.source_meta["file_name"] == "a.xlsx"
        assert cfg.parser_version and "files" not in cfg.source_meta
        from fingerprint import result_state
        assert result_state(cfg)["cache"] == "fresh"
    g = call(c, "get_sizing", {"sizing_id": s["sizing"]["id"]})["structuredContent"]
    assert g["rerun_inputs_complete"] is True
    assert g["rerun_inputs"]["workload"]["total_vcpus"] == 12
    assert g["sizing"]["source_type"] == "rvtools"


# ── merging ──────────────────────────────────────────────────────────────────

def test_merge_uploads(world):
    c, ids, files = world
    ua, _ = _upload(c, files["a"])
    ub, _ = _upload(c, files["b"])
    r = call(c, "merge_uploads", {"upload_ids": [ua, ub]})
    assert r["isError"] is False, r["content"][0]["text"]
    s = r["structuredContent"]
    assert s["upload_id"].startswith("m_")
    assert [src["upload_id"] for src in s["sources"]] == [ua, ub]
    ov = [o for o in s["overlaps"] if o["name"] == "web01"]
    assert ov and ov[0]["kind"] == "vm" and ov[0]["possible_duplicate"] is False
    assert {cp["upload_id"] for cp in ov[0]["copies"]} == {ua, ub}
    assert ov[0]["default"] == ub          # the newer file's copy
    # Two files naming a cluster "Prod" that share no hosts default to two
    # targets (import_merge._default_targets); the user maps them together.
    assert [t["name"] for t in s["targets"]] == ["Prod · a", "Prod · b"]
    assert [g["cluster"] for g in s["groups"]] == ["Prod", "Prod"]
    mapping = [{"group": g["key"], "target": "Prod"} for g in s["groups"]]
    s = call(c, "merge_uploads", {"upload_ids": [ua, ub], "mapping": mapping})["structuredContent"]
    assert [t["name"] for t in s["targets"]] == ["Prod"]
    t = s["targets"][0]
    assert t["hosts"] == 3 and t["vms"] == 5            # web01 once
    assert t["inputs"]["workload"]["total_vcpus"] == 4 + 8 + 6 + 1
    assert all("text" in w for w in t["import_warnings"])
    # Keep both copies instead: one more VM.
    s2 = call(c, "merge_uploads", {"upload_ids": [ua, ub], "mapping": mapping,
                                   "resolutions": [{"key": ov[0]["key"], "keep": "both"}]})["structuredContent"]
    assert s2["targets"][0]["vms"] == 6
    # The merge is sized and listed like any upload.
    mid = s["upload_id"]
    g = call(c, "get_upload", {"upload_id": mid})["structuredContent"]
    assert g["kind"] == "merged" and g["assessment"]["source"]["files"] == 2
    assert [cl["name"] for cl in g["assessment"]["clusters"]] == ["Prod"]
    v = call(c, "list_upload_vms", {"upload_id": mid, "sort": "name", "descending": False})["structuredContent"]
    assert [x["name"] for x in v["vms"]] == ["app02", "db01", "old01", "vCLS-9a1", "web01"]
    out = call(c, "create_sizing", {"from_upload": {"upload_id": mid}})["structuredContent"]
    assert out["inputs_used"]["workload"]["total_vcpus"] == 19
    out = call(c, "create_sizing", {"from_upload": {"upload_id": mid, "source_cluster": "Prod",
                                                     "exclude_compute": [0]}})["structuredContent"]
    assert out["inputs_used"]["workload"]["total_vcpus"] < 19
    r = call(c, "save_sizing", {"name": "Merged", "from_upload": {"upload_id": mid}})
    assert r["isError"] is False, r["content"][0]["text"]
    with appmod.app.app_context():
        cfg = db.session.get(Configuration, r["structuredContent"]["sizing"]["id"])
        im = cfg.payload["import"]
        assert len(im["sources"]) == 2 and len(im["importVms"]) == 5
        assert {v["src"] for v in im["importVms"]} == {src["id"] for src in im["sources"]}
        assert len(cfg.source_meta["files"]) == 2 and cfg.source_meta["vm_count"] == 6
        assert cfg.source_meta.get("cluster") is None      # the whole estate was sized
    # A merge cannot be merged again.
    assert _err(call(c, "merge_uploads", {"upload_ids": [mid]}))["code"] == "invalid_input"


# ── BOM ──────────────────────────────────────────────────────────────────────

def test_bom_upload_records_the_project_check(world):
    c, ids, files = world
    assert _err(call(c, "start_upload", {"kind": "bom"}))["invalid"][0]["field"] == "project_id"
    pid = c.post("/api/projects/", json={"name": "ACME"}).get_json()["id"]
    uid, token = _start(c, kind="bom", project_id=pid)
    r = c.get("/mcp/upload/" + token)
    assert r.status_code == 200 and b"hardware BOM" in r.data and b"ACME" in r.data
    # The page posts the BOM to the project's own route, then reports the check.
    with open(FIX, "rb") as fh:
        body = fh.read()
    r = c.post("/api/projects/%d/bom-checks" % pid,
               data={"file": (io.BytesIO(body), "template.xlsx")}, content_type="multipart/form-data")
    assert r.status_code == 201, r.get_json()
    check_id = r.get_json()["id"]
    r = c.post("/mcp/upload/%s/done" % token, json={"check_id": 999999})
    assert r.status_code == 400
    r = c.post("/mcp/upload/%s/done" % token, json={"check_id": check_id})
    assert r.status_code == 200
    s = call(c, "get_upload", {"upload_id": uid})["structuredContent"]
    assert s["status"] == "ready" and s["kind"] == "bom"
    assert s["bom"]["check_id"] == check_id and s["bom"]["result"]["technical"]["verdict"]
    assert _offending_keys(s) == []
    # A colleague (same tenant) sees the project but may not add to it; a
    # sizing outside the project is refused too.
    appmod.app.config["MCP_TEST_USER_ID"] = ids["bob"]
    assert _err(call(c, "start_upload", {"kind": "bom", "project_id": pid}))["code"] == "not_owner"
    appmod.app.config["MCP_TEST_USER_ID"] = ids["alice"]
    assert _err(call(c, "start_upload", {"kind": "bom", "project_id": pid, "sizing_id": 4242}))["code"] == "not_found"
    assert _err(call(c, "start_upload", {"kind": "assessment", "project_id": pid}))["code"] == "invalid_input"


def test_real_export_metadata_is_stored_as_json(world, tmp_path):
    """RVTools writes vMetaData "xlsx creation datetime" as a datetime cell,
    which the parser passes through; the stored record must still be plain
    JSON (the acceptance server answered 500 on the first real export)."""
    from datetime import datetime as _dt
    from mcp_uploads import McpUpload
    c, ids, files = world
    path = rvtools(str(tmp_path / "real.xlsx"), HOSTS, VMS, DATASTORES,
                   date=_dt(2026, 9, 7, 8, 6, 28))
    uid, token = _start(c)
    r = _post_file(c, token, path)
    assert r.status_code == 200, r.get_json()
    with appmod.app.app_context():
        row = McpUpload.query.filter_by(token=token).one()
        assert json.dumps(row.data, allow_nan=False)
        assert row.data["project"]["xlsx creation datetime"] == "2026-09-07T08:06:28"
    s = call(c, "get_upload", {"upload_id": uid})["structuredContent"]
    assert s["status"] == "ready" and s["assessment"]["source"]["vm_count"] == 4
