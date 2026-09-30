"""A tiny PDF writer for tests: positioned Helvetica text, rules and fills,
and the hostile extras the certainty score must catch (invisible text,
JavaScript, extra pages). No dependency, so synthetic lookalikes of the real
partner PDFs (which are customer documents and stay out of the repo) can be
built at test time.

Coordinates are in points from the TOP-left, like the layouts are read.
"""


def _esc(text):
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class PdfMaker:
    def __init__(self, width=612, height=792):
        self.width, self.height = width, height
        self.pages = []
        self.javascript = None
        self.page()

    def page(self):
        self.pages.append([])
        return self

    def text(self, x, top, s, size=9, render=0, gray=None):
        """Text whose top sits at ``top``. ``render`` 3 = invisible;
        ``gray`` 1.0 = white fill colour."""
        y = self.height - top - size * 0.8
        ops = []
        if gray is not None:
            ops.append("%.2f g" % gray)
        ops.append("BT /F1 %.1f Tf %d Tr %.1f %.1f Td (%s) Tj ET" % (size, render, x, y, _esc(s)))
        if gray is not None:
            ops.append("0 g")
        self.pages[-1].append("\n".join(ops))
        return self

    def row(self, top, cells, size=9, **kw):
        """Several (x, text) pieces on one line."""
        for x, s in cells:
            if s != "":
                self.text(x, top, str(s), size=size, **kw)
        return self

    def hline(self, x0, x1, top):
        y = self.height - top
        self.pages[-1].append("0.5 w %.1f %.1f m %.1f %.1f l S" % (x0, y, x1, y))
        return self

    def fill(self, x0, top, x1, bottom, gray):
        self.pages[-1].append("%.2f g %.1f %.1f %.1f %.1f re f 0 g"
                              % (gray, x0, self.height - bottom, x1 - x0, bottom - top))
        return self

    def bytes(self):
        objs = []                                     # index i -> object i+1

        def add(body):
            objs.append(body)
            return len(objs)

        font = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
        pages_id = len(objs) + 1 + 2 * len(self.pages) + (1 if self.javascript else 0) + 1
        kids = []
        for ops in self.pages:
            stream = "\n".join(ops).encode("latin-1")
            content = add("<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream.decode("latin-1")))
            kids.append(add("<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] "
                            "/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
                            % (pages_id, self.width, self.height, font, content)))
        extra = ""
        if self.javascript:
            js = add("<< /Type /Action /S /JavaScript /JS (%s) >>" % _esc(self.javascript))
            extra = " /OpenAction %d 0 R" % js
        catalog = add("<< /Type /Catalog /Pages %d 0 R%s >>" % (pages_id, extra))
        assert add("<< /Type /Pages /Kids [%s] /Count %d >>"
                   % (" ".join("%d 0 R" % k for k in kids), len(kids))) == pages_id
        out = bytearray(b"%PDF-1.4\n")
        offsets = []
        for i, body in enumerate(objs, 1):
            offsets.append(len(out))
            out += ("%d 0 obj\n%s\nendobj\n" % (i, body)).encode("latin-1")
        xref = len(out)
        out += ("xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)).encode()
        for off in offsets:
            out += ("%010d 00000 n \n" % off).encode()
        out += ("trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
                % (len(objs) + 1, catalog, xref)).encode()
        return bytes(out)


# ── lookalikes of the partner layouts (fake data) ──────────────────────────────

DELL_SOLUTION_ROWS = [
    # (module heading, description, qty per server, option price, sku)
    ("Base", "PowerEdge R760 Server", 1, 5000.00, "210-BDZY"),
    ("Chassis Configuration", "3.5\" Chassis with up to 12 SAS/SATA Drives", 1, 300.00, "404-BBDS"),
    ("Processor", "Intel Xeon Silver 4514Y 2G, 16C/32T, 16GT/s, 30M Cache, Turbo,", 1, 1500.00, "338-CPBZ"),
    ("Memory Capacity", "16GB RDIMM, 5600MT/s, Single Rank", 8, 1600.00, "370-BBRQ"),
    ("RAID/Internal Storage Controllers", "HBA355i Adapter LP", 1, 1100.00, "405-AAZF"),
    ("Hard Drives", "1.92TB SSD SATA Read Intensive 6Gbps 512e 2.5in Hot-plug", 4, 4000.00, "400-AXRK"),
    ("OCP 3.0 Network Adapters", "Intel E810-XXV Dual Port 10/25GbE SFP28, OCP NIC 3.0", 1, 500.00, "540-BCXW"),
    ("Boot Optimized Storage Cards", "BOSS Blank", 1, 0.00, "329-BERC"),
]


def dell_solution(nodes=3, rows=DELL_SOLUTION_ROWS, unit_price=None, tamper=None):
    """A Dell Solutions Configurator print (English), one group."""
    pdf = PdfMaker()
    unit = sum(r[3] for r in rows) if unit_price is None else unit_price
    pdf.row(43, [(35, "Acme - 1234567.1"), (522, "List Price")])
    pdf.row(67, [(35, "General")])
    pdf.row(118, [(35, "Components")])
    pdf.row(142, [(36, "PowerEdge R760 -"), (205, "Category: Server"), (381, "Quantity: %d" % nodes),
                  (462, "Unit Price"), (506, "$%s" % format(unit, ",.2f"))])
    pdf.row(154, [(36, "[AMER_R760_15724_VI_VP]")])
    pdf.row(169, [(35, "Group Name: Group 1")])
    pdf.row(178, [(35, "Group ID: 1234567.1.1"), (411, "Extended Price (List)"),
                  (501, "$%s" % format(unit * nodes, ",.2f"))])
    pdf.row(202, [(37, "Product Name"), (341, "Qty"), (360, "Option List Price"), (452, "Option"), (498, "SKUs")])
    pdf.row(214, [(452, "ID")])
    top = 228
    for module, desc, qty, price, sku in rows:
        pdf.row(top, [(37, module)])
        pdf.row(top + 14.4, [(37, desc), (341, qty), (360, "$%s" % format(price, ",.2f")),
                             (452, "GXXXXXX"), (498, sku)])
        top += 32.4
    if tamper:
        tamper(pdf, top)
    pdf.row(top + 20, [(35, "* Pricing in the Solutions Configurator is based on the Dell list price.")])
    return pdf


def lenovo_build(hosts=3):
    pdf = PdfMaker()
    pdf.row(51, [(370, "HC5650D Build Order")])
    pdf.row(75, [(123, "Acme"), (359, "%d Hosts | Generated: March 18, 2026" % hosts)])
    pdf.row(109, [(43, "CPU: 2x Intel Xeon 6507P 8C 150W 3.5GHz Processor | RAM: 16x ThinkSystem 16GB "
                       "RDIMM | Storage: 3x 12TB SAS HDD + 1x 3.84TB NVMe | NIC: Broadcom 57504")], size=7)
    pdf.fill(35, 166, 580, 182, 0.2)
    pdf.row(173, [(40, "Feature Code"), (126, "Description"), (378, "Lenovo Part"), (436, "Notes"),
                  (551, "Qty")], gray=1.0)
    rows = [
        ("section", "FIXED PARTS - ThinkSystem SR650 V4 (x%d hosts)" % hosts),
        ("C3QL", "ThinkSystem SR650 V4 12x3.5\" Chassis (Base Server)", "7DGDCTO1WW", "", hosts),
        ("BM50", "ThinkSystem 440-16i SAS/SATA PCIe Gen4 12Gb HBA", "4Y37A78602", "", hosts),
        ("section", "SELECTED OPTIONS (x%d hosts)" % hosts),
        ("section", "Processor"),
        ("C5R6", "Intel Xeon 6507P 8C 150W 3.5GHz Processor", "", "2x per host (dual socket)", 2 * hosts),
        ("section", "Memory"),
        ("C0U2", "ThinkSystem 16GB TruDDR5 6400MHz (1Rx8) RDIMM", "4X77A90963", "16x per host = 256GB",
         16 * hosts),
        ("section", "Network Interface Card"),
        ("BPPW", "ThinkSystem Broadcom 57504 10/25GbE SFP28 4-Port OCP Ethernet Adapter", "4XC7A80567",
         "SFP28 OCP slot", hosts),
        ("section", "Storage"),
        ("C4DA", "ThinkSystem 3.5\" 12TB 7.2K SAS 12Gb Hot Swap 512e HDD v2", "4XB7A93788",
         "3x per host", 3 * hosts),
        ("CFA5", "ThinkSystem 3.5\" U.2 VA 3.84TB Read Intensive NVMe PCIe 5.0 x4 HS SSD", "",
         "1x per host", hosts),
    ]
    top = 184
    pdf.hline(35, 580, top)
    for row in rows:
        if row[0] == "section":
            pdf.row(top + 5, [(40, row[1])])
            top += 16
        elif len(row[1]) <= 44:
            code, desc, part, notes, qty = row
            pdf.row(top + 5, [(40, code), (126, desc), (378, part), (436, notes[:24]), (556, qty)], size=8)
            top += 16
        else:
            # A wrapped cell, vertically centred like ReportLab draws it: the
            # code and quantity sit on the middle line of the description.
            code, desc, part, notes, qty = row
            cut = desc.rfind(" ", 0, 44)
            pdf.row(top + 3, [(126, desc[:cut])], size=8)
            pdf.row(top + 8, [(40, code), (378, part), (436, notes[:24]), (556, qty)], size=8)
            pdf.row(top + 13, [(126, desc[cut + 1:])], size=8)
            top += 26
        pdf.hline(35, 580, top)
    pdf.row(top + 12, [(42, "Note: Quantities shown are for the full cluster order.")], size=7)
    return pdf


def dell_quote(nodes=1):
    pdf = PdfMaker()
    pdf.row(30, [(20, "Subject: Your Dell Quote 3000200000000.1 | Acme Box")])
    pdf.row(90, [(109, "Product"), (356, "Unit Price"), (419, "Quantity"), (509, "Subtotal")])
    pdf.row(112, [(109, "PowerEdge R760 -"), (351, "$10,000.00"), (437, nodes), (499, "$%s" % format(10000.0 * nodes, ",.2f"))])
    pdf.row(125, [(109, "[amer_r760_15724_vi_vp]")])
    pdf.row(155, [(355, "Subtotal:"), (499, "$%s" % format(10000.0 * nodes, ",.2f"))])
    pdf.row(410, [(398, "Unit Price"), (452, "Quantity"), (521, "Subtotal")])
    pdf.row(425, [(87, "PowerEdge R760 - [amer_r760_15724_vi_vp]"), (395, "$10,000.00"), (470, nodes),
                  (511, "$%s" % format(10000.0 * nodes, ",.2f"))])
    pdf.row(438, [(87, "Estimated delivery if purchased today:")])
    pdf.row(470, [(87, "Description"), (361, "SKU"), (398, "Unit Price"), (452, "Quantity"), (521, "Subtotal")])
    rows = [("PowerEdge R760 Server", None, "210-BDZY", 1),
            ("Intel Xeon Gold 5415+ 2.9G, 8C/16T, 16GT/s,", "22.5M Cache, Turbo, HT (150W) DDR5-4400", "338-CHSS", 1),
            ("Intel Xeon Gold 5415+ 2.9G, 8C/16T, 16GT/s,", "22.5M Cache, Turbo, HT (150W) DDR5-4400", "338-CHSS", 1),
            ("32GB RDIMM, 6400MT/s, Dual Rank", None, "370-BCCY", 12),
            ("Dell HBA355i Adapter, Low Profile", None, "405-AAZF", 1),
            ("4TB Hard Drive SAS ISE 12Gbps 7.2K 512n 3.5in", "Hot-Plug, AG Drive", "161-BCPH", 9),
            ("Intel E810-XXVDA4 Quad Port 10/25GbE SFP28", "Adapter, OCP NIC 3.0", "540-BDDU", 1)]
    top = 487
    for desc, wrap, sku, qty in rows:
        pdf.row(top, [(87, desc), (334, sku), (443, "-"), (470, qty), (559, "-")])
        if wrap:
            pdf.row(top + 12, [(87, wrap)])
            top += 12
        top += 18.4
    pdf.row(top + 10, [(430, "Subtotal:"), (511, "$%s" % format(10000.0 * nodes, ",.2f"))])
    return pdf


def supermicro_quote(nodes=3):
    pdf = PdfMaker()
    pdf.row(56, [(3, "Customer Name"), (107, "Acme")])
    pdf.row(129, [(3, "Product"), (107, "Part / Project Description"), (477, "Qty"), (520, "Unit Price"),
                  (568, "Total Price")], size=7)
    pdf.row(140, [(107, "511R-M-OTO-17"), (480, nodes), (518, "$20,000.00"),
                  (568, "$%s" % format(20000.0 * nodes, ",.2f"))], size=7)
    lines = ["OPTIMIZED UP 1U X13SCH-SYS",
             "SYS-511R-M(x%d)UP 1U X13SCH-SYS" % nodes,
             "P4X-UPE2434-SRMXC(x%d)RPL-E E-2434 1P 4C/8T 3.4G" % nodes,
             "MEM-DR532MD-EU56(x%d)32GB DDR5-5600 2RX8 ECC UDIMM" % (4 * nodes),
             "AOC-S3808L-L8IT-P(x%d)8 int 12Gb/s SAS3 ports HBA" % nodes,
             "HDS-25T0-001T9-M1-TXE-NON-007(x%d)SSD 2.5\" SATA 1.9TB" % (4 * nodes),
             "AOC-STG-I4T-P(x%d)Std LP 4-port 10G RJ45, Intel XL710" % nodes]
    top = 149
    for text in lines:
        pdf.row(top, [(107, text)], size=7)
        top += 8.8
    pdf.row(top + 10, [(3, "FREIGHT_CHARGE"), (107, "Freight charges calculated at time of shipping")], size=7)
    pdf.row(top + 26, [(469, "Sub-Total"), (514, "$"), (560, format(20000.0 * nodes, ",.2f"))], size=7)
    return pdf


def lenovo_list(machines=3, gap_at=None):
    """A DCSC configuration retyped as a numbered, ruled table. ``gap_at``
    leaves one S/N out (a row the reader lost)."""
    pdf = PdfMaker()
    pdf.row(60, [(54, "S/N"), (79, "Part Number"), (227, "Description"), (369, "Qty")], size=7)
    pdf.row(72, [(187, "Lenovo Server - 12C")], size=7)
    rows = [("7D73CTO1WW", "Server : ThinkSystem SR630 V3-3yr Base Warranty", 1),
            ("BLK3", "ThinkSystem V3 1U 4x3.5\" Chassis", 1),
            ("BQ67", "Intel Xeon Silver 4410Y 12C 150W 2.0GHz Processor", 1),
            ("BKTM", "ThinkSystem 32GB TruDDR5 4800MHz (2Rx8) RDIMM", 4),
            ("B8NY", "ThinkSystem RAID 940-8i 4GB Flash PCIe Gen4 12Gb Adapter", 1),
            ("C5X9", "ThinkSystem 3.5\" 4TB 7.2K SAS 12Gb Hot Swap 512e HDD v2", 4),
            ("BPPY", "ThinkSystem Intel X710-T4L 10GBase-T 4-Port OCP Ethernet Adapter", 1),
            ("7S0XCTO5WW", "XClarity Controller Platin-FOD", 1),
            ("SBCV", "Lenovo XClarity XCC2 Platinum Upgrade (FOD)", 1),
            ("7Q01CTS2WW", "SERVER PREMIER NBD RESP", 1),
            ("QA0Y", "Months", 36)]
    top = 80
    pdf.hline(50, 380, top - 1)
    sn = 0
    for part, desc, per_node in rows:
        sn += 1
        if gap_at == sn:
            sn += 1
        pdf.row(top, [(58, sn), (80, part), (140, desc), (369, per_node * machines)], size=7)
        top += 8
        pdf.hline(50, 380, top - 1)
    return pdf


def dell_arrow(nodes=3, total=None):
    """The Arrow quote sheet with a Dell system as module/option rows."""
    pdf = PdfMaker()
    unit = 20000.0
    pdf.row(56, [(3, "Customer Name"), (107, "Acme")], size=7)
    pdf.row(120, [(3, "Product"), (107, "Part / Project Description"), (477, "Qty"), (520, "Unit Price"),
                  (568, "Total Price")], size=7)
    pdf.row(131, [(107, "PowerEdge R760xs [EMEA_R760XS] (210-BGLV)"), (480, nodes), (513, "EUR"),
                  (526, format(unit, ",.2f")), (560, "EUR"),
                  (579, format(total if total is not None else unit * nodes, ",.2f"))], size=7)
    rows = [("Base", "PowerEdge R760xs", 1),
            ("Processor", "Intel Xeon Gold 5418Y 2G, 24C/48T, 16GT/s, 45M Cache", 1),
            ("Additional Processor", "Intel Xeon Gold 5418Y 2G, 24C/48T, 16GT/s, 45M Cache", 1),
            ("Memory Capacity", "32GB RDIMM, 6400MT/s, Dual Rank", 8),
            ("RAID Controller", "PERC H355 Adapter, Low Profile", 1),
            ("Hard Drives", "4TB Hard Drive SAS ISE 12Gbps 7.2K 512n 3.5in Hot-Plug", 3),
            ("BIOS and Advanced System", None, None),
            ("Configuration Settings", "Performance BIOS Settings", 1),
            ("OCP 3.0 Network Adapters", "Broadcom 57504 Quad Port 10/25GbE, SFP28, OCP 3.0 NIC", 1)]
    top = 140
    for module, desc, qty in rows:
        pdf.row(top, [(3, module)] + ([(107, desc), (480, qty)] if desc else []), size=7)
        top += 8.8
    pdf.row(top + 10, [(3, "FREIGHT_CHARGE"), (107, "Freight charges calculated at time of shipping")], size=7)
    pdf.row(top + 26, [(469, "Sub-Total"), (514, "EUR"), (579, format(unit * nodes, ",.2f"))], size=7)
    return pdf


def scale_quotation():
    pdf = PdfMaker()
    pdf.row(40, [(400, "Quotation")])
    pdf.row(60, [(20, "Scale Computing")])
    pdf.row(200, [(20, "PART #"), (120, "PRODUCT"), (340, "QTY"), (400, "MSRP/UNIT")])
    pdf.row(215, [(20, "CHA-4-1D"), (120, "HE153 Chassis"), (340, "3"), (400, "EUR 1,588.00")])
    return pdf
