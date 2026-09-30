"""Dell system quote from Arrow (the Arrow Excel quote sheet printed to PDF).

The same Arrow template as parsers/pdf_supermicro_quote.py — header
'Product | Part / Project Description | Qty | Unit Price | Total Price' —
but a Dell system is laid out as the configurator's module/option rows:

    Product            Part / Project Description                     Qty  Unit Price  Total Price
                       PowerEdge R760xs [… [EMEA_R760XS]] (210-BGLV)   3   € 27,545.00  € 82,635.00
    Base               PowerEdge R760xs                                1
    Processor          Intel Xeon Gold 5418Y 2G, 24C/48T, …            1
    Memory Capacity    32GB RDIMM, 6400MT/s, Dual Rank                 8
    RAID Controller    PERC H355 Adapter, Low Profile                  1
    BIOS and Advanced System                                                <- module name wraps
    Configuration Settings  Performance BIOS Settings                  1

The system row carries the node count; option quantities are per server
(8 DIMMs each), so totals are qty x nodes — exactly the Dell configurator's
module/option data, read by the same dell_solution.add_option (module
categories, absence lines, dropped settings). There are no SKUs except the
base '(210-…)' one, so HCL matching falls back to descriptions, as for the
DCSC exports.

Cross-checks: unit price x quantity = total per system, and the system
totals add up to the Sub-Total. Visible item-like lines under the table
(the Arrow template carries three stale ones) are recorded as stray.
"""
import re
from typing import List, Optional

from bom.normalize import NormalizedBOM
from bom.parsers.dell_solution import _Group, add_option
from bom.parsers.pdf_common import Evidence, first_word_x, money, whole_number
from bom.parsers.pdf_supermicro_quote import _header

FORMAT = 'dell_arrow_pdf'

_BASE_SKU = re.compile(r'\((\d{3}-[A-Z0-9]{4})\)')
_END = ('integration services', 'freight_charge', 'sub-total')


def detect(doc) -> bool:
    header = next((l for l in doc.lines[:40] if _header(l)), None)
    if header is None:
        return False
    desc_x = first_word_x(header, 'Part')
    qty_x = first_word_x(header, 'Qty')
    after = [l for l in doc.lines if (l.page, l.top) > (header.page, header.top)][:6]
    # A PowerEdge system row, then module rows with a label in the Product column.
    return (any('poweredge' in l.text.lower() and _BASE_SKU.search(l.text) for l in after[:2])
            and sum(1 for l in after if l.words[0].x0 < desc_x - 10
                    and any(abs(w.x0 - qty_x) < 25 for w in l.words)) >= 2)


def parse(doc):
    ev = Evidence()
    header = next(l for l in doc.lines if _header(l))
    desc_x = first_word_x(header, 'Part')
    qty_x = first_word_x(header, 'Qty')
    unit_x = first_word_x(header, 'Unit')
    total_x = first_word_x(header, 'Total')
    systems = []
    cur = None
    label = ''                      # module name wrapped onto a line of its own
    ended = False
    subtotal = None
    item_lines = []
    for line in doc.lines:
        if (line.page, line.top) <= (header.page, header.top):
            continue
        low = line.text.lower()
        if 'sub-total' in low:
            subtotal = money(line.text.split('Sub-Total', 1)[-1])
        if ended or low.startswith(_END):
            ended = True
            left = line.between(0, qty_x - 40).strip()
            if re.match(r'^\d{1,3}\s+\S', left) and len(left) > 12:
                ev.notes.append('stray: %s' % left[:80])
            continue
        qty = next((int(w.text) for w in line.words
                    if abs(w.x0 - qty_x) < 25 and whole_number(w.text) is not None), None)
        module = line.between(0, desc_x - 5).strip()
        desc = line.between(desc_x - 5, qty_x - 20).strip()
        if qty is not None and not module and _BASE_SKU.search(desc):
            title = _BASE_SKU.sub('', desc).strip()
            cur = {'title': title, 'sku': _BASE_SKU.search(desc).group(1), 'nodes': qty, 'rows': [],
                   'unit': money(line.between(unit_x - 12, total_x - 5)),
                   'total': money(line.between(total_x - 12, 10 ** 6))}
            systems.append(cur)
            item_lines.append(line)
            continue
        if qty is None:
            if module and not desc:
                label = ('%s %s' % (label, module)).strip()      # heading / wrapped module name
            elif desc or module:
                ev.unexplained_line(line)
            continue
        if cur is None or not desc:
            ev.unexplained_line(line)
            continue
        cur['rows'].append((('%s %s' % (label, module)).strip(), desc, qty))
        label = ''
        item_lines.append(line)

    configs = []
    grand = 0.0
    for si, s in enumerate(systems, 1):
        grp = _Group('', s['title'], s['nodes'])
        for module, desc, qty in s['rows']:
            ev.rows += 1
            sku = s['sku'] if module.lower() in ('base', 'basis') else None
            add_option(grp, module, desc, sku, qty)
        if s['unit'] is not None and s['total'] is not None:
            ev.check(abs(s['unit'] * s['nodes'] - s['total']) <= 0.01 * max(s['total'], 1),
                     'system %d: unit price x quantity = total' % si)
            grand += s['total']
        if grp.order:
            cfg = grp.config(si)
            cfg.name = ' '.join(s['title'].split())[:120]
            configs.append(cfg)
    if subtotal is not None and grand:
        ev.check(abs(subtotal - grand) <= 0.01 * max(subtotal, 1), 'system totals add up to the sub-total')
    ev.region(item_lines)
    return NormalizedBOM(vendor='Dell', configs=configs, raw_text=doc.text()[:20000]), ev
