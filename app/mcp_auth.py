"""OAuth 2.1 for the MCP endpoint: the app as its own authorization server,
and the bearer check the endpoint runs (docs/mcp/03-auth.md; MCP
authorization spec 2026-07-28, which claude.ai's connector docs build on).

The account behind every token is one of the tool's own: a person signs in
with their usual login, sees what the client will be allowed to do, and
consents. There are no API keys, personal access tokens, password or
client-credentials grants (findings decision "Auth").

What is here:

* **Discovery.** Protected-resource metadata (RFC 9728) naming this server
  as the issuer, and authorization-server metadata (RFC 8414, also at the
  OIDC discovery path) advertising code + PKCE S256, DCR and CIMD.
* **Clients.** Dynamic registration (RFC 7591; what Gemini CLI does and what
  claude.ai falls back to) and Client ID Metadata Documents (the client_id
  is an HTTPS URL to a JSON document; what Claude Code and claude.ai prefer).
  Redirect URIs are matched exactly, except loopback ones, matched without
  the port (RFC 8252 §7.3; Claude Code picks a port per session).
* **Authorization code + PKCE** with a consent page under the normal session
  login, ``resource`` (RFC 8707) bound to this MCP endpoint, ``iss`` in the
  response (RFC 9207; Gemini CLI checks it).
* **Tokens.** Opaque, random, hashed at rest. Access tokens live an hour;
  refresh tokens rotate on every use and a reused one revokes its family.
  ``invalid_grant`` for a dead refresh token, as Claude expects.
* **The resource server.** ``user_for_request`` turns a bearer token into
  the user, re-checking disabled/blocked on every call exactly as the
  session resolver does.
* **Revocation**: RFC 7009, plus the account's own connection list.
"""
import base64
import hashlib
import json
import os
import secrets
import socket
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

from flask import (Blueprint, Response, abort, current_app, g, jsonify, make_response,
                   redirect, render_template, request, session)

from auth import audit, current_user
from auth_models import JSON_TYPE, User
from database import db
from extensions import limiter

SCOPE = "sizing"
ACCESS_TOKEN_SECONDS = 3600
REFRESH_TOKEN_DAYS = 30
CODE_SECONDS = 600
CIMD_CACHE_HOURS = 24
CIMD_MAX_BYTES = 64 * 1024
CIMD_TIMEOUT = 5
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")

oauth_bp = Blueprint("oauth", __name__)            # discovery, register, token, revoke, authorize
connections_bp = Blueprint("mcp_connections", __name__, url_prefix="/api/mcp")


# ── helpers ──────────────────────────────────────────────────────────────────

def _utcnow():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _hash(value):
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()


def base_url():
    return (os.environ.get("APP_BASE_URL") or request.host_url).rstrip("/")


def issuer():
    return base_url()


def resource_url():
    return base_url() + "/mcp"


def _no_store(resp):
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Pragma"] = "no-cache"
    return resp


def _oauth_error(error, description, status=400):
    return _no_store(jsonify({"error": error, "error_description": description})), status


# ── models ───────────────────────────────────────────────────────────────────

class OAuthClient(db.Model):
    """A registered client: from DCR (one per connection a client makes), a
    CIMD fetch (cached document), or a static entry."""
    __tablename__ = "oauth_clients"

    id = db.Column(db.String(512), primary_key=True)         # client_id (a URL for CIMD)
    kind = db.Column(db.String(8), nullable=False)             # dcr | cimd | static
    name = db.Column(db.String(200))
    secret_hash = db.Column(db.String(64))                     # confidential clients only
    token_endpoint_auth_method = db.Column(db.String(32), nullable=False, default="none")
    redirect_uris = db.Column(JSON_TYPE, nullable=False)
    client_metadata = db.Column(JSON_TYPE)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    fetched_at = db.Column(db.DateTime(timezone=True))         # CIMD: last successful fetch
    last_used_at = db.Column(db.DateTime(timezone=True))

    @property
    def is_confidential(self):
        return bool(self.secret_hash)


class OAuthCode(db.Model):
    __tablename__ = "oauth_codes"

    id = db.Column(db.Integer, primary_key=True)
    code_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    client_id = db.Column(db.String(512), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    redirect_uri = db.Column(db.String(2048), nullable=False)
    scope = db.Column(db.String(200), nullable=False)
    code_challenge = db.Column(db.String(128), nullable=False)
    resource = db.Column(db.String(512))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True))


class OAuthToken(db.Model):
    """One access/refresh pair. A refresh rotation creates a new row in the
    same ``family``; the family is what a person revokes."""
    __tablename__ = "oauth_tokens"

    id = db.Column(db.Integer, primary_key=True)
    family = db.Column(db.String(32), nullable=False, index=True)
    access_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    refresh_hash = db.Column(db.String(64), unique=True, index=True)
    client_id = db.Column(db.String(512), nullable=False)
    client_name = db.Column(db.String(200))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    scope = db.Column(db.String(200), nullable=False)
    resource = db.Column(db.String(512), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    access_expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    refresh_expires_at = db.Column(db.DateTime(timezone=True))
    rotated_at = db.Column(db.DateTime(timezone=True))        # refresh used: superseded
    revoked_at = db.Column(db.DateTime(timezone=True))
    last_used_at = db.Column(db.DateTime(timezone=True))


# ── redirect URIs ────────────────────────────────────────────────────────────

def _is_loopback(uri):
    p = urlparse(uri)
    return p.scheme == "http" and (p.hostname or "").lower() in ("localhost", "127.0.0.1", "::1")


def valid_redirect_uri(uri):
    """What a client may register: HTTPS, or loopback HTTP (RFC 8252)."""
    try:
        p = urlparse(uri)
    except ValueError:
        return False
    if not p.netloc or p.fragment:
        return False
    if p.scheme == "https":
        return True
    return _is_loopback(uri)


def redirect_uri_matches(registered, requested):
    """Exact match, except loopback URIs where the port is ignored: a native
    client binds an ephemeral port per run (Claude Code: localhost and
    127.0.0.1, any port)."""
    if registered == requested:
        return True
    if not (_is_loopback(registered) and _is_loopback(requested)):
        return False
    a, b = urlparse(registered), urlparse(requested)
    return (a.scheme, (a.hostname or "").lower(), a.path or "/", a.query) == \
           (b.scheme, (b.hostname or "").lower(), b.path or "/", b.query)


# ── clients: DCR and CIMD ────────────────────────────────────────────────────

def _fetch_cimd(url):
    """The metadata document behind a CIMD client_id. HTTPS only, public
    host only, bounded size and time (an SSRF guard, since the URL is
    attacker-chosen)."""
    import urllib.request
    p = urlparse(url)
    if p.scheme != "https" or not p.hostname or p.port not in (None, 443) or p.fragment:
        raise ValueError("client_id must be an https URL")
    host = p.hostname.lower()
    if host in ("localhost",) or host.endswith(".local"):
        raise ValueError("client_id host not allowed")
    try:
        for info in socket.getaddrinfo(host, 443):
            ip = info[4][0]
            import ipaddress
            addr = ipaddress.ip_address(ip)
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                raise ValueError("client_id host not allowed")
    except socket.gaierror:
        raise ValueError("client_id host does not resolve")
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "scale-sizer-mcp/1.0"})
    with urllib.request.urlopen(req, timeout=CIMD_TIMEOUT) as resp:
        data = resp.read(CIMD_MAX_BYTES + 1)
    if len(data) > CIMD_MAX_BYTES:
        raise ValueError("client metadata document too large")
    return json.loads(data.decode("utf-8"))


def _cimd_client(client_id):
    """The cached or freshly fetched CIMD client, or None when the document
    is unusable. The document's client_id MUST equal its URL."""
    row = db.session.get(OAuthClient, client_id)
    fresh = row is not None and row.kind == "cimd" and row.fetched_at and \
        _aware(row.fetched_at) > _utcnow() - timedelta(hours=CIMD_CACHE_HOURS)
    if fresh:
        return row
    try:
        doc = _fetch_cimd(client_id)
        if doc.get("client_id") != client_id:
            raise ValueError("client_id in the document does not match its URL")
        uris = [u for u in doc.get("redirect_uris") or [] if isinstance(u, str) and valid_redirect_uri(u)]
        if not uris:
            raise ValueError("no usable redirect_uris")
    except Exception as exc:    # noqa: BLE001 — fetch, parse or validation
        current_app.logger.warning("CIMD %s unusable: %s", client_id[:200], exc)
        return row if row is not None and row.kind == "cimd" else None
    if row is None:
        row = OAuthClient(id=client_id, kind="cimd", redirect_uris=uris)
        db.session.add(row)
    row.redirect_uris = uris
    row.name = (doc.get("client_name") or "")[:200] or urlparse(client_id).hostname
    row.client_metadata = {k: doc.get(k) for k in ("client_name", "client_uri", "logo_uri",
                                                    "grant_types", "response_types")}
    row.token_endpoint_auth_method = "none"
    row.fetched_at = _utcnow()
    db.session.commit()
    return row


def load_client(client_id):
    """A client by id: a registered row, else a CIMD document if the id is
    an https URL."""
    if not client_id:
        return None
    row = db.session.get(OAuthClient, client_id)
    if row is not None and row.kind != "cimd":
        return row
    if client_id.startswith("https://"):
        return _cimd_client(client_id)
    return row


# ── discovery ────────────────────────────────────────────────────────────────

def protected_resource_metadata():
    return {
        "resource": resource_url(),
        "authorization_servers": [issuer()],
        "scopes_supported": [SCOPE],
        "bearer_methods_supported": ["header"],
        "resource_name": "Scale Computing Infrastructure Sizer",
    }


def authorization_server_metadata():
    base = issuer()
    return {
        "issuer": base,
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "registration_endpoint": base + "/oauth/register",
        "revocation_endpoint": base + "/oauth/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
        "revocation_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
        "scopes_supported": [SCOPE],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
        "service_documentation": base + "/manual",
    }


@oauth_bp.route("/.well-known/oauth-protected-resource/mcp")
@oauth_bp.route("/.well-known/oauth-protected-resource")
def well_known_resource():
    resp = jsonify(protected_resource_metadata())
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@oauth_bp.route("/.well-known/oauth-authorization-server")
@oauth_bp.route("/.well-known/openid-configuration")
def well_known_as():
    resp = jsonify(authorization_server_metadata())
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


# ── dynamic client registration ──────────────────────────────────────────────

@oauth_bp.route("/oauth/register", methods=["POST"])
@limiter.limit("30 per hour")
def register_client():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _oauth_error("invalid_client_metadata", "A JSON object is required")
    uris = data.get("redirect_uris")
    if not isinstance(uris, list) or not uris or not all(isinstance(u, str) for u in uris):
        return _oauth_error("invalid_redirect_uri", "redirect_uris must be a non-empty list")
    bad = [u for u in uris if not valid_redirect_uri(u)]
    if bad:
        return _oauth_error("invalid_redirect_uri", "Only https and loopback http redirect URIs "
                            "are accepted: %s" % ", ".join(bad[:3]))
    grants = data.get("grant_types") or ["authorization_code"]
    if not set(grants) <= {"authorization_code", "refresh_token"}:
        return _oauth_error("invalid_client_metadata", "Only authorization_code and refresh_token "
                            "grants are supported")
    if (data.get("response_types") or ["code"]) != ["code"]:
        return _oauth_error("invalid_client_metadata", "Only the code response type is supported")
    method = data.get("token_endpoint_auth_method") or "none"
    if method not in ("none", "client_secret_basic", "client_secret_post"):
        return _oauth_error("invalid_client_metadata", "Unsupported token_endpoint_auth_method")
    scope = data.get("scope")
    if scope and not set(scope.split()) <= {SCOPE}:
        return _oauth_error("invalid_client_metadata", "Unknown scope; only %r exists" % SCOPE)

    client_id = "dcr_" + secrets.token_urlsafe(18)
    secret = secrets.token_urlsafe(32) if method != "none" else None
    row = OAuthClient(
        id=client_id, kind="dcr", name=(str(data.get("client_name") or "")[:200] or None),
        secret_hash=_hash(secret) if secret else None, token_endpoint_auth_method=method,
        redirect_uris=uris,
        client_metadata={k: data.get(k) for k in ("client_name", "client_uri", "logo_uri",
                                                   "software_id", "software_version")})
    db.session.add(row)
    db.session.commit()
    body = {
        "client_id": client_id,
        "client_id_issued_at": int(_utcnow().timestamp()),
        "client_name": row.name,
        "redirect_uris": uris,
        "grant_types": sorted(set(grants) | {"refresh_token"}),
        "response_types": ["code"],
        "token_endpoint_auth_method": method,
        "scope": SCOPE,
    }
    if secret:
        body["client_secret"] = secret
        body["client_secret_expires_at"] = 0
    return _no_store(jsonify(body)), 201


# ── authorization endpoint ───────────────────────────────────────────────────

def _with_query(uri, params):
    p = urlparse(uri)
    query = dict(parse_qs(p.query, keep_blank_values=True))
    query = {k: v[0] for k, v in query.items()}
    query.update({k: v for k, v in params.items() if v is not None})
    return urlunparse(p._replace(query=urlencode(query)))


def _redirect_with(uri, params):
    return redirect(_with_query(uri, params), code=302)


def _leave_to(uri, params, allowed):
    """The consent decision's way back to the client: a page that navigates
    to the callback, not a 302. Chrome applies the proxy's CSP
    ``form-action 'self'`` to a redirect that follows a form POST and would
    block the hop to claude.ai or a loopback port."""
    url = _with_query(uri, params)
    resp = make_response(render_template("oauth_redirect.html", url=url, allowed=allowed,
                                         host=urlparse(url).hostname or url))
    return _no_store(resp)


def _authorize_request(args):
    """Validate an authorize request. Returns (client, redirect_uri, params)
    or raises AuthorizeError. Errors in client_id / redirect_uri can only be
    shown (never redirected to); the rest go back to the client."""
    client = load_client(args.get("client_id"))
    if client is None:
        raise AuthorizeError("invalid_client", "Unknown client_id", show=True)
    requested = args.get("redirect_uri")
    registered = client.redirect_uris or []
    if requested:
        match = next((r for r in registered if redirect_uri_matches(r, requested)), None)
        if match is None:
            raise AuthorizeError("invalid_request", "redirect_uri is not registered for this client",
                                 show=True)
        redirect_uri = requested
    elif len(registered) == 1 and not _is_loopback(registered[0]):
        redirect_uri = registered[0]
    else:
        raise AuthorizeError("invalid_request", "redirect_uri is required", show=True)

    def fail(error, desc):
        raise AuthorizeError(error, desc, redirect_uri=redirect_uri, state=args.get("state"))

    if args.get("response_type") != "code":
        fail("unsupported_response_type", "response_type must be code")
    challenge = args.get("code_challenge") or ""
    if not (43 <= len(challenge) <= 128):
        fail("invalid_request", "code_challenge (PKCE) is required")
    if args.get("code_challenge_method", "S256") != "S256":
        fail("invalid_request", "code_challenge_method must be S256")
    scope = (args.get("scope") or SCOPE).split()
    if not set(scope) <= {SCOPE}:
        fail("invalid_scope", "Only the %r scope exists" % SCOPE)
    resource = args.get("resource")
    if resource and resource.rstrip("/") != resource_url():
        fail("invalid_target", "resource must be %s" % resource_url())
    params = {"client_id": client.id, "redirect_uri": redirect_uri, "scope": " ".join(scope),
              "state": args.get("state"), "code_challenge": challenge,
              "resource": resource_url()}
    return client, redirect_uri, params


class AuthorizeError(Exception):
    def __init__(self, error, description, show=False, redirect_uri=None, state=None):
        super().__init__(description)
        self.error, self.description = error, description
        self.show, self.redirect_uri, self.state = show, redirect_uri, state


def _authorize_failure(exc):
    if exc.show or not exc.redirect_uri:
        return render_template("oauth_error.html", error=exc.error,
                               description=exc.description), 400
    return _redirect_with(exc.redirect_uri, {"error": exc.error,
                                             "error_description": exc.description,
                                             "state": exc.state, "iss": issuer()})


@oauth_bp.route("/oauth/authorize", methods=["GET"])
@limiter.limit("60 per minute")
def authorize():
    try:
        client, redirect_uri, params = _authorize_request(request.args)
    except AuthorizeError as exc:
        return _authorize_failure(exc)
    user = current_user()
    if user is None:
        # The app's login lives in the shell page; it returns here afterwards.
        return redirect("/?next=" + quote(request.full_path, safe=""))
    nonce = secrets.token_urlsafe(16)
    session["oauth_nonce"] = nonce
    host = urlparse(redirect_uri).hostname or redirect_uri
    return render_template(
        "oauth_consent.html", client=client, params=params, nonce=nonce,
        redirect_host=host, loopback_only=all(_is_loopback(u) for u in client.redirect_uris or []),
        user_email=user.email, scope_lines=SCOPE_LINES)


SCOPE_LINES = [
    "Run sizings from figures you give it or files you upload through it.",
    "Read the projects and saved sizings your account can see.",
    "Save new sizings and BOM checks into your own projects when you ask.",
    "It can never see prices, change or delete existing sizings, or act as an administrator.",
    "What it reads or sizes goes to the assistant's provider, like anything you type into it.",
]


@oauth_bp.route("/oauth/authorize", methods=["POST"])
@limiter.limit("60 per minute")
def authorize_decide():
    user = current_user()
    if user is None:
        abort(401)
    form = request.form
    if not form.get("nonce") or form.get("nonce") != session.pop("oauth_nonce", None):
        return render_template("oauth_error.html", error="invalid_request",
                               description="The consent form expired; start again from the client."), 400
    try:
        client, redirect_uri, params = _authorize_request(form)
    except AuthorizeError as exc:
        return _authorize_failure(exc)
    if form.get("decision") != "allow":
        audit("mcp.consent_denied", "client %s" % client.id[:200], actor=user)
        db.session.commit()
        return _leave_to(redirect_uri, {"error": "access_denied",
                                        "error_description": "The user declined",
                                        "state": params["state"], "iss": issuer()}, False)
    code = secrets.token_urlsafe(32)
    db.session.add(OAuthCode(
        code_hash=_hash(code), client_id=client.id, user_id=user.id,
        redirect_uri=redirect_uri, scope=params["scope"], code_challenge=params["code_challenge"],
        resource=params["resource"], expires_at=_utcnow() + timedelta(seconds=CODE_SECONDS)))
    client.last_used_at = _utcnow()
    audit("mcp.consent", "client %s (%s)" % (client.id[:200], client.name or "unnamed"), actor=user)
    db.session.commit()
    return _leave_to(redirect_uri, {"code": code, "state": params["state"], "iss": issuer()}, True)


# ── token endpoint ───────────────────────────────────────────────────────────

def _client_from_token_request():
    """(client, error) with the client authenticated as its registration
    requires: a public client by id, a confidential one by secret (Basic or
    form)."""
    client_id, secret = request.form.get("client_id"), request.form.get("client_secret")
    auth = request.authorization
    if auth and auth.type == "basic":
        client_id, secret = auth.username or client_id, auth.password
    client = load_client(client_id)
    if client is None:
        return None, _oauth_error("invalid_client", "Unknown client", 401)
    if client.is_confidential:
        if not secret or not secrets.compare_digest(_hash(secret), client.secret_hash):
            return None, _oauth_error("invalid_client", "Client authentication failed", 401)
    return client, None


def _pkce_ok(verifier, challenge):
    if not verifier or not (43 <= len(verifier) <= 128):
        return False
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return secrets.compare_digest(base64.urlsafe_b64encode(digest).rstrip(b"=").decode(), challenge)


def _issue(client, user_id, scope, resource, family=None):
    access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    now = _utcnow()
    row = OAuthToken(
        family=family or secrets.token_hex(8), access_hash=_hash(access), refresh_hash=_hash(refresh),
        client_id=client.id, client_name=client.name, user_id=user_id, scope=scope,
        resource=resource, access_expires_at=now + timedelta(seconds=ACCESS_TOKEN_SECONDS),
        refresh_expires_at=now + timedelta(days=REFRESH_TOKEN_DAYS))
    db.session.add(row)
    client.last_used_at = now
    db.session.commit()
    return _no_store(jsonify({"access_token": access, "token_type": "Bearer",
                              "expires_in": ACCESS_TOKEN_SECONDS, "refresh_token": refresh,
                              "scope": scope}))


def _revoke_family(family, when=None):
    OAuthToken.query.filter(OAuthToken.family == family, OAuthToken.revoked_at.is_(None)).update(
        {"revoked_at": when or _utcnow()}, synchronize_session=False)


@oauth_bp.route("/oauth/token", methods=["POST"])
@limiter.limit("120 per minute")
def token():
    if not request.form:
        return _oauth_error("invalid_request", "A form-encoded body is required")
    grant = request.form.get("grant_type")
    client, err = _client_from_token_request()
    if err:
        return err
    resource = request.form.get("resource")
    if resource and resource.rstrip("/") != resource_url():
        return _oauth_error("invalid_target", "resource must be %s" % resource_url())

    if grant == "authorization_code":
        row = OAuthCode.query.filter_by(code_hash=_hash(request.form.get("code") or "")).first()
        if row is None or row.client_id != client.id:
            return _oauth_error("invalid_grant", "Unknown authorization code")
        if row.used_at is not None:
            # Replay: every token issued from this code is revoked (OAuth 2.1 §4.1.3).
            _revoke_family("code%d" % row.id)
            db.session.commit()
            return _oauth_error("invalid_grant", "Authorization code already used")
        if _aware(row.expires_at) < _utcnow():
            return _oauth_error("invalid_grant", "Authorization code expired")
        if request.form.get("redirect_uri") and request.form.get("redirect_uri") != row.redirect_uri:
            return _oauth_error("invalid_grant", "redirect_uri does not match")
        if not _pkce_ok(request.form.get("code_verifier"), row.code_challenge):
            return _oauth_error("invalid_grant", "PKCE verification failed")
        row.used_at = _utcnow()
        db.session.commit()
        return _issue(client, row.user_id, row.scope, row.resource or resource_url(),
                      family="code%d" % row.id)

    if grant == "refresh_token":
        row = OAuthToken.query.filter_by(refresh_hash=_hash(request.form.get("refresh_token") or "")).first()
        if row is None or row.client_id != client.id:
            return _oauth_error("invalid_grant", "Unknown refresh token")
        if row.rotated_at is not None:
            # A rotated refresh token presented again: theft or a lost
            # response; either way the whole family goes.
            _revoke_family(row.family)
            db.session.commit()
            return _oauth_error("invalid_grant", "Refresh token reuse detected; sign in again")
        if row.revoked_at is not None or _aware(row.refresh_expires_at) < _utcnow():
            return _oauth_error("invalid_grant", "Refresh token expired or revoked")
        user = db.session.get(User, row.user_id)
        if not _user_ok(user):
            _revoke_family(row.family)
            db.session.commit()
            return _oauth_error("invalid_grant", "The account is no longer active")
        scope = request.form.get("scope") or row.scope
        if not set(scope.split()) <= set(row.scope.split()):
            return _oauth_error("invalid_scope", "Scope cannot be widened on refresh")
        row.rotated_at = row.revoked_at = _utcnow()
        db.session.commit()
        return _issue(client, row.user_id, scope, row.resource, family=row.family)

    return _oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token")


@oauth_bp.route("/oauth/revoke", methods=["POST"])
@limiter.limit("60 per minute")
def revoke():
    client, err = _client_from_token_request()
    if err:
        return err
    value = _hash(request.form.get("token") or "")
    row = (OAuthToken.query.filter_by(access_hash=value).first()
           or OAuthToken.query.filter_by(refresh_hash=value).first())
    if row is not None and row.client_id == client.id:
        _revoke_family(row.family)
        db.session.commit()
    return _no_store(Response(status=200))


# ── the resource server ──────────────────────────────────────────────────────

class TokenError(Exception):
    """A bearer token was presented and is not acceptable."""

    def __init__(self, error, description):
        super().__init__(description)
        self.error, self.description = error, description


def _user_ok(user):
    return user is not None and not user.is_disabled and not (user.tenant and user.tenant.is_blocked)


def user_for_request(req):
    """The account behind the request's bearer token, or None when there is
    no bearer token. Raises TokenError for a token that is unknown,
    expired, revoked, bound to another resource, or whose account is
    disabled or blocked."""
    header = req.headers.get("Authorization") or ""
    if not header.lower().startswith("bearer "):
        return None
    value = header[7:].strip()
    row = OAuthToken.query.filter_by(access_hash=_hash(value)).first() if value else None
    if row is None:
        raise TokenError("invalid_token", "Unknown access token")
    if row.revoked_at is not None:
        raise TokenError("invalid_token", "The token was revoked")
    if _aware(row.access_expires_at) < _utcnow():
        raise TokenError("invalid_token", "The access token expired")
    if row.resource.rstrip("/") != resource_url():
        raise TokenError("invalid_token", "The token is for another resource")
    if SCOPE not in row.scope.split():
        raise TokenError("insufficient_scope", "The %r scope is required" % SCOPE)
    user = db.session.get(User, row.user_id)
    if not _user_ok(user):
        raise TokenError("invalid_token", "The account is no longer active")
    row.last_used_at = _utcnow()
    db.session.commit()
    g.mcp_token = row
    return user


# ── the account's connections ────────────────────────────────────────────────

def _login_required(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        if current_user() is None:
            return jsonify({"error": "Authentication required"}), 401
        return fn(*a, **kw)
    return wrapper


def _active_families(user_id):
    rows = OAuthToken.query.filter(OAuthToken.user_id == user_id,
                                   OAuthToken.revoked_at.is_(None)).all()
    families = {}
    for r in rows:
        if r.refresh_expires_at and _aware(r.refresh_expires_at) < _utcnow():
            continue
        f = families.setdefault(r.family, {"family": r.family, "client_id": r.client_id,
                                           "client_name": r.client_name, "created_at": r.created_at,
                                           "last_used_at": None})
        f["created_at"] = min(f["created_at"], r.created_at)
        if r.last_used_at and (f["last_used_at"] is None or r.last_used_at > f["last_used_at"]):
            f["last_used_at"] = r.last_used_at
    out = sorted(families.values(), key=lambda f: f["created_at"], reverse=True)
    for f in out:
        f["created_at"] = _aware(f["created_at"]).isoformat()
        f["last_used_at"] = _aware(f["last_used_at"]).isoformat() if f["last_used_at"] else None
    return out


@connections_bp.route("/connections")
@_login_required
def list_connections():
    """The assistants connected to this account, for the account page."""
    return jsonify(_active_families(current_user().id))


@connections_bp.route("/connections/<family>", methods=["DELETE"])
@_login_required
def revoke_connection(family):
    user = current_user()
    rows = OAuthToken.query.filter_by(family=family, user_id=user.id).count()
    if not rows:
        return jsonify({"error": "No such connection"}), 404
    _revoke_family(family)
    audit("mcp.revoke", "connection %s" % family, actor=user)
    db.session.commit()
    return jsonify({"ok": True})


def purge_expired():
    """Spent codes, dead tokens and clients nobody used: from the
    maintenance loop. Returns the counts."""
    now = _utcnow()
    codes = OAuthCode.query.filter(OAuthCode.expires_at < now - timedelta(days=1)).delete(
        synchronize_session=False)
    tokens = OAuthToken.query.filter(
        db.or_(OAuthToken.revoked_at < now - timedelta(days=7),
               OAuthToken.refresh_expires_at < now - timedelta(days=7))).delete(
        synchronize_session=False)
    live_clients = {c for (c,) in db.session.query(OAuthToken.client_id).distinct()}
    stale = OAuthClient.query.filter(OAuthClient.kind == "dcr",
                                     OAuthClient.created_at < now - timedelta(days=30)).all()
    clients = 0
    for c in stale:
        if c.id not in live_clients and (c.last_used_at is None
                                         or _aware(c.last_used_at) < now - timedelta(days=30)):
            db.session.delete(c)
            clients += 1
    db.session.commit()
    return {"codes": codes, "tokens": tokens, "clients": clients}
