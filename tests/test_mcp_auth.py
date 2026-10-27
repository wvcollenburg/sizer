"""OAuth for the MCP endpoint (docs/mcp/03-auth.md).

Locks in:
  - discovery: protected-resource metadata names this server; AS metadata
    advertises code + PKCE S256, DCR, CIMD, the iss parameter and one scope
  - DCR issues a public client (secret only when asked); bad redirect URIs
    and grants are refused
  - CIMD: a client_id that is an https URL is fetched, must name itself,
    is cached; loopback redirects match without the port
  - authorize: anonymous visitors go to the login with a return path; the
    consent page shows the client and redirect host; allow sends code,
    state and iss; deny sends access_denied; bad client/redirect are shown,
    never redirected; the nonce stops a replayed form
  - token: PKCE verified; a code works once and a replay revokes what it
    issued; refresh rotates and a reused refresh token revokes the family;
    the resource must be this server; invalid_grant for dead tokens
  - the endpoint: a valid bearer token reaches the tools; unknown, expired,
    revoked and disabled-account tokens get the invalid_token challenge
  - the account can list and revoke its connections; purge cleans up

Run: .venv/bin/python -m pytest tests/test_mcp_auth.py -q
"""
import base64
import hashlib
import json
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from html import escape, unescape
from urllib.parse import parse_qs, quote, urlparse

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.pop("SESSION_COOKIE_SECURE", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402

import app as appmod  # noqa: E402
import mcp_auth  # noqa: E402
from auth_models import User  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from test_mcp_server import META, MODERN  # noqa: E402
from test_mcp_uploads import _login, _signup  # noqa: E402
from test_sizing_service import _seed_catalog  # noqa: E402

CALLBACK = "https://claude.ai/api/mcp/auth_callback"
CIMD_URL = "https://claude.ai/oauth/claude-code-client-metadata"
CIMD_DOC = {"client_id": CIMD_URL, "client_name": "Claude Code",
            "redirect_uris": ["http://localhost/callback", "http://127.0.0.1/callback"]}


@pytest.fixture()
def world(monkeypatch):
    # Set per test, not at import: a module-level os.environ write would be
    # inherited by the browser tests' dev-server subprocess.
    monkeypatch.setenv("APP_BASE_URL", "http://localhost")
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
    ids = {"alice": _signup(c, "alice@partnerco.example")}
    monkeypatch.setattr(mcp_auth, "_fetch_cimd", lambda url: dict(CIMD_DOC) if url == CIMD_URL
                        else (_ for _ in ()).throw(ValueError("no such document")))
    yield c, ids
    app.config.pop("MCP_TEST_USER_ID", None)


def _register(c, **extra):
    body = dict({"redirect_uris": [CALLBACK], "client_name": "Claude", "grant_types":
                 ["authorization_code", "refresh_token"], "response_types": ["code"],
                 "token_endpoint_auth_method": "none"}, **extra)
    return c.post("/oauth/register", json=body)


def _pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _consent(c, client_id, challenge, redirect_uri=CALLBACK, state="xyz", **extra):
    """Open the consent page as the logged-in user and allow. Returns the
    redirect's query."""
    args = dict({"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
                 "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
                 "scope": "sizing", "resource": "http://localhost/mcp"}, **extra)
    r = c.get("/oauth/authorize", query_string=args)
    assert r.status_code == 200, r.data[:300]
    html = r.get_data(as_text=True)
    nonce = html.split('name="nonce" value="')[1].split('"')[0]
    form = dict(args, nonce=nonce, decision="allow")
    r = c.post("/oauth/authorize", data=form)
    loc = _leave_target(r)
    assert loc.startswith(redirect_uri.split("?")[0])
    return {k: v[0] for k, v in parse_qs(urlparse(loc).query).items()}


def _leave_target(r):
    """The decision answers with a page that navigates to the callback (a
    302 after a form POST would be blocked by the proxy's CSP form-action in
    Chrome): the URL is in the meta refresh and the fallback link."""
    assert r.status_code == 200, r.data[:300]
    assert r.headers["Cache-Control"] == "no-store"
    html = r.get_data(as_text=True)
    href = unescape(html.split('id="continue" href="')[1].split('"')[0])
    assert ('content="0;url=%s"' % escape(href, quote=True)) in html
    return href


def _exchange(c, client_id, code, verifier, **extra):
    return c.post("/oauth/token", data=dict({"grant_type": "authorization_code", "code": code,
                                             "redirect_uri": CALLBACK, "client_id": client_id,
                                             "code_verifier": verifier,
                                             "resource": "http://localhost/mcp"}, **extra))


def _mcp(c, token, method="server/discover", params=None):
    params = dict(params or {}, _meta=META)
    h = {"MCP-Protocol-Version": MODERN, "Mcp-Method": method, "Authorization": "Bearer " + token}
    if method == "tools/call":
        h["Mcp-Name"] = params["name"]
    return c.post("/mcp", data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                           "params": params}),
                  content_type="application/json", headers=h)


def _connect(c, ids):
    """Register, consent and exchange: (client_id, access, refresh)."""
    client_id = _register(c).get_json()["client_id"]
    _login(c, "alice@partnerco.example")
    verifier, challenge = _pkce()
    q = _consent(c, client_id, challenge)
    t = _exchange(c, client_id, q["code"], verifier).get_json()
    return client_id, t["access_token"], t["refresh_token"]


# ── discovery ────────────────────────────────────────────────────────────────

def test_discovery_documents(world):
    c, ids = world
    for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
        r = c.get(path)
        assert r.status_code == 200
        d = r.get_json()
        assert d["resource"] == "http://localhost/mcp"
        assert d["authorization_servers"] == ["http://localhost"]
        assert d["scopes_supported"] == ["sizing"]
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"):
        d = c.get(path).get_json()
        assert d["issuer"] == "http://localhost"
        assert d["code_challenge_methods_supported"] == ["S256"]
        assert "none" in d["token_endpoint_auth_methods_supported"]
        assert d["client_id_metadata_document_supported"] is True
        assert d["authorization_response_iss_parameter_supported"] is True
        assert d["registration_endpoint"].endswith("/oauth/register")
        assert d["grant_types_supported"] == ["authorization_code", "refresh_token"]
        assert d["scopes_supported"] == ["sizing"] and "offline_access" not in d["scopes_supported"]
    # The endpoint's challenge points at the metadata.
    r = _mcp(c, "")
    assert r.status_code == 401
    assert "/.well-known/oauth-protected-resource/mcp" in r.headers["WWW-Authenticate"]


# ── clients ──────────────────────────────────────────────────────────────────

def test_dynamic_registration(world):
    c, ids = world
    r = _register(c)
    assert r.status_code == 201, r.get_json()
    d = r.get_json()
    assert d["client_id"].startswith("dcr_") and "client_secret" not in d
    assert d["token_endpoint_auth_method"] == "none" and d["scope"] == "sizing"
    assert r.headers["Cache-Control"] == "no-store"
    d = _register(c, token_endpoint_auth_method="client_secret_post").get_json()
    assert d["client_secret"] and d["client_secret_expires_at"] == 0
    assert _register(c, redirect_uris=["http://evil.example/cb"]).status_code == 400
    assert _register(c, redirect_uris=[]).status_code == 400
    assert _register(c, grant_types=["client_credentials"]).status_code == 400
    assert _register(c, scope="admin").status_code == 400
    r = c.post("/oauth/register", data="x", content_type="text/plain")
    assert r.status_code == 400


def test_cimd_client_and_loopback_redirects(world):
    c, ids = world
    _login(c, "alice@partnerco.example")
    verifier, challenge = _pkce()
    # A CIMD client identified by its URL; Claude Code's ephemeral port is ignored.
    q = _consent(c, CIMD_URL, challenge, redirect_uri="http://localhost:3118/callback")
    assert q["iss"] == "http://localhost" and q["state"] == "xyz"
    with appmod.app.app_context():
        row = db.session.get(mcp_auth.OAuthClient, CIMD_URL)
        assert row.kind == "cimd" and row.name == "Claude Code" and row.fetched_at
    r = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": q["code"],
                                     "redirect_uri": "http://localhost:3118/callback",
                                     "client_id": CIMD_URL, "code_verifier": verifier})
    assert r.status_code == 200, r.get_json()
    # Unknown document, wrong path, and an https URL that is not a document.
    r = c.get("/oauth/authorize", query_string={"client_id": "https://example.com/nope",
                                                 "redirect_uri": CALLBACK, "response_type": "code",
                                                 "code_challenge": challenge})
    assert r.status_code == 400 and b"Unknown client_id" in r.data
    r = c.get("/oauth/authorize", query_string={"client_id": CIMD_URL,
                                                 "redirect_uri": "http://localhost:3118/other",
                                                 "response_type": "code", "code_challenge": challenge})
    assert r.status_code == 400 and b"not registered" in r.data
    assert mcp_auth.redirect_uri_matches("http://127.0.0.1/callback", "http://127.0.0.1:5000/callback")
    assert not mcp_auth.redirect_uri_matches("https://a.example/cb", "https://a.example:8443/cb")
    assert not mcp_auth.redirect_uri_matches("http://localhost/callback", "http://localhost/callback/x")


# ── authorize ────────────────────────────────────────────────────────────────

def test_authorize_flow(world):
    c, ids = world
    client_id = _register(c).get_json()["client_id"]
    verifier, challenge = _pkce()
    args = {"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK,
            "state": "s1", "code_challenge": challenge, "code_challenge_method": "S256"}
    # Anonymous: to the login, with the way back.
    c.post("/api/auth/logout")
    r = c.get("/oauth/authorize", query_string=args)
    assert r.status_code == 302
    assert r.headers["Location"].startswith("/?next=" + quote("/oauth/authorize?", safe=""))
    # Signed in: the consent page names the client and where the user goes next.
    _login(c, "alice@partnerco.example")
    r = c.get("/oauth/authorize", query_string=args)
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "Claude" in html and "claude.ai" in html and "alice@partnerco.example" in html
    assert "never see prices" in html
    # Missing PKCE / wrong response type: back to the client as an error.
    r = c.get("/oauth/authorize", query_string=dict(args, code_challenge=""))
    assert r.status_code == 302
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["Location"]).query).items()}
    assert q["error"] == "invalid_request" and q["state"] == "s1" and q["iss"] == "http://localhost"
    r = c.get("/oauth/authorize", query_string=dict(args, response_type="token"))
    assert "error=unsupported_response_type" in r.headers["Location"]
    r = c.get("/oauth/authorize", query_string=dict(args, scope="admin"))
    assert "error=invalid_scope" in r.headers["Location"]
    r = c.get("/oauth/authorize", query_string=dict(args, resource="https://other.example/mcp"))
    assert "error=invalid_target" in r.headers["Location"]
    # Deny.
    r = c.get("/oauth/authorize", query_string=args)
    nonce = r.get_data(as_text=True).split('name="nonce" value="')[1].split('"')[0]
    r = c.post("/oauth/authorize", data=dict(args, nonce=nonce, decision="deny"))
    assert "error=access_denied" in _leave_target(r)
    # A stale nonce (the form replayed) is refused without a redirect.
    r = c.post("/oauth/authorize", data=dict(args, nonce=nonce, decision="allow"))
    assert r.status_code == 400
    # Allow.
    q = _consent(c, client_id, challenge, state="s2")
    assert set(q) == {"code", "state", "iss"} and q["state"] == "s2"
    with appmod.app.app_context():
        from auth_models import AdminAuditLog
        assert AdminAuditLog.query.filter_by(action="mcp.consent").count() == 1


# ── token ────────────────────────────────────────────────────────────────────

def test_token_exchange_pkce_and_replay(world):
    c, ids = world
    client_id = _register(c).get_json()["client_id"]
    _login(c, "alice@partnerco.example")
    verifier, challenge = _pkce()
    q = _consent(c, client_id, challenge)
    # Wrong verifier first: refused, code still unused.
    r = _exchange(c, client_id, q["code"], "wrong-" + verifier)
    assert r.status_code == 400 and r.get_json()["error"] == "invalid_grant"
    r = _exchange(c, client_id, q["code"], verifier, resource="https://other.example/mcp")
    assert r.get_json()["error"] == "invalid_target"
    r = _exchange(c, client_id, q["code"], verifier)
    assert r.status_code == 200, r.get_json()
    t = r.get_json()
    assert t["token_type"] == "Bearer" and t["expires_in"] == 3600 and t["scope"] == "sizing"
    assert r.headers["Cache-Control"] == "no-store"
    assert _mcp(c, t["access_token"]).status_code == 200
    # Replaying the code revokes what it issued.
    r = _exchange(c, client_id, q["code"], verifier)
    assert r.get_json()["error"] == "invalid_grant"
    r = _mcp(c, t["access_token"])
    assert r.status_code == 401 and 'error="invalid_token"' in r.headers["WWW-Authenticate"]
    # Another client cannot redeem this client's code; unknown grant types fail.
    other = _register(c).get_json()["client_id"]
    q = _consent(c, client_id, challenge)
    assert _exchange(c, other, q["code"], verifier).get_json()["error"] == "invalid_grant"
    r = c.post("/oauth/token", data={"grant_type": "password", "client_id": client_id})
    assert r.get_json()["error"] == "unsupported_grant_type"
    r = c.post("/oauth/token", data={"grant_type": "client_credentials", "client_id": client_id})
    assert r.get_json()["error"] == "unsupported_grant_type"
    assert c.post("/oauth/token", json={"grant_type": "authorization_code"}).status_code == 400


def test_confidential_client_authenticates(world):
    c, ids = world
    d = _register(c, token_endpoint_auth_method="client_secret_basic").get_json()
    _login(c, "alice@partnerco.example")
    verifier, challenge = _pkce()
    q = _consent(c, d["client_id"], challenge)
    r = _exchange(c, d["client_id"], q["code"], verifier)
    assert r.status_code == 401 and r.get_json()["error"] == "invalid_client"
    basic = base64.b64encode(("%s:%s" % (d["client_id"], d["client_secret"])).encode()).decode()
    r = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": q["code"],
                                     "redirect_uri": CALLBACK, "code_verifier": verifier},
               headers={"Authorization": "Basic " + basic})
    assert r.status_code == 200, r.get_json()


def test_refresh_rotation_and_reuse(world):
    c, ids = world
    client_id, access, refresh = _connect(c, ids)
    r = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh,
                                     "client_id": client_id})
    assert r.status_code == 200, r.get_json()
    t2 = r.get_json()
    assert t2["refresh_token"] != refresh and t2["access_token"] != access
    assert _mcp(c, t2["access_token"]).status_code == 200
    assert _mcp(c, access).status_code == 401          # the old access token went with the rotation
    # Reusing the old refresh token revokes the whole family.
    r = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh,
                                     "client_id": client_id})
    assert r.get_json()["error"] == "invalid_grant"
    assert _mcp(c, t2["access_token"]).status_code == 401
    r = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": t2["refresh_token"],
                                     "client_id": client_id})
    assert r.get_json()["error"] == "invalid_grant"
    # Scope cannot widen on refresh; a garbage token is invalid_grant.
    client_id, access, refresh = _connect(c, ids)
    r = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh,
                                     "client_id": client_id, "scope": "sizing admin"})
    assert r.get_json()["error"] == "invalid_scope"
    r = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "nope",
                                     "client_id": client_id})
    assert r.get_json()["error"] == "invalid_grant"


# ── the resource server ──────────────────────────────────────────────────────

def test_bearer_reaches_the_tools_and_is_checked_every_call(world):
    c, ids = world
    client_id, access, refresh = _connect(c, ids)
    r = _mcp(c, access, "tools/call", {"name": "get_constants", "arguments": {}})
    assert r.status_code == 200 and r.get_json()["result"]["isError"] is False
    r = _mcp(c, "not-a-token")
    assert r.status_code == 401 and 'error="invalid_token"' in r.headers["WWW-Authenticate"]
    # Expired access token.
    with appmod.app.app_context():
        row = mcp_auth.OAuthToken.query.filter_by(access_hash=mcp_auth._hash(access)).one()
        row.access_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.session.commit()
    r = _mcp(c, access)
    assert r.status_code == 401 and "expired" in r.headers["WWW-Authenticate"]
    # A disabled account: the token stops working at the next call.
    client_id, access, refresh = _connect(c, ids)
    with appmod.app.app_context():
        db.session.get(User, ids["alice"]).is_disabled = True
        db.session.commit()
    assert _mcp(c, access).status_code == 401
    r = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh,
                                     "client_id": client_id})
    assert r.get_json()["error"] == "invalid_grant"
    with appmod.app.app_context():
        db.session.get(User, ids["alice"]).is_disabled = False
        db.session.commit()


def test_connections_list_and_revoke(world):
    c, ids = world
    client_id, access, refresh = _connect(c, ids)
    _login(c, "alice@partnerco.example")
    rows = c.get("/api/mcp/connections").get_json()
    assert len(rows) == 1 and rows[0]["client_name"] == "Claude" and rows[0]["client_id"] == client_id
    r = c.delete("/api/mcp/connections/" + rows[0]["family"])
    assert r.status_code == 200
    assert c.get("/api/mcp/connections").get_json() == []
    assert _mcp(c, access).status_code == 401
    r = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": refresh,
                                     "client_id": client_id})
    assert r.get_json()["error"] == "invalid_grant"
    assert c.delete("/api/mcp/connections/nope").status_code == 404
    # RFC 7009 revocation by the client itself.
    client_id, access, refresh = _connect(c, ids)
    r = c.post("/oauth/revoke", data={"token": refresh, "client_id": client_id})
    assert r.status_code == 200 and _mcp(c, access).status_code == 401
    # Anonymous callers get nothing.
    c.post("/api/auth/logout")
    assert c.get("/api/mcp/connections").status_code == 401


def test_purge(world):
    c, ids = world
    client_id, access, refresh = _connect(c, ids)
    with appmod.app.app_context():
        old = datetime.now(timezone.utc) - timedelta(days=40)
        for row in mcp_auth.OAuthToken.query.all():
            row.revoked_at = old
        for row in mcp_auth.OAuthCode.query.all():
            row.expires_at = old
        client = db.session.get(mcp_auth.OAuthClient, client_id)
        client.created_at = old
        client.last_used_at = old
        db.session.commit()
        counts = mcp_auth.purge_expired()
        assert counts == {"codes": 1, "tokens": 1, "clients": 1}
        assert db.session.get(mcp_auth.OAuthClient, client_id) is None
