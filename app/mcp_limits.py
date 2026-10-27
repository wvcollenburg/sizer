"""Per-account rate limits on the MCP endpoint (docs/mcp/04-security.md §4).

flask-limiter's decorators key on the client IP, which says little here:
every claude.ai user arrives from the same egress range, so one busy
connector would throttle everyone else. These limits are counted per account
after the bearer token has been checked, in the limiter's own storage (Redis
when RATELIMIT_STORAGE_URI points there, per-worker memory otherwise), so
they share the limiter's on/off switch and its outage behaviour.

Buckets:
  calls    every tools/call, whatever the tool
  uploads  start_upload and merge_uploads: each one is a browser page and a
           parsed customer file kept for the retention window
  sizings  create_sizing, validate_inputs and check_bom_rows: the engine runs
"""
import os
import time

from limits import parse

from extensions import limiter


def _items(env, default):
    return tuple(parse(s.strip()) for s in (os.environ.get(env) or default).split(";") if s.strip())


CALLS = _items("MCP_LIMIT_CALLS", "120 per minute; 3000 per hour")
UPLOADS = _items("MCP_LIMIT_UPLOADS", "30 per hour")
SIZINGS = _items("MCP_LIMIT_SIZINGS", "60 per minute")

BUCKETS = {
    "start_upload": UPLOADS, "merge_uploads": UPLOADS,
    "create_sizing": SIZINGS, "validate_inputs": SIZINGS, "check_bom_rows": SIZINGS,
}


def retry_after(user, tool_name):
    """Count one call by ``user`` to ``tool_name`` against its buckets.
    Returns 0 when the call may go ahead, else the number of seconds the
    client should wait (the ``Retry-After`` value). A limiter that is
    switched off, or whose storage is unreachable, never refuses."""
    if not limiter.enabled:
        return 0
    try:
        strategy = limiter.limiter
    except Exception:       # noqa: BLE001 — not initialised (no app)
        return 0
    key = str(getattr(user, "id", "anon"))
    wait = 0
    try:
        for bucket, items in (("calls", CALLS), (tool_name, BUCKETS.get(tool_name, ()))):
            for item in items:
                if not strategy.hit(item, "mcp", bucket, key):
                    stats = strategy.get_window_stats(item, "mcp", bucket, key)
                    wait = max(wait, int(stats.reset_time - time.time()) + 1)
    except Exception as exc:    # noqa: BLE001 — storage trouble must not block callers
        from flask import current_app
        current_app.logger.warning("mcp rate limit storage unavailable: %s", exc)
        return 0
    return wait
