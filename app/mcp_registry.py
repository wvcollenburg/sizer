"""The MCP tool registry: definitions, the caller, errors and result rendering
(docs/mcp/01-tool-design.md §1, §4, §5).

A tool is a definition (what ``tools/list`` publishes) plus a handler
``fn(caller, args) -> (structured, text)``. ``call()`` validates the arguments
against the published schema, applies defaults, runs the handler and renders
the result the transport sends: ``structuredContent`` plus a text block that
holds the human summary followed by the same JSON (§0).

The caller is the account holder behind the request, **role-capped**: every
visibility and permission helper sees ``role == "user"``, whatever is stored
(findings §7.2). The cap is a proxy, so the helpers are called unchanged.
"""
import json
import time

from flask import current_app

import mcp_schema

SCHEMA_VERSION = "1"

TOOLS = {}          # name -> Tool, in registration order (dicts keep it)


class ToolError(Exception):
    """A tool execution error (``isError: true``): something the model can
    act on. ``code`` is the stable key of §4; ``extra`` carries the
    structured detail (missing/invalid lists, options, retry_after_s)."""

    def __init__(self, code, message, retryable=False, **extra):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.extra = extra

    def body(self):
        out = {"code": self.code, "message": str(self), "retryable": self.retryable}
        out.update(self.extra)
        return {"error": out}


class CappedUser:
    """A User seen as an ordinary user. Super and tenant admins widen data
    access through the shared helpers (source_for, _visible_configs,
    can_edit), so over MCP every check runs against this view. Attributes not
    overridden pass through: id, email, tenant_id, tenant, is_scale (a tenant
    property, not a role), display_name."""
    role = "user"
    is_super_admin = False
    is_tenant_admin = False

    def __init__(self, user):
        self._user = user

    def __getattr__(self, name):
        return getattr(self._user, name)

    def __repr__(self):
        return "<CappedUser %s>" % getattr(self._user, "email", "?")


class Caller:
    """Who is calling and how: the capped user plus the request's language
    for the strings that have translations."""

    def __init__(self, user, lang="en"):
        self.user = CappedUser(user) if user is not None and not isinstance(user, CappedUser) else user
        self.lang = lang


class Tool:
    def __init__(self, name, title, description, input_schema, handler,
                 read_only, destructive=False, idempotent=True, open_world=False):
        self.name = name
        self.title = title
        self.description = description
        self.input_schema = input_schema
        self.handler = handler
        self.annotations = {"title": title, "readOnlyHint": read_only,
                            "destructiveHint": destructive,
                            "idempotentHint": idempotent, "openWorldHint": open_world}

    def definition(self):
        return {"name": self.name, "title": self.title, "description": self.description,
                "inputSchema": self.input_schema, "annotations": dict(self.annotations)}


def tool(name, title, description, input_schema, read_only, destructive=False,
         idempotent=True, open_world=False):
    """Register ``fn(caller, args) -> (structured, text)`` as a tool."""
    def register(fn):
        if name in TOOLS:
            raise ValueError("duplicate tool %s" % name)
        TOOLS[name] = Tool(name, title, description, input_schema, fn,
                           read_only, destructive, idempotent, open_world)
        return fn
    return register


def definitions():
    """Every tool's published definition, in registration order."""
    return [t.definition() for t in TOOLS.values()]


def invalid_input(problems, message=None):
    missing = [p["field"] for p in problems if p["problem"] == "is required"]
    invalid = [p for p in problems if p["problem"] != "is required"]
    if message is None:
        parts = []
        if missing:
            parts.append("missing: " + ", ".join(missing))
        if invalid:
            parts.append("; ".join("%s %s" % (p["field"], p["problem"]) for p in invalid))
        message = "Invalid input (" + "; ".join(parts) + ")."
    return ToolError("invalid_input", message, missing=missing, invalid=invalid)


def call(name, args, caller):
    """Run a tool. Returns ``(structured, text, is_error)``. A ToolError is
    rendered as the §4 error body; anything else is logged with a correlation
    id and rendered as ``internal_error``. Arguments and results are never
    logged."""
    tool_ = TOOLS.get(name)
    if tool_ is None:
        raise KeyError(name)
    args = args if isinstance(args, dict) else {}
    started = time.monotonic()
    status = "ok"
    try:
        problems = mcp_schema.validate(tool_.input_schema, args)
        if problems:
            raise invalid_input(problems)
        args = mcp_schema.apply_defaults(tool_.input_schema, args)
        structured, text = tool_.handler(caller, args)
        structured = scrub(structured)
        return structured, render_text(text, structured), False
    except ToolError as exc:
        status = exc.code
        body = exc.body()
        return None, json.dumps(body, ensure_ascii=False), True
    except Exception as exc:   # noqa: BLE001 — a tool must not take the server down
        from database import db
        try:
            db.session.rollback()
        except Exception:      # noqa: BLE001
            pass
        ref = "%x" % int(time.time() * 1000)
        status = "internal_error"
        current_app.logger.exception("mcp tool %s failed (ref %s): %s", name, ref, exc)
        body = ToolError("internal_error", "The tool failed unexpectedly. "
                         "Reference %s." % ref, reference=ref).body()
        return None, json.dumps(body), True
    finally:
        user = getattr(caller, "user", None)
        current_app.logger.info(
            "mcp tool=%s user=%s tenant=%s ms=%d status=%s", name,
            getattr(user, "id", None), getattr(user, "tenant_id", None),
            int((time.monotonic() - started) * 1000), status)


def audit_event(caller, action, detail):
    """An admin audit-log row for a tool that ran, opened or saved a sizing
    (04-security.md §5): who and which id, never the numbers. Committed here
    because read tools have nothing else to commit."""
    from auth import audit
    from database import db
    audit("mcp." + action, detail, actor=caller.user)
    db.session.commit()


# Key fragments that mark commercial data (the same list tests/test_security
# asserts on). Belt and braces: the tools never put such keys in a result,
# and scrub() removes any that a stored record or a future serializer slips
# in, so pricing cannot leave the server through this path (§5).
PRICE_SHAPED = ("cost", "price", "tier", "eur", "usd", "msrp", "discount", "margin")


def scrub(node):
    if isinstance(node, dict):
        return {k: scrub(v) for k, v in node.items()
                if not any(w in str(k).lower() for w in PRICE_SHAPED)}
    if isinstance(node, list):
        return [scrub(v) for v in node]
    return node


def render_text(summary, structured):
    """The text block: the summary, then the serialized JSON (spec: a tool
    with structured content SHOULD also return it as text)."""
    data = json.dumps(structured, ensure_ascii=False, default=str)
    return (summary.rstrip() + "\n\n" + data) if summary else data
