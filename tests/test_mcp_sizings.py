"""The MCP tools over projects and saved sizings (docs/mcp/01-tool-design.md
§3.6–§3.9a, §5).

Locks in:
  - list_projects / list_sizings show what the REST routes show, scoped and
    paged, with 'mine' as the default
  - get_sizing presents a stored result, says when it is stale, and builds
    rerun_inputs (complete from a canonical block, partial from a UI save)
  - a sizing outside the caller's visibility is not_found, and the role cap
    keeps a super admin inside their own tenant
  - open_shared_sizing links a partner sizing for a Scale user, and refuses
    an ordinary user's foreign code
  - save_sizing writes a payload the UI's own save path produces (manual
    fields + canonical block), a fresh result snapshot, under an owned or
    new project; refuses someone else's project, a moved ranking, an
    unrepresentable ratio; never overwrites
  - nothing price-shaped in any output

Run: .venv/bin/python -m pytest tests/test_mcp_sizings.py -q
"""
import json
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.pop("SESSION_COOKIE_SECURE", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

import app as appmod  # noqa: E402
import mcp_payload  # noqa: E402
from auth_models import Configuration, Tenant, User  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from test_mcp_server import call  # noqa: E402
from test_security import _offending_keys  # noqa: E402
from test_sizing_service import SUMMARY, WORKLOAD, _seed_catalog  # noqa: E402

PW = "Abcdef1!xy"


def _signup(client, email):
    r = client.post("/api/auth/signup", json={"email": email, "password": PW,
                                              "accept_privacy": True})
    assert r.status_code in (200, 201), r.get_json()
    with appmod.app.app_context():
        return User.query.filter_by(email=email).one().id


def _login(client, email):
    client.post("/api/auth/logout")
    r = client.post("/api/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.get_json()


def _as(uid):
    appmod.app.config["MCP_TEST_USER_ID"] = uid


@pytest.fixture()
def world():
    """Two partner users in one tenant, one in another, one Scale user; a
    project and a UI-saved sizing per partner."""
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
           "bob": _signup(c, "bob@partnerco.example"),
           "carol": _signup(c, "carol@otherco.example"),
           "sam": _signup(c, "sam@scale.example")}
    with app.app_context():
        sam = db.session.get(User, ids["sam"])
        sam.tenant.is_scale = True
        db.session.commit()
    sizings = {}
    for who in ("alice", "carol"):
        _login(c, "%s@%s.example" % (who, "partnerco" if who == "alice" else "otherco"))
        pid = c.post("/api/projects/", json={"name": who.title() + " project",
                                             "customer_name": "ACME " + who}).get_json()["id"]
        payload = {"version": 4, "mode": "manual",
                   "fields": {"man-vcpus": "148", "man-prov-ram": "423", "man-ds-used": "8.2",
                              "ratio-slider": "3", "max-day-one-storage": "50",
                              "max-day-one-ram": "50", "growth-pct": "10"},
                   "manual": {"selectedRec": {}}}
        row = c.post("/api/configs/", json={"name": who.title() + " sizing", "project_id": pid,
                                            "payload": payload}).get_json()
        # Store a result the way the browser does.
        res = c.post("/api/recommend", json={"summary": SUMMARY}).get_json()
        snap = {"clusters": [{"name": "Cluster", "summary": SUMMARY,
                              "recommendation": res["recommendations"][0],
                              "projection": res["projection"], "source_perf": None,
                              "replicates_to": "", "refs": res["recommendations"][0]["refs"]}],
                "totals": None}
        r = c.put("/api/sizings/%d/result" % row["id"], json=snap)
        assert r.status_code == 200, r.get_json()
        sizings[who] = {"project_id": pid, "id": row["id"], "code": row["code"]}
    c.post("/api/auth/logout")
    yield c, ids, sizings
    app.config.pop("MCP_TEST_USER_ID", None)


def _err(r):
    assert r["isError"], r
    return json.loads(r["content"][0]["text"])["error"]


# ── listing ──────────────────────────────────────────────────────────────────

def test_list_projects_and_sizings_scoped(world):
    c, ids, sz = world
    _as(ids["alice"])
    s = call(c, "list_projects", {})["structuredContent"]
    assert [p["name"] for p in s["projects"]] == ["Alice project"]
    assert s["projects"][0]["sizing_count"] == 1 and s["projects"][0]["can_edit"] is True
    assert _offending_keys(s) == []
    s = call(c, "list_projects", {"query": "acme"})["structuredContent"]
    assert s["total"] == 1
    s = call(c, "list_sizings", {})["structuredContent"]
    assert [r["name"] for r in s["sizings"]] == ["Alice sizing"]
    row = s["sizings"][0]
    assert row["project"]["name"] == "Alice project" and row["has_result"] is True
    assert row["source_type"] == "manual" and row["source"] == "owned"
    # Bob (same tenant) sees Alice's work only when asking for all visible.
    _as(ids["bob"])
    assert call(c, "list_sizings", {})["structuredContent"]["total"] == 0
    s = call(c, "list_sizings", {"scope": "all_visible"})["structuredContent"]
    assert s["total"] == 1 and s["sizings"][0]["source"] == "tenant"
    assert call(c, "list_projects", {})["structuredContent"]["total"] == 0
    assert call(c, "list_projects", {"scope": "organization"})["structuredContent"]["total"] == 1
    # Carol (other tenant) sees nothing of theirs.
    _as(ids["carol"])
    s = call(c, "list_sizings", {"scope": "all_visible"})["structuredContent"]
    assert [r["name"] for r in s["sizings"]] == ["Carol sizing"]
    # Paging.
    s = call(c, "list_sizings", {"scope": "all_visible", "limit": 1, "offset": 5})["structuredContent"]
    assert s["sizings"] == [] and s["next_offset"] is None and s["total"] == 1


def test_role_cap_keeps_admins_in_their_tenant(world):
    c, ids, sz = world
    with appmod.app.app_context():
        db.session.get(User, ids["bob"]).role = "super_admin"
        db.session.commit()
    _as(ids["bob"])
    s = call(c, "list_sizings", {"scope": "all_visible"})["structuredContent"]
    assert [r["name"] for r in s["sizings"]] == ["Alice sizing"]     # not Carol's
    r = call(c, "get_sizing", {"sizing_id": sz["carol"]["id"]})
    assert _err(r)["code"] == "not_found"
    r = call(c, "open_shared_sizing", {"share_code": sz["carol"]["code"]})
    assert _err(r)["code"] == "not_found"
    # Nor may a capped super admin save into another owner's project.
    r = call(c, "save_sizing", {"name": "x", "workload": WORKLOAD,
                                "project_id": sz["alice"]["project_id"]})
    assert _err(r)["code"] == "not_owner"


# ── get / open ───────────────────────────────────────────────────────────────

def test_get_sizing_presents_a_ui_saved_result(world):
    c, ids, sz = world
    _as(ids["alice"])
    r = call(c, "get_sizing", {"sizing_id": sz["alice"]["id"]})
    assert r["isError"] is False
    s = r["structuredContent"]
    assert s["sizing"]["code"] == sz["alice"]["code"] and s["sizing"]["can_edit"] is True
    assert s["result_state"]["cache"] == "fresh"
    cl = s["clusters"][0]
    assert cl["kind"] == "proposal"
    assert cl["inputs"]["workload"]["total_vcpus"] == 148
    assert cl["selected_option"]["model"] == "TEST-HCI"
    assert cl["selected_option"]["determinant"]["statement"].startswith("Determined by")
    assert [ch["id"] for ch in cl["selected_option"]["checks"]]
    assert s["rerun_inputs_complete"] is False
    assert s["rerun_inputs"]["workload"]["largest_vm_ram_gb"] == 64
    assert s["rerun_inputs"]["params"]["vcpu_ratio"] == 3.0
    assert s["rerun_inputs"]["params"]["growth_pct"] == 10
    assert any("incomplete" in w for w in s["warnings"])
    assert _offending_keys(s) == []
    assert r["content"][0]["text"].startswith("Sizing 'Alice sizing'")
    # Bob can read it (same tenant), cannot edit it.
    _as(ids["bob"])
    s = call(c, "get_sizing", {"sizing_id": sz["alice"]["id"]})["structuredContent"]
    assert s["sizing"]["can_edit"] is False and s["sizing"]["source"] == "tenant"
    assert _err(call(c, "get_sizing", {"sizing_id": 999999}))["code"] == "not_found"


def test_stale_result_is_reported(world):
    c, ids, sz = world
    _as(ids["alice"])
    with appmod.app.app_context():
        from orm_models import SizingSetting
        row = SizingSetting.query.filter_by(key="default_vcpu_ratio").first()
        row.value = 4.0
        db.session.commit()
    s = call(c, "get_sizing", {"sizing_id": sz["alice"]["id"]})["structuredContent"]
    assert s["result_state"]["cache"] == "stale"
    assert any("stale" in w for w in s["warnings"])


def test_open_shared_sizing(world):
    c, ids, sz = world
    _as(ids["sam"])
    r = call(c, "open_shared_sizing", {"share_code": sz["carol"]["code"]})
    assert r["isError"] is False
    s = r["structuredContent"]
    assert s["sizing"]["source"] == "linked" and s["sizing"]["can_edit"] is False
    assert any("linked" in w for w in s["warnings"])
    # Now in Sam's list, as the UI would show it.
    s = call(c, "list_sizings", {"scope": "all_visible"})["structuredContent"]
    assert "Carol sizing" in [row["name"] for row in s["sizings"]]
    # A partner cannot open a foreign tenant's code.
    _as(ids["alice"])
    assert _err(call(c, "open_shared_sizing", {"share_code": sz["carol"]["code"]}))["code"] == "not_found"
    assert _err(call(c, "open_shared_sizing", {"share_code": "abcdefghijkl"}))["code"] == "invalid_input"


# ── save ─────────────────────────────────────────────────────────────────────

def test_save_sizing_writes_what_the_ui_reads(world):
    c, ids, sz = world
    _as(ids["alice"])
    pid = sz["alice"]["project_id"]
    r = call(c, "save_sizing", {"name": "ACME HQ", "workload": dict(WORKLOAD, nic_speed_gbe=25),
                                "params": {"vcpu_ratio": 3.5, "years": 3, "snapshot_pct": 15},
                                "project_id": pid, "notes": "Agreed with the customer",
                                "option_rank": 1,
                                "expected_option": {"model": "TEST-HCI", "node_count": 3}})
    if r["isError"]:
        err = _err(r)
        assert err["code"] == "option_changed", err
        # The fixture fit ranks differently: take what it offers.
        exp = err["options"][0]
        r = call(c, "save_sizing", {"name": "ACME HQ", "workload": dict(WORKLOAD, nic_speed_gbe=25),
                                    "params": {"vcpu_ratio": 3.5, "years": 3, "snapshot_pct": 15},
                                    "project_id": pid, "notes": "Agreed with the customer",
                                    "expected_option": {"model": exp["model"],
                                                        "node_count": exp["node_count"]}})
    assert r["isError"] is False, r["content"][0]["text"]
    s = r["structuredContent"]
    assert s["sizing"]["project"] == {"id": pid, "name": "Alice project", "created": False}
    assert s["open_url"].endswith("/?project=%d&sizing=%d" % (pid, s["sizing"]["id"]))
    assert len(s["sizing"]["code"]) == 12
    assert s["saved_option"]["rank"] == 1
    assert _offending_keys(s) == []

    with appmod.app.app_context():
        cfg = db.session.get(Configuration, s["sizing"]["id"])
        assert cfg.owner_id == ids["alice"] and cfg.project_id == pid
        assert cfg.notes == "Agreed with the customer" and cfg.position == 1
        p = cfg.payload
        assert p["version"] == 4 and p["mode"] == "manual"
        f = p["fields"]
        assert f["man-vcpus"] == "148" and f["man-prov-ram"] == "423"
        assert f["man-largest-vcpus"] == "16" and f["man-largest-ram"] == "64"
        assert f["man-nic-speed"] == "25000" and f["man-active-vms"] == "59"
        assert f["ratio-slider"] == "3.5" and f["growth-years"] == "3"
        assert f["snapshot-pct"] == "15" and f["growth-pct"] == "10"
        assert f["separate-storage-growth"] is False and f["size-full-cluster"] is False
        assert f["storage-pref"] == "auto" and f["sizing-mode"] == "certified"
        assert f["license-term-years"] == "5" and f["guest-licensing"] == "windows"
        assert set(f) >= set(mcp_payload.MANUAL_FIELDS) - {"man-platform", "man-cluster",
                                                            "man-hosts",
                                                            "man-threads", "man-host-ram",
                                                            "man-peak-cpu", "man-avg-cpu",
                                                            "man-peak-mem", "man-avg-mem",
                                                            "man-avg-iops", "man-peak-iops",
                                                            "man-total-vms", "man-used-ram",
                                                            "man-prov-storage", "man-ds-total"}
        canon = p["canonical"]
        assert canon["version"] == 1 and canon["source"] == "mcp"
        assert canon["summary"]["total_vcpus"] == 148 and canon["params"]["vcpu_ratio"] == 3.5
        assert canon["selected_option"] == 1
        snap = cfg.result_snapshot
        assert snap["clusters"][0]["recommendation"]["model"] == "TEST-HCI"
        assert snap["clusters"][0]["refs"]["mode"] == "appliance"
        assert snap["clusters"][0]["name"] == "Cluster"
        assert cfg.result_fingerprint and cfg.result_computed_at
        from fingerprint import result_state
        assert result_state(cfg)["cache"] == "fresh"
        from auth_models import AdminAuditLog
        assert AdminAuditLog.query.filter_by(action="mcp.save_sizing").count() == 1

    # Readable back through the API as the UI would load it, and through get_sizing
    # with complete rerun inputs.
    _login(c, "alice@partnerco.example")
    api = c.get("/api/configs/%d" % s["sizing"]["id"]).get_json()
    assert api["payload"]["fields"]["man-vcpus"] == "148" and api["has_result"] is True
    g = call(c, "get_sizing", {"sizing_id": s["sizing"]["id"]})["structuredContent"]
    assert g["rerun_inputs_complete"] is True
    assert g["rerun_inputs"]["params"]["years"] == 3
    assert g["rerun_inputs"]["workload"]["nic_speed_gbe"] == 25
    assert g["result_state"]["cache"] == "fresh"
    # Re-sizing from rerun_inputs reproduces the saved option.
    again = call(c, "create_sizing", g["rerun_inputs"])["structuredContent"]
    assert again["options"][0]["model"] == s["saved_option"]["model"]
    assert again["options"][0]["node_count"] == s["saved_option"]["node_count"]


def test_save_sizing_projects_and_refusals(world):
    c, ids, sz = world
    _as(ids["alice"])
    # Default: the personal Unfiled project, created on first use.
    s = call(c, "save_sizing", {"name": "Quick", "workload": WORKLOAD})["structuredContent"]
    assert s["sizing"]["project"]["name"] == "Unfiled"
    # A new project.
    s = call(c, "save_sizing", {"name": "Site B", "workload": WORKLOAD,
                                "new_project_name": "Globex"})["structuredContent"]
    assert s["sizing"]["project"]["created"] is True and s["sizing"]["project"]["name"] == "Globex"
    s2 = call(c, "list_projects", {})["structuredContent"]
    assert "Globex" in [p["name"] for p in s2["projects"]]
    # Both project fields at once.
    r = call(c, "save_sizing", {"name": "x", "workload": WORKLOAD, "project_id": 1,
                                "new_project_name": "y"})
    assert _err(r)["invalid"][0]["field"] == "new_project_name"
    # Someone else's project.
    r = call(c, "save_sizing", {"name": "x", "workload": WORKLOAD,
                                "project_id": sz["carol"]["project_id"]})
    assert _err(r)["code"] == "not_found"            # not even visible
    _as(ids["bob"])
    r = call(c, "save_sizing", {"name": "x", "workload": WORKLOAD,
                                "project_id": sz["alice"]["project_id"]})
    assert _err(r)["code"] == "not_owner"
    # A ratio the UI cannot show.
    _as(ids["alice"])
    r = call(c, "save_sizing", {"name": "x", "workload": WORKLOAD, "params": {"vcpu_ratio": 9}})
    assert _err(r)["invalid"][0]["field"] == "params.vcpu_ratio"
    r = call(c, "save_sizing", {"name": "x", "workload": WORKLOAD, "params": {"vcpu_ratio": 3.3}})
    assert _err(r)["code"] == "invalid_input"
    # Nothing fits: no_fit, nothing written.
    before = _count()
    r = call(c, "save_sizing", {"name": "x", "workload": dict(WORKLOAD, largest_vm_vcpus=5000)})
    assert _err(r)["code"] == "no_fit" and _count() == before
    # A moved ranking: option_changed with the current list, nothing written.
    r = call(c, "save_sizing", {"name": "x", "workload": WORKLOAD,
                                "expected_option": {"model": "OTHER", "node_count": 99}})
    err = _err(r)
    assert err["code"] == "option_changed" and err["options"][0]["model"] == "TEST-HCI"
    assert _count() == before
    r = call(c, "save_sizing", {"name": "x", "workload": WORKLOAD, "option_rank": 8})
    assert _err(r)["code"] == "option_changed"


def _count():
    with appmod.app.app_context():
        return Configuration.query.count()


def test_payload_writer_shapes():
    params = {"vcpu_ratio": 3.0, "years": 5, "growth_pct": 10, "storage_growth_pct": 25,
              "snapshot_pct": 20, "max_day_one_storage_pct": 50.0, "max_day_one_ram_pct": 50.0,
              "target_nodes": 4, "storage_preference": "flash", "sizing_mode": "validated",
              "vendor": "lenovo", "size_cpu_for_full_cluster": True,
              "allow_storage_only_nodes": False, "target_model": "HE150",
              "include_eol_eos": True, "license_term_years": 3}
    f = mcp_payload.shared_fields(params, "windows_db")
    assert f["separate-storage-growth"] is True and f["storage-growth-pct"] == "25"
    assert f["target-nodes"] == "4" and f["sizing-vendor"] == "lenovo"
    assert f["size-full-cluster"] is True and f["sizing-include-eol"] is True
    assert f["license-term-years"] == "3" and f["guest-licensing"] == "windows_db"
    assert f["ratio-slider"] == "3" and f["max-day-one-storage"] == "50"
    assert mcp_payload.ratio_representable(3.25) and not mcp_payload.ratio_representable(3.3)
    assert not mcp_payload.ratio_representable(8.25)
    exp = mcp_payload.source_perf_export(
        {"total_specrate": 677.2, "cpus": [{"model": "A", "sockets": 6, "perf_index": 100.0,
                                             "perf_type": "specrate"},
                                            {"model": "B", "sockets": 1, "perf_index": 20000.0,
                                             "perf_type": "passmark"}]})
    assert exp["cpus"][1] == {"model": "B", "sockets": 1, "type": "passmark", "score": 20000.0,
                              "specrate": 77.2, "total": 77.2}
    assert mcp_payload.source_perf_export({"total_specrate": None, "cpus": []}) is None


# ── create_project ───────────────────────────────────────────────────────────

def test_create_project_is_the_new_project_button(world):
    """A colleague's first trial: "create a project for customer X" had no tool
    to call, and a BOM check for a new customer needs a project first."""
    c, ids, sz = world
    _as(ids["alice"])
    s = call(c, "create_project", {"name": "  Initech refresh ", "customer_name": "Initech",
                                   "opportunity_ref": "OPP-42"})["structuredContent"]
    p = s["project"]
    assert p["name"] == "Initech refresh" and p["customer_name"] == "Initech"
    assert p["opportunity_ref"] == "OPP-42" and p["can_edit"] is True
    assert p["sizing_count"] == 0 and p["is_scratch"] is False and p["source"] == "owned"
    assert s["open_url"].endswith("/?project=%d" % p["id"])
    assert _offending_keys(s) == []
    # It lists, empty, and takes a sizing by id.
    names = [q["name"] for q in call(c, "list_projects", {})["structuredContent"]["projects"]]
    assert names[0] == "Initech refresh"
    saved = call(c, "save_sizing", {"name": "Site A", "workload": WORKLOAD,
                                    "project_id": p["id"]})["structuredContent"]
    assert saved["sizing"]["project"]["id"] == p["id"] and saved["sizing"]["project"]["created"] is False
    # Only a name is required, and a blank one is refused.
    r = call(c, "create_project", {"name": "   "})
    assert _err(r)["code"] == "invalid_input"
    # Invisible to another tenant; visible to a colleague as not editable.
    _as(ids["carol"])
    assert "Initech refresh" not in [q["name"] for q in call(
        c, "list_projects", {"scope": "organization"})["structuredContent"]["projects"]]
    _as(ids["bob"])
    rows = call(c, "list_projects", {"scope": "organization"})["structuredContent"]["projects"]
    assert [q["can_edit"] for q in rows if q["name"] == "Initech refresh"] == [False]
    # Audited by id and name, never with figures.
    from auth_models import AdminAuditLog
    with appmod.app.app_context():
        row = AdminAuditLog.query.filter_by(action="mcp.create_project").one()
        assert "Initech refresh" in row.detail and str(p["id"]) in row.detail
