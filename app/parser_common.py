"""Shared summary builder for the workbook parsers.

``build_summary`` was duplicated almost verbatim in liveoptics.py and rvtools.py
(~90 lines each) — the two differed only in incidentals the single function now
folds in:

  * cluster vs local storage is split on the datastore ``type`` field; RVTools
    datastores carry no ``type``, so ``!= "local"`` maps them all to cluster and
    the local totals fall out as 0 (exactly the old hardcoded behaviour);
  * ``p95_iops`` is read with ``.get`` so a perf shape that lacks it (RVTools)
    contributes 0 rather than raising;
  * the optional ``source`` tag is passed in by the RVTools caller.

Kept in its own module (not xlsx_utils) because it is parser output, not
workbook I/O — and it is listed in fingerprint.PARSER_MODULES so a change to the
summary maths flags saved sizings "re-import needed", the same as an edit to
either parser would.
"""
from xlsx_utils import source_cpus as _source_cpus

# ── Guest licensing exposure (docs/pricebook-plan.md §5.6) ───────────────────
# Windows, SQL Server and Oracle bill per PHYSICAL core with no cap, and on a
# database estate that line routinely exceeds hardware and the HyperCore licence
# combined. We never quote it — but dropping the pressure entirely would leave
# nothing arguing against high core counts once the Scale licence goes flat
# above its cap.
#
# Two facts shape this:
#
#   * **Exposure is near-binary, not proportional.** Windows Datacenter licenses
#     EVERY physical core of any host that might run a Windows VM, and with HA
#     free to move VMs anywhere, one Windows VM makes the whole cluster
#     billable. An 83%-Windows estate and a 100%-Windows estate have the same
#     per-core exposure. So the measured share is a good DETECTOR and a bad
#     multiplier — we report it for the annotation and gate on presence.
#   * **Databases cannot be detected.** Oracle or SQL Server is a VM with a
#     database on it, invisible in the guest OS string. VM-name heuristics would
#     be guessing. That dimension is always declared by the SA.

EXPOSURE_NONE = "none"
EXPOSURE_WINDOWS = "windows"
EXPOSURE_WINDOWS_DB = "windows_db"

VALID_EXPOSURES = (EXPOSURE_NONE, EXPOSURE_WINDOWS, EXPOSURE_WINDOWS_DB)


def detect_guest_licensing(vms):
    """Infer core-licensed guest OS exposure from parsed VMs.

    Returns (exposure, detail) where exposure is one of VALID_EXPOSURES and
    detail is a short human string for the candidate annotation, or None when
    the source carries no OS information at all.

    Never returns EXPOSURE_WINDOWS_DB — a database is not detectable. The SA
    declares that.
    """
    known = [v for v in vms if str(v.get("os") or "").strip()]
    if not known:
        return None, None

    def _is_windows(v):
        return "windows" in str(v.get("os") or "").lower()

    win = [v for v in known if _is_windows(v)]
    if not win:
        return EXPOSURE_NONE, (
            f"no Windows guests among {len(known)} VMs with a known OS"
        )

    # Share is reported, not used as a multiplier — see the note above.
    total_vcpu = sum(v.get("vcpus") or 0 for v in known)
    win_vcpu = sum(v.get("vcpus") or 0 for v in win)
    pct = round(100 * win_vcpu / total_vcpu) if total_vcpu else 0
    return EXPOSURE_WINDOWS, f"Windows detected in source ({pct}% of vCPU)"



def build_summary(data, source=None):
    """Aggregate a parsed workload (hosts / vms / perf / datastores) into the
    flat summary dict the recommender and UI consume. ``source`` optionally tags
    the originating format (e.g. "rvtools")."""
    hosts = data["hosts"]
    vms = data["vms"]
    perfs = data["host_performance"]
    datastores = data["datastores"]

    active_vms = [v for v in vms if v["powered_on"] and not v["is_template"]]

    total_host_cores = sum(h["cpu_cores"] for h in hosts)
    total_host_threads = sum(h["cpu_threads"] for h in hosts)
    total_host_ghz = sum(h["cpu_ghz"] * h["cpu_cores"] for h in hosts)
    total_host_ram_gb = sum(h["memory_gb"] for h in hosts)

    total_vcpus = sum(v["vcpus"] for v in active_vms)
    total_vm_prov_mem_gb = sum(v["provisioned_memory_gb"] for v in active_vms)
    total_vm_used_mem_gb = sum(v["consumed_memory_gb"] for v in active_vms)
    total_vm_disk_prov_gb = sum(v["vdisk_size_gb"] for v in active_vms)
    total_vm_disk_used_gb = sum(v["vdisk_used_gb"] for v in active_vms)

    # Cluster (shared) storage is the default sizing basis; local (per-host)
    # storage is added only when the user opts in. A datastore with no ``type``
    # (RVTools) counts as cluster, leaving the local totals at 0.
    cluster_total_gib = sum(d["capacity_gib"] for d in datastores if d.get("type") != "local")
    cluster_used_gib = sum(d["used_gib"] for d in datastores if d.get("type") != "local")
    local_total_gib = sum(d["capacity_gib"] for d in datastores if d.get("type") == "local")
    local_used_gib = sum(d["used_gib"] for d in datastores if d.get("type") == "local")

    peak_cpu_pct = max((p["peak_cpu_pct"] for p in perfs), default=0)
    avg_cpu_pct = sum(p["avg_cpu_pct"] for p in perfs) / len(perfs) if perfs else 0
    peak_cpu_ghz = sum(p["peak_cpu_ghz"] for p in perfs)
    avg_cpu_ghz = sum(p["avg_cpu_ghz"] for p in perfs)
    peak_mem_pct = max((p["peak_mem_pct"] for p in perfs), default=0)
    avg_mem_pct = sum(p["avg_mem_pct"] for p in perfs) / len(perfs) if perfs else 0
    total_peak_iops = sum(p["peak_iops"] for p in perfs)
    total_avg_iops = sum(p["avg_iops"] for p in perfs)
    total_p95_iops = sum(p.get("p95_iops", 0) for p in perfs)

    nic_speeds = set()
    for n in data.get("host_nics", []):
        if n["speed_mbps"] > 0:
            nic_speeds.add(n["speed_mbps"])

    summary = {
        "host_count": len(hosts),
        "cluster_name": hosts[0]["cluster"] if hosts else "",
        "current_platform": f"{hosts[0]['manufacturer']} {hosts[0]['model']}" if hosts else "",
        "source_cpus": _source_cpus(hosts),

        "total_host_cores": total_host_cores,
        "total_host_threads": total_host_threads,
        "total_host_ghz": round(total_host_ghz, 1),
        "total_host_ram_gb": round(total_host_ram_gb, 1),
        "per_host_cores": round(total_host_cores / len(hosts), 1) if hosts else 0,
        "per_host_ram_gb": round(total_host_ram_gb / len(hosts), 1) if hosts else 0,

        "total_vms": len(vms),
        "active_vms": len(active_vms),
        "total_vcpus": total_vcpus,
        "total_vm_provisioned_memory_gb": round(total_vm_prov_mem_gb, 1),
        "total_vm_used_memory_gb": round(total_vm_used_mem_gb, 1),
        "total_vm_provisioned_storage_gb": round(total_vm_disk_prov_gb, 1),
        "total_vm_used_storage_gb": round(total_vm_disk_used_gb, 1),
        "total_vm_provisioned_storage_tb": round(total_vm_disk_prov_gb / 1024, 2),
        "total_vm_used_storage_tb": round(total_vm_disk_used_gb / 1024, 2),

        "datastore_total_tb": round(cluster_total_gib / 1024, 2),
        "datastore_used_tb": round(cluster_used_gib / 1024, 2),
        "local_total_tb": round(local_total_gib / 1024, 2),
        "local_used_tb": round(local_used_gib / 1024, 2),
        "local_used_gb": round(local_used_gib),

        "peak_cpu_pct": round(peak_cpu_pct, 1),
        "avg_cpu_pct": round(avg_cpu_pct, 1),
        "peak_cpu_ghz": round(peak_cpu_ghz, 1),
        "avg_cpu_ghz": round(avg_cpu_ghz, 1),
        "peak_mem_pct": round(peak_mem_pct, 1),
        "avg_mem_pct": round(avg_mem_pct, 1),
        "total_peak_iops": round(total_peak_iops),
        "total_avg_iops": round(total_avg_iops),
        "p95_iops": round(total_p95_iops),

        "nic_speed_mbps": max(nic_speeds) if nic_speeds else 0,

        "vcpu_per_core_ratio": round(total_vcpus / total_host_cores, 2) if total_host_cores > 0 else 0,
        "vcpu_per_thread_ratio": round(total_vcpus / total_host_threads, 2) if total_host_threads > 0 else 0,

        "max_vm_ram_gb": max((v["provisioned_memory_gb"] for v in active_vms), default=0),
        "max_vm_cores": max((v["vcpus"] for v in active_vms), default=0),
    }

    # Detected default for the guest-licensing exposure. The SA can override it
    # in advanced options; detection only sets what the control starts on.
    exposure, detail = detect_guest_licensing(active_vms or vms)
    if exposure:
        summary["guest_licensing"] = exposure
        summary["guest_licensing_detail"] = detail

    if source:
        summary["source"] = source
    return summary
