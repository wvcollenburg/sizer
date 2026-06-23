"""Generate SC// proposal / configuration PowerPoint decks.

Decks are derived from the branded SC template at resources/template.pptx so they
inherit its theme (Arial + the SC colour scheme) and slide masters; our content
slides are drawn on the template's BLANK layout. If the template file is missing
(e.g. not deployed), we fall back to a plain blank presentation so exports never
break.
"""

import io
import os
import re
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_CONNECTOR
from pptx.oxml.ns import qn

# Brand palette taken from the template theme (resources/template.pptx):
#   dk1 272727 · dk2 113859 · lt2 E9EAF0 · accent1 009ADE · accent2 194F90
#   accent4 3FB748 · accent5 97CAEB · accent6 F78D2C
SC_BLUE = RGBColor(0x00, 0x9A, 0xDE)       # accent1 — primary SC blue
SC_DARK_BLUE = RGBColor(0x11, 0x38, 0x59)  # dk2 — title bar / headings
SC_DEEP_BLUE = RGBColor(0x19, 0x4F, 0x90)  # accent2 — secondary accent / rules
SC_LIGHT_BLUE = RGBColor(0x97, 0xCA, 0xEB)  # accent5 — "SC//" prefix on dark
CHARCOAL = RGBColor(0x27, 0x27, 0x27)      # dk1
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
LIGHT_GRAY = RGBColor(0xE9, 0xEA, 0xF0)    # lt2
MID_GRAY = RGBColor(0x66, 0x66, 0x66)
GREEN = RGBColor(0x3F, 0xB7, 0x48)         # accent4
RED = RGBColor(0xC6, 0x28, 0x28)           # semantic warning (no template red)
ORANGE = RGBColor(0xF7, 0x8D, 0x2C)        # accent6
BORDER_SUBTLE = RGBColor(0xDE, 0xE2, 0xE6)
CARD_BG = RGBColor(0xE9, 0xEA, 0xF0)       # lt2
CARD_BORDER = RGBColor(0xDD, 0xDD, 0xDD)

_TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "resources", "template.pptx")

# Slide-title font — embedded in the template (embeddedFontLst), so it renders
# even where it isn't installed. ExtraLight is a thin weight, so titles aren't bold.
TITLE_FONT = "Martel Sans ExtraLight"


def _new_deck():
    """A fresh deck derived from the SC template (sample slides stripped), or a
    plain blank presentation if the template isn't available."""
    if os.path.exists(_TEMPLATE_PATH):
        prs = Presentation(_TEMPLATE_PATH)
        # Remove the template's sample slides. Drop BOTH the sldId reference and
        # the relationship, otherwise the orphaned slide parts linger and collide
        # with our new slides on save ("Duplicate name: slide1.xml").
        sld_id_lst = prs.slides._sldIdLst
        for sld_id in list(sld_id_lst):
            rid = sld_id.get(qn("r:id"))
            if rid:
                prs.part.drop_rel(rid)
            sld_id_lst.remove(sld_id)
    else:
        prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    return prs


def _blank_layout(prs):
    """The template's BLANK layout (falls back to the standard blank)."""
    for layout in prs.slide_layouts:
        if (layout.name or "").strip().upper() == "BLANK":
            return layout
    # python-pptx default template: layout 6 is "Blank".
    return prs.slide_layouts[6] if len(prs.slide_layouts) > 6 else prs.slide_layouts[-1]


def _content_layout(prs):
    """The branded light content layout ('b. blank light wide'). Its background
    is a clean light gradient with only a small bottom-right corner + // logo (no
    frame, no right-edge accent), so our tables clear the branding. We draw on top
    of it rather than painting our own background. Searches all masters (this
    layout lives on the second master) and falls back to BLANK if absent."""
    for master in prs.slide_masters:
        for layout in master.slide_layouts:
            if (layout.name or "").strip().lower() == "b. blank light wide":
                return layout
    return _blank_layout(prs)


def generate_proposal(summary, recommendation, projection):
    prs = _new_deck()

    _slide_current_env(prs, summary)
    _slide_workload(prs, summary)
    _slide_proposal(prs, recommendation, projection)
    _slide_projection(prs, summary, recommendation, projection)

    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf


def generate_config_slide(result):
    prs = _new_deck()

    slide = _add_slide(prs)
    mode = result.get("mode", "appliance")
    node_count = result["node_count"]
    so = result.get("storage_only")
    if so:
        nodes_label = (f"{node_count} HCI + {so['count']} storage-only "
                       f"({result.get('total_node_count', node_count)} total)")
    else:
        nodes_label = f"{node_count} node{'' if node_count == 1 else 's'}"
    num_cl = result.get("num_clusters", 1)
    if num_cl > 1:
        layout = result.get("cluster_layout", [])
        nodes_label += f"  —  {num_cl} clusters ({' + '.join(map(str, layout))})"
    pn = result["per_node"]
    cl = result["cluster_total"]
    n1 = result["n_minus_1"]

    if mode == "appliance":
        title = f"Configuration: {result.get('model', '')}"
        subtitle_parts = [nodes_label]
        if result.get("form_factor"):
            subtitle_parts.append(result["form_factor"])
        if result.get("chassis"):
            subtitle_parts.append(result["chassis"])
        _add_title(slide, title, "  —  ".join(subtitle_parts))

        node_rows = [
            ["Per Node", ""],
            ["CPU", pn.get("cpu", "")],
            ["Cores", str(pn["cores"])],
            ["Threads", str(pn["threads"])],
            ["Clock Speed", f"{pn['ghz']} GHz"],
            ["RAM", _fmt_ram(pn["ram_gb"])],
            ["RAW Storage", f"{pn['raw_storage_tb']} TB"],
        ]
    else:
        title = "Configuration: Software Only (Validated)"
        storage_type = result.get("storage_type", "")
        _add_title(slide, title,
                   f"{nodes_label}  —  {storage_type}  —  {pn.get('disk_count', 0)} drives per node")

        node_rows = [
            ["Per Node", ""],
            ["Cores", str(pn["cores"])],
            ["Threads", str(pn["threads"])],
            ["Clock Speed", f"{pn['ghz']} GHz"],
            ["RAM", _fmt_ram(pn["ram_gb"])],
            ["Drives", str(pn.get("disk_count", 0))],
            ["RAW Storage", f"{pn['raw_storage_tb']} TB"],
        ]

    _add_table(slide, 0.6, 1.6, 4.0, node_rows, [1.5, 2.5])

    total_rows = [
        ["Cluster Total", ""],
        ["Cores", str(cl["cores"])],
        ["Threads", str(cl["threads"])],
        ["GHz", str(cl["total_ghz"])],
        ["RAM", _fmt_ram(cl["ram_gb"])],
        ["RAW Storage", f"{cl['raw_storage_tb']} TB"],
        ["Usable Storage", f"{cl['usable_storage_tb']} TB"],
    ]
    _add_table(slide, 4.8, 1.6, 4.0, total_rows, [1.5, 2.5])

    if result.get("single_node"):
        # No peer node to fail over to — N-1 is meaningless. Replace the figures
        # with a greyed-out no-redundancy notice.
        _add_no_redundancy_box(slide, 9.0, 1.6, 4.0, result.get("redundancy_note")
                               or "No redundancy — ensure replication or backup is configured.")
    else:
        n1_rows = [
            ["N-1 Available", ""],
            ["Cores", str(n1["cores"])],
            ["Threads", str(n1["threads"])],
            ["GHz", str(n1["total_ghz"])],
            ["RAM", _fmt_ram(n1["ram_gb"])],
            ["Usable Storage", f"{n1['usable_storage_tb']} TB"],
        ]
        _add_table(slide, 9.0, 1.6, 4.0, n1_rows, [1.5, 2.5])

    if so:
        _add_storage_only_note(slide, so, 4.7)

    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf


def _add_slide(prs):
    # Use the branded content layout and let its background show — no white fill.
    return prs.slides.add_slide(_content_layout(prs))


def _add_title(slide, text, subtitle=None):
    # No bar, no "SC//" prefix — the template's branded background (corner + //
    # logo) carries the SC mark. Title sits lower to match the template's title
    # height, in Martel Sans ExtraLight; subtitle beneath it.
    txBox = slide.shapes.add_textbox(Inches(0.6), Inches(0.7), Inches(12.2), Inches(0.6))
    tf = txBox.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    run = p.add_run()
    run.text = text
    run.font.size = Pt(26)
    run.font.bold = False
    run.font.name = TITLE_FONT
    run.font.color.rgb = SC_DARK_BLUE

    # Thin dark-blue rule from just after the title to the right edge (matches the
    # template title style). Title width is estimated from the text length since
    # python-pptx can't measure rendered glyphs; the gap is kept generous so the
    # rule never overlaps the text. Skipped if the title is too long to leave room.
    title_end = 0.6 + len(text) * 0.17
    line_x1 = min(title_end + 0.35, 11.5)
    line_y = 0.96
    if line_x1 < 12.6:
        rule = slide.shapes.add_connector(
            MSO_CONNECTOR.STRAIGHT, Inches(line_x1), Inches(line_y),
            Inches(12.85), Inches(line_y))
        rule.line.color.rgb = SC_DARK_BLUE
        rule.line.width = Pt(1)

    if subtitle:
        txBox2 = slide.shapes.add_textbox(Inches(0.6), Inches(1.18), Inches(12.2), Inches(0.35))
        tf2 = txBox2.text_frame
        p2 = tf2.paragraphs[0]
        run2 = p2.add_run()
        run2.text = subtitle
        run2.font.size = Pt(13)
        run2.font.color.rgb = MID_GRAY


def _add_card(slide, left, top, width, height, label, value, accent=False):
    shape = slide.shapes.add_shape(
        1, Inches(left), Inches(top), Inches(width), Inches(height)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = CARD_BG
    shape.line.color.rgb = CARD_BORDER
    shape.line.width = Pt(1)

    tf = shape.text_frame
    tf.word_wrap = True
    tf.margin_top = Pt(8)
    tf.margin_left = Pt(10)
    tf.margin_right = Pt(10)

    p_label = tf.paragraphs[0]
    run_l = p_label.add_run()
    run_l.text = label
    run_l.font.size = Pt(10)
    run_l.font.color.rgb = MID_GRAY

    p_val = tf.add_paragraph()
    run_v = p_val.add_run()
    run_v.text = str(value)
    run_v.font.size = Pt(18)
    run_v.font.bold = True
    run_v.font.color.rgb = SC_BLUE if accent else CHARCOAL


def _add_no_redundancy_box(slide, left, top, width, msg):
    """Greyed-out N-1 replacement for a Single Node System: a header and the
    no-redundancy notice, styled to read as a warning rather than data."""
    height = 2.0
    shape = slide.shapes.add_shape(
        1, Inches(left), Inches(top), Inches(width), Inches(height)
    )
    shape.fill.solid()
    shape.fill.fore_color.rgb = LIGHT_GRAY
    shape.line.color.rgb = CARD_BORDER
    shape.line.width = Pt(1)

    tf = shape.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.TOP
    tf.margin_top = Pt(10)
    tf.margin_left = Pt(12)
    tf.margin_right = Pt(12)

    p_label = tf.paragraphs[0]
    run_l = p_label.add_run()
    run_l.text = "N-1 (Update/Failure)"
    run_l.font.size = Pt(11)
    run_l.font.bold = True
    run_l.font.color.rgb = MID_GRAY

    p_head = tf.add_paragraph()
    p_head.space_before = Pt(8)
    run_h = p_head.add_run()
    run_h.text = "No Redundancy"
    run_h.font.size = Pt(14)
    run_h.font.bold = True
    run_h.font.color.rgb = RED

    p_msg = tf.add_paragraph()
    p_msg.space_before = Pt(6)
    run_m = p_msg.add_run()
    # Drop the leading "No redundancy — " since the heading already says it.
    run_m.text = re.sub(r"^No redundancy[^a-zA-Z]*", "", msg)
    run_m.font.size = Pt(10.5)
    run_m.font.color.rgb = CHARCOAL


HEADER_BG = RGBColor(0xE9, 0xEA, 0xF0)   # lt2 — table header band
ROW_ALT_BG = RGBColor(0xF6, 0xF7, 0xFA)  # subtle zebra stripe


def _set_cell_border(tc_pr, side, width_pt, color_hex):
    from pptx.oxml.ns import qn
    tag = {"bottom": "a:lnB", "top": "a:lnT", "left": "a:lnL", "right": "a:lnR"}[side]
    for old in tc_pr.findall(qn(tag)):
        tc_pr.remove(old)
    ln = tc_pr.makeelement(qn(tag), {})
    if color_hex is None:
        ln.append(ln.makeelement(qn("a:noFill"), {}))
    else:
        ln.set("w", str(Pt(width_pt)))
        sf = ln.makeelement(qn("a:solidFill"), {})
        sf.append(sf.makeelement(qn("a:srgbClr"), {"val": color_hex}))
        ln.append(sf)
    tc_pr.append(ln)


def _add_table(slide, left, top, width, rows_data, col_widths=None):
    rows = len(rows_data)
    cols = len(rows_data[0]) if rows_data else 2
    table_shape = slide.shapes.add_table(rows, cols, Inches(left), Inches(top),
                                          Inches(width), Inches(0.38 * rows))
    table = table_shape.table

    tbl_pr = table._tbl.tblPr
    for attr in ["bandRow", "bandCol", "firstRow", "lastRow"]:
        tbl_pr.attrib[attr] = "0"

    if col_widths:
        for i, w in enumerate(col_widths):
            table.columns[i].width = Inches(w)

    for r, row in enumerate(rows_data):
        for c, val in enumerate(row):
            cell = table.cell(r, c)
            cell.text = str(val)
            cell.margin_top = Pt(5)
            cell.margin_bottom = Pt(5)
            cell.margin_left = Pt(8)

            cell.fill.solid()
            if r == 0:
                cell.fill.fore_color.rgb = HEADER_BG
            elif r % 2 == 0:
                cell.fill.fore_color.rgb = ROW_ALT_BG
            else:
                cell.fill.fore_color.rgb = WHITE

            tc_pr = cell._tc.get_or_add_tcPr()
            _set_cell_border(tc_pr, "left", None, None)
            _set_cell_border(tc_pr, "right", None, None)
            _set_cell_border(tc_pr, "top", None, None)

            if r == 0:
                _set_cell_border(tc_pr, "bottom", 1.5, "009ADE")
            elif r < rows - 1:
                _set_cell_border(tc_pr, "bottom", 0.5, "DEE2E6")
            else:
                _set_cell_border(tc_pr, "bottom", None, None)

            for paragraph in cell.text_frame.paragraphs:
                if r == 0:
                    paragraph.font.size = Pt(11)
                    paragraph.font.bold = True
                    paragraph.font.color.rgb = SC_DARK_BLUE
                elif c == 0:
                    paragraph.font.size = Pt(11)
                    paragraph.font.color.rgb = MID_GRAY
                else:
                    paragraph.font.size = Pt(11)
                    paragraph.font.color.rgb = CHARCOAL

    return table_shape


def _fmt_ram(gb):
    if gb >= 1024:
        return f"{gb / 1024:.1f} TB"
    return f"{gb} GB"


def _storage_only_desc(so):
    """One-line CPU/RAM descriptor for a storage-only-node block. Handles both
    the appliance/recommendation shape (has 'cpu') and the validated shape
    (cores/threads/ghz)."""
    cpu = so.get("cpu")
    if not cpu:
        cpu = f"{so.get('cores', 0)} cores @ {so.get('ghz', 0)}GHz"
    # The block's CPU desc carries a "1 x" prefix (each storage-only node is
    # single-socket); drop it so it doesn't read "2 × 1 x ..." after the count.
    for prefix in ("1 x ", "1 × "):
        if cpu.startswith(prefix):
            cpu = cpu[len(prefix):]
            break
    return cpu


def _add_storage_only_note(slide, so, top):
    """Render the storage-only-node summary line. ``so`` is the storage_only
    block; ``top`` is the vertical position in inches."""
    box = slide.shapes.add_textbox(Inches(0.6), Inches(top), Inches(12), Inches(0.6))
    tf = box.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    run = p.add_run()
    run.text = "Storage-only nodes: "
    run.font.size = Pt(11)
    run.font.bold = True
    run.font.color.rgb = SC_BLUE
    run2 = p.add_run()
    run2.text = (
        f"{so['count']} × {_storage_only_desc(so)}, {_fmt_ram(so['ram_gb'])} RAM, "
        f"{so['raw_storage_tb']} TB raw each — virtualization disabled "
        f"(storage + IOPS only, no VMs)."
    )
    run2.font.size = Pt(11)
    run2.font.color.rgb = CHARCOAL


def _fmt_num(n):
    if isinstance(n, float):
        return f"{n:,.1f}"
    return f"{n:,}"


# ── Slide 1: Current Environment ─────────────────────────────────────────────

def _slide_current_env(prs, s):
    slide = _add_slide(prs)
    _add_title(slide, "Current Environment",
               f"{s.get('current_platform', '')}  —  {s.get('cluster_name', '')}")

    y = 1.6
    cards = [
        ("Hosts", s.get("host_count", 0)),
        ("Total Cores", _fmt_num(s.get("total_host_cores", 0))),
        ("Total Threads", _fmt_num(s.get("total_host_threads", 0))),
        ("Total GHz", _fmt_num(s.get("total_host_ghz", 0))),
        ("Total RAM", _fmt_ram(s.get("total_host_ram_gb", 0))),
    ]
    for i, (label, val) in enumerate(cards):
        _add_card(slide, 0.6 + i * 2.5, y, 2.3, 0.9, label, val)

    y2 = 2.8
    perf = [
        ("Peak CPU %", f"{s.get('peak_cpu_pct', 0)}%"),
        ("Avg CPU %", f"{s.get('avg_cpu_pct', 0)}%"),
        ("Peak Memory %", f"{s.get('peak_mem_pct', 0)}%"),
        ("Avg Memory %", f"{s.get('avg_mem_pct', 0)}%"),
    ]
    for i, (label, val) in enumerate(perf):
        _add_card(slide, 0.6 + i * 2.5, y2, 2.3, 0.9, label, val)

    y3 = 4.0
    iops = [
        ("Avg IOPS", _fmt_num(s.get("total_avg_iops", 0))),
        ("Peak IOPS", _fmt_num(s.get("total_peak_iops", 0))),
    ]
    p95 = s.get("p95_iops", 0)
    if p95 and p95 > 0:
        iops.append(("P95 IOPS", _fmt_num(p95)))
    iops.append(("NIC Speed", f"{s.get('nic_speed_mbps', 0) / 1000:.0f} GbE"))

    for i, (label, val) in enumerate(iops):
        _add_card(slide, 0.6 + i * 2.5, y3, 2.3, 0.9, label, val)

    y4 = 5.2
    ratio = s.get("vcpu_per_core_ratio", 0)
    if ratio > 0:
        _add_card(slide, 0.6, y4, 2.3, 0.9, "vCPU : Core Ratio",
                  f"{ratio:.2f} : 1", accent=True)


# ── Slide 2: Workload Consumption ────────────────────────────────────────────

def _slide_workload(prs, s):
    slide = _add_slide(prs)
    _add_title(slide, "Workload Analysis",
               f"{s.get('active_vms', 0)} active VMs of {s.get('total_vms', 0)} total")

    y = 1.6
    compute = [
        ("Total vCPUs", _fmt_num(s.get("total_vcpus", 0)), True),
        ("Provisioned RAM", _fmt_ram(s.get("total_vm_provisioned_memory_gb", 0)), True),
        ("Used RAM", _fmt_ram(s.get("total_vm_used_memory_gb", 0)), False),
    ]
    for i, (label, val, accent) in enumerate(compute):
        _add_card(slide, 0.6 + i * 4.0, y, 3.7, 1.0, label, val, accent)

    y2 = 2.9
    storage = [
        ("Provisioned Storage", f"{s.get('total_vm_provisioned_storage_tb', 0)} TiB", False),
        ("Datastore Used", f"{s.get('datastore_used_tb', 0)} TiB", True),
        ("Datastore Total", f"{s.get('datastore_total_tb', 0)} TiB", False),
    ]
    for i, (label, val, accent) in enumerate(storage):
        _add_card(slide, 0.6 + i * 4.0, y2, 3.7, 1.0, label, val, accent)

    y3 = 4.4
    ratio = s.get("vcpu_per_core_ratio", 0)
    if ratio > 0:
        _add_card(slide, 0.6, y3, 5.5, 1.0,
                  "Current Virtualization Ratio",
                  f"{ratio:.2f} : 1  ({s.get('total_vcpus', 0)} vCPUs / "
                  f"{s.get('total_host_cores', 0)} cores)", accent=True)


# ── Slide 3: Proposed Configuration ──────────────────────────────────────────

def _slide_proposal(prs, r, projection=None):
    slide = _add_slide(prs)

    num_cl = r.get("num_clusters", 1)
    layout = r.get("cluster_layout", [r["node_count"]])
    layout_str = " + ".join(str(x) for x in layout)
    cluster_desc = f"{num_cl} cluster{'s' if num_cl > 1 else ''} ({layout_str})" if num_cl > 1 else "1 cluster"

    if r.get("validated_only"):
        model_label = r["model"]
    elif r.get("validated"):
        model_label = f"Validated – based off {r['model']}"
    else:
        model_label = r["model"]
    so = r.get("storage_only")
    nodes_label = (f"{r.get('hci_node_count', r['node_count'])} HCI + "
                   f"{so['count']} storage-only" if so else f"{r['node_count']} nodes")
    _add_title(slide, f"Proposed: {model_label}",
               f"{nodes_label}  —  {cluster_desc}  —  {r['form_factor']}  —  {r['chassis']}")

    iops = r.get("iops") or {}

    node_rows = [
        ["Per HCI Node" if so else "Per Node", ""],
        ["CPU", r["cpu"]],
        ["Cores", str(r["cores_per_node"])],
        ["Threads", str(r["threads_per_node"])],
        ["RAM", _fmt_ram(r["ram_per_node_gb"])],
        ["Storage", r["storage_config"]["desc"]],
    ]
    if iops:
        node_rows.append(["Net IOPS", f"{iops['per_node']:,}"])
    _add_table(slide, 0.6, 1.6, 4.0, node_rows, [1.5, 2.5])

    t = r["totals"]
    total_rows = [
        ["Cluster Total", ""],
        ["Cores", str(t["cores"])],
        ["Threads", str(t["threads"])],
        ["GHz", str(t["total_ghz"])],
        ["RAM", _fmt_ram(t["ram_gb"])],
        ["Raw Storage", f"{t['raw_storage_tb']} TB"],
        ["Usable Storage", f"{t['usable_storage_tb']} TB"],
    ]
    if iops:
        total_rows.append(["Net IOPS", f"{iops['total']:,}"])
    _add_table(slide, 4.8, 1.6, 4.0, total_rows, [1.5, 2.5])

    n = r["n_minus_1"]
    n1_label = f"N-1 ({num_cl} spare{'s' if num_cl > 1 else ''})" if num_cl > 1 else "N-1 Available"
    n1_rows = [
        [n1_label, ""],
        ["Cores", str(n["cores"])],
        ["Threads", str(n["threads"])],
        ["GHz", str(n["total_ghz"])],
        ["RAM", _fmt_ram(n["ram_gb"])],
        ["Usable Storage", f"{n['usable_storage_tb']} TB"],
    ]
    if iops:
        n1_rows.append(["Net IOPS", f"{iops['n_minus_1']:,}"])
    _add_table(slide, 9.0, 1.6, 4.0, n1_rows, [1.5, 2.5])

    if so:
        _add_storage_only_note(slide, so, 4.5)

    _add_card(slide, 0.6, 5.4, 3.5, 0.9,
              "vCPU : Core Ratio at N-1",
              f"{r['vcpu_ratio']:.2f} : 1", accent=True)

    # Net IOPS headroom vs the workload's measured demand (informational).
    demand = (projection or {}).get("iops_demand") or {}
    if iops and (demand.get("p95") or demand.get("avg")):
        metric = "P95" if demand.get("p95") else "Avg"
        value = demand.get("p95") or demand.get("avg")
        ratio = iops["total"] / value if value else 0
        _add_card(slide, 4.3, 5.4, 4.0, 0.9,
                  f"Net IOPS Headroom vs {metric}",
                  f"{ratio:.1f}x  ({iops['total']:,} net / {value:,} demand)")

    # Derivation footnote — the PPTX is the one place we show how net IOPS is
    # reached (raw drive IOPS, SCRIBE derating, RF write-amplification).
    if iops and iops.get("raw_per_node"):
        note = (f"Net IOPS = raw {iops['raw_per_node']:,}/node "
                f"− {iops['derating_pct']:.0f}% derating "
                f"= {iops['derated_per_node']:,} ÷ {iops['write_amp']}× RF write-amp "
                f"= {iops['per_node']:,}/node, × {r['node_count']} nodes.")
        box = slide.shapes.add_textbox(Inches(0.6), Inches(6.5), Inches(11.2), Inches(0.5))
        p = box.text_frame.paragraphs[0]
        run = p.add_run()
        run.text = note
        run.font.size = Pt(9)
        run.font.color.rgb = MID_GRAY


# ── Slide 4: Growth Projection ───────────────────────────────────────────────

def _slide_projection(prs, s, r, p):
    slide = _add_slide(prs)
    full_cluster = r.get("sized_full_cluster", False)
    cpu_basis = "full cluster (N)" if full_cluster else "N-1"
    _add_title(slide, f"Capacity Planning — {p['years']} Year Projection",
               f"{p['growth_pct']}% YoY growth  —  {p['snapshot_pct']}% snapshot overhead  "
               f"—  Growth factor: {p['growth_factor']}x  —  CPU sizing: {cpu_basis}")

    n1 = r["n_minus_1"]

    headers = ["Resource", "Current", f"Year {p['years']} Projected",
               "Proposed (N-1)", "Headroom"]

    # vCPU is sized against the full cluster when that mode is on; RAM, GHz and
    # storage remain N-1, so only this row's basis changes.
    cpu_basis_cores = r["totals"]["cores"] if full_cluster else n1["cores"]
    vcpu_headroom = cpu_basis_cores * r["vcpu_ratio"] - p["projected_vcpus"]
    ram_headroom = n1["ram_gb"] - p["projected_ram_gb"]
    stor_headroom = n1["usable_storage_tb"] - p["projected_storage_tb"]
    ghz_headroom = n1["total_ghz"] - p.get("projected_ghz", 0)

    rows = [
        headers,
        ["vCPUs", _fmt_num(p["base_vcpus"]), _fmt_num(p["projected_vcpus"]),
         f"{_fmt_num(cpu_basis_cores)} cores @ {r['vcpu_ratio']:.1f}:1"
         + (" (N)" if full_cluster else ""),
         f"+{_fmt_num(int(vcpu_headroom))} vCPU capacity"],
        ["RAM", _fmt_ram(p["base_ram_gb"]), _fmt_ram(p["projected_ram_gb"]),
         _fmt_ram(n1["ram_gb"]),
         f"+{_fmt_ram(round(ram_headroom, 1))}"],
        ["GHz", f"{p.get('base_ghz', 0)}", f"{p.get('projected_ghz', 0)}",
         f"{n1['total_ghz']}",
         f"+{round(ghz_headroom, 1)} GHz"],
        ["Storage", f"{p['base_storage_tb']} TB",
         f"{p['projected_storage_tb']} TB (incl. snapshots)",
         f"{n1['usable_storage_tb']} TB usable",
         f"+{round(stor_headroom, 2)} TB"],
    ]

    table_shape = _add_table(slide, 0.6, 1.6, 12.1, rows,
                              [1.5, 2.2, 3.0, 3.0, 2.4])

    y = 4.2
    params = [
        ("Growth Rate", f"{p['growth_pct']}% per year"),
        ("Growth Factor", f"{p['growth_factor']}x over {p['years']} years"),
        ("Snapshot Overhead", f"{p['snapshot_pct']}% base → "
                              f"{p.get('snapshot_pct_at_target', 0)}% at year {p['years']}"),
    ]
    for i, (label, val) in enumerate(params):
        _add_card(slide, 0.6 + i * 4.0, y, 3.7, 0.9, label, val)

    all_ok = vcpu_headroom >= 0 and ram_headroom >= 0 and stor_headroom >= 0
    if all_ok:
        verdict_text = ("This configuration meets all projected requirements through "
                        f"year {p['years']}.")
    else:
        verdict_text = (f"Based on current projections, some resources may need to be expanded "
                        f"before year {p['years']}.")

    txBox = slide.shapes.add_textbox(Inches(0.6), Inches(5.5), Inches(11.2), Inches(0.5))
    tf = txBox.text_frame
    pr = tf.paragraphs[0]
    run = pr.add_run()
    run.text = verdict_text
    run.font.size = Pt(14)
    run.font.bold = True
    run.font.color.rgb = GREEN if all_ok else SC_BLUE

    if full_cluster:
        cpu_note = slide.shapes.add_textbox(Inches(0.6), Inches(6.05), Inches(11.2), Inches(0.4))
        ctf = cpu_note.text_frame
        ctf.word_wrap = True
        cp = ctf.paragraphs[0]
        cr = cp.add_run()
        cr.text = ("CPU capacity is sized on the assumption that all nodes are operational; "
                   "in the event of a node failure, CPU performance may be temporarily reduced.")
        cr.font.size = Pt(10.5)
        cr.font.color.rgb = CHARCOAL

    disclaimer = slide.shapes.add_textbox(Inches(0.6), Inches(6.6), Inches(11.2), Inches(0.7))
    dtf = disclaimer.text_frame
    dtf.word_wrap = True
    dp = dtf.paragraphs[0]
    dr = dp.add_run()
    dr.text = (f"Note: {p['years']}-year projections are estimates based on assumed linear growth rates "
               "and should not be taken as guarantees. Actual capacity needs will depend on business "
               "changes, application evolution, market trends, and workload characteristics.")
    dr.font.size = Pt(10)
    dr.font.italic = True
    dr.font.color.rgb = MID_GRAY
