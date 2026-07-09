"""Admission gate for the CPU-heavy export endpoints.

Every export first BUILDS a document — python-docx / python-pptx authoring plus
cairosvg SVG→PNG rasterisation (network + replication diagrams at 2200px) and
PIL gauge rendering — which is heavy, CPU-bound Python work. The PDF variants
then add a headless LibreOffice subprocess on top. Under a burst, gunicorn would
otherwise run one build per worker thread (up to workers×threads of them) and
peg every core, starving light traffic (page loads, /api/calculate) — and load
testing showed exactly that: ~18 concurrent exports drove the box to 400% CPU
while a plain page load went from 60ms to ~13s.

The LibreOffice-only semaphore in export_docx doesn't help here: it guards just
the soffice subprocess, which runs AFTER the expensive build. This decorator
wraps the WHOLE handler, so the build phase is bounded too, and sheds excess
requests EARLY — before any building — with HTTP 503.

Concurrency is per-process (threading), so the effective global caps are
~gunicorn-workers × the values below. Defaults: 3 workers × (1 running + 3
queued) → ≤3 concurrent full exports and ≤12 in flight before shedding. Because
each export is largely CPU-bound, cores — not RAM — are what this protects;
keep the running cap near 1 per worker unless you add vCPUs. For an *exact*
global cap independent of worker count, move this to a Redis-backed semaphore
(the Redis used for rate limiting is already available).
"""
import os
import threading
from functools import wraps

from flask import jsonify

_MAX = max(1, int(os.environ.get("EXPORT_MAX_CONCURRENCY", "1")))
_QUEUE_MAX = max(0, int(os.environ.get("EXPORT_QUEUE_MAX", "3")))
# Bound the in-line wait so wait + work stays under the gunicorn worker timeout
# (180s); also stops one slow export from holding the whole queue hostage.
_ACQUIRE_TIMEOUT = 45

_sem = threading.BoundedSemaphore(_MAX)
_lock = threading.Lock()
_outstanding = 0  # running + waiting, this process

_BUSY_MSG = {"error": "The server is busy generating exports right now. "
                      "Please try again in a moment."}


def export_gate(fn):
    """Bound concurrent export work; shed early with 503 when the line is full."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        global _outstanding
        with _lock:
            if _outstanding >= _MAX + _QUEUE_MAX:
                return jsonify(_BUSY_MSG), 503
            _outstanding += 1
        try:
            if not _sem.acquire(timeout=_ACQUIRE_TIMEOUT):
                return jsonify(_BUSY_MSG), 503
            try:
                return fn(*args, **kwargs)
            finally:
                _sem.release()
        finally:
            with _lock:
                _outstanding -= 1
    return wrapper
