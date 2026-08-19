"""Deletion paths against the project tables, with foreign keys ENFORCED.

SQLite ignores foreign keys unless asked, which is exactly why two real bugs
survived the rest of the suite: `_delete_user_cascade()` didn't know about
`projects.owner_id` (NOT NULL, and every user has a scratch project), and
`purge_expired()` hard-deleted configurations still referenced by
`configuration_tags` / `replication_links`. On Postgres both raise
IntegrityError — the first breaks admin user-delete, the second aborts the whole
nightly purge.

These tests turn the pragma on so the enforcement matches production.

Run: .venv/bin/python -m pytest tests/test_purge_integrity.py -q
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
from auth_models import Configuration, User, _utcnow  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from project_models import (  # noqa: E402
    ConfigurationTag, ExportJob, Project, ProjectTag, ReplicationLink,
)

PASSWORD = "Abcdef1!xy"
OWNER = "owner@partnerco.example"
OTHER = "other@partnerco.example"


@pytest.fixture()
def app():
    application = appmod.app
    application.config["TESTING"] = True
    application.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False

    with application.app_context():
        # Schema first, with enforcement OFF: tenants and users reference each
        # other, so drop_all can't order the drops legally.
        db.session.execute(db.text("PRAGMA foreign_keys=OFF"))
        db.drop_all()
        db.create_all()
        # Now match production. The in-memory database keeps one connection per
        # thread, so this pragma holds for the whole test.
        db.session.execute(db.text("PRAGMA foreign_keys=ON"))
        db.session.commit()
        enforced = db.session.execute(db.text("PRAGMA foreign_keys")).scalar()
        assert enforced == 1, "these tests are pointless without FK enforcement"

    yield application

    # The in-memory database is shared for the whole pytest session, so leaving
    # enforcement on would break every other module's drop_all() (tenants and
    # users reference each other). Hand it back as we found it.
    with application.app_context():
        db.session.execute(db.text("PRAGMA foreign_keys=OFF"))
        db.session.commit()


def client_for(app, email):
    c = app.test_client()
    c.post("/api/auth/signup",
           json={"email": email, "password": PASSWORD, "accept_privacy": True})
    c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    return c


def _populated_project(c, name="Acme"):
    """A project with two sizings, a tag on one, and a replication link."""
    project = c.post("/api/projects/", json={"name": name}).get_json()
    first = c.post("/api/configs/", json={
        "name": "Site A", "payload": {"mode": "import"},
        "project_id": project["id"]}).get_json()
    second = c.post("/api/configs/", json={
        "name": "Site B", "payload": {"mode": "import"},
        "project_id": project["id"]}).get_json()
    tag = c.post(f"/api/projects/{project['id']}/tags",
                 json={"name": "option-1"}).get_json()
    c.post(f"/api/sizings/{first['id']}/tags", json={"tag_ids": [tag["id"]]})
    c.post(f"/api/sizings/{first['id']}/replication", json={
        "target_configuration_id": second["id"],
        "source_cluster": "", "target_cluster": ""})
    return project, first, second


def test_deleting_a_user_removes_their_projects_and_links(app):
    """Every user owns at least a scratch project, so this path runs for every
    delete — it must not trip over projects.owner_id."""
    c = client_for(app, OWNER)
    project, first, second = _populated_project(c)

    with app.app_context():
        from auth import _delete_user_cascade
        user = User.query.filter_by(email=OWNER).first()
        _delete_user_cascade(user)
        db.session.commit()

        assert User.query.filter_by(email=OWNER).first() is None
        assert db.session.get(Project, project["id"]) is None
        assert db.session.get(Configuration, first["id"]) is None
        assert ProjectTag.query.count() == 0
        assert ConfigurationTag.query.count() == 0
        assert ReplicationLink.query.count() == 0


def test_deleting_a_user_rehomes_a_colleagues_sizing(app):
    """A colleague's sizing living in the deleted user's project must survive —
    deleting the container is not consent to destroy someone else's work."""
    owner = client_for(app, OWNER)
    project, _, _ = _populated_project(owner)

    colleague = client_for(app, OTHER)
    stray = colleague.post("/api/configs/", json={
        "name": "Their option", "payload": {"mode": "manual"}}).get_json()
    with app.app_context():
        db.session.get(Configuration, stray["id"]).project_id = project["id"]
        db.session.commit()

        from auth import _delete_user_cascade
        _delete_user_cascade(User.query.filter_by(email=OWNER).first())
        db.session.commit()

        survivor = db.session.get(Configuration, stray["id"])
        assert survivor is not None, "a colleague's sizing must not be deleted"
        assert survivor.project_id != project["id"]
        assert db.session.get(Project, survivor.project_id).owner_id == survivor.owner_id


def test_purge_removes_tagged_and_linked_configurations(app):
    """The nightly purge hard-deletes aged-out sizings; rows referencing them
    must go first or the whole run aborts."""
    c = client_for(app, OWNER)
    _, first, second = _populated_project(c)

    with app.app_context():
        from auth import RETENTION_DAYS, purge_expired
        old = _utcnow() - timedelta(days=RETENTION_DAYS + 1)
        for cid in (first["id"], second["id"]):
            row = db.session.get(Configuration, cid)
            row.is_deleted = True
            row.deleted_at = old
        db.session.commit()

        result = purge_expired()
        assert result["configs_purged"] == 2
        assert db.session.get(Configuration, first["id"]) is None
        assert ConfigurationTag.query.count() == 0
        assert ReplicationLink.query.count() == 0


def test_purge_clears_failed_export_jobs(app):
    """A failed job never gets expires_at, so filtering on it alone would leave
    those rows in the table forever."""
    c = client_for(app, OWNER)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()

    with app.app_context():
        from auth import _purge_expired_exports
        user = User.query.filter_by(email=OWNER).first()
        db.session.add(ExportJob(
            user_id=user.id, project_id=project["id"], fmt="pptx",
            status="failed", error="boom",
            created_at=_utcnow() - timedelta(days=3)))
        db.session.commit()

        assert _purge_expired_exports() == 1
        assert ExportJob.query.count() == 0
