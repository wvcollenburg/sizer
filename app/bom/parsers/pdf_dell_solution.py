"""Dell Solutions Configurator, printed to PDF (English and German).

The same solution the xlsx export carries (parsers/dell_solution.py), laid
out as a web page and printed by the configurator (wkhtmltopdf):

    PowerEdge R760 -    Category: Server    Quantity: 3    Unit Price $61,293.31
    [AMER_R760_15724_VI_VP]
    Group Name: Group 1
    Group ID: 20731299.1.1                    Extended Price (List) $183,879.93
    Product Name                      Qty  Option List Price  Option ID  SKUs
    Processor                                                    <- module heading
    Intel® Xeon® Silver 4514Y 2G, …    1   $1,596.00          GEN5JCH    338-CPBZ
    HT (150W) DDR5-4400                                          <- wrapped description

Module headings and wrapped continuation lines look alike (a line with only
description text); they are told apart by the gap above them: a wrapped line
sits tight under its option row, a heading has the paragraph gap. SKUs can
wrap too ('405-' / 'AAXY750-' / 'ACFR' = 405-AAXY and 750-ACFR). Columns are
cut at character level from the header row's positions, because the
description column can run into the quantity ('… 1 or 2' + '1').

Quantities are per server and the group's Quantity is the node count, so
totals are qty x nodes (same as the xlsx export). Cross-checks from the
document itself: the option prices of a group add up to its Unit Price, and
Unit Price x Quantity is its Extended Price. A missed or misread row breaks
the first; both feed the certainty score.
"""
import re
from typing import List, Optional

from bom.normalize import NormalizedBOM
from bom.parsers.dell_solution import _Group, add_option
from bom.parsers.pdf_common import (DELL_SKU, Evidence, doc_head, first_word_x,
                                    money, whole_number)

FORMAT = 'dell_solution_pdf'

_CATEGORY = ('category', 'kategorie')
_QUANTITY = ('quantity', 'menge')
_GROUP_NAME = ('group name', 'gruppenname')
_GROUP_ID = ('group id', 'gruppen-id')
_PRODUCT_NAME = ('product name', 'produktname')
_UNIT_PRICE = ('unit price', 'preis pro einheit')
_EXTENDED = ('extended price', 'erweiterter preis')


def _starts(low: str, labels) -> Optional[str]:
    for label in labels:
        if low.startswith(label):
            return label
    return None


def _is_group_header(line) -> bool:
    return (first_word_x(line, *_CATEGORY) is not None
            and first_word_x(line, *_QUANTITY) is not None)


def _is_footer(low: str) -> bool:
    return low.startswith('@2011 dell') or ('published' in low and (
        'confidential' in low or 'vertraulich' in low))


def detect(doc) -> bool:
    head = doc_head(doc, 60)
    if 'components' not in head and 'komponenten' not in head:
        return False
    lines = doc.lines[:80]
    return (any(_is_group_header(l) for l in lines)
            and any(_starts(l.text.lower(), _PRODUCT_NAME) and 'skus' in l.text.lower()
                    for l in lines))


class _PdfGroup:
    def __init__(self, product: str, nodes: int, unit_price: Optional[float]):
        self.product = product
        self.name = ''
        self.nodes = nodes
        self.unit_price = unit_price
        self.extended = None
        self.rows = []


def parse(doc):
    ev = Evidence()
    groups: List[_PdfGroup] = []
    cur: Optional[_PdfGroup] = None
    cols = None
    module = ''
    last_row = None
    prev = None
    in_product = False
    in_table = False
    item_lines = []

    for line in doc.lines:
        text = line.text.strip()
        low = text.lower()
        if _is_footer(low):
            continue
        if _is_group_header(line):
            cat_x = first_word_x(line, *_CATEGORY)
            qty_x = first_word_x(line, *_QUANTITY)
            product = line.between(0, cat_x).strip().rstrip('-').strip()
            nodes = whole_number(line.between(qty_x, qty_x + 60).split(':')[-1].split()[0]
                                 if ':' in line.between(qty_x, qty_x + 60) else '')
            price_label = next((l for l in _UNIT_PRICE if l in low), None)
            unit = money(low.split(price_label, 1)[1]) if price_label else None
            if nodes is None:
                ev.unexplained_line(line)
                nodes = 1
            cur = _PdfGroup(product, nodes, unit)
            groups.append(cur)
            in_product, in_table, cols, module, last_row, prev = True, False, None, '', None, None
            continue
        if cur is None:
            continue
        label = _starts(low, _GROUP_NAME)
        if label:
            cur.name = text[len(label):].lstrip(': ').strip()
            in_product = False
            continue
        if _starts(low, _GROUP_ID):
            ext_label = next((l for l in _EXTENDED if l in low), None)
            if ext_label:
                cur.extended = money(low.split(ext_label, 1)[1])
            in_product = False
            continue
        if in_product:
            cur.product = ('%s %s' % (cur.product, text)).strip()
            continue
        if _starts(low, _PRODUCT_NAME) and 'skus' in low:
            qty_x = line.words[1].x0 if len(line.words) > 1 else None
            xs = [w.x0 for w in line.words]
            # Product Name | Qty | <price> | <option id> | SKUs, in that order.
            sku_x = first_word_x(line, 'SKUs')
            right = [x for x in xs if x > xs[0] + 100]
            if sku_x is None or len(right) < 4:
                ev.unexplained_line(line)
                continue
            qty_x, price_x, optid_x = right[0], right[1], right[-2]
            cols = (qty_x - 2, price_x - 2, optid_x - 2, sku_x - 2)
            in_table, header_line = True, line
            continue
        if low.startswith('*'):
            in_table = False              # the price disclaimer closes the table
            continue
        if not in_table or cols is None:
            continue
        qty_lo, price_lo, optid_lo, sku_lo = cols
        desc = line.between(0, qty_lo).strip()
        qty_t = line.between(qty_lo, price_lo).strip()
        price_t = line.between(price_lo, optid_lo).strip()
        optid_t = line.between(optid_lo, sku_lo).strip()
        sku_t = line.between(sku_lo, 10 ** 6).replace(' ', '')
        # The header's second line ('ID' / 'der Option ID') has no description.
        if not desc and not qty_t and prev is None and line.top - header_line.bottom < 20:
            continue
        qty = whole_number(qty_t)
        if qty is not None and (sku_t or optid_t):
            last_row = {'module': module, 'desc': desc, 'qty': qty,
                        'price': money(price_t), 'skus': sku_t, 'line': line}
            cur.rows.append(last_row)
            item_lines.append(line)
            prev = line
            continue
        if qty_t or price_t or optid_t:
            ev.unexplained_line(line)
            prev = line
            continue
        gap = (line.top - prev.bottom) if (prev is not None and prev.page == line.page) else None
        size = max((w.size for w in line.words), default=8)
        if last_row is not None and ((gap is not None and gap < 0.6 * size) or (sku_t and not desc)):
            if desc:
                last_row['desc'] = ('%s %s' % (last_row['desc'], desc)).strip()
            last_row['skus'] += sku_t
            item_lines.append(line)
        else:
            module = desc
            last_row = None
        prev = line

    configs = []
    for index, g in enumerate(groups, 1):
        grp = _Group(g.name, g.product, g.nodes)
        for row in g.rows:
            skus = DELL_SKU.findall(row['skus'])
            if not skus:
                ev.pattern_misses.append(row['desc'][:80] or row['skus'])
            ev.rows += 1
            add_option(grp, row['module'], row['desc'], skus[0] if skus else None, row['qty'])
        if not grp.order:
            continue
        prices = [r['price'] for r in g.rows]
        if g.unit_price is not None and all(p is not None for p in prices):
            ev.check(abs(sum(prices) - g.unit_price) <= max(1.0, 0.005 * g.unit_price),
                     'group %d: option prices add up to the unit price' % index)
        if g.unit_price is not None and g.extended is not None:
            ev.check(abs(g.unit_price * g.nodes - g.extended) <= max(1.0, 0.005 * g.extended),
                     'group %d: unit price x quantity = extended price' % index)
        configs.append(grp.config(index))
    ev.region(item_lines)
    return NormalizedBOM(vendor='Dell', configs=configs, raw_text=doc.text()[:20000]), ev
