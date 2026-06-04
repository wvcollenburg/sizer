"""Session auth, RBAC decorators, and the account/config/admin blueprints.

Replaces the old global HTTP Basic Auth gate. Login is OPTIONAL — anonymous
requests resolve to ``g.current_user = None`` and all existing sizer routes keep
working. An account only unlocks saving and sharing configurations.

Tenancy = email domain. Roles: user / tenant_admin / super_admin. Scale users
(``@scalecomputing.com``) get cross-tenant config retrieval by code.
"""
import os
import re
import time
import threading
import smtplib
import secrets
from datetime import timedelta, timezone
from email.message import EmailMessage
from functools import wraps

from flask import Blueprint, jsonify, request, session, g, redirect
from sqlalchemy import or_, and_
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash

from database import db
from auth_models import (
    Tenant, User, Configuration, ScaleConfigLink, AppSetting, AdminAuditLog,
    PiiErasure, ROLE_USER, ROLE_TENANT_ADMIN, ROLE_SUPER_ADMIN, _utcnow,
)
from email_domains import normalize_email, domain_of, is_public_domain

# pbkdf2 is available everywhere; werkzeug's default (scrypt) needs OpenSSL
# support that some Python builds lack.
PWHASH_METHOD = "pbkdf2:sha256"

# Password policy. "Special" = any non-alphanumeric character (matches the
# frontend's /[^A-Za-z0-9]/ so client and server agree).
PASSWORD_MIN_LENGTH = 10


def validate_password(pw):
    """Return an error message if the password fails policy, else None."""
    pw = pw or ""
    if len(pw) < PASSWORD_MIN_LENGTH:
        return "Password must be at least {} characters".format(PASSWORD_MIN_LENGTH)
    if not re.search(r"[A-Z]", pw):
        return "Password must contain an uppercase letter"
    if not re.search(r"[a-z]", pw):
        return "Password must contain a lowercase letter"
    if not re.search(r"[0-9]", pw):
        return "Password must contain a number"
    if not re.search(r"[^A-Za-z0-9]", pw):
        return "Password must contain a special character"
    return None
RETENTION_DAYS = 90
# Opaque client snapshot — capped to prevent abuse. Generous because an import
# snapshot carries the full per-VM list for large environments.
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
PURGE_MIN_INTERVAL_HOURS = 24
LAST_PURGE_KEY = "auth_last_purge_at"

# Daily maintenance scheduler (retention purge + GDPR PII anonymization). Runs
# at most once per calendar day, regardless of traffic, so date-bound erasure
# happens on the due date even with no logins.
DAILY_RUN_KEY = "last_daily_maintenance"
DAILY_ADVISORY_LOCK = 91238
_scheduler_started = False

# Brute-force lockout: after this many consecutive failures, lock the account
# for the cooldown window. A successful login resets the counter.
LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES = 15

# Password-reset link validity.
RESET_TOKEN_TTL_HOURS = 2

# A user is "inactive" once it's been this long since their last successful login
# (or since signup, if they never logged in).
STALE_DAYS = 365

# GDPR: how long after a user is deleted before their PII is scrubbed from the
# audit log, and the placeholder it's replaced with.
PII_RETENTION_DAYS = 365
ANON_LABEL = "[anonymized]"

# String settings keys (auth_models.AppSetting).
SMTP_KEYS = ["smtp_host", "smtp_port", "smtp_username", "smtp_password",
             "smtp_from", "smtp_use_tls", "verify_email_enabled"]


# ── app settings (string key/value) ──────────────────────────────────────────

def get_setting(key, default=None):
    row = AppSetting.query.filter_by(key=key).first()
    return row.value if row and row.value is not None else default


def set_setting(key, value):
    row = AppSetting.query.filter_by(key=key).first()
    if row is None:
        row = AppSetting(key=key, value=value)
        db.session.add(row)
    else:
        row.value = value


def _setting_bool(key):
    return (get_setting(key, "false") or "false").strip().lower() in ("true", "1", "yes")


def smtp_configured():
    """SMTP is usable when at least a host and a from-address are set."""
    return bool(get_setting("smtp_host") and get_setting("smtp_from"))


def verification_active():
    """Email verification is enforced only when SMTP is configured AND the admin
    has turned the toggle on."""
    return smtp_configured() and _setting_bool("verify_email_enabled")


# ── email ────────────────────────────────────────────────────────────────────

def _smtp_send(msg):
    """Open a connection to the configured SMTP server and send ``msg``.
    Raises on failure."""
    host = get_setting("smtp_host")
    port = int(get_setting("smtp_port", "587") or "587")
    username = get_setting("smtp_username")
    password = get_setting("smtp_password")
    use_tls = _setting_bool("smtp_use_tls")
    # Port 465 is implicit TLS (SMTPS); 587/25 use optional STARTTLS.
    use_ssl = port == 465

    cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    with cls(host, port, timeout=20) as server:
        server.ehlo()
        if use_tls and not use_ssl:
            server.starttls()
            server.ehlo()
        if username:
            server.login(username, password or "")
        refused = server.send_message(msg)
    if refused:
        raise smtplib.SMTPRecipientsRefused(refused)


def _build_message(to_addr, subject, body):
    msg = EmailMessage()
    sender = get_setting("smtp_from") or get_setting("smtp_username")
    msg["From"] = sender
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def send_email(to_addr, subject, body):
    """Send a plain-text email via the configured SMTP server. Raises on failure."""
    _smtp_send(_build_message(to_addr, subject, body))


def send_verification_email(user, base_url):
    """Issue a fresh token and email a verification link. Returns True on success."""
    token = secrets.token_urlsafe(32)[:64]
    user.verification_token = token
    user.verification_sent_at = _utcnow()
    db.session.commit()
    link = "{}api/auth/verify/{}".format(base_url, token)
    try:
        send_email(
            user.email, "Verify your SC// Sizer account",
            "Welcome to the SC// Infrastructure Sizer.\n\n"
            "Please confirm your email address by opening this link:\n\n"
            "{}\n\n"
            "If you did not create this account, you can ignore this email.".format(link),
        )
        return True
    except Exception:  # noqa: BLE001 — caller decides how to surface failures
        return False


def send_reset_email(user, base_url):
    """Issue a fresh reset token and email a reset link. Returns True on success."""
    token = secrets.token_urlsafe(32)[:64]
    user.reset_token = token
    user.reset_sent_at = _utcnow()
    db.session.commit()
    link = "{}?reset={}".format(base_url, token)
    try:
        send_email(
            user.email, "Reset your SC// Sizer password",
            "A password reset was requested for your SC// Infrastructure Sizer "
            "account.\n\nOpen this link to choose a new password (valid for {} "
            "hours):\n\n{}\n\nIf you did not request this, you can ignore this "
            "email — your password will not change.".format(RESET_TOKEN_TTL_HOURS, link),
        )
        return True
    except Exception:  # noqa: BLE001
        return False


# ── audit log ────────────────────────────────────────────────────────────────

def audit(action, detail, actor=None):
    """Record an action in the audit log. ``actor`` defaults to the current user;
    pass it explicitly for self-service events (signup/activation) where the
    subject is the actor and there is no logged-in user. Does not commit — the
    caller's commit persists it alongside the change being logged."""
    if actor is None:
        actor = current_user()
    db.session.add(AdminAuditLog(
        actor_id=actor.id if actor else None,
        actor_email=actor.email if actor else None,
        action=action, detail=detail,
    ))


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


# Endpoints reachable without an account: the auth API, the app shell (so the
# login UI can load), and static assets. Everything else requires login.
def require_login():
    """Global gate: an account is required for all operations. Runs after
    load_current_user. API calls get a 401; page requests are sent to the shell
    (which shows the mandatory login modal)."""
    if request.method == "OPTIONS":
        return None
    if request.endpoint == "static":
        return None
    if request.blueprint == "auth":
        return None
    if request.path in ("/", "/privacy"):
        return None
    if current_user() is not None:
        return None
    if "/api/" in request.path:
        return jsonify({"error": "Authentication required"}), 401
    return redirect("/")


def _aware(dt):
    """Coerce a possibly-naive datetime (e.g. read back from SQLite) to aware
    UTC so it can be compared with _utcnow(). On Postgres TIMESTAMPTZ values are
    already aware and pass through unchanged."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


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
        _delete_user_cascade(u)

    db.session.commit()

    tenants_removed = _cleanup_empty_tenants()
    db.session.commit()
    pii_anonymized = anonymize_expired_pii()
    return {"configs_purged": len(cfg_ids), "users_purged": len(stale_users),
            "tenants_removed": tenants_removed, "pii_anonymized": pii_anonymized}


def anonymize_expired_pii():
    """GDPR: scrub deleted users' PII from the audit log PII_RETENTION_DAYS after
    deletion. Replaces the email in both ``actor_email`` and ``detail`` with an
    anonymized placeholder, then drops the erasure marker. The action history is
    preserved — only the personal identifier is removed."""
    cutoff = _utcnow() - timedelta(days=PII_RETENTION_DAYS)
    due = PiiErasure.query.filter(PiiErasure.deleted_at < cutoff).all()
    for rec in due:
        email = rec.email
        AdminAuditLog.query.filter(AdminAuditLog.actor_email == email).update(
            {"actor_email": ANON_LABEL}, synchronize_session=False)
        # detail may embed the email as a substring; autoescape protects against
        # an email's own LIKE metacharacters (e.g. an underscore).
        for row in AdminAuditLog.query.filter(
                AdminAuditLog.detail.contains(email, autoescape=True)).all():
            if row.detail:
                row.detail = row.detail.replace(email, ANON_LABEL)
        db.session.delete(rec)
    db.session.commit()
    return len(due)


def _delete_user_cascade(user):
    """Hard-delete a user and everything that references them. Owned
    configurations (owner_id is NOT NULL) and scale links are removed; audit/
    actor back-references are NULLed so the audit trail survives the delete
    without violating foreign keys. A GDPR erasure marker is recorded so the
    user's PII is scrubbed from the audit log after PII_RETENTION_DAYS."""
    email = user.email

    owned = Configuration.query.filter_by(owner_id=user.id).all()
    owned_ids = [c.id for c in owned]
    if owned_ids:
        ScaleConfigLink.query.filter(
            ScaleConfigLink.configuration_id.in_(owned_ids)
        ).delete(synchronize_session=False)
    for c in owned:
        db.session.delete(c)
    ScaleConfigLink.query.filter_by(user_id=user.id).delete(
        synchronize_session=False)

    # Break inbound references (all nullable) so the hard delete is FK-safe while
    # keeping history. The audit row's actor_email is retained until the GDPR
    # scrub; only the link to the (now-deleted) user id is dropped.
    AdminAuditLog.query.filter_by(actor_id=user.id).update(
        {"actor_id": None}, synchronize_session=False)
    User.query.filter_by(disabled_by_user_id=user.id).update(
        {"disabled_by_user_id": None}, synchronize_session=False)
    Tenant.query.filter_by(blocked_by_user_id=user.id).update(
        {"blocked_by_user_id": None}, synchronize_session=False)
    Configuration.query.filter_by(deleted_by_user_id=user.id).update(
        {"deleted_by_user_id": None}, synchronize_session=False)

    db.session.add(PiiErasure(email=email))
    db.session.delete(user)


def _cleanup_empty_tenants():
    """Remove tenants that have no remaining users. Blocked domains are kept so a
    block can't be escaped by deleting all its accounts (a fresh signup would
    otherwise recreate the domain unblocked)."""
    removed = 0
    for t in Tenant.query.filter_by(is_blocked=False).all():
        if not t.users and not Configuration.query.filter_by(tenant_id=t.id).first():
            db.session.delete(t)
            removed += 1
    return removed


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


def run_daily_maintenance(app):
    """Run the retention/anonymization sweep at most once per calendar day.
    Coordinated across gunicorn workers by a Postgres advisory lock plus a date
    stamp, so exactly one worker runs it per day. Idempotent and safe to call
    often; it no-ops until the date rolls over."""
    with app.app_context():
        today = _utcnow().date().isoformat()
        if get_setting(DAILY_RUN_KEY) == today:
            return None

        is_pg = db.engine.url.get_backend_name() == "postgresql"
        if is_pg:
            got = db.session.execute(
                db.text("SELECT pg_try_advisory_lock(:k)"), {"k": DAILY_ADVISORY_LOCK}
            ).scalar()
            if not got:
                return None
        try:
            # Re-check under the lock — another worker may have just run it.
            if get_setting(DAILY_RUN_KEY) == today:
                return None
            set_setting(DAILY_RUN_KEY, today)
            db.session.commit()
            result = purge_expired()
            app.logger.info("Daily maintenance ran: %s", result)
            return result
        finally:
            if is_pg:
                db.session.execute(
                    db.text("SELECT pg_advisory_unlock(:k)"), {"k": DAILY_ADVISORY_LOCK})
                db.session.commit()


def start_scheduler(app):
    """Start the once-per-day maintenance loop in a daemon thread. Ticks
    frequently but the day-stamp gate means real work happens once per calendar
    day; the first tick after boot catches up anything past-due (e.g. if the
    container was down on the due date). Safe with sync gunicorn workers (one
    thread per worker, all gated). Do not use with `--preload`."""
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    interval = int(os.environ.get("MAINTENANCE_INTERVAL_SECONDS", "3600") or "3600")

    def loop():
        # Small initial delay so boot/seed settles before the first sweep.
        time.sleep(15)
        while True:
            try:
                run_daily_maintenance(app)
            except Exception as e:  # noqa: BLE001 — never let the loop die
                try:
                    db.session.rollback()
                except Exception:
                    pass
                app.logger.warning("Daily maintenance failed: %s", e)
            time.sleep(interval)

    threading.Thread(target=loop, daemon=True, name="daily-maintenance").start()
    app.logger.info("Daily maintenance scheduler started (every %ss).", interval)


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
    pw_error = validate_password(password)
    if pw_error:
        return jsonify({"error": pw_error}), 400
    if not data.get("accept_privacy"):
        return jsonify({"error": "You must accept the privacy policy to sign up."}), 400

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

    needs_verification = verification_active()
    user = User(
        email=email,
        password_hash=generate_password_hash(password, method=PWHASH_METHOD),
        tenant_id=tenant.id,
        role=role,
        is_verified=not needs_verification,
        privacy_accepted_at=_utcnow(),
    )
    db.session.add(user)
    db.session.commit()
    audit("signup", "{} as {}{}".format(
        user.email, role, " (pending verification)" if needs_verification else ""),
        actor=user)
    db.session.commit()

    if needs_verification:
        sent = send_verification_email(user, request.url_root)
        # Do not log them in until verified.
        return jsonify({
            "pending_verification": True,
            "email": user.email,
            "email_sent": sent,
        }), 201

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

    # Lockout check first — applies even on correct passwords during cooldown.
    locked_until = _aware(user.locked_until) if user else None
    if locked_until and _utcnow() < locked_until:
        mins = max(1, int((locked_until - _utcnow()).total_seconds() // 60) + 1)
        return jsonify({"error": (
            "Too many failed attempts. Try again in about {} minute(s).".format(mins)
        )}), 429

    if not user or not check_password_hash(user.password_hash, password):
        if user:
            user.failed_login_count = (user.failed_login_count or 0) + 1
            if user.failed_login_count >= LOCKOUT_THRESHOLD:
                user.locked_until = _utcnow() + timedelta(minutes=LOCKOUT_MINUTES)
                user.failed_login_count = 0
            db.session.commit()
        return jsonify({"error": "Invalid email or password"}), 401

    if user.is_disabled:
        return jsonify({"error": "This account has been disabled"}), 403
    if user.tenant and user.tenant.is_blocked:
        return jsonify({"error": "This domain has been blocked. Contact support."}), 403
    if verification_active() and not user.is_verified:
        return jsonify({
            "error": "Please verify your email address before signing in.",
            "needs_verification": True,
        }), 403

    user.failed_login_count = 0
    user.locked_until = None
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


@auth_bp.route("/verify/<token>")
def verify_email(token):
    user = User.query.filter_by(verification_token=(token or "").strip()).first()
    if user is None:
        return redirect("/?verify=invalid")
    user.is_verified = True
    user.verification_token = None
    audit("activate", user.email, actor=user)
    db.session.commit()
    return redirect("/?verify=ok")


@auth_bp.route("/resend-verification", methods=["POST"])
def resend_verification():
    # Always returns a generic success so it can't be used to probe for accounts.
    email = normalize_email((request.json or {}).get("email"))
    if email and verification_active():
        user = User.query.filter_by(email=email).first()
        if user and not user.is_verified and not user.is_disabled:
            send_verification_email(user, request.url_root)
    return jsonify({"message":
                    "If that account exists and needs verification, an email is on its way."})


@auth_bp.route("/forgot-password", methods=["POST"])
def forgot_password():
    # Generic response regardless of outcome — never reveal whether an account
    # exists. Only does anything when SMTP is configured.
    email = normalize_email((request.json or {}).get("email"))
    generic = jsonify({"message":
                       "If that account exists, a password reset email has been sent."})
    if not email or not smtp_configured():
        return generic
    user = User.query.filter_by(email=email).first()
    if user and not user.is_disabled and not (user.tenant and user.tenant.is_blocked):
        send_reset_email(user, request.url_root)
        audit("forgot_password", "{} requested a reset link".format(user.email),
              actor=user)
        db.session.commit()
    return generic


@auth_bp.route("/reset-password", methods=["POST"])
def reset_password():
    data = request.json or {}
    token = (data.get("token") or "").strip()
    password = data.get("password") or ""
    pw_error = validate_password(password)
    if pw_error:
        return jsonify({"error": pw_error}), 400

    user = User.query.filter_by(reset_token=token).first() if token else None
    if user is None:
        return jsonify({"error": "This reset link is invalid or has already been used."}), 400
    sent = _aware(user.reset_sent_at)
    if not sent or (_utcnow() - sent) > timedelta(hours=RESET_TOKEN_TTL_HOURS):
        return jsonify({"error": "This reset link has expired. Request a new one."}), 400

    user.password_hash = generate_password_hash(password, method=PWHASH_METHOD)
    user.reset_token = None
    user.reset_sent_at = None
    user.failed_login_count = 0
    user.locked_until = None
    audit("password_changed", "{} via reset link".format(user.email), actor=user)
    db.session.commit()
    return jsonify({"message": "Your password has been reset. You can now sign in."})


# ── configurations blueprint ─────────────────────────────────────────────────

configs_bp = Blueprint("configs", __name__, url_prefix="/api/configs")


def _visible_configs(user):
    """Return (Configuration, source) pairs the user may see in listings."""
    if user.is_super_admin:
        # The My Sizings list is the working list, not the audit view — hide
        # soft-deleted configs here. They remain visible (and purgeable) in the
        # admin portal's "show deleted" view.
        rows = Configuration.query.filter(
            Configuration.is_deleted.is_(False)
        ).order_by(Configuration.updated_at.desc()).all()
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
    audit("disable_user", "{} ({})".format(target.email, target.domain))
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
    if request.args.get("unverified") == "true":
        q = q.filter_by(is_verified=False)
    return jsonify([u.to_dict() for u in q.order_by(User.email).all()])


@super_bp.route("/users/<int:user_id>/restore", methods=["POST"])
@super_admin_required
def restore_user(user_id):
    target = User.query.get_or_404(user_id)
    target.is_disabled = False
    target.disabled_at = None
    target.disabled_by_user_id = None
    audit("restore_user", "{} ({})".format(target.email, target.domain))
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
    email, domain = target.email, target.domain
    _delete_user_cascade(target)
    audit("delete_user", "{} ({})".format(email, domain))
    db.session.commit()
    _cleanup_empty_tenants()
    db.session.commit()
    return jsonify({"message": "User permanently deleted"})


def _active_super_admin_count():
    return User.query.filter_by(role=ROLE_SUPER_ADMIN, is_disabled=False).count()


@super_bp.route("/users/<int:user_id>/role", methods=["POST"])
@super_admin_required
def change_user_role(user_id):
    target = User.query.get_or_404(user_id)
    new_role = (request.json or {}).get("role")
    if new_role not in (ROLE_USER, ROLE_TENANT_ADMIN, ROLE_SUPER_ADMIN):
        return jsonify({"error": "Invalid role"}), 400
    if target.is_disabled:
        return jsonify({"error": "Restore the user before changing their role"}), 400
    # Never strip the last active super admin (would lock everyone out of admin).
    if (target.is_super_admin and new_role != ROLE_SUPER_ADMIN
            and _active_super_admin_count() <= 1):
        return jsonify({"error": "Cannot demote the last super admin"}), 400

    old_role = target.role
    target.role = new_role
    audit("change_role", "{}: {} -> {}".format(target.email, old_role, new_role))
    db.session.commit()
    return jsonify({"message": "Role updated", "user": target.to_dict()})


@super_bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
@super_admin_required
def super_reset_password(user_id):
    target = User.query.get_or_404(user_id)
    password = (request.json or {}).get("password") or ""

    # With a password supplied, set it directly. Without one, email a reset link
    # (requires SMTP) so the user chooses their own.
    if password:
        pw_error = validate_password(password)
        if pw_error:
            return jsonify({"error": pw_error}), 400
        target.password_hash = generate_password_hash(password, method=PWHASH_METHOD)
        target.failed_login_count = 0
        target.locked_until = None
        target.reset_token = None
        audit("reset_password", "{} (set directly)".format(target.email))
        db.session.commit()
        return jsonify({"message": "Password reset for {}.".format(target.email)})

    if not smtp_configured():
        return jsonify({"error":
                        "Provide a new password, or configure SMTP to email a reset link."}), 400
    sent = send_reset_email(target, request.url_root)
    audit("reset_password_email", target.email)
    db.session.commit()
    return jsonify({"message": ("Reset link sent to {}.".format(target.email) if sent
                                else "Could not send the reset email — check SMTP settings.")})


@super_bp.route("/users/stale", methods=["GET"])
@super_admin_required
def stale_users():
    """Active, non-super-admin users who haven't logged in for STALE_DAYS — or
    who never logged in and signed up that long ago."""
    cutoff = _utcnow() - timedelta(days=STALE_DAYS)
    q = User.query.filter(
        User.role != ROLE_SUPER_ADMIN,
        User.is_disabled.is_(False),
        or_(
            and_(User.last_login_at.isnot(None), User.last_login_at < cutoff),
            and_(User.last_login_at.is_(None), User.created_at < cutoff),
        ),
    ).order_by(User.tenant_id, User.email)
    return jsonify([u.to_dict() for u in q.all()])


@super_bp.route("/users/purge", methods=["POST"])
@super_admin_required
def purge_users():
    """Bulk hard-delete users by id (used by the inactive-users tab). Skips super
    admins and the acting user. Cascades each user's configs/links."""
    ids = (request.json or {}).get("ids") or []
    actor = current_user()
    purged = skipped = 0
    for uid in ids:
        u = User.query.get(uid)
        if u is None or u.is_super_admin or (actor and u.id == actor.id):
            skipped += 1
            continue
        audit("purge_user", "{} ({})".format(u.email, u.domain))
        _delete_user_cascade(u)
        purged += 1
    db.session.commit()
    _cleanup_empty_tenants()
    db.session.commit()
    return jsonify({"purged": purged, "skipped": skipped})


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
    audit("reassign_tenant_admin", "{} -> {}".format(tenant.domain, new_admin.email))
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
    audit("block_domain" if blocked else "unblock_domain", tenant.domain)
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
    audit("purge_config", "{} (code {})".format(config.name, config.code))
    ScaleConfigLink.query.filter_by(configuration_id=config.id).delete(
        synchronize_session=False)
    db.session.delete(config)
    db.session.commit()
    return jsonify({"message": "Configuration permanently deleted"})


@super_bp.route("/purge-run", methods=["POST"])
@super_admin_required
def run_purge():
    result = purge_expired()
    audit("purge_run", "configs={configs_purged} users={users_purged} "
          "tenants={tenants_removed}".format(**result))
    db.session.commit()
    return jsonify(result)


# ── Email / SMTP settings ─────────────────────────────────────────────────────

@super_bp.route("/email-settings", methods=["GET"])
@super_admin_required
def get_email_settings():
    # Never return the stored password — only whether one is set.
    return jsonify({
        "smtp_host": get_setting("smtp_host", ""),
        "smtp_port": get_setting("smtp_port", "587"),
        "smtp_username": get_setting("smtp_username", ""),
        "smtp_password_set": bool(get_setting("smtp_password")),
        "smtp_from": get_setting("smtp_from", ""),
        "smtp_use_tls": _setting_bool("smtp_use_tls"),
        "verify_email_enabled": _setting_bool("verify_email_enabled"),
        "configured": smtp_configured(),
        "verification_active": verification_active(),
    })


@super_bp.route("/email-settings", methods=["PUT"])
@super_admin_required
def update_email_settings():
    data = request.json or {}
    for key in ["smtp_host", "smtp_port", "smtp_username", "smtp_from"]:
        if key in data:
            set_setting(key, str(data[key]).strip())
    for key in ["smtp_use_tls", "verify_email_enabled"]:
        if key in data:
            set_setting(key, "true" if data[key] else "false")
    # Only overwrite the password when a non-empty new value is supplied.
    if data.get("smtp_password"):
        set_setting("smtp_password", str(data["smtp_password"]))
    if data.get("clear_password"):
        set_setting("smtp_password", "")
    audit("update_email_settings", "verify={}".format(
        "true" if (data.get("verify_email_enabled")) else "false"))
    db.session.commit()
    return get_email_settings()


@super_bp.route("/email-settings/test", methods=["POST"])
@super_admin_required
def test_email_settings():
    to = normalize_email((request.json or {}).get("to")) or current_user().email
    if not smtp_configured():
        return jsonify({"error": "Set an SMTP host and from-address first."}), 400
    msg = _build_message(
        to, "SC// Sizer test email",
        "This is a test message from the SC// Infrastructure Sizer. "
        "If you received it, SMTP is configured correctly.")
    try:
        _smtp_send(msg)
        return jsonify({
            "message": "The SMTP server accepted the message for {} (from {}). "
                       "If it does not arrive, check spam — the From address must "
                       "be authorised by your mail provider.".format(to, msg["From"]),
        })
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": "Send failed: {}".format(e)}), 502


# ── Audit log ─────────────────────────────────────────────────────────────────

@super_bp.route("/audit", methods=["GET"])
@super_admin_required
def get_audit_log():
    limit = min(request.args.get("limit", 200, type=int), 1000)
    rows = AdminAuditLog.query.order_by(
        AdminAuditLog.created_at.desc()).limit(limit).all()
    return jsonify([r.to_dict() for r in rows])


def register_auth(app):
    """Wire the resolver + all auth blueprints into the app."""
    app.before_request(load_current_user)
    app.before_request(require_login)  # mandatory login — runs after the resolver
    app.register_blueprint(auth_bp)
    app.register_blueprint(configs_bp)
    app.register_blueprint(admin_users_bp)
    app.register_blueprint(super_bp)
