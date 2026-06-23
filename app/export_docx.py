"""Author the proposal into the branded Scale Computing Word template and convert
it to PDF.

The template (resources/TMPL - Generic Document Template_2025.docx) carries the
branding — first-page watermark/title header, running header/footer, logo, and
named styles (Title, Heading 1/2, Normal). We strip its instructional body
content but keep the section properties (so headers/footers/watermark survive),
then author the proposal using those styles. The deck content is mirrored but
"leads with the recommendation".

PDF is produced by converting the authored .docx with headless LibreOffice
(soffice), which is installed in the container.
"""

import io
import os
import shutil
import subprocess
import tempfile

from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.enum.text import WD_ALIGN_PARAGRAPH

from export_pptx import _svg_to_png_bytes, _fmt_ram, _fmt_num

_TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "resources",
                         "TMPL - Generic Document Template_2025.docx")

DK2 = RGBColor(0x11, 0x38, 0x59)
MUTED = RGBColor(0x5A, 0x6B, 0x7D)


def _clear_body(doc):
    """Remove the template's instructional body content but keep the final
    sectPr (which references the branded headers/footers and page setup)."""
    body = doc.element.body
    for child in list(body):
        if child.tag != qn("w:sectPr"):
            body.remove(child)


def _content_width(doc):
    """Usable text width (inches) from the template's actual page + margins, so
    tables match the paragraph block exactly (template uses 0.75" margins → 7.0")."""
    sec = doc.sections[0]
    return (sec.page_width - sec.left_margin - sec.right_margin) / 914400


# OOXML requires tblPr children in this exact order. Word ENFORCES it (and
# silently drops misordered tblW/tblLayout → falls back to autofit → tables
# overflow the page); LibreOffice is lenient, which is why the PDF looked fine
# while the .docx in Word did not. We must insert in-order, not append.
_TBLPR_ORDER = [
    "w:tblStyle", "w:tblpPr", "w:tblOverlap", "w:bidiVisual",
    "w:tblStyleRowBandSize", "w:tblStyleColBandSize", "w:tblW", "w:jc",
    "w:tblCellSpacing", "w:tblInd", "w:tblBorders", "w:shd", "w:tblLayout",
    "w:tblCellMar", "w:tblLook", "w:tblCaption", "w:tblDescription",
]


def _set_tblpr_child(tblPr, tag):
    """Replace (or create) a tblPr child, inserted at its schema-correct position."""
    for el in tblPr.findall(qn(tag)):
        tblPr.remove(el)
    el = OxmlElement(tag)
    successors = {qn(t) for t in _TBLPR_ORDER[_TBLPR_ORDER.index(tag) + 1:]}
    for child in tblPr:
        if child.tag in successors:
            child.addprevious(el)
            return el
    tblPr.append(el)
    return el


def _shade(cell, hex_fill):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hex_fill)
    tcPr.append(shd)


def _set_table_borders(table, color="DDE2E6"):
    borders = _set_tblpr_child(table._tbl.tblPr, "w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:color"), color)
        borders.append(el)


def _cell_margins(table, top=70, bottom=70, left=130, right=130):
    """Breathing room inside every cell (twips)."""
    mar = _set_tblpr_child(table._tbl.tblPr, "w:tblCellMar")
    for side, val in (("top", top), ("bottom", bottom), ("left", left), ("right", right)):
        e = OxmlElement(f"w:{side}")
        e.set(qn("w:w"), str(val))
        e.set(qn("w:type"), "dxa")
        mar.append(e)


def _fixed_layout(table, widths):
    """Lock column widths so long content WRAPS instead of overflowing the page.
    Sets tblW (preferred width), tblLayout=fixed, tblInd=0, the authoritative
    tblGrid columns, and each cell's tcW — all inserted in schema order so Word
    honours them."""
    table.autofit = False
    table.allow_autofit = False
    tbl = table._tbl
    tblPr = tbl.tblPr

    tblW = _set_tblpr_child(tblPr, "w:tblW")
    tblW.set(qn("w:w"), str(int(sum(widths) * 1440)))
    tblW.set(qn("w:type"), "dxa")

    tblInd = _set_tblpr_child(tblPr, "w:tblInd")
    tblInd.set(qn("w:w"), "0")
    tblInd.set(qn("w:type"), "dxa")

    layout = _set_tblpr_child(tblPr, "w:tblLayout")
    layout.set(qn("w:type"), "fixed")

    grid_cols = tbl.tblGrid.findall(qn("w:gridCol"))
    for i, w in enumerate(widths):
        if i < len(grid_cols):
            grid_cols[i].set(qn("w:w"), str(int(w * 1440)))
    for row in table.rows:
        for i, w in enumerate(widths):
            row.cells[i].width = Inches(w)


def _style_cell(cell, text, bold=False, color=None, fill=None, align=None):
    cell.text = ""
    pr = cell.paragraphs[0]
    if align is not None:
        pr.alignment = align
    run = pr.add_run(str(text))
    run.bold = bold
    if color is not None:
        run.font.color.rgb = color
    if fill is not None:
        _shade(cell, fill)


def _spec_table(doc, rows, total_w, label_w=2.5):
    """Clean two-column spec table: shaded bold labels (left) + values (right),
    spanning the full content width so its right edge matches the paragraphs."""
    t = doc.add_table(rows=0, cols=2)
    _set_table_borders(t)
    _cell_margins(t)
    for k, v in rows:
        cells = t.add_row().cells
        _style_cell(cells[0], k, bold=True, color=DK2, fill="EEF2F7")
        _style_cell(cells[1], v)
    _fixed_layout(t, [label_w, total_w - label_w])
    return t


def _grid_table(doc, headers, rows, total_w, weights=None):
    """Header-row (dark) + data-rows table spanning the full content width.
    weights are relative column proportions (default equal)."""
    t = doc.add_table(rows=1, cols=len(headers))
    _set_table_borders(t)
    _cell_margins(t)
    for i, h in enumerate(headers):
        _style_cell(t.rows[0].cells[i], h, bold=True,
                    color=RGBColor(0xFF, 0xFF, 0xFF), fill="113859")
    for r in rows:
        cells = t.add_row().cells
        for i, val in enumerate(r):
            _style_cell(cells[i], val)
    if weights is None:
        weights = [1] * len(headers)
    scale = total_w / sum(weights)
    _fixed_layout(t, [w * scale for w in weights])
    return t


def _spacer(doc, pts=6):
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(pts)
    p.paragraph_format.space_before = Pt(0)
    return p


def _para(doc, text, italic=False, color=None, size=None):
    p = doc.add_paragraph()
    run = p.add_run(text)
    run.italic = italic
    if color is not None:
        run.font.color.rgb = color
    if size is not None:
        run.font.size = Pt(size)
    return p


def build_proposal_docx(summary, recommendation, projection):
    doc = Document(_TEMPLATE) if os.path.exists(_TEMPLATE) else Document()
    _clear_body(doc)
    # The template ships with tight 0.75" (~1.9 cm) side margins, which leaves the
    # body running to the page edge. Widen to a comfortable 1" (2.54 cm) so there's
    # real whitespace on the right and full-width tables sit inside the page.
    sec = doc.sections[0]
    sec.left_margin = Inches(1.0)
    sec.right_margin = Inches(1.0)
    r = recommendation
    s = summary
    p = projection
    t = r["totals"]
    n1 = r["n_minus_1"]
    iops = r.get("iops") or {}
    bullet_style = "List Bullet" if "List Bullet" in [st.name for st in doc.styles] else "Normal"
    cw = _content_width(doc)

    so = r.get("storage_only")
    hci_nodes = r.get("hci_node_count", r["node_count"])
    nodes_label = (f"{hci_nodes} HCI + {so['count']} storage-only"
                   if so else f"{r['node_count']} node{'' if r['node_count'] == 1 else 's'}")
    num_cl = r.get("num_clusters", 1)
    cl_label = (f"{num_cl} clusters ({' + '.join(map(str, r.get('cluster_layout', [])))})"
                if num_cl > 1 else "single cluster")

    # ── Title ────────────────────────────────────────────────────────────────
    doc.add_paragraph("Infrastructure Sizing Proposal", style="Title")
    subtitle = s.get("cluster_name") or s.get("current_platform") or ""
    if subtitle:
        doc.add_paragraph(subtitle, style="Subtitle")

    # ── Management overview (executive summary — leads the document) ──────────
    doc.add_heading("Management Overview", level=1)
    _para(doc,
          f"This proposal consolidates the current {s.get('current_platform', 'virtualization')} "
          f"environment ({s.get('host_count', 0)} hosts, {s.get('active_vms', 0)} active VMs, "
          f"{s.get('datastore_used_tb', 0)} TB in use) onto a Scale Computing HyperCore cluster. "
          f"We recommend a {nodes_label} {r['model']} cluster delivering "
          f"{t['usable_storage_tb']} TB usable capacity and {t['cores']} CPU cores, engineered for "
          f"N-1 resilience so a complete node can fail without service interruption. The "
          f"configuration absorbs {p['years']}-year growth at {p['growth_pct']}% annually while "
          f"maintaining a {r['vcpu_ratio']:.2f}:1 vCPU-to-core ratio.")
    _spacer(doc, 4)
    fits = (p["projected_storage_tb"] <= n1["usable_storage_tb"]
            and p["projected_vcpus"] <= n1["cores"] * r["vcpu_ratio"])
    proj_fits = "within the proposed N-1 capacity" if fits else "approaching the proposed capacity"
    _spec_table(doc, [
        ("Recommended platform", f"{r['model']} · {nodes_label} · {cl_label}"),
        ("Usable capacity", f"{t['usable_storage_tb']} TB (N-1 protected: "
                            f"{n1['usable_storage_tb']} TB)"),
        ("Compute", f"{t['cores']} cores @ {r['vcpu_ratio']:.2f}:1 vCPU:core"),
        (f"{p['years']}-year outlook", f"~{_fmt_num(p['projected_vcpus'])} vCPUs / "
                                       f"{p['projected_storage_tb']} TB projected — {proj_fits}"),
    ], total_w=cw, label_w=2.5)
    _spacer(doc)

    # ── Recommended configuration ────────────────────────────────────────────
    doc.add_heading("Recommended Configuration", level=1)
    _para(doc, f"{r['model']} — {nodes_label}, {cl_label}. "
               f"{r['form_factor']} ({r['chassis']}).")
    spec_rows = [
        ("Per-node CPU", r["cpu"]),
        ("Per-node cores / threads", f"{r['cores_per_node']} cores / {r['threads_per_node']} threads"),
        ("Per-node RAM", _fmt_ram(r["ram_per_node_gb"])),
        ("Per-node storage", r["storage_config"]["desc"]),
        ("Cluster cores", str(t["cores"])),
        ("Cluster RAM", _fmt_ram(t["ram_gb"])),
        ("Cluster usable storage", f"{t['usable_storage_tb']} TB"),
    ]
    if iops:
        spec_rows.append(("Cluster net IOPS", f"{iops['total']:,}"))
    spec_rows += [
        ("N-1 (resilient)", f"{n1['cores']} cores · {_fmt_ram(n1['ram_gb'])} · "
                            f"{n1['usable_storage_tb']} TB usable"),
        ("vCPU : core ratio", f"{r['vcpu_ratio']:.2f} : 1"),
    ]
    _spec_table(doc, spec_rows, total_w=cw)
    _spacer(doc)

    # Network diagram
    svg = r.get("network_svg")
    if svg:
        png = _svg_to_png_bytes(svg, out_width=2200)
        if png:
            doc.add_heading("Cluster Network", level=2)
            doc.add_picture(io.BytesIO(png), width=Inches(min(6.5, cw)))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            _spacer(doc)

    # ── Management & operations ──────────────────────────────────────────────
    doc.add_heading("Management & Operations", level=1)
    _para(doc, "Scale Computing HyperCore runs the hypervisor, storage, and "
               "orchestration as a single integrated fabric — managed from one "
               "interface with no external SAN, no separate hypervisor licensing, and "
               "no specialist virtualization administration required.")
    for note in [
        "Single-pane management — compute, storage, networking, and VMs are administered from "
        "the built-in HyperCore web interface; no separate storage console or vCenter-equivalent.",
        "SC Fleet Manager — cloud-based monitoring, alerting, and fleet-wide management across "
        "clusters and sites from one dashboard.",
        "Self-healing storage — RF2 mirroring rebuilds automatically on a disk or node failure, "
        "with no administrator intervention.",
        "Non-disruptive operations — rolling updates and node additions complete with VMs running; "
        "scale out by adding a node, no forklift upgrades.",
        "Built-in data protection — native VM snapshots and replication for backup and DR without "
        "third-party software.",
    ]:
        doc.add_paragraph(note, style=bullet_style)
    _spacer(doc)

    # ── Current environment & workload ───────────────────────────────────────
    doc.add_heading("Current Environment & Workload", level=1)
    _spec_table(doc, [
        ("Platform", s.get("current_platform", "")),
        ("Hosts", f"{s.get('host_count', 0)} · {_fmt_num(s.get('total_host_cores', 0))} cores · "
                  f"{_fmt_ram(s.get('total_host_ram_gb', 0))}"),
        ("VMs", f"{s.get('active_vms', 0)} active of {s.get('total_vms', 0)} total"),
        ("Workload", f"{_fmt_num(s.get('total_vcpus', 0))} vCPUs · "
                     f"{_fmt_ram(s.get('total_vm_provisioned_memory_gb', 0))} RAM · "
                     f"{s.get('datastore_used_tb', 0)} TB used"),
        ("Measured ratio", f"{s.get('vcpu_per_core_ratio', 0):.2f} : 1"),
    ], total_w=cw)
    _spacer(doc)

    # ── Capacity planning ────────────────────────────────────────────────────
    doc.add_heading(f"Capacity Planning — {p['years']}-Year Projection", level=1)
    _para(doc, f"{p['growth_pct']}% YoY growth, {p['snapshot_pct']}% snapshot overhead "
               f"(growth factor {p.get('growth_factor', 1)}×).", italic=True, color=MUTED)
    _grid_table(doc,
                ["Resource", "Current", f"Year {p['years']}", "Proposed (N-1)"],
                [["vCPUs", _fmt_num(p["base_vcpus"]), _fmt_num(p["projected_vcpus"]),
                  f"{n1['cores']} cores @ {r['vcpu_ratio']:.1f}:1"],
                 ["RAM", _fmt_ram(p["base_ram_gb"]), _fmt_ram(p["projected_ram_gb"]),
                  _fmt_ram(n1["ram_gb"])],
                 ["Storage", f"{p['base_storage_tb']} TB", f"{p['projected_storage_tb']} TB",
                  f"{n1['usable_storage_tb']} TB usable"]],
                total_w=cw, weights=[1.6, 1.8, 1.8, 1.8])
    _spacer(doc)

    # ── Assumptions ──────────────────────────────────────────────────────────
    doc.add_heading("Assumptions & Notes", level=1)
    for note in [
        f"Sized for N-1 resilience ({cl_label}); usable capacity reflects RF2 mirroring.",
        f"vCPU:core ratio {r['vcpu_ratio']:.2f}:1; OS overhead and growth/snapshot reserve included.",
        "No LAG — networking is active/passive failover across two switches.",
    ]:
        doc.add_paragraph(note, style=bullet_style)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


def _soffice_bin():
    return shutil.which("soffice") or shutil.which("libreoffice")


def convert_docx_to_pdf(docx_bytes):
    """Convert .docx bytes → PDF bytes via headless LibreOffice. Returns None if
    LibreOffice isn't available (caller can fall back to serving the .docx)."""
    soffice = _soffice_bin()
    if not soffice:
        return None
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "proposal.docx")
        with open(src, "wb") as f:
            f.write(docx_bytes)
        try:
            subprocess.run([soffice, "--headless", "--convert-to", "pdf",
                            "--outdir", d, src],
                           check=True, timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env={**os.environ, "HOME": d})
        except Exception:
            return None
        out = os.path.join(d, "proposal.pdf")
        return open(out, "rb").read() if os.path.exists(out) else None
