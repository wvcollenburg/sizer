"""HCL catalog admin API — scrape trigger, approval queue, catalog browsing,
blocked vendors, snapshot import (docs/bom-checker-build.md §6).

Super-admin only: the whole blueprint sits behind ``admin_routes.
require_super_admin`` (the same gate as the models/catalog area) and lives
under ``/admin/api/hcl`` so that gate answers JSON 403, not a redirect.

Why the scrape is a per-request daemon thread rather than a queued job:
the plan decided "admin button only, no schedule", which keeps this the
sole background activity besides the export worker and avoids the
real-scheduler migration a second scheduled job would trigger. The run row
is the lock — a run still 'running' blocks another; one older than 2 hours
(longer than a scrape's worst case with every page timing out and retried)
is assumed dead (a gunicorn restart mid-scrape) and marked failed so the
button never wedges shut. A timed-out run stays failed for good: its
thread, if still alive, notices and drops its snapshot instead of
resurrecting the row and racing a newer scrape. The thread never raises:
every failure lands on the run row where the UI can show it.

``run_scrape`` is the thread body and is public so a test can call it
synchronously with a fake fetch serving the HTML fixtures.

``/import-snapshot`` is the no-network seeding path: a JSON snapshot
produced elsewhere by ``hcl_scrape.scrape_all`` goes through exactly the
same diff/queue as a live scrape. Day-to-day ops must work over HTTP.
"""
import json
import os
import threading
import traceback
from datetime import timedelta

from flask import Blueprint, current_app, jsonify, request

from admin_routes import require_super_admin
from auth import _aware, audit, current_user
from auth_models import _utcnow
from bom import hcl_data, hcl_scrape, hcl_sync
from database import db
from extensions import limiter
from hcl_models import (
    HclComponent, HclDevice, HclPendingChange, HclPlatform, HclScrapeRun,
    PENDING, RUN_FAILED, RUN_QUEUED, RUN_RUNNING, STATUS_ACTIVE,
    STATUS_DELISTED,
)

hcl_admin_bp = Blueprint("hcl_admin", __name__, url_prefix="/admin/api/hcl")
hcl_admin_bp.before_request(require_super_admin)

# Worst case for a scrape with an unresponsive site is ~64 pages x ~2 min of
# retries+timeouts; the stale window must sit above that or a "timed out" run
# is still alive and would overlap the next one.
RUN_STALE_AFTER = timedelta(hours=2)
IMPORT_MAX_BYTES = 8 * 1024 * 1024
PENDING_DEFAULT_LIMIT = 500
PENDING_MAX_LIMIT = 2000
COMPONENT_MAX_LIMIT = 1000


def _error(message, code=400):
    return jsonify({"error": message}), code


def _int_field(value, cap=100000):
    """Snapshot page counters are admin-supplied JSON: coerce, clamp into
    [0, cap], and return None (-> a JSON 400, not an HTML 500) for
    non-numeric junk."""
    try:
        n = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    return max(0, min(n, cap))


# ── scrape run lifecycle ──────────────────────────────────────────────────────

def _live_running_run():
    """The run that is really still running, or None. Runs stuck in
    'running' past the stale window are marked failed on the way."""
    now = _utcnow()
    live = None
    for run in HclScrapeRun.query.filter(
            HclScrapeRun.status.in_((RUN_RUNNING, RUN_QUEUED))).order_by(HclScrapeRun.id).all():
        started = _aware(run.started_at) or now
        if now - started > RUN_STALE_AFTER:
            run.status = RUN_FAILED
            run.error = "timed out"
            run.finished_at = now
        else:
            live = run
    db.session.commit()
    return live


def _last_run():
    return HclScrapeRun.query.order_by(HclScrapeRun.id.desc()).first()


def run_scrape(app, run_id, fetch=None, delay=None):
    """Thread body: scrape the site and queue the diff on run ``run_id``.

    ``fetch``/``delay`` are injection points for tests (fixture pages, no
    politeness pause). Never raises; a failure is written to the run.
    Returns the run id on success, None otherwise (the row itself belongs to
    the thread's own session, which is gone once the context pops).
    """
    with app.app_context():
        run = db.session.get(HclScrapeRun, run_id)
        if run is None:
            return None
        try:
            run.status = RUN_RUNNING
            run.started_at = _utcnow()
            db.session.commit()

            base_url = os.environ.get("HCL_BASE_URL", hcl_scrape.DEFAULT_BASE_URL)
            if fetch is None:
                def fetch(path):
                    return hcl_scrape.fetch_page(path, base_url=base_url)
            if delay is None:
                delay = float(os.environ.get("HCL_SCRAPE_DELAY", "0.25"))

            def progress(done, total, label):
                run.pages_done = done
                run.pages_total = total
                if done % 5 == 0 or done >= total:
                    db.session.commit()

            snapshot = hcl_scrape.scrape_all(fetch=fetch, progress=progress, delay=delay)
            # The scrape may have outlived the stale window: _live_running_run
            # then marked this run failed ('timed out') and possibly let a new
            # scrape start. That decision is final — building the run anyway
            # would flip a failed row back to succeeded and race the newer
            # scrape's record_changes. Drop the snapshot instead.
            db.session.expire(run)
            if run.status != RUN_RUNNING:
                app.logger.warning(
                    "HCL scrape run %s finished after being marked %s; discarding its snapshot",
                    run_id, run.status)
                return None
            hcl_sync.build_run(snapshot, user=None, source=run.source, run=run)
            return run.id
        except Exception as exc:  # noqa: BLE001 - recorded on the run, never raised
            db.session.rollback()
            try:
                run = db.session.get(HclScrapeRun, run_id)
                if run is not None:
                    run.status = RUN_FAILED
                    run.error = (str(exc) or exc.__class__.__name__)[:2000]
                    run.finished_at = _utcnow()
                    db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
            try:
                app.logger.warning("HCL scrape run %s failed: %s\n%s", run_id, exc,
                                   traceback.format_exc())
            except Exception:  # noqa: BLE001
                pass
    return None


def _start_scrape_thread(app, run_id):
    """Separate so tests can monkeypatch it to run inline."""
    thread = threading.Thread(target=run_scrape, args=(app, run_id), daemon=True,
                              name="hcl-scrape-%s" % run_id)
    thread.start()
    return thread


@hcl_admin_bp.route("/scrape", methods=["POST"])
@limiter.limit("5 per hour")
def start_scrape():
    live = _live_running_run()
    if live is not None:
        return _error("A scrape is already running (run %s)" % live.id, 409)
    user = current_user()
    run = HclScrapeRun(status=RUN_QUEUED, source="scrape",
                       triggered_by_user_id=getattr(user, "id", None))
    db.session.add(run)
    db.session.flush()
    audit("hcl_scrape_start", "run %s" % run.id)
    db.session.commit()
    _start_scrape_thread(current_app._get_current_object(), run.id)
    return jsonify({"run": run.to_dict()}), 202


@hcl_admin_bp.route("/scrape/status", methods=["GET"])
def scrape_status():
    live = _live_running_run()
    last = _last_run()
    return jsonify({"running": live is not None,
                    "last": last.to_dict() if last else None})


@hcl_admin_bp.route("/runs", methods=["GET"])
def list_runs():
    rows = HclScrapeRun.query.order_by(HclScrapeRun.id.desc()).limit(20).all()
    return jsonify([r.to_dict() for r in rows])


@hcl_admin_bp.route("/stats", methods=["GET"])
def stats():
    live = _live_running_run()
    last = _last_run()
    counts = hcl_data.catalog_counts()
    counts["pending"] = HclPendingChange.query.filter_by(status=PENDING).count()
    counts["last_run"] = last.to_dict() if last else None
    counts["running"] = live is not None
    counts["blocked_vendors"] = hcl_data.blocked_vendors()
    counts["catalog_stamp"] = hcl_sync.catalog_stamp()
    return jsonify(counts)


# ── pending changes ───────────────────────────────────────────────────────────

@hcl_admin_bp.route("/pending", methods=["GET"])
def list_pending():
    status = (request.args.get("status") or PENDING).strip().lower()
    q = HclPendingChange.query
    if status != "all":
        q = q.filter(HclPendingChange.status == status)
    kind = (request.args.get("kind") or "").strip().lower()
    if kind:
        q = q.filter(HclPendingChange.change_kind == kind)
    entity_type = (request.args.get("entity_type") or "").strip().lower()
    if entity_type:
        q = q.filter(HclPendingChange.entity_type == entity_type)
    run_id = request.args.get("run_id", type=int)
    if run_id:
        q = q.filter(HclPendingChange.run_id == run_id)
    limit = request.args.get("limit", type=int)
    limit = PENDING_DEFAULT_LIMIT if limit is None else max(1, min(limit, PENDING_MAX_LIMIT))
    rows = q.order_by(HclPendingChange.entity_type, HclPendingChange.entity_key,
                      HclPendingChange.field, HclPendingChange.id).limit(limit).all()
    return jsonify([r.to_dict() for r in rows])


def _note_from_request():
    data = request.get_json(silent=True)
    if isinstance(data, dict) and data.get("note") is not None:
        return str(data["note"])[:2000]
    return None


@hcl_admin_bp.route("/pending/<int:change_id>/approve", methods=["POST"])
def approve_change(change_id):
    change = db.session.get(HclPendingChange, change_id)
    if change is None:
        return _error("Change not found", 404)
    try:
        result = hcl_sync.approve(change, current_user(), note=_note_from_request())
    except hcl_sync.ChangeStateError as exc:
        return _error(str(exc), 409)
    audit("hcl_change_approve", "#%s %s" % (change.id, change.label or change.entity_key))
    db.session.commit()
    message = "Change applied" if result["applied"] else "Change approved (nothing to apply)"
    return jsonify({"message": message, "change": change.to_dict(),
                    "created": result.get("created", [])})


@hcl_admin_bp.route("/pending/<int:change_id>/reject", methods=["POST"])
def reject_change(change_id):
    change = db.session.get(HclPendingChange, change_id)
    if change is None:
        return _error("Change not found", 404)
    try:
        hcl_sync.reject(change, current_user(), note=_note_from_request())
    except hcl_sync.ChangeStateError as exc:
        return _error(str(exc), 409)
    audit("hcl_change_reject", "#%s %s" % (change.id, change.label or change.entity_key))
    db.session.commit()
    return jsonify({"message": "Change rejected", "change": change.to_dict()})


@hcl_admin_bp.route("/pending/bulk", methods=["POST"])
def bulk_changes():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return _error("JSON body required")
    action = (data.get("action") or "").strip().lower()
    if action not in ("approve", "reject"):
        return _error("action must be 'approve' or 'reject'")
    if data.get("all"):
        filters = data.get("filter") if isinstance(data.get("filter"), dict) else {}
        counts = hcl_sync.bulk(None, action, current_user(), all_pending=True, filters=filters)
        detail = "%s all pending %s" % (action, json.dumps(filters, sort_keys=True))
    else:
        ids = data.get("ids")
        if not isinstance(ids, list) or not ids:
            return _error("ids must be a non-empty list, or pass all: true")
        try:
            ids = [int(i) for i in ids]
        except (TypeError, ValueError):
            return _error("ids must be integers")
        counts = hcl_sync.bulk(ids, action, current_user())
        detail = "%s %d change(s)" % (action, len(ids))
    audit("hcl_bulk", "%s -> approved %d, rejected %d, skipped %d" % (
        detail, counts["approved"], counts["rejected"], counts["skipped"]))
    db.session.commit()
    return jsonify(counts)


# ── catalog browsing ──────────────────────────────────────────────────────────

def _status_filter(query, model):
    status = (request.args.get("status") or STATUS_ACTIVE).strip().lower()
    if status == "all":
        return query
    if status not in (STATUS_ACTIVE, STATUS_DELISTED):
        status = STATUS_ACTIVE
    return query.filter(model.status == status)


@hcl_admin_bp.route("/platforms", methods=["GET"])
def list_platforms():
    q = _status_filter(HclPlatform.query, HclPlatform)
    rows = q.order_by(HclPlatform.brand, HclPlatform.sc_model).all()
    return jsonify([p.to_dict() for p in rows])


@hcl_admin_bp.route("/platforms/<int:platform_id>", methods=["GET"])
def get_platform(platform_id):
    platform = db.session.get(HclPlatform, platform_id)
    if platform is None:
        return _error("Platform not found", 404)
    return jsonify(platform.to_dict(with_components=True))


@hcl_admin_bp.route("/components", methods=["GET"])
def list_components():
    q = _status_filter(HclComponent.query, HclComponent)
    kind = (request.args.get("kind") or "").strip().lower()
    if kind:
        q = q.filter(HclComponent.kind == kind)
    term = (request.args.get("q") or "").strip()
    if term:
        like = "%" + term.replace("%", "").replace("_", "") + "%"
        q = q.filter(db.or_(HclComponent.part_number.ilike(like),
                            HclComponent.description.ilike(like)))
    rows = q.order_by(HclComponent.kind, HclComponent.part_number).limit(COMPONENT_MAX_LIMIT).all()
    # platform_count feeds the catalog table's Platforms column; active links
    # only, so a delisted platform doesn't inflate the number.
    return jsonify([dict(c.to_dict(),
                         platform_count=sum(1 for l in c.links if l.status == STATUS_ACTIVE))
                    for c in rows])


@hcl_admin_bp.route("/devices", methods=["GET"])
def list_devices():
    q = _status_filter(HclDevice.query, HclDevice)
    rows = q.order_by(HclDevice.ven_id, HclDevice.dev_id).all()
    return jsonify([d.to_dict() for d in rows])


# ── settings ──────────────────────────────────────────────────────────────────

@hcl_admin_bp.route("/settings", methods=["GET"])
def get_settings():
    return jsonify({"blocked_vendors": hcl_data.blocked_vendors()})


@hcl_admin_bp.route("/settings", methods=["PUT"])
def put_settings():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or "blocked_vendors" not in data:
        return _error("blocked_vendors list required")
    vendors = data["blocked_vendors"]
    if not isinstance(vendors, list) or not all(isinstance(v, str) for v in vendors):
        return _error("blocked_vendors must be a list of strings")
    if any(len(v) > 40 for v in vendors):
        return _error("vendor names must be 40 characters or fewer")
    cleaned = hcl_data.set_blocked_vendors(vendors)
    audit("hcl_settings", "blocked_vendors = %s" % (", ".join(cleaned) or "(none)"))
    db.session.commit()
    return jsonify({"blocked_vendors": cleaned})


# ── snapshot import (no-network seeding) ──────────────────────────────────────

@hcl_admin_bp.route("/import-snapshot", methods=["POST"])
def import_snapshot():
    if "file" not in request.files:
        return _error("No file uploaded")
    f = request.files["file"]
    raw = f.stream.read(IMPORT_MAX_BYTES + 1)
    if len(raw) > IMPORT_MAX_BYTES:
        return _error("Snapshot file exceeds 8 MB")
    if not raw.strip():
        return _error("Snapshot file is empty")
    try:
        snapshot = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _error("Snapshot must be a JSON file produced by the HCL scraper")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("platforms"), list):
        return _error("Snapshot JSON must carry a 'platforms' list")
    if not isinstance(snapshot.get("devices", []), list):
        return _error("Snapshot 'devices' must be a list")
    snapshot.setdefault("devices", [])
    snapshot.setdefault("complete", False)
    pages_total = _int_field(snapshot.get("pages_total"))
    pages_done = _int_field(snapshot.get("pages_done"))
    if pages_total is None or pages_done is None:
        return _error("pages_total/pages_done must be a non-negative integer")
    snapshot["pages_total"] = pages_total
    snapshot["pages_done"] = pages_done

    user = current_user()
    run = HclScrapeRun(status=RUN_RUNNING, source="import",
                       triggered_by_user_id=getattr(user, "id", None),
                       pages_total=pages_total, pages_done=pages_done)
    db.session.add(run)
    db.session.commit()
    try:
        hcl_sync.build_run(snapshot, user=user, source="import", run=run)
    except Exception as exc:  # noqa: BLE001 - run row carries the failure
        current_app.logger.exception("HCL snapshot import failed")
        run = db.session.get(HclScrapeRun, run.id)
        return jsonify({"error": "Snapshot import failed: %s" % (str(exc)[:200]),
                        "run": run.to_dict() if run else None}), 400
    audit("hcl_import", "run %s from %s (%d platforms, %d devices, complete=%s)" % (
        run.id, f.filename or "upload", len(snapshot["platforms"]),
        len(snapshot["devices"]), bool(snapshot.get("complete"))))
    db.session.commit()
    return jsonify({"run": run.to_dict()}), 201
