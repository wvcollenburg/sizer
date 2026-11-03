import re

from openpyxl import load_workbook
from xlsx_utils import (sheet_rows as _sheet_rows, to_float as _float,
                        to_int as _int)
from parser_common import build_summary as _build_summary, vm_identity
from cluster_split import cluster_summaries as _cluster_summaries


# Live Optics Azure collector export. It carries the Live Optics "Details"
# sheet, so it must be detected before the regular Live Optics check — it
# otherwise parses as a vSphere export with no hosts, VMs or datastores.
#
# A cloud inventory, not an assessment: there are no hosts and no performance
# data, only the configured size of every VM and its managed disks. So:
#   * every Azure VM (and scale-set instance) is a VM sized on its SKU: vCPUs
#     and memory as provisioned, "used" = provisioned (nothing measured);
#   * storage is the provisioned size of the attached managed disks — Azure
#     reports no consumed figure, and never under-sizing beats guessing. The
#     temporary resource disk is ephemeral and not counted;
#   * the region is the source "cluster", so a multi-region estate splits
#     into one site per region like a multi-cluster vSphere export (with no
#     hosts to mount them, cluster_split shares the regional disk pools out
#     by each region's running-VM storage, as it does for RVTools);
#   * with no physical cores there is no overcommit to measure, so the vCPU
#     ratio falls back to the assumed 3:1 of the GENERAL server path.
# Unattached disks (ASR replicas, orphans) belong to no VM and are left out.

def is_lo_azure(sheetnames):
    return "Azure VMs" in sheetnames and "Azure Disks" in sheetnames


def parse_lo_azure(file_path):
    wb = load_workbook(file_path, read_only=True, data_only=True)
    vms = _parse_vms(wb)
    result = {
        "project": _parse_details(wb),
        "scan_type": "azure",
        "hosts": [],
        "host_performance": [],
        "datastores": _region_datastores(vms),
        "vms": vms,
        "host_nics": [],
    }
    wb.close()

    result["summary"] = _build_summary(result, source="azure")
    _finalize_summary(result["summary"], result["vms"])
    # One per region of a multi-region estate.
    result["clusters"] = _cluster_summaries(result, _build_summary)
    for cluster in result["clusters"]:
        _finalize_summary(cluster["summary"],
                          [vm for vm in vms if vm["cluster"] == cluster["name"]])
    return result


def _parse_details(wb):
    if "Details" not in wb.sheetnames:
        return {}
    info = {}
    for row in wb["Details"].iter_rows(values_only=True):
        if row and len(row) > 1 and row[0] and row[1] not in (None, ""):
            info[str(row[0]).strip()] = row[1]
    return info


# Memory per vCPU (GiB) by size family, for a SKU whose figures the export
# leaves blank. Only families with a fixed ratio across their sizes; "l" is
# the low-memory variant (D8ls_v5: 16 GiB).
_GIB_PER_VCPU = {"D": 4, "DL": 2, "E": 8, "F": 2}
# Sizes Azure reports as 0 cores / 0 MiB (GPU partitions), from Azure's docs.
_KNOWN_SKUS = {
    "STANDARD_NV4AS_V4": (4, 14), "STANDARD_NV8AS_V4": (8, 28),
    "STANDARD_NV16AS_V4": (16, 56), "STANDARD_NV32AS_V4": (32, 112),
}
_SKU_RE = re.compile(r"^Standard_([A-Z]+)(\d+)([a-z]*)", re.I)


def _sku_spec(sku, measured):
    """(vcpus, memory GiB) for an Azure size: the figures the export gives
    for that size elsewhere, else Azure's published spec, else derived from
    the size name (Standard_D16ds_v5 = 16 vCPU, D family 4 GiB per vCPU).
    (0, 0) when the size can't be read."""
    if sku in measured:
        return measured[sku]
    key = str(sku or "").upper()
    if key in _KNOWN_SKUS:
        return _KNOWN_SKUS[key]
    m = _SKU_RE.match(str(sku or ""))
    if not m:
        return 0, 0
    family, vcpus, flags = m.group(1).upper(), int(m.group(2)), m.group(3).lower()
    ratio = _GIB_PER_VCPU.get(family + ("L" if "l" in flags else ""),
                              _GIB_PER_VCPU.get(family, 0))
    return vcpus, vcpus * ratio


def _os_name(urn):
    """Publisher:Offer:Sku:Version image URN to a readable OS string. Custom
    images carry an empty URN (":::1.0.0") and stay unknown."""
    parts = [p for p in str(urn or "").split(":")[:3] if p]
    return " ".join(parts)


def _parse_vms(wb):
    # Attached managed disks per VM (keyed on the VM's resource id): the
    # authoritative sizes. The VM sheet's "OS Disk Size" is the 1023 GiB
    # maximum on most rows, not the disk's size.
    disks = {}
    for r in _sheet_rows(wb, "Azure Disks"):
        vm_id = str(r.get("VM ID") or "").lower()
        if vm_id:
            disks[vm_id] = disks.get(vm_id, 0.0) + _float(r.get("Disk Size (GiB)", 0))

    rows = list(_sheet_rows(wb, "Azure VMs"))
    measured = {}
    for r in rows:
        cores, mem_mib = _int(r.get("CPU Cores", 0)), _float(r.get("Memory (MiB)", 0))
        if cores and mem_mib:
            measured[r.get("VM Size")] = (cores, mem_mib / 1024)

    vms, seen = [], set()
    for r in rows:
        name = str(r.get("VM Name") or "").strip()
        if not name:
            continue
        seen.add(str(r.get("vmId") or "").lower())
        vcpus, mem_gb = _int(r.get("CPU Cores", 0)), _float(r.get("Memory (MiB)", 0)) / 1024
        if not (vcpus and mem_gb):
            vcpus, mem_gb = _sku_spec(r.get("VM Size"), measured)
        disk_gb = disks.get(str(r.get("ID") or "").lower())
        if disk_gb is None:
            disk_gb = (_float(r.get("OS Disk Size (MiB)", 0)) / 1024
                       + _float(r.get("Total Data Disks Size (GiB)", 0)))
        vms.append(_vm(r, name, str(r.get("VM State", "")), r.get("VM Size"),
                       vcpus, mem_gb, disk_gb))

    # Scale-set instances (AKS node pools, ...) list a SKU but no cores or
    # memory, and their power state only appears on the Load Balancers sheet;
    # an instance absent there is a running member of its scale set.
    states = {}
    for r in _sheet_rows(wb, "Load Balancers"):
        states[str(r.get("VM ID") or "").lower()] = str(r.get("VM State", ""))
    for r in _sheet_rows(wb, "Scale Set VMs"):
        name = str(r.get("VM Name") or "").strip()
        if not name or str(r.get("vmId") or "").lower() in seen:
            continue
        vcpus, mem_gb = _sku_spec(r.get("SKU"), measured)
        disk_gb = _float(r.get("OS Disk Size (GiB)", 0)) + _float(r.get("Total Data Disks Size (GiB)", 0))
        state = states.get(str(r.get("ID") or "").lower(), "VM running")
        vms.append(_vm(r, name, state, r.get("SKU"), vcpus, mem_gb, disk_gb,
                       scale_set=r.get("Scale Set Name") or ""))
    return vms


def _vm(r, name, state, sku, vcpus, mem_gb, disk_gb, scale_set=None):
    # "VM deallocated" / "VM stopped" are off; "VM running" and the transient
    # starting/updating states are on.
    powered_on = not any(s in state.lower() for s in ("deallocat", "stopp"))
    disk_gb = round(disk_gb, 2)
    mem_gb = round(mem_gb, 2)
    vm = {
        "name": name,
        "powered_on": powered_on,
        "is_template": False,
        "os": _os_name(r.get("Image URN")),
        "model": str(sku or ""),
        "vcpus": vcpus,
        "provisioned_memory_gb": mem_gb,
        # Nothing measured: Azure reserves the full size for a running VM.
        "used_memory_gb": mem_gb,
        "consumed_memory_gb": mem_gb,
        "disk_capacity_gb": disk_gb,
        "disk_used_gb": disk_gb,
        "vdisk_size_gb": disk_gb,
        "vdisk_used_gb": disk_gb,
        "datastore": r.get("Location", "") or "",
        "host": "",
        "cluster": r.get("Location", "") or "",
        **vm_identity(r, ("vmId",), ()),
    }
    if scale_set is not None:
        vm["scale_set"] = scale_set
    return vm


def _region_datastores(vms):
    """One shared pool per region holding every VM's disks, deallocated VMs
    included (their disks still exist and move with them)."""
    pools = {}
    for vm in vms:
        pool = pools.setdefault(vm["cluster"], {
            "name": f"Azure managed disks ({vm['cluster'] or 'unknown region'})",
            "type": "cluster", "capacity_gib": 0.0, "used_gib": 0.0,
            "free_gib": 0.0, "vm_count": 0, "mounts": [],
        })
        pool["capacity_gib"] += vm["vdisk_size_gb"]
        pool["used_gib"] += vm["vdisk_used_gb"]
        pool["vm_count"] += 1
    for pool in pools.values():
        pool["capacity_gib"] = round(pool["capacity_gib"], 1)
        pool["used_gib"] = round(pool["used_gib"], 1)
    return list(pools.values())


def _finalize_summary(summary, vms):
    regions = sorted({vm["cluster"] for vm in vms if vm["cluster"]})
    summary["current_platform"] = "Microsoft Azure" + (
        f" ({', '.join(regions)})" if regions else "")
    summary["cluster_name"] = regions[0] if len(regions) == 1 else ""
    summary["scan_type"] = "azure"
    # No physical cores behind a cloud VM: no overcommit to measure. Same
    # assumed 3:1 default (flagged) as a Live Optics GENERAL server scan.
    summary["vcpu_per_core_ratio"] = 3.0
    summary["vcpu_ratio_assumed"] = True
