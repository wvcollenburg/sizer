"""The MCP endpoint and the stateless tools (docs/mcp/01-tool-design.md §0,
§3.1-§3.5, §4, §5; spec 2026-07-28 and 2025-11-25).

Locks in:
  - transport: 401 with the resource_metadata challenge when unauthenticated;
    405 on GET; 403 on a foreign Origin; modern requests need matching
    MCP-Protocol-Version/_meta and Mcp-Method/Mcp-Name headers (-32020),
    unsupported versions get -32022 with the supported list, unknown methods
    404/-32601; legacy clients get initialize/ping/tools without resultType;
    notifications are 202; no session id is ever minted
  - every tool is published with a title and all four annotation hints, with
    a flat schema (no $ref/oneOf/anyOf/allOf)
  - tools/call renders structuredContent plus text = summary + JSON; a tool
    error is isError with the §4 body and no structuredContent
  - the stateless tools: constants allowlist (no scoring weights), catalog
    without cost_tier, CPU lookup, validate_inputs and create_sizing parity
    with /api/recommend, the role cap, and no price-shaped keys anywhere

Run: .venv/bin/python -m pytest tests/test_mcp_server.py -q
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
import mcp_registry  # noqa: E402
import mcp_schema  # noqa: E402
import mcp_tools  # noqa: E402
from auth_models import User  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from test_security import _offending_keys  # noqa: E402
from test_sizing_service import SUMMARY, WORKLOAD, _seed_catalog  # noqa: E402

MODERN = "2026-07-28"
LEGACY = "2025-11-25"
META = {"io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "0"},
        "io.modelcontextprotocol/clientCapabilities": {}}


@pytest.fixture()
def client():
    app = appmod.app
    app.config["TESTING"] = True
    app.config["RATELIMIT_ENABLED"] = False
    app.config.pop("MCP_TEST_USER_ID", None)
    limiter.enabled = False
    with app.app_context():
        db.drop_all()
        db.create_all()
        _seed_catalog()
    c = app.test_client()
    c.post("/api/auth/signup", json={"email": "alice@examplecorp.com",
                                     "password": "Abcdef1!xy", "accept_privacy": True})
    with app.app_context():
        app.config["MCP_TEST_USER_ID"] = User.query.filter_by(email="alice@examplecorp.com").one().id
    yield c
    app.config.pop("MCP_TEST_USER_ID", None)


def _post(client, body, headers=None):
    return client.post("/mcp", data=json.dumps(body), content_type="application/json",
                       headers=headers or {})


def modern(client, method, params=None, rid=1, headers=None):
    params = dict(params or {})
    params["_meta"] = META
    h = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method,
         "Accept": "application/json, text/event-stream"}
    if method == "tools/call":
        h["Mcp-Name"] = params.get("name", "")
    h.update(headers or {})
    body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
    return _post(client, body, h)


def legacy(client, method, params=None, rid=1, notification=False):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if not notification:
        body["id"] = rid
    return _post(client, body, {"MCP-Protocol-Version": LEGACY})


def call(client, name, arguments, era=modern):
    res = era(client, "tools/call", {"name": name, "arguments": arguments})
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()["result"]


# ── transport ────────────────────────────────────────────────────────────────

def test_unauthenticated_gets_the_401_challenge(client):
    appmod.app.config.pop("MCP_TEST_USER_ID")
    res = modern(client, "server/discover")
    assert res.status_code == 401
    www = res.headers["WWW-Authenticate"]
    assert www.startswith("Bearer ")
    assert 'resource_metadata="' in www and "/.well-known/oauth-protected-resource/mcp" in www
    assert 'scope="sizing"' in www


def test_get_and_delete_are_405(client):
    assert client.get("/mcp").status_code == 405
    assert client.delete("/mcp").status_code == 405


def test_foreign_origin_is_refused_and_own_origin_allowed(client):
    res = modern(client, "ping", headers={"Origin": "https://evil.example"})
    assert res.status_code == 403
    res = modern(client, "ping", headers={"Origin": "http://localhost"})
    assert res.status_code == 200


def test_modern_discover_and_version_checks(client):
    res = modern(client, "server/discover")
    assert res.status_code == 200
    r = res.get_json()["result"]
    assert r["resultType"] == "complete"
    assert r["supportedVersions"] == [MODERN]
    assert r["capabilities"] == {"tools": {}}
    assert r["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "scale-sizer"
    assert "Mcp-Session-Id" not in res.headers

    # Header and _meta must agree.
    res = modern(client, "ping", headers={"MCP-Protocol-Version": LEGACY})
    assert res.status_code == 400 and res.get_json()["error"]["code"] == -32020
    # Unsupported modern version: -32022 with the supported list.
    params = {"_meta": dict(META, **{"io.modelcontextprotocol/protocolVersion": "2099-01-01"})}
    res = _post(client, {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": params},
                {"MCP-Protocol-Version": "2099-01-01", "Mcp-Method": "ping"})
    assert res.status_code == 400
    err = res.get_json()["error"]
    assert err["code"] == -32022 and MODERN in err["data"]["supported"]
    assert err["data"]["requested"] == "2099-01-01"
    # Mirrored headers are required.
    res = modern(client, "ping", headers={"Mcp-Method": "tools/list"})
    assert res.status_code == 400 and res.get_json()["error"]["code"] == -32020
    res = modern(client, "tools/call", {"name": "get_constants", "arguments": {}},
                 headers={"Mcp-Name": "other"})
    assert res.status_code == 400 and res.get_json()["error"]["code"] == -32020
    # Base64 sentinel on Mcp-Name decodes.
    import base64
    enc = "=?base64?" + base64.b64encode(b"get_constants").decode() + "?="
    res = modern(client, "tools/call", {"name": "get_constants", "arguments": {}},
                 headers={"Mcp-Name": enc})
    assert res.status_code == 200
    # Unknown method: 404 + -32601.
    res = modern(client, "resources/list")
    assert res.status_code == 404 and res.get_json()["error"]["code"] == -32601
    # initialize is not a modern method.
    res = modern(client, "initialize")
    assert res.status_code == 404


def test_legacy_handshake(client):
    res = legacy(client, "initialize", {"protocolVersion": LEGACY, "capabilities": {},
                                        "clientInfo": {"name": "claude", "version": "1"}})
    assert res.status_code == 200
    r = res.get_json()["result"]
    assert r["protocolVersion"] == LEGACY and r["capabilities"] == {"tools": {}}
    assert r["serverInfo"]["name"] == "scale-sizer" and "resultType" not in r
    assert "Mcp-Session-Id" not in res.headers
    assert legacy(client, "notifications/initialized", notification=True).status_code == 202
    res = legacy(client, "ping")
    assert res.status_code == 200 and res.get_json()["result"] == {}
    # A pre-2025-06-18 client sends no header at all: still legacy.
    res = _post(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert res.status_code == 200 and "tools" in res.get_json()["result"]
    # An unknown legacy version is refused with the supported list.
    res = _post(client, {"jsonrpc": "2.0", "id": 3, "method": "ping"},
                {"MCP-Protocol-Version": "2024-11-05"})
    assert res.status_code == 400 and res.get_json()["error"]["code"] == -32022
    # Unknown legacy method: JSON-RPC error in a 200.
    res = legacy(client, "prompts/list")
    assert res.status_code == 200 and res.get_json()["error"]["code"] == -32601
    # A session id from an older client is ignored, not echoed.
    res = _post(client, {"jsonrpc": "2.0", "id": 4, "method": "ping"},
                {"MCP-Protocol-Version": LEGACY, "Mcp-Session-Id": "abc"})
    assert res.status_code == 200 and "Mcp-Session-Id" not in res.headers


def test_bad_bodies(client):
    h = {"MCP-Protocol-Version": LEGACY}
    res = client.post("/mcp", data="not json", content_type="application/json", headers=h)
    assert res.status_code == 400 and res.get_json()["error"]["code"] == -32700
    res = _post(client, [{"jsonrpc": "2.0", "id": 1, "method": "ping"}], h)
    assert res.status_code == 400 and res.get_json()["error"]["code"] == -32600
    res = _post(client, {"id": 1, "method": "ping"}, h)
    assert res.status_code == 400 and res.get_json()["error"]["code"] == -32600


def test_tools_list_publishes_titles_annotations_and_flat_schemas(client):
    res = modern(client, "tools/list")
    r = res.get_json()["result"]
    assert r["resultType"] == "complete" and r["ttlMs"] > 0
    names = [t["name"] for t in r["tools"]]
    assert names[:5] == ["get_constants", "list_node_models", "lookup_cpu_benchmark",
                         "validate_inputs", "create_sizing"]
    for t in r["tools"]:
        assert t["title"] and t["description"]
        assert 1 <= len(t["name"]) <= 32 and t["name"].replace("_", "").isalnum()
        ann = t["annotations"]
        assert set(ann) == {"title", "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"}
        assert t["inputSchema"]["type"] == "object"
        assert t["inputSchema"].get("additionalProperties") is False
        text = json.dumps(t["inputSchema"])
        for banned in ('"$ref"', '"oneOf"', '"anyOf"', '"allOf"', '"$schema"'):
            assert banned not in text, (t["name"], banned)
    legacy_tools = legacy(client, "tools/list").get_json()["result"]
    assert "resultType" not in legacy_tools and legacy_tools["tools"] == r["tools"]


def test_unknown_tool_is_a_protocol_error(client):
    res = modern(client, "tools/call", {"name": "nope", "arguments": {}})
    assert res.status_code == 200 and res.get_json()["error"]["code"] == -32602


# ── registry and schema ──────────────────────────────────────────────────────

def test_schema_validator():
    schema = mcp_tools.WORKLOAD_SCHEMA
    assert mcp_schema.validate(schema, WORKLOAD) == []
    probs = mcp_schema.validate(schema, dict(WORKLOAD, total_vcpus="many", peak_cpu_pct=140,
                                             extra=1, source_cpus=[{"sockets": 1}],
                                             guest_licensing="bsd"))
    fields = {p["field"]: p["problem"] for p in probs}
    assert fields["total_vcpus"] == "must be a whole number"
    assert fields["peak_cpu_pct"] == "must be at most 100"
    assert fields["extra"] == "is not a known field"
    assert fields["source_cpus[0].model"] == "is required"
    assert fields["guest_licensing"].startswith("must be one of")
    req = mcp_tools._with_required(schema, mcp_tools.WORKLOAD_REQUIRED)
    probs = mcp_schema.validate(req, {"total_vcpus": 4})
    assert {p["field"] for p in probs} == {"provisioned_ram_gb", "used_storage_tb",
                                           "largest_vm_vcpus", "largest_vm_ram_gb"}
    assert mcp_schema.validate({"type": "object"}, []) == [
        {"field": "(arguments)", "problem": "must be an object"}]
    assert mcp_schema.apply_defaults({"properties": {"a": {"default": 1}, "b": {}}}, {"b": 2}) == {"a": 1, "b": 2}


def test_capped_user_hides_admin_roles(client):
    with appmod.app.app_context():
        user = User.query.filter_by(email="alice@examplecorp.com").one()
        user.role = "super_admin"
        capped = mcp_registry.CappedUser(user)
        assert user.is_super_admin and not capped.is_super_admin
        assert capped.role == "user" and not capped.is_tenant_admin
        assert capped.id == user.id and capped.email == user.email
        assert capped.tenant_id == user.tenant_id and capped.is_scale == user.is_scale


def test_tool_error_body_shape(client):
    r = call(client, "create_sizing", {"workload": {"total_vcpus": 10}})
    assert r["isError"] is True and "structuredContent" not in r
    body = json.loads(r["content"][0]["text"])["error"]
    assert body["code"] == "invalid_input" and body["retryable"] is False
    assert body["missing"] == ["workload.provisioned_ram_gb", "workload.used_storage_tb",
                               "workload.largest_vm_vcpus", "workload.largest_vm_ram_gb"]
    assert "Ask the user" in body["message"] or "missing" in body["message"]


# ── the stateless tools ──────────────────────────────────────────────────────

def test_get_constants_is_an_allowlist(client):
    r = call(client, "get_constants", {})
    assert r["isError"] is False and r["resultType"] == "complete"
    s = r["structuredContent"]
    keys = {c["key"] for c in s["constants"]}
    assert keys == set(mcp_tools.CONSTANT_KEYS)
    assert not keys & {"w_cost", "eur_per_tier_point", "w_cpu_perf", "node_overhead",
                       "default_cost_tier", "license_scoring", "w_waste"}
    assert s["defaults"]["vcpu_ratio"] == 3.0 and s["defaults"]["nic_speed_gbe"] == 10
    assert s["iops"]["derating_pct"] == 35.0 and s["iops"]["per_drive_iops"] == {"NVMe": 100000}
    assert _offending_keys(s) == []
    assert all(c["meaning"] for c in s["constants"])
    # Text block: summary then the JSON.
    text = r["content"][0]["text"]
    assert text.startswith("Defaults: ") and json.loads(text.split("\n\n", 1)[1]) == s


def test_list_node_models_has_no_pricing(client):
    r = call(client, "list_node_models", {})
    s = r["structuredContent"]
    assert [m["name"] for m in s["models"]] == ["TEST-HCI"]
    m = s["models"][0]
    assert m["cpu_options"][0]["cores"] == 48 and m["ram_options_gb"] == [256, 384, 512]
    assert "notes" not in m and "exclude_from_recommendations" not in m
    assert _offending_keys(s) == []
    r = call(client, "list_node_models", {"model": "NOPE"})
    assert r["isError"] and json.loads(r["content"][0]["text"])["error"]["code"] == "not_found"


def test_lookup_cpu_benchmark(client):
    r = call(client, "lookup_cpu_benchmark", {"query": "Intel Xeon Silver 4110"})
    s = r["structuredContent"]
    assert s["found"] and s["perf_index"] > 0
    assert r["content"][0]["text"].startswith(s["model"])
    r = call(client, "lookup_cpu_benchmark", {"query": "zz no such cpu"})
    assert r["structuredContent"]["found"] is False


def test_validate_inputs_reports_without_blocking(client):
    r = call(client, "validate_inputs", {"workload": {"total_vcpus": 148}})
    s = r["structuredContent"]
    assert s["valid"] is False and "largest_vm_ram_gb" in s["missing"]
    r = call(client, "validate_inputs", {"workload": dict(WORKLOAD, provisioned_ram_gb=148 * 40),
                                         "params": {"target_model": "NOPE"}})
    s = r["structuredContent"]
    assert s["valid"] is False
    assert s["invalid"][0]["field"] == "params.target_model"
    assert s["plausibility"][0]["code"] == "ram_per_vcpu_high"
    r = call(client, "validate_inputs", {"workload": WORKLOAD})
    s = r["structuredContent"]
    assert s["valid"] is True and s["warnings"] == []
    assert s["inputs_used"]["defaults_applied"][0] == "nic_speed_gbe"
    assert s["inputs_used"]["params"]["vcpu_ratio"] == 3.0


def test_create_sizing_matches_the_rest_route(client):
    r = call(client, "create_sizing", {"workload": WORKLOAD, "max_options": 8})
    assert r["isError"] is False
    s = r["structuredContent"]
    assert s["schema_version"] == "1"
    assert _offending_keys(s) == []
    via_route = client.post("/api/recommend", json={"summary": SUMMARY}).get_json()
    assert s["options_total"] == len(via_route["recommendations"])
    for opt, rec in zip(s["options"], via_route["recommendations"]):
        assert (opt["model"], opt["node_count"]) == (rec["model"], rec["node_count"])
        assert opt["determinant"]["resource"] == rec["determinant"]["resource"]
    assert any("assumed 10 GbE" in w for w in s["warnings"])
    assert s["inputs_used"]["workload"]["nic_speed_gbe"] == 10
    assert "nic_speed_gbe" in s["inputs_used"]["defaults_applied"]
    assert "growth_pct" in s["inputs_used"]["defaults_applied"]
    text = r["content"][0]["text"]
    assert text.startswith("Top option: ") and "Determined by" in text
    assert json.loads(text.split("\n\n", 1)[1]) == s
    # Legacy era: same result, no resultType.
    r2 = call(client, "create_sizing", {"workload": WORKLOAD, "max_options": 8}, era=legacy)
    assert "resultType" not in r2 and r2["structuredContent"]["options"] == s["options"]


def test_create_sizing_params_and_source_cpus(client):
    r = call(client, "create_sizing", {
        "workload": dict(WORKLOAD, nic_speed_gbe=25,
                         source_cpus=[{"model": "Intel Xeon Silver 4110", "sockets": 6},
                                      {"model": "Mystery CPU 9000", "sockets": 2}]),
        "params": {"vcpu_ratio": 4, "years": 3, "storage_preference": "flash",
                   "target_nodes": 3}})
    s = r["structuredContent"]
    assert s["inputs_used"]["params"]["vcpu_ratio"] == 4.0
    assert s["inputs_used"]["params"]["years"] == 3
    assert s["inputs_used"]["params"]["storage_preference"] == "flash"
    assert "vcpu_ratio" not in s["inputs_used"]["defaults_applied"]
    assert s["inputs_used"]["source_perf"]["unresolved"] == ["Mystery CPU 9000"]
    assert s["inputs_used"]["source_perf"]["total_specrate"] > 0
    assert s["perf_comparison"]["source_specrate"] == s["inputs_used"]["source_perf"]["total_specrate"]
    assert any("Mystery CPU 9000" in w for w in s["warnings"])
    assert not any("assumed 10 GbE" in w for w in s["warnings"])
    assert all(o["node_count"] == 3 for o in s["options"]) or s["options"] == []


def test_create_sizing_refuses_silent_fallbacks(client):
    r = call(client, "create_sizing", {"workload": WORKLOAD, "params": {"target_model": "GHOST"}})
    body = json.loads(r["content"][0]["text"])["error"]
    assert body["code"] == "invalid_input"
    assert body["invalid"][0]["field"] == "params.target_model"
    r = call(client, "create_sizing", {"workload": WORKLOAD,
                                       "params": {"sizing_mode": "validated", "vendor": "acme"}})
    body = json.loads(r["content"][0]["text"])["error"]
    assert body["invalid"][0]["field"] == "params.vendor"


def test_create_sizing_no_fit_is_not_an_error(client):
    r = call(client, "create_sizing", {"workload": dict(WORKLOAD, largest_vm_vcpus=5000)})
    assert r["isError"] is False
    s = r["structuredContent"]
    assert s["options"] == [] and s["options_total"] == 0
    assert any("largest VM" in w for w in s["warnings"])
    assert r["content"][0]["text"].startswith("No configuration fits")
