#!/usr/bin/env python3
"""Exercise a deployed MCP server end to end as a real OAuth client.

    .venv/bin/python tools/mcp_suite.py login  https://betasizer.demolabs.me
    .venv/bin/python tools/mcp_suite.py run    [--limits] [--refresh]
    .venv/bin/python tools/mcp_suite.py logout

``login`` registers a loopback client with Dynamic Client Registration, prints
the consent URL for you to open and approve (the account you sign in with is
the account the suite runs as), catches the authorization code on
127.0.0.1, exchanges it with PKCE and keeps the tokens in the state directory
(``MCP_SUITE_STATE``, default ``~/.mcp-suite``, mode 0700). This is the path
Claude Code takes, minus Anthropic's hosted client metadata document.

``run`` drives the transport (both protocol eras), every tool, the save
round trip, BOM rows, uploads and the error paths, and reports PASS/FAIL per
check. It writes only what it names: one project "MCP suite test <date>"
holding one sizing and one BOM check, plus one open upload link that expires
on its own; existing sizings are only read. Delete the project in the web UI
afterwards (MCP cannot delete, by design). Two checks are opt-in because they
disturb the session: ``--limits`` spends the per-account call budget (other
clients of that account get 429s for up to a minute) and ``--refresh``
rotates the refresh token, then deliberately reuses it, which revokes the
whole token family: log in again before the next run.

``logout`` revokes the stored tokens.

Only the standard library is needed; no server code is imported, so the
script runs from any machine that can reach the server.
"""
import argparse
import base64
import datetime
import hashlib
import json
import os
import secrets
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

MODERN = "2026-07-28"
LEGACY = "2025-11-25"
CLIENT_INFO = {"name": "mcp-suite-test", "version": "1"}
META = {"io.modelcontextprotocol/protocolVersion": MODERN,
        "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
        "io.modelcontextprotocol/clientCapabilities": {}}
PRICE_SHAPED = ("cost", "price", "tier", "eur", "usd", "msrp", "discount", "margin")
# docs/mcp/01-tool-design.md §13: the golden workload, sized against the live catalog.
GOLDEN = {"total_vcpus": 148, "provisioned_ram_gb": 423, "used_storage_tb": 8.2,
          "largest_vm_vcpus": 16, "largest_vm_ram_gb": 64, "active_vms": 59, "total_vms": 59,
          "p95_iops": 16000,
          "source_cpus": [{"model": "Intel Xeon Silver 4110", "sockets": 6}]}
BOM_ROWS = {"vendor": "Dell", "source_description": "suite test rows",
            "configs": [{"name": "Config 1", "serverModel": "PowerEdge R760", "nodeCount": 3, "components": [
                {"partNumber": "338-CHUK", "description": "Intel Xeon Gold 6526Y 2.8G, 16C/32T", "quantity": 6, "category": "cpu"},
                {"partNumber": "370-AGZP", "description": "64GB RDIMM, 5600MT/s Dual Rank", "quantity": 24, "category": "memory"},
                {"partNumber": "400-BMTR", "description": "3.84TB SSD SAS Read Intensive 24Gbps 512e 2.5in Hot-Plug", "quantity": 18, "category": "storage"},
                {"partNumber": "405-AAXS", "description": "PERC H965i Controller, Front", "quantity": 3, "category": "controller"},
                {"partNumber": "540-BCOB", "description": "Intel X710-T4L Quad Port 10GbE BASE-T Adapter, OCP NIC 3.0", "quantity": 3, "category": "nic"},
                {"partNumber": "403-BCMU", "description": "BOSS-N1 controller card + with 2 M.2 960GB (RAID 1)", "quantity": 3, "category": "boss"},
                {"partNumber": None, "description": "PowerEdge R760 Server", "quantity": 3, "category": "chassis"}]}]}

STATE_DIR = os.environ.get("MCP_SUITE_STATE") or os.path.expanduser("~/.mcp-suite")


# ── state ────────────────────────────────────────────────────────────────────
def _path(name):
    return os.path.join(STATE_DIR, name)


def _save(name, data):
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    fd = os.open(_path(name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=1, default=str)


def _load(name):
    try:
        with open(_path(name)) as fh:
            return json.load(fh)
    except FileNotFoundError:
        sys.exit("No %s in %s: run `mcp_suite.py login <server-url>` first." % (name, STATE_DIR))


# ── HTTP helpers ─────────────────────────────────────────────────────────────
def _form(url, fields, timeout=15):
    """POST a form; (status, json) whether the server answered 2xx or 4xx."""
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {}


def _json(url, body, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ── login / logout ───────────────────────────────────────────────────────────
class _Callback(BaseHTTPRequestHandler):
    received = None

    def do_GET(self):
        _Callback.received = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).items()}
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<h2>Consent received. You can close this tab and go back to the terminal.</h2>")

    def log_message(self, *a):
        pass


def login(server):
    server = server.rstrip("/")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    redirect_uri = "http://127.0.0.1:%d/callback" % port

    st, reg = _json(server + "/oauth/register", {
        "client_name": "MCP suite test (tools/mcp_suite.py)", "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        "token_endpoint_auth_method": "none", "scope": "sizing"})
    if st != 201 or not reg.get("client_id"):
        sys.exit("registration failed (%s): %s" % (st, reg))
    print("registered client", reg["client_id"])

    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(12)
    url = server + "/oauth/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": reg["client_id"], "redirect_uri": redirect_uri,
        "code_challenge": challenge, "code_challenge_method": "S256", "state": state,
        "scope": "sizing", "resource": server + "/mcp"})
    print("\nOpen this URL, sign in as the account the suite should run as, and allow:\n\n  %s\n" % url)
    print("waiting for the browser to come back to %s ..." % redirect_uri)
    httpd = HTTPServer(("127.0.0.1", port), _Callback)
    httpd.timeout = 600
    httpd.handle_request()
    cb = _Callback.received
    if not cb or cb.get("state") != state or "code" not in cb:
        sys.exit("authorization did not complete: %r" % (cb,))
    if cb.get("iss") != server:
        sys.exit("iss mismatch (RFC 9207): got %r, expected %r" % (cb.get("iss"), server))

    st, tok = _form(server + "/oauth/token", {
        "grant_type": "authorization_code", "code": cb["code"], "redirect_uri": redirect_uri,
        "client_id": reg["client_id"], "code_verifier": verifier, "resource": server + "/mcp"})
    if st != 200 or not tok.get("access_token"):
        sys.exit("token exchange failed (%s): %s" % (st, tok))
    _save("client.json", {"server": server, "client_id": reg["client_id"], "redirect_uri": redirect_uri})
    _save("token.json", tok)
    print("logged in: token_type=%s expires_in=%s scope=%s refresh=%s\nstate in %s" % (
        tok.get("token_type"), tok.get("expires_in"), tok.get("scope"),
        "yes" if tok.get("refresh_token") else "no", STATE_DIR))


def logout():
    client, tok = _load("client.json"), _load("token.json")
    for which in ("refresh_token", "access_token"):
        if tok.get(which):
            st, _ = _form(client["server"] + "/oauth/revoke", {"token": tok[which], "client_id": client["client_id"]})
            print("revoke %s -> %s" % (which, st))
    os.remove(_path("token.json"))


# ── the suite ────────────────────────────────────────────────────────────────
class Suite:
    def __init__(self, client, tok):
        self.server = client["server"]
        self.client = client
        self.tok = tok
        self.results = []
        self.rid = 0

    # transport
    def post(self, body, headers=None, token="default", timeout=60):
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if token == "default":
            token = self.tok["access_token"]
        if token:
            h["Authorization"] = "Bearer " + token
        h.update(headers or {})
        req = urllib.request.Request(self.server + "/mcp", data=json.dumps(body).encode(), headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as e:
            data = e.read()
            try:
                data = json.loads(data)
            except ValueError:
                data = data.decode(errors="replace")[:300]
            return e.code, dict(e.headers), data

    def rpc(self, method, params=None, modern=True, headers=None, token="default"):
        self.rid += 1
        params = dict(params or {})
        if modern:
            params["_meta"] = META
            h = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method}
            if method == "tools/call":
                h["Mcp-Name"] = params.get("name", "")
        else:
            h = {"MCP-Protocol-Version": LEGACY}
        h.update(headers or {})
        return self.post({"jsonrpc": "2.0", "id": self.rid, "method": method, "params": params}, h, token)

    def call(self, name, args, modern=True):
        """(status, structuredContent, text, result) of one tools/call."""
        status, _, body = self.rpc("tools/call", {"name": name, "arguments": args}, modern)
        if status != 200 or "result" not in (body or {}):
            return status, None, None, body
        r = body["result"]
        text = r["content"][0]["text"] if r.get("content") else ""
        return status, r.get("structuredContent"), text, r

    # bookkeeping
    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok), detail))
        print(("PASS " if ok else "FAIL ") + name + ((": " + str(detail)[:200]) if detail else ""))
        return bool(ok)

    @staticmethod
    def err_of(r):
        try:
            return json.loads(r["content"][0]["text"])["error"]
        except Exception:   # noqa: BLE001
            return {}

    @staticmethod
    def offending(node, path="$"):
        """Keys anywhere in a result whose name looks like a price."""
        out = []
        if isinstance(node, dict):
            for k, v in node.items():
                if any(w in str(k).lower() for w in PRICE_SHAPED):
                    out.append(path + "." + str(k))
                out += Suite.offending(v, path + "." + str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                out += Suite.offending(v, "%s[%d]" % (path, i))
        return out

    # sections
    def transport(self):
        check, post, rpc = self.check, self.post, self.rpc
        s, h, b = post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=None)
        check("401 challenge without token", s == 401 and "resource_metadata=" in h.get("WWW-Authenticate", ""), h.get("WWW-Authenticate", "")[:80])
        s, h, b = post({"jsonrpc": "2.0", "id": 1, "method": "ping"}, token="nope")
        check("401 invalid_token for a bad token", s == 401 and 'error="invalid_token"' in h.get("WWW-Authenticate", ""))
        s, h, b = rpc("server/discover")
        check("server/discover (%s)" % MODERN, s == 200 and b["result"].get("supportedVersions") == [MODERN] and b["result"].get("resultType") == "complete", b.get("result", {}).get("_meta"))
        s, h, b = rpc("initialize", {"protocolVersion": LEGACY, "capabilities": {}, "clientInfo": CLIENT_INFO}, modern=False)
        check("initialize (legacy %s), no session id" % LEGACY, s == 200 and b["result"].get("protocolVersion") == LEGACY and "Mcp-Session-Id" not in h, b.get("result", {}).get("serverInfo"))
        s, h, b = post({"jsonrpc": "2.0", "method": "notifications/initialized"}, {"MCP-Protocol-Version": LEGACY})
        check("notification -> 202", s == 202)
        s, h, b = rpc("tools/list")
        tools = b.get("result", {}).get("tools", []) if s == 200 else []
        names = sorted(t["name"] for t in tools)
        check("tools/list: %d tools" % len(tools), len(tools) >= 15, ", ".join(names))
        bad = [t["name"] for t in tools if not t.get("title") or "annotations" not in t
               or any(k not in t["annotations"] for k in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"))]
        check("every tool has title + 4 annotation hints", not bad, bad)
        flat = [t["name"] for t in tools if any(k in json.dumps(t["inputSchema"]) for k in ('"$ref"', '"oneOf"', '"anyOf"', '"allOf"'))]
        check("schemas flat (no $ref/oneOf/anyOf/allOf), names <= 63", not flat and all(len(n) <= 63 for n in names), flat)
        s, h, b = rpc("tools/call", {"name": "get_constants", "arguments": {}}, headers={"Mcp-Name": "other"})
        check("Mcp-Name mismatch -> 400/-32020", s == 400 and (b or {}).get("error", {}).get("code") == -32020)
        s, h, b = rpc("nope/method")
        check("unknown method -> 404/-32601", s == 404 and (b or {}).get("error", {}).get("code") == -32601)
        s, sc, text, r = self.call("get_constants", {}, modern=False)
        check("tools/call over the legacy era works, no resultType", s == 200 and r and "resultType" not in r)

    def stateless(self):
        check, call, err_of, offending = self.check, self.call, self.err_of, self.offending
        s, sc, text, r = call("get_constants", {})
        check("get_constants", s == 200 and sc and len(sc) > 3, list((sc or {}).keys())[:6])
        check("get_constants: no price keys, no scoring weights", sc and not offending(sc) and not any(k in json.dumps(sc) for k in ('"w_cpu"', '"w_ram"', '"w_storage"', '"score"')), offending(sc or {}))
        s, sc, text, r = call("list_node_models", {})
        models = (sc or {}).get("models") or []
        check("list_node_models: %d models" % len(models), s == 200 and models, [m.get("name") for m in models[:5]])
        check("list_node_models: no price-shaped keys", not offending(sc or {}), offending(sc or {})[:5])
        s, sc, text, r = call("lookup_cpu_benchmark", {"query": "Intel Xeon Silver 4110"})
        check("lookup_cpu_benchmark Silver 4110 found", s == 200 and (sc or {}).get("found") is True, text)
        s, sc, text, r = call("lookup_cpu_benchmark", {"query": "E5-2667 v4"})
        check("lookup_cpu_benchmark unknown -> found:false + PassMark advice", s == 200 and (sc or {}).get("found") is False and "PassMark" in (text or ""), (text or "")[:120])
        s, sc, text, r = call("validate_inputs", {"workload": GOLDEN})
        check("validate_inputs golden: nothing missing or invalid", s == 200 and sc and sc.get("missing") == [] and sc.get("invalid") == [], (sc or {}).get("plausibility"))
        s, sc, text, r = call("validate_inputs", {"workload": {"total_vcpus": 10, "provisioned_ram_gb": 40, "used_storage_tb": 1}})
        check("validate_inputs: missing largest VM listed", s == 200 and sc and set(sc.get("missing") or []) >= {"largest_vm_vcpus", "largest_vm_ram_gb"}, (sc or {}).get("missing"))
        s, sc, text, r = call("create_sizing", {"workload": {"total_vcpus": 10, "provisioned_ram_gb": 40, "used_storage_tb": 1}})
        check("create_sizing missing fields -> invalid_input (isError)", s == 200 and r and r.get("isError") and err_of(r).get("code") == "invalid_input", err_of(r).get("message", "")[:100])
        s, sc, text, r = call("create_sizing", {"workload": GOLDEN, "params": {"target_model": "NOPE-1"}})
        check("create_sizing unknown target_model -> invalid_input", r and r.get("isError") and err_of(r).get("code") == "invalid_input", err_of(r).get("message", "")[:100])

        s, sc, text, r = call("create_sizing", {"workload": GOLDEN, "params": {"vcpu_ratio": 3}})
        opts = (sc or {}).get("options") or []
        top = opts[0] if opts else {}
        check("create_sizing golden §13: %d options" % len(opts), s == 200 and not r.get("isError") and opts,
              "top %s x %s; %s" % (top.get("node_count"), top.get("display_model") or top.get("model"), (top.get("determinant") or {}).get("statement")))
        check("golden: checks present, none failing on the top option", top and top.get("checks") and all(c["status"] in ("pass", "warn", "not_applicable") for c in top["checks"]),
              [(c["id"], c["status"]) for c in top.get("checks", [])])
        check("golden: inputs_used echoes defaults + source perf", sc and sc.get("inputs_used", {}).get("defaults_applied") and sc["inputs_used"].get("source_perf"),
              {"defaults": (sc or {}).get("inputs_used", {}).get("defaults_applied"), "perf": (sc or {}).get("inputs_used", {}).get("source_perf")})
        check("golden: text = summary + JSON", text and text.strip().endswith("}") and "Top option" in text, text.split("\n")[0][:120] if text else "")
        check("golden: no price-shaped keys, no score", not offending(sc or {}) and '"score"' not in json.dumps(sc), offending(sc or {})[:5])
        check("golden: result size %d chars (< 150k)" % len(json.dumps(r)), len(json.dumps(r)) < 150000)
        return top

    def saved(self):
        check, call, err_of, offending = self.check, self.call, self.err_of, self.offending
        s, sc, text, r = call("list_projects", {})
        projects = (sc or {}).get("projects") or []
        check("list_projects: %d" % len(projects), s == 200 and sc is not None, [p.get("name") for p in projects[:5]])
        s, sc, text, r = call("list_sizings", {})
        sizings = (sc or {}).get("sizings") or []
        check("list_sizings: %d" % len(sizings), s == 200 and sc is not None, [(z.get("id"), z.get("name")) for z in sizings[:5]])
        check("list_sizings: no price-shaped keys", not offending(sc or {}))
        if sizings:
            sid = sizings[0]["id"]
            s, sc, text, r = call("get_sizing", {"sizing_id": sid})
            check("get_sizing %s" % sid, s == 200 and sc and sc.get("result_state"), {"state": (sc or {}).get("result_state"), "warnings": len((sc or {}).get("warnings") or [])})
            check("get_sizing: no price-shaped keys", not offending(sc or {}), offending(sc or {})[:5])
            if sizings[0].get("code"):
                s, sc, text, r = call("open_shared_sizing", {"share_code": sizings[0]["code"]})
                check("open_shared_sizing on an own code", s == 200 and sc and sc.get("sizing", {}).get("id") == sid, (sc or {}).get("sizing", {}).get("source"))
        s, sc, text, r = call("create_project", {"name": "   "})
        check("create_project blank name -> invalid_input", r and r.get("isError") and err_of(r).get("code") == "invalid_input")
        s, sc, text, r = call("get_sizing", {"sizing_id": 99999999})
        check("get_sizing unknown -> not_found", r and r.get("isError") and err_of(r).get("code") == "not_found")
        s, sc, text, r = call("open_shared_sizing", {"share_code": "000000000000"})
        check("open_shared_sizing bad code -> not_found", r and r.get("isError") and err_of(r).get("code") == "not_found")

    def save_round_trip(self, top):
        """Returns (sizing_id, project_id) of what was written, or (None, None)."""
        check, call, err_of = self.check, self.call, self.err_of
        pname = "MCP suite test " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        s, sc, text, r = call("create_project", {"name": pname, "customer_name": "Suite customer"})
        pid = ((sc or {}).get("project") or {}).get("id")
        if not check("create_project %r" % pname, s == 200 and not r.get("isError") and pid and (sc or {}).get("open_url"),
                     {"project": pid, "open_url": (sc or {}).get("open_url")} if not r.get("isError") else err_of(r)):
            return None, None
        s, sc, text, r = call("save_sizing", {"name": "Golden §13 (suite)", "workload": GOLDEN, "params": {"vcpu_ratio": 3},
                                              "option_rank": 1, "expected_option": {"model": top.get("model"), "node_count": top.get("node_count")},
                                              "project_id": pid, "notes": "Written by tools/mcp_suite.py; safe to delete."})
        sz = (sc or {}).get("sizing") or {}
        sid, code = sz.get("id"), sz.get("code")
        if not check("save_sizing into the new project", s == 200 and not r.get("isError") and sid and (sz.get("project") or {}).get("id") == pid and (sc or {}).get("open_url"),
                     {"sizing": sid, "code": code, "open_url": (sc or {}).get("open_url")} if not r.get("isError") else err_of(r)):
            return None, pid
        s, sc, text, r = call("get_sizing", {"sizing_id": sid})
        check("saved sizing reads back fresh with complete rerun inputs",
              s == 200 and sc and sc["result_state"]["cache"] == "fresh" and sc.get("rerun_inputs_complete") is True,
              {"state": (sc or {}).get("result_state"), "complete": (sc or {}).get("rerun_inputs_complete"),
               "option": (((sc or {}).get("clusters") or [{}])[0].get("selected_option") or {}).get("model"), "warnings": (sc or {}).get("warnings")})
        check("saved sizing: rerun_inputs workload == golden",
              sc and all(sc["rerun_inputs"]["workload"].get(k) == v for k, v in GOLDEN.items() if k != "source_cpus"),
              {k: (sc or {}).get("rerun_inputs", {}).get("workload", {}).get(k) for k in ("total_vcpus", "largest_vm_ram_gb", "p95_iops")})
        s, sc, text, r = call("open_shared_sizing", {"share_code": code})
        check("open_shared_sizing on the saved code", s == 200 and sc and sc["sizing"]["id"] == sid, sc["sizing"].get("source") if sc else err_of(r))
        s, sc, text, r = call("save_sizing", {"name": "x", "workload": GOLDEN, "params": {"vcpu_ratio": 3}, "option_rank": 1,
                                              "expected_option": {"model": "HC9999", "node_count": 99}, "project_id": pid})
        check("save_sizing wrong expected_option -> option_changed, nothing written",
              r and r.get("isError") and err_of(r).get("code") == "option_changed", err_of(r).get("message", "")[:120])
        s, sc, text, r = call("list_sizings", {"project_id": pid})
        check("list_sizings(project) shows exactly the one saved sizing",
              sc and [z["id"] for z in sc["sizings"]] == [sid], [z["id"] for z in (sc or {}).get("sizings", [])])
        s, sc, text, r = call("save_sizing", {"name": "x", "workload": GOLDEN, "project_id": 99999999})
        check("save_sizing into an invisible project -> not_found", r and r.get("isError") and err_of(r).get("code") == "not_found", err_of(r).get("code"))
        return sid, pid

    def bom(self, sid, pid):
        check, call, err_of, offending = self.check, self.call, self.err_of, self.offending
        s, sc, text, r = call("check_bom_rows", BOM_ROWS)
        check("check_bom_rows: fit + agent_ungrounded flag", s == 200 and not r.get("isError") and sc and "agent_ungrounded" in json.dumps(sc),
              {k: sc.get(k) for k in ("fit", "flag_reasons") if k in sc} if sc else err_of(r))
        check("check_bom_rows: no price-shaped keys", not offending(sc or {}), offending(sc or {})[:5])
        s, sc, text, r = call("check_bom_rows", dict(BOM_ROWS, documentContainsInstructions=True))
        check("documentContainsInstructions -> agent_instructions flag", sc and "agent_instructions" in json.dumps(sc))
        if sid:
            s, sc, text, r = call("check_bom_rows", dict(BOM_ROWS, sizing_id=sid))
            check("check_bom_rows against the saved sizing", s == 200 and not r.get("isError") and sc, {k: sc.get(k) for k in ("fit",)} if sc else err_of(r))
            s, sc, text, r = call("save_bom_check", dict(BOM_ROWS, project_id=pid, sizing_id=sid, name="Suite BOM check"))
            check("save_bom_check into the suite project", s == 200 and not r.get("isError") and sc and sc.get("check_id"),
                  {"check_id": (sc or {}).get("check_id"), "open_url": (sc or {}).get("open_url")} if not r.get("isError") else err_of(r))

    def uploads(self):
        check, call, err_of = self.check, self.call, self.err_of
        s, sc, text, r = call("start_upload", {"kind": "assessment"})
        up = sc or {}
        check("start_upload: link", s == 200 and up.get("upload_id") and up.get("upload_url", "").startswith(self.server + "/mcp/upload/"), up.get("upload_url"))
        if up.get("upload_id"):
            s, sc, text, r = call("get_upload", {"upload_id": up["upload_id"], "wait_seconds": 0})
            check("get_upload: waiting", sc and sc.get("status") == "waiting", (sc or {}).get("status"))
            s, sc, text, r = call("list_upload_vms", {"upload_id": up["upload_id"]})
            check("list_upload_vms before the upload -> error", r and r.get("isError"), err_of(r).get("code"))
        s, sc, text, r = call("get_upload", {"upload_id": "u_doesnotexist"})
        check("get_upload unknown -> not_found", r and r.get("isError") and err_of(r).get("code") == "not_found")
        s, sc, text, r = call("start_upload", {"kind": "bom"})
        check("start_upload bom without a project -> invalid_input", r and r.get("isError") and err_of(r).get("code") == "invalid_input")

    def limits(self):
        check, rpc = self.check, self.rpc
        hit = None
        for i in range(200):
            st, hh, bb = rpc("tools/call", {"name": "get_constants", "arguments": {}})
            if st == 429:
                hit = (i + 1, hh.get("Retry-After"), (bb or {}).get("error", {}).get("code"), (bb or {}).get("id"))
                break
        check("per-account rate limit: 429 + Retry-After + -32000 carrying the request id",
              hit and hit[2] == -32000 and hit[1] and hit[3] == self.rid, hit)
        st, hh, bb = rpc("tools/list")
        check("tools/list still allowed while tool calls are limited", st == 200, st)
        if hit and hit[1]:
            print("   (sleeping %s s for the window to pass)" % hit[1])
            time.sleep(int(hit[1]) + 1)

    def refresh(self):
        check, rpc, client, tok = self.check, self.rpc, self.client, self.tok

        def refresh(token):
            return _form(self.server + "/oauth/token", {"grant_type": "refresh_token", "refresh_token": token, "client_id": client["client_id"]})
        st, new = refresh(tok["refresh_token"])
        check("refresh rotates the pair", st == 200 and new.get("access_token") and new.get("refresh_token") not in (None, tok["refresh_token"]), st)
        st2, again = refresh(tok["refresh_token"])
        check("reused refresh token -> invalid_grant", st2 == 400 and again.get("error") == "invalid_grant", again)
        st3, hh, bb = rpc("ping", token=new.get("access_token", "x"))
        check("family revoked after reuse: the fresh access token is refused", st3 == 401, st3)
        st4, _ = _form(self.server + "/oauth/revoke", {"token": tok["access_token"], "client_id": client["client_id"]})
        check("revoke endpoint answers 200", st4 == 200)
        os.remove(_path("token.json"))
        print("   (tokens revoked by design: `login` again before the next run)")


def run(args):
    client, tok = _load("client.json"), _load("token.json")
    suite = Suite(client, tok)
    print("server %s, client %s, state %s\n" % (client["server"], client["client_id"], STATE_DIR))
    print("── transport"); suite.transport()
    print("── stateless tools"); top = suite.stateless()
    print("── saved sizings"); suite.saved()
    print("── save round trip"); sid, pid = suite.save_round_trip(top)
    print("── BOM rows"); suite.bom(sid, pid)
    print("── uploads"); suite.uploads()
    if args.limits:
        print("── rate limit"); suite.limits()
    if args.refresh:
        print("── refresh rotation"); suite.refresh()
    passed = sum(1 for x in suite.results if x[1])
    failed = [x for x in suite.results if not x[1]]
    print("\n%d passed, %d failed" % (passed, len(failed)))
    for name, _, detail in failed:
        print("  FAIL", name, "-", str(detail)[:200])
    if pid:
        print("\nLeft on the server for you to delete in the web UI: project %s (sizing %s, one BOM check)." % (pid, sid))
    _save("report.json", {"server": client["server"], "when": datetime.datetime.now().isoformat(),
                          "results": suite.results, "project_id": pid, "sizing_id": sid})
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("login", help="register a client, obtain consent in the browser, store tokens")
    p.add_argument("server", help="base URL, e.g. https://betasizer.demolabs.me")
    p = sub.add_parser("run", help="run every check with the stored token")
    p.add_argument("--limits", action="store_true", help="also hit the per-account rate limit (locks the account out for up to a minute)")
    p.add_argument("--refresh", action="store_true", help="also test refresh rotation and reuse detection (revokes the stored tokens)")
    sub.add_parser("logout", help="revoke the stored tokens")
    args = ap.parse_args()
    if args.cmd == "login":
        login(args.server)
    elif args.cmd == "logout":
        logout()
    else:
        sys.exit(run(args))


if __name__ == "__main__":
    main()
