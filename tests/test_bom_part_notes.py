"""Reviewers' part notes: a standing verdict on one specific part.

The case that started it: a Dell BOM with a "Broadcom 5720 Dual Port 1GbE LOM"
failed on "NIC not found in the Hardware Compatibility List". The LOM is not a
problem as such — it must be disabled in the BIOS. A super admin records that
once during review; every later check containing the part gets the note
instead of the generic finding (owner decisions 2026-09-17):

  * the note REPLACES the generic "can't vouch for this part" finding, at the
    severity the admin chose, so a warning no longer fails the BOM;
  * on a part with no such finding (it is on the HCL) the note is ADDED;
  * a note matches exactly (part number, else description) or on
    "contains all these words", optionally limited to one category;
  * existing checks change only when they are re-checked;
  * only super admins manage notes, and every change is audit-logged.

Run: .venv/bin/python -m pytest tests/test_bom_part_notes.py -q
"""
import os
import sys
import types

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.pop("SESSION_COOKIE_SECURE", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402

from bom import part_notes  # noqa: E402
from bom.normalize import BOMComponent, BOMConfig, Finding  # noqa: E402
from bom.rules import determine_verdict  # noqa: E402

LOM = BOMComponent("540-BDKD", "Broadcom 5720 Dual Port 1GbE LOM", 2, "nic")
OCP = BOMComponent("540-BCRX", "Broadcom 57504 Quad Port 10/25GbE, SFP28, OCP 3.0 NIC", 2, "nic")


def note(**kw):
    base = dict(match_mode="exact", part_number=None, description=None, match_text=None,
                category=None, severity="warning", issue="Disable this LOM in the BIOS",
                remediation="The onboard 5720 must be disabled in the BIOS for the server "
                            "to be compliant.")
    base.update(kw)
    return types.SimpleNamespace(**base)


def lom_error():
    return Finding(severity="error", component=LOM.description,
                   issue="NIC not found in the Hardware Compatibility List",
                   remediation="This NIC is not supported.", code="nic_not_in_hcl")


def config(*components):
    return BOMConfig(name="R6615", server_model="PowerEdge R6615", node_count=2,
                     components=list(components))


# ── matching ─────────────────────────────────────────────────────────────────

def test_exact_matches_the_part_number_and_ignores_a_p_suffix():
    assert part_notes.note_matches(note(part_number="540-BDKD"), LOM)
    lom_p = BOMComponent("540-BDKD-P", LOM.description, 2, "nic")
    assert part_notes.note_matches(note(part_number="540-bdkd"), lom_p)
    assert not part_notes.note_matches(note(part_number="540-BDKD"), OCP)


def test_exact_falls_back_to_the_description_for_part_number_less_quotes():
    no_part = BOMComponent(None, "Broadcom  5720 dual port 1GbE LOM", 8, "nic")
    assert part_notes.note_matches(note(description=LOM.description), no_part)


def test_contains_needs_every_word_across_vendors():
    n = note(match_mode="contains", match_text="5720 LOM")
    assert part_notes.note_matches(n, LOM)
    lenovo = BOMComponent(None, "ThinkSystem Broadcom 5720 1GbE RJ45 2-Port LOM", 4, "nic")
    assert part_notes.note_matches(n, lenovo)
    assert not part_notes.note_matches(n, OCP)
    assert not part_notes.note_matches(note(match_mode="contains", match_text="5720 OCP"), LOM)


def test_a_category_limits_the_note():
    n = note(match_mode="contains", match_text="5720", category="controller")
    assert not part_notes.note_matches(n, LOM)


def test_exact_notes_win_over_contains_notes():
    broad = note(match_mode="contains", match_text="5720", issue="broad")
    exact = note(part_number="540-BDKD", issue="exact")
    assert part_notes.note_for(LOM, sorted([broad, exact],
                               key=lambda n: 0 if n.match_mode == "exact" else 1)).issue == "exact"


# ── applying ─────────────────────────────────────────────────────────────────

def test_a_warning_note_replaces_the_hcl_error_and_the_bom_no_longer_fails():
    findings = [lom_error()]
    assert determine_verdict(findings) == "FAIL"
    out = part_notes.apply_notes(config(LOM), findings, [note(part_number="540-BDKD")])
    assert [f.code for f in out] == ["part_note"]
    assert out[0].severity == "warning" and "BIOS" in out[0].issue
    assert out[0].component == LOM.description
    assert determine_verdict(out) == "PASS"
    assert findings == [lom_error()]                  # input untouched


def test_an_error_note_keeps_the_bom_failing_with_the_custom_text():
    out = part_notes.apply_notes(config(LOM), [lom_error()],
                                 [note(part_number="540-BDKD", severity="error",
                                       issue="Not allowed on HyperCore")])
    assert determine_verdict(out) == "FAIL"
    assert out[0].issue == "Not allowed on HyperCore"


def test_a_note_on_a_listed_part_is_added_not_replacing():
    passthrough = Finding(severity="info", component=OCP.description, issue="Set firmware X",
                          remediation="", code="nic_firmware")
    out = part_notes.apply_notes(config(OCP), [passthrough],
                                 [note(part_number="540-BCRX", severity="info",
                                       issue="Use the latest firmware")])
    assert [f.code for f in out] == ["nic_firmware", "part_note"]


def test_a_note_replaces_an_unknown_cpu_so_the_bom_is_no_longer_inconclusive():
    cpu = BOMComponent("338-CGXU", "AMD EPYC 9334 2.70GHz, 32C/64T", 2, "cpu")
    unknown = Finding(severity="warning", component=cpu.description,
                      issue="CPU generation could not be determined", remediation="",
                      code="cpu_unknown")
    assert determine_verdict([unknown]) == "INCONCLUSIVE"
    out = part_notes.apply_notes(config(cpu), [unknown],
                                 [note(part_number="338-CGXU", severity="info",
                                       issue="EPYC Genoa: supported on HCL platforms only")])
    assert determine_verdict(out) == "PASS"


def test_no_notes_leaves_findings_alone():
    findings = [lom_error()]
    assert part_notes.apply_notes(config(LOM), findings, []) is findings


# ── end to end: run_check, the review flags, and the API ─────────────────────

@pytest.fixture()
def app():
    import app as appmod
    from database import db
    from extensions import limiter

    application = appmod.app
    application.config["TESTING"] = True
    application.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    with application.app_context():
        db.drop_all()
        db.create_all()
        yield application
        db.session.remove()


def _run(bom_config):
    from bom.check import run_check
    from bom.normalize import NormalizedBOM
    from bom.rules import HclData
    return run_check(NormalizedBOM(vendor="Dell", configs=[bom_config]),
                     hcl=HclData(), platforms=[])


def test_run_check_applies_a_stored_note_and_it_stops_the_review(app):
    from bom_models import BomPartNote
    from database import db

    cfg = config(LOM, BOMComponent("338-CGXU", "Intel Xeon Gold 6526Y", 2, "cpu"),
                 BOMComponent("345-BJNW", "7.68TB Data Center NVMe", 20, "storage"))
    before = _run(cfg)
    codes = [f["code"] for f in before["technical"]["config_results"][0]["findings"]]
    assert "nic_not_in_hcl" in codes and before["technical"]["verdict"] == "FAIL"

    db.session.add(BomPartNote(part_number="540-BDKD", severity="warning",
                               issue="Disable this LOM in the BIOS"))
    db.session.commit()
    after = _run(cfg)
    findings = after["technical"]["config_results"][0]["findings"]
    assert "nic_not_in_hcl" not in [f["code"] for f in findings]
    assert any(f["code"] == "part_note" and "BIOS" in f["issue"] for f in findings)
    assert "nic_not_in_hcl" not in after["flag_reasons"]


def test_an_inactive_note_is_ignored(app):
    from bom_models import BomPartNote
    from database import db
    db.session.add(BomPartNote(part_number="540-BDKD", severity="warning",
                               issue="Disable this LOM in the BIOS", active=False))
    db.session.commit()
    result = _run(config(LOM))
    codes = [f["code"] for f in result["technical"]["config_results"][0]["findings"]]
    assert "nic_not_in_hcl" in codes and "part_note" not in codes


PASSWORD = "Abcdef1!xy"


def _client(app, email, super_admin=False):
    from auth_models import ROLE_SUPER_ADMIN, User
    from database import db
    c = app.test_client()
    c.post("/api/auth/signup", json={"email": email, "password": PASSWORD, "accept_privacy": True})
    if super_admin:
        u = User.query.filter_by(email=email).first()
        u.role = ROLE_SUPER_ADMIN
        db.session.commit()
    c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    return c


def test_super_admins_manage_notes_and_every_change_is_audited(app):
    from auth_models import AdminAuditLog
    c = _client(app, "admin@scalecomputing.com", super_admin=True)
    r = c.post("/admin/api/bom-part-notes", json={
        "match_mode": "exact", "part_number": "540-BDKD", "category": "nic",
        "severity": "warning", "issue": "Disable this LOM in the BIOS",
        "remediation": "The onboard 5720 must be disabled in the BIOS."})
    assert r.status_code == 201, r.get_data(as_text=True)
    note_id = r.get_json()["id"]
    assert [n["id"] for n in c.get("/admin/api/bom-part-notes").get_json()] == [note_id]

    r = c.put(f"/admin/api/bom-part-notes/{note_id}", json={
        "match_mode": "contains", "match_text": "5720 LOM", "severity": "info",
        "issue": "Disable in BIOS", "active": False})
    body = r.get_json()
    assert r.status_code == 200
    assert (body["match_mode"], body["part_number"], body["active"]) == ("contains", None, False)

    assert c.delete(f"/admin/api/bom-part-notes/{note_id}").status_code == 200
    assert c.get("/admin/api/bom-part-notes").get_json() == []
    actions = [a.action for a in AdminAuditLog.query.all()]
    assert actions.count("bom_part_note") == 3


@pytest.mark.parametrize("payload,error", [
    ({"match_mode": "exact", "issue": "x"}, "part number or a description"),
    ({"match_mode": "contains", "match_text": "57", "issue": "x"}, "at least 3"),
    ({"match_mode": "exact", "part_number": "540-BDKD"}, "title"),
    ({"match_mode": "exact", "part_number": "540-BDKD", "issue": "x", "severity": "fatal"}, "severity"),
])
def test_invalid_notes_are_refused(app, payload, error):
    c = _client(app, "admin@scalecomputing.com", super_admin=True)
    r = c.post("/admin/api/bom-part-notes", json=payload)
    assert r.status_code == 400 and error in r.get_json()["error"]


def test_non_admins_cannot_touch_notes(app):
    c = _client(app, "partner@partnerco.example")
    assert c.get("/admin/api/bom-part-notes").status_code in (401, 403)
    assert c.post("/admin/api/bom-part-notes", json={
        "part_number": "540-BDKD", "issue": "x"}).status_code in (401, 403)
