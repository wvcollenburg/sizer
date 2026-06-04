"""Auth / multitenancy ORM models.

Kept separate from the hardware catalog in orm_models.py; both share the ``db``
from database.py. The config payload is an opaque, client-supplied frontend
snapshot stored as JSONB on Postgres (generic JSON elsewhere, for sqlite tests).
"""
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import JSONB

from database import db
from email_domains import SCALE_DOMAIN

# JSONB on Postgres, portable JSON on sqlite (used by the local test client).
JSON_TYPE = db.JSON().with_variant(JSONB, "postgresql")

ROLE_USER = "user"
ROLE_TENANT_ADMIN = "tenant_admin"
ROLE_SUPER_ADMIN = "super_admin"


def _utcnow():
    """Timezone-aware UTC now (datetime.utcnow() is naive and deprecated)."""
    return datetime.now(timezone.utc)


class Tenant(db.Model):
    """One row per email domain. The tenant *is* the domain."""
    __tablename__ = "tenants"

    id = db.Column(db.Integer, primary_key=True)
    domain = db.Column(db.String(255), nullable=False, unique=True, index=True)
    is_scale = db.Column(db.Boolean, nullable=False, default=False)
    is_blocked = db.Column(db.Boolean, nullable=False, default=False)
    blocked_at = db.Column(db.DateTime(timezone=True))
    blocked_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)

    users = db.relationship(
        "User", back_populates="tenant",
        foreign_keys="User.tenant_id",
    )

    @staticmethod
    def domain_is_scale(domain):
        return (domain or "").strip().lower() == SCALE_DOMAIN

    def active_admins(self):
        return [u for u in self.users
                if u.role == ROLE_TENANT_ADMIN and not u.is_disabled]

    def to_dict(self):
        return {
            "id": self.id,
            "domain": self.domain,
            "is_scale": self.is_scale,
            "is_blocked": self.is_blocked,
            "blocked_at": _iso(self.blocked_at),
            "created_at": _iso(self.created_at),
            "user_count": sum(1 for u in self.users if not u.is_disabled),
        }


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id"),
                          nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False, default=ROLE_USER)
    is_disabled = db.Column(db.Boolean, nullable=False, default=False)
    disabled_at = db.Column(db.DateTime(timezone=True))
    disabled_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    last_login_at = db.Column(db.DateTime(timezone=True))

    # Email verification (only enforced when SMTP is configured + enabled).
    # Defaults to True so accounts that predate verification stay usable.
    is_verified = db.Column(db.Boolean, nullable=False, default=True)
    verification_token = db.Column(db.String(64), index=True)
    verification_sent_at = db.Column(db.DateTime(timezone=True))

    # Self-service password reset (SMTP-gated).
    reset_token = db.Column(db.String(64), index=True)
    reset_sent_at = db.Column(db.DateTime(timezone=True))

    # GDPR: timestamp the user accepted the privacy policy at signup (proof of consent).
    privacy_accepted_at = db.Column(db.DateTime(timezone=True))

    # Brute-force lockout.
    failed_login_count = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime(timezone=True))

    tenant = db.relationship(
        "Tenant", back_populates="users", foreign_keys=[tenant_id],
    )

    __table_args__ = (
        db.Index("ix_users_tenant_disabled", "tenant_id", "is_disabled"),
    )

    @property
    def is_scale(self):
        return bool(self.tenant and self.tenant.is_scale)

    @property
    def is_super_admin(self):
        return self.role == ROLE_SUPER_ADMIN

    @property
    def is_tenant_admin(self):
        return self.role == ROLE_TENANT_ADMIN

    @property
    def domain(self):
        return self.tenant.domain if self.tenant else None

    def to_dict(self):
        """Public shape — never exposes the password hash."""
        return {
            "id": self.id,
            "email": self.email,
            "role": self.role,
            "is_scale": self.is_scale,
            "tenant_domain": self.domain,
            "is_disabled": self.is_disabled,
            "is_verified": self.is_verified,
            "disabled_at": _iso(self.disabled_at),
            "created_at": _iso(self.created_at),
            "last_login_at": _iso(self.last_login_at),
            "privacy_accepted_at": _iso(self.privacy_accepted_at),
        }


class Configuration(db.Model):
    """A saved sizing: a named, code-addressable snapshot of the full UI state."""
    __tablename__ = "configurations"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(12), nullable=False, unique=True, index=True)
    name = db.Column(db.String(200), nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                         nullable=False, index=True)
    # Denormalised from owner at save time: the dominant listing/purge queries
    # filter on tenant directly and must survive the owner being disabled.
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id"),
                          nullable=False, index=True)
    payload = db.Column(JSON_TYPE, nullable=False)
    is_deleted = db.Column(db.Boolean, nullable=False, default=False)
    deleted_at = db.Column(db.DateTime(timezone=True))
    deleted_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=_utcnow, onupdate=_utcnow)

    owner = db.relationship("User", foreign_keys=[owner_id])
    tenant = db.relationship("Tenant", foreign_keys=[tenant_id])

    __table_args__ = (
        db.Index("ix_configurations_tenant_active", "tenant_id", "is_deleted"),
    )

    def to_summary(self, current_user=None, source="tenant"):
        """List-row shape (no payload). ``source`` tags why it's visible."""
        owner_email = self.owner.email if self.owner else None
        can_delete = bool(
            current_user and (
                current_user.is_super_admin
                or self.owner_id == current_user.id
                or (current_user.is_scale and source == "linked")
            )
        )
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "owner_email": owner_email,
            "tenant_domain": self.tenant.domain if self.tenant else None,
            "is_deleted": self.is_deleted,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "source": source,
            "can_delete": can_delete,
        }

    def to_dict(self, current_user=None, source="tenant"):
        d = self.to_summary(current_user, source)
        d["payload"] = self.payload
        return d


class ScaleConfigLink(db.Model):
    """Permanent link from a scale user to a config they pulled by code.

    Exists only for *foreign* configs (not owned, not in a scale tenant the user
    already sees). A scale user "deleting" such a config removes this row only.
    """
    __tablename__ = "scale_config_links"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False, index=True)
    configuration_id = db.Column(db.Integer, db.ForeignKey("configurations.id"),
                                 nullable=False)
    linked_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        db.UniqueConstraint("user_id", "configuration_id", name="uq_scale_link"),
    )


class AppSetting(db.Model):
    """String key/value app config (e.g. SMTP settings, verification toggle).
    Distinct from orm_models.SizingSetting, which stores numeric sizing knobs."""
    __tablename__ = "app_settings"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(60), nullable=False, unique=True, index=True)
    value = db.Column(db.Text)

    def to_dict(self):
        return {"key": self.key, "value": self.value}


class AdminAuditLog(db.Model):
    """Append-only record of privileged admin actions (disable/restore/delete
    user, block domain, reassign admin, purge). actor_email is snapshotted so the
    log survives the actor being deleted."""
    __tablename__ = "admin_audit_log"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    actor_email = db.Column(db.String(255))
    action = db.Column(db.String(60), nullable=False)
    detail = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=_utcnow, index=True)

    def to_dict(self):
        return {
            "id": self.id,
            "actor_email": self.actor_email,
            "action": self.action,
            "detail": self.detail,
            "created_at": _iso(self.created_at),
        }


class PiiErasure(db.Model):
    """GDPR marker: a deleted user's email is retained here only so the audit log
    can be scrubbed of their PII exactly one retention period after deletion.
    The marker (and the matching audit-log PII) are erased once processed."""
    __tablename__ = "pii_erasure"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), nullable=False, index=True)
    deleted_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=_utcnow, index=True)


def _iso(dt):
    return dt.isoformat() if dt else None
