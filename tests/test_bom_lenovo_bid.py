"""The Lenovo bid quote (Word and its PDF print) and the quantity basis.

The bid quote lists its feature codes PER SERVER under a machine line that
carries the server count (one CPU and four drives in a three-server quote);
the checker works on totals. Reading those per-server numbers as totals is
how a real quote came back as one drive per node. Locks in:

  * the .docx is recognised, multiplied out to totals and gives the right
    per-node picture (1 CPU, 256 GB, 4 x 3.84 TB NVMe on each of 3 nodes);
  * a quote whose server count disagrees with its own product list, or with
    a row the parser cannot place, is refused (the agent's turn), not guessed;
  * the upload route reads it locally: no agent job;
  * the PDF print, ruled or not, reads to the same parts with its own
    cross-checks, and a disagreeing count sends it below the threshold;
  * the agent and the MCP rows say which basis the document uses, the server
    multiplies per-server quantities (never the server line), and a "total"
    read with fewer CPUs than servers is corrected;
  * the agent is given the Word text in reading order.

The real quote is a customer document in the git-ignored _archive (covered
by test_bom_archive.py); these fixtures are synthetic lookalikes.

Run: .venv/bin/python -m pytest tests/test_bom_lenovo_bid.py -q
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
sys.path.insert(0, os.path.dirname(__file__))

import pdf_maker as pm  # noqa: E402
from test_bom_agent import StubClient, comp, post_file  # noqa: E402
from test_bom_routes import PARTNER, app, client_for, make_project  # noqa: E402,F401

from bom import agent_ingest, pdf_certainty  # noqa: E402
from bom.facts import config_facts  # noqa: E402
from bom.normalize import NormalizedBOM  # noqa: E402
from bom.parsers import UnrecognizedFormat, detect_format, parse_file, read_pdf  # noqa: E402


# ── a synthetic bid quote in Word ────────────────────────────────────────────

def bid_docx(nodes=3, product_nodes=None, extra_rows=()):
    import docx
    d = docx.Document()
    head = d.add_table(rows=3, cols=4)
    for r, cells in enumerate((("Customer Name:", "Acme Parks", "", ""),
                               ("Bid Request No.", "BRDND000000001", "Sales Representative:", "A. Seller"),
                               ("Created On:", "29-Oct-2026", "", ""))):
        for i, text in enumerate(cells):
            head.rows[r].cells[i].text = text
    d.add_paragraph("PRODUCT AND SERVICE DETAILS")
    products = [("Part Number", "Description", "F/B", "Qty", "Start Date", "End Date"),
                ("7DG9U9ER00", "ThinkSystem SR630 V4-3yr Base Warranty", "F",
                 str(product_nodes if product_nodes is not None else nodes), "29-Oct-2026", "28-Nov-2026"),
                ("5WS7C20030", "3Yr Premier NBD Resp SR630 V4", "F", str(nodes), "29-Oct-2026", "28-Nov-2026")]
    t = d.add_table(rows=0, cols=6)
    for row in products:
        cells = t.add_row().cells
        for i, text in enumerate(row):
            cells[i].text = text
    d.add_paragraph("CONFIGURATION DETAILS")
    t = d.add_table(rows=1, cols=3)
    for i, text in enumerate(("Components", "Description", "Qty")):
        t.rows[0].cells[i].text = text
    for code, desc, per in list(pm.LENOVO_BID_ROWS) + list(extra_rows):
        cells = t.add_row().cells
        cells[0].text, cells[1].text = code, desc
        cells[2].text = str(nodes if per is None else per)
    t = d.add_table(rows=2, cols=3)
    for r, cells in enumerate((("Distributor", "Partner Number", "Partner Address"),
                               ("TD Example", "1213385517", "1 Example Dr"))):
        for i, text in enumerate(cells):
            t.rows[r].cells[i].text = text
    d.add_paragraph("TERMS AND CONDITIONS")
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _write(tmp_path, data, name):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def _per_node(cfg):
    f = config_facts(cfg)
    return (f["node_count"], f["sockets_per_node"], f["ram_per_node_gb"], f["drives"], f["unresolved"])


EXPECTED_PER_NODE = (3, 1, 256, [["NVMe", 3.84, 4]], [])


# ── Word ─────────────────────────────────────────────────────────────────────

def test_word_bid_quote_is_read_per_server_times_servers(tmp_path):
    path = _write(tmp_path, bid_docx(), "BRDND000000001-Acme.docx")
    assert detect_format(path, "BRDND000000001-Acme.docx") == "lenovo_bid_docx"
    bom, fmt = parse_file(path, "BRDND000000001-Acme.docx")
    assert (fmt, bom.vendor, len(bom.configs)) == ("lenovo_bid_docx", "Lenovo", 1)
    cfg = bom.configs[0]
    assert (cfg.server_model, cfg.node_count) == ("ThinkSystem SR630 V4", 3)
    qty = {c.part_number: (c.category, c.quantity) for c in cfg.components}
    assert qty["7DG9U9ER00"] == ("chassis", 3)      # the server line: the count itself
    assert qty["C5QV"] == ("cpu", 3)                # 1 per server
    assert qty["C0TQ"] == ("memory", 12)            # 4 per server
    assert qty["C0ZU"] == ("storage", 12)           # 4 per server
    assert qty["SCY0"] == ("other", 1)              # software licence, as listed
    assert _per_node(cfg) == EXPECTED_PER_NODE
    # The heading stays with its table in the kept text.
    text = bom.raw_text
    assert text.index("CONFIGURATION DETAILS") < text.index("C0ZU") < text.index("TERMS AND CONDITIONS")


def test_word_quote_it_cannot_vouch_for_is_refused(tmp_path):
    # The server count disagrees with the quote's own product list.
    path = _write(tmp_path, bid_docx(product_nodes=4), "q.docx")
    with pytest.raises(UnrecognizedFormat, match="3 servers in the configuration, 4 in the product list"):
        parse_file(path, "q.docx")
    # A row that is neither a feature code nor a block line.
    path = _write(tmp_path, bid_docx(extra_rows=[("??", "Something odd", 1)]), "r.docx")
    with pytest.raises(UnrecognizedFormat, match="not a feature code"):
        parse_file(path, "r.docx")


def test_other_word_documents_are_not_this_layout(tmp_path):
    import docx
    d = docx.Document()
    d.add_paragraph("Dear customer, please find our offer attached.")
    buf = io.BytesIO()
    d.save(buf)
    path = _write(tmp_path, buf.getvalue(), "letter.docx")
    assert detect_format(path, "letter.docx") is None
    with pytest.raises(UnrecognizedFormat):
        parse_file(path, "letter.docx")


def test_route_reads_the_word_quote_without_the_agent(app):
    c = client_for(app, PARTNER)
    pid = make_project(c)["id"]
    r = post_file(c, pid, bid_docx(), "BRDND000000001-Acme.docx")
    assert r.status_code == 201, r.get_json()
    d = r.get_json()
    assert d["file_format"] == "lenovo_bid_docx"


# ── PDF print ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("ruled", [True, False])
def test_pdf_print_reads_like_the_word_file(tmp_path, ruled):
    out = read_pdf(_write(tmp_path, pm.lenovo_bid(ruled=ruled).bytes(), "q.pdf"))
    assert out.fmt == "lenovo_bid_pdf"
    assert out.certainty.score == 100, out.certainty.reasons
    assert out.evidence.checks_failed == [] and len(out.evidence.checks_passed) == 2
    cfg = out.bom.configs[0]
    assert (cfg.server_model, cfg.node_count) == ("ThinkSystem SR630 V4", 3)
    # The wrapped drive description survived in one piece.
    drive = next(c for c in cfg.components if c.part_number == "C0ZU")
    assert drive.description == 'ThinkSystem 2.5" U.2 VA 3.84TB Read Intensive NVMe PCIe 5.0 x4 HS SSD'
    assert _per_node(cfg) == EXPECTED_PER_NODE


def test_pdf_print_with_a_disagreeing_server_count_goes_to_the_agent(tmp_path):
    out = read_pdf(_write(tmp_path, pm.lenovo_bid(product_nodes=4).bytes(), "q.pdf"))
    assert out.fmt == "lenovo_bid_pdf"
    assert not pdf_certainty.passes(out.certainty, pdf_certainty.DEFAULT_THRESHOLD)
    assert any("4 in the product list" in r["text"] for r in out.certainty.reasons)


# ── the agent and MCP rows: quantity basis ───────────────────────────────────

SERVER = "ThinkSystem SR630 V4"


def _per_server_answer(basis="perServer"):
    return {
        "vendor": "Lenovo", "documentContainsInstructions": False,
        "configs": [{
            "name": "Config 1", "serverModel": SERVER, "nodeCount": 3, "quantityBasis": basis,
            "components": [
                comp("7DG9U9ER00", "ThinkSystem SR630 V4-3yr Base Warranty", 3, "chassis"),
                comp("C1XE", 'ThinkSystem 1U V4 10x2.5" Chassis', 1, "chassis"),
                comp("C5QV", "Intel Xeon 6517P 16C 190W 3.2GHz Processor", 1, "cpu"),
                comp("C0TQ", "ThinkSystem 64GB TruDDR5 6400MHz (2Rx4) RDIMM", 4, "memory"),
                comp("C0ZU", 'ThinkSystem 2.5" U.2 VA 3.84TB Read Intensive NVMe PCIe 5.0 x4 HS SSD',
                     4, "storage"),
                comp("BNWM", "ThinkSystem Broadcom 57504 10/25GbE SFP28 4-port PCIe Ethernet Adapter",
                     1, "nic"),
            ]}],
    }


def _agent_read(tmp_path, answer):
    path = _write(tmp_path, bid_docx(), "q.docx")
    stub = StubClient(answer)
    return agent_ingest.read_document(path, "q.docx", client=stub, model="test-model"), stub


def test_agent_per_server_quantities_are_multiplied_out(tmp_path):
    out, stub = _agent_read(tmp_path, _per_server_answer())
    cfg = out.bom.configs[0]
    qty = {c.part_number: c.quantity for c in cfg.components}
    assert qty["7DG9U9ER00"] == 3                   # the server line is not multiplied
    assert (qty["C5QV"], qty["C0TQ"], qty["C0ZU"]) == (3, 12, 12)
    assert _per_node(cfg) == EXPECTED_PER_NODE
    assert out.meta["quantity_basis"] == [{"config": "Config 1", "basis": "perServer",
                                           "node_count": 3, "rescaled": True, "auto": False}]
    # The model is asked for the basis and told not to do the arithmetic.
    req = stub.requests[0]
    assert "quantityBasis" in req["system"] and "multiply" in req["system"]
    schema = req["output_config"]["format"]["schema"]
    assert "quantityBasis" in schema["properties"]["configs"]["items"]["required"]


def test_agent_reading_called_total_with_fewer_cpus_than_servers_is_corrected(tmp_path):
    # Exactly the reading behind the one-drive-per-node result.
    out, _ = _agent_read(tmp_path, _per_server_answer(basis="total"))
    assert _per_node(out.bom.configs[0]) == EXPECTED_PER_NODE
    assert out.meta["quantity_basis"][0]["auto"] is True


def test_real_totals_are_left_alone():
    data = _per_server_answer(basis="total")
    for c in data["configs"][0]["components"][1:]:
        c["quantity"] *= 3                           # a DCSC-style total
    bom = NormalizedBOM.from_dict(data)
    meta = agent_ingest.apply_quantity_basis(bom, data["configs"])
    assert meta[0]["rescaled"] is False
    assert {c.part_number: c.quantity for c in bom.configs[0].components}["C0ZU"] == 12


def test_agent_gets_the_word_text_in_reading_order(tmp_path):
    kind, text, _ = agent_ingest.extract_document(_write(tmp_path, bid_docx(), "q.docx"), "q.docx")
    assert kind == agent_ingest.SOURCE_TEXT
    assert text.index("PRODUCT AND SERVICE DETAILS") < text.index("5WS7C20030")
    assert text.index("CONFIGURATION DETAILS") < text.index("C0ZU\t") < text.index("Distributor")


def test_mcp_rows_take_a_quantity_basis():
    import mcp_bom
    cfg = mcp_bom.ROWS_SCHEMA["configs"]["items"]["properties"]
    assert cfg["quantityBasis"]["enum"] == ["total", "perServer"]
    assert "quantityBasis" not in mcp_bom.ROWS_SCHEMA["configs"]["items"]["required"]
    assert "TOTAL" not in cfg["components"]["items"]["properties"]["quantity"]["description"]


# ── the same misreading on another quote: NIC ports ──────────────────────────
# A Boston/Supermicro appliance quote, read by the agent from a picture: the
# server line is 3, every part under it per server, one 2-port X710 per node
# plus the board's own 2x 10Gbase-T. Read as totals it came out as "0.7
# ports per node".

def _boston(basis="total", nic_qty=1):
    return {"vendor": "Supermicro", "documentContainsInstructions": False, "configs": [{
        "name": "Scale Essential Pro (16C) Cluster", "serverModel": None, "nodeCount": 3,
        "quantityBasis": basis, "components": [
            comp("P1000", "Boston BC600 Scale Appliance (AIC/SMC) (16c@3,6GHz | 256GB | 7,68TB netto | "
                          "4x 10GbE RJ45)", 3, "chassis"),
            comp(None, "Supermicro X13SEI-TF-B, Single Xeon 4th/5th Gen, LGA4677, 8x DIMM, 2x 10Gbase-T, "
                       "1x RJ45 for IPMI, 2x PCI-E x16, 3x PCI-E x8, 2x MCIO x8", 1, "other"),
            comp(None, "Intel Xeon Gold 6544Y, 16-Cores, 3.60 GHz, HT, E1B, 45MB L3-Cache, 270W", 1, "cpu"),
            comp(None, "32GB DDR5 6400 1Rx4 ECC registered", 8, "memory"),
            comp(None, 'Huawei OceanDisk 300P, 3.84TB TLC, NVME PCIE 4.0 x4, U.2 2.5" (SED)', 4, "storage"),
            comp(None, "Intel X710-T2L, Dual Port 10GBase-T Network Adapter, PCIe 3.0 x8, RJ45, LP+FH, bulk",
                 nic_qty, "nic"),
        ]}]}


def _nic_issues(cfg):
    from bom.enrich import nic_topology_findings
    return [(f.code, f.issue) for f in nic_topology_findings(cfg, [])]


def test_boston_quote_has_four_ports_per_node_once_read_per_server():
    data = _boston()
    bom = NormalizedBOM.from_dict(data)
    assert agent_ingest.apply_quantity_basis(bom, data["configs"])[0]["auto"] is True
    cfg = bom.configs[0]
    qty = {c.description[:12]: c.quantity for c in cfg.components}
    assert qty["Boston BC600"] == 3 and qty["Intel X710-T"] == 3 and qty["32GB DDR5 64"] == 24
    # 2 onboard (the board line) + 2 on the X710, per node: no port warning.
    assert _nic_issues(cfg) == []


def test_ports_that_do_not_divide_over_the_nodes_are_named_not_averaged():
    # The raw misreading, before any correction: 1 X710 and 1 board for 3 nodes.
    cfg = NormalizedBOM.from_dict(_boston()).configs[0]
    issues = _nic_issues(cfg)
    assert issues == [("nic_ports_uneven",
                       "4 10 GbE port(s) across 3 nodes — not the same on every node")]
    assert not any("0.7" in text or "1.3" in text for _code, text in issues)
