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
stored. Unrecognised files are refused with a hint towards the template —
they never fall through to a best-effort parse.
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
from bom import ai_prefill
from bom.check import run_check
from bom.normalize import NormalizedBOM
from bom_models import (BomCheck, BomRejectedFile, REVIEW_CONFIRMED,
                        REVIEW_INCORRECT, REVIEW_NONE, REVIEW_OPEN)
from database import db
from extensions import limiter
from projects import _owned_project_or_error, _visible_project

bom_bp = Blueprint("bom", __name__, url_prefix="/api/bom")
bom_project_bp = Blueprint("bom_project", __name__, url_prefix="/api/projects")
bom_checks_bp = Blueprint("bom_checks", __name__, url_prefix="/api/bom-checks")
bom_admin_bp = Blueprint("bom_admin", __name__, url_prefix="/admin/api/bom-reviews")
bom_admin_bp.before_request(require_super_admin)
bom_reject_admin_bp = Blueprint("bom_reject_admin", __name__,
                                url_prefix="/admin/api/bom-rejects")
bom_reject_admin_bp.before_request(require_super_admin)

MAX_BOM_BYTES = 10 * 1024 * 1024
ACCEPTED_EXTENSIONS = (".xlsx", ".csv")
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def register_bom(app):
    app.register_blueprint(bom_bp)
    app.register_blueprint(bom_project_bp)
    app.register_blueprint(bom_checks_bp)
    app.register_blueprint(bom_admin_bp)
    app.register_blueprint(bom_reject_admin_bp)


# ── helpers ──────────────────────────────────────────────────────────────────

def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _save_upload(f, allowed, magic_xlsx=True):
    """Validate + spool the upload to a temp file. Returns (path, ext) or
    raises ValueError with a user message. Caller unlinks the path."""
    name = f.filename or ""
    ext = os.path.splitext(name)[1].lower()
    if ext not in allowed:
        raise ValueError("Unsupported file type %s. Accepted: %s" % (ext or "(none)", ", ".join(allowed)))
    head = f.stream.read(4)
    f.stream.seek(0)
    if ext == ".xlsx" and magic_xlsx and head != b"PK\x03\x04":
        raise ValueError("File must be an .xlsx Excel file")
    if ext == ".pdf" and head != b"%PDF":
        raise ValueError("File must be a PDF")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
    try:
        f.save(tmp.name)
    finally:
        tmp.close()
    if os.path.getsize(tmp.name) > MAX_BOM_BYTES:
        os.unlink(tmp.name)
        raise ValueError("File too large (max 10 MB)")
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


def _apply_result(check, result, sizing):
    check.result = result
    check.configuration_id = sizing.id if sizing else None
    check.technical_verdict = (result.get("technical") or {}).get("verdict")
    fit = result.get("fit")
    check.fit_verdict = fit.get("verdict") if fit else None
    check.catalog_stamp = result.get("catalog_stamp")
    check.checked_at = _utcnow()
    flags = result.get("flag_reasons") or []
    current = check.review_status or REVIEW_NONE   # None before the first flush
    if flags and current == REVIEW_NONE:
        check.review_status = REVIEW_OPEN
    elif not flags and current == REVIEW_OPEN:
        check.review_status = REVIEW_NONE
    else:
        check.review_status = current


def _history_entry(check):
    return {
        "checked_at": check.checked_at.isoformat() if check.checked_at else None,
        "technical_verdict": check.technical_verdict,
        "fit_verdict": check.fit_verdict,
        "catalog_stamp": check.catalog_stamp,
        "sizing_id": check.configuration_id,
    }


# ── capabilities / template / pre-fill ───────────────────────────────────────

@bom_bp.route("/capabilities", methods=["GET"])
@login_required
def capabilities():
    from bom.parsers import FORMAT_LABELS
    caps = ai_prefill.capabilities()
    caps.update({
        "accepted_extensions": list(ACCEPTED_EXTENSIONS),
        "max_bytes": MAX_BOM_BYTES,
        "formats": FORMAT_LABELS,
        "template_url": "/api/bom/template",
    })
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


@bom_bp.route("/prefill", methods=["POST"])
@login_required
@limiter.limit("20 per hour")
def prefill():
    if not ai_prefill.available():
        return jsonify({"error": "AI pre-fill is not configured on this server. "
                                 "Download the blank template instead."}), 503
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    try:
        path, ext = _save_upload(f, ai_prefill.PREFILL_EXTENSIONS)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        data = ai_prefill.prefill_template(path, f.filename, lang=request.form.get("lang") or "en")
    except ai_prefill.PrefillUnavailable as exc:
        return jsonify({"error": str(exc)}), 503
    except ai_prefill.PrefillError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # SDK/network errors: user-safe message, detail in log
        from flask import current_app
        current_app.logger.warning("BOM pre-fill failed: %s", exc)
        return jsonify({"error": "The pre-fill service is unavailable right now. "
                                 "Try again later or fill the blank template."}), 502
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    stem = os.path.splitext(os.path.basename(f.filename or "bom"))[0][:60] or "bom"
    return send_file(io.BytesIO(data), as_attachment=True,
                     download_name="%s-prefilled-template.xlsx" % stem, mimetype=XLSX_MIME)


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


@bom_project_bp.route("/<int:project_id>/bom-checks", methods=["POST"])
@login_required
@limiter.limit("30 per hour")
def create_check(project_id):
    from bom.parsers import UnrecognizedFormat, parse_file
    from bom.parsers.template import TemplateError
    from xlsx_utils import SheetTooLargeError
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
        path, ext = _save_upload(f, ACCEPTED_EXTENSIONS)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        digest = _sha256(path)
        try:
            bom, fmt = parse_file(path, f.filename)
        except TemplateError as exc:
            return jsonify({"error": "The template has errors - fix the rows below and upload again.",
                            "details": list(exc.errors)}), 400
        except UnrecognizedFormat as exc:
            return jsonify({"error": "This file is not a BOM format the checker recognises.",
                            "hint": str(exc),
                            "retainable": True,
                            "details": []}), 400
        except SheetTooLargeError as exc:
            # Oversized sheet or a zip decompression bomb: a clear refusal,
            # not the generic 'could not process' fallback below.
            return jsonify({"error": str(exc), "details": []}), 400
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

    name = (request.form.get("name") or "").strip()[:200] or os.path.basename(f.filename or "BOM")[:200]
    check = BomCheck(
        project_id=project.id, user_id=user.id, tenant_id=user.tenant_id,
        name=name, filename=(f.filename or "")[:200], file_sha256=digest,
        file_format=fmt, vendor=bom.vendor, normalized=bom.to_dict(), history=[],
    )
    _apply_result(check, result, sizing)
    check.history = [_history_entry(check)]
    db.session.add(check)
    db.session.commit()
    return jsonify(check.to_dict(full=True)), 201


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
    _apply_result(check, result, sizing)
    history = list(check.history or [])
    history.append(_history_entry(check))
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
        path, ext = _save_upload(f, ACCEPTED_EXTENSIONS)
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
    safe = "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in row.filename) or "bom"
    mime = XLSX_MIME if safe.lower().endswith(".xlsx") else "text/csv"
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
