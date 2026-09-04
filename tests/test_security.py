"""Regression tests for the security-hardening pass (branch fix/security-updates).

Covers the fixes so they can't silently regress:
  - xlsx parsing row/column caps (decompression-bomb guard)
  - SMTP SSRF target validation
  - CSRF same-origin guard
  - /api/calculate node_count clamp
  - login: no weaponizable per-account lockout; logout clears session
  - upload content-type (magic-byte) rejection

Run: .venv/bin/python -m pytest tests/test_security.py -q
"""
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.pop("SESSION_COOKIE_SECURE", None)  # not "prod" for these tests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import io  # noqa: E402
import pytest  # noqa: E402
import app as appmod  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402


@pytest.fixture()
def client():
    app = appmod.app
    app.config["TESTING"] = True
    app.config["RATELIMIT_ENABLED"] = False  # exercise the guards, not the limiter
    limiter.enabled = False
    with app.app_context():
        db.drop_all()
        db.create_all()
    return app.test_client()


def _signup(client, email="alice@examplecorp.com"):
    return client.post("/api/auth/signup", json={
        "email": email, "password": "Abcdef1!xy", "accept_privacy": True})


# ── xlsx caps ────────────────────────────────────────────────────────────────

def test_sheet_rows_normal_parse():
    from openpyxl import Workbook
    from xlsx_utils import sheet_rows
    wb = Workbook(); ws = wb.active; ws.title = "S"
    ws.append(["a", "b"]); ws.append([1, 2]); ws.append([3, 4])
    assert sheet_rows(wb, "S") == [{"a": 1, "b": 2}, {"a": 3, "b": 4}]


def test_sheet_rows_rejects_oversized(monkeypatch):
    import xlsx_utils
    from openpyxl import Workbook
    # Shrink the cap so the test doesn't have to build 100k rows.
    monkeypatch.setattr(xlsx_utils, "MAX_SHEET_ROWS", 10)
    wb = Workbook(); ws = wb.active; ws.title = "S"; ws.append(["a"])
    for i in range(15):  # header + 15 data rows > cap of 10
        ws.append([i])
    with pytest.raises(xlsx_utils.SheetTooLargeError):
        xlsx_utils.sheet_rows(wb, "S")


# ── SMTP SSRF guard ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("host,port,allowed", [
    ("8.8.8.8", 587, True),             # public IP literal (no DNS needed)
    ("127.0.0.1", 587, False),
    ("localhost", 587, False),
    ("169.254.169.254", 587, False),   # cloud metadata
    ("10.0.0.5", 22, False),            # non-mail port (rejected before any resolve)
])
def test_validate_smtp_target(host, port, allowed):
    from auth import _validate_smtp_target
    if allowed:
        _validate_smtp_target(host, port)  # must not raise
    else:
        with pytest.raises(ValueError):
            _validate_smtp_target(host, port)


# ── CSRF same-origin guard ───────────────────────────────────────────────────

def test_csrf_blocks_cross_origin(client):
    r = client.post("/api/auth/login",
                    json={"email": "x@y.com", "password": "z"},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_csrf_allows_no_origin(client):
    r = client.post("/api/auth/login", json={"email": "x@y.com", "password": "z"})
    assert r.status_code == 401  # reached auth, wrong creds — not CSRF-blocked


# ── node_count clamp ─────────────────────────────────────────────────────────

def test_calculate_clamps_node_count(client):
    _signup(client)
    assert client.post("/api/calculate",
                       json={"mode": "appliance", "node_count": 100_000_000}).status_code == 400
    assert client.post("/api/calculate",
                       json={"mode": "appliance", "node_count": "abc"}).status_code == 400


# ── login / logout ───────────────────────────────────────────────────────────

def test_login_no_weaponizable_lockout_and_logout_clears(client):
    _signup(client)
    client.post("/api/auth/logout")
    for _ in range(6):  # repeated wrong passwords must never lock the account
        r = client.post("/api/auth/login",
                        json={"email": "alice@examplecorp.com", "password": "WRONGpw1!"})
        assert r.status_code == 401  # never a 429 account-lock (only per-IP limiter would)
    # Correct password still works despite prior failures (victim not DoS'd).
    assert client.post("/api/auth/login",
                       json={"email": "alice@examplecorp.com", "password": "Abcdef1!xy"}).status_code == 200
    client.post("/api/auth/logout")
    assert client.get("/api/configs/").status_code == 401  # session cleared


# ── upload content sniff ─────────────────────────────────────────────────────

def test_verification_temp_off_window(client):
    """Once SMTP is configured verification is mandatory, suspendable only for a
    bounded window that auto-resumes."""
    from datetime import timedelta
    import auth
    from database import db
    with appmod.app.app_context():
        assert auth.verification_active() is False          # no SMTP -> off
        auth.set_setting("smtp_host", "smtp.example.com")
        auth.set_setting("smtp_from", "no-reply@example.com")
        db.session.commit()
        assert auth.verification_active() is True           # SMTP configured -> on

        # Suspend for the max window -> temporarily off, minutes reported.
        auth.set_setting(auth.VERIFY_OFF_UNTIL_KEY,
                         (auth._utcnow() + timedelta(minutes=auth.VERIFY_TEMP_OFF_MINUTES)).isoformat())
        db.session.commit()
        assert auth.verification_active() is False
        assert 1 <= auth.verify_off_minutes_remaining() <= auth.VERIFY_TEMP_OFF_MINUTES

        # Window elapsed -> auto-resumes.
        auth.set_setting(auth.VERIFY_OFF_UNTIL_KEY,
                         (auth._utcnow() - timedelta(minutes=1)).isoformat())
        db.session.commit()
        assert auth.verification_active() is True
        assert auth.verify_off_minutes_remaining() == 0


def test_import_rejects_non_zip(client):
    _signup(client)
    r = client.post("/api/import-liveoptics",
                    data={"file": (io.BytesIO(b"totally not a zip"), "evil.xlsx")},
                    content_type="multipart/form-data")
    assert r.status_code == 400


# ── No price-shaped data on the wire (docs/pricebook-plan.md §3) ─────────────
#
# cost_tier is a ranking weight, and the licensing work is about to put real
# euro next to it in the score. Nothing price-shaped may reach a browser: /api/
# is gated by login, but registration is a blocklist (any unblocked domain can
# self-register), so "logged in" is a weak boundary for commercial data.
#
# These assert on the SERIALIZED RESPONSE, not on the model definition — a field
# can reappear through a nested dict, a **spread, or a new serializer without the
# definition changing.

PRICE_SHAPED = ("cost", "price", "tier", "eur", "usd", "msrp", "discount", "margin")


def _offending_keys(node, path="$"):
    """Every key anywhere in a JSON structure whose name looks commercial."""
    found = []
    if isinstance(node, dict):
        for k, v in node.items():
            if any(word in str(k).lower() for word in PRICE_SHAPED):
                found.append(f"{path}.{k}")
            found += _offending_keys(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found += _offending_keys(v, f"{path}[{i}]")
    return found


def _seed_one_model():
    """Minimal Active model so /api/models returns a populated payload."""
    from orm_models import (Model, CpuCatalog, ModelCpuOption, RamOption,
                            DriveCatalog, StorageConfig, StorageConfigDrive)
    nvme = DriveCatalog(drive_type="NVMe", size_tb=7.68)
    cpu = CpuCatalog(description="Xeon 6338", cores=32, threads=64, ghz=2.4)
    model = Model(name="HE500", status="Active", category="compute",
                  form_factor="1U", chassis="single", min_nodes=1, cost_tier=17.5)
    db.session.add_all([nvme, cpu, model])
    db.session.flush()
    storage = StorageConfig(model_id=model.id, storage_type="nvme_only",
                            drives_per_node=4)
    db.session.add(storage)
    db.session.flush()
    db.session.add_all([
        StorageConfigDrive(storage_config_id=storage.id, drive_id=nvme.id),
        ModelCpuOption(model_id=model.id, cpu_id=cpu.id, quantity=2),
        RamOption(model_id=model.id, size_gb=512),
    ])
    db.session.commit()
    return model


def test_to_dict_hides_cost_tier_by_default():
    """Default-deny: a new caller of to_dict() gets the safe shape."""
    with appmod.app.app_context():
        db.drop_all(); db.create_all()
        model = _seed_one_model()
        assert "cost_tier" not in model.to_dict()
        # The engine and the admin UI opt in explicitly and still get it.
        assert model.to_dict(include_internal=True)["cost_tier"] == 17.5


def test_api_models_carries_no_price_shaped_key(client):
    _signup(client)
    with appmod.app.app_context():
        _seed_one_model()
    r = client.get("/api/models")
    assert r.status_code == 200
    body = r.get_json()
    assert body, "expected a populated catalog, or this test proves nothing"
    assert _offending_keys(body) == []


def test_api_recommend_carries_no_price_shaped_key(client):
    _signup(client)
    with appmod.app.app_context():
        _seed_one_model()
    r = client.post("/api/recommend", json={"summary": {
        "active_vms": 40, "total_vms": 44, "total_vcpus": 180, "total_ram_gb": 900,
        "used_storage_tb": 22.5, "total_storage_tb": 60.0, "hosts": 4,
        "total_host_ghz": 400.0, "peak_cpu_ghz": 120.0, "total_host_cores": 96,
        "total_host_ram_gb": 1024, "vm_iops": 0, "peak_ram_gb": 700,
        "total_vm_provisioned_memory_gb": 900, "datastore_used_tb": 22.5,
        "nic_speed_mbps": 10000,
    }})
    assert r.status_code == 200
    body = r.get_json()
    assert body.get("recommendations"), "no candidates — the scan would pass vacuously"
    assert _offending_keys(body) == []
