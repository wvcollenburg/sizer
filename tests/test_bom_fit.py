"""bom.fit — BOM config -> transient cluster -> per-dimension fit vs a sizing.

Everything except the last block is pure (no Flask): derive_nodes, resolve_cpu,
cluster_from_nodes and compare() are exercised with hand-built requirement
dicts so each relation (equal / bigger / fits / smaller / unknown) is pinned
against the owner's semantics from docs/bom-checker-build.md §5. The last
block builds a minimal Flask+SQLite app to read a real Configuration row
through sizing_requirements(), because that is where old snapshots must
degrade into notes rather than KeyErrors.

Run: .venv/bin/python -m pytest tests/test_bom_fit.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402

from tunables import T  # noqa: E402
from bom.normalize import BOMComponent, BOMConfig, NormalizedBOM  # noqa: E402
from bom import fit  # noqa: E402


# ─── fixtures ────────────────────────────────────────────────────────────────

CPU_6526Y = "Intel Xeon Gold 6526Y 16C 195W 2.8GHz Processor"
DIMM_32 = "ThinkSystem 32GB TruDDR5 5600MHz (2Rx8) RDIMM"
NVME_384 = 'ThinkSystem 2.5" U.2 VA 3.84TB Read Intensive NVMe PCIe 4.0 x4 HS SSD'
NIC_E810 = "ThinkSystem Intel E810-DA4 10/25GbE SFP28 4-Port OCP Ethernet Adapter"


def _c(desc, qty, cat, part=None):
    return BOMComponent(part_number=part, description=desc, quantity=qty, category=cat)


def lenovo_config(nodes=3, ram_dimms_per_node=8, drives_per_node=4, cpu_qty=None,
                  cpu=CPU_6526Y, name="SR650 V3"):
    """3-node Lenovo-style config: quantities are totals across nodes."""
    return BOMConfig(name=name, server_model="ThinkSystem SR650 V3", components=[
        _c("ThinkSystem SR650 V3 MB", nodes, "chassis"),
        _c(cpu, cpu_qty if cpu_qty is not None else nodes, "cpu"),
        _c(DIMM_32, ram_dimms_per_node * nodes, "memory"),
        _c(NVME_384, drives_per_node * nodes, "storage"),
        _c(NIC_E810, nodes, "nic"),
        _c("ThinkSystem 1U 2.5\" 8-Bay Backplane", nodes, "other"),
    ])


def dell_single_node():
    """A Dell quote: one server, everything listed with qty 1 (two CPU lines)."""
    return BOMConfig(name="R760", server_model="PowerEdge R760", components=[
        _c("PowerEdge R760 Server", 1, "chassis"),
        _c("Intel Xeon Gold 6526Y 2.8G, 16C/32T, 20GT/s, 37.5M Cache, Turbo, HT (195W) DDR5-5200", 1, "cpu"),
        _c("Intel Xeon Gold 6526Y 2.8G, 16C/32T, 20GT/s, 37.5M Cache, Turbo, HT (195W) DDR5-5200, Additional Processor", 1, "cpu"),
        _c("64GB RDIMM, 5600MT/s, Dual Rank", 8, "memory"),
        _c("3.84TB Enterprise NVMe Read Intensive AG Drive U.2 Gen4 with carrier", 4, "storage"),
        _c("Broadcom 57414 Dual Port 10/25GbE SFP28 Adapter, OCP NIC 3.0", 1, "nic"),
        _c("No BOSS Card", 1, "boss"),
        _c("Riser Blank", 1, "other"),
    ])


@pytest.fixture(autouse=True)
def _default_tunables():
    """Pure tests must not depend on whatever a previous test left in T."""
    T.set_values({})
    yield
    T.set_values({})


def _dims(result):
    return {d["key"]: d for d in result["dimensions"]}


def requirements(**over):
    """Recommendation-type requirements that mirror the 3-node Lenovo BOM
    exactly (so the untouched case is 'equal' on every dimension): 6526Y at
    N-1 = 2 nodes -> 30 usable cores, 500 GB usable RAM, (15.36x3-3.84)/2 = 21.12 TB usable."""
    req = {
        "kind": "recommendation",
        "node_count": 3, "hci_node_count": 3, "so_count": 0, "cluster_layout": [3],
        "sized_full_cluster": False,
        "cores_required": 24, "ram_required_gb": 400, "storage_required_tb": 15.0,
        "compute_floor": {"active": False},
        "legacy_ghz": 200.0,
        "max_vm_ram_gb": 64, "max_vm_cores": 8, "nic_mbps": 10000,
        "storage_category": "flash", "validated": True,
        "sized": {
            "cores": 45, "cores_n1": 30, "ram_gb": 750, "ram_n1": 500,
            "usable_storage_tb": 21.12, "perf_index": 508.5, "total_ghz": 168.0,
            "total_ghz_n1": 112.0, "compute_coverage_pct": None,
            "usable_ram_per_node_gb": 250, "threads_per_node": 32, "nic_ports": 4,
            "ram_per_node_gb": 256, "cores_per_node": 16, "node_count": 3,
            "model": "HC3350", "cpu": "1 x Xeon Gold 6526Y",
        },
        "notes": [], "sizing": {"id": 1, "name": "Prod", "mode": "import"},
    }
    sized = over.pop("sized", None)
    req.update(over)
    if sized:
        req["sized"].update(sized)
    return req


# ─── 1. derive_nodes ─────────────────────────────────────────────────────────

def test_derive_nodes_lenovo_three_node_config():
    n = fit.derive_nodes(lenovo_config())
    assert n["node_count"] == 3
    assert n["cpus"] == [{"model_text": CPU_6526Y, "qty_per_node": 1}]
    assert n["sockets_per_node"] == 1
    assert n["ram_gb_per_node"] == 256
    assert n["dimm_count_per_node"] == 8 and n["dimm_gb"] == 32
    assert len(n["drives"]) == 1
    d = n["drives"][0]
    assert (d["kind"], d["capacity_tb"], d["qty_per_node"]) == ("nvme", 3.84, 4)
    assert n["bays_per_node"] == 4
    assert n["nic_speed_gbe"] == 25 and n["nic_ports"] == 4
    assert n["unresolved"] == []


def test_derive_nodes_dell_single_node_two_sockets():
    n = fit.derive_nodes(dell_single_node())
    assert n["node_count"] == 1, "the 'Server' chassis line carries the node count"
    assert n["sockets_per_node"] == 2, "two qty-1 CPU lines are two sockets in one node"
    assert n["ram_gb_per_node"] == 512
    assert n["drives"][0]["qty_per_node"] == 4
    assert n["nic_speed_gbe"] == 25 and n["nic_ports"] == 2
    # Absence indicators and non-hardware lines never leak into the model.
    assert not any("BOSS" in note for note in n["notes"])


def test_derive_nodes_without_chassis_line_is_unknown_not_a_guess():
    # Finding: "Node-count inference silently doubles the cluster and turns an
    # under-sized BOM into a green 'bigger' verdict" — a 3-node dual-socket
    # BOM (6 CPUs / 48 DIMMs / 12 drives) used to be guessed as 6 single-
    # socket nodes from the smallest capacity quantity. No evidence -> None.
    cfg = BOMConfig(name="x", server_model=None, components=[
        _c(CPU_6526Y, 6, "cpu"), _c(DIMM_32, 48, "memory"), _c(NVME_384, 12, "storage"),
    ])
    n = fit.derive_nodes(cfg)
    assert n["node_count"] is None
    assert "nodes" in n["unresolved"]
    assert any("Node count unknown" in note for note in n["notes"])


def test_derive_nodes_backplane_rows_are_not_the_server_line():
    # Finding (chassis branch): a Lenovo backplane row is categorised
    # 'chassis' and names the model; listed before the chassis row with 2 per
    # node it used to win the node count. It must be skipped.
    cfg = BOMConfig(name="x", server_model="ThinkSystem SR650 V3", components=[
        _c('ThinkSystem SR650 V3 2.5" SAS/SATA 8-Bay Backplane', 6, "chassis"),
        _c("ThinkSystem SR650 V3 Chassis", 3, "chassis"),
        _c(CPU_6526Y, 6, "cpu"), _c(DIMM_32, 48, "memory"), _c(NVME_384, 12, "storage"),
    ])
    assert fit.derive_nodes(cfg)["node_count"] == 3
    # ... and with ONLY the backplane row there is no evidence at all.
    cfg2 = BOMConfig(name="y", server_model="ThinkSystem SR650 V3", components=[
        _c('ThinkSystem SR650 V3 2.5" SAS/SATA 8-Bay Backplane', 6, "chassis"),
        _c(CPU_6526Y, 6, "cpu"), _c(DIMM_32, 48, "memory"), _c(NVME_384, 12, "storage"),
    ])
    assert fit.derive_nodes(cfg2)["node_count"] is None


def test_compare_unknown_node_count_never_reaches_the_verdict():
    # Finding regression: with the node count unguessable, every dimension is
    # 'unknown' and the verdict is 'unknown' — never a confident 'bigger'
    # built on a doubled cluster.
    cfg = BOMConfig(name="x", server_model=None, components=[
        _c(CPU_6526Y, 6, "cpu"), _c(DIMM_32, 48, "memory"), _c(NVME_384, 12, "storage"),
        _c(NIC_E810, 3, "nic"),
    ])
    r = fit.compare(cfg, requirements())
    assert r["verdict"] == "unknown"
    assert all(d["relation"] == "unknown" for d in r["dimensions"])
    assert any("Nodes column" in n for n in r["notes"])
    assert r["cluster"]["node_count"] is None


def test_derive_nodes_honours_parser_supplied_node_count():
    cfg = lenovo_config(nodes=3)
    cfg.node_count = 3
    cfg.components = [c for c in cfg.components if c.category != "chassis"]
    assert fit.derive_nodes(cfg)["node_count"] == 3


def test_derive_nodes_non_integer_split_is_floored_and_flagged():
    n = fit.derive_nodes(lenovo_config(nodes=2, cpu_qty=5))
    assert n["node_count"] == 2
    assert n["cpus"][0]["qty_per_node"] == 2, "5 CPUs over 2 nodes: size on 2 sockets"
    assert "cpu" in n["unresolved"]
    assert any("5 across 2 nodes" in note for note in n["notes"])


def test_derive_nodes_parses_gb_drives_hdd_kind_and_various_nic_strings():
    cfg = BOMConfig(name="h", server_model="SR650", components=[
        _c("ThinkSystem SR650 chassis", 2, "chassis"),
        _c(CPU_6526Y, 2, "cpu"),
        _c("ThinkSystem 64 GB TruDDR5 RDIMM", 8, "memory"),
        _c("ThinkSystem 3.5\" 12TB 7.2K SAS 12Gb Hot Swap 512e HDD", 12, "storage"),
        _c("ThinkSystem 2.5\" 960GB Mixed Use SATA 6Gb HS SSD", 4, "storage"),
        _c("Intel X710-T4L 4x10GBase-T OCP", 2, "nic"),
        _c("Broadcom 5720 Quad Port 1GbE BASE-T Adapter", 2, "nic"),
    ])
    n = fit.derive_nodes(cfg)
    kinds = {d["kind"]: d for d in n["drives"]}
    assert kinds["hdd"]["capacity_tb"] == 12 and kinds["hdd"]["qty_per_node"] == 6
    assert kinds["ssd"]["capacity_tb"] == pytest.approx(0.96) and kinds["ssd"]["qty_per_node"] == 2
    assert n["ram_gb_per_node"] == 256 and n["dimm_gb"] == 64
    assert n["nic_speed_gbe"] == 10, "the fastest NIC wins; 6Gb/12Gb drive interfaces are not NICs"
    assert n["nic_ports"] == 4


# ─── 2. resolve_cpu ──────────────────────────────────────────────────────────

def test_resolve_cpu_from_catalog_folds_sockets_and_uses_sizing_clock():
    cpu = fit.resolve_cpu(CPU_6526Y, 2)
    assert cpu["source"] == "catalog"
    assert cpu["cores"] == 32 and cpu["threads"] == 64 and cpu["p_cores"] == 32
    assert cpu["ghz"] == 3.5, "all-core turbo, not the 2.8 GHz base printed in the BOM"
    assert cpu["perf_index"] == pytest.approx(339.0)
    assert cpu["base_clock_only"] is False
    assert cpu["desc"] == "2 x Xeon Gold 6526Y"


def test_resolve_cpu_unknown_sku_is_parsed_from_bom_text_and_flagged():
    cpu = fit.resolve_cpu("Intel Xeon E5-2697A v4 16C 2.6GHz", 1)
    assert cpu["source"] == "parsed"
    assert cpu["cores"] == 16 and cpu["threads"] == 32
    assert cpu["ghz"] == 2.6 and cpu["base_clock_only"] is True
    assert cpu["perf_index"] is None


def test_resolve_cpu_benchmark_lookup_supplies_specrate_only():
    cpu = fit.resolve_cpu("Intel Xeon E5-2650 v4 12-Core 2.2GHz", 2)
    assert cpu["source"] == "spec-cpu2017"
    assert cpu["cores"] == 24 and cpu["perf_index"] == pytest.approx(105.0)
    assert cpu["base_clock_only"] is True


def test_resolve_cpu_nothing_parseable_is_unresolved():
    cpu = fit.resolve_cpu("Mystery Processor", 1)
    assert cpu["source"] == "unresolved" and cpu["cores"] is None


# ─── 3. cluster_from_nodes ───────────────────────────────────────────────────

def test_cluster_three_nodes_rf2_rebuild_reserve_and_n_minus_1():
    c = fit.cluster_from_nodes(fit.derive_nodes(lenovo_config()))
    assert c["cluster_layout"] == [3] and c["n1_hci_nodes"] == 2
    assert c["usable_storage_tb"] == pytest.approx((15.36 * 3 - 3.84) / 2)  # 21.12
    overhead = T.usable_ram_overhead_for(4)
    assert c["ram_n1"] == (256 - overhead) * 2
    assert c["cores_n1"] == (16 - T.os_core_overhead) * 2
    assert c["cores_full"] == 45 and c["threads_per_node"] == 32
    assert c["perf_full"] == pytest.approx(169.5 * 3)
    assert c["n_minus_1"]["ram_gb"] == c["ram_n1"]
    assert c["storage_category"] == "flash" and c["nic_gbe"] == 25
    assert c["feasibility"]["disk_cap_ok"] and not c["feasibility"]["exactly_two_disks"]


def test_cluster_single_node_uses_the_sns_rule():
    c = fit.cluster_from_nodes(fit.derive_nodes(dell_single_node()))
    assert c["node_count"] == 1 and c["n1_hci_nodes"] == 1
    assert c["usable_storage_tb"] == pytest.approx(15.36 / 2), "raw/2, no rebuild reserve"
    one_disk = fit.derive_nodes(lenovo_config(nodes=1, drives_per_node=1))
    assert fit.cluster_from_nodes(one_disk)["usable_storage_tb"] == pytest.approx(3.84)


def test_cluster_ten_nodes_splits_into_two_clusters():
    c = fit.cluster_from_nodes(fit.derive_nodes(lenovo_config(nodes=10)))
    assert c["cluster_layout"] == [5, 5] and c["num_clusters"] == 2
    assert c["n1_hci_nodes"] == 8, "one node down per cluster"
    assert c["usable_storage_tb"] == pytest.approx(2 * (15.36 * 5 - 3.84) / 2)


def test_cluster_storage_only_nodes_reduce_the_compute_pool_only():
    c = fit.cluster_from_nodes(fit.derive_nodes(lenovo_config(nodes=4)), hci_count=3)
    assert (c["hci_node_count"], c["so_count"]) == (3, 1)
    assert c["cores_n1"] == 30 and c["usable_storage_tb"] == pytest.approx((15.36 * 4 - 3.84) / 2)
    assert c["feasibility"]["min_hci_per_cluster_ok"]


def test_cluster_hybrid_feasibility_facts():
    cfg = BOMConfig(name="h", server_model="SR650", components=[
        _c("SR650 chassis", 3, "chassis"), _c(CPU_6526Y, 3, "cpu"), _c(DIMM_32, 24, "memory"),
        _c("12TB 7.2K SAS HDD", 18, "storage"), _c("3.84TB NVMe SSD", 3, "storage"),
    ])
    c = fit.cluster_from_nodes(fit.derive_nodes(cfg))
    f = c["feasibility"]
    assert c["storage_category"] == "hybrid"
    assert f["hdd_per_flash"] == 6.0 and f["hybrid_hdd_ratio_ok"]
    assert f["flash_pct_of_raw"] == pytest.approx(3.84 / 75.84 * 100, abs=0.1)
    assert f["hybrid_flash_in_band"] is False, "5% flash is under the 7% floor"


# ─── 5. compare ──────────────────────────────────────────────────────────────

def test_compare_identical_bom_is_equal_on_every_dimension():
    r = fit.compare(lenovo_config(), requirements())
    d = _dims(r)
    for key in ("nodes", "cores", "ram", "storage", "largest_vm"):
        assert d[key]["relation"] == "equal", (key, d[key])
    assert d["storage"]["bom"] == 21.12 and d["storage"]["delta_vs_sized"] == 0
    assert r["verdict"] == "match"
    assert r["config_name"] == "SR650 V3" and r["sizing"]["id"] == 1


def test_compare_within_two_percent_counts_as_equal():
    r = fit.compare(lenovo_config(), requirements(sized={"ram_n1": 508}))
    assert _dims(r)["ram"]["relation"] == "equal"
    r = fit.compare(lenovo_config(), requirements(sized={"ram_n1": 540}))
    assert _dims(r)["ram"]["relation"] == "fits"


def test_compare_bigger_shows_delta_and_never_warns():
    r = fit.compare(lenovo_config(ram_dimms_per_node=16), requirements())
    d = _dims(r)["ram"]
    assert d["relation"] == "bigger"
    assert d["bom"] == 1012 and d["delta_vs_sized"] == 512
    assert r["verdict"] == "bigger"
    assert not any("warn" in n.lower() for n in r["notes"])


def test_compare_fits_meets_requirement_below_sized():
    r = fit.compare(lenovo_config(), requirements(sized={"ram_n1": 700, "ram_gb": 1050}))
    d = _dims(r)["ram"]
    assert d["relation"] == "fits" and d["delta_vs_sized"] == -200
    assert r["verdict"] == "match", "fits is green — the requirement is met"


def test_compare_smaller_reds_the_verdict():
    r = fit.compare(lenovo_config(), requirements(ram_required_gb=600))
    d = _dims(r)["ram"]
    assert d["relation"] == "smaller" and d["required"] == 600 and d["bom"] == 500
    assert r["verdict"] == "smaller"


def test_compare_mixed_bigger_and_smaller_is_smaller():
    r = fit.compare(lenovo_config(ram_dimms_per_node=16),
                    requirements(storage_required_tb=30.0))
    d = _dims(r)
    assert d["ram"]["relation"] == "bigger" and d["storage"]["relation"] == "smaller"
    assert r["verdict"] == "smaller"


def test_compare_compute_uses_the_engine_coverage_blend():
    # Demand exactly what 2 x 6526Y deliver: 3.5 GHz x 16 cores x 2 nodes = 112 GHz,
    # 169.5 SPECrate x 2 nodes = 339 -> blended coverage 100 %.
    req = requirements(compute_floor={"active": True, "balance": 0.5,
                                      "required_ghz": 112.0, "required_perf_index": 339.0},
                       sized={"compute_coverage_pct": 100.0})
    r = fit.compare(lenovo_config(), req)
    d = _dims(r)["compute"]
    assert d["unit"] == "%" and d["bom"] == pytest.approx(100.0) and d["relation"] == "equal"
    assert "GHz 100%" in d["note"] and "SPECrate 100%" in d["note"]

    req = requirements(compute_floor={"active": True, "balance": 0.5,
                                      "required_ghz": 224.0, "required_perf_index": 678.0})
    r = fit.compare(lenovo_config(), req)
    d = _dims(r)["compute"]
    assert d["bom"] == pytest.approx(50.0) and d["relation"] == "smaller"
    assert r["verdict"] == "smaller"


def test_compare_compute_floor_off_is_informational_ghz():
    r = fit.compare(lenovo_config(), requirements(legacy_ghz=500.0))
    d = _dims(r)["compute"]
    assert d["unit"] == "GHz" and d["required"] is None
    assert d["relation"] == "equal" and "informational" in d["note"]
    assert "not covered" in d["note"]
    assert r["verdict"] == "match", "the legacy GHz signal never reds the verdict"


def test_compare_largest_vm_guard_on_ram_and_threads():
    r = fit.compare(lenovo_config(), requirements(max_vm_ram_gb=300))
    d = _dims(r)["largest_vm"]
    assert d["relation"] == "smaller" and d["bom"] == 250 and d["required"] == 300
    assert r["verdict"] == "smaller"

    r = fit.compare(lenovo_config(), requirements(max_vm_cores=64))
    d = _dims(r)["largest_vm"]
    assert d["relation"] == "smaller" and "threads" in d["note"]

    r = fit.compare(lenovo_config(), requirements(max_vm_ram_gb=230))
    d = _dims(r)["largest_vm"]
    assert d["relation"] == "equal" and "Warning" in d["note"], "at >= 90 % a warning text is attached"


def test_compare_nic_is_informational_and_never_reds():
    r = fit.compare(lenovo_config(), requirements(nic_mbps=100000))
    d = _dims(r)["nic"]
    assert d["relation"] == "smaller" and d["bom"] == 25 and d["required"] == 100
    assert "Informational" in d["note"]
    assert r["verdict"] == "match"


def test_compare_fewer_nodes_fits_unless_something_is_under_requirement():
    small = requirements(cores_required=12, ram_required_gb=200, storage_required_tb=5.0)
    r = fit.compare(lenovo_config(nodes=2), small)
    d = _dims(r)
    assert d["nodes"]["relation"] == "fits" and d["nodes"]["bom"] == 2
    assert r["verdict"] == "match"

    r = fit.compare(lenovo_config(nodes=2), requirements(ram_required_gb=400))
    d = _dims(r)
    assert d["ram"]["relation"] == "smaller"
    assert d["nodes"]["relation"] == "smaller"
    assert r["verdict"] == "smaller"

    r = fit.compare(lenovo_config(nodes=4), small)
    assert _dims(r)["nodes"]["relation"] == "bigger" and r["verdict"] == "bigger"


def test_compare_unresolved_cpu_yields_unknown_not_a_crash():
    r = fit.compare(lenovo_config(cpu="Mystery Processor"), requirements())
    d = _dims(r)
    assert d["cores"]["relation"] == "unknown" and d["compute"]["relation"] == "unknown"
    assert d["ram"]["relation"] == "equal"
    assert any("Unresolved" in n or "unknown" in n for n in r["notes"])


def test_compare_config_type_sizing_is_hardware_vs_hardware():
    req = {
        "kind": "config", "node_count": 3, "hci_node_count": 3, "so_count": 0,
        "cluster_layout": [3], "sized_full_cluster": False, "compute_floor": {},
        "legacy_ghz": 0, "max_vm_ram_gb": 0, "max_vm_cores": 0, "nic_mbps": 0,
        "storage_category": "flash", "validated": True,
        "sized": {"cores": 45, "cores_n1": 30, "ram_gb": 750, "ram_n1": 500,
                  "usable_storage_tb": 21.12, "total_ghz": 168.0, "total_ghz_n1": 112.0,
                  "usable_ram_per_node_gb": 250, "threads_per_node": 32, "nic_ports": 4},
        "notes": ["direct build"], "sizing": {"id": 2, "name": "Build", "mode": "validated"},
    }
    r = fit.compare(lenovo_config(), req)
    d = _dims(r)
    assert all(d[k]["required"] is None for k in ("cores", "ram", "storage", "compute"))
    assert d["ram"]["relation"] == "equal" and r["verdict"] == "match"

    r = fit.compare(lenovo_config(ram_dimms_per_node=4), req)
    d = _dims(r)
    assert d["ram"]["relation"] == "smaller", "no demand known: below the build is 'smaller'"
    assert r["verdict"] == "smaller"
    assert any("Direct build" in n for n in r["notes"])


def test_compare_without_a_sizing_result_is_unknown():
    r = fit.compare(lenovo_config(), {"result_snapshot": None, "payload": {}})
    assert r["verdict"] == "unknown"
    assert all(d["relation"] == "unknown" for d in r["dimensions"])


def test_compare_storage_feasibility_lands_in_notes():
    r = fit.compare(lenovo_config(drives_per_node=2), requirements())
    assert any("Exactly 2 disks" in n for n in r["notes"])
    T.set_values({"max_cluster_disks": 10})
    r = fit.compare(lenovo_config(drives_per_node=4), requirements())
    assert any("exceed the 10-disk limit" in n for n in r["notes"])


# ─── 6. pick_config_for_sizing ───────────────────────────────────────────────

def test_pick_config_prefers_the_matching_node_count():
    bom = NormalizedBOM(vendor="Lenovo", configs=[
        BOMConfig(name="Services", server_model=None,
                  components=[_c("3 year premier support", 1, "other")]),
        lenovo_config(nodes=4, name="Option A"),
        lenovo_config(nodes=3, name="Option B"),
    ])
    assert fit.pick_config_for_sizing(bom, requirements()).name == "Option B"
    assert fit.pick_config_for_sizing(bom, requirements(node_count=5)).name == "Option A"
    assert fit.pick_config_for_sizing(bom, None).name == "Option A"
    assert fit.pick_config_for_sizing(NormalizedBOM(vendor="Dell"), None) is None


# ─── 4. sizing_requirements on a real Configuration row ──────────────────────

def _snapshot(nodes=6, cores=192, ram=1536, tb=40.0, model="HE500", extra=None):
    """tests/test_export_jobs.py's _sized() shape, plus what the engine adds."""
    rec = {
        "model": model, "node_count": nodes,
        "cluster_total": {"cores": cores, "ram_gb": ram, "usable_storage_tb": tb},
        "n_minus_1": {"cores": int(cores * 0.8), "ram_gb": int(ram * 0.8)},
    }
    rec.update(extra or {})
    return {"clusters": [{
        "name": "Prod",
        "summary": {"active_vms": 40, "total_host_ghz": 300.0, "max_vm_ram_gb": 48,
                    "max_vm_cores": 12, "nic_speed_mbps": 25000},
        "projection": {"years": 5, "base_ram_gb": 200.0, "base_storage_tb": 8.0,
                       "compute_floor": {"active": True, "balance": 0.5,
                                         "required_ghz": 150.0, "required_perf_index": None}},
        "recommendation": rec,
        "refs": {"mode": "validated"},
    }], "tunables": "t1"}


@pytest.fixture()
def sqlite_app():
    from flask import Flask
    from database import db
    import orm_models as om
    import auth_models  # noqa: F401  (configurations, users, tenants)
    import project_models  # noqa: F401  (projects — FK target of configurations)
    from tunables import DEFAULTS

    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    with app.app_context():
        db.create_all()
        for k, v in DEFAULTS.items():
            db.session.add(om.SizingSetting(key=k, value=float(v)))
        db.session.commit()
        yield app
        # No drop_all: each test builds a new app -> new in-memory engine, and
        # dropping trips SQLAlchemy over the users<->tenants FK cycle.
        db.session.remove()


def _config_row(payload, snapshot):
    from database import db
    from auth_models import Configuration, Tenant, User
    tenant = Tenant(domain="partnerco.example")
    db.session.add(tenant)
    db.session.flush()
    user = User(email="pm@partnerco.example", password_hash="x", tenant_id=tenant.id)
    db.session.add(user)
    db.session.flush()
    row = Configuration(code="ABC123", name="Site A", owner_id=user.id,
                        tenant_id=tenant.id, payload=payload, result_snapshot=snapshot)
    db.session.add(row)
    db.session.commit()
    return row


def test_sizing_requirements_reads_a_real_row_and_applies_day_one_floors(sqlite_app):
    extra = {
        "hci_node_count": 6, "cluster_layout": [6], "sized_full_cluster": False,
        "utilization": {
            "cpu": {"abs": {"total": 120, "unit": "cores"}},
            "ram": {"abs": {"total": 300.0, "unit": "GB"}},
            "storage": {"abs": {"total": 10.0, "unit": "TB"}},
        },
        "storage_config": {"drive_counts": {"NVMe": 6}},
        "usable_ram_per_node_gb": 246, "threads_per_node": 64, "nic_ports": 2,
        "compute_floor": {"coverage_pct": 130.0},
    }
    payload = {"mode": "import", "fields": {"max-day-one-ram": "50", "max-day-one-storage": "40"}}
    row = _config_row(payload, _snapshot(extra=extra))
    req = fit.sizing_requirements(row)
    assert req["kind"] == "recommendation"
    assert req["node_count"] == 6 and req["cluster_layout"] == [6]
    assert req["cores_required"] == 120
    # Day-one floors: 200 GB / 50 % = 400 > 300 projected; 8 TB / 40 % = 20 > 10.
    assert req["ram_required_gb"] == pytest.approx(400.0)
    assert req["storage_required_tb"] == pytest.approx(20.0)
    assert req["compute_floor"]["required_ghz"] == 150.0
    assert req["legacy_ghz"] == 300.0 and req["nic_mbps"] == 25000
    assert (req["max_vm_ram_gb"], req["max_vm_cores"]) == (48, 12)
    assert req["storage_category"] == "flash"
    assert req["sized"]["cores"] == 192 and req["sized"]["cores_n1"] == 153
    assert req["sized"]["ram_n1"] == 1228 and req["sized"]["usable_storage_tb"] == 40.0
    assert req["sized"]["compute_coverage_pct"] == 130.0
    assert req["sizing"] == {"id": row.id, "name": "Site A", "mode": "import"}
    assert req["notes"] == []

    # And compare() accepts the ORM row directly.
    r = fit.compare(lenovo_config(nodes=6), row)
    assert {d["key"] for d in r["dimensions"]} == set(fit.DIMENSION_KEYS)
    assert r["sizing"]["name"] == "Site A"


def test_sizing_requirements_tolerates_old_snapshots_with_notes(sqlite_app):
    # The bare _sized() shape: no utilization, no compute floor, no drive counts.
    row = _config_row({"mode": "import"}, _snapshot())
    req = fit.sizing_requirements(row)
    assert req["cores_required"] is None
    assert req["ram_required_gb"] == pytest.approx(400.0), "floor still applies from the projection"
    assert req["storage_required_tb"] == pytest.approx(16.0)
    assert any("missing" in n.lower() for n in req["notes"])
    assert any("utilization.cpu.abs.total" in n for n in req["notes"])
    r = fit.compare(lenovo_config(nodes=6), row)
    assert _dims(r)["cores"]["relation"] == "unknown"
    assert r["verdict"] in ("match", "bigger", "smaller")


def test_sizing_requirements_none_without_result(sqlite_app):
    row = _config_row({"mode": "appliance"}, None)
    assert fit.sizing_requirements(row) is None
    assert fit.sizing_requirements(None) is None
    assert fit.sizing_requirements({"result_snapshot": {"clusters": []}, "payload": {}}) is None


def test_sizing_requirements_config_type_direct_build(sqlite_app):
    snapshot = {"clusters": [{
        "name": "HC3350", "summary": None, "recommendation": None, "projection": None,
        "config": {
            "mode": "validated", "node_count": 3, "total_node_count": 4,
            "cluster_layout": [4], "storage_only": {"count": 1}, "nic_ports": 4,
            "per_node": {"cpu": "1 x Xeon Gold 6526Y", "cores": 15, "threads": 32, "ghz": 3.5,
                         "ram_gb": 250, "physical_cores": 16, "physical_ram_gb": 256,
                         "raw_storage_tb": 15.36,
                         "disks": [{"type": "NVMe", "size_tb": 3.84}] * 4},
            "cluster_total": {"cores": 45, "threads": 96, "total_ghz": 168.0, "ram_gb": 750,
                              "usable_storage_tb": 28.8},
            "n_minus_1": {"cores": 30, "ram_gb": 500, "total_ghz": 112.0, "usable_storage_tb": 28.8},
        },
        "refs": {"mode": "validated"},
    }], "tunables": "t1"}
    row = _config_row({"mode": "validated"}, snapshot)
    req = fit.sizing_requirements(row)
    assert req["kind"] == "config"
    assert (req["node_count"], req["hci_node_count"], req["so_count"]) == (4, 3, 1)
    assert "cores_required" not in req
    assert req["sized"]["cores_n1"] == 30 and req["sized"]["usable_ram_per_node_gb"] == 250
    assert req["storage_category"] == "flash"
    r = fit.compare(lenovo_config(nodes=4), row, hci_count=3)
    d = _dims(r)
    assert d["nodes"]["relation"] == "equal" and d["cores"]["relation"] == "equal"
    assert d["storage"]["relation"] == "equal" and r["verdict"] == "match"


# ─── 7. replication-target sizings (fit vs engine gates) ─────────────────────

def _rep_req(extra):
    """sizing_requirements through the dict path on the _snapshot shape."""
    return fit.sizing_requirements({
        "result_snapshot": _snapshot(extra=extra),
        "payload": {"mode": "import", "fields": {}},
        "id": 9, "name": "DR target",
    })


def test_requirements_hold_replication_reserve_on_top_of_the_day_one_floors():
    # Finding: "Fit requirements include the replication reserve, so the N-1
    # cores gate and the RAM day-one floor disagree with the engine" —
    # reserved mode. The engine gates max(own, floor) + reserve
    # (recommend.py:875), while the old fit computed max(own + reserve,
    # floor), under-requiring by up to the reserve when the floor governs.
    extra = {
        "hci_node_count": 6, "cluster_layout": [6], "sized_full_cluster": False,
        "n_minus_1": {"cores": 500, "ram_gb": 750},
        "utilization": {
            # cpu: own 200 + reserve 200; sized N-1 (500) covers 400 -> the
            # reserve stays in the N-1 requirement (reserved mode).
            "cpu": {"total": 100, "replication": 50, "abs": {"total": 400}},
            # ram: own 320 + reserve 320; floor = 200 / 50 % = 400 GB.
            "ram": {"total": 64, "replication": 32, "abs": {"total": 640.0}},
            # storage: own 14 + reserve 3.5; floor = 8 / 50 % = 16 TB.
            "storage": {"total": 50, "replication": 10, "abs": {"total": 17.5}},
        },
    }
    req = _rep_req(extra)
    assert req["cores_required"] == 400
    # Engine: max(320, 400) + 320 = 720 (the old figure was max(640, 400) = 640).
    assert req["ram_required_gb"] == pytest.approx(720.0)
    # Engine: max(14, 16) + 3.5 = 19.5 (the old figure was max(17.5, 16) = 17.5).
    assert req["storage_required_tb"] == pytest.approx(19.5)
    assert any("Replication reserve" in n for n in req["notes"])


def test_requirements_failover_reserve_is_excluded_from_the_n1_gate():
    # Finding, failover mode: the reserve counts at the full cluster only
    # (rep_*_n1 = 0, recommend.py:297), so a BOM copying the sized config
    # line for line used to come out 'smaller' on cores. The mode is not in
    # the snapshot; it is inferred from the sized N-1 pool being below the
    # reserved-mode requirement.
    extra = {
        "hci_node_count": 6, "cluster_layout": [6], "sized_full_cluster": False,
        # sized N-1 pools the engine ACCEPTED, yet below own+reserve:
        "n_minus_1": {"cores": 250, "ram_gb": 500},
        "utilization": {
            "cpu": {"total": 100, "replication": 50, "abs": {"total": 400}},
            "ram": {"total": 64, "replication": 32, "abs": {"total": 640.0}},
            "storage": {"abs": {"total": 10.0}},
        },
    }
    req = _rep_req(extra)
    # own 200 (the old figure was 400 > sized N-1 250 -> guaranteed red).
    assert req["cores_required"] == pytest.approx(200)
    assert req["cores_required"] <= 250, "a copied config must not red on cores"
    # RAM: max(own 320, floor 400) = 400, reserve at the full cluster only.
    assert req["ram_required_gb"] == pytest.approx(400.0)
    assert any("failover mode inferred" in n for n in req["notes"])


def test_requirements_without_replication_band_are_unchanged():
    extra = {
        "hci_node_count": 6, "cluster_layout": [6], "sized_full_cluster": False,
        "utilization": {
            "cpu": {"abs": {"total": 120}},
            "ram": {"abs": {"total": 300.0}},
            "storage": {"abs": {"total": 10.0}},
        },
    }
    req = _rep_req(extra)
    assert req["cores_required"] == 120
    assert req["ram_required_gb"] == pytest.approx(400.0)   # floor 200 / 50 %
    assert req["storage_required_tb"] == pytest.approx(16.0)  # floor 8 / 50 %
    assert not any("Replication" in n for n in req["notes"])
