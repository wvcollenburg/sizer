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

from bom.parsers.common import LENOVO_BLOCK_OR_CM, LENOVO_CTO, LENOVO_FC
from bom.parsers.lenovo_dcsc import configs_from_blocks
from bom.parsers.pdf_common import (ARROW_END, Evidence, arrow_header, check_grand_total, finish,
                                    money, qty_in_column, read_arrow_table)

FORMAT = 'lenovo_arrow_pdf'

# A stray under the table: a feature code and a description.
_STRAY = re.compile(r'^[A-Z0-9]{4}\s+\S')


def detect(doc) -> bool:
    header = next((l for l in doc.lines[:40] if arrow_header(l)), None)
    if header is None:
        return False
    first = next((l for l in doc.lines if (l.page, l.top) > (header.page, header.top)), None)
    return first is not None and bool(LENOVO_CTO.match(first.words[0].text))


def parse(doc):
    ev = Evidence()
    _header, cols, body, subtotal = read_arrow_table(doc, ev, stray_re=_STRAY, end_re=ARROW_END)
    blocks: List[dict] = []
    item_lines = []
    for line in body:
        qty = qty_in_column(line, cols.qty)
        part = line.between(0, cols.desc - 5).strip()
        desc = line.between(cols.desc - 5, cols.qty - 20).strip()
        if qty is None or not part or not desc or ' ' in part:
            ev.unexplained_line(line)
            continue
        ev.rows += 1
        item_lines.append(line)
        if LENOVO_BLOCK_OR_CM.match(part):
            blocks.append({'sku': part, 'title': desc, 'qty': qty, 'rows': [],
                           'unit': money(line.between(cols.unit - 12, cols.total - 5)),
                           'total': money(line.between(cols.total - 12, 10 ** 6))})
        elif blocks:
            if not LENOVO_FC.match(part):
                ev.pattern_misses.append(part)
            # Per machine on this sheet; DCSC (and configs_from_blocks) totals.
            blocks[-1]['rows'].append((ev.rows, part, desc, qty * blocks[-1]['qty']))
        else:
            ev.unexplained_line(line)

    grand = 0.0
    for b in blocks:
        if b['unit'] is not None and b['total'] is not None:
            ev.check_close(b['unit'] * b['qty'], b['total'], '%s: unit price x quantity = total' % b['sku'])
            grand += b['total']
    check_grand_total(ev, subtotal, grand, 'block totals add up to the sub-total')
    return finish(doc, ev, 'Lenovo', configs_from_blocks(blocks), item_lines)
