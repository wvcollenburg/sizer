"""Never one disk in a node that can hold more (owner decision 2026-09-17).

A Validated recommendation recommended a 12-bay Lenovo SR650 with a single
20 TB HDD. The reason for the rule is redundancy, not cost: with one disk, a
disk failure is a node failure, which a multi-bay chassis never has to accept.

Pinned here:
  * the chassis bay count (HCL "up to N HDD / SSD") decides whether a node can
    hold more than one disk; the model's certified disk count stands in when
    the HCL does not say;
  * such a node never gets 1 disk; 3+ is preferred and 2 is used only when no
    3+ build works — and never on a Single Node System;
  * a box certified with ONE disk (the HE15x NUCs) keeps it: Validated only
    removes disks from the certified build;
  * the manual Validated calculator accepts 2 disks;
  * the BOM checker warns about a single disk in a multi-bay chassis.

Run: .venv/bin/python -m pytest tests/test_disk_redundancy.py -q
"""
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.pop("SESSION_COOKIE_SECURE", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402

import recommend  # noqa: E402
from recommend import (_multi_disk_node, _pick_uniform_drives,  # noqa: E402
                       _validated_disk_counts)

HDD_SIZES = [4.0, 8.0, 12.0, 16.0, 20.0]


def _disks(pick):
    return sum(pick["drive_counts"].values())


# ── which counts are allowed ─────────────────────────────────────────────────

def test_a_multi_disk_node_never_offers_one_disk():
    preferred, fallback = _validated_disk_counts(12, multi_disk=True)
    assert 1 not in preferred and 2 not in preferred
    assert preferred == list(range(3, 13))
    assert fallback == [2]


def test_a_single_disk_node_keeps_one():
    preferred, fallback = _validated_disk_counts(1, multi_disk=False)
    assert preferred == [1] and fallback == []


def test_no_two_disk_fallback_on_a_single_node_system():
    _preferred, fallback = _validated_disk_counts(12, multi_disk=True, single_node=True)
    assert fallback == []


def test_bay_count_decides_and_certified_count_is_the_fallback():
    hdd12 = {"type": "hdd_only", "drives_per_node": 12}
    nuc = {"type": "nvme_only", "drives_per_node": 1}
    assert _multi_disk_node(12, hdd12) is True          # HCL: 12 bays
    assert _multi_disk_node(None, hdd12) is True        # no HCL figure: certified 12
    assert _multi_disk_node(1, hdd12) is False          # HCL says one bay
    # Certified with one disk: nothing to keep, even with a spare bay.
    assert _multi_disk_node(2, nuc) is False
    assert _multi_disk_node(None, nuc) is False


# ── what the picker does ─────────────────────────────────────────────────────

def test_the_sr650_case_gets_three_disks_not_one():
    """The reported case: a small workload in a 12-bay node. One 20 TB disk
    would cover it; it must still get 3+."""
    pick = _pick_uniform_drives(HDD_SIZES, 12, usable_needed=15.0,
                                cluster_layout=[3], drive_type="hdd",
                                validated=True, multi_disk=True)
    assert _disks(pick) >= 3


def test_without_the_rule_one_disk_is_still_the_closest_fit():
    """Guards the test above: it must be the rule doing the work."""
    pick = _pick_uniform_drives(HDD_SIZES, 12, usable_needed=15.0,
                                cluster_layout=[3], drive_type="hdd",
                                validated=True, multi_disk=False)
    assert _disks(pick) == 1


def test_two_disks_only_when_no_three_disk_build_exists():
    # Certified build of 2: no 3+ count exists, so 2 is used.
    pick = _pick_uniform_drives([3.84, 7.68], 2, usable_needed=5.0,
                                cluster_layout=[3], drive_type="nvme",
                                validated=True, multi_disk=True)
    assert _disks(pick) == 2
    # The per-cluster disk cap (100) blocks 3 x 40 nodes but allows 2 x 40.
    from tunables import T
    T.set_values({"max_cluster_disks": 100})
    pick = _pick_uniform_drives(HDD_SIZES, 12, usable_needed=15.0,
                                cluster_layout=[40], drive_type="hdd",
                                validated=True, multi_disk=True)
    assert _disks(pick) == 2


def test_a_single_node_system_never_falls_back_to_two():
    pick = _pick_uniform_drives([3.84, 7.68], 2, usable_needed=1.0,
                                cluster_layout=[1], drive_type="nvme",
                                validated=True, multi_disk=True)
    assert pick is None


def test_certified_builds_are_untouched():
    pick = _pick_uniform_drives(HDD_SIZES, 12, usable_needed=15.0,
                                cluster_layout=[3], drive_type="hdd",
                                validated=False, multi_disk=True)
    assert _disks(pick) == 12


# ── the engine end to end ────────────────────────────────────────────────────

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


def _seed_hc5400(hdd_max):
    import orm_models as om
    from database import db
    from hcl_models import HclPlatform
    from tunables import DEFAULTS

    for key, value in DEFAULTS.items():
        db.session.add(om.SizingSetting(key=key, value=float(value)))
    cpu = om.CpuCatalog(description="Xeon Gold 6426Y", cores=16, threads=32, ghz=3.3)
    db.session.add(cpu)
    db.session.flush()
    m = om.Model(name="HC5400", status="Active", category="5XXX", form_factor="2U",
                 chassis="Lenovo SR650v3", min_nodes=3, cost_tier=5.0, validated_only=False)
    db.session.add(m)
    db.session.flush()
    db.session.add(om.ModelCpuOption(model_id=m.id, cpu_id=cpu.id, quantity=1))
    db.session.add(om.RamOption(model_id=m.id, size_gb=256))
    sc = om.StorageConfig(model_id=m.id, storage_type="hdd_only", drives_per_node=12)
    db.session.add(sc)
    db.session.flush()
    for size in HDD_SIZES:
        drive = om.DriveCatalog(drive_type="HDD", size_tb=size)
        db.session.add(drive)
        db.session.flush()
        db.session.add(om.StorageConfigDrive(storage_config_id=sc.id, drive_id=drive.id))
    db.session.add(HclPlatform(brand="lenovo", sc_model="HC5400", server="ThinkSystem SR650 V3",
                               form_factor="2U", hdd_max=hdd_max, ssd_max=hdd_max))
    db.session.commit()


SMALL_WORKLOAD = {
    "active_vms": 20, "total_vms": 20, "total_vcpus": 64, "total_ram_gb": 400,
    "used_storage_tb": 9.0, "total_storage_tb": 12.0, "hosts": 3,
    "total_host_ghz": 150.0, "peak_cpu_ghz": 40.0, "total_host_cores": 48,
    "total_host_ram_gb": 768, "vm_iops": 0, "peak_ram_gb": 400,
    "total_vm_provisioned_memory_gb": 400, "datastore_used_tb": 9.0,
    "nic_speed_mbps": 10000,
}


def test_validated_recommendations_for_a_12_bay_node_have_more_than_one_disk(app):
    _seed_hc5400(hdd_max=12)
    result = recommend.generate_recommendations(SMALL_WORKLOAD, 3.0,
                                                sizing_mode="validated", vendor="lenovo")
    recs = result["recommendations"]
    assert recs, "no Validated candidates"
    for rec in recs:
        assert sum(rec["storage_config"]["drive_counts"].values()) >= 3, \
            rec["storage_config"]["desc"]


# ── manual calculator and BOM checker ────────────────────────────────────────

def test_the_manual_validated_calculator_accepts_two_disks(app):
    from calc import calculate_validated
    result = calculate_validated({
        "cores_per_node": 16, "threads_per_node": 32, "ghz": 3.0, "ram_gb": 256,
        "disks": [{"type": "SSD", "size_tb": 3.84}, {"type": "SSD", "size_tb": 3.84}],
    }, 3)
    assert "error" not in result, result.get("error")


def test_bom_checker_warns_about_a_single_disk_in_a_multi_bay_chassis():
    from bom import enrich
    from bom.normalize import BOMComponent, BOMConfig
    from hcl_models import HclPlatform

    def config(disks_per_node):
        return BOMConfig(name="SR650", server_model="ThinkSystem SR650 V3", node_count=3,
                         components=[
                             BOMComponent(None, "ThinkSystem SR650 V3 Chassis", 3, "chassis"),
                             BOMComponent(None, "ThinkSystem 3.5\" 20TB 7.2K SAS HDD",
                                          3 * disks_per_node, "storage"),
                         ])

    sr650 = [HclPlatform(brand="lenovo", sc_model="HC5400", server="ThinkSystem SR650 V3",
                         hdd_max=12, ssd_max=12)]
    finding = enrich.single_disk_finding(config(1), sr650)
    assert finding is not None and finding.severity == "warning"
    assert finding.code == "single_disk_multi_bay"
    assert enrich.single_disk_finding(config(3), sr650) is None
    # a one-bay platform, or one whose bays the HCL does not state: no warning
    tiny = [HclPlatform(brand="lenovo", sc_model="HE150", server="Tiny", hdd_max=None, ssd_max=1)]
    assert enrich.single_disk_finding(config(1), tiny) is None
    assert enrich.single_disk_finding(config(1), []) is None
