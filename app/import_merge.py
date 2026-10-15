"""Merge several parsed assessment files into one estate (plan B).

The inverse of cluster_split: several uploads (RVTools, Live Optics, Nutanix
Collector — any parser that returns the common dict shape) become ONE set of
hosts / VMs / datastores / perf rows, re-cut into the target clusters the user
mapped them to. Pure and stateless: the client posts every dataset with the
current overlap choices and mapping, and calls again whenever one changes.

    merge(datasets, resolutions=None, mapping=None) -> {
        sources, overlaps, groups, targets, import_warnings}

A dataset is one parsed file as the raw import route returns it:
    {id, file_type, scan_type, source_meta: {file_name, file_sha256,
     collected_at, imported_at, ...}, hosts, host_performance, datastores,
     vms, host_nics, summary}

Rules (docs/multi-import-storage-growth-plan.md, B):
  * Identity. A VM matches another file's VM on uuid (instance or BIOS); when
    either side has none, on name (case-insensitive), marked a possible
    duplicate. A host matches on its name without the domain suffix.
  * Overlap choices per entity: keep one file's copy, or (VMs only) keep both.
    The default is the copy from the most recently collected file (P3). A host
    takes its perf row, NICs and datastores with it.
  * Mixed performance (M2). Per target, a metric measured on only part of the
    hosts is extrapolated: CPU by vCPU, memory by provisioned RAM, IOPS by used
    storage, each over the active VMs. A metric measured nowhere stays 0.
  * Groups (M5): one per (file, source cluster), plus ONE "Standalone hosts"
    group for every unclustered host and VM from every file. Each group maps to
    a target cluster by name; clusters default to their own target, standalone
    hosts to one shared target.
  * The summary of a target is always rebuilt from its records
    (parser_common.build_summary); summaries are never added together.
"""
import os
import re
from collections import Counter

from cluster_split import UNCLUSTERED, _attribute_datastores
from parser_common import build_summary
from import_checks import build_import_warnings

STANDALONE = "__standalone__"
STANDALONE_TARGET = "Standalone hosts"
KEEP_BOTH = "both"
# A mapping value that leaves a group out of every target.
SKIP = "__skip__"

_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


# ── identity ─────────────────────────────────────────────────────────────────

def host_key(name):
    """A host's identity across files: its name without the domain suffix,
    lower-case. An IP address is kept whole (its dots are not a domain)."""
    n = str(name or "").strip().lower()
    if not n or _IPV4.match(n):
        return n
    return n.split(".", 1)[0]


def _uuids(vm):
    return {u for u in (str(vm.get("uuid") or "").strip().lower(),
                        str(vm.get("bios_uuid") or "").strip().lower()) if u}


def _vm_name(vm):
    return str(vm.get("name") or "").strip().lower()


def _cluster_of(rec):
    return (rec.get("cluster") or "").strip()


def _is_active(vm):
    return vm.get("powered_on") and not vm.get("is_template")


# ── sources ──────────────────────────────────────────────────────────────────

def _stem(name):
    return os.path.splitext(os.path.basename(str(name or "")))[0]


def _dataset_id(ds, i):
    return str(ds.get("id") if ds.get("id") not in (None, "") else "f%d" % i)


def _collected(ds):
    meta = ds.get("source_meta") or {}
    return str(meta.get("collected_at") or meta.get("imported_at") or "")


def _has_metric(perf, metric):
    if not perf:
        return False
    if metric == "cpu":
        return (perf.get("peak_cpu_ghz") or 0) > 0
    if metric == "mem":
        return ((perf.get("peak_mem_pct") or 0) > 0 or (perf.get("avg_mem_pct") or 0) > 0
                or (perf.get("peak_mem_mib") or 0) > 0)
    return any((perf.get(k) or 0) > 0 for k in ("peak_iops", "avg_iops", "p95_iops"))


def _source_rows(datasets):
    """Per-file meta, with identical files (same sha256) after the first
    dropped."""
    rows, seen = [], {}
    for i, ds in enumerate(datasets):
        meta = ds.get("source_meta") or {}
        sid = _dataset_id(ds, i)
        sha = meta.get("file_sha256")
        perfs = ds.get("host_performance") or []
        row = {
            "id": sid,
            "index": i,
            "file_name": meta.get("file_name") or "",
            "file_type": ds.get("file_type") or meta.get("file_type") or "",
            "scan_type": ds.get("scan_type"),
            "collected_at": _collected(ds),
            "host_count": len(ds.get("hosts") or []),
            "vm_count": len(ds.get("vms") or []),
            "has_cpu_perf": any(_has_metric(p, "cpu") for p in perfs),
            "has_mem_perf": any(_has_metric(p, "mem") for p in perfs),
            "has_iops": any(_has_metric(p, "io") for p in perfs),
            "dropped": None,
        }
        if sha and sha in seen:
            row["dropped"] = "duplicate"
            row["duplicate_of"] = seen[sha]
        elif sha:
            seen[sha] = sid
        rows.append(row)
    return rows


# ── overlaps ─────────────────────────────────────────────────────────────────

class _UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _newest(srcs, order):
    """The default copy: the most recently collected file; on a tie, the one
    uploaded last."""
    return max(srcs, key=lambda s: (order[s]["collected_at"], order[s]["index"]))


def _vm_copy(sid, i, vm):
    return {"src": sid, "ref": "%s:%d" % (sid, i), "name": vm.get("name") or "", "host": vm.get("host") or "",
            "cluster": vm.get("cluster") or "", "vcpus": vm.get("vcpus") or 0,
            "ram_gb": vm.get("provisioned_memory_gb") or 0,
            "used_gb": vm.get("vdisk_used_gb") or 0,
            "powered_on": bool(vm.get("powered_on"))}


def _find_overlaps(live, order):
    """Entities present in more than one file. Returns (overlaps, vm_entity,
    host_entity): overlaps without choices yet, and the entity key of every
    overlapping (src, index) VM / (src, host_key) host."""
    overlaps = []

    # Hosts: same short name in more than one file.
    by_key = {}
    for sid, ds in live:
        for h in ds.get("hosts") or []:
            k = host_key(h.get("name"))
            if k:
                by_key.setdefault(k, {}).setdefault(sid, h)
    host_entity = {}
    for k, per in by_key.items():
        if len(per) < 2:
            continue
        ent = "host:" + k
        for sid in per:
            host_entity[(sid, k)] = ent
        overlaps.append({
            "key": ent, "kind": "host", "name": next(iter(per.values())).get("name") or k,
            "possible": False,
            "copies": [{"src": sid, "name": h.get("name") or "",
                        "cluster": h.get("cluster") or "",
                        "cores": h.get("cpu_cores") or 0,
                        "ram_gb": h.get("memory_gb") or 0} for sid, h in per.items()],
        })

    # VMs: union-find across files on uuid, then on name where a side has none.
    uf = _UF()
    by_uuid, by_name = {}, {}
    nodes = {}
    for sid, ds in live:
        for i, vm in enumerate(ds.get("vms") or []):
            node = (sid, i)
            nodes[node] = vm
            uf.find(node)
            for u in _uuids(vm):
                by_uuid.setdefault(u, []).append(node)
            n = _vm_name(vm)
            if n:
                by_name.setdefault(n, []).append(node)
    for group in by_uuid.values():
        for other in group[1:]:
            if other[0] != group[0][0]:
                uf.union(group[0], other)
    for group in by_name.values():
        for a in group:
            for b in group:
                if a[0] >= b[0]:
                    continue
                ua, ub = _uuids(nodes[a]), _uuids(nodes[b])
                if ua and ub:
                    continue          # both have a uuid: the uuid decides
                uf.union(a, b)
    comps = {}
    for node in nodes:
        comps.setdefault(uf.find(node), []).append(node)
    vm_entity = {}
    for root, members in comps.items():
        srcs = {m[0] for m in members}
        if len(srcs) < 2:
            continue
        members.sort(key=lambda m: (order[m[0]]["index"], m[1]))
        ent = "vm:%s:%d" % members[0]
        for m in members:
            vm_entity[m] = ent
        # Matched on uuid only when one uuid is shared by every copy; anything
        # else was (also) matched on name and may be two different VMs.
        common = set.intersection(*[_uuids(nodes[m]) for m in members])
        possible = not common
        overlaps.append({
            "key": ent, "kind": "vm", "name": nodes[members[0]].get("name") or "",
            "possible": bool(possible),
            "copies": [_vm_copy(m[0], m[1], nodes[m]) for m in members],
        })
    return overlaps, vm_entity, host_entity


def _apply_choices(overlaps, resolutions, order):
    """Fill each overlap's ``choice`` from the resolutions, else the default."""
    resolutions = resolutions or {}
    for ov in overlaps:
        srcs = [c["src"] for c in ov["copies"]]
        want = resolutions.get(ov["key"])
        if want == KEEP_BOTH and ov["kind"] == "vm":
            ov["choice"] = KEEP_BOTH
        elif want in srcs:
            ov["choice"] = want
        else:
            ov["choice"] = _newest(set(srcs), order)
        ov["default"] = _newest(set(srcs), order)


# ── groups and mapping ───────────────────────────────────────────────────────

def _group_key(sid, cluster):
    return "%s::%s" % (sid, cluster) if cluster else STANDALONE


def _default_targets(groups):
    """Clusters map to their own name, standalone hosts to one shared
    target. A name used by two files gets the file's stem added, unless the
    groups are likely the same cluster (they share hosts): those default to
    one target, which the user can still split."""
    names = Counter(g["cluster"] for g in groups if g["key"] != STANDALONE)
    same = {g["cluster"] for g in groups if g.get("duplicate_of")}
    out = {}
    for g in groups:
        if g["key"] == STANDALONE:
            out[g["key"]] = STANDALONE_TARGET
        elif names[g["cluster"]] > 1 and g["cluster"] not in same:
            out[g["key"]] = "%s · %s" % (g["cluster"], g["file_stem"])
        else:
            out[g["key"]] = g["cluster"]
    return out


def _clean_target(name):
    name = str(name or "").strip()[:120]
    return name or None


# ── extrapolation (M2) ───────────────────────────────────────────────────────

def _extrapolate(summary, data):
    """Scale the performance totals of a partly measured set up to the whole
    set. Returns the {metric, measured_pct} entries that were extrapolated."""
    perf_by_host = {}
    for p in data["host_performance"]:
        perf_by_host[host_key(p.get("host"))] = p
    active = [v for v in data["vms"] if _is_active(v)]

    def measured_share(metric, weight):
        total = sum(weight(v) for v in active)
        on = sum(weight(v) for v in active
                 if _has_metric(perf_by_host.get(host_key(v.get("host"))), metric))
        return total, on

    out = []

    def ratio(metric, weight):
        total, on = measured_share(metric, weight)
        if on <= 0 or total <= 0 or on >= total - 1e-9:
            return None
        out.append({"metric": metric, "measured_pct": round(100.0 * on / total)})
        return total / on

    measured = lambda m: [p for p in data["host_performance"] if _has_metric(p, m)]  # noqa: E731

    r_cpu = ratio("cpu", lambda v: v.get("vcpus") or 0)
    if r_cpu:
        summary["peak_cpu_ghz"] = round(summary["peak_cpu_ghz"] * r_cpu, 1)
        summary["avg_cpu_ghz"] = round(summary["avg_cpu_ghz"] * r_cpu, 1)
        rows = measured("cpu")
        summary["avg_cpu_pct"] = round(sum(p["avg_cpu_pct"] for p in rows) / len(rows), 1)

    r_mem = ratio("mem", lambda v: v.get("provisioned_memory_gb") or 0)
    if r_mem:
        # The memory figures are percentages: a peak is already the measured
        # maximum, and the average is taken over the measured hosts only.
        rows = measured("mem")
        summary["avg_mem_pct"] = round(sum(p["avg_mem_pct"] for p in rows) / len(rows), 1)

    r_io = ratio("io", lambda v: v.get("vdisk_used_gb") or 0)
    if r_io:
        for k in ("total_peak_iops", "total_avg_iops", "p95_iops"):
            summary[k] = round(summary[k] * r_io)
    return out


# ── targets ──────────────────────────────────────────────────────────────────

def _finalize(summary, parts):
    """Combine the Live Optics finalizer flags by rule (vcpu_ratio_assumed =
    any, scan type = the list, platform = the most common one)."""
    scans = sorted({p["scan_type"] for p in parts if p["scan_type"]})
    if len(scans) == 1 and len({p["scan_type"] for p in parts}) == 1:
        summary["scan_type"] = scans[0]
    elif scans:
        summary["scan_types"] = scans
    if any(p["scan_type"] == "general" or p["summary"].get("vcpu_ratio_assumed")
           for p in parts):
        summary["vcpu_per_core_ratio"] = 3.0
        summary["vcpu_ratio_assumed"] = True
    platforms = Counter()
    for p in parts:
        plat = p["summary"].get("current_platform")
        if plat:
            platforms[plat] += max(1, p["hosts"])
    if platforms:
        summary["current_platform"] = platforms.most_common(1)[0][0]
    types = {p["file_type"] for p in parts}
    if len(types) == 1 and next(iter(types)) in ("rvtools", "nutanix"):
        summary["source"] = next(iter(types))


def _target_warnings(data, summary, parts, extrapolated):
    types = sorted({p["file_type"] for p in parts})
    warns = build_import_warnings({"summary": summary, "hosts": data["hosts"]},
                                  types[0] if len(types) == 1 else None)
    extra = []
    if len(types) > 1:
        extra.append({"code": "mixed_sources", "params": {"types": types}})
    for e in extrapolated:
        extra.append({"code": "perf_extrapolated", "params": e})
    if extrapolated:
        # The measured part now stands for the whole: a "no perf" caveat
        # for a metric that WAS extrapolated would contradict it.
        done = {e["metric"] for e in extrapolated}
        warns = [w for w in warns
                 if not (w["code"] == "no_perf" and "cpu" in done)
                 and not (w["code"] == "no_iops" and "io" in done)]
    return extra + warns


def merge(datasets, resolutions=None, mapping=None):
    datasets = list(datasets or [])
    sources = _source_rows(datasets)
    order = {s["id"]: s for s in sources}
    live = [(s["id"], datasets[s["index"]]) for s in sources if not s["dropped"]]

    overlaps, vm_entity, host_entity = _find_overlaps(live, order)
    _apply_choices(overlaps, resolutions, order)
    choice = {ov["key"]: ov["choice"] for ov in overlaps}

    # Keep the chosen copies, tagging every record with its file and source
    # cluster. Datastores follow their hosts.
    kept = []                     # (sid, ds-dict of kept records)
    for sid, ds in live:
        hosts, kept_hosts = [], set()
        for h in ds.get("hosts") or []:
            k = host_key(h.get("name"))
            ent = host_entity.get((sid, k))
            if ent and choice[ent] != sid:
                continue
            kept_hosts.add(k)
            hosts.append(dict(h, src=sid, source_cluster=h.get("cluster") or ""))
        perfs = [dict(p, src=sid) for p in ds.get("host_performance") or []
                 if host_key(p.get("host")) in kept_hosts]
        nics = [dict(n, src=sid) for n in ds.get("host_nics") or []
                if host_key(n.get("host")) in kept_hosts]
        stores = []
        for d in ds.get("datastores") or []:
            mounts = d.get("mounts") or []
            if mounts:
                left = [m for m in mounts if host_key(m) in kept_hosts]
                if not left:
                    continue
                stores.append(dict(d, mounts=left, src=sid))
            elif kept_hosts or not (ds.get("hosts") or []):
                # No host link: kept while the file keeps any host.
                stores.append(dict(d, src=sid))
        vms = []
        for i, vm in enumerate(ds.get("vms") or []):
            ent = vm_entity.get((sid, i))
            if ent and choice[ent] not in (sid, KEEP_BOTH):
                continue
            # ref = "<file id>:<index in that file>": lets a client that
            # appends files map each merged VM back to the one it sent.
            vms.append(dict(vm, src=sid, ref="%s:%d" % (sid, i),
                            source_cluster=vm.get("cluster") or ""))
        kept.append((sid, ds, {"hosts": hosts, "host_performance": perfs,
                               "host_nics": nics, "datastores": stores, "vms": vms}))

    # Groups: (file, cluster), plus one standalone group across all files.
    groups, gidx = [], {}

    def _group(sid, cluster):
        key = _group_key(sid, cluster)
        if key not in gidx:
            meta = order[sid] if sid else {}
            g = {"key": key, "src": sid if cluster else None,
                 "cluster": cluster or "",
                 "file_name": meta.get("file_name", "") if cluster else "",
                 "file_stem": _stem(meta.get("file_name", "")) if cluster else "",
                 "label": ("%s · %s" % (cluster, meta.get("file_name", "")))
                 if cluster else STANDALONE_TARGET,
                 "hosts": 0, "vms": 0, "active_vms": 0, "vcpus": 0, "ram_gb": 0.0,
                 "used_tb": 0.0, "host_names": set()}
            gidx[key] = g
            groups.append(g)
        return gidx[key]

    # Every (file, cluster) is a group, even one whose copies all lost an
    # overlap: the dialog lists it (with the duplicate hint) at zero figures.
    for sid, ds in live:
        for rec in list(ds.get("hosts") or []) + list(ds.get("vms") or []):
            _group(sid, _cluster_of(rec))
    for sid, _ds, rec in kept:
        for h in rec["hosts"]:
            g = _group(sid, _cluster_of(h))
            g["hosts"] += 1
            g["host_names"].add(host_key(h.get("name")))
        for v in rec["vms"]:
            g = _group(sid, _cluster_of(v))
            g["vms"] += 1
            if _is_active(v):
                g["active_vms"] += 1
                g["vcpus"] += v.get("vcpus") or 0
                g["ram_gb"] += v.get("provisioned_memory_gb") or 0
                g["used_tb"] += (v.get("vdisk_used_gb") or 0) / 1024

    # Likely duplicates: same cluster name in two files with shared hosts
    # (before the overlap choice removed them from one side).
    raw_hosts = {}
    for sid, ds in live:
        for h in ds.get("hosts") or []:
            c = _cluster_of(h)
            if c:
                raw_hosts.setdefault((sid, c), set()).add(host_key(h.get("name")))
    for g in groups:
        g["duplicate_of"] = None
        if g["key"] == STANDALONE:
            continue
        mine = raw_hosts.get((g["src"], g["cluster"]), set())
        for other in groups:
            if other is g or other["key"] == STANDALONE or other["cluster"] != g["cluster"]:
                continue
            if mine & raw_hosts.get((other["src"], other["cluster"]), set()):
                g["duplicate_of"] = other["key"]
                break

    defaults = _default_targets(groups)
    mapping = mapping or {}
    target_of = {g["key"]: (SKIP if mapping.get(g["key"]) == SKIP else
                            _clean_target(mapping.get(g["key"])) or defaults[g["key"]])
                 for g in groups}
    for g in groups:
        g["default_target"] = defaults[g["key"]]
        g["target"] = target_of[g["key"]]
        g["ram_gb"] = round(g["ram_gb"], 1)
        g["used_tb"] = round(g["used_tb"], 2)
        del g["host_names"]

    # Re-cut into targets. Each file's datastores are attributed to the
    # targets of that file's hosts (mounts, else used-storage share), so a
    # datastore never leaks into another file's clusters.
    target_names = []
    for g in groups:
        if g["target"] not in target_names:
            target_names.append(g["target"])
    tdata = {t: {"hosts": [], "host_performance": [], "datastores": [], "vms": [],
                 "host_nics": [], "_parts": {}} for t in target_names}

    for sid, ds, rec in kept:
        def tgt(record):
            return target_of[_group_key(sid, _cluster_of(record))]
        host_t = {}
        for h in rec["hosts"]:
            t = tgt(h)
            host_t[host_key(h.get("name"))] = t
            tdata[t]["hosts"].append(dict(h, cluster=t))
        for v in rec["vms"]:
            tdata[tgt(v)]["vms"].append(dict(v, cluster=tgt(v)))
        for p in rec["host_performance"]:
            t = host_t.get(host_key(p.get("host")))
            if t:
                tdata[t]["host_performance"].append(p)
        for n in rec["host_nics"]:
            t = host_t.get(host_key(n.get("host")))
            if t:
                tdata[t]["host_nics"].append(n)
        file_targets = []
        for t in list(host_t.values()) + [tgt(v) for v in rec["vms"]]:
            if t not in file_targets:
                file_targets.append(t)
        if rec["datastores"] and file_targets:
            # mounts name hosts as the file did; map them to targets by key
            mount_t = {}
            for h in rec["hosts"]:
                mount_t[h.get("name")] = host_t[host_key(h.get("name"))]
            for d in rec["datastores"]:
                for m in d.get("mounts") or []:
                    mount_t.setdefault(m, host_t.get(host_key(m)))
            used = {t: 0.0 for t in file_targets}
            for v in rec["vms"]:
                if _is_active(v):
                    used[tgt(v)] += v.get("vdisk_used_gb") or 0
            attributed = _attribute_datastores(
                rec["datastores"], {k: v for k, v in mount_t.items() if v},
                file_targets, used)
            for t, stores in attributed.items():
                tdata[t]["datastores"].extend(stores)
        for t in file_targets:
            tdata[t]["_parts"][sid] = ds

    targets = []
    for t in target_names:
        data = tdata[t]
        parts_ds = data.pop("_parts")
        # A skipped group is cut out like any target (so its share of a shared
        # datastore goes with it) and then left out.
        if t == SKIP or not (data["hosts"] or data["vms"]):
            continue
        summary = build_summary(data)
        parts = [{"src": sid, "file_type": order[sid]["file_type"],
                  "scan_type": ds.get("scan_type"), "summary": ds.get("summary") or {},
                  "hosts": sum(1 for h in data["hosts"] if h["src"] == sid)}
                 for sid, ds in parts_ds.items()]
        _finalize(summary, parts)
        summary["cluster_name"] = t if t != UNCLUSTERED else ""
        extrapolated = _extrapolate(summary, data)
        targets.append(dict(data, name=t, summary=summary,
                            sources=[p["src"] for p in parts],
                            groups=[g["key"] for g in groups if g["target"] == t],
                            import_warnings=_target_warnings(data, summary, parts,
                                                             extrapolated)))

    # The whole set's warnings (as if it were one target).
    all_types = sorted({order[sid]["file_type"] for sid, _ in live})
    warnings = []
    if len(all_types) > 1:
        warnings.append({"code": "mixed_sources", "params": {"types": all_types}})
    seen = set()
    for t in targets:
        for w in t["import_warnings"]:
            if w["code"] == "perf_extrapolated":
                key = (w["params"]["metric"], t["name"])
                if key not in seen:
                    seen.add(key)
                    warnings.append({"code": "perf_extrapolated",
                                     "params": dict(w["params"], target=t["name"])})
    dup = [s for s in sources if s["dropped"] == "duplicate"]
    for s in dup:
        warnings.append({"code": "duplicate_file",
                         "params": {"file": s["file_name"]}})

    return {"sources": sources, "overlaps": overlaps, "groups": groups,
            "targets": targets, "import_warnings": warnings}
