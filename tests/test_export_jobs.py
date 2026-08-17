"""Bundle export jobs and the comparison view (docs/projects-plan.md §6, §7.2).

The rules worth pinning:

  * a job is claimed atomically, so several gunicorn workers cooperate instead
    of building the same bundle three times over
  * a job orphaned by a restart comes back to the queue
  * the artifact is owner-checked and expiry-checked at download — the daily
    sweep can leave a file on disk past its deadline, so the file existing is
    not proof it is still offered
  * competing options are never summed into a combined total
  * a comparison says when its columns are not comparable

Run: .venv/bin/python -m pytest tests/test_export_jobs.py -q
"""
import os
import sys
from datetime import timedelta

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.pop("SESSION_COOKIE_SECURE", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402
import app as appmod  # noqa: E402
import export_worker  # noqa: E402
from auth_models import Configuration, _utcnow  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from project_models import ExportJob, JOB_DONE, JOB_QUEUED, JOB_RUNNING  # noqa: E402

PASSWORD = "Abcdef1!xy"
SCALE = "sa@scalecomputing.com"
PARTNER = "pm@partnerco.example"


@pytest.fixture()
def app():
    application = appmod.app
    application.config["TESTING"] = True
    application.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    with application.app_context():
        db.drop_all()
        db.create_all()
    return application


def client_for(app, email):
    c = app.test_client()
    c.post("/api/auth/signup",
           json={"email": email, "password": PASSWORD, "accept_privacy": True})
    c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    return c


def _snapshot(nodes=6, cores=192, ram=1536, tb=40.0, model="HE500"):
    return {"clusters": [{
        "name": "Prod",
        "summary": {"active_vms": 40},
        "projection": {"years": 5},
        "recommendation": {
            "model": model, "node_count": nodes,
            "cluster_total": {"cores": cores, "ram_gb": ram, "usable_storage_tb": tb},
            "n_minus_1": {"cores": int(cores * 0.8), "ram_gb": int(ram * 0.8)},
        },
        "refs": {"mode": "validated"},
    }], "tunables": "t1"}


def _sized(c, project_id, name, **kw):
    row = c.post("/api/configs/", json={
        "name": name, "payload": {"mode": "import"}, "project_id": project_id}).get_json()
    c.put(f"/api/sizings/{row['id']}/result", json=_snapshot(**kw))
    return row


# ── claiming ─────────────────────────────────────────────────────────────────

def test_a_job_is_claimed_once(app):
    """Several worker threads poll the same table; only one may take a job."""
    c = client_for(app, SCALE)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()
    sizing = _sized(c, project["id"], "Site A")
    c.post(f"/api/projects/{project['id']}/export",
           json={"format": "pptx", "sizing_ids": [sizing["id"]]})

    with app.app_context():
        first = export_worker.claim_next_job()
        second = export_worker.claim_next_job()
        assert first is not None
        assert second is None, "a queued job must not be claimable twice"
        assert first.status == JOB_RUNNING
        assert first.claimed_by


def test_an_abandoned_job_returns_to_the_queue(app):
    c = client_for(app, SCALE)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()
    sizing = _sized(c, project["id"], "Site A")
    c.post(f"/api/projects/{project['id']}/export",
           json={"format": "pptx", "sizing_ids": [sizing["id"]]})

    with app.app_context():
        job = export_worker.claim_next_job()
        # Simulate the container dying mid-build.
        job.claimed_at = _utcnow() - timedelta(hours=2)
        db.session.commit()

        assert export_worker.requeue_abandoned_jobs() == 1
        assert ExportJob.query.get(job.id).status == JOB_QUEUED
        assert export_worker.claim_next_job() is not None


# ── section assembly ─────────────────────────────────────────────────────────

def test_sections_follow_project_order(app):
    c = client_for(app, SCALE)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()
    one = _sized(c, project["id"], "Option 1")
    two = _sized(c, project["id"], "Option 2")
    c.post("/api/sizings/reorder",
           json={"project_id": project["id"], "sizing_ids": [two["id"], one["id"]]})

    with app.app_context():
        job = ExportJob(user_id=1, project_id=project["id"], fmt="pptx",
                        sizing_ids=[one["id"], two["id"]])
        sections, skipped = export_worker.sections_for(job)
        assert [s["name"] for s in sections] == ["Option 2", "Option 1"], \
            "the exported document must follow the order set in the project view"


def test_sizings_without_a_proposal_are_skipped_not_fatal(app):
    c = client_for(app, SCALE)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()
    good = _sized(c, project["id"], "Site A")
    bare = c.post("/api/configs/", json={
        "name": "Appliance build", "payload": {"mode": "appliance"},
        "project_id": project["id"]}).get_json()

    with app.app_context():
        job = ExportJob(user_id=1, project_id=project["id"], fmt="pptx",
                        sizing_ids=[good["id"], bare["id"]])
        sections, skipped = export_worker.sections_for(job)
        assert [s["name"] for s in sections] == ["Site A"]
        assert skipped == ["Appliance build"]


def test_a_sizing_deleted_after_queueing_does_not_break_the_build(app):
    c = client_for(app, SCALE)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()
    one = _sized(c, project["id"], "Site A")
    two = _sized(c, project["id"], "Site B")

    with app.app_context():
        job = ExportJob(user_id=1, project_id=project["id"], fmt="pptx",
                        sizing_ids=[one["id"], two["id"]])
        Configuration.query.get(two["id"]).is_deleted = True
        db.session.commit()
        sections, _ = export_worker.sections_for(job)
        assert [s["name"] for s in sections] == ["Site A"]


# ── queueing rules ───────────────────────────────────────────────────────────

def test_editable_formats_stay_a_scale_privilege(app):
    c = client_for(app, PARTNER)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()
    sizing = _sized(c, project["id"], "Site A")
    for fmt, expected in (("pptx", 403), ("docx", 403), ("pdf", 202)):
        resp = c.post(f"/api/projects/{project['id']}/export",
                      json={"format": fmt, "sizing_ids": [sizing["id"]]})
        assert resp.status_code == expected, fmt


def test_export_needs_a_selection_and_a_known_format(app):
    c = client_for(app, SCALE)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()
    assert c.post(f"/api/projects/{project['id']}/export",
                  json={"format": "pptx", "sizing_ids": []}).status_code == 400
    assert c.post(f"/api/projects/{project['id']}/export",
                  json={"format": "wat", "sizing_ids": [1]}).status_code == 400


def test_section_limit_is_enforced_before_the_job_runs(app, monkeypatch):
    monkeypatch.setattr(appmod, "MAX_EXPORT_SECTIONS", 1)
    c = client_for(app, SCALE)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()
    one = _sized(c, project["id"], "Site A")
    two = _sized(c, project["id"], "Site B")
    resp = c.post(f"/api/projects/{project['id']}/export",
                  json={"format": "pptx", "sizing_ids": [one["id"], two["id"]]})
    assert resp.status_code == 400
    assert "limit" in resp.get_json()["error"]


# ── artifact access ──────────────────────────────────────────────────────────

def test_a_project_name_cannot_escape_the_artifact_directory(app):
    """Project names are free text. "Acme / Phase 2" would otherwise put a
    directory separator in the path — failing the write, or landing the file
    somewhere other than ARTIFACT_DIR."""
    from export_worker import _safe_stem
    assert "/" not in _safe_stem("Acme / Phase 2")
    assert _safe_stem("../../etc/passwd") == "etc_passwd"
    assert _safe_stem("   ") == "project"
    assert _safe_stem("Acme Corp: Phase 2!") == "Acme_Corp_Phase_2"
    assert len(_safe_stem("x" * 200)) <= 40


def test_a_section_missing_its_projection_is_skipped_not_fatal(app):
    """The generators index projection unconditionally, so one incomplete
    section would otherwise fail the whole bundle."""
    c = client_for(app, SCALE)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()
    good = _sized(c, project["id"], "Site A")
    partial = c.post("/api/configs/", json={
        "name": "Half sized", "payload": {"mode": "import"},
        "project_id": project["id"]}).get_json()
    snap = _snapshot()
    snap["clusters"][0].pop("projection")
    c.put(f"/api/sizings/{partial['id']}/result", json=snap)

    with app.app_context():
        job = ExportJob(user_id=1, project_id=project["id"], fmt="pptx",
                        sizing_ids=[good["id"], partial["id"]])
        sections, skipped = export_worker.sections_for(job)
        assert [s["name"] for s in sections] == ["Site A"]
        assert skipped == ["Half sized"]


def test_another_user_cannot_download_your_export(app):
    owner = client_for(app, SCALE)
    project = owner.post("/api/projects/", json={"name": "Acme"}).get_json()
    sizing = _sized(owner, project["id"], "Site A")
    job = owner.post(f"/api/projects/{project['id']}/export",
                     json={"format": "pptx", "sizing_ids": [sizing["id"]]}).get_json()

    intruder = client_for(app, "someone@else.example")
    assert intruder.get(f"/api/export-jobs/{job['id']}").status_code == 404
    assert intruder.get(f"/api/export-jobs/{job['id']}/file").status_code == 404


def test_an_expired_artifact_is_refused_even_if_the_file_remains(app, tmp_path):
    owner = client_for(app, SCALE)
    project = owner.post("/api/projects/", json={"name": "Acme"}).get_json()
    sizing = _sized(owner, project["id"], "Site A")
    job = owner.post(f"/api/projects/{project['id']}/export",
                     json={"format": "pptx", "sizing_ids": [sizing["id"]]}).get_json()

    artifact = tmp_path / "bundle.pptx"
    artifact.write_bytes(b"still here")
    with app.app_context():
        row = ExportJob.query.get(job["id"])
        row.status = JOB_DONE
        row.artifact_path = str(artifact)
        row.filename = "bundle.pptx"
        row.expires_at = _utcnow() - timedelta(hours=1)
        db.session.commit()

    resp = owner.get(f"/api/export-jobs/{job['id']}/file")
    assert resp.status_code == 410, "presence on disk is not proof it is still offered"
    assert artifact.exists()


def test_purge_removes_expired_artifacts_and_rows(app, tmp_path):
    from auth import _purge_expired_exports
    owner = client_for(app, SCALE)
    project = owner.post("/api/projects/", json={"name": "Acme"}).get_json()
    sizing = _sized(owner, project["id"], "Site A")
    job = owner.post(f"/api/projects/{project['id']}/export",
                     json={"format": "pptx", "sizing_ids": [sizing["id"]]}).get_json()
    artifact = tmp_path / "old.pptx"
    artifact.write_bytes(b"old")

    with app.app_context():
        row = ExportJob.query.get(job["id"])
        row.status = JOB_DONE
        row.artifact_path = str(artifact)
        row.expires_at = _utcnow() - timedelta(hours=1)
        db.session.commit()

        assert _purge_expired_exports() == 1
        assert ExportJob.query.get(job["id"]) is None
        assert not artifact.exists()


# ── comparison ───────────────────────────────────────────────────────────────

def test_alternatives_are_never_summed(app):
    c = client_for(app, SCALE)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()
    one = _sized(c, project["id"], "Option 1", nodes=6)
    two = _sized(c, project["id"], "Option 2", nodes=9)
    for row in (one, two):
        c.post(f"/api/sizings/{row['id']}/role", json={"role": "alternative"})

    data = c.post(f"/api/projects/{project['id']}/compare",
                  json={"sizing_ids": [one["id"], two["id"]]}).get_json()
    assert data["rollup"] is None, "competing options must never produce a combined total"
    assert len(data["rows"]) == 2
    assert data["rows"][0]["totals"]["nodes"] == 6


def test_additive_sizings_are_summed(app):
    c = client_for(app, SCALE)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()
    one = _sized(c, project["id"], "Site A", nodes=6, cores=100)
    two = _sized(c, project["id"], "Site B", nodes=4, cores=80)
    for row in (one, two):
        c.post(f"/api/sizings/{row['id']}/role", json={"role": "additive"})

    data = c.post(f"/api/projects/{project['id']}/compare",
                  json={"sizing_ids": [one["id"], two["id"]]}).get_json()
    assert data["rollup"]["nodes"] == 10
    assert data["rollup"]["cores"] == 180
    assert data["rollup"]["count"] == 2


def test_mixed_roles_and_stale_rows_are_flagged(app):
    c = client_for(app, SCALE)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()
    one = _sized(c, project["id"], "Option 1")
    two = _sized(c, project["id"], "Site B")
    unsized = c.post("/api/configs/", json={
        "name": "Draft", "payload": {"mode": "import"},
        "project_id": project["id"]}).get_json()
    c.post(f"/api/sizings/{one['id']}/role", json={"role": "alternative"})
    c.post(f"/api/sizings/{two['id']}/role", json={"role": "additive"})

    data = c.post(f"/api/projects/{project['id']}/compare",
                  json={"sizing_ids": [one["id"], two["id"], unsized["id"]]}).get_json()
    codes = {w["code"] for w in data["warnings"]}
    assert "mixed_roles" in codes
    assert "not_sized" in codes


def test_cluster_count_is_physical_clusters_not_sections(app):
    """"Number of clusters" is what the customer ends up running.

    A sizing holding one source cluster can still split into several HyperCore
    clusters above max_nodes_per_cluster, so the figure is summed from
    num_clusters rather than counted from the section list — otherwise a
    13-node single-site sizing would report 1 cluster while quoting 2.
    """
    c = client_for(app, SCALE)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()
    row = c.post("/api/configs/", json={
        "name": "All in one", "payload": {"mode": "import"},
        "project_id": project["id"]}).get_json()

    snap = _snapshot(nodes=13)
    snap["clusters"][0]["recommendation"]["num_clusters"] = 2
    snap["clusters"][0]["recommendation"]["cluster_layout"] = [8, 5]
    c.put(f"/api/sizings/{row['id']}/result", json=snap)

    data = c.post(f"/api/projects/{project['id']}/compare",
                  json={"sizing_ids": [row["id"]]}).get_json()
    totals = data["rows"][0]["totals"]
    assert totals["clusters"] == 2, "one section can still be two clusters"
    assert totals["nodes"] == 13
    assert totals["layout"] == [8, 5]


def test_two_source_clusters_report_two_clusters(app):
    """The comparison the feature was asked for: 2 clusters vs 1."""
    c = client_for(app, SCALE)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()

    separate = c.post("/api/configs/", json={
        "name": "Two clusters", "payload": {"mode": "import"},
        "project_id": project["id"]}).get_json()
    snap = _snapshot(nodes=4)
    second = dict(snap["clusters"][0], name="Process")
    snap["clusters"] = [snap["clusters"][0], second]
    c.put(f"/api/sizings/{separate['id']}/result", json=snap)

    combined = c.post("/api/configs/", json={
        "name": "One cluster", "payload": {"mode": "import"},
        "project_id": project["id"]}).get_json()
    c.put(f"/api/sizings/{combined['id']}/result", json=_snapshot(nodes=7))

    data = c.post(f"/api/projects/{project['id']}/compare",
                  json={"sizing_ids": [separate["id"], combined["id"]]}).get_json()
    by_name = {r["name"]: r["totals"] for r in data["rows"]}
    assert by_name["Two clusters"]["clusters"] == 2
    assert by_name["One cluster"]["clusters"] == 1


def test_multi_cluster_sizing_reports_one_total_row(app):
    """A sizing with several clusters has no single node count, so its clusters
    are summed into one row and carried as sub-rows."""
    c = client_for(app, SCALE)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()
    row = c.post("/api/configs/", json={
        "name": "Two sites", "payload": {"mode": "import"},
        "project_id": project["id"]}).get_json()
    snap = _snapshot()
    second = dict(snap["clusters"][0])
    second["name"] = "DR"
    second["recommendation"] = dict(second["recommendation"], node_count=3)
    snap["clusters"] = snap["clusters"] + [second]
    c.put(f"/api/sizings/{row['id']}/result", json=snap)

    data = c.post(f"/api/projects/{project['id']}/compare",
                  json={"sizing_ids": [row["id"]]}).get_json()
    assert data["rows"][0]["totals"]["nodes"] == 9        # 6 + 3
    assert len(data["rows"][0]["clusters"]) == 2
