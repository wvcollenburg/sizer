"""What a BOM file says about the hardware, as a small stable record.

Used by the partner-BOM archive harness (tests/test_bom_archive.py and
tools/bom_archive.py): every real BOM dropped into ``_archive/boms/`` is read
into these facts, checked against sanity rules, and — once a person has looked
at them — compared against the reviewed ``<file>.expected.json`` next to it.

Deliberately limited to what the FILE establishes: format, configs, node
counts, per-node CPU / RAM / disks / NICs and the storage maths that follows
from them. Nothing that depends on the HCL catalog (verdicts, findings,
platform matches) or on admin-tunable figures, so the facts of a file never
change unless the parsers or the fit derivation change — which is exactly what
the harness is there to notice.

Pure: no Flask app or database needed (bom/fit.py's catalog lookups skip
themselves without one).
"""
from typing import Any, Dict, List

from bom import fit
from bom.parsers import parse_file
from bom.rules import is_hardware_config

ARCHIVE_EXTENSIONS = (".xlsx", ".csv")


def config_facts(config) -> Dict[str, Any]:
    nodes = fit.derive_nodes(config)
    cluster = fit._cluster_summary(fit.cluster_from_nodes(nodes))
    return {
        "name": config.name,
        "server_model": config.server_model,
        "node_count": cluster["node_count"],
        "hci_node_count": cluster["hci_node_count"],
        "sockets_per_node": cluster["sockets_per_node"],
        "cpu": cluster["cpu"],
        "cpu_source": cluster["cpu_source"],
        "cores_per_node": cluster["cores_per_node"],
        "threads_per_node": cluster["threads_per_node"],
        # The CPU's sizing clock and benchmark, per node: a mis-read clock
        # ('2,8 GHz' once parsed as 8) changes every compute figure downstream.
        "ghz_per_node": (round(cluster["ghz_full"] / cluster["hci_node_count"], 1)
                         if cluster.get("ghz_full") and cluster.get("hci_node_count") else None),
        "perf_per_node": (round(cluster["perf_full"] / cluster["hci_node_count"], 1)
                          if cluster.get("perf_full") and cluster.get("hci_node_count") else None),
        "ram_per_node_gb": cluster["ram_per_node_gb"],
        "drives": sorted([[d["kind"], d["capacity_tb"], d["qty_per_node"]]
                          for d in cluster.get("drives") or []]),
        "storage_category": cluster["storage_category"],
        "raw_storage_tb": cluster["raw_storage_tb"],
        "usable_storage_tb": cluster["usable_storage_tb"],
        "nic_gbe": cluster["nic_gbe"],
        "nic_ports": cluster["nic_ports"],
        "unresolved": sorted(nodes["unresolved"]),
    }


def bom_facts(path: str) -> Dict[str, Any]:
    """Facts for one BOM file. Raises parsers.UnrecognizedFormat when no
    parser recognises it."""
    bom, fmt = parse_file(path, path)
    return {
        "format": fmt,
        "vendor": bom.vendor,
        "configs": [config_facts(c) for c in bom.configs if is_hardware_config(c)],
    }


def sanity_problems(facts: Dict[str, Any]) -> List[str]:
    """Things that are wrong with a read no matter which BOM it is — the silent
    losses a parse can survive while looking successful (a hybrid node that
    lost its spindles, a node count guessed away, a CPU nobody recognised)."""
    problems = []  # type: List[str]
    if not facts["configs"]:
        return ["no hardware configuration found"]
    for c in facts["configs"]:
        label = "config %r" % c["name"]
        if c["node_count"] is None:
            problems.append("%s: node count could not be determined" % label)
        if not c["server_model"]:
            problems.append("%s: no server model" % label)
        if not c["cores_per_node"]:
            problems.append("%s: no CPU cores" % label)
        if c["cpu_source"] in (None, "unresolved"):
            problems.append("%s: CPU not resolved (%s)" % (label, c["cpu"]))
        if not c["ram_per_node_gb"]:
            problems.append("%s: no RAM" % label)
        if not c["drives"]:
            problems.append("%s: no data drives" % label)
        elif any(not cap for _kind, cap, _qty in c["drives"]):
            problems.append("%s: a drive without a capacity" % label)
        if not c["usable_storage_tb"]:
            problems.append("%s: no usable storage" % label)
        if c["unresolved"]:
            problems.append("%s: unresolved %s" % (label, ", ".join(c["unresolved"])))
    return problems
