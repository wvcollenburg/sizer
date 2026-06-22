import math
from sqlalchemy.orm import joinedload
from orm_models import (
    Model, StorageConfig, ModelCpuOption, ModelNicOption, StorageConfigDrive,
    DriveTypeIops, SizingSetting,
)
from storage_only import single_cpu_options, MIN_HCI_NODES_PER_CLUSTER

# Map internal storage drive-type tokens to the catalog/IOPS type keys.
DRIVE_TYPE_KEY = {"nvme": "NVMe", "ssd": "SSD", "hdd": "HDD"}

# HyperCore OS overhead per node
OS_CORE_OVERHEAD = 1          # 1 core reserved for HyperCore OS
OS_RAM_GB = 4                 # 4 GB RAM for HyperCore OS
RAM_BUFFER_GB = 2             # 2 GB safety buffer
USABLE_RAM_OVERHEAD = OS_RAM_GB + RAM_BUFFER_GB  # 6 GB total per node


STORAGE_CATEGORIES = {
    "nvme_only": "flash",
    "ssd_only": "flash",
    "nvme_and_ssd": "flash",
    "hybrid": "hybrid",
    "hybrid_nvme": "hybrid",
    "hdd_only": "spinning",
}


MAX_CLUSTER_DISKS = 100      # Software-only hard cap on disks per cluster
HYBRID_FLASH_MIN_PCT = 7.0   # Validated hybrid: flash tier floor (% of raw capacity)
HYBRID_FLASH_MAX_PCT = 24.3  # Validated hybrid: flash tier ceiling


def generate_recommendations(summary, vcpu_ratio=None, growth_pct=10,
                             snapshot_pct=20, years=5, target_nodes=None,
                             storage_pref=None, size_full_cluster=False,
                             sizing_mode="certified", allow_storage_only=False,
                             target_model=None, include_eol_eos=False):
    if vcpu_ratio is None:
        vcpu_ratio = summary.get("vcpu_per_core_ratio", 3.0)
    vcpu_ratio = max(1.0, min(vcpu_ratio, 10.0))
    years = max(1, min(years, 5))

    # Optional hard cap on node count. Blank/0/invalid means "no limit".
    try:
        target_nodes = int(target_nodes) if target_nodes else None
    except (TypeError, ValueError):
        target_nodes = None
    if target_nodes is not None and target_nodes < 1:
        target_nodes = None

    # Optional storage-media preference. Anything else (incl. "auto") = no filter.
    if storage_pref not in ("flash", "hybrid", "spinning"):
        storage_pref = None

    # Size CPU against the full cluster instead of N-1 (degraded on node failure).
    size_full_cluster = bool(size_full_cluster)

    # Opt-in: let storage-bound configs use storage-only nodes (no VMs) beyond
    # the 2-HCI-per-cluster minimum, instead of more full HCI nodes.
    allow_storage_only = bool(allow_storage_only)

    # Optional: size against ONE specific model, and/or include EOL/EOS models
    # (default is Active only). An explicit model wins over the status filter.
    target_model = (target_model or "").strip() or None
    include_eol_eos = bool(include_eol_eos)

    # Software-only ("Validated") sizing reuses the certified Model catalog but
    # lets the engine fit FEWER disks per node than the certified (fully-populated)
    # count — sizing storage to need instead of to the fixed appliance config.
    validated = (sizing_mode == "validated")
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
        "max_vm_ram_gb": summary.get("max_vm_ram_gb", 0),
        "max_vm_cores": summary.get("max_vm_cores", 0),
        "size_full_cluster": size_full_cluster,
    }

    required_cores = math.ceil(needs["vcpus"] / vcpu_ratio)

    # IOPS sizing inputs (admin-configurable). per-type IOPS + cluster adjustments.
    iops_map = {r.drive_type: r.iops for r in DriveTypeIops.query.all()}
    sizing = {s.key: s.value for s in SizingSetting.query.all()}
    derating_pct = sizing.get("iops_derating_pct", 0.35)
    rf = sizing.get("iops_replication_factor", 2)
    read_frac = sizing.get("iops_read_fraction", 0.70)
    write_amp = read_frac + (1 - read_frac) * rf
    iops_cfg = {"map": iops_map, "derating_pct": derating_pct, "write_amp": write_amp}

    model_q = Model.query.options(
        joinedload(Model.cpu_links).joinedload(ModelCpuOption.cpu),
        joinedload(Model.nic_links).joinedload(ModelNicOption.nic),
        joinedload(Model.ram_options),
        joinedload(Model.storage_config)
            .joinedload(StorageConfig.drive_links)
            .joinedload(StorageConfigDrive.drive),
    )
    if target_model:
        # Explicit model selection sizes against exactly that model, regardless
        # of its lifecycle status.
        model_q = model_q.filter(Model.name == target_model)
    elif not include_eol_eos:
        model_q = model_q.filter(Model.status == "Active")
    # Validated-only models have no certified equivalent: exclude them from
    # Certified recommendations; include them only in Validated mode.
    if not validated:
        model_q = model_q.filter(Model.validated_only == False)  # noqa: E712
    models = model_q.all()
    candidates = []
    matched_storage = 0

    for m in models:
        md = m.to_dict()
        md["name"] = m.name
        stype = md["storage"]["type"]
        if stype == "cloud":
            continue
        # nvme_and_ssd is inherently a 1+1 (2-disk) node, which violates the
        # software-only "1 or 3+ disks" rule and cannot be flexed lower.
        if validated and stype == "nvme_and_ssd":
            continue
        if storage_pref and STORAGE_CATEGORIES.get(stype) != storage_pref:
            continue
        matched_storage += 1

        fits = _fit_model(md, needs, required_cores, validated=validated,
                          validated_only=md.get("validated_only", False),
                          iops_cfg=iops_cfg,
                          allow_storage_only=allow_storage_only)
        candidates.extend(fits)

    # Ranking priority (each tier only breaks ties of the previous one):
    #   1) fewest nodes  2) closest CPU match  3) closest IOPS match
    #   4) closest storage match  5) closest RAM match  6) cheapest.
    # "Closest" = least over-provisioning above the requirement. Closeness is
    # bucketed (5% steps) so near-equal fits fall through to the next tier
    # instead of a hair's-width difference dominating.
    p95_iops = summary.get("p95_iops", 0) or 0
    avg_iops = summary.get("total_avg_iops", 0) or 0
    iops_demand_val = p95_iops if p95_iops > 0 else avg_iops
    candidates.sort(key=lambda c: _rank_key(c, needs, required_cores, iops_demand_val))

    seen = set()
    deduped = []
    for c in candidates:
        key = (c["model"], c["node_count"])
        if key not in seen:
            seen.add(key)
            deduped.append(c)

    # Apply the optional node-count ceiling: drop anything that needs more
    # nodes than the user is willing to deploy. If nothing fits within the
    # cap, return no recommendations and explain why (naming the smallest
    # feasible count so the user knows how high to raise the target).
    warnings = []
    if target_model and not candidates:
        warnings.append(
            f"{target_model} cannot meet this workload. Try “All models”, "
            f"a higher vCPU:core ratio, or relaxing the storage/node constraints."
        )
    if storage_pref and matched_storage == 0:
        labels = {"flash": "all-flash", "hybrid": "hybrid",
                  "spinning": "all-spinning (HDD-only)"}
        warnings.append(
            f"No {labels[storage_pref]} configurations are available for this "
            f"workload. Switch the storage preference to Auto to see all options."
        )
    if target_nodes is not None:
        within_cap = [c for c in deduped if c["node_count"] <= target_nodes]
        if not within_cap and deduped:
            warnings.append(
                _target_infeasible_warning(deduped, target_nodes, vcpu_ratio, needs)
            )
        elif not within_cap:
            warnings.append(
                f"No SC// configuration can fit this workload within "
                f"{target_nodes} node{'s' if target_nodes != 1 else ''}."
            )
        deduped = within_cap

    if validated and not deduped and not warnings:
        warnings.append(
            "No Validated configuration fits the software-only constraints "
            "(≤100 disks/cluster, 1-or-3+ disks per node, hybrid flash "
            "7–24.3%). Try Certified mode or raise the target node count."
        )

    # Build warnings — only warn when EVERY recommendation is tight on RAM
    max_vm_ram = needs["max_vm_ram_gb"]
    if max_vm_ram > 0 and deduped:
        all_tight = all(
            (rec["usable_ram_per_node_gb"] > 0 and
             (max_vm_ram / rec["usable_ram_per_node_gb"]) >= 0.9)
            for rec in deduped
        )
        if all_tight:
            worst_pct = max(
                (max_vm_ram / rec["usable_ram_per_node_gb"]) * 100
                for rec in deduped if rec["usable_ram_per_node_gb"] > 0
            )
            warnings.append(
                f"Largest VM ({max_vm_ram:.0f} GB RAM) uses {worst_pct:.0f}%+ of "
                f"usable node RAM across all recommendations. "
                f"Check HA implications."
            )

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

    # Workload IOPS demand (the measured front-end figures — what the workload
    # asks for). Compared against each config's net available IOPS. Metrics with
    # no measured value are omitted.
    iops_demand = {}
    if p95_iops > 0:
        iops_demand["p95"] = round(p95_iops)
    if avg_iops > 0:
        iops_demand["avg"] = round(avg_iops)
    projection["iops_demand"] = iops_demand

    top = deduped[:8]
    for c in top:
        c.pop("_nodes_for_cpu", None)
        c.pop("_nodes_for_ram", None)
        c.pop("_nodes_for_storage", None)

    return {"recommendations": top, "projection": projection, "warnings": warnings}


RANK_BUCKET = 0.05   # 5% closeness buckets for the ranking tiers


def _bucket(over_provision):
    """Quantise an over-provisioning fraction so near-equal fits tie and fall
    through to the next ranking tier. Negative (shouldn't happen for met
    requirements) clamps to 0."""
    return round(max(0.0, over_provision) / RANK_BUCKET)


def _rank_key(c, needs, required_cores, iops_demand_val):
    """Lexicographic ranking: fewest total nodes, then (when storage-only is in
    play) fewest HCI nodes so compute is concentrated on the top CPU and the
    rest offloaded to cheap storage-only nodes, then closest CPU / IOPS /
    storage / RAM match (least over-provisioning), then cheapest. Lower sorts
    first."""
    full = c.get("sized_full_cluster")
    cpu_avail = c["totals"]["cores"] if full else c["n_minus_1"]["cores"]
    cpu_close = (cpu_avail - required_cores) / required_cores if required_cores > 0 else 0
    # Maximise storage-only offload: among configs with the same total node
    # count, prefer the one with the fewest full HCI nodes (achieved by the
    # most capable CPU). With no storage-only split this equals node_count for
    # every candidate, so it has no effect.
    hci_nodes = c.get("hci_node_count", c["node_count"])

    if iops_demand_val > 0:
        net = c["iops"]["total"]
        if net >= iops_demand_val:
            iops_close = (net - iops_demand_val) / iops_demand_val
        else:
            # Configs that don't meet demand rank after all that do.
            iops_close = 1000 + (iops_demand_val - net) / iops_demand_val
    else:
        iops_close = 0

    need_stor = needs["usable_storage_tb"]
    stor_close = (c["totals"]["usable_storage_tb"] - need_stor) / need_stor if need_stor > 0 else 0
    need_ram = needs["ram_gb"]
    ram_close = (c["n_minus_1"]["ram_gb"] - need_ram) / need_ram if need_ram > 0 else 0

    return (
        c["node_count"],
        hci_nodes,
        _bucket(cpu_close),
        _bucket(iops_close) if iops_close < 1000 else 1_000_000 + round(iops_close),
        _bucket(stor_close),
        _bucket(ram_close),
        c["cost_tier"],
        c["totals"]["cores"],   # final deterministic tiebreak
    )


def _target_infeasible_warning(deduped, target_nodes, vcpu_ratio, needs):
    """Explain why no config fits within the target node cap, naming the
    limiting resource(s) and — when CPU is the fixable constraint — a vCPU:core
    ratio that would make it fit."""
    plural = "s" if target_nodes != 1 else ""
    best = min(deduped, key=lambda c: c["node_count"])
    mf = best["node_count"]

    # A resource is "binding" for the closest-to-target config when it alone
    # drives that config's node count.
    binding = []
    if best["_nodes_for_cpu"] >= mf:
        binding.append(f"CPU cores (≥{best['_nodes_for_cpu']} nodes "
                       f"at {vcpu_ratio:g}:1 vCPU:core)")
    if best["_nodes_for_ram"] >= mf:
        binding.append(f"RAM (≥{best['_nodes_for_ram']} nodes)")
    if best["_nodes_for_storage"] >= mf:
        binding.append(f"storage capacity (≥{best['_nodes_for_storage']} nodes)")

    msg = (f"No SC// configuration fits this workload within "
           f"{target_nodes} node{plural} (smallest feasible: {mf} nodes).")
    if binding:
        msg += " Limiting factor: " + "; ".join(binding) + "."

    # CPU is fixable by a higher ratio only if some config already meets RAM and
    # storage within the cap, with cores the sole resource pushing it over.
    # The core pool at the target is the full cluster (full-cluster sizing) or
    # N-1 (default), matching how _fit_model gated the CPU fit.
    layout = _cluster_layout(target_nodes)
    cpu_nodes = target_nodes if needs.get("size_full_cluster") else target_nodes - len(layout)
    if cpu_nodes > 0:
        fixable = [c for c in deduped
                   if c["_nodes_for_cpu"] > target_nodes
                   and c["_nodes_for_ram"] <= target_nodes
                   and c["_nodes_for_storage"] <= target_nodes]
        if fixable:
            needed = min(needs["vcpus"] / (c["usable_cores_per_node"] * cpu_nodes)
                         for c in fixable)
            suggested = math.ceil(needed * 4) / 4   # round up to a 0.25 step
            if suggested <= 8:                      # within the ratio slider range
                msg += (f" Raising the vCPU:core ratio to {suggested:g}:1 "
                        f"would allow a fit within {target_nodes} node{plural}.")
            else:
                msg += (" Increase the target node count — the vCPU:core ratio "
                        "alone cannot close the gap.")
        else:
            msg += " Increase the target node count to proceed."

    return msg


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


def _hci_split(usable_cores, required_cores, viable_ram_options, ram_need,
               num_clusters, node_count, full_cluster):
    """For a storage-bound config, find the fewest full HCI nodes that still
    satisfy compute + RAM (so the rest can be storage-only). Returns
    (hci_nodes, ram_gb). Enforces >=2 HCI per cluster and at least one compute
    node per cluster at N-1. Falls back to (node_count, None) if no split fits
    — caller then treats it as an all-HCI config."""
    lo = max(MIN_HCI_NODES_PER_CLUSTER * num_clusters, num_clusters + 1)
    for h in range(lo, node_count + 1):
        comp = h if full_cluster else (h - num_clusters)
        if comp <= 0:
            continue
        if usable_cores * comp < required_cores:
            continue
        ram = _pick_ram(viable_ram_options, ram_need, h, num_clusters)
        if ram is None:
            continue
        return h, ram
    return node_count, None


def _fit_model(model, needs, required_cores, validated=False, validated_only=False,
               iops_cfg=None, allow_storage_only=False):
    results = []
    iops_cfg = iops_cfg or {"map": {}, "derating_pct": 0.35, "write_amp": 1.3}
    storage = model["storage"]
    # 2-node clusters (with a witness) are supported; per-model minimums (e.g. 3)
    # still win where the hardware requires them.
    min_nodes = max(model.get("min_nodes", 2), 2)

    max_raw, max_biggest = _max_raw_per_node(storage)

    needed_nodes_storage = 0
    if max_raw > 0 and needs["usable_storage_tb"] > 0:
        for n in range(min_nodes, 200):
            layout = _cluster_layout(n)
            usable = _cluster_usable_storage(max_raw, max_biggest, layout)
            if usable >= needs["usable_storage_tb"]:
                needed_nodes_storage = n
                break

    # Filter RAM options: skip any that can't fit the largest VM after overhead
    max_vm_ram = needs.get("max_vm_ram_gb", 0)
    viable_ram_options = model["ram_options_gb"]
    if max_vm_ram > 0:
        viable_ram_options = [r for r in viable_ram_options
                              if (r - USABLE_RAM_OVERHEAD) >= max_vm_ram]
    if not viable_ram_options:
        return results

    max_ram = max(viable_ram_options)

    needed_nodes_ram = 0
    if max_ram > 0 and needs["ram_gb"] > 0:
        for n in range(min_nodes, 200):
            layout = _cluster_layout(n)
            n1_nodes = n - len(layout)
            if (max_ram - USABLE_RAM_OVERHEAD) * n1_nodes >= needs["ram_gb"]:
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
        usable_cores = cores_per_node - OS_CORE_OVERHEAD

        if cores_per_node == 0 or usable_cores <= 0:
            continue

        full_cluster = needs.get("size_full_cluster", False)

        needed_nodes_cpu = 0
        for n in range(min_nodes, 200):
            layout = _cluster_layout(n)
            n1 = n - len(layout)
            # When sizing for the full cluster, all nodes carry load; otherwise
            # the workload must fit with one node down per cluster (N-1).
            cpu_nodes = n if full_cluster else n1
            if usable_cores * cpu_nodes >= required_cores:
                needed_nodes_cpu = n
                break

        start_nodes = max(min_nodes, needed_nodes_cpu,
                          needed_nodes_storage, needed_nodes_ram)

        for node_count in range(start_nodes, start_nodes + 5):
            layout = _cluster_layout(node_count)
            num_clusters = len(layout)
            n1_nodes = node_count - num_clusters
            full_usable_cores_all = usable_cores * node_count
            n1_usable_cores_all = usable_cores * n1_nodes
            cpu_avail_all = full_usable_cores_all if full_cluster else n1_usable_cores_all

            # The all-HCI config must still meet compute (storage-only only ever
            # reduces the compute pool, so this is the easy upper bound).
            if cpu_avail_all < required_cores:
                continue

            # Storage-only split: keep the fewest HCI nodes that satisfy compute
            # + RAM (>=2 per cluster); the remainder become storage-only nodes.
            # Storage capacity and IOPS still span ALL nodes.
            so_nodes = 0
            hci_nodes = node_count
            ram_gb = None
            if allow_storage_only:
                hci_nodes, ram_gb = _hci_split(
                    usable_cores, required_cores, viable_ram_options,
                    needs["ram_gb"], num_clusters, node_count, full_cluster)
                so_nodes = node_count - hci_nodes
            if ram_gb is None:
                hci_nodes, so_nodes = node_count, 0
                ram_gb = _pick_ram(viable_ram_options, needs["ram_gb"],
                                   node_count, num_clusters)
            if not ram_gb:
                continue

            # Compute pool spans HCI nodes only; storage spans all nodes.
            compute_n1_nodes = hci_nodes - num_clusters
            n1_usable_cores = usable_cores * compute_n1_nodes
            full_usable_cores = usable_cores * hci_nodes
            cpu_avail = full_usable_cores if full_cluster else n1_usable_cores

            stor = _pick_storage_multi(storage, needs["usable_storage_tb"],
                                       layout, validated=validated)
            if not stor:
                continue

            usable_ram_per_node = ram_gb - USABLE_RAM_OVERHEAD

            total_cores = cores_per_node * hci_nodes
            total_threads = threads_per_node * hci_nodes
            total_ghz = ghz_per_node * hci_nodes
            total_ram = ram_gb * hci_nodes

            raw_per_node = stor["raw_per_node"]
            biggest_disk = stor["biggest_disk"]
            total_raw = raw_per_node * node_count
            usable = _cluster_usable_storage(raw_per_node, biggest_disk, layout)

            if usable < needs["usable_storage_tb"]:
                continue

            # Per-node IOPS: raw drive IOPS → derate for cluster overhead → divide
            # by RF write-amplification to get the NET IOPS available to workloads
            # (the only figure surfaced in the UI). The raw/derated/write-amp
            # detail is retained for the PPTX export only.
            write_amp = iops_cfg.get("write_amp", 1.0) or 1.0
            iops_raw_per_node = sum(
                cnt * iops_cfg["map"].get(dtype, 0)
                for dtype, cnt in stor.get("drive_counts", {}).items()
            )
            iops_derated_per_node = iops_raw_per_node * (1 - iops_cfg["derating_pct"])
            iops_net_per_node = round(iops_derated_per_node / write_amp)
            iops_block = {
                # Headline: net IOPS available to workloads.
                "per_node": iops_net_per_node,
                "total": iops_net_per_node * node_count,
                "n_minus_1": iops_net_per_node * n1_nodes,
                # Derivation detail (PPTX export only):
                "raw_per_node": round(iops_raw_per_node),
                "derated_per_node": round(iops_derated_per_node),
                "derating_pct": round(iops_cfg["derating_pct"] * 100, 1),
                "write_amp": round(write_amp, 3),
            }

            n1_ghz = ghz_per_node * compute_n1_nodes
            # Operational ratio is measured against the sizing basis; the degraded
            # ratio is always the N-1 case (what you'd run at during a failure).
            n1_ratio = needs["vcpus"] / n1_usable_cores if n1_usable_cores > 0 else 99
            full_ratio = needs["vcpus"] / full_usable_cores if full_usable_cores > 0 else 99
            rec_ratio = full_ratio if full_cluster else n1_ratio

            cost_tier = _model_cost_tier(model["name"])
            excess_cores = usable_cores * hci_nodes - required_cores

            score = node_count * 20
            score += cost_tier * 6
            score += excess_cores * 0.3

            core_headroom = (cpu_avail - required_cores) / required_cores if required_cores > 0 else 0
            ram_headroom = (usable_ram_per_node * compute_n1_nodes - needs["ram_gb"]) / needs["ram_gb"] if needs["ram_gb"] > 0 else 0
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

            # Storage-only nodes (only when the split actually moved nodes off
            # the compute pool). Same model/drives, single lowest-tier CPU, the
            # model's compliant minimum RAM.
            storage_only_block = None
            if so_nodes > 0:
                # Validated (software-only) may derive a single CPU from any
                # platform; Certified must use real single-CPU SKUs only (the
                # model's own or a single-socket sibling, precomputed in
                # storage_only_cpu_options).
                if validated:
                    so_cpu_opts = single_cpu_options(model["cpu_options"])
                else:
                    so_cpu_opts = (model.get("storage_only_cpu_options")
                                   or model["cpu_options"])
                so_cpu = so_cpu_opts[0] if so_cpu_opts else None
                so_ram_gb = min(model["ram_options_gb"]) if model.get("ram_options_gb") else 16
                storage_only_block = {
                    "count": so_nodes,
                    "cpu": so_cpu["desc"] if so_cpu else "",
                    "cores": so_cpu["cores"] if so_cpu else 0,
                    "threads": so_cpu["threads"] if so_cpu else 0,
                    "ghz": so_cpu["ghz"] if so_cpu else 0,
                    "ram_gb": so_ram_gb,
                    "raw_storage_tb": round(raw_per_node, 2),
                }

            # Determining factor: which resource drove the node count (the one
            # needing the most nodes). Ties break to the tightest headroom. If
            # nothing exceeds the cluster minimum, the floor itself is the driver.
            _det_nodes = {"CPU": needed_nodes_cpu, "RAM": needed_nodes_ram,
                          "Storage": needed_nodes_storage}
            _binding = max(_det_nodes.values())
            if _binding <= min_nodes:
                determinant = {"resource": "minimum", "required": None,
                               "achieved": None, "unit": None, "headroom_pct": None}
            else:
                _hr = {"CPU": core_headroom, "RAM": ram_headroom,
                       "Storage": stor_headroom}
                _res = min((r for r, n in _det_nodes.items() if n == _binding),
                           key=lambda r: _hr[r])
                _vals = {
                    "CPU": (required_cores, cpu_avail, "cores"),
                    "RAM": (needs["ram_gb"], usable_ram_per_node * compute_n1_nodes, "GB"),
                    "Storage": (needs["usable_storage_tb"], usable, "TB"),
                }
                _req, _ach, _unit = _vals[_res]
                determinant = {
                    "resource": _res,
                    "required": round(_req, 1),
                    "achieved": round(_ach, 1),
                    "unit": _unit,
                    "headroom_pct": round(_hr[_res] * 100, 1),
                }

            results.append({
                "model": model["name"],
                "category": model["category"],
                "form_factor": model["form_factor"],
                "chassis": model["chassis"],
                "cost_tier": cost_tier,
                "node_count": node_count,
                "hci_node_count": hci_nodes,
                "storage_only": storage_only_block,
                "num_clusters": num_clusters,
                "cluster_layout": layout,
                "cpu": cpu["desc"],
                "cpu_index": cpu_idx,
                "cores_per_node": cores_per_node,
                "usable_cores_per_node": usable_cores,
                "threads_per_node": threads_per_node,
                "ghz": cpu["ghz"],
                "ram_per_node_gb": ram_gb,
                "usable_ram_per_node_gb": usable_ram_per_node,
                "storage_config": stor,
                "vcpu_ratio": round(rec_ratio, 2),
                "vcpu_ratio_degraded": round(n1_ratio, 2),
                "sized_full_cluster": full_cluster,
                "determinant": determinant,
                "validated": validated,
                "validated_only": validated_only,
                "iops": iops_block,
                "totals": {
                    "cores": usable_cores * hci_nodes,
                    "threads": total_threads,
                    "total_ghz": round(total_ghz, 1),
                    "ram_gb": round(usable_ram_per_node * hci_nodes, 1),
                    "raw_storage_tb": round(total_raw, 2),
                    "usable_storage_tb": round(usable, 2),
                },
                "n_minus_1": {
                    "cores": n1_usable_cores,
                    "threads": threads_per_node * compute_n1_nodes,
                    "total_ghz": round(n1_ghz, 1),
                    "ram_gb": round(usable_ram_per_node * compute_n1_nodes, 1),
                    "usable_storage_tb": round(usable, 2),
                },
                "score": round(score, 2),
                # Per-resource minimum node counts (internal; used to explain
                # why a target-node cap can't be met). Stripped before return.
                "_nodes_for_cpu": needed_nodes_cpu,
                "_nodes_for_ram": needed_nodes_ram,
                "_nodes_for_storage": needed_nodes_storage,
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
        if (r - USABLE_RAM_OVERHEAD) * n1_nodes >= total_needed_gb:
            return r
    return None


def _pick_storage_multi(storage, usable_needed_tb, cluster_layout, validated=False):
    stype = storage["type"]

    if stype == "nvme_only":
        return _pick_uniform_drives(
            storage.get("nvme_options_tb", []),
            storage.get("drives_per_node", 1),
            usable_needed_tb, cluster_layout, "nvme", validated
        )
    elif stype == "ssd_only":
        return _pick_uniform_drives(
            storage.get("ssd_options_tb", []),
            storage.get("drives_per_node", 4),
            usable_needed_tb, cluster_layout, "ssd", validated
        )
    elif stype == "hdd_only":
        return _pick_uniform_drives(
            storage.get("hdd_options_tb", []),
            storage.get("drives_per_node", 4),
            usable_needed_tb, cluster_layout, "hdd", validated
        )
    elif stype == "hybrid":
        return _pick_hybrid(storage, usable_needed_tb, cluster_layout, "ssd", validated)
    elif stype == "hybrid_nvme":
        return _pick_hybrid(storage, usable_needed_tb, cluster_layout, "nvme", validated)
    elif stype == "nvme_and_ssd":
        # Excluded upstream in Validated mode; only reachable for Certified.
        return _pick_nvme_and_ssd(storage, usable_needed_tb, cluster_layout)

    return None


def _validated_disk_counts(certified_count):
    """Valid per-node disk counts when flexing down from a fully-populated
    certified node: 1, or 3..certified_count. Never 2, never above certified."""
    counts = [n for n in range(3, certified_count + 1)]
    if certified_count >= 1:
        counts.append(1)
    return sorted(set(counts))


def _pick_uniform_drives(size_options, drives_per_node, usable_needed,
                         cluster_layout, drive_type, validated=False):
    # The 100-disk cap is per CLUSTER, so it binds on the largest cluster in the
    # layout — not the total node count across clusters.
    max_cluster_nodes = max(cluster_layout)
    # Certified: fixed count, smallest size that fits. Validated: also flex the
    # count down (1 or 3+, never above certified), picking the closest fit.
    counts = _validated_disk_counts(drives_per_node) if validated else [drives_per_node]

    best = None
    for size in sorted(size_options):
        for count in counts:
            if validated and count * max_cluster_nodes > MAX_CLUSTER_DISKS:
                continue
            raw_per_node = size * count
            usable = _cluster_usable_storage(raw_per_node, size, cluster_layout)
            if usable < usable_needed:
                continue
            cand = {
                "raw_per_node": raw_per_node,
                "biggest_disk": size,
                "desc": f"{count}x {size}TB {drive_type.upper()}",
                f"{drive_type}_tb": size,
                "drive_counts": {DRIVE_TYPE_KEY[drive_type]: count},
                "_usable": usable,
                "_disks": count,
            }
            if not validated:
                return _strip_pick(cand)
            # Closest fit: least usable; tie-break fewer disks, smaller size.
            key = (usable, count, size)
            if best is None or key < best[0]:
                best = (key, cand)
    return _strip_pick(best[1]) if best else None


def _pick_hybrid(storage, usable_needed, cluster_layout, flash_key, validated=False):
    hdd_options = sorted(storage.get("hdd_options_tb", []))
    flash_options = sorted(storage.get(f"{flash_key}_options_tb", []))
    hdd_count = storage.get("hdd_count", 3)
    flash_count = storage.get(f"{flash_key}_count", 1)
    # Per-cluster disk cap binds on the largest cluster, not the total.
    max_cluster_nodes = max(cluster_layout)

    # Certified: fixed tier counts. Validated: flex both tiers down (never above
    # certified), keeping total disks valid (3+), the cluster cap, and the
    # 7-24.3% flash-capacity band.
    if validated:
        hdd_counts = list(range(1, hdd_count + 1))
        flash_counts = list(range(1, flash_count + 1))
    else:
        hdd_counts = [hdd_count]
        flash_counts = [flash_count]

    best = None
    for hdd_tb in hdd_options:
        for flash_tb in flash_options:
            for h in hdd_counts:
                for f in flash_counts:
                    if validated:
                        total = h + f
                        if total < 3 or total * max_cluster_nodes > MAX_CLUSTER_DISKS:
                            continue
                    raw_per_node = (hdd_tb * h) + (flash_tb * f)
                    # The 7-24.3% flash-capacity band is a hybrid architecture
                    # requirement, not a validated-only one: a fixed certified
                    # tier-count paired with a freely-chosen drive size can still
                    # land out of band (e.g. small HDD + large NVMe -> 56% flash),
                    # so enforce it on every hybrid pick.
                    flash_pct = (flash_tb * f / raw_per_node) * 100 if raw_per_node else 0
                    if flash_pct < HYBRID_FLASH_MIN_PCT or flash_pct > HYBRID_FLASH_MAX_PCT:
                        continue
                    biggest = max(hdd_tb, flash_tb)
                    usable = _cluster_usable_storage(raw_per_node, biggest, cluster_layout)
                    if usable < usable_needed:
                        continue
                    cand = {
                        "raw_per_node": raw_per_node,
                        "biggest_disk": biggest,
                        "desc": f"{h}x {hdd_tb}TB HDD + {f}x {flash_tb}TB {flash_key.upper()}",
                        "hdd_tb": hdd_tb,
                        f"{flash_key}_tb": flash_tb,
                        "drive_counts": {"HDD": h, DRIVE_TYPE_KEY[flash_key]: f},
                        "_usable": usable,
                        "_disks": h + f,
                    }
                    if not validated:
                        return _strip_pick(cand)
                    key = (usable, h + f, biggest)
                    if best is None or key < best[0]:
                        best = (key, cand)
    return _strip_pick(best[1]) if best else None


def _strip_pick(pick):
    pick.pop("_usable", None)
    pick.pop("_disks", None)
    return pick


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
                    "drive_counts": {"NVMe": 1, "SSD": 1},
                }
    return None
