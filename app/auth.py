"""Session auth, RBAC decorators, and the account/config/admin blueprints.

Replaces the old global HTTP Basic Auth gate. Login is OPTIONAL — anonymous
requests resolve to ``g.current_user = None`` and all existing sizer routes keep
working. An account only unlocks saving and sharing configurations.

Tenancy = email domain. Roles: user / tenant_admin / super_admin. Scale users
(``@scalecomputing.com``) get cross-tenant config retrieval by code.
"""
import os
import secrets
from datetime import timedelta
from functools import wraps

from flask import Blueprint, jsonify, request, session, g
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash

from database import db
from auth_models import (
    Tenant, User, Configuration, ScaleConfigLink,
    ROLE_USER, ROLE_TENANT_ADMIN, ROLE_SUPER_ADMIN, _utcnow,
)
from email_domains import normalize_email, domain_of, is_public_domain

# pbkdf2 is available everywhere; werkzeug's default (scrypt) needs OpenSSL
# support that some Python builds lack.
PWHASH_METHOD = "pbkdf2:sha256"
RETENTION_DAYS = 90
# Opaque client snapshot — capped to prevent abuse. Generous because an import
# snapshot carries the full per-VM list for large environments.
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
PURGE_MIN_INTERVAL_HOURS = 24
LAST_PURGE_KEY = "auth_last_purge_at"


# ── current_user resolution ──────────────────────────────────────────────────

def load_current_user():
    """before_request hook: populate g.current_user (or None). Never rejects.

    Returns None — i.e. treats the request as anonymous — when the session is
    absent, the user is gone, disabled, or their tenant is blocked. That makes a
    disable/block take effect at the very next request without server-side
    session invalidation.
    """
    g.current_user = None
    uid = session.get("user_id")
    if not uid:
        return
    user = User.query.get(uid)
    if not user or user.is_disabled:
        session.pop("user_id", None)
        return
    if user.tenant and user.tenant.is_blocked:
        session.pop("user_id", None)
        return
    g.current_user = user


def current_user():
    return getattr(g, "current_user", None)


# ── decorators ───────────────────────────────────────────────────────────────

def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if current_user() is None:
            return jsonify({"error": "Authentication required"}), 401
        return fn(*args, **kwargs)
    return wrapper


def super_admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if u is None:
            return jsonify({"error": "Authentication required"}), 401
        if not u.is_super_admin:
            return jsonify({"error": "Super admin access required"}), 403
        return fn(*args, **kwargs)
    return wrapper


def tenant_admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if u is None:
            return jsonify({"error": "Authentication required"}), 401
        if not (u.is_tenant_admin or u.is_super_admin):
            return jsonify({"error": "Tenant admin access required"}), 403
        return fn(*args, **kwargs)
    return wrapper


# ── helpers ──────────────────────────────────────────────────────────────────

def _get_or_create_tenant(domain):
    tenant = Tenant.query.filter_by(domain=domain).first()
    if tenant is None:
        tenant = Tenant(domain=domain, is_scale=Tenant.domain_is_scale(domain))
        db.session.add(tenant)
        db.session.flush()
    return tenant


def _generate_code():
    """A unique-on-insert 12-digit code. Caller relies on the DB unique
    constraint + retry; this only produces a candidate."""
    return "{:012d}".format(secrets.randbelow(10 ** 12))


def _scale_tenant_ids():
    return [t.id for t in Tenant.query.filter_by(is_scale=True).all()]


# ── purge (opportunistic, no scheduler) ──────────────────────────────────────

def purge_expired():
    """Hard-delete soft-deleted configs and disabled users past the retention
    window. Safe to call repeatedly; returns counts."""
    cutoff = _utcnow() - timedelta(days=RETENTION_DAYS)

    stale_configs = Configuration.query.filter(
        Configuration.is_deleted.is_(True),
        Configuration.deleted_at.isnot(None),
        Configuration.deleted_at < cutoff,
    ).all()
    cfg_ids = [c.id for c in stale_configs]
    if cfg_ids:
        ScaleConfigLink.query.filter(
            ScaleConfigLink.configuration_id.in_(cfg_ids)
        ).delete(synchronize_session=False)
    for c in stale_configs:
        db.session.delete(c)

    stale_users = User.query.filter(
        User.is_disabled.is_(True),
        User.disabled_at.isnot(None),
        User.disabled_at < cutoff,
        User.role != ROLE_SUPER_ADMIN,
    ).all()
    for u in stale_users:
        # Detach the user's links (their own configs are independent rows).
        ScaleConfigLink.query.filter_by(user_id=u.id).delete(
            synchronize_session=False)
        db.session.delete(u)

    db.session.commit()
    return {"configs_purged": len(cfg_ids), "users_purged": len(stale_users)}


def maybe_purge():
    """Throttled purge for the login path. Uses a SizingSetting row as a clock
    and a Postgres advisory lock to keep the two gunicorn workers from racing.
    Falls back to a plain throttle on sqlite."""
    from orm_models import SizingSetting
    now = _utcnow()
    row = SizingSetting.query.filter_by(key=LAST_PURGE_KEY).first()
    if row is not None:
        last = float(row.value or 0)
        if (now.timestamp() - last) < PURGE_MIN_INTERVAL_HOURS * 3600:
            return

    is_pg = db.engine.url.get_backend_name() == "postgresql"
    if is_pg:
        got = db.session.execute(
            db.text("SELECT pg_try_advisory_lock(91237)")
        ).scalar()
        if not got:
            return
    try:
        if row is None:
            row = SizingSetting(key=LAST_PURGE_KEY, value=now.timestamp())
            db.session.add(row)
        else:
            row.value = now.timestamp()
        db.session.commit()
        purge_expired()
    finally:
        if is_pg:
            db.session.execute(db.text("SELECT pg_advisory_unlock(91237)"))
            db.session.commit()


# ── auth blueprint ───────────────────────────────────────────────────────────

auth_bp = Blueprint("auth", __name__, url_prefix="/api/auth")


@auth_bp.route("/me")
def me():
    u = current_user()
    return jsonify({"user": u.to_dict() if u else None})


@auth_bp.route("/signup", methods=["POST"])
def signup():
    data = request.json or {}
    email = normalize_email(data.get("email"))
    password = data.get("password") or ""

    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        return jsonify({"error": "A valid email address is required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    domain = domain_of(email)
    if is_public_domain(domain):
        return jsonify({"error": (
            "Public email providers are not allowed. Please sign up with your "
            "organisation's email domain."
        )}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({"error": "An account with this email already exists"}), 409

    tenant = Tenant.query.filter_by(domain=domain).first()
    if tenant and tenant.is_blocked:
        return jsonify({"error": "This domain has been blocked. Contact support."}), 403
    if tenant is None:
        tenant = _get_or_create_tenant(domain)

    # First active registrant of a domain (or a domain left without an active
    # admin) becomes tenant admin.
    is_first_admin = not tenant.active_admins()
    role = ROLE_TENANT_ADMIN if is_first_admin else ROLE_USER

    user = User(
        email=email,
        password_hash=generate_password_hash(password, method=PWHASH_METHOD),
        tenant_id=tenant.id,
        role=role,
    )
    db.session.add(user)
    db.session.commit()

    session["user_id"] = user.id
    g.current_user = user
    return jsonify({
        "user": user.to_dict(),
        "is_tenant_admin": role == ROLE_TENANT_ADMIN,
    }), 201


@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.json or {}
    email = normalize_email(data.get("email"))
    password = data.get("password") or ""

    user = User.query.filter_by(email=email).first()
    if not user or not check_password_hash(user.password_hash, password):
        return jsonify({"error": "Invalid email or password"}), 401
    if user.is_disabled:
        return jsonify({"error": "This account has been disabled"}), 403
    if user.tenant and user.tenant.is_blocked:
        return jsonify({"error": "This domain has been blocked. Contact support."}), 403

    user.last_login_at = _utcnow()
    db.session.commit()
    session["user_id"] = user.id
    g.current_user = user

    try:
        maybe_purge()
    except Exception:
        db.session.rollback()  # purge is best-effort; never block a login

    return jsonify({"user": user.to_dict()})


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.pop("user_id", None)
    g.current_user = None
    return jsonify({"message": "Logged out"})


# ── configurations blueprint ─────────────────────────────────────────────────

configs_bp = Blueprint("configs", __name__, url_prefix="/api/configs")


def _visible_configs(user):
    """Return (Configuration, source) pairs the user may see in listings."""
    if user.is_super_admin:
        rows = Configuration.query.order_by(Configuration.updated_at.desc()).all()
        return [(c, "owned" if c.owner_id == user.id else "tenant") for c in rows]

    pairs = []
    seen = set()

    if user.is_scale:
        scale_ids = _scale_tenant_ids()
        rows = Configuration.query.filter(
            Configuration.tenant_id.in_(scale_ids),
            Configuration.is_deleted.is_(False),
        ).all()
        for c in rows:
            seen.add(c.id)
            pairs.append((c, "owned" if c.owner_id == user.id else "scale"))

        links = ScaleConfigLink.query.filter_by(user_id=user.id).all()
        for link in links:
            c = Configuration.query.get(link.configuration_id)
            if c and not c.is_deleted and c.id not in seen:
                if c.tenant and c.tenant.is_blocked:
                    continue
                seen.add(c.id)
                pairs.append((c, "linked"))
    else:
        rows = Configuration.query.filter(
            Configuration.tenant_id == user.tenant_id,
            Configuration.is_deleted.is_(False),
        ).all()
        for c in rows:
            pairs.append((c, "owned" if c.owner_id == user.id else "tenant"))

    pairs.sort(key=lambda p: p[0].updated_at or p[0].created_at, reverse=True)
    return pairs


def _config_source_for(user, config):
    """Why (if at all) ``user`` can see ``config``. Returns a source tag or None."""
    if config.is_deleted and not user.is_super_admin:
        return None
    if user.is_super_admin:
        return "owned" if config.owner_id == user.id else "tenant"
    if config.owner_id == user.id:
        return "owned"
    if user.is_scale:
        if config.tenant and config.tenant.is_scale:
            return "scale"
        link = ScaleConfigLink.query.filter_by(
            user_id=user.id, configuration_id=config.id).first()
        if link and not (config.tenant and config.tenant.is_blocked):
            return "linked"
        return None
    if config.tenant_id == user.tenant_id:
        return "tenant"
    return None


@configs_bp.route("/", methods=["GET"])
@login_required
def list_configs():
    user = current_user()
    pairs = _visible_configs(user)
    return jsonify([c.to_summary(user, source) for c, source in pairs])


@configs_bp.route("/", methods=["POST"])
@login_required
def create_config():
    user = current_user()
    data = request.json or {}
    name = (data.get("name") or "").strip()
    payload = data.get("payload")

    if not name:
        return jsonify({"error": "A name is required to save a configuration"}), 400
    if payload is None:
        return jsonify({"error": "No configuration payload provided"}), 400
    if len(request.get_data() or b"") > MAX_PAYLOAD_BYTES:
        return jsonify({"error": "Configuration is too large to save"}), 413

    for _ in range(6):
        config = Configuration(
            code=_generate_code(), name=name[:200],
            owner_id=user.id, tenant_id=user.tenant_id, payload=payload,
        )
        db.session.add(config)
        try:
            db.session.commit()
            break
        except IntegrityError:
            db.session.rollback()  # code collision — regenerate and retry
    else:
        return jsonify({"error": "Could not allocate a unique code. Try again."}), 500

    return jsonify(config.to_summary(user, "owned")), 201


@configs_bp.route("/<int:config_id>", methods=["GET"])
@login_required
def get_config(config_id):
    user = current_user()
    config = Configuration.query.get(config_id)
    if config is None:
        return jsonify({"error": "Configuration not found"}), 404
    source = _config_source_for(user, config)
    if source is None:
        return jsonify({"error": "Configuration not found"}), 404  # don't leak
    return jsonify(config.to_dict(user, source))


@configs_bp.route("/code/<code>", methods=["GET"])
@login_required
def get_config_by_code(code):
    user = current_user()
    config = Configuration.query.filter_by(code=(code or "").strip()).first()
    if config is None or (config.is_deleted and not user.is_super_admin):
        return jsonify({"error": "No configuration found for that code"}), 404

    # Cross-tenant retrieval by code is a scale-user (and super admin) privilege.
    already = _config_source_for(user, config)
    if already is not None:
        return jsonify(config.to_dict(user, already))

    if user.is_super_admin:
        return jsonify(config.to_dict(user, "tenant"))

    if user.is_scale:
        if config.tenant and config.tenant.is_blocked:
            return jsonify({"error": "No configuration found for that code"}), 404
        if not ScaleConfigLink.query.filter_by(
                user_id=user.id, configuration_id=config.id).first():
            db.session.add(ScaleConfigLink(
                user_id=user.id, configuration_id=config.id))
            db.session.commit()
        return jsonify(config.to_dict(user, "linked"))

    # Non-scale users cannot pull foreign configs by code.
    return jsonify({"error": "No configuration found for that code"}), 404


@configs_bp.route("/<int:config_id>", methods=["PUT"])
@login_required
def update_config(config_id):
    user = current_user()
    config = Configuration.query.get(config_id)
    if config is None or config.is_deleted:
        return jsonify({"error": "Configuration not found"}), 404
    if config.owner_id != user.id and not user.is_super_admin:
        return jsonify({"error": "Only the owner can edit this configuration"}), 403

    data = request.json or {}
    if "name" in data:
        new_name = (data.get("name") or "").strip()
        if not new_name:
            return jsonify({"error": "Name cannot be empty"}), 400
        config.name = new_name[:200]
    if "payload" in data and data["payload"] is not None:
        if len(request.get_data() or b"") > MAX_PAYLOAD_BYTES:
            return jsonify({"error": "Configuration is too large to save"}), 413
        config.payload = data["payload"]
    db.session.commit()
    return jsonify(config.to_summary(user, "owned"))


@configs_bp.route("/<int:config_id>", methods=["DELETE"])
@login_required
def delete_config(config_id):
    user = current_user()
    config = Configuration.query.get(config_id)
    if config is None:
        return jsonify({"error": "Configuration not found"}), 404

    # Owner / super admin → soft delete (vanishes for all but super admin).
    if config.owner_id == user.id or user.is_super_admin:
        if not config.is_deleted:
            config.is_deleted = True
            config.deleted_at = _utcnow()
            config.deleted_by_user_id = user.id
            db.session.commit()
        return jsonify({"message": "Configuration deleted"})

    # Scale user on a foreign linked config → unlink only.
    if user.is_scale:
        link = ScaleConfigLink.query.filter_by(
            user_id=user.id, configuration_id=config.id).first()
        if link:
            db.session.delete(link)
            db.session.commit()
            return jsonify({"message": "Removed from your list"})

    return jsonify({"error": "You cannot delete this configuration"}), 403


# ── tenant-admin user management ─────────────────────────────────────────────

admin_users_bp = Blueprint("admin_users", __name__, url_prefix="/api/admin/users")


@admin_users_bp.route("/", methods=["GET"])
@tenant_admin_required
def list_org_users():
    user = current_user()
    if user.is_super_admin:
        q = User.query
        tenant_id = request.args.get("tenant", type=int)
        if tenant_id:
            q = q.filter_by(tenant_id=tenant_id)
        if request.args.get("include_disabled") != "true":
            q = q.filter_by(is_disabled=False)
        users = q.order_by(User.email).all()
    else:
        users = User.query.filter_by(
            tenant_id=user.tenant_id, is_disabled=False
        ).order_by(User.email).all()
    return jsonify([u.to_dict() for u in users])


@admin_users_bp.route("/<int:user_id>/disable", methods=["POST"])
@tenant_admin_required
def disable_user(user_id):
    actor = current_user()
    target = User.query.get_or_404(user_id)

    if target.id == actor.id:
        return jsonify({"error": "You cannot disable your own account"}), 403
    if target.is_super_admin:
        return jsonify({"error": "Cannot disable a super admin"}), 403
    if not actor.is_super_admin and target.tenant_id != actor.tenant_id:
        return jsonify({"error": "User is not in your organisation"}), 403

    target.is_disabled = True
    target.disabled_at = _utcnow()
    target.disabled_by_user_id = actor.id
    db.session.commit()
    return jsonify({"message": "User disabled"})


# ── super-admin blueprint ────────────────────────────────────────────────────

super_bp = Blueprint("super", __name__, url_prefix="/api/admin/super")


@super_bp.route("/users", methods=["GET"])
@super_admin_required
def super_list_users():
    q = User.query
    tenant_id = request.args.get("tenant", type=int)
    if tenant_id:
        q = q.filter_by(tenant_id=tenant_id)
    if request.args.get("include_disabled") != "true":
        q = q.filter_by(is_disabled=False)
    return jsonify([u.to_dict() for u in q.order_by(User.email).all()])


@super_bp.route("/users/<int:user_id>/restore", methods=["POST"])
@super_admin_required
def restore_user(user_id):
    target = User.query.get_or_404(user_id)
    target.is_disabled = False
    target.disabled_at = None
    target.disabled_by_user_id = None
    db.session.commit()
    return jsonify({"message": "User restored", "user": target.to_dict()})


@super_bp.route("/users/<int:user_id>", methods=["DELETE"])
@super_admin_required
def hard_delete_user(user_id):
    target = User.query.get_or_404(user_id)
    if not target.is_disabled:
        return jsonify({"error": "Disable the user before deleting"}), 400
    if target.is_super_admin:
        return jsonify({"error": "Cannot delete a super admin"}), 403
    ScaleConfigLink.query.filter_by(user_id=target.id).delete(
        synchronize_session=False)
    db.session.delete(target)
    db.session.commit()
    return jsonify({"message": "User permanently deleted"})


@super_bp.route("/tenants", methods=["GET"])
@super_admin_required
def list_tenants():
    tenants = Tenant.query.order_by(Tenant.domain).all()
    return jsonify([t.to_dict() for t in tenants])


@super_bp.route("/tenants/<int:tenant_id>/admin", methods=["POST"])
@super_admin_required
def reassign_tenant_admin(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    data = request.json or {}
    new_admin = User.query.get_or_404(data.get("user_id"))
    if new_admin.tenant_id != tenant.id:
        return jsonify({"error": "User is not in this tenant"}), 400
    if new_admin.is_disabled:
        return jsonify({"error": "Cannot promote a disabled user"}), 400

    # Single-admin model: demote current active admins, promote the chosen user.
    for u in tenant.active_admins():
        u.role = ROLE_USER
    new_admin.role = ROLE_TENANT_ADMIN
    db.session.commit()
    return jsonify({"message": "Tenant admin reassigned", "user": new_admin.to_dict()})


@super_bp.route("/tenants/<int:tenant_id>/block", methods=["POST"])
@super_admin_required
def block_tenant(tenant_id):
    tenant = Tenant.query.get_or_404(tenant_id)
    actor = current_user()
    blocked = bool((request.json or {}).get("blocked", True))
    tenant.is_blocked = blocked
    tenant.blocked_at = _utcnow() if blocked else None
    tenant.blocked_by_user_id = actor.id if blocked else None
    db.session.commit()
    return jsonify({"message": "Domain blocked" if blocked else "Domain unblocked",
                    "tenant": tenant.to_dict()})


@super_bp.route("/configs", methods=["GET"])
@super_admin_required
def super_list_configs():
    q = Configuration.query
    tenant_id = request.args.get("tenant", type=int)
    if tenant_id:
        q = q.filter_by(tenant_id=tenant_id)
    if request.args.get("include_deleted") != "true":
        q = q.filter_by(is_deleted=False)
    rows = q.order_by(Configuration.updated_at.desc()).all()
    user = current_user()
    return jsonify([c.to_summary(user, "owned" if c.owner_id == user.id else "tenant")
                    for c in rows])


@super_bp.route("/configs/<int:config_id>/purge", methods=["DELETE"])
@super_admin_required
def purge_config(config_id):
    config = Configuration.query.get_or_404(config_id)
    ScaleConfigLink.query.filter_by(configuration_id=config.id).delete(
        synchronize_session=False)
    db.session.delete(config)
    db.session.commit()
    return jsonify({"message": "Configuration permanently deleted"})


@super_bp.route("/purge-run", methods=["POST"])
@super_admin_required
def run_purge():
    return jsonify(purge_expired())


def register_auth(app):
    """Wire the resolver + all auth blueprints into the app."""
    app.before_request(load_current_user)
    app.register_blueprint(auth_bp)
    app.register_blueprint(configs_bp)
    app.register_blueprint(admin_users_bp)
    app.register_blueprint(super_bp)
