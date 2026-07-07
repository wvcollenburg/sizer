"""Split a parsed workload (the dict shape emitted by liveoptics/rvtools) into
one sub-dataset per SOURCE cluster (the vSphere cluster a host/VM belonged to,
read from the `Cluster` column) so each can be summarised and sized on its own.

This is unrelated to `recommend._cluster_layout`, which splits ONE
recommendation's node count into physical appliance clusters. Here "cluster"
means the source-side vSphere cluster (e.g. "Print", "Prosess").

The public entry point is `cluster_summaries(data, build_summary)`: it groups the
data by source cluster and runs the caller's own `_build_summary` on each group,
returning one summary per cluster (or `[]` when the dataset is a single cluster,
in which case the aggregate summary already covers it).
"""

UNCLUSTERED = "(unclustered)"


def _cluster_of(rec):
    return (rec.get("cluster") or "").strip() or UNCLUSTERED


def _distinct_clusters(hosts, vms):
    """Cluster names in first-seen order, hosts before VMs."""
    names, seen = [], set()
    for rec in list(hosts) + list(vms):
        c = _cluster_of(rec)
        if c not in seen:
            seen.add(c)
            names.append(c)
    return names


def _attribute_datastores(datastores, host_cluster, cluster_names, vm_used_by_cluster):
    """Return {cluster_name: [datastore-copies]}.

    A datastore carries no cluster field. We attribute each one via the hosts
    that mount it (LiveOptics records these in `mounts`); a datastore mounted by
    hosts in several clusters is split evenly across them. When mount info is
    absent (e.g. RVTools, which has no host↔datastore link), we fall back to
    distributing the datastore across clusters in proportion to each cluster's
    share of active-VM used storage.
    """
    out = {c: [] for c in cluster_names}

    total_vm_used = sum(vm_used_by_cluster.values())

    def _scaled(d, frac):
        c = dict(d)
        for k in ("capacity_gib", "used_gib", "free_gib"):
            if k in c and c[k] is not None:
                c[k] = round(c[k] * frac, 2)
        if "vm_count" in c and c["vm_count"] is not None:
            c["vm_count"] = round(c["vm_count"] * frac)
        return c

    for d in datastores:
        mounts = d.get("mounts") or []
        mount_clusters = []
        for m in mounts:
            c = host_cluster.get(m)
            if c and c not in mount_clusters:
                mount_clusters.append(c)

        if mount_clusters:
            frac = 1.0 / len(mount_clusters)
            for c in mount_clusters:
                out.setdefault(c, []).append(_scaled(d, frac) if len(mount_clusters) > 1 else dict(d))
            continue

        # Fallback: proportional to per-cluster active-VM used storage.
        if total_vm_used > 0:
            for c in cluster_names:
                frac = vm_used_by_cluster.get(c, 0) / total_vm_used
                if frac > 0:
                    out[c].append(_scaled(d, frac))
        else:
            frac = 1.0 / len(cluster_names)
            for c in cluster_names:
                out[c].append(_scaled(d, frac))

    return out


def split_by_cluster(data):
    """Group a parsed workload into per-source-cluster sub-datasets.

    Returns a list of (cluster_name, sub_data) preserving first-seen order, where
    sub_data has the same keys as `data` but filtered to that cluster. Returns
    None when the dataset has a single (or no) distinct cluster — the caller
    should just use the aggregate.
    """
    hosts = data.get("hosts", [])
    vms = data.get("vms", [])
    perfs = data.get("host_performance", [])
    datastores = data.get("datastores", [])
    nics = data.get("host_nics", [])

    names = _distinct_clusters(hosts, vms)
    if len(names) <= 1:
        return None

    host_cluster = {h["name"]: _cluster_of(h) for h in hosts}

    groups = {c: {
        "project": data.get("project"),
        "scan_type": data.get("scan_type"),
        "hosts": [], "host_performance": [], "datastores": [],
        "vms": [], "host_nics": [],
    } for c in names}

    for h in hosts:
        groups[_cluster_of(h)]["hosts"].append(h)
    for v in vms:
        groups[_cluster_of(v)]["vms"].append(v)
    for p in perfs:
        c = host_cluster.get(p.get("host"))
        if c:
            groups[c]["host_performance"].append(p)
    for n in nics:
        c = host_cluster.get(n.get("host"))
        if c:
            groups[c]["host_nics"].append(n)

    vm_used_by_cluster = {
        c: sum(v["vdisk_used_gb"] for v in groups[c]["vms"]
               if v.get("powered_on") and not v.get("is_template"))
        for c in names
    }
    ds_by_cluster = _attribute_datastores(datastores, host_cluster, names, vm_used_by_cluster)
    for c in names:
        groups[c]["datastores"] = ds_by_cluster.get(c, [])

    return [(c, groups[c]) for c in names]


def cluster_summaries(data, build_summary):
    """Build one per-cluster summary using the caller's own `build_summary`.

    Returns a list of {name, summary, host_count, vm_count} in first-seen order,
    or [] for a single-cluster dataset (the aggregate summary already covers it).
    """
    split = split_by_cluster(data)
    if not split:
        return []
    out = []
    for name, sub in split:
        summ = build_summary(sub)
        out.append({
            "name": name,
            "summary": summ,
            "host_count": summ.get("host_count", 0),
            "vm_count": summ.get("total_vms", 0),
        })
    return out
