"""Storage basis per datastore (import_service.adjust_summary with a datastore
list). Hand-computed cases; the browser's copy is held to the same answers by
tests/test_browser_exclusion_parity.py.

Rules:
  - shared datastores count unless excluded, local ones only when included
  - excluded shared datastores are listed in summary["excluded_datastores"]
  - a storage-excluded VM whose datastores are all out of the basis is not
    subtracted from datastore used a second time
  - a VM whose datastores are all out because of an excluded shared datastore
    leaves the VM storage totals as well
  - no datastore list (sizing saved before multi-file import): only the old
    include_local_storage toggle applies
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402

from import_checks import build_import_warnings  # noqa: E402
from import_service import GIB_PER_TB, adjust_summary, datastore_list  # noqa: E402

G = GIB_PER_TB
SOURCES = [{"id": "f1", "datastores": [
    {"name": "A", "type": "cluster", "capacity_gib": 2000, "used_gib": 1000, "mounts": ["h1", "h2"]},
    {"name": "B", "type": "cluster", "capacity_gib": 1000, "used_gib": 500, "mounts": ["h1", "h2"]},
    {"name": "L", "type": "local", "capacity_gib": 200, "used_gib": 100, "mounts": ["h1"]},
]}]


def _vm(name, ds, used):
    return {"name": name, "datastore": ds, "vdisk_used_gb": used, "vdisk_size_gb": used * 2,
            "vcpus": 2, "provisioned_memory_gb": 4, "consumed_memory_gb": 2,
            "powered_on": True, "is_template": False, "src": "f1"}


VMS = [_vm("v0", "A", 300), _vm("v1", "B", 200), _vm("v2", "A, L", 50),
       _vm("v3", "", 100), _vm("v4", "L", 40)]
VM_USED = 690.0
BASE = {"datastore_used_tb": round(1500 / G, 2), "datastore_total_tb": round(3000 / G, 2),
        "local_used_tb": round(100 / G, 2), "local_total_tb": round(200 / G, 2),
        "total_vm_used_storage_gb": VM_USED, "total_vm_provisioned_storage_gb": VM_USED * 2,
        "total_host_cores": 32}
DS = datastore_list(SOURCES)


def _adj(**kw):
    kw.setdefault("datastores", DS)
    return adjust_summary(BASE, VMS, **kw)


def _tb(gib):
    """TB as the summary shows it: built on the import's rounded totals, so
    within half a hundredth of the exact figure."""
    return pytest.approx(gib / G, abs=0.006)


def test_keys_are_stable_and_unique():
    dup = datastore_list([{"id": "f9", "datastores": [{"name": "x"}, {"name": "x"}, {"name": "y"}]}])
    assert [d["key"] for d in dup] == ["f9|x", "f9|x#2", "f9|y"]
    assert [d["key"] for d in DS] == ["f1|A", "f1|B", "f1|L"]
    assert datastore_list(None) == []


def test_default_basis_is_the_import():
    a = _adj()
    assert a["datastore_used_tb"] == BASE["datastore_used_tb"]
    assert a["datastore_total_tb"] == BASE["datastore_total_tb"]
    assert a["total_vm_used_storage_gb"] == VM_USED
    assert "excluded_datastores" not in a


def test_excluding_a_shared_datastore():
    a = _adj(exclude_datastores=["f1|A"])
    assert a["datastore_used_tb"] == _tb(500)
    assert a["datastore_total_tb"] == _tb(1000)
    assert a["excluded_datastores"] == [{"name": "A", "used_tb": round(1000 / G, 2)}]
    # v0 is only on A, v2 on A and the (left-out) local L: both leave the VM totals.
    assert a["total_vm_used_storage_gb"] == VM_USED - 300 - 50
    assert a["total_vm_provisioned_storage_gb"] == VM_USED * 2 - 600 - 100


def test_vm_on_excluded_datastore_is_not_subtracted_twice():
    a = _adj(exclude_datastores=["f1|A"], exclude_storage=[0])
    assert a["datastore_used_tb"] == _tb(500)
    assert a["total_vm_used_storage_gb"] == VM_USED - 300 - 50
    # On a datastore still in the basis, it is subtracted as before.
    b = _adj(exclude_storage=[1])
    assert b["datastore_used_tb"] == _tb(1500 - 200)


def test_local_datastores_out_by_default_and_included_on_request():
    # v4 sits on L only, which is out: excluding it changes no datastore total.
    assert _adj(exclude_storage=[4])["datastore_used_tb"] == _tb(1500)
    a = _adj(include_datastores=["f1|L"])
    assert a["datastore_used_tb"] == _tb(1600)
    # Built on the (rounded) import total, as the old local toggle was.
    assert a["datastore_total_tb"] == pytest.approx(BASE["datastore_total_tb"] + 200 / G, abs=0.006)
    assert _adj(include_datastores=["f1|L"], exclude_storage=[4])["datastore_used_tb"] == _tb(1560)
    # The old toggle means every local datastore.
    assert _adj(include_local_storage=True)["datastore_used_tb"] == _tb(1600)
    # Local datastores left out by default are not "excluded" by the user.
    assert "excluded_datastores" not in _adj()


def test_vm_without_or_with_unknown_datastore_counts_as_in_basis():
    vms = VMS + [_vm("v5", "elsewhere", 70)]
    a = adjust_summary(BASE, vms, exclude_storage=[3, 5], datastores=DS)
    assert a["datastore_used_tb"] == _tb(1500 - 100 - 70)


def test_without_a_datastore_list_the_old_toggle_applies():
    a = adjust_summary(BASE, VMS, include_local_storage=True)
    assert a["datastore_used_tb"] == round(BASE["datastore_used_tb"] + BASE["local_used_tb"], 2)
    assert adjust_summary(BASE, VMS, exclude_datastores=["f1|A"])["datastore_used_tb"] \
        == BASE["datastore_used_tb"]


@pytest.mark.parametrize("ds_used,vm_used,fires", [(775, 211, True), (40, 25, False),
                                                    (25, 10, True), (18, 11, False)])
def test_storage_gap_warning(ds_used, vm_used, fires):
    data = {"summary": {"datastore_used_tb": ds_used},
            "vms": [{"vdisk_used_gb": vm_used * G}],
            "datastores": [{"name": "big", "type": "cluster", "used_gib": ds_used * G * 0.8},
                           {"name": "small", "type": "cluster", "used_gib": ds_used * G * 0.2},
                           {"name": "huge-local", "type": "local", "used_gib": ds_used * G * 9}]}
    w = [x for x in build_import_warnings(data) if x["code"] == "storage_gap"]
    assert bool(w) is fires
    if fires:
        assert w[0]["params"]["name"] == "big"
        assert w[0]["params"]["ds_tb"] == ds_used and w[0]["params"]["vm_tb"] == vm_used
