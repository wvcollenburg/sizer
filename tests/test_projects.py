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


def test_export_language_defaults_to_the_creation_language_and_is_editable(app):
    """The project remembers the language it was created in, and that choice is
    what its exports use — so it has to be changeable when the deck is for a
    customer who reads something else."""
    c = client_for(app, PARTNER_EMAIL)
    project = c.post("/api/projects/", json={"name": "Acme", "lang": "nl"}).get_json()
    assert project["lang"] == "nl"

    updated = c.put(f"/api/projects/{project['id']}", json={"lang": "de"}).get_json()
    assert updated["lang"] == "de"


def test_export_language_must_be_one_we_ship(app):
    """It is handed straight to the export translator; an unknown code would
    silently produce an English document instead of an error."""
    c = client_for(app, PARTNER_EMAIL)
    assert c.post("/api/projects/",
                  json={"name": "Acme", "lang": "xx"}).status_code == 400
    project = make_project(c, "Globex")
    assert c.put(f"/api/projects/{project['id']}",
                 json={"lang": "klingon"}).status_code == 400


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
        assert db.session.get(Project, project["id"]).name == "Acme HQ"


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
        assert db.session.get(Configuration, sizing["id"]).name == "Option 1"


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
        assert db.session.get(Project, project["id"]).salesforce_url is None


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
            assert db.session.get(Project, copy_project_id).salesforce_url is None


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
        assert db.session.get(Project, project["id"]).is_deleted is True
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
        row = db.session.get(Configuration, resp.get_json()["id"])
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


# ── multi-cluster fan-out: untouched flag + option naming ────────────────────

def _fanout_save(c, name, project_id):
    resp = c.post("/api/configs/", json={
        "name": name, "payload": {"mode": "import", "fields": {}},
        "project_id": project_id, "untouched": True,
    })
    assert resp.status_code == 201, resp.get_data(as_text=True)
    return resp.get_json()


def test_fanout_untouched_flag_set_and_cleared_by_human_save(app):
    c = client_for(app, PARTNER_EMAIL)
    pid = make_project(c, "Multi")["id"]

    row = _fanout_save(c, "PROD", pid)
    assert row["untouched"] is True

    # Ordinary saves are never flagged.
    plain = save_sizing(c, "Manual one", project_id=pid)
    assert plain["untouched"] is False

    # Any payload save is the human-review signal: the flag comes off.
    upd = c.put(f"/api/configs/{row['id']}",
                json={"payload": {"mode": "import", "fields": {"a": 1}}})
    assert upd.status_code == 200
    assert upd.get_json()["untouched"] is False


def test_fanout_option_naming_on_collision(app):
    """First import keeps plain cluster names; a re-import of the same file
    reads as alternative options, not mystery duplicates."""
    c = client_for(app, PARTNER_EMAIL)
    pid = make_project(c, "Multi")["id"]

    assert _fanout_save(c, "PROD", pid)["name"] == "PROD"
    assert _fanout_save(c, "PROD", pid)["name"] == "PROD - option 2"
    assert _fanout_save(c, "PROD", pid)["name"] == "PROD - option 3"

    # Scoped to fan-out saves: ordinary saves keep duplicate names untouched.
    assert save_sizing(c, "PROD", project_id=pid)["name"] == "PROD"


# ── legacy multi-cluster split migration ─────────────────────────────────────

def _legacy_multi_payload():
    """A saved-sizing payload in the retired shape: two workload clusters, one
    in-sizing dedicated DR cluster, per-cluster options, VM edits/exclusions
    (indexed into the FULL VM list), and in-sizing replication."""
    vms = [
        {"name": "prod-vm1", "cluster": "PROD", "vcpus": 4},
        {"name": "db-vm1", "cluster": "DB", "vcpus": 8},
        {"name": "prod-vm2", "cluster": "PROD", "vcpus": 2},
        {"name": "db-vm2", "cluster": "DB", "vcpus": 2},
    ]
    return {
        "version": 2, "mode": "import",
        "fields": {"growth-pct": "10", "snapshot-pct": "20"},
        "drCluster": {"enabled": False},
        "import": {
            "originalImportSummary": {"total_vms": 4},
            "importSummary": {"total_vms": 4},
            "importVms": vms,
            # Keys are strings after a JSON round-trip; index into the FULL list.
            "vmConfig": {"2": {"vcpus": 6}, "1": {"model": "db box"}},
            "exclCompute": [3], "exclStorage": [3],
            "includeLocalStorage": True,
            "sourceClusters": [
                {"name": "PROD", "host_count": 2, "vm_count": 2},
                {"name": "DB", "host_count": 1, "vm_count": 2},
                {"name": "DR-Site", "host_count": 0, "vm_count": 0},
            ],
            "clusterBase": {"PROD": {"total_vms": 2, "total_vcpus": 6},
                            "DB": {"total_vms": 2, "total_vcpus": 10},
                            "DR-Site": {"total_vms": 0}},
            "separateClusters": True,
            "clusterOptions": {"DB": {"growth-pct": "25"}},
            "clusterSelectedRec": {"PROD": 1},
            "clusterReplication": {
                "PROD": {"target": "DR-Site", "computePct": 50,
                         "storagePct": 100, "mode": "failover"},
            },
            "dedicatedClusters": ["DR-Site"],
            "activeCluster": "PROD",
        },
    }


def test_split_legacy_multicluster_sizing(app):
    c = client_for(app, PARTNER_EMAIL)
    pid = make_project(c, "Legacy")["id"]
    row = save_sizing(c, "Old combined", project_id=pid,
                      payload=_legacy_multi_payload())

    resp = c.post(f"/api/sizings/{row['id']}/split")
    assert resp.status_code == 201, resp.get_data(as_text=True)
    out = resp.get_json()
    by_name = {r["name"]: r for r in out["created"]}
    assert set(by_name) == {"PROD", "DB", "DR-Site"}
    assert by_name["DR-Site"]["is_dr_target"] is True
    assert by_name["PROD"]["untouched"] is False   # content was human-reviewed

    with app.app_context():
        from project_models import ReplicationLink
        prod = db.session.get(Configuration, by_name["PROD"]["id"])
        dbc = db.session.get(Configuration, by_name["DB"]["id"])

        # PROD: its 2 VMs, the edit on full-list idx 2 remapped to local idx 1.
        pimp = prod.payload["import"]
        assert [v["name"] for v in pimp["importVms"]] == ["prod-vm1", "prod-vm2"]
        assert pimp["vmConfig"] == {"1": {"vcpus": 6}}
        assert pimp["exclCompute"] == [] and pimp["selectedRec"] == 1
        assert pimp["originalImportSummary"]["total_vcpus"] == 6
        assert prod.payload["fields"]["growth-pct"] == "10"

        # DB: exclusion on full-list idx 3 remapped to local idx 1; options
        # overlay applied.
        dimp = dbc.payload["import"]
        assert [v["name"] for v in dimp["importVms"]] == ["db-vm1", "db-vm2"]
        assert dimp["vmConfig"] == {"0": {"model": "db box"}}
        assert dimp["exclCompute"] == [1] and dimp["exclStorage"] == [1]
        assert dbc.payload["fields"]["growth-pct"] == "25"

        # Replication became a project link with whole-sizing endpoints.
        (link,) = ReplicationLink.query.all()
        assert link.source_configuration_id == prod.id
        assert link.target_configuration_id == by_name["DR-Site"]["id"]
        assert (link.compute_pct, link.storage_pct, link.mode) == (50, 100, "failover")
        assert link.source_cluster == "" and link.target_cluster == ""

        # The original is gone from the project (soft-deleted).
        original = db.session.get(Configuration, row["id"])
        assert original.is_deleted is True


def test_split_refuses_single_cluster_sizing(app):
    c = client_for(app, PARTNER_EMAIL)
    pid = make_project(c, "Single")["id"]
    row = save_sizing(c, "Plain", project_id=pid, payload={
        "mode": "import", "fields": {},
        "import": {"importVms": [], "sourceClusters": []},
    })
    resp = c.post(f"/api/sizings/{row['id']}/split")
    assert resp.status_code == 400


def test_split_converts_whole_workload_dr(app):
    """The non-separate path's single DR target becomes a project DR-target
    sizing with one inbound link per workload sizing."""
    c = client_for(app, PARTNER_EMAIL)
    pid = make_project(c, "DRLegacy")["id"]
    payload = _legacy_multi_payload()
    payload["drCluster"] = {"enabled": True, "computePct": 40,
                            "storagePct": 80, "mode": "reserved"}
    payload["import"]["clusterReplication"] = {}
    row = save_sizing(c, "Old with DR", project_id=pid, payload=payload)

    resp = c.post(f"/api/sizings/{row['id']}/split")
    assert resp.status_code == 201
    by_name = {r["name"]: r for r in resp.get_json()["created"]}
    assert "DR target" in by_name and by_name["DR target"]["is_dr_target"] is True

    with app.app_context():
        from project_models import ReplicationLink
        links = ReplicationLink.query.filter_by(
            target_configuration_id=by_name["DR target"]["id"]).all()
        assert len(links) == 2      # PROD and DB both replicate in
        assert {(l.compute_pct, l.storage_pct, l.mode) for l in links} \
            == {(40, 80, "reserved")}


# ── inbound replication reserve + role plumbing ──────────────────────────────

def test_create_config_accepts_explicit_role(app):
    c = client_for(app, PARTNER_EMAIL)
    pid = make_project(c, "Roles")["id"]
    resp = c.post("/api/configs/", json={
        "name": "PROD", "payload": {"mode": "import", "fields": {}},
        "project_id": pid, "untouched": True, "role": "additive",
    })
    assert resp.status_code == 201
    assert resp.get_json()["role"] == "additive"

    bad = c.post("/api/configs/", json={
        "name": "X", "payload": {}, "project_id": pid, "role": "nonsense"})
    assert bad.status_code == 400


def test_split_pieces_are_additive(app):
    c = client_for(app, PARTNER_EMAIL)
    pid = make_project(c, "SplitRoles")["id"]
    row = save_sizing(c, "Old", project_id=pid, payload=_legacy_multi_payload())
    out = c.post(f"/api/sizings/{row['id']}/split").get_json()
    assert {r["role"] for r in out["created"]} == {"additive"}


def test_inbound_reserve_endpoint(app):
    """A workload sizing that is a replication target reports the aggregated
    inbound reserve — the figures the sizer folds into its recommendation."""
    c = client_for(app, PARTNER_EMAIL)
    pid = make_project(c, "Reserve")["id"]
    src = save_sizing(c, "Source", project_id=pid, payload={
        "mode": "import", "fields": {},
        "import": {"importSummary": {"total_vcpus": 100,
                                     "total_vm_provisioned_memory_gb": 400,
                                     "datastore_used_tb": 10.0}},
    })
    tgt = save_sizing(c, "Target", project_id=pid, payload={
        "mode": "import", "fields": {},
        "import": {"importSummary": {"total_vcpus": 8}},
    })

    # No links yet.
    empty = c.get(f"/api/sizings/{tgt['id']}/inbound-reserve").get_json()
    assert empty["has_inbound"] is False

    resp = c.post(f"/api/sizings/{src['id']}/replication", json={
        "target_configuration_id": tgt["id"],
        "compute_pct": 50, "storage_pct": 80, "mode": "reserved",
    })
    assert resp.status_code in (200, 201), resp.get_data(as_text=True)

    d = c.get(f"/api/sizings/{tgt['id']}/inbound-reserve").get_json()
    assert d["has_inbound"] is True
    assert d["reserve"] == {"vcpus": 50.0, "ram_gb": 200.0, "storage_tb": 8.0}
    assert d["mode"] == "reserved"
    assert d["sources"][0]["sizing_name"] == "Source"

    # All-failover links flip the compute basis to full-cluster.
    c.post(f"/api/sizings/{src['id']}/replication", json={
        "target_configuration_id": tgt["id"],
        "compute_pct": 50, "storage_pct": 80, "mode": "failover",
    })
    d2 = c.get(f"/api/sizings/{tgt['id']}/inbound-reserve").get_json()
    assert d2["mode"] == "failover"


# ── tag-based comparison (solution sets) ─────────────────────────────────────

def _snapshot(model, nodes, cores, ram_gb, usable_tb, n1_cores, n1_ram):
    return {"clusters": [{"name": "c", "refs": {"mode": "import"},
                          "recommendation": {
        "model": model, "node_count": nodes, "num_clusters": 1,
        "cluster_layout": [nodes],
        "totals": {"cores": cores, "ram_gb": ram_gb,
                   "usable_storage_tb": usable_tb},
        "n_minus_1": {"cores": n1_cores, "ram_gb": n1_ram},
    }}]}


def test_create_config_tag_creates_and_links_project_tag(app):
    c = client_for(app, PARTNER_EMAIL)
    pid = make_project(c, "Tagged")["id"]
    r1 = c.post("/api/configs/", json={
        "name": "PROD", "payload": {"mode": "import"}, "project_id": pid,
        "untouched": True, "tag": "lo-sept"}).get_json()
    r2 = c.post("/api/configs/", json={
        "name": "DB", "payload": {"mode": "import"}, "project_id": pid,
        "untouched": True, "tag": "lo-sept"}).get_json()

    with app.app_context():
        from project_models import ProjectTag, ConfigurationTag
        (tag,) = ProjectTag.query.filter_by(project_id=pid).all()
        assert tag.name == "lo-sept"
        linked = {l.configuration_id for l in
                  ConfigurationTag.query.filter_by(tag_id=tag.id).all()}
        assert linked == {r1["id"], r2["id"]}


def test_split_pieces_share_a_tag_named_after_the_original(app):
    c = client_for(app, PARTNER_EMAIL)
    pid = make_project(c, "SplitTag")["id"]
    row = save_sizing(c, "Old combined", project_id=pid,
                      payload=_legacy_multi_payload())
    out = c.post(f"/api/sizings/{row['id']}/split").get_json()
    with app.app_context():
        from project_models import ProjectTag, ConfigurationTag
        tag = ProjectTag.query.filter_by(project_id=pid,
                                         name="Old combined").one()
        linked = {l.configuration_id for l in
                  ConfigurationTag.query.filter_by(tag_id=tag.id).all()}
        assert linked == {r["id"] for r in out["created"]}


def test_compare_by_tags_aggregates_solution_sets(app):
    """One column per tag, members summed (DR targets included), member
    sub-rows carried, and no cross-column rollup (rival sets must not add)."""
    c = client_for(app, PARTNER_EMAIL)
    pid = make_project(c, "TagCompare")["id"]

    def sized(name, tag, snapshot, dr=False):
        row = c.post("/api/configs/", json={
            "name": name, "payload": {"mode": "import"}, "project_id": pid,
            "untouched": True, "tag": tag, "role": "additive"}).get_json()
        if dr:
            with app.app_context():
                cfg = db.session.get(Configuration, row["id"])
                cfg.is_dr_target = True
                db.session.commit()
        resp = c.put(f"/api/sizings/{row['id']}/result", json=snapshot)
        assert resp.status_code == 200, resp.get_data(as_text=True)
        return row

    sized("PROD", "set-a", _snapshot("HC5450D", 3, 192, 3072, 24.0, 126, 2048))
    sized("DR", "set-a", _snapshot("HC1650D", 2, 48, 1024, 20.0, 24, 512), dr=True)
    sized("PROD", "set-b", _snapshot("HC3650DF", 3, 192, 3072, 17.0, 126, 2048))

    with app.app_context():
        from project_models import ProjectTag
        tags = {t.name: t.id for t in ProjectTag.query.filter_by(project_id=pid)}

    d = c.post(f"/api/projects/{pid}/compare",
               json={"tag_ids": [tags["set-a"], tags["set-b"]]}).get_json()
    assert d["mode"] == "tags" and d["rollup"] is None
    a, b = d["rows"]
    assert (a["name"], a["member_count"]) == ("set-a", 2)
    # DR target counted into the set's totals.
    assert a["totals"]["nodes"] == 5 and a["totals"]["cores"] == 240
    assert a["totals"]["ram_gb"] == 4096
    assert round(a["totals"]["usable_tb"], 2) == 44.0
    assert sorted(a["totals"]["model"].split(", ")) == ["HC1650D", "HC5450D"]
    assert [m["name"] for m in a["clusters"]] == ["PROD", "DR"]
    assert b["totals"]["nodes"] == 3

    # An unsized member is named in the warnings, not silently zeroed.
    c.post("/api/configs/", json={
        "name": "Unsized", "payload": {"mode": "import"}, "project_id": pid,
        "untouched": True, "tag": "set-b"})
    d2 = c.post(f"/api/projects/{pid}/compare",
                json={"tag_ids": [tags["set-a"], tags["set-b"]]}).get_json()
    assert {"code": "not_sized", "name": "Unsized"} in d2["warnings"]
