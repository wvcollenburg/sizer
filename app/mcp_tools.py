"""The MCP tools that need no saved state: constants, catalog, CPU lookup,
input validation and the sizing itself (docs/mcp/01-tool-design.md §3.1–§3.5).

Each handler takes the role-capped caller and validated, defaulted arguments
and returns ``(structured, summary_text)``. Everything they compute goes
through the same functions the REST routes use (sizing_service,
catalog_service, the engine), so a number can only come out one way.
"""
import catalog_service
import hcl_vendor
import mcp_presenter as presenter
import sizing_service as svc
from mcp_registry import SCHEMA_VERSION, ToolError, audit_event, invalid_input, tool
from tunables import T, TUNABLE_DEFS, refresh_from_db

# ── shared schemas (§2.1, §2.2); inlined into each tool's inputSchema ────────

def _num(desc, minimum=0, **kw):
    # Exclusive bounds are numbers (draft 2020-12): {"exclusiveMinimum": 0},
    # never draft 4's boolean flag, which the Claude API rejects outright.
    out = {"type": "number", "description": desc}
    if minimum is not None:
        out["minimum"] = minimum
    out.update(kw)
    return out


def _int(desc, minimum=0, **kw):
    out = {"type": "integer", "minimum": minimum, "description": desc}
    out.update(kw)
    return out


WORKLOAD_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "description": "The measured source environment. Required fields must be measured, never guessed; ask the user for a missing one.",
    "properties": {
        "total_vcpus": _int("Sum of vCPUs of all powered-on VMs.", 1),
        "provisioned_ram_gb": _num("Sum of the CONFIGURED (provisioned) RAM of all powered-on VMs, in GB. Not host RAM, not consumed RAM.", None, exclusiveMinimum=0),
        "used_storage_tb": _num("Used capacity on the shared datastores, in TB.", 0),
        "largest_vm_vcpus": _int("vCPUs of the single largest VM. It must fit on one node.", 1),
        "largest_vm_ram_gb": _num("Configured RAM of the single largest VM, in GB. It must fit on one node.", 0),
        "nic_speed_gbe": _num("Host NIC speed in GbE. Default 10 when omitted; the result says when the default was applied.", 0),
        "active_vms": _int("Number of powered-on VMs. Enables the VM-density check."),
        "total_vms": _int("Total VMs including powered-off and templates."),
        "p95_iops": _int("95th-percentile IOPS of the whole estate. Preferred IOPS figure."),
        "avg_iops": _int("Average IOPS; used when p95_iops is absent."),
        "peak_iops": _int("Peak IOPS (informational)."),
        "used_ram_gb": _num("Consumed (active) RAM of powered-on VMs, in GB. Informational; sizing uses provisioned RAM."),
        "provisioned_storage_tb": _num("Provisioned virtual disk capacity, in TB (informational)."),
        "datastore_total_tb": _num("Total datastore capacity, in TB (informational)."),
        "host_count": _int("Number of source hosts."),
        "total_host_cores": _int("Physical cores across all source hosts. Gives the current vCPU:core ratio."),
        "total_host_threads": _int("Logical processors across all source hosts."),
        "total_host_ghz": _num("Sum over hosts of (cores × GHz), NOT the GHz of one CPU. Feeds the compute floor."),
        "total_host_ram_gb": _num("Physical RAM across all source hosts, in GB."),
        "peak_cpu_ghz": _num("Measured peak GHz consumed across hosts (from an assessment). Preferred over peak_cpu_pct. Only counts together with total_host_ghz (utilisation = peak / total)."),
        "peak_cpu_pct": _num("Measured peak host CPU %, 0-100.", 0, maximum=100),
        "avg_cpu_pct": _num("Average host CPU %, 0-100.", 0, maximum=100),
        "peak_mem_pct": _num("Peak host memory %, 0-100.", 0, maximum=100),
        "avg_mem_pct": _num("Average host memory %, 0-100.", 0, maximum=100),
        "source_cpus": {
            "type": "array", "maxItems": 20,
            "description": "The source hosts' CPU models with socket counts, for the throughput comparison. Benchmark scores are looked up when not given.",
            "items": {"type": "object", "additionalProperties": False,
                      "required": ["model", "sockets"],
                      "properties": {
                          "model": {"type": "string", "minLength": 2, "maxLength": 120, "description": "CPU model as printed, e.g. 'Intel Xeon Silver 4110'."},
                          "sockets": _int("Total sockets of this CPU across the hosts.", 1),
                          "perf_index": _num("Optional benchmark score per socket (SPECrate2017_int, or PassMark CPU Mark with perf_type=passmark)."),
                          "perf_type": {"type": "string", "enum": ["specrate", "passmark"]}}}},
        "current_platform": {"type": "string", "maxLength": 120, "description": "Current hypervisor/hardware, e.g. 'VMware vSphere on Dell R740' (data only)."},
        "cluster_name": {"type": "string", "maxLength": 120, "description": "Source cluster name (data only)."},
        "guest_licensing": {"type": "string", "enum": list(svc.GUEST_LICENSING_VALUES),
                            "description": "Core-licensed guest software present: none (Linux/BSD), windows, or windows_db (Windows plus a core-billed database such as SQL Server or Oracle). A database cannot be detected and must be declared."},
    },
}
WORKLOAD_REQUIRED = list(svc.REQUIRED_WORKLOAD)

PARAMS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "description": "Planning parameters. Every field is optional; the tool's defaults apply (see get_constants).",
    "properties": {
        "vcpu_ratio": _num("Target vCPU:physical-core consolidation ratio, 1-10. Default from the tool.", 1, maximum=10),
        "growth_pct": _num("Yearly growth of compute (and storage unless storage_growth_pct is given), in %. Default 10.", 0, maximum=100),
        "storage_growth_pct": _num("Separate yearly storage growth, in %. Omit to grow storage at growth_pct.", 0, maximum=100),
        "snapshot_pct": _num("Snapshot reserve as % of used storage. Default 20.", 0, maximum=100),
        "years": _int("Planning horizon in years, 1-5. Default 5.", 1, maximum=5),
        "max_day_one_storage_pct": _num("Cap on today's storage consumption as % of usable capacity, 1-100. Default: the tool's setting, reported in inputs_used.params (see get_constants).", 1, maximum=100),
        "max_day_one_ram_pct": _num("Cap on today's RAM consumption as % of N-1 RAM, 1-100. Default: the tool's setting, reported in inputs_used.params (see get_constants).", 1, maximum=100),
        "target_nodes": _int("Exact node count wanted. Larger counts are shown only if this one is infeasible.", 1, maximum=64),
        "storage_preference": {"type": "string", "enum": ["auto", "flash", "hybrid", "spinning"], "description": "Restrict node storage media. Default auto."},
        "size_cpu_for_full_cluster": {"type": "boolean", "description": "Size CPU against all nodes instead of N-1 (degraded on a node failure). Default false."},
        "sizing_mode": {"type": "string", "enum": ["certified", "validated"], "description": "certified = Scale Computing appliances (default); validated = software-only on a vendor's HCL-listed servers."},
        "vendor": {"type": "string", "maxLength": 40, "description": "Validated mode only: the hardware vendor (a key from list_node_models.vendors)."},
        "allow_storage_only_nodes": {"type": "boolean", "description": "Let storage-bound configurations add storage-only nodes. Default false."},
        "target_model": {"type": "string", "maxLength": 50, "description": "Size against one model only (a name from list_node_models). Omit for all models."},
        "include_eol_eos": {"type": "boolean", "description": "Also consider end-of-life models. Default false."},
        "license_term_years": _int("Licence term to shape, 1-5 years, independent of the growth horizon. Default from the tool (5).", 1, maximum=5),
    },
}

# MCP parameter name -> engine keyword argument.
PARAM_TO_ENGINE = {
    "vcpu_ratio": "vcpu_ratio", "growth_pct": "growth_pct",
    "storage_growth_pct": "storage_growth_pct", "snapshot_pct": "snapshot_pct",
    "years": "years", "max_day_one_storage_pct": "max_day_one_storage_pct",
    "max_day_one_ram_pct": "max_day_one_ram_pct", "target_nodes": "target_nodes",
    "storage_preference": "storage_pref", "size_cpu_for_full_cluster": "size_full_cluster",
    "sizing_mode": "sizing_mode", "vendor": "vendor",
    "allow_storage_only_nodes": "allow_storage_only", "target_model": "target_model",
    "include_eol_eos": "include_eol_eos", "license_term_years": "license_term_years",
}


def _with_required(schema, required):
    out = dict(schema)
    out["required"] = list(required)
    return out


FROM_UPLOAD_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["upload_id"],
    "description": "Size an uploaded assessment (start_upload / get_upload) instead of giving a workload. The VM list stays on the server; exclude VMs by the vm_index values list_upload_vms shows.",
    "properties": {
        "upload_id": {"type": "string", "minLength": 3, "maxLength": 64,
                      "description": "An upload from start_upload, or a merge from merge_uploads."},
        "source_cluster": {"type": "string", "maxLength": 120,
                           "description": "Size one source cluster of a multi-cluster export (a name from get_upload.clusters), or one target cluster of a merge (a name from merge_uploads.targets). Omit to size the whole estate."},
        "exclude_compute": {"type": "array", "maxItems": 5000, "items": {"type": "integer", "minimum": 0},
                            "description": "vm_index values whose vCPUs and RAM are left out, e.g. VMs being retired or staying on the old platform."},
        "exclude_storage": {"type": "array", "maxItems": 5000, "items": {"type": "integer", "minimum": 0},
                            "description": "vm_index values whose disk usage is subtracted from used storage. A VM that is not migrating at all usually goes in both lists."},
        "include_local_storage": {"type": "boolean", "default": False,
                                  "description": "Add per-host local datastore usage (ISOs, templates) to the storage basis. Live Optics only; RVTools has no local split."},
    },
}


# ── get_constants ────────────────────────────────────────────────────────────

# TUNABLE_DEFS keys a caller may see (§3.1). An allowlist: a new tunable stays
# hidden until someone decides it is not price-shaped.
CONSTANT_KEYS = (
    "os_core_overhead", "os_ram_gb", "os_ram_bays_1_4_gb", "os_ram_bays_5_12_gb",
    "os_ram_bays_13_plus_gb", "ram_buffer_gb",
    "default_vcpu_ratio", "max_day_one_storage_pct", "max_day_one_ram_pct",
    "default_license_term_years", "perf_scaling", "perf_ghz_balance",
    "vm_density_warn_per_node",
    "max_nodes_per_cluster", "min_hci_nodes_per_cluster", "storage_only_ram_floor_gb",
    "max_cluster_disks", "hybrid_flash_min_pct", "hybrid_flash_max_pct",
    "hybrid_min_hdd_per_flash",
    "w_pcore", "w_ecore",
)
CONSTANT_UNITS = {
    "os_core_overhead": "cores/node", "os_ram_gb": "GB/node", "os_ram_bays_1_4_gb": "GB/node",
    "os_ram_bays_5_12_gb": "GB/node", "os_ram_bays_13_plus_gb": "GB/node",
    "ram_buffer_gb": "GB/node", "default_vcpu_ratio": "vCPU:core",
    "max_day_one_storage_pct": "%", "max_day_one_ram_pct": "%",
    "default_license_term_years": "years", "perf_scaling": "0/1", "perf_ghz_balance": "0-1",
    "vm_density_warn_per_node": "VMs/node", "max_nodes_per_cluster": "nodes",
    "min_hci_nodes_per_cluster": "nodes", "storage_only_ram_floor_gb": "GB",
    "max_cluster_disks": "disks", "hybrid_flash_min_pct": "%", "hybrid_flash_max_pct": "%",
    "hybrid_min_hdd_per_flash": "HDD/flash disk", "w_pcore": "weight", "w_ecore": "weight",
}
PER_SIZING_OVERRIDE = {"default_vcpu_ratio": "params.vcpu_ratio",
                       "max_day_one_storage_pct": "params.max_day_one_storage_pct",
                       "max_day_one_ram_pct": "params.max_day_one_ram_pct",
                       "default_license_term_years": "params.license_term_years"}

FIXED_RULES = [
    {"id": "rf2", "text": "Usable storage assumes two-way replication (RF2): half of raw, less the largest disk per cluster."},
    {"id": "n_minus_1", "text": "CPU and RAM are sized with one node per cluster down (N-1), unless size_cpu_for_full_cluster is set."},
    {"id": "largest_vm", "text": "The largest VM must fit within one node's logical processors and usable RAM."},
    {"id": "passmark_factor", "text": "PassMark CPU Mark scores are converted to SPECrate2017_int at %s per mark (approximate)." % svc.PASSMARK_TO_SPECRATE},
]


@tool("get_constants", "Sizing constants",
      "Returns the sizing constants and defaults in effect right now: default vCPU:core ratio, "
      "growth/snapshot/horizon defaults, day-one caps, per-node OS reserves, cluster limits, IOPS "
      "assumptions and the fixed rules (RF2, N-1, largest VM). Use it to explain how a sizing was "
      "calculated or before choosing planning parameters. Values are set by Scale Computing and "
      "cannot be changed per sizing except where create_sizing accepts a parameter.",
      {"type": "object", "additionalProperties": False, "properties": {}}, read_only=True)
def get_constants(caller, args):
    from orm_models import DriveTypeIops, SizingSetting
    refresh_from_db()
    defs = {d["key"]: d for d in TUNABLE_DEFS}
    constants = []
    for key in CONSTANT_KEYS:
        d = defs.get(key)
        if not d:
            continue
        constants.append({
            "key": key, "value": getattr(T, key), "unit": CONSTANT_UNITS.get(key, ""),
            "group": d.get("group"), "label": d.get("label"),
            "meaning": d.get("what") or d.get("help") or "",
            "per_sizing_override": PER_SIZING_OVERRIDE.get(key),
        })
    sizing = {s.key: s.value for s in SizingSetting.query.all()}
    out = {
        "schema_version": SCHEMA_VERSION,
        "defaults": {
            "vcpu_ratio": T.default_vcpu_ratio,
            "growth_pct": svc.RECOMMEND_DEFAULTS["growth_pct"],
            "snapshot_pct": svc.RECOMMEND_DEFAULTS["snapshot_pct"],
            "years": svc.RECOMMEND_DEFAULTS["years"],
            "max_day_one_storage_pct": T.max_day_one_storage_pct,
            "max_day_one_ram_pct": T.max_day_one_ram_pct,
            "license_term_years": T.default_license_term_years,
            "nic_speed_gbe": svc.DEFAULT_NIC_SPEED_GBE,
        },
        "constants": constants,
        "iops": {
            "derating_pct": round(float(sizing.get("iops_derating_pct", 0.35)) * 100, 1),
            "replication_factor": sizing.get("iops_replication_factor", 2),
            "read_fraction": sizing.get("iops_read_fraction", 0.70),
            "per_drive_iops": {r.drive_type: r.iops for r in DriveTypeIops.query.all()},
        },
        "fixed_rules": FIXED_RULES,
        "compute_floor": {"active": bool(T.perf_scaling),
                          "ghz_vs_benchmark_balance": T.perf_ghz_balance},
    }
    text = ("Defaults: vCPU ratio %s, growth %s%%/yr, snapshot %s%%, %s years, day-one caps "
            "storage %s%% / RAM %s%%, licence term %s years, NIC %s GbE. %d constants listed."
            % (T.default_vcpu_ratio, out["defaults"]["growth_pct"], out["defaults"]["snapshot_pct"],
               out["defaults"]["years"], T.max_day_one_storage_pct, T.max_day_one_ram_pct,
               T.default_license_term_years, svc.DEFAULT_NIC_SPEED_GBE, len(constants)))
    return out, text


# ── list_node_models ─────────────────────────────────────────────────────────

MODEL_FIELDS = ("status", "category", "form_factor", "chassis", "min_nodes",
                "validated_only", "ram_options_gb", "storage", "nic_options")
CPU_FIELDS = ("desc", "cores", "threads", "p_cores", "e_cores", "ghz", "generation",
              "perf_index", "specrate_int")


def _public_model(name, md):
    out = {"name": name}
    for key in MODEL_FIELDS:
        out[key] = md.get(key)
    out["cpu_options"] = [{k: c.get(k) for k in CPU_FIELDS} for c in md.get("cpu_options") or []]
    return out


@tool("list_node_models", "Node models",
      "Lists the Scale Computing node models the sizing engine can recommend, with their CPU, RAM, "
      "drive and NIC options. Use it to answer catalog questions or to pick a value for "
      "params.target_model. Prices are never included. In validated mode it lists the hardware "
      "vendors available and shows models per vendor chassis.",
      {"type": "object", "additionalProperties": False, "properties": {
          "sizing_mode": {"type": "string", "enum": ["certified", "validated"], "default": "certified"},
          "vendor": {"type": "string", "maxLength": 40, "description": "Validated mode only: a key from `vendors` of a previous call."},
          "include_eol_eos": {"type": "boolean", "default": False},
          "model": {"type": "string", "maxLength": 50, "description": "Return only this model."},
      }}, read_only=True)
def list_node_models(caller, args):
    validated = args["sizing_mode"] == "validated"
    status = "all_current" if args["include_eol_eos"] else "active"
    vendors = hcl_vendor.list_vendors() if validated else []
    vendor_used = hcl_vendor.resolve_vendor(args.get("vendor")) if validated else None
    raw = catalog_service.appliance_models(status_filter=status, validated=validated,
                                           for_sizing=True, vendor=vendor_used)
    models = []
    for name, md in raw.items():
        if validated:
            models.append(dict(md, name=name))
            continue
        if (md.get("storage") or {}).get("type") == "cloud":
            continue
        models.append(_public_model(name, md))
    if args.get("model"):
        models = [m for m in models if m["name"] == args["model"]]
        if not models:
            raise ToolError("not_found", "No model named %r in the %s catalog." %
                            (args["model"], args["sizing_mode"]))
    out = {"schema_version": SCHEMA_VERSION, "sizing_mode": args["sizing_mode"],
           "vendor_used": vendor_used,
           "vendors": [{"key": v["brand"], "label": v["label"]} for v in vendors],
           "models": models}
    names = ", ".join(m["name"] for m in models[:12])
    text = "%d %s model%s%s%s" % (len(models), args["sizing_mode"], "" if len(models) == 1 else "s",
                                  (" for %s" % hcl_vendor.brand_label(vendor_used)) if vendor_used else "",
                                  (": " + names + ("…" if len(models) > 12 else "")) if models else ".")
    return out, text


# ── lookup_cpu_benchmark ─────────────────────────────────────────────────────

@tool("lookup_cpu_benchmark", "CPU benchmark lookup",
      "Looks up a source (existing) CPU's benchmark score, per socket, so a sizing can compare the "
      "old environment's throughput with the new cluster. Pass the CPU model as printed, e.g. "
      "'Intel Xeon Silver 4110'. You rarely need this: create_sizing looks CPUs up itself when "
      "workload.source_cpus has no perf_index.",
      {"type": "object", "additionalProperties": False, "required": ["query"], "properties": {
          "query": {"type": "string", "minLength": 2, "maxLength": 120}}}, read_only=True)
def lookup_cpu_benchmark(caller, args):
    hit = svc.lookup_cpu_perf(args["query"])
    out = dict(hit, schema_version=SCHEMA_VERSION)
    if hit.get("found"):
        text = "%s: %s %s per socket (%s)." % (hit["model"], hit["perf_index"],
                                               hit["perf_type"], hit["source"])
    else:
        text = ("No benchmark found for %r in the appliance catalog or the SPEC CPU 2017 table. "
                "Try the bare model (e.g. 'E5-2667 v4'); if still unknown, look up the PassMark "
                "CPU Mark and pass it as source_cpus[].perf_index with perf_type 'passmark'."
                % args["query"])
    return out, text


# ── validate_inputs and create_sizing ────────────────────────────────────────

def _resolve_params(args_params):
    """Engine keyword arguments and the resolved parameter echo for one call:
    the request's values where given, the route defaults otherwise, with
    the engine's own clamping applied to what it clamps."""
    given = args_params or {}
    kwargs = dict(svc.RECOMMEND_DEFAULTS)
    for field, engine_key in PARAM_TO_ENGINE.items():
        if field in given and given[field] is not None:
            kwargs[engine_key] = given[field]
    if kwargs["storage_pref"] == "auto":
        kwargs["storage_pref"] = None
    refresh_from_db()
    resolved = {
        "vcpu_ratio": max(1.0, min(float(kwargs["vcpu_ratio"] if kwargs["vcpu_ratio"] is not None
                                         else T.default_vcpu_ratio), 10.0)),
        "growth_pct": kwargs["growth_pct"],
        "storage_growth_pct": kwargs["storage_growth_pct"],
        "snapshot_pct": kwargs["snapshot_pct"],
        "years": max(1, min(int(kwargs["years"]), 5)),
        "max_day_one_storage_pct": presenter._resolve_pct(kwargs["max_day_one_storage_pct"],
                                                           T.max_day_one_storage_pct),
        "max_day_one_ram_pct": presenter._resolve_pct(kwargs["max_day_one_ram_pct"],
                                                       T.max_day_one_ram_pct),
        "target_nodes": kwargs["target_nodes"],
        "storage_preference": kwargs["storage_pref"] or "auto",
        "size_cpu_for_full_cluster": bool(kwargs["size_full_cluster"]),
        "sizing_mode": kwargs["sizing_mode"],
        "vendor": kwargs["vendor"],
        "allow_storage_only_nodes": bool(kwargs["allow_storage_only"]),
        "target_model": kwargs["target_model"],
        "include_eol_eos": bool(kwargs["include_eol_eos"]),
        "license_term_years": (int(kwargs["license_term_years"])
                               if kwargs["license_term_years"] is not None
                               else T.default_license_term_years),
    }
    defaults_applied = [f for f in PARAM_TO_ENGINE if f not in given or given[f] is None]
    return kwargs, resolved, defaults_applied


def _check_catalog_choices(kwargs):
    """A target model or vendor the engine would silently drop or replace is
    refused instead (§2.2: silent fallbacks become visible)."""
    problems, notes = [], []
    if kwargs["sizing_mode"] == "validated":
        vendors = [v["brand"] for v in hcl_vendor.list_vendors()]
        wanted = (kwargs.get("vendor") or "").strip().lower()
        used = hcl_vendor.resolve_vendor(kwargs.get("vendor"))
        if wanted and wanted != used:
            problems.append({"field": "params.vendor",
                             "problem": "is not a vendor with HCL-listed platforms (available: %s)"
                             % (", ".join(vendors) or "none")})
        kwargs["vendor"] = used
    elif kwargs.get("target_model"):
        models = catalog_service.appliance_models(status_filter="all", for_sizing=True)
        if kwargs["target_model"] not in models:
            problems.append({"field": "params.target_model",
                             "problem": "is not a model in the catalog (see list_node_models)"})
    return problems, notes


def _from_upload(caller, spec):
    """The engine summary of an uploaded assessment after exclusions, plus
    the upload context save_sizing needs. Returns (workload, upload_ctx)."""
    import mcp_uploads
    row, dataset = mcp_uploads.dataset_for(caller, spec["upload_id"], spec.get("source_cluster"))
    summary = mcp_uploads.adjusted_summary(dataset, spec)
    workload = svc.summary_to_workload(summary)
    ctx = {"row": row, "dataset": dataset, "summary": summary, "spec": spec,
           "exclusions": mcp_uploads.exclusion_stats(dataset, spec, summary)}
    return workload, ctx


def _prepare(caller, args):
    """Workload and params of a validate/create call to the engine's inputs.
    Returns (summary, kwargs, inputs_used, warnings) or raises. With
    ``from_upload`` the workload is the upload's summary after exclusions."""
    workload, upload_ctx = args.get("workload"), None
    if isinstance(args.get("from_upload"), dict):
        if isinstance(workload, dict):
            raise invalid_input([{"field": "from_upload", "problem": "cannot be combined with "
                                  "workload: give one or the other"}])
        workload, upload_ctx = _from_upload(caller, args["from_upload"])
    if not isinstance(workload, dict):
        raise invalid_input([{"field": "workload", "problem": "is required"}],
                            "Give a workload (measured figures) or a from_upload to size.")
    check = svc.validate_workload(workload)
    problems = ([{"field": "workload." + f, "problem": "is required"} for f in check["missing"]]
                + [{"field": "workload." + p["field"], "problem": p["problem"]} for p in check["invalid"]])
    kwargs, resolved, param_defaults = _resolve_params(args.get("params"))
    cat_problems, notes = _check_catalog_choices(kwargs)
    problems += cat_problems
    if problems:
        missing = [p["field"] for p in problems if p["problem"] == "is required"]
        msg = None
        if missing and upload_ctx is not None:
            msg = ("The uploaded file does not supply %s, so it cannot be sized as is "
                   "(for example an export with no powered-on VMs has no largest VM)."
                   % ", ".join(f.split(".", 1)[1] for f in missing))
        elif missing:
            msg = ("Missing measured inputs: %s. Ask the user for them; do not guess."
                   % ", ".join(f.split(".", 1)[1] for f in missing))
        raise invalid_input(problems, msg)

    if upload_ctx is not None:
        # The parser's summary IS the engine input; keep every key it set
        # (per-host figures, the detected guest licensing, the ratio) and
        # only rebuild what the MCP names cover.
        summary, workload_defaults = dict(upload_ctx["summary"]), []
        mapped, _ = svc.workload_to_summary(workload)
        summary.update(mapped)
        # From the stored record: a cluster slice (cluster_dataset) carries
        # the records only, not the file type or the import warnings.
        unmeasured = _unmeasured_fields(upload_ctx["row"].data or upload_ctx["dataset"])
        if not summary.get("nic_speed_mbps"):
            summary["nic_speed_mbps"] = svc.DEFAULT_NIC_SPEED_GBE * 1000
            workload_defaults.append("nic_speed_gbe")
    else:
        summary, workload_defaults = svc.workload_to_summary(workload)
        unmeasured = []
    if not summary.get("guest_licensing"):
        # The engine assumes standard Windows/Linux guests; say so rather
        # than let the licensing block look like a user choice.
        workload_defaults.append("guest_licensing")
    warnings = list(notes)
    if "nic_speed_gbe" in workload_defaults:
        warnings.append("NIC speed not given: assumed %s GbE, the same default the web tool "
                        "applies to manual input." % svc.DEFAULT_NIC_SPEED_GBE)
    source_perf = None
    if workload.get("source_cpus"):
        source_perf = svc.source_perf_index(workload["source_cpus"])
        if source_perf["total_specrate"] is not None:
            kwargs["source_perf_index"] = source_perf["total_specrate"]
            kwargs["source_perf_type"] = "specrate"
        for name in source_perf["unresolved"]:
            warnings.append("No benchmark found for source CPU %r in the appliance catalog or "
                            "the SPEC CPU 2017 table; it is left out of the throughput "
                            "comparison. Try lookup_cpu_benchmark with the bare model (e.g. "
                            "'E5-2667 v4'); if still unknown, pass its PassMark CPU Mark as "
                            "source_cpus[].perf_index with perf_type 'passmark'." % name)
    for flag in check["plausibility"]:
        warnings.append("Check %s: %s" % (flag["field"], flag["detail"]))

    workload_used = svc.summary_to_workload(summary)
    if "guest_licensing" in workload_defaults:
        workload_used["guest_licensing"] = svc.DEFAULT_GUEST_LICENSING
    if unmeasured:
        # The parser writes 0 where the export has no measurement; to the
        # caller that is "unknown", and the engine treats it that way too
        # (the IOPS check does not run on 0).
        for field in unmeasured:
            workload_used[field] = None
        warnings.append("The export carries no measurement for %s: reported as null, not zero. "
                        "Without IOPS the demand side of the IOPS check is unknown, so it does "
                        "not run; each option's IOPS capability (from per-drive figures) is still "
                        "reported. Pass the fields in workload if they are known from elsewhere."
                        % ", ".join(unmeasured))
    inputs_used = {
        "workload": workload_used,
        "params": resolved,
        "defaults_applied": workload_defaults + param_defaults,
        "vendor_used": kwargs["vendor"] if kwargs["sizing_mode"] == "validated" else None,
        "source_perf": source_perf,
        "plausibility": check["plausibility"],
    }
    if upload_ctx is not None:
        inputs_used["from_upload"] = {
            "upload_id": upload_ctx["row"].public_id,
            "source_cluster": upload_ctx["spec"].get("source_cluster"),
            "exclusions": upload_ctx["exclusions"]}
        if upload_ctx["summary"].get("guest_licensing_detail"):
            warnings.append("Guest licensing %r taken from the file: %s. Pass "
                            "workload.guest_licensing to declare a core-billed database."
                            % (upload_ctx["summary"].get("guest_licensing"),
                               upload_ctx["summary"]["guest_licensing_detail"]))
        inputs_used["_upload_ctx"] = upload_ctx     # for save_sizing; stripped below
    return summary, kwargs, inputs_used, warnings


def _unmeasured_fields(dataset):
    """get_upload and create_sizing agree on what an export did not measure."""
    import mcp_uploads
    return mcp_uploads.unmeasured_fields(dataset)


def public_inputs(inputs_used):
    """``inputs_used`` without the server-side upload context."""
    return {k: v for k, v in inputs_used.items() if not k.startswith("_")}


def _engine_error(exc):
    if exc.code == "catalog_error":
        return ToolError("catalog_error", "%s This is a catalog problem for Scale Computing to "
                         "fix; the sizing cannot run until then." % exc)
    return ToolError("invalid_input", str(exc))


@tool("validate_inputs", "Validate sizing inputs",
      "Checks a workload and planning parameters without running a sizing. Returns missing "
      "required fields, values out of range, implausible combinations, and the same input-quality "
      "warnings a sizing would give, plus the inputs after defaults. Use it when figures come from "
      "a conversation or a document and you are unsure they are complete. Never fill a missing "
      "measured value with a guess: ask the user.",
      {"type": "object", "additionalProperties": False, "properties": {
          "workload": WORKLOAD_SCHEMA, "from_upload": FROM_UPLOAD_SCHEMA,
          "params": PARAMS_SCHEMA}}, read_only=True)
def validate_inputs(caller, args):
    workload = args.get("workload") or {}
    if isinstance(args.get("from_upload"), dict):
        workload, _ = _from_upload(caller, args["from_upload"])
    check = svc.validate_workload(workload)
    kwargs, resolved, param_defaults = _resolve_params(args.get("params"))
    cat_problems, _ = _check_catalog_choices(kwargs)
    invalid = list(check["invalid"]) + cat_problems
    out = {"schema_version": SCHEMA_VERSION, "valid": not check["missing"] and not invalid,
           "missing": check["missing"], "invalid": invalid,
           "plausibility": check["plausibility"], "warnings": [], "inputs_used": None}
    if out["valid"]:
        summary, defaults = svc.workload_to_summary(workload)
        out["warnings"] = svc.input_quality_warnings(summary)
        out["inputs_used"] = {"workload": svc.summary_to_workload(summary), "params": resolved,
                              "defaults_applied": defaults + param_defaults}
    parts = []
    if check["missing"]:
        parts.append("missing: " + ", ".join(check["missing"]))
    if invalid:
        parts.append("invalid: " + "; ".join("%s %s" % (p["field"], p["problem"]) for p in invalid))
    if check["plausibility"]:
        parts.append("check: " + "; ".join(f["detail"] for f in check["plausibility"]))
    text = ("Inputs are complete and valid." if out["valid"] else "Inputs are not usable yet.")
    if parts:
        text += " " + ". ".join(parts) + "."
    return out, text


@tool("create_sizing", "Run a sizing",
      "Runs Scale Computing's sizing engine on a measured workload and returns up to 8 ranked "
      "cluster options. Each option has node model and count, capacity required vs available at "
      "N-1 (one node down), the resource that determined the size, rule checks and warnings. "
      "Nothing is saved: call save_sizing only when the user asks to save. Inputs must be "
      "measured values from the customer's environment (an assessment upload via "
      "start_upload/get_upload, or figures the user gives you). If the largest VM is unknown, "
      "ask; do not guess. If no option fits, options is empty and warnings says why.",
      {"type": "object", "additionalProperties": False, "properties": {
          "workload": _with_required(WORKLOAD_SCHEMA, WORKLOAD_REQUIRED),
          "from_upload": FROM_UPLOAD_SCHEMA,
          "params": PARAMS_SCHEMA,
          "max_options": {"type": "integer", "minimum": 1, "maximum": 8, "default": 5,
                          "description": "How many ranked options to return (1-8)."},
      }}, read_only=True)
def create_sizing(caller, args):
    summary, kwargs, inputs_used, warnings = _prepare(caller, args)
    try:
        result = svc.run_recommendation(summary, **kwargs)
    except svc.SizingRequestError as exc:
        raise _engine_error(exc)
    out = presenter.present_result(result, summary, inputs_used["params"],
                                   max_options=args.get("max_options", 5))
    out["warnings"] = out["warnings"] + warnings
    inputs = public_inputs(inputs_used)
    out = dict({"schema_version": SCHEMA_VERSION, "inputs_used": inputs}, **out)
    audit_event(caller, "create_sizing", "from upload %s" % inputs["from_upload"]["upload_id"]
                if inputs.get("from_upload") else "manual workload")
    return out, presenter.summary_text(out, inputs)
