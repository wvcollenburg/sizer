#!/usr/bin/env python
"""Generate the synthetic BOM fixtures under tests/fixtures/bom/.

The 26 real BOMs SC//Design collected are customer documents and stay in the
gitignored _archive/. Every layout the deterministic parsers recognise is
reproduced here with fake customer / config names, fake prices and the same
structural quirks the survey (report_formats.md §1) found in the real files:
the Lenovo DCSC blank-row blocks with int *and* float quantities, the Dell
service-tag 30-character wrap and its numeric-looking piece parts, the Dell
quote's repeated SKU header before a second group, the letter variant with
the header on row 12, the D&H champion line, the VNET module names, the three
hand-typed Dell lists and our own strict template.

Run (from the repo root):

    .venv/bin/python tests/fixtures/bom/make_fixtures.py
    .venv/bin/python tests/fixtures/bom/make_fixtures.py --write-normalized

The first form (re)writes the spreadsheets and prints what detect_format /
parse_file make of each one. The second additionally rewrites
normalized/<name>.json from the parser output — only do that after checking
the printed parse by hand against the sheet you changed; the JSON files are
the *reviewed* expectations tests/test_bom_parsers.py holds the parsers to.

Output is byte-for-byte reproducible: workbook timestamps are pinned and the
zip entries are rewritten with a fixed date so re-running never churns git.
"""
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
sys.path.insert(0, os.path.join(ROOT, 'app'))

from openpyxl import Workbook  # noqa: E402

FIXED_TIME = dt.datetime(2026, 9, 1, 12, 0, 0)
ZIP_DATE = (2026, 9, 1, 12, 0, 0)


# ─── helpers ──────────────────────────────────────────────────────────────────

_STAMP = re.compile(rb'(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:)')
_FIXED_STAMP = FIXED_TIME.strftime('%Y-%m-%dT%H:%M:%SZ').encode()


def _deterministic_zip(data):
    """Rewrite an xlsx (zip) with a fixed entry date and pinned document
    timestamps (openpyxl stamps 'modified' with now() on every save) so the
    bytes are stable across runs."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            zi = zipfile.ZipInfo(info.filename, date_time=ZIP_DATE)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = info.external_attr
            payload = src.read(info.filename)
            if info.filename == 'docProps/core.xml':
                payload = _STAMP.sub(lambda m: m.group(1) + _FIXED_STAMP + m.group(2), payload)
            dst.writestr(zi, payload)
    return out.getvalue()


def save_wb(wb, name):
    wb.properties.created = FIXED_TIME
    wb.properties.modified = FIXED_TIME
    wb.properties.creator = 'make_fixtures.py'
    wb.properties.lastModifiedBy = 'make_fixtures.py'
    buf = io.BytesIO()
    wb.save(buf)
    save_bytes(_deterministic_zip(buf.getvalue()), name)


def save_bytes(data, name):
    with open(os.path.join(HERE, name), 'wb') as fh:
        fh.write(data)


def put(ws, row, cells):
    """cells: {'A': value, 'C': value, ...} on one row."""
    for col, value in cells.items():
        ws['%s%d' % (col, row)] = value


def wrap30(text):
    """Reproduce the Dell service-tag exporter's hard wrap: 30-character
    chunks, each right-trimmed, joined with a single space."""
    chunks = [text[i:i + 30].rstrip() for i in range(0, len(text), 30)]
    return ' '.join(chunks)


# ─── Lenovo DCSC ──────────────────────────────────────────────────────────────

_DCSC_TITLE = 'Data Center Solution Configurator Quote'

PROD_CHILDREN = [
    # (feature code, description, qty)  — totals across the 3 machines
    ('Z001', 'ThinkSystem SR650 V4 12x3.5" Chassis', 3),
    ('Z002', 'Intel Xeon 6517P 16C 190W 3.2GHz Processor', 6),
    ('Z003', 'ThinkSystem 32GB TruDDR5 6400MHz (2Rx8) RDIMM', 48),
    ('5977', 'Select Storage devices - no configured RAID required', 3),
    ('Z004', 'ThinkSystem 440-16i SAS/SATA PCIe Gen4 12Gb HBA', 3),
    ('Z005', 'ThinkSystem 2.5" U.2 VA 3.84TB Read Intensive NVMe PCIe 5.0 x4 HS SSD', 9),
    ('Z006', 'ThinkSystem 3.5" 12TB 7.2K SAS 12Gb Hot Swap 512e HDD v2', 27),
    ('Z007', 'ThinkSystem SR650 V4 12x3.5" SAS/SATA + 4x AnyBay Backplane', 3),
    ('Z008', 'SR650 V4/SR630 V4 x16 OCP Cable Kit', 3),
    ('Z008', 'SR650 V4/SR630 V4 x16 OCP Cable Kit', 3),          # duplicate FC row → summed
    ('Z009', 'ThinkSystem Broadcom 57504 10/25GbE SFP28 4-Port OCP Ethernet Adapter', 3),
    ('Z010', 'ThinkSystem 1100W 230V Titanium Hot-Swap Gen2 Power Supply v4', 6),
    ('Z011', 'ThinkSystem SR650 V4 Performance Fan Module', 18),
    ('Z012', 'ThinkSystem Toolless Slide Rail Kit v2', 3),
    ('Z013', 'ThinkSystem Trusted Platform Module 2.0 V6', 3),
    ('Z014', 'ThinkSystem Memory Dummy', 48),                    # dropped
    ('Z015', 'ThinkSystem 3.5" HDD Filler', 9),                  # dropped
    ('Z016', 'ThinkSystem 2U V4 Performance Heatsink', 6),
    ('Z017', 'XClarity Controller 3 Premier Upgrade - FOD', 3),  # dropped
]

DR_CHILDREN = [
    ('Z001', 'ThinkSystem SR650 V4 12x3.5" Chassis', 1.0),
    ('Z002', 'Intel Xeon 6517P 16C 190W 3.2GHz Processor', 2.0),
    ('Z003', 'ThinkSystem 32GB TruDDR5 6400MHz (2Rx8) RDIMM', 16.0),
    ('Z004', 'ThinkSystem 440-16i SAS/SATA PCIe Gen4 12Gb HBA', 1.0),
    ('Z005', 'ThinkSystem 2.5" U.2 VA 3.84TB Read Intensive NVMe PCIe 5.0 x4 HS SSD', 3.0),
    ('Z006', 'ThinkSystem 3.5" 12TB 7.2K SAS 12Gb Hot Swap 512e HDD v2', 9.0),
    ('Z009', 'ThinkSystem Broadcom 57504 10/25GbE SFP28 4-Port OCP Ethernet Adapter', 1.0),
    ('Z010', 'ThinkSystem 1100W 230V Titanium Hot-Swap Gen2 Power Supply v4', 2.0),
    ('Z011', 'ThinkSystem SR650 V4 Performance Fan Module', 6.0),
]


def _dcsc_head(ws, labels, currency='US Dollar'):
    put(ws, 1, {'C': _DCSC_TITLE})
    put(ws, 2, {'C': 'Prepared for:', 'D': 'Prepared by:', 'E': 'Jane Partner', 'H': 'Quote #:'})
    put(ws, 3, {'D': 'Price Date:', 'E': '01-Sep-26', 'H': 'Config ID:', 'I': 'CIDU00SYN-00'})
    put(ws, 6, dict(zip('ACEFGH', labels)))
    put(ws, 7, {'F': '(per unit)\n' + currency, 'G': '(quantity x unit price)\n' + currency})


def _dcsc_block(ws, row, sku, title, qty, children, price=0.01, child_price=0):
    put(ws, row, {'A': sku, 'C': title, 'E': qty, 'F': price})
    row += 1
    for fc, desc, q in children:
        cells = {'A': fc, 'C': desc, 'E': q}
        if child_price is not None:
            cells['F'] = child_price
        put(ws, row, cells)
        row += 1
    return row + 1        # one blank separator row


def _dcsc_tail(ws, row, total, terms='TERMS AND CONDITIONS:'):
    put(ws, row, {'F': 'Total', 'G': total})
    put(ws, row + 2, {'A': terms})
    put(ws, row + 3, {'A': 'This quotation is for the synthetic Acme fixtures only and carries no legal meaning.'})


def make_lenovo_dcsc():
    wb = Workbook()
    ws = wb.active
    ws.title = 'Quote'
    _dcsc_head(ws, ('Part number', 'Product Description', 'Qty', 'Price',
                    'Total Part Price', 'Export Control'))
    r = _dcsc_block(ws, 9, '7ZZ9CTO1WW',
                    'Acme PROD : ThinkSystem SR650 V4-3yr Base Warranty', 3, PROD_CHILDREN)
    # Software block between the machines → folded into Acme PROD as 'other'.
    r = _dcsc_block(ws, r, '7S0NCTO1WW', 'Scale Computing Software', 1,
                    [('SC7H', 'SC//HyperCore - 1 Site, 1-5 Workloads, 5-year License', 3)])
    # Services block → dropped wholesale (not a CTO code).
    r = _dcsc_block(ws, r, '7Q01CTSAWW', 'Lenovo Services', 3,
                    [('QAJY', 'SR650 V4', 3), ('QA0Y', 'Months', 108)])
    r = _dcsc_block(ws, r, '7ZZ9CTO1WW',
                    'Acme DR : ThinkSystem SR650 V4-3yr Base Warranty', 1.0, DR_CHILDREN,
                    child_price=None)
    _dcsc_tail(ws, r, 12345.67)

    ws_sum = wb.create_sheet('Summary')
    _dcsc_head(ws_sum, ('Part number', 'Product Description', 'Qty', 'Price',
                        'Total Part Price', 'Export Control'))
    put(ws_sum, 9, {'A': '7ZZ9CTO1WW', 'C': 'Acme PROD : ThinkSystem SR650 V4-3yr Base Warranty', 'E': 3})
    put(ws_sum, 11, {'A': '7S0NCTO1WW', 'C': 'Scale Computing Software', 'E': 1})
    put(ws_sum, 13, {'A': '7ZZ9CTO1WW', 'C': 'Acme DR : ThinkSystem SR650 V4-3yr Base Warranty', 'E': 1})
    put(ws_sum, 15, {'F': 'Total', 'G': 12345.67})

    ws_pow = wb.create_sheet('Power Report')
    put(ws_pow, 6, {'A': 'Machine Name', 'B': 'Acme PROD', 'D': 'Model', 'E': '7ZZ9CTO1WW',
                    'F': 'Quantity', 'G': 3})
    put(ws_pow, 7, {'A': 'Input Power (W)', 'B': 1210.5})
    put(ws_pow, 14, {'A': 'Machine Name', 'B': 'Acme DR', 'D': 'Model', 'E': '7ZZ9CTO1WW',
                     'F': 'Quantity', 'G': 1.0})
    put(ws_pow, 15, {'A': 'Input Power (W)', 'B': 1180.0})
    save_wb(wb, 'synthetic_lenovo_dcsc.xlsx')


PTBR_CHILDREN = [
    ('Z101', 'Chassi ThinkSystem SR650 V4 de 2,5" 24 compartimentos', 3.0),
    ('Z102', 'Processador Intel Xeon 6505P 12C 150 W 2,2 GHz', 6.0),
    ('Z103', 'RDIMM ThinkSystem de 16 GB TruDDR5 6.400 MHz (1Rx8)', 48.0),
    ('Z104', 'HBA ThinkSystem 440-16i SAS/SATA PCIe Gen4 12 Gb', 3.0),
    ('Z105', 'Unidade de estado sólido HS ThinkSystem 2,5" U.2 VA 3,84 TB de uso intenso '
             'de leitura NVMe PCIe 5.0 x4', 9.0),
    ('Z106', 'Unidade de disco rígido hot-swap 512e ThinkSystem 3,5 pol. 12 TB 7,2K SAS 12 Gb v2', 27.0),
    ('Z107', 'Adaptador Ethernet ThinkSystem Broadcom 57504 10/25 GbE SFP28 de 4 Portas PCIe', 3.0),
    ('Z108', 'Fonte de alimentação hot-swap ThinkSystem 1100 W 230 V Titanium v4', 6.0),
    ('Z109', 'Ventilador de desempenho ThinkSystem SR650 V4', 18.0),
    ('Z110', 'Cabo SAS ThinkSystem SR650 V4', 3.0),
    ('Z111', 'Preenchimento de HDD ThinkSystem de 3,5"', 9.0),              # dropped
    ('Z112', 'Etiqueta de tipo de HDD', 3.0),                                # dropped
    ('Z113', 'Apenas registro', 3.0),                                        # dropped
    ('Z114', 'Dispositivos de armazenamento - nenhum RAID configurado necessário', 3.0),
    ('Z115', 'Kit de trilho deslizante sem ferramentas ThinkSystem v2', 3.0),
]


def make_lenovo_dcsc_ptbr():
    wb = Workbook()
    ws = wb.active
    ws.title = 'Cotação'
    _dcsc_head(ws, ('Número de peça', 'Descrição do Produto', 'Qtd', 'Preço',
                    'Preço total da peça', 'Controlo das exportações'), currency='Real brasileiro')
    put(ws, 3, {'E': '01-set-26'})
    r = _dcsc_block(ws, 9, '7ZZ9CTO1WW',
                    'Servidor : ThinkSystem SR650 V4-3yr Base Warranty', 3.0, PTBR_CHILDREN,
                    price=645266.8169, child_price=None)
    _dcsc_tail(ws, r, 645266.8169, terms='TERMOS E CONDIÇÕES:')
    ws_sum = wb.create_sheet('Resumo')
    _dcsc_head(ws_sum, ('Número de peça', 'Descrição do Produto', 'Qtd', 'Preço',
                        'Preço total da peça', 'Controlo das exportações'), currency='Real brasileiro')
    put(ws_sum, 9, {'A': '7ZZ9CTO1WW', 'C': 'Servidor : ThinkSystem SR650 V4-3yr Base Warranty', 'E': 3.0})
    put(ws_sum, 11, {'F': 'Total', 'G': 645266.8169})
    save_wb(wb, 'synthetic_lenovo_dcsc_ptbr.xlsx')


BOSS_CHILDREN = [
    ('Z201', 'ThinkSystem SR650 V4 2.5" Chassis', 3.0),
    ('Z202', 'Intel Xeon 6505P 12C 150W 2.2GHz Processor', 3.0),
    ('Z203', 'ThinkSystem 16GB TruDDR5 6400MHz (1Rx8) RDIMM', 24.0),
    ('Z204', 'ThinkSystem M.2 RAID B540p-2HS SATA/NVMe Adapter', 3.0),
    ('Z205', 'ThinkSystem M.2 VA 480GB Read Intensive NVMe PCIe 4.0 x4 NHS SSD', 6.0),
    ('Z206', 'ThinkSystem 2.5" U.2 VA 3.84TB Read Intensive NVMe PCIe 5.0 x4 HS SSD', 12.0),
    ('Z207', 'ThinkSystem Broadcom 57504 10/25GbE SFP28 4-Port OCP Ethernet Adapter', 3.0),
    ('Z208', 'ThinkSystem 2U V4 Standard Fan Module', 15.0),
    ('Z209', 'ThinkSystem 750W 230V Titanium Hot-Swap Gen2 Power Supply', 6.0),
]


def make_lenovo_dcsc_boss():
    wb = Workbook()
    ws = wb.active
    ws.title = 'Quote'
    _dcsc_head(ws, ('Part number', 'Product Description', 'Qty', 'Price',
                    'Total Part Price', 'Export Control'), currency='British Pound')
    r = _dcsc_block(ws, 9, '7ZZ9CTO1WW',
                    'HC3650F-SYN : ThinkSystem SR650 V4-3yr Base Warranty', 3.0, BOSS_CHILDREN,
                    price=0.009, child_price=0.009)
    _dcsc_tail(ws, r, 9876.54)
    save_wb(wb, 'synthetic_lenovo_dcsc_boss.xlsx')


# ─── Dell service-tag export ─────────────────────────────────────────────────

# (option, [(piece part, piece description, qty), ...]) in the export's
# descending-SKU order. The FIRST piece sits on the option row itself.
SERVICE_TAG_GROUPS = [
    ('817-BBBB : Custom Configuration',
     [('W21JJ', 'INFO,GNRC,OEM,TRACKING,PN', 1)]),
    ('800-BBDM : UEFI BIOS Boot Mode with GPT Partition',
     [('0616F', 'INFO,BOOT,GPT,OVERRIDE', 1), ('XP9T9', 'INFO,BOOT,CNTNR,GPT', 1)]),
    ('780-BCDI : No RAID',
     [('0M1FT', 'INFO,NO RAID', 1)]),
    ('540-BCXW : Intel E810-XXV Dual Port 10/25GbE SFP28, OCP NIC 3.0',
     [('4V7D7', 'DSK PROG,DIAGS,E810,OCP', 1), ('1KP0N', 'CRD,NTWK,OCP,DP,25G,E810,LP', 1)]),
    ('450-AKYB : Dual, Hot Plug, Power Supply (1+1) Redundant 1400W 2U',
     [('CMPGM', 'PWR SPLY,1400,RDNT,LTON', 2)]),
    ('405-AAZF : Dell HBA355i Adapter, Low Profile',
     [('T4FH6', 'ASSY,CRD,CTL,HBA355I,LP', 1), ('9YKMY', 'ASSY,CBL,FPERC,C1,X8,NV,R760', 1)]),
    ('403-BCID : No BOSS Card',
     [('8RJ7X', 'INFO,NO BOSS', 1)]),
    ('400-BKGJ : 3.84TB Enterprise NVMe Read Intensive AG Drive U.2 Gen4 Flex Bay',
     [('DMF5Y', 'SSDR,3.84T,NVME,RI,U.2,AG,EC', 3),
      ('R9445', 'Screw,M3X.05X4.5MM,Flat Head  Machine Screw,Zinc Plated     Steel', 12),
      ('NTPP3', 'Assembly,Carrier,Hard Drive, PLSTC, U.2, 2.5', 3),
      ('FM833', 'LBL,POD,HD,RECTANGULAR', 3)]),
    ('161-BCPX : 8TB Hard Drive SAS 12Gbps 7.2K 512e 3.5in Hot-Plug, AG Drive',
     [('4K8HW', 'HD,8T,722E,IS12,3.5,T-MG,EC', 1)] * 9 + [('WH5D2', 'ASSY,CARR,HD,3.5,V3', 9)]),
    ('370-BBRY : 32GB RDIMM, 5600MT/s, Dual Rank',
     [('CPC7G', 'DIMM,32GB,5600,2RX8,DDR5,R', 16)]),
    ('370-ABWF : DIMM Blanks for System with 2 Processors',
     [('21PJD', 'Filler,Blank,Dual In-Line Memory Module,Processor,R760', 16)]),
    ('338-CPBV : Intel Xeon Gold 6526Y 2.8G, 16C/32T, 20GT/s, 37.5M Cache, Turbo, HT (195W) DDR5-5200',
     [('C06J5', 'PRC,6526Y,2.8G,EMR,16C,195W,M1', 2)]),
    ('340-DCEP : PowerEdge R760 Shipping',
     [('9C7YV', 'SHP MTL,CTN,R760,2U,15G', 1), ('YRD6N', 'GDE,SETUP,R760,DAO', 1),
      ('MVW0N', 'PREP MTL,MOD,BOX,2U', 1), ('08123', 'INFO,NO DELL DIRECT CATALOG', 1)]),
    ('329-BHOF : PowerEdge R760 Motherboard with Broadcom 5720 Dual Port 1Gb On-Board LOM',
     [('P9WP6', 'ASSY,PWA,PLN,R760,LOM', 1)]),
    ('321-BGZI : 2.5" Chassis with up to 16 NVMe Drives, Dual Controller, RAID Config',
     [('RJ8J9', 'Assembly,Filler,Blank,Hard Drive,2.5,14G', 13),
      ('FYK80', 'ASSY,CHAS,0/16/24,L5, R76X,LTN', 1),
      ('YD2C2', 'ASSY,PWA,BKPLN,2U,8X2.5,LL,15G', 1)]),
    ('210-BDZY : PowerEdge R760 Server',
     [('2WN6D', 'INSTR,TRIG,SVC TAG,R760', 1), ('C5V2M', 'SRV,SW,BIOS,R760', 1)]),
]


def _service_tag_records():
    """(component, piece, description, qty) rows as the export writes them:
    the option text hard-wrapped every 30 characters."""
    rows = []
    for option, pieces in SERVICE_TAG_GROUPS:
        sku, _, text = option.partition(' : ')
        comp = '%s : %s' % (sku, wrap30(text))
        for i, (pn, desc, qty) in enumerate(pieces):
            rows.append((comp if i == 0 else '', pn, desc, qty))
    return rows


def make_dell_servicetag_csv():
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator='\n')
    writer.writerow(['Component', 'Part Number', 'Description', 'Quantity'])
    for comp, pn, desc, qty in _service_tag_records():
        writer.writerow([comp, pn, desc, str(qty)])
    save_bytes(buf.getvalue().encode('utf-8'), 'synthetic_dell_servicetag.csv')


def make_dell_servicetag_xlsx():
    wb = Workbook()
    ws = wb.active
    ws.title = 'ABC1234'
    ws.append(['Component', 'Part Number', 'Description', 'Quantity'])
    ws.append([])
    for comp, pn, desc, qty in _service_tag_records():
        # numeric-looking piece parts come back as numbers, quantities as floats
        pn_cell = int(pn) if pn.isdigit() else pn
        ws.append([comp or None, pn_cell, desc, float(qty)])
    save_wb(wb, 'synthetic_dell_servicetag.xlsx')


# ─── Dell quote export ───────────────────────────────────────────────────────

PROD_ITEMS = [
    # (SKU, description, qty as string — totals for the 3 systems)
    ('210-BDZY', 'PowerEdge R760 Server', '3'),
    ('461-AAIG', 'Trusted Platform Module 2.0 V3', '3'),
    ('321-BGZI', '2.5" Chassis with up to 16 NVMe Drives, Dual Controller, RAID Config', '3'),
    ('338-CPBV', 'Intel Xeon Gold 6526Y 2.8G, 16C/32T, 20GT/s, 37.5M Cache, Turbo, HT (195W) DDR5-5200', '3'),
    ('379-BDCO', 'Additional Processor Selected', '3'),
    ('338-CPBV', 'Intel Xeon Gold 6526Y 2.8G, 16C/32T, 20GT/s, 37.5M Cache, Turbo, HT (195W) DDR5-5200', '3'),
    ('379-BFFD', 'No HBM', '3'),
    ('412-ABCP', 'Heatsink for 2 CPU configuration (CPU greater than 165W)', '3'),
    ('370-AAIP', 'Performance Optimized', '3'),
    ('370-BCCX', '6400MT/s RDIMMs', '3'),
    ('780-BCDI', 'No RAID', '3'),
    ('405-AAZF', 'Dell HBA355i Adapter, Low Profile', '3'),
    ('750-ACFR', 'Front PERC Mechanical Parts, left and right', '3'),
    ('384-BBBL', 'Performance BIOS Settings', '3'),
    ('800-BBDM', 'UEFI BIOS Boot Mode with GPT Partition', '3'),
    ('750-ADGJ', 'Standard Fan x6 V3', '3'),
    ('450-AKYB', 'Dual, Hot Plug, Power Supply (1+1) Redundant 1400W 2U', '3'),
    ('450-AAGG', 'No Power Cord', '3'),
    ('330-BBXJ', 'Riser Config 1, 2x16 LP', '3'),
    ('329-BKCH', 'PowerEdge R760 Motherboard with ONLY CPUs below 250W supported, MLK', '3'),
    ('540-BCXW', 'Intel E810-XXV Dual Port 10/25GbE SFP28, OCP NIC 3.0', '3'),
    ('540-BDOW', 'LOM Blank', '3'),
    ('470-AEYU', 'No Cables Required', '3'),
    ('321-BHMY', 'Luggage Tag', '3'),
    ('325-BEVH', 'PowerEdge 2U Standard Bezel', '3'),
    ('329-BERC', 'Assembly BOSS Blank', '3'),
    ('350-BCEL', 'Quick Sync 2 (At-the-box mgmt)', '3'),
    ('379-BCQV', 'iDRAC,Factory Generated Password', '3'),
    ('611-BBBF', 'No Operating System', '3'),
    ('605-BBFN', 'No Media Required', '3'),
    ('528-CTIC', 'iDRAC9, Enterprise 16G', '3'),
    ('770-BCJI', 'ReadyRails Sliding Rails With Cable Management Arm', '3'),
    ('631-ADAJ', 'No Systems Documentation, No OpenManage DVD Kit', '3'),
    ('340-DCEP', 'PowerEdge R760 Shipping', '3'),
    ('343-BBRR', 'PowerEdge R760 CE, CCC, BIS Marking', '3'),
    ('817-BBBB', 'None Required', '3'),
    ('709-BBFM', 'Parts Only Warranty 12 Months', '3'),
    ('865-BBMY', 'ProSupport and Next Business Day Onsite Service Initial, 36 Month(s)', '3'),
    ('883-BBFN', 'Basic Deployment, Dell Server', '3'),
    # multi-quantity items come after the services, as in the real export
    ('370-BBRY', '32GB RDIMM, 5600MT/s, Dual Rank', '96'),
    ('161-BCPX', '8TB Hard Drive SAS 12Gbps 7.2K 512e 3.5in Hot-Plug, AG Drive', '27'),
    ('400-BKGJ', '3.84TB Enterprise NVMe Read Intensive AG Drive U.2 Gen4 Flex Bay', '9'),
    ('450-AALV', 'C13 to C14, PDU Style, 10 AMP, 6.5 Feet (2m), Power Cord', '6'),
]

DR_ITEMS = [
    ('210-BDZY', 'PowerEdge R760 Server', '1'),
    ('321-BGZI', '2.5" Chassis with up to 16 NVMe Drives, Dual Controller, RAID Config', '1'),
    ('338-CPBV', 'Intel Xeon Gold 6526Y 2.8G, 16C/32T, 20GT/s, 37.5M Cache, Turbo, HT (195W) DDR5-5200', '1'),
    ('379-BDCO', 'Additional Processor Selected', '1'),
    ('338-CPBV', 'Intel Xeon Gold 6526Y 2.8G, 16C/32T, 20GT/s, 37.5M Cache, Turbo, HT (195W) DDR5-5200', '1'),
    ('780-BCDI', 'No RAID', '1'),
    ('405-AAZF', 'Dell HBA355i Adapter, Low Profile', '1'),
    ('450-AKYB', 'Dual, Hot Plug, Power Supply (1+1) Redundant 1400W 2U', '1'),
    ('540-BCXW', 'Intel E810-XXV Dual Port 10/25GbE SFP28, OCP NIC 3.0', '1'),
    ('329-BERC', 'Assembly BOSS Blank', '1'),
    ('865-BBMY', 'ProSupport and Next Business Day Onsite Service Initial, 36 Month(s)', '1'),
    ('370-BBRY', '32GB RDIMM, 5600MT/s, Dual Rank', '32'),
    ('161-BCPX', '8TB Hard Drive SAS 12Gbps 7.2K 512e 3.5in Hot-Plug, AG Drive', '9'),
    ('400-BKGJ', '3.84TB Enterprise NVMe Read Intensive AG Drive U.2 Gen4 Flex Bay', '3'),
    ('450-AALV', 'C13 to C14, PDU Style, 10 AMP, 6.5 Feet (2m), Power Cord', '2'),
]

R660XS_ITEMS = [
    ('210-BFUZ', 'PowerEdge R660xs', '1'),
    ('461-AAIG', 'Trusted Platform Module 2.0 V3', '1'),
    ('321-BJZZ', '2.5" Chassis with up to 8 SAS/SATA Drives, Front PERC 11', '1'),
    ('338-CHQR', 'Intel Xeon Silver 4509Y 2.6G, 8C/16T, 16GT/s, 22.5M Cache, Turbo, HT (125W) DDR5-4400', '1'),
    ('379-BDCO', 'Additional Processor Selected', '1'),
    ('338-CHQR', 'Intel Xeon Silver 4509Y 2.6G, 8C/16T, 16GT/s, 22.5M Cache, Turbo, HT (125W) DDR5-4400', '1'),
    ('370-BCCX', '6400MT/s RDIMMs', '1'),
    ('780-BCDI', 'No RAID', '1'),
    ('405-AAZF', 'Dell HBA355i Adapter, Low Profile', '1'),
    ('329-BHOF', 'PowerEdge R660xs Motherboard with Broadcom 5720 Dual Port 1Gb On-Board LOM', '1'),
    ('540-BCXW', 'Intel E810-XXV Dual Port 10/25GbE SFP28, OCP NIC 3.0', '1'),
    ('450-AKMT', 'Dual, Hot-Plug, Power Supply, 800W', '1'),
    ('892-9155', 'ProSupport Plus 4-Hour Mission Critical, 3 Years', '1'),
    ('370-AGZP', '16GB RDIMM, 6400MT/s, Single Rank', '16'),
    ('161-BBVV', '12TB Hard Drive SAS ISE 12Gbps 7.2K 512e 3.5in Hot-Plug', '3'),
]


def _quote_sheet1(ws, number, groups, sales_rep=False):
    if sales_rep:
        put(ws, 1, {'A': 'Sales rep: Jane Partner | %s' % number})
        put(ws, 10, {'A': 'Quote number:', 'B': number})
    else:
        put(ws, 1, {'A': 'Quote number:', 'B': number})
    put(ws, 2, {'A': 'Quote date:', 'B': 'Sep. 1, 2026'})
    put(ws, 15, {'A': 'Pricing Summary'})
    put(ws, 16, {'A': 'Item', 'F': 'Qty', 'H': 'List Price', 'J': 'Unit Price', 'O': 'Subtotal'})
    r = 18
    for name, qty in groups:
        put(ws, r, {'A': name, 'F': str(qty), 'H': '$100,000.00', 'J': '$30,000.00', 'O': '$%s.00' % (30000 * qty)})
        r += 2


def _quote_items(ws, r, items, cols):
    a, c, q, price = cols
    for sku, desc, qty in items:
        put(ws, r, {a: sku, c: desc, q: qty, price: '-'})
        ws.merge_cells('%s%d:%s%d' % (a, r, 'B', r))
        r += 2
    return r


def make_dell_quote():
    wb = Workbook()
    ws1 = wb.active
    ws1.title = 'Sheet1'
    _quote_sheet1(ws1, '3000200000000.1',
                  [('PowerEdge R760 Smart Selection - [PE_R760_SYN] Production', 3),
                   ('PowerEdge R760 Smart Selection - [PE_R760_SYN] Disaster Recovery', 1)])
    ws = wb.create_sheet('Sheet2')
    header = {'A': 'SKU', 'C': 'Description', 'K': 'Qty', 'L': 'Unit Price', 'P': 'Subtotal'}
    put(ws, 1, header)
    ws.merge_cells('A1:B2')
    ws.merge_cells('C1:J2')
    put(ws, 3, {'C': 'PowerEdge R760 Smart Selection - [PE_R760_SYN] Production', 'K': '3', 'L': 30000.0})
    put(ws, 4, {'C': 'Estimated delivery date:\xa0Sep. 23, 2026'})
    put(ws, 5, {'C': 'Contract No: C000000000001'})
    r = _quote_items(ws, 7, PROD_ITEMS, ('A', 'C', 'K', 'L'))
    put(ws, r, header)                       # repeated header before group 2
    r += 2
    put(ws, r, {'C': 'PowerEdge R760 Smart Selection - [PE_R760_SYN] Disaster Recovery', 'K': '1', 'L': 30000.0})
    put(ws, r + 1, {'C': 'Estimated delivery date:\xa0Sep. 23, 2026'})
    r = _quote_items(ws, r + 3, DR_ITEMS, ('A', 'C', 'K', 'L'))
    put(ws, r, {'K': 'Subtotal:', 'P': '$120,000.00'})
    put(ws, r + 1, {'K': 'Shipping:', 'P': '$0.00'})
    put(ws, r + 2, {'K': 'Environmental Fees:', 'P': '$0.00'})
    put(ws, r + 3, {'K': 'Estimated Tax:', 'P': '$0.00'})
    put(ws, r + 4, {'K': 'Total:', 'P': '$120,000.00'})
    save_wb(wb, 'synthetic_dell_quote.xlsx')


def make_dell_quote_letter():
    wb = Workbook()
    ws1 = wb.active
    ws1.title = 'Sheet1'
    _quote_sheet1(ws1, '3000200000001.1',
                  [('PowerEdge R660xs - Qty 1 needed| Dual 8c| x16 16GB RAM| x3 12TB HDD', 1)],
                  sales_rep=True)
    ws = wb.create_sheet('Sheet2')
    letter = ['Dear Customer,', '',
              'Your quote is ready. Thank you for choosing Acme Reseller.',
              'This quote is valid until Sep. 30, 2026.', '',
              'Regards,', 'Jane Partner', 'Acme Reseller']
    for i, line in enumerate(letter, start=1):
        put(ws, i, {'A': line})
    put(ws, 12, {'A': 'SKU', 'C': 'Description', 'H': 'Qty', 'I': 'Unit Price', 'L': 'Subtotal'})
    put(ws, 14, {'C': 'PowerEdge R660xs - Qty 1 needed| Dual 8c| x16 16GB RAM| x3 12TB HDD',
                 'H': '1', 'I': 9000.0})
    put(ws, 15, {'C': 'Estimated delivery date:\xa0Oct. 5, 2026'})
    r = _quote_items(ws, 17, R660XS_ITEMS, ('A', 'C', 'H', 'I'))
    put(ws, r, {'H': 'Subtotal:', 'L': '$9,000.00'})
    put(ws, r + 1, {'H': 'Total:', 'L': '$9,000.00'})
    wb.create_sheet('Sheet3')
    save_wb(wb, 'synthetic_dell_quote_letter.xlsx')


# ─── D&H bid ─────────────────────────────────────────────────────────────────

DH_CONFIG_A = [        # totals for the 3-system champion line
    ('PowerEdge R760 Server', 3),
    ('Intel Xeon Gold 6526Y 2.8G, 16C/32T, 20GT/s, 37.5M Cache, Turbo, HT (195W) DDR5-5200', 3),
    ('Intel Xeon Gold 6526Y 2.8G, 16C/32T, 20GT/s, 37.5M Cache, Turbo, HT (195W) DDR5-5200', 3),
    ('32GB RDIMM, 5600MT/s, Dual Rank', 96),
    ('Dell HBA355i Adapter, Low Profile', 3),
    ('3.84TB Enterprise NVMe Read Intensive AG Drive U.2 Gen4 Flex Bay', 9),
    ('8TB Hard Drive SAS 12Gbps 7.2K 512e 3.5in Hot-Plug, AG Drive', 27),
    ('Intel E810-XXV Dual Port 10/25GbE SFP28, OCP NIC 3.0', 3),
    ('Dual, Hot Plug, Power Supply (1+1) Redundant 1400W 2U', 3),
    ('C13 to C14, PDU Style, 10 AMP, 6.5 Feet (2m), Power Cord', 6),
    ('ReadyRails Sliding Rails With Cable Management Arm', 3),
    ('ProSupport and Next Business Day Onsite Service Initial, 36 Month(s)', 3),
]
DH_SKUS_A = ['210-BDZY', '338-CPBV', '338-CPBV', '370-BBRY', '405-AAZF', '400-BKGJ', '161-BCPX',
             '540-BCXW', '450-AKYB', '450-AALV', '770-BCJI', '865-BBMY']

DH_CONFIG_B = [
    ('PowerEdge R760 Server', 1),
    ('Intel Xeon Gold 6526Y 2.8G, 16C/32T, 20GT/s, 37.5M Cache, Turbo, HT (195W) DDR5-5200', 1),
    ('Intel Xeon Gold 6526Y 2.8G, 16C/32T, 20GT/s, 37.5M Cache, Turbo, HT (195W) DDR5-5200', 1),
    ('32GB RDIMM, 5600MT/s, Dual Rank', 32),
    ('Dell HBA355i Adapter, Low Profile', 1),
    ('3.84TB Enterprise NVMe Read Intensive AG Drive U.2 Gen4 Flex Bay', 3),
    ('8TB Hard Drive SAS 12Gbps 7.2K 512e 3.5in Hot-Plug, AG Drive', 9),
    ('Intel E810-XXV Dual Port 10/25GbE SFP28, OCP NIC 3.0', 1),
    ('Dual, Hot Plug, Power Supply (1+1) Redundant 1400W 2U', 1),
    ('C13 to C14, PDU Style, 10 AMP, 6.5 Feet (2m), Power Cord', 2),
]
DH_SKUS_B = DH_SKUS_A[:10]


def make_dh_bid():
    wb = Workbook()
    ws = wb.active
    ws.title = 'DH Quotation'
    put(ws, 2, {'B': 'Quotation', 'G': FIXED_TIME})
    put(ws, 4, {'B': 'Bid Number:', 'C': '611179-00000-00'})
    put(ws, 9, {'B': 'Currency:', 'G': 'USD'})
    put(ws, 22, {'B': 'Bid Line No.', 'C': 'Vendor Name', 'D': 'D&H Part #', 'E': 'Manufacturer Part #',
                 'F': 'Part Notes', 'G': 'Description', 'H': 'Unit List Price',
                 'I': '% Discount\noff List', 'J': 'Reseller\nUnit Price', 'K': 'Quantity',
                 'L': 'Reseller\nExtended Price'})
    r = 23
    line = 1

    def champion(dh_part, qty):
        nonlocal r, line
        put(ws, r, {'B': line, 'C': 'DELL', 'D': dh_part, 'E': '210-BDZY',
                    'G': 'POWEREDGE R760, CHAMPION PE', 'H': 99999.99, 'I': '70.00%',
                    'J': 29999.99, 'K': qty, 'L': 29999.99 * qty})
        r += 1
        line += 1

    def items(dh_part, descs, skus):
        nonlocal r, line
        for (desc, qty), sku in zip(descs, skus):
            put(ws, r, {'B': line, 'C': 'DELL', 'D': dh_part, 'E': sku, 'G': desc,
                        'H': 0, 'I': '100.00%', 'J': 0, 'K': qty, 'L': 0})
            r += 1
            line += 1

    champion('S30009990000001', 3)
    items('S30009990000001', DH_CONFIG_A, DH_SKUS_A)
    # a bid line with no manufacturer part (a note) — must be skipped
    put(ws, r, {'B': line, 'C': 'DELL', 'G': 'Note: lead time 4 weeks after order', 'K': 1})
    r += 1
    line += 1
    champion('S30009990000002', 1)
    items('S30009990000002', DH_CONFIG_B, DH_SKUS_B)
    put(ws, r, {'K': 'Product Subtotal:', 'L': 119999.96})
    put(ws, r + 1, {'K': 'Total Price:', 'L': 119999.96})
    put(ws, r + 4, {'B': 'Prices are valid for 30 days. Synthetic fixture, no legal meaning.'})

    ws_o = wb.create_sheet('Ordering Details')
    put(ws_o, 1, {'A': 'Ordering Details'})
    put(ws_o, 29, {'A': 1, 'B': '210-BDZY', 'C': 'POWEREDGE R760, CHAMPION PE', 'D': 3})
    put(ws_o, 30, {'A': 2, 'B': '210-BDZY', 'C': 'PowerEdge R760 Server', 'D': 3})
    put(ws_o, 31, {'A': 14, 'B': '210-BDZY', 'C': 'POWEREDGE R760, CHAMPION PE', 'D': 1})
    put(ws_o, 32, {'A': 15, 'B': '210-BDZY', 'C': 'PowerEdge R760 Server', 'D': 1})
    ws_c = wb.create_sheet('Configuration Details')
    put(ws_c, 1, {'D': 'PRODUCT NUMBER', 'E': 'QUANTITY', 'H': 'SERVICE PRODUCT DESCRIPTION', 'X': 'LINE NUMBER'})
    put(ws_c, 2, {'D': '210-BDZY', 'E': 3, 'H': 'PowerEdge R760 Server', 'X': 1})
    save_wb(wb, 'synthetic_dh_bid.xlsx')


# ─── Dell VNET module export ─────────────────────────────────────────────────

VNET_ROWS = [
    # (Module Name, Option ID, Option Name, SKUs, Qty)
    ('Base', 'GYF3001', 'PowerEdge R760 Server', '210-BDZY', 1),
    ('Trusted Platform Module', 'GYF3002', 'Trusted Platform Module 2.0 V3', '461-AAIG', 1),
    ('Chassis Configuration', 'GYF3003', '2.5" Chassis with up to 16 NVMe Drives, Dual Controller, RAID Config', '321-BGZI', 1),
    ('Processor', 'GYF3004', 'Intel Xeon Gold 6526Y 2.8G, 16C/32T, 20GT/s, 37.5M Cache, Turbo, HT (195W) DDR5-5200', '338-CPBV', 1),
    ('Additional Processor', 'GYF3005', 'Intel Xeon Gold 6526Y 2.8G, 16C/32T, 20GT/s, 37.5M Cache, Turbo, HT (195W) DDR5-5200', '338-CPBV', 1),
    ('Additional Processor Features', 'GYF3006', 'Additional Processor Selected', '379-BDCO', 1),
    ('Processor Thermal Configuration', 'GYF3007', 'Heatsink for 2 CPU configuration (CPU greater than 165W)', '412-ABCP', 1),
    ('Memory Configuration Type', 'GYF3008', 'Performance Optimized', '370-AAIP', 1),
    ('Memory DIMM Type and Speed', 'GYF3009', '5600MT/s RDIMMs', '370-BBRX', 1),
    ('Memory Capacity', 'GYF3010', '32GB RDIMM, 5600MT/s, Dual Rank', '370-BBRY', 12),
    ('RAID Configuration', 'GYF3011', 'No RAID', '780-BCDI', 1),
    ('RAID/Internal Storage Controllers', 'GYF3012', 'Dell HBA355i Adapter, Low Profile', '405-AAZF', 1),
    ('Hard Drives', 'GYF3013', '3.84TB Enterprise NVMe Read Intensive AG Drive U.2 Gen4 Flex Bay', '400-BKGJ', 3),
    ('Hard Drives', 'GYF3014', '8TB Hard Drive SAS 12Gbps 7.2K 512e 3.5in Hot-Plug, AG Drive', '161-BCPX', 9),
    ('BIOS and Advanced System Configuration Settings', 'GYF3015', 'Performance BIOS Settings', '384-BBBL', 1),
    ('Advanced System Configurations', 'GYF3016', 'UEFI BIOS Boot Mode with GPT Partition', '800-BBDM', 1),
    ('Fans', 'GYF3017', 'Standard Fan x6 V3', '750-ADGJ', 1),
    ('Power Supply', 'GYF3018', 'Dual, Hot Plug, Power Supply (1+1) Redundant 1400W 2U', '450-AKYB', 1),
    ('Power Cords', 'GYF3019', 'C13 to C14, PDU Style, 10 AMP, 6.5 Feet (2m), Power Cord', '450-AALV', 2),
    ('PCIe Riser', 'GYF3020', 'Riser Config 1, 2x16 LP', '330-BBXJ', 1),
    ('Motherboard', 'GYF3021', 'PowerEdge R760 Motherboard with ONLY CPUs below 250W supported, MLK', '329-BKCH', 1),
    ('OCP 3.0 Network Adapters', 'GYF3022', 'Intel E810-XXV Dual Port 10/25GbE SFP28, OCP NIC 3.0', '540-BCXW', 1),
    ('Additional Network Cards', 'GYF3023', 'LOM Blank', '540-BDOW', 1),
    ('GPU', 'GYF3024', 'NVIDIA L4 24GB PCIe', '490-BJIT', 1),
    ('Bezel', 'GYF3025', 'PowerEdge 2U Standard Bezel', '321-BHMY, 325-BEVI', 1),
    ('Boot Optimized Storage Cards', 'GYF3026', 'BOSS Blank', '329-BERC', 1),
    ('Quick Sync', 'GYF3027', 'Quick Sync 2 (At-the-box mgmt)', '350-BCEL', 1),
    ('Password', 'GYF3028', 'iDRAC,Factory Generated Password', '379-BCQV', 1),
    ('Group Manager', 'GYF3029', 'iDRAC Group Manager, Disabled', '379-BCQY', 1),
    ('Operating System', 'GYF3030', 'No Operating System', '611-BBBF', 1),
    ('OS Media Kits', 'GYF3031', 'No Media Required', '605-BBFN', 1),
    ('iDRAC Systems Management Options', 'GYF3032', 'iDRAC9, Enterprise 16G', '528-CTIC', 1),
    ('Rack Rails', 'GYF3033', 'ReadyRails Sliding Rails With Cable Management Arm', '770-BDRQ, 770-BEKK', 1),
    ('System Documentation', 'GYF3034', 'No Systems Documentation, No OpenManage DVD Kit', '631-ADAJ', 1),
    ('SHIPPING', 'GYF3035', 'PowerEdge R760 Shipping', '340-DCEP', 1),
    ('Regulatory', 'GYF3036', 'PowerEdge R760 CE, CCC, BIS Marking', '343-BBST, 343-BBSX', 1),
    ('Standard Hardware Support Service', 'GYF3037', 'Basic Next Business Day 36 Months', '709-BBFM', 1),
    ('Hardware Support Services Upgrades', 'GYF3038',
     'ProSupport and Next Business Day Onsite Service Initial, 36 Month(s)', '865-BBMY, 865-BBMZ', 1),
]


def make_dell_vnet():
    wb = Workbook()
    ws = wb.active
    ws.title = 'AcmeCo_1'
    put(ws, 1, {'A': 'Configuration Export'})
    put(ws, 3, {'A': 'Customer:', 'B': 'Acme Co'})
    put(ws, 4, {'A': 'Configuration:', 'B': 'AcmeCo_1'})
    put(ws, 7, {'A': 'Module Name', 'B': 'Option ID', 'C': 'Option Name', 'D': 'SKUs', 'E': 'Qty'})
    r = 9
    for module, oid, option, skus, qty in VNET_ROWS:
        put(ws, r, {'A': module, 'B': oid, 'C': option, 'D': skus, 'E': qty})
        r += 1
    save_wb(wb, 'synthetic_dell_vnet.xlsx')


# ─── hand-typed Dell lists ───────────────────────────────────────────────────

R660_ROWS = [
    ('PowerEdge R660 Server', '210-BFUZ'),
    ('Intel Xeon Gold 6526Y 2.8G, 16C/32T, 20GT/s, 37.5M Cache, Turbo, HT (195W) DDR5-5200', '338-CPBV'),
    ('Intel Xeon Gold 6526Y 2.8G, 16C/32T, 20GT/s, 37.5M Cache, Turbo, HT (195W) DDR5-5200', '338-CPBV'),
    ('8x 16GB RDIMM, 5600MT/s, Single Rank', '(Dell - Hynix PN) HMCG78AEBRA107N'),
    ('4x 3.84TB Data Center NVMe Read Intensive AG Drive U2  with Carrier', '400-BMTN'),
    ('Dell HBA355i Front', '405-AAZF'),
    ('Intel X710 Dual Port 10GbE SFP+ OCP NIC 3.0', '540-BBXO'),
    ('Riser Config 2, 2 x16 LP', '330-BBXH'),
    ('Dual, Hot-Plug, Power Supply, 800W', '450-AKMT'),
    ('PowerEdge 1U Standard Bezel', '325-BEVE, 350-BCKC'),
    ('ReadyRails Sliding Rails', '770-BDMT, 770-BECD'),
    ('UEFI BIOS Boot Mode with GPT Partition', '800-BBDM'),
    ('ProSupport Next Business Day 5 Years', '865-BBMY'),
    ('No BOSS Card', '470-AFBU'),
]

R750XS_ROWS = [
    (1, 'Dell R750XS 12x 3.5in LFF, Riser 4 Config', 'PER750XS-12LFF-R4'),
    (2, 'Intel Xeon Silver 4310 2.1G 12C/24T', 'SRKXR'),
    (8, '32GB RDIMM 3200MT/s Dual Rank', 'HMA84GR7CJR4N-XN'),
    (1, 'Dell HBA355I Controller PCI Full Height', '7GRF6'),
    (6, '12TB 7.2K RPM SAS 12Gbps 512e 3.5in Hot-plug Hard Drive', '9HXK6-14G'),
    (1, 'Intel X710 Dual Port 10GB Base-T OCP 3.0', 'XC0M4'),
    (2, 'Dell 1400W Hot Plug Power Supply', 'MGPPC'),
    (1, 'ProSupport Next Business Day 5 Years', 'DRB5W'),
    (1, 'Dell 2U Cable Management Arm Kit', '385-BBPP'),
]


def make_dell_lists():
    wb = Workbook()
    ws = wb.active
    ws.title = 'R660'
    put(ws, 1, {'A': 'QTY 3', 'B': 'Config 1', 'C': 'Available SKUs'})
    for i, (desc, skus) in enumerate(R660_ROWS, start=2):
        put(ws, i, {'B': desc, 'C': skus})
    ws2 = wb.create_sheet('R750XS')
    put(ws2, 1, {'A': 'QTY', 'C': 'Description', 'D': 'Part Number'})
    for i, (qty, desc, pn) in enumerate(R750XS_ROWS, start=2):
        put(ws2, i, {'A': qty, 'C': desc, 'D': pn})
    save_wb(wb, 'synthetic_dell_lists.xlsx')


COLUMNS_ROWS = [
    ('3x PowerEdge R660xs', '3x PowerEdge R660'),
    ('Intel Xeon Gold 6434 3.7G, 8C/16T, 16GT/s, 22.5M Cache, Turbo, HT (195W) DDR5-4800',
     'Intel Xeon Silver 4410Y 2.0G, 12C/24T, 16GT/s, 30M Cache, Turbo, HT (150W) DDR5-4000'),
    ('Intel Xeon Gold 6434 3.7G, 8C/16T, 16GT/s, 22.5M Cache, Turbo, HT (195W) DDR5-4800',
     'Intel Xeon Silver 4410Y 2.0G, 12C/24T, 16GT/s, 30M Cache, Turbo, HT (150W) DDR5-4000'),
    ('8x 16GB RDIMM, 4800MT/s, Dual Rank', '8x 32GB RDIMM, 4800MT/s, Dual Rank'),
    ('4x 3.84TB NVMe SAS Read Intensive SSD',                       # contradictory on purpose
     '4x 3.84TB SSD SAS Read Intensive 12Gbps 512e 2.5in Hot-Plug'),
    ('2.5" Chassis with up to 8 NVMe Direct Drives', '2.5" Chassis with up to 8 SAS/SATA Drives'),
    ('Dell HBA355i Front', 'Dell HBA355i Front'),
    ('Intel X710 Dual Port 10GbE SFP+ OCP NIC 3.0', 'Broadcom 57414 Dual Port 10/25GbE SFP28 OCP NIC 3.0'),
    ('Dual, Hot-Plug, Power Supply, 800W', 'Dual, Hot-Plug, Power Supply, 800W'),
    ('BOSS-N1 controller card + with 2 M.2 480GB (RAID 1)', 'No BOSS Card'),
    ('Performance BIOS Settings', 'Performance BIOS Settings'),
    ('UEFI BIOS Boot Mode with GPT Partition', 'UEFI BIOS Boot Mode with GPT Partition'),
    ('Dell Connectivity Client', 'Dell Connectivity Client'),
    ('ProSupport Next Business Day 5 Years', 'ProSupport Next Business Day 5 Years'),
    ('A/C Power Recovery, Last', 'A/C Power Recovery, Last'),
]


def make_dell_columns():
    wb = Workbook()
    ws = wb.active
    ws.title = 'Sheet1'
    put(ws, 1, {'A': 'Config 1', 'B': 'Config 2'})
    for i, (a, b) in enumerate(COLUMNS_ROWS, start=2):
        put(ws, i, {'A': a, 'B': b})
    save_wb(wb, 'synthetic_dell_columns.xlsx')


# ─── our strict template (Supermicro example) ───────────────────────────────

def supermicro_bom():
    """The NormalizedBOM the template fixture is built from (exported by
    tests as the round-trip source)."""
    from bom.normalize import BOMComponent, BOMConfig, NormalizedBOM
    comps = [
        ('511R-M-OTO-17', 'UP 1U X13SCH-SYS, CSE-813MF2TS-R0RCNBP, PWS-602A-1R Optimized System', 3, 'chassis'),
        ('P4X-UPE2434-SRMXC', 'Intel Xeon E-2434 3.4GHz 4C/8T 55W', 3, 'cpu'),
        ('MEM-DR532MD-EU56', '32GB DDR5-5600 ECC UDIMM', 12, 'memory'),
        ('AOC-S3808L-L8IT-P', '8-port 12Gb/s SAS3 HBA, PCIe x8 Gen4, Low Profile', 3, 'controller'),
        ('HDS-25T0-001T9-M1-TXE-NON-007', 'SSD 2.5" SATA 1.9TB >1DWPD TLC, 7mm', 12, 'storage'),
        ('AOC-STG-I4T-P', 'Intel XL710/X557 4-port 10GBase-T NIC', 3, 'nic'),
        ('AOM-TPM-9672H-P', 'TPM 2.0 module', 3, 'other'),
        ('PWS-602A-1R', '600W 1U redundant power supply', 6, 'other'),
    ]
    config = BOMConfig(name='Acme Edge', server_model='SYS-511R-M',
                       components=[BOMComponent(pn, d, q, c) for pn, d, q, c in comps],
                       node_count=3)
    return NormalizedBOM(vendor='Supermicro', configs=[config])


def make_template():
    from bom.parsers import template
    save_bytes(_deterministic_zip(template.build_template_bytes(supermicro_bom())),
               'synthetic_template.xlsx')


# ─── not a BOM ───────────────────────────────────────────────────────────────

def make_not_a_bom():
    wb = Workbook()
    ws = wb.active
    ws.title = 'Sheet1'
    ws.append(['Name', 'Value', 'Notes'])
    ws.append(['alpha', 1, 'first'])
    ws.append(['beta', 2.5, 'second'])
    save_wb(wb, 'not_a_bom.xlsx')
    save_bytes(b'name,value,notes\nalpha,1,first\nbeta,2.5,second\n', 'not_a_bom.csv')


# ─── driver ──────────────────────────────────────────────────────────────────

FIXTURES = [
    ('synthetic_lenovo_dcsc.xlsx', make_lenovo_dcsc, 'lenovo_dcsc'),
    ('synthetic_lenovo_dcsc_ptbr.xlsx', make_lenovo_dcsc_ptbr, 'lenovo_dcsc'),
    ('synthetic_lenovo_dcsc_boss.xlsx', make_lenovo_dcsc_boss, 'lenovo_dcsc'),
    ('synthetic_dell_servicetag.csv', make_dell_servicetag_csv, 'dell_service_tag'),
    ('synthetic_dell_servicetag.xlsx', make_dell_servicetag_xlsx, 'dell_service_tag'),
    ('synthetic_dell_quote.xlsx', make_dell_quote, 'dell_quote'),
    ('synthetic_dell_quote_letter.xlsx', make_dell_quote_letter, 'dell_quote'),
    ('synthetic_dh_bid.xlsx', make_dh_bid, 'dh_bid'),
    ('synthetic_dell_vnet.xlsx', make_dell_vnet, 'dell_vnet'),
    ('synthetic_dell_lists.xlsx', make_dell_lists, 'dell_list_sku'),
    ('synthetic_dell_columns.xlsx', make_dell_columns, 'dell_list_columns'),
    ('synthetic_template.xlsx', make_template, 'template'),
]


def main(argv):
    write_normalized = '--write-normalized' in argv
    make_not_a_bom()
    from bom.parsers import detect_format, parse_file
    norm_dir = os.path.join(HERE, 'normalized')
    if write_normalized:
        os.makedirs(norm_dir, exist_ok=True)
    for name, builder, expected_fmt in FIXTURES:
        builder()
        path = os.path.join(HERE, name)
        fmt = detect_format(path, name)
        assert fmt == expected_fmt, '%s detected as %r, expected %r' % (name, fmt, expected_fmt)
        bom, fmt2 = parse_file(path, name)
        assert fmt2 == fmt
        print('%-36s %-22s vendor=%s' % (name, fmt, bom.vendor))
        for cfg in bom.configs:
            print('    %-24r model=%-22s nodes=%s comps=%d' % (
                cfg.name, cfg.server_model, cfg.node_count, len(cfg.components)))
            for c in cfg.components:
                print('        %-10s %-18s %4d  %s' % (c.category, c.part_number, c.quantity, c.description))
        if write_normalized:
            # <original filename>.json, the archive's convention
            with open(os.path.join(norm_dir, name + '.json'), 'w', encoding='utf-8') as fh:
                json.dump(bom.to_dict(), fh, indent=2, ensure_ascii=False)
                fh.write('\n')
    for name in ('not_a_bom.xlsx', 'not_a_bom.csv'):
        assert detect_format(os.path.join(HERE, name), name) is None, name
    print('ok')


if __name__ == '__main__':
    main(sys.argv[1:])
