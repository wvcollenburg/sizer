"""Helpers for storage-only nodes — full HyperCore nodes with the
virtualization layer disabled. They run no VMs (contribute no usable
compute) but still participate in the storage cluster, adding raw capacity
and drive IOPS.

Rules (in addition to the normal cluster rules):
  * Minimum 2 full HCI nodes per cluster (HA + rolling updates).
  * Storage architecture / drives must match the HCI nodes (same model).
  * Single CPU, lowest tier — even when the HCI nodes are dual-socket. The
    "D" in a model name (e.g. HC5650D) denotes dual CPUs; a storage-only
    node populates just one, i.e. the single-socket sibling config.
  * Minimal memory, but at least 16 GB. In Certified configs the compliant
    minimum is often higher (the model's smallest RAM option), so callers
    floor against that.
  * Best practice: same family (1000 with 1000, 3000 with 3000, …) — implied
    by reusing the same model.
"""
import re

MIN_HCI_NODES_PER_CLUSTER = 2
STORAGE_ONLY_RAM_FLOOR_GB = 16

_QTY_RE = re.compile(r"^\s*(\d+)\s*x\s+", re.IGNORECASE)


def is_single_cpu(cpu_options):
    """True when every CPU option is single-socket (a "1 x" quantity prefix)."""
    if not cpu_options:
        return False
    return all(c.get("desc", "").lstrip().startswith(("1 x", "1 × "))
               for c in cpu_options)


def sibling_single_socket_name(name):
    """The single-socket sibling model name for a dual ("D") appliance, or None.
    The "D" denotes the dual-CPU variant (HC5650D, HC3650DF); dropping it gives
    the single-socket sibling (HC5650, HC3650F). Whether that sibling actually
    exists in the catalog is the caller's responsibility — many dual-only
    families have no single sibling (HC5650D, HC5250D, HC1650D, …)."""
    if name.endswith("DF"):
        return name[:-2] + "F"
    if name.endswith("D"):
        return name[:-1]
    return None


def certified_single_cpu_options(cpu_options, sibling_cpu_options):
    """Real single-CPU choices for a *certified* storage-only node — never
    fabricated. Returns, in order of preference:
      * the model's own options if it is already single-socket;
      * else a single-socket sibling's options when one exists;
      * else the model's own (dual) options — no single-CPU SKU exists in the
        family, so the storage-only node keeps the dual CPU (lowest tier).
    ``sibling_cpu_options`` is the sibling model's cpu_options (or None)."""
    if is_single_cpu(cpu_options):
        return cpu_options
    if sibling_cpu_options and is_single_cpu(sibling_cpu_options):
        return sibling_cpu_options
    return cpu_options


def single_cpu_options(cpu_options):
    """Derive single-socket CPU choices for a *validated* (software-only)
    storage-only node from a model's ``cpu_options`` (each
    ``{desc, cores, threads, ghz}``). Software-only permits running one CPU on
    any platform, so a dual ("2 x") option collapses to a single ("1 x") with
    half the cores/threads. Order is preserved (lowest tier first); duplicates
    merged. NOT for Certified — that must use real SKUs only (see
    ``certified_single_cpu_options``)."""
    out = []
    seen = set()
    for c in cpu_options or []:
        m = _QTY_RE.match(c.get("desc", ""))
        qty = int(m.group(1)) if m else 1
        base = c["desc"][m.end():] if m else c.get("desc", "")
        if qty < 1:
            qty = 1
        cores = max(1, int(c.get("cores", 0)) // qty)
        threads = max(1, int(c.get("threads", 0)) // qty)
        desc = f"1 x {base}".rstrip()
        if desc in seen:
            continue
        seen.add(desc)
        out.append({
            "desc": desc,
            "cores": cores,
            "threads": threads,
            "ghz": c.get("ghz", 0),
        })
    return out
