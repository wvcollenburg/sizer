"""Result-cache fingerprint tests (docs/projects-plan.md §3).

The fingerprint is what makes a stored result safe to export without opening
the sizing. These tests pin the four behaviours it exists for:

  * editing a catalog row the sizing USED marks it stale
  * editing an unrelated catalog row does NOT (decision 6 — otherwise every
    saved sizing in the system goes stale each time a CPU is added)
  * changing a tunable marks everything stale
  * changing the sizing maths marks everything stale, with no version constant
    to remember (decision 9)

Plus the two rules that are easy to get backwards: a read-only viewer may
still store a refreshed result, and a parser change is reported as
"re-import needed" rather than as ordinary staleness (§3.3).

Run: .venv/bin/python -m pytest tests/test_fingerprint.py -q
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
import fingerprint as fp  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from auth_models import Configuration  # noqa: E402
from orm_models import (  # noqa: E402
    CpuCatalog, Model, ModelCpuOption, RamOption, SizingSetting,
    StorageConfig, StorageConfigDrive, DriveCatalog,
)

PASSWORD = "Abcdef1!xy"
OWNER = "owner@partnerco.example"
COLLEAGUE = "mate@partnerco.example"


@pytest.fixture()
def app():
    application = appmod.app
    application.config["TESTING"] = True
    application.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    with application.app_context():
        db.drop_all()
        db.create_all()
        _seed_two_models()
    return application


def _seed_two_models():
    """Two appliance models with their own CPU, so "used" vs "unrelated" can be
    told apart."""
    nvme = DriveCatalog(drive_type="NVMe", size_tb=15.36)
    db.session.add(nvme)
    db.session.flush()

    # Catalog descriptions are bare; the resolved option desc is
    # "{quantity} x {description}", which is what refs record.
    for name, desc, cores in (("HE153", "Xeon 4310", 12),
                              ("HE500", "Xeon 6338", 32)):
        cpu = CpuCatalog(description=desc, cores=cores, threads=cores * 2, ghz=2.4)
        model = Model(name=name, form_factor="1U", chassis="single",
                      status="current", category="edge", min_nodes=1)
        db.session.add_all([cpu, model])
        db.session.flush()
        storage = StorageConfig(model_id=model.id, storage_type="nvme_only",
                                drives_per_node=1)
        db.session.add(storage)
        db.session.flush()
        db.session.add(StorageConfigDrive(storage_config_id=storage.id,
                                          drive_id=nvme.id))
        db.session.add(ModelCpuOption(model_id=model.id, cpu_id=cpu.id, quantity=1))
        db.session.add(RamOption(model_id=model.id, size_gb=256))
    db.session.commit()


def _refs(model="HE153", cpu_desc="1 x Xeon 4310"):
    return {"mode": "appliance", "model": model, "cpu_desc": cpu_desc,
            "selection": {"ram_gb": 256, "nvme_tb": 15.36, "node_count": 3}}


def _snapshot(refs=None):
    return {"clusters": [{"name": "Cluster A", "summary": {}, "recommendation": {},
                          "projection": {}, "refs": refs or _refs()}]}


# ── catalog sensitivity ──────────────────────────────────────────────────────

def test_editing_a_used_catalog_row_marks_the_sizing_stale(app):
    with app.app_context():
        before = fp.fingerprint_snapshot(_snapshot())
        cpu = CpuCatalog.query.filter_by(description="Xeon 4310").first()
        cpu.cores = 16                       # the CPU this sizing selected
        db.session.commit()
        assert fp.fingerprint_snapshot(_snapshot()) != before


def test_editing_an_unrelated_catalog_row_does_not(app):
    with app.app_context():
        before = fp.fingerprint_snapshot(_snapshot())
        other = CpuCatalog.query.filter_by(description="Xeon 6338").first()
        other.cores = 64                     # a different model's CPU
        db.session.commit()
        assert fp.fingerprint_snapshot(_snapshot()) == before, \
            "an unrelated catalog edit must not invalidate every saved sizing"


def test_adding_a_new_cpu_to_the_catalog_changes_nothing(app):
    with app.app_context():
        before = fp.fingerprint_snapshot(_snapshot())
        db.session.add(CpuCatalog(description="Xeon 9999", cores=96,
                                  threads=192, ghz=2.0))
        db.session.commit()
        assert fp.fingerprint_snapshot(_snapshot()) == before


def test_changing_the_selection_changes_the_fingerprint(app):
    with app.app_context():
        a = fp.fingerprint_snapshot(_snapshot())
        other = _refs()
        other["selection"]["ram_gb"] = 512
        assert fp.fingerprint_snapshot(_snapshot(other)) != a


def test_withdrawn_model_is_permanently_stale_but_stable(app):
    with app.app_context():
        refs = _refs(model="HE-GONE")
        first = fp.fingerprint_snapshot(_snapshot(refs))
        second = fp.fingerprint_snapshot(_snapshot(refs))
        assert first == second, "a missing model must not flap between refreshes"
        assert first != fp.fingerprint_snapshot(_snapshot())


# ── tunables and engine ──────────────────────────────────────────────────────

def test_changing_a_tunable_marks_everything_stale(app):
    with app.app_context():
        before = fp.fingerprint_snapshot(_snapshot())
        setting = SizingSetting.query.first()
        if setting is None:
            setting = SizingSetting(key="os_core_overhead", value=2)
            db.session.add(setting)
        else:
            setting.value = (setting.value or 0) + 1
        db.session.commit()
        assert fp.fingerprint_snapshot(_snapshot()) != before


def test_engine_version_tracks_the_sizing_modules(app):
    """No version constant to bump: the hash follows the files themselves."""
    assert fp.ENGINE_VERSION and len(fp.ENGINE_VERSION) == 16
    assert "app.py" not in fp.ENGINE_MODULES, \
        "hashing app.py would invalidate every result on an unrelated route edit"
    assert "calc.py" in fp.ENGINE_MODULES


def test_changing_the_engine_changes_every_fingerprint(app, tmp_path, monkeypatch):
    with app.app_context():
        before = fp.fingerprint_snapshot(_snapshot())
        monkeypatch.setattr(fp, "ENGINE_VERSION", "0" * 16)
        assert fp.fingerprint_snapshot(_snapshot()) != before


def test_parser_version_is_separate_from_the_engine(app):
    """A parser fix cannot be repaired by recalculating, so it must not present
    as ordinary staleness (§3.3)."""
    assert fp.PARSER_VERSION != fp.ENGINE_VERSION
    for module in fp.PARSER_MODULES:
        assert module not in fp.ENGINE_MODULES


# ── validated + manual modes ─────────────────────────────────────────────────

def test_validated_sizings_ignore_the_catalog(app):
    with app.app_context():
        snap = _snapshot({"mode": "validated"})
        before = fp.fingerprint_snapshot(snap)
        cpu = CpuCatalog.query.filter_by(description="Xeon 4310").first()
        cpu.cores = 99
        db.session.commit()
        assert fp.fingerprint_snapshot(snap) == before, \
            "software-only maths never reads the catalog, so it cannot go stale from it"


def test_snapshot_without_refs_is_untrusted(app):
    with app.app_context():
        assert fp.fingerprint_snapshot({"clusters": [{"summary": {}}]}) is None
        assert fp.fingerprint_snapshot({}) is None


# ── the API surface ──────────────────────────────────────────────────────────

def _client(app, email):
    c = app.test_client()
    c.post("/api/auth/signup",
           json={"email": email, "password": PASSWORD, "accept_privacy": True})
    c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    return c


def _save(c, name="Option 1", project_id=None):
    body = {"name": name, "payload": {"mode": "appliance"}}
    if project_id:
        body["project_id"] = project_id
    return c.post("/api/configs/", json=body).get_json()


def test_storing_a_result_marks_the_sizing_fresh(app):
    c = _client(app, OWNER)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()
    sizing = _save(c, project_id=project["id"])

    resp = c.put(f"/api/sizings/{sizing['id']}/result",
                 json=_snapshot() | {"totals": {"nodes": 3}})
    assert resp.status_code == 200
    assert resp.get_json()["cache"] == "fresh"

    detail = c.get(f"/api/projects/{project['id']}").get_json()
    assert detail["sizings"][0]["stale"] is False


def test_a_catalog_edit_shows_up_as_stale_in_the_project(app):
    c = _client(app, OWNER)
    project = c.post("/api/projects/", json={"name": "Acme"}).get_json()
    sizing = _save(c, project_id=project["id"])
    c.put(f"/api/sizings/{sizing['id']}/result", json=_snapshot())

    with app.app_context():
        cpu = CpuCatalog.query.filter_by(description="Xeon 4310").first()
        cpu.ghz = 3.9
        db.session.commit()

    detail = c.get(f"/api/projects/{project['id']}").get_json()
    assert detail["sizings"][0]["stale"] is True
    assert detail["sizings"][0]["cache"] == "stale"


def test_a_result_without_refs_is_refused(app):
    c = _client(app, OWNER)
    sizing = _save(c)
    resp = c.put(f"/api/sizings/{sizing['id']}/result",
                 json={"clusters": [{"summary": {}}]})
    assert resp.status_code == 400


def test_read_only_viewer_may_still_refresh_a_result(app):
    """Refresh is the only route to current numbers; blocking it would leave a
    shared project permanently uncomparable (§4)."""
    owner = _client(app, OWNER)
    project = owner.post("/api/projects/", json={"name": "Acme"}).get_json()
    sizing = _save(owner, project_id=project["id"])

    viewer = _client(app, COLLEAGUE)          # same tenant, read-only
    assert viewer.put(f"/api/projects/{project['id']}",
                      json={"name": "nope"}).status_code == 403
    assert viewer.put(f"/api/sizings/{sizing['id']}/result",
                      json=_snapshot()).status_code == 200


def test_refresh_cannot_smuggle_an_edit(app):
    """The result route accepts a result and nothing else."""
    owner = _client(app, OWNER)
    project = owner.post("/api/projects/", json={"name": "Acme"}).get_json()
    sizing = _save(owner, "Option 1", project_id=project["id"])

    viewer = _client(app, COLLEAGUE)
    viewer.put(f"/api/sizings/{sizing['id']}/result", json=dict(
        _snapshot(), name="renamed", payload={"mode": "hacked"},
        role="additive", notes="injected"))

    with app.app_context():
        row = Configuration.query.get(sizing["id"])
        assert row.name == "Option 1"
        assert row.payload == {"mode": "appliance"}
        assert row.role is None
        assert row.notes is None
