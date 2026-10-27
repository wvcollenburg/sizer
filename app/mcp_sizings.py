"""The MCP tools over saved sizings and projects (docs/mcp/01-tool-design.md
§3.6–§3.9a).

Visibility is the API's own: ``visibility.source_for``, ``resolve_by_code``,
``auth._visible_configs`` and ``projects.visible_projects``, called with the
role-capped caller and nothing added. A sizing the caller may not see is
"not found", never "forbidden", exactly as the routes answer (no leak).
"""
from database import db
from mcp_registry import SCHEMA_VERSION, ToolError, audit_event, tool
import mcp_presenter as presenter
import sizing_service as svc
from tunables import T, refresh_from_db

PAGE = {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
        "offset": {"type": "integer", "minimum": 0, "default": 0}}


def _page(rows, args):
    offset, limit = args.get("offset", 0), args.get("limit", 20)
    chunk = rows[offset:offset + limit]
    nxt = offset + limit if offset + limit < len(rows) else None
    return chunk, len(rows), nxt


def _matches(query, *values):
    q = (query or "").strip().lower()
    return not q or any(q in str(v or "").lower() for v in values)


# ── list_projects ────────────────────────────────────────────────────────────

def _project_row(project, source, user, sizing_count=None):
    d = project.to_summary(user, source, sizing_count=sizing_count)
    return {k: d.get(k) for k in ("id", "code", "name", "customer_name", "opportunity_ref",
                                  "owner_email", "source", "can_edit", "is_scratch",
                                  "sizing_count", "updated_at")}


@tool("list_projects", "List projects",
      "Lists the user's projects (customer engagements), newest first, for choosing where to save "
      "a sizing or which sizings to look at. scope='mine' (default) shows projects the user "
      "created or contributed to; scope='organization' adds every project in their organization. "
      "updated_at is when the project record itself last changed; sizings carry their own.",
      {"type": "object", "additionalProperties": False, "properties": dict(PAGE, **{
          "scope": {"type": "string", "enum": ["mine", "organization"], "default": "mine"},
          "query": {"type": "string", "maxLength": 100,
                    "description": "Matched against project name, customer name and opportunity reference."},
      })}, read_only=True)
def list_projects(caller, args):
    from projects import _list_counts, visible_projects
    user = caller.user
    rows = visible_projects(user, "tenant" if args["scope"] == "organization" else "mine")
    rows = [(p, s) for p, s in rows
            if _matches(args.get("query"), p.name, p.customer_name, p.opportunity_ref)]
    chunk, total, nxt = _page(rows, args)
    counts, _ = _list_counts([p.id for p, _ in chunk])
    projects = [_project_row(p, s, user, counts.get(p.id, 0)) for p, s in chunk]
    out = {"schema_version": SCHEMA_VERSION, "projects": projects, "total": total,
           "next_offset": nxt}
    names = "; ".join("%s (id %s, %s sizing%s)" % (
        p["name"], p["id"], p["sizing_count"], "" if p["sizing_count"] == 1 else "s")
        for p in projects[:10])
    return out, "%d project%s%s" % (total, "" if total == 1 else "s", (": " + names) if names else ".")


# ── list_sizings ─────────────────────────────────────────────────────────────

def _sizing_row(config, source, user, project):
    d = config.to_summary(user, source)
    row = {k: d.get(k) for k in ("id", "code", "name", "owner_email", "source", "can_edit",
                                 "role", "is_dr_target", "has_result", "updated_at")}
    row["project"] = ({"id": project.id, "name": project.name,
                       "customer_name": project.customer_name} if project else None)
    row["source_type"] = (config.source_meta or {}).get("file_type") or "manual"
    return row


def _projects_by_id(ids):
    from project_models import Project
    ids = [i for i in set(ids) if i]
    if not ids:
        return {}
    return {p.id: p for p in Project.query.filter(Project.id.in_(ids)).all()}


@tool("list_sizings", "List saved sizings",
      "Lists saved sizings the user can see, newest first, with their project. scope='mine' "
      "(default) lists the user's own; scope='all_visible' lists everything their account may "
      "see (colleagues' work too). Filter by project or by a text match on sizing, project or "
      "customer name. Use get_sizing to open one.",
      {"type": "object", "additionalProperties": False, "properties": dict(PAGE, **{
          "project_id": {"type": "integer", "minimum": 1},
          "query": {"type": "string", "maxLength": 100},
          "scope": {"type": "string", "enum": ["mine", "all_visible"], "default": "mine"},
      })}, read_only=True)
def list_sizings(caller, args):
    from auth import _visible_configs
    user = caller.user
    pairs = _visible_configs(user)
    if args["scope"] == "mine":
        pairs = [(c, s) for c, s in pairs if s == "owned"]
    if args.get("project_id"):
        pairs = [(c, s) for c, s in pairs if c.project_id == args["project_id"]]
    projects = _projects_by_id([c.project_id for c, _ in pairs])
    if args.get("query"):
        pairs = [(c, s) for c, s in pairs
                 if _matches(args["query"], c.name,
                             getattr(projects.get(c.project_id), "name", None),
                             getattr(projects.get(c.project_id), "customer_name", None))]
    chunk, total, nxt = _page(pairs, args)
    sizings = [_sizing_row(c, s, user, projects.get(c.project_id)) for c, s in chunk]
    out = {"schema_version": SCHEMA_VERSION, "sizings": sizings, "total": total,
           "next_offset": nxt}
    names = "; ".join("%s (id %s%s)" % (
        r["name"], r["id"], (", project " + r["project"]["name"]) if r["project"] else "")
        for r in sizings[:10])
    return out, "%d sizing%s%s" % (total, "" if total == 1 else "s", (": " + names) if names else ".")


# ── get_sizing / open_shared_sizing ──────────────────────────────────────────

def _params_from_fields(fields):
    """The planning parameters a UI-saved payload carries in its DOM-field
    snapshot. Only the controls needed to present the stored option are
    read (ratio, day-one caps); the rest is reported as unknown."""
    fields = fields or {}

    def num(key):
        try:
            return float(fields[key]) if fields.get(key) not in (None, "") else None
        except (TypeError, ValueError):
            return None
    return {"vcpu_ratio": num("ratio-slider"),
            "max_day_one_storage_pct": num("max-day-one-storage"),
            "max_day_one_ram_pct": num("max-day-one-ram"),
            "growth_pct": num("growth-pct"), "snapshot_pct": num("snapshot-pct"),
            "storage_growth_pct": (num("storage-growth-pct")
                                   if fields.get("separate-storage-growth") else None),
            "years": num("growth-years"),
            "license_term_years": num("license-term-years"),
            "guest_licensing": fields.get("guest-licensing") or None}


def present_saved(config, source, user, warnings=None):
    """The ``get_sizing`` body for a visible sizing (§3.9)."""
    from fingerprint import result_state
    refresh_from_db()
    warnings = list(warnings or [])
    summary_row = config.to_summary(user, source)
    sizing = {k: summary_row.get(k) for k in (
        "id", "code", "name", "owner_email", "source", "can_edit", "role", "is_dr_target",
        "has_result", "project_id", "notes", "source_meta", "created_at", "updated_at",
        "result_computed_at")}
    sizing["source_type"] = (config.source_meta or {}).get("file_type") or "manual"
    state = result_state(config)
    payload = config.payload if isinstance(config.payload, dict) else {}
    canonical = payload.get("canonical") if isinstance(payload.get("canonical"), dict) else None
    fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}

    if canonical:
        rerun = {"workload": svc.summary_to_workload(canonical.get("summary") or {}),
                 "params": dict(canonical.get("params") or {})}
        complete = True
    else:
        rerun = {"workload": None, "params": {k: v for k, v in _params_from_fields(fields).items()
                                             if v is not None}}
        complete = False

    presented_params = {
        "vcpu_ratio": rerun["params"].get("vcpu_ratio") or T.default_vcpu_ratio,
        "max_day_one_storage_pct": rerun["params"].get("max_day_one_storage_pct"),
        "max_day_one_ram_pct": rerun["params"].get("max_day_one_ram_pct"),
    }
    clusters = []
    snapshot = config.result_snapshot if isinstance(config.result_snapshot, dict) else {}
    for c in snapshot.get("clusters") or []:
        if not isinstance(c, dict):
            continue
        rec, summary, projection = c.get("recommendation"), c.get("summary"), c.get("projection")
        entry = {"name": c.get("name"), "kind": "proposal" if rec else "hardware_configuration",
                 "inputs": None, "projection": None, "selected_option": None,
                 "hardware_configuration": None}
        if isinstance(summary, dict):
            entry["inputs"] = {"workload": svc.summary_to_workload(summary)}
            if rerun["workload"] is None:
                rerun["workload"] = entry["inputs"]["workload"]
        if isinstance(projection, dict):
            entry["projection"] = presenter.present_projection(projection)
            if not canonical:
                for key in ("years", "growth_pct", "snapshot_pct", "storage_growth_pct"):
                    if projection.get(key) is not None and rerun["params"].get(key) is None:
                        rerun["params"][key] = projection[key]
        if isinstance(rec, dict) and isinstance(summary, dict):
            entry["selected_option"] = presenter.present_option(
                rec, {"projection": projection or {}}, summary, presented_params, rank=None)
        cfg = c.get("config")
        if isinstance(cfg, dict) and not rec:
            entry["hardware_configuration"] = {
                "model": cfg.get("model"), "node_count": cfg.get("node_count"),
                "totals": cfg.get("totals") or cfg.get("cluster_total")}
        clusters.append(entry)

    if any(e["selected_option"] and e["selected_option"].get("licensing") is None for e in clusters):
        warnings.append("No licensing estimate was stored with this result (it was saved before "
                        "the engine produced one); create_sizing with rerun_inputs returns it.")
    if not canonical and rerun["workload"] is not None:
        warnings.append("This sizing was saved from the web tool: the workload comes from its "
                        "stored result and only the planning parameters it recorded are known. "
                        "rerun_inputs is incomplete; the remaining parameters take the defaults.")
    if state.get("cache") == "stale":
        warnings.append("The stored result is stale (the engine, its constants or the catalog "
                        "changed since). Re-run create_sizing with rerun_inputs for current numbers.")
    if state.get("needs_reimport"):
        warnings.append("The assessment parser changed since this sizing was imported; "
                        "re-import the source file in the web tool for corrected figures.")
    out = {"schema_version": SCHEMA_VERSION, "sizing": sizing,
           "result_state": {"cache": state.get("cache"), "needs_reimport": bool(state.get("needs_reimport"))},
           "clusters": clusters, "rerun_inputs": rerun, "rerun_inputs_complete": complete,
           "warnings": warnings}
    top = next((c["selected_option"] for c in clusters if c["selected_option"]), None)
    text = "Sizing %r (id %s, code %s)." % (config.name, config.id, config.code)
    if top:
        text += " Saved option: %s × %s. %s" % (
            presenter._fmt(top["node_count"]), top.get("display_model") or top.get("model"),
            (top.get("determinant") or {}).get("statement") or "")
    elif not clusters:
        text += " No stored result yet."
    text += " Result: %s." % out["result_state"]["cache"]
    for w in warnings:
        text += "\nWarning: " + w
    return out, text


@tool("get_sizing", "Open a saved sizing",
      "Opens one saved sizing by its id (from list_sizings). Returns the saved inputs, the option "
      "the user settled on (with checks), the planning parameters when known, and whether the "
      "stored result is still current. If the result is stale, call create_sizing with "
      "rerun_inputs to see current numbers. To open a sizing by a 12-digit share code someone "
      "gave the user, use open_shared_sizing.",
      {"type": "object", "additionalProperties": False, "required": ["sizing_id"],
       "properties": {"sizing_id": {"type": "integer", "minimum": 1}}}, read_only=True)
def get_sizing(caller, args):
    from auth import _config_source_for
    from auth_models import Configuration
    config = db.session.get(Configuration, args["sizing_id"])
    source = _config_source_for(caller.user, config) if config else None
    if source is None:
        raise ToolError("not_found", "No sizing with id %s is visible to this account."
                        % args["sizing_id"])
    audit_event(caller, "get_sizing", "sizing %d (%s)" % (config.id, source))
    return present_saved(config, source, caller.user)


@tool("open_shared_sizing", "Open a shared sizing by code",
      "Opens a saved sizing by the 12-digit share code someone gave the user. For Scale "
      "Computing staff, opening a partner's code adds that sizing to their own sizing list, "
      "exactly as in the web tool. Other users can open codes only for sizings they could "
      "already see. Returns the same result as get_sizing.",
      {"type": "object", "additionalProperties": False, "required": ["share_code"],
       "properties": {"share_code": {"type": "string", "minLength": 12, "maxLength": 12,
                                     "description": "The 12-digit code shown on the sizing."}}},
      read_only=False, destructive=False, idempotent=True)
def open_shared_sizing(caller, args):
    from auth_models import Configuration, ScaleConfigLink
    from visibility import resolve_by_code
    code = args["share_code"].strip()
    if not code.isdigit():
        raise ToolError("invalid_input", "A share code is 12 digits.",
                        invalid=[{"field": "share_code", "problem": "must be 12 digits"}])
    config = Configuration.query.filter_by(code=code).first()
    source = resolve_by_code(caller.user, config, ScaleConfigLink, "configuration_id")
    if source is None:
        raise ToolError("not_found", "No sizing was found for that code, or it is not "
                        "available to this account.")
    warnings = []
    if source == "linked":
        warnings.append("This sizing is now linked to the user's account and appears in "
                        "their sizing list.")
    audit_event(caller, "open_shared_sizing", "sizing %d (%s)" % (config.id, source))
    return present_saved(config, source, caller.user, warnings)
