"""Files over MCP: the upload link, its page, and the tools around it
(docs/mcp/01-tool-design.md §3.10, §3.11, §3.11a, §3.11b).

MCP has no file transfer, so a file reaches the server the way it always
has: through the person's browser, under their normal login. A tool mints a
short-lived, single-use link bound to the caller; the page behind it accepts
one file; the parsed result stays on the row for a while so the conversation
can come back to it. The link is an identifier, not a credential: the page
requires the session cookie of the user who asked for it (the spec's
URL-elicitation rules, followed so this can become an elicitation later).

Assessments are parsed by ``import_service.parse_assessment`` — the import
route's own pipeline — and stored as the dataset the merge and the VM list
read. BOMs go through the project's own check route from the page, so the
parsers, the agent path and its budgets apply unchanged; the row then holds
the check or job id. A merge (``merge_uploads``) is stored as another row of
kind ``merged`` whose targets are sized like any upload.
"""
import math
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from flask import Blueprint, abort, current_app, jsonify, render_template, request

from auth import current_user
from auth_models import JSON_TYPE
from database import db
from extensions import limiter
import import_merge
import import_service
from mcp_registry import SCHEMA_VERSION, ToolError, audit_event, invalid_input, tool
import mcp_payload
import sizing_service as svc

LINK_MINUTES = 15
RETENTION_HOURS = int(os.environ.get("MCP_UPLOAD_RETENTION_HOURS", "72") or "72")
STATUS_WAITING, STATUS_PROCESSING, STATUS_READY, STATUS_FAILED, STATUS_EXPIRED = (
    "waiting", "processing", "ready", "failed", "expired")
KIND_ASSESSMENT, KIND_BOM, KIND_MERGED = "assessment", "bom", "merged"
MAX_MERGE_FILES = 50


def _utcnow():
    return datetime.now(timezone.utc)


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


class McpUpload(db.Model):
    """One upload link and what came through it. ``data`` holds the parsed
    assessment dataset (or a merge's targets), never the file."""
    __tablename__ = "mcp_uploads"

    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(48), nullable=False, unique=True, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)
    kind = db.Column(db.String(12), nullable=False)
    status = db.Column(db.String(12), nullable=False, default=STATUS_WAITING)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"))
    sizing_id = db.Column(db.Integer, db.ForeignKey("configurations.id"))
    check_id = db.Column(db.Integer)
    job_id = db.Column(db.Integer)
    data = db.Column(JSON_TYPE)
    error = db.Column(JSON_TYPE)
    merge_inputs = db.Column(JSON_TYPE)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    link_expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    retention_until = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    consumed_at = db.Column(db.DateTime(timezone=True))

    @property
    def public_id(self):
        return ("m_" if self.kind == KIND_MERGED else "u_") + self.token

    def link_open(self):
        return self.status == STATUS_WAITING and _aware(self.link_expires_at) > _utcnow()


def _new_row(user, kind, **fields):
    now = _utcnow()
    row = McpUpload(token=secrets.token_urlsafe(24), user_id=user.id, tenant_id=user.tenant_id,
                    kind=kind, link_expires_at=now + timedelta(minutes=LINK_MINUTES),
                    retention_until=now + timedelta(hours=RETENTION_HOURS), **fields)
    db.session.add(row)
    db.session.commit()
    return row


def _row_for(caller, upload_id):
    """The caller's own row behind a tool argument, or not_found: an upload
    is readable by the user who started it and nobody else (§3.10)."""
    token = str(upload_id or "")
    if "_" in token:
        token = token.split("_", 1)[1]
    row = McpUpload.query.filter_by(token=token).first()
    if row is None or row.user_id != caller.user.id:
        raise ToolError("not_found", "No upload %r belongs to this account." % upload_id)
    if _aware(row.retention_until) <= _utcnow():
        raise ToolError("upload_expired", "Upload %s has been purged (results are kept for "
                        "%d hours). Start a new upload." % (upload_id, RETENTION_HOURS))
    return row


# ── the page (session-authenticated, not part of the MCP blueprint) ──────────

upload_bp = Blueprint("mcp_upload", __name__)


def _page_row(token):
    row = McpUpload.query.filter_by(token=token).first()
    user = current_user()
    # Not the user who asked, or no such link: 404, never a hint.
    if row is None or user is None or row.user_id != user.id:
        abort(404)
    return row


@upload_bp.route("/mcp/upload/<token>")
def upload_page(token):
    row = _page_row(token)
    if not row.link_open():
        return render_template("mcp_upload_closed.html", status=row.status), 410
    project_name, accept = None, ".xlsx"
    if row.kind == KIND_BOM:
        from bom_routes import accepted_extensions
        from project_models import Project
        project = db.session.get(Project, row.project_id)
        project_name = project.name if project else ""
        accept = ",".join(accepted_extensions())
    return render_template("mcp_upload.html", token=token, kind=row.kind,
                           project_id=row.project_id, sizing_id=row.sizing_id,
                           project_name=project_name, accept=accept,
                           expires_at=_aware(row.link_expires_at).isoformat(),
                           user_email=current_user().email)


@upload_bp.route("/mcp/upload/<token>", methods=["POST"])
@limiter.limit("20 per minute")
def upload_file(token):
    """The assessment file itself. Parsed with the import route's pipeline,
    stored as the dataset, the link closed either way."""
    from request_utils import UploadError, spooled_xlsx
    row = _page_row(token)
    if not row.link_open():
        return jsonify({"error": "This upload link has expired or was already used."}), 410
    if row.kind != KIND_ASSESSMENT:
        return jsonify({"error": "This link is for a BOM upload."}), 400
    row.status = STATUS_PROCESSING
    db.session.commit()
    try:
        with spooled_xlsx(request, bad_type_msg="The file must be an .xlsx export "
                          "(Live Optics, RVTools or Nutanix Collector).") as path:
            data = import_service.parse_assessment(path, request.files["file"].filename)
    except UploadError as exc:
        row.status = STATUS_WAITING     # a wrong file keeps the link open
        db.session.commit()
        return jsonify({"error": str(exc)}), exc.status
    except import_service.AssessmentError as exc:
        _fail(row, exc.code or "unrecognised_file", str(exc), exc.params)
        return jsonify({"error": str(exc)}), exc.status
    except Exception as exc:    # noqa: BLE001 — a parser crash is the file's fault
        # The type only: a parser's message can quote a cell of the file.
        current_app.logger.warning("MCP upload parse failed: %s", type(exc).__name__)
        _fail(row, "parse_failed", import_service.PARSE_FAILED_MESSAGE)
        return jsonify({"error": import_service.PARSE_FAILED_MESSAGE}), 400
    try:
        row.data = _dataset_record(data)
        row.status = STATUS_READY
        row.consumed_at = _utcnow()
        db.session.commit()
    except Exception as exc:    # noqa: BLE001 — the file parsed; storing it is our fault
        db.session.rollback()
        ref = "%x" % int(_utcnow().timestamp() * 1000)
        current_app.logger.exception("MCP upload store failed (ref %s): %s", ref, type(exc).__name__)
        _fail(row, "store_failed", "The file was read but its result could not be stored "
              "(reference %s). Start a new upload; if it happens again, report the reference." % ref)
        return jsonify({"error": row.error}), 500
    return jsonify({"ok": True, "file_type": data["file_type"],
                    "vm_count": len(data.get("vms") or [])})


@upload_bp.route("/mcp/upload/<token>/done", methods=["POST"])
def upload_done(token):
    """The BOM page reports the check or agent job it created through the
    project's own route."""
    row = _page_row(token)
    if not row.link_open() or row.kind != KIND_BOM:
        return jsonify({"error": "This upload link has expired or was already used."}), 410
    body = request.get_json(silent=True) or {}
    check_id, job_id = body.get("check_id"), body.get("job_id")
    if not _bom_belongs(row, check_id, job_id):
        return jsonify({"error": "Unknown check or job."}), 400
    row.check_id, row.job_id = check_id, job_id
    row.status = STATUS_READY
    row.consumed_at = _utcnow()
    db.session.commit()
    return jsonify({"ok": True})


def _bom_belongs(row, check_id, job_id):
    from bom_models import BomAgentJob, BomCheck
    if check_id:
        check = db.session.get(BomCheck, int(check_id))
        return bool(check and check.user_id == row.user_id and check.project_id == row.project_id)
    if job_id:
        job = db.session.get(BomAgentJob, int(job_id))
        return bool(job and job.user_id == row.user_id and job.project_id == row.project_id)
    return False


def _fail(row, code, message, params=None):
    row.status = STATUS_FAILED
    row.error = {"code": code, "message": message, "params": params or {}}
    row.consumed_at = _utcnow()
    db.session.commit()


def _json_safe(node):
    """The parsed dataset as plain JSON values. The parsers pass workbook
    cells through as they are (RVTools' vMetaData "xlsx creation datetime"
    is a datetime), which jsonify tolerates on the web route but the JSON
    column does not; Postgres also refuses NaN and infinity."""
    if isinstance(node, dict):
        return {str(k): _json_safe(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_json_safe(v) for v in node]
    if isinstance(node, bool) or node is None or isinstance(node, (str, int)):
        return node
    if isinstance(node, float):
        return node if math.isfinite(node) else None
    if isinstance(node, (datetime, date)):
        return node.isoformat()
    if isinstance(node, Decimal):
        return float(node) if node.is_finite() else None
    return str(node)


def _dataset_record(data):
    """What is kept of a parsed assessment: the dataset shape the merge and
    the raw import route use, plus the id the merge keys sources by."""
    meta = data["source_meta"]
    return _json_safe({
        "id": "f" + (meta.get("file_sha256") or secrets.token_hex(5))[:10],
        "file_type": data["file_type"], "scan_type": data.get("scan_type"),
        "source_meta": meta, "project": data.get("project"),
        "summary": data["summary"], "hosts": data.get("hosts") or [],
        "host_performance": data.get("host_performance") or [],
        "host_nics": data.get("host_nics") or [], "datastores": data.get("datastores") or [],
        "vms": data.get("vms") or [], "clusters": data.get("clusters") or [],
        "import_warnings": data.get("import_warnings") or [],
    })


def purge_expired():
    """Rows past their retention. Called from the maintenance loop."""
    n = McpUpload.query.filter(McpUpload.retention_until < _utcnow()).delete(
        synchronize_session=False)
    db.session.commit()
    return n


# ── datasets behind a from_upload argument ───────────────────────────────────

def dataset_for(caller, upload_id, source_cluster=None):
    """``(row, dataset)`` for an upload or merge id: the whole estate, one
    source cluster of a single file, or one target of a merge. The dataset
    has ``summary`` and ``vms`` (plus hosts etc.) and is what the exclusion
    maths and the VM list work on."""
    row = _row_for(caller, upload_id)
    if row.kind == KIND_BOM:
        raise invalid_input([{"field": "from_upload.upload_id",
                              "problem": "is a BOM upload, not an assessment"}])
    if row.status != STATUS_READY:
        raise ToolError("upload_not_ready", "Upload %s is %s; nothing to size yet."
                        % (upload_id, row.status), status=row.status)
    data = row.data or {}
    if row.kind == KIND_MERGED:
        targets = data.get("targets") or []
        if source_cluster is None:
            if len(targets) != 1:
                raise invalid_input([{"field": "from_upload.source_cluster",
                                      "problem": "is required for a merge with %d targets (%s)"
                                      % (len(targets), ", ".join(t["name"] for t in targets))}])
            return row, targets[0]
        for t in targets:
            if t["name"] == source_cluster:
                return row, t
        raise invalid_input([{"field": "from_upload.source_cluster",
                              "problem": "is not a target of this merge (%s)"
                              % ", ".join(t["name"] for t in targets)}])
    if source_cluster is None:
        return row, data
    sub = import_service.cluster_dataset(data, source_cluster)
    if sub is None:
        names = [c["name"] for c in data.get("clusters") or []]
        raise invalid_input([{"field": "from_upload.source_cluster",
                              "problem": "is not a source cluster of this upload (%s)"
                              % (", ".join(names) or "single cluster")}])
    from parser_common import build_summary
    sub = dict(sub, summary=build_summary(sub, data.get("file_type")))
    return row, sub


def adjusted_summary(dataset, spec):
    """The dataset's summary after the exclusions of a from_upload block."""
    vms = dataset.get("vms") or []
    for field in ("exclude_compute", "exclude_storage"):
        bad = [i for i in spec.get(field) or [] if i >= len(vms)]
        if bad:
            raise invalid_input([{"field": "from_upload." + field,
                                  "problem": "has vm_index values out of range (%s); the upload "
                                  "has %d VMs" % (", ".join(map(str, bad[:5])), len(vms))}])
    return import_service.adjust_summary(
        dataset["summary"], vms, spec.get("exclude_compute") or [],
        spec.get("exclude_storage") or [], bool(spec.get("include_local_storage")))


def exclusion_stats(dataset, spec, summary):
    base = dataset["summary"]
    return {"compute": len(set(spec.get("exclude_compute") or [])),
            "storage": len(set(spec.get("exclude_storage") or [])),
            "vcpus_removed": (base.get("total_vcpus") or 0) - (summary.get("total_vcpus") or 0),
            "ram_gb_removed": round((base.get("total_vm_provisioned_memory_gb") or 0)
                                    - (summary.get("total_vm_provisioned_memory_gb") or 0), 1),
            "storage_tb_removed": round((base.get("datastore_used_tb") or 0)
                                        - (summary.get("datastore_used_tb") or 0), 2),
            "include_local_storage": bool(spec.get("include_local_storage"))}


# ── tools ────────────────────────────────────────────────────────────────────

def _base_url():
    return (os.environ.get("APP_BASE_URL") or "").rstrip("/")


@tool("start_upload", "Upload a file",
      "Creates a one-time upload page so the user can upload a file from their own browser: a "
      "Live Optics, RVTools or Nutanix Collector export (.xlsx) to size from, or a hardware "
      "BOM/quote to check. Give the returned upload_url to the user, ask them to open it while "
      "signed in to the sizing tool, and wait for them to say it is done. Then call get_upload. "
      "The link expires after 15 minutes and works once, only for this user. Never ask the user "
      "to paste file contents instead.",
      {"type": "object", "additionalProperties": False, "required": ["kind"], "properties": {
          "kind": {"type": "string", "enum": [KIND_ASSESSMENT, KIND_BOM]},
          "project_id": {"type": "integer", "minimum": 1,
                         "description": "Required for kind=bom: a project the user owns. The BOM check is stored there, as in the web tool."},
          "sizing_id": {"type": "integer", "minimum": 1,
                        "description": "kind=bom only, optional: a sizing in that project to compare the BOM's capacity against."},
      }}, read_only=False, destructive=False, idempotent=False)
def start_upload(caller, args):
    user = caller.user
    fields = {}
    if args["kind"] == KIND_BOM:
        from bom_models import live_sizing
        from projects import _visible_project
        if not args.get("project_id"):
            raise invalid_input([{"field": "project_id", "problem": "is required for a BOM upload"}])
        project, _ = _visible_project(args["project_id"], user)
        if project is None:
            raise ToolError("not_found", "No project with id %s is visible to this account."
                            % args["project_id"])
        if not project.can_edit(user):
            raise ToolError("not_owner", "BOM checks can only be added to the user's own projects.")
        fields["project_id"] = project.id
        if args.get("sizing_id"):
            from auth_models import Configuration
            sizing = live_sizing(db.session.get(Configuration, args["sizing_id"]), project.id)
            if sizing is None:
                raise ToolError("not_found", "Sizing %s is not in project %s." %
                                (args["sizing_id"], project.id))
            fields["sizing_id"] = sizing.id
    elif args.get("project_id") or args.get("sizing_id"):
        raise invalid_input([{"field": "project_id", "problem": "applies to kind=bom only"}])
    row = _new_row(user, args["kind"], **fields)
    audit_event(caller, "start_upload", "%s upload %s%s" % (
        args["kind"], row.public_id,
        " for project %d" % fields["project_id"] if fields.get("project_id") else ""))
    url = "%s/mcp/upload/%s" % (_base_url(), row.token)
    accepted = [".xlsx"]
    if args["kind"] == KIND_BOM:
        from bom_routes import accepted_extensions
        accepted = list(accepted_extensions())
    out = {"schema_version": SCHEMA_VERSION, "upload_id": row.public_id, "upload_url": url,
           "expires_at": _aware(row.link_expires_at).isoformat(), "kind": args["kind"],
           "accepted": accepted,
           "instructions": "Open the link in a browser while signed in to the sizing tool, "
                           "choose the file, then come back here."}
    text = ("Upload link for %s: %s (valid %d minutes, one file, this user only). Ask the user "
            "to open it, then call get_upload with upload_id %s."
            % ("a BOM" if args["kind"] == KIND_BOM else "an assessment export",
               url, LINK_MINUTES, row.public_id))
    return out, text


_UNMEASURED_IOPS = ("p95_iops", "avg_iops", "peak_iops")


def unmeasured_fields(dataset):
    """Workload fields the export cannot have measured, where the parser's 0
    means "no data": IOPS when the import flagged none (RVTools always),
    and used RAM for RVTools, which has no consumed-memory column."""
    dataset = dataset or {}
    codes = {(w or {}).get("code") for w in dataset.get("import_warnings") or []}
    fields = []
    rvtools = dataset.get("file_type") == "rvtools"
    if "no_iops" in codes or rvtools:
        fields.extend(_UNMEASURED_IOPS)
    if rvtools:
        fields.append("used_ram_gb")
    return fields


def _summary_inputs(summary, dataset=None):
    """The summary as a workload, unmeasured fields as null, plus what a
    sizing would still need."""
    workload = svc.summary_to_workload(summary)
    check = svc.validate_workload(workload)
    for field in unmeasured_fields(dataset):
        workload[field] = None
    return {"workload": workload}, check["missing"]


def _largest_vms(vms, n=5):
    active = [(i, v) for i, v in enumerate(vms) if import_service._is_active(v)]
    rows = []
    for i, v in sorted(active, key=lambda p: (-(p[1].get("vcpus") or 0),
                                              -(p[1].get("provisioned_memory_gb") or 0)))[:n]:
        rows.append({"vm_index": i, "name": v.get("name"), "vcpus": v.get("vcpus"),
                     "ram_gb": v.get("provisioned_memory_gb"), "powered_on": True})
    return rows


# Import warnings read as the wizard shows them, except where the wizard's
# wording would mislead a caller that reasons from it: no_iops speaks of
# "default per-drive IOPS", which is the capability side of each option, not
# a demand figure; the engine skips the IOPS comparison when demand is unknown.
_WARNING_TEXT_OVERRIDES = {
    "no_iops": "No IOPS data was found in this import: the IOPS check cannot run (demand unknown); "
               "options still report their per-drive IOPS capability.",
}


def _warning_text(warning, lang):
    code = (warning or {}).get("code")
    if code in _WARNING_TEXT_OVERRIDES:
        return _WARNING_TEXT_OVERRIDES[code]
    return mcp_payload.import_warning_text(warning, lang)


def _assessment_body(row, lang):
    data = row.data or {}
    inputs, missing = _summary_inputs(data["summary"], data)
    clusters = []
    for c in data.get("clusters") or []:
        ci, cm = _summary_inputs(c["summary"], data)
        clusters.append({"name": c["name"], "host_count": c.get("host_count"),
                         "vm_count": c.get("vm_count"), "inputs": ci, "missing_for_sizing": cm})
    meta = data.get("source_meta") or {}
    return {
        "source": {k: meta.get(k) for k in ("file_name", "file_type", "collected_at",
                                             "host_count", "vm_count", "parser_version")},
        "inputs": inputs, "missing_for_sizing": missing,
        "import_warnings": [dict(w, text=_warning_text(w, lang))
                            for w in data.get("import_warnings") or []],
        "largest_vms": _largest_vms(data.get("vms") or []),
        "clusters": clusters,
    }


def _merged_body(row, lang):
    data = row.data or {}
    targets = []
    for t in data.get("targets") or []:
        ti, tm = _summary_inputs(t["summary"], t)
        targets.append({"name": t["name"], "sources": t.get("sources"),
                        "host_count": len(t.get("hosts") or []), "vm_count": len(t.get("vms") or []),
                        "inputs": ti, "missing_for_sizing": tm,
                        "import_warnings": [dict(w, text=_warning_text(w, lang))
                                            for w in t.get("import_warnings") or []]})
    return {"source": {"file_type": "merged", "files": len(data.get("sources") or [])},
            "inputs": targets[0]["inputs"] if len(targets) == 1 else None,
            "missing_for_sizing": targets[0]["missing_for_sizing"] if len(targets) == 1 else [],
            "import_warnings": [dict(w, text=_warning_text(w, lang))
                                for w in data.get("import_warnings") or []],
            "largest_vms": _largest_vms(targets and (data["targets"][0].get("vms") or []) or []),
            "clusters": targets}


def _bom_body(row):
    from bom_models import BomAgentJob, BomCheck
    body = {"check_id": row.check_id, "job": None, "result": None}
    job = db.session.get(BomAgentJob, row.job_id) if row.job_id else None
    if job is not None:
        body["job"] = {"id": job.id, "status": job.status}
        if getattr(job, "check_id", None):
            body["check_id"] = job.check_id
    check = db.session.get(BomCheck, body["check_id"]) if body["check_id"] else None
    if check is not None and not check.is_deleted:
        body["result"] = check.result
        body["name"] = check.name
        body["file_format"] = check.file_format
    return body


@tool("get_upload", "Get upload result",
      "Returns the status and result of an upload started with start_upload. For an assessment, "
      "size it by passing from_upload: {upload_id} to create_sizing. Use list_upload_vms to review "
      "VMs with the user and exclude some. The result also has the summary inputs and data-quality "
      "warnings about the file. If the export held several vSphere clusters, clusters has inputs "
      "per cluster: ask the user whether to size them together or separately. For a BOM the "
      "result is the compatibility check. If status is waiting, the user has not uploaded yet.",
      {"type": "object", "additionalProperties": False, "required": ["upload_id"], "properties": {
          "upload_id": {"type": "string", "minLength": 3, "maxLength": 64},
          "wait_seconds": {"type": "integer", "minimum": 0, "maximum": 20, "default": 0,
                           "description": "Wait up to this long for the status to leave 'waiting'."},
      }}, read_only=True)
def get_upload(caller, args):
    import time
    row = _row_for(caller, args["upload_id"])
    deadline = time.monotonic() + args.get("wait_seconds", 0)
    while row.status in (STATUS_WAITING, STATUS_PROCESSING) and time.monotonic() < deadline:
        time.sleep(1)
        db.session.expire(row)
        row = McpUpload.query.get(row.id)
    status = row.status
    if status == STATUS_WAITING and not row.link_open():
        status = STATUS_EXPIRED
    out = {"schema_version": SCHEMA_VERSION, "upload_id": row.public_id, "kind": row.kind,
           "status": status, "error": row.error, "assessment": None, "bom": None}
    text = "Upload %s is %s." % (row.public_id, status)
    if status == STATUS_READY:
        if row.kind == KIND_BOM:
            out["bom"] = _bom_body(row)
            job = out["bom"]["job"]
            text += (" BOM check %s." % out["bom"]["check_id"] if out["bom"]["result"] is not None
                     else " The file is being read by the agent (job %s, %s); call again."
                     % (job and job["id"], job and job["status"]))
        else:
            out["assessment"] = (_merged_body(row, caller.lang) if row.kind == KIND_MERGED
                                 else _assessment_body(row, caller.lang))
            a = out["assessment"]
            src = a["source"]
            text += " %s: %s hosts, %s VMs." % (src.get("file_type"), src.get("host_count"),
                                                src.get("vm_count")) if row.kind != KIND_MERGED \
                else " merged estate of %s files, %d target cluster%s." % (
                    src["files"], len(a["clusters"]), "" if len(a["clusters"]) == 1 else "s")
            if a["missing_for_sizing"]:
                text += " Missing for sizing: %s." % ", ".join(a["missing_for_sizing"])
            if len(a["clusters"]) > 1:
                text += " Clusters: %s." % ", ".join(c["name"] for c in a["clusters"])
            for w in a["import_warnings"]:
                text += "\nWarning: " + w["text"]
    elif status == STATUS_FAILED:
        text += " " + ((row.error or {}).get("message") or "")
    elif status == STATUS_EXPIRED:
        text += " The link was not used in time; call start_upload again."
    return out, text


VM_SORT = {"name": lambda v: str(v.get("name") or "").lower(),
           "vcpus": lambda v: v.get("vcpus") or 0,
           "ram_gb": lambda v: v.get("provisioned_memory_gb") or 0,
           "storage_used_gb": lambda v: v.get("vdisk_used_gb") or 0}


@tool("list_upload_vms", "List uploaded VMs",
      "Lists the VMs in an uploaded assessment, filtered and paged, so you can work out with the "
      "user which VMs to leave out of the sizing (retiring, staying behind, infrastructure VMs). "
      "Each row has a vm_index: pass those in from_upload.exclude_compute / exclude_storage to "
      "create_sizing and save_sizing. Rows flagged suggested_exclusion match patterns for VMs "
      "that never migrate (storage controller VMs of the old HCI, vCLS agents, vCenter). Suggest "
      "them, but let the user decide. Do not request every page of a large estate: filter or "
      "sort instead.",
      {"type": "object", "additionalProperties": False, "required": ["upload_id"], "properties": {
          "upload_id": {"type": "string", "minLength": 3, "maxLength": 64},
          "source_cluster": {"type": "string", "maxLength": 120},
          "name_contains": {"type": "string", "maxLength": 100},
          "os_contains": {"type": "string", "maxLength": 100},
          "powered_on": {"type": "boolean"},
          "include_templates": {"type": "boolean", "default": False},
          "suggested_exclusions_only": {"type": "boolean", "default": False},
          "min_vcpus": {"type": "integer", "minimum": 0},
          "min_ram_gb": {"type": "number", "minimum": 0},
          "min_storage_gb": {"type": "number", "minimum": 0},
          "sort": {"type": "string", "enum": list(VM_SORT), "default": "vcpus"},
          "descending": {"type": "boolean", "default": True},
          "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50},
          "offset": {"type": "integer", "minimum": 0, "default": 0},
      }}, read_only=True)
def list_upload_vms(caller, args):
    _, dataset = dataset_for(caller, args["upload_id"], args.get("source_cluster"))
    vms = dataset.get("vms") or []
    rows, estate = [], {"vms": len(vms), "powered_on": 0, "templates": 0, "suggested_exclusions": 0}
    for i, v in enumerate(vms):
        flag = import_service.suggested_exclusion(v.get("name"))
        estate["powered_on"] += 1 if v.get("powered_on") else 0
        estate["templates"] += 1 if v.get("is_template") else 0
        estate["suggested_exclusions"] += 1 if flag else 0
        if v.get("is_template") and not args["include_templates"]:
            continue
        if args.get("powered_on") is not None and bool(v.get("powered_on")) != args["powered_on"]:
            continue
        if args["suggested_exclusions_only"] and not flag:
            continue
        if args.get("name_contains") and args["name_contains"].lower() not in str(v.get("name") or "").lower():
            continue
        if args.get("os_contains") and args["os_contains"].lower() not in str(v.get("os") or "").lower():
            continue
        if args.get("min_vcpus") is not None and (v.get("vcpus") or 0) < args["min_vcpus"]:
            continue
        if args.get("min_ram_gb") is not None and (v.get("provisioned_memory_gb") or 0) < args["min_ram_gb"]:
            continue
        if args.get("min_storage_gb") is not None and (v.get("vdisk_used_gb") or 0) < args["min_storage_gb"]:
            continue
        rows.append((i, v, flag))
    rows.sort(key=lambda t: VM_SORT[args["sort"]](t[1]), reverse=args["descending"])
    offset, limit = args["offset"], args["limit"]
    page = rows[offset:offset + limit]
    out_rows = [{"vm_index": i, "name": v.get("name"), "cluster": v.get("cluster"),
                 "host": v.get("host"), "os": v.get("os"), "powered_on": bool(v.get("powered_on")),
                 "is_template": bool(v.get("is_template")), "vcpus": v.get("vcpus"),
                 "ram_gb": v.get("provisioned_memory_gb"), "ram_used_gb": v.get("consumed_memory_gb"),
                 "storage_provisioned_gb": v.get("vdisk_size_gb"),
                 "storage_used_gb": v.get("vdisk_used_gb"), "suggested_exclusion": flag}
                for i, v, flag in page]
    out = {"schema_version": SCHEMA_VERSION, "vms": out_rows, "total_matching": len(rows),
           "next_offset": offset + limit if offset + limit < len(rows) else None, "estate": estate}
    text = ("%d of %d VMs match (%d powered on, %d templates, %d suggested exclusions in the "
            "estate); showing %d from %d."
            % (len(rows), len(vms), estate["powered_on"], estate["templates"],
               estate["suggested_exclusions"], len(out_rows), offset))
    return out, text


@tool("merge_uploads", "Merge uploaded files",
      "Combines several uploaded assessment files (one per start_upload) into one estate, the way "
      "the web tool's multi-file import does. Use it when a customer's environment came as more "
      "than one Live Optics, RVTools or Nutanix export. The first call returns the overlaps (a "
      "VM or host found in more than one file) and the source-cluster groups with their default "
      "target clusters. Show the user the overlaps and the mapping, then call again with their "
      "choices. The result is a new upload_id that get_upload, list_upload_vms, create_sizing "
      "and save_sizing accept; size one target at a time with from_upload.source_cluster.",
      {"type": "object", "additionalProperties": False, "required": ["upload_ids"], "properties": {
          "upload_ids": {"type": "array", "minItems": 1, "maxItems": MAX_MERGE_FILES,
                         "items": {"type": "string", "minLength": 3, "maxLength": 64},
                         "description": "Assessment uploads to merge, in the order the user gave them. All must belong to the user and be ready."},
          "resolutions": {"type": "array", "maxItems": 5000, "items": {
              "type": "object", "additionalProperties": False, "required": ["key", "keep"],
              "properties": {"key": {"type": "string", "maxLength": 200, "description": "An overlap key from a previous call (vm:… or host:…)."},
                             "keep": {"type": "string", "maxLength": 64, "description": "The upload_id whose copy to keep, or 'both' for a VM that really is two different VMs."}}},
              "description": "Overlap choices. Default: the copy from the most recently collected file."},
          "mapping": {"type": "array", "maxItems": 500, "items": {
              "type": "object", "additionalProperties": False, "required": ["group", "target"],
              "properties": {"group": {"type": "string", "maxLength": 200, "description": "A group key from a previous call."},
                             "target": {"type": "string", "maxLength": 120, "description": "Target cluster name, or 'skip' to leave the group out."}}},
              "description": "Which target cluster each source group goes to. Default: clusters to their own name, standalone hosts to one shared target."},
      }}, read_only=False, destructive=False, idempotent=True)
def merge_uploads(caller, args):
    rows, datasets, by_public = [], [], {}
    for uid in args["upload_ids"]:
        row = _row_for(caller, uid)
        if row.kind != KIND_ASSESSMENT:
            raise invalid_input([{"field": "upload_ids", "problem": "%s is not an assessment upload "
                                  "(merges and BOMs cannot be merged)" % uid}])
        if row.status != STATUS_READY:
            raise ToolError("upload_not_ready", "Upload %s is %s." % (uid, row.status))
        rows.append(row)
        ds = dict(row.data)
        ds["id"] = row.token          # the merge keys sources by this id
        datasets.append(ds)
        by_public[row.public_id] = row.token
        by_public[row.token] = row.token
    resolutions = {}
    for r in args.get("resolutions") or []:
        keep = r["keep"]
        resolutions[r["key"]] = import_merge.KEEP_BOTH if keep == "both" else by_public.get(keep, keep)
    mapping = {}
    for m in args.get("mapping") or []:
        mapping[m["group"]] = import_merge.SKIP if m["target"] == "skip" else m["target"]

    merged = import_merge.merge(datasets, resolutions, mapping)
    public = {row.token: row.public_id for row in rows}

    def pub(sid):
        return public.get(sid, sid)

    sources = [{"upload_id": pub(s["id"]), "file_name": s.get("file_name"),
                "file_type": s.get("file_type"), "collected_at": s.get("collected_at"),
                "dropped": s.get("dropped") or None} for s in merged["sources"]]
    overlaps = [{"key": o["key"], "kind": o["kind"], "name": o["name"],
                 "possible_duplicate": bool(o.get("possible")),
                 "copies": [{"upload_id": pub(c["src"]), "name": c.get("name"),
                             "cluster": c.get("cluster"),
                             "vcpus": c.get("vcpus") if o["kind"] == "vm" else None,
                             "cores": c.get("cores") if o["kind"] == "host" else None,
                             "ram_gb": c.get("ram_gb")} for c in o["copies"]],
                 "choice": "both" if o["choice"] == import_merge.KEEP_BOTH else pub(o["choice"]),
                 "default": pub(o["default"])} for o in merged["overlaps"]]
    groups = [{"key": g["key"], "upload_id": pub(g.get("src")) if g.get("src") else None,
               "cluster": g.get("cluster"), "hosts": g.get("hosts"), "vms": g.get("vms"),
               "target": g.get("target"), "duplicate_of": g.get("duplicate_of")}
              for g in merged["groups"]]
    targets = [dict(t, sources=[pub(s) for s in t.get("sources") or []]) for t in merged["targets"]]
    row = _new_row(caller.user, KIND_MERGED, status=STATUS_READY, consumed_at=_utcnow(),
                   data={"sources": sources, "targets": targets,
                         "import_warnings": merged["import_warnings"]},
                   merge_inputs={"upload_ids": args["upload_ids"],
                                 "resolutions": args.get("resolutions") or [],
                                 "mapping": args.get("mapping") or []})
    lang = caller.lang
    out = {"schema_version": SCHEMA_VERSION, "upload_id": row.public_id, "sources": sources,
           "overlaps": overlaps, "groups": groups,
           "targets": [{"name": t["name"], "sources": t["sources"],
                        "hosts": len(t.get("hosts") or []), "vms": len(t.get("vms") or []),
                        "inputs": _summary_inputs(t["summary"], t)[0],
                        "missing_for_sizing": _summary_inputs(t["summary"], t)[1],
                        "import_warnings": [dict(w, text=_warning_text(w, lang))
                                            for w in t.get("import_warnings") or []]}
                       for t in targets],
           "import_warnings": [dict(w, text=_warning_text(w, lang))
                               for w in merged["import_warnings"]]}
    text = ("Merged %d file%s into %d target cluster%s (%s); %d overlap%s to review. Result "
            "upload_id %s." % (len(rows), "" if len(rows) == 1 else "s", len(targets),
                               "" if len(targets) == 1 else "s",
                               ", ".join(t["name"] for t in targets), len(overlaps),
                               "" if len(overlaps) == 1 else "s", row.public_id))
    return out, text
