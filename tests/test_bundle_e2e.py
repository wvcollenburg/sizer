"""End-to-end bundle build: engine → stored snapshot → document on disk.

Every other test in this area uses hand-made snapshots, which is exactly how a
bundle can pass its tests and still fail for a user: the generators read fields
those fixtures don't have. This one runs the real recommendation engine, stores
what it produces, and builds an actual PowerPoint and Word file.

It seeds a small catalog rather than calling seed_all(), which needs Postgres
([[seed-migrate-postgres-only]]).

Run: .venv/bin/python -m pytest tests/test_bundle_e2e.py -q
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
import export_worker  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from orm_models import (  # noqa: E402
    CpuCatalog, DriveCatalog, DriveTypeIops, Model, ModelCpuOption, RamOption,
    StorageConfig, StorageConfigDrive,
)
from project_models import ExportJob  # noqa: E402
from recommend import generate_recommendations  # noqa: E402

SCALE = "sa@scalecomputing.com"
PASSWORD = "Abcdef1!xy"

# A real Live Optics summary carries all of these; the engine reads them by
# subscript, so a missing one is a KeyError rather than a default.
SUMMARY = {
    "active_vms": 40, "total_vms": 44, "total_vcpus": 180, "total_ram_gb": 900,
    "used_storage_tb": 22.5, "total_storage_tb": 60.0, "hosts": 4,
    "total_host_ghz": 400.0, "peak_cpu_ghz": 120.0, "total_host_cores": 96,
    "total_host_ram_gb": 1024, "vm_iops": 0, "peak_ram_gb": 700,
    "total_vm_provisioned_memory_gb": 900, "datastore_used_tb": 22.5,
    "nic_speed_mbps": 10000,
}


def _seed_catalog():
    nvme = DriveCatalog(drive_type="NVMe", size_tb=7.68)
    db.session.add(nvme)
    db.session.flush()
    db.session.add(DriveTypeIops(drive_type="NVMe", iops=75000))
    # status must be "Active" — the engine filters on it, and a catalog seeded
    # with anything else yields zero candidates.
    for name, desc, cores, qty in (("HE500", "Xeon 6338", 32, 2),
                                   ("HE151", "Xeon 4310", 12, 1)):
        cpu = CpuCatalog(description=desc, cores=cores, threads=cores * 2, ghz=2.4)
        model = Model(name=name, status="Active", category="compute",
                      form_factor="1U", chassis="single", min_nodes=1, cost_tier=5.0)
        db.session.add_all([cpu, model])
        db.session.flush()
        storage = StorageConfig(model_id=model.id, storage_type="nvme_only",
                                drives_per_node=4)
        db.session.add(storage)
        db.session.flush()
        db.session.add(StorageConfigDrive(storage_config_id=storage.id, drive_id=nvme.id))
        db.session.add(ModelCpuOption(model_id=model.id, cpu_id=cpu.id, quantity=qty))
        for size in (256, 512, 1024):
            db.session.add(RamOption(model_id=model.id, size_gb=size))
    db.session.commit()


@pytest.fixture()
def app():
    application = appmod.app
    application.config["TESTING"] = True
    application.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    with application.app_context():
        db.drop_all()
        db.create_all()
        _seed_catalog()
    return application


@pytest.fixture()
def engine_output(app):
    with app.app_context():
        result = generate_recommendations(SUMMARY, 4.0)
    assert result["recommendations"], "the seeded catalog produced no candidates"
    return result


def _client(app):
    c = app.test_client()
    c.post("/api/auth/signup",
           json={"email": SCALE, "password": PASSWORD, "accept_privacy": True})
    c.post("/api/auth/login", json={"email": SCALE, "password": PASSWORD})
    return c


def _store(c, project_id, name, rec, projection):
    row = c.post("/api/configs/", json={
        "name": name, "payload": {"mode": "import"}, "project_id": project_id}).get_json()
    resp = c.put(f"/api/sizings/{row['id']}/result", json={
        "clusters": [{"name": "Prod", "summary": SUMMARY, "projection": projection,
                      "recommendation": rec, "source_perf": None,
                      "replicates_to": "", "refs": rec.get("refs") or {"mode": "appliance"}}],
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return row


def test_engine_output_carries_catalog_refs(engine_output):
    """Without refs a stored result can never be re-checked against the catalog,
    so imported sizings would never notice a spec change."""
    rec = engine_output["recommendations"][0]
    assert rec.get("refs"), "recommendations must carry catalog identity"
    assert rec["refs"]["mode"] == "appliance"
    assert rec["refs"]["model"] == rec["model"]
    assert rec["refs"]["cpu_desc"] == rec["cpu"]


@pytest.mark.parametrize("fmt", ["pptx", "docx"])
def test_a_real_bundle_builds_and_downloads(app, engine_output, fmt):
    recs = engine_output["recommendations"]
    projection = engine_output["projection"]
    c = _client(app)
    project = c.post("/api/projects/", json={"name": "Acme HQ"}).get_json()

    first = _store(c, project["id"], "Site A", recs[0], projection)
    second = _store(c, project["id"], "Site B", recs[min(1, len(recs) - 1)], projection)
    for row in (first, second):
        c.post(f"/api/sizings/{row['id']}/role", json={"role": "additive"})

    queued = c.post(f"/api/projects/{project['id']}/export", json={
        "format": fmt, "sizing_ids": [first["id"], second["id"]]})
    assert queued.status_code == 202
    job_id = queued.get_json()["id"]

    with app.app_context():
        export_worker.run_job(export_worker.claim_next_job(), app)
        job = ExportJob.query.get(job_id)
        assert job.status == "done", f"export failed: {job.error}"
        assert job.artifact_path and os.path.exists(job.artifact_path)
        assert os.path.getsize(job.artifact_path) > 20000, "suspiciously small document"

    resp = c.get(f"/api/export-jobs/{job_id}/file")
    assert resp.status_code == 200
    assert len(resp.get_data()) > 20000


def test_comparison_reads_real_engine_totals(app, engine_output):
    """The recommendation calls its cluster figures "totals" while an appliance
    calculation calls them "cluster_total"; reading one shape only would show
    zeros for every imported sizing."""
    recs = engine_output["recommendations"]
    projection = engine_output["projection"]
    c = _client(app)
    project = c.post("/api/projects/", json={"name": "Acme HQ"}).get_json()
    row = _store(c, project["id"], "Site A", recs[0], projection)
    c.post(f"/api/sizings/{row['id']}/role", json={"role": "additive"})

    data = c.post(f"/api/projects/{project['id']}/compare",
                  json={"sizing_ids": [row["id"]]}).get_json()
    totals = data["rows"][0]["totals"]
    assert totals["nodes"] == recs[0]["node_count"]
    assert totals["cores"] == recs[0]["totals"]["cores"] > 0
    assert totals["model"] == recs[0]["model"]
    assert data["rollup"]["nodes"] == totals["nodes"]
