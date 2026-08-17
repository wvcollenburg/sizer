"""Project container tests (docs/projects-plan.md steps 2 and 9).

Covers the rules that are invisible until they break:
  - every saved sizing lands in a project; the quick path uses one scratch
    project per user rather than inventing a new one each time
  - a project reached by code is read-only, and every write path enforces that
    server-side (not by hiding buttons)
  - the Salesforce link is absent — not null — from every response a non-scale
    user can obtain, and is never settable by one
  - deleting a project soft-deletes its sizings with it
  - ordering survives, because the exported bundle follows position

Run: .venv/bin/python -m pytest tests/test_projects.py -q
"""
import json
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.pop("SESSION_COOKIE_SECURE", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402
import app as appmod  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from auth_models import Configuration, User  # noqa: E402
from project_models import Project, valid_salesforce_url  # noqa: E402

SCALE_EMAIL = "sa@scalecomputing.com"
PARTNER_EMAIL = "pm@partnerco.example"
COLLEAGUE_EMAIL = "colleague@partnerco.example"
PASSWORD = "Abcdef1!xy"


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
    """A signed-up, signed-in test client for ``email``."""
    c = app.test_client()
    c.post("/api/auth/signup",
           json={"email": email, "password": PASSWORD, "accept_privacy": True})
    c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    return c


def make_project(c, name="Acme HQ", **extra):
    resp = c.post("/api/projects/", json=dict(name=name, **extra))
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp.get_json()


def save_sizing(c, name="Option 1", project_id=None, payload=None):
    body = {"name": name, "payload": payload or {"mode": "manual", "fields": {}}}
    if project_id:
        body["project_id"] = project_id
    resp = c.post("/api/configs/", json=body)
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp.get_json()


# ── every sizing belongs to a project ────────────────────────────────────────

def test_saving_without_a_project_uses_one_scratch_project(app):
    c = client_for(app, PARTNER_EMAIL)
    first = save_sizing(c, "Quick 1")
    second = save_sizing(c, "Quick 2")
    assert first["project_id"] is not None
    assert first["project_id"] == second["project_id"], \
        "each quick sizing must reuse the one scratch project, not create another"

    with app.app_context():
        scratch = Project.query.filter_by(is_scratch=True).all()
        assert len(scratch) == 1
        assert scratch[0].name == "Unfiled"


def test_saving_into_a_named_project(app):
    c = client_for(app, PARTNER_EMAIL)
    project = make_project(c)
    sizing = save_sizing(c, "Option 1", project_id=project["id"])
    assert sizing["project_id"] == project["id"]

    detail = c.get(f"/api/projects/{project['id']}").get_json()
    assert [s["name"] for s in detail["sizings"]] == ["Option 1"]


def test_cannot_save_into_someone_elses_project(app):
    owner = client_for(app, PARTNER_EMAIL)
    project = make_project(owner)
    intruder = client_for(app, "outsider@elsewhere.example")
    resp = intruder.post("/api/configs/", json={
        "name": "sneaky", "payload": {"mode": "manual"},
        "project_id": project["id"]})
    assert resp.status_code in (403, 404)


# ── creation asks for a name only ────────────────────────────────────────────

def test_prepared_by_defaults_to_the_creators_name(app):
    c = app.test_client()
    c.post("/api/auth/signup", json={
        "email": "jane@partnerco.example", "password": PASSWORD,
        "accept_privacy": True, "full_name": "Jane Doe"})
    c.post("/api/auth/login", json={"email": "jane@partnerco.example",
                                    "password": PASSWORD})
    project = make_project(c, "Acme")
    assert project["prepared_by"] == "Jane Doe"


def test_prepared_by_falls_back_to_a_readable_name(app):
    """Accounts predate the name field, so a bare address must still produce
    something presentable on a proposal."""
    c = client_for(app, "john.doe@partnerco.example")
    assert make_project(c, "Acme")["prepared_by"] == "John Doe"


def test_prepared_by_is_editable_and_not_pinned_to_the_account(app):
    c = client_for(app, PARTNER_EMAIL)
    project = make_project(c, "Acme")
    resp = c.put(f"/api/projects/{project['id']}",
                 json={"prepared_by": "Someone Else"})
    assert resp.get_json()["prepared_by"] == "Someone Else"


def test_changing_your_name_does_not_rewrite_existing_projects(app):
    """Renaming yourself must not retro-attribute work already prepared."""
    c = client_for(app, "jane@partnerco.example")
    project = make_project(c, "Acme")
    original = project["prepared_by"]

    c.put("/api/auth/me", json={"full_name": "Jane Married"})
    assert c.get(f"/api/projects/{project['id']}").get_json()["prepared_by"] == original
    # ...but the next project picks up the new name.
    assert make_project(c, "Globex")["prepared_by"] == "Jane Married"


def test_project_creation_requires_only_a_name(app):
    c = client_for(app, PARTNER_EMAIL)
    project = make_project(c, name="Just a name")
    assert project["customer_name"] is None
    assert project["opportunity_ref"] is None
    assert project["description"] is None


def test_project_creation_rejects_empty_name(app):
    c = client_for(app, PARTNER_EMAIL)
    assert c.post("/api/projects/", json={"name": "   "}).status_code == 400


# ── read-only sharing (decision 22) ──────────────────────────────────────────

def test_shared_project_is_read_only_for_a_colleague(app):
    owner = client_for(app, PARTNER_EMAIL)
    project = make_project(owner)
    save_sizing(owner, "Option 1", project_id=project["id"])

    # A colleague sees it only after opting into the organization-wide scope;
    # the default listing is the user's own work.
    colleague = client_for(app, COLLEAGUE_EMAIL)
    assert colleague.get("/api/projects/").get_json() == []
    listing = colleague.get("/api/projects/?scope=tenant").get_json()
    assert [p["id"] for p in listing] == [project["id"]]
    assert listing[0]["can_edit"] is False

    # every write path must refuse, not just the hidden buttons
    assert colleague.put(f"/api/projects/{project['id']}",
                         json={"name": "hijacked"}).status_code == 403
    assert colleague.delete(f"/api/projects/{project['id']}").status_code == 403
    assert colleague.post(f"/api/projects/{project['id']}/tags",
                          json={"name": "x"}).status_code == 403
    assert colleague.post("/api/sizings/reorder",
                          json={"project_id": project["id"],
                                "sizing_ids": []}).status_code == 403

    with app.app_context():
        assert Project.query.get(project["id"]).name == "Acme HQ"


def test_viewer_edit_produces_a_copy_in_their_own_project(app):
    owner = client_for(app, PARTNER_EMAIL)
    project = make_project(owner)
    sizing = save_sizing(owner, "Option 1", project_id=project["id"])

    colleague = client_for(app, COLLEAGUE_EMAIL)
    resp = colleague.post(f"/api/sizings/{sizing['id']}/duplicate", json={})
    assert resp.status_code == 201
    copy = resp.get_json()

    assert copy["id"] != sizing["id"]
    assert copy["code"] != sizing["code"], "a copy is a new sizing, not a shared code"
    assert copy["project_id"] != project["id"], \
        "the copy must land in the viewer's own project, not the original"
    with app.app_context():
        assert Configuration.query.get(sizing["id"]).name == "Option 1"


# ── Salesforce link: scale-only (decision 27) ────────────────────────────────

def _raw(resp):
    """Assert on the raw body so an accidental `"salesforce_url": null` fails."""
    return resp.get_data(as_text=True)


def test_scale_user_can_set_and_see_salesforce_link(app):
    c = client_for(app, SCALE_EMAIL)
    project = make_project(c)
    url = "https://acme.lightning.force.com/lightning/r/Opportunity/006/view"
    resp = c.put(f"/api/projects/{project['id']}", json={"salesforce_url": url})
    assert resp.status_code == 200
    assert resp.get_json()["salesforce_url"] == url
    assert "salesforce_url" in _raw(c.get("/api/projects/"))
    assert "salesforce_url" in _raw(c.get(f"/api/projects/{project['id']}"))


def test_salesforce_link_absent_for_non_scale_owner(app):
    c = client_for(app, PARTNER_EMAIL)
    project = make_project(c)
    for resp in (c.get("/api/projects/"), c.get(f"/api/projects/{project['id']}")):
        assert "salesforce_url" not in _raw(resp), \
            "the key must be omitted, not null — a null still reveals the field"


def test_non_scale_user_cannot_set_salesforce_link(app):
    c = client_for(app, PARTNER_EMAIL)
    project = make_project(c)
    url = "https://acme.lightning.force.com/x"
    resp = c.put(f"/api/projects/{project['id']}", json={"salesforce_url": url})
    assert resp.status_code == 200          # dropped silently, not an error
    assert "salesforce_url" not in _raw(resp)
    with app.app_context():
        assert Project.query.get(project["id"]).salesforce_url is None


def test_salesforce_link_hidden_from_tenant_colleague_and_by_code(app):
    scale = client_for(app, SCALE_EMAIL)
    project = make_project(scale)
    scale.put(f"/api/projects/{project['id']}",
              json={"salesforce_url": "https://acme.my.salesforce.com/006"})

    partner = client_for(app, PARTNER_EMAIL)
    by_code = partner.get(f"/api/projects/code/{project['code']}")
    assert "salesforce_url" not in _raw(by_code)


def test_salesforce_url_validation():
    assert valid_salesforce_url("https://acme.lightning.force.com/x")
    assert valid_salesforce_url("https://acme.my.salesforce.com/006")
    assert not valid_salesforce_url("http://acme.my.salesforce.com/006")   # not https
    assert not valid_salesforce_url("https://evil.example/salesforce.com")
    assert not valid_salesforce_url("https://notsalesforce.com.evil.io/x")
    assert not valid_salesforce_url("")


def test_scale_user_copy_does_not_carry_salesforce_link(app):
    """A copy taken by a non-scale user must carry no trace of the link."""
    scale = client_for(app, SCALE_EMAIL)
    project = make_project(scale)
    scale.put(f"/api/projects/{project['id']}",
              json={"salesforce_url": "https://acme.my.salesforce.com/006"})
    sizing = save_sizing(scale, "Option 1", project_id=project["id"])

    partner = client_for(app, PARTNER_EMAIL)
    resp = partner.post(f"/api/sizings/{sizing['id']}/duplicate", json={})
    if resp.status_code == 201:      # only reachable if the sizing is visible
        copy_project_id = resp.get_json()["project_id"]
        with app.app_context():
            assert Project.query.get(copy_project_id).salesforce_url is None


# ── deletion cascades ────────────────────────────────────────────────────────

def test_deleting_a_project_soft_deletes_its_sizings(app):
    c = client_for(app, PARTNER_EMAIL)
    project = make_project(c)
    save_sizing(c, "Option 1", project_id=project["id"])
    save_sizing(c, "Option 2", project_id=project["id"])

    resp = c.delete(f"/api/projects/{project['id']}")
    assert resp.status_code == 200
    assert resp.get_json()["sizings_deleted"] == 2

    with app.app_context():
        assert Project.query.get(project["id"]).is_deleted is True
        rows = Configuration.query.filter_by(project_id=project["id"]).all()
        assert rows and all(r.is_deleted for r in rows), \
            "sizings must be recoverable with the project, not orphaned"
    assert c.get("/api/projects/").get_json() == []


# ── ordering (the bundle follows position) ───────────────────────────────────

def test_reorder_persists_positions(app):
    c = client_for(app, PARTNER_EMAIL)
    project = make_project(c)
    one = save_sizing(c, "Option 1", project_id=project["id"])
    two = save_sizing(c, "Option 2", project_id=project["id"])

    resp = c.post("/api/sizings/reorder", json={
        "project_id": project["id"], "sizing_ids": [two["id"], one["id"]]})
    assert resp.status_code == 200

    detail = c.get(f"/api/projects/{project['id']}").get_json()
    assert [s["name"] for s in detail["sizings"]] == ["Option 2", "Option 1"]


# ── roles and tags ───────────────────────────────────────────────────────────

def test_role_must_be_a_known_value(app):
    c = client_for(app, PARTNER_EMAIL)
    project = make_project(c)
    sizing = save_sizing(c, "Option 1", project_id=project["id"])

    assert c.post(f"/api/sizings/{sizing['id']}/role",
                  json={"role": "alternative"}).status_code == 200
    assert c.post(f"/api/sizings/{sizing['id']}/role",
                  json={"role": "whatever"}).status_code == 400


def test_new_sizing_inherits_the_project_default_role(app):
    c = client_for(app, PARTNER_EMAIL)
    project = make_project(c)
    c.put(f"/api/projects/{project['id']}", json={"default_role": "additive"})
    sizing = save_sizing(c, "Site B", project_id=project["id"])
    assert sizing["role"] == "additive"


def test_tags_are_scoped_to_their_project(app):
    c = client_for(app, PARTNER_EMAIL)
    first = make_project(c, name="Acme")
    second = make_project(c, name="Globex")
    tag = c.post(f"/api/projects/{first['id']}/tags",
                 json={"name": "option-1"}).get_json()
    sizing = save_sizing(c, "Option 1", project_id=second["id"])

    # A tag from another project must not stick to this sizing.
    resp = c.post(f"/api/sizings/{sizing['id']}/tags", json={"tag_ids": [tag["id"]]})
    assert resp.status_code == 200
    assert resp.get_json()["tags"] == []


def test_tag_names_are_unique_per_project(app):
    c = client_for(app, PARTNER_EMAIL)
    project = make_project(c)
    first = c.post(f"/api/projects/{project['id']}/tags", json={"name": "option-1"})
    again = c.post(f"/api/projects/{project['id']}/tags", json={"name": "option-1"})
    assert first.get_json()["id"] == again.get_json()["id"]


def test_listing_defaults_to_my_own_projects(app):
    """A shared tenant would otherwise bury your engagements under everyone
    else's the moment a second person signs up."""
    mine = client_for(app, PARTNER_EMAIL)
    make_project(mine, "My engagement")

    theirs = client_for(app, COLLEAGUE_EMAIL)
    make_project(theirs, "Their engagement")

    names = [p["name"] for p in mine.get("/api/projects/").get_json()]
    assert names == ["My engagement"]


def test_scope_tenant_shows_the_whole_organization(app):
    mine = client_for(app, PARTNER_EMAIL)
    make_project(mine, "My engagement")
    theirs = client_for(app, COLLEAGUE_EMAIL)
    make_project(theirs, "Their engagement")

    names = {p["name"] for p in
             mine.get("/api/projects/?scope=tenant").get_json()}
    assert names == {"My engagement", "Their engagement"}


def test_a_project_holding_my_sizing_counts_as_mine(app):
    """Ownership of the project and authorship of the sizing are different
    things: a colleague can own the engagement while my sizing lives in it."""
    owner = client_for(app, PARTNER_EMAIL)
    project = make_project(owner, "Shared engagement")

    colleague = client_for(app, COLLEAGUE_EMAIL)
    assert colleague.get("/api/projects/").get_json() == []

    save_sizing(colleague, "My option", project_id=None)      # scratch first
    with app.app_context():
        row = Configuration.query.filter_by(name="My option").first()
        row.project_id = project["id"]
        db.session.commit()

    names = [p["name"] for p in colleague.get("/api/projects/").get_json()]
    assert "Shared engagement" in names


def test_provenance_is_stored_with_the_sizing(app):
    c = client_for(app, PARTNER_EMAIL)
    project = make_project(c)
    meta = {"file_name": "LiveOptics_Acme.xlsx", "file_type": "liveoptics",
            "file_sha256": "a" * 64, "host_count": 5, "vm_count": 78,
            "parser_version": "abc123"}
    resp = c.post("/api/configs/", json={
        "name": "Site A", "payload": {"mode": "import"},
        "project_id": project["id"], "source_meta": meta})
    assert resp.status_code == 201
    assert resp.get_json()["source_meta"]["file_name"] == "LiveOptics_Acme.xlsx"

    with app.app_context():
        row = Configuration.query.get(resp.get_json()["id"])
        assert row.parser_version == "abc123", \
            "the parser version must be its own column so a parser fix can flag re-import"


def test_reimporting_the_same_file_is_flagged(app):
    c = client_for(app, PARTNER_EMAIL)
    project = make_project(c)
    digest = "b" * 64
    c.post("/api/configs/", json={
        "name": "Site A", "payload": {"mode": "import"},
        "project_id": project["id"],
        "source_meta": {"file_name": "Acme.xlsx", "file_sha256": digest}})

    hit = c.post(f"/api/projects/{project['id']}/source-check",
                 json={"file_sha256": digest}).get_json()
    assert hit["duplicate"] is True
    assert hit["sizing_name"] == "Site A"

    # Matching is on content, not filename.
    miss = c.post(f"/api/projects/{project['id']}/source-check",
                  json={"file_sha256": "c" * 64}).get_json()
    assert miss["duplicate"] is False


def test_backfill_files_pre_project_sizings_including_deleted(app):
    """The one-off migration in seed.py. Soft-deleted rows must be filed too:
    they are restorable and still visible to super admins, so leaving them
    unfiled would strand rows behind a NOT NULL expectation (§2.3)."""
    from seed import _backfill_projects

    c = client_for(app, PARTNER_EMAIL)          # creates the user + tenant
    with app.app_context():
        user = User.query.filter_by(email=PARTNER_EMAIL).first()
        for i, deleted in enumerate([False, False, True]):
            db.session.add(Configuration(
                code=f"legacy{i:06d}", name=f"Legacy {i}",
                owner_id=user.id, tenant_id=user.tenant_id,
                payload={"mode": "manual"}, is_deleted=deleted))
        db.session.commit()
        assert Configuration.query.filter(
            Configuration.project_id.is_(None)).count() == 3

        _backfill_projects()

        assert Configuration.query.filter(
            Configuration.project_id.is_(None)).count() == 0, \
            "no sizing may be left unfiled — including soft-deleted ones"
        scratch = Project.query.filter_by(owner_id=user.id, is_scratch=True).all()
        assert len(scratch) == 1, "one scratch project per owner, not one per run"
        assert sorted(c.position for c in Configuration.query.all()) == [0, 1, 2]

        _backfill_projects()                     # idempotent on the next boot
        assert len(Project.query.filter_by(is_scratch=True).all()) == 1


def test_moving_a_sizing_drops_its_project_scoped_tags(app):
    c = client_for(app, PARTNER_EMAIL)
    first = make_project(c, name="Acme")
    second = make_project(c, name="Globex")
    sizing = save_sizing(c, "Option 1", project_id=first["id"])
    tag = c.post(f"/api/projects/{first['id']}/tags",
                 json={"name": "option-1"}).get_json()
    c.post(f"/api/sizings/{sizing['id']}/tags", json={"tag_ids": [tag["id"]]})

    resp = c.post(f"/api/sizings/{sizing['id']}/move",
                  json={"project_id": second["id"]})
    assert resp.status_code == 200
    detail = c.get(f"/api/projects/{second['id']}").get_json()
    assert detail["sizings"][0]["tags"] == [], \
        "tags belong to the old project's vocabulary and must not follow"
