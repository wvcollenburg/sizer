"""Background worker for project bundle exports (docs/projects-plan.md §7.2).

Building a bundle is CPU-bound — python-pptx/docx authoring plus cairosvg
rasterisation — and the PDF variants add a LibreOffice subprocess on top. A
ten-sizing bundle can take minutes, which is far too long to hold a request
open, so exports are queued and drained here.

Two things this has to get right:

* **"One worker thread" is per gunicorn process, not per deployment.** The app
  runs several workers, so several threads poll the same table. Jobs are
  therefore claimed with an atomic UPDATE ... RETURNING guarded by SKIP LOCKED;
  without it, three processes would build the same bundle at once — exactly the
  CPU storm export_gate exists to prevent.
* **A job whose claimer dies must come back.** A container restart mid-build
  otherwise leaves a row stuck in "running" forever and its owner waiting for an
  email that never arrives, so a stale claim is returned to the queue.

Artifacts live on local disk for 24 hours (decision 20) and are swept by the
existing retention purge. That assumes a single app container; with a second
one the worker and the download route must share a volume.
"""
import os
import tempfile
import threading
import time
import traceback
from datetime import timedelta

from sqlalchemy import text

from auth_models import Configuration, User, _utcnow
from database import db
from project_models import (
    EXPORT_ARTIFACT_TTL, ExportJob, JOB_DONE, JOB_FAILED, JOB_QUEUED,
    JOB_RUNNING, Project,
)

POLL_SECONDS = int(os.environ.get("EXPORT_WORKER_POLL", "5"))
# A claim older than this is treated as abandoned (gunicorn's worker timeout is
# 180s; a long PDF render must not be reclaimed while it is still running).
CLAIM_TIMEOUT = timedelta(minutes=20)

# Outside the source tree by default: bundles are regenerable, expire in 24
# hours, and carry customer names — they have no business in the repo, and a
# default under app/ meant every test run dirtied the working tree. Point
# EXPORT_ARTIFACT_DIR at a mounted volume if they should survive a restart.
ARTIFACT_DIR = os.environ.get(
    "EXPORT_ARTIFACT_DIR",
    os.path.join(tempfile.gettempdir(), "sizer-exports"))

_worker_started = False
_worker_lock = threading.Lock()


# ── claiming ─────────────────────────────────────────────────────────────────

def _worker_id():
    return f"{os.getpid()}:{threading.get_ident()}"


def claim_next_job():
    """Atomically take the oldest queued job, or None.

    SKIP LOCKED keeps two workers from grabbing the same row; on SQLite (tests)
    it degrades to a plain guarded UPDATE, which is fine for a single process.
    """
    if db.engine.dialect.name == "postgresql":
        row = db.session.execute(text("""
            UPDATE export_jobs SET status = :running, claimed_by = :worker,
                                   claimed_at = :now
            WHERE id = (
                SELECT id FROM export_jobs
                WHERE status = :queued
                ORDER BY created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            RETURNING id
        """), {"running": JOB_RUNNING, "queued": JOB_QUEUED,
               "worker": _worker_id(), "now": _utcnow()}).first()
        db.session.commit()
        return db.session.get(ExportJob, row[0]) if row else None

    job = ExportJob.query.filter_by(status=JOB_QUEUED).order_by(
        ExportJob.created_at).first()
    if job is None:
        return None
    job.status = JOB_RUNNING
    job.claimed_by = _worker_id()
    job.claimed_at = _utcnow()
    db.session.commit()
    return job


def requeue_abandoned_jobs():
    """Return jobs whose claimer died to the queue."""
    cutoff = _utcnow() - CLAIM_TIMEOUT
    stuck = ExportJob.query.filter(
        ExportJob.status == JOB_RUNNING,
        ExportJob.claimed_at.isnot(None),
        ExportJob.claimed_at < cutoff).all()
    for job in stuck:
        job.status = JOB_QUEUED
        job.claimed_by = None
        job.claimed_at = None
    if stuck:
        db.session.commit()
    return len(stuck)


# ── building ─────────────────────────────────────────────────────────────────

def sections_for(job):
    """Flatten the selected sizings into the section list the bundle generators
    consume, in the project's own order.

    Resolved when the job is CLAIMED, not when it was queued: a sizing deleted
    in between must not crash the worker or half-render the document.
    """
    ids = job.sizing_ids or []
    sizings = Configuration.query.filter(
        Configuration.id.in_(ids or [-1]),
        Configuration.is_deleted.is_(False)).order_by(
            Configuration.position, Configuration.id).all()

    sections, skipped = [], []
    for sizing in sizings:
        clusters = ((sizing.result_snapshot or {}).get("clusters")) or []
        # Two renderable section shapes:
        #   * a PROPOSAL cluster — summary + recommendation + projection (the
        #     generators index these unconditionally, so a partial one would take
        #     the whole bundle down; it must be complete);
        #   * a CONFIG cluster — an appliance/validated hardware config with no
        #     recommendation, rendered as a configuration section instead.
        usable = [c for c in clusters
                  if c.get("recommendation") and c.get("summary")
                  and c.get("projection")]
        config_only = [c for c in clusters
                       if c.get("config") and not c.get("recommendation")]
        if not usable and not config_only:
            # Nothing renderable (e.g. a sizing that was never calculated/saved).
            skipped.append(sizing.name)
            continue

        def _named(cluster, siblings):
            section = dict(cluster)
            # Qualify the section name so "Prod" from two sizings can't read as
            # one cluster in the document.
            base = cluster.get("name") or ""
            section["name"] = f"{sizing.name} — {base}" if base and len(siblings) > 1 \
                else (sizing.name or base)
            return section

        for cluster in usable:
            sections.append(_named(cluster, usable))
        for cluster in config_only:
            sections.append(_named(cluster, config_only))
    return sections, skipped


def _safe_stem(name):
    """A filename fragment from a user-supplied project name.

    Project names are free text: "Acme / Phase 2" would otherwise become a path
    with a directory in it — the write fails with a bare errno if the directory
    is missing, and lands outside ARTIFACT_DIR if it happens to exist.
    """
    import re
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip())
    cleaned = cleaned.strip("._-")[:40]
    return cleaned or "project"


def build_artifact(job, sections):
    """Render the bundle and return (path, filename)."""
    from export_docx import build_bundle_proposal_docx, convert_docx_to_pdf, convert_pptx_to_pdf
    from export_pptx import generate_bundle_proposal

    lang = job.lang or "en"
    fmt = job.fmt
    project = db.session.get(Project, job.project_id)
    stem = "SC_Proposal_" + _safe_stem(project.name if project else "project")

    if fmt == "pptx":
        buf = generate_bundle_proposal(sections, lang=lang)
        data, name = buf.getvalue(), stem + ".pptx"
    elif fmt == "docx":
        buf = build_bundle_proposal_docx(sections, lang=lang)
        data, name = buf.getvalue(), stem + ".docx"
    elif fmt == "pdf":
        buf = build_bundle_proposal_docx(sections, lang=lang)
        data = convert_docx_to_pdf(buf.getvalue())
        name = stem + ".pdf"
    elif fmt == "presentation-pdf":
        buf = generate_bundle_proposal(sections, lang=lang)
        data = convert_pptx_to_pdf(buf.getvalue())
        name = stem + "_slides.pdf"
    else:
        raise ValueError(f"unknown export format {fmt!r}")

    if not data:
        raise RuntimeError("PDF conversion is unavailable on this server")

    os.makedirs(ARTIFACT_DIR, exist_ok=True)
    path = os.path.join(ARTIFACT_DIR, f"job{job.id}_{name}")
    with open(path, "wb") as handle:
        handle.write(data)
    return path, name


def run_job(job, app=None):
    """Build one claimed job, recording success or failure on the row."""
    try:
        sections, skipped = sections_for(job)
        if not sections:
            raise RuntimeError("None of the selected sizings carry an exportable "
                               "proposal. Open and re-save them first.")
        job.progress = 25
        db.session.commit()

        path, name = build_artifact(job, sections)
        job.artifact_path = path
        job.filename = name
        job.status = JOB_DONE
        job.progress = 100
        job.finished_at = _utcnow()
        job.expires_at = _utcnow() + EXPORT_ARTIFACT_TTL
        if skipped:
            job.error = "Skipped (no proposal data): " + ", ".join(skipped)
        db.session.commit()
    except KeyError as exc:
        # A stored snapshot missing a field the generators require. Bare
        # "'form_factor'" tells the user nothing, so name what to do about it.
        db.session.rollback()
        job.status = JOB_FAILED
        job.error = (f"A selected sizing is missing {exc} in its stored result — "
                     f"it was probably saved by an older version. Open it and "
                     f"save it again, then re-export.")[:500]
        job.finished_at = _utcnow()
        db.session.commit()
        if app:
            app.logger.warning("Export job %s: incomplete snapshot (%s)", job.id, exc)
    except Exception as exc:                     # noqa: BLE001 - recorded, not raised
        db.session.rollback()
        job.status = JOB_FAILED
        job.error = str(exc)[:500]
        job.finished_at = _utcnow()
        db.session.commit()
        if app:
            app.logger.warning("Export job %s failed: %s\n%s", job.id, exc,
                               traceback.format_exc())
    finally:
        _notify(job, app)


def _notify(job, app=None):
    """Email the requester, if they asked to be told (decision 28).

    Always to the account's own verified address, taken from the job's user —
    never a recipient from the request, which would make the sizer a mail relay.
    A failed job notifies too: silence after opting in reads as "still running".
    """
    if not job.notify_email:
        return
    try:
        from auth import app_base_url, send_email, smtp_configured
        if not smtp_configured():
            return
        user = db.session.get(User, job.user_id)
        if not user or not user.email:
            return
        project = db.session.get(Project, job.project_id)
        name = project.name if project else "your project"
        link = f"{app_base_url()}/#project-{job.project_id}"
        if job.status == JOB_DONE:
            subject = f"Your export for {name} is ready"
            body = (f"The {job.fmt.upper()} export for {name} is ready.\n\n"
                    f"Download it from the project's Exports panel:\n{link}\n\n"
                    f"It stays available for 24 hours, after which you can "
                    f"generate it again.")
        else:
            subject = f"Your export for {name} failed"
            body = (f"The {job.fmt.upper()} export for {name} could not be "
                    f"generated.\n\n{job.error or ''}\n\nOpen the project and "
                    f"try again:\n{link}")
        send_email(user.email, subject, body)
    except Exception:                            # noqa: BLE001 - never fail a job on mail
        if app:
            app.logger.warning("Export notification failed for job %s", job.id)


# ── the loop ─────────────────────────────────────────────────────────────────

def _loop(app):
    while True:
        try:
            with app.app_context():
                requeue_abandoned_jobs()
                job = claim_next_job()
                while job is not None:
                    run_job(job, app)
                    job = claim_next_job()
        except Exception:                        # noqa: BLE001 - keep the worker alive
            try:
                app.logger.warning("Export worker cycle failed:\n%s",
                                   traceback.format_exc())
            except Exception:
                pass
        time.sleep(POLL_SECONDS)


def start_export_worker(app):
    """Start the drain thread once per process."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    thread = threading.Thread(target=_loop, args=(app,), daemon=True,
                              name="export-worker")
    thread.start()
