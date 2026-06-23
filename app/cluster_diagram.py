"""Generate SC// house-style cluster network diagrams as SVG.

One generator covers every topology the sizer produces:
  * SNS (single node)        — LAN only (L0/L1), no backplane.
  * 2-node + witness         — two data nodes plus a witness (all links).
  * Normal cluster (3+)      — full LAN + backplane.

Cabling rules (no LAG — always active/passive failover):
  * Switch 1 carries L0 + B0 from every node; Switch 2 carries L1 + B1.
  * Interlink between the two switches carries the backplane between switches;
    the backplane VLAN never uplinks to the existing network. LAN (L0/L1) is
    what uplinks.
  * 2-NIC nodes run the backplane as a VLAN over L0/L1 (no dedicated B0/B1).
  * We always plug into the customer's existing switches — 2 of them, or 1 only
    when a node has a single NIC.

The SVG has a transparent background so it drops onto the branded slide / PDF;
colours come from the house palette (LAN = blue, backplane = yellow).
"""

from xml.sax.saxutils import escape

# ── house palette ────────────────────────────────────────────────────────────
DK2 = "#113859"          # navy — HCI nodes, borders, labels
SWITCH_FILL = "#2C5F8A"  # switch bars
EXIST_FILL = "#EEF2F7"
MUTED = "#5A6B7D"        # witness node (lighter grey)
STORAGE_FILL = "#45555F"  # storage-only node (slightly darker grey than witness)
ROLE_LABEL = "#B9C7D4"   # role sub-label on the dark node bars
L0 = "#009ADE"           # LAN leg to switch 1 (bright blue)
L1 = "#0B4A8A"           # LAN leg to switch 2 (deep blue)
B0 = "#F2C200"           # backplane leg to switch 1 (yellow)
B1 = "#BD9100"           # backplane leg to switch 2 (dark gold)
WHITE = "#FFFFFF"

FONT = "Arial, Helvetica, sans-serif"


class _SVG:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.parts = []

    def rect(self, x, y, w, h, fill, rx=6, stroke=None, sw=1, opacity=None):
        s = f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}" fill="{fill}"'
        if stroke:
            s += f' stroke="{stroke}" stroke-width="{sw}"'
        if opacity is not None:
            s += f' opacity="{opacity}"'
        self.parts.append(s + "/>")

    def text(self, x, y, s, size=15, fill=DK2, anchor="middle", weight="normal"):
        self.parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" '
            f'fill="{fill}" text-anchor="{anchor}" font-weight="{weight}" '
            f'dominant-baseline="middle">{escape(s)}</text>')

    def line(self, x1, y1, x2, y2, stroke, sw=2, dash=None):
        s = f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" stroke="{stroke}" stroke-width="{sw}"'
        if dash:
            s += f' stroke-dasharray="{dash}"'
        self.parts.append(s + "/>")

    def path(self, pts, stroke, sw=2):
        d = "M " + " L ".join(f"{x:.1f} {y:.1f}" for x, y in pts)
        self.parts.append(
            f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{sw}" '
            f'stroke-linejoin="round" stroke-linecap="round"/>')

    def svg(self):
        body = "\n".join(self.parts)
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" '
                f'height="{self.h}" viewBox="0 0 {self.w} {self.h}">\n{body}\n</svg>')


def _chip(svg, x, y, label, fill, text_fill):
    svg.rect(x, y, CHIP_W, CHIP_H, fill, rx=3, stroke=DK2, sw=1)
    svg.text(x + CHIP_W / 2, y + CHIP_H / 2 + 1, label, size=11,
             fill=text_fill, weight="bold")


# ── geometry constants ───────────────────────────────────────────────────────
CHIP_W, CHIP_H = 34, 22


def render_cluster_svg(nodes, witness=False, single_switch=False, title=None,
                       canvas_h=None):
    """nodes: list of {"name": str, "nics": 2|4}. Returns an SVG string.

    canvas_h overrides the height (used only to pad to a square for qlmanage
    self-checks; production calls leave it None so the SVG is content-sized)."""
    two_sw = not single_switch

    entities = list(nodes)
    if witness:
        entities = entities + [{"name": "Witness", "nics": 4, "_witness": True}]
    n = len(entities)
    multi = n >= 2          # backplane exists only when there are peers to talk to
    any_ded_bp = any_vlan_bp = False

    # ── layout grid (scales with node count) ──────────────────────────────────
    lane_step = 26
    sw_w, sw_h = 780, 46
    node_w, node_h = 360, 54
    gap = 16
    fan = n * lane_step + 36                   # left/right room for n cable lanes
    sw_x = fan                                 # switch starts after the left fan
    W = sw_x + sw_w + fan                       # symmetric → switch centred in W
    cx = W / 2
    node_x = cx - node_w / 2
    sw1_y, sw2_y = 196, 268
    nodes_top = 360
    lan_lane0 = sw_x - 22                       # innermost LAN lane (just left of switch)
    bp_lane0 = sw_x + sw_w + 22                # innermost backplane lane (just right)
    content_bottom = nodes_top + n * (node_h + gap) + 110
    H = canvas_h or content_bottom
    svg = _SVG(W, H)

    def entry_y(base):  # spread the n cable entry points across the switch height
        return [base + 9 + i * ((sw_h - 18) / max(n - 1, 1)) for i in range(n)]
    sw1_ys, sw2_ys = entry_y(sw1_y), entry_y(sw2_y)

    # ── existing network + LAN uplinks ───────────────────────────────────────
    en_w, en_h = 260, 76
    en_x, en_y = cx - en_w / 2, 28
    svg.rect(en_x, en_y, en_w, en_h, EXIST_FILL, rx=8, stroke=DK2, sw=1.5)
    svg.text(cx, en_y + en_h / 2, "Existing Network", size=16, fill=DK2, weight="bold")
    for ux in (cx - 70, cx + 70):
        svg.line(ux, sw1_y, ux, en_y + en_h, L0, sw=2, dash="6 5")
    svg.text(cx + 84, en_y + en_h + 6, "LAN uplink", size=12, fill=MUTED, anchor="start")

    # ── switches + interlink ─────────────────────────────────────────────────
    svg.rect(sw_x, sw1_y, sw_w, sw_h, SWITCH_FILL, rx=6)
    svg.text(cx, sw1_y + sw_h / 2, "Switch 1 (primary)", size=16, fill=WHITE, weight="bold")
    if two_sw:
        svg.rect(sw_x, sw2_y, sw_w, sw_h, SWITCH_FILL, rx=6)
        svg.text(cx, sw2_y + sw_h / 2, "Switch 2 (secondary)", size=16, fill=WHITE, weight="bold")
        svg.line(cx, sw1_y + sw_h, cx, sw2_y, DK2, sw=2)
        svg.parts.append(f'<polygon points="{cx-4},{sw2_y-1} {cx+4},{sw2_y-1} {cx},{sw2_y+6}" fill="{DK2}"/>')
        svg.parts.append(f'<polygon points="{cx-4},{sw1_y+sw_h+1} {cx+4},{sw1_y+sw_h+1} {cx},{sw1_y+sw_h-6}" fill="{DK2}"/>')
        svg.text(cx + 64, (sw1_y + sw_h + sw2_y) / 2, "Interlink", size=12, fill=MUTED, anchor="start")

    # ── nodes + cabling ──────────────────────────────────────────────────────
    for i, ent in enumerate(entities):
        ny = nodes_top + i * (node_h + gap)
        role = "witness" if ent.get("_witness") else ent.get("role", "hci")
        is_wit = role == "witness"
        fill = {"hci": DK2, "storage": STORAGE_FILL, "witness": MUTED}[role]
        svg.rect(node_x, ny, node_w, node_h, fill, rx=6)
        ncx = node_x + node_w / 2
        if is_wit:
            svg.text(ncx, ny + node_h / 2, ent["name"], size=15, fill=WHITE, weight="bold")
        else:
            svg.text(ncx, ny + node_h / 2 - 8, ent["name"], size=15, fill=WHITE, weight="bold")
            svg.text(ncx, ny + node_h / 2 + 11, "HCI" if role == "hci" else "Storage-only",
                     size=10, fill=ROLE_LABEL)

        # ≤3 NICs leaves no room for a dedicated backplane pair, so it rides a
        # tagged VLAN over the LAN NICs; ≥4 NICs (2 LAN + 2 backplane) → dedicated.
        shared_nics = ent.get("nics", 4) < 4 and not is_wit
        vlan_bp = shared_nics and multi   # backplane rides a VLAN over L0/L1
        ded_bp = (not shared_nics) and multi  # dedicated B0/B1 NICs
        any_ded_bp = any_ded_bp or ded_bp
        any_vlan_bp = any_vlan_bp or vlan_bp
        l0y, l1y = ny + 5, ny + node_h - CHIP_H - 5
        _chip(svg, node_x + 4, l0y, "L0", L0, WHITE)
        _chip(svg, node_x + 4, l1y, "L1", L1, WHITE)
        if vlan_bp:
            svg.text(node_x + 4 + CHIP_W + 5, ny + node_h / 2, "+BP VLAN",
                     size=9, fill="#BFE3F7", anchor="start")

        lane = lan_lane0 - i * lane_step     # < sw_x, so vertical run clears the switch
        svg.path([(node_x + 4, l0y + CHIP_H / 2), (lane, l0y + CHIP_H / 2),
                  (lane, sw1_ys[i]), (sw_x, sw1_ys[i])], L0)
        l1_target = (sw2_ys[i], sw2_y) if two_sw else (sw1_ys[i] + 5, sw1_y)
        svg.path([(node_x + 4, l1y + CHIP_H / 2), (lane - 11, l1y + CHIP_H / 2),
                  (lane - 11, l1_target[0]), (sw_x, l1_target[0])], L1)

        if ded_bp:
            b0y, b1y = ny + 5, ny + node_h - CHIP_H - 5
            _chip(svg, node_x + node_w - CHIP_W - 4, b0y, "B0", B0, DK2)
            _chip(svg, node_x + node_w - CHIP_W - 4, b1y, "B1", B1, WHITE)
            rlane = bp_lane0 + i * lane_step  # > sw right edge, clears the switch
            svg.path([(node_x + node_w - 4, b0y + CHIP_H / 2), (rlane, b0y + CHIP_H / 2),
                      (rlane, sw1_ys[i]), (sw_x + sw_w, sw1_ys[i])], B0)
            if two_sw:
                svg.path([(node_x + node_w - 4, b1y + CHIP_H / 2), (rlane + 11, b1y + CHIP_H / 2),
                          (rlane + 11, sw2_ys[i]), (sw_x + sw_w, sw2_ys[i])], B1)

    # ── legend ───────────────────────────────────────────────────────────────
    ly = content_bottom - 96
    svg.rect(node_x, ly, 22, 12, L0, rx=2, stroke=DK2, sw=0.5)
    svg.text(node_x + 30, ly + 6, "LAN (L0/L1) — VMs ↔ network, uplinks to existing",
             size=12, fill=DK2, anchor="start")
    ly += 22
    if any_ded_bp:
        svg.rect(node_x, ly, 22, 12, B0, rx=2, stroke=DK2, sw=0.5)
        svg.text(node_x + 30, ly + 6, "Backplane (B0/B1) — node-to-node, stays on switches (interlink only)",
                 size=12, fill=DK2, anchor="start")
        ly += 22
    elif any_vlan_bp:
        svg.text(node_x, ly + 6, "Backplane — tagged VLAN over L0/L1 (no dedicated NICs); stays on switches",
                 size=12, fill=DK2, anchor="start")
        ly += 22
    svg.text(node_x, ly + 6, "No LAG — active/passive failover across switches",
             size=12, fill=MUTED, anchor="start")

    if title:
        svg.text(cx, 18, title, size=18, fill=DK2, weight="bold")
    return svg.svg()


def network_svg_for(hci_count, storage_count=0, nic_ports=2):
    """Build the network-diagram SVG for a topology described by counts. Shared by
    the recommendation export and the manual builder. Returns None if no nodes."""
    nodes = [{"name": f"Node {i+1}", "nics": nic_ports, "role": "hci"} for i in range(hci_count)]
    nodes += [{"name": f"Storage {i+1}", "nics": nic_ports, "role": "storage"} for i in range(storage_count)]
    if not nodes:
        return None
    try:
        return render_cluster_svg(nodes,
                                  witness=(hci_count == 2 and storage_count == 0),
                                  single_switch=(nic_ports <= 1))
    except Exception:
        return None


if __name__ == "__main__":
    # qlmanage squares the thumbnail, so pad to a square canvas for self-checks.
    cases = {
        "cluster_3node": dict(nodes=[{"name": f"Node {i+1}", "nics": 4} for i in range(3)],
                              title="3-Node Cluster"),
        "cluster_sns": dict(nodes=[{"name": "Single Node", "nics": 2}], title="Single Node System"),
        "cluster_2node_witness": dict(nodes=[{"name": f"Node {i+1}", "nics": 4} for i in range(2)],
                                      witness=True, title="2-Node + Witness"),
        "cluster_2nic": dict(nodes=[{"name": f"Node {i+1}", "nics": 2} for i in range(3)],
                             title="3-Node (2-NIC, backplane VLAN)"),
    }
    for name, kw in cases.items():
        svg = render_cluster_svg(canvas_h=1200, **kw)
        with open(f"/tmp/{name}.svg", "w") as f:
            f.write(svg)
        print(f"wrote /tmp/{name}.svg")
