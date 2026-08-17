"""Project ORM models — the container a customer engagement's sizings live in.

A project holds many Configurations (sizings). See docs/projects-plan.md for the
decisions these tables encode; the ones that show up as structure here:

  * every sizing belongs to a project (decision 2) — each user has exactly one
    personal ``is_scratch`` project for quick sizings and pre-project leftovers
  * tags are real rows scoped to one project (decision 10)
  * ``role`` on a sizing decides whether it is summed or listed as an
    alternative (decision 3); ``Project.default_role`` is the answer to the
    one-time question asked when a second sizing appears (decision 4)
  * ``salesforce_url`` is scale-only and is omitted — never nulled — from
    serialization for everyone else (decision 27, §9.1)

Kept separate from auth_models.py (identity) and orm_models.py (hardware
catalog); all three share ``db`` from database.py.
"""
from datetime import timedelta, timezone

from auth_models import JSON_TYPE, _iso, _utcnow
from database import db

# Roles a sizing plays in its project's rollup.
ROLE_ALTERNATIVE = "alternative"   # one of several competing options — never summed
ROLE_ADDITIVE = "additive"         # a site/phase that adds to the total
SIZING_ROLES = (ROLE_ALTERNATIVE, ROLE_ADDITIVE)

# Generated bundles are fetchable for a day, then swept by purge_expired()
# (decision 20). Long enough to collect after a meeting, short enough that
# customer documents don't accumulate on disk.
EXPORT_ARTIFACT_TTL = timedelta(hours=24)

# Export job lifecycle.
JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"

# Only https URLs under Salesforce's own domains are accepted, so the field
# can't be repurposed as an arbitrary link store or a phishing vector inside a
# project view a partner can read (§9.1).
SALESFORCE_HOST_SUFFIXES = ("salesforce.com", "force.com")


class Project(db.Model):
    """A customer engagement: a named, code-addressable set of sizings."""
    __tablename__ = "projects"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(12), nullable=False, unique=True, index=True)
    # The only field asked for at creation (decision 24). Everything below is
    # optional so a partner is never required to hand over customer details.
    name = db.Column(db.String(200), nullable=False)
    customer_name = db.Column(db.String(200))
    opportunity_ref = db.Column(db.String(120))
    prepared_by = db.Column(db.String(200))
    description = db.Column(db.Text)
    # UI language when the project was created; drives the export-language
    # prompt when it differs from the session language (decision 25).
    lang = db.Column(db.String(5))
    # Scale-only. Never serialized for anyone else, never rendered into an
    # export, never copied into another user's project (§9.1).
    salesforce_url = db.Column(db.Text)

    # Denormalised from the owner, as Configuration does: listing and purge
    # queries filter on tenant directly and must survive a disabled owner.
    owner_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                         nullable=False, index=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id"),
                          nullable=False, index=True)

    # The per-user personal project: quick sizings land here, and the migration
    # files pre-project sizings here too. One per user.
    is_scratch = db.Column(db.Boolean, nullable=False, default=False)
    # NULL until the second sizing is added and the user is asked once whether
    # these are competing options or parts of one estate.
    default_role = db.Column(db.String(12))

    is_deleted = db.Column(db.Boolean, nullable=False, default=False)
    deleted_at = db.Column(db.DateTime(timezone=True))
    deleted_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=_utcnow, onupdate=_utcnow)

    owner = db.relationship("User", foreign_keys=[owner_id])
    tenant = db.relationship("Tenant", foreign_keys=[tenant_id])
    tags = db.relationship("ProjectTag", back_populates="project",
                           cascade="all, delete-orphan")

    __table_args__ = (
        db.Index("ix_projects_tenant_active", "tenant_id", "is_deleted"),
        db.Index("ix_projects_owner_active", "owner_id", "is_deleted"),
    )

    def to_summary(self, current_user=None, source="tenant", sizing_count=None):
        """List-row shape. ``source`` tags why it's visible, mirroring
        Configuration.to_summary."""
        d = {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "customer_name": self.customer_name,
            "opportunity_ref": self.opportunity_ref,
            "prepared_by": self.prepared_by,
            "description": self.description,
            "lang": self.lang,
            "is_scratch": self.is_scratch,
            "default_role": self.default_role,
            "owner_email": self.owner.email if self.owner else None,
            "tenant_domain": self.tenant.domain if self.tenant else None,
            "is_deleted": self.is_deleted,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "source": source,
            "can_edit": self.can_edit(current_user),
        }
        if sizing_count is not None:
            d["sizing_count"] = sizing_count
        # Omitted, not nulled: an always-present salesforce_url: null would still
        # tell a partner the field exists.
        if self.may_see_salesforce(current_user):
            d["salesforce_url"] = self.salesforce_url
        return d

    def to_dict(self, current_user=None, source="tenant", sizings=None):
        d = self.to_summary(current_user, source)
        d["tags"] = [t.to_dict() for t in sorted(self.tags, key=lambda t: t.name.lower())]
        if sizings is not None:
            d["sizings"] = sizings
        return d

    # ── access helpers ───────────────────────────────────────────────────────

    @staticmethod
    def may_see_salesforce(user):
        """Scale users and super admins only — enforced here, in the write path,
        and in the copy path, never by hiding a field in the UI."""
        return bool(user and (getattr(user, "is_scale", False)
                              or getattr(user, "is_super_admin", False)))

    def can_edit(self, user):
        """A project reached by code is read-only: viewers get view/compare/
        export, and any edit copies the sizing into their own project instead
        (decision 22). Ownership — not mere visibility — grants writes."""
        if not user:
            return False
        if getattr(user, "is_super_admin", False):
            return True
        return self.owner_id == user.id and not self.is_deleted


class ProjectTag(db.Model):
    """A freeform label scoped to one project (decision 10). Project-scoped so
    one customer's vocabulary never leaks into another's autocomplete."""
    __tablename__ = "project_tags"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"),
                           nullable=False, index=True)
    name = db.Column(db.String(60), nullable=False)
    color = db.Column(db.String(20))

    project = db.relationship("Project", back_populates="tags")

    __table_args__ = (
        db.UniqueConstraint("project_id", "name", name="uq_project_tag_name"),
    )

    def to_dict(self):
        return {"id": self.id, "name": self.name, "color": self.color}


class ConfigurationTag(db.Model):
    """Join row: one tag applied to one sizing."""
    __tablename__ = "configuration_tags"

    configuration_id = db.Column(db.Integer, db.ForeignKey("configurations.id"),
                                 primary_key=True)
    tag_id = db.Column(db.Integer, db.ForeignKey("project_tags.id"),
                       primary_key=True)


class ScaleProjectLink(db.Model):
    """Permanent link from a scale user to a project they pulled by code.
    Mirrors ScaleConfigLink, one level up: the link grants read-only visibility
    of the whole set at once."""
    __tablename__ = "scale_project_links"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), nullable=False)
    linked_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        db.UniqueConstraint("user_id", "project_id", name="uq_scale_project_link"),
    )


class ReplicationLink(db.Model):
    """One cluster replicating to a cluster in another sizing (§8.5).

    Replication between clusters of a *single* import already worked; this lifts
    it to the project, where the DR site routinely arrives as its own LiveOptics
    file. Both ends must live in the same project — a link across projects would
    pull one customer's demand into another's sizing.

    The reserve is computed from the source's *demand*, never its sized result,
    so mutual A<->B replication stays well-defined rather than circular.
    """
    __tablename__ = "replication_links"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"),
                           nullable=False, index=True)
    source_configuration_id = db.Column(
        db.Integer, db.ForeignKey("configurations.id"), nullable=False, index=True)
    source_cluster = db.Column(db.String(120), nullable=False, default="")
    target_configuration_id = db.Column(
        db.Integer, db.ForeignKey("configurations.id"), nullable=False, index=True)
    target_cluster = db.Column(db.String(120), nullable=False, default="")

    compute_pct = db.Column(db.Integer, nullable=False, default=100)
    storage_pct = db.Column(db.Integer, nullable=False, default=100)
    # "reserved": inbound compute is held at N-1 too. "failover": replicas are
    # expected to run only after the source site has failed over.
    mode = db.Column(db.String(12), nullable=False, default="reserved")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (
        # One target per source cluster, matching the single-target model the
        # in-sizing feature already uses.
        db.UniqueConstraint("source_configuration_id", "source_cluster",
                            name="uq_replication_source"),
    )

    def to_dict(self, names=None):
        names = names or {}
        return {
            "id": self.id,
            "source_configuration_id": self.source_configuration_id,
            "source_cluster": self.source_cluster,
            "source_label": _qualified(names.get(self.source_configuration_id),
                                       self.source_cluster),
            "target_configuration_id": self.target_configuration_id,
            "target_cluster": self.target_cluster,
            "target_label": _qualified(names.get(self.target_configuration_id),
                                       self.target_cluster),
            "compute_pct": self.compute_pct,
            "storage_pct": self.storage_pct,
            "mode": self.mode,
        }

    def digest_material(self, source_payload_digest):
        """What a target's fingerprint folds in for this inbound link (§8.5).

        The source's payload digest plus the terms of the link: change the
        source's workload, or how much of it is reserved, and the target's
        cached result stops being trustworthy.
        """
        return [source_payload_digest or "", self.source_cluster,
                str(self.compute_pct), str(self.storage_pct), self.mode]


def _qualified(sizing_name, cluster):
    """"Site B — Prosess": a cluster called "Prod" in two different sizings must
    not read as one cluster in an export or a picker."""
    sizing_name = sizing_name or "?"
    return f"{sizing_name} — {cluster}" if cluster else sizing_name


class ExportJob(db.Model):
    """One queued/running/finished bundle export (§7.2).

    Claimed atomically by a worker thread: the app runs several gunicorn
    workers, so "one worker thread" is per process and an unclaimed queue would
    otherwise be built several times over.
    """
    __tablename__ = "export_jobs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"),
                        nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"),
                           nullable=False, index=True)
    fmt = db.Column(db.String(24), nullable=False)          # pptx | docx | pdf | ...
    status = db.Column(db.String(12), nullable=False, default=JOB_QUEUED, index=True)
    progress = db.Column(db.Integer, nullable=False, default=0)   # 0-100
    sizing_ids = db.Column(JSON_TYPE)
    lang = db.Column(db.String(5))
    notify_email = db.Column(db.Boolean, nullable=False, default=False)
    filename = db.Column(db.String(255))
    artifact_path = db.Column(db.Text)
    error = db.Column(db.Text)
    # Which process holds it, and since when, so a job orphaned by a restart
    # can be returned to the queue instead of sitting "running" forever.
    claimed_by = db.Column(db.String(80))
    claimed_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=_utcnow, index=True)
    finished_at = db.Column(db.DateTime(timezone=True))
    expires_at = db.Column(db.DateTime(timezone=True), index=True)

    def is_expired(self, now=None):
        """Checked by the download route itself: the daily sweep can leave a
        file on disk up to a day past expiry, so the file's presence is not
        proof it is still offered."""
        if not self.expires_at:
            return False
        deadline = self.expires_at
        # Postgres hands back an aware datetime; SQLite (tests, and any local
        # run) drops the zone. Treat a naive value as the UTC it was written as,
        # rather than raising on the comparison.
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return (now or _utcnow()) >= deadline

    def to_dict(self):
        return {
            "id": self.id,
            "project_id": self.project_id,
            "format": self.fmt,
            "status": self.status,
            "progress": self.progress,
            "filename": self.filename,
            "error": self.error,
            "created_at": _iso(self.created_at),
            "finished_at": _iso(self.finished_at),
            "expires_at": _iso(self.expires_at),
            "expired": self.is_expired(),
        }


def new_code():
    """A unique-on-insert 12-digit project code. Callers rely on the DB unique
    constraint plus a retry, exactly as configuration codes do."""
    import secrets
    return "{:012d}".format(secrets.randbelow(10 ** 12))


def ensure_scratch_project(owner_id, tenant_id, name="Unfiled"):
    """Find (or create) the one personal project a user's quick sizings land in.

    Scratch and "Unfiled" are deliberately the same project: two personal
    buckets would be one more place to lose a sizing in. Created lazily, so a
    user who always works in named projects never accumulates an empty one.
    """
    from sqlalchemy.exc import IntegrityError

    existing = Project.query.filter_by(
        owner_id=owner_id, is_scratch=True, is_deleted=False).first()
    if existing:
        return existing

    for _ in range(6):
        project = Project(code=new_code(), name=name, owner_id=owner_id,
                          tenant_id=tenant_id, is_scratch=True)
        db.session.add(project)
        try:
            db.session.commit()
            return project
        except IntegrityError:      # code collision — regenerate and retry
            db.session.rollback()
    raise RuntimeError("Could not allocate a unique project code")


def valid_salesforce_url(url):
    """True when ``url`` is an https link to a Salesforce host (§9.1)."""
    from urllib.parse import urlparse
    if not url:
        return False
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    return any(host == s or host.endswith("." + s) for s in SALESFORCE_HOST_SUFFIXES)
