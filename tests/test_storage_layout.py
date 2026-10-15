"""One drive layout per storage type (storage_layout.py), shared by the
calculator and the engine.

Every consumer used to re-derive the per-tier drive counts with its own
fallback (4 SSDs here, 1 there, 0 for the RAM tier). Owner decision: a
missing count on a type that needs one is a catalog error, never a guess.

Run: .venv/bin/python -m pytest tests/test_storage_layout.py -q
"""
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.pop("SESSION_COOKIE_SECURE", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402

import storage_layout as sl  # noqa: E402
from storage_layout import StorageLayoutError  # noqa: E402

HYBRID = {"type": "hybrid", "hdd_count": 3, "ssd_count": 1,
          "hdd_options_tb": [4, 8], "ssd_options_tb": [0.96, 1.92]}
HYBRID_NVME = {"type": "hybrid_nvme", "hdd_count": 9, "nvme_count": 3,
               "hdd_options_tb": [8, 16], "nvme_options_tb": [3.84]}
NVME_SSD = {"type": "nvme_and_ssd", "nvme_options_tb": [1.92, 3.84], "ssd_options_tb": [0.96]}
SSD = {"type": "ssd_only", "drives_per_node": 4, "ssd_options_tb": [1.92, 3.84, 7.68]}
HDD = {"type": "hdd_only", "drives_per_node": 12, "hdd_options_tb": [8, 20]}
NVME = {"type": "nvme_only", "drives_per_node": 1, "nvme_options_tb": [0.96, 1.92]}
CLOUD = {"type": "cloud", "options": ["standard"]}


# ── slots per type ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("storage, slots", [
    (HYBRID, [("hdd", 3), ("ssd", 1)]),
    (HYBRID_NVME, [("hdd", 9), ("nvme", 3)]),
    (NVME_SSD, [("nvme", 1), ("ssd", 1)]),
    (SSD, [("ssd", 4)]),
    (HDD, [("hdd", 12)]),
    (NVME, [("nvme", 1)]),
    (CLOUD, []),
    ({"type": "something_new"}, []),
])
def test_drive_slots_per_type(storage, slots):
    assert sl.drive_slots(storage) == slots
    assert sl.drive_count(storage) == sum(n for _, n in slots)


def test_slot_count_is_zero_for_a_tier_the_type_lacks():
    assert sl.slot_count(HYBRID, "hdd") == 3
    assert sl.slot_count(HYBRID, "nvme") == 0
    assert sl.slot_count(SSD, "ssd") == 4


# ── arithmetic ───────────────────────────────────────────────────────────────

def test_raw_and_biggest_follow_the_chosen_sizes():
    assert sl.raw_per_node(HYBRID, {"hdd": 8, "ssd": 1.92}) == 8 * 3 + 1.92
    assert sl.biggest_disk_tb(HYBRID, {"hdd": 8, "ssd": 1.92}) == 8
    assert sl.raw_per_node(NVME_SSD, {"nvme": 3.84, "ssd": 0.96}) == 3.84 + 0.96
    assert sl.biggest_disk_tb(NVME_SSD, {"nvme": 3.84, "ssd": 0.96}) == 3.84
    assert sl.raw_per_node(HDD, {"hdd": 20}) == 240
    assert sl.raw_per_node(CLOUD, {}) == 0
    assert sl.biggest_disk_tb(CLOUD, {}) == 0


def test_max_raw_per_node_uses_the_largest_certified_size_of_every_tier():
    assert sl.max_raw_per_node(HYBRID) == (8 * 3 + 1.92, 8)
    assert sl.max_raw_per_node(HYBRID_NVME) == (16 * 9 + 3.84 * 3, 16)
    assert sl.max_raw_per_node(NVME_SSD) == (3.84 + 0.96, 3.84)
    assert sl.max_raw_per_node(SSD) == (7.68 * 4, 7.68)
    assert sl.max_raw_per_node(CLOUD) == (0, 0)
    # a tier without sizes means the model can't be sized for storage
    assert sl.max_raw_per_node(dict(HYBRID, ssd_options_tb=[])) == (0, 0)


# ── missing counts are catalog errors ────────────────────────────────────────

@pytest.mark.parametrize("storage, missing", [
    ({"type": "ssd_only", "ssd_options_tb": [1.92]}, "drives_per_node"),
    ({"type": "hdd_only"}, "drives_per_node"),
    ({"type": "nvme_only", "drives_per_node": None}, "drives_per_node"),
    ({"type": "hybrid", "hdd_count": 3}, "ssd_count"),
    ({"type": "hybrid", "ssd_count": 1}, "hdd_count"),
    ({"type": "hybrid_nvme", "hdd_count": 3}, "nvme_count"),
])
def test_a_missing_count_raises_naming_the_field_and_the_type(storage, missing):
    with pytest.raises(StorageLayoutError) as exc:
        sl.drive_slots(storage)
    msg = str(exc.value)
    assert missing in msg and storage["type"] in msg
    assert issubclass(StorageLayoutError, ValueError)
    for fn in (sl.drive_count, sl.max_raw_per_node):
        with pytest.raises(StorageLayoutError):
            fn(storage)


@pytest.mark.parametrize("bad", [0, -1, 2.5, "4", True])
def test_a_count_must_be_a_positive_integer(bad):
    with pytest.raises(StorageLayoutError):
        sl.drive_slots({"type": "ssd_only", "drives_per_node": bad})


def test_the_shipped_catalog_names_every_count_its_types_need():
    from models import APPLIANCE_MODELS
    for name, m in APPLIANCE_MODELS.items():
        sl.drive_slots(m["storage"])  # raises on a gap


# ── the calculator and the engine go through the same layout ─────────────────

def test_calc_helpers_agree_with_the_layout():
    from calc import (compute_biggest_disk_appliance, compute_drive_count_appliance,
                      compute_raw_per_node_appliance)
    # default sizes: the smallest certified option per tier
    assert compute_raw_per_node_appliance({}, HYBRID) == 4 * 3 + 0.96
    assert compute_biggest_disk_appliance({}, HYBRID) == 4
    # chosen sizes from the request
    assert compute_raw_per_node_appliance({"hdd_tb": 8}, HYBRID) == 8 * 3 + 0.96
    assert compute_raw_per_node_appliance({"ssd_tb": 7.68}, SSD) == 7.68 * 4
    assert compute_raw_per_node_appliance({}, NVME_SSD) == 1.92 + 0.96
    assert compute_drive_count_appliance({}, NVME_SSD) == 2
    assert compute_drive_count_appliance({}, HDD) == 12
    assert compute_drive_count_appliance({}, CLOUD) == 0
    with pytest.raises(StorageLayoutError):
        compute_raw_per_node_appliance({}, {"type": "ssd_only", "ssd_options_tb": [1.92]})


def test_engine_helpers_agree_with_the_layout():
    from recommend import _bay_count, _max_raw_per_node
    assert _bay_count(HYBRID) == 4
    assert _bay_count(NVME_SSD) == 2
    assert _bay_count(CLOUD) == 0
    assert _max_raw_per_node(SSD) == (7.68 * 4, 7.68)
    with pytest.raises(StorageLayoutError):
        _bay_count({"type": "hdd_only"})


# ── routes answer 400, not 500, on a broken catalog entry ────────────────────

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


def _user(app):
    c = app.test_client()
    c.post("/api/auth/signup", json={"email": "pm@partnerco.example", "password": "Abcdef1!xy",
                                     "accept_privacy": True})
    c.post("/api/auth/login", json={"email": "pm@partnerco.example", "password": "Abcdef1!xy"})
    return c


def _seed_broken_model(name="HC1250"):
    """A model whose storage config lost its drive count (an old or
    hand-edited row): the engine must refuse it, not crash on it."""
    import orm_models as om
    from database import db
    from tunables import DEFAULTS

    for key, value in DEFAULTS.items():
        db.session.add(om.SizingSetting(key=key, value=float(value)))
    cpu = om.CpuCatalog(description="Xeon Silver 4410Y", cores=12, threads=24, ghz=2.0)
    db.session.add(cpu)
    db.session.flush()
    m = om.Model(name=name, status="Active", category="1XXX", form_factor="1U",
                 chassis="Dell R660xs", min_nodes=1, cost_tier=1.0, validated_only=False)
    db.session.add(m)
    db.session.flush()
    db.session.add(om.ModelCpuOption(model_id=m.id, cpu_id=cpu.id, quantity=1))
    db.session.add(om.RamOption(model_id=m.id, size_gb=256))
    sc = om.StorageConfig(model_id=m.id, storage_type="nvme_only")  # no drives_per_node
    db.session.add(sc)
    db.session.flush()
    drive = om.DriveCatalog(drive_type="NVMe", size_tb=3.84)
    db.session.add(drive)
    db.session.flush()
    db.session.add(om.StorageConfigDrive(storage_config_id=sc.id, drive_id=drive.id))
    db.session.commit()


def test_calculate_answers_400_for_a_model_without_its_drive_count(app):
    with app.app_context():
        _seed_broken_model()
    r = _user(app).post("/api/calculate", json={
        "mode": "appliance", "model": "HC1250", "node_count": 3})
    assert r.status_code == 400, r.get_data(as_text=True)
    assert "drives_per_node" in r.get_json()["error"]


def test_recommend_answers_400_for_a_model_without_its_drive_count(app):
    with app.app_context():
        _seed_broken_model()
    summary = {
        "active_vms": 20, "total_vms": 20, "total_vcpus": 64, "total_ram_gb": 400,
        "used_storage_tb": 9.0, "total_storage_tb": 12.0, "hosts": 3,
        "total_host_ghz": 150.0, "peak_cpu_ghz": 40.0, "total_host_cores": 48,
        "total_host_ram_gb": 768, "vm_iops": 0, "peak_ram_gb": 400,
        "total_vm_provisioned_memory_gb": 400, "datastore_used_tb": 9.0,
        "nic_speed_mbps": 10000,
    }
    r = _user(app).post("/api/recommend", json={"summary": summary, "vcpu_ratio": 3.0})
    assert r.status_code == 400, r.get_data(as_text=True)
    assert "drives_per_node" in r.get_json()["error"]


def test_seed_refuses_a_storage_config_without_its_count(app, monkeypatch):
    import seed
    broken = {"HCX": {
        "status": "Active", "category": "1XXX", "form_factor": "1U", "chassis": "x",
        "socket": "single", "cpu_options": [
            {"desc": "1 x Xeon 8C/16T 2.0GHz", "cores": 8, "threads": 16, "ghz": 2.0}],
        "ram_options_gb": [64], "storage": {"type": "ssd_only", "ssd_options_tb": [1.92]},
        "nic_options": [], "psu": "x", "ram_slots": 4, "min_nodes": 1,
    }}
    monkeypatch.setattr(seed, "APPLIANCE_MODELS", broken)
    with app.app_context():
        with pytest.raises(StorageLayoutError):
            seed.seed_appliance_models()
