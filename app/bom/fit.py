"""Sizing fit: does a third-party BOM actually carry the workload of a sizing?

The technical verdict (bom.rules) answers "are these the right parts?". This
module answers the other half of docs/bom-checker-plan.md: "is there enough
of them?" — and it must answer with the SAME arithmetic the recommendation
engine used to size the cluster, or the two would contradict each other on
the project page (a BOM that copies the sized config line for line has to
come out as 'equal', not 'smaller by 6 %').

So this is deliberately NOT a second sizing engine. It:

  1. turns one BOM config into a homogeneous node model (derive_nodes) — the
     BOM lists totals across nodes, so everything is divided by the node count;
  2. resolves the CPU through the same catalogs the engine uses (resolve_cpu):
     cpu_specs first, the admin-editable CpuCatalog when a DB is around, then
     the broad SPEC CPU 2017 lookup, and only then the cores/clock printed in
     the BOM text (which is a BASE clock, while the engine sizes on all-core
     turbo — that mismatch is surfaced, never silently mixed in);
  3. builds the transient cluster with the engine's own helpers
     (cluster_from_nodes): _cluster_layout, _cluster_usable_storage (RF2 plus
     one rebuild disk per cluster), the bay-tiered OS RAM overhead, the
     os_core_overhead and _n_minus_1_block;
  4. reads the requirement back out of the saved sizing (sizing_requirements)
     — the engine's `needs` dict is never persisted, so demand is recovered
     from utilization.*.abs.total plus the re-derived day-one floors;
  5. compares per dimension (compare) with the owner's relation semantics:
     smaller = below the requirement (red), fits = meets it but below the sized
     config, equal = within 2 % of the sized config, bigger = above it (never a
     warning — with software-only licensing, oversizing is the customer's call).

Pure Python; Flask/DB are optional (tunables fall back to defaults, CPU
resolution skips the catalog table) so tests and background jobs can run it
without an app context. Python 3.9-compatible.
"""
import re
from typing import Any, Dict, List, Optional

from tunables import T
from recommend import (_cluster_layout, _cluster_usable_storage,
                       _effective_cores, _compute_coverage)
from calc import _n_minus_1_block
from cpu_specs import CPU_SPECS, cpu_model_key, sizing_ghz, perf_index
import cpu_benchmarks

from bom.normalize import BOMComponent, BOMConfig, NormalizedBOM
from bom.parsers.common import SERVER_LINE
from bom.rules import (is_absence_indicator, is_hdd, is_nvme_drive, is_ssd,
                       is_hardware_config)

# 'equal' band: a BOM within this fraction of the sized value is the same
# config as far as a buyer is concerned (rounding of DIMM/drive sizes).
EQUAL_TOLERANCE = 0.02
# The engine warns when the largest VM needs >= 90 % of one node's usable RAM.
LARGEST_VM_WARN_FRACTION = 0.9

DIMENSION_KEYS = ("nodes", "cores", "compute", "ram", "largest_vm", "storage", "nic")
# Dimensions whose 'smaller' reds the verdict. NIC is informational: the engine
# never gates on NIC speed either (summary.nic_speed_mbps is only reported).
VERDICT_DIMENSIONS = ("nodes", "cores", "compute", "ram", "largest_vm", "storage")

# Engine drive-type keys (recommend.DRIVE_TYPE_KEY values).
TIER_KEY = {"hdd": "HDD", "ssd": "SSD", "nvme": "NVMe"}

# Categories that carry capacity. 'chassis' only contributes the node count;
# controller/boss/gpu/other are the technical checker's business.
_CAPACITY_CATEGORIES = ("cpu", "memory", "storage", "nic")

_CAPACITY_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(TB|GB)\b", re.IGNORECASE)
# '10/25GbE', '25GbE', '10GBase-T', '10Gb', '100GbE', '2.5GbE', '10 Gigabit'
_NIC_SPEED_RE = re.compile(r"((?:\d+(?:\.\d+)?\s*/\s*)*\d+(?:\.\d+)?)\s*G(?:b|ig)",
                           re.IGNORECASE)
_NIC_PORTS_RE = re.compile(r"(\d+)\s*-?\s*port", re.IGNORECASE)
_NIC_NX_RE = re.compile(r"(\d+)\s*x\s*\d+(?:\.\d+)?\s*G(?:b|ig)", re.IGNORECASE)
_CPU_CORES_RE = re.compile(r"(\d+)\s*C\b|(\d+)-Core", re.IGNORECASE)
_CPU_THREADS_RE = re.compile(r"(\d+)\s*T\b", re.IGNORECASE)
_CPU_GHZ_RE = re.compile(r"(\d+(?:\.\d+)?)\s*GHz", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9]+")


# ─── small helpers ───────────────────────────────────────────────────────────

def _words(text: str) -> set:
    return set(_WORD_RE.findall((text or "").lower()))


def _parse_capacity_tb(desc: str) -> Optional[float]:
    """'3.84TB' -> 3.84, '960GB' -> 0.96, '12TB' -> 12. First unit match wins
    (a drive line names its own capacity before any interface figure)."""
    m = _CAPACITY_RE.search(desc or "")
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    return value / 1000.0 if m.group(2).upper() == "GB" else value


def _parse_dimm_gb(desc: str) -> Optional[int]:
    m = _CAPACITY_RE.search(desc or "")
    if not m:
        return None
    value = float(m.group(1).replace(",", "."))
    if m.group(2).upper() == "TB":
        value *= 1000
    return int(round(value))


def _parse_nic_speed(desc: str) -> Optional[float]:
    """Max GbE figure on a NIC line; '10/25GbE' -> 25 (it is a 25G part)."""
    best = None
    for m in _NIC_SPEED_RE.finditer(desc or ""):
        for part in m.group(1).split("/"):
            try:
                v = float(part.strip())
            except ValueError:
                continue
            if best is None or v > best:
                best = v
    return best


def _parse_nic_ports(desc: str) -> Optional[int]:
    d = desc or ""
    m = _NIC_PORTS_RE.search(d)
    if m:
        return int(m.group(1))
    m = _NIC_NX_RE.search(d)
    if m:
        return int(m.group(1))
    low = d.lower()
    if "quad" in low:
        return 4
    if "dual" in low:
        return 2
    return None


def _split_per_node(total: int, node_count: int, what: str,
                    notes: List[str], unresolved: List[str]) -> int:
    """Totals-across-nodes -> per node. A remainder means the BOM is not the
    homogeneous cluster the engine assumes (5 CPUs over 2 nodes); size on the
    floor and say so rather than inventing a fractional socket."""
    if node_count <= 0:
        return total
    per, rem = divmod(int(total), node_count)
    if rem:
        notes.append(
            f"{what}: {total} across {node_count} nodes is not a whole number per "
            f"node; sized on {per} per node (remainder {rem} ignored).")
        if what not in unresolved:
            unresolved.append(what)
    return per


def _round(value: Any, unit: str) -> Any:
    if value is None:
        return None
    if unit in ("cores", "nodes", "GB", "threads"):
        return int(round(value))
    if unit == "TB":
        return round(float(value), 2)
    return round(float(value), 1)


# ─── 1. BOM config -> node model ─────────────────────────────────────────────

def _infer_node_count(config: BOMConfig, lines: List[BOMComponent],
                      notes: List[str]) -> Optional[int]:
    """The BOM's quantities are totals; the node count is the one number that
    turns them into a per-node picture, so only real evidence counts: the
    parser-supplied count, or a chassis line that IS the server. Guessing it
    from part quantities silently multiplied the cluster (6 CPUs of a 3-node
    dual-socket BOM became 6 nodes and an under-sized BOM went green), so
    when no evidence exists this returns None and compare() reports every
    dimension as unknown instead of sizing on a guess."""
    if config.node_count:
        notes.append(f"Node count {config.node_count} taken from the BOM source.")
        return int(config.node_count)

    chassis = [c for c in lines if c.category == "chassis"]
    model_words = _words(config.server_model or "")
    # Drop the vendor prefixes so 'ThinkSystem' alone never counts as a match.
    model_words -= {"thinksystem", "poweredge", "proliant", "supermicro", "lenovo",
                    "dell", "hpe", "server", "system"}
    for c in chassis:
        words = _words(c.description)
        # Backplane / media-bay rows are categorised 'chassis' too, but they
        # are per-node hardware (often several per node) — never the server
        # line, even when they name the model.
        if "backplane" in words or ("media" in words and "bay" in words):
            continue
        hit_model = bool(model_words) and bool(model_words & words)
        if (hit_model or "chassis" in words or "server" in words
                or SERVER_LINE.match(c.description or "")):
            notes.append(f"Node count {c.quantity} from chassis line '{c.description}'.")
            return max(int(c.quantity or 1), 1)
    notes.append("Node count unknown: the BOM names no node count and no "
                 "server/chassis line identifies one. Fill the Nodes column "
                 "of the template (or include the server line).")
    return None


def derive_nodes(config: BOMConfig) -> Dict[str, Any]:
    """One BOM config -> the homogeneous per-node model the engine sizes on."""
    notes = []          # type: List[str]
    unresolved = []     # type: List[str]
    lines = [c for c in config.components
             if c.category in _CAPACITY_CATEGORIES + ("chassis",)
             and not is_absence_indicator(c)]

    node_count = _infer_node_count(config, lines, notes)
    node_count_known = node_count is not None
    if not node_count_known:
        # The sums below still run (as a single machine) so the cluster
        # summary has something to show, but per-node figures cannot be
        # trusted without a node count: compare() reports every dimension
        # as unknown instead of sizing on a guess.
        unresolved.append("nodes")
        node_count = 1

    # CPUs: identical descriptions merge (two lines of the same SKU is still
    # one socket type); qty per node = sockets of that SKU.
    cpu_totals = {}    # type: Dict[str, int]
    cpu_order = []     # type: List[str]
    for c in lines:
        if c.category != "cpu":
            continue
        key = c.description.strip()
        if key not in cpu_totals:
            cpu_totals[key] = 0
            cpu_order.append(key)
        cpu_totals[key] += int(c.quantity or 1)
    cpus = []
    for key in cpu_order:
        per = _split_per_node(cpu_totals[key], node_count, "cpu", notes, unresolved)
        if per > 0:
            cpus.append({"model_text": key, "qty_per_node": per})
    sockets = sum(c["qty_per_node"] for c in cpus)
    if not cpus:
        notes.append("No CPU line found in this config.")
        unresolved.append("cpu")

    # Memory: GB per DIMM from the description; total DIMMs split per node.
    dimm_total = 0
    ram_total_gb = 0
    dimm_sizes = {}    # type: Dict[int, int]
    for c in lines:
        if c.category != "memory":
            continue
        gb = _parse_dimm_gb(c.description)
        qty = int(c.quantity or 1)
        if gb is None:
            notes.append(f"Memory line '{c.description}': no GB figure found; ignored.")
            if "memory" not in unresolved:
                unresolved.append("memory")
            continue
        dimm_total += qty
        ram_total_gb += gb * qty
        dimm_sizes[gb] = dimm_sizes.get(gb, 0) + qty
    dimm_count = _split_per_node(dimm_total, node_count, "memory", notes, unresolved)
    if dimm_total and dimm_count * node_count != dimm_total:
        # Mixed remainder: recompute RAM from the whole DIMMs that landed per node.
        ram_gb = 0
        remaining = dimm_count
        for gb in sorted(dimm_sizes, reverse=True):
            take = min(remaining, dimm_sizes[gb] // node_count)
            ram_gb += gb * take
            remaining -= take
    else:
        ram_gb = ram_total_gb // node_count if node_count else ram_total_gb
    dimm_gb = max(dimm_sizes, key=lambda g: dimm_sizes[g]) if dimm_sizes else None
    if len(dimm_sizes) > 1:
        notes.append("Mixed DIMM sizes in this config: " + ", ".join(
            f"{n} x {gb} GB" for gb, n in sorted(dimm_sizes.items())) + ".")
    if not dimm_total:
        notes.append("No memory line found in this config.")
        if "memory" not in unresolved:
            unresolved.append("memory")

    # Drives: kind in the same order as rules.get_drive_count so both verdicts
    # agree on what a line is; capacity from the description.
    drives = []
    for c in lines:
        if c.category != "storage":
            continue
        if is_nvme_drive(c):
            kind = "nvme"
        elif is_hdd(c):
            kind = "hdd"
        elif is_ssd(c):
            kind = "ssd"
        else:
            notes.append(f"Storage line '{c.description}': drive type not recognised; ignored.")
            if "storage" not in unresolved:
                unresolved.append("storage")
            continue
        cap = _parse_capacity_tb(c.description)
        if cap is None:
            notes.append(f"Storage line '{c.description}': no capacity figure found; ignored.")
            if "storage" not in unresolved:
                unresolved.append("storage")
            continue
        per = _split_per_node(int(c.quantity or 1), node_count, "storage", notes, unresolved)
        if per > 0:
            drives.append({"kind": kind, "capacity_tb": cap, "qty_per_node": per,
                           "description": c.description})
    bays = sum(d["qty_per_node"] for d in drives)
    if not drives:
        notes.append("No data drives found in this config.")
        if "storage" not in unresolved:
            unresolved.append("storage")

    # NICs: the fastest port speed on the node and the largest adapter's port
    # count (the engine's nic_ports is also the max over a model's NIC options).
    nic_speed = None
    nic_ports = None
    nic_lines = [c for c in lines if c.category == "nic"]
    for c in nic_lines:
        s = _parse_nic_speed(c.description)
        p = _parse_nic_ports(c.description)
        if s is not None and (nic_speed is None or s > nic_speed):
            nic_speed = s
        if p is not None and (nic_ports is None or p > nic_ports):
            nic_ports = p
        if s is None:
            notes.append(f"NIC line '{c.description}': no port speed found.")
    if nic_lines and nic_speed is None:
        unresolved.append("nic")
    if not nic_lines:
        notes.append("No NIC line found in this config.")

    return {
        "node_count": node_count if node_count_known else None,
        "cpus": cpus,
        "sockets_per_node": sockets,
        "ram_gb_per_node": int(ram_gb),
        "dimm_count_per_node": int(dimm_count),
        "dimm_gb": dimm_gb,
        "drives": drives,
        "bays_per_node": bays,
        "nic_speed_gbe": nic_speed,
        "nic_ports": nic_ports,
        "notes": notes,
        "unresolved": unresolved,
    }


# ─── 2. CPU resolution ───────────────────────────────────────────────────────

def _from_spec(spec: Dict[str, Any], qty: int, source: str) -> Dict[str, Any]:
    pi = perf_index(spec)
    sr = spec.get("specrate_int")
    return {
        "desc": f"{qty} x {spec['model']}",
        "cores": spec["cores"] * qty,
        "threads": spec["threads"] * qty,
        "p_cores": spec["p_cores"] * qty if spec.get("p_cores") is not None else None,
        "e_cores": spec["e_cores"] * qty if spec.get("e_cores") is not None else None,
        "ghz": sizing_ghz(spec),
        "generation": spec.get("generation"),
        "model": spec["model"],
        "specrate_int": sr * qty if sr is not None else None,
        "perf_index": pi * qty if pi is not None else None,
        "sockets": qty,
        "source": source,
        "base_clock_only": False,
    }


def _catalog_row(model_text: str, key: Optional[str]):
    """Match a CpuCatalog row by SKU key (or normalised model name). Returns
    None whenever there is no app context / DB — the pure path must survive."""
    try:
        from orm_models import CpuCatalog
        rows = CpuCatalog.query.all()
    except Exception:      # no app context, no table, no engine bound
        return None
    want = cpu_benchmarks.normalize_cpu(model_text)
    for row in rows:
        if key and cpu_model_key(row.description or "") == key:
            return row
        if row.model and cpu_benchmarks.normalize_cpu(row.model) == want:
            return row
    return None


def resolve_cpu(model_text: str, qty: int) -> Dict[str, Any]:
    """BOM CPU text + sockets -> the engine's per-node cpu option shape, with
    sockets folded in, plus 'source' and 'base_clock_only' provenance."""
    qty = max(int(qty or 1), 1)
    key = cpu_model_key(model_text or "")
    spec = CPU_SPECS.get(key) if key else None
    if spec:
        return _from_spec(spec, qty, "catalog")

    row = _catalog_row(model_text, key)
    if row is not None:
        # The catalog's `ghz` IS the sizing clock (back-filled to all-core turbo).
        spec = {"model": row.model or row.description, "cores": row.cores,
                "threads": row.threads, "p_cores": row.p_cores, "e_cores": row.e_cores,
                "generation": row.generation, "specrate_int": row.specrate_int,
                "passmark_cpu_mark": row.passmark_cpu_mark, "family": row.family,
                "base_ghz": row.ghz, "all_core_turbo_ghz": row.all_core_turbo_ghz or row.ghz}
        out = _from_spec(spec, qty, "db")
        out["ghz"] = row.ghz
        return out

    # Unknown to both catalogs: SPECrate from the broad lookup (throughput only)
    # and cores/threads/clock from whatever the BOM text prints.
    hit = cpu_benchmarks.lookup(model_text or "")
    m_c = _CPU_CORES_RE.search(model_text or "")
    m_t = _CPU_THREADS_RE.search(model_text or "")
    m_g = _CPU_GHZ_RE.search(model_text or "")
    cores = int(m_c.group(1) or m_c.group(2)) if m_c else None
    threads = int(m_t.group(1)) if m_t else (cores * 2 if cores else None)
    ghz = float(m_g.group(1)) if m_g else None
    if hit:
        source = "spec-cpu2017"
    elif cores is not None:
        source = "parsed"
    else:
        source = "unresolved"
    return {
        "desc": f"{qty} x {model_text}",
        "cores": cores * qty if cores else None,
        "threads": threads * qty if threads else None,
        "p_cores": None,
        "e_cores": None,
        "ghz": ghz,
        "generation": None,
        "model": hit["model"] if hit else model_text,
        "specrate_int": hit["specrate_int"] * qty if hit else None,
        "perf_index": hit["specrate_int"] * qty if hit else None,
        "sockets": qty,
        "source": source,
        # A clock printed in a BOM line is the base clock; the engine's ghz for
        # known SKUs is the all-core turbo. Callers must say so, not blend them.
        "base_clock_only": ghz is not None,
    }


def _merge_cpus(resolved: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A node with several CPU lines (rare: mixed SKUs) sums cores/threads/perf
    and takes the core-weighted clock. None when nothing resolved."""
    if not resolved:
        return None
    if len(resolved) == 1:
        return dict(resolved[0])
    cores = [r["cores"] for r in resolved]
    if any(c is None for c in cores):
        merged = dict(resolved[0])
        merged["cores"] = None
        merged["threads"] = None
        merged["source"] = "unresolved"
        return merged
    total_cores = sum(cores)
    ghz_w = sum((r["ghz"] or 0) * r["cores"] for r in resolved)
    perfs = [r.get("perf_index") for r in resolved]
    return {
        "desc": " + ".join(r["desc"] for r in resolved),
        "cores": total_cores,
        "threads": sum(r["threads"] or 0 for r in resolved),
        "p_cores": (sum(r["p_cores"] or 0 for r in resolved)
                    if all(r.get("p_cores") is not None for r in resolved) else None),
        "e_cores": (sum(r["e_cores"] or 0 for r in resolved)
                    if all(r.get("e_cores") is not None for r in resolved) else None),
        "ghz": (ghz_w / total_cores) if total_cores and ghz_w else None,
        "generation": resolved[0].get("generation"),
        "model": " + ".join(r["model"] for r in resolved),
        "specrate_int": (sum(r["specrate_int"] or 0 for r in resolved)
                         if any(r.get("specrate_int") for r in resolved) else None),
        "perf_index": sum(p for p in perfs if p) if all(p is not None for p in perfs) else None,
        "sockets": sum(r["sockets"] for r in resolved),
        "source": "mixed:" + "/".join(sorted({r["source"] for r in resolved})),
        "base_clock_only": any(r.get("base_clock_only") for r in resolved),
    }


# ─── 3. Transient cluster ────────────────────────────────────────────────────

def _refresh_tunables() -> None:
    """Live admin tunables when a DB is bound; defaults otherwise."""
    try:
        from tunables import refresh_from_db
        refresh_from_db()
    except Exception:
        pass


def _storage_category(counts: Dict[str, int]) -> Optional[str]:
    """recommend.STORAGE_CATEGORIES buckets derived from drive counts."""
    hdd = counts.get("HDD", 0)
    flash = counts.get("SSD", 0) + counts.get("NVMe", 0)
    if hdd and flash:
        return "hybrid"
    if hdd:
        return "spinning"
    if flash:
        return "flash"
    return None


def cluster_from_nodes(node_model: Dict[str, Any],
                       hci_count: Optional[int] = None) -> Dict[str, Any]:
    """Node model -> the cluster figures the engine would report for it.
    Storage spans all nodes (HCI + storage-only); compute spans HCI nodes only
    and N-1 removes one node per cluster — exactly as recommend.py/calc.py."""
    _refresh_tunables()
    notes = []  # type: List[str]

    total = max(int(node_model.get("node_count") or 1), 1)
    hci = total if hci_count is None else max(min(int(hci_count), total), 0)
    so = total - hci
    layout = _cluster_layout(total)
    k = len(layout)
    n1_hci = max(hci - k, 0) if total > 1 else 1

    cpu = _merge_cpus([resolve_cpu(c["model_text"], c["qty_per_node"])
                       for c in node_model.get("cpus", [])])
    if cpu is None:
        cpu = {"desc": None, "cores": None, "threads": None, "p_cores": None,
               "e_cores": None, "ghz": None, "perf_index": None, "sockets": 0,
               "source": "unresolved", "base_clock_only": False, "model": None}
    cores_known = cpu.get("cores") is not None and cpu["cores"] > 0
    eff_cores = _effective_cores(cpu) if cores_known else None
    usable_cores = (eff_cores - T.os_core_overhead) if eff_cores is not None else None
    ghz_per_node = ((cpu.get("ghz") or 0) * eff_cores) if eff_cores else None
    node_perf = cpu.get("perf_index")
    if cpu.get("base_clock_only"):
        notes.append(
            f"CPU '{cpu.get('model')}' is not in the CPU catalog: cores and clock "
            f"were read from the BOM text. That clock is the BASE clock; the sizing "
            f"engine sizes catalog CPUs on their all-core turbo, so GHz-based figures "
            f"for this BOM are conservative and not directly comparable.")
    if cpu.get("source") == "spec-cpu2017":
        notes.append(f"CPU '{cpu.get('model')}': SPECrate from the SPEC CPU 2017 "
                     f"lookup; cores/clock from the BOM text.")
    if not cores_known:
        notes.append("CPU core count unknown: compute dimensions cannot be evaluated.")

    drives = node_model.get("drives", [])
    bays = int(node_model.get("bays_per_node") or sum(d["qty_per_node"] for d in drives))
    raw_by_kind = {}     # type: Dict[str, float]
    drive_counts = {}    # type: Dict[str, int]
    for d in drives:
        key = TIER_KEY[d["kind"]]
        raw_by_kind[key] = raw_by_kind.get(key, 0.0) + d["capacity_tb"] * d["qty_per_node"]
        drive_counts[key] = drive_counts.get(key, 0) + d["qty_per_node"]
    raw_per_node = sum(raw_by_kind.values())
    biggest = max((d["capacity_tb"] for d in drives), default=0.0)
    if drives:
        if total > 1:
            usable_tb = _cluster_usable_storage(raw_per_node, biggest, layout)
        else:
            # Single Node System: RF2 mirrors within the node (raw/2) but holds
            # no rebuild disk; one disk cannot mirror at all -> raw.
            usable_tb = raw_per_node if bays <= 1 else raw_per_node / 2
    else:
        usable_tb = 0.0
    usable_by_kind = {}
    if raw_per_node > 0:
        # Pro-rata only — the engine has no per-tier capacity maths.
        usable_by_kind = {t: usable_tb * (v / raw_per_node) for t, v in raw_by_kind.items()}

    ram_gb = int(node_model.get("ram_gb_per_node") or 0)
    ram_overhead = T.usable_ram_overhead_for(bays)
    usable_ram = ram_gb - ram_overhead

    # Feasibility facts the caller turns into notes (calc.py:377-450 rules).
    hdd_n = drive_counts.get("HDD", 0)
    flash_n = drive_counts.get("SSD", 0) + drive_counts.get("NVMe", 0)
    flash_tb = raw_by_kind.get("SSD", 0.0) + raw_by_kind.get("NVMe", 0.0)
    flash_pct = (flash_tb / raw_per_node * 100) if raw_per_node > 0 else None
    category = _storage_category(drive_counts)
    hybrid_in_band = None
    hdd_ratio_ok = None
    if category == "hybrid":
        hybrid_in_band = T.hybrid_flash_min_pct <= flash_pct <= T.hybrid_flash_max_pct
        hdd_ratio_ok = hdd_n >= T.hybrid_min_hdd_per_flash * flash_n
    cluster_disks = bays * max(layout)
    multi = so > 0 or k > 1
    min_hci = T.min_hci_nodes_per_cluster * k if multi else 0

    n1 = None
    if usable_cores is not None:
        n1 = _n_minus_1_block(hci, k, usable_cores, cpu["threads"] or 0,
                              ghz_per_node or 0, usable_ram, usable_tb,
                              T.os_core_overhead, ram_overhead)

    nic_gbe = node_model.get("nic_speed_gbe")
    return {
        "node_count": total, "hci_node_count": hci, "so_count": so,
        "cluster_layout": layout, "num_clusters": k, "n1_hci_nodes": n1_hci,
        "cpu": cpu, "sockets_per_node": cpu.get("sockets", 0),
        "cores_per_node": eff_cores, "usable_cores_per_node": usable_cores,
        "threads_per_node": cpu.get("threads"),
        "ghz_per_node": ghz_per_node, "node_perf": node_perf,
        "cores_full": usable_cores * hci if usable_cores is not None else None,
        "cores_n1": usable_cores * n1_hci if usable_cores is not None else None,
        "ghz_full": ghz_per_node * hci if ghz_per_node is not None else None,
        "ghz_n1": ghz_per_node * n1_hci if ghz_per_node is not None else None,
        "perf_full": node_perf * hci if node_perf else None,
        "perf_n1": node_perf * n1_hci if node_perf else None,
        "ram_per_node_gb": ram_gb, "ram_overhead_gb": ram_overhead,
        "usable_ram_per_node": usable_ram,
        "ram_full": usable_ram * hci, "ram_n1": usable_ram * n1_hci,
        "raw_storage_tb": raw_per_node * total, "raw_per_node_tb": raw_per_node,
        "usable_storage_tb": usable_tb, "biggest_disk_tb": biggest,
        "raw_by_kind": {t: v * total for t, v in raw_by_kind.items()},
        "usable_by_kind": usable_by_kind,
        "drive_counts": drive_counts, "bays": bays,
        "storage_category": category,
        "nic_gbe": nic_gbe, "nic_mbps": nic_gbe * 1000 if nic_gbe else None,
        "nic_ports": node_model.get("nic_ports"),
        "n_minus_1": n1,
        "feasibility": {
            "flash_pct_of_raw": round(flash_pct, 1) if flash_pct is not None else None,
            "hdd_per_flash": round(hdd_n / flash_n, 2) if flash_n else None,
            "hybrid_flash_in_band": hybrid_in_band,
            "hybrid_hdd_ratio_ok": hdd_ratio_ok,
            "exactly_two_disks": bays == 2,
            "cluster_disks": cluster_disks,
            "max_cluster_disks": T.max_cluster_disks,
            "disk_cap_ok": cluster_disks <= T.max_cluster_disks,
            "min_hci_per_cluster_ok": hci >= min_hci,
            "min_hci_required": min_hci,
        },
        "notes": notes,
    }


# ─── 4. Requirements from a saved sizing ─────────────────────────────────────

def _pct(fields: Dict[str, Any], key: str, default: Any) -> float:
    """Day-one cap from the payload fields (strings); clamped like the API."""
    try:
        return max(1.0, min(float(fields.get(key, default)), 100.0))
    except (TypeError, ValueError):
        return float(default)


def _get(d: Any, *path: str, default: Any = None, missing: Optional[List[str]] = None):
    """Nested read that never raises; records the dotted path when absent."""
    cur = d
    for p in path:
        if not isinstance(cur, dict) or p not in cur or cur[p] is None:
            if missing is not None:
                missing.append(".".join(path))
            return default
        cur = cur[p]
    return cur


def _snapshot_and_payload(configuration: Any):
    if configuration is None:
        return None, {}
    if isinstance(configuration, dict):
        return configuration.get("result_snapshot"), configuration.get("payload") or {}
    return (getattr(configuration, "result_snapshot", None),
            getattr(configuration, "payload", None) or {})


def sizing_requirements(configuration: Any) -> Optional[Dict[str, Any]]:
    """What the saved sizing demands and what it was sized to. None when the
    sizing has no result. Old/partial snapshots yield notes, never exceptions."""
    _refresh_tunables()
    snap, payload = _snapshot_and_payload(configuration)
    if not isinstance(snap, dict):
        return None
    clusters = snap.get("clusters") or []
    if not clusters or not isinstance(clusters[0], dict):
        return None
    cl = clusters[0]
    fields = (payload.get("fields") if isinstance(payload, dict) else None) or {}
    missing = []   # type: List[str]
    notes = []     # type: List[str]
    ident = {
        "id": getattr(configuration, "id", None) if not isinstance(configuration, dict)
        else configuration.get("id"),
        "name": getattr(configuration, "name", None) if not isinstance(configuration, dict)
        else configuration.get("name"),
        "mode": (payload.get("mode") if isinstance(payload, dict) else None)
        or _get(cl, "refs", "mode"),
    }

    rec = cl.get("recommendation")
    cfg = cl.get("config")
    if isinstance(rec, dict):
        out = _requirements_from_recommendation(cl, rec, fields, missing, notes)
    elif isinstance(cfg, dict):
        out = _requirements_from_config(cfg, missing, notes)
    else:
        return None
    if missing:
        notes.append("Snapshot fields missing (older sizing?): " + ", ".join(sorted(set(missing))))
    out["notes"] = notes
    out["sizing"] = ident
    return out


def _replication_split(rec, dim: str, total) -> float:
    """Approximate replication reserve of a utilization axis in real units.

    The snapshot folds the reserve into ``abs.total`` and keeps it only as a
    rounded percentage band (recommend.py: utilization.*.replication), so the
    reserve is recovered as total * replication% / total%. 0 when there is no
    replication band or the axis is missing."""
    if total is None:
        return 0.0
    rep_pct = _get(rec, "utilization", dim, "replication", default=0) or 0
    tot_pct = _get(rec, "utilization", dim, "total", default=0) or 0
    if rep_pct <= 0 or tot_pct <= 0:
        return 0.0
    return float(total) * min(float(rep_pct) / float(tot_pct), 1.0)


def _requirements_from_recommendation(cl, rec, fields, missing, notes):
    proj = cl.get("projection") or {}
    summ = cl.get("summary") or {}
    node_count = _get(rec, "node_count", default=None, missing=missing)
    hci = rec.get("hci_node_count") or node_count
    so_count = _get(rec, "storage_only", "count", default=0)
    full = bool(rec.get("sized_full_cluster"))

    cores_req = _get(rec, "utilization", "cpu", "abs", "total", missing=missing)
    ram_demand = _get(rec, "utilization", "ram", "abs", "total", missing=missing)
    stor_demand = _get(rec, "utilization", "storage", "abs", "total", missing=missing)

    # Replication-target sizings: abs.total is own demand + the inbound
    # replication reserve. The engine holds that reserve ON TOP of the
    # day-one floor (recommend.py: max(own, floor) + rep), and in "failover"
    # compute mode gates CPU/RAM with it at the full cluster only
    # (rep_*_n1 = 0). Recover the reserve from the percentage bands so the
    # fit gates the same figures the engine did.
    rep_cores = _replication_split(rec, "cpu", cores_req)
    rep_ram_gb = _replication_split(rec, "ram", ram_demand)
    rep_stor_tb = _replication_split(rec, "storage", stor_demand)
    if rep_cores or rep_ram_gb or rep_stor_tb:
        notes.append("Replication reserve separated approximately from the "
                     "utilization bands (the snapshot does not itemise it).")

    # Day-one floors are not persisted; re-derive them from the projection the
    # way the engine does (recommend.py: floor = base / (pct/100)), and hold
    # the replication reserve on top of the floor like the engine does.
    ram_req = ram_demand
    base_ram = proj.get("base_ram_gb")
    if base_ram is not None:
        floor = base_ram / (_pct(fields, "max-day-one-ram", T.max_day_one_ram_pct) / 100.0)
        ram_req = max((ram_demand or 0) - rep_ram_gb, floor) + rep_ram_gb
    elif ram_demand is not None:
        notes.append("No projection.base_ram_gb: day-one RAM floor not applied.")
    stor_req = stor_demand
    base_stor = proj.get("base_storage_tb")
    if base_stor is not None:
        floor = base_stor / (_pct(fields, "max-day-one-storage", T.max_day_one_storage_pct) / 100.0)
        stor_req = max((stor_demand or 0) - rep_stor_tb, floor) + rep_stor_tb
    elif stor_demand is not None:
        notes.append("No projection.base_storage_tb: day-one storage floor not applied.")

    # Compute mode: "reserved" holds the CPU/RAM reserve at N-1 too, "failover"
    # only at the full cluster (recommend.py: rep_*_n1). The mode is not
    # persisted, so it is inferred from the engine's own accepted result: a
    # sized N-1 pool below the reserved-mode requirement can only have passed
    # the engine in failover mode — then the reserve must not red the N-1
    # comparison here (the docstring contract: a copied config is never
    # 'smaller').
    if not full:
        n1_block = rec.get("n_minus_1") or {}
        sized_n1_cores = n1_block.get("cores")
        if (rep_cores and cores_req is not None and sized_n1_cores is not None
                and sized_n1_cores < cores_req):
            cores_req = cores_req - rep_cores
            notes.append("Replication compute reserve gated at the full cluster "
                         "only (failover mode inferred): the N-1 cores "
                         "requirement excludes it.")
        sized_n1_ram = n1_block.get("ram_gb")
        if (rep_ram_gb and ram_req is not None and sized_n1_ram is not None
                and sized_n1_ram < ram_req - max(1.0, 0.01 * ram_req)):
            ram_req = ram_req - rep_ram_gb
            notes.append("Replication RAM reserve gated at the full cluster "
                         "only (failover mode inferred): the N-1 RAM "
                         "requirement excludes it.")

    counts = _get(rec, "storage_config", "drive_counts", default={}) or {}
    totals = rec.get("totals") or rec.get("cluster_total") or {}
    n1 = rec.get("n_minus_1") or {}
    return {
        "kind": "recommendation",
        "node_count": node_count, "hci_node_count": hci, "so_count": so_count or 0,
        "cluster_layout": rec.get("cluster_layout") or (_cluster_layout(node_count) if node_count else []),
        "sized_full_cluster": full,
        "cores_required": cores_req,
        "ram_required_gb": ram_req,
        "storage_required_tb": stor_req,
        "compute_floor": proj.get("compute_floor") or {},
        "legacy_ghz": summ.get("total_host_ghz") or 0,
        "max_vm_ram_gb": summ.get("max_vm_ram_gb") or 0,
        "max_vm_cores": summ.get("max_vm_cores") or 0,
        "nic_mbps": summ.get("nic_speed_mbps") or 0,
        "storage_category": _storage_category(counts),
        "validated": bool(rec.get("validated")),
        "sized": {
            "cores": totals.get("cores"), "cores_n1": n1.get("cores"),
            "ram_gb": totals.get("ram_gb"), "ram_n1": n1.get("ram_gb"),
            "usable_storage_tb": totals.get("usable_storage_tb"),
            "perf_index": totals.get("perf_index"),
            "total_ghz": totals.get("total_ghz"), "total_ghz_n1": n1.get("total_ghz"),
            "compute_coverage_pct": _get(rec, "compute_floor", "coverage_pct"),
            "usable_ram_per_node_gb": rec.get("usable_ram_per_node_gb"),
            "threads_per_node": rec.get("threads_per_node"),
            "nic_ports": rec.get("nic_ports"),
            "ram_per_node_gb": rec.get("ram_per_node_gb"),
            "cores_per_node": rec.get("cores_per_node"),
            "node_count": node_count, "model": rec.get("model"), "cpu": rec.get("cpu"),
            "drive_counts": counts,
        },
    }


def _requirements_from_config(cfg, missing, notes):
    """Direct appliance/validated build: hardware only, no workload demand."""
    hci = _get(cfg, "node_count", default=None, missing=missing)
    total = cfg.get("total_node_count") or hci
    so_count = _get(cfg, "storage_only", "count", default=0) or 0
    per = cfg.get("per_node") or {}
    tot = cfg.get("cluster_total") or {}
    n1 = cfg.get("n_minus_1") or {}
    notes.append("The sizing is a direct build with no workload demand: the BOM is "
                 "compared to its hardware only.")
    disks = per.get("disks") or []
    counts = {}  # type: Dict[str, int]
    for d in disks:
        t = d.get("type")
        key = "HDD" if t in ("SAS", "NLSAS", "SATA", "HDD") else ("NVMe" if t == "NVMe" else "SSD")
        counts[key] = counts.get(key, 0) + 1
    return {
        "kind": "config",
        "node_count": total, "hci_node_count": hci, "so_count": so_count,
        "cluster_layout": cfg.get("cluster_layout") or (_cluster_layout(total) if total else []),
        "sized_full_cluster": False,
        "compute_floor": {},
        "legacy_ghz": 0, "max_vm_ram_gb": 0, "max_vm_cores": 0, "nic_mbps": 0,
        "storage_category": _storage_category(counts) if counts else None,
        "validated": cfg.get("mode") == "validated",
        "sized": {
            "cores": tot.get("cores"), "cores_n1": n1.get("cores"),
            "ram_gb": tot.get("ram_gb"), "ram_n1": n1.get("ram_gb"),
            "usable_storage_tb": tot.get("usable_storage_tb"),
            "perf_index": None,
            "total_ghz": tot.get("total_ghz"), "total_ghz_n1": n1.get("total_ghz"),
            "compute_coverage_pct": None,
            "usable_ram_per_node_gb": per.get("ram_gb"),
            "threads_per_node": per.get("threads"),
            "nic_ports": cfg.get("nic_ports"),
            "ram_per_node_gb": per.get("physical_ram_gb"),
            "cores_per_node": per.get("physical_cores"),
            "node_count": total, "model": cfg.get("model"), "cpu": per.get("cpu"),
            "drive_counts": counts,
        },
    }


# ─── 5. Compare ──────────────────────────────────────────────────────────────

def _relation(bom: Any, required: Any, sized: Any, no_demand: bool = False,
              sized_only: bool = False) -> str:
    """Owner semantics. `no_demand` (config-type sizing) turns 'fits' into
    'smaller': with no requirement to satisfy, below the sized hardware is
    simply less hardware. `sized_only` marks an informational dimension that
    is compared to the sized figure alone. Otherwise a missing requirement
    (old snapshot) is 'unknown' — 'fits' would claim a demand was met that
    nobody has seen."""
    if bom is None:
        return "unknown"
    if required is None and not (no_demand or sized_only):
        return "unknown"
    if required is not None and bom < required:
        return "smaller"
    if sized is None:
        return "fits" if required is not None else "unknown"
    if sized == 0:
        return "equal" if bom == 0 else "bigger"
    if abs(bom - sized) <= EQUAL_TOLERANCE * abs(sized):
        return "equal"
    if bom > sized:
        return "bigger"
    return "smaller" if no_demand else "fits"


def _dim(key, relation, bom, required, sized, unit, note=""):
    delta = None
    if bom is not None and sized is not None:
        delta = _round(bom - sized, unit)
    return {"key": key, "relation": relation, "bom": _round(bom, unit),
            "required": _round(required, unit), "sized": _round(sized, unit),
            "unit": unit, "delta_vs_sized": delta, "note": note}


def _fmt(value, unit):
    v = _round(value, unit)
    return "n/a" if v is None else f"{v} {unit}"


def compare(config: BOMConfig, configuration_or_requirements: Any,
            hci_count: Optional[int] = None) -> Dict[str, Any]:
    """The §5 'fit' object: per-dimension relation + deltas, cluster summary,
    notes and the overall verdict."""
    if isinstance(configuration_or_requirements, dict) and "kind" in configuration_or_requirements:
        req = configuration_or_requirements
    else:
        req = sizing_requirements(configuration_or_requirements)

    nodes = derive_nodes(config)
    cluster = cluster_from_nodes(nodes, hci_count)
    notes = list(nodes["notes"]) + list(cluster["notes"])
    if nodes["unresolved"]:
        notes.append("Unresolved BOM dimensions: " + ", ".join(nodes["unresolved"]) + ".")

    if req is None:
        return {
            "verdict": "unknown", "sizing": None, "config_name": config.name,
            "dimensions": [_dim(k, "unknown", None, None, None, _UNITS[k],
                                "The sizing has no stored result to compare against.")
                           for k in DIMENSION_KEYS],
            "cluster": _cluster_summary(cluster),
            "notes": notes + ["The selected sizing has no stored result."],
        }
    notes.extend(req.get("notes") or [])
    if nodes["node_count"] is None:
        # Node-count inference found no evidence: a guessed count silently
        # multiplies/divides every per-node figure and has turned an
        # under-sized BOM into a green verdict, so nothing is compared.
        note = ("Node count could not be determined from the BOM; fill the "
                "Nodes column of the template (or include the server line).")
        summary = _cluster_summary(cluster)
        summary["node_count"] = None
        return {
            "verdict": "unknown", "sizing": req.get("sizing"),
            "config_name": config.name,
            "dimensions": [_dim(k, "unknown", None, None, None, _UNITS[k], note)
                           for k in DIMENSION_KEYS],
            "cluster": summary,
            "notes": notes + [note],
        }
    no_demand = req.get("kind") == "config"
    if no_demand:
        notes.append("Direct build: relations are hardware vs hardware; 'smaller' means "
                     "less hardware than the build, not a failed requirement.")
    sized = req.get("sized") or {}
    full = bool(req.get("sized_full_cluster"))
    pool_label = "full cluster" if full else "N-1"
    dims = []  # type: List[Dict[str, Any]]

    # cores — on the same pool the engine gated on.
    bom_cores = cluster["cores_full"] if full else cluster["cores_n1"]
    sized_cores = sized.get("cores") if full else sized.get("cores_n1")
    if sized_cores is None:
        sized_cores = sized.get("cores")
    req_cores = None if no_demand else req.get("cores_required")
    rel = _relation(bom_cores, req_cores, sized_cores, no_demand)
    note = f"Usable cores at {pool_label} ({T.os_core_overhead} core per node reserved for the OS)."
    if bom_cores is None:
        note = "CPU could not be resolved to a core count."
    dims.append(_dim("cores", rel, bom_cores, req_cores, sized_cores, "cores", note))

    # compute — the active floor's blended coverage, else GHz informational.
    cf = req.get("compute_floor") or {}
    req_ghz = cf.get("required_ghz") or 0
    req_perf = cf.get("required_perf_index") or 0
    pool = cluster["hci_node_count"] if full else cluster["n1_hci_nodes"]
    if cf.get("active") and (req_ghz > 0 or req_perf > 0) and cluster["ghz_per_node"] is not None:
        cov = _compute_coverage(cluster["ghz_per_node"], cluster["node_perf"],
                                req_ghz, req_perf, cf.get("balance", 0.5), pool)
        if cov is None:
            dims.append(_dim("compute", "unknown", None, 100, sized.get("compute_coverage_pct"),
                             "%", "No compute demand recorded."))
        else:
            pct = cov["blended"] * 100
            s_pct = sized.get("compute_coverage_pct")
            rel = _relation(pct, 100.0, s_pct)
            parts = []
            if cov["ghz"] is not None:
                parts.append(f"GHz {cov['ghz'] * 100:.0f}%")
            if cov["perf"] is not None:
                parts.append(f"SPECrate {cov['perf'] * 100:.0f}%")
            elif cluster["node_perf"] is None:
                parts.append("no benchmark for this CPU, GHz only")
            note = (f"Blended compute coverage at {pool_label} "
                    f"(balance {cf.get('balance', 0.5)}): " + ", ".join(parts) + ".")
            if cluster["cpu"].get("base_clock_only"):
                note += " BOM clock is the base clock (conservative)."
            dims.append(_dim("compute", rel, pct, 100.0, s_pct, "%", note))
    else:
        bom_ghz = cluster["ghz_full"] if full else cluster["ghz_n1"]
        sized_ghz = sized.get("total_ghz") if full else sized.get("total_ghz_n1")
        if sized_ghz is None:
            sized_ghz = sized.get("total_ghz")
        if bom_ghz is None:
            dims.append(_dim("compute", "unknown", None, None, sized_ghz, "GHz",
                             "CPU could not be resolved to a clock/core count."))
        else:
            rel = _relation(bom_ghz, None, sized_ghz, no_demand, sized_only=True)
            legacy = req.get("legacy_ghz") or 0
            note = f"Total GHz at {pool_label}; informational — the compute floor is off."
            if legacy:
                note += (f" Source estate: {legacy:.0f} GHz nameplate "
                         f"({'covered' if bom_ghz >= legacy else 'not covered'} at {pool_label}).")
            if cluster["cpu"].get("base_clock_only"):
                note += " BOM clock is the base clock (conservative)."
            dims.append(_dim("compute", rel, bom_ghz, None, sized_ghz, "GHz", note))

    # ram — usable RAM on the same pool, vs max(projected, day-one floor).
    bom_ram = cluster["ram_full"] if full else cluster["ram_n1"]
    sized_ram = sized.get("ram_gb") if full else sized.get("ram_n1")
    if sized_ram is None:
        sized_ram = sized.get("ram_gb")
    req_ram = None if no_demand else req.get("ram_required_gb")
    rel = _relation(bom_ram, req_ram, sized_ram, no_demand)
    note = (f"Usable RAM at {pool_label} ({cluster['ram_overhead_gb']} GB OS overhead/node "
            f"for {cluster['bays']} bays).")
    if nodes["ram_gb_per_node"] == 0:
        rel = "unknown"
        note = "No memory found in the BOM."
    dims.append(_dim("ram", rel, bom_ram, req_ram, sized_ram, "GB", note))

    # largest VM — one node must host it (RAM and vCPUs).
    max_ram = req.get("max_vm_ram_gb") or 0
    max_cores = req.get("max_vm_cores") or 0
    usable_node = cluster["usable_ram_per_node"]
    threads = cluster["threads_per_node"]
    sized_node = sized.get("usable_ram_per_node_gb")
    if nodes["ram_gb_per_node"] == 0:
        rel, note = "unknown", "No memory found in the BOM."
    elif max_ram <= 0 and max_cores <= 0:
        rel = _relation(usable_node, None, sized_node, no_demand, sized_only=True)
        note = "No largest-VM data in the sizing; usable RAM per node vs the sized node."
    else:
        rel = _relation(usable_node, max_ram if max_ram > 0 else None, sized_node)
        note = f"Largest VM: {max_ram:.0f} GB RAM, {max_cores} vCPUs."
        if max_cores > 0:
            if threads is None:
                note += " Threads per node unknown."
            elif threads < max_cores:
                rel = "smaller"
                note += f" Node has {threads} threads — the VM's vCPUs do not fit one node."
        if usable_node > 0 and max_ram > 0 and rel != "smaller":
            frac = max_ram / usable_node
            if frac >= LARGEST_VM_WARN_FRACTION:
                note += (f" Warning: the largest VM uses {frac * 100:.0f}% of one node's "
                         f"usable RAM — check HA implications.")
    dims.append(_dim("largest_vm", rel, usable_node, max_ram if max_ram > 0 else None,
                     sized_node, "GB", note))

    # storage — usable TB (RF2 + rebuild reserve), plus feasibility facts.
    bom_tb = cluster["usable_storage_tb"]
    sized_tb = sized.get("usable_storage_tb")
    req_tb = None if no_demand else req.get("storage_required_tb")
    rel = _relation(bom_tb if cluster["bays"] else None, req_tb, sized_tb, no_demand)
    note = "Usable TB after RF2 and one rebuild disk per cluster."
    if not cluster["bays"]:
        note = "No data drives found in the BOM."
    feas = cluster["feasibility"]
    s_cat = req.get("storage_category")
    b_cat = cluster["storage_category"]
    if s_cat and b_cat and s_cat != b_cat:
        notes.append(f"Storage tier differs: BOM is {b_cat}, the sizing is {s_cat}.")
    if feas["exactly_two_disks"]:
        notes.append("Exactly 2 disks per node is not a supported layout (1 or 3+).")
    if not feas["disk_cap_ok"]:
        notes.append(f"{feas['cluster_disks']} disks in the largest cluster exceed the "
                     f"{feas['max_cluster_disks']}-disk limit.")
    if feas["hybrid_flash_in_band"] is False:
        notes.append(f"Hybrid flash tier is {feas['flash_pct_of_raw']}% of raw; the band is "
                     f"{T.hybrid_flash_min_pct:g}-{T.hybrid_flash_max_pct:g}%.")
    if feas["hybrid_hdd_ratio_ok"] is False:
        notes.append(f"Hybrid layout has {feas['hdd_per_flash']} HDDs per flash disk; "
                     f"at least {T.hybrid_min_hdd_per_flash} are needed.")
    if not feas["min_hci_per_cluster_ok"]:
        notes.append(f"{cluster['hci_node_count']} HCI nodes is below the "
                     f"{feas['min_hci_required']} required for this layout.")
    dims.append(_dim("storage", rel, bom_tb if cluster["bays"] else None, req_tb, sized_tb,
                     "TB", note))

    # nic — informational; relation computed, never reds the verdict.
    bom_nic = cluster["nic_gbe"]
    req_nic = (req.get("nic_mbps") or 0) / 1000.0 or None
    rel = _relation(bom_nic, req_nic, None, sized_only=True)
    if rel == "unknown" and bom_nic is not None:
        rel = "fits"
    note = "Informational only: NIC speed never fails the fit on its own."
    if bom_nic is None:
        note = "No NIC speed found in the BOM. " + note
    elif req_nic:
        note = f"Source hosts run {req_nic:g} GbE. " + note
    if cluster["nic_ports"] is not None and sized.get("nic_ports") is not None:
        note += f" Ports per adapter: BOM {cluster['nic_ports']}, sized {sized['nic_ports']}."
    dims.append(_dim("nic", rel, bom_nic, req_nic, None, "GbE", note))

    # nodes — decided last: fewer nodes is only a problem if something else is.
    others_smaller = any(d["relation"] == "smaller" for d in dims
                         if d["key"] in VERDICT_DIMENSIONS)
    sized_nodes = req.get("node_count")
    bom_nodes = cluster["node_count"]
    if sized_nodes is None:
        rel, note = "unknown", "The sizing carries no node count."
    elif bom_nodes == sized_nodes:
        rel, note = "equal", "Same node count as the sizing."
    elif bom_nodes > sized_nodes:
        rel, note = "bigger", f"{bom_nodes - sized_nodes} more node(s) than the sizing."
    elif no_demand:
        rel, note = "smaller", f"{sized_nodes - bom_nodes} fewer node(s) than the build."
    elif others_smaller:
        rel = "smaller"
        note = f"{sized_nodes - bom_nodes} fewer node(s) and another dimension is under requirement."
    else:
        rel = "fits"
        note = f"{sized_nodes - bom_nodes} fewer node(s), yet every requirement is met."
    if req.get("so_count"):
        note += f" Sizing has {req['so_count']} storage-only node(s)."
    if cluster["so_count"]:
        note += f" BOM treated as {cluster['hci_node_count']} HCI + {cluster['so_count']} storage-only."
    dims.insert(0, _dim("nodes", rel, bom_nodes, None, sized_nodes, "nodes", note))

    return {
        "verdict": _verdict(dims),
        "sizing": req.get("sizing"),
        "config_name": config.name,
        "dimensions": dims,
        "cluster": _cluster_summary(cluster),
        "notes": notes,
    }


_UNITS = {"nodes": "nodes", "cores": "cores", "compute": "%", "ram": "GB",
          "largest_vm": "GB", "storage": "TB", "nic": "GbE"}


def _verdict(dims: List[Dict[str, Any]]) -> str:
    rels = [d["relation"] for d in dims if d["key"] in VERDICT_DIMENSIONS]
    if "smaller" in rels:
        return "smaller"
    if "bigger" in rels:
        return "bigger"
    if rels and all(r == "unknown" for r in rels):
        return "unknown"
    return "match"


def _cluster_summary(c: Dict[str, Any]) -> Dict[str, Any]:
    cpu = c.get("cpu") or {}
    return {
        "node_count": c["node_count"], "hci_node_count": c["hci_node_count"],
        "so_count": c["so_count"], "cluster_layout": c["cluster_layout"],
        "cpu": cpu.get("desc"), "cpu_source": cpu.get("source"),
        "sockets_per_node": c["sockets_per_node"],
        "cores_per_node": c["cores_per_node"], "threads_per_node": c["threads_per_node"],
        "ram_per_node_gb": c["ram_per_node_gb"],
        "usable_ram_per_node_gb": c["usable_ram_per_node"],
        "cores_full": c["cores_full"], "cores_n1": c["cores_n1"],
        "ram_full": _round(c["ram_full"], "GB"), "ram_n1": _round(c["ram_n1"], "GB"),
        "ghz_full": _round(c["ghz_full"], "GHz"), "ghz_n1": _round(c["ghz_n1"], "GHz"),
        "perf_full": _round(c["perf_full"], "idx"), "perf_n1": _round(c["perf_n1"], "idx"),
        "raw_storage_tb": _round(c["raw_storage_tb"], "TB"),
        "usable_storage_tb": _round(c["usable_storage_tb"], "TB"),
        "usable_by_kind_tb": {k: _round(v, "TB") for k, v in c["usable_by_kind"].items()},
        "drive_counts": c["drive_counts"], "bays": c["bays"],
        "storage_category": c["storage_category"],
        "nic_gbe": c["nic_gbe"], "nic_ports": c["nic_ports"],
    }


# ─── 6. Which config to compare ──────────────────────────────────────────────

def pick_config_for_sizing(bom: NormalizedBOM, requirements: Optional[Dict[str, Any]]) -> Optional[BOMConfig]:
    """First hardware config whose node count equals the sizing's; else the
    first hardware config (a quote usually lists options, the first is 'the'
    one)."""
    hardware = [c for c in bom.configs if is_hardware_config(c)]
    if not hardware:
        return None
    want = (requirements or {}).get("node_count")
    if want:
        for c in hardware:
            if derive_nodes(c)["node_count"] == want:
                return c
    return hardware[0]
