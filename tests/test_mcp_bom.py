"""BOM rows over MCP (docs/mcp/01-tool-design.md §3.12, §3.13).

Locks in:
  - check_bom_rows runs the agent pipeline minus the model call on rows in
    the agent's schema, marks the result caller-extracted and not grounded,
    and flags it for review (instructions in the document add a reason)
  - rows that fail the template round trip come back as bom_template_errors;
    malformed rows as invalid_input
  - save_bom_check stores a BomCheck with the mcp_caller format in an owned
    project, with a review open, visible on the project's BOM list; refuses
    someone else's project and a sizing outside it
  - nothing price-shaped in the outputs

Run: .venv/bin/python -m pytest tests/test_mcp_bom.py -q
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
from bom_models import BomCheck, REVIEW_OPEN  # noqa: E402
from database import db  # noqa: E402
from test_mcp_server import call  # noqa: E402
from test_mcp_uploads import _login, _signup  # noqa: E402
from test_security import _offending_keys  # noqa: E402
from test_sizing_service import WORKLOAD, _seed_catalog  # noqa: E402
from extensions import limiter  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "bom", "normalized",
                   "synthetic_dell_quote.xlsx.json")


def _rows():
    with open(FIX, encoding="utf-8") as fh:
        d = json.load(fh)
    return {"vendor": d["vendor"], "configs": d["configs"]}


@pytest.fixture()
def world():
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
    pid = c.post("/api/projects/", json={"name": "ACME"}).get_json()["id"]
    yield c, ids, pid
    app.config.pop("MCP_TEST_USER_ID", None)


def _err(r):
    assert r["isError"], r
    return json.loads(r["content"][0]["text"])["error"]


def test_check_bom_rows(world):
    c, ids, pid = world
    r = call(c, "check_bom_rows", dict(_rows(), source_description="Dell quote PDF"))
    assert r["isError"] is False, r["content"][0]["text"]
    s = r["structuredContent"]
    assert s["provenance"] == {"extracted_by": "caller", "grounded": False,
                               "note": s["provenance"]["note"], "instructions_detected": False,
                               "demoted_lines": s["provenance"]["demoted_lines"],
                               "source_description": "Dell quote PDF"}
    assert s["technical"]["verdict"] in ("PASS", "FAIL", "INCONCLUSIVE")
    assert s["technical"]["config_results"][0]["configName"] == "Production"
    assert s["technical"]["config_results"][0]["findings"]
    assert "agent_ungrounded" in s["flag_reasons"] and "agent_instructions" not in s["flag_reasons"]
    assert s["fit"] is None and s["catalog_stamp"]
    assert _offending_keys(s) == []
    text = r["content"][0]["text"]
    assert text.startswith("BOM check: ") and "Not grounded" in text
    # A document that tried to steer the reader adds the review reason.
    s = call(c, "check_bom_rows", dict(_rows(), documentContainsInstructions=True))["structuredContent"]
    assert "agent_instructions" in s["flag_reasons"]
    assert s["provenance"]["instructions_detected"] is True


def test_bad_rows(world):
    c, ids, pid = world
    rows = _rows()
    del rows["configs"][0]["components"][0]["category"]
    err = _err(call(c, "check_bom_rows", rows))
    assert err["code"] == "invalid_input"
    assert err["missing"] == ["configs[0].components[0].category"]
    rows = _rows()
    rows["configs"][0]["components"][0]["quantity"] = 0
    assert _err(call(c, "check_bom_rows", rows))["code"] == "invalid_input"
    # Compare against a sizing the caller cannot see.
    assert _err(call(c, "check_bom_rows", dict(_rows(), sizing_id=4242)))["code"] == "not_found"


def test_check_against_a_sizing(world):
    c, ids, pid = world
    saved = call(c, "save_sizing", {"name": "HQ", "workload": WORKLOAD,
                                    "project_id": pid})["structuredContent"]
    s = call(c, "check_bom_rows", dict(_rows(), sizing_id=saved["sizing"]["id"]))["structuredContent"]
    assert s["fit"] and s["fit"]["sizing"]["id"] == saved["sizing"]["id"]
    assert s["fit"]["verdict"]


def test_save_bom_check(world):
    c, ids, pid = world
    r = call(c, "save_bom_check", dict(_rows(), project_id=pid, source_description="Dell quote PDF"))
    assert r["isError"] is False, r["content"][0]["text"]
    s = r["structuredContent"]
    assert s["project"] == {"id": pid, "name": "ACME"} and s["open_url"].endswith("/?project=%d" % pid)
    assert _offending_keys(s) == []
    with appmod.app.app_context():
        check = db.session.get(BomCheck, s["check_id"])
        assert check.file_format == "mcp_caller" and check.review_status == REVIEW_OPEN
        assert check.user_id == ids["alice"] and check.project_id == pid
        assert check.name == "Dell quote PDF" and check.filename in (None, "")
        assert check.ingest_meta["source_kind"] == "mcp_caller"
        assert check.ingest_meta["grounded"] is False
        assert "agent_ungrounded" in check.flag_reasons
        from auth_models import AdminAuditLog
        assert AdminAuditLog.query.filter_by(action="mcp.save_bom_check").count() == 1
    # On the project's BOM list, with a label the UI can show.
    _login(c, "alice@partnerco.example")
    rows = c.get("/api/projects/%d/bom-checks" % pid).get_json()
    assert [x["id"] for x in rows] == [s["check_id"]]
    assert rows[0]["file_format"] == "mcp_caller"
    caps = c.get("/api/bom/capabilities").get_json()
    assert "mcp_caller" in caps["formats"]
    # Refusals: a colleague's project (visible, read-only), an invisible one,
    # a sizing outside the project.
    appmod.app.config["MCP_TEST_USER_ID"] = ids["bob"]
    assert _err(call(c, "save_bom_check", dict(_rows(), project_id=pid)))["code"] == "not_owner"
    assert _err(call(c, "save_bom_check", dict(_rows(), project_id=999)))["code"] == "not_found"
    appmod.app.config["MCP_TEST_USER_ID"] = ids["alice"]
    assert _err(call(c, "save_bom_check", dict(_rows(), project_id=pid, sizing_id=4242)))["code"] == "not_found"
