"""Shared Flask extension singletons (imported by both app.py and the auth
blueprints, so they live here to avoid a circular import)."""
import os

from flask_limiter import Limiter
from flask_limiter.util import get_remote_address


# Per-client-IP rate limiter. There are no global default limits — only the
# abuse-prone auth/email endpoints opt in via @limiter.limit. The key is the
# real client IP, which requires ProxyFix (see app.create_app) so every request
# isn't bucketed under the reverse proxy's address.
#
# Storage: point RATELIMIT_STORAGE_URI at Redis (redis://redis:6379 in compose)
# so limits are exact and shared across gunicorn workers. Falls back to in-memory
# (per-worker) if unset.
#
# Resilience: swallow_errors + an in-memory fallback mean a Redis outage degrades
# to per-worker limiting (or open) rather than 500-ing every login — availability
# of auth wins over perfectly-exact limits during an outage.
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
    swallow_errors=True,
    in_memory_fallback_enabled=True,
)
