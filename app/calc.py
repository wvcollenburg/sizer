"""Cluster sizing math for the appliance ("Certified") and validated
("software-only") calculators.

Extracted from app.py so the sizing engine lives apart from the HTTP routes:
saved sizings are fingerprinted against a content hash of the engine modules
(docs/projects-plan.md §3.2), and hashing app.py would invalidate every stored
result whenever an unrelated route changed.

Pure functions over request dicts plus the model catalog — no Flask request
state, so they can be called from a route, a worker, or a test.
"""
from orm_models import Model
from tunables import T
from recommend import _cluster_layout, _cluster_usable_storage
from cluster_diagram import network_svg_for

# Ceiling on user-supplied storage-only node counts (mirrors MAX_NODE_COUNT in
# app.py, which clamps the HCI node count at the route).
MAX_STORAGE_ONLY_COUNT = 1000

# Shown (GUI + PPTX) in place of the N-1 block for a Single Node System, which
# has no peer to fail over to. Surfaced via the response so both renderers stay
# in sync on wording.
SNS_NO_REDUNDANCY_MSG = (
    "No redundancy — a single-node system cannot tolerate a node failure. "
    "Ensure workloads are protected with replication or a properly configured backup."
)


def _cluster_min_hci_error(node_count, so_count, layout):
    """Multi-cluster or storage-only builds need >=2 full HCI nodes per cluster
    (HA + rolling updates), so the HCI floor scales with the cluster count.
    Returns an error dict if that floor isn't met, else None. Shared by the
    appliance and validated calculators."""
    num_clusters = len(layout)
    min_hci = T.min_hci_nodes_per_cluster * num_clusters
    if (so_count > 0 or num_clusters > 1) and node_count < min_hci:
        plural = "s" if num_clusters != 1 else ""
        return {"error": (
            f"{num_clusters} cluster{plural} ({' + '.join(map(str, layout))} nodes, "
            f"max {T.max_nodes_per_cluster} per cluster) require at least {min_hci} full "
            f"HCI nodes — 2 per cluster for HA and rolling updates. You have {node_count}."
        )}
    return None


def _n_minus_1_block(node_count, num_clusters, cores, threads, ghz, ram, usable_tb):
    """Surviving capacity with one HCI node offline per cluster. For a single
    node there's no peer to fail over to, so the node's own full capacity is
    reported (the GUI/PPTX label it as no-redundancy separately)."""
    n1_hci = max(node_count - num_clusters, 0)
    mult = n1_hci if node_count > 1 else 1
    return {
        "cores": cores * mult,
        "threads": threads * mult,
        "total_ghz": round(ghz * mult, 2),
        "ram_gb": ram * mult,
        "usable_storage_tb": round(usable_tb, 2),
    }


def calculate_appliance(data, node_count):
    model_name = data.get("model")
    m = Model.query.filter_by(name=model_name).first()
    if not m:
        return {"error": "Invalid model"}

    model = m.to_dict()
    min_nodes = model.get("min_nodes", 1)
    if node_count < min_nodes:
        return {"error": f"Minimum {min_nodes} nodes required for {model_name}"}

    cpu_idx = data.get("cpu_index", 0)
    if cpu_idx >= len(model["cpu_options"]):
        return {"error": "Invalid CPU selection"}
    cpu = model["cpu_options"][cpu_idx]

    ram_gb = data.get("ram_gb", model["ram_options_gb"][0])
    if ram_gb not in model["ram_options_gb"]:
        return {"error": "Invalid RAM selection"}

    storage = model["storage"]
    raw_per_node = compute_raw_per_node_appliance(data, storage)
    if isinstance(raw_per_node, dict) and "error" in raw_per_node:
        return raw_per_node

    biggest_disk = compute_biggest_disk_appliance(data, storage)

    # Optional storage-only nodes: same model/drives, virtualization disabled.
    # They add raw capacity (and disks) to the cluster but no usable compute.
    so_block, so_err = _appliance_storage_only(model, data, raw_per_node, node_count)
    if so_err:
        return so_err
    so_count = so_block["count"] if so_block else 0
    total_nodes = node_count + so_count

    # A HyperCore cluster holds at most 8 nodes, so larger builds split into
    # several clusters (the HCI floor scales with the cluster count).
    layout = _cluster_layout(total_nodes)
    num_clusters = len(layout)
    hci_err = _cluster_min_hci_error(node_count, so_count, layout)
    if hci_err:
        return hci_err

    total_raw = raw_per_node * total_nodes
    if total_nodes > 1:
        usable = _cluster_usable_storage(raw_per_node, biggest_disk, layout)
    else:
        # Single Node System. A hybrid SNS must mirror each tier within the one
        # node, which needs >=2 disks of every type; a 3+1 layout is out of scope.
        sns_err = _sns_storage_error(storage, model_name)
        if sns_err:
            return sns_err
        # RF2 still mirrors across the node's own drives (usable = raw/2), but
        # reserves no rebuild disk — there's no peer node to rebuild onto, so the
        # largest-disk reserve that multi-node clusters hold back doesn't apply. A
        # single-disk SNS (e.g. HE153) can't mirror at all, so its raw capacity is
        # fully usable.
        usable = (raw_per_node if compute_drive_count_appliance(data, storage) <= 1
                  else raw_per_node / 2)

    # Apply HyperCore OS overhead. Compute capacity comes from the HCI nodes only.
    # OS RAM overhead is tiered by this node's drive-bay count.
    usable_cores = cpu["cores"] - T.os_core_overhead
    usable_ram = ram_gb - T.usable_ram_overhead_for(
        compute_drive_count_appliance(data, storage))

    total_cores = usable_cores * node_count
    total_threads = cpu["threads"] * node_count
    total_ghz = cpu["ghz"] * cpu["cores"] * node_count
    total_ram = usable_ram * node_count

    n_minus_1 = _n_minus_1_block(node_count, num_clusters, usable_cores,
                                 cpu["threads"], cpu["ghz"] * cpu["cores"],
                                 usable_ram, usable)

    _nic_ports = max((o.get("ports", 2) for o in model.get("nic_options", [])), default=2)
    network_svg = network_svg_for(node_count, so_block["count"] if so_block else 0, _nic_ports)

    return {
        "mode": "appliance",
        "model": model_name,
        # Identity of the catalog rows this result was computed from, so a
        # stored snapshot can be re-checked against the live catalog later
        # (docs/projects-plan.md §3.2). Identity only — never the resolved
        # values, which would make the staleness check self-referential.
        "refs": {
            "mode": "appliance",
            "model": model_name,
            "cpu_desc": cpu.get("desc"),
            "so_cpu_desc": (so_block or {}).get("cpu"),
            "selection": {
                "ram_gb": ram_gb,
                "hdd_tb": data.get("hdd_tb"),
                "ssd_tb": data.get("ssd_tb"),
                "nvme_tb": data.get("nvme_tb"),
                "node_count": node_count,
                "so_count": so_count,
            },
        },
        "node_count": node_count,
        "total_node_count": total_nodes,
        "num_clusters": num_clusters,
        "cluster_layout": layout,
        "storage_only": so_block,
        "network_svg": network_svg,
        # Carry the port count so the exporters' diagram regeneration
        # (_rec_network_svg) matches the on-screen SVG instead of defaulting to 2.
        "nic_ports": _nic_ports,
        "per_node": {
            "cpu": cpu["desc"],
            "cores": usable_cores,
            "threads": cpu["threads"],
            "ghz": cpu["ghz"],
            "ram_gb": usable_ram,
            "raw_storage_tb": round(raw_per_node, 2),
        },
        "cluster_total": {
            "cores": total_cores,
            "threads": total_threads,
            "total_ghz": round(total_ghz, 2),
            "ram_gb": total_ram,
            "raw_storage_tb": round(total_raw, 2),
            "usable_storage_tb": round(usable, 2),
        },
        "n_minus_1": n_minus_1,
        "single_node": total_nodes == 1,
        "redundancy_note": SNS_NO_REDUNDANCY_MSG if total_nodes == 1 else None,
        "form_factor": model["form_factor"],
        "chassis": model["chassis"],
        "status": model["status"],
    }


def _appliance_storage_only(model, data, raw_per_node, hci_count):
    """Build the storage-only-node block for an appliance config, or (None, None)
    when none requested. Returns (block, error_dict). Storage-only nodes reuse
    the model's drives (so raw_per_node is shared), take a single lowest-tier CPU
    and a compliant RAM option, and require >=2 full HCI nodes in the cluster."""
    so = data.get("storage_only") or {}
    try:
        count = int(so.get("count", 0) or 0)
    except (TypeError, ValueError):
        return None, None
    if count <= 0:
        return None, None
    if count > MAX_STORAGE_ONLY_COUNT:
        return None, {"error": f"Storage-only node count must be {MAX_STORAGE_ONLY_COUNT} or fewer"}
    if hci_count < T.min_hci_nodes_per_cluster:
        return None, {"error": (
            f"At least {T.min_hci_nodes_per_cluster} full HCI nodes are required "
            f"when adding storage-only nodes (for HA and rolling updates)."
        )}

    # Certified: real single-CPU SKUs only (sibling model) — falls back to the
    # model's own CPUs (dual when no single sibling exists). Never fabricated.
    cpu_opts = model.get("storage_only_cpu_options") or model["cpu_options"]
    if not cpu_opts:
        return None, {"error": "No storage-only CPU option for this model."}
    ci = int(so.get("cpu_index", 0) or 0)
    if ci < 0 or ci >= len(cpu_opts):
        return None, {"error": "Invalid storage-only CPU selection"}
    cpu = cpu_opts[ci]

    ram_options = model["ram_options_gb"]
    # Certified: the compliant minimum is the model's smallest RAM option (often
    # >16 GB). Editable upward, but only to a real model option.
    ram_gb = so.get("ram_gb", ram_options[0] if ram_options else T.storage_only_ram_floor_gb)
    if ram_options and ram_gb not in ram_options:
        return None, {"error": "Invalid storage-only RAM selection"}

    return {
        "count": count,
        "cpu": cpu["desc"],
        "cpu_index": ci,
        "cores": cpu["cores"],
        "threads": cpu["threads"],
        "ghz": cpu["ghz"],
        "ram_gb": ram_gb,
        "raw_storage_tb": round(raw_per_node, 2),
    }, None


def compute_raw_per_node_appliance(data, storage):
    stype = storage["type"]
    if stype == "nvme_only":
        nvme_tb = data.get("nvme_tb", storage["nvme_options_tb"][0])
        count = storage.get("drives_per_node", 1)
        return nvme_tb * count
    elif stype == "ssd_only":
        ssd_tb = data.get("ssd_tb", storage["ssd_options_tb"][0])
        count = storage.get("drives_per_node", 4)
        return ssd_tb * count
    elif stype == "hdd_only":
        hdd_tb = data.get("hdd_tb", storage["hdd_options_tb"][0])
        count = storage.get("drives_per_node", 4)
        return hdd_tb * count
    elif stype == "hybrid":
        hdd_tb = data.get("hdd_tb", storage["hdd_options_tb"][0])
        ssd_tb = data.get("ssd_tb", storage["ssd_options_tb"][0])
        return (hdd_tb * storage["hdd_count"]) + (ssd_tb * storage["ssd_count"])
    elif stype == "hybrid_nvme":
        hdd_tb = data.get("hdd_tb", storage["hdd_options_tb"][0])
        nvme_tb = data.get("nvme_tb", storage["nvme_options_tb"][0])
        return (hdd_tb * storage["hdd_count"]) + (nvme_tb * storage["nvme_count"])
    elif stype == "nvme_and_ssd":
        nvme_tb = data.get("nvme_tb", storage["nvme_options_tb"][0])
        ssd_tb = data.get("ssd_tb", storage["ssd_options_tb"][0])
        return nvme_tb + ssd_tb
    elif stype == "cloud":
        return 0
    return 0


def _sns_storage_error(storage, model_name):
    """Validate that a model can run as a Single Node System (SNS). A hybrid SNS
    must mirror each storage tier within the one node (RF2), which requires at
    least two disks of every type. A 3+1 layout (a single disk in one tier) can't
    be mirrored, so it's out of scope for SNS — return an error pointing the user
    at a multi-node build."""
    stype = storage["type"]
    if stype == "hybrid":
        tiers = {"HDD": storage["hdd_count"], "SSD": storage["ssd_count"]}
    elif stype == "hybrid_nvme":
        tiers = {"HDD": storage["hdd_count"], "NVMe": storage["nvme_count"]}
    elif stype == "nvme_and_ssd":
        tiers = {"NVMe": 1, "SSD": 1}
    else:
        return None
    if any(c < 2 for c in tiers.values()):
        layout = ", ".join("%d× %s" % (c, t) for t, c in tiers.items())
        return {"error": (
            f"{model_name} can't be configured as a single node: a hybrid Single "
            f"Node System must mirror each storage tier locally, which needs at "
            f"least 2 disks of every type (this layout is {layout}). Use 2 or more "
            f"nodes for this model."
        )}
    return None


def compute_drive_count_appliance(data, storage):
    """Number of physical drives in one node — used to decide whether a Single
    Node System can mirror (RF2). A single-disk node has no second drive to
    mirror to, so it runs unprotected (usable = raw)."""
    stype = storage["type"]
    if stype in ("nvme_only", "ssd_only", "hdd_only"):
        return storage.get("drives_per_node", 1)
    elif stype == "hybrid":
        return storage["hdd_count"] + storage["ssd_count"]
    elif stype == "hybrid_nvme":
        return storage["hdd_count"] + storage["nvme_count"]
    elif stype == "nvme_and_ssd":
        return 2
    return 0


def compute_biggest_disk_appliance(data, storage):
    stype = storage["type"]
    if stype == "nvme_only":
        return data.get("nvme_tb", storage["nvme_options_tb"][0])
    elif stype == "ssd_only":
        return data.get("ssd_tb", storage["ssd_options_tb"][0])
    elif stype == "hdd_only":
        return data.get("hdd_tb", storage["hdd_options_tb"][0])
    elif stype == "hybrid":
        return max(data.get("hdd_tb", storage["hdd_options_tb"][0]),
                   data.get("ssd_tb", storage["ssd_options_tb"][0]))
    elif stype == "hybrid_nvme":
        return max(data.get("hdd_tb", storage["hdd_options_tb"][0]),
                   data.get("nvme_tb", storage["nvme_options_tb"][0]))
    elif stype == "nvme_and_ssd":
        return max(data.get("nvme_tb", storage["nvme_options_tb"][0]),
                   data.get("ssd_tb", storage["ssd_options_tb"][0]))
    return 0


def calculate_validated(data, node_count):
    if node_count < 2:
        return {"error": "Software-only (validated) requires minimum 2 nodes"}

    cores = data.get("cores_per_node", 4)
    threads = data.get("threads_per_node", 8)
    ghz = data.get("ghz", 2.0)
    ram_gb = data.get("ram_gb", 64)

    disks = data.get("disks", [])
    if not disks:
        return {"error": "At least 1 disk required per node"}

    disk_count = len(disks)
    if disk_count == 2:
        return {"error": "Disk count must be 1 or 3+. 2 disks is not supported."}

    # Optional storage-only nodes: same disks, virtualization disabled. They add
    # capacity and disks to the cluster but no usable compute.
    so_block, so_err = _validated_storage_only(data, disk_count=disk_count,
                                               hci_count=node_count)
    if so_err:
        return so_err
    so_count = so_block["count"] if so_block else 0
    total_nodes = node_count + so_count

    # A HyperCore cluster holds at most 8 nodes, so larger builds split into
    # several clusters (the HCI floor scales with the cluster count).
    layout = _cluster_layout(total_nodes)
    num_clusters = len(layout)
    hci_err = _cluster_min_hci_error(node_count, so_count, layout)
    if hci_err:
        return hci_err

    # 100-disk hard limit binds on the LARGEST cluster, not the total node
    # count. Storage-only nodes carry the same disks, so they count too.
    largest_cluster = max(layout)
    max_cluster_disks = disk_count * largest_cluster
    if max_cluster_disks > 100:
        return {
            "error": (
                f"Cluster disk limit exceeded: {max_cluster_disks} disks "
                f"({disk_count} per node × {largest_cluster} nodes in the largest "
                f"cluster). The maximum is 100 disks per cluster. When more storage "
                f"capacity is required, deploy more clusters or use bigger disks."
            )
        }

    has_spinning = any(d["type"] in ("SAS", "NLSAS", "SATA", "HDD") for d in disks)
    has_flash = any(d["type"] in ("SSD", "NVMe") for d in disks)
    is_hybrid = has_spinning and has_flash

    if is_hybrid:
        total_cap = sum(d["size_tb"] for d in disks)
        flash_cap = sum(d["size_tb"] for d in disks if d["type"] in ("SSD", "NVMe"))
        if total_cap > 0:
            flash_pct = (flash_cap / total_cap) * 100
            if flash_pct < 7 or flash_pct > 25:
                return {
                    "error": f"Hybrid fast tier must be 7-25% of total capacity. Currently {flash_pct:.1f}%",
                    "flash_percentage": round(flash_pct, 1),
                }
        # HEAT best practice: enough HDD spindles per flash disk so the slow tier
        # can absorb cold data evicted from flash (Certified appliances already
        # encode this; enforce it on Validated configs too).
        hdd_n = sum(1 for d in disks if d["type"] in ("SAS", "NLSAS", "SATA", "HDD"))
        flash_n = sum(1 for d in disks if d["type"] in ("SSD", "NVMe"))
        min_ratio = T.hybrid_min_hdd_per_flash
        if flash_n > 0 and hdd_n < min_ratio * flash_n:
            return {
                "error": (f"Hybrid tiered layout needs at least {min_ratio} HDDs per "
                          f"flash disk for HEAT down-tiering. Currently {hdd_n}× HDD : "
                          f"{flash_n}× flash."),
            }

    # Apply HyperCore OS overhead; OS RAM is tiered by the node's drive count.
    usable_cores = cores - T.os_core_overhead
    usable_ram = ram_gb - T.usable_ram_overhead_for(disk_count)

    raw_per_node = sum(d["size_tb"] for d in disks)
    biggest_disk = max(d["size_tb"] for d in disks)
    # Storage spans all nodes (HCI + storage-only), per cluster; compute spans
    # the HCI nodes only.
    total_raw = raw_per_node * total_nodes
    if total_nodes > 1:
        usable = _cluster_usable_storage(raw_per_node, biggest_disk, layout)
    else:
        # Single Node System: RF2 mirrors across the node's own drives (raw/2)
        # but reserves no rebuild disk; a single-disk SNS can't mirror at all.
        usable = raw_per_node if disk_count <= 1 else raw_per_node / 2
    if so_block:
        so_block["raw_storage_tb"] = round(raw_per_node, 2)

    total_cores = usable_cores * node_count
    total_threads = threads * node_count
    total_ghz = ghz * cores * node_count
    total_ram = usable_ram * node_count

    n_minus_1 = _n_minus_1_block(node_count, num_clusters, usable_cores,
                                 threads, ghz * cores, usable_ram, usable)

    storage_type = "All-Flash"
    if is_hybrid:
        storage_type = "Hybrid"
    elif has_spinning:
        storage_type = "HDD-Only"

    # Software-only configs carry no model NIC count; default to dedicated (4)
    # unless the request specifies otherwise.
    _nic_ports = int(data.get("nic_ports", 4) or 4)
    network_svg = network_svg_for(node_count, so_block["count"] if so_block else 0, _nic_ports)

    return {
        "mode": "validated",
        # Software-only sizing reads nothing from the hardware catalog — the
        # disks and CPU figures come from the request — so there is no catalog
        # identity to record. The fingerprint for these rests on the engine and
        # tunables alone, which is correct: a catalog edit cannot move a number
        # that was never read from the catalog.
        "refs": {"mode": "validated"},
        "node_count": node_count,
        "total_node_count": total_nodes,
        "num_clusters": num_clusters,
        "cluster_layout": layout,
        "storage_only": so_block,
        "storage_type": storage_type,
        "network_svg": network_svg,
        # Carry the port count so the exporters' diagram regeneration
        # (_rec_network_svg) matches the on-screen SVG instead of defaulting to 2.
        "nic_ports": _nic_ports,
        "per_node": {
            "cores": usable_cores,
            "threads": threads,
            "ghz": ghz,
            "ram_gb": usable_ram,
            "disk_count": disk_count,
            "raw_storage_tb": round(raw_per_node, 2),
            "disks": disks,
        },
        "cluster_total": {
            "cores": total_cores,
            "threads": total_threads,
            "total_ghz": round(total_ghz, 2),
            "ram_gb": total_ram,
            "raw_storage_tb": round(total_raw, 2),
            "usable_storage_tb": round(usable, 2),
        },
        "n_minus_1": n_minus_1,
        "single_node": total_nodes == 1,
        "redundancy_note": SNS_NO_REDUNDANCY_MSG if total_nodes == 1 else None,
        "validation": {
            "disk_count_valid": disk_count == 1 or disk_count >= 3,
            "hybrid_ratio_valid": True,
            "no_raid": True,
            "internal_only": True,
        },
    }


def _validated_storage_only(data, disk_count, hci_count):
    """Build the storage-only-node block for a validated (software-only) config,
    or (None, None) when none requested. Storage-only nodes carry the same disks
    as the HCI nodes; the caller fills in raw_storage_tb. A single low CPU and
    >=16 GB RAM are user-supplied; requires >=2 full HCI nodes."""
    so = data.get("storage_only") or {}
    try:
        count = int(so.get("count", 0) or 0)
    except (TypeError, ValueError):
        return None, None
    if count <= 0:
        return None, None
    if count > MAX_STORAGE_ONLY_COUNT:
        return None, {"error": f"Storage-only node count must be {MAX_STORAGE_ONLY_COUNT} or fewer"}
    if hci_count < T.min_hci_nodes_per_cluster:
        return None, {"error": (
            f"At least {T.min_hci_nodes_per_cluster} full HCI nodes are required "
            f"when adding storage-only nodes (for HA and rolling updates)."
        )}

    cores = int(so.get("cores", 1) or 1)
    threads = int(so.get("threads", cores * 2) or cores * 2)
    ghz = float(so.get("ghz", 2.0) or 2.0)
    ram_gb = int(so.get("ram_gb", T.storage_only_ram_floor_gb) or T.storage_only_ram_floor_gb)
    if ram_gb < T.storage_only_ram_floor_gb:
        return None, {"error": (
            f"Storage-only nodes require at least {T.storage_only_ram_floor_gb} GB RAM."
        )}

    return {
        "count": count,
        "cores": cores,
        "threads": threads,
        "ghz": ghz,
        "ram_gb": ram_gb,
        "disk_count": disk_count,
        "raw_storage_tb": 0,  # filled in once raw_per_node is known
    }, None
