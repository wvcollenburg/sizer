"""Generate a 4-slide SC// proposal PowerPoint deck."""

import io
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR

SC_BLUE = RGBColor(0x00, 0x76, 0xCE)
SC_DARK_BLUE = RGBColor(0x00, 0x3A, 0x70)
CHARCOAL = RGBColor(0x33, 0x33, 0x33)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
LIGHT_GRAY = RGBColor(0xF2, 0xF4, 0xF7)
MID_GRAY = RGBColor(0x66, 0x66, 0x66)
GREEN = RGBColor(0x2E, 0x7D, 0x32)
RED = RGBColor(0xC6, 0x28, 0x28)
BORDER_SUBTLE = RGBColor(0xDE, 0xE2, 0xE6)
CARD_BG = RGBColor(0xF2, 0xF4, 0xF7)
CARD_BORDER = RGBColor(0xDD, 0xDD, 0xDD)


def generate_proposal(summary, recommendation, projection):
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    _slide_current_env(prs, summary)
    _slide_workload(prs, summary)
    _slide_proposal(prs, recommendation)
    _slide_projection(prs, summary, recommendation, projection)

    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf


def generate_config_slide(result):
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    slide = _add_slide(prs)
    mode = result.get("mode", "appliance")
    node_count = result["node_count"]
    pn = result["per_node"]
    cl = result["cluster_total"]
    n1 = result["n_minus_1"]

    if mode == "appliance":
        title = f"Configuration: {result.get('model', '')}"
        subtitle_parts = [f"{node_count} nodes"]
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
                   f"{node_count} nodes  —  {storage_type}  —  {pn.get('disk_count', 0)} drives per node")

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

    n1_rows = [
        ["N-1 Available", ""],
        ["Cores", str(n1["cores"])],
        ["Threads", str(n1["threads"])],
        ["GHz", str(n1["total_ghz"])],
        ["RAM", _fmt_ram(n1["ram_gb"])],
        ["Usable Storage", f"{n1['usable_storage_tb']} TB"],
    ]
    _add_table(slide, 9.0, 1.6, 4.0, n1_rows, [1.5, 2.5])

    buf = io.BytesIO()
    prs.save(buf)
    buf.seek(0)
    return buf


def _add_slide(prs):
    layout = prs.slide_layouts[6]  # blank
    slide = prs.slides.add_slide(layout)
    bg = slide.background.fill
    bg.solid()
    bg.fore_color.rgb = WHITE
    return slide


def _add_title(slide, text, subtitle=None):
    bar = slide.shapes.add_shape(
        1, Inches(0), Inches(0), Inches(13.333), Inches(1.1)
    )
    bar.fill.solid()
    bar.fill.fore_color.rgb = SC_DARK_BLUE
    bar.line.fill.background()

    txBox = slide.shapes.add_textbox(Inches(0.6), Inches(0.15), Inches(12), Inches(0.6))
    tf = txBox.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    run = p.add_run()
    run.text = "SC// "
    run.font.size = Pt(28)
    run.font.bold = True
    run.font.color.rgb = RGBColor(0x4D, 0xB8, 0xFF)
    run = p.add_run()
    run.text = text
    run.font.size = Pt(28)
    run.font.bold = True
    run.font.color.rgb = WHITE

    if subtitle:
        txBox2 = slide.shapes.add_textbox(Inches(0.6), Inches(0.7), Inches(12), Inches(0.35))
        tf2 = txBox2.text_frame
        p2 = tf2.paragraphs[0]
        run2 = p2.add_run()
        run2.text = subtitle
        run2.font.size = Pt(13)
        run2.font.color.rgb = RGBColor(0xBB, 0xD5, 0xEE)


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


HEADER_BG = RGBColor(0xE8, 0xF0, 0xF8)
ROW_ALT_BG = RGBColor(0xF7, 0xF8, 0xFA)


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
                _set_cell_border(tc_pr, "bottom", 1.5, "0076CE")
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
        ("Provisioned Storage", f"{s.get('total_vm_provisioned_storage_tb', 0)} TB", False),
        ("Datastore Used", f"{s.get('datastore_used_tb', 0)} TB", True),
        ("Datastore Total", f"{s.get('datastore_total_tb', 0)} TB", False),
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

def _slide_proposal(prs, r):
    slide = _add_slide(prs)

    num_cl = r.get("num_clusters", 1)
    layout = r.get("cluster_layout", [r["node_count"]])
    layout_str = " + ".join(str(x) for x in layout)
    cluster_desc = f"{num_cl} cluster{'s' if num_cl > 1 else ''} ({layout_str})" if num_cl > 1 else "1 cluster"

    model_label = f"Validated – based off {r['model']}" if r.get("validated") else r["model"]
    _add_title(slide, f"Proposed: {model_label}",
               f"{r['node_count']} nodes  —  {cluster_desc}  —  {r['form_factor']}  —  {r['chassis']}")

    node_rows = [
        ["Per Node", ""],
        ["CPU", r["cpu"]],
        ["Cores", str(r["cores_per_node"])],
        ["Threads", str(r["threads_per_node"])],
        ["RAM", _fmt_ram(r["ram_per_node_gb"])],
        ["Storage", r["storage_config"]["desc"]],
    ]
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
    _add_table(slide, 9.0, 1.6, 4.0, n1_rows, [1.5, 2.5])

    _add_card(slide, 0.6, 5.4, 3.5, 0.9,
              "vCPU : Core Ratio at N-1",
              f"{r['vcpu_ratio']:.2f} : 1", accent=True)


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

    txBox = slide.shapes.add_textbox(Inches(0.6), Inches(5.5), Inches(12), Inches(0.5))
    tf = txBox.text_frame
    pr = tf.paragraphs[0]
    run = pr.add_run()
    run.text = verdict_text
    run.font.size = Pt(14)
    run.font.bold = True
    run.font.color.rgb = GREEN if all_ok else SC_BLUE

    if full_cluster:
        cpu_note = slide.shapes.add_textbox(Inches(0.6), Inches(6.05), Inches(12), Inches(0.4))
        ctf = cpu_note.text_frame
        ctf.word_wrap = True
        cp = ctf.paragraphs[0]
        cr = cp.add_run()
        cr.text = ("CPU capacity is sized on the assumption that all nodes are operational; "
                   "in the event of a node failure, CPU performance may be temporarily reduced.")
        cr.font.size = Pt(10.5)
        cr.font.color.rgb = CHARCOAL

    disclaimer = slide.shapes.add_textbox(Inches(0.6), Inches(6.6), Inches(12), Inches(0.7))
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
