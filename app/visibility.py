"""Who may see a saved sizing or a project, and why.

The rules are the same for both (docs/projects-plan.md): a super admin sees
everything, an owner sees their own, a scale user sees every scale tenant's
plus whatever they pulled in by share code (unless that tenant is blocked),
and anyone else sees their own tenant's. Only the link table differs —
``ScaleConfigLink`` for sizings, ``ScaleProjectLink`` for projects — so the
callers pass the link model and the name of its foreign-key column.

Imports models only, so both auth.py and projects.py can use it without a
cycle.
"""
from database import db


def source_for(user, obj, link_model, link_fk):
    """Why (if at all) ``user`` can see ``obj``: 'owned', 'tenant', 'scale',
    'linked', or None. ``link_fk`` names the link model's column that
    points at ``obj`` (``configuration_id`` / ``project_id``)."""
    if obj is None:
        return None
    if obj.is_deleted and not user.is_super_admin:
        return None
    if user.is_super_admin:
        return "owned" if obj.owner_id == user.id else "tenant"
    if obj.owner_id == user.id:
        return "owned"
    if user.is_scale:
        if obj.tenant and obj.tenant.is_scale:
            return "scale"
        link = link_model.query.filter_by(
            user_id=user.id, **{link_fk: obj.id}).first()
        if link and not (obj.tenant and obj.tenant.is_blocked):
            return "linked"
        return None
    if obj.tenant_id == user.tenant_id:
        return "tenant"
    return None


def resolve_by_code(user, obj, link_model, link_fk):
    """The source under which ``user`` opens ``obj`` by its share code, or
    None when the code must look unknown to them.

    Cross-tenant retrieval by code is a scale-user (and super-admin)
    privilege: a scale user who could not see it yet gets a link row (so it
    shows up in their list from now on), unless its tenant is blocked.
    Commits the new link."""
    if obj is None or (obj.is_deleted and not user.is_super_admin):
        return None
    already = source_for(user, obj, link_model, link_fk)
    if already is not None:
        return already
    if user.is_super_admin:
        return "tenant"
    if user.is_scale:
        if obj.tenant and obj.tenant.is_blocked:
            return None
        if not link_model.query.filter_by(user_id=user.id, **{link_fk: obj.id}).first():
            db.session.add(link_model(user_id=user.id, **{link_fk: obj.id}))
            db.session.commit()
        return "linked"
    return None
