"""The MCP endpoint: stateless Streamable HTTP, JSON responses, dual-era
(docs/mcp/01-tool-design.md §0, §7; spec 2026-07-28 and 2025-11-25).

One POST endpoint, ``/mcp``. Each request is parsed, checked and answered on
its own: there is no session, no SSE and no server-initiated message, because
the tools need none of that. Two client generations are served:

* **Modern (2026-07-28):** the request carries its protocol version in
  ``params._meta`` and the ``MCP-Protocol-Version`` header, plus the
  ``Mcp-Method`` / ``Mcp-Name`` headers mirrored from the body, which the
  server MUST validate (``-32020`` on mismatch). Results carry
  ``resultType``. ``server/discover`` replaces the handshake.
* **Legacy (2025-11-25 and the two before it):** the client opens with
  ``initialize`` and sends ``notifications/initialized``; results have no
  ``resultType``; no session id is minted (a stateless server may omit it)
  and one sent by the client is ignored.

The era is read per request, so both can be served on the same endpoint.
Authentication is a bearer token checked by ``mcp_auth`` (Phase 3); until
then every request without one gets the 401 that starts a client's sign-in,
and tests inject a user through ``app.config["MCP_TEST_USER_ID"]``.
"""
import base64
import json
import os
from urllib.parse import urlparse

from flask import Blueprint, Response, current_app, g, jsonify, request

from extensions import limiter
import mcp_limits
import mcp_registry
import mcp_tools  # noqa: F401 — registers the stateless tools
import mcp_sizings  # noqa: F401 — registers the saved-sizing tools
import mcp_save  # noqa: F401 — registers save_sizing
import mcp_uploads  # noqa: F401 — registers the upload tools
import mcp_bom  # noqa: F401 — registers the BOM tools

mcp_bp = Blueprint("mcp", __name__)

SERVER_INFO = {"name": "scale-sizer", "version": "1.0"}
INSTRUCTIONS = (
    "Scale Computing infrastructure sizing. Sizings run on MEASURED figures from the customer's "
    "environment: upload a Live Optics, RVTools or Nutanix export with start_upload, or pass the "
    "figures the user gives you. Never guess a measured value; if the largest VM is unknown, ask. "
    "create_sizing stores nothing; save_sizing only when the user asks. Prices are never available."
)
MODERN_VERSIONS = ("2026-07-28",)
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# JSON-RPC and MCP error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
HEADER_MISMATCH = -32020
UNSUPPORTED_VERSION = -32022
RATE_LIMITED = -32000       # implementation-defined; carried with HTTP 429 + Retry-After


def _error(code, message, rid=None, status=200, data=None):
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return jsonify({"jsonrpc": "2.0", "id": rid, "error": err}), status


def _ok(rid, result):
    return jsonify({"jsonrpc": "2.0", "id": rid, "result": result}), 200


def resource_url():
    """The canonical URL of this MCP server, which tokens are bound to and
    the protected-resource metadata names."""
    base = (os.environ.get("APP_BASE_URL") or request.host_url).rstrip("/")
    return base + "/mcp"


def _allowed_origin(origin):
    """DNS-rebinding guard (spec: Origin MUST be validated). Server-to-server
    clients send no Origin; browsers may send ours, a loopback one in
    development, or one listed in MCP_ALLOWED_ORIGINS."""
    if not origin:
        return True
    host = urlparse(origin).hostname or ""
    own = urlparse(os.environ.get("APP_BASE_URL") or request.host_url).hostname or ""
    if host and host == own:
        return True
    if host in ("localhost", "127.0.0.1") and (current_app.debug or current_app.testing
                                               or os.environ.get("MCP_ALLOW_LOCAL_ORIGINS")):
        return True
    allowed = [o.strip().lower() for o in os.environ.get("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    return origin.lower().rstrip("/") in allowed


def authenticate():
    """The account holder behind the request's bearer token (mcp_auth), or
    None when there is none. A token that is presented but not acceptable
    raises mcp_auth.TokenError. The test hook injects a user without a
    token, gated on app.testing."""
    import mcp_auth
    user = mcp_auth.user_for_request(request)
    if user is not None:
        return user
    if current_app.testing and current_app.config.get("MCP_TEST_USER_ID"):
        from auth_models import User
        from database import db
        return db.session.get(User, current_app.config["MCP_TEST_USER_ID"])
    return None


def unauthorized(error=None, description=None):
    """The 401 that starts a client's sign-in: ``WWW-Authenticate`` points
    at the protected-resource metadata (RFC 9728). With ``error`` it is the
    RFC 6750 challenge for a token that was presented and refused, so a
    client refreshes (invalid_token) instead of re-registering."""
    base = (os.environ.get("APP_BASE_URL") or request.host_url).rstrip("/")
    parts = ['resource_metadata="%s/.well-known/oauth-protected-resource/mcp"' % base,
             'scope="sizing"']
    if error:
        parts.append('error="%s"' % error)
        if description:
            parts.append('error_description="%s"' % description.replace('"', ""))
    resp = jsonify({"error": error or "unauthorized",
                    "error_description": description or "Sign in to the sizing tool."})
    resp.status_code = 403 if error == "insufficient_scope" else 401
    resp.headers["WWW-Authenticate"] = "Bearer " + ", ".join(parts)
    return resp


def _decode_header_value(value):
    """The Base64 sentinel encoding of Mcp-Name / Mcp-Param-* values."""
    if value is None:
        return None
    if value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[len("=?base64?"):-2]).decode("utf-8")
        except Exception:   # noqa: BLE001
            return None
    return value


@mcp_bp.route("/mcp", methods=["GET", "DELETE", "PUT", "PATCH"])
def mcp_not_allowed():
    # No GET listen stream, no sessions to DELETE (2026-07-28 §Earlier revisions).
    return Response(status=405, headers={"Allow": "POST"})


@mcp_bp.route("/mcp", methods=["POST"])
@limiter.limit("60 per minute", deduct_when=lambda r: r.status_code == 401)
def mcp_endpoint():
    # Per IP, only refused tokens count: a guessing client is slowed down,
    # while the accepted calls of signed-in users, who share claude.ai's
    # egress range, never add up. Those are limited per account (mcp_limits).
    if not _allowed_origin(request.headers.get("Origin")):
        return _error(INVALID_REQUEST, "Origin not allowed", status=403)

    import mcp_auth
    try:
        user = authenticate()
    except mcp_auth.TokenError as exc:
        return unauthorized(exc.error, exc.description)
    if user is None:
        return unauthorized()
    g.mcp_user = user

    try:
        body = json.loads(request.get_data(as_text=True) or "")
    except ValueError:
        return _error(PARSE_ERROR, "Parse error", status=400)
    if isinstance(body, list):
        return _error(INVALID_REQUEST, "Batching is not supported", status=400)
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str):
        return _error(INVALID_REQUEST, "Invalid Request", status=400)

    method = body["method"]
    params = body.get("params") if isinstance(body.get("params"), dict) else {}
    rid = body.get("id")
    is_notification = "id" not in body
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    header_version = request.headers.get("MCP-Protocol-Version")
    meta_version = meta.get(META_VERSION)
    modern = meta_version is not None or header_version in MODERN_VERSIONS

    if modern:
        # Header/body mirrors are REQUIRED and MUST agree (transport §Server Validation).
        if not header_version or header_version != meta_version:
            return _error(HEADER_MISMATCH, "MCP-Protocol-Version header (%s) does not match "
                          "_meta protocol version (%s)" % (header_version, meta_version), rid, 400)
        if meta_version not in MODERN_VERSIONS:
            return _error(UNSUPPORTED_VERSION, "Unsupported protocol version", rid, 400,
                          {"supported": list(MODERN_VERSIONS + LEGACY_VERSIONS),
                           "requested": meta_version})
        if not is_notification:
            if request.headers.get("Mcp-Method") != method:
                return _error(HEADER_MISMATCH, "Mcp-Method header missing or does not match "
                              "the request method", rid, 400)
            if method == "tools/call":
                name = _decode_header_value(request.headers.get("Mcp-Name"))
                if name is None or name != params.get("name"):
                    return _error(HEADER_MISMATCH, "Mcp-Name header missing or does not match "
                                  "params.name", rid, 400)
    elif header_version and header_version not in LEGACY_VERSIONS:
        return _error(UNSUPPORTED_VERSION, "Unsupported protocol version", rid, 400,
                      {"supported": list(MODERN_VERSIONS + LEGACY_VERSIONS),
                       "requested": header_version})

    if is_notification:
        # notifications/initialized, notifications/cancelled: accepted, nothing to do.
        return Response(status=202)

    lang = (request.headers.get("Accept-Language") or "en")[:2].lower()
    caller = mcp_registry.Caller(user, lang=lang if lang.isalpha() else "en")

    if method == "ping":
        return _ok(rid, _result({}, modern))
    if method == "server/discover" and modern:
        return _ok(rid, _result({
            "supportedVersions": list(MODERN_VERSIONS),
            "capabilities": {"tools": {}},
            "_meta": {META_SERVER_INFO: SERVER_INFO},
            "instructions": INSTRUCTIONS,
            "ttlMs": 3600000, "cacheScope": "public",
        }, modern))
    if method == "initialize" and not modern:
        requested = params.get("protocolVersion")
        version = requested if requested in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        return _ok(rid, {"protocolVersion": version, "capabilities": {"tools": {}},
                         "serverInfo": SERVER_INFO, "instructions": INSTRUCTIONS})
    if method == "tools/list":
        result = {"tools": mcp_registry.definitions()}
        if modern:
            result.update({"ttlMs": 3600000, "cacheScope": "public"})
        return _ok(rid, _result(result, modern))
    if method == "tools/call":
        name = params.get("name")
        if name not in mcp_registry.TOOLS:
            return _error(INVALID_PARAMS, "Unknown tool: %s" % name, rid)
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            return _error(INVALID_PARAMS, "arguments must be an object", rid)
        wait = mcp_limits.retry_after(user, name)
        if wait:
            resp, status = _error(RATE_LIMITED, "Rate limit exceeded; retry in %d seconds"
                                  % wait, rid, 429, {"retryAfterSeconds": wait})
            resp.headers["Retry-After"] = str(wait)
            return resp, status
        structured, text, is_error = mcp_registry.call(name, arguments or {}, caller)
        result = {"content": [{"type": "text", "text": text}], "isError": is_error}
        if structured is not None:
            result["structuredContent"] = structured
        return _ok(rid, _result(result, modern))

    if modern:
        return _error(METHOD_NOT_FOUND, "Method not found: %s" % method, rid, 404)
    return _error(METHOD_NOT_FOUND, "Method not found: %s" % method, rid)


def _result(result, modern):
    if modern:
        result = dict(result)
        result.setdefault("resultType", "complete")
    return result
