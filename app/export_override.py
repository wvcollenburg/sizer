"""Export customization: describe the hardware the partner actually sells.

A Validated recommendation is sized on one vendor chassis, but the VAR quoting
it may well sell another one — we size a Dell R660, the VAR BOM-checks an R670.
The proposal then has to describe the R670, or it describes hardware nobody is
ordering.

Three sources, merged PER FIELD (owner decision, 2026-09-16):

    manual override  >  linked BOM check  >  the recommendation

so overriding only the chassis still leaves CPU/RAM/disks coming from the BOM,
and a manual entry never has to restate what the BOM already got right.

What the quoted hardware changes (owner decisions 2026-09-16 and 2026-09-17):

  * the chassis name/vendor/form factor and the per-node hardware — CPU,
    cores, threads, RAM, disks, node count — plus every cluster figure that
    follows from them: totals, N-1, usable storage (the engine's own RF2 +
    rebuild-disk maths), the vCPU:core ratio and the IOPS totals;
  * the utilization bars. The workload's DEMAND is kept as sized — it is the
    same workload — and only the CAPACITY is replaced, so a bar answers "does
    this workload fit the box being ordered". A quote that is too small goes
    past 100 % and the renderers show it (red, rescaled axis) rather than
    clamping it to "exactly full";
  * the compute floor coverage, from the quoted CPU's GHz/benchmark when they
    are known, and dropped rather than guessed when they are not;
  * the licence requirement, recomputed from the quoted cores/RAM/layout.

Unchanged: which resource drove the SIZING and what it required, and the
ranking score — they explain the engine's choice, not the quote. The
rationale's "achieved" and headroom are capacity, so they follow the quote.

Exports name only the quoted chassis — no "sized on X, quoted as Y" note
(owner decision): the document describes what is being bought. While an
override exists the quoted hardware always wins, whichever engine option is
picked (owner decision 2026-09-17).

Validated sizings only. A Certified recommendation is a Scale appliance and
keeps its SC model name.
"""
from typing import Any, Dict, List, Optional

# ── the stored setting ───────────────────────────────────────────────────────
# Configuration.export_override, a JSON dict:
#   {"bom": "auto" | "none" | <bom_check_id>, "manual": {field: value, ...}}
# NULL/absent is AUTO with no manual fields, i.e. today's behaviour plus "a
# passing BOM check applies by itself once it is linked" (owner decision).
BOM_AUTO = "auto"
BOM_NONE = "none"

# Manual fields a person may set. Anything else in the stored dict is ignored
# rather than trusted into a rendered document. `storage_desc` is the pre-2026-
# 09-17 free-text storage line: still honoured as a label for settings saved
# before the structured `drives` field existed, no longer offered by the UI.
TEXT_FIELDS = ("chassis", "vendor", "form_factor", "cpu", "storage_desc")
INT_FIELDS = ("cores_per_node", "threads_per_node", "ram_per_node_gb", "node_count")
MANUAL_FIELDS = TEXT_FIELDS + INT_FIELDS + ("drives",)

# Structured manual disks: up to two tiers (a hybrid node is capacity + cache).
DRIVE_KINDS = {"nvme": "NVMe", "ssd": "SSD", "hdd": "HDD"}
MAX_DRIVE_TIERS = 2
MAX_DRIVES_PER_TIER = 64
MAX_DRIVE_TB = 1000.0

_MAX_TEXT = 120


# ── normalising what gets stored ─────────────────────────────────────────────

def _clean_drives(raw: Any) -> List[Dict[str, Any]]:
    """[{kind, capacity_tb, qty_per_node}] from the dialog's drive rows. A row
    missing its count, size or a known type is dropped — an incomplete row is
    "not filled in", not "zero disks"."""
    out = []  # type: List[Dict[str, Any]]
    if not isinstance(raw, list):
        return out
    for row in raw[:MAX_DRIVE_TIERS]:
        if not isinstance(row, dict):
            continue
        kind = DRIVE_KINDS.get(str(row.get("type") or row.get("kind") or "").strip().lower())
        try:
            size = float(str(row.get("size_tb", row.get("capacity_tb", ""))).replace(",", "."))
            count = int(float(row.get("count", row.get("qty_per_node", ""))))
        except (TypeError, ValueError):
            continue
        if not kind or not (0 < size <= MAX_DRIVE_TB) or not (0 < count <= MAX_DRIVES_PER_TIER):
            continue
        out.append({"kind": kind, "capacity_tb": round(size, 3), "qty_per_node": count})
    return out


def clean_manual(raw: Any) -> Dict[str, Any]:
    """Keep only known fields with usable values. Empty string clears a field
    (the UI's way of falling back to the BOM/recommendation), so it is dropped
    rather than stored as "".
    """
    out = {}  # type: Dict[str, Any]
    if not isinstance(raw, dict):
        return out
    for key in TEXT_FIELDS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()[:_MAX_TEXT]
    for key in INT_FIELDS:
        value = raw.get(key)
        if value is None or value == "":
            continue
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            continue
        # A zero or negative node count would divide the cluster totals to
        # nothing; a person clearing the field is expressing "use the source
        # value", which is the same as not storing it.
        if number > 0:
            out[key] = number
    drives = _clean_drives(raw.get("drives"))
    if drives:
        out["drives"] = drives
    return out


def clean_setting(raw: Any) -> Dict[str, Any]:
    """Normalise a stored/posted override record."""
    raw = raw if isinstance(raw, dict) else {}
    bom = raw.get("bom", BOM_AUTO)
    if bom not in (BOM_AUTO, BOM_NONE):
        try:
            bom = int(bom)
        except (TypeError, ValueError):
            bom = BOM_AUTO
    return {"bom": bom, "manual": clean_manual(raw.get("manual"))}


def is_default(setting: Optional[Dict[str, Any]]) -> bool:
    """True when the setting asks for nothing beyond the default behaviour."""
    if not setting:
        return True
    cleaned = clean_setting(setting)
    return cleaned["bom"] == BOM_AUTO and not cleaned["manual"]


# ── choosing the BOM check ───────────────────────────────────────────────────

def _fit_is_red(check) -> bool:
    return (check.fit_verdict or "") == "smaller"


def _technically_failed(check) -> bool:
    return (check.technical_verdict or "") == "FAIL"


def _has_usable_cluster(check) -> bool:
    return bool(_bom_cluster(check))


def candidate_checks(sizing) -> List[Any]:
    """BOM checks linked to this sizing that could drive its export, newest
    first. A check with no usable hardware block is not offered at all — there
    would be nothing to show."""
    from bom_models import BomCheck
    # Ordered by creation, not by checked_at: a re-check updates the row in
    # place, so "newest" is the newest upload either way, and created_at is
    # never NULL (no dialect-specific NULLS LAST needed).
    rows = (BomCheck.query
            .filter_by(configuration_id=sizing.id, is_deleted=False)
            .order_by(BomCheck.created_at.desc(), BomCheck.id.desc())
            .all())
    return [c for c in rows if _has_usable_cluster(c)]


def suggested_check(sizing, checks=None):
    """What AUTO picks: the most recent check that neither failed technically
    nor came out under the requirement. A failing BOM can still be chosen by
    hand — deliberately, and never by itself (owner decision)."""
    checks = candidate_checks(sizing) if checks is None else checks
    for check in checks:
        if not _technically_failed(check) and not _fit_is_red(check):
            return check
    return None


def _resolve_check(sizing, setting, checks=None):
    """(check, auto) for a normalised setting. ``checks`` may be pre-loaded
    (the project page resolves a whole list in one query)."""
    bom = setting["bom"]
    if bom == BOM_NONE:
        return None, False
    checks = candidate_checks(sizing) if checks is None else checks
    if bom == BOM_AUTO:
        return suggested_check(sizing, checks), True
    chosen = next((c for c in checks if c.id == bom), None)
    # A check that was deleted or unlinked since it was chosen falls back to
    # AUTO rather than silently reverting the export to the recommendation.
    if chosen is None:
        return suggested_check(sizing, checks), True
    return chosen, False


# ── reading a BOM check ──────────────────────────────────────────────────────

def _bom_cluster(check) -> Optional[Dict[str, Any]]:
    """The per-node/cluster block bom/fit.py derived from the BOM, or None.

    This is the same arithmetic the engine uses (bom/fit.cluster_from_nodes),
    which is why the figures can be dropped into a recommendation at all.
    """
    fit = ((check.result or {}).get("fit")) or {}
    cluster = fit.get("cluster") or {}
    if not cluster.get("node_count") or not cluster.get("cores_per_node"):
        return None
    return cluster


def _bom_platform(check) -> Dict[str, Any]:
    """Chassis identity from the technical section's first identified platform."""
    technical = ((check.result or {}).get("technical")) or {}
    for config in technical.get("config_results") or []:
        platform = config.get("platform") or {}
        if platform.get("server"):
            return platform
    return {}


def _fmt_tb(value: float) -> str:
    """7.68 -> '7.68', 8.0 -> '8'."""
    return ("%.2f" % value).rstrip("0").rstrip(".")


def drives_desc(drives: List[Dict[str, Any]]) -> Optional[str]:
    """'4 x 7.68 TB NVMe + 3 x 8 TB HDD' for the per-node storage row."""
    parts = ["%d x %s TB %s" % (int(d["qty_per_node"]), _fmt_tb(float(d["capacity_tb"])), d["kind"])
             for d in drives if d.get("qty_per_node")]
    return " + ".join(parts) or None


def _count_only_desc(cluster) -> Optional[str]:
    """'6 x NVMe (18.4 TB raw)' — for BOM checks stored before bom/fit.py kept
    drive sizes (2026-09-17). Re-checking the BOM upgrades it to named disks."""
    counts = cluster.get("drive_counts") or {}
    parts = [f"{int(n)} x {kind}" for kind, n in sorted(counts.items()) if n]
    if not parts:
        return None
    desc = " + ".join(parts)
    raw = cluster.get("raw_storage_tb")
    nodes = cluster.get("node_count")
    if raw and nodes:
        desc += f" ({round(float(raw) / float(nodes), 1)} TB raw)"
    return desc


def bom_values(check) -> Dict[str, Any]:
    """The override fields a BOM check supplies."""
    cluster = _bom_cluster(check)
    if not cluster:
        return {}
    platform = _bom_platform(check)
    values = {}  # type: Dict[str, Any]
    if platform.get("server"):
        server = str(platform["server"])
        brand = (platform.get("brand") or check.vendor or "").strip()
        # 'PowerEdge R670' on a Dell BOM reads as 'Dell PowerEdge R670', but a
        # server line that already names its brand must not be doubled.
        if brand and not server.lower().startswith(brand.lower()):
            server = f"{brand.title()} {server}"
        values["chassis"] = server
    if platform.get("brand") or check.vendor:
        values["vendor"] = (platform.get("brand") or check.vendor)
    if platform.get("form_factor"):
        values["form_factor"] = platform["form_factor"]
    for key in ("cpu", "cores_per_node", "threads_per_node", "ram_per_node_gb", "node_count"):
        value = cluster.get(key)
        if value:
            values[key] = value
    if cluster.get("drives"):
        values["drives"] = [dict(d) for d in cluster["drives"]]
    else:
        desc = _count_only_desc(cluster)
        if desc:
            values["storage_desc"] = desc
    # Carried for the rebuild below, not user-settable.
    values["_cluster"] = cluster
    return values


# ── resolving the effective override ─────────────────────────────────────────

def resolve(sizing, checks=None) -> Optional[Dict[str, Any]]:
    """The effective override for a sizing, or None when its export should show
    the recommendation unchanged.

    Never raises: an export must not fail because a BOM check is malformed.
    """
    try:
        setting = clean_setting(getattr(sizing, "export_override", None))
        check, auto = _resolve_check(sizing, setting, checks)
        values = bom_values(check) if check is not None else {}
        cluster = values.pop("_cluster", None)
        manual = setting["manual"]
        # Per-field merge: manual wins field by field over the BOM. Structured
        # manual disks also retire the BOM's legacy count-only storage line.
        merged = dict(values)
        if "drives" in manual:
            merged.pop("storage_desc", None)
        merged.update(manual)
        if not merged:
            return None
        return {
            "values": merged,
            "manual": manual,
            "cluster": cluster,
            "manual_fields": sorted(manual),
            "source": ("manual" if manual and not values
                       else "mixed" if manual else "bom"),
            "bom_check_id": check.id if check is not None else None,
            "bom_check_name": check.name if check is not None else None,
            "bom_auto": auto,
            "bom_technical_verdict": check.technical_verdict if check is not None else None,
            "bom_fit_verdict": check.fit_verdict if check is not None else None,
        }
    except Exception:  # pragma: no cover - defensive
        return None


# ── applying it to a recommendation ──────────────────────────────────────────

def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _num(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def applies_to(rec: Dict[str, Any]) -> bool:
    """Validated recommendations only (owner decision)."""
    return bool(isinstance(rec, dict) and rec.get("validated"))


def _layout(total_nodes: int) -> List[int]:
    from recommend import _cluster_layout
    return _cluster_layout(total_nodes)


def _usable_storage(raw_per_node: float, biggest: float, bays: int,
                    layout: List[int]) -> float:
    """The engine's usable-storage maths (RF2 plus one rebuild disk per
    cluster), with bom/fit.py's Single Node System rule for a one-node build:
    RF2 mirrors within the node and holds no rebuild disk; a single disk cannot
    mirror at all."""
    from recommend import _cluster_usable_storage
    if sum(layout) > 1:
        return _cluster_usable_storage(raw_per_node, biggest, layout)
    return raw_per_node if bays <= 1 else raw_per_node / 2


def apply_to_rec(rec: Dict[str, Any], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A copy of ``rec`` describing the quoted hardware.

    The original is never mutated: the same snapshot feeds the comparison, the
    exports and the API, and a shared dict edited in place would leak the
    override into places that asked for the sized figures.
    """
    if not override or not applies_to(rec):
        return rec
    values = override.get("values") or {}
    if not values:
        return rec
    manual = override.get("manual") or {}
    cluster = override.get("cluster") or {}
    out = dict(rec)

    # ── identity ────────────────────────────────────────────────────────────
    chassis = values.get("chassis")
    if chassis:
        # Both display helpers (hcl_vendor.rec_display_model and the JS
        # recDisplayModel) read vendor_chassis first, so setting it renames the
        # recommendation everywhere at once. `model` stays the SC model: it is
        # catalog identity — refs, fingerprint and the BOM fit key ride on it.
        out["vendor_chassis"] = chassis
        out["chassis"] = chassis
    if values.get("vendor"):
        out["vendor"] = values["vendor"]
    if values.get("form_factor"):
        out["form_factor"] = values["form_factor"]
        out["category"] = values["form_factor"]

    hw = _apply_hardware(out, rec, values, manual, cluster)
    if hw["changed"]:
        _rebuild_cluster(out, rec, hw)
        _rebuild_ratio(out, rec)
        _rebuild_compute_floor(out, rec, hw)
        _rebuild_licensing(out, rec)
        _rebuild_determinant(out, rec)
        _rebuild_utilization(out, rec)
        _rebuild_iops(out, rec, hw)
    out["export_override"] = _badge(override)
    return out


def _apply_hardware(out, rec, values, manual, cluster) -> Dict[str, Any]:
    """Per-node figures onto ``out``. Returns what the cluster rebuild needs:
    which parts changed and where the CPU's clock/benchmark now come from."""
    hw = {"changed": False, "cpu_changed": False, "cpu_known": True,
          "ghz_per_node": None, "perf_per_node": None,
          "nodes_changed": False, "storage_changed": False,
          "raw_per_node": None, "biggest": None, "bays": None,
          "layout": None, "storage_usable": None, "storage_raw": None}

    # A CPU typed by hand has no catalog behind it: its clock and benchmark are
    # unknown, so figures derived from them are dropped, never guessed.
    manual_cpu = any(k in manual for k in ("cpu", "cores_per_node", "threads_per_node"))
    bom_cpu = bool(cluster) and not manual_cpu and any(
        k in values for k in ("cpu", "cores_per_node", "threads_per_node"))

    if values.get("cpu") and values["cpu"] != rec.get("cpu"):
        out["cpu"] = values["cpu"]
        hw["cpu_changed"] = True
    cores = _int(values.get("cores_per_node"))
    if cores and cores != rec.get("cores_per_node"):
        # The OS core overhead is a platform property, carried across from the
        # sized config: the quoted node loses the same slice the sized one did.
        overhead = max((rec.get("cores_per_node") or 0) - (rec.get("usable_cores_per_node") or 0), 0)
        out["cores_per_node"] = cores
        out["usable_cores_per_node"] = max(cores - overhead, 0)
        hw["cpu_changed"] = True
    threads = _int(values.get("threads_per_node"))
    if not threads and cores and "cores_per_node" in manual and rec.get("cores_per_node"):
        # Cores typed without threads: keep the sized CPU's threads-per-core
        # (hyperthreading on or off), rather than 16 cores beside 64 threads.
        threads = int(round(cores * (rec.get("threads_per_node") or 0)
                            / float(rec["cores_per_node"])))
    if threads and threads != rec.get("threads_per_node"):
        out["threads_per_node"] = threads
        hw["cpu_changed"] = True

    if hw["cpu_changed"]:
        hw["changed"] = True
        if bom_cpu:
            hci_bom = cluster.get("hci_node_count") or cluster.get("node_count") or 0
            if hci_bom and cluster.get("ghz_full"):
                hw["ghz_per_node"] = float(cluster["ghz_full"]) / hci_bom
                eff = out.get("cores_per_node") or 0
                if eff:
                    out["ghz"] = round(hw["ghz_per_node"] / eff, 3)
            if hci_bom and cluster.get("perf_full"):
                hw["perf_per_node"] = float(cluster["perf_full"]) / hci_bom
                out["cpu_perf_index"] = round(hw["perf_per_node"], 1)
            else:
                out["cpu_perf_index"] = None
            out["cpu_generation"] = None
        else:
            hw["cpu_known"] = False
            out["cpu_perf_index"] = None
            out["cpu_generation"] = None
    else:
        if rec.get("ghz") and rec.get("cores_per_node"):
            hw["ghz_per_node"] = float(rec["ghz"]) * rec["cores_per_node"]
        hw["perf_per_node"] = _num(rec.get("cpu_perf_index"))

    ram = _int(values.get("ram_per_node_gb"))
    if ram and ram != rec.get("ram_per_node_gb"):
        if cluster.get("usable_ram_per_node_gb") and "ram_per_node_gb" not in manual:
            usable_ram = cluster["usable_ram_per_node_gb"]
        else:
            overhead = max((rec.get("ram_per_node_gb") or 0) - (rec.get("usable_ram_per_node_gb") or 0), 0)
            usable_ram = max(ram - overhead, 0)
        out["ram_per_node_gb"] = ram
        out["usable_ram_per_node_gb"] = usable_ram
        hw["changed"] = True

    # ── nodes ───────────────────────────────────────────────────────────────
    nodes = _int(values.get("node_count"))
    total = rec.get("node_count") or 0
    if nodes and nodes != rec.get("node_count"):
        total = nodes
        out["node_count"] = nodes
        # node_count is the WHOLE cluster, so the HCI count is what is left
        # after any storage-only nodes. A BOM-derived cluster states its own
        # split (bom/fit.py works it out from the BOM); for a manual override
        # the sizing's storage-only block still applies, and a quoted count at
        # or below it would leave no HCI node at all, so it is floored at 1.
        bom_nodes = bool(cluster) and "node_count" not in manual
        so_count = cluster.get("so_count") if bom_nodes else None
        if so_count is None:
            so_count = ((rec.get("storage_only") or {}).get("count") or 0)
        hci = cluster.get("hci_node_count") if bom_nodes else None
        out["hci_node_count"] = hci or (max(nodes - so_count, 1) if so_count else nodes)
        out["single_node"] = nodes == 1
        hw["nodes_changed"] = hw["changed"] = True
    layout = (list(cluster["cluster_layout"])
              if cluster.get("cluster_layout") and "node_count" not in manual
              and sum(cluster["cluster_layout"]) == total
              else (_layout(total) if hw["nodes_changed"] else list(rec.get("cluster_layout") or [total])))
    hw["layout"] = layout
    if hw["nodes_changed"]:
        out["cluster_layout"] = layout
        out["num_clusters"] = len(layout)
    if cluster.get("nic_ports") and "node_count" not in manual:
        out["nic_ports"] = cluster["nic_ports"]

    # ── storage ─────────────────────────────────────────────────────────────
    drives = values.get("drives")
    storage = dict(rec.get("storage_config") or {})
    if drives:
        raw_pn = sum(float(d["capacity_tb"]) * int(d["qty_per_node"]) for d in drives)
        biggest = max(float(d["capacity_tb"]) for d in drives)
        bays = sum(int(d["qty_per_node"]) for d in drives)
        storage.update({"desc": drives_desc(drives), "raw_per_node": round(raw_pn, 3),
                        "biggest_disk": biggest,
                        "drive_counts": _drive_counts(drives)})
        hw.update(raw_per_node=raw_pn, biggest=biggest, bays=bays, storage_changed=True)
    elif values.get("storage_desc"):
        storage["desc"] = values["storage_desc"]
        if cluster.get("usable_storage_tb") and "node_count" not in manual:
            # A BOM check from before drive sizes were stored: its own totals
            # are exact for its own layout, so they are used as they are.
            hw.update(storage_usable=float(cluster["usable_storage_tb"]),
                      storage_raw=_num(cluster.get("raw_storage_tb")),
                      storage_changed=True)
    elif hw["nodes_changed"] and storage.get("raw_per_node"):
        # Same disks per node, another node count: the sized disks re-laid-out
        # are exact, not an estimate.
        hw.update(raw_per_node=float(storage["raw_per_node"]),
                  biggest=float(storage.get("biggest_disk") or 0),
                  bays=sum((storage.get("drive_counts") or {}).values()) or 2)
    if hw["storage_changed"]:
        hw["changed"] = True
    out["storage_config"] = storage
    return hw


def _drive_counts(drives: List[Dict[str, Any]]) -> Dict[str, int]:
    counts = {}  # type: Dict[str, int]
    for d in drives:
        counts[d["kind"]] = counts.get(d["kind"], 0) + int(d["qty_per_node"])
    return counts


def _rebuild_cluster(out, rec, hw) -> None:
    """Totals and N-1 from the quoted per-node figures, with the engine's own
    N-1 rule (calc._n_minus_1_block: one node held back per cluster)."""
    from calc import _n_minus_1_block

    total_nodes = out.get("node_count") or 0
    hci = out.get("hci_node_count") or total_nodes
    layout = hw["layout"] or [total_nodes]
    usable_cores = out.get("usable_cores_per_node") or 0
    threads = out.get("threads_per_node") or 0
    usable_ram = out.get("usable_ram_per_node_gb") or 0
    ghz_pn = hw["ghz_per_node"] if hw["ghz_per_node"] is not None else (
        float(out.get("ghz") or 0) * (out.get("cores_per_node") or 0))

    totals = dict(rec.get("totals") or {})
    totals["cores"] = usable_cores * hci
    totals["threads"] = threads * hci
    totals["total_ghz"] = round(ghz_pn * hci, 1)
    totals["ram_gb"] = round(usable_ram * hci, 1)
    if hw["perf_per_node"] is not None:
        totals["perf_index"] = round(hw["perf_per_node"] * hci, 1)
    elif "perf_index" in totals:
        totals["perf_index"] = None

    if hw["raw_per_node"] is not None:
        usable = _usable_storage(hw["raw_per_node"], hw["biggest"] or 0.0,
                                 hw["bays"] or 0, layout)
        totals["raw_storage_tb"] = round(hw["raw_per_node"] * total_nodes, 2)
        totals["usable_storage_tb"] = round(usable, 2)
    elif hw["storage_usable"] is not None:
        totals["usable_storage_tb"] = round(hw["storage_usable"], 2)
        if hw["storage_raw"] is not None:
            totals["raw_storage_tb"] = round(hw["storage_raw"], 2)
    out["totals"] = totals

    n1 = dict(rec.get("n_minus_1") or {})
    block = _n_minus_1_block(hci, len(layout), usable_cores, threads, ghz_pn,
                             usable_ram, totals.get("usable_storage_tb") or 0)
    for key in ("cores", "threads", "total_ghz", "ram_gb", "usable_storage_tb"):
        n1[key] = round(block[key], 1) if key in ("total_ghz", "ram_gb") else block[key]
    out["n_minus_1"] = n1


def _rebuild_ratio(out, rec) -> None:
    """vCPU:core ratio for the quoted cores. The engine divides the workload's
    vCPUs by the sizing basis (N-1 cores, or all cores when sizing for the full
    cluster); the vCPUs are unchanged, so the ratio scales exactly with the
    inverse of the core count."""
    def scaled(ratio, old, new):
        if ratio is None or not old or not new:
            return ratio
        return round(float(ratio) * float(old) / float(new), 2)

    full = bool(rec.get("sized_full_cluster"))
    old_basis = (rec.get("totals") or {}).get("cores") if full else (rec.get("n_minus_1") or {}).get("cores")
    new_basis = out["totals"].get("cores") if full else out["n_minus_1"].get("cores")
    out["vcpu_ratio"] = scaled(rec.get("vcpu_ratio"), old_basis, new_basis)
    out["vcpu_ratio_degraded"] = scaled(rec.get("vcpu_ratio_degraded"),
                                        (rec.get("n_minus_1") or {}).get("cores"),
                                        out["n_minus_1"].get("cores"))


def _rebuild_compute_floor(out, rec, hw) -> None:
    """Coverage of the source's compute demand by the quoted CPUs.

    The stored block has percentages, not the demand, but coverage is linear
    in (per-node GHz x compute nodes) and (per-node benchmark x compute nodes),
    so each signal scales exactly by the ratio of new to old supply. When the
    quoted CPU's clock or benchmark is unknown (typed by hand, or missing from
    the catalog) the line is dropped rather than guessed (owner decision)."""
    cf = rec.get("compute_floor")
    if not cf:
        return
    full = bool(rec.get("sized_full_cluster"))

    def pool(r):
        # The engine's compute pool: every HCI node when sizing for the full
        # cluster, otherwise the nodes left with one held back per cluster.
        hci = r.get("hci_node_count") or r.get("node_count") or 0
        if full or hci <= 1:
            return hci
        return max(hci - len(r.get("cluster_layout") or [hci]), 1)

    old_pool, new_pool = pool(rec), pool(out)
    old_ghz = float(rec.get("ghz") or 0) * (rec.get("cores_per_node") or 0)
    old_perf = _num(rec.get("cpu_perf_index"))

    def scale(pct, old_node, new_node):
        if pct is None:
            return None
        if new_node is None or not old_node or not old_pool:
            return "unknown"
        return round(float(pct) * (new_node * new_pool) / (old_node * old_pool), 1)

    ghz_pct = scale(cf.get("ghz_pct"), old_ghz, hw["ghz_per_node"] if hw["cpu_known"] else None)
    perf_pct = scale(cf.get("perf_pct"), old_perf, hw["perf_per_node"] if hw["cpu_known"] else None)
    if "unknown" in (ghz_pct, perf_pct):
        out["compute_floor"] = None
        return
    balance = float(cf.get("balance") or 0.0)
    if ghz_pct is not None and perf_pct is not None:
        blended = (1.0 - balance) * ghz_pct + balance * perf_pct
    else:
        blended = ghz_pct if ghz_pct is not None else perf_pct
    new = dict(cf)
    new.update({"ghz_pct": ghz_pct, "perf_pct": perf_pct,
                "coverage_pct": round(blended, 1) if blended is not None else None})
    out["compute_floor"] = new


def _rebuild_determinant(out, rec) -> None:
    """The rationale keeps WHICH resource drove the sizing and what it
    required — that is the engine's reasoning — but "achieved" and the headroom
    are capacity figures, so they describe the quoted hardware. Otherwise the
    rationale would claim headroom the ordered box does not have, next to bars
    showing it over capacity. Headroom may go negative; that is the point."""
    det = rec.get("determinant")
    if not isinstance(det, dict) or det.get("required") in (None, 0):
        return
    resource = det.get("resource")
    totals, n1 = out["totals"], out["n_minus_1"]
    if resource == "CPU":
        achieved = totals.get("cores") if rec.get("sized_full_cluster") else n1.get("cores")
    elif resource == "RAM":
        achieved = n1.get("ram_gb")
    elif resource == "Storage":
        achieved = totals.get("usable_storage_tb")
    elif resource == "Compute":
        achieved = (out.get("compute_floor") or {}).get("coverage_pct")
    else:
        return
    new = dict(det)
    if achieved is None:
        new["achieved"] = None
        new["headroom_pct"] = None
    else:
        required = float(det["required"])
        new["achieved"] = round(float(achieved), 1)
        new["headroom_pct"] = round((float(achieved) - required) / required * 100, 1)
    out["determinant"] = new


def _rebuild_licensing(out, rec) -> None:
    """The licence requirement for the quoted cores/RAM/layout (owner decision),
    with the same helper a direct hardware build uses. Strings and booleans
    only — no price crosses into the recommendation. When licence scoring is
    off (or no price feed) the helper returns None and the line is omitted."""
    if rec.get("licensing") is None:
        return
    try:
        from recommend import license_annotations_for
        out["licensing"] = license_annotations_for(
            out.get("cluster_layout") or [out.get("hci_node_count") or out.get("node_count") or 1],
            out.get("cores_per_node") or 0, out.get("ram_per_node_gb") or 0)
    except Exception:  # pragma: no cover - never fail an export on this
        out["licensing"] = None


def _rebuild_utilization(out, rec) -> None:
    """Keep the workload's demand, replace the capacity with the quoted one.

    Each resource's `abs` carries the demand in real units, so the percentages
    are simply recomputed against the new capacity — and NOT clamped: a quote
    that cannot carry the workload reads over 100 %, which the renderers show
    in red on a rescaled axis instead of flattening it to "exactly full". The
    HA band is the full-vs-N-1 gap of the QUOTED cluster, still released for
    CPU only when sizing for the full cluster (as the engine does)."""
    util = rec.get("utilization")
    if not isinstance(util, dict):
        return
    totals, n1 = out["totals"], out["n_minus_1"]
    capacity = {"cpu": (totals.get("cores") or 0, n1.get("cores") or 0),
                "ram": (totals.get("ram_gb") or 0, n1.get("ram_gb") or 0),
                "storage": (totals.get("usable_storage_tb") or 0,
                            totals.get("usable_storage_tb") or 0)}
    rounding = {"cpu": 0, "ram": 1, "storage": 2}
    new_util = dict(util)
    for key, (full, n1_cap) in capacity.items():
        block = util.get(key)
        if not isinstance(block, dict) or not isinstance(block.get("abs"), dict):
            continue
        abs_ = dict(block["abs"])
        old_cap = _num(abs_.get("capacity")) or 0.0
        if not full or abs(float(full) - old_cap) < 1e-9:
            continue

        def pct(amount):
            return int(round(float(amount or 0) / float(full) * 100))

        rep_abs = (float(block.get("replication") or 0) * old_cap / 100.0) if old_cap else 0.0
        new_block = dict(block)
        new_block["current"] = pct(abs_.get("current"))
        new_block["total"] = pct(abs_.get("total"))
        new_block["snapshot"] = pct(abs_.get("snapshot"))
        new_block["replication"] = pct(rep_abs)
        if key == "storage" or (key == "cpu" and rec.get("sized_full_cluster")):
            new_block["ha_reserve"] = 0
        else:
            new_block["ha_reserve"] = int(round((full - n1_cap) / full * 100)) if full else 0
        cap = round(float(full), rounding[key]) if rounding[key] else int(round(full))
        abs_["capacity"] = cap
        new_block["abs"] = abs_
        new_util[key] = new_block
    out["utilization"] = new_util


def _rebuild_iops(out, rec, hw) -> None:
    """Net IOPS per node depends on the disks. Other disks: the sized figures
    no longer describe the node, so the block is dropped rather than shown
    wrong. Same disks, another node count: the totals re-multiply exactly."""
    iops = rec.get("iops")
    if not iops:
        return
    if hw["storage_changed"]:
        out["iops"] = None
        return
    if hw["nodes_changed"] and iops.get("per_node") is not None:
        new = dict(iops)
        per = iops["per_node"]
        hci = out.get("hci_node_count") or out.get("node_count") or 0
        layout = hw["layout"] or [hci]
        new["total"] = per * (out.get("node_count") or 0)
        new["n_minus_1"] = per * max(hci - len(layout), 1 if hci == 1 else 0)
        out["iops"] = new


def _badge(override: Dict[str, Any]) -> Dict[str, Any]:
    """What the screen shows about an applied override (never in the export)."""
    values = override.get("values") or {}
    return {
        "chassis": values.get("chassis"),
        "source": override.get("source"),
        "bom_check_id": override.get("bom_check_id"),
        "bom_check_name": override.get("bom_check_name"),
        "bom_auto": override.get("bom_auto"),
        "fields": sorted(k for k in values if k in MANUAL_FIELDS),
    }


def apply_to_clusters(clusters: Any, override: Optional[Dict[str, Any]]) -> Any:
    """Apply an override to every recommendation in a stored snapshot's
    ``clusters`` list. Returns the list unchanged when nothing applies."""
    if not override or not isinstance(clusters, list):
        return clusters
    out, touched = [], False
    for cluster in clusters:
        if not isinstance(cluster, dict):
            out.append(cluster)
            continue
        rec = cluster.get("recommendation")
        if not applies_to(rec):
            out.append(cluster)
            continue
        patched = apply_to_rec(rec, override)
        if patched is rec:
            out.append(cluster)
            continue
        copy = dict(cluster)
        copy["recommendation"] = patched
        out.append(copy)
        touched = True
    return out if touched else clusters


def apply_to_snapshot(sizing) -> Any:
    """A sizing's result snapshot with its export override applied."""
    snapshot = getattr(sizing, "result_snapshot", None) or {}
    clusters = snapshot.get("clusters")
    override = resolve(sizing)
    if not override or not clusters:
        return snapshot
    patched = apply_to_clusters(clusters, override)
    if patched is clusters:
        return snapshot
    out = dict(snapshot)
    out["clusters"] = patched
    return out


def badges_for(sizings: List[Any]) -> Dict[int, Dict[str, Any]]:
    """{sizing id: badge} for a whole project in ONE query.

    The project page lists every sizing, and resolving each one on its own
    would put a BOM-check query behind every row.
    """
    from bom_models import BomCheck

    ids = [s.id for s in sizings]
    if not ids:
        return {}
    by_sizing = {}  # type: Dict[int, List[Any]]
    try:
        rows = (BomCheck.query
                .filter(BomCheck.configuration_id.in_(ids),
                        BomCheck.is_deleted.is_(False))
                .order_by(BomCheck.created_at.desc(), BomCheck.id.desc())
                .all())
    except Exception:  # pragma: no cover - no BOM tables yet
        rows = []
    for row in rows:
        if _has_usable_cluster(row):
            by_sizing.setdefault(row.configuration_id, []).append(row)

    out = {}
    for sizing in sizings:
        badge = badge_for(sizing, checks=by_sizing.get(sizing.id, []))
        if badge:
            out[sizing.id] = badge
    return out


def badge_for(sizing, checks=None) -> Optional[Dict[str, Any]]:
    """Summary for the project row / result card, or None when the sizing
    exports as sized."""
    override = resolve(sizing, checks)
    if not override:
        return None
    badge = _badge(override)
    # A vendor that differs from the one the sizing was built on is worth
    # saying out loud: the BOM is followed (owner decision), but a Lenovo quote
    # against a Dell sizing is usually news to whoever sized it.
    badge["vendor"] = (override.get("values") or {}).get("vendor")
    return badge
