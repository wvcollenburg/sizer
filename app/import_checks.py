"""Data-quality checks for an imported Live Optics / RVTools environment.

These surface caveats the user should be aware of before trusting a sizing —
things the parsers otherwise swallow silently (a garbage/absent cell becomes 0).
They describe the QUALITY of the imported data, not the recommendation (that's
recommend.generate_recommendations' own `warnings`).

Returned as {code, params} objects, NOT prose, so the client can translate them
(see wizard.js renderEnvCaveats + the wizard.warn.* i18n keys). Keep this in sync
with those keys when adding a code.
"""


def build_import_warnings(data, file_type=None):
    """Inspect an assembled parse result and return a list of {code, params}
    caveats, most-important first. Pure/read-only; safe on partial data."""
    summary = (data or {}).get("summary") or {}
    hosts = (data or {}).get("hosts") or []
    warnings = []

    def num(key):
        v = summary.get(key)
        return v if isinstance(v, (int, float)) else 0

    # No measured storage IOPS (RVTools never has any; some Live Optics exports
    # omit it). Storage then falls back to default per-drive IOPS.
    if num("total_avg_iops") == 0 and num("total_peak_iops") == 0:
        warnings.append({"code": "no_iops", "params": {}})

    # No measured CPU/memory performance — perf-based sizing can't scale by
    # utilization and falls back to provisioned figures.
    if num("peak_cpu_ghz") == 0 and num("avg_cpu_ghz") == 0:
        warnings.append({"code": "no_perf", "params": {}})

    # Physical-server ("general") scans have no hypervisor overcommit to measure,
    # so a vCPU:core ratio was assumed.
    if summary.get("vcpu_ratio_assumed"):
        ratio = int(round(num("vcpu_per_core_ratio"))) or 3
        warnings.append({"code": "assumed_ratio", "params": {"ratio": ratio}})

    # Some hosts carry no cluster name while others do — they collapse into one
    # unclustered group. (All-blank, e.g. a general scan, is normal — skip that.)
    blank = sum(1 for h in hosts if not (h.get("cluster") or "").strip())
    if blank and blank < len(hosts):
        warnings.append({"code": "unclustered_hosts", "params": {"count": blank}})

    # Only per-host local storage was found (no shared/cluster datastores). It's
    # excluded from the sizing basis unless the user opts in.
    if num("datastore_total_tb") == 0 and num("local_total_tb") > 0:
        warnings.append({"code": "local_only_storage", "params": {}})

    return warnings
