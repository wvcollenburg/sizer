"""Nutanix Collector (vCenter mode) import — issue #32.

The collector reuses RVTools sheet names (vInfo, vCPU, ...) with different
columns, so it used to be detected as RVTools and every VM came back with
0 vCPU / 0 RAM / powered off. The workbook here is a trimmed copy of the real
export's layout (same sheet names and headers, a handful of rows).

Run: .venv/bin/python -m pytest tests/test_nutanix_collector.py -q
"""
import io
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.pop("SESSION_COOKIE_SECURE", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402
from openpyxl import Workbook  # noqa: E402

import app as appmod  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from nutanix_collector import parse_nutanix_collector  # noqa: E402

CL = "NTNX-Cluster-1"

SHEETS = {
    "vHosts": (
        ["Cluster", "Cluster Name", "MOID", "Host IP", "Host Name", "Model", "Service Tag",
         "NICs", "BIOS", "Vendor", "CPUs", "VMs", "CPU Model", "CPU Cores", "CPU Speed",
         "Cores per CPU", "Memory Size", "CPU Usage", "Memory Usage",
         "95th Percentile Disk Throughput (KBps)", "95th Percentile IOPS",
         "95th Percentile Number of Reads", "95th Percentile Number of Writes",
         "Capacity (MiB)", "Consumed (MiB)", "Free Space (MiB)", "GPU Count", "GPU Name",
         "GPU Vendor", "GPU Memory Size (MiB)", "Hypervisor", "Maintenance Mode"],
        [["domain-c1", CL, "host-1", "ip1", "H1", "ProLiant DL380 Gen10", "st1", 4, "U30",
          "HPE", 2, 2, "Intel(R) Xeon(R) Silver 4210R CPU @ 2.40GHz", 20, 2400, 10,
          511.661, 50.0, 80.0, 0, 9, 9, 0, 1, 1, 0, None, None, None, None,
          "VMware ESXi 7.0.3", "False"],
         ["domain-c1", CL, "host-2", "ip2", "H2", "ProLiant DL380 Gen10", "st2", 4, "U30",
          "HPE", 2, 1, "Intel(R) Xeon(R) Silver 4210R CPU @ 2.40GHz", 20, 2400, 10,
          511.661, 20.0, 40.0, 0, 0, 0, 0, 1, 1, 0, None, None, None, None,
          "VMware ESXi 7.0.3", "False"]],
    ),
    "Datastore": (
        ["Datastore ID", "Datastore Name", "URL", "Accessible", "Datastore Type",
         "Consumed (MiB)", "Capacity (MiB)", "Freespace (MiB)", "Consumed ISO (MiB)",
         "VM Count", "Host Count", "Host Names", "Host IDs", "DC Name", "Cluster Name"],
        [["ds-1", "shared", "u1", "True", "VMFS", 512 * 1024, 1024 * 1024, 512 * 1024, 0,
          3, 2, "H1,H2", "host-1,host-2", "DC", CL],
         ["ds-2", "local-h1", "u2", "True", "VMFS", 1024, 100 * 1024, 99 * 1024, 0,
          0, 1, "H1", "host-1", "DC", CL],
         ["ds-3", "gone", "u3", "False", "NFS", 9999, 99999, 0, 0,
          0, 2, "H1,H2", "host-1,host-2", "DC", CL]],
    ),
    "vInfo": (
        ["VM Name", "Sizing Enabled", "Power State", "95th Percentile IOPS", "Cluster Name",
         "Host Name", "MOID"],
        [["vm-a", "True", "poweredOn", 100.5, CL, "H1", "vm-1"],
         ["vm-b", "True", "poweredOn", 20, CL, "H2", "vm-2"],
         ["vm-c", "True", "poweredOff", 999, CL, "H1", "vm-3"]],
    ),
    "vMemory": (
        ["VM Name", "Power State", "Size (MiB)", "Peak %", "Average %", "Median %",
         "Custom Percentile %", "95th Percentile % (recommended)"],
        [["vm-a", "poweredOn", 16384, 10, 5, 5, None, 50.0],
         ["vm-b", "poweredOn", 8192, 10, 5, 5, None, 25.0],
         ["vm-c", "poweredOff", 4096, 0, 0, 0, None, 0]],
    ),
    "vPartition": (
        ["VM Name", "Power State", "Path", "Consumed (MiB)", "Capacity (MiB)"],
        [["vm-a", "poweredOn", "C:\\", 10240, 51200],
         ["vm-b", "poweredOn", "/", 2048, 20480]],
    ),
    "vNICs": (
        ["Host", "Network Device", "Drivers", "Speed (Mbps)", "Duplex", "MAC Address",
         "Switch", "PCI", "Adapter"],
        [["H1", "vmnic0", "i40en", 10000, "True", "m", None, "0", "Intel X710"]],
    ),
    "vmList": (
        ["VM Name", "Sizing Enabled", "Power State", "vCPUs", "Max CPU (MHz)", "Memory (MiB)",
         "Thin Provisioned", "Capacity (MiB)", "Consumed (MiB)", "Datastore",
         "Connection state", "Target Cluster", "Guest OS", "Template", "Host",
         "Cluster Name", "Datacenter Name"],
        [["vm-a", "True", "poweredOn", 8, 0, 16384, "False", 102400, 40960, "shared",
          "connected", "Cluster-1", "Microsoft Windows Server 2019 (64-bit)", "False",
          "H1", CL, "DC"],
         # Blank datastore consumption on a thin disk: fall back to the guest's view.
         ["vm-b", "True", "poweredOn", 4, 0, 8192, "True", 51200, None, "shared",
          "connected", "Cluster-1", None, "False", "H2", CL, "DC"],
         ["vm-c", "True", "poweredOff", 2, 0, 4096, "False", 10240, None, "shared",
          "connected", "Cluster-1", None, "False", "H1", CL, "DC"]],
    ),
    "Metadata": (
        ["Hypervisor", "Connection Mode", "Port", "Platform", "Collector Version",
         "CLI Extract", "Collection Date & Time", "Performance Data Duration",
         "Is Anonymized"],
        [["vCenter", None, "443", "win32", "7.1.1", "False", "2026-09-08 10:55:02 UTC",
          "7 Days", "True"]],
    ),
}


def _workbook_bytes():
    wb = Workbook()
    wb.remove(wb.active)
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
    p = tmp_path / "Nutanix Collector.xlsx"
    p.write_bytes(_workbook_bytes())
    return str(p)


def test_detected_as_nutanix_not_rvtools(xlsx_path):
    assert appmod._detect_file_type(xlsx_path) == "nutanix"


def test_vms_are_read(xlsx_path):
    d = parse_nutanix_collector(xlsx_path)
    s = d["summary"]
    assert s["source"] == "nutanix"
    assert s["total_vms"] == 3 and s["active_vms"] == 2
    assert s["total_vcpus"] == 12
    assert s["total_vm_provisioned_memory_gb"] == 24.0
    # 95th percentile memory: 16 GiB x 50% + 8 GiB x 25%
    assert s["total_vm_used_memory_gb"] == 10.0
    vm_b = next(v for v in d["vms"] if v["name"] == "vm-b")
    assert vm_b["vdisk_size_gb"] == 50.0 and vm_b["vdisk_used_gb"] == 2.0
    assert s["guest_licensing"] == "windows"


def test_hosts_perf_and_storage(xlsx_path):
    s = parse_nutanix_collector(xlsx_path)["summary"]
    assert s["host_count"] == 2 and s["total_host_cores"] == 40
    assert s["current_platform"] == "HPE ProLiant DL380 Gen10"
    assert s["peak_cpu_pct"] == 50.0 and s["peak_cpu_ghz"] > 0
    # Powered-on VMs' p95 IOPS; the powered-off VM's 999 is ignored.
    assert s["p95_iops"] == 120
    # Shared datastore is cluster storage, single-host one is local, the
    # inaccessible one is dropped.
    assert s["datastore_total_tb"] == 1.0
    assert s["local_total_tb"] == 0.1
    assert s["nic_speed_mbps"] == 10000


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
                        data={"file": (fh, "Nutanix Collector.xlsx")},
                        content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    j = r.get_json()
    assert j["source"] == "nutanix"
    assert j["vm_count"] == 3 and j["active_vm_count"] == 2
    assert "no_iops" not in {w["code"] for w in j["import_warnings"]}
