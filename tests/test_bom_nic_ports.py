"""Owner rule (2026-09-30): all active ports on a node run at the same speed;
1-3 ports is backplane over VLAN, 4 or more is dedicated failover for LAN
and backplane (bom/enrich.nic_topology_findings).

The case that raised it: the Broadcom 5720 1 GbE LOM, now accepted as a
pre-publication part, next to a 2-port 10/25 GbE adapter. The LOM must be
disabled (a warning, not "limited to backplane over VLAN"), which leaves 2
ports per node: backplane over VLAN, with a suggestion to go to 4 ports.

Run: .venv/bin/python -m pytest tests/test_bom_nic_ports.py -q
"""
import pytest

from test_bom_routes import (PARTNER, app, client_for, make_project,  # noqa: F401
                             template_upload)
from bom.enrich import (CODE_NIC_PORTS_VLAN, CODE_NIC_SPEED_MISMATCH,
                        nic_topology_findings)
from bom.normalize import BOMComponent, BOMConfig, Finding, NormalizedBOM

LOM = "Broadcom 5720 Dual Port 1GbE On-Board LOM"
E810_2 = "ThinkSystem Intel E810-DA2 10/25GbE SFP28 2-Port OCP Ethernet Adapter"
E810_4 = "ThinkSystem Intel E810-DA4 10/25GbE SFP28 4-Port OCP Ethernet Adapter"


def comp(desc, qty, cat="nic", part=None):
    return BOMComponent(part_number=part, description=desc, quantity=qty, category=cat)


def codes(components, nodes=3, findings=()):
    cfg = BOMConfig(name="c", server_model="ThinkSystem SR630 V3", node_count=nodes,
                    components=[comp("ThinkSystem SR630 V3", nodes, "chassis")] + components)
    return [f.code for f in nic_topology_findings(cfg, list(findings))]


def test_lom_next_to_a_two_port_adapter():
    assert codes([comp(LOM, 3), comp(E810_2, 3)]) == [CODE_NIC_SPEED_MISMATCH, CODE_NIC_PORTS_VLAN]


def test_lom_next_to_a_four_port_adapter_only_needs_disabling():
    assert codes([comp(LOM, 3), comp(E810_4, 3)]) == [CODE_NIC_SPEED_MISMATCH]


@pytest.mark.parametrize("cards,expected", [
    ([E810_2], [CODE_NIC_PORTS_VLAN]),               # 2 ports: VLAN
    ([E810_2, E810_2], []),                          # 2 + 2 = 4: dedicated failover
    ([E810_4], []),
])
def test_port_count_decides_vlan_or_dedicated(cards, expected):
    assert codes([comp(c, 3) for c in cards]) == expected


def test_shared_speed_is_not_a_mismatch():
    # A 10 GbE X710 and a 10/25 GbE E810 can both run at 10 GbE.
    assert codes([comp("Intel X710 Dual Port 10GbE SFP+", 3), comp(E810_2, 3)]) == []


def test_onboard_ports_named_on_the_motherboard_line():
    board = comp("PowerEdge R360 Motherboard with Broadcom 5720, 2 ports, 1 Gbit, integrated LOM", 3, "chassis")
    assert codes([board, comp("Intel X710-T4L Quad Port 10GbE BASE-T Adapter", 3)]) == [CODE_NIC_SPEED_MISMATCH]


def test_absence_lines_are_not_ports():
    assert codes([comp("LOM Blank", 1, "other"), comp(E810_4, 1)], nodes=1) == []


def test_ported_lom_note_is_replaced_when_a_faster_adapter_exists():
    old = Finding(severity="info", component=LOM, issue="Onboard/mezzanine NIC (LOM/rNDC) — limited "
                  "to Backplane over VLAN", remediation="", code="nic_lom")
    fam = Finding(severity="info", component="NIC", issue="Multiple discrete NIC types detected — select one",
                  remediation="", code="nic_multiple_families")
    got = codes([comp(LOM, 3), comp(E810_4, 3)], findings=[old, fam])
    assert "nic_lom" not in got and "nic_multiple_families" not in got


def test_unknown_node_count_still_flags_when_even_one_node_is_short():
    assert codes([comp(E810_2, 1)], nodes=None) == [CODE_NIC_PORTS_VLAN]


def test_through_the_check_with_four_port_suggestions(app):
    """End to end: the check keeps its verdict (warnings only) and offers the
    platform's validated 4-port adapters of the same speed."""
    c = client_for(app, PARTNER)
    p = make_project(c)
    bom = NormalizedBOM(vendor="Lenovo", configs=[BOMConfig(
        name="Acme", server_model="ThinkSystem SR630 V3", node_count=3, components=[
            comp('ThinkSystem V3 1U 10x2.5" Chassis', 3, "chassis", "BLK4"),
            comp("Intel Xeon Gold 6526Y 16C 195W 2.8GHz Processor", 3, "cpu", "BYVX"),
            comp("ThinkSystem 32GB TruDDR5 5600MHz (2Rx8) RDIMM", 24, "memory", "BWJC"),
            comp('ThinkSystem 2.5" U.2 VA 3.84TB Read Intensive NVMe PCIe 4.0 x4 HS SSD', 12, "storage", "C18M"),
            comp(LOM, 3, "nic", "B5XX"),
            comp(E810_2, 3, "nic", "4XC7A08294"),
        ])])
    r = c.post("/api/projects/%d/bom-checks" % p["id"], data={"file": template_upload(bom)},
               content_type="multipart/form-data")
    assert r.status_code == 201, r.get_data(as_text=True)
    result = r.get_json()["result"]
    findings = result["technical"]["config_results"][0]["findings"]
    by_code = {f["code"]: f for f in findings}
    assert by_code[CODE_NIC_SPEED_MISMATCH]["severity"] == "warning"
    assert by_code[CODE_NIC_SPEED_MISMATCH]["component"] == LOM
    assert "Only 2 25 GbE port(s) per node" in by_code[CODE_NIC_PORTS_VLAN]["issue"]
    assert "nic_lom" not in by_code
    swap = [s for s in result["suggestions"] if s["code"] == CODE_NIC_PORTS_VLAN]
    assert swap and all((cand["ports"] or 0) >= 4 for cand in swap[0]["candidates"])
    assert swap[0]["candidates"][0]["part_number"] == "4XC7A80269"      # the E810-DA4
