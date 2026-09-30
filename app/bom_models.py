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

# file_format of a check whose BOM the Claude agent read (bom/agent_ingest.py)
# because no deterministic parser recognised the file.
AGENT_FORMAT = "ai_agent"

# AppSettings (admin UI, BOM reviews tab). Caps: agent jobs per UTC day,
# "0" = unlimited (the testing default; set real caps before production).
AGENT_ALWAYS_REVIEW_SETTING = "bom_agent_always_review"
AGENT_TENANT_CAP_SETTING = "bom_agent_daily_cap_tenant"
AGENT_GLOBAL_CAP_SETTING = "bom_agent_daily_cap_global"

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"


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
    # Only for file_format == AGENT_FORMAT: how the Claude agent read the file
    # (source kind, grounding result, dropped lines, injection flag, tokens).
    # Separate from ``result`` because a re-check rewrites the result.
    ingest_meta = db.Column(JSON_TYPE)
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

    @property
    def is_agent_read(self):
        return self.file_format == AGENT_FORMAT

    def agent_flag_reasons(self):
        """Review reasons that come from HOW the file was read, not from the
        rules: an agent-read check opens a review while the owner builds trust
        in the agent (setting ``bom_agent_always_review``, on by default), and
        always when the document tried to instruct the agent or lines had to
        be dropped because they were not in the document."""
        if not self.is_agent_read:
            return []
        from auth import get_setting
        meta = self.ingest_meta or {}
        reasons = []
        if (get_setting(AGENT_ALWAYS_REVIEW_SETTING, "1") or "1") != "0":
            reasons.append("agent_read")
        if meta.get("instructions_detected"):
            reasons.append("agent_instructions")
        if meta.get("dropped") or meta.get("model_changed"):
            reasons.append("agent_ungrounded")
        local = meta.get("pdf_local") or {}
        if meta.get("hidden_words") or local.get("hard_stop"):
            # Invisible text, active content, encryption: the file itself is
            # suspect, whatever the agent made of its visible text.
            reasons.append("pdf_suspicious")
        return reasons

    def apply_result(self, result, sizing):
        """Store a run_check result and keep the review status in step: flags
        open a review, a clean re-check closes one nobody has acted on."""
        extra = [r for r in self.agent_flag_reasons() if r not in (result.get("flag_reasons") or [])]
        if extra:
            result = dict(result)
            result["flag_reasons"] = list(result.get("flag_reasons") or []) + extra
        self.result = result
        self.configuration_id = sizing.id if sizing else None
        self.technical_verdict = (result.get("technical") or {}).get("verdict")
        fit = result.get("fit")
        self.fit_verdict = fit.get("verdict") if fit else None
        self.catalog_stamp = result.get("catalog_stamp")
        self.checked_at = _utcnow()
        flags = result.get("flag_reasons") or []
        current = self.review_status or REVIEW_NONE   # None before the first flush
        if flags and current == REVIEW_NONE:
            self.review_status = REVIEW_OPEN
        elif not flags and current == REVIEW_OPEN:
            self.review_status = REVIEW_NONE
        else:
            self.review_status = current

    def history_entry(self):
        return {
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "technical_verdict": self.technical_verdict,
            "fit_verdict": self.fit_verdict,
            "catalog_stamp": self.catalog_stamp,
            "sizing_id": self.configuration_id,
        }

    def agent_summary(self):
        """What the UI needs to warn about an agent-read check: None for a
        file a deterministic parser read. Model details stay server-side."""
        if not self.is_agent_read:
            return None
        meta = self.ingest_meta or {}
        local = meta.get("pdf_local") or {}
        return {
            "source_kind": meta.get("source_kind"),
            "pdf_mode": meta.get("pdf_mode"),
            "grounded": meta.get("grounded"),
            "dropped": list(meta.get("dropped") or [])[:50],
            "model_changed": list(meta.get("model_changed") or [])[:20],
            "instructions_detected": bool(meta.get("instructions_detected")),
            "hidden_words": int(meta.get("hidden_words") or 0),
            # A PDF layout we know, read with too little certainty to trust:
            # the score and the reasons, so the user sees why the agent ran.
            "pdf_certainty": local.get("score") if local.get("format") else None,
            "pdf_reasons": [r.get("text") for r in (local.get("reasons") or [])][:5],
        }

    def pdf_summary(self):
        """Certainty of a PDF read by a local parser; None otherwise."""
        pdf = (self.ingest_meta or {}).get("pdf") if not self.is_agent_read else None
        if not pdf:
            return None
        return {"score": pdf.get("score"), "format": pdf.get("format"),
                "reasons": [r.get("text") for r in (pdf.get("reasons") or [])][:5]}

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
            "agent": self.agent_summary(),
            "pdf": self.pdf_summary(),
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


# ── part notes ───────────────────────────────────────────────────────────────

NOTE_MATCH_EXACT = "exact"         # part number (normalised), else exact description
NOTE_MATCH_CONTAINS = "contains"   # every word of match_text appears in the line
NOTE_MATCH_MODES = (NOTE_MATCH_EXACT, NOTE_MATCH_CONTAINS)
NOTE_SEVERITIES = ("info", "warning", "error")


class BomPartNote(db.Model):
    """A reviewer's standing verdict on one specific part.

    Some parts are neither simply supported nor simply wrong: the Broadcom 5720
    LOM on a Dell is fine once it is disabled in the BIOS. A super admin writes
    that down once, during a BOM review, and every later check containing the
    part gets this note INSTEAD of the generic finding ("NIC not found in the
    Hardware Compatibility List"), at the severity the admin chose — or, for a
    part that is on the HCL, as an extra finding (owner decisions 2026-09-17).

    Kept apart from the HCL catalog on purpose: the catalog mirrors what Scale
    publishes and is rewritten by the scrape, while a note is local reviewer
    knowledge. bom/part_notes.py applies them; the ported rules never see them.
    Existing checks pick a note up on their next re-check.
    """
    __tablename__ = "bom_part_notes"

    id = db.Column(db.Integer, primary_key=True)
    match_mode = db.Column(db.String(12), nullable=False, default=NOTE_MATCH_EXACT)
    part_number = db.Column(db.String(80))          # exact mode
    description = db.Column(db.String(300))         # exact mode, when no part number
    match_text = db.Column(db.String(200))          # contains mode
    # BOM line category the note is limited to (nic, controller, cpu, ...);
    # NULL matches any. Keeps a "contains" note from hitting the wrong kind of part.
    category = db.Column(db.String(20))
    severity = db.Column(db.String(10), nullable=False, default="warning")
    issue = db.Column(db.String(300), nullable=False)
    remediation = db.Column(db.Text)
    active = db.Column(db.Boolean, nullable=False, default=True)
    source_check_id = db.Column(db.Integer, db.ForeignKey("bom_checks.id"))
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=_utcnow, onupdate=_utcnow)

    created_by = db.relationship("User", foreign_keys=[created_by_user_id])

    def to_dict(self):
        return {
            "id": self.id,
            "match_mode": self.match_mode,
            "part_number": self.part_number,
            "description": self.description,
            "match_text": self.match_text,
            "category": self.category,
            "severity": self.severity,
            "issue": self.issue,
            "remediation": self.remediation,
            "active": bool(self.active),
            "source_check_id": self.source_check_id,
            "created_by": self.created_by.email if self.created_by else None,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class BomAgentJob(db.Model):
    """One upload the deterministic parsers could not read, queued for the
    Claude agent (bom/agent_ingest.py, drained by bom/agent_worker.py).

    Why a queue and not the request: reading a PDF or a picture takes up to
    a minute or two, longer than the proxy in front of the app waits. The
    upload returns 202 at once, the modal polls, and the user can ask for an
    email and walk away, the same as the project exports.

    The uploaded bytes live in ``content`` ONLY until the job finishes; the
    worker nulls the column in the same commit that records the outcome, so
    the check route's "never keep the quote" rule still holds once a job is
    done. The row itself stays: its ``meta`` (tokens, model, outcome) is the
    agent usage record, and the daily caps count rows per day.
    """
    __tablename__ = "bom_agent_jobs"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey("projects.id"),
                           nullable=False, index=True)
    configuration_id = db.Column(db.Integer, db.ForeignKey("configurations.id"))
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)
    name = db.Column(db.String(200), nullable=False)
    filename = db.Column(db.String(200), nullable=False)
    file_sha256 = db.Column(db.String(64), nullable=False, index=True)
    content = db.Column(db.LargeBinary)                 # nulled when finished
    status = db.Column(db.String(12), nullable=False, default=JOB_QUEUED, index=True)
    error = db.Column(db.Text)
    notify_email = db.Column(db.Boolean, nullable=False, default=False)
    check_id = db.Column(db.Integer, db.ForeignKey("bom_checks.id"))
    # A failed round trip through the template still yields the agent's
    # pre-filled template; kept (small) so the user can fix and upload it.
    template = db.Column(db.LargeBinary)
    meta = db.Column(JSON_TYPE)
    lang = db.Column(db.String(5))
    claimed_by = db.Column(db.String(80))
    claimed_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=_utcnow, index=True)
    finished_at = db.Column(db.DateTime(timezone=True))

    project = db.relationship("Project", foreign_keys=[project_id])
    user = db.relationship("User", foreign_keys=[user_id])

    def to_dict(self):
        return {
            "id": self.id,
            "project_id": self.project_id,
            "configuration_id": self.configuration_id,
            "name": self.name,
            "filename": self.filename,
            "status": self.status,
            "error": self.error,
            "notify_email": bool(self.notify_email),
            "check_id": self.check_id,
            "has_template": self.template is not None,
            "created_at": _iso(self.created_at),
            "finished_at": _iso(self.finished_at),
        }
