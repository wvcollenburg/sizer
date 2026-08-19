from openpyxl import load_workbook
from xlsx_utils import (sheet_rows as _sheet_rows, to_float as _float,
                        to_int as _int)
from parser_common import build_summary as _build_summary
from cluster_split import cluster_summaries as _cluster_summaries


def parse_rvtools(file_path):
    wb = load_workbook(file_path, read_only=True, data_only=True)
    result = {
        "project": _parse_metadata(wb),
        "hosts": _parse_hosts(wb),
        "host_performance": _parse_host_perf(wb),
        "datastores": _parse_datastores(wb),
        "vms": _parse_vms(wb),
        "host_nics": _parse_nics(wb),
    }
    wb.close()

    result["summary"] = _build_summary(result, source="rvtools")
    # Per-source-cluster summaries (empty for single-cluster datasets). RVTools
    # has no host↔datastore link, so per-cluster storage is attributed by each
    # cluster's share of VM used storage (see cluster_split).
    result["clusters"] = _cluster_summaries(result, _build_summary)
    return result


def _parse_metadata(wb):
    info = {}
    for r in _sheet_rows(wb, "vMetaData"):
        key = str(r.get("col_0", r.get("Key", ""))).strip()
        val = r.get("col_1", r.get("Value", ""))
        if key and val:
            info[key] = val
    return info


def _parse_hosts(wb):
    hosts = []
    for r in _sheet_rows(wb, "vHost"):
        speed_mhz = _float(r.get("Speed", 0))
        sockets = _int(r.get("# CPU", 0))
        cores_per_cpu = _int(r.get("Cores per CPU", 0))
        total_cores = _int(r.get("# Cores", 0))
        if total_cores == 0:
            total_cores = sockets * cores_per_cpu
        ht_active = str(r.get("HT Active", "")).lower() == "true"
        total_threads = total_cores * 2 if ht_active else total_cores
        mem_mib = _float(r.get("# Memory", 0))

        hosts.append({
            "name": r.get("Host", ""),
            "cluster": r.get("Cluster", ""),
            "manufacturer": r.get("Vendor", ""),
            "model": r.get("Model", ""),
            "cpu_sockets": sockets,
            "cpu_cores": total_cores,
            "cpu_threads": total_threads,
            "cpu_desc": r.get("CPU Model", ""),
            "cpu_ghz": round(speed_mhz / 1000, 3),
            "net_ghz": round(speed_mhz / 1000 * total_cores, 1),
            "memory_kib": mem_mib * 1024,
            "memory_gb": round(mem_mib / 1024, 1),
            "local_capacity_gib": 0,
            "vm_count": _int(r.get("# VMs", 0)),
            "nic_count": _int(r.get("# NICs", 0)),
        })
    return hosts


def _parse_host_perf(wb):
    perfs = []
    for r in _sheet_rows(wb, "vHost"):
        cpu_usage_pct = _float(r.get("CPU usage %", 0))
        mem_usage_pct = _float(r.get("Memory usage %", 0))
        speed_mhz = _float(r.get("Speed", 0))
        total_cores = _int(r.get("# Cores", 0))
        mem_mib = _float(r.get("# Memory", 0))

        total_ghz = (speed_mhz / 1000) * total_cores
        peak_cpu_ghz = total_ghz * (cpu_usage_pct / 100) if cpu_usage_pct > 0 else 0
        peak_mem_mib = mem_mib * (mem_usage_pct / 100) if mem_usage_pct > 0 else 0

        perfs.append({
            "host": r.get("Host", ""),
            "peak_cpu_pct": cpu_usage_pct,
            "peak_cpu_ghz": round(peak_cpu_ghz, 1),
            "avg_cpu_pct": cpu_usage_pct,
            "avg_cpu_ghz": round(peak_cpu_ghz, 1),
            "peak_mem_pct": mem_usage_pct,
            "peak_mem_mib": round(peak_mem_mib, 1),
            "avg_mem_pct": mem_usage_pct,
            "avg_mem_mib": round(peak_mem_mib, 1),
            "peak_iops": 0,
            "avg_iops": 0,
            "peak_throughput_mbs": 0,
            "avg_throughput_mbs": 0,
        })
    return perfs


def _mib(row, base):
    """Read a binary-capacity column that RVTools renamed across versions.

    Pre-4.0 exports label these columns '<base> MiB'; RVTools 4.0.x relabelled
    them '<base> MB' while keeping the values binary (still MiB). Accept either
    spelling so storage isn't silently read as 0 on newer exports."""
    for key in (base + " MiB", base + " MB"):
        if key in row:
            return _float(row[key])
    return 0.0


def _parse_datastores(wb):
    stores = []
    for r in _sheet_rows(wb, "vDatastore"):
        stores.append({
            "name": r.get("Name", ""),
            "capacity_gib": round(_mib(r, "Capacity") / 1024, 1),
            "used_gib": round(_mib(r, "In Use") / 1024, 1),
            "free_gib": round(_mib(r, "Free") / 1024, 1),
            "vm_count": _int(r.get("# VMs", 0)),
        })
    return stores


def _parse_vms(wb):
    vms = []
    for r in _sheet_rows(wb, "vInfo"):
        powered_on = str(r.get("Powerstate", "")).lower() == "poweredon"
        is_template = str(r.get("Template", "")).upper() == "TRUE"
        prov_mem_mib = _float(r.get("Memory", 0))
        provisioned_mib = _mib(r, "Provisioned")
        in_use_mib = _mib(r, "In Use")
        # RVTools 4.0.x dropped the 'Total disk capacity' column; fall back to
        # provisioned capacity (the closest equivalent) when it's absent.
        disk_cap_mib = _mib(r, "Total disk capacity") or provisioned_mib

        vms.append({
            "name": r.get("VM", ""),
            "powered_on": powered_on,
            "is_template": is_template,
            "os": r.get("OS according to the configuration file", ""),
            "vcpus": _int(r.get("CPUs", 0)),
            "provisioned_memory_gb": round(prov_mem_mib / 1024, 2),
            "used_memory_gb": 0,
            "consumed_memory_gb": round(_float(r.get("Active Memory", 0)) / 1024, 2),
            "disk_capacity_gb": round(disk_cap_mib / 1024, 2),
            "disk_used_gb": round(in_use_mib / 1024, 2),
            "vdisk_size_gb": round(provisioned_mib / 1024, 2),
            "vdisk_used_gb": round(in_use_mib / 1024, 2),
            "datastore": "",
            "host": r.get("Host", ""),
            "cluster": r.get("Cluster", ""),
        })
    return vms


def _parse_nics(wb):
    nics = []
    for r in _sheet_rows(wb, "vNIC"):
        speed = _float(r.get("Speed", 0))
        nics.append({
            "host": r.get("Host", ""),
            "name": r.get("Network Device", ""),
            "speed_mbps": speed,
            "vendor": "",
            "device": r.get("Driver", ""),
        })
    return nics

