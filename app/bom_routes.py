"""BOM checker HTTP surface — docs/bom-checker-build.md §6 (user routes) and
the super-admin review queue.

Placement follows the project-level-exports rule: a check belongs to a
project, is run from the project page, and optionally compares against one of
that project's sizings. Visibility and write rights are the project's
(projects._visible_project / Project.can_edit), so a partner sees their own
checks, a Scale user sees the ones on projects shared with them, and nobody
else sees anything.

The uploaded file is parsed synchronously (deterministic parsers are fast)
and then discarded; only its digest, the normalised BOM and the result are
stored. A file no parser recognises (or a PDF / picture) is queued for the
Claude agent instead (bom/agent_ingest.py, bom/agent_worker.py): the route
answers 202 with the job, the upload bytes live on the job row until the
worker is done, and the result is an ordinary check marked AGENT_FORMAT.
Without an API key such files are refused with the blank template offered,
as before.
"""
import hashlib
import io
import os
import tempfile
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, send_file

from admin_routes import require_super_admin
from auth import audit, current_user, login_required
from auth_models import Configuration, _utcnow
from bom import agent_ingest
from bom.check import run_check
from bom.normalize import NormalizedBOM
# What the deterministic parsers read; with the agent configured, every
# agent_ingest.AGENT_EXTENSIONS type is accepted too (see accepted_extensions).
from bom.parsers import ACCEPTED_EXTENSIONS as PARSER_EXTENSIONS
from bom_models import (AGENT_ALWAYS_REVIEW_SETTING, AGENT_FORMAT,
                        AGENT_GLOBAL_CAP_SETTING, AGENT_TENANT_CAP_SETTING,
                        BomAgentJob, BomCheck, BomRejectedFile, JOB_FAILED,
                        JOB_QUEUED, JOB_RUNNING, REVIEW_CONFIRMED,
                        REVIEW_INCORRECT, REVIEW_NONE, REVIEW_OPEN)
from database import db
from extensions import limiter
from projects import _owned_project_or_error, _visible_project

bom_bp = Blueprint("bom", __name__, url_prefix="/api/bom")
bom_project_bp = Blueprint("bom_project", __name__, url_prefix="/api/projects")
bom_checks_bp = Blueprint("bom_checks", __name__, url_prefix="/api/bom-checks")
bom_jobs_bp = Blueprint("bom_agent_jobs", __name__, url_prefix="/api/bom-agent-jobs")
bom_admin_bp = Blueprint("bom_admin", __name__, url_prefix="/admin/api/bom-reviews")
bom_admin_bp.before_request(require_super_admin)
bom_reject_admin_bp = Blueprint("bom_reject_admin", __name__,
                                url_prefix="/admin/api/bom-rejects")
bom_reject_admin_bp.before_request(require_super_admin)
bom_notes_admin_bp = Blueprint("bom_notes_admin", __name__,
                               url_prefix="/admin/api/bom-part-notes")
bom_notes_admin_bp.before_request(require_super_admin)
# Machine-to-machine pull feed for the HCL team (no user accounts on their
# side): deliberately NOT behind login — auth.require_login exempts exactly
# this path, and the route enforces its own bearer token instead.
hcl_feed_bp = Blueprint("hcl_feed", __name__, url_prefix="/api/hcl")

MAX_BOM_BYTES = 10 * 1024 * 1024
# Agent jobs per user per hour, on top of the route's request limit: each
# one is a paid model call.
AGENT_JOBS_PER_USER_HOUR = 20
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def register_bom(app):
    app.register_blueprint(bom_bp)
    app.register_blueprint(bom_project_bp)
    app.register_blueprint(bom_checks_bp)
    app.register_blueprint(bom_jobs_bp)
    app.register_blueprint(bom_admin_bp)
    app.register_blueprint(bom_reject_admin_bp)
    app.register_blueprint(bom_notes_admin_bp)
    app.register_blueprint(hcl_feed_bp)


# ── helpers ──────────────────────────────────────────────────────────────────

def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def accepted_extensions():
    return agent_ingest.AGENT_EXTENSIONS if agent_ingest.available() else PARSER_EXTENSIONS


def _save_upload(f, allowed):
    """Validate + spool the upload to a temp file. Returns (path, ext) or
    raises ValueError with a user message. Caller unlinks the path. The
    bytes must match the extension (agent_ingest.sniff: ZIP for xlsx/docx,
    OLE for xls, %PDF); pictures are decoded later, by the agent path."""
    name = f.filename or ""
    ext = os.path.splitext(name)[1].lower()
    if ext not in allowed:
        raise ValueError("Unsupported file type %s. Accepted: %s" % (ext or "(none)", ", ".join(allowed)))
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        f.save(tmp.name)
    finally:
        tmp.close()
    try:
        agent_ingest.sniff(tmp.name, name)
    except agent_ingest.AgentError as exc:
        os.unlink(tmp.name)
        raise ValueError(str(exc))
    return tmp.name, ext


def _sizing_for(project, sizing_id):
    """(sizing, error) — a sizing of this project or None when not requested."""
    if sizing_id in (None, "", "null", "none", 0, "0"):
        return None, None
    try:
        sid = int(sizing_id)
    except (TypeError, ValueError):
        return None, (jsonify({"error": "Invalid sizing id"}), 400)
    sizing = db.session.get(Configuration, sid)
    if sizing is None or sizing.is_deleted or sizing.project_id != project.id:
        return None, (jsonify({"error": "Sizing not found in this project"}), 400)
    return sizing, None


def _visible_check(check_id, user):
    check = db.session.get(BomCheck, check_id)
    if check is None or check.is_deleted:
        return None, None
    project, source = _visible_project(check.project_id, user)
    if project is None:
        return None, None
    return check, project


def _can_write(check, project, user):
    return user.is_super_admin or check.user_id == user.id or project.can_edit(user)


# ── capabilities / template ──────────────────────────────────────────────────

@bom_bp.route("/capabilities", methods=["GET"])
@login_required
def capabilities():
    from bom.parsers import FORMAT_LABELS
    caps = agent_ingest.capabilities()
    caps.update({
        "accepted_extensions": list(accepted_extensions()),
        "parser_extensions": list(PARSER_EXTENSIONS),
        "max_bytes": MAX_BOM_BYTES,
        "formats": FORMAT_LABELS,
        "template_url": "/api/bom/template",
    })
    from auth import smtp_configured
    # The "email me when done" option only shows when mail can be sent.
    caps["notify_available"] = bool(caps["agent_available"] and smtp_configured())
    try:
        from bom.hcl_data import catalog_counts
        from auth import get_setting
        counts = catalog_counts()
        caps["catalog"] = {
            "platforms": (counts.get("platforms") or {}).get("active", 0),
            "components": (counts.get("components") or {}).get("active", 0),
            "last_scrape_at": get_setting("hcl_last_scrape_at"),
        }
    except Exception:
        caps["catalog"] = {"platforms": 0, "components": 0, "last_scrape_at": None}
    return jsonify(caps)


@bom_bp.route("/template", methods=["GET"])
@login_required
def template_download():
    from bom.parsers.template import build_template_bytes
    lang = request.args.get("lang") or "en"
    data = build_template_bytes(lang=lang)
    return send_file(io.BytesIO(data), as_attachment=True,
                     download_name="sc-bom-template.xlsx", mimetype=XLSX_MIME)


def _template_name(filename):
    stem = os.path.splitext(os.path.basename(filename or "bom"))[0][:60] or "bom"
    stem = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in stem)
    return "%s-prefilled-template.xlsx" % stem


# ── checks on a project ──────────────────────────────────────────────────────

@bom_project_bp.route("/<int:project_id>/bom-checks", methods=["GET"])
@login_required
def list_checks(project_id):
    user = current_user()
    project, source = _visible_project(project_id, user)
    if project is None:
        return jsonify({"error": "Project not found"}), 404
    rows = (BomCheck.query.filter_by(project_id=project.id, is_deleted=False)
            .order_by(BomCheck.created_at.desc()).all())
    return jsonify([c.to_dict() for c in rows])


def _parse_upload(path, filename):
    """(bom, fmt, None) from a deterministic parser, or (None, None, reply)
    when the upload must be refused outright, or (None, None, None) when no
    parser could read it and it is a case for the agent."""
    from bom.parsers import UnrecognizedFormat, parse_file
    from bom.parsers.template import TemplateError
    from xlsx_utils import SheetTooLargeError
    if os.path.splitext(filename or "")[1].lower() not in PARSER_EXTENSIONS:
        return None, None, None                  # PDF, picture, docx: agent only
    try:
        bom, fmt = parse_file(path, filename)
        return bom, fmt, None
    except TemplateError as exc:
        # Our own template, filled in by hand: its errors are the user's to
        # fix, never something to hand to the agent.
        return None, None, (jsonify({
            "error": "The template has errors - fix the rows below and upload again.",
            "details": list(exc.errors)}), 400)
    except SheetTooLargeError as exc:
        # Oversized sheet or a zip decompression bomb: a clear refusal.
        return None, None, (jsonify({"error": str(exc), "details": []}), 400)
    except UnrecognizedFormat:
        return None, None, None
    except Exception as exc:                     # a parser crash: let the agent try
        from flask import current_app
        current_app.logger.warning("BOM parse failed, trying the agent: %s", exc)
        return None, None, None


def _refuse_unrecognised():
    return jsonify({"error": "This file is not a BOM format the checker recognises.",
                    "hint": "Download the blank template, fill it in and upload that instead.",
                    "retainable": True, "details": []}), 400


def _cap(setting):
    from auth import get_setting
    try:
        return max(0, int(get_setting(setting, "0") or 0))
    except (TypeError, ValueError):
        return 0


def _agent_budget_error(user):
    """A 429 reply when a limit is reached, else None. Caps count jobs per
    UTC day; 0 means unlimited (the testing default)."""
    from datetime import timedelta
    now = _utcnow()
    hour_ago = now - timedelta(hours=1)
    recent = BomAgentJob.query.filter(BomAgentJob.user_id == user.id,
                                      BomAgentJob.created_at >= hour_ago).count()
    if recent >= AGENT_JOBS_PER_USER_HOUR:
        return jsonify({"error": "You have sent many unrecognised files in the last hour. "
                                 "Try again later, or fill in the blank template."}), 429
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tenant_cap = _cap(AGENT_TENANT_CAP_SETTING)
    if tenant_cap and BomAgentJob.query.filter(
            BomAgentJob.tenant_id == user.tenant_id,
            BomAgentJob.created_at >= day_start).count() >= tenant_cap:
        return jsonify({"error": "Your organisation reached today's limit for files read by "
                                 "the agent. Try again tomorrow, or fill in the blank template."}), 429
    global_cap = _cap(AGENT_GLOBAL_CAP_SETTING)
    if global_cap and BomAgentJob.query.filter(
            BomAgentJob.created_at >= day_start).count() >= global_cap:
        return jsonify({"error": "Today's limit for files read by the agent has been reached. "
                                 "Try again tomorrow, or fill in the blank template."}), 429
    return None


def _agent_path(project, user, sizing, name, filename, path, digest):
    """Queue the file for the agent (202), reuse an earlier agent reading of
    the same file (201), or refuse it."""
    from bom.agent_worker import store_agent_check
    if not agent_ingest.available():
        return _refuse_unrecognised()

    # The same file already read by the agent for this organisation: reuse
    # that reading, no second paid call (retries, another sizing, another
    # project of the same customer).
    earlier = (BomCheck.query.filter_by(file_sha256=digest, file_format=AGENT_FORMAT,
                                        tenant_id=user.tenant_id)
               .filter(BomCheck.ingest_meta.isnot(None))
               .order_by(BomCheck.created_at.desc()).first())
    if earlier is not None:
        meta = dict(earlier.ingest_meta or {})
        meta.update({"reused_from_check": earlier.id, "input_tokens": 0, "output_tokens": 0})
        check = store_agent_check(
            project_id=project.id, user=user, name=name, filename=filename, digest=digest,
            bom=NormalizedBOM.from_dict(earlier.normalized or {}), meta=meta, sizing=sizing)
        db.session.commit()
        return jsonify(check.to_dict(full=True)), 201

    pending = BomAgentJob.query.filter(
        BomAgentJob.project_id == project.id, BomAgentJob.file_sha256 == digest,
        BomAgentJob.status.in_((JOB_QUEUED, JOB_RUNNING))).first()
    if pending is not None:
        return jsonify({"job": pending.to_dict()}), 202

    refused = _agent_budget_error(user)
    if refused:
        return refused
    with open(path, "rb") as fh:
        content = fh.read()
    job = BomAgentJob(
        project_id=project.id, configuration_id=sizing.id if sizing else None,
        user_id=user.id, tenant_id=user.tenant_id, name=name,
        filename=(filename or "bom")[:200], file_sha256=digest, content=content,
        notify_email=(request.form.get("notify") or "") in ("1", "true", "on"),
        lang=(request.form.get("lang") or "en")[:5],
    )
    db.session.add(job)
    db.session.commit()
    return jsonify({"job": job.to_dict()}), 202


@bom_project_bp.route("/<int:project_id>/bom-checks", methods=["POST"])
@login_required
@limiter.limit("30 per hour")
def create_check(project_id):
    user = current_user()
    project, err = _owned_project_or_error(project_id, user)
    if err:
        return err
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    sizing, err = _sizing_for(project, request.form.get("sizing_id"))
    if err:
        return err
    try:
        path, ext = _save_upload(f, accepted_extensions())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    name = (request.form.get("name") or "").strip()[:200] or os.path.basename(f.filename or "BOM")[:200]
    try:
        digest = _sha256(path)
        bom, fmt, refusal = _parse_upload(path, f.filename)
        if refusal:
            return refusal
        if bom is None:
            return _agent_path(project, user, sizing, name, f.filename, path, digest)
        result = run_check(bom, sizing)
    except Exception as exc:
        from flask import current_app
        current_app.logger.warning("BOM check failed: %s", exc)
        return jsonify({"error": "Could not process the file. If it is a quote in another "
                                 "layout, download the template and fill it in.",
                        "retainable": True}), 400
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    check = BomCheck(
        project_id=project.id, user_id=user.id, tenant_id=user.tenant_id,
        name=name, filename=(f.filename or "")[:200], file_sha256=digest,
        file_format=fmt, vendor=bom.vendor, normalized=bom.to_dict(), history=[],
    )
    check.apply_result(result, sizing)
    check.history = [check.history_entry()]
    db.session.add(check)
    db.session.commit()
    return jsonify(check.to_dict(full=True)), 201


# ── agent jobs ───────────────────────────────────────────────────────────────

@bom_project_bp.route("/<int:project_id>/bom-agent-jobs", methods=["GET"])
@login_required
def list_agent_jobs(project_id):
    """Jobs still in the queue, plus failures of the last day (so a user who
    closed the modal still learns what happened)."""
    from datetime import timedelta
    user = current_user()
    project, source = _visible_project(project_id, user)
    if project is None:
        return jsonify({"error": "Project not found"}), 404
    since = _utcnow() - timedelta(days=1)
    rows = (BomAgentJob.query.filter(BomAgentJob.project_id == project.id)
            .filter(BomAgentJob.status.in_((JOB_QUEUED, JOB_RUNNING))
                    | ((BomAgentJob.status == JOB_FAILED) & (BomAgentJob.created_at >= since)))
            .order_by(BomAgentJob.created_at.desc()).limit(50).all())
    return jsonify([j.to_dict() for j in rows])


def _visible_job(job_id, user):
    job = db.session.get(BomAgentJob, job_id)
    if job is None:
        return None, None
    project, source = _visible_project(job.project_id, user)
    if project is None:
        return None, None
    return job, project


@bom_jobs_bp.route("/<int:job_id>", methods=["GET"])
@login_required
def get_agent_job(job_id):
    job, project = _visible_job(job_id, current_user())
    if job is None:
        return jsonify({"error": "Job not found"}), 404
    return jsonify(job.to_dict())


@bom_jobs_bp.route("/<int:job_id>/notify", methods=["POST"])
@login_required
def set_agent_job_notify(job_id):
    """Opt in to (or out of) the done-email while the job is waiting. Only
    the uploader: the mail goes to their own address."""
    user = current_user()
    job, project = _visible_job(job_id, user)
    if job is None or job.user_id != user.id:
        return jsonify({"error": "Job not found"}), 404
    job.notify_email = bool((request.get_json(silent=True) or {}).get("notify"))
    db.session.commit()
    return jsonify(job.to_dict())


@bom_jobs_bp.route("/<int:job_id>/template", methods=["GET"])
@login_required
def agent_job_template(job_id):
    """The agent's pre-filled template of a job whose reading failed the
    template parser, so the user can fix the rows and upload it."""
    job, project = _visible_job(job_id, current_user())
    if job is None or job.template is None:
        return jsonify({"error": "Template not found"}), 404
    return send_file(io.BytesIO(job.template), as_attachment=True,
                     download_name=_template_name(job.filename), mimetype=XLSX_MIME)


@bom_checks_bp.route("/<int:check_id>/template", methods=["GET"])
@login_required
def check_template(check_id):
    """The check's BOM as a filled template, regenerated from the stored
    normalised BOM (nothing extra is kept). Offered on agent-read checks:
    when the agent misread a line, fix it here and upload the template."""
    from bom.parsers.template import build_template_bytes
    check, project = _visible_check(check_id, current_user())
    if check is None:
        return jsonify({"error": "BOM check not found"}), 404
    data = build_template_bytes(bom=NormalizedBOM.from_dict(check.normalized or {}),
                                lang=request.args.get("lang") or "en")
    return send_file(io.BytesIO(data), as_attachment=True,
                     download_name=_template_name(check.filename or check.name), mimetype=XLSX_MIME)


@bom_checks_bp.route("/<int:check_id>", methods=["GET"])
@login_required
def get_check(check_id):
    check, project = _visible_check(check_id, current_user())
    if check is None:
        return jsonify({"error": "BOM check not found"}), 404
    return jsonify(check.to_dict(full=True))


@bom_checks_bp.route("/<int:check_id>", methods=["DELETE"])
@login_required
def delete_check(check_id):
    user = current_user()
    check, project = _visible_check(check_id, user)
    if check is None:
        return jsonify({"error": "BOM check not found"}), 404
    if not _can_write(check, project, user):
        return jsonify({"error": "You cannot delete this BOM check"}), 403
    check.is_deleted = True
    check.deleted_at = _utcnow()
    db.session.commit()
    return jsonify({"message": "BOM check deleted"})


@bom_checks_bp.route("/<int:check_id>/recheck", methods=["POST"])
@login_required
@limiter.limit("60 per hour")
def recheck(check_id):
    user = current_user()
    check, project = _visible_check(check_id, user)
    if check is None:
        return jsonify({"error": "BOM check not found"}), 404
    if not _can_write(check, project, user):
        return jsonify({"error": "You cannot re-check this BOM"}), 403
    data = request.get_json(silent=True) or {}
    if "sizing_id" in data:
        sizing, err = _sizing_for(project, data.get("sizing_id"))
        if err:
            return err
    else:
        # Reusing the stored sizing: it must still be a live member of THIS
        # project — sizings can be moved between projects (move_sizing), and
        # the explicit sizing_id path above enforces membership via
        # _sizing_for, so the implicit path must not bypass that rule.
        sizing = check.configuration
        if sizing is not None and (sizing.is_deleted or sizing.project_id != project.id):
            sizing = None
    bom = NormalizedBOM.from_dict(check.normalized or {})
    result = run_check(bom, sizing)
    check.apply_result(result, sizing)
    history = list(check.history or [])
    history.append(check.history_entry())
    check.history = history[-20:]
    db.session.commit()
    return jsonify(check.to_dict(full=True))


# ── super-admin review queue ─────────────────────────────────────────────────

@bom_admin_bp.route("", methods=["GET"])
def list_reviews():
    status = request.args.get("status", "open")
    q = BomCheck.query.filter_by(is_deleted=False)
    if status == "open":
        q = q.filter(BomCheck.review_status == REVIEW_OPEN)
    elif status in (REVIEW_CONFIRMED, REVIEW_INCORRECT):
        q = q.filter(BomCheck.review_status == status)
    elif status != "all":
        q = q.filter(BomCheck.review_status != REVIEW_NONE)
    rows = q.order_by(BomCheck.created_at.desc()).limit(500).all()
    out = []
    for c in rows:
        d = c.to_dict()
        d["project"] = ({"id": c.project.id, "name": c.project.name, "code": c.project.code}
                        if c.project else None)
        d["tenant_domain"] = c.user.tenant.domain if (c.user and c.user.tenant) else None
        out.append(d)
    return jsonify(out)


@bom_admin_bp.route("/agent-settings", methods=["GET"])
def get_agent_settings():
    from auth import get_setting
    return jsonify({
        "available": agent_ingest.available(),
        "model": agent_ingest.model_name() if agent_ingest.available() else None,
        "always_review": (get_setting(AGENT_ALWAYS_REVIEW_SETTING, "1") or "1") != "0",
        "tenant_daily_cap": _cap(AGENT_TENANT_CAP_SETTING),
        "global_daily_cap": _cap(AGENT_GLOBAL_CAP_SETTING),
        "user_hourly_limit": AGENT_JOBS_PER_USER_HOUR,
    })


@bom_admin_bp.route("/agent-settings", methods=["PUT"])
def put_agent_settings():
    """Caps are agent jobs per UTC day; 0 = unlimited."""
    from auth import set_setting
    data = request.get_json(silent=True) or {}
    changes = []
    for key, setting in (("tenant_daily_cap", AGENT_TENANT_CAP_SETTING),
                         ("global_daily_cap", AGENT_GLOBAL_CAP_SETTING)):
        if key in data:
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100000:
                return jsonify({"error": "%s must be a whole number from 0 (unlimited) to 100000" % key}), 400
            set_setting(setting, str(value))
            changes.append("%s=%d" % (key, value))
    if "always_review" in data:
        on = bool(data["always_review"])
        set_setting(AGENT_ALWAYS_REVIEW_SETTING, "1" if on else "0")
        changes.append("always_review=%s" % ("on" if on else "off"))
    if not changes:
        return jsonify({"error": "Nothing to change"}), 400
    audit("bom_agent_settings", ", ".join(changes))
    db.session.commit()
    return get_agent_settings()


@bom_admin_bp.route("/agent-usage", methods=["GET"])
def agent_usage():
    """Agent jobs of the last 30 days per UTC day and organisation: counts by
    outcome and the tokens billed (cache reuses bill nothing and are not
    jobs). Aggregated in Python: a few thousand rows at most."""
    from datetime import timedelta
    from auth_models import Tenant
    since = _utcnow() - timedelta(days=30)
    rows = BomAgentJob.query.filter(BomAgentJob.created_at >= since).all()
    domains = {t.id: t.domain for t in Tenant.query.filter(
        Tenant.id.in_({r.tenant_id for r in rows} or {-1})).all()}
    buckets = {}
    for job in rows:
        key = (job.created_at.date().isoformat(), job.tenant_id)
        b = buckets.setdefault(key, {"day": key[0], "tenant": domains.get(job.tenant_id),
                                     "jobs": 0, "done": 0, "failed": 0, "pending": 0,
                                     "input_tokens": 0, "output_tokens": 0})
        b["jobs"] += 1
        if job.status == JOB_FAILED:
            b["failed"] += 1
        elif job.status in (JOB_QUEUED, JOB_RUNNING):
            b["pending"] += 1
        else:
            b["done"] += 1
        meta = job.meta or {}
        b["input_tokens"] += int(meta.get("input_tokens") or 0)
        b["output_tokens"] += int(meta.get("output_tokens") or 0)
    out = sorted(buckets.values(), key=lambda b: (b["day"], b["tenant"] or ""), reverse=True)
    return jsonify(out)


@bom_admin_bp.route("/<int:check_id>", methods=["GET"])
def get_review(check_id):
    check = db.session.get(BomCheck, check_id)
    if check is None or check.is_deleted:
        return jsonify({"error": "BOM check not found"}), 404
    d = check.to_dict(full=True)
    d["project"] = ({"id": check.project.id, "name": check.project.name, "code": check.project.code}
                    if check.project else None)
    return jsonify(d)


@bom_admin_bp.route("/<int:check_id>", methods=["POST"])
def annotate_review(check_id):
    check = db.session.get(BomCheck, check_id)
    if check is None or check.is_deleted:
        return jsonify({"error": "BOM check not found"}), 404
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip().lower()
    if status not in (REVIEW_OPEN, REVIEW_CONFIRMED, REVIEW_INCORRECT):
        return jsonify({"error": "status must be open, confirmed or incorrect"}), 400
    note = (data.get("note") or "").strip()[:4000] or None
    user = current_user()
    check.review_status = status
    check.review_note = note
    check.reviewed_by_user_id = user.id if user else None
    check.reviewed_at = _utcnow()
    audit("bom_review", "check #%d (%s) -> %s%s" % (
        check.id, check.name, status, (": " + note[:120]) if note else ""))
    db.session.commit()
    return jsonify({"message": "Review saved", "review": check.to_dict()})


@bom_admin_bp.route("/<int:check_id>/acceptable", methods=["GET"])
def acceptable_parts(check_id):
    """Candidates a super admin could accept ahead of HCL publication —
    findings whose parts the HCL team may already have verified."""
    check = db.session.get(BomCheck, check_id)
    if check is None or check.is_deleted:
        return jsonify({"error": "BOM check not found"}), 404
    from bom import preview
    return jsonify(preview.candidates_for(check))


@bom_admin_bp.route("/<int:check_id>/accept-parts", methods=["POST"])
def accept_parts(check_id):
    check = db.session.get(BomCheck, check_id)
    if check is None or check.is_deleted:
        return jsonify({"error": "BOM check not found"}), 404
    data = request.get_json(silent=True) or {}
    keys = data.get("keys")
    if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
        return jsonify({"error": "keys must be a non-empty list of candidate keys"}), 400
    note = (data.get("note") or "").strip()[:2000] or None
    # Optional {brand, sc_model, server}: create the platform as
    # pre-publication when the check identified none (validated in preview).
    platform_spec = data.get("platform")
    from bom import preview
    try:
        result = preview.accept_parts(check, keys, current_user(), note=note,
                                      platform_spec=platform_spec)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    audit("hcl_preview_accept", "check #%d (%s): %s" % (
        check.id, check.name, ", ".join(keys)[:150]))
    db.session.commit()
    return jsonify(result)


# ── preview pull feed (HCL team, machine-to-machine) ─────────────────────────

@hcl_feed_bp.route("/preview-feed", methods=["GET"])
@limiter.limit("60 per hour")
def preview_feed():
    """The accepted-but-unpublished parts, pulled by the HCL team.

    No session, no account: the caller authenticates with the bearer token a
    super admin configured in the HCL settings (``?token=`` accepted too, for
    curl-friendliness). No token configured means the feature is off — answer
    404 so the endpoint's existence is not revealed. Nothing but the preview
    set is ever exposed here.
    """
    import hmac
    from auth import get_setting
    from bom import preview
    token = (get_setting(preview.PREVIEW_FEED_TOKEN_SETTING, "") or "").strip()
    if not token:
        return jsonify({"error": "Not found"}), 404
    header = request.headers.get("Authorization") or ""
    presented = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not presented:
        presented = (request.args.get("token") or "").strip()
    if not presented or not hmac.compare_digest(presented, token):
        return jsonify({"error": "Invalid token"}), 401
    return jsonify(preview.preview_feed())


# ── rejected-file retention (consent-based) ──────────────────────────────────
# The check route never keeps the upload. When a file is refused the client
# offers to share it; only an explicit second POST — the consent — lands here.
# The owner then downloads it over HTTP to teach the checker the format.

@bom_project_bp.route("/<int:project_id>/bom-rejects", methods=["POST"])
@login_required
@limiter.limit("10 per hour")
def retain_rejected(project_id):
    user = current_user()
    project, err = _owned_project_or_error(project_id, user)
    if err:
        return err
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    try:
        path, ext = _save_upload(f, accepted_extensions())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        digest = _sha256(path)
        with open(path, "rb") as fh:
            content = fh.read()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    existing = BomRejectedFile.query.filter_by(
        file_sha256=digest, project_id=project.id).first()
    if existing is not None:
        # The same file offered twice (a retried upload): one copy is enough.
        return jsonify(existing.to_dict()), 200
    row = BomRejectedFile(
        user_id=user.id, tenant_id=user.tenant_id, project_id=project.id,
        filename=(f.filename or "bom")[:200], file_sha256=digest,
        size_bytes=len(content), content=content,
        error=(request.form.get("error") or "").strip()[:2000] or None,
        note=(request.form.get("note") or "").strip()[:2000] or None,
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(row.to_dict()), 201


@bom_reject_admin_bp.route("", methods=["GET"])
def list_rejects():
    rows = (BomRejectedFile.query
            .order_by(BomRejectedFile.created_at.desc()).limit(200).all())
    return jsonify([r.to_dict() for r in rows])


@bom_reject_admin_bp.route("/<int:reject_id>/file", methods=["GET"])
def download_reject(reject_id):
    row = db.session.get(BomRejectedFile, reject_id)
    if row is None:
        return jsonify({"error": "File not found"}), 404
    import mimetypes
    safe = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in row.filename) or "bom"
    mime = XLSX_MIME if safe.lower().endswith(".xlsx") else (
        mimetypes.guess_type(safe)[0] or "application/octet-stream")
    return send_file(io.BytesIO(row.content), as_attachment=True,
                     download_name=safe, mimetype=mime)


@bom_reject_admin_bp.route("/<int:reject_id>", methods=["DELETE"])
def delete_reject(reject_id):
    row = db.session.get(BomRejectedFile, reject_id)
    if row is None:
        return jsonify({"error": "File not found"}), 404
    audit("bom_reject_delete", "rejected file #%d (%s)" % (row.id, row.filename))
    db.session.delete(row)
    db.session.commit()
    return jsonify({"message": "File deleted"})


# ── part notes (super admin) ─────────────────────────────────────────────────
# A reviewer's standing verdict on one specific part; applied to every later
# check by bom/part_notes.py. Existing checks pick a note up on re-check.

def _note_from_payload(data, note=None):
    """(note, error) — validated fields copied onto ``note`` (new when None)."""
    from bom_models import (BomPartNote, NOTE_MATCH_CONTAINS, NOTE_MATCH_EXACT,
                            NOTE_MATCH_MODES, NOTE_SEVERITIES)

    def text(key, limit):
        value = (data.get(key) or "").strip()
        return value[:limit] or None

    mode = (data.get("match_mode") or NOTE_MATCH_EXACT).strip().lower()
    if mode not in NOTE_MATCH_MODES:
        return None, "Unknown match mode"
    severity = (data.get("severity") or "warning").strip().lower()
    if severity not in NOTE_SEVERITIES:
        return None, "Unknown severity"
    issue = text("issue", 300)
    if not issue:
        return None, "A note needs a title"
    part_number, description, match_text = text("part_number", 80), text("description", 300), text("match_text", 200)
    if mode == NOTE_MATCH_EXACT and not (part_number or description):
        return None, "An exact note needs a part number or a description"
    if mode == NOTE_MATCH_CONTAINS and (not match_text or len(match_text) < 3):
        # A one- or two-letter text would hit half the BOM.
        return None, "A 'contains' note needs a match text of at least 3 characters"

    note = note or BomPartNote()
    note.match_mode = mode
    note.part_number = part_number if mode == NOTE_MATCH_EXACT else None
    note.description = description if mode == NOTE_MATCH_EXACT else None
    note.match_text = match_text if mode == NOTE_MATCH_CONTAINS else None
    note.category = text("category", 20)
    note.severity = severity
    note.issue = issue
    note.remediation = text("remediation", 2000)
    if "active" in data:
        note.active = bool(data.get("active"))
    return note, None


def _note_label(note):
    return note.part_number or note.description or ("contains '%s'" % note.match_text)


@bom_notes_admin_bp.route("", methods=["GET"])
def list_part_notes():
    from bom_models import BomPartNote
    rows = BomPartNote.query.order_by(BomPartNote.updated_at.desc(), BomPartNote.id.desc()).all()
    return jsonify([n.to_dict() for n in rows])


@bom_notes_admin_bp.route("", methods=["POST"])
def create_part_note():
    data = request.json or {}
    note, err = _note_from_payload(data)
    if err:
        return jsonify({"error": err}), 400
    user = current_user()
    note.created_by_user_id = user.id if user else None
    check_id = data.get("source_check_id")
    if check_id and db.session.get(BomCheck, int(check_id)) is not None:
        note.source_check_id = int(check_id)
    db.session.add(note)
    db.session.flush()
    audit("bom_part_note", "created note #%d (%s, %s): %s"
          % (note.id, _note_label(note), note.severity, note.issue))
    db.session.commit()
    return jsonify(note.to_dict()), 201


@bom_notes_admin_bp.route("/<int:note_id>", methods=["PUT"])
def update_part_note(note_id):
    from bom_models import BomPartNote
    note = db.session.get(BomPartNote, note_id)
    if note is None:
        return jsonify({"error": "Note not found"}), 404
    note, err = _note_from_payload(request.json or {}, note)
    if err:
        return jsonify({"error": err}), 400
    audit("bom_part_note", "updated note #%d (%s, %s, %s)"
          % (note.id, _note_label(note), note.severity, "active" if note.active else "disabled"))
    db.session.commit()
    return jsonify(note.to_dict())


@bom_notes_admin_bp.route("/<int:note_id>", methods=["DELETE"])
def delete_part_note(note_id):
    from bom_models import BomPartNote
    note = db.session.get(BomPartNote, note_id)
    if note is None:
        return jsonify({"error": "Note not found"}), 404
    audit("bom_part_note", "deleted note #%d (%s)" % (note.id, _note_label(note)))
    db.session.delete(note)
    db.session.commit()
    return jsonify({"message": "Note deleted"})
