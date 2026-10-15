"""Lenovo system quote from Arrow (the Arrow Excel quote sheet printed to PDF).

The same Arrow template as parsers/pdf_dell_arrow.py and
parsers/pdf_supermicro_quote.py — header 'Product | Part / Project
Description | Qty | Unit Price | Total Price' — but filled with a Lenovo DCSC
configuration (first seen 2026-10-15, CMS Distribution):

    Product     Part / Project Description                          Qty  Unit Price  Total Price
    7D76CTO1WW  ThinkSystem SR650 V3-3yr Base Warranty-CMS            3  £ 31,720.00 £ 95,160.00
    BLKJ        ThinkSystem V3 2U 12x3.5" Chassis                     1
    BYVY        Intel Xeon Gold 6542Y 24C 250W 2.9GHz Processor       2
    BWJC        ThinkSystem 32GB TruDDR5 5600MHz (2Rx8) RDIMM         8
    …
    5374CM1     Configuration Instruction                             3
    7S0XCTO5WW  XClarity Controller Platin-FOD                        3   <- software
    7Q01CTS2WW  SERVER PREMIER NBD RESP                               3   <- service

It is the DCSC export without its blank separator rows, like
parsers/pdf_lenovo_list.py, with one difference: feature-code quantities are
per machine (8 DIMMs each), not totalled over the block as DCSC does. So each
child quantity is multiplied by its block's count to restore the DCSC totals
and the blocks go to lenovo_dcsc.configs_from_blocks — every Lenovo DCSC
shape is read by the same code.

Cross-checks: unit price x quantity = total per priced block, and the priced
blocks add up to the Sub-Total. Lines under the table that look like items
are recorded as stray, as on the Dell variant.
"""
import re
from typing import List

from bom.normalize import NormalizedBOM
from bom.parsers.common import LENOVO_CTO
from bom.parsers.lenovo_dcsc import configs_from_blocks
from bom.parsers.pdf_common import Evidence, first_word_x, money, whole_number
from bom.parsers.pdf_supermicro_quote import _header

FORMAT = 'lenovo_arrow_pdf'

# Block starters: machine / software CTOs (7D76CTO1WW, 7S0XCTO5WW), service
# CTOs (7Q01CTS2WW) and configuration instructions (5374CM1, whose children
# — 'BM34 … HBA Placement' — must not land in the machine's parts).
_BLOCK = re.compile(r'^(?:[0-9A-Z]{4}CT[OS0-9][0-9A-Z]WW|\d{4}CM\d)$')
_FEATURE = re.compile(r'^[A-Z0-9]{4}$')
_END = ('integration services', 'freight_charge', 'sub-total')


def detect(doc) -> bool:
    header = next((l for l in doc.lines[:40] if _header(l)), None)
    if header is None:
        return False
    first = next((l for l in doc.lines if (l.page, l.top) > (header.page, header.top)), None)
    return first is not None and bool(LENOVO_CTO.match(first.words[0].text))


def parse(doc):
    ev = Evidence()
    header = next(l for l in doc.lines if _header(l))
    desc_x = first_word_x(header, 'Part')
    qty_x = first_word_x(header, 'Qty')
    unit_x = first_word_x(header, 'Unit')
    total_x = first_word_x(header, 'Total')
    blocks: List[dict] = []
    item_lines = []
    subtotal = None
    ended = False
    for line in doc.lines:
        if (line.page, line.top) <= (header.page, header.top):
            continue
        low = line.text.lower()
        if 'sub-total' in low:
            subtotal = money(line.text.split('Sub-Total', 1)[-1])
        if ended or low.startswith(_END):
            ended = True
            left = line.between(0, qty_x - 40).strip()
            if re.match(r'^[0-9A-Z]{4,10}\s+\S', left) and _FEATURE.match(left.split()[0]):
                ev.notes.append('stray: %s' % left[:80])
            continue
        qty = next((int(w.text) for w in line.words
                    if abs(w.x0 - qty_x) < 25 and whole_number(w.text) is not None), None)
        part = line.between(0, desc_x - 5).strip()
        desc = line.between(desc_x - 5, qty_x - 20).strip()
        if qty is None or not part or not desc or ' ' in part:
            ev.unexplained_line(line)
            continue
        ev.rows += 1
        item_lines.append(line)
        if _BLOCK.match(part):
            blocks.append({'sku': part, 'title': desc, 'qty': qty, 'rows': [],
                           'unit': money(line.between(unit_x - 12, total_x - 5)),
                           'total': money(line.between(total_x - 12, 10 ** 6))})
        elif blocks:
            if not _FEATURE.match(part):
                ev.pattern_misses.append(part)
            # Per machine on this sheet; DCSC (and configs_from_blocks) totals.
            blocks[-1]['rows'].append((ev.rows, part, desc, qty * blocks[-1]['qty']))
        else:
            ev.unexplained_line(line)

    grand = 0.0
    for b in blocks:
        if b['unit'] is not None and b['total'] is not None:
            ev.check(abs(b['unit'] * b['qty'] - b['total']) <= 0.01 * max(b['total'], 1),
                     '%s: unit price x quantity = total' % b['sku'])
            grand += b['total']
    if subtotal is not None and grand:
        ev.check(abs(subtotal - grand) <= 0.01 * max(subtotal, 1), 'block totals add up to the sub-total')
    configs = configs_from_blocks(blocks, True)
    ev.region(item_lines)
    return NormalizedBOM(vendor='Lenovo', configs=configs, raw_text=doc.text()[:20000]), ev
