"""Live Optics "Hypervisors"-sheet exports (GENERAL collector on hypervisor hosts).

The same workbook shape carries Hyper-V and ESXi hosts; shared datastores live
on the "Cluster Disks" sheet, one row per mounting host.
"""
import os
import sys

from openpyxl import Workbook

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from liveoptics import parse_liveoptics  # noqa: E402
from parser_common import GIB_PER_TB  # noqa: E402

HOST_COLS = ["Server Name", "Cluster", "OS", "Manufacturer", "Model", "CPU Sockets",
             "CPU Cores", "CPU Threads", "CPU Clock Speed (GHz)", "Net Clock Speed (GHz)",
             "Peak CPU Percentage", "Memory (KiB)", "Peak Memory Usage (KiB)",
             "Local Capacity (GiB)", "Free Capacity (GiB)", "Used Capacity (GiB)",
             "Guest VM Count"]
DISK_COLS = ["Server Name", "Device Name", "Capacity (GiB)", "Free Capacity (GiB)",
             "Used Capacity (GiB)"]
VM_COLS = ["Guest VM Name", "IsRunning", "Virtual CPU", "Provisioned Memory (Bytes)",
           "Guest VM Disk Used (MiB)", "Cluster", "Hypervisor"]


def _host(name, cluster, os_name):
    return [name, cluster, os_name, "Cisco", "HX240", 2, 32, 64, 2.6, 83.2, 70,
            1609208448, 475312768, 216, 195, 21, 2]


def _write(tmp_path, os_name, cluster_disks):
    wb = Workbook()
    wb.active.title = "Details"
    ws = wb.create_sheet("Hypervisors")
    ws.append(HOST_COLS)
    hosts = [("h1", "A"), ("h2", "A"), ("h3", "B")]
    for n, c in hosts:
        ws.append(_host(n, c, os_name))
    if cluster_disks:
        ws = wb.create_sheet("Cluster Disks")
        ws.append(DISK_COLS)
        for n, _ in hosts:  # shared by every host
            ws.append([n, "SHARED-01", 10240, 4096, 6144])
        for n in ("h1", "h2"):  # cluster A only
            ws.append([n, "A-DS", 2048, 1024, 1024])
    ws = wb.create_sheet("VMs")
    ws.append(VM_COLS)
    ws.append(["vm1", "TRUE", 4, 8 * 1024**3, 100 * 1024, "A", "h1"])
    ws.append(["vm2", "TRUE", 4, 8 * 1024**3, 100 * 1024, "B", "h3"])
    path = tmp_path / "lo.xlsx"
    wb.save(path)
    return str(path)


def test_esxi_hosts_with_cluster_disks(tmp_path):
    r = parse_liveoptics(_write(tmp_path, "VMware ESXi 7.0.3 build-24784741", True))
    s = r["summary"]
    assert s["current_platform"] == "VMware vSphere (3 hosts)"
    # Each shared datastore counted once, not once per mounting host.
    assert s["datastore_used_tb"] == round((6144 + 1024) / GIB_PER_TB, 2)
    # Host-local disks become excludable local storage.
    assert s["local_used_tb"] == round(3 * 21 / GIB_PER_TB, 2)
    shared = next(d for d in r["datastores"] if d["name"] == "SHARED-01")
    assert shared["mounts"] == ["h1", "h2", "h3"]
    by_name = {c["name"]: c["summary"] for c in r["clusters"]}
    assert set(by_name) == {"A", "B"}
    # A-DS is A's alone; SHARED-01 is split across both clusters.
    assert by_name["B"]["datastore_used_tb"] == round(3072 / GIB_PER_TB, 2)


def test_hyperv_without_cluster_disks_keeps_local_as_pool(tmp_path):
    r = parse_liveoptics(_write(tmp_path, "Microsoft Windows Server 2019 Datacenter", False))
    s = r["summary"]
    assert s["current_platform"] == "Hyper-V (3 hosts)"
    assert s["local_total_tb"] == 0
    assert s["datastore_used_tb"] == round(3 * 21 / GIB_PER_TB, 2)
