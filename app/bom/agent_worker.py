"""Background worker for BOM agent jobs (bom_models.BomAgentJob).

The check route answers an unrecognised upload with 202 and a queued job;
this thread picks the job up, has the Claude agent read the file
(bom/agent_ingest.py), runs the normal check on the result and stores it as
an ordinary BomCheck marked AGENT_FORMAT. Claiming and requeueing reuse
export_worker's functions (atomic UPDATE ... SKIP LOCKED on Postgres, since
every gunicorn process runs one of these threads), so a restart mid-read
puts the job back in the queue instead of leaving it "running" forever.

Its own thread rather than a second job type in the export loop: a ten-sizing
PDF bundle can take minutes, and a partner waiting on a one-page BOM should
not queue behind it.
"""
import os
import tempfile
import threading
import time
import traceback
from datetime import timedelta

from auth_models import Configuration, _utcnow
from bom import agent_ingest
from bom_models import (AGENT_FORMAT, BomAgentJob, BomCheck, JOB_DONE,
                        JOB_FAILED)
from database import db

POLL_SECONDS = int(os.environ.get("BOM_AGENT_WORKER_POLL", "3"))
# The model call times out at 300 s and retries twice; past this the claimer
# is presumed dead and the job goes back to the queue.
CLAIM_TIMEOUT = timedelta(minutes=20)

_worker_started = False
_worker_lock = threading.Lock()


def _live_sizing(job):
    """The sizing the job was queued against, if it is still a live member
    of the project (it may have been deleted or moved meanwhile)."""
    if not job.configuration_id:
        return None
    sizing = db.session.get(Configuration, job.configuration_id)
    if sizing is None or sizing.is_deleted or sizing.project_id != job.project_id:
        return None
    return sizing


def store_agent_check(*, project_id, user, name, filename, digest, bom, meta, sizing):
    """Create the BomCheck for an agent-read BOM (worker and the route's
    same-file cache both land here). Caller commits."""
    from bom.check import run_check
    check = BomCheck(
        project_id=project_id, user_id=user.id, tenant_id=user.tenant_id,
        name=name, filename=(filename or "")[:200], file_sha256=digest,
        file_format=AGENT_FORMAT, vendor=bom.vendor, normalized=bom.to_dict(),
        ingest_meta=meta, history=[],
    )
    check.apply_result(run_check(bom, sizing), sizing)
    check.history = [check.history_entry()]
    db.session.add(check)
    db.session.flush()
    return check


def _merged(job, meta):
    """The agent's usage/grounding meta, keeping what the route recorded when
    the job was queued (``pdf_local``: why the local PDF parse was not
    trusted)."""
    out = dict(meta or {})
    local = (job.meta or {}).get("pdf_local")
    if local:
        out["pdf_local"] = local
    return out


def _audit(job, outcome_text):
    from auth import audit
    meta = job.meta or {}
    audit("bom_agent", "job #%d project #%d file %s (%s): %s; model %s, tokens in/out %s/%s" % (
        job.id, job.project_id, job.file_sha256[:12], job.filename[:80], outcome_text,
        meta.get("model") or "-", meta.get("input_tokens", 0), meta.get("output_tokens", 0)),
        actor=job.user)


def run_agent_job(job, app=None, client=None):
    """Process one claimed job, recording the outcome on the row. The upload
    bytes are dropped in the same commit that records it."""
    path = None
    try:
        if not job.content:
            raise agent_ingest.AgentError("The uploaded file is no longer available. Upload it again.")
        ext = agent_ingest.extension(job.filename)
        fd, path = tempfile.mkstemp(suffix=ext)
        with os.fdopen(fd, "wb") as fh:
            fh.write(job.content)
        outcome = agent_ingest.read_document(path, job.filename, client=client,
                                             lang=job.lang or "en")
        job.meta = _merged(job, outcome.meta)
        check = store_agent_check(
            project_id=job.project_id, user=job.user, name=job.name,
            filename=job.filename, digest=job.file_sha256, bom=outcome.bom,
            meta=job.meta, sizing=_live_sizing(job))
        job.check_id = check.id
        job.status = JOB_DONE
        _audit(job, "check #%d, %s" % (check.id, check.technical_verdict))
    except agent_ingest.AgentTemplateError as exc:
        db.session.rollback()
        job.meta = _merged(job, exc.meta)
        job.status = JOB_FAILED
        job.error = str(exc)[:1000]
        job.template = exc.template
        _audit(job, "template round trip failed")
    except agent_ingest.AgentError as exc:
        db.session.rollback()
        job.meta = _merged(job, exc.meta)
        job.status = JOB_FAILED
        job.error = str(exc)[:1000]
        _audit(job, "failed: %s" % str(exc)[:120])
    except Exception as exc:                     # noqa: BLE001 - recorded, not raised
        db.session.rollback()
        job.status = JOB_FAILED
        job.error = ("The BOM agent is unavailable right now. Try again later, or fill "
                     "in the blank template.")
        _audit(job, "error: %s" % type(exc).__name__)
        if app:
            app.logger.warning("BOM agent job %s failed: %s\n%s", job.id, exc,
                               traceback.format_exc())
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        job.content = None
        job.finished_at = _utcnow()
        db.session.commit()
        _notify(job, app)


def _notify(job, app=None):
    """Email the uploader when they asked to be told: always their own
    account address, never one from the request (no mail relay). A failed
    job notifies too; silence after opting in reads as "still running"."""
    if not job.notify_email:
        return
    try:
        from auth import app_base_url, send_email, smtp_configured
        if not smtp_configured() or not job.user or not job.user.email:
            return
        name = job.project.name if job.project else "your project"
        link = f"{app_base_url()}/#project-{job.project_id}"
        if job.status == JOB_DONE:
            subject = f"Your BOM check for {name} is ready"
            body = (f"The BOM check of {job.name} in {name} is done.\n\n"
                    f"The file was in a layout the checker does not know, so a Claude "
                    f"agent read it. Review the parts it found before relying on the "
                    f"result.\n\nOpen the project:\n{link}")
        else:
            subject = f"Your BOM check for {name} failed"
            body = (f"The BOM check of {job.name} in {name} could not be completed.\n\n"
                    f"{job.error or ''}\n\nOpen the project:\n{link}")
        send_email(job.user.email, subject, body)
    except Exception:                            # noqa: BLE001 - never fail a job on mail
        if app:
            app.logger.warning("BOM agent notification failed for job %s", job.id)


def _loop(app):
    from export_worker import claim_next_job, requeue_abandoned_jobs
    while True:
        try:
            with app.app_context():
                requeue_abandoned_jobs(BomAgentJob, CLAIM_TIMEOUT)
                job = claim_next_job(BomAgentJob)
                while job is not None:
                    run_agent_job(job, app)
                    job = claim_next_job(BomAgentJob)
        except Exception:                        # noqa: BLE001 - keep the worker alive
            try:
                app.logger.warning("BOM agent worker cycle failed:\n%s", traceback.format_exc())
            except Exception:
                pass
        time.sleep(POLL_SECONDS)


def start_bom_agent_worker(app):
    """Start the drain thread once per process."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
    thread = threading.Thread(target=_loop, args=(app,), daemon=True,
                              name="bom-agent-worker")
    thread.start()
