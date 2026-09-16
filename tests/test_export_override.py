"""Export customization — the chassis (and hardware) the exports name.

A Validated sizing is sized on one vendor chassis, but the partner quoting it
may sell another one: we size a Dell R660, the VAR BOM-checks an R670, and the
proposal has to say R670. Three sources merged PER FIELD:

    manual override  >  linked BOM check  >  the recommendation

These tests pin the owner's decisions of 2026-09-16:
  * only Validated recommendations are ever renamed;
  * a passing BOM check applies by itself, a failing one never does;
  * per-node hardware follows the source, and the cluster totals follow the
    per-node figures — while utilization, licensing and the compute floor keep
    describing the SIZING, which is what they are an answer to;
  * the stored snapshot is never mutated.

Run: .venv/bin/python -m pytest tests/test_export_override.py -q
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
import export_override as eo  # noqa: E402
from auth_models import Configuration, Tenant, User  # noqa: E402
from bom_models import BomCheck  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from project_models import Project  # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────────

def _rec(validated=True):
    """A stored Validated recommendation, trimmed to the fields that matter."""
    return {
        "validated": validated,
        "model": "HC3450DF",
        "vendor": "dell",
        "vendor_chassis": "Dell PowerEdge R660" if validated else None,
        "chassis": "Dell PowerEdge R660",
        "category": "1U",
        "form_factor": "1U",
        "cpu": "2 x Intel Xeon Gold 6526Y",
        "cores_per_node": 32,
        "usable_cores_per_node": 30,
        "threads_per_node": 64,
        "ghz": 2.8,
        "ram_per_node_gb": 512,
        "usable_ram_per_node_gb": 480,
        "node_count": 4,
        "hci_node_count": 4,
        "cluster_layout": [4],
        "storage_config": {"desc": "4 x 3.84TB NVMe"},
        "totals": {"cores": 120, "threads": 256, "total_ghz": 358.4,
                   "ram_gb": 1920.0, "raw_storage_tb": 61.4,
                   "usable_storage_tb": 24.0},
        "n_minus_1": {"cores": 90, "threads": 192, "total_ghz": 268.8,
                      "ram_gb": 1440.0, "usable_storage_tb": 24.0},
        "utilization": {"cpu": {"total": 70, "abs": {"capacity": 120}}},
        "licensing": {"band": "49-64"},
    }


def _snapshot(rec=None):
    # summary/projection must be non-empty: the export worker only renders a
    # cluster that carries all three blocks.
    return {"clusters": [{"name": "Prod",
                          "summary": {"vm_count": 40},
                          "projection": {"years": 3},
                          "recommendation": rec or _rec()}],
            "totals": {}, "tunables": "x"}


def _bom_result(server="PowerEdge R670", brand="dell", nodes=5, cores=48,
                ram=768, threads=96, with_cluster=True):
    """The shape bom/check.py stores: technical config_results + the fit block
    whose `cluster` is bom/fit.py's derived cluster summary."""
    result = {
        "technical": {"verdict": "PASS", "config_results": [{
            "config_name": "R670",
            "verdict": "PASS",
            "platform": {"brand": brand, "server": server, "form_factor": "2U",
                         "sc_models": ["HC3450DF"], "platform_ids": [1]},
            "node_count": nodes,
        }]},
        "fit": {"verdict": "match", "dimensions": [], "notes": []},
    }
    if with_cluster:
        result["fit"]["cluster"] = {
            "node_count": nodes, "hci_node_count": nodes, "so_count": 0,
            "cluster_layout": [nodes], "cpu": "2 x Intel Xeon Gold 6542Y",
            "cores_per_node": cores, "threads_per_node": threads,
            "sockets_per_node": 2,
            "ram_per_node_gb": ram, "usable_ram_per_node_gb": ram - 32,
            "cores_full": (cores - 2) * nodes, "cores_n1": (cores - 2) * (nodes - 1),
            "ram_full": (ram - 32) * nodes, "ram_n1": (ram - 32) * (nodes - 1),
            "ghz_full": 400.0, "ghz_n1": 320.0,
            "raw_storage_tb": 92.2, "usable_storage_tb": 36.0,
            "drive_counts": {"NVMe": 6}, "bays": 10,
            "storage_category": "flash", "nic_gbe": 25, "nic_ports": 2,
        }
    return result


@pytest.fixture()
def app():
    application = appmod.app
    application.config["TESTING"] = True
    application.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    with application.app_context():
        db.drop_all()
        db.create_all()
        yield application
        db.session.remove()


@pytest.fixture()
def sizing(app):
    tenant = Tenant(domain="example.com")
    db.session.add(tenant)
    db.session.flush()
    user = User(email="sa@example.com", password_hash="x", tenant_id=tenant.id,
                is_verified=True)
    db.session.add(user)
    db.session.flush()
    project = Project(name="Acme", owner_id=user.id, tenant_id=tenant.id,
                      code="PRJ1")
    db.session.add(project)
    db.session.flush()
    config = Configuration(code="SZ1", name="Option 1", owner_id=user.id,
                           tenant_id=tenant.id, project_id=project.id,
                           payload={}, result_snapshot=_snapshot())
    db.session.add(config)
    db.session.commit()
    return config


def _add_check(sizing, name="VAR quote", result=None, technical="PASS",
               fit_verdict="match", vendor="dell"):
    check = BomCheck(project_id=sizing.project_id, configuration_id=sizing.id,
                     user_id=sizing.owner_id, tenant_id=sizing.tenant_id,
                     name=name, vendor=vendor,
                     normalized={"vendor": vendor, "configs": []},
                     result=result if result is not None else _bom_result(),
                     technical_verdict=technical, fit_verdict=fit_verdict)
    db.session.add(check)
    db.session.commit()
    return check


# ── the stored setting ───────────────────────────────────────────────────────

def test_clean_manual_keeps_known_fields_and_drops_the_rest():
    cleaned = eo.clean_manual({"chassis": "  Dell PowerEdge R670  ",
                               "cores_per_node": "48", "node_count": 0,
                               "ram_per_node_gb": "", "cpu": "   ",
                               "score": 9999})
    assert cleaned == {"chassis": "Dell PowerEdge R670", "cores_per_node": 48}


def test_default_setting_stores_as_nothing():
    assert eo.is_default(None)
    assert eo.is_default({"bom": "auto", "manual": {}})
    assert not eo.is_default({"bom": "none", "manual": {}})
    assert not eo.is_default({"bom": "auto", "manual": {"chassis": "R670"}})


# ── which BOM check drives it ────────────────────────────────────────────────

def test_a_passing_bom_check_applies_by_itself(sizing):
    _add_check(sizing)
    badge = eo.badge_for(sizing)
    assert badge["chassis"] == "Dell PowerEdge R670"
    assert badge["bom_auto"] is True


def test_a_failing_check_is_never_picked_automatically(sizing):
    _add_check(sizing, name="Bad quote", technical="FAIL")
    assert eo.badge_for(sizing) is None


def test_a_check_under_the_requirement_is_never_picked_automatically(sizing):
    _add_check(sizing, name="Too small", fit_verdict="smaller")
    assert eo.badge_for(sizing) is None


def test_a_failing_check_can_still_be_chosen_by_hand(sizing):
    check = _add_check(sizing, name="Bad quote", technical="FAIL")
    sizing.export_override = {"bom": check.id, "manual": {}}
    badge = eo.badge_for(sizing)
    assert badge["chassis"] == "Dell PowerEdge R670"
    assert badge["bom_auto"] is False


def test_bom_none_keeps_the_recommendation(sizing):
    _add_check(sizing)
    sizing.export_override = {"bom": "none", "manual": {}}
    assert eo.badge_for(sizing) is None


def test_a_check_without_a_usable_cluster_is_not_offered(sizing):
    # Old/degraded results carry a fit block with no cluster at all.
    _add_check(sizing, result=_bom_result(with_cluster=False))
    assert eo.candidate_checks(sizing) == []
    assert eo.badge_for(sizing) is None


def test_a_deleted_chosen_check_falls_back_to_the_suggestion(sizing):
    good = _add_check(sizing, name="Current")
    sizing.export_override = {"bom": good.id + 999, "manual": {}}
    badge = eo.badge_for(sizing)
    assert badge["bom_check_id"] == good.id
    assert badge["bom_auto"] is True


# ── applying it to a recommendation ──────────────────────────────────────────

def test_certified_recommendations_are_never_renamed(sizing):
    _add_check(sizing)
    override = eo.resolve(sizing)
    certified = _rec(validated=False)
    certified.pop("vendor_chassis")
    assert eo.apply_to_rec(certified, override) is certified


def test_bom_supplies_chassis_and_per_node_hardware(sizing):
    _add_check(sizing)
    override = eo.resolve(sizing)
    out = eo.apply_to_rec(_rec(), override)
    assert out["vendor_chassis"] == "Dell PowerEdge R670"
    assert out["chassis"] == "Dell PowerEdge R670"
    assert out["form_factor"] == "2U"
    assert out["cpu"] == "2 x Intel Xeon Gold 6542Y"
    assert out["cores_per_node"] == 48
    assert out["threads_per_node"] == 96
    assert out["ram_per_node_gb"] == 768
    assert out["node_count"] == 5
    assert "6 x NVMe" in out["storage_config"]["desc"]


def test_the_sc_model_stays_as_catalog_identity(sizing):
    _add_check(sizing)
    out = eo.apply_to_rec(_rec(), eo.resolve(sizing))
    # refs/fingerprint/BOM-fit all ride on `model`; only the display name moves.
    assert out["model"] == "HC3450DF"


def test_cluster_totals_follow_the_quoted_hardware(sizing):
    _add_check(sizing)
    out = eo.apply_to_rec(_rec(), eo.resolve(sizing))
    cluster = _bom_result()["fit"]["cluster"]
    assert out["totals"]["cores"] == cluster["cores_full"]
    assert out["totals"]["ram_gb"] == cluster["ram_full"]
    assert out["totals"]["usable_storage_tb"] == cluster["usable_storage_tb"]
    assert out["n_minus_1"]["cores"] == cluster["cores_n1"]


def test_utilization_and_licensing_still_describe_the_sizing(sizing):
    _add_check(sizing)
    out = eo.apply_to_rec(_rec(), eo.resolve(sizing))
    assert out["utilization"] == _rec()["utilization"]
    assert out["licensing"] == _rec()["licensing"]


def test_manual_wins_field_by_field_over_the_bom(sizing):
    _add_check(sizing)
    sizing.export_override = {"bom": "auto",
                              "manual": {"chassis": "Dell PowerEdge R6715"}}
    out = eo.apply_to_rec(_rec(), eo.resolve(sizing))
    assert out["vendor_chassis"] == "Dell PowerEdge R6715"   # manual
    assert out["cores_per_node"] == 48                       # still the BOM's
    assert out["ram_per_node_gb"] == 768


def test_manual_only_override_without_any_bom_check(sizing):
    sizing.export_override = {"bom": "auto", "manual": {
        "chassis": "Supermicro AS-1115HS", "cores_per_node": 64,
        "node_count": 3, "ram_per_node_gb": 1024}}
    out = eo.apply_to_rec(_rec(), eo.resolve(sizing))
    assert out["vendor_chassis"] == "Supermicro AS-1115HS"
    # The OS overheads are carried across from the sized config rather than
    # re-derived: 32 - 30 cores and 512 - 480 GB.
    assert out["usable_cores_per_node"] == 62
    assert out["usable_ram_per_node_gb"] == 992
    assert out["totals"]["cores"] == 62 * 3
    assert out["totals"]["ram_gb"] == 992 * 3


def test_a_quoted_node_count_splits_hci_and_storage_only(sizing):
    """node_count is the whole cluster: the HCI count is what is left after the
    storage-only nodes, which is what every cluster figure is summed over."""
    rec = _rec()
    rec["storage_only"] = {"count": 2, "cpu": "1 x Xeon 4310"}
    rec["hci_node_count"] = 2
    sizing.export_override = {"bom": "auto", "manual": {"node_count": 6}}
    out = eo.apply_to_rec(rec, eo.resolve(sizing))
    assert out["node_count"] == 6
    assert out["hci_node_count"] == 4


def test_a_bom_cluster_states_its_own_hci_split(sizing):
    _add_check(sizing)
    out = eo.apply_to_rec(_rec(), eo.resolve(sizing))
    assert out["node_count"] == 5
    assert out["hci_node_count"] == 5


def test_the_stored_snapshot_is_never_mutated(sizing):
    _add_check(sizing)
    before = sizing.result_snapshot["clusters"][0]["recommendation"]["vendor_chassis"]
    patched = eo.apply_to_snapshot(sizing)
    after = sizing.result_snapshot["clusters"][0]["recommendation"]["vendor_chassis"]
    assert before == after == "Dell PowerEdge R660"
    assert patched["clusters"][0]["recommendation"]["vendor_chassis"] \
        == "Dell PowerEdge R670"


def test_vendor_mismatch_is_followed_and_reported(sizing):
    _add_check(sizing, result=_bom_result(server="ThinkSystem SR630 V3",
                                          brand="lenovo"), vendor="lenovo")
    badge = eo.badge_for(sizing)
    # The BOM is what is being ordered (owner decision), so it wins — but the
    # vendor it came from is on the badge for the warning the UI shows.
    assert badge["chassis"] == "Lenovo ThinkSystem SR630 V3"
    assert badge["vendor"] == "lenovo"


def test_a_server_line_already_naming_its_brand_is_not_doubled(sizing):
    _add_check(sizing, result=_bom_result(server="Dell PowerEdge R670"))
    assert eo.badge_for(sizing)["chassis"] == "Dell PowerEdge R670"


def test_badges_for_resolves_a_whole_project_in_one_pass(sizing):
    _add_check(sizing)
    badges = eo.badges_for([sizing])
    assert badges[sizing.id]["chassis"] == "Dell PowerEdge R670"


# ── the API ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def client(app, sizing):
    with app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = sizing.owner_id
            sess["_fresh"] = True
        yield c


def test_get_returns_the_setting_and_the_candidates(client, sizing):
    _add_check(sizing)
    res = client.get(f"/api/sizings/{sizing.id}/export-override")
    assert res.status_code == 200
    body = res.get_json()
    assert body["setting"] == {"bom": "auto", "manual": {}}
    assert body["effective"]["chassis"] == "Dell PowerEdge R670"
    assert len(body["checks"]) == 1
    assert body["checks"][0]["values"]["cores_per_node"] == 48


def test_put_stores_and_clears_the_override(client, sizing):
    res = client.put(f"/api/sizings/{sizing.id}/export-override",
                     json={"bom": "none", "manual": {"chassis": "R670"}})
    assert res.status_code == 200
    assert db.session.get(Configuration, sizing.id).export_override == {
        "bom": "none", "manual": {"chassis": "R670"}}

    res = client.put(f"/api/sizings/{sizing.id}/export-override",
                     json={"bom": "auto", "manual": {}})
    assert res.status_code == 200
    # A default setting stores as NULL, so "has an override" stays a non-null
    # test in queries.
    assert db.session.get(Configuration, sizing.id).export_override is None


def test_the_comparison_screen_shows_the_quoted_chassis(client, sizing):
    """The comparison and compare.xlsx read the same helper as the exports, so
    a chosen option cannot read as one box on screen and another in the file."""
    _add_check(sizing)
    res = client.post(f"/api/projects/{sizing.project_id}/compare",
                      json={"sizing_ids": [sizing.id]})
    assert res.status_code == 200
    rows = res.get_json()["rows"]
    assert rows[0]["totals"]["model"] == "Dell PowerEdge R670"


def test_put_rejects_someone_elses_sizing(app, sizing):
    other = User(email="other@example.com", password_hash="x",
                 tenant_id=sizing.tenant_id, is_verified=True)
    db.session.add(other)
    db.session.commit()
    with appmod.app.test_client() as c:
        with c.session_transaction() as sess:
            sess["user_id"] = other.id
            sess["_fresh"] = True
        res = c.put(f"/api/sizings/{sizing.id}/export-override",
                    json={"bom": "none", "manual": {}})
    assert res.status_code == 403


def test_the_export_worker_renders_the_quoted_chassis(app, sizing):
    """The whole point: what the bundle generators receive.

    sections_for is the single funnel every format goes through, so this is
    also what the PPTX/Word/PDF titles and the per-node table end up naming.
    """
    from project_models import ExportJob
    from export_worker import sections_for

    _add_check(sizing)
    job = ExportJob(user_id=sizing.owner_id, project_id=sizing.project_id,
                    fmt="pptx", sizing_ids=[sizing.id], lang="en")
    db.session.add(job)
    db.session.commit()

    sections, skipped = sections_for(job)
    assert not skipped
    rec = sections[0]["recommendation"]
    assert rec["vendor_chassis"] == "Dell PowerEdge R670"
    assert rec["cores_per_node"] == 48
    # and the sizing itself still holds what was sized
    assert sizing.result_snapshot["clusters"][0]["recommendation"][
        "vendor_chassis"] == "Dell PowerEdge R660"


def test_a_real_proposal_names_the_quoted_chassis(app):
    """End-to-end on REAL engine output: seed a catalog, size a Validated
    cluster, attach a BOM check for another chassis, and read the rendered
    PPTX/DOCX back.

    The hand-built recommendation above is a trimmed fixture; this one is
    whatever the engine actually produces today, which is the only way to know
    the document does not still say the sized chassis somewhere.
    """
    import io

    from docx import Document
    from pptx import Presentation

    from export_docx import build_bundle_proposal_docx
    from export_pptx import generate_bundle_proposal
    from export_worker import sections_for
    from hcl_models import HclPlatform
    from orm_models import (CpuCatalog, DriveCatalog, DriveTypeIops, Model,
                            ModelCpuOption, RamOption, SizingSetting,
                            StorageConfig, StorageConfigDrive)
    from project_models import ExportJob
    from recommend import generate_recommendations
    from tunables import DEFAULTS

    summary = {
        "active_vms": 40, "total_vms": 44, "total_vcpus": 180,
        "total_ram_gb": 900, "used_storage_tb": 22.5, "total_storage_tb": 60.0,
        "hosts": 4, "total_host_ghz": 400.0, "peak_cpu_ghz": 120.0,
        "total_host_cores": 96, "total_host_ram_gb": 1024, "vm_iops": 0,
        "peak_ram_gb": 700, "total_vm_provisioned_memory_gb": 900,
        "datastore_used_tb": 22.5, "nic_speed_mbps": 10000,
    }

    for key, value in DEFAULTS.items():
        db.session.add(SizingSetting(key=key, value=float(value)))
    nvme = DriveCatalog(drive_type="NVMe", size_tb=7.68)
    db.session.add_all([nvme, DriveTypeIops(drive_type="NVMe", iops=75000)])
    db.session.flush()
    cpu = CpuCatalog(description="Xeon Gold 6526Y", cores=32, threads=64, ghz=2.4)
    model = Model(name="HC3450DF", status="Active", category="3XXX Core",
                  form_factor="1U", chassis="Catalog chassis", min_nodes=2,
                  cost_tier=5.0)
    db.session.add_all([cpu, model])
    db.session.flush()
    storage = StorageConfig(model_id=model.id, storage_type="nvme_only",
                            drives_per_node=4)
    db.session.add(storage)
    db.session.flush()
    db.session.add_all([
        StorageConfigDrive(storage_config_id=storage.id, drive_id=nvme.id),
        ModelCpuOption(model_id=model.id, cpu_id=cpu.id, quantity=2),
        # The sized chassis, and the one the partner quotes instead.
        HclPlatform(brand="dell", sc_model="HC3450DF",
                    server="PowerEdge R660", form_factor="1U"),
    ])
    for size in (256, 512, 1024):
        db.session.add(RamOption(model_id=model.id, size_gb=size))
    db.session.commit()

    result = generate_recommendations(summary, 4.0, sizing_mode="validated",
                                      vendor="dell")
    recs = result["recommendations"]
    assert recs, "the seeded catalog produced no Validated candidates"
    rec = recs[0]
    assert rec["vendor_chassis"] == "Dell PowerEdge R660"

    tenant = Tenant(domain="e2e.example")
    db.session.add(tenant)
    db.session.flush()
    user = User(email="e2e@e2e.example", password_hash="x",
                tenant_id=tenant.id, is_verified=True)
    db.session.add(user)
    db.session.flush()
    project = Project(name="E2E", owner_id=user.id, tenant_id=tenant.id,
                      code="PRJE")
    db.session.add(project)
    db.session.flush()
    config = Configuration(
        code="SZE", name="Site A", owner_id=user.id, tenant_id=tenant.id,
        project_id=project.id, payload={"mode": "import"},
        result_snapshot={"clusters": [{
            "name": "Prod", "summary": summary,
            "projection": result["projection"], "recommendation": rec,
            "refs": rec.get("refs") or {"mode": "appliance"},
        }], "totals": {}, "tunables": "x"})
    db.session.add(config)
    db.session.commit()
    _add_check(config)   # a passing BOM check for the R670

    job = ExportJob(user_id=user.id, project_id=project.id, fmt="pptx",
                    sizing_ids=[config.id], lang="en")
    db.session.add(job)
    db.session.commit()
    sections, skipped = sections_for(job)
    assert not skipped

    pptx = Presentation(io.BytesIO(
        generate_bundle_proposal(sections, lang="en").getvalue()))
    text = []
    for slide in pptx.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                text.append(shape.text_frame.text)
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    text.append(" | ".join(c.text for c in row.cells))
    pptx_text = "\n".join(text)

    doc = Document(io.BytesIO(
        build_bundle_proposal_docx(sections, lang="en").getvalue()))
    docx_text = "\n".join([p.text for p in doc.paragraphs] +
                          [" | ".join(c.text for c in row.cells)
                           for table in doc.tables for row in table.rows])

    for name, body in (("pptx", pptx_text), ("docx", docx_text)):
        assert "PowerEdge R670" in body, f"{name} never names the quoted chassis"
        # The sized chassis must not survive anywhere in a customer document,
        # and neither must the SC model (which exports never show).
        assert "PowerEdge R660" not in body, f"{name} still names the sized chassis"
        assert "HC3450DF" not in body, f"{name} leaked the SC model"
        # the quoted per-node hardware, not the sized 32C/512GB node
        assert "6542Y" in body, f"{name} shows the sized CPU"


def test_put_ignores_fields_that_are_not_override_fields(client, sizing):
    client.put(f"/api/sizings/{sizing.id}/export-override",
               json={"bom": "auto", "manual": {"score": 1, "model": "HC9999",
                                               "chassis": "R670"}})
    stored = db.session.get(Configuration, sizing.id).export_override
    assert stored["manual"] == {"chassis": "R670"}
