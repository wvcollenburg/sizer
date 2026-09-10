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


# ── failed outbound email is recorded, not swallowed ─────────────────────────
# Every mail send is best-effort so a broken relay can't take signup down with
# it. That used to mean a failure left no trace anywhere: the account existed,
# the link never arrived, and nothing said why. These pin the trace down.

SMTP_BOOM = "smtp-refused-in-test"


def _configure_smtp(password="s3cret-smtp-pw"):
    """Turn verification on by configuring SMTP, with a password we can then
    assert never reaches the audit log."""
    import auth
    auth.set_setting("smtp_host", "smtp.example.com")
    auth.set_setting("smtp_from", "no-reply@example.com")
    auth.set_setting("smtp_username", "mailer@example.com")
    auth.set_setting("smtp_password", auth._encrypt_secret(password))
    db.session.commit()


def _working_smtp(monkeypatch):
    """A send that succeeds. Needed explicitly: the configured host is not
    resolvable from a test box, so without this the SSRF guard fails the send
    and the 'mail was working' half of these tests is not what it claims."""
    import auth
    monkeypatch.setattr(auth, "_smtp_send", lambda msg: None)


def _break_smtp(monkeypatch):
    """A server-side failure: the relay is unreachable."""
    import auth

    def boom(msg):
        raise OSError(SMTP_BOOM)

    monkeypatch.setattr(auth, "_smtp_send", boom)


def _refuse_recipient(monkeypatch, addr):
    """A recipient-side failure: the relay is healthy and rejects the address,
    which is what a mistyped or non-existent domain actually looks like."""
    import auth
    import smtplib

    def refuse(msg):
        raise smtplib.SMTPRecipientsRefused(
            {addr: (550, b"5.1.2 Host unknown; domain does not exist")})

    monkeypatch.setattr(auth, "_smtp_send", refuse)


def _email_failures():
    from auth_models import AdminAuditLog
    return AdminAuditLog.query.filter_by(action="email_failed").all()


def test_failed_verification_email_is_audited_with_a_full_trace(client, monkeypatch):
    with appmod.app.app_context():
        _configure_smtp()
        _break_smtp(monkeypatch)

        resp = _signup(client, "bob@examplecorp.com")
        assert resp.status_code == 201
        # The user is told, rather than being sent to wait for mail that is
        # never coming.
        assert resp.get_json()["email_sent"] is False

        rows = _email_failures()
        assert len(rows) == 1, "a failed verification email must leave one audit row"
        detail = rows[0].detail
        assert "bob@examplecorp.com" in detail          # who missed out
        assert "smtp.example.com" in detail             # which server
        assert "OSError" in detail and SMTP_BOOM in detail
        assert "Traceback (most recent call last)" in detail   # the full trace
        assert "s3cret-smtp-pw" not in detail           # never the password
        assert rows[0].actor_email == "bob@examplecorp.com"


def test_failed_email_is_recorded_even_when_the_caller_never_commits(client, monkeypatch):
    """resend-verification returns a generic message and commits nothing, so the
    audit row has to persist itself or it is lost with the session."""
    with appmod.app.app_context():
        _configure_smtp()
        _working_smtp(monkeypatch)
        _signup(client, "carol@examplecorp.com")        # sends fine, no row
        assert _email_failures() == []

        _break_smtp(monkeypatch)
        resp = client.post("/api/auth/resend-verification",
                           json={"email": "carol@examplecorp.com"})
        assert resp.status_code == 200                  # still generic to the caller
        rows = _email_failures()
        assert len(rows) == 1
        assert "carol@examplecorp.com" in rows[0].detail
        assert "Traceback (most recent call last)" in rows[0].detail


def test_repeat_signup_reports_the_real_send_failure(client, monkeypatch):
    """Signing up again is what someone does when the first link never arrived.
    That path used to answer 'sent' unconditionally, hiding the very failure the
    user was reacting to."""
    with appmod.app.app_context():
        _configure_smtp()
        _working_smtp(monkeypatch)
        _signup(client, "dave@examplecorp.com")         # first signup, mail OK
        assert _email_failures() == []

        _break_smtp(monkeypatch)
        again = _signup(client, "dave@examplecorp.com")
        assert again.status_code == 201
        assert again.get_json()["email_sent"] is False
        assert len(_email_failures()) == 1


def test_smtp_test_button_records_the_trace_without_echoing_it(client, monkeypatch):
    """The response stays generic — a raw SMTP error can fingerprint a probed
    service — but the super admin gets the trace in the audit log."""
    from auth_models import ROLE_SUPER_ADMIN, User
    with appmod.app.app_context():
        _configure_smtp()
        _working_smtp(monkeypatch)
        _signup(client, "erin@examplecorp.com")
        user = User.query.filter_by(email="erin@examplecorp.com").first()
        user.role = ROLE_SUPER_ADMIN
        user.is_verified = True
        db.session.commit()
        client.post("/api/auth/login", json={"email": "erin@examplecorp.com",
                                             "password": "Abcdef1!xy"})

        _break_smtp(monkeypatch)
        resp = client.post("/api/admin/super/email-settings/test",
                           json={"to": "erin@examplecorp.com"})
        assert resp.status_code == 502
        assert SMTP_BOOM not in resp.get_data(as_text=True)   # not echoed

        rows = _email_failures()
        assert len(rows) == 1
        assert SMTP_BOOM in rows[0].detail
        assert "Traceback (most recent call last)" in rows[0].detail


def test_refused_recipient_is_not_reported_as_a_server_problem(client, monkeypatch):
    """A 550 on the address means the mail server is working and the address is
    wrong. Blaming the SMTP settings sends an admin to inspect a healthy server
    because somebody mistyped a domain."""
    addr = "typo@nonexistent-domain.example"
    with appmod.app.app_context():
        _configure_smtp()
        _refuse_recipient(monkeypatch, addr)

        body = _signup(client, addr).get_json()
        assert body["email_sent"] is False
        assert body["email_error"] == "recipient_refused"

        detail = _email_failures()[0].detail
        # The cause leads, ahead of the SMTP target.
        assert detail.index("Likely cause:") < detail.index("SMTP target:")
        assert "rejected the recipient address" in detail
        assert "the mail server itself is fine" in detail
        assert "Traceback (most recent call last)" in detail


def test_unreachable_server_is_reported_as_a_server_problem(client, monkeypatch):
    with appmod.app.app_context():
        _configure_smtp()
        _break_smtp(monkeypatch)

        body = _signup(client, "frank@examplecorp.com").get_json()
        assert body["email_sent"] is False
        assert body["email_error"] == "server"

        detail = _email_failures()[0].detail
        assert "Could not reach the mail server" in detail


@pytest.mark.parametrize("exc_factory,expected", [
    (lambda: __import__("smtplib").SMTPRecipientsRefused({"a@b.example": (550, b"no")}),
     "recipient_refused"),
    (lambda: __import__("smtplib").SMTPAuthenticationError(535, b"bad creds"), "server"),
    (lambda: __import__("smtplib").SMTPSenderRefused(553, b"bad from", "a@b.example"),
     "server"),
    (lambda: ValueError("SMTP host could not be resolved"), "server"),
    (lambda: OSError("connection refused"), "server"),
    (lambda: __import__("smtplib").SMTPException("something else"), "server"),
    (lambda: RuntimeError("unexpected"), "server"),
])
def test_email_failure_classification(exc_factory, expected):
    """Only a refused recipient is the user's to fix; everything else, including
    anything unforeseen, defaults to the administrator."""
    from auth import _email_failure_cause
    slug, phrase = _email_failure_cause(exc_factory())
    assert slug == expected
    assert phrase and phrase[0].isupper() and phrase.endswith((".", ")"))


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
