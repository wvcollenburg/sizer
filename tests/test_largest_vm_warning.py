"""A missing largest VM is never silent (sizing_service.input_quality_warnings).

The engine reads a missing or zero largest VM as "no constraint" and skips the
check that the biggest VM fits on one node. Manual sizings had no way to enter
it at all, so the check was silently off for every typed-in sizing. Locks in:
  - the warning names what is missing (vCPUs, RAM or both) and is absent when
    both are known
  - /api/recommend appends it to the engine's warnings, non-fatally
  - sizing_service stays out of the engine fingerprint, so changing what is
    said about inputs never marks stored sizings stale

Run: .venv/bin/python -m pytest tests/test_largest_vm_warning.py -q
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
from fingerprint import ENGINE_MODULES  # noqa: E402
from sizing_service import input_quality_warnings  # noqa: E402

SUMMARY = {"total_vcpus": 120, "total_vm_provisioned_memory_gb": 480,
           "datastore_used_tb": 10, "nic_speed_mbps": 10000, "active_vms": 30}


@pytest.fixture()
def client():
    app = appmod.app
    app.config["TESTING"] = True
    app.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    with app.app_context():
        db.drop_all()
        db.create_all()
    c = app.test_client()
    c.post("/api/auth/signup", json={"email": "alice@examplecorp.com",
                                     "password": "Abcdef1!xy",
                                     "accept_privacy": True})
    return c


def _largest_vm_warnings(warnings):
    return [w for w in warnings if w.startswith("Largest VM")]


@pytest.mark.parametrize("extra, named", [
    ({}, "vCPUs and RAM"),
    ({"max_vm_cores": 0, "max_vm_ram_gb": 0}, "vCPUs and RAM"),
    ({"max_vm_cores": 8}, "RAM"),
    ({"max_vm_ram_gb": 64}, "vCPUs"),
    ({"max_vm_cores": "", "max_vm_ram_gb": None}, "vCPUs and RAM"),
])
def test_warns_naming_what_is_missing(extra, named):
    warnings = input_quality_warnings({**SUMMARY, **extra})
    assert len(warnings) == 1
    assert warnings[0].startswith(f"Largest VM {named} unknown")


def test_silent_when_largest_vm_known():
    assert input_quality_warnings(
        {**SUMMARY, "max_vm_cores": 8, "max_vm_ram_gb": 64}) == []


def test_recommend_route_appends_warning(client):
    res = client.post("/api/recommend", json={"summary": SUMMARY})
    assert res.status_code == 200
    assert len(_largest_vm_warnings(res.get_json()["warnings"])) == 1


def test_recommend_route_silent_when_known(client):
    summary = {**SUMMARY, "max_vm_cores": 8, "max_vm_ram_gb": 64}
    res = client.post("/api/recommend", json={"summary": summary})
    assert res.status_code == 200
    assert _largest_vm_warnings(res.get_json()["warnings"]) == []


def test_service_is_not_fingerprinted():
    assert "sizing_service.py" not in ENGINE_MODULES
