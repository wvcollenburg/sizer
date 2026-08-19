"""Project API — the container endpoints for grouping sizings.

Visibility mirrors the saved-sizing rules already in auth.py one level up: you
see your own projects, your tenant's, and (as a scale user) any you pulled in by
code. What differs is *writes*: a project reached by code is read-only, and an
edit copies the sizing into the viewer's own project rather than changing the
original (docs/projects-plan.md decision 22), so a project can never shift under
someone who is presenting it.

Routes here own project membership and the sizing-level operations that only
make sense inside a project (move, duplicate, role, notes, tags). Sizing
save/load itself stays in auth.py's configs blueprint.
"""
from flask import Blueprint, jsonify, request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer

from auth import current_user, login_required
from auth_models import Configuration, ScaleConfigLink, _utcnow
from database import db
from extensions import limiter
from project_models import (
    Project, ProjectTag, ConfigurationTag, ReplicationLink, ScaleProjectLink,
    SIZING_ROLES, ensure_scratch_project, new_code, valid_salesforce_url,
)

projects_bp = Blueprint("projects", __name__, url_prefix="/api/projects")

# Free-text fields a project owner may set. salesforce_url is deliberately NOT
# here — it is scale-only and handled separately (§9.1).
EDITABLE_FIELDS = {
    "name": 200,
    "customer_name": 200,
    "opportunity_ref": 120,
    "prepared_by": 200,
    "description": 4000,
    "lang": 5,
}


# ── visibility ───────────────────────────────────────────────────────────────

def _project_source_for(user, project):
    """Why (if at all) ``user`` can see ``project``. Mirrors auth._config_source_for."""
    if project is None:
        return None
    if project.is_deleted and not user.is_super_admin:
        return None
    if user.is_super_admin:
        return "owned" if project.owner_id == user.id else "tenant"
    if project.owner_id == user.id:
        return "owned"
    if user.is_scale:
        if project.tenant and project.tenant.is_scale:
            return "scale"
        link = ScaleProjectLink.query.filter_by(
            user_id=user.id, project_id=project.id).first()
        if link and not (project.tenant and project.tenant.is_blocked):
            return "linked"
        return None
    if project.tenant_id == user.tenant_id:
        return "tenant"
    return None


def _visible_project(project_id, user):
    """(project, source) for a project the user may see, else (None, None)."""
    project = db.session.get(Project, project_id)
    source = _project_source_for(user, project)
    return (project, source) if source else (None, None)


def _owned_project_or_error(project_id, user):
    """A project the user may WRITE to, or an error response.

    Read-only viewers are refused here rather than in the UI: hiding a button is
    not a permission model, and every new write route must land on this check.
    """
    project, source = _visible_project(project_id, user)
    if project is None:
        return None, (jsonify({"error": "Project not found"}), 404)
    if not project.can_edit(user):
        return None, (jsonify({
            "error": "This project is shared with you read-only. Duplicate a "
                     "sizing into your own project to make changes."
        }), 403)
    return project, None


def _sizing_count(project_id):
    return Configuration.query.filter_by(
        project_id=project_id, is_deleted=False).count()


# ── project CRUD ─────────────────────────────────────────────────────────────

@projects_bp.route("/", methods=["GET"])
@login_required
def list_projects():
    """Projects the user can see.

    Defaults to **their own work**: projects they created, plus any project
    holding a sizing they made — a colleague can own the project while your
    sizing lives in it. ``?scope=tenant`` widens it to everything in the
    organization, which is the opt-in behind the checkbox rather than the
    default; a shared tenant would otherwise bury your own engagements.
    """
    user = current_user()
    scope = (request.args.get("scope") or "mine").lower()
    seen, rows = set(), []

    def add(project, source):
        if project.id in seen:
            return
        seen.add(project.id)
        rows.append((project, source))

    for project in Project.query.filter_by(owner_id=user.id, is_deleted=False).all():
        add(project, "owned")

    # Projects that hold a sizing of mine, whoever owns the project itself.
    contributed = {
        row.project_id for row in Configuration.query.with_entities(
            Configuration.project_id).filter(
                Configuration.owner_id == user.id,
                Configuration.is_deleted.is_(False),
                Configuration.project_id.isnot(None)).distinct()
    }
    for project_id in contributed:
        project = db.session.get(Project, project_id)
        if project and not project.is_deleted:
            source = _project_source_for(user, project)
            if source:
                add(project, source)

    if scope == "tenant" and user.tenant_id:
        for project in Project.query.filter_by(
                tenant_id=user.tenant_id, is_deleted=False).all():
            add(project, _project_source_for(user, project) or "tenant")

    # A project pulled in by code was asked for explicitly, so it belongs in
    # the default view regardless of scope.
    if user.is_scale:
        links = ScaleProjectLink.query.filter_by(user_id=user.id).all()
        for link in links:
            project = db.session.get(Project, link.project_id)
            if project and not project.is_deleted:
                add(project, _project_source_for(user, project) or "linked")

    rows.sort(key=lambda pair: pair[0].updated_at or pair[0].created_at, reverse=True)
    return jsonify([
        p.to_summary(user, source, sizing_count=_sizing_count(p.id))
        for p, source in rows
    ])


@projects_bp.route("/", methods=["POST"])
@login_required
def create_project():
    """Creation asks for a name and nothing else (decision 24). Optional detail
    is filled in later from project settings, so a partner is never required to
    hand over customer information to use the tool."""
    user = current_user()
    data = request.json or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "A project name is required"}), 400

    from i18n import SUPPORTED_LANGS
    lang = (data.get("lang") or "").strip().lower()
    if lang and lang not in SUPPORTED_LANGS:
        return jsonify({"error": "Unsupported export language"}), 400

    for _ in range(6):
        project = Project(
            code=new_code(), name=name[:200],
            owner_id=user.id, tenant_id=user.tenant_id,
            # The language the project was created in — the default for its
            # exports, changeable later in Details.
            lang=(lang or None),
            # Whoever creates the project is who prepared it, by default. Stored
            # rather than derived at render time, so it stays correct after the
            # creator changes their own name — and so it can simply be edited
            # when a colleague presents the work.
            prepared_by=((data.get("prepared_by") or "").strip()
                         or user.display_name)[:200],
        )
        field_err = _apply_optional_fields(project, data, user)
        if field_err:
            # e.g. a malformed Salesforce URL. Returning 201 while silently
            # dropping the value would report success for a field that wasn't
            # saved.
            db.session.rollback()
            return field_err
        # _apply_optional_fields blanks a field sent empty; a project should
        # still open with its creator's name in place.
        if not project.prepared_by:
            project.prepared_by = user.display_name[:200]
        db.session.add(project)
        try:
            db.session.commit()
            break
        except IntegrityError:      # code collision — regenerate and retry
            db.session.rollback()
    else:
        return jsonify({"error": "Could not allocate a unique code. Try again."}), 500

    return jsonify(project.to_summary(user, "owned", sizing_count=0)), 201


@projects_bp.route("/<int:project_id>", methods=["GET"])
@login_required
def get_project(project_id):
    user = current_user()
    project, source = _visible_project(project_id, user)
    if project is None:
        return jsonify({"error": "Project not found"}), 404

    # Don't drag every payload across for a listing: they hold whole VM lists
    # (up to MAX_PAYLOAD_BYTES each), and none of it is used here. The result
    # snapshot is needed for the state badge; payload_digest exists precisely so
    # replication staleness can be checked without the payload itself.
    sizings = Configuration.query.filter_by(
        project_id=project.id, is_deleted=False).options(
            defer(Configuration.payload)).order_by(
                Configuration.position, Configuration.id).all()
    tag_map = _tags_by_configuration([s.id for s in sizings])

    # One tunables read for the whole project rather than one per sizing.
    from fingerprint import tunables_digest
    tunables = tunables_digest()

    rows = []
    for sizing in sizings:
        row = sizing.to_summary(user, "owned" if sizing.owner_id == user.id else source)
        row["tags"] = tag_map.get(sizing.id, [])
        row.update(_result_state(sizing, tunables))
        rows.append(row)

    payload = project.to_dict(user, source, sizings=rows)
    names = {s.id: s.name for s in sizings}
    payload["replication_links"] = [
        link.to_dict(names) for link in ReplicationLink.query.filter_by(
            project_id=project.id).order_by(
                ReplicationLink.source_configuration_id).all()
    ]
    return jsonify(payload)


@projects_bp.route("/<int:project_id>", methods=["PUT"])
@login_required
def update_project(project_id):
    user = current_user()
    project, err = _owned_project_or_error(project_id, user)
    if err:
        return err

    data = request.json or {}
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Name cannot be empty"}), 400
        project.name = name[:200]
    if "default_role" in data:
        role = data.get("default_role")
        if role is not None and role not in SIZING_ROLES:
            return jsonify({"error": "Unknown role"}), 400
        project.default_role = role

    field_err = _apply_optional_fields(project, data, user)
    if field_err:
        return field_err

    db.session.commit()
    return jsonify(project.to_summary(user, "owned",
                                      sizing_count=_sizing_count(project.id)))


def _apply_optional_fields(project, data, user):
    """Copy the optional metadata fields onto ``project``.

    The Salesforce link is dropped silently for non-scale users — not rejected
    with an error, which would itself disclose that the field exists.
    """
    for field, limit in EDITABLE_FIELDS.items():
        if field == "name" or field not in data:
            continue
        value = data.get(field)
        value = (value or "").strip()[:limit] or None
        if field == "lang":
            # This is handed straight to the export translator, so an unknown
            # code would silently produce an English document rather than an
            # error. Reject it instead of storing it.
            from i18n import SUPPORTED_LANGS
            if value is not None and value not in SUPPORTED_LANGS:
                return jsonify({"error": "Unsupported export language"}), 400
        setattr(project, field, value)

    if "salesforce_url" in data and Project.may_see_salesforce(user):
        url = (data.get("salesforce_url") or "").strip()
        if not url:
            project.salesforce_url = None
        elif valid_salesforce_url(url):
            project.salesforce_url = url
        else:
            return jsonify({
                "error": "The opportunity link must be an https URL on a "
                         "Salesforce domain."
            }), 400
    return None


@projects_bp.route("/<int:project_id>", methods=["DELETE"])
@login_required
def delete_project(project_id):
    """Soft-delete the project and its sizings together (decision 15), so the
    existing recovery, audit and purge paths apply unchanged."""
    user = current_user()
    project, err = _owned_project_or_error(project_id, user)
    if err:
        return err

    now = _utcnow()
    if not project.is_deleted:
        project.is_deleted = True
        project.deleted_at = now
        project.deleted_by_user_id = user.id
    sizings = Configuration.query.filter_by(
        project_id=project.id, is_deleted=False).all()
    for sizing in sizings:
        sizing.is_deleted = True
        sizing.deleted_at = now
        sizing.deleted_by_user_id = user.id
    db.session.commit()
    return jsonify({"message": "Project deleted", "sizings_deleted": len(sizings)})


@projects_bp.route("/code/<code>", methods=["GET"])
@login_required
@limiter.limit("30 per minute")
def get_project_by_code(code):
    """Open a project by its share code. Cross-tenant retrieval stays a scale-user
    (and super-admin) privilege, as it is for single sizings."""
    user = current_user()
    project = Project.query.filter_by(code=(code or "").strip()).first()
    if project is None or (project.is_deleted and not user.is_super_admin):
        return jsonify({"error": "No project found for that code"}), 404

    already = _project_source_for(user, project)
    if already is not None:
        return jsonify(project.to_summary(
            user, already, sizing_count=_sizing_count(project.id)))

    if user.is_super_admin:
        return jsonify(project.to_summary(
            user, "tenant", sizing_count=_sizing_count(project.id)))

    if user.is_scale:
        if project.tenant and project.tenant.is_blocked:
            return jsonify({"error": "No project found for that code"}), 404
        if not ScaleProjectLink.query.filter_by(
                user_id=user.id, project_id=project.id).first():
            db.session.add(ScaleProjectLink(user_id=user.id, project_id=project.id))
            db.session.commit()
        return jsonify(project.to_summary(
            user, "linked", sizing_count=_sizing_count(project.id)))

    return jsonify({"error": "No project found for that code"}), 404


@projects_bp.route("/<int:project_id>/source-check", methods=["POST"])
@login_required
def check_source_duplicate(project_id):
    """Has this exact file already been imported into this project? (§8)

    Answers on the file digest, not the name — the same Live Optics export is
    routinely re-downloaded under a different filename, and two genuinely
    different customers' exports are often called the same thing.
    """
    user = current_user()
    project, source = _visible_project(project_id, user)
    if project is None:
        return jsonify({"error": "Project not found"}), 404

    digest = ((request.json or {}).get("file_sha256") or "").strip()
    if not digest:
        return jsonify({"duplicate": False})

    for sizing in Configuration.query.filter_by(
            project_id=project.id, is_deleted=False).options(
                defer(Configuration.payload)).all():
        meta = sizing.source_meta or {}
        if meta.get("file_sha256") == digest:
            return jsonify({
                "duplicate": True,
                "sizing_id": sizing.id,
                "sizing_name": sizing.name,
                "file_name": meta.get("file_name"),
                "imported_at": meta.get("imported_at"),
            })
    return jsonify({"duplicate": False})


@projects_bp.route("/scratch", methods=["POST"])
@login_required
def get_or_create_scratch():
    """The quick-sizing path: a sizing still belongs to a project, it just isn't
    one the user had to name first (decision 2)."""
    user = current_user()
    project = ensure_scratch_project(user.id, user.tenant_id)
    return jsonify(project.to_summary(user, "owned",
                                      sizing_count=_sizing_count(project.id)))


# ── sizing operations within a project ───────────────────────────────────────

sizings_bp = Blueprint("sizings", __name__, url_prefix="/api/sizings")


def _writable_sizing(config_id, user):
    """(sizing, None) when the user may modify it, else (None, error response)."""
    sizing = db.session.get(Configuration, config_id)
    if sizing is None or sizing.is_deleted:
        return None, (jsonify({"error": "Sizing not found"}), 404)
    if sizing.owner_id != user.id and not user.is_super_admin:
        return None, (jsonify({
            "error": "This sizing belongs to someone else. Duplicate it into "
                     "your own project to make changes."
        }), 403)
    return sizing, None


@sizings_bp.route("/<int:config_id>/duplicate", methods=["POST"])
@login_required
def duplicate_sizing(config_id):
    """Clone a sizing — the fastest way to build "Option 2 is Option 1 with RF3".

    A viewer duplicating someone else's sizing gets a copy in their own scratch
    project; provenance and the cached result come along, the share code does
    not (the copy is a new sizing).
    """
    user = current_user()
    source = db.session.get(Configuration, config_id)
    if source is None or source.is_deleted:
        return jsonify({"error": "Sizing not found"}), 404

    from auth import _config_source_for
    if _config_source_for(user, source) is None:
        return jsonify({"error": "Sizing not found"}), 404

    data = request.json or {}
    target_id = data.get("project_id")
    if target_id and source.owner_id == user.id:
        target, err = _owned_project_or_error(target_id, user)
        if err:
            return err
    elif source.owner_id == user.id and source.project_id:
        target, err = _owned_project_or_error(source.project_id, user)
        if err:      # own sizing sitting in a project one can't write: fall back
            target = ensure_scratch_project(user.id, user.tenant_id)
    else:
        target = ensure_scratch_project(user.id, user.tenant_id)

    last = Configuration.query.filter_by(
        project_id=target.id, is_deleted=False).count()

    for _ in range(6):
        copy = Configuration(
            code=new_code(),
            name=(data.get("name") or f"{source.name} (copy)")[:200],
            owner_id=user.id, tenant_id=user.tenant_id,
            payload=source.payload,
            project_id=target.id, position=last,
            role=source.role, notes=source.notes,
            source_meta=source.source_meta,
            result_snapshot=source.result_snapshot,
            result_fingerprint=source.result_fingerprint,
            result_computed_at=source.result_computed_at,
            parser_version=source.parser_version,
        )
        db.session.add(copy)
        try:
            db.session.commit()
            break
        except IntegrityError:
            db.session.rollback()
    else:
        return jsonify({"error": "Could not allocate a unique code. Try again."}), 500

    return jsonify(copy.to_summary(user, "owned")), 201


@sizings_bp.route("/<int:config_id>/result", methods=["PUT"])
@login_required
def store_sizing_result(config_id):
    """Store a freshly calculated result for a sizing (§4).

    Deliberately allowed for **anyone who can see the sizing**, not only its
    owner. Refresh is the only path to current numbers, so gating it on
    ownership would leave a read-only viewer permanently unable to compare or
    export a shared project full of stale sizings.

    It is not an edit: only the result and its refs are accepted — never
    payload, name, tags or role — and the server derives the fingerprint
    itself rather than trusting one from the client.
    """
    from auth import _config_source_for
    from fingerprint import (PARSER_VERSION, fingerprint_snapshot,
                             inbound_replication_digest)

    user = current_user()
    sizing = db.session.get(Configuration, config_id)
    if sizing is None or sizing.is_deleted or _config_source_for(user, sizing) is None:
        return jsonify({"error": "Sizing not found"}), 404

    data = request.json or {}
    clusters = data.get("clusters")
    if not isinstance(clusters, list) or not clusters:
        return jsonify({"error": "A result must carry at least one cluster"}), 400

    from fingerprint import tunables_digest
    # Stamped so the comparison can say "these were sized under different
    # assumptions" — the check decision 16 asks for, warn-only.
    snapshot = {"clusters": clusters, "totals": data.get("totals"),
                "tunables": tunables_digest()}
    fingerprint = fingerprint_snapshot(
        snapshot, replication=inbound_replication_digest(sizing.id))
    if not fingerprint:
        return jsonify({"error": "Result is missing the refs needed to "
                                 "validate it later"}), 400

    sizing.result_snapshot = snapshot
    sizing.result_fingerprint = fingerprint
    sizing.result_computed_at = _utcnow()
    # Only sizings that came from a file have a parser version to be out of date
    # against. Stamping one on a manual or appliance sizing would flag it
    # "re-import needed" the next time a parser changes — for a file that never
    # existed, and which the refresh loop then skips forever (§3.3).
    if sizing.parser_version is None and (sizing.source_meta or {}).get("file_sha256"):
        sizing.parser_version = PARSER_VERSION
    db.session.commit()

    row = sizing.to_summary(user, "owned" if sizing.owner_id == user.id else "tenant")
    row.update(_result_state(sizing))
    return jsonify(row)


def _result_state(sizing, tunables=None):
    from fingerprint import result_state
    return result_state(sizing, tunables)


@sizings_bp.route("/<int:config_id>/move", methods=["POST"])
@login_required
def move_sizing(config_id):
    user = current_user()
    sizing, err = _writable_sizing(config_id, user)
    if err:
        return err
    target, err = _owned_project_or_error((request.json or {}).get("project_id"), user)
    if err:
        return err

    # Moving out is a deletion as far as its replication partners are concerned:
    # links may not span projects, so they would have to be dropped, silently
    # stripping the inbound reserve those partners were sized for.
    from auth import _replication_dependents
    blockers = _replication_dependents(sizing.id)
    if blockers:
        return jsonify({
            "error": "Other sizings replicate to this one. Remove those "
                     "replication links before moving it.",
            "replicated_from": blockers,
        }), 409
    # Its own outbound links can't survive the move either.
    ReplicationLink.query.filter_by(source_configuration_id=sizing.id).delete()

    sizing.project_id = target.id
    sizing.position = Configuration.query.filter_by(
        project_id=target.id, is_deleted=False).count()
    # Tags are project-scoped, so they cannot follow the sizing to another
    # project — drop the links rather than leave them pointing at a stranger's
    # vocabulary.
    ConfigurationTag.query.filter_by(configuration_id=sizing.id).delete()
    db.session.commit()
    return jsonify(sizing.to_summary(user, "owned"))


# ── comparison (§6) ──────────────────────────────────────────────────────────

def _metrics_from_snapshot(snapshot):
    """Flatten a stored result into the columns the comparison table shows.

    A sizing holding several clusters has no single node count, so its clusters
    are summed into one row and carried as sub-rows — mixing a three-cluster
    sizing into a flat table as if it were one appliance would misstate every
    column.
    """
    clusters = (snapshot or {}).get("clusters") or []
    rows, total = [], {
        "nodes": 0, "cores": 0, "ram_gb": 0, "usable_tb": 0.0,
        "n1_cores": 0, "n1_ram_gb": 0, "rack_units": 0,
        # Physical HyperCore clusters the customer ends up running. Not the same
        # as the number of sections: one source cluster still splits into
        # several output clusters above max_nodes_per_cluster (§0), so this is
        # summed from num_clusters rather than counted from the list.
        "clusters": 0,
    }
    models, layout = [], []
    for cluster in clusters:
        rec = cluster.get("recommendation") or cluster.get("config") or {}
        # Two shapes reach here: a recommendation from /api/recommend calls its
        # cluster figures "totals", while an appliance/validated calculation
        # calls them "cluster_total". Reading only one silently yields zeros.
        totals = rec.get("totals") or rec.get("cluster_total") or {}
        n1 = rec.get("n_minus_1") or {}
        row = {
            "name": cluster.get("name"),
            "model": rec.get("model"),
            "nodes": rec.get("node_count") or rec.get("total_node_count") or 0,
            "clusters": rec.get("num_clusters") or (1 if rec else 0),
            "cores": totals.get("cores") or 0,
            "ram_gb": totals.get("ram_gb") or 0,
            "usable_tb": totals.get("usable_storage_tb") or 0,
            "n1_cores": n1.get("cores") or 0,
            "n1_ram_gb": n1.get("ram_gb") or 0,
        }
        if rec.get("model"):
            models.append(rec["model"])
        layout.extend(rec.get("cluster_layout") or [])
        for key in ("nodes", "clusters", "cores", "ram_gb", "n1_cores", "n1_ram_gb"):
            total[key] += row[key] or 0
        total["usable_tb"] += float(row["usable_tb"] or 0)
        rows.append(row)

    total["usable_tb"] = round(total["usable_tb"], 2)
    total["model"] = ", ".join(sorted(set(models))) if models else None
    # e.g. "8 + 5" — how those nodes actually divide, so a cluster count of 2
    # can be read as the split it represents.
    total["layout"] = layout
    return total, rows


@projects_bp.route("/<int:project_id>/compare", methods=["POST"])
@login_required
def compare_sizings(project_id):
    """Side-by-side figures for the selected sizings, plus the caveats that make
    a comparison invalid if ignored (§6)."""
    user = current_user()
    project, source = _visible_project(project_id, user)
    if project is None:
        return jsonify({"error": "Project not found"}), 404

    wanted = (request.json or {}).get("sizing_ids") or []
    sizings = Configuration.query.filter(
        Configuration.project_id == project.id,
        Configuration.is_deleted.is_(False),
        Configuration.id.in_(wanted or [-1])).order_by(
            Configuration.position, Configuration.id).all()

    from fingerprint import result_state, tunables_digest
    tunables = tunables_digest()

    rows, warnings, seen_tunables = [], [], set()
    for sizing in sizings:
        state = result_state(sizing, tunables)
        totals, clusters = _metrics_from_snapshot(sizing.result_snapshot)
        rows.append({
            "id": sizing.id, "name": sizing.name, "role": sizing.role,
            "notes": sizing.notes, "is_dr_target": sizing.is_dr_target,
            "totals": totals, "clusters": clusters,
            "stale": state["stale"], "cache": state["cache"],
            "needs_reimport": state["needs_reimport"],
        })
        stamp = (sizing.result_snapshot or {}).get("tunables")
        if stamp:
            seen_tunables.add(stamp)
        if state["cache"] == "none":
            warnings.append({"code": "not_sized", "name": sizing.name})
        elif state["stale"]:
            warnings.append({"code": "stale", "name": sizing.name})
        if state["needs_reimport"]:
            warnings.append({"code": "reimport", "name": sizing.name})

    # Comparing options sized under different assumptions is invalid; say so
    # rather than enforce it (decision 16 — warn only).
    if len(seen_tunables) > 1:
        warnings.append({"code": "mixed_tunables"})

    roles = {r["role"] for r in rows}
    if "additive" in roles and "alternative" in roles:
        warnings.append({"code": "mixed_roles"})

    # Only additive sizings are summed: adding Option 1 to Option 2 would
    # invent a cluster nobody is buying (decision 3).
    additive = [r for r in rows if r["role"] == "additive"]
    rollup = None
    if additive:
        rollup = {key: sum(r["totals"][key] for r in additive)
                  for key in ("nodes", "clusters", "cores", "ram_gb",
                              "n1_cores", "n1_ram_gb")}
        rollup["usable_tb"] = round(
            sum(float(r["totals"]["usable_tb"]) for r in additive), 2)
        rollup["count"] = len(additive)

    return jsonify({"rows": rows, "warnings": warnings, "rollup": rollup})


# ── replication partners (§8.5) ──────────────────────────────────────────────

REPLICATION_MODES = ("reserved", "failover")


def _pct(value, default=100):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(100, number))


@sizings_bp.route("/<int:config_id>/replication", methods=["POST"])
@login_required
def set_replication_link(config_id):
    """Point one of this sizing's clusters at a cluster in another sizing.

    Both ends must sit in the same project (decision 29): a link across
    projects would pull one customer's demand into another's sizing. Mutual
    links are allowed on purpose — the reserve is computed from each side's
    *demand*, never from the other's sized result, so A<->B is well-defined
    rather than circular.
    """
    user = current_user()
    sizing, err = _writable_sizing(config_id, user)
    if err:
        return err

    data = request.json or {}
    target_id = data.get("target_configuration_id")
    target = db.session.get(Configuration, target_id) if target_id else None
    if target is None or target.is_deleted:
        return jsonify({"error": "Replication target not found"}), 404
    if target.project_id != sizing.project_id:
        return jsonify({
            "error": "A replication partner must be in the same project."}), 400
    if target.id == sizing.id and (data.get("target_cluster") or "") == \
            (data.get("source_cluster") or ""):
        return jsonify({"error": "A cluster cannot replicate to itself."}), 400

    mode = (data.get("mode") or "reserved").lower()
    if mode not in REPLICATION_MODES:
        return jsonify({"error": "Unknown replication mode"}), 400

    source_cluster = (data.get("source_cluster") or "")[:120]
    link = ReplicationLink.query.filter_by(
        source_configuration_id=sizing.id, source_cluster=source_cluster).first()
    if link is None:
        link = ReplicationLink(project_id=sizing.project_id,
                               source_configuration_id=sizing.id,
                               source_cluster=source_cluster)
        db.session.add(link)

    link.target_configuration_id = target.id
    link.target_cluster = (data.get("target_cluster") or "")[:120]
    link.compute_pct = _pct(data.get("compute_pct"))
    link.storage_pct = _pct(data.get("storage_pct"))
    link.mode = mode
    db.session.commit()

    return jsonify(link.to_dict(_sizing_names(sizing.project_id))), 201


@sizings_bp.route("/<int:config_id>/replication", methods=["DELETE"])
@login_required
def clear_replication_link(config_id):
    user = current_user()
    sizing, err = _writable_sizing(config_id, user)
    if err:
        return err
    source_cluster = (request.args.get("source_cluster") or "")[:120]
    ReplicationLink.query.filter_by(
        source_configuration_id=sizing.id,
        source_cluster=source_cluster).delete()
    db.session.commit()
    return jsonify({"message": "Replication link removed"})


def _sizing_names(project_id):
    return {c.id: c.name for c in Configuration.query.filter_by(
        project_id=project_id).all()}


@projects_bp.route("/<int:project_id>/dr-target", methods=["POST"])
@login_required
def create_dr_target(project_id):
    """Add a sizing that carries no workload of its own and exists purely as a
    replication target (decision 30) — the DR site you have no Live Optics for.
    It is sized from what replicates into it."""
    user = current_user()
    project, err = _owned_project_or_error(project_id, user)
    if err:
        return err

    name = ((request.json or {}).get("name") or "DR target").strip()[:200]
    position = Configuration.query.filter_by(
        project_id=project.id, is_deleted=False).count()

    for _ in range(6):
        sizing = Configuration(
            code=new_code(), name=name,
            owner_id=user.id, tenant_id=user.tenant_id,
            payload={"mode": "dr_target"},
            project_id=project.id, position=position,
            role=project.default_role, is_dr_target=True,
        )
        db.session.add(sizing)
        try:
            db.session.commit()
            break
        except IntegrityError:
            db.session.rollback()
    else:
        return jsonify({"error": "Could not allocate a unique code. Try again."}), 500

    return jsonify(sizing.to_summary(user, "owned")), 201


# ── DR-target sizing (workload-less, sized from inbound replication) ──────────

def _demand_of_cluster(cluster):
    """Day-one demand (vCPU / RAM GB / used TB) a stored result cluster carries.
    Read from the cluster's summary — the input demand, not its sized result, so
    a target never depends on another sizing's output (§8.5)."""
    s = (cluster or {}).get("summary") or {}
    return {
        "vcpus": s.get("total_vcpus", 0) or 0,
        "ram_gb": s.get("total_vm_provisioned_memory_gb", 0) or 0,
        "storage_tb": s.get("datastore_used_tb", 0) or 0,
    }


def _source_cluster_demand(source, cluster_name):
    """Demand of one source cluster (by name) within a source sizing's stored
    result. An empty/blank name — a single-cluster source — aggregates every
    cluster the source holds."""
    clusters = ((source.result_snapshot or {}).get("clusters")) or []
    for c in clusters:
        if (c.get("name") or "") == (cluster_name or ""):
            return _demand_of_cluster(c)
    if not (cluster_name or "").strip():
        agg = {"vcpus": 0, "ram_gb": 0, "storage_tb": 0}
        for c in clusters:
            d = _demand_of_cluster(c)
            for k in agg:
                agg[k] += d[k]
        return agg
    return {"vcpus": 0, "ram_gb": 0, "storage_tb": 0}


def _dr_inbound_reserve(dr_sizing):
    """Aggregate the inbound replication reserve a DR target must host.

    Returns (reserve, sources, size_full_cluster):
      * reserve — day-one {vcpus, ram_gb, storage_tb} summed over every sizing
        that replicates INTO this target, each scaled by its link's percentages;
      * sources — per-link breakdown for the UI;
      * size_full_cluster — False when any inbound link is held at N-1
        ("reserved"), True only when they are all "failover" (replicas need to
        fit only with all nodes up). This maps the link mode onto the engine's
        own-workload N-1 vs full-cluster basis.
    """
    from project_models import ReplicationLink
    links = ReplicationLink.query.filter_by(
        target_configuration_id=dr_sizing.id).order_by(
            ReplicationLink.source_configuration_id,
            ReplicationLink.source_cluster).all()
    reserve = {"vcpus": 0.0, "ram_gb": 0.0, "storage_tb": 0.0}
    sources, modes = [], set()
    if not links:
        return reserve, sources, False

    src_ids = [l.source_configuration_id for l in links]
    src_map = {c.id: c for c in Configuration.query.filter(
        Configuration.id.in_(src_ids)).all()}
    for link in links:
        src = src_map.get(link.source_configuration_id)
        if src is None or src.is_deleted:
            continue
        demand = _source_cluster_demand(src, link.source_cluster)
        v = demand["vcpus"] * (link.compute_pct or 0) / 100.0
        r = demand["ram_gb"] * (link.compute_pct or 0) / 100.0
        s = demand["storage_tb"] * (link.storage_pct or 0) / 100.0
        reserve["vcpus"] += v
        reserve["ram_gb"] += r
        reserve["storage_tb"] += s
        modes.add(link.mode)
        sources.append({
            "sizing_name": src.name,
            "cluster": link.source_cluster,
            "vcpus": round(v), "ram_gb": round(r), "storage_tb": round(s, 2),
            "compute_pct": link.compute_pct, "storage_pct": link.storage_pct,
            "mode": link.mode,
            "sized": bool((src.result_snapshot or {}).get("clusters")),
        })
    # Reserved (N-1) is the conservative default; only go full-cluster when every
    # inbound link is failover-only.
    size_full_cluster = bool(modes) and "reserved" not in modes
    return reserve, sources, size_full_cluster


def _dr_demand_summary(reserve):
    """A summary dict standing in for the DR target's 'own' workload — the
    inbound reserve. Shaped like a parser summary (so the engine and the exporter
    read the fields they expect), with the reserve as demand and everything
    measured-but-absent (perf, IOPS, per-VM maxima) left at 0."""
    return {
        "host_count": 0,
        "cluster_name": "",
        "current_platform": "DR replication reserve",
        "source_cpus": [],
        "total_host_cores": 0, "total_host_threads": 0, "total_host_ghz": 0,
        "total_host_ram_gb": 0, "per_host_cores": 0, "per_host_ram_gb": 0,
        "total_vms": 0, "active_vms": 0,
        "total_vcpus": round(reserve["vcpus"]),
        "total_vm_provisioned_memory_gb": round(reserve["ram_gb"], 1),
        "total_vm_used_memory_gb": round(reserve["ram_gb"], 1),
        "total_vm_provisioned_storage_gb": round(reserve["storage_tb"] * 1024, 1),
        "total_vm_used_storage_gb": round(reserve["storage_tb"] * 1024, 1),
        "total_vm_provisioned_storage_tb": round(reserve["storage_tb"], 2),
        "total_vm_used_storage_tb": round(reserve["storage_tb"], 2),
        "datastore_total_tb": round(reserve["storage_tb"], 2),
        "datastore_used_tb": round(reserve["storage_tb"], 2),
        "local_total_tb": 0, "local_used_tb": 0, "local_used_gb": 0,
        "peak_cpu_pct": 0, "avg_cpu_pct": 0, "peak_cpu_ghz": 0, "avg_cpu_ghz": 0,
        "peak_mem_pct": 0, "avg_mem_pct": 0,
        "total_peak_iops": 0, "total_avg_iops": 0, "p95_iops": 0,
        "nic_speed_mbps": 0,
        "vcpu_per_core_ratio": 0, "vcpu_per_thread_ratio": 0,
        "max_vm_ram_gb": 0, "max_vm_cores": 0,
    }


@sizings_bp.route("/<int:config_id>/dr-recommend", methods=["POST"])
@login_required
def dr_recommend(config_id):
    """Size a workload-less DR target from its inbound replication reserve.

    The reserve (Σ of what replicates in, per §8.5) is treated as the target's
    own demand and run through the normal engine, so the projection, utilization
    and recommendation cards all come out coherent. Available to anyone who can
    see the sizing (not only its owner) — the same rule as storing a result — so
    a shared project's DR target can still be sized and compared.
    """
    from auth import _config_source_for
    from recommend import generate_recommendations

    user = current_user()
    sizing = db.session.get(Configuration, config_id)
    if sizing is None or sizing.is_deleted or _config_source_for(user, sizing) is None:
        return jsonify({"error": "Sizing not found"}), 404
    if not sizing.is_dr_target:
        return jsonify({"error": "Not a DR target"}), 400

    reserve, sources, size_full_cluster = _dr_inbound_reserve(sizing)
    summary = _dr_demand_summary(reserve)

    data = request.json or {}

    def _num(key, default):
        try:
            return float(data.get(key, default))
        except (TypeError, ValueError):
            return default

    from i18n import SUPPORTED_LANGS  # noqa: F401 (kept parallel to other routes)
    sizing_mode = "validated" if data.get("sizing_mode") == "validated" else "certified"
    target_model = (data.get("target_model") or "").strip() or None

    has_reserve = any(reserve[k] > 0 for k in reserve)
    if not has_reserve:
        # Nothing replicates in yet — there is nothing to size. Report the empty
        # reserve so the UI can prompt the user to point a source at this target.
        return jsonify({"reserve": {k: round(v, 2) for k, v in reserve.items()},
                        "sources": sources, "recommendations": [],
                        "projection": None, "size_full_cluster": size_full_cluster,
                        "warnings": [{"code": "dr_no_inbound"}]})

    result = generate_recommendations(
        summary,
        vcpu_ratio=_num("vcpu_ratio", None) if data.get("vcpu_ratio") is not None else None,
        growth_pct=_num("growth_pct", 10),
        snapshot_pct=_num("snapshot_pct", 20),
        years=int(_num("years", 5)),
        storage_pref=data.get("storage_pref"),
        size_full_cluster=size_full_cluster,
        sizing_mode=sizing_mode,
        allow_storage_only=bool(data.get("allow_storage_only")),
        target_model=target_model,
        include_eol_eos=bool(data.get("include_eol_eos")),
        allow_single_node=bool(data.get("allow_single_node")),
    )
    return jsonify({
        "reserve": {k: round(v, 2) for k, v in reserve.items()},
        "sources": sources,
        "size_full_cluster": size_full_cluster,
        "recommendations": result["recommendations"],
        "projection": result["projection"],
        "warnings": result.get("warnings", []),
        # Handed back so the client stores exactly the summary the engine sized
        # against — the DR reserve as the "current environment" in exports.
        "summary": summary,
    })


@sizings_bp.route("/<int:config_id>/role", methods=["POST"])
@login_required
def set_sizing_role(config_id):
    """Alternative (never summed) vs additive (part of one estate) — the flag the
    rollup depends on (decision 3)."""
    user = current_user()
    sizing, err = _writable_sizing(config_id, user)
    if err:
        return err
    role = (request.json or {}).get("role")
    if role is not None and role not in SIZING_ROLES:
        return jsonify({"error": "Unknown role"}), 400
    sizing.role = role
    db.session.commit()
    return jsonify(sizing.to_summary(user, "owned"))


@sizings_bp.route("/<int:config_id>/notes", methods=["POST"])
@login_required
def set_sizing_notes(config_id):
    user = current_user()
    sizing, err = _writable_sizing(config_id, user)
    if err:
        return err
    notes = (request.json or {}).get("notes")
    sizing.notes = ((notes or "").strip()[:4000]) or None
    db.session.commit()
    return jsonify(sizing.to_summary(user, "owned"))


@sizings_bp.route("/reorder", methods=["POST"])
@login_required
def reorder_sizings():
    """Persist the project view's ordering — the exported bundle follows
    ``position``, so dragging Option 1 above Option 2 must survive (§7.1)."""
    user = current_user()
    data = request.json or {}
    project, err = _owned_project_or_error(data.get("project_id"), user)
    if err:
        return err

    order = data.get("sizing_ids") or []
    if not isinstance(order, list):
        return jsonify({"error": "sizing_ids must be a list"}), 400
    rows = {c.id: c for c in Configuration.query.filter_by(
        project_id=project.id, is_deleted=False).all()}
    for position, sizing_id in enumerate(order):
        row = rows.get(sizing_id)
        if row is not None:
            row.position = position
    db.session.commit()
    return jsonify({"message": "Order saved", "count": len(order)})


# ── tags ─────────────────────────────────────────────────────────────────────

def _tags_by_configuration(config_ids):
    if not config_ids:
        return {}
    links = ConfigurationTag.query.filter(
        ConfigurationTag.configuration_id.in_(config_ids)).all()
    tag_ids = {link.tag_id for link in links}
    tags = {t.id: t for t in ProjectTag.query.filter(ProjectTag.id.in_(tag_ids)).all()}
    out = {}
    for link in links:
        tag = tags.get(link.tag_id)
        if tag:
            out.setdefault(link.configuration_id, []).append(tag.to_dict())
    for rows in out.values():
        rows.sort(key=lambda t: t["name"].lower())
    return out


@projects_bp.route("/<int:project_id>/tags", methods=["POST"])
@login_required
def create_tag(project_id):
    user = current_user()
    project, err = _owned_project_or_error(project_id, user)
    if err:
        return err
    data = request.json or {}
    name = (data.get("name") or "").strip()[:60]
    if not name:
        return jsonify({"error": "A tag name is required"}), 400

    existing = ProjectTag.query.filter_by(project_id=project.id, name=name).first()
    if existing:
        return jsonify(existing.to_dict())
    tag = ProjectTag(project_id=project.id, name=name,
                     color=(data.get("color") or None))
    db.session.add(tag)
    db.session.commit()
    return jsonify(tag.to_dict()), 201


@projects_bp.route("/<int:project_id>/tags/<int:tag_id>", methods=["DELETE"])
@login_required
def delete_tag(project_id, tag_id):
    user = current_user()
    project, err = _owned_project_or_error(project_id, user)
    if err:
        return err
    tag = ProjectTag.query.filter_by(id=tag_id, project_id=project.id).first()
    if tag is None:
        return jsonify({"error": "Tag not found"}), 404
    ConfigurationTag.query.filter_by(tag_id=tag.id).delete()
    db.session.delete(tag)
    db.session.commit()
    return jsonify({"message": "Tag deleted"})


@sizings_bp.route("/<int:config_id>/tags", methods=["POST"])
@login_required
def set_sizing_tags(config_id):
    """Replace a sizing's tags with ``tag_ids``. Tags must belong to the
    sizing's own project — project-scoped vocabulary (decision 10)."""
    user = current_user()
    sizing, err = _writable_sizing(config_id, user)
    if err:
        return err
    tag_ids = (request.json or {}).get("tag_ids") or []
    if not isinstance(tag_ids, list):
        return jsonify({"error": "tag_ids must be a list"}), 400

    valid = {t.id for t in ProjectTag.query.filter(
        ProjectTag.project_id == sizing.project_id,
        ProjectTag.id.in_(tag_ids or [-1])).all()}
    ConfigurationTag.query.filter_by(configuration_id=sizing.id).delete()
    for tag_id in valid:
        db.session.add(ConfigurationTag(configuration_id=sizing.id, tag_id=tag_id))
    db.session.commit()
    row = sizing.to_summary(user, "owned")
    row["tags"] = _tags_by_configuration([sizing.id]).get(sizing.id, [])
    return jsonify(row)


# ── bundle exports (§7.2) ────────────────────────────────────────────────────

export_jobs_bp = Blueprint("export_jobs", __name__, url_prefix="/api/export-jobs")

EXPORT_FORMATS = ("pptx", "docx", "pdf", "presentation-pdf")


@projects_bp.route("/<int:project_id>/export", methods=["POST"])
@login_required
def queue_export(project_id):
    """Queue a bundle of the selected sizings.

    Returns immediately with a job id: the build is CPU-heavy and the PDF
    variants shell out to LibreOffice, so a ten-sizing bundle would otherwise
    hold a request open for minutes (§7.2).
    """
    from app import MAX_EXPORT_SECTIONS
    from project_models import ExportJob, JOB_QUEUED

    user = current_user()
    project, source = _visible_project(project_id, user)
    if project is None:
        return jsonify({"error": "Project not found"}), 404

    data = request.json or {}
    fmt = (data.get("format") or "pptx").lower()
    if fmt not in EXPORT_FORMATS:
        return jsonify({"error": "Unknown export format"}), 400

    # Editable source files stay a scale-user privilege, exactly as they are for
    # single-sizing exports.
    if fmt in ("pptx", "docx") and not (user.is_scale or user.is_super_admin):
        return jsonify({"error": "The editable PowerPoint and Word files are "
                                 "available to Scale users only. Use a PDF."}), 403

    wanted = data.get("sizing_ids") or []
    sizings = Configuration.query.filter(
        Configuration.project_id == project.id,
        Configuration.is_deleted.is_(False),
        Configuration.id.in_(wanted or [-1])).all()
    if not sizings:
        return jsonify({"error": "Select at least one sizing to export"}), 400

    # The limit counts flattened sections, and is enforced at selection time so
    # the user is told before the job runs rather than after it fails.
    sections = sum(len(((s.result_snapshot or {}).get("clusters")) or []) or 1
                   for s in sizings)
    if sections > MAX_EXPORT_SECTIONS:
        return jsonify({
            "error": f"That selection is {sections} sections; the limit for one "
                     f"document is {MAX_EXPORT_SECTIONS}. Export it in parts."
        }), 400

    job = ExportJob(
        user_id=user.id, project_id=project.id, fmt=fmt, status=JOB_QUEUED,
        sizing_ids=[s.id for s in sizings],
        # The project remembers the language it was created in; the caller may
        # override, but the request never silently inherits the session's.
        lang=(data.get("lang") or project.lang or "en"),
        notify_email=bool(data.get("notify_email")),
    )
    db.session.add(job)
    db.session.commit()
    return jsonify(job.to_dict()), 202


@projects_bp.route("/<int:project_id>/exports", methods=["GET"])
@login_required
def list_exports(project_id):
    """Recent jobs for this project. Exists regardless of email: a job runs
    server-side whether or not anyone is watching, and without this panel a user
    who navigates away has no route back to a finished bundle."""
    from project_models import ExportJob

    user = current_user()
    project, source = _visible_project(project_id, user)
    if project is None:
        return jsonify({"error": "Project not found"}), 404
    jobs = ExportJob.query.filter_by(
        project_id=project.id, user_id=user.id).order_by(
            ExportJob.created_at.desc()).limit(10).all()
    return jsonify([j.to_dict() for j in jobs])


@export_jobs_bp.route("/<int:job_id>", methods=["GET"])
@login_required
def get_export_job(job_id):
    from project_models import ExportJob
    user = current_user()
    job = db.session.get(ExportJob, job_id)
    if job is None or (job.user_id != user.id and not user.is_super_admin):
        return jsonify({"error": "Export not found"}), 404
    return jsonify(job.to_dict())


@export_jobs_bp.route("/<int:job_id>/file", methods=["GET"])
@login_required
def download_export(job_id):
    """Hand over the artifact.

    Ownership is checked rather than relying on an unguessable id — a bundle is
    customer sizing data, not a public download. Expiry is checked here too: the
    daily sweep can leave a file on disk up to a day past its deadline, so the
    file's presence is not proof it is still offered (§7.2).
    """
    import os as _os

    from flask import send_file

    from project_models import ExportJob, JOB_DONE

    user = current_user()
    job = db.session.get(ExportJob, job_id)
    if job is None or (job.user_id != user.id and not user.is_super_admin):
        return jsonify({"error": "Export not found"}), 404
    if job.status != JOB_DONE or not job.artifact_path:
        return jsonify({"error": "That export is not ready yet."}), 409
    if job.is_expired():
        return jsonify({"error": "That export has expired. Generate it again "
                                 "from the project."}), 410
    if not _os.path.exists(job.artifact_path):
        return jsonify({"error": "That export is no longer available. Generate "
                                 "it again from the project."}), 410
    return send_file(job.artifact_path, as_attachment=True,
                     download_name=job.filename or "export")


def register_projects(app):
    app.register_blueprint(projects_bp)
    app.register_blueprint(sizings_bp)
    app.register_blueprint(export_jobs_bp)
