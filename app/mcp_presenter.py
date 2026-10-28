"""Engine output as the MCP tools return it (docs/mcp/01-tool-design.md §2.4,
§2.5, §3.5).

``recommend.generate_recommendations`` returns candidates built for the
browser: every field the cards and exports need, the SVG, the ranking score.
A model reading a tool result needs less and different: per resource, what
was required against what the cluster gives, the resource that decided the
node count as one sentence, and the rule checks spelled out as pass/warn/fail
with numbers, because the engine *filters* on its hard rules and says
nothing about the ones that passed.

Everything here is derived from engine fields; nothing is recomputed. Two
things are deliberately left out: ``score`` (cost-derived; rank order is the
information) and ``network_svg`` (size). Not fingerprinted: a presentation
of a result is not the result.
"""
import math

import hcl_vendor
from tunables import T

# Rule checks, in the order they are reported. See checks_for().
CHECK_IDS = ("largest_vm_cpu", "largest_vm_ram", "day_one_storage", "day_one_ram",
             "iops", "vm_density", "cluster_size", "n_minus_1", "compute_floor",
             "hybrid_flash")


def present_result(result, summary, params, max_options=5):
    """The ``create_sizing`` result body from an engine result (§3.5)."""
    recs = result.get("recommendations") or []
    options = [present_option(rec, result, summary, params, rank)
               for rank, rec in enumerate(recs[:max_options], start=1)]
    return {
        "projection": present_projection(result.get("projection") or {}),
        "options": options,
        "options_total": len(recs),
        "perf_comparison": present_perf_comparison(result.get("perf_comparison")),
        "warnings": list(result.get("warnings") or []),
    }


def present_projection(p):
    """The engine's flat projection regrouped into today / at the horizon."""
    return {
        "years": p.get("years"),
        "growth_pct": p.get("growth_pct"),
        "storage_growth_pct": p.get("storage_growth_pct"),
        "snapshot_pct": p.get("snapshot_pct"),
        "base": {"vcpus": p.get("base_vcpus"), "ram_gb": p.get("base_ram_gb"),
                 "storage_tb": p.get("base_storage_tb"), "ghz": p.get("base_ghz")},
        "at_horizon": {"vcpus": p.get("projected_vcpus"), "ram_gb": p.get("projected_ram_gb"),
                       "storage_tb": p.get("projected_storage_tb"),
                       "ghz": p.get("projected_ghz"),
                       "snapshot_pct": p.get("snapshot_pct_at_target")},
        "iops_demand": p.get("iops_demand") or {},
        "compute_floor": _present_compute_floor(p.get("compute_floor")),
    }


def _present_compute_floor(cf):
    """The engine reports the floor's switch (`active`) even when there is
    nothing to size against, with utilisation defaulting to 100%. Say whether
    it was applied, and drop the defaulted utilisation, so a caller does not
    read "active, 100%" next to checks that say "not checked"."""
    if not cf:
        return cf
    out = dict(cf)
    applied = bool(cf.get("active")) and bool(cf.get("required_ghz") or cf.get("required_perf_index"))
    out["applied"] = applied
    if not applied:
        out["source_cpu_util_pct"] = None
        out["reason"] = ("switched off (perf_scaling = 0)" if not cf.get("active") else
                         "no source compute demand: needs total_host_ghz (with peak_cpu_ghz or "
                         "peak_cpu_pct for utilisation) or source_cpus")
    return out


def present_perf_comparison(pc):
    if not pc:
        return None
    return {
        "source_specrate": pc.get("source_index_specrate"),
        "source_index_raw": pc.get("source_index_raw"),
        "source_type": pc.get("source_type"),
        "target_specrate": pc.get("target_index"),
        "target_model": pc.get("target_model"),
        "ratio": pc.get("ratio"),
        "note": pc.get("note"),
    }


def present_option(rec, result, summary, params, rank):
    """One ranked candidate (§2.4). ``params`` are the resolved planning
    parameters (vcpu_ratio and the day-one caps after defaults), because the
    engine does not echo the requested ratio."""
    projection = result.get("projection") or {}
    cpu_required = _required_cores(projection, params)
    cpu_available = (rec["totals"]["cores"] if rec.get("sized_full_cluster")
                     else rec["n_minus_1"]["cores"])
    ram_required = projection.get("projected_ram_gb")
    ram_available = rec["n_minus_1"]["ram_gb"]
    stor_required = projection.get("projected_storage_tb")
    stor_available = rec["totals"]["usable_storage_tb"]
    iops_required = _iops_demand(summary)
    iops = rec.get("iops") or {}

    storage_only = rec.get("storage_only")
    stor_cfg = rec.get("storage_config") or {}
    option = {
        "rank": rank,
        "model": rec.get("model"),
        "display_model": hcl_vendor.rec_display_model(rec),
        "category": rec.get("category"),
        "form_factor": rec.get("form_factor"),
        "chassis": rec.get("chassis"),
        # The mode the sizing ran in, not a property of the model: the engine
        # stamps every option of a validated-mode run as validated.
        "sizing_mode": "validated" if rec.get("validated") else "certified",
        "vendor_chassis": rec.get("vendor_chassis"),
        "node_count": rec.get("node_count"),
        "hci_node_count": rec.get("hci_node_count"),
        "num_clusters": rec.get("num_clusters"),
        "cluster_layout": rec.get("cluster_layout"),
        "single_node": bool(rec.get("single_node")),
        "storage_only_nodes": dict(storage_only) if storage_only else None,
        "node": {
            "cpu": rec.get("cpu"),
            "cores": rec.get("cores_per_node"),
            "usable_cores": rec.get("usable_cores_per_node"),
            "threads": rec.get("threads_per_node"),
            "ghz": rec.get("ghz"),
            "ram_gb": rec.get("ram_per_node_gb"),
            "usable_ram_gb": rec.get("usable_ram_per_node_gb"),
            "storage": stor_cfg.get("desc"),
            "raw_storage_tb": stor_cfg.get("raw_per_node"),
            "nic_ports": rec.get("nic_ports"),
            "cpu_generation": rec.get("cpu_generation"),
            "perf_index": rec.get("cpu_perf_index"),
            "perf_index_is_passmark_estimate": bool(rec.get("cpu_perf_is_passmark")),
        },
        "capacity": {
            "cpu": _capacity(cpu_required, cpu_available, "cores",
                             "full_cluster" if rec.get("sized_full_cluster") else "n_minus_1"),
            "ram": _capacity(ram_required, ram_available, "GB", "n_minus_1"),
            "storage": _capacity(stor_required, stor_available, "TB", "usable_rf2"),
            "iops": {"required": iops_required, "available": iops.get("total"),
                     "available_n_minus_1": iops.get("n_minus_1"),
                     "unit": "IOPS", "basis": "total"},
        },
        "determinant": present_determinant(rec),
        "utilization_pct": _utilization(rec.get("utilization") or {}),
        "vcpu_ratio": rec.get("vcpu_ratio"),
        "vcpu_ratio_at_n_minus_1": rec.get("vcpu_ratio_degraded"),
        "sized_cpu_for_full_cluster": bool(rec.get("sized_full_cluster")),
        "compute_floor": rec.get("compute_floor"),
        "licensing": rec.get("licensing"),
    }
    option["checks"] = checks_for(rec, result, summary, params, option)
    return option


# ── pieces ───────────────────────────────────────────────────────────────────

def _required_cores(projection, params):
    vcpus = projection.get("projected_vcpus") or 0
    ratio = params.get("vcpu_ratio") or 0
    return math.ceil(vcpus / ratio) if ratio else None


def _iops_demand(summary):
    p95 = summary.get("p95_iops") or 0
    avg = summary.get("total_avg_iops") or 0
    if p95 > 0:
        return round(p95)
    if avg > 0:
        return round(avg)
    return None


def _capacity(required, available, unit, basis):
    headroom = None
    if required and available is not None:
        # The engine's own formula (recommend._fit_model): over-provisioning
        # as a fraction of the requirement.
        headroom = round((available - required) / required * 100, 1)
    return {"required": required, "available": available, "unit": unit,
            "basis": basis, "headroom_pct": headroom}


def _utilization(u):
    def band(key, extra):
        d = u.get(key) or {}
        out = {"today": d.get("current"), "at_horizon": d.get("total")}
        out[extra] = d.get(extra)
        return out
    return {"cpu": band("cpu", "ha_reserve"), "ram": band("ram", "ha_reserve"),
            "storage": band("storage", "snapshot")}


def _fmt(value):
    """Numbers as a person would write them: 80, 17.5, not 80.0."""
    if value is None:
        return "?"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def present_determinant(rec):
    """The engine's determinant plus the one-sentence statement in the
    guide's format: "Determined by CPU: 80 cores required vs 94 usable at
    N-1 (17.5% headroom)."."""
    det = dict(rec.get("determinant") or {})
    resource = det.get("resource")
    hr = det.get("headroom_pct")
    if resource == "minimum":
        det["statement"] = ("Determined by the minimum cluster size: no resource "
                            "needs more than %s nodes." % _fmt(rec.get("node_count")))
    elif resource == "Compute":
        det["statement"] = ("Determined by compute throughput: %s%% of the source's "
                            "utilised, grown compute demand (%s%% headroom)."
                            % (_fmt(det.get("achieved")), _fmt(hr)))
    elif resource in ("CPU", "RAM", "Storage"):
        unit = det.get("unit") or ""
        if resource == "Storage":
            basis = "usable"
        elif resource == "CPU" and rec.get("sized_full_cluster"):
            basis = "usable across the full cluster"
        else:
            basis = "usable at N-1"
        det["statement"] = ("Determined by %s: %s %s required vs %s %s %s (%s%% headroom)."
                            % (resource, _fmt(det.get("required")), unit,
                               _fmt(det.get("achieved")), unit, basis, _fmt(hr)))
    else:
        det["statement"] = None
    return det


def _resolve_pct(value, fallback):
    """The engine's day-one cap resolution (recommend.generate_recommendations)."""
    try:
        v = float(value) if value is not None else float(fallback)
    except (TypeError, ValueError):
        v = float(fallback)
    return max(1.0, min(v, 100.0))


def checks_for(rec, result, summary, params, option):
    """The rule checks for one option (§2.5), each ``{id, status, detail}``
    with status pass / warn / fail / not_applicable. Hard rules pass by
    construction (the engine dropped what failed them); the detail states the
    numbers so the pass is verifiable."""
    projection = result.get("projection") or {}
    checks = []

    def add(cid, status, detail):
        checks.append({"id": cid, "status": status, "detail": detail})

    # Largest VM: vCPUs within a node's logical processors, RAM within usable
    # node RAM (recommend._fit_model filters CPU and RAM options on both).
    mv_cores = summary.get("max_vm_cores") or 0
    mv_ram = summary.get("max_vm_ram_gb") or 0
    threads = rec.get("threads_per_node") or 0
    usable_ram = rec.get("usable_ram_per_node_gb") or 0
    if mv_cores > 0:
        add("largest_vm_cpu", "pass",
            "Largest VM has %s vCPUs; each node has %s logical processors."
            % (_fmt(mv_cores), _fmt(threads)))
    else:
        add("largest_vm_cpu", "not_applicable", "Largest VM vCPUs unknown: not checked.")
    if mv_ram > 0 and usable_ram > 0:
        share = mv_ram / usable_ram
        status = "warn" if share >= 0.9 else "pass"
        detail = ("Largest VM needs %s GB RAM; a node has %s GB usable (%s%%)."
                  % (_fmt(mv_ram), _fmt(usable_ram), _fmt(round(share * 100))))
        if status == "warn":
            detail += " Above 90%: check the HA implications of a VM this large."
        add("largest_vm_ram", status, detail)
    else:
        add("largest_vm_ram", "not_applicable", "Largest VM RAM unknown: not checked.")

    # Day-one caps: today's demand against the configured share of capacity
    # (storage against usable, RAM against N-1), before any growth.
    cap_s = _resolve_pct(params.get("max_day_one_storage_pct"), T.max_day_one_storage_pct)
    cap_r = _resolve_pct(params.get("max_day_one_ram_pct"), T.max_day_one_ram_pct)
    base_s = projection.get("base_storage_tb") or 0
    base_r = projection.get("base_ram_gb") or 0
    usable_s = rec["totals"]["usable_storage_tb"] or 0
    n1_ram = rec["n_minus_1"]["ram_gb"] or 0
    if usable_s > 0:
        pct = base_s / usable_s * 100
        add("day_one_storage", "pass" if pct <= cap_s + 1e-9 else "fail",
            "Today's %s TB is %s%% of %s TB usable (cap %s%%)."
            % (_fmt(base_s), _fmt(round(pct, 1)), _fmt(usable_s), _fmt(cap_s)))
    if n1_ram > 0:
        pct = base_r / n1_ram * 100
        add("day_one_ram", "pass" if pct <= cap_r + 1e-9 else "fail",
            "Today's %s GB RAM is %s%% of %s GB usable at N-1 (cap %s%%)."
            % (_fmt(base_r), _fmt(round(pct, 1)), _fmt(n1_ram), _fmt(cap_r)))

    # IOPS: the engine gates on TOTAL net IOPS and ranks a shortfall last
    # rather than dropping it, so a fail can appear here.
    demand = option["capacity"]["iops"]["required"]
    total = (rec.get("iops") or {}).get("total")
    if demand and total is not None:
        add("iops", "pass" if total >= demand else "fail",
            "%s IOPS required; the cluster provides %s net IOPS (%s at N-1)."
            % (_fmt(demand), _fmt(total), _fmt((rec.get("iops") or {}).get("n_minus_1"))))
    else:
        counts = (rec.get("storage_config") or {}).get("drive_counts") or {}
        all_hdd = bool(counts.get("HDD")) and not any(counts.get(k) for k in ("NVMe", "SSD"))
        add("iops", "not_applicable", "No IOPS figure in the inputs: not checked."
            + (" This option's drives are all HDD: confirm the workload's IOPS before "
               "proposing it." if all_hdd else ""))

    # VM density: the engine warns for the top option only; the same
    # threshold is applied to every option here.
    active = summary.get("active_vms") or 0
    hci = rec.get("hci_node_count") or rec.get("node_count") or 0
    if active > 0 and hci > 0:
        per_node = active / hci
        limit = T.vm_density_warn_per_node
        add("vm_density", "pass" if per_node <= limit else "warn",
            "%s VMs across %s VM-running nodes is ~%s per node (advisory limit %s)."
            % (_fmt(active), _fmt(hci), _fmt(round(per_node)), _fmt(limit)))
    else:
        add("vm_density", "not_applicable", "Powered-on VM count unknown: not checked.")

    layout = rec.get("cluster_layout") or [rec.get("node_count")]
    add("cluster_size", "pass",
        "%s node%s in %s cluster%s (%s); at most %s nodes per cluster."
        % (_fmt(rec.get("node_count")), "" if rec.get("node_count") == 1 else "s",
           _fmt(len(layout)), "" if len(layout) == 1 else "s",
           " + ".join(_fmt(n) for n in layout), _fmt(T.max_nodes_per_cluster)))

    if rec.get("single_node"):
        add("n_minus_1", "not_applicable", "Single node: no failover node is held back.")
    else:
        cpu = option["capacity"]["cpu"]
        ram = option["capacity"]["ram"]
        detail = ("With one node per cluster down: %s of %s cores and %s of %s GB RAM in use."
                  % (_fmt(cpu["required"]), _fmt(rec["n_minus_1"]["cores"]),
                     _fmt(ram["required"]), _fmt(ram["available"])))
        if rec.get("sized_full_cluster"):
            detail += " CPU was sized for the full cluster at the caller's request; RAM still fits at N-1."
        add("n_minus_1", "pass", detail)

    floor = rec.get("compute_floor")
    if floor and floor.get("coverage_pct") is not None:
        cov = floor["coverage_pct"]
        add("compute_floor", "pass" if cov >= 100 else "fail",
            "Compute throughput covers %s%% of the source's utilised, grown demand."
            % _fmt(cov))
    else:
        add("compute_floor", "not_applicable",
            "Compute floor off, or no source compute data: not checked.")

    checks.append(_hybrid_check(rec))
    return checks


def _hybrid_check(rec):
    stor = rec.get("storage_config") or {}
    counts = stor.get("drive_counts") or {}
    hdd = counts.get("HDD") or 0
    flash_key = next((k for k in ("NVMe", "SSD") if counts.get(k)), None)
    if not (rec.get("validated") and hdd and flash_key):
        return {"id": "hybrid_flash", "status": "not_applicable",
                "detail": "Not a Validated hybrid build: not checked."}
    flash = counts[flash_key]
    flash_tb = stor.get("%s_tb" % flash_key.lower()) or 0
    raw = stor.get("raw_per_node") or 0
    pct = flash_tb * flash / raw * 100 if raw else 0
    ok = (T.hybrid_flash_min_pct <= pct <= T.hybrid_flash_max_pct
          and hdd / flash >= T.hybrid_min_hdd_per_flash)
    return {"id": "hybrid_flash", "status": "pass" if ok else "fail",
            "detail": "Flash is %s%% of raw capacity (band %s–%s%%); %s HDD per flash disk (minimum %s)."
            % (_fmt(round(pct, 1)), _fmt(T.hybrid_flash_min_pct), _fmt(T.hybrid_flash_max_pct),
               _fmt(round(hdd / flash, 1)), _fmt(T.hybrid_min_hdd_per_flash))}


def summary_text(presented, inputs_used=None):
    """The short human summary that heads the text content (§3.5)."""
    options = presented.get("options") or []
    lines = []
    if not options:
        lines.append("No configuration fits this workload.")
    else:
        top = options[0]
        name = top.get("display_model") or top.get("model")
        lines.append("Top option: %s × %s%s." % (
            _fmt(top.get("node_count")), name,
            " (%s clusters)" % _fmt(top.get("num_clusters")) if (top.get("num_clusters") or 1) > 1 else ""))
        stmt = (top.get("determinant") or {}).get("statement")
        if stmt:
            lines.append(stmt)
        flagged = [c for c in top.get("checks") or [] if c["status"] in ("warn", "fail")]
        for c in flagged:
            lines.append("%s (%s): %s" % (c["id"], c["status"], c["detail"]))
        total = presented.get("options_total") or len(options)
        if len(options) > 1:
            others = ", ".join("%s × %s" % (_fmt(o.get("node_count")),
                                            o.get("display_model") or o.get("model"))
                               for o in options[1:])
            lines.append("Also returned: %s." % others)
        if total > len(options):
            lines.append("%s more option%s found; raise max_options to see %s." % (
                _fmt(total - len(options)), "" if total - len(options) == 1 else "s",
                "it" if total - len(options) == 1 else "them"))
    for w in presented.get("warnings") or []:
        lines.append("Warning: " + w)
    if inputs_used and inputs_used.get("defaults_applied"):
        lines.append("Defaults applied: " + ", ".join(inputs_used["defaults_applied"]) + ".")
    return "\n".join(lines)
