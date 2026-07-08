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
    for _ in range(6):  # exceed LOCKOUT_THRESHOLD with wrong password
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
