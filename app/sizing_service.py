"""The layer between the HTTP routes and the sizing engine.

Holds what every caller of ``recommend.generate_recommendations`` should get
regardless of how the inputs arrived (a typed-in manual sizing, an import, and
the MCP server, docs/mcp/00-findings.md §9), so the routes do not each grow
their own copy:

  * ``recommend(data)``: the request parsing, defaults and error mapping that
    used to live inline in ``/api/recommend``. ``/api/recommend`` and the MCP
    ``create_sizing`` tool both call it, so a number can only ever come out
    one way (docs/mcp/01-tool-design.md §7).
  * ``lookup_cpu_perf(query)``: the ``/api/cpu-perf`` ordering (curated
    catalog first, then the broad SPECrate table).
  * ``source_perf_index(cpus)``: the socket-weighted source benchmark the
    browser computed in ``computeSourcePerf`` (app.js), server-side.
  * ``workload_to_summary`` / ``summary_to_workload``: the MCP field names
    (units in the name, docs/mcp/01-tool-design.md §2.1) to the engine's
    summary keys and back, plus ``validate_workload`` for the input checks
    the engine itself does not make.
  * ``input_quality_warnings``: plain-language caveats about the inputs.

Deliberately NOT in ``fingerprint.ENGINE_MODULES``: nothing here changes a
computed number, only how a request is read and what is said about the
inputs, so editing this module must not mark every stored sizing stale.
"""
import math

from recommend import generate_recommendations
from storage_layout import StorageLayoutError

# Planning-parameter defaults applied when a request leaves them out. These
# are the /api/recommend defaults, now in one place so MCP applies the same.
# (vcpu_ratio and the day-one caps default inside the engine, from tunables.)
RECOMMEND_DEFAULTS = {
    "vcpu_ratio": None,
    "growth_pct": 10,
    "storage_growth_pct": None,
    "snapshot_pct": 20,
    "years": 5,
    "target_nodes": None,
    "storage_pref": None,
    "size_full_cluster": False,
    "sizing_mode": "certified",
    "allow_storage_only": False,
    "target_model": None,
    "include_eol_eos": False,
    "max_day_one_storage_pct": None,
    "max_day_one_ram_pct": None,
    "source_perf_index": None,
    "source_perf_type": None,
    "replication_reserve": None,
    "replication_compute_mode": "reserved",
    "allow_single_node": False,
    "license_term_years": None,
    "guest_licensing": None,
    "vendor": None,
}

MALFORMED_INPUT_MESSAGE = ("The imported figures are incomplete or malformed. "
                           "Re-import the environment.")

# PassMark CPU Mark -> SPECrate-equivalent, the factor the engine and the
# browser both use (recommend.py, app.js computeSourcePerf).
PASSMARK_TO_SPECRATE = 0.00386

# The NIC speed the browser assumes for a manual sizing (app.js
# calculateManual). MCP applies the same default, but says so.
DEFAULT_NIC_SPEED_GBE = 10


class SizingRequestError(Exception):
    """A request the engine must not see: a bad body, malformed figures, or a
    catalog row an admin has to fix. ``status`` is the HTTP status the REST
    route answers with; ``code`` is the stable key MCP reports."""

    def __init__(self, message, status=400, code="invalid_input"):
        super().__init__(message)
        self.status = status
        self.code = code


def parse_recommend_request(data):
    """The summary and engine keyword arguments of a ``/api/recommend`` body,
    exactly as the route read them: a missing key takes its default, a key
    that is present is passed as given (so an explicit null stays null)."""
    if not isinstance(data, dict):
        raise SizingRequestError("Invalid request body", code="bad_request")
    summary = data.get("summary")
    # The summary is a bag of measured numbers the engine does arithmetic on;
    # a non-dict (or non-numeric field inside it) would otherwise surface as a
    # 500 deep in the math. Require a dict here; coerce failures are mapped to
    # a 400 in run_recommendation.
    if not isinstance(summary, dict) or not summary:
        raise SizingRequestError("No summary provided", code="bad_request")
    kwargs = {key: data.get(key, default)
              for key, default in RECOMMEND_DEFAULTS.items()}
    return summary, kwargs


def run_recommendation(summary, **kwargs):
    """Run the engine and append the input caveats, mapping the engine's
    failure modes to one error type. Every caller that sizes a summary goes
    through here."""
    try:
        result = generate_recommendations(summary, **kwargs)
    except StorageLayoutError as exc:
        # The model's catalog entry lacks a drive count: a data error for
        # the admin to fix, not a server fault.
        raise SizingRequestError(str(exc), code="catalog_error") from exc
    except (TypeError, ValueError, KeyError) as exc:
        # Malformed numeric fields in the client-supplied summary/params
        # (e.g. a non-numeric vCPU count). A bad request, not a server fault.
        raise SizingRequestError(MALFORMED_INPUT_MESSAGE) from exc
    result["warnings"] = (result.get("warnings", [])
                          + input_quality_warnings(summary))
    return result


def recommend(data):
    """A ``/api/recommend`` body in, the engine result out."""
    summary, kwargs = parse_recommend_request(data)
    return run_recommendation(summary, **kwargs)


# ── source CPU benchmark ─────────────────────────────────────────────────────

def lookup_cpu_perf(query):
    """A CPU's benchmark score by (fuzzy) description, per CPU/socket. Tries
    the curated appliance catalog first (precise), then the broad SPECrate2017
    lookup (~625 CPUs averaged from all published SPEC CPU 2017 int-rate
    results) so arbitrary SOURCE CPUs resolve too. ``found`` is False only
    when neither knows it."""
    import cpu_benchmarks
    from cpu_specs import CPU_SPECS, cpu_model_key, perf_index as _perf_index
    q = query or ""
    spec = CPU_SPECS.get(cpu_model_key(q) or "")
    if spec:
        ptype = "specrate" if spec.get("specrate_int") is not None else "passmark"
        return {
            "found": True,
            "model": spec["model"],
            "perf_type": ptype,
            "perf_index": _perf_index(spec),
            "specrate_int": spec.get("specrate_int"),
            "passmark_cpu_mark": spec.get("passmark_cpu_mark"),
            "passmark_single": spec.get("passmark_single"),
            "source": "catalog",
        }
    hit = cpu_benchmarks.lookup(q)
    if hit:
        return {
            "found": True,
            "model": hit["model"],
            "perf_type": "specrate",
            "perf_index": hit["specrate_int"],
            "specrate_int": hit["specrate_int"],
            "passmark_cpu_mark": None,
            "passmark_single": None,
            "source": "spec-cpu2017",
            "samples": hit["samples"],
        }
    return {"found": False}


def source_perf_index(cpus, lookup=lookup_cpu_perf):
    """Total source-environment throughput on the SPECrate scale: per-CPU
    score × sockets, PassMark normalised, summed over the source CPUs. The
    browser's ``computeSourcePerf`` (app.js), for callers with no browser.

    ``cpus`` is a list of ``{model, sockets, perf_index?, perf_type?}``. A CPU
    with no score is looked up; one that cannot be resolved contributes
    nothing and is listed in ``unresolved``. Returns ``{total_specrate,
    cpus, unresolved}`` with ``total_specrate`` None when nothing resolved,
    exactly as the browser reports "—"."""
    total, any_scored, detail, unresolved = 0.0, False, [], []
    for cpu in cpus or []:
        model = str(cpu.get("model") or "").strip()
        try:
            sockets = int(cpu.get("sockets") or 1)
        except (TypeError, ValueError):
            sockets = 1
        sockets = max(sockets, 1)
        score = cpu.get("perf_index")
        ptype = (cpu.get("perf_type") or "specrate").lower()
        source = "given"
        if not _positive(score):
            hit = lookup(model) if model else {"found": False}
            if hit.get("found"):
                score, ptype, source = hit["perf_index"], hit["perf_type"], hit["source"]
            else:
                unresolved.append(model)
                continue
        score = float(score)
        specrate = score * PASSMARK_TO_SPECRATE if ptype == "passmark" else score
        total += specrate * sockets
        any_scored = True
        detail.append({"model": model, "sockets": sockets, "perf_index": score,
                       "perf_type": ptype, "source": source,
                       "specrate_total": round(specrate * sockets, 1)})
    return {
        "total_specrate": round(total * 10) / 10 if any_scored else None,
        "cpus": detail,
        "unresolved": unresolved,
    }


# ── MCP workload <-> engine summary ──────────────────────────────────────────

# MCP field -> engine summary key, for the fields that map 1:1. Units sit in
# the MCP name because the engine's names mislead a model reading them
# (max_vm_cores holds vCPUs; total_vm_provisioned_memory_gb is not used RAM).
# nic_speed_gbe and source_cpus are converted, not renamed.
WORKLOAD_FIELDS = {
    "total_vcpus": "total_vcpus",
    "provisioned_ram_gb": "total_vm_provisioned_memory_gb",
    "used_storage_tb": "datastore_used_tb",
    "largest_vm_vcpus": "max_vm_cores",
    "largest_vm_ram_gb": "max_vm_ram_gb",
    "active_vms": "active_vms",
    "total_vms": "total_vms",
    "p95_iops": "p95_iops",
    "avg_iops": "total_avg_iops",
    "peak_iops": "total_peak_iops",
    "used_ram_gb": "total_vm_used_memory_gb",
    "provisioned_storage_tb": "total_vm_provisioned_storage_tb",
    "datastore_total_tb": "datastore_total_tb",
    "host_count": "host_count",
    "total_host_cores": "total_host_cores",
    "total_host_threads": "total_host_threads",
    "total_host_ghz": "total_host_ghz",
    "total_host_ram_gb": "total_host_ram_gb",
    "peak_cpu_ghz": "peak_cpu_ghz",
    "peak_cpu_pct": "peak_cpu_pct",
    "avg_cpu_pct": "avg_cpu_pct",
    "peak_mem_pct": "peak_mem_pct",
    "avg_mem_pct": "avg_mem_pct",
    "current_platform": "current_platform",
    "cluster_name": "cluster_name",
    "guest_licensing": "guest_licensing",
}
SUMMARY_TO_WORKLOAD = {v: k for k, v in WORKLOAD_FIELDS.items()}

# Measured inputs the engine cannot size without. Never defaulted: a missing
# one is an error the caller must resolve by asking (docs/mcp/01-tool-design.md
# §2.1), because the engine would read 0 as "no constraint".
REQUIRED_WORKLOAD = ("total_vcpus", "provisioned_ram_gb", "used_storage_tb",
                     "largest_vm_vcpus", "largest_vm_ram_gb")

_INT_FIELDS = {"total_vcpus", "largest_vm_vcpus", "active_vms", "total_vms",
               "p95_iops", "avg_iops", "peak_iops", "host_count",
               "total_host_cores", "total_host_threads"}
_PCT_FIELDS = {"peak_cpu_pct", "avg_cpu_pct", "peak_mem_pct", "avg_mem_pct"}
_TEXT_FIELDS = {"current_platform", "cluster_name"}
_POSITIVE_FIELDS = set(REQUIRED_WORKLOAD) | {"nic_speed_gbe"}
GUEST_LICENSING_VALUES = ("none", "windows", "windows_db")
DEFAULT_GUEST_LICENSING = "windows"     # what the engine assumes when unset (recommend._license_context)


def workload_to_summary(workload):
    """An MCP workload (§2.1 names) as the engine summary dict.

    Returns ``(summary, defaults_applied)``. Optional fields that are absent
    stay absent, never 0, so the engine's own ``.get`` defaults apply. The one
    exception is the NIC speed, which the engine requires: it takes the
    browser's manual-input default and is reported in ``defaults_applied``.
    Validation is ``validate_workload``'s job; this only maps."""
    summary, defaults = {}, []
    for field, key in WORKLOAD_FIELDS.items():
        if field in workload and workload[field] is not None:
            summary[key] = workload[field]
    nic = workload.get("nic_speed_gbe")
    if nic is None:
        nic = DEFAULT_NIC_SPEED_GBE
        defaults.append("nic_speed_gbe")
    summary["nic_speed_mbps"] = _as_number(nic) * 1000 if _is_number(nic) else nic
    cpus = workload.get("source_cpus")
    if cpus:
        summary["source_cpus"] = [
            {"model": str(c.get("model") or "").strip(),
             "sockets": c.get("sockets") or 1,
             "hosts": c.get("hosts") or 0}
            for c in cpus]
    cores = summary.get("total_host_cores") or 0
    vcpus = summary.get("total_vcpus") or 0
    if _is_number(cores) and _is_number(vcpus) and _as_number(cores) > 0:
        summary["vcpu_per_core_ratio"] = round(_as_number(vcpus) / _as_number(cores), 2)
    return summary, defaults


def summary_to_workload(summary):
    """The inverse: an engine summary (from a parser, a stored result or a
    manual sizing) as MCP workload fields. Keys the engine knows and MCP does
    not (per-host averages, ratios, the source tag) are left out."""
    workload = {}
    for key, field in SUMMARY_TO_WORKLOAD.items():
        if key in summary and summary[key] is not None:
            workload[field] = summary[key]
    mbps = summary.get("nic_speed_mbps")
    if _is_number(mbps) and _as_number(mbps) > 0:
        gbe = _as_number(mbps) / 1000
        workload["nic_speed_gbe"] = int(gbe) if gbe == int(gbe) else gbe
    cpus = summary.get("source_cpus")
    if cpus:
        workload["source_cpus"] = [
            {"model": c.get("model") or "", "sockets": c.get("sockets") or 1}
            for c in cpus if c.get("model")]
    return workload


def validate_workload(workload):
    """What the schema cannot say and the engine does not check: required
    measured fields present, numbers in range, enums known, plus advisory
    plausibility flags for figures that are probably the wrong figure.

    Returns ``{"missing": [...], "invalid": [{field, problem}],
    "plausibility": [{field, code, detail}]}``. Only ``missing`` and
    ``invalid`` make the input unusable; ``plausibility`` never blocks."""
    missing, invalid, flags = [], [], []
    if not isinstance(workload, dict):
        return {"missing": list(REQUIRED_WORKLOAD), "invalid": [], "plausibility": []}

    for field in REQUIRED_WORKLOAD:
        if workload.get(field) is None:
            missing.append(field)

    for field in list(WORKLOAD_FIELDS) + ["nic_speed_gbe"]:
        value = workload.get(field)
        if value is None:
            continue
        if field == "guest_licensing":
            if value not in GUEST_LICENSING_VALUES:
                invalid.append({"field": field, "problem": "must be one of "
                                + ", ".join(GUEST_LICENSING_VALUES)})
            continue
        if field in _TEXT_FIELDS:
            if not isinstance(value, str):
                invalid.append({"field": field, "problem": "must be text"})
            continue
        if not _is_number(value):
            invalid.append({"field": field, "problem": "must be a number"})
            continue
        number = _as_number(value)
        if field in _INT_FIELDS and number != int(number):
            invalid.append({"field": field, "problem": "must be a whole number"})
        elif field in _POSITIVE_FIELDS and number <= 0:
            invalid.append({"field": field, "problem": "must be greater than 0"})
        elif number < 0:
            invalid.append({"field": field, "problem": "must not be negative"})
        elif field in _PCT_FIELDS and number > 100:
            invalid.append({"field": field, "problem": "must be between 0 and 100"})

    cpus = workload.get("source_cpus")
    if cpus is not None:
        if not isinstance(cpus, list):
            invalid.append({"field": "source_cpus", "problem": "must be a list"})
        else:
            for i, cpu in enumerate(cpus):
                if not isinstance(cpu, dict) or not str(cpu.get("model") or "").strip():
                    invalid.append({"field": "source_cpus[%d].model" % i,
                                    "problem": "must name the CPU"})
                elif cpu.get("perf_type") not in (None, "specrate", "passmark"):
                    invalid.append({"field": "source_cpus[%d].perf_type" % i,
                                    "problem": "must be specrate or passmark"})

    if not missing and not invalid:
        flags = _plausibility(workload)
    return {"missing": missing, "invalid": invalid, "plausibility": flags}


# Advisory thresholds (docs/mcp/01-tool-design.md §3.4). Calibrated so no
# real assessment import trips them; they say "check this", never "no".
RAM_PER_VCPU_HIGH_GB = 32
RAM_PER_VCPU_LOW_GB = 0.5
GHZ_PER_HOST_FLOOR = 10


def _plausibility(w):
    flags = []

    def flag(field, code, detail):
        flags.append({"field": field, "code": code, "detail": detail})

    vcpus = _as_number(w["total_vcpus"])
    ram = _as_number(w["provisioned_ram_gb"])
    per_vcpu = ram / vcpus if vcpus else 0
    if per_vcpu > RAM_PER_VCPU_HIGH_GB:
        flag("provisioned_ram_gb", "ram_per_vcpu_high",
             "%.0f GB of RAM per vCPU is unusual; check this is the VMs' "
             "configured RAM, not the hosts' RAM." % per_vcpu)
    elif per_vcpu < RAM_PER_VCPU_LOW_GB:
        flag("provisioned_ram_gb", "ram_per_vcpu_low",
             "%.2f GB of RAM per vCPU is unusual; check the RAM is in GB "
             "and covers every powered-on VM." % per_vcpu)
    if _as_number(w["largest_vm_vcpus"]) > vcpus:
        flag("largest_vm_vcpus", "largest_vm_exceeds_total",
             "The largest VM has more vCPUs than the whole estate.")
    if _as_number(w["largest_vm_ram_gb"]) > ram:
        flag("largest_vm_ram_gb", "largest_vm_exceeds_total",
             "The largest VM has more RAM than the whole estate.")
    used_ram = w.get("used_ram_gb")
    if used_ram is not None and _as_number(used_ram) > ram:
        flag("used_ram_gb", "used_gt_provisioned_ram",
             "Used RAM is above provisioned RAM; the two may be swapped.")
    ds_total = w.get("datastore_total_tb")
    if ds_total is not None and _as_number(w["used_storage_tb"]) > _as_number(ds_total):
        flag("used_storage_tb", "storage_gt_datastore",
             "Used storage is above the datastore capacity; the two may be swapped.")
    prov_storage = w.get("provisioned_storage_tb")
    if prov_storage is not None and _as_number(w["used_storage_tb"]) > _as_number(prov_storage):
        flag("used_storage_tb", "used_gt_provisioned_storage",
             "Used storage is above the VMs' provisioned storage: snapshots, powered-off VMs "
             "or non-VM files on the datastores usually explain it. The sizing uses the used "
             "figure.")
    active = w.get("active_vms")
    if active is not None and vcpus < _as_number(active):
        flag("total_vcpus", "vcpus_lt_vms",
             "There are fewer vCPUs than powered-on VMs.")
    hosts, ghz = w.get("host_count"), w.get("total_host_ghz")
    if hosts and ghz and _as_number(ghz) < GHZ_PER_HOST_FLOOR * _as_number(hosts):
        flag("total_host_ghz", "ghz_looks_per_cpu",
             "Total host GHz looks like a per-CPU clock speed; it should be "
             "the sum over hosts of cores × GHz.")
    return flags


# ── helpers ──────────────────────────────────────────────────────────────────

def _is_number(value):
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return not (isinstance(value, float) and math.isnan(value))
    try:
        float(value)
        return isinstance(value, str) and value.strip() != ""
    except (TypeError, ValueError):
        return False


def _as_number(value):
    return float(value)


def _positive(value):
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def input_quality_warnings(summary):
    """Plain-language caveats about the inputs themselves, appended to the
    engine's own warnings.

    The engine reads a missing or zero largest VM as "no constraint" and skips
    the largest-VM fit check (``recommend._fit_model``): a sizing typed in
    without it could recommend nodes the biggest VM cannot run on, with nothing
    on screen saying so. Non-fatal on purpose; the sizing is still useful, it
    just has to be checked by hand."""
    missing = []
    if not _positive(summary.get("max_vm_cores")):
        missing.append("vCPUs")
    if not _positive(summary.get("max_vm_ram_gb")):
        missing.append("RAM")
    if not missing:
        return []
    return [
        f"Largest VM {' and '.join(missing)} unknown, so the check that the "
        "largest VM fits on a single node was skipped and this sizing may be "
        "wrong. Verify that no VM needs more vCPUs than a node's logical "
        "processors or more RAM than a node's usable RAM, or enter the largest "
        "VM to have it checked."
    ]
