"""Cross-sizing replication partners (docs/projects-plan.md §8.5).

Replication between clusters of one import already worked; these cover lifting
it to the project, where the DR site usually arrives as its own Live Optics
file. The rules that matter:

  * a partner must be in the same project (a link across projects would pull
    one customer's demand into another's sizing)
  * mutual A<->B links are allowed — the reserve comes from each side's demand,
    never from the other's sized result, so there is nothing circular
  * changing a source's workload marks its DR targets stale, which is the whole
    reason this touches the fingerprint at all (decision 31)
  * a sizing others replicate to cannot be deleted or moved out (decision 32)

Run: .venv/bin/python -m pytest tests/test_replication.py -q
"""
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
from auth_models import Configuration  # noqa: E402
from project_models import ReplicationLink  # noqa: E402

PASSWORD = "Abcdef1!xy"
OWNER = "owner@partnerco.example"


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


@pytest.fixture()
def c(app):
    client = app.test_client()
    client.post("/api/auth/signup",
                json={"email": OWNER, "password": PASSWORD, "accept_privacy": True})
    client.post("/api/auth/login", json={"email": OWNER, "password": PASSWORD})
    return client


def _project(c, name="Acme"):
    return c.post("/api/projects/", json={"name": name}).get_json()


def _sizing(c, name, project_id, payload=None):
    return c.post("/api/configs/", json={
        "name": name, "payload": payload or {"mode": "import", "vms": [1, 2, 3]},
        "project_id": project_id}).get_json()


def _link(c, source, target, **extra):
    body = {"target_configuration_id": target["id"], "source_cluster": "Prod",
            "target_cluster": "DR"}
    body.update(extra)
    return c.post(f"/api/sizings/{source['id']}/replication", json=body)


def _snapshot():
    return {"clusters": [{"name": "Prod", "summary": {}, "recommendation": {},
                          "projection": {},
                          "refs": {"mode": "validated"}}]}


# ── linking ──────────────────────────────────────────────────────────────────

def test_a_cluster_can_replicate_to_a_cluster_in_another_sizing(app, c):
    project = _project(c)
    site_a = _sizing(c, "Site A", project["id"])
    site_b = _sizing(c, "Site B", project["id"])

    resp = _link(c, site_a, site_b)
    assert resp.status_code == 201
    link = resp.get_json()
    assert link["target_configuration_id"] == site_b["id"]
    # Qualified labels: "Prod" in two sizings must not read as one cluster.
    assert link["source_label"] == "Site A — Prod"
    assert link["target_label"] == "Site B — DR"

    detail = c.get(f"/api/projects/{project['id']}").get_json()
    assert len(detail["replication_links"]) == 1


def test_partner_must_be_in_the_same_project(app, c):
    first = _project(c, "Acme")
    second = _project(c, "Globex")
    site_a = _sizing(c, "Site A", first["id"])
    stranger = _sizing(c, "Other customer", second["id"])

    resp = _link(c, site_a, stranger)
    assert resp.status_code == 400
    assert "same project" in resp.get_json()["error"]


def test_a_cluster_cannot_replicate_to_itself(app, c):
    project = _project(c)
    site_a = _sizing(c, "Site A", project["id"])
    resp = c.post(f"/api/sizings/{site_a['id']}/replication", json={
        "target_configuration_id": site_a["id"],
        "source_cluster": "Prod", "target_cluster": "Prod"})
    assert resp.status_code == 400


def test_mutual_replication_is_allowed(app, c):
    """Both sites protecting each other is what customers actually buy, and it
    is well-defined because each reserve comes from the other's demand."""
    project = _project(c)
    site_a = _sizing(c, "Site A", project["id"])
    site_b = _sizing(c, "Site B", project["id"])

    assert _link(c, site_a, site_b).status_code == 201
    assert _link(c, site_b, site_a).status_code == 201
    detail = c.get(f"/api/projects/{project['id']}").get_json()
    assert len(detail["replication_links"]) == 2


def test_one_target_per_source_cluster(app, c):
    project = _project(c)
    site_a = _sizing(c, "Site A", project["id"])
    site_b = _sizing(c, "Site B", project["id"])
    site_c = _sizing(c, "Site C", project["id"])

    _link(c, site_a, site_b)
    _link(c, site_a, site_c)          # same source cluster, retargeted
    with app.app_context():
        links = ReplicationLink.query.filter_by(
            source_configuration_id=site_a["id"]).all()
        assert len(links) == 1
        assert links[0].target_configuration_id == site_c["id"]


def test_mode_and_percentages_are_validated(app, c):
    project = _project(c)
    site_a = _sizing(c, "Site A", project["id"])
    site_b = _sizing(c, "Site B", project["id"])

    assert _link(c, site_a, site_b, mode="nonsense").status_code == 400
    link = _link(c, site_a, site_b, mode="failover",
                 compute_pct=500, storage_pct=-20).get_json()
    assert link["mode"] == "failover"
    assert link["compute_pct"] == 100      # clamped, not rejected
    assert link["storage_pct"] == 0


def test_clearing_a_link(app, c):
    project = _project(c)
    site_a = _sizing(c, "Site A", project["id"])
    site_b = _sizing(c, "Site B", project["id"])
    _link(c, site_a, site_b)

    resp = c.delete(f"/api/sizings/{site_a['id']}/replication?source_cluster=Prod")
    assert resp.status_code == 200
    detail = c.get(f"/api/projects/{project['id']}").get_json()
    assert detail["replication_links"] == []


# ── workload-less DR target (decision 30) ────────────────────────────────────

def test_a_dr_target_sizing_can_exist_without_a_workload(app, c):
    project = _project(c)
    resp = c.post(f"/api/projects/{project['id']}/dr-target",
                  json={"name": "DR site"})
    assert resp.status_code == 201
    target = resp.get_json()
    assert target["is_dr_target"] is True

    site_a = _sizing(c, "Site A", project["id"])
    assert _link(c, site_a, target).status_code == 201


# ── staleness propagation (decision 31) ──────────────────────────────────────

def test_editing_a_source_marks_its_dr_target_stale(app, c):
    """The reason this feature touches the fingerprint at all: exclude VMs from
    the source and the target's inbound reserve is silently wrong."""
    project = _project(c)
    site_a = _sizing(c, "Site A", project["id"])
    site_b = _sizing(c, "Site B", project["id"])
    _link(c, site_a, site_b)

    assert c.put(f"/api/sizings/{site_b['id']}/result",
                 json=_snapshot()).get_json()["cache"] == "fresh"

    # Source workload changes — fewer VMs in scope.
    c.put(f"/api/configs/{site_a['id']}",
          json={"payload": {"mode": "import", "vms": [1]}})

    detail = c.get(f"/api/projects/{project['id']}").get_json()
    target_row = next(s for s in detail["sizings"] if s["id"] == site_b["id"])
    assert target_row["stale"] is True, \
        "a changed source must mark everything replicating into it stale"


def test_renaming_a_source_does_not_churn_its_target(app, c):
    """The digest follows the payload, so cosmetic edits don't cause a refresh
    storm across the project."""
    project = _project(c)
    site_a = _sizing(c, "Site A", project["id"])
    site_b = _sizing(c, "Site B", project["id"])
    _link(c, site_a, site_b)
    c.put(f"/api/sizings/{site_b['id']}/result", json=_snapshot())

    c.put(f"/api/configs/{site_a['id']}", json={"name": "Site A (HQ)"})

    detail = c.get(f"/api/projects/{project['id']}").get_json()
    target_row = next(s for s in detail["sizings"] if s["id"] == site_b["id"])
    assert target_row["stale"] is False


def test_changing_the_link_terms_marks_the_target_stale(app, c):
    project = _project(c)
    site_a = _sizing(c, "Site A", project["id"])
    site_b = _sizing(c, "Site B", project["id"])
    _link(c, site_a, site_b, storage_pct=100)
    c.put(f"/api/sizings/{site_b['id']}/result", json=_snapshot())

    _link(c, site_a, site_b, storage_pct=50)      # half the data now reserved

    detail = c.get(f"/api/projects/{project['id']}").get_json()
    target_row = next(s for s in detail["sizings"] if s["id"] == site_b["id"])
    assert target_row["stale"] is True


def test_a_sizing_with_no_inbound_links_is_unaffected(app, c):
    project = _project(c)
    lonely = _sizing(c, "Standalone", project["id"])
    assert c.put(f"/api/sizings/{lonely['id']}/result",
                 json=_snapshot()).get_json()["cache"] == "fresh"
    detail = c.get(f"/api/projects/{project['id']}").get_json()
    assert detail["sizings"][0]["stale"] is False


# ── deletion and move guards (decision 32) ───────────────────────────────────

def test_cannot_delete_a_sizing_others_replicate_to(app, c):
    project = _project(c)
    site_a = _sizing(c, "Site A", project["id"])
    site_b = _sizing(c, "Site B", project["id"])
    _link(c, site_a, site_b)

    resp = c.delete(f"/api/configs/{site_b['id']}")
    assert resp.status_code == 409
    assert resp.get_json()["replicated_from"] == ["Site A"]
    with app.app_context():
        assert db.session.get(Configuration, site_b["id"]).is_deleted is False


def test_can_delete_once_the_link_is_removed(app, c):
    project = _project(c)
    site_a = _sizing(c, "Site A", project["id"])
    site_b = _sizing(c, "Site B", project["id"])
    _link(c, site_a, site_b)
    c.delete(f"/api/sizings/{site_a['id']}/replication?source_cluster=Prod")

    assert c.delete(f"/api/configs/{site_b['id']}").status_code == 200


def test_cannot_move_a_sizing_others_replicate_to(app, c):
    first = _project(c, "Acme")
    second = _project(c, "Globex")
    site_a = _sizing(c, "Site A", first["id"])
    site_b = _sizing(c, "Site B", first["id"])
    _link(c, site_a, site_b)

    resp = c.post(f"/api/sizings/{site_b['id']}/move",
                  json={"project_id": second["id"]})
    assert resp.status_code == 409


def test_moving_a_source_drops_its_own_outbound_links(app, c):
    """Links may not span projects, so a source's own links cannot survive."""
    first = _project(c, "Acme")
    second = _project(c, "Globex")
    site_a = _sizing(c, "Site A", first["id"])
    site_b = _sizing(c, "Site B", first["id"])
    _link(c, site_a, site_b)

    assert c.post(f"/api/sizings/{site_a['id']}/move",
                  json={"project_id": second["id"]}).status_code == 200
    with app.app_context():
        assert ReplicationLink.query.filter_by(
            source_configuration_id=site_a["id"]).count() == 0


def test_deleting_the_whole_project_still_cascades(app, c):
    """Source and target die together, so nothing is left dangling — the block
    applies to individual sizings, not to the project itself."""
    project = _project(c)
    site_a = _sizing(c, "Site A", project["id"])
    site_b = _sizing(c, "Site B", project["id"])
    _link(c, site_a, site_b)

    resp = c.delete(f"/api/projects/{project['id']}")
    assert resp.status_code == 200
    assert resp.get_json()["sizings_deleted"] == 2
