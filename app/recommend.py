import math
from sqlalchemy.orm import joinedload
from orm_models import (
    Model, StorageConfig, ModelCpuOption, ModelNicOption, StorageConfigDrive,
    DriveTypeIops, SizingSetting,
)
from storage_only import single_cpu_options
from cluster_diagram import network_svg_for


def _rec_network_svg(rec):
    """House-style network diagram SVG for a recommendation: HCI/storage-only
    split + witness (2-node) + NIC ports → topology."""
    so_count = rec["storage_only"]["count"] if rec.get("storage_only") else 0
    hci_count = rec.get("hci_node_count") or (rec.get("node_count", 1) - so_count)
    return network_svg_for(hci_count, so_count, rec.get("nic_ports", 2))
from tunables import T, refresh_from_db

# Map internal storage drive-type tokens to the catalog/IOPS type keys.
DRIVE_TYPE_KEY = {"nvme": "NVMe", "ssd": "SSD", "hdd": "HDD"}

# Sizing/scoring constants (OS overheads, scoring weights, topology limits) are
# admin-tunable and live in tunables.T — read as T.<name> so admin edits take
# effect without a restart. refresh_from_db() (called at the top of
# generate_recommendations) loads the live values for the request.

STORAGE_CATEGORIES = {
    "nvme_only": "flash",
    "ssd_only": "flash",
    "nvme_and_ssd": "flash",
    "hybrid": "hybrid",
    "hybrid_nvme": "hybrid",
    "hdd_only": "spinning",
}


def generate_recommendations(summary, vcpu_ratio=None, growth_pct=10,
                             snapshot_pct=20, years=5, target_nodes=None,
                             storage_pref=None, size_full_cluster=False,
                             sizing_mode="certified", allow_storage_only=False,
                             target_model=None, include_eol_eos=False,
                             max_day_one_storage_pct=None, max_day_one_ram_pct=None):
    # Load the current admin-tuned weights/overheads/limits for this request.
    refresh_from_db()
    if vcpu_ratio is None:
        # Default every sizing to the admin-tuned consolidation ratio rather than
        # the source environment's measured ratio (which is still reported in the
        # summary for reference). The UI slider lets the user dial it up or down.
        vcpu_ratio = T.default_vcpu_ratio
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

    # Max day-one (pre-growth) consumption caps. The cluster is sized so today's
    # load sits at or below this fraction of capacity, on top of the growth /
    # snapshot reserve. Modeled as a capacity FLOOR (capacity >= base_demand /
    # (cap/100)); the engine later sizes to the max of this floor and the
    # projected-demand requirement. Storage is capped against full-cluster usable
    # (RF replication already covers a node loss, so storage N-1 == full); RAM
    # against N-1 available (so the workload still fits with a node down even at
    # the projected end-state). 100% (or blank) effectively disables the cap.
    def _resolve_pct(val, fallback):
        try:
            v = float(val) if val is not None else float(fallback)
        except (TypeError, ValueError):
            v = float(fallback)
        return max(1.0, min(v, 100.0))

    max_day_one_storage_pct = _resolve_pct(max_day_one_storage_pct,
                                           T.max_day_one_storage_pct)
    max_day_one_ram_pct = _resolve_pct(max_day_one_ram_pct, T.max_day_one_ram_pct)

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

    # Day-one capacity floors: the workload must occupy <= cap% of capacity today.
    # Equivalent to requiring capacity >= base_demand / (cap/100). Sizing then
    # gates on max(projected demand, floor) — see min_capacity_* in needs.
    storage_floor_tb = base_storage / (max_day_one_storage_pct / 100)
    ram_floor_gb = base_ram / (max_day_one_ram_pct / 100)

    needs = {
        "vcpus": projected_vcpus,
        "ram_gb": projected_ram,
        "usable_storage_tb": projected_storage,
        # Capacity the fit is gated on per dimension: the larger of projected
        # demand and the day-one consumption floor. usable_storage_tb / ram_gb
        # above stay as projected demand for the utilization bars & headroom.
        "min_capacity_storage_tb": max(projected_storage, storage_floor_tb),
        "min_capacity_ram_gb": max(projected_ram, ram_floor_gb),
        # Current (pre-growth) demand, so the UI can show how much of each
        # utilization bar is today's load vs reserved growth/snapshot headroom.
        "base_vcpus": base_vcpus,
        "base_ram_gb": base_ram,
        "base_storage_tb": base_storage,
        "nic_speed_mbps": summary["nic_speed_mbps"],
        "vcpu_ratio": vcpu_ratio,
        "current_total_ghz": summary.get("total_host_ghz", 0),
        "max_vm_ram_gb": summary.get("max_vm_ram_gb", 0),
        "max_vm_cores": summary.get("max_vm_cores", 0),
        "size_full_cluster": size_full_cluster,
        # Exact node-count target: when set and reachable, _fit_model sizes each
        # model at exactly this many nodes (not its minimum). Larger fallbacks
        # only appear when the target is infeasible.
        "target_nodes": target_nodes,
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
    # Best single-node hosting capacity across the considered (non-cloud) catalog,
    # used to explain an empty result caused by an over-large VM.
    catalog_max_threads = 0
    catalog_max_usable_ram = 0

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
        catalog_max_threads = max(
            catalog_max_threads,
            max((c["threads"] for c in md["cpu_options"]), default=0))
        _md_overhead = T.usable_ram_overhead_for(_bay_count(md["storage"]))
        catalog_max_usable_ram = max(
            catalog_max_usable_ram,
            max((r - _md_overhead for r in md["ram_options_gb"]), default=0))

        fits = _fit_model(md, needs, required_cores, validated=validated,
                          validated_only=md.get("validated_only", False),
                          iops_cfg=iops_cfg,
                          allow_storage_only=allow_storage_only)
        candidates.extend(fits)

    # Ranking: a single right-sizing score (lower = better) that trades total
    # fleet cost against wasted capacity (CPU/RAM/storage over-provisioning),
    # so the engine can pick a slightly larger but tighter/cheaper cluster
    # instead of always minimising node count. IOPS is a hard feasibility gate
    # ahead of the score — configs that can't meet measured demand sort last.
    # See _rank_key and the weight block above it.
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
        # The target is an exact node count: show only N-node configurations.
        # Larger sizes are offered (with a warning) only when N is infeasible.
        exact = [c for c in deduped if c["node_count"] == target_nodes]
        if exact:
            deduped = exact
        else:
            larger = [c for c in deduped if c["node_count"] > target_nodes]
            if larger:
                warnings.append(
                    _target_infeasible_warning(larger, target_nodes, vcpu_ratio, needs)
                )
            else:
                warnings.append(
                    f"No SC// configuration can fit this workload in "
                    f"{target_nodes} node{'s' if target_nodes != 1 else ''}."
                )
            deduped = larger

    if validated and not deduped and not warnings:
        warnings.append(
            "No Validated configuration fits the software-only constraints "
            "(≤100 disks/cluster, 1-or-3+ disks per node, hybrid flash "
            "7–24.3%). Try Certified mode or raise the target node count."
        )

    # Empty result with no more specific reason: usually a single VM too large
    # for any node. Name the offending dimension(s) so the user knows what to fix.
    if not deduped and not warnings:
        mv_cores = needs.get("max_vm_cores", 0)
        mv_ram = needs.get("max_vm_ram_gb", 0)
        reasons = []
        if mv_cores and catalog_max_threads and mv_cores > catalog_max_threads:
            reasons.append(f"its {mv_cores} vCPUs exceed the largest node's "
                           f"{catalog_max_threads} logical processors")
        if mv_ram and catalog_max_usable_ram and mv_ram > catalog_max_usable_ram:
            reasons.append(f"its {mv_ram:.0f} GB RAM exceeds the largest node's "
                           f"{catalog_max_usable_ram:.0f} GB usable RAM")
        if reasons:
            warnings.append(
                "No SC// configuration fits this workload: the largest VM can't "
                "run on any single node — " + "; and ".join(reasons) +
                ". Split or shrink that VM, or add a larger node to the catalog."
            )
        else:
            warnings.append(
                "No SC// configuration fits this workload. Try a higher vCPU:core "
                "ratio or relaxing the storage/node constraints."
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

    # VM density: warn when the headline (top-ranked) recommendation packs more
    # than the admin-tuned ceiling of VMs onto each VM-running node (scheduling/
    # management overhead). Density uses the HCI node count in normal operation;
    # lower-ranked options with more nodes are still available to the user.
    active_vms = summary.get("active_vms", 0)
    if active_vms > 0 and deduped:
        top = deduped[0]
        hci = top.get("hci_node_count", top["node_count"])
        if hci > 0 and active_vms / hci > T.vm_density_warn_per_node:
            warnings.append(
                f"High VM density: the top recommendation runs ~{active_vms / hci:.0f} "
                f"VMs per node ({active_vms} VMs across {hci} nodes, >"
                f"{T.vm_density_warn_per_node}/node). Expect scheduling and "
                f"management overhead; a higher node count spreads the load."
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
        c["network_svg"] = _rec_network_svg(c)

    return {"recommendations": top, "projection": projection, "warnings": warnings}


# ── Right-sizing score ───────────────────────────────────────────────────────
# Candidates are ranked by a single scalar score (lower = better) computed per
# candidate in _fit_model, instead of a lexicographic tuple. A scalar lets a
# large saving in cost or wasted capacity outweigh a small increase in node
# count (and vice-versa) — which a lexicographic ordering, where node count
# alone decides, can never do.
#
#   fleet cost  = node_count × (per-model cost_tier + T.node_overhead)
#   core cost   = total physical HCI cores × T.w_core_license  (per-core licensing)
#   waste       = Σ capped per-dimension over-provisioning (CPU / RAM / storage)
#   ghz penalty = added only when raw cluster GHz drops below the source
#
# All weights (T.w_cost, T.node_overhead, T.w_core_license, T.w_waste, T.w_cpu,
# T.w_ram, T.w_stor, T.waste_cap, T.w_ghz_shortfall) are admin-tunable — see
# tunables.py and the super-admin Tuning page.


def _rank_key(c, needs, required_cores, iops_demand_val):
    """Rank by the single right-sizing score (lower = better) computed per
    candidate in _fit_model — fleet cost + capped capacity waste + GHz
    shortfall. IOPS is a feasibility gate, not a score term: any config that
    cannot meet the measured IOPS demand sorts after every config that can.
    Ties break to the most storage-only offload (fewest HCI nodes for the same
    total), then the smallest CPU for determinism."""
    meets_iops = 0
    if iops_demand_val > 0 and c["iops"]["total"] < iops_demand_val:
        meets_iops = 1
    # Among same-score configs, prefer the one that offloads the most to
    # storage-only nodes (fewest full HCI nodes). With no storage-only split
    # this equals node_count for every candidate, so it has no effect.
    hci_nodes = c.get("hci_node_count", c["node_count"])

    return (
        meets_iops,
        c["score"],
        hci_nodes,
        c["totals"]["cores"],   # final deterministic tiebreak
    )


def _target_infeasible_warning(deduped, target_nodes, vcpu_ratio, needs):
    """Explain why the exact node-count target can't be met, naming the limiting
    resource(s) and — when CPU is the fixable constraint — a vCPU:core ratio that
    would make it fit. `deduped` here is the set of feasible (larger) configs."""
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

    msg = (f"Target of {target_nodes} node{plural} could not be achieved for "
           f"this workload (smallest feasible: {mf} nodes).")
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


def _cluster_layout(total_nodes):
    max_per = T.max_nodes_per_cluster
    if total_nodes <= max_per:
        return [total_nodes]
    num_clusters = math.ceil(total_nodes / max_per)
    base = total_nodes // num_clusters
    remainder = total_nodes % num_clusters
    return [base + 1] * remainder + [base] * (num_clusters - remainder)


def _cluster_usable_storage(raw_per_node, biggest_disk, cluster_sizes):
    total = 0
    for size in cluster_sizes:
        total += (raw_per_node * size - biggest_disk) / 2
    return total


def _hci_split(usable_cores, required_cores, viable_ram_options, ram_need,
               num_clusters, node_count, full_cluster, ram_overhead=None):
    """For a storage-bound config, find the fewest full HCI nodes that still
    satisfy compute + RAM (so the rest can be storage-only). Returns
    (hci_nodes, ram_gb). Enforces >=2 HCI per cluster and at least one compute
    node per cluster at N-1. Falls back to (node_count, None) if no split fits
    — caller then treats it as an all-HCI config."""
    lo = max(T.min_hci_nodes_per_cluster * num_clusters, num_clusters + 1)
    for h in range(lo, node_count + 1):
        comp = h if full_cluster else (h - num_clusters)
        if comp <= 0:
            continue
        if usable_cores * comp < required_cores:
            continue
        ram = _pick_ram(viable_ram_options, ram_need, h, num_clusters, ram_overhead)
        if ram is None:
            continue
        return h, ram
    return node_count, None


def _fit_model(model, needs, required_cores, validated=False, validated_only=False,
               iops_cfg=None, allow_storage_only=False):
    results = []
    iops_cfg = iops_cfg or {"map": {}, "derating_pct": 0.35, "write_amp": 1.3}
    storage = model["storage"]
    # OS RAM overhead is tiered by this node's drive-bay count (SCRIBE scales with
    # drive count); compute it once for the model and use it everywhere below.
    ram_overhead = T.usable_ram_overhead_for(_bay_count(storage))
    # 2-node clusters (with a witness) are supported; per-model minimums (e.g. 3)
    # still win where the hardware requires them.
    min_nodes = max(model.get("min_nodes", 2), 2)

    max_raw, max_biggest = _max_raw_per_node(storage)

    needed_nodes_storage = 0
    if max_raw > 0 and needs["min_capacity_storage_tb"] > 0:
        for n in range(min_nodes, 200):
            layout = _cluster_layout(n)
            usable = _cluster_usable_storage(max_raw, max_biggest, layout)
            if usable >= needs["min_capacity_storage_tb"]:
                needed_nodes_storage = n
                break

    # Filter RAM options: skip any that can't fit the largest VM after overhead
    max_vm_ram = needs.get("max_vm_ram_gb", 0)
    viable_ram_options = model["ram_options_gb"]
    if max_vm_ram > 0:
        viable_ram_options = [r for r in viable_ram_options
                              if (r - ram_overhead) >= max_vm_ram]
    if not viable_ram_options:
        return results

    # A single VM runs on one node, so the largest VM's vCPUs must fit within a
    # node's logical processors (threads). Filter out CPU options that can't host
    # it — the symmetric CPU counterpart to the RAM filter above.
    max_vm_cores = needs.get("max_vm_cores", 0)

    max_ram = max(viable_ram_options)

    needed_nodes_ram = 0
    if max_ram > 0 and needs["min_capacity_ram_gb"] > 0:
        for n in range(min_nodes, 200):
            layout = _cluster_layout(n)
            n1_nodes = n - len(layout)
            if (max_ram - ram_overhead) * n1_nodes >= needs["min_capacity_ram_gb"]:
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
        usable_cores = cores_per_node - T.os_core_overhead

        if cores_per_node == 0 or usable_cores <= 0:
            continue

        # Skip CPUs that can't host the largest single VM (its vCPUs exceed the
        # node's logical processors). If this eliminates every CPU for the model,
        # the model yields no fit — same as the RAM filter.
        if max_vm_cores > 0 and threads_per_node < max_vm_cores:
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

        # Exact node-count target: when the user asks for N nodes and this CPU
        # can serve the workload in exactly N (N >= the minimum feasible), size
        # it at N only. When N is below the minimum (workload too big for N with
        # this CPU), fall back to the normal window so the caller can still
        # surface the smallest feasible larger size and warn that N is impossible.
        target_nodes = needs.get("target_nodes")
        if target_nodes is not None and target_nodes >= start_nodes:
            node_counts = (target_nodes,)
        else:
            node_counts = range(start_nodes, start_nodes + 5)

        for node_count in node_counts:
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
                    needs["min_capacity_ram_gb"], num_clusters, node_count,
                    full_cluster, ram_overhead)
                so_nodes = node_count - hci_nodes
            if ram_gb is None:
                hci_nodes, so_nodes = node_count, 0
                ram_gb = _pick_ram(viable_ram_options, needs["min_capacity_ram_gb"],
                                   node_count, num_clusters, ram_overhead)
            if not ram_gb:
                continue

            # Compute pool spans HCI nodes only; storage spans all nodes.
            compute_n1_nodes = hci_nodes - num_clusters
            n1_usable_cores = usable_cores * compute_n1_nodes
            full_usable_cores = usable_cores * hci_nodes
            cpu_avail = full_usable_cores if full_cluster else n1_usable_cores

            stor = _pick_storage_multi(storage, needs["min_capacity_storage_tb"],
                                       layout, validated=validated)
            if not stor:
                continue

            usable_ram_per_node = ram_gb - ram_overhead

            total_cores = cores_per_node * hci_nodes
            total_threads = threads_per_node * hci_nodes
            total_ghz = ghz_per_node * hci_nodes

            raw_per_node = stor["raw_per_node"]
            biggest_disk = stor["biggest_disk"]
            total_raw = raw_per_node * node_count
            usable = _cluster_usable_storage(raw_per_node, biggest_disk, layout)

            if usable < needs["min_capacity_storage_tb"]:
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

            cost_tier = model.get("cost_tier") or T.default_cost_tier

            # Per-dimension over-provisioning (fraction bought above the
            # requirement), measured on the same basis the fit was gated on
            # (N-1, or the full cluster when sizing for it). Also reused by the
            # determining-factor block below.
            core_headroom = (cpu_avail - required_cores) / required_cores if required_cores > 0 else 0
            ram_headroom = (usable_ram_per_node * compute_n1_nodes - needs["ram_gb"]) / needs["ram_gb"] if needs["ram_gb"] > 0 else 0
            stor_headroom = (usable - needs["usable_storage_tb"]) / needs["usable_storage_tb"] if needs["usable_storage_tb"] > 0 else 0

            # Utilization, expressed against FULL (all-nodes, normal-operation)
            # capacity so the bars reflect day-to-day load, not the tighter N-1
            # basis the fit is gated on. Each resource reports:
            #   current     today's demand,
            #   total       demand after growth + snapshot reserve (workload sized to),
            #   ha_reserve  capacity held for HA failover = the (full - N-1) gap.
            # For CPU/RAM the failover node(s) make ha_reserve > 0; storage usable
            # is the same at N-1 and full, so its ha_reserve is 0. When sizing for
            # the full cluster the failover node isn't held back, so ha_reserve is 0.
            n1_ram = usable_ram_per_node * compute_n1_nodes
            full_ram = usable_ram_per_node * hci_nodes
            full_cores = usable_cores * hci_nodes
            _ratio = needs.get("vcpu_ratio") or 0
            base_required_cores = math.ceil(needs["base_vcpus"] / _ratio) if _ratio else required_cores

            def _u(demand, cap):
                return round(demand / cap * 100) if cap > 0 else 0

            def _ha(full, n1):
                return 0 if full_cluster else (round((full - n1) / full * 100) if full > 0 else 0)

            utilization = {
                "cpu": {"current": _u(base_required_cores, full_cores),
                        "total": _u(required_cores, full_cores),
                        "ha_reserve": _ha(full_cores, n1_usable_cores)},
                "ram": {"current": _u(needs["base_ram_gb"], full_ram),
                        "total": _u(needs["ram_gb"], full_ram),
                        "ha_reserve": _ha(full_ram, n1_ram)},
                "storage": {"current": _u(needs["base_storage_tb"], usable),
                            "total": _u(needs["usable_storage_tb"], usable),
                            "ha_reserve": 0},
            }

            # Right-sizing score (lower = better) — see the weight block above
            # _rank_key. One combined waste term (each dimension capped so a
            # single wildly-oversized axis can't dominate) traded off against
            # total fleet cost, so the ranker can prefer a slightly larger but
            # much tighter / cheaper cluster instead of always minimising nodes.
            waste = (T.w_cpu * min(max(0.0, core_headroom), T.waste_cap)
                     + T.w_ram * min(max(0.0, ram_headroom), T.waste_cap)
                     + T.w_stor * min(max(0.0, stor_headroom), T.waste_cap))
            fleet_cost = node_count * (cost_tier + T.node_overhead)
            # Per-core licensing cost: linear in the total PHYSICAL cores of the
            # HCI (VM-running) nodes — total_cores = cores_per_node × hci_nodes,
            # the physical (not usable) count, which is what core-based licences
            # bill on. Storage-only nodes run no VMs and are excluded. This makes
            # a config with fewer total cores rank ahead of a fewer-node config
            # that packs in more cores.
            core_cost = T.w_core_license * total_cores
            score = T.w_cost * fleet_cost + core_cost + T.w_waste * waste

            # Don't let a high vCPU:core ratio quietly under-power the cluster:
            # penalise raw GHz that falls below the source cluster's total.
            if needs["current_total_ghz"] > 0:
                ghz_ratio = n1_ghz / needs["current_total_ghz"]
                if ghz_ratio < 1.0:
                    score += (1.0 - ghz_ratio) * T.w_ghz_shortfall

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
                "nic_ports": max((o.get("ports", 2) for o in model.get("nic_options", [])), default=2),
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
                "utilization": utilization,
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


def _bay_count(storage):
    """Total drive bays in one node, used to tier the OS RAM overhead (SCRIBE
    scales with drive count). Returns 0 (flat-overhead fallback) for cloud or
    unrecognised storage."""
    stype = storage.get("type", "")
    if stype in ("nvme_only", "ssd_only", "hdd_only"):
        return storage.get("drives_per_node", 0)
    if stype == "hybrid":
        return storage.get("hdd_count", 0) + storage.get("ssd_count", 0)
    if stype == "hybrid_nvme":
        return storage.get("hdd_count", 0) + storage.get("nvme_count", 0)
    if stype == "nvme_and_ssd":
        return 2
    return 0


def _pick_ram(options, total_needed_gb, node_count, num_clusters=1, ram_overhead=None):
    if ram_overhead is None:
        ram_overhead = T.usable_ram_overhead
    n1_nodes = node_count - num_clusters
    for r in sorted(options):
        if (r - ram_overhead) * n1_nodes >= total_needed_gb:
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
            if validated and count * max_cluster_nodes > T.max_cluster_disks:
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
                        if total < 3 or total * max_cluster_nodes > T.max_cluster_disks:
                            continue
                        # HEAT best practice: the slow (HDD) tier must have enough
                        # spindles to absorb cold data evicted from flash, so keep
                        # at least N HDDs per flash disk. Certified models already
                        # encode this; Validated flexes counts, so enforce it here.
                        if h < T.hybrid_min_hdd_per_flash * f:
                            continue
                    raw_per_node = (hdd_tb * h) + (flash_tb * f)
                    # The 7-24.3% flash-capacity band is a hybrid architecture
                    # requirement, not a validated-only one: a fixed certified
                    # tier-count paired with a freely-chosen drive size can still
                    # land out of band (e.g. small HDD + large NVMe -> 56% flash),
                    # so enforce it on every hybrid pick.
                    flash_pct = (flash_tb * f / raw_per_node) * 100 if raw_per_node else 0
                    if flash_pct < T.hybrid_flash_min_pct or flash_pct > T.hybrid_flash_max_pct:
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
