"""``save_sizing``: the one MCP tool that creates a sizing
(docs/mcp/01-tool-design.md §3.6).

It re-runs the engine on the given inputs, takes the option the user chose,
and writes a Configuration the way ``POST /api/configs`` plus ``PUT
/api/sizings/<id>/result`` do from the browser: the payload in the UI's
shape (mcp_payload), the result snapshot and its fingerprint, under a
project the caller owns. It never updates or deletes anything.
"""
import os

from sqlalchemy.exc import IntegrityError

from database import db
import mcp_payload
import mcp_tools
from mcp_registry import SCHEMA_VERSION, ToolError, invalid_input, tool
import mcp_presenter as presenter
import sizing_service as svc

SAVE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["name"],
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 200,
                 "description": "The sizing's name as it will appear in the project."},
        "workload": mcp_tools._with_required(mcp_tools.WORKLOAD_SCHEMA, mcp_tools.WORKLOAD_REQUIRED),
        "from_upload": mcp_tools.FROM_UPLOAD_SCHEMA,
        "params": mcp_tools.PARAMS_SCHEMA,
        "option_rank": {"type": "integer", "minimum": 1, "maximum": 8, "default": 1,
                        "description": "Which ranked option to store (1 = top), as listed by create_sizing with the same inputs."},
        "expected_option": {"type": "object", "additionalProperties": False,
                            "required": ["model", "node_count"],
                            "properties": {"model": {"type": "string", "maxLength": 120},
                                           "node_count": {"type": "integer", "minimum": 1}},
                            "description": "The model and node count the user agreed to. If the re-run ranks a different option at option_rank, nothing is saved and the error lists the current options."},
        "project_id": {"type": "integer", "minimum": 1,
                       "description": "An existing project the user owns (list_projects). Omit both project fields to save into the user's personal 'Unfiled' project."},
        "new_project_name": {"type": "string", "minLength": 1, "maxLength": 200,
                             "description": "Create a new project with this name and save into it. Not together with project_id."},
        "notes": {"type": "string", "maxLength": 2000,
                  "description": "Why this option: shown on the sizing and in the proposal export."},
    },
}


def _open_url(project, config):
    base = (os.environ.get("APP_BASE_URL") or "").rstrip("/")
    return "%s/?project=%d&sizing=%d" % (base, project.id, config.id)


def _resolve_project(user, args):
    from project_models import ensure_scratch_project
    pid, new_name = args.get("project_id"), (args.get("new_project_name") or "").strip()
    if pid and new_name:
        raise invalid_input([{"field": "new_project_name",
                              "problem": "cannot be combined with project_id"}])
    if pid:
        from projects import _visible_project
        # Visibility first, then ownership: a project the caller cannot see
        # is "not found", never "someone else's" (the routes' no-leak rule).
        project, _ = _visible_project(pid, user)
        if project is None:
            raise ToolError("not_found", "No project with id %s is visible to this account." % pid)
        if not project.can_edit(user):
            raise ToolError("not_owner", "Project %r belongs to someone else; sizings can only "
                            "be saved into the user's own projects (list_projects shows "
                            "can_edit)." % project.name)
        return project, False
    if new_name:
        from mcp_sizings import new_project
        return new_project(user, new_name), True
    return ensure_scratch_project(user.id, user.tenant_id), False


@tool("save_sizing", "Save a sizing",
      "Saves a sizing so it can be opened, adjusted and exported in the Scale Computing sizing "
      "tool. Call this only when the user asks to save. It re-runs the engine on the same inputs "
      "and stores the option at option_rank (1 = top); pass expected_option so nothing is saved "
      "if the ranking moved. It always creates a new sizing owned by the user; it never changes "
      "or overwrites an existing one. Returns the sizing id, share code and a link to open it.",
      SAVE_SCHEMA, read_only=False, destructive=False, idempotent=False)
def save_sizing(caller, args):
    from auth import _payload_digest, audit
    from auth_models import Configuration
    from fingerprint import (fingerprint_snapshot, inbound_replication_digest,
                             tunables_digest)
    from project_models import new_code

    user = caller.user
    summary, kwargs, inputs_used, warnings = mcp_tools._prepare(caller, args)
    ratio = inputs_used["params"]["vcpu_ratio"]
    if not mcp_payload.ratio_representable(ratio):
        raise invalid_input([{"field": "params.vcpu_ratio",
                              "problem": "must be between %s and %s in steps of %s to be "
                              "saved: the web tool's ratio control cannot show other values"
                              % (mcp_payload.RATIO_MIN, mcp_payload.RATIO_MAX,
                                 mcp_payload.RATIO_STEP)}])
    try:
        result = svc.run_recommendation(summary, **kwargs)
    except svc.SizingRequestError as exc:
        raise mcp_tools._engine_error(exc)
    recs = result.get("recommendations") or []
    listing = [{"rank": i + 1, "model": r.get("model"), "node_count": r.get("node_count")}
               for i, r in enumerate(recs)]
    if not recs:
        raise ToolError("no_fit", "No configuration fits this workload, so there is nothing "
                        "to save. " + " ".join(result.get("warnings") or []), options=[])
    rank = args.get("option_rank", 1)
    if rank > len(recs):
        raise ToolError("option_changed", "Only %d option%s fit; option_rank %d does not exist."
                        % (len(recs), "" if len(recs) == 1 else "s", rank), options=listing)
    rec = recs[rank - 1]
    expected = args.get("expected_option")
    if expected and (expected["model"] != rec.get("model")
                     or expected["node_count"] != rec.get("node_count")):
        raise ToolError("option_changed", "Option %d is now %s × %s, not %s × %s. Nothing was "
                        "saved; confirm the current option with the user."
                        % (rank, presenter._fmt(rec.get("node_count")), rec.get("model"),
                           presenter._fmt(expected["node_count"]), expected["model"]),
                        options=listing)

    project, created = _resolve_project(user, args)
    params = inputs_used["params"]
    upload_ctx = inputs_used.pop("_upload_ctx", None)
    if upload_ctx is not None:
        payload, source_meta = mcp_payload.import_payload(
            upload_ctx, summary, params, rank, result.get("projection"))
    else:
        payload, source_meta = mcp_payload.manual_payload(summary, params, rank), None
    position = Configuration.query.filter_by(project_id=project.id, is_deleted=False).count()
    name = args["name"].strip()[:200]
    for _ in range(6):
        config = Configuration(
            code=new_code(), name=name, owner_id=user.id, tenant_id=user.tenant_id,
            payload=payload, project_id=project.id, position=position,
            role=project.default_role, notes=(args.get("notes") or "").strip()[:2000] or None,
            source_meta=source_meta,
            parser_version=(source_meta or {}).get("parser_version"),
            payload_digest=_payload_digest(payload), untouched=False)
        db.session.add(config)
        try:
            db.session.flush()
            break
        except IntegrityError:
            db.session.rollback()
    else:
        raise ToolError("internal_error", "Could not allocate a unique code. Try again.",
                        retryable=True)

    snapshot = mcp_payload.result_snapshot(summary, rec, result.get("projection"),
                                           inputs_used.get("source_perf"), lang=caller.lang)
    snapshot["tunables"] = tunables_digest()
    config.result_snapshot = snapshot
    config.result_fingerprint = fingerprint_snapshot(
        snapshot, replication=inbound_replication_digest(config.id))
    config.result_computed_at = mcp_payload.utcnow()
    audit("mcp.save_sizing", "sizing %d %r in project %d%s" % (
        config.id, name, project.id, " (new project)" if created else ""), actor=user)
    db.session.commit()

    option = presenter.present_option(rec, result, summary, params, rank)
    out = {
        "schema_version": SCHEMA_VERSION,
        "sizing": {"id": config.id, "code": config.code, "name": config.name,
                   "project": {"id": project.id, "name": project.name, "created": created}},
        "open_url": _open_url(project, config),
        "saved_option": option,
        "inputs_used": mcp_tools.public_inputs(inputs_used),
        "warnings": list(result.get("warnings") or []) + warnings,
    }
    text = ("Saved %r as sizing %d (code %s) in project %r: %s × %s. Open it at %s."
            % (config.name, config.id, config.code, project.name,
               presenter._fmt(rec.get("node_count")), option.get("display_model") or rec.get("model"),
               out["open_url"]))
    stmt = (option.get("determinant") or {}).get("statement")
    if stmt:
        text += " " + stmt
    return out, text
