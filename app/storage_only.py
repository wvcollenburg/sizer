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


def single_cpu_options(cpu_options):
    """Derive single-socket CPU choices for a storage-only node from a model's
    ``cpu_options`` (each ``{desc, cores, threads, ghz}``). A storage-only node
    always populates ONE CPU, so a dual ("2 x") option collapses to a single
    ("1 x") with half the cores/threads. Order is preserved (lowest tier first,
    as supplied); duplicate single-CPU configs are merged."""
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
