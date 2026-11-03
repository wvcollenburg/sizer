"""Live Optics Azure collector import.

The Azure export carries the Live Optics "Details" sheet, so it used to be
detected as a vSphere Live Optics export and came back empty. It is a cloud
inventory: no hosts, no performance data, only VM sizes and managed disks. The
workbook here is a trimmed copy of the real export's layout (same sheet names
and headers, a handful of rows).

Run: .venv/bin/python -m pytest tests/test_lo_azure_collector.py -q
"""
import datetime
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
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from import_service import detect_file_type, parse_assessment  # noqa: E402
from lo_azure_collector import parse_lo_azure  # noqa: E402
from sizing_service import run_recommendation  # noqa: E402
from test_mcp_golden import _seed_guide_node  # noqa: E402

SUB = "/subscriptions/s/resourceGroups/RG/providers/Microsoft.Compute"


def _vm_id(name):
    return f"{SUB}/virtualMachines/{name}"


VM_HEADER = ["VM Name", "ID", "vmId", "VM State", "VM Size", "CPU Cores", "Memory (MiB)",
             "OS Disk Size (MiB)", "Resource Disk Size (MiB)", "Max Data Disk Count",
             "Total Data Disks Size (GiB)", "Image URN", "Location"]
DISK_HEADER = ["VM Name", "VM ID", "Location", "VM Status", "VM Size", "Disk Name",
               "Disk ID", "Disk Type", "Disk Size (GiB)", "Disk IOPS Cap",
               "Disk Throughput Cap (MB/s)", "SKU", "Tier", "Disk State", "Encryption Type"]

SHEETS = {
    "Azure VMs": (VM_HEADER, [
        ["app-01", _vm_id("app-01"), "U1", "VM running", "Standard_D8s_v3", 8, 32768,
         1047552, 65536, 16, 600,
         "MicrosoftWindowsServer:WindowsServer:2019-Datacenter:17763.1", "uksouth"],
        ["db-01", _vm_id("db-01"), "U2", "VM running", "Standard_E16as_v4", 16, 131072,
         1047552, 262144, 32, 0, "RedHat:RHEL:8_10:8.10.1", "uksouth"],
        ["old-01", _vm_id("old-01"), "U3", "VM deallocated", "Standard_D8s_v3", 8, 32768,
         1047552, 65536, 16, 0, ":::1.0.0", "uksouth"],
        # GPU partition size: Azure reports 0 cores / 0 MiB.
        ["gpu-01", _vm_id("gpu-01"), "U4", "VM running", "Standard_NV4as_v4", 0, 0,
         0, 0, 8, 0, "Canonical:UbuntuServer:18.04-LTS:1", "westeurope"],
    ]),
    "Scale Set VMs": (
        ["VM Name", "InstanceId", "ID", "vmId", "SKU", "OS Disk Size (GiB)",
         "Total Data Disks Size (GiB)", "Image URN", "Location", "Scale Set Name"],
        [["aks-pool_1", 1, f"{SUB}/virtualMachineScaleSets/aks-pool/virtualMachines/1",
          "U5", "Standard_D16ds_v5", 128, 20, ":::2023.01.10", "uksouth", "aks-pool"],
         ["aks-pool_2", 2, f"{SUB}/virtualMachineScaleSets/aks-pool/virtualMachines/2",
          "U6", "Standard_D8s_v3", 128, 0, ":::2023.01.10", "uksouth", "aks-pool"]],
    ),
    "Azure Disks": (DISK_HEADER, [
        ["app-01", _vm_id("app-01"), "uksouth", "VM running", "Standard_D8s_v3", "app-os",
         "d1", "OS Disk", 128, 500, 100, "Premium_LRS", "P10", "Attached", "x"],
        ["app-01", _vm_id("app-01"), "uksouth", "VM running", "Standard_D8s_v3", "app-data",
         "d2", "Data Disk", 600, 5000, 200, "Premium_LRS", "P30", "Attached", "x"],
        ["db-01", _vm_id("db-01"), "uksouth", "VM running", "Standard_E16as_v4", "db-os",
         "d3", "OS Disk", 512, 2300, 150, "Premium_LRS", "P20", "Attached", "x"],
        ["old-01", _vm_id("old-01"), "uksouth", "VM deallocated", "Standard_D8s_v3",
         "old-os", "d4", "OS Disk", 30, 120, 25, "Premium_LRS", "P4", "Reserved", "x"],
        ["gpu-01", _vm_id("gpu-01"), "westeurope", "VM running", "Standard_NV4as_v4",
         "gpu-os", "d5", "OS Disk", 64, 240, 50, "Premium_LRS", "P6", "Attached", "x"],
    ]),
    "Unattached Disks": (
        ["Disk Name", "Disk ID", "Location", "Disk Size (GiB)", "Disk State"],
        [["app-os-ASRReplica", "d9", "ukwest", 4096, "Unattached"]],
    ),
    "Load Balancers": (
        ["VM Name", "VM ID", "VM State", "Load Balancer Name"],
        [["aks-pool_2", f"{SUB}/virtualMachineScaleSets/aks-pool/virtualMachines/2",
          "VM deallocated", "kubernetes"]],
    ),
}


def _workbook_bytes():
    wb = Workbook()
    ws = wb.active
    ws.title = "Details"
    for row in ([None, None], [None, None], [None, None],
                ["Date", datetime.datetime(2026, 10, 30, 10, 45)],
                ["Collector Build Version", "1.0.0.0"]):
        ws.append(row)
    for name, (header, rows) in SHEETS.items():
        ws = wb.create_sheet(name)
        ws.append(header)
        for r in rows:
            ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture()
def xlsx_path(tmp_path):
    p = tmp_path / "Live Optics Azure_2026_10_30_10_44_56.xlsx"
    p.write_bytes(_workbook_bytes())
    return str(p)


def _vm(d, name):
    return next(v for v in d["vms"] if v["name"] == name)


def test_detected_as_azure_not_liveoptics(xlsx_path):
    assert detect_file_type(xlsx_path) == "azure"


def test_vms_are_read(xlsx_path):
    d = parse_lo_azure(xlsx_path)
    assert d["hosts"] == [] and d["scan_type"] == "azure"
    app = _vm(d, "app-01")
    assert app["vcpus"] == 8 and app["provisioned_memory_gb"] == 32.0
    assert app["powered_on"] and app["uuid"] == "u1"
    assert app["os"] == "MicrosoftWindowsServer WindowsServer 2019-Datacenter"
    # Managed disks, not the VM sheet's 1023 GiB "OS Disk Size" placeholder;
    # the temporary resource disk is not counted.
    assert app["vdisk_size_gb"] == app["vdisk_used_gb"] == 728.0
    old = _vm(d, "old-01")
    assert not old["powered_on"] and old["os"] == ""


def test_sizes_the_export_leaves_blank(xlsx_path):
    d = parse_lo_azure(xlsx_path)
    gpu = _vm(d, "gpu-01")
    assert (gpu["vcpus"], gpu["provisioned_memory_gb"]) == (4, 14)
    # Scale-set SKU seen on a VM row: that row's figures.
    assert _vm(d, "aks-pool_2")["provisioned_memory_gb"] == 32.0
    # Unseen SKU: read from its name, D family at 4 GiB per vCPU.
    ss = _vm(d, "aks-pool_1")
    assert (ss["vcpus"], ss["provisioned_memory_gb"]) == (16, 64)
    assert ss["scale_set"] == "aks-pool" and ss["vdisk_size_gb"] == 148.0
    # Power state from the Load Balancers sheet, else running.
    assert ss["powered_on"] and not _vm(d, "aks-pool_2")["powered_on"]


def test_summary(xlsx_path):
    d = parse_lo_azure(xlsx_path)
    s = d["summary"]
    assert s["source"] == "azure" and s["scan_type"] == "azure"
    assert s["total_vms"] == 6 and s["active_vms"] == 4
    assert s["total_vcpus"] == 8 + 16 + 4 + 16
    assert s["total_vm_provisioned_memory_gb"] == 32 + 128 + 14 + 64
    assert s["vcpu_per_core_ratio"] == 3.0 and s["vcpu_ratio_assumed"]
    assert s["guest_licensing"] == "windows"
    assert s["current_platform"] == "Microsoft Azure (uksouth, westeurope)"
    # Every VM's disks (deallocated ones too), one pool per region; the
    # unattached ASR replica is not workload.
    pools = {p["name"]: p["used_gib"] for p in d["datastores"]}
    assert pools == {"Azure managed disks (uksouth)": 728 + 512 + 30 + 148 + 128,
                     "Azure managed disks (westeurope)": 64}
    assert s["local_total_tb"] == 0


def test_regions_split_into_clusters(xlsx_path):
    d = parse_lo_azure(xlsx_path)
    by_name = {c["name"]: c["summary"] for c in d["clusters"]}
    assert set(by_name) == {"uksouth", "westeurope"}
    west = by_name["westeurope"]
    assert west["total_vcpus"] == 4
    # Datastores carry no host mounts, so cluster_split spreads them by each
    # region's running-VM storage: the per-region totals still add up.
    assert sum(c["datastore_used_tb"] for c in by_name.values()) == pytest.approx(
        d["summary"]["datastore_used_tb"], abs=0.02)
    assert west["current_platform"] == "Microsoft Azure (westeurope)"
    assert all(c["vcpu_ratio_assumed"] and c["vcpu_per_core_ratio"] == 3.0
               for c in by_name.values())


def test_import_pipeline_and_sizing(xlsx_path):
    app = appmod.app
    app.config["TESTING"] = True
    limiter.enabled = False
    d = parse_assessment(xlsx_path, "azure.xlsx")
    assert d["file_type"] == "azure"
    assert d["source_meta"]["collected_at"] == "2026-10-30"
    codes = {w["code"] for w in d["import_warnings"]}
    assert {"no_perf", "no_iops", "assumed_ratio"} <= codes
    with app.app_context():
        db.drop_all()
        db.create_all()
        _seed_guide_node()
        result = run_recommendation(d["summary"])
    assert result["recommendations"], result["warnings"]


def test_import_route_accepts_it(xlsx_path):
    app = appmod.app
    app.config["TESTING"] = True
    limiter.enabled = False
    with app.app_context():
        db.drop_all()
        db.create_all()
    client = app.test_client()
    client.post("/api/auth/signup", json={
        "email": "alice@examplecorp.com", "password": "Abcdef1!xy", "accept_privacy": True})
    with open(xlsx_path, "rb") as fh:
        r = client.post("/api/import-liveoptics",
                        data={"file": (fh, "Live Optics Azure.xlsx")},
                        content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    j = r.get_json()
    assert j["source"] == "azure"
    assert j["vm_count"] == 6 and j["active_vm_count"] == 4
