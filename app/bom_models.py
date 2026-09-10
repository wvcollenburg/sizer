"""BOM check ORM — docs/bom-checker-build.md §2 (bom_checks).

A check stores the *normalised* BOM and the *result*, never the uploaded file
(only its digest): the file is customer-facing quote material and the sizer
already follows that rule for LiveOptics/RVTools imports. Storing the
normalised form is what makes a later re-check possible — after a catalog
approval or a sizing change the same input is validated again and the
``history`` list shows what moved.

``review_status`` is the admin queue. Only checks whose result carried
``flag_reasons`` (INCONCLUSIVE verdicts, "not in the HCL" findings, delisted
parts) open a review; clean passes and well-known failures (BOSS card, old CPU,
diskless) go straight back to the user.
"""
from database import db
from auth_models import JSON_TYPE, _iso, _utcnow

REVIEW_NONE = "none"
REVIEW_OPEN = "open"
REVIEW_CONFIRMED = "confirmed"
REVIEW_INCORRECT = "incorrect"
REVIEW_STATUSES = (REVIEW_NONE, REVIEW_OPEN, REVIEW_CONFIRMED, REVIEW_INCORRECT)


class BomCheck(db.Model):
    __tablename__ = "bom_checks"
    __table_args__ = (
        db.Index("ix_bom_checks_project_active", "project_id", "is_deleted"),
    )

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"),
                           nullable=False, index=True)
    configuration_id = db.Column(db.Integer, db.ForeignKey("configurations.id"),
                                 index=True)                     # the sizing, optional
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    filename = db.Column(db.String(200))
    file_sha256 = db.Column(db.String(64))
    file_format = db.Column(db.String(24))
    vendor = db.Column(db.String(20))
    normalized = db.Column(JSON_TYPE, nullable=False)
    result = db.Column(JSON_TYPE)
    technical_verdict = db.Column(db.String(12), index=True)
    fit_verdict = db.Column(db.String(12))
    review_status = db.Column(db.String(12), nullable=False, default=REVIEW_NONE, index=True)
    review_note = db.Column(db.Text)
    reviewed_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime(timezone=True))
    catalog_stamp = db.Column(db.String(64))
    history = db.Column(JSON_TYPE)
    checked_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=_utcnow, onupdate=_utcnow)
    is_deleted = db.Column(db.Boolean, nullable=False, default=False)
    deleted_at = db.Column(db.DateTime(timezone=True))

    project = db.relationship("Project", foreign_keys=[project_id])
    configuration = db.relationship("Configuration", foreign_keys=[configuration_id])
    user = db.relationship("User", foreign_keys=[user_id])

    @property
    def flag_reasons(self):
        return list((self.result or {}).get("flag_reasons") or [])

    def to_dict(self, full=False):
        """Summary by default (list rows); ``full`` adds the normalised BOM and
        the whole result. Nothing here is Scale-only, so no field gating."""
        d = {
            "id": self.id,
            "project_id": self.project_id,
            "configuration_id": self.configuration_id,
            "sizing_name": self.configuration.name if self.configuration else None,
            "user_id": self.user_id,
            "owner_email": self.user.email if self.user else None,
            "name": self.name,
            "filename": self.filename,
            "file_format": self.file_format,
            "vendor": self.vendor,
            "technical_verdict": self.technical_verdict,
            "fit_verdict": self.fit_verdict,
            "review_status": self.review_status,
            "review_note": self.review_note,
            "reviewed_at": _iso(self.reviewed_at),
            "flag_reasons": self.flag_reasons,
            "catalog_stamp": self.catalog_stamp,
            "checked_at": _iso(self.checked_at),
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "config_count": len((self.normalized or {}).get("configs") or []),
        }
        if full:
            d["normalized"] = self.normalized
            d["result"] = self.result
            d["history"] = self.history or []
        return d


class BomRejectedFile(db.Model):
    """A BOM upload the parsers refused, kept WITH THE UPLOADER'S CONSENT so
    the owner can teach the checker its format.

    Nothing lands here automatically: the check route keeps its
    delete-after-parsing rule, and the client re-submits the file to a
    dedicated endpoint only after the user accepts the offer shown on the
    rejection. The bytes live in the database, not on disk — the box has no
    shell operator to fish files out of a container filesystem, retrieval
    must work over HTTP, and the daily purge ages rows out after
    ``RETENTION_DAYS`` so consented quotes don't accumulate forever.
    """
    __tablename__ = "bom_rejected_files"

    RETENTION_DAYS = 90

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"), index=True)
    filename = db.Column(db.String(200), nullable=False)
    file_sha256 = db.Column(db.String(64), nullable=False, index=True)
    size_bytes = db.Column(db.Integer, nullable=False)
    content = db.Column(db.LargeBinary, nullable=False)
    error = db.Column(db.Text)            # what the checker answered
    note = db.Column(db.Text)             # optional word from the uploader
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=_utcnow, index=True)

    user = db.relationship("User", foreign_keys=[user_id])
    project = db.relationship("Project", foreign_keys=[project_id])

    def to_dict(self):
        """Metadata only — the bytes go through the download route."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "project_name": self.project.name if self.project else None,
            "owner_email": self.user.email if self.user else None,
            "tenant_domain": self.user.tenant.domain if (self.user and self.user.tenant) else None,
            "filename": self.filename,
            "file_sha256": self.file_sha256,
            "size_bytes": self.size_bytes,
            "error": self.error,
            "note": self.note,
            "created_at": _iso(self.created_at),
        }
