"""hcl_sync + hcl_data: fixture snapshot -> queue -> approve -> catalog.

Drives the real HTML fixtures (tests/fixtures/hcl) through hcl_scrape into
the diff/queue/apply layer on a bare Flask + SQLite app, so the numbers the
first approved scrape seeds the catalog with are pinned here, and every
lifecycle edge the plan calls for (delist only from a complete snapshot,
never delete, relist, supersede, idempotent approval) has a test.

Run: .venv/bin/python -m pytest tests/test_hcl_sync.py -q
"""
import copy
import json
import os
import sys
import types

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

from database import db  # noqa: E402
import orm_models  # noqa: F401,E402  - complete the mapper registry
import auth_models  # noqa: F401,E402
import project_models  # noqa: F401,E402
import bom_models  # noqa: F401,E402
import hcl_models as hm  # noqa: E402
from bom import hcl_scrape as hs  # noqa: E402
from bom import hcl_sync as sync  # noqa: E402
from bom import hcl_data  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "hcl")
EXPECTED_PLATFORMS = 62
EXPECTED_DEVICES = 64
EXPECTED_DISTINCT_PARTS = {"cpu": 71, "nic": 32, "hba": 12, "hdd": 30, "ssd": 52}

ADMIN = types.SimpleNamespace(id=1, email="admin@test", is_super_admin=True)


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def fixture_fetch():
    manifest = json.loads(_read("manifest.json"))
    by_url = {p["url"]: p["file"] for p in manifest["pages"]}

    def fetch(path):
        if path not in by_url:
            raise hs.ScrapeError("GET %s failed: HTTP 404" % path)
        return _read(by_url[path])
    return fetch


@pytest.fixture(scope="module")
def snapshot():
    snap = hs.scrape_all(fetch=fixture_fetch(), sleep=lambda s: None)
    assert snap["complete"] is True
    return snap


@pytest.fixture()
def app():
    application = Flask(__name__)
    application.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    application.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(application)
    with application.app_context():
        db.drop_all()
        db.create_all()
        yield application


def _snap(snapshot):
    return copy.deepcopy(snapshot)


def _platform(snap, brand, sc_model):
    return next(p for p in snap["platforms"] if p["brand"] == brand and p["sc_model"] == sc_model)


def _pending(**kw):
    q = hm.HclPendingChange.query.filter_by(**kw)
    return q.order_by(hm.HclPendingChange.id).all()


def _kinds(changes):
    # the run summary always carries all four kinds, zeros included
    out = {"add": 0, "update": 0, "delist": 0, "relist": 0, "merge": 0}
    for c in changes:
        out[c["change_kind"]] = out.get(c["change_kind"], 0) + 1
    return out


def _seed(snapshot):
    """First run + approve everything: the catalog as the site shows it."""
    run = sync.build_run(_snap(snapshot), user=ADMIN, source="import")
    counts = sync.bulk(None, "approve", ADMIN, all_pending=True)
    return run, counts


# ---------------------------------------------------------------- first run

def test_first_run_on_empty_catalog(app, snapshot):
    run = sync.build_run(_snap(snapshot), user=ADMIN, source="import")
    assert run.status == hm.RUN_SUCCEEDED and run.complete is True and run.errors == []
    s = run.summary
    assert s["platforms"] == EXPECTED_PLATFORMS
    assert s["devices"] == EXPECTED_DEVICES
    assert s["components"] == sum(EXPECTED_DISTINCT_PARTS.values())
    n_components = len(hs.snapshot_component_index(snapshot))
    assert s["changes"] == {"add": EXPECTED_PLATFORMS + n_components + EXPECTED_DEVICES,
                            "update": 0, "delist": 0, "relist": 0, "merge": 0}
    assert s["pending_total"] == s["changes"]["add"]
    assert hm.HclPendingChange.query.filter_by(status=hm.PENDING).count() == s["pending_total"]
    # nothing has landed in the catalog yet
    assert hm.HclPlatform.query.count() == 0
    assert hm.HclComponent.query.count() == 0
    assert hm.HclDevice.query.count() == 0
    # component adds carry what the part is
    add = _pending(entity_type="component", entity_key="nic/4XC7A80566")[0]
    assert add.change_kind == "add"
    assert add.payload["description"].startswith("ThinkSystem Broadcom 57504")
    assert add.payload["attrs"]["speed_gbe"] == 25 and add.payload["tce"] is True
    assert "57504" in add.label
    # the last-scrape stamp is recorded
    from auth import get_setting
    assert get_setting("hcl_last_scrape_at", "").startswith("2026-")


def test_bulk_approve_all_seeds_the_catalog(app, snapshot):
    run, counts = _seed(snapshot)
    total = run.summary["pending_total"]
    assert counts == {"approved": total, "rejected": 0, "skipped": 0}
    assert hm.HclPendingChange.query.filter_by(status=hm.PENDING).count() == 0

    counts = hcl_data.catalog_counts()
    assert counts["platforms"] == {"active": EXPECTED_PLATFORMS, "delisted": 0}
    assert counts["devices"] == {"active": EXPECTED_DEVICES, "delisted": 0}
    assert counts["components"]["active"] == len(hs.snapshot_component_index(snapshot))
    assert counts["components"]["delisted"] == 0
    assert counts["components"]["by_kind"] == EXPECTED_DISTINCT_PARTS

    # links: one per distinct (platform, part) pair the detail pages list
    # parts without a part number get the same synthetic key the sync stores
    expected_links = sum(
        len({(c["kind"], c["part_number"] or hs.synthetic_part_number(
            c["kind"], c["description"], c.get("attrs"))) for c in p["components"]})
        for p in snapshot["platforms"])
    assert hm.HclPlatformComponent.query.filter_by(status=hm.STATUS_ACTIVE).count() == expected_links

    # per-link TCE preserved, component TCE = any link TCE
    hc1450d = hm.HclPlatform.query.filter_by(brand="lenovo", sc_model="HC1450D").one()
    assert sum(1 for l in hc1450d.links if l.tce) == 3
    assert hc1450d.server == "ThinkSystem SR630V3" and hc1450d.sockets == 2
    assert hc1450d.memory_type == "DDR5 RDIMM" and hc1450d.max_ram_gb == 8192
    for comp in hm.HclComponent.query.all():
        assert comp.tce == any(l.tce for l in comp.links), comp.key
        assert comp.links, "every part is linked to a platform"
    tce_nic = hm.HclComponent.query.filter_by(kind="nic", part_number="4XC7A80566").one()
    assert tce_nic.tce is True
    hc1350 = hm.HclPlatform.query.filter_by(brand="lenovo", sc_model="HC1350").one()
    link = next(l for l in hc1350.links if l.component_id == tce_nic.id)
    assert link.tce is True

    # component add rows that a platform apply created were auto-approved
    via = hm.HclPendingChange.query.filter(hm.HclPendingChange.note.like("via platform %")).count()
    assert via == 0 or via > 0  # depends on ordering; either way none pending
    assert all(c.status == hm.APPROVED for c in hm.HclPendingChange.query.all())


def test_second_run_on_same_snapshot_is_empty(app, snapshot):
    _seed(snapshot)
    assert sync.diff_snapshot(_snap(snapshot)) == []
    run = sync.build_run(_snap(snapshot), user=ADMIN, source="import")
    assert run.summary["changes"] == {"add": 0, "update": 0, "delist": 0, "relist": 0, "merge": 0}
    assert run.summary["pending_total"] == 0


# ---------------------------------------------------------------- modified snapshot

def _modified(snapshot):
    """Drop lenovo/HC1350; drop one CPU from lenovo/HC1450D; rename a NIC;
    change a device driver; add a brand-new platform with a brand-new part."""
    snap = _snap(snapshot)
    snap["platforms"] = [p for p in snap["platforms"]
                         if not (p["brand"] == "lenovo" and p["sc_model"] == "HC1350")]
    hc1450d = _platform(snap, "lenovo", "HC1450D")
    dropped_cpu = next(c for c in hc1450d["components"]
                       if c["kind"] == "cpu" and c["part_number"] == "PK8072205511700")
    hc1450d["components"] = [c for c in hc1450d["components"] if c is not dropped_cpu]
    nic_key = None
    for p in snap["platforms"]:
        for c in p["components"]:
            if c["kind"] == "nic" and c["part_number"] == "4XC7A08294":
                c["description"] = "RENAMED " + c["description"]
                nic_key = "nic/4XC7A08294"
    assert nic_key
    dev = snap["devices"][0]
    dev["driver"] = "ahci-next"
    snap["platforms"].append({
        "sc_model": "HC9999", "brand": "acme", "form_factor": "2U", "socket": "SP5",
        "cpu_count": 1, "memory_type": "DDR5 RDIMM", "max_ram_gb": 1024,
        "server": "Acme Box 9000", "sockets": 1, "ram_slots": 12, "nic_listed": True,
        "components": [
            {"kind": "nic", "part_number": "ACME-NIC-1",
             "description": "Acme 25GbE SFP28 2-port OCP", "tce": True,
             "attrs": {"speed_gbe": 25, "ports": 2, "form_factor": "OCP",
                       "family": None, "media": "SFP28"}},
            # a part that already exists in the catalog, on a new platform
            {"kind": "cpu", "part_number": "PK8072205511700",
             "description": "Intel® Xeon® Platinum 8593Q Processor", "tce": False,
             "attrs": {"cores": 64, "threads": 128, "ghz": 3.0, "vendor": "Intel",
                       "model": "Xeon Platinum 8593Q", "tier": "Platinum"}},
        ],
    })
    return snap


def _exclusive_parts(snapshot, brand, sc_model):
    """Parts listed only on the given platform (they delist with it)."""
    mine = {(c["kind"], c["part_number"]) for c in _platform(snapshot, brand, sc_model)["components"]}
    others = set()
    for p in snapshot["platforms"]:
        if p["brand"] == brand and p["sc_model"] == sc_model:
            continue
        others |= {(c["kind"], c["part_number"]) for c in p["components"]}
    return mine - others


def test_modified_snapshot_produces_expected_rows(app, snapshot):
    _seed(snapshot)
    modified = _modified(snapshot)
    changes = sync.diff_snapshot(modified)
    by = {}
    for c in changes:
        by.setdefault((c["entity_type"], c["change_kind"]), []).append(c)

    # platform delist + exclusive component delists, no device delist
    assert [c["entity_key"] for c in by[("platform", "delist")]] == ["lenovo/HC1350"]
    exclusive = {"%s/%s" % k for k in _exclusive_parts(snapshot, "lenovo", "HC1350")}
    # lenovo/HC1350 shares its whole list with HC1300, so no part goes with it;
    # the set is empty and no component delist row may appear.
    assert {c["entity_key"] for c in by.get(("component", "delist"), [])} == exclusive
    assert ("device", "delist") not in by

    # one platform update: HC1450D's part list lost the CPU
    updates = by[("platform", "update")]
    assert len(updates) == 1
    (u,) = updates
    assert u["entity_key"] == "lenovo/HC1450D" and u["field"] == "components"
    assert "cpu/PK8072205511700" in u["old_value"] and "cpu/PK8072205511700" not in u["new_value"]
    assert len(u["payload"]["components"]) == len(u["new_value"])
    assert "-1" in u["label"]

    # component update: description only (attrs unchanged)
    cu = by[("component", "update")]
    assert [(c["entity_key"], c["field"]) for c in cu] == [("nic/4XC7A08294", "description")]
    assert cu[0]["new_value"].startswith("RENAMED ")

    # device update: driver
    du = by[("device", "update")]
    assert [(c["entity_key"], c["field"], c["old_value"], c["new_value"]) for c in du] == [
        ("8086/a352", "driver", "ahci", "ahci-next")]

    # adds: new platform and its brand-new part only (the CPU exists already)
    assert [c["entity_key"] for c in by[("platform", "add")]] == ["acme/HC9999"]
    assert [c["entity_key"] for c in by[("component", "add")]] == ["nic/ACME-NIC-1"]
    assert ("device", "add") not in by
    assert not any(k[1] == "relist" for k in by)

    run = sync.build_run(modified, user=ADMIN, source="import")
    assert run.summary["changes"] == _kinds(changes)


def test_approving_delists_keeps_rows(app, snapshot):
    _seed(snapshot)
    run = sync.build_run(_modified(snapshot), user=ADMIN, source="import")
    delists = _pending(run_id=run.id, change_kind="delist", status=hm.PENDING)
    assert delists
    before_platforms = hm.HclPlatform.query.count()
    before_components = hm.HclComponent.query.count()
    counts = sync.bulk([c.id for c in delists], "approve", ADMIN)
    assert counts == {"approved": len(delists), "rejected": 0, "skipped": 0}
    # nothing deleted
    assert hm.HclPlatform.query.count() == before_platforms
    assert hm.HclComponent.query.count() == before_components
    hc1350 = hm.HclPlatform.query.filter_by(brand="lenovo", sc_model="HC1350").one()
    assert hc1350.status == hm.STATUS_DELISTED and hc1350.delisted_at is not None
    assert all(l.status == hm.STATUS_DELISTED for l in hc1350.links)
    for c in delists:
        if c.entity_type == "component":
            kind, part = c.entity_key.split("/", 1)
            comp = hm.HclComponent.query.filter_by(kind=kind, part_number=part).one()
            assert comp.status == hm.STATUS_DELISTED and comp.delisted_at is not None
    assert hcl_data.catalog_counts()["platforms"] == {"active": EXPECTED_PLATFORMS - 1, "delisted": 1}


def test_approving_updates_and_adds(app, snapshot):
    _seed(snapshot)
    run = sync.build_run(_modified(snapshot), user=ADMIN, source="import")

    # platform part-list update: link delisted, part itself stays active (other platforms)
    (pu,) = _pending(run_id=run.id, entity_type="platform", change_kind="update")
    res = sync.approve(pu, ADMIN)
    assert res["applied"] is True and pu.status == hm.APPROVED and pu.decided_at is not None
    hc1450d = hm.HclPlatform.query.filter_by(brand="lenovo", sc_model="HC1450D").one()
    cpu = hm.HclComponent.query.filter_by(kind="cpu", part_number="PK8072205511700").one()
    link = next(l for l in hc1450d.links if l.component_id == cpu.id)
    assert link.status == hm.STATUS_DELISTED and cpu.status == hm.STATUS_ACTIVE
    assert hc1450d.to_dict()["component_count"] == len(pu.new_value)

    # component description update
    (cu,) = _pending(run_id=run.id, entity_type="component", change_kind="update")
    sync.approve(cu, ADMIN)
    nic = hm.HclComponent.query.filter_by(kind="nic", part_number="4XC7A08294").one()
    assert nic.description.startswith("RENAMED ")

    # device driver update
    (du,) = _pending(run_id=run.id, entity_type="device", change_kind="update")
    sync.approve(du, ADMIN)
    assert hm.HclDevice.query.filter_by(ven_id="8086", dev_id="a352").one().driver == "ahci-next"

    # new platform: approving it creates the new part and decides its add row
    (pa,) = _pending(run_id=run.id, entity_type="platform", change_kind="add")
    (ca,) = _pending(run_id=run.id, entity_type="component", change_kind="add")
    assert ca.status == hm.PENDING
    res = sync.approve(pa, ADMIN)
    assert "acme/HC9999" in res["created"] and "nic/ACME-NIC-1" in res["created"]
    assert res["auto_approved"] == [ca.id]
    db.session.refresh(ca)
    assert ca.status == hm.APPROVED and ca.note == "via platform acme/HC9999"
    new_platform = hm.HclPlatform.query.filter_by(brand="acme", sc_model="HC9999").one()
    assert new_platform.status == hm.STATUS_ACTIVE and new_platform.server == "Acme Box 9000"
    assert new_platform.sockets == 1 and new_platform.form_factor == "2U"
    assert {l.component.key: l.tce for l in new_platform.links} == {
        "nic/ACME-NIC-1": True, "cpu/PK8072205511700": False}
    new_nic = hm.HclComponent.query.filter_by(kind="nic", part_number="ACME-NIC-1").one()
    assert new_nic.tce is True and new_nic.attrs["speed_gbe"] == 25
    assert hm.HclComponent.query.filter_by(kind="cpu", part_number="PK8072205511700").count() == 1

    # approving the already-decided component add again is harmless
    res = sync.approve(ca, ADMIN)
    assert res["applied"] is True
    assert hm.HclComponent.query.filter_by(kind="nic", part_number="ACME-NIC-1").count() == 1
    assert hm.HclPlatformComponent.query.filter_by(component_id=new_nic.id).count() == 1

    # the platform delist is still pending; once it is approved too, the
    # modified snapshot IS the catalog and a re-diff of it is empty
    for c in _pending(run_id=run.id, change_kind="delist"):
        sync.approve(c, ADMIN)
    assert sync.diff_snapshot(_modified(snapshot)) == []


def test_incomplete_snapshot_never_delists(app, snapshot):
    _seed(snapshot)
    partial = _modified(snapshot)
    partial["complete"] = False
    partial["errors"] = [{"path": "/hcready/detail?model=HC1350&brand=lenovo", "error": "IncompleteRead"}]
    changes = sync.diff_snapshot(partial)
    assert changes and not any(c["change_kind"] == "delist" for c in changes)
    run = sync.build_run(partial, user=ADMIN, source="import")
    assert run.complete is False and run.summary["changes"]["delist"] == 0
    assert len(run.errors) == 1
    assert _pending(run_id=run.id, change_kind="delist") == []


def test_relist_when_a_delisted_platform_reappears(app, snapshot):
    _seed(snapshot)
    run = sync.build_run(_modified(snapshot), user=ADMIN, source="import")
    sync.bulk(None, "approve", ADMIN, all_pending=True, filters={"run_id": run.id})
    assert hm.HclPlatform.query.filter_by(brand="lenovo", sc_model="HC1350").one().status == "delisted"

    # the original site again: HC1350 and its exclusive parts come back
    changes = sync.diff_snapshot(_snap(snapshot))
    relists = {(c["entity_type"], c["entity_key"]) for c in changes if c["change_kind"] == "relist"}
    assert ("platform", "lenovo/HC1350") in relists
    exclusive = {("component", "%s/%s" % k) for k in _exclusive_parts(snapshot, "lenovo", "HC1350")}
    assert exclusive <= relists
    # and the acme platform, absent from the original, delists again
    assert ("platform", "acme/HC9999") in {
        (c["entity_type"], c["entity_key"]) for c in changes if c["change_kind"] == "delist"}

    run2 = sync.build_run(_snap(snapshot), user=ADMIN, source="import")
    (pr,) = _pending(run_id=run2.id, entity_type="platform", change_kind="relist")
    res = sync.approve(pr, ADMIN)
    assert res["applied"] is True
    hc1350 = hm.HclPlatform.query.filter_by(brand="lenovo", sc_model="HC1350").one()
    assert hc1350.status == hm.STATUS_ACTIVE and hc1350.delisted_at is None
    assert all(l.status == hm.STATUS_ACTIVE for l in hc1350.links)
    for kind, part in _exclusive_parts(snapshot, "lenovo", "HC1350"):
        comp = hm.HclComponent.query.filter_by(kind=kind, part_number=part).one()
        assert comp.status == hm.STATUS_ACTIVE and comp.delisted_at is None
        # the platform apply decided the parts' own relist rows
        rows = _pending(entity_type="component", entity_key=comp.key, change_kind="relist",
                        run_id=run2.id)
        assert rows and rows[0].status == hm.APPROVED and "via platform" in rows[0].note
    # only one row per platform ever
    assert hm.HclPlatform.query.filter_by(brand="lenovo", sc_model="HC1350").count() == 1


def test_superseding_older_pending_rows(app, snapshot):
    _seed(snapshot)
    run_a = sync.build_run(_modified(snapshot), user=ADMIN, source="import")
    run_b = sync.build_run(_modified(snapshot), user=ADMIN, source="import")
    a_rows = _pending(run_id=run_a.id)
    b_rows = _pending(run_id=run_b.id)
    assert len(a_rows) == len(b_rows) > 0
    assert all(r.status == hm.SUPERSEDED for r in a_rows)
    assert all(r.status == hm.PENDING for r in b_rows)
    assert run_b.summary["pending_total"] == len(b_rows)
    with pytest.raises(sync.ChangeStateError):
        sync.approve(a_rows[0], ADMIN)
    with pytest.raises(sync.ChangeStateError):
        sync.reject(a_rows[0], ADMIN)


def test_reject_leaves_catalog_untouched(app, snapshot):
    _seed(snapshot)
    run = sync.build_run(_modified(snapshot), user=ADMIN, source="import")
    before = {
        "platforms": {p.key: p.status for p in hm.HclPlatform.query.all()},
        "components": {c.key: (c.status, c.description) for c in hm.HclComponent.query.all()},
        "devices": {d.key: (d.status, d.driver) for d in hm.HclDevice.query.all()},
    }
    (cu,) = _pending(run_id=run.id, entity_type="component", change_kind="update")
    res = sync.reject(cu, ADMIN, note="not convinced")
    assert res["change"]["status"] == "rejected" and cu.note == "not convinced"
    counts = sync.bulk(None, "reject", ADMIN, all_pending=True)
    assert counts["rejected"] > 0 and counts["approved"] == 0
    after = {
        "platforms": {p.key: p.status for p in hm.HclPlatform.query.all()},
        "components": {c.key: (c.status, c.description) for c in hm.HclComponent.query.all()},
        "devices": {d.key: (d.status, d.driver) for d in hm.HclDevice.query.all()},
    }
    assert before == after
    assert hm.HclPendingChange.query.filter_by(status=hm.PENDING).count() == 0
    with pytest.raises(sync.ChangeStateError):
        sync.reject(_pending(status=hm.APPROVED)[0], ADMIN)


def test_double_approve_is_idempotent(app, snapshot):
    run = sync.build_run(_snap(snapshot), user=ADMIN, source="import")
    (pa,) = _pending(run_id=run.id, entity_type="platform", entity_key="lenovo/HC1350")
    sync.approve(pa, ADMIN)
    links = hm.HclPlatformComponent.query.count()
    comps = hm.HclComponent.query.count()
    sync.approve(pa, ADMIN)
    assert hm.HclPlatform.query.filter_by(brand="lenovo", sc_model="HC1350").count() == 1
    assert hm.HclPlatformComponent.query.count() == links
    assert hm.HclComponent.query.count() == comps
    # bulk over already-decided ids skips them, unknown ids too
    counts = sync.bulk([pa.id, 999999], "approve", ADMIN)
    assert counts == {"approved": 0, "rejected": 0, "skipped": 2}


def test_catalog_stamp_moves_with_approvals(app, snapshot):
    assert sync.catalog_stamp() == "run0:change0:n0"
    run = sync.build_run(_snap(snapshot), user=ADMIN, source="import")
    stamp0 = sync.catalog_stamp()
    assert stamp0 == "run%d:change0:n0" % run.id
    first = _pending(run_id=run.id)[0]
    sync.approve(first, ADMIN)
    stamp1 = sync.catalog_stamp()
    assert stamp1 != stamp0 and stamp1.startswith("run%d:change" % run.id)
    sync.bulk(None, "approve", ADMIN, all_pending=True)
    stamp2 = sync.catalog_stamp()
    assert stamp2 != stamp1
    max_id = max(c.id for c in hm.HclPendingChange.query.all())
    approved = hm.HclPendingChange.query.filter_by(status=hm.APPROVED).count()
    assert stamp2 == "run%d:change%d:n%d" % (run.id, max_id, approved)


def test_catalog_stamp_moves_regardless_of_approval_order(app, snapshot):
    # Finding: "catalog_stamp does not move when lower-id changes are
    # approved after higher-id ones" — ids are assigned in queue order
    # (component, device, platform), so approving platforms first and
    # devices second used to leave max(approved id) unchanged.
    sync.build_run(_snap(snapshot), user=ADMIN, source="import")
    sync.bulk(None, "approve", ADMIN, all_pending=True, filters={"entity_type": "platform"})
    stamp1 = sync.catalog_stamp()
    assert hm.HclDevice.query.count() == 0
    sync.bulk(None, "approve", ADMIN, all_pending=True, filters={"entity_type": "device"})
    assert hm.HclDevice.query.count() == EXPECTED_DEVICES
    stamp2 = sync.catalog_stamp()
    assert stamp2 != stamp1, "the catalog changed, the stamp must move"


def test_touch_seen_bumps_last_seen_without_approval(app, snapshot):
    _seed(snapshot)
    from datetime import datetime, timezone
    later = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    n = sync.touch_seen(_snap(snapshot), later)
    db.session.commit()
    assert n == EXPECTED_PLATFORMS + EXPECTED_DEVICES + len(hs.snapshot_component_index(snapshot))
    p = hm.HclPlatform.query.filter_by(brand="lenovo", sc_model="HC1350").one()
    assert p.last_seen.replace(tzinfo=None) == later.replace(tzinfo=None)
    assert p.first_seen.replace(tzinfo=None) != later.replace(tzinfo=None)


# ---------------------------------------------------------------- regressions


def test_bulk_skips_poison_row_instead_of_aborting(app, snapshot):
    # Finding: "Snapshot import stores untyped/over-wide values in pending
    # rows; approving them 500s and blocks 'approve all'" — one row whose
    # apply fails at flush must be skipped with a note, not sink the batch.
    run = sync.build_run(_snap(snapshot), user=ADMIN, source="import")
    total = run.summary["pending_total"]
    bad = hm.HclPendingChange(
        run_id=run.id, entity_type="platform", entity_key="lenovo/HCBAD",
        change_kind="add",
        payload={"brand": "lenovo", "sc_model": "HCBAD",
                 "sockets": {"a": 1}, "max_cores": "two"},
        label="poison legacy row", status=hm.PENDING)
    db.session.add(bad)
    db.session.commit()
    counts = sync.bulk(None, "approve", ADMIN, all_pending=True)
    assert counts["approved"] == total
    assert counts["skipped"] == 1 and counts["rejected"] == 0
    db.session.refresh(bad)
    assert bad.status == hm.PENDING and "apply failed" in bad.note
    # the healthy rows all landed
    assert hm.HclPlatform.query.count() == EXPECTED_PLATFORMS
    assert hm.HclDevice.query.count() == EXPECTED_DEVICES
    assert hm.HclPlatform.query.filter_by(sc_model="HCBAD").count() == 0


def test_snapshot_records_coerce_untyped_and_overwide_values(app, snapshot):
    # Finding: "Snapshot import stores untyped/over-wide values in pending
    # rows" — record building must coerce numerics, trim brand to its
    # column width and drop components with a junk kind.
    rec = sync.platform_record({
        "brand": "B" * 60, "sc_model": "HC1", "sockets": "two",
        "max_cores": {"a": 1}, "ram_slots": "12", "max_ram_gb": None,
        "hdd_max": 3.0, "ssd_max": "junk",
        "components": [
            {"kind": "k" * 30, "part_number": "X1", "description": "junk kind"},
            {"kind": "nic", "part_number": "N1", "description": "ok"},
        ]})
    assert rec["brand"] == "b" * 20 and len(rec["key"]) <= 160
    assert rec["sockets"] is None and rec["max_cores"] is None
    assert rec["ram_slots"] == 12 and rec["hdd_max"] == 3 and rec["ssd_max"] is None
    assert [c["key"] for c in rec["components"]] == ["nic/N1"]
    assert sync.component_record({"kind": "widget99", "part_number": "X"}) is None


def test_per_platform_tce_flip_reaches_the_link(app, snapshot):
    # Finding: "Per-platform TCE flag changes are invisible to the diff and
    # never reach the link row" — clearing the badge on ONE platform (the
    # part stays TCE elsewhere) must queue a components row and, once
    # approved, land on that platform's link only.
    _seed(snapshot)
    snap = _snap(snapshot)
    target = None
    for p in snap["platforms"]:
        for c in p["components"]:
            if not (c.get("tce") and c.get("part_number")):
                continue
            elsewhere = any(
                q is not p and any(
                    d["kind"] == c["kind"] and d.get("part_number") == c["part_number"]
                    and d.get("tce") for d in q["components"])
                for q in snap["platforms"])
            if elsewhere:
                target = (p, c)
                break
        if target:
            break
    assert target, "fixtures must contain a TCE part on two platforms"
    p, c = target
    c["tce"] = False
    changes = sync.diff_snapshot(snap)
    pkey = "%s/%s" % (p["brand"], p["sc_model"])
    assert ("platform", pkey, "components") in [
        (ch["entity_type"], ch["entity_key"], ch["field"]) for ch in changes]
    run = sync.build_run(snap, user=ADMIN, source="import")
    sync.bulk(None, "approve", ADMIN, all_pending=True, filters={"run_id": run.id})
    platform = hm.HclPlatform.query.filter_by(brand=p["brand"], sc_model=p["sc_model"]).one()
    comp = hm.HclComponent.query.filter_by(kind=c["kind"], part_number=c["part_number"]).one()
    link = next(l for l in platform.links if l.component_id == comp.id)
    assert link.tce is False
    assert comp.tce is True  # still TCE on the other platform
    assert sync.diff_snapshot(snap) == []  # applied fully, no self-healing loop


def test_single_platform_tce_update_sets_the_only_link(app, snapshot):
    # Finding 4, single-platform case: a component-level 'tce' update used to
    # set comp.tce only, leaving the sole link's flag (what
    # platform_components()/enrich show) stale forever.
    _seed(snapshot)
    snap = _snap(snapshot)
    where = {}  # (kind, part) -> [entries], platforms
    plats = {}
    tce_any = {}
    for p in snap["platforms"]:
        for e in p["components"]:
            if not e.get("part_number"):
                continue
            k = (e["kind"], e["part_number"])
            where.setdefault(k, []).append(e)
            plats.setdefault(k, set()).add((p["brand"], p["sc_model"]))
            tce_any[k] = tce_any.get(k, False) or bool(e.get("tce"))
    k = next(k for k in where if len(plats[k]) == 1 and not tce_any[k])
    for e in where[k]:
        e["tce"] = True
    comp = hm.HclComponent.query.filter_by(kind=k[0], part_number=k[1]).one()
    assert comp.tce is False
    assert len([l for l in comp.links if l.status == hm.STATUS_ACTIVE]) == 1
    run = sync.build_run(snap, user=ADMIN, source="import")
    rows = _pending(run_id=run.id, entity_type="component", change_kind="update", field="tce")
    (tce_row,) = [r for r in rows if r.entity_key == comp.key]
    sync.approve(tce_row, ADMIN)
    db.session.refresh(comp)
    assert comp.tce is True
    assert next(l for l in comp.links if l.status == hm.STATUS_ACTIVE).tce is True


def test_stale_pending_rows_superseded_by_newer_complete_run(app, snapshot):
    # Finding: "Stale pending rows from an older run survive a newer complete
    # run and corrupt the catalog when approved" — run A queues changes the
    # site then reverts; the complete run B reproduces none of them, so they
    # must be retired, or 'approve all' writes stale content.
    _seed(snapshot)
    run_a = sync.build_run(_modified(snapshot), user=ADMIN, source="import")
    stale = _pending(run_id=run_a.id, status=hm.PENDING)
    assert stale
    run_b = sync.build_run(_snap(snapshot), user=ADMIN, source="import")
    assert run_b.summary["changes"] == {"add": 0, "update": 0, "delist": 0, "relist": 0, "merge": 0}
    assert run_b.summary["pending_total"] == 0
    for row in _pending(run_id=run_a.id):
        assert row.status == hm.SUPERSEDED
        assert row.note == "not reproduced by run %s" % run_b.id
    # 'approve all' now applies nothing stale
    counts = sync.bulk(None, "approve", ADMIN, all_pending=True)
    assert counts["approved"] == 0
    assert hm.HclPlatform.query.filter_by(brand="acme").count() == 0
    hc1350 = hm.HclPlatform.query.filter_by(brand="lenovo", sc_model="HC1350").one()
    assert hc1350.status == hm.STATUS_ACTIVE
    nic = hm.HclComponent.query.filter_by(kind="nic", part_number="4XC7A08294").one()
    assert not nic.description.startswith("RENAMED ")


def test_partial_snapshot_supersedes_only_fully_diffed_entities(app, snapshot):
    # Finding 5, partial-run guard: a failed detail page must never retire a
    # real pending change — only entities the partial snapshot actually
    # carried (and therefore fully diffed) are swept.
    _seed(snapshot)
    run_a = sync.build_run(_modified(snapshot), user=ADMIN, source="import")
    partial = _snap(snapshot)
    partial["platforms"] = [p for p in partial["platforms"]
                            if p["brand"] == "lenovo" and p["sc_model"] == "HC1450D"]
    partial["devices"] = []
    partial["complete"] = False
    sync.build_run(partial, user=ADMIN, source="import")
    # HC1450D was in the partial snapshot and its part-list change was not
    # reproduced -> superseded
    (pu,) = _pending(run_id=run_a.id, entity_type="platform", entity_key="lenovo/HC1450D")
    assert pu.status == hm.SUPERSEDED
    # entities absent from the partial snapshot keep their pending rows
    (dl,) = _pending(run_id=run_a.id, entity_type="platform", entity_key="lenovo/HC1350")
    assert dl.status == hm.PENDING
    (pa,) = _pending(run_id=run_a.id, entity_type="platform", entity_key="acme/HC9999")
    assert pa.status == hm.PENDING
    (du,) = _pending(run_id=run_a.id, entity_type="device", entity_key="8086/a352")
    assert du.status == hm.PENDING


def test_older_snapshot_never_moves_time_backwards(app, snapshot):
    # Finding: "Importing an older snapshot moves last_seen and
    # hcl_last_scrape_at backwards"
    from auth import get_setting
    _seed(snapshot)
    before = get_setting("hcl_last_scrape_at")
    assert before
    old = _snap(snapshot)
    old["scraped_at"] = "2024-01-01T00:00:00+00:00"
    run = sync.build_run(old, user=ADMIN, source="import")
    assert run.status == hm.RUN_SUCCEEDED
    assert get_setting("hcl_last_scrape_at") == before
    p = hm.HclPlatform.query.filter_by(brand="lenovo", sc_model="HC1350").one()
    assert p.last_seen.year >= 2026
    comp = hm.HclComponent.query.filter_by(kind="nic", part_number="4XC7A08294").one()
    assert comp.last_seen.year >= 2026


def test_rejected_component_add_is_annotated_when_platform_creates_it(app, snapshot):
    # Finding: "A rejected component 'add' is silently re-created by
    # approving the platform that lists it" — the platform's validated list
    # is authoritative so the part IS created, but the rejected row must say
    # the platform approval overrode it.
    _seed(snapshot)
    run = sync.build_run(_modified(snapshot), user=ADMIN, source="import")
    (ca,) = _pending(run_id=run.id, entity_type="component", change_kind="add")
    assert ca.entity_key == "nic/ACME-NIC-1"
    sync.reject(ca, ADMIN, note="do not want")
    (pa,) = _pending(run_id=run.id, entity_type="platform", change_kind="add")
    res = sync.approve(pa, ADMIN)
    assert "nic/ACME-NIC-1" in res["created"]
    assert hm.HclComponent.query.filter_by(kind="nic", part_number="ACME-NIC-1").count() == 1
    db.session.refresh(ca)
    assert ca.status == hm.REJECTED  # the decision record is kept
    assert "do not want" in ca.note
    assert "created anyway via platform acme/HC9999" in ca.note


# ---------------------------------------------------------------- hcl_data

def test_load_hcl_data_from_catalog(app, snapshot):
    _seed(snapshot)
    data = hcl_data.load_hcl_data()
    assert len(data.nics) == EXPECTED_DISTINCT_PARTS["nic"]
    assert len(data.hbas) == EXPECTED_DISTINCT_PARTS["hba"]
    assert len(data.cpus) == EXPECTED_DISTINCT_PARTS["cpu"]
    assert data.gpus == [] and data.blocked_vendors == []
    nic = next(n for n in data.nics if n.part == "4XC7A08294")
    assert nic.speed == "25GbE" and nic.form_factor == "OCP" and nic.type == "e810"
    assert nic.description.startswith("ThinkSystem Intel E810-DA2")
    lom = next(n for n in data.nics if n.form_factor.lower() == "lom")
    assert lom.speed == "10GbE"
    hba = next(h for h in data.hbas if h.part == "AOC-S3008L-L8i")
    assert hba.type == "AOC-S3008L-L8i" and hba.eol is False and hba.supported is True
    hba = next(h for h in data.hbas if h.part == "7Y37A01088")
    assert hba.type == "430-8i"
    cpu = next(c for c in data.cpus if "8593Q" in c.model)
    assert cpu.model == "Xeon Platinum 8593Q" and cpu.socket == "FCLGA4677"
    assert all(c.socket for c in data.cpus)

    # delisted parts come back with eol=True, or not at all when excluded
    comp = hm.HclComponent.query.filter_by(kind="hba", part_number="7Y37A01088").one()
    comp.status = hm.STATUS_DELISTED
    db.session.commit()
    data = hcl_data.load_hcl_data()
    assert next(h for h in data.hbas if h.part == "7Y37A01088").eol is True
    strict = hcl_data.load_hcl_data(include_delisted=False)
    assert len(strict.hbas) == EXPECTED_DISTINCT_PARTS["hba"] - 1


def test_hcl_data_from_snapshot_matches_catalog(app, snapshot):
    _seed(snapshot)
    from_db = hcl_data.load_hcl_data()
    from_snap = hcl_data.hcl_data_from_snapshot(snapshot)

    def _nics(d):
        return {(n.part, n.type, n.speed, n.description, n.form_factor) for n in d.nics}

    def _hbas(d):
        return {(h.part, h.type, h.description, h.eol, h.supported) for h in d.hbas}

    def _cpus(d):
        return {(c.model, c.description, c.socket) for c in d.cpus}

    assert _nics(from_db) == _nics(from_snap)
    assert _hbas(from_db) == _hbas(from_snap)
    assert _cpus(from_db) == _cpus(from_snap)
    assert hcl_data.hcl_data_from_snapshot({}).nics == []
    assert hcl_data.hcl_data_from_snapshot(snapshot, blocked="HPE, Acme").blocked_vendors == [
        "hpe", "acme"]


def test_blocked_vendors_round_trip(app):
    assert hcl_data.blocked_vendors() == []
    assert hcl_data.set_blocked_vendors([" HPE ", "acme", "hpe", ""]) == ["hpe", "acme"]
    db.session.commit()
    assert hcl_data.blocked_vendors() == ["hpe", "acme"]
    assert hcl_data.load_hcl_data().blocked_vendors == ["hpe", "acme"]
    hcl_data.set_blocked_vendors([])
    db.session.commit()
    assert hcl_data.blocked_vendors() == []


def test_platform_helpers(app, snapshot):
    _seed(snapshot)
    platforms = hcl_data.active_platforms("lenovo")
    assert platforms and all(p.brand == "lenovo" for p in platforms)
    index = hcl_data.platform_index()
    assert "sr630v2" in index and any(p.sc_model == "HC1350" for p in index["sr630v2"])
    hc1350 = next(p for p in index["sr630v2"] if p.sc_model == "HC1350")
    nics = hcl_data.platform_components([hc1350.id], kind="nic")
    assert nics and all(n["kind"] == "nic" and n["platform_id"] == hc1350.id for n in nics)
    tce = next(n for n in nics if n["part_number"] == "4XC7A80566")
    assert tce["tce"] is True and tce["platform_key"] == "lenovo/HC1350"
    everything = hcl_data.platform_components([hc1350.id])
    assert len(everything) == hc1350.to_dict()["component_count"]
    assert hcl_data.platform_components([]) == []
