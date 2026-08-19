"""Regression tests for the code-review fix pass (branch fix/the-great-bughunt).

Locks in the behavioural changes so they can't silently regress:
  - C1: the manual/validated calculator honours the admin tunables (hybrid flash
        band, per-cluster disk cap) instead of hardcoded constants
  - C2: /api/recommend returns 400 (not 500) on malformed input
  - G1: marketing-email consent is opt-in, recorded, and withdrawable
  - G2: self-service data export and account deletion

Run: .venv/bin/python -m pytest tests/test_review_fixes.py -q
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


@pytest.fixture()
def client():
    app = appmod.app
    app.config["TESTING"] = True
    app.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    with app.app_context():
        db.drop_all()
        db.create_all()
    return app.test_client()


def _signup(client, email="alice@examplecorp.com", **extra):
    body = {"email": email, "password": "Abcdef1!xy", "accept_privacy": True}
    body.update(extra)
    return client.post("/api/auth/signup", json=body)


def _set_setting(key, value):
    from orm_models import SizingSetting
    with appmod.app.app_context():
        row = SizingSetting.query.filter_by(key=key).first()
        if row:
            row.value = value
        else:
            db.session.add(SizingSetting(key=key, value=value))
        db.session.commit()


# ── C1: manual/validated calculator honours the admin tunables ───────────────

def _validated_hybrid_body():
    # 3× HDD 4TB + 1× SSD 6TB = 6/18 = 33.3% flash, HDD:flash = 3:1 (ratio OK).
    return {
        "mode": "validated", "node_count": 3,
        "cores_per_node": 16, "threads_per_node": 32, "ghz": 2.5, "ram_gb": 128,
        "disks": [
            {"type": "HDD", "size_tb": 4}, {"type": "HDD", "size_tb": 4},
            {"type": "HDD", "size_tb": 4}, {"type": "SSD", "size_tb": 6},
        ],
    }


def test_validated_flash_band_follows_tunable(client):
    _signup(client)
    body = _validated_hybrid_body()

    # Default band is 7-25%; 33.3% is rejected.
    err = (client.post("/api/calculate", json=body).get_json() or {}).get("error", "")
    assert "Hybrid fast tier" in err

    # Widen the ceiling to 40% via the admin tunable — now 33.3% is accepted.
    _set_setting("hybrid_flash_max_pct", 40)
    err2 = (client.post("/api/calculate", json=body).get_json() or {}).get("error", "")
    assert "Hybrid fast tier" not in err2


def test_validated_disk_cap_follows_tunable(client):
    _signup(client)
    body = _validated_hybrid_body()  # 4 disks × 3 nodes = 12 per cluster

    # Under the default cap of 100, 12 disks is fine (no disk-limit error).
    err = (client.post("/api/calculate", json=body).get_json() or {}).get("error", "")
    assert "disk limit" not in err.lower()

    # Drop the cap below the config; the calculator must now reject it.
    _set_setting("max_cluster_disks", 10)
    err2 = (client.post("/api/calculate", json=body).get_json() or {}).get("error", "")
    assert "disk limit" in err2.lower()


# ── C2: /api/recommend rejects malformed input with 400, not 500 ─────────────

def test_recommend_rejects_non_numeric_summary(client):
    _signup(client)
    assert client.post("/api/recommend",
                       json={"summary": {"total_vcpus": "abc"}}).status_code == 400


def test_recommend_rejects_non_dict_summary(client):
    _signup(client)
    assert client.post("/api/recommend",
                       json={"summary": "not-a-dict"}).status_code == 400


# ── G1: marketing-email consent (opt-in, recorded, withdrawable) ─────────────

def test_marketing_consent_defaults_off(client):
    _signup(client)
    me = client.get("/api/auth/me").get_json()["user"]
    assert me["marketing_opted_in"] is False
    assert me["marketing_consent_at"] is None


def test_marketing_consent_opt_in_and_out(client):
    r = _signup(client, email="m@examplecorp.com", marketing_consent=True)
    assert r.status_code == 201
    me = client.get("/api/auth/me").get_json()["user"]
    assert me["marketing_opted_in"] is True
    assert me["marketing_consent_at"] is not None

    client.put("/api/auth/me", json={"marketing_consent": False})
    me2 = client.get("/api/auth/me").get_json()["user"]
    assert me2["marketing_opted_in"] is False


# ── G2: self-service data export + account deletion ──────────────────────────

def test_export_my_data(client):
    _signup(client)
    r = client.get("/api/auth/me/export")
    assert r.status_code == 200
    assert "attachment" in (r.headers.get("Content-Disposition") or "")
    data = r.get_json()
    assert data["account"]["email"] == "alice@examplecorp.com"
    for key in ("projects", "sizings", "account_activity", "exported_at"):
        assert key in data


def test_self_delete_disables_and_signs_out(client):
    _signup(client, email="leaver@examplecorp.com")
    assert client.delete("/api/auth/me").status_code == 200
    # Session cleared → subsequent authed calls are anonymous.
    assert client.get("/api/configs/").status_code == 401
    # Account is soft-disabled (admin-recoverable until the retention purge).
    with appmod.app.app_context():
        from auth_models import User
        u = User.query.filter_by(email="leaver@examplecorp.com").first()
        assert u.is_disabled is True
        assert u.disabled_by_user_id == u.id


# ── DR target: workload-less sizing from inbound replication reserve ──────────

def _make_source_with_demand(client, project_id, name="Site A", cluster="Prod",
                             vcpus=200, ram_gb=1024, storage_tb=40):
    src = client.post("/api/configs/", json={
        "name": name, "payload": {"mode": "import"}, "project_id": project_id
    }).get_json()
    client.put(f"/api/sizings/{src['id']}/result", json={
        "clusters": [{
            "name": cluster,
            "summary": {"total_vcpus": vcpus,
                        "total_vm_provisioned_memory_gb": ram_gb,
                        "datastore_used_tb": storage_tb},
            "recommendation": {"refs": {"mode": "import"}},
            "projection": {}, "refs": {"mode": "validated"},
        }],
        "totals": None,
    })
    return src


def test_dr_target_opens_without_error(client):
    _signup(client)
    pid = client.post("/api/projects/", json={"name": "P"}).get_json()["id"]
    dr = client.post(f"/api/projects/{pid}/dr-target", json={"name": "DR"}).get_json()
    # The row is flagged so the client routes it to the DR view (not the sizer).
    row = client.get(f"/api/configs/{dr['id']}").get_json()
    assert row["is_dr_target"] is True
    assert row["payload"] == {"mode": "dr_target"}


def test_dr_recommend_computes_inbound_reserve(client):
    _signup(client)
    pid = client.post("/api/projects/", json={"name": "P"}).get_json()["id"]
    src = _make_source_with_demand(client, pid)
    dr = client.post(f"/api/projects/{pid}/dr-target", json={"name": "DR"}).get_json()
    client.post(f"/api/sizings/{src['id']}/replication", json={
        "target_configuration_id": dr["id"], "source_cluster": "Prod",
        "target_cluster": "", "compute_pct": 50, "storage_pct": 100,
        "mode": "reserved"})

    out = client.post(f"/api/sizings/{dr['id']}/dr-recommend",
                      json={"growth_pct": 0, "years": 1}).get_json()
    # 50% of the source's 200 vCPU / 1024 GB, 100% of 40 TB.
    assert out["reserve"]["vcpus"] == 100
    assert out["reserve"]["ram_gb"] == 512
    assert out["reserve"]["storage_tb"] == 40
    assert out["size_full_cluster"] is False       # reserved link -> N-1 basis
    assert len(out["sources"]) == 1


def test_dr_recommend_empty_when_no_inbound(client):
    _signup(client)
    pid = client.post("/api/projects/", json={"name": "P"}).get_json()["id"]
    dr = client.post(f"/api/projects/{pid}/dr-target", json={"name": "DR"}).get_json()
    out = client.post(f"/api/sizings/{dr['id']}/dr-recommend", json={}).get_json()
    assert out["reserve"] == {"vcpus": 0, "ram_gb": 0, "storage_tb": 0}
    assert out["recommendations"] == []
    assert any(w.get("code") == "dr_no_inbound" for w in out["warnings"])


def test_dr_recommend_rejects_non_dr_sizing(client):
    _signup(client)
    pid = client.post("/api/projects/", json={"name": "P"}).get_json()["id"]
    src = _make_source_with_demand(client, pid)
    r = client.post(f"/api/sizings/{src['id']}/dr-recommend", json={})
    assert r.status_code == 400


def test_dr_recommend_failover_uses_full_cluster(client):
    _signup(client)
    pid = client.post("/api/projects/", json={"name": "P"}).get_json()["id"]
    src = _make_source_with_demand(client, pid)
    dr = client.post(f"/api/projects/{pid}/dr-target", json={"name": "DR"}).get_json()
    client.post(f"/api/sizings/{src['id']}/replication", json={
        "target_configuration_id": dr["id"], "source_cluster": "Prod",
        "target_cluster": "", "compute_pct": 100, "storage_pct": 100,
        "mode": "failover"})
    out = client.post(f"/api/sizings/{dr['id']}/dr-recommend", json={}).get_json()
    assert out["size_full_cluster"] is True        # all-failover -> full-cluster basis


def test_dr_reserve_from_appliance_source(client):
    """An appliance source has no workload summary — the reserve comes from its
    config's usable capacity (cluster_total), not zero."""
    _signup(client)
    pid = client.post("/api/projects/", json={"name": "P"}).get_json()["id"]
    src = client.post("/api/configs/", json={
        "name": "option 1 site 1", "payload": {"mode": "appliance"},
        "project_id": pid}).get_json()
    client.put(f"/api/sizings/{src['id']}/result", json={"clusters": [{
        "name": "HW", "summary": None, "recommendation": None, "projection": None,
        "config": {"cluster_total": {"cores": 252, "ram_gb": 2008,
                                     "usable_storage_tb": 92}},
        "refs": {"mode": "appliance"}}], "totals": None})
    dr = client.post(f"/api/projects/{pid}/dr-target", json={"name": "DR"}).get_json()
    client.post(f"/api/sizings/{src['id']}/replication", json={
        "target_configuration_id": dr["id"], "source_cluster": "",
        "target_cluster": "", "compute_pct": 100, "storage_pct": 100,
        "mode": "failover"})
    out = client.post(f"/api/sizings/{dr['id']}/dr-recommend", json={}).get_json()
    assert out["reserve"] == {"vcpus": 252, "ram_gb": 2008, "storage_tb": 92}


def test_dr_reserve_from_unsized_import_payload(client):
    """A source linked before it was sized still contributes — the demand is
    read from its saved import payload as a fallback."""
    _signup(client)
    pid = client.post("/api/projects/", json={"name": "P"}).get_json()["id"]
    src = client.post("/api/configs/", json={
        "name": "Site A", "project_id": pid,
        "payload": {"mode": "import", "import": {"importSummary": {
            "total_vcpus": 100, "total_vm_provisioned_memory_gb": 256,
            "datastore_used_tb": 8}}}}).get_json()
    # deliberately NO /result stored (unsized)
    dr = client.post(f"/api/projects/{pid}/dr-target", json={"name": "DR"}).get_json()
    client.post(f"/api/sizings/{src['id']}/replication", json={
        "target_configuration_id": dr["id"], "source_cluster": "",
        "target_cluster": "", "compute_pct": 100, "storage_pct": 100,
        "mode": "reserved"})
    out = client.post(f"/api/sizings/{dr['id']}/dr-recommend", json={}).get_json()
    assert out["reserve"] == {"vcpus": 100, "ram_gb": 256, "storage_tb": 8}


def test_dr_message_distinguishes_no_demand_from_no_links(client):
    """Links present but zero demand reports dr_sources_no_demand, not the
    misleading 'no sizings replicate' message."""
    _signup(client)
    pid = client.post("/api/projects/", json={"name": "P"}).get_json()["id"]
    # A source with a payload that carries no workload at all.
    src = client.post("/api/configs/", json={
        "name": "Empty", "payload": {"mode": "appliance"},
        "project_id": pid}).get_json()
    dr = client.post(f"/api/projects/{pid}/dr-target", json={"name": "DR"}).get_json()
    client.post(f"/api/sizings/{src['id']}/replication", json={
        "target_configuration_id": dr["id"], "source_cluster": "",
        "target_cluster": "", "compute_pct": 100, "storage_pct": 100,
        "mode": "reserved"})
    out = client.post(f"/api/sizings/{dr['id']}/dr-recommend", json={}).get_json()
    assert any(w.get("code") == "dr_sources_no_demand" for w in out["warnings"])
    # And with no links at all it's the other code.
    dr2 = client.post(f"/api/projects/{pid}/dr-target", json={"name": "DR2"}).get_json()
    out2 = client.post(f"/api/sizings/{dr2['id']}/dr-recommend", json={}).get_json()
    assert any(w.get("code") == "dr_no_inbound" for w in out2["warnings"])


# ── Project export: proposal + config sizings both become bundle sections ─────

def test_bundle_sections_include_config_sizings(client):
    """A config-only (appliance/validated) sizing is now a renderable bundle
    section, not silently skipped — so it can be exported at the project level."""
    from export_worker import sections_for

    _signup(client)
    pid = client.post("/api/projects/", json={"name": "P"}).get_json()["id"]

    prop = client.post("/api/configs/", json={
        "name": "Import site", "payload": {"mode": "import"}, "project_id": pid}).get_json()
    client.put(f"/api/sizings/{prop['id']}/result", json={"clusters": [{
        "name": "Prod",
        "summary": {"total_vcpus": 100, "total_vm_provisioned_memory_gb": 256,
                    "datastore_used_tb": 8},
        "recommendation": {"model": "X", "node_count": 3, "totals": {}},
        "projection": {"years": 5}, "refs": {"mode": "import"}}], "totals": None})

    appl = client.post("/api/configs/", json={
        "name": "Appliance opt", "payload": {"mode": "appliance"}, "project_id": pid}).get_json()
    client.put(f"/api/sizings/{appl['id']}/result", json={"clusters": [{
        "name": "HW", "summary": None, "recommendation": None, "projection": None,
        "config": {"cluster_total": {"cores": 100, "ram_gb": 512, "usable_storage_tb": 40}},
        "refs": {"mode": "appliance"}}], "totals": None})

    class _Job:
        sizing_ids = [prop["id"], appl["id"]]

    with appmod.app.app_context():
        sections, skipped = sections_for(_Job())

    assert skipped == []
    assert len(sections) == 2
    # One proposal section (has recommendation) and one config section (has config).
    assert any(s.get("recommendation") for s in sections)
    assert any(s.get("config") and not s.get("recommendation") for s in sections)
