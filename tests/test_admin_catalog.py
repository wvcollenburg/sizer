"""Admin catalog API (admin_routes.py): CPU/NIC/drive CRUD and model storage
refuse bad input with a 400, never a 500, and uploads are screened the same
way on every importer.

Run: .venv/bin/python -m pytest tests/test_admin_catalog.py -q
"""
import io
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
from auth_models import ROLE_SUPER_ADMIN, User  # noqa: E402

PASSWORD = "Abcdef1!xy"
ADMIN = "admin@scalecomputing.com"


@pytest.fixture()
def admin():
    application = appmod.app
    application.config["TESTING"] = True
    application.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    with application.app_context():
        db.drop_all()
        db.create_all()
    c = application.test_client()
    c.post("/api/auth/signup", json={"email": ADMIN, "password": PASSWORD, "accept_privacy": True})
    c.post("/api/auth/login", json={"email": ADMIN, "password": PASSWORD})
    with application.app_context():
        u = User.query.filter_by(email=ADMIN).first()
        u.role = ROLE_SUPER_ADMIN
        db.session.commit()
    return c


CPU = {"desc": "Xeon Gold 6526Y", "cores": 16, "threads": 32, "ghz": 3.5}


# ── CPUs ─────────────────────────────────────────────────────────────────────

def test_cpu_crud_happy_path_keeps_its_shapes(admin):
    r = admin.post("/admin/api/cpus", json=CPU)
    assert r.status_code == 201, r.get_data(as_text=True)
    cpu = r.get_json()
    assert cpu["cores"] == 16 and cpu["ghz"] == 3.5 and "id" in cpu

    assert admin.post("/admin/api/cpus", json=CPU).status_code == 409

    r = admin.put(f"/admin/api/cpus/{cpu['id']}", json={"cores": "24", "ghz": "2.1"})
    assert r.status_code == 200
    assert r.get_json()["cores"] == 24 and r.get_json()["ghz"] == 2.1

    r = admin.delete(f"/admin/api/cpus/{cpu['id']}")
    assert r.status_code == 200 and r.get_json() == {"message": "CPU deleted"}


@pytest.mark.parametrize("field, bad", [("cores", "sixteen"), ("threads", "x"), ("ghz", "fast")])
def test_cpu_rejects_a_non_number_with_400(admin, field, bad):
    r = admin.post("/admin/api/cpus", json=dict(CPU, **{field: bad}))
    assert r.status_code == 400
    assert field in r.get_json()["error"]

    cpu = admin.post("/admin/api/cpus", json=CPU).get_json()
    r = admin.put(f"/admin/api/cpus/{cpu['id']}", json={field: bad})
    assert r.status_code == 400
    # the row is untouched
    assert admin.get("/admin/api/cpus").get_json()[0][field] == CPU[field]


def test_cpu_in_use_cannot_be_deleted(admin):
    cpu = admin.post("/admin/api/cpus", json=CPU).get_json()
    r = admin.post("/admin/api/models", json={
        "name": "HC1250", "status": "Active", "category": "1XXX",
        "cpu_options": [dict(CPU, qty=1)], "ram_options_gb": [128],
        "storage": {"type": "nvme_only", "drives_per_node": 4, "nvme_options_tb": [1.92]},
    })
    assert r.status_code == 201, r.get_data(as_text=True)
    r = admin.delete(f"/admin/api/cpus/{cpu['id']}")
    assert r.status_code == 409 and "used by 1 model(s)" in r.get_json()["error"]


def test_a_missing_or_non_json_body_is_a_400_not_a_415(admin):
    r = admin.post("/admin/api/cpus", data="not json", content_type="text/plain")
    assert r.status_code == 400 and r.get_json()["error"] == "Description is required"
    r = admin.post("/admin/api/cpus", json=["a", "list"])
    assert r.status_code == 400


# ── NICs and drives ──────────────────────────────────────────────────────────

def test_nic_ports_must_be_a_number(admin):
    r = admin.post("/admin/api/nics", json={"desc": "25GbE 2-port", "ports": "two", "speed": "25GbE"})
    assert r.status_code == 400 and "ports" in r.get_json()["error"]
    r = admin.post("/admin/api/nics", json={"desc": "25GbE 2-port", "ports": 2, "speed": "25GbE"})
    assert r.status_code == 201 and r.get_json()["ports"] == 2
    nic = r.get_json()
    assert admin.put(f"/admin/api/nics/{nic['id']}", json={"ports": None}).status_code == 400
    assert admin.delete(f"/admin/api/nics/{nic['id']}").get_json() == {"message": "NIC deleted"}


def test_drive_size_must_be_a_positive_number(admin):
    r = admin.post("/admin/api/drives", json={"drive_type": "NVMe", "size_tb": "big"})
    assert r.status_code == 400
    r = admin.post("/admin/api/drives", json={"drive_type": "NVMe", "size_tb": 3.84})
    assert r.status_code == 201
    drive = r.get_json()
    assert drive == {"id": drive["id"], "drive_type": "NVMe", "size_tb": 3.84}
    assert admin.post("/admin/api/drives", json={"drive_type": "NVMe", "size_tb": "3.84"}).status_code == 409
    r = admin.put(f"/admin/api/drives/{drive['id']}", json={"size_tb": "huge"})
    assert r.status_code == 400 and "size_tb" in r.get_json()["error"]
    r = admin.put(f"/admin/api/drives/{drive['id']}", json={"size_tb": "7.68"})
    assert r.status_code == 200 and r.get_json()["size_tb"] == 7.68
    assert admin.delete(f"/admin/api/drives/{drive['id']}").get_json() == {"message": "Drive deleted"}


# ── model storage needs its drive count ──────────────────────────────────────

def _model(storage):
    return {"name": "HC1250", "status": "Active", "category": "1XXX",
            "cpu_options": [dict(CPU, qty=1)], "ram_options_gb": [128], "storage": storage}


@pytest.mark.parametrize("storage", [
    {"type": "nvme_only", "nvme_options_tb": [1.92]},
    {"type": "hybrid", "hdd_count": 3, "hdd_options_tb": [8], "ssd_options_tb": [0.96]},
    "nonsense",
])
def test_model_without_its_drive_count_is_refused(admin, storage):
    r = admin.post("/admin/api/models", json=_model(storage))
    assert r.status_code == 400, r.get_data(as_text=True)
    assert "storage" in r.get_json()["error"].lower()
    assert admin.get("/admin/api/models").get_json() == []


def test_model_update_cannot_drop_the_drive_count(admin):
    r = admin.post("/admin/api/models", json=_model(
        {"type": "nvme_only", "drives_per_node": 4, "nvme_options_tb": [1.92]}))
    assert r.status_code == 201
    mid = r.get_json()["id"]
    r = admin.put(f"/admin/api/models/{mid}", json={"storage": {"type": "ssd_only", "ssd_options_tb": [1.92]}})
    assert r.status_code == 400 and "drives_per_node" in r.get_json()["error"]
    m = admin.get(f"/admin/api/models/{mid}").get_json()
    assert m["storage"]["type"] == "nvme_only" and m["storage"]["drives_per_node"] == 4


# ── uploads are screened the same way everywhere ─────────────────────────────

@pytest.mark.parametrize("url", ["/admin/api/import-catalog", "/admin/api/import-pricebook"])
def test_importers_refuse_a_missing_or_non_xlsx_upload(admin, url):
    r = admin.post(url, data={}, content_type="multipart/form-data")
    assert r.status_code == 400 and r.get_json()["error"] == "No file uploaded"
    r = admin.post(url, data={"file": (io.BytesIO(b"PK\x03\x04..."), "catalog.csv")},
                   content_type="multipart/form-data")
    assert r.status_code == 400 and r.get_json()["error"] == "File must be .xlsx"
    # the right name but not a ZIP: refused before openpyxl ever sees it
    r = admin.post(url, data={"file": (io.BytesIO(b"<html>oops</html>"), "catalog.xlsx")},
                   content_type="multipart/form-data")
    assert r.status_code == 400 and r.get_json()["error"] == "File must be .xlsx"
