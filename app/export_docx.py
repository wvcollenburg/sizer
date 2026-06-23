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
import re
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


def _set_table_borders(table):
    tblPr = table._tbl.tblPr
    borders = OxmlElement("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = OxmlElement(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:color"), "DDDDDD")
        borders.append(el)
    tblPr.append(borders)


def _kv_table(doc, rows, widths=(2.4, 4.0)):
    """A two-column key/value table (key bold)."""
    t = doc.add_table(rows=0, cols=2)
    _set_table_borders(t)
    for k, v in rows:
        c = t.add_row().cells
        c[0].text = ""
        run = c[0].paragraphs[0].add_run(str(k))
        run.bold = True
        run.font.color.rgb = DK2
        c[1].text = str(v)
    for row in t.rows:
        for i, w in enumerate(widths):
            row.cells[i].width = Inches(w)
    return t


def _grid_table(doc, headers, rows):
    """A header-row + data-rows table."""
    t = doc.add_table(rows=1, cols=len(headers))
    _set_table_borders(t)
    for i, h in enumerate(headers):
        cell = t.rows[0].cells[i]
        cell.text = ""
        run = cell.paragraphs[0].add_run(str(h))
        run.bold = True
        run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        _shade(cell, "113859")
    for r in rows:
        cells = t.add_row().cells
        for i, val in enumerate(r):
            cells[i].text = str(val)
    return t


def _shade(cell, hex_fill):
    tcPr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hex_fill)
    tcPr.append(shd)


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
    r = recommendation
    s = summary
    p = projection

    # ── Title ────────────────────────────────────────────────────────────────
    doc.add_paragraph("Infrastructure Sizing Proposal", style="Title")
    subtitle = s.get("cluster_name") or s.get("current_platform") or ""
    if subtitle:
        doc.add_paragraph(subtitle, style="Subtitle")

    # ── Lead with the recommendation ─────────────────────────────────────────
    doc.add_heading("Recommended Configuration", level=1)
    so = r.get("storage_only")
    nodes_label = (f"{r.get('hci_node_count', r['node_count'])} HCI + {so['count']} storage-only"
                   if so else f"{r['node_count']} node{'' if r['node_count'] == 1 else 's'}")
    num_cl = r.get("num_clusters", 1)
    cl_label = (f"{num_cl} clusters ({' + '.join(map(str, r.get('cluster_layout', [])))})"
                if num_cl > 1 else "1 cluster")
    _para(doc, f"{r['model']} — {nodes_label}, {cl_label}. "
               f"{r['form_factor']} ({r['chassis']}).")

    t = r["totals"]
    iops = r.get("iops") or {}
    _kv_table(doc, [
        ("Per node", f"{r['cpu']} · {r['cores_per_node']}C/{r['threads_per_node']}T · "
                     f"{_fmt_ram(r['ram_per_node_gb'])} RAM · {r['storage_config']['desc']}"),
        ("Cluster total", f"{t['cores']} cores · {_fmt_ram(t['ram_gb'])} · "
                          f"{t['usable_storage_tb']} TB usable"
                          + (f" · {iops['total']:,} net IOPS" if iops else "")),
        ("N-1 (resilient)", f"{r['n_minus_1']['cores']} cores · "
                            f"{_fmt_ram(r['n_minus_1']['ram_gb'])} · "
                            f"{r['n_minus_1']['usable_storage_tb']} TB usable"),
        ("vCPU : core ratio", f"{r['vcpu_ratio']:.2f} : 1"),
    ])

    # Network diagram
    svg = r.get("network_svg")
    if svg:
        png = _svg_to_png_bytes(svg, out_width=2200)
        if png:
            doc.add_heading("Cluster Network", level=2)
            doc.add_picture(io.BytesIO(png), width=Inches(6.5))

    # ── Current environment & workload ───────────────────────────────────────
    doc.add_heading("Current Environment & Workload", level=1)
    _kv_table(doc, [
        ("Platform", s.get("current_platform", "")),
        ("Hosts", f"{s.get('host_count', 0)} · {_fmt_num(s.get('total_host_cores', 0))} cores · "
                  f"{_fmt_ram(s.get('total_host_ram_gb', 0))}"),
        ("VMs", f"{s.get('active_vms', 0)} active of {s.get('total_vms', 0)} total"),
        ("Workload", f"{_fmt_num(s.get('total_vcpus', 0))} vCPUs · "
                     f"{_fmt_ram(s.get('total_vm_provisioned_memory_gb', 0))} RAM · "
                     f"{s.get('datastore_used_tb', 0)} TB used"),
        ("Measured ratio", f"{s.get('vcpu_per_core_ratio', 0):.2f} : 1"),
    ])

    # ── Capacity planning ────────────────────────────────────────────────────
    doc.add_heading(f"Capacity Planning — {p['years']}-Year Projection", level=1)
    _para(doc, f"{p['growth_pct']}% YoY growth, {p['snapshot_pct']}% snapshot overhead "
               f"(growth factor {p.get('growth_factor', 1)}×).", italic=True, color=MUTED)
    n1 = r["n_minus_1"]
    _grid_table(doc,
                ["Resource", "Current", f"Year {p['years']}", "Proposed (N-1)"],
                [["vCPUs", _fmt_num(p["base_vcpus"]), _fmt_num(p["projected_vcpus"]),
                  f"{n1['cores']} cores @ {r['vcpu_ratio']:.1f}:1"],
                 ["RAM", _fmt_ram(p["base_ram_gb"]), _fmt_ram(p["projected_ram_gb"]),
                  _fmt_ram(n1["ram_gb"])],
                 ["Storage", f"{p['base_storage_tb']} TB", f"{p['projected_storage_tb']} TB",
                  f"{n1['usable_storage_tb']} TB usable"]])

    # ── Assumptions ──────────────────────────────────────────────────────────
    doc.add_heading("Assumptions & Notes", level=1)
    for note in [
        f"Sized for N-1 resilience ({cl_label}); usable capacity reflects RF2 mirroring.",
        f"vCPU:core ratio {r['vcpu_ratio']:.2f}:1; OS overhead and growth/snapshot reserve included.",
        "No LAG — networking is active/passive failover across two switches.",
    ]:
        doc.add_paragraph(note, style="Normal").style = "List Bullet" \
            if "List Bullet" in [st.name for st in doc.styles] else "Normal"

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
