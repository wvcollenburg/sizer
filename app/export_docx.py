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
from export_gauges import render_util_bars, util_rows, compute_floor_sentence

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


def _set_header_text(section, text):
    """Replace the template's 'Header' placeholder in both the first-page and
    running headers, preserving its (grey Martel Sans) styling. The placeholder
    lives inside a text box (vertical-text shape), so it isn't reachable via
    paragraphs/runs — walk every w:t in the header part instead (catches both the
    shape and its mc:Fallback copy)."""
    for hdr in (section.first_page_header, section.header):
        if hdr is None:
            continue
        for t in hdr._element.iter(qn("w:t")):
            if (t.text or "").strip() == "Header":
                t.text = text


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


def _keep_table_together(table):
    """Keep a table from being split across pages: mark every row 'cannot split'
    (no row breaks mid-cell) and 'keep with next' on all rows but the last, so
    Word holds the whole table on one page and pushes it to the next page when it
    won't fit. A table taller than a single page still breaks — Word overrides
    keep-with-next once the content exceeds the page, which is the desired
    behaviour."""
    rows = table.rows
    last = len(rows) - 1
    for ri, row in enumerate(rows):
        trPr = row._tr.get_or_add_trPr()
        if not trPr.findall(qn("w:cantSplit")):
            trPr.append(OxmlElement("w:cantSplit"))
        if ri < last:
            for cell in row.cells:
                for p in cell.paragraphs:
                    p.paragraph_format.keep_with_next = True


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
    _keep_table_together(t)
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
    _keep_table_together(t)
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


def build_proposal_docx(summary, recommendation, projection, source_perf=None):
    doc = Document(_TEMPLATE) if os.path.exists(_TEMPLATE) else Document()
    _clear_body(doc)
    # The template ships with tight 0.75" (~1.9 cm) side margins, which leaves the
    # body running to the page edge. Widen to a comfortable 1" (2.54 cm) so there's
    # real whitespace on the right and full-width tables sit inside the page.
    sec = doc.sections[0]
    sec.left_margin = Inches(1.0)
    sec.right_margin = Inches(1.0)
    # The template's paragraph styles inherit a left indent, so the body sits inset
    # from the title/tables (a visible "double" left margin). Zero it on every style
    # we use so text aligns to the page margin; a final per-paragraph pass before
    # save catches anything inherited from docDefaults.
    for sname in ("Normal", "Title", "Subtitle", "Heading 1", "Heading 2", "Heading 3"):
        try:
            spf = doc.styles[sname].paragraph_format
            spf.left_indent = Inches(0)
            spf.right_indent = Inches(0)
            spf.first_line_indent = Inches(0)
        except KeyError:
            pass
    # Replace the template's "Header" placeholder with the proposal title (plus the
    # customer/cluster name when one is set), on every page.
    _hdr_name = (summary.get("cluster_name") or "").strip()
    _set_header_text(sec, "Infrastructure Sizing Proposal"
                     + (f" — {_hdr_name}" if _hdr_name else ""))
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
    _para(doc,
          "Scale Computing SC//HyperCore simplifies your systems and moves beyond "
          "traditional IT silos. The award-winning, self-healing platform identifies, "
          "reduces, and corrects problems in real-time — making application uptime "
          "easier for IT to manage and more affordable to run. Its lightweight, "
          "all-in-one architecture eliminates the need to combine separate "
          "virtualization software, disaster recovery software, servers, and shared "
          "storage from different vendors, deploying fully integrated, highly available "
          "virtualization right out of the box. Designed to scale as the business grows "
          "without downtime, disruption, or rigid hardware requirements, it lets teams "
          "spend less time on infrastructure maintenance and more on strategic projects.")
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
            # Start the sizing rationale on a fresh page (only when the diagram
            # was actually rendered, so we never emit a stray blank page).
            doc.add_page_break()

    # ── Sizing rationale (utilization bars + how the node count was reached) ──
    u = r.get("utilization")
    if u:
        rows_u, any_ha = util_rows(u)
        if rows_u:
            doc.add_heading("Sizing Rationale", level=1)
            png = render_util_bars(
                rows_u, limiting_key=(r.get("determinant") or {}).get("resource", ""),
                any_ha=any_ha)
            doc.add_picture(io.BytesIO(png), width=Inches(cw))
            doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
            _spacer(doc, 4)
            det = r.get("determinant") or {}
            res, hr = det.get("resource"), det.get("headroom_pct")
            if res == "CPU":
                _para(doc, f"Determined by CPU — {s.get('total_vcpus')} vCPUs ÷ "
                           f"{r.get('vcpu_ratio', 0):.2f}:1 overcommit = {det.get('required'):.0f} "
                           f"cores required, vs {det.get('achieved'):.0f} usable cores available at "
                           f"N-1 ({hr:.1f}% headroom).")
            elif res in ("RAM", "Storage"):
                unit = det.get("unit", "")
                _para(doc, f"Determined by {res} — {det.get('required')} {unit} required, vs "
                           f"{det.get('achieved')} {unit} available at N-1 ({hr:.1f}% headroom).")
            elif res == "Compute":
                cf = r.get("compute_floor") or {}
                util = cf.get("source_cpu_util_pct", 100)
                _para(doc, f"Determined by CPU performance — this cluster delivers "
                           f"{det.get('achieved'):.0f}% of your current environment's compute "
                           f"demand (rated throughput scaled to {util:.0f}% measured peak "
                           f"utilization, grown to the horizon); the node count was raised to "
                           f"clear that floor.")
            # Show compute-floor coverage even when another resource was binding.
            if res != "Compute":
                cfs = compute_floor_sentence(r)
                if cfs:
                    _para(doc, cfs, italic=True, size=9)
            _para(doc, "Each bar is 100% of the full cluster: solid = today's load, light hatch = "
                       "growth + snapshot reserve the workload is sized to, dark hatch = HA failover "
                       "capacity held back so the cluster still meets the workload with one node down "
                       "(N-1).", italic=True, size=9)
            _spacer(doc)

    # ── Performance vs current environment (benchmark comparison) ─────────────
    tgt = t.get("perf_index")
    if source_perf and source_perf.get("total_specrate") and tgt:
        doc.add_heading("Performance vs Current Environment", level=1)
        src_total = source_perf["total_specrate"]
        ratio = tgt / src_total if src_total else 0
        used_pm = False
        grid_rows = []
        for c in source_perf.get("cpus", []):
            is_pm = c.get("type") == "passmark"
            used_pm = used_pm or is_pm
            grid_rows.append([c.get("model", ""), str(c.get("sockets", "")),
                              f"{_fmt_num(c.get('score', 0))} {'PassMark' if is_pm else 'SPECrate'}",
                              _fmt_num(c.get("total", 0))])
        grid_rows.append(["Total environment", "", "", _fmt_num(src_total)])
        _grid_table(doc, ["Your current CPUs", "Sockets", "Score", "SPECrate"],
                    grid_rows, total_w=cw, weights=[3.4, 1.0, 1.6, 1.0])
        _spacer(doc, 4)
        used_pm = used_pm or bool(r.get("cpu_perf_is_passmark"))
        verdict = (f"{ratio:.1f}× the compute throughput of your current environment"
                   if ratio >= 1 else
                   f"{round(ratio * 100)}% of your current environment's compute throughput")
        _spec_table(doc, [
            ("Recommended cluster", f"{r.get('cpu', '')} × {r.get('node_count', '')} nodes"),
            ("Cluster SPECrate2017", _fmt_num(tgt)),
            ("In a benchmark, this should deliver", verdict),
        ], total_w=cw, label_w=2.6)
        _spacer(doc, 4)
        if used_pm:
            _para(doc, "Figures marked PassMark are converted to the SPECrate scale "
                       "(~0.00386 per CPU Mark, roughly ±20%).", italic=True, size=9)
        _para(doc, "Disclaimer: benchmark data is externally sourced (public SPEC and PassMark "
                   "results); provided for guidance only — no rights can be derived from these "
                   "figures.", italic=True, size=9)
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

    # ── HEAT automated tiering ───────────────────────────────────────────────
    doc.add_heading("HEAT — HyperCore Enhanced Automated Tiering", level=1)
    _para(doc,
          "Optimizing storage efficiency with HEAT. In flash-equipped SC//HyperCore "
          "nodes, HyperCore Enhanced Automated Tiering (HEAT) meters real-time IOPS for "
          "each virtual disk and intelligently places data blocks across the flash and "
          "spinning tiers based on block I/O heat mapping assessed from historical "
          "activity — striking the correct balance of IOPS efficiency across virtual "
          "disks and VMs.")
    for note in [
        "Per-disk flash priority — configurable flash allocation at the individual "
        "virtual-disk level through an easy-to-use slide bar in the HyperCore UI.",
        "Intelligent data placement — data-block priority based on block I/O heat "
        "mapping assessed from historical information.",
        "Automatic warm-up — new writes are assigned to the flash tier until SCRIBE can "
        "accurately assess their activity.",
        "Exponential priority scale — the 0–11 scale is exponential; raising a virtual "
        "disk from 4 to 5 doubles its priority for flash placement.",
        "Flexible tiering — setting 0 keeps static data off flash, while setting 11 "
        "multiplies flash priority by an order of magnitude.",
    ]:
        doc.add_paragraph(note, style=bullet_style)

    # ── SCRIBE block engine ──────────────────────────────────────────────────
    doc.add_heading("SCRIBE Block Engine", level=1)
    _para(doc,
          "Efficiency redefined. The Scale Computing Reliable Independent Block "
          "Engine (SCRIBE) is a critical software component of the SC//HyperCore "
          "virtualization suite — an enterprise-class, clustered, block storage layer "
          "purpose-built to be consumed directly by the KVM-based SC//HyperCore "
          "hypervisor. By interfacing directly with the hypervisor rather than "
          "repurposing a traditional file system, SCRIBE eliminates the performance "
          "bottlenecks, latency, and alignment issues associated with repurposed file "
          "systems.")
    for note in [
        "Performance — hypervisor-integrated block storage eliminates file-system "
        "latency, disk-partition misalignment, and snapshot delta-file merging.",
        "Simplified management — complex storage management tasks are abstracted away "
        "for automated storage with minimal manual intervention.",
        "Reliability — avoiding intermediary file-system abstractions minimizes the "
        "risk of data corruption or performance degradation.",
        "Native snapshots — fast, native snapshots with no merge penalties or "
        "performance hit.",
        "Storage efficiency — purpose-built for virtualized workloads with zero "
        "alignment issues.",
        "Effortless scale — simply add nodes; SCRIBE scales storage automatically with "
        "no downtime.",
        "Hardware-agnostic — runs on industry-standard hardware.",
    ]:
        doc.add_paragraph(note, style=bullet_style)

    # ── AIME autonomous infrastructure management ────────────────────────────
    doc.add_heading("AIME — Autonomous Infrastructure Management Engine", level=1)
    _para(doc,
          "AIME by Scale Computing — the AIOps platform for intelligent "
          "infrastructure. AIME is the artificial-intelligence orchestration and "
          "management functionality that powers the SC//HyperCore virtualization "
          "suite. Acting as a digital twin — a hand-built model of the environment the "
          "cluster runs in — it continuously thinks about the state the system is in, "
          "modelling the hardware, cluster operations, and surrounding environment so "
          "SC//HyperCore can handle day-to-day operational and maintenance tasks "
          "automatically. AIME monitors for security, hardware, and software errors, "
          "remediates issues where possible, and identifies root causes to minimize "
          "impact when automatic repair isn't feasible.")
    for note in [
        "Reduce manual intervention — comprehensive monitoring, proactive problem "
        "detection, and automated remediation keep the cluster healthy.",
        "Simplified troubleshooting — precise problem determination and actionable "
        "insights replace the guesswork of log interpretation.",
        "Autonomous remediation — automatically addresses issues to minimize downtime.",
        "Predictive anomaly detection — continuous monitoring surfaces problems before "
        "they escalate.",
        "Zero-touch deployment — automated provisioning of new nodes and resources.",
        "Resource optimization — dynamic workload balancing and automatic resource "
        "rerouting.",
        "Policy-based security automation — enforced without manual scripting.",
    ]:
        doc.add_paragraph(note, style=bullet_style)

    # Final pass: clear any indent inherited from docDefaults on every paragraph.
    for par in doc.paragraphs:
        ppf = par.paragraph_format
        ppf.left_indent = Inches(0)
        ppf.right_indent = Inches(0)
        ppf.first_line_indent = Inches(0)

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf


def _soffice_bin():
    return shutil.which("soffice") or shutil.which("libreoffice")


def _office_to_pdf(data_bytes, in_ext):
    """Convert an office document (.docx/.pptx) → PDF bytes via headless
    LibreOffice. Returns None if LibreOffice isn't available."""
    soffice = _soffice_bin()
    if not soffice:
        return None
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, f"doc.{in_ext}")
        with open(src, "wb") as f:
            f.write(data_bytes)
        try:
            subprocess.run([soffice, "--headless", "--convert-to", "pdf",
                            "--outdir", d, src],
                           check=True, timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env={**os.environ, "HOME": d})
        except Exception:
            return None
        out = os.path.join(d, "doc.pdf")
        return open(out, "rb").read() if os.path.exists(out) else None


def convert_docx_to_pdf(docx_bytes):
    return _office_to_pdf(docx_bytes, "docx")


def convert_pptx_to_pdf(pptx_bytes):
    return _office_to_pdf(pptx_bytes, "pptx")
