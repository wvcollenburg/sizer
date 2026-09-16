"""Export customization: name the chassis the partner actually sells.

A Validated recommendation is sized on one vendor chassis, but the VAR quoting
it may well sell another one — we size a Dell R660, the VAR BOM-checks an R670.
The proposal then has to say R670, or it describes hardware nobody is ordering.

Three sources, merged PER FIELD (owner decision, 2026-09-16):

    manual override  >  linked BOM check  >  the recommendation

so overriding only the chassis still leaves CPU/RAM/disks coming from the BOM,
and a manual entry never has to restate what the BOM already got right.

What it changes, and what it deliberately does NOT:

  * changed — the chassis name/vendor/form factor and the PER-NODE hardware
    (CPU, cores, threads, RAM, disks, node count), plus the cluster totals that
    are plain arithmetic over those per-node figures;
  * unchanged — utilization bars, the rationale, licensing and pricing. Those
    are the *sizing's* answer to "does this workload fit", not the BOM's. The
    bars keep reading the sized capacity on purpose: recomputing them here
    would let the export contradict the sizing screen, and the BOM-vs-sizing
    comparison already lives on the project page where the check was run.

Exports name only the quoted chassis — no "sized on X, quoted as Y" note
(owner decision): the document describes what is being bought.

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
# rather than trusted into a rendered document.
TEXT_FIELDS = ("chassis", "vendor", "form_factor", "cpu", "storage_desc")
INT_FIELDS = ("cores_per_node", "threads_per_node", "ram_per_node_gb", "node_count")
MANUAL_FIELDS = TEXT_FIELDS + INT_FIELDS

_MAX_TEXT = 120


# ── normalising what gets stored ─────────────────────────────────────────────

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


def _storage_desc(cluster) -> Optional[str]:
    """'6 x NVMe (46.1 TB raw)' style line for the per-node storage row.

    bom/fit.py's stored cluster summary carries drive counts per node and the
    cluster's raw capacity, but NOT the size of an individual drive — so the
    description says what the BOM actually established and no more. Per-node
    raw is divided back out rather than invented.
    """
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
    for src, dst in (("cpu", "cpu"),
                     ("cores_per_node", "cores_per_node"),
                     ("threads_per_node", "threads_per_node"),
                     ("ram_per_node_gb", "ram_per_node_gb"),
                     ("node_count", "node_count")):
        value = cluster.get(src)
        if value:
            values[dst] = value
    desc = _storage_desc(cluster)
    if desc:
        values["storage_desc"] = desc
    # Carried for the totals rebuild below, not user-settable.
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
        # Per-field merge: manual wins field by field over the BOM.
        merged = dict(values)
        merged.update(manual)
        if not merged:
            return None
        return {
            "values": merged,
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


def applies_to(rec: Dict[str, Any]) -> bool:
    """Validated recommendations only (owner decision)."""
    return bool(isinstance(rec, dict) and rec.get("validated"))


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
    out = dict(rec)

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
    if values.get("cpu"):
        out["cpu"] = values["cpu"]
    if values.get("storage_desc"):
        storage = dict(out.get("storage_config") or {})
        storage["desc"] = values["storage_desc"]
        out["storage_config"] = storage

    cores = _int(values.get("cores_per_node"))
    threads = _int(values.get("threads_per_node"))
    ram = _int(values.get("ram_per_node_gb"))
    nodes = _int(values.get("node_count"))

    if cores:
        # The OS core/RAM overheads are properties of the platform, not of the
        # sizing, so they are carried across from the sized config rather than
        # re-derived — the quoted node loses the same slice the sized one did.
        core_overhead = max((rec.get("cores_per_node") or 0)
                            - (rec.get("usable_cores_per_node") or 0), 0)
        out["cores_per_node"] = cores
        out["usable_cores_per_node"] = max(cores - core_overhead, 0)
    if threads:
        out["threads_per_node"] = threads
    if ram:
        ram_overhead = max((rec.get("ram_per_node_gb") or 0)
                           - (rec.get("usable_ram_per_node_gb") or 0), 0)
        out["ram_per_node_gb"] = ram
        out["usable_ram_per_node_gb"] = max(ram - ram_overhead, 0)
    if nodes:
        out["node_count"] = nodes
        # node_count is the WHOLE cluster, so the HCI count is what is left
        # after any storage-only nodes. A BOM-derived cluster states its own
        # split (bom/fit.py works it out from the BOM); for a manual override
        # the sizing's storage-only block still applies, and a quoted count at
        # or below it would leave no HCI node at all, so it is floored at 1.
        cluster = (override.get("cluster") or {})
        so_count = cluster.get("so_count") if cluster else None
        if so_count is None:
            so_count = ((rec.get("storage_only") or {}).get("count") or 0)
        hci = cluster.get("hci_node_count") if cluster else None
        out["hci_node_count"] = hci or (max(nodes - so_count, 1) if so_count else nodes)
        out["single_node"] = nodes == 1

    _rebuild_totals(out, rec, override.get("cluster"))
    out["export_override"] = _badge(override)
    return out


def _rebuild_totals(out: Dict[str, Any], rec: Dict[str, Any],
                    cluster: Optional[Dict[str, Any]]) -> None:
    """Keep the cluster-total table consistent with the per-node table.

    Only plain arithmetic over the quoted per-node figures — never the
    utilization bars, the compute floor or anything with a price behind it.
    A BOM-derived cluster brings its own totals (bom/fit.py ran the engine's
    own helpers over them), so those are preferred where they exist.
    """
    totals = dict(out.get("totals") or {})
    n1 = dict(out.get("n_minus_1") or {})
    if not totals and not n1:
        return

    hci = _int(out.get("hci_node_count")) or _int(out.get("node_count")) or 0
    usable_cores = _int(out.get("usable_cores_per_node")) or 0
    usable_ram = out.get("usable_ram_per_node_gb") or 0
    threads = _int(out.get("threads_per_node")) or 0

    changed_cpu = (out.get("cores_per_node") != rec.get("cores_per_node")
                   or out.get("node_count") != rec.get("node_count")
                   or out.get("threads_per_node") != rec.get("threads_per_node"))
    changed_ram = (out.get("ram_per_node_gb") != rec.get("ram_per_node_gb")
                   or out.get("node_count") != rec.get("node_count"))

    if hci and changed_cpu:
        totals["cores"] = usable_cores * hci
        totals["threads"] = threads * hci
        ghz = out.get("ghz")
        if ghz and usable_cores:
            totals["total_ghz"] = round(ghz * (out.get("cores_per_node") or 0) * hci, 1)
    if hci and changed_ram:
        totals["ram_gb"] = round(usable_ram * hci, 1)

    if cluster:
        # bom/fit.py already summed these with the engine's helpers, including
        # the RF2 + rebuild-disk storage maths that cannot be recovered from a
        # per-node figure alone.
        for src, dst in (("cores_full", "cores"), ("ram_full", "ram_gb"),
                         ("ghz_full", "total_ghz"),
                         ("raw_storage_tb", "raw_storage_tb"),
                         ("usable_storage_tb", "usable_storage_tb")):
            if cluster.get(src):
                totals[dst] = cluster[src]
        if cluster.get("threads_per_node") and cluster.get("hci_node_count"):
            totals["threads"] = cluster["threads_per_node"] * cluster["hci_node_count"]
        for src, dst in (("cores_n1", "cores"), ("ram_n1", "ram_gb"),
                         ("ghz_n1", "total_ghz"),
                         ("usable_storage_tb", "usable_storage_tb")):
            if cluster.get(src) and dst in n1:
                n1[dst] = cluster[src]
        if cluster.get("cluster_layout"):
            out["cluster_layout"] = cluster["cluster_layout"]
        if cluster.get("num_clusters"):
            out["num_clusters"] = cluster["num_clusters"]
    elif hci and (changed_cpu or changed_ram):
        # Manual-only change: N-1 keeps its own node basis.
        n1_nodes = max(hci - len(out.get("cluster_layout") or [hci]), 0) or max(hci - 1, 0)
        if changed_cpu:
            n1["cores"] = usable_cores * n1_nodes
            n1["threads"] = threads * n1_nodes
        if changed_ram:
            n1["ram_gb"] = round(usable_ram * n1_nodes, 1)

    if totals:
        out["totals"] = totals
    if n1:
        out["n_minus_1"] = n1


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
