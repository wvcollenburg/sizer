"""Supermicro system quote from a distributor (Arrow; an Excel sheet printed
to PDF).

    Product  Part / Project Description                      Qty  Unit Price  Total Price
             511R-M-OTO-17                                     3  $21,160.00  $63,480.00
             OPTIMIZED UP 1U X13SCH-SYS, CSE-813MF2TS-R0RCNBP,PWS-602A-1R
             SYS-511R-M(x3)UP 1U X13SCH-SYS, …
             P4X-UPE2434-SRMXC(x3)RPL-E E-2434 1P 4C/8T 3.4G 12MB 55W …
             MEM-DR532MD-EU56(x12)32GB DDR5-5600 2RX8 (16Gb) LP ECC UDIMM …

One bundle row per system, then the bundle's parts inline in its wrapped
description: every part is 'PART(xN)description' with N the total across
the bundle's systems. The bundle row is the chassis line (quantity = node
count); the SYS- barebone inside it names the server model but is not a
second chassis. Warranty, assembly and licence lines stay 'other'.

Categories come from Supermicro's part-number prefixes first (MEM-, HDS-,
AOC-S… is an HBA, AOC-STG/ATG/… a NIC), the description second.

Cross-checks: unit price x quantity = total per bundle, the bundle totals
add up to the Sub-Total, and every inline (xN) is a whole multiple of the
bundle's system count (per-system parts). Visible item-like lines outside
the table are recorded as stray (they cost a little certainty) rather than
read or silently ignored.
"""
import re
from typing import List, Optional

from bom.normalize import BOMComponent, BOMConfig, NormalizedBOM
from bom.parsers.common import categorize, is_absence
from bom.parsers.pdf_common import Evidence, doc_head, first_word_x, money, whole_number

FORMAT = 'supermicro_quote_pdf'

_ITEM = re.compile(r'(?<![A-Za-z0-9-])([A-Z0-9][A-Z0-9.\-]{2,40})\(x(\d{1,4})\)')
_CQ = re.compile(r'\(CQ\d+\)')
_KNOWN_PREFIX = re.compile(r'^(SYS|AS|SSG|P4X|P4D|PSE|MEM|HDS|HDD|AOC|AOM|MCP|PWS|SFT|EXS|MC0|CBL|RSC|SNK|'
                           r'BTR|FAN|SNK|GPU|NVM|SSD|TPM|CSE|MBD|RAM|BPN|OS|DWS|PIO)[A-Z0-9.\-]*$')

_PREFIX_CATEGORY = [
    (re.compile(r'^(P4X|P4D|PSE)-'), 'cpu'),
    (re.compile(r'^MEM-'), 'memory'),
    (re.compile(r'^(HDS|HDD|SSD)-'), 'storage'),
    (re.compile(r'^AOC-(S|H)\d'), 'controller'),           # AOC-S3808L… SAS HBA
    (re.compile(r'^AOC-(STG|ATG|A25G|A100G|MTG|MCX|S25G|S100G|STGN|STGS|SGP)'), 'nic'),
    (re.compile(r'^AOC-SLG'), 'boss'),                      # M.2 carrier boards
    (re.compile(r'^(AOM|MCP|PWS|SFT|EXS|MC0|CBL|RSC|SNK|BTR|FAN)'), 'other'),
    (re.compile(r'^(SYS|AS|SSG)-'), 'other'),               # barebone: model, not a 2nd chassis
]


def _header(line) -> bool:
    low = line.text.lower()
    return low.startswith('product') and 'description' in low and 'qty' in low and 'total' in low


def detect(doc) -> bool:
    if not any(_header(l) for l in doc.lines[:40]):
        return False
    return bool(_ITEM.search(doc.text()))


def _category(part: str, desc: str) -> str:
    for rx, cat in _PREFIX_CATEGORY:
        if rx.search(part):
            return cat
    return categorize(desc)


def parse(doc):
    ev = Evidence()
    header = next(l for l in doc.lines if _header(l))
    desc_x = first_word_x(header, 'Part') or header.words[1].x0
    qty_x = first_word_x(header, 'Qty')
    bundles = []
    cur = None
    item_lines = []
    ended = False
    subtotal = None
    for line in doc.lines:
        if line.page < header.page or (line.page == header.page and line.top <= header.top):
            continue
        low = line.text.lower()
        if ended:
            if 'sub-total' in low or 'subtotal' in low:
                subtotal = money(line.text.split('Sub-Total', 1)[-1])
            left = line.between(0, qty_x - 40).strip()
            if re.match(r'^\d{1,3}\s+\S', left) and len(left) > 12:
                ev.notes.append('stray: %s' % left[:80])
            continue
        if line.words[0].x0 < desc_x - 10:          # a label column row ends the table
            ended = True
            if 'sub-total' in low:
                subtotal = money(line.text.split('Sub-Total', 1)[-1])
            continue
        qty = next((int(w.text) for w in line.words
                    if abs(w.x0 - qty_x) < 25 and whole_number(w.text) is not None), None)
        text = line.between(desc_x - 5, qty_x - 20).strip()
        if qty is not None:
            prices = [money(w.text) for w in line.words if w.text.startswith(('$', '€', '£'))]
            cur = {'part': text.split()[0] if text else '', 'qty': qty, 'prices': prices, 'desc': []}
            bundles.append(cur)
            item_lines.append(line)
            continue
        if cur is None:
            ev.unexplained_line(line)
            continue
        cur['desc'].append(text)
        item_lines.append(line)

    configs = []
    grand = 0.0
    for bi, b in enumerate(bundles, 1):
        nodes = b['qty']
        blob = _CQ.sub('', ' '.join(b['desc']))
        matches = list(_ITEM.finditer(blob))
        intro = blob[:matches[0].start()].strip() if matches else blob.strip()
        comps: List[BOMComponent] = [BOMComponent(part_number=b['part'], description=intro or b['part'],
                                                  quantity=nodes, category='chassis')]
        model: Optional[str] = None
        off_multiple: List[str] = []
        for k, m in enumerate(matches):
            part, n = m.group(1), int(m.group(2))
            end = matches[k + 1].start() if k + 1 < len(matches) else len(blob)
            desc = ' '.join(blob[m.end():end].split()).strip(' ,')
            if not _KNOWN_PREFIX.match(part):
                ev.pattern_misses.append(part)
            if n % nodes:
                off_multiple.append('%s (x%d)' % (part, n))
            cat = _category(part, desc)
            if cat != 'other' or not is_absence(desc):
                comps.append(BOMComponent(part_number=part, description=desc or part,
                                          quantity=n, category=cat))
            if part.startswith(('SYS-', 'AS-', 'SSG-')) and model is None:
                model = part
            ev.rows += 1
        if matches:
            ev.check(not off_multiple, 'bundle %d: every inline quantity is a multiple of %d systems%s'
                     % (bi, nodes, (' (not: %s)' % ', '.join(off_multiple)) if off_multiple else ''))
        if len(b['prices']) >= 2 and None not in b['prices']:
            ev.check(abs(b['prices'][0] * nodes - b['prices'][-1]) <= 0.01 * max(b['prices'][-1], 1),
                     'bundle %d: unit price x quantity = total' % bi)
            grand += b['prices'][-1]
        configs.append(BOMConfig(name='Config %d' % bi if len(bundles) > 1 else 'Config 1',
                                 server_model=model, components=comps, node_count=nodes))
    if subtotal is not None and grand:
        ev.check(abs(subtotal - grand) <= 0.01 * max(subtotal, 1), 'bundle totals add up to the sub-total')
    ev.region(item_lines)
    return NormalizedBOM(vendor='Supermicro', configs=configs, raw_text=doc.text()[:20000]), ev
