from openpyxl import load_workbook


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

    result["summary"] = _build_summary(result)
    return result


def _sheet_rows(wb, name):
    if name not in wb.sheetnames:
        return []
    ws = wb[name]
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return []
    headers = [str(h).strip() if h else f"col_{i}" for i, h in enumerate(rows[0])]
    return [dict(zip(headers, row)) for row in rows[1:] if any(v is not None for v in row)]


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


def _build_summary(data):
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

    ds_total_gib = sum(d["capacity_gib"] for d in datastores)
    ds_used_gib = sum(d["used_gib"] for d in datastores)

    peak_cpu_pct = max((p["peak_cpu_pct"] for p in perfs), default=0)
    avg_cpu_pct = sum(p["avg_cpu_pct"] for p in perfs) / len(perfs) if perfs else 0
    peak_cpu_ghz = sum(p["peak_cpu_ghz"] for p in perfs)
    avg_cpu_ghz = sum(p["avg_cpu_ghz"] for p in perfs)
    peak_mem_pct = max((p["peak_mem_pct"] for p in perfs), default=0)
    avg_mem_pct = sum(p["avg_mem_pct"] for p in perfs) / len(perfs) if perfs else 0
    total_peak_iops = sum(p["peak_iops"] for p in perfs)
    total_avg_iops = sum(p["avg_iops"] for p in perfs)

    nic_speeds = set()
    for n in data.get("host_nics", []):
        if n["speed_mbps"] > 0:
            nic_speeds.add(n["speed_mbps"])

    return {
        "host_count": len(hosts),
        "cluster_name": hosts[0]["cluster"] if hosts else "",
        "current_platform": f"{hosts[0]['manufacturer']} {hosts[0]['model']}" if hosts else "",

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

        "datastore_total_tb": round(ds_total_gib / 1024, 2),
        "datastore_used_tb": round(ds_used_gib / 1024, 2),
        # RVTools lists each datastore once (no per-host duplication, no local
        # split), so there is no separate local storage to offer.
        "local_total_tb": 0,
        "local_used_tb": 0,
        "local_used_gb": 0,

        "peak_cpu_pct": round(peak_cpu_pct, 1),
        "avg_cpu_pct": round(avg_cpu_pct, 1),
        "peak_cpu_ghz": round(peak_cpu_ghz, 1),
        "avg_cpu_ghz": round(avg_cpu_ghz, 1),
        "peak_mem_pct": round(peak_mem_pct, 1),
        "avg_mem_pct": round(avg_mem_pct, 1),
        "total_peak_iops": round(total_peak_iops),
        "total_avg_iops": round(total_avg_iops),

        "nic_speed_mbps": max(nic_speeds) if nic_speeds else 0,

        "vcpu_per_core_ratio": round(total_vcpus / total_host_cores, 2) if total_host_cores > 0 else 0,
        "vcpu_per_thread_ratio": round(total_vcpus / total_host_threads, 2) if total_host_threads > 0 else 0,

        "max_vm_ram_gb": max((v["provisioned_memory_gb"] for v in active_vms), default=0),
        "max_vm_cores": max((v["vcpus"] for v in active_vms), default=0),

        "source": "rvtools",
    }


def _float(v):
    try:
        return float(v) if v else 0.0
    except (ValueError, TypeError):
        return 0.0


def _int(v):
    try:
        return int(v) if v else 0
    except (ValueError, TypeError):
        return 0
