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
# Storage defaults to in-memory, which is per-gunicorn-worker: with N workers a
# limit is effectively enforced up to N times before biting. That's acceptable
# for slowing brute-force / email-bombing here; point RATELIMIT_STORAGE_URI at a
# shared backend (e.g. redis://...) to make limits exact across workers.
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.environ.get("RATELIMIT_STORAGE_URI", "memory://"),
)
