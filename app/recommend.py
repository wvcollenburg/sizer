import math
from sqlalchemy.orm import joinedload
from orm_models import (
    Model, StorageConfig, ModelCpuOption, ModelNicOption, StorageConfigDrive,
)


def generate_recommendations(summary, vcpu_ratio=None, growth_pct=10,
                             snapshot_pct=20, years=5):
    if vcpu_ratio is None:
        vcpu_ratio = summary.get("vcpu_per_core_ratio", 3.0)
    vcpu_ratio = max(1.0, min(vcpu_ratio, 10.0))
    years = max(1, min(years, 5))
    growth = growth_pct / 100
    snap_base = snapshot_pct / 100

    growth_factor = (1 + growth) ** years
    snap_at_target = snap_base * (1 + growth) ** years

    base_vcpus = summary["total_vcpus"]
    base_ram = summary["total_vm_provisioned_memory_gb"]
    base_storage = summary["datastore_used_tb"]

    projected_vcpus = math.ceil(base_vcpus * growth_factor)
    projected_ram = base_ram * growth_factor
    projected_storage = base_storage * growth_factor * (1 + snap_at_target)

    needs = {
        "vcpus": projected_vcpus,
        "ram_gb": projected_ram,
        "usable_storage_tb": projected_storage,
        "nic_speed_mbps": summary["nic_speed_mbps"],
        "vcpu_ratio": vcpu_ratio,
        "current_total_ghz": summary.get("total_host_ghz", 0),
    }

    required_cores = math.ceil(needs["vcpus"] / vcpu_ratio)

    models = Model.query.options(
        joinedload(Model.cpu_links).joinedload(ModelCpuOption.cpu),
        joinedload(Model.nic_links).joinedload(ModelNicOption.nic),
        joinedload(Model.ram_options),
        joinedload(Model.storage_config)
            .joinedload(StorageConfig.drive_links)
            .joinedload(StorageConfigDrive.drive),
    ).filter(Model.status == "Active").all()
    candidates = []

    for m in models:
        md = m.to_dict()
        md["name"] = m.name
        if md["storage"]["type"] == "cloud":
            continue

        fits = _fit_model(md, needs, required_cores)
        candidates.extend(fits)

    candidates.sort(key=lambda c: c["score"])

    seen = set()
    deduped = []
    for c in candidates:
        key = (c["model"], c["node_count"])
        if key not in seen:
            seen.add(key)
            deduped.append(c)

    peak_ghz = summary.get("peak_cpu_ghz", 0)
    if peak_ghz <= 0:
        peak_ghz = summary.get("total_host_ghz", 0)
    projected_ghz = round(peak_ghz * growth_factor, 1)

    projection = {
        "years": years,
        "growth_pct": growth_pct,
        "snapshot_pct": snapshot_pct,
        "base_vcpus": base_vcpus,
        "base_ram_gb": round(base_ram, 1),
        "base_storage_tb": round(base_storage, 2),
        "base_ghz": round(peak_ghz, 1),
        "projected_vcpus": projected_vcpus,
        "projected_ram_gb": round(projected_ram, 1),
        "projected_storage_tb": round(projected_storage, 2),
        "projected_ghz": projected_ghz,
        "snapshot_pct_at_target": round(snap_at_target * 100, 1),
        "growth_factor": round(growth_factor, 3),
    }

    return {"recommendations": deduped[:8], "projection": projection}


MAX_NODES_PER_CLUSTER = 8

COST_TIERS = {
    "SE":     3,   # Lenovo
    "HE15":   1,   # Intel NUC class
    "HE25":   2,   # Minisforum/SNUC class
    "HE5":    5,   # Supermicro 1U entry
    "HC1":    6,   # Supermicro datacenter 1U
    "HC3F":  10,   # Dell/Lenovo single-socket server
    "HC3DF": 12,   # Dell/Lenovo dual-socket server
    "HC5H":  13,   # 2U HDD-only (no NVMe)
    "HC5D":  14,   # 2U hybrid with NVMe
}


def _model_cost_tier(name):
    if name.startswith("SE"):
        return COST_TIERS["SE"]
    if name.startswith("HE15"):
        return COST_TIERS["HE15"]
    if name.startswith("HE25"):
        return COST_TIERS["HE25"]
    if name.startswith("HE5"):
        return COST_TIERS["HE5"]
    if name.startswith("HC1"):
        return COST_TIERS["HC1"]
    if name.startswith("HC3"):
        return COST_TIERS["HC3DF"] if "DF" in name else COST_TIERS["HC3F"]
    if name.startswith("HC5"):
        if "50" in name:
            return COST_TIERS["HC5D"]
        return COST_TIERS["HC5H"]
    return 5


def _cluster_layout(total_nodes):
    if total_nodes <= MAX_NODES_PER_CLUSTER:
        return [total_nodes]
    num_clusters = math.ceil(total_nodes / MAX_NODES_PER_CLUSTER)
    base = total_nodes // num_clusters
    remainder = total_nodes % num_clusters
    return [base + 1] * remainder + [base] * (num_clusters - remainder)


def _cluster_usable_storage(raw_per_node, biggest_disk, cluster_sizes):
    total = 0
    for size in cluster_sizes:
        total += (raw_per_node * size - biggest_disk) / 2
    return total


def _fit_model(model, needs, required_cores):
    results = []
    storage = model["storage"]
    min_nodes = max(model.get("min_nodes", 3), 3)

    max_raw, max_biggest = _max_raw_per_node(storage)
    max_ram = max(model["ram_options_gb"]) if model["ram_options_gb"] else 0

    needed_nodes_storage = 0
    if max_raw > 0 and needs["usable_storage_tb"] > 0:
        for n in range(min_nodes, 200):
            layout = _cluster_layout(n)
            usable = _cluster_usable_storage(max_raw, max_biggest, layout)
            if usable >= needs["usable_storage_tb"]:
                needed_nodes_storage = n
                break

    needed_nodes_ram = 0
    if max_ram > 0 and needs["ram_gb"] > 0:
        for n in range(min_nodes, 200):
            layout = _cluster_layout(n)
            n1_nodes = n - len(layout)
            if max_ram * n1_nodes >= needs["ram_gb"]:
                needed_nodes_ram = n
                break

    cpus_by_cores = sorted(
        enumerate(model["cpu_options"]),
        key=lambda x: x[1]["cores"],
        reverse=True,
    )

    for cpu_idx, cpu in cpus_by_cores:
        cores_per_node = cpu["cores"]
        threads_per_node = cpu["threads"]
        ghz_per_node = cpu["ghz"] * cores_per_node

        if cores_per_node == 0:
            continue

        needed_nodes_cpu = 0
        for n in range(min_nodes, 200):
            layout = _cluster_layout(n)
            n1 = n - len(layout)
            if cores_per_node * n1 >= required_cores:
                needed_nodes_cpu = n
                break

        start_nodes = max(min_nodes, needed_nodes_cpu,
                          needed_nodes_storage, needed_nodes_ram)

        for node_count in range(start_nodes, start_nodes + 5):
            layout = _cluster_layout(node_count)
            num_clusters = len(layout)
            n1_nodes = node_count - num_clusters
            n1_cores = cores_per_node * n1_nodes

            if n1_cores < required_cores:
                continue

            ram_gb = _pick_ram(model["ram_options_gb"], needs["ram_gb"],
                               node_count, num_clusters)
            if not ram_gb:
                continue

            stor = _pick_storage_multi(storage, needs["usable_storage_tb"],
                                       layout)
            if not stor:
                continue

            total_cores = cores_per_node * node_count
            total_threads = threads_per_node * node_count
            total_ghz = ghz_per_node * node_count
            total_ram = ram_gb * node_count

            raw_per_node = stor["raw_per_node"]
            biggest_disk = stor["biggest_disk"]
            total_raw = raw_per_node * node_count
            usable = _cluster_usable_storage(raw_per_node, biggest_disk, layout)

            if usable < needs["usable_storage_tb"]:
                continue

            n1_ghz = ghz_per_node * n1_nodes
            rec_ratio = needs["vcpus"] / n1_cores if n1_cores > 0 else 99

            cost_tier = _model_cost_tier(model["name"])
            excess_cores = total_cores - required_cores

            score = node_count * 20
            score += cost_tier * 6
            score += excess_cores * 0.3

            core_headroom = (n1_cores - required_cores) / required_cores if required_cores > 0 else 0
            ram_headroom = (ram_gb * n1_nodes - needs["ram_gb"]) / needs["ram_gb"] if needs["ram_gb"] > 0 else 0
            stor_headroom = (usable - needs["usable_storage_tb"]) / needs["usable_storage_tb"] if needs["usable_storage_tb"] > 0 else 0

            score += core_headroom * 3
            score += ram_headroom * 3
            score += stor_headroom * 2

            if needs["current_total_ghz"] > 0:
                ghz_ratio = n1_ghz / needs["current_total_ghz"]
                if ghz_ratio >= 1.0:
                    score -= (ghz_ratio - 1.0) * 5
                else:
                    score += (1.0 - ghz_ratio) * 15

            results.append({
                "model": model["name"],
                "category": model["category"],
                "form_factor": model["form_factor"],
                "chassis": model["chassis"],
                "cost_tier": cost_tier,
                "node_count": node_count,
                "num_clusters": num_clusters,
                "cluster_layout": layout,
                "cpu": cpu["desc"],
                "cpu_index": cpu_idx,
                "cores_per_node": cores_per_node,
                "threads_per_node": threads_per_node,
                "ghz": cpu["ghz"],
                "ram_per_node_gb": ram_gb,
                "storage_config": stor,
                "vcpu_ratio": round(rec_ratio, 2),
                "totals": {
                    "cores": total_cores,
                    "threads": total_threads,
                    "total_ghz": round(total_ghz, 1),
                    "ram_gb": total_ram,
                    "raw_storage_tb": round(total_raw, 2),
                    "usable_storage_tb": round(usable, 2),
                },
                "n_minus_1": {
                    "cores": cores_per_node * n1_nodes,
                    "threads": threads_per_node * n1_nodes,
                    "total_ghz": round(n1_ghz, 1),
                    "ram_gb": ram_gb * n1_nodes,
                    "usable_storage_tb": round(usable, 2),
                },
                "score": round(score, 2),
            })

            break

    return results


def _max_raw_per_node(storage):
    stype = storage.get("type", "")
    if stype == "nvme_only":
        opts = storage.get("nvme_options_tb", [])
        count = storage.get("drives_per_node", 1)
        if opts:
            biggest = max(opts)
            return biggest * count, biggest
    elif stype == "ssd_only":
        opts = storage.get("ssd_options_tb", [])
        count = storage.get("drives_per_node", 4)
        if opts:
            biggest = max(opts)
            return biggest * count, biggest
    elif stype == "hdd_only":
        opts = storage.get("hdd_options_tb", [])
        count = storage.get("drives_per_node", 4)
        if opts:
            biggest = max(opts)
            return biggest * count, biggest
    elif stype == "hybrid":
        hdd_opts = storage.get("hdd_options_tb", [])
        ssd_opts = storage.get("ssd_options_tb", [])
        if hdd_opts and ssd_opts:
            hdd_max = max(hdd_opts)
            ssd_max = max(ssd_opts)
            raw = hdd_max * storage.get("hdd_count", 3) + ssd_max * storage.get("ssd_count", 1)
            return raw, max(hdd_max, ssd_max)
    elif stype == "hybrid_nvme":
        hdd_opts = storage.get("hdd_options_tb", [])
        nvme_opts = storage.get("nvme_options_tb", [])
        if hdd_opts and nvme_opts:
            hdd_max = max(hdd_opts)
            nvme_max = max(nvme_opts)
            raw = hdd_max * storage.get("hdd_count", 3) + nvme_max * storage.get("nvme_count", 1)
            return raw, max(hdd_max, nvme_max)
    elif stype == "nvme_and_ssd":
        nvme_opts = storage.get("nvme_options_tb", [])
        ssd_opts = storage.get("ssd_options_tb", [])
        if nvme_opts and ssd_opts:
            nvme_max = max(nvme_opts)
            ssd_max = max(ssd_opts)
            return nvme_max + ssd_max, max(nvme_max, ssd_max)
    return 0, 0


def _pick_ram(options, total_needed_gb, node_count, num_clusters=1):
    n1_nodes = node_count - num_clusters
    for r in sorted(options):
        if r * n1_nodes >= total_needed_gb:
            return r
    return None


def _pick_storage_multi(storage, usable_needed_tb, cluster_layout):
    stype = storage["type"]

    if stype == "nvme_only":
        return _pick_uniform_drives(
            storage.get("nvme_options_tb", []),
            storage.get("drives_per_node", 1),
            usable_needed_tb, cluster_layout, "nvme"
        )
    elif stype == "ssd_only":
        return _pick_uniform_drives(
            storage.get("ssd_options_tb", []),
            storage.get("drives_per_node", 4),
            usable_needed_tb, cluster_layout, "ssd"
        )
    elif stype == "hdd_only":
        return _pick_uniform_drives(
            storage.get("hdd_options_tb", []),
            storage.get("drives_per_node", 4),
            usable_needed_tb, cluster_layout, "hdd"
        )
    elif stype == "hybrid":
        return _pick_hybrid(storage, usable_needed_tb, cluster_layout, flash_key="ssd")
    elif stype == "hybrid_nvme":
        return _pick_hybrid(storage, usable_needed_tb, cluster_layout, flash_key="nvme")
    elif stype == "nvme_and_ssd":
        return _pick_nvme_and_ssd(storage, usable_needed_tb, cluster_layout)

    return None


def _pick_uniform_drives(size_options, drives_per_node, usable_needed, cluster_layout, drive_type):
    for size in sorted(size_options):
        raw_per_node = size * drives_per_node
        biggest = size
        usable = _cluster_usable_storage(raw_per_node, biggest, cluster_layout)
        if usable >= usable_needed:
            return {
                "raw_per_node": raw_per_node,
                "biggest_disk": biggest,
                "desc": f"{drives_per_node}x {size}TB {drive_type.upper()}",
                f"{drive_type}_tb": size,
            }
    return None


def _pick_hybrid(storage, usable_needed, cluster_layout, flash_key):
    hdd_options = sorted(storage.get("hdd_options_tb", []))
    flash_options = sorted(storage.get(f"{flash_key}_options_tb", []))
    hdd_count = storage.get("hdd_count", 3)
    flash_count = storage.get(f"{flash_key}_count", 1)

    for hdd_tb in hdd_options:
        for flash_tb in flash_options:
            raw_per_node = (hdd_tb * hdd_count) + (flash_tb * flash_count)
            biggest = max(hdd_tb, flash_tb)
            usable = _cluster_usable_storage(raw_per_node, biggest, cluster_layout)
            if usable >= usable_needed:
                return {
                    "raw_per_node": raw_per_node,
                    "biggest_disk": biggest,
                    "desc": f"{hdd_count}x {hdd_tb}TB HDD + {flash_count}x {flash_tb}TB {flash_key.upper()}",
                    "hdd_tb": hdd_tb,
                    f"{flash_key}_tb": flash_tb,
                }
    return None


def _pick_nvme_and_ssd(storage, usable_needed, cluster_layout):
    for nvme in sorted(storage.get("nvme_options_tb", [])):
        for ssd in sorted(storage.get("ssd_options_tb", [])):
            raw_per_node = nvme + ssd
            biggest = max(nvme, ssd)
            usable = _cluster_usable_storage(raw_per_node, biggest, cluster_layout)
            if usable >= usable_needed:
                return {
                    "raw_per_node": raw_per_node,
                    "biggest_disk": biggest,
                    "desc": f"1x {nvme}TB NVMe + 1x {ssd}TB SSD",
                    "nvme_tb": nvme,
                    "ssd_tb": ssd,
                }
    return None
