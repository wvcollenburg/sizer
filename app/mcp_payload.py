"""What a sizing saved over MCP stores, in the shapes the web tool reads
(docs/mcp/01-tool-design.md §3.6; findings §9).

A saved sizing is two things: a *payload* of inputs the UI restores into its
controls (``captureSizingState`` / ``restoreSizingState`` in app.js, version
4), and a *result snapshot* the project page, compare and the exports read
without opening a browser (``buildResultSnapshot``). Both are written here
from the engine's inputs and output, in exactly the UI's shapes, so a sizing
Claude saved opens, re-sizes and exports like one saved from the screen.

The payload also carries a ``canonical`` block: the engine summary and the
resolved planning parameters by their engine names. The UI ignores it; the
MCP ``get_sizing`` tool reads it back as ``rerun_inputs``.

The DOM ids below are the single place the server knows them. A browser test
(tests/test_browser_mcp_payload.py) opens what this writes in the real UI
and re-sizes it, which is what holds this file to app.js.
"""
import os
import re
from datetime import datetime, timezone

import sizing_service as svc

SNAPSHOT_VERSION = 4
CANONICAL_VERSION = 1

# Manual-form control -> engine summary key (app.js calculateManual, reversed).
MANUAL_FIELDS = {
    "man-platform": "current_platform",
    "man-cluster": "cluster_name",
    "man-hosts": "host_count",
    "man-cores": "total_host_cores",
    "man-threads": "total_host_threads",
    "man-ghz": "total_host_ghz",
    "man-host-ram": "total_host_ram_gb",
    "man-peak-cpu": "peak_cpu_pct",
    "man-avg-cpu": "avg_cpu_pct",
    "man-peak-mem": "peak_mem_pct",
    "man-avg-mem": "avg_mem_pct",
    "man-avg-iops": "total_avg_iops",
    "man-peak-iops": "total_peak_iops",
    "man-p95-iops": "p95_iops",
    "man-nic-speed": "nic_speed_mbps",
    "man-total-vms": "total_vms",
    "man-active-vms": "active_vms",
    "man-vcpus": "total_vcpus",
    "man-prov-ram": "total_vm_provisioned_memory_gb",
    "man-used-ram": "total_vm_used_memory_gb",
    "man-prov-storage": "total_vm_provisioned_storage_tb",
    "man-ds-used": "datastore_used_tb",
    "man-ds-total": "datastore_total_tb",
    "man-largest-vcpus": "max_vm_cores",
    "man-largest-ram": "max_vm_ram_gb",
}

# The web tool's ratio control: a range input, 1-8 in steps of 0.25. A ratio
# it cannot show would be snapped on reopen and the sizing recalculated
# differently from what was saved, so save_sizing refuses such a ratio.
RATIO_MIN, RATIO_MAX, RATIO_STEP = 1.0, 8.0, 0.25


def ratio_representable(ratio):
    try:
        r = float(ratio)
    except (TypeError, ValueError):
        return False
    return RATIO_MIN <= r <= RATIO_MAX and abs(r / RATIO_STEP - round(r / RATIO_STEP)) < 1e-9


def _text(value):
    """A control value as the UI reads it from ``.value``: text, whole
    numbers without a decimal point, blank for nothing."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def shared_fields(params, guest_licensing):
    """The shared sizing-options block (``_SHARED_SIZING_FIELDS``), from the
    resolved planning parameters of §2.2."""
    separate = params.get("storage_growth_pct") is not None
    return {
        "ratio-slider": _text(params.get("vcpu_ratio")),
        "growth-years": _text(int(params.get("years") or 5)),
        "growth-pct": _text(params.get("growth_pct")),
        "separate-storage-growth": separate,
        "storage-growth-pct": _text(params.get("storage_growth_pct")) if separate else _text(params.get("growth_pct")),
        "snapshot-pct": _text(params.get("snapshot_pct")),
        "max-day-one-storage": _text(params.get("max_day_one_storage_pct")),
        "max-day-one-ram": _text(params.get("max_day_one_ram_pct")),
        "target-nodes": _text(params.get("target_nodes")),
        "storage-pref": params.get("storage_preference") or "auto",
        "sizing-mode": params.get("sizing_mode") or "certified",
        "sizing-vendor": params.get("vendor") or "",
        "size-full-cluster": bool(params.get("size_cpu_for_full_cluster")),
        "allow-storage-only": bool(params.get("allow_storage_only_nodes")),
        "sizing-model-select": params.get("target_model") or "",
        "sizing-include-eol": bool(params.get("include_eol_eos")),
        "license-term-years": _text(int(params.get("license_term_years") or 5)),
        "guest-licensing": guest_licensing or "windows",
    }


def canonical_block(summary, params, selected_rank, source="mcp"):
    return {"version": CANONICAL_VERSION, "source": source,
            "summary": dict(summary), "params": dict(params),
            "selected_option": selected_rank}


def manual_payload(summary, params, selected_rank):
    """The v4 ``manual`` payload the UI restores (SNAP_FIELDS.manual), from an
    engine summary and the resolved planning parameters."""
    fields = {}
    for dom_id, key in MANUAL_FIELDS.items():
        value = summary.get(key)
        if value is not None and value != "":
            fields[dom_id] = _text(value)
    fields.update(shared_fields(params, summary.get("guest_licensing")))
    return {
        "version": SNAPSHOT_VERSION,
        "mode": "manual",
        "fields": fields,
        # The UI's own extra (captureSizingState): the summary the form
        # produced and which listed option was chosen, keyed by mode.
        "manual": {"manualSummary": dict(summary),
                   "selectedRec": {"manual": selected_rank - 1}},
        "canonical": canonical_block(summary, params, selected_rank),
    }


# VM fields the UI drops before saving (app.js VM_UNSAVED_FIELDS).
VM_UNSAVED_FIELDS = ("disk_capacity_gb", "disk_used_gb", "used_memory_gb", "datastore", "ref")


def compact_vm(vm, src=None):
    """``_compactVm``: the saved VM record, tagged with its source file."""
    out = {k: v for k, v in vm.items() if k not in VM_UNSAVED_FIELDS}
    if out.get("uuid") and out.get("bios_uuid"):
        del out["bios_uuid"]
    if src is not None and not out.get("src"):
        out["src"] = src
    return out


def _meta_of(meta):
    """``_metaOf``: a file's provenance without the combined-meta keys."""
    m = dict(meta or {})
    m.pop("cluster", None)
    m.pop("files", None)
    return m


def source_record(sid, dataset, records):
    """``_sourceRecord``: one file's share of a sizing, for appending files
    and re-merging later. ``dataset`` is the file (its summary and meta),
    ``records`` the hosts/perf/datastores/NICs that went into this sizing."""
    return {"id": sid, "file_type": dataset.get("file_type"),
            "scan_type": dataset.get("scan_type"),
            "source_meta": _meta_of(dataset.get("source_meta")),
            "summary": dataset.get("summary"),
            "hosts": records.get("hosts") or [],
            "host_performance": records.get("host_performance") or [],
            "datastores": records.get("datastores") or [],
            "host_nics": records.get("host_nics") or []}


def combined_source_meta(metas, cluster=None):
    """``_combinedSourceMeta``: the first file's fields at the top (what
    older readers expect) plus every file in ``files`` when there are
    several, with the counts summed."""
    if not metas:
        return None
    out = dict(metas[0])
    if len(metas) > 1:
        out["files"] = list(metas)
        out["host_count"] = sum(m.get("host_count") or 0 for m in metas)
        out["vm_count"] = sum(m.get("vm_count") or 0 for m in metas)
    if cluster:
        out["cluster"] = cluster
    return out


def import_payload(upload_ctx, summary, params, selected_rank, projection):
    """The v4 ``import`` payload the UI restores (``captureSizingState`` /
    ``_fanoutSnap``) for a sizing made from an upload: the file's records,
    the VM list with the exclusions the model chose, and the per-file
    sources so files can be appended in the browser later. Returns
    ``(payload, source_meta)``; the second is the Configuration's provenance
    column (``_combinedSourceMeta``)."""
    row, dataset, spec = upload_ctx["row"], upload_ctx["dataset"], upload_ctx["spec"]
    cluster = spec.get("source_cluster")
    if row.kind == "merged":
        sources, metas = _merged_sources(row, dataset)
        vms = [compact_vm(v) for v in dataset.get("vms") or []]
    else:
        sid = (row.data or {}).get("id") or "f" + row.token[:10]
        file_level = row.data or {}
        sources = [source_record(sid, file_level, dataset)]
        metas = [_meta_of(file_level.get("source_meta"))]
        vms = [compact_vm(v, sid) for v in dataset.get("vms") or []]
    payload = {
        "version": SNAPSHOT_VERSION,
        "mode": "import",
        "fields": shared_fields(params, summary.get("guest_licensing")),
        "import": {
            "originalImportSummary": dict(dataset["summary"]),
            "importSummary": dict(summary),
            "importVms": vms,
            "vmConfig": {},
            "exclCompute": sorted({int(i) for i in spec.get("exclude_compute") or []}),
            "exclStorage": sorted({int(i) for i in spec.get("exclude_storage") or []}),
            "includeLocalStorage": bool(spec.get("include_local_storage")),
            "lastProjection": projection,
            "selectedRec": {"import": selected_rank - 1},
            "wizardStep": None,
            "sources": sources,
            "importWarnings": list(dataset.get("import_warnings") or []),
        },
        "canonical": canonical_block(summary, params, selected_rank, source="mcp_upload"),
    }
    payload["canonical"]["from_upload"] = {
        "upload_id": row.public_id, "source_cluster": cluster,
        "exclude_compute": payload["import"]["exclCompute"],
        "exclude_storage": payload["import"]["exclStorage"],
        "include_local_storage": payload["import"]["includeLocalStorage"]}
    return payload, combined_source_meta(metas, cluster)


def _merged_sources(row, target):
    """``_targetSources`` for a merge target: each source file's records
    filtered to this target (they carry ``src``), with that file's own
    summary and provenance from its upload row."""
    from mcp_uploads import McpUpload
    sources, metas = [], []
    for public_id in target.get("sources") or []:
        token = public_id.split("_", 1)[1] if "_" in public_id else public_id
        src_row = McpUpload.query.filter_by(token=token).first()
        file_level = (src_row.data if src_row else None) or {}
        mine = lambda rec, t=token: rec.get("src") == t   # noqa: E731
        records = {k: [r for r in target.get(k) or [] if mine(r)]
                   for k in ("hosts", "host_performance", "datastores", "host_nics")}
        sources.append(source_record(token, file_level, records))
        metas.append(_meta_of(file_level.get("source_meta")))
    return sources, metas


def source_perf_export(source_perf):
    """``buildSourcePerfExport``'s shape from sizing_service.source_perf_index's."""
    if not source_perf or source_perf.get("total_specrate") is None:
        return None
    cpus = []
    for c in source_perf.get("cpus") or []:
        score = float(c["perf_index"])
        specrate = score * svc.PASSMARK_TO_SPECRATE if c.get("perf_type") == "passmark" else score
        cpus.append({"model": c.get("model") or "", "sockets": c.get("sockets") or 1,
                     "type": c.get("perf_type") or "specrate", "score": score,
                     "specrate": round(specrate, 1),
                     "total": round(specrate * (c.get("sockets") or 1), 1)})
    return {"total_specrate": source_perf["total_specrate"], "cpus": cpus}


_JS_LANG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "js", "lang")
_js_strings = {}


def js_string(key, lang="en", default=None, **params):
    """A front-end UI string from the browser's own locale file
    (static/js/lang/<lang>.js), English when the locale lacks it, ``default``
    (else the key) when neither has it. The JS catalogs are not in the
    Python ones, and the MCP tools must say what the screen says — the
    snapshot cluster label, the import-warning texts. ``params`` fill the
    ``{name}`` placeholders the way the browser's t() does."""
    for code in ((lang or "en").lower(), "en"):
        table = _js_strings.get(code)
        if table is None:
            table = _js_strings[code] = _load_js_strings(code)
        if key in table:
            text = table[key]
            if params:
                try:
                    return text.format(**params)
                except (KeyError, IndexError, ValueError):
                    return text
            return text
    return key if default is None else default


_JS_ENTRY_RE = re.compile(r'^\s*"((?:[^"\\]|\\.)*)"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,?\s*$', re.M)


def _load_js_strings(lang):
    path = os.path.join(_JS_LANG_DIR, "%s.js" % lang)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return {}
    # One "key": "value" entry per line is how the files are assembled.
    return {k.encode().decode("unicode_escape"): v.encode().decode("unicode_escape")
            for k, v in _JS_ENTRY_RE.findall(text)}


def cluster_label(lang="en"):
    """The name the UI gives a single-cluster result ("Cluster"), so the
    project page reads the same for both paths. A label must never fail a
    save, hence the default."""
    return js_string("project.snapshot.cluster", lang, default="Cluster")


def import_warning_text(warning, lang="en"):
    """An ``{code, params}`` import warning as the wizard shows it."""
    code = (warning or {}).get("code") or ""
    params = (warning or {}).get("params") or {}
    if isinstance(params, dict):
        params = {k: (", ".join(str(x) for x in v) if isinstance(v, list) else v)
                  for k, v in params.items()}
    else:
        params = {}
    return js_string("wizard.warn." + code, lang, default=code, **params)


def result_snapshot(summary, rec, projection, source_perf=None, lang="en"):
    """``buildResultSnapshot``'s shape: one cluster carrying the full engine
    record of the chosen option (compare and the exports read it), the
    summary it was sized from and the projection. ``tunables`` is stamped
    by the caller, which has the database."""
    return {
        "clusters": [{
            "name": cluster_label(lang),
            "summary": dict(summary),
            "recommendation": rec,
            "projection": projection,
            "source_perf": source_perf_export(source_perf),
            "replicates_to": "",
            "refs": rec.get("refs") or {"mode": "manual"},
        }],
        "totals": None,
    }


def utcnow():
    return datetime.now(timezone.utc)
