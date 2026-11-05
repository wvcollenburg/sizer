"""import_service: the assessment pipeline out of the route, and the VM
exclusion maths server-side (docs/mcp/01-tool-design.md §2.7, §7).

Locks in:
  - /api/import-liveoptics returns what import_service.parse_assessment
    produces (parity), and its refusals keep their replies
  - adjust_summary with no exclusions reproduces the parsed summary, and with
    exclusions/edits/added VMs does the browser's arithmetic, rounding like
    Math.round (half up), not like Python's round (half to even)
  - suggested_exclusion flags the same names the exclusion modal flags
  - cluster_dataset cuts one source cluster the way cluster_split does
  - neither module is fingerprinted

The JS-vs-Python parity on a live page is tests/test_browser_exclusions.py
(needs headless Chrome). Run: .venv/bin/python -m pytest tests/test_import_service.py -q
"""
import io
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.pop("SESSION_COOKIE_SECURE", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.dirname(__file__))

import pytest  # noqa: E402
from openpyxl import Workbook  # noqa: E402

import app as appmod  # noqa: E402
import import_service as svc  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from fingerprint import ENGINE_MODULES, PARSER_MODULES  # noqa: E402
from merge_fixtures import host, liveoptics, rvtools, vm  # noqa: E402
from test_sizing_service import _seed_catalog  # noqa: E402

HOSTS = [host("esx1", "Prod"), host("esx2", "Prod")]            # 32 cores
VMS = [vm("web01", "esx1", "Prod", vcpus=4, ram_gb=16, used_gb=100),
       vm("db01", "esx2", "Prod", vcpus=8, ram_gb=32, used_gb=200),
       vm("old01", "esx2", "Prod", vcpus=2, ram_gb=8, used_gb=50, on=False)]
DATASTORES = [("ds1", 2048, 1024)]                               # 1 TiB = 1.1 TB used


@pytest.fixture()
def client():
    app = appmod.app
    app.config["TESTING"] = True
    app.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    with app.app_context():
        db.drop_all()
        db.create_all()
        _seed_catalog()
    c = app.test_client()
    c.post("/api/auth/signup", json={"email": "alice@examplecorp.com",
                                     "password": "Abcdef1!xy",
                                     "accept_privacy": True})
    return c


def _post(client, path, raw=True):
    with open(path, "rb") as fh:
        body = fh.read()
    url = "/api/import-liveoptics" + ("?raw=1" if raw else "")
    return client.post(url, data={"file": (io.BytesIO(body), os.path.basename(path))},
                       content_type="multipart/form-data")


# ── pipeline parity ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("builder, file_type", [(rvtools, "rvtools"),
                                                (liveoptics, "liveoptics")])
def test_route_returns_what_the_service_parses(client, tmp_path, builder, file_type):
    stores = DATASTORES if builder is rvtools else [("ds1", 2048, 1024, ["esx1", "esx2"])]
    path = builder(str(tmp_path / "est.xlsx"), HOSTS, VMS, stores)
    res = _post(client, path)
    assert res.status_code == 200, res.get_json()
    via_route = res.get_json()
    data = svc.parse_assessment(path, "est.xlsx")
    assert data["file_type"] == file_type == via_route["file_type"]
    for key in ("summary", "hosts", "vms", "datastores", "host_performance",
                "host_nics", "clusters", "import_warnings"):
        assert via_route[key] == data[key], key
    meta_route, meta_svc = dict(via_route["source_meta"]), dict(data["source_meta"])
    meta_route.pop("imported_at"), meta_svc.pop("imported_at")
    assert meta_route == meta_svc
    assert meta_svc["file_name"] == "est.xlsx" and meta_svc["vm_count"] == 3


def test_full_import_sizes_through_sizing_service(client, tmp_path):
    path = rvtools(str(tmp_path / "est.xlsx"), HOSTS, VMS, DATASTORES)
    res = _post(client, path, raw=False)
    assert res.status_code == 200, res.get_json()
    body = res.get_json()
    assert body["recommendations"] and body["active_vm_count"] == 2
    # The input caveats ride on the engine warnings (sizing_service), and the
    # fixture VMs have a largest VM, so none is expected.
    assert not any(w.startswith("Largest VM") for w in body["warnings"])


def test_unrecognised_workbook_keeps_its_reply(client, tmp_path):
    path = str(tmp_path / "other.xlsx")
    wb = Workbook()
    wb.active.title = "Sheet1"
    wb.save(path)
    res = _post(client, path)
    assert res.status_code == 400
    assert res.get_json() == {"error": svc.UNRECOGNISED_MESSAGE}
    with pytest.raises(svc.AssessmentError) as exc:
        svc.parse_assessment(path, "other.xlsx")
    assert exc.value.status == 400 and exc.value.code is None
    assert svc.detect_file_type(path) is None


def test_incomplete_export_is_a_422_with_its_code(client, tmp_path, monkeypatch):
    monkeypatch.setattr(svc, "import_rejection",
                        lambda data, ft: {"code": "lo_incomplete",
                                          "params": {"covered": 1, "vms": 3}})
    path = rvtools(str(tmp_path / "est.xlsx"), HOSTS, VMS, DATASTORES)
    res = _post(client, path)
    assert res.status_code == 422
    body = res.get_json()
    assert body["error_code"] == "lo_incomplete"
    assert body["error_params"] == {"covered": 1, "vms": 3}
    assert "incomplete" in body["error"]


def test_parser_crash_keeps_its_reply(client, tmp_path, monkeypatch):
    def boom(path):
        raise ValueError("bad cell")
    monkeypatch.setitem(svc.PARSERS, "rvtools", boom)
    path = rvtools(str(tmp_path / "est.xlsx"), HOSTS, VMS, DATASTORES)
    res = _post(client, path)
    assert res.status_code == 400
    assert res.get_json() == {"error": svc.PARSE_FAILED_MESSAGE}


def test_not_fingerprinted():
    assert "import_service.py" not in ENGINE_MODULES
    assert "import_service.py" not in PARSER_MODULES


# ── adjust_summary ───────────────────────────────────────────────────────────

@pytest.fixture()
def parsed(tmp_path):
    return svc.parse_assessment(
        rvtools(str(tmp_path / "est.xlsx"), HOSTS, VMS, DATASTORES), "est.xlsx")


def test_no_exclusions_reproduces_the_import(parsed):
    base, vms = parsed["summary"], parsed["vms"]
    assert svc.adjust_summary(base, vms) == base
    assert base["total_vcpus"] == 12 and base["active_vms"] == 2
    assert base["datastore_used_tb"] == 1.1                 # 1024 GiB in decimal TB


def test_compute_exclusion_rebuilds_totals_and_largest_vm(parsed):
    out = svc.adjust_summary(parsed["summary"], parsed["vms"], exclude_compute=[1])
    assert out["total_vcpus"] == 4
    assert out["total_vm_provisioned_memory_gb"] == 16
    assert out["total_vm_used_memory_gb"] == 8          # consumed = ram/2
    assert out["active_vms"] == 1
    assert out["max_vm_cores"] == 4 and out["max_vm_ram_gb"] == 16
    # 4 / 32 = 0.125: Math.round gives 0.13 where Python's round gives 0.12.
    assert out["vcpu_per_core_ratio"] == 0.13
    # Storage untouched by a compute-only exclusion.
    assert out["datastore_used_tb"] == parsed["summary"]["datastore_used_tb"]


def test_storage_exclusion_subtracts_measured_disk(parsed):
    base = parsed["summary"]
    assert base["total_vm_provisioned_storage_gb"] == 600   # active: 200 + 400
    assert base["total_vm_used_storage_gb"] == 300
    out = svc.adjust_summary(base, parsed["vms"], exclude_storage=[1])
    assert out["datastore_used_tb"] == 0.89                 # 1.1 - 200 GiB (0.21 TB)
    assert out["total_vm_provisioned_storage_gb"] == 200
    assert out["total_vm_provisioned_storage_tb"] == 0.21
    assert out["total_vm_used_storage_gb"] == 100
    assert out["total_vm_used_storage_tb"] == 0.11
    # A powered-off VM's disk still comes off the datastore total, but not
    # off the active-VM provisioned/used figures.
    out = svc.adjust_summary(base, parsed["vms"], exclude_storage=[2])
    assert out["datastore_used_tb"] == 1.05
    assert out["total_vm_provisioned_storage_gb"] == 600


def test_edits_added_and_removed_vms(parsed):
    base, vms = parsed["summary"], list(parsed["vms"])
    out = svc.adjust_summary(base, vms, vm_config={"0": {"vcpus": 6}})
    assert out["total_vcpus"] == 14 and out["max_vm_cores"] == 8
    new = dict(vms[0], name="new01", vcpus=2, provisioned_memory_gb=4,
               consumed_memory_gb=0, vdisk_used_gb=50, vdisk_size_gb=50)
    out = svc.adjust_summary(base, vms + [new], added=[3])
    assert out["total_vcpus"] == 14
    assert out["datastore_used_tb"] == 1.15                 # 1.1 + 50 GiB (0.05 TB)
    assert out["total_vm_provisioned_storage_gb"] == 650
    assert out["total_vm_used_storage_gb"] == 350
    # Removed again: the UI excludes it from compute and marks it removed.
    out = svc.adjust_summary(base, vms + [new], added=[3], removed=[3],
                             exclude_compute=[3], exclude_storage=[3])
    assert out["total_vcpus"] == 12 and out["datastore_used_tb"] == 1.1


def test_local_storage_and_floor():
    base = {"datastore_used_tb": 1.0, "datastore_total_tb": 2.0,
            "local_used_tb": 0.5, "local_total_tb": 1.0, "total_host_cores": 0,
            "total_vm_provisioned_storage_gb": 0, "total_vm_used_storage_gb": 0}
    out = svc.adjust_summary(base, [], include_local_storage=True)
    assert out["datastore_used_tb"] == 1.5 and out["datastore_total_tb"] == 3.0
    big = dict(VMS[0], **{"powered_on": True, "is_template": False,
                          "vdisk_used_gb": 4096, "vdisk_size_gb": 4096,
                          "vcpus": 4, "provisioned_memory_gb": 16,
                          "consumed_memory_gb": 8})
    out = svc.adjust_summary(base, [big], exclude_storage=[0])
    assert out["datastore_used_tb"] == 0


def test_rounding_is_half_up_like_the_browser():
    assert svc._js_round(2.25, 1) == 2.3 and round(2.25, 1) == 2.2
    assert svc._js_round(0.125, 2) == 0.13
    assert svc._js_round(-2.5, 0) == -2       # Math.round(-2.5) === -2


# ── suggested exclusions and clusters ────────────────────────────────────────

@pytest.mark.parametrize("name, flag", [
    ("NTNX-CVM-01", "cvm"), ("stCtlVM-1", "cvm"), ("witness-vm", "cvm"),
    ("HPE SimpliVity OVC", "cvm"), ("vsa-02", "cvm"),
    ("vCLS-3f2a", "vmware_infrastructure"), ("VCSA01", "vmware_infrastructure"),
    ("prod-vcenter", "vmware_infrastructure"),
    ("web01", None), ("sql-cluster-vm", None), ("", None), (None, None),
])
def test_suggested_exclusion(name, flag):
    assert svc.suggested_exclusion(name) == flag


def test_cluster_dataset_cuts_one_source_cluster(tmp_path):
    hosts = [host("a1", "ClusterA"), host("b1", "ClusterB")]
    vms = [vm("va", "a1", "ClusterA", used_gb=100), vm("vb", "b1", "ClusterB", used_gb=300)]
    path = liveoptics(str(tmp_path / "two.xlsx"), hosts, vms,
                      [("dsA", 1024, 100, ["a1"]), ("dsB", 1024, 300, ["b1"])])
    data = svc.parse_assessment(path, "two.xlsx")
    assert [c["name"] for c in data["clusters"]] == ["ClusterA", "ClusterB"]
    sub = svc.cluster_dataset(data, "ClusterB")
    assert [h["name"] for h in sub["hosts"]] == ["b1"]
    assert [v["name"] for v in sub["vms"]] == ["vb"]
    assert svc.cluster_dataset(data, "ClusterC") is None
    single = svc.parse_assessment(
        rvtools(str(tmp_path / "one.xlsx"), HOSTS, VMS, DATASTORES), "one.xlsx")
    assert svc.cluster_dataset(single, "Prod") is None
