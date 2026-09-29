"""Dell Premier "Your quote is ready" e-mail, printed to PDF.

The same quote as the xlsx export (parsers/dell_quote.py), as the partner
usually receives it: an HTML e-mail printed from a mail client. Per system
group:

    Unit Price  Quantity  Subtotal
    PowerEdge R760 - [amer_r760_15724_vi_vp]   $46,434.07   1   $46,434.07
    Description                              SKU       Unit Price  Quantity  Subtotal
    PowerEdge R760 Server                    210-BDZY  -           1         -
    3.5" Chassis with up to 12 SAS/SATA      404-BBED  -           1         -
    Drives, 4x2.5" Rear NVMe Direct …                                     <- same cell

Cells are vertically centred, so a description can wrap above the SKU line
as well as below it: rows are rebuilt as blocks of lines separated by the
row gap, each block holding exactly one SKU line. Mail clients print a row
cut by the page break twice (clipped at the foot of one page, whole at the
head of the next), so a block at the foot of a page that repeats the block
at the head of the next is dropped — only across a page break, because the
same SKU on two rows within a page is real (two CPUs quoted as two lines).

Quantities are totals for the system group, summed per SKU, exactly as the
xlsx export is read. Cross-checks: the summary table at the top lists each
system with its quantity, which must match the group; the group line's unit
price x quantity must equal its subtotal.
"""
import re
from typing import List, Optional

from bom.normalize import BOMConfig, NormalizedBOM
from bom.parsers.common import categorize, server_model_from_text, should_drop
from bom.parsers.dell_quote import _Acc, _config_name, _model_from_server_line
from bom.parsers.pdf_common import (DELL_SKU, Evidence, doc_head, first_word_x,
                                    money, whole_number)

FORMAT = 'dell_quote_pdf'

_SKU_AT_END = re.compile(r'(.*?)(\d{3}-[A-Z0-9]{4})$')


def detect(doc) -> bool:
    head = doc_head(doc, 80)
    if 'quote' not in head:
        return False
    return any(_item_header(l) for l in doc.lines)


def _item_header(line) -> bool:
    words = [w.text.lower() for w in line.words]
    return words[:1] == ['description'] and 'sku' in words and 'quantity' in words


def _group_header(line) -> bool:
    words = [w.text.lower() for w in line.words]
    return (words[:2] == ['unit', 'price'] or words[:1] == ['product']) and 'quantity' in words \
        and 'subtotal' in words and 'description' not in words


def _qty_span(line):
    """x-range of the Quantity header word, widened a little: quantities are
    whole words centred under it (prices to the left run close)."""
    for w in line.words:
        if w.text.lower() == 'quantity':
            return (w.x0 - 12, w.x1 + 12)
    return None


def _qty(line, span) -> Optional[int]:
    if not span:
        return None
    for w in line.words:
        if span[0] <= (w.x0 + w.x1) / 2 <= span[1] and whole_number(w.text) is not None:
            return int(w.text)
    return None


def _sku_split(line):
    """(description part, sku, word index) when the line carries a SKU."""
    for i, w in enumerate(line.words):
        if DELL_SKU.fullmatch(w.text):
            return ' '.join(x.text for x in line.words[:i]), w.text, i
        m = _SKU_AT_END.match(w.text)
        if m and m.group(1) and not m.group(1)[-1].isdigit():
            return ' '.join([x.text for x in line.words[:i]] + [m.group(1)]), m.group(2), i
    return None


def parse(doc):
    ev = Evidence()
    summary = []                      # (product text, qty) from the top table
    groups = []
    cur = None
    mode = None                       # 'summary' | 'group' | 'items'
    qty_span = None
    blocks = []                       # current group's row blocks: list of lines
    item_lines = []

    def close_blocks():
        if cur is not None:
            cur['blocks'].extend(blocks)
        blocks.clear()

    for line in doc.lines:
        low = line.text.lower()
        if _item_header(line):
            qty_span = _qty_span(line)
            mode = 'items'
            continue
        if _group_header(line):
            close_blocks()
            qty_span = _qty_span(line)
            mode = 'summary' if line.words[0].text.lower() == 'product' else 'group'
            continue
        if mode in ('summary', 'group'):
            if low.startswith('subtotal') or low.startswith('estimated delivery'):
                mode = None if mode == 'summary' else 'group-done'
                continue
            qty = _qty(line, qty_span)
            if qty is not None:
                text = ' '.join(w.text for w in line.words if not w.text.startswith(('$', '€', '£'))
                                and (w.x0 + w.x1) / 2 < qty_span[0]).strip()
                prices = [money(w.text) for w in line.words if w.text.startswith(('$', '€', '£'))]
                if mode == 'summary':
                    summary.append([text, qty])
                else:
                    cur = {'name': text, 'qty': qty, 'blocks': [], 'prices': prices}
                    groups.append(cur)
            elif line.text.startswith('[') and (summary if mode == 'summary' else groups):
                target = summary[-1] if mode == 'summary' else None
                if target is not None:
                    target[0] = '%s %s' % (target[0], line.text)
            continue
        if mode != 'items' or cur is None:
            continue
        if low.lstrip().startswith(('subtotal:', 'shipping:', 'total:')) or first_word_x(line, 'Subtotal') is not None:
            close_blocks()
            mode = None
            continue
        size = max((w.size for w in line.words), default=9)
        prev_line = blocks[-1][-1] if blocks else None
        same_row = (prev_line is not None and prev_line.page == line.page
                    and line.top - prev_line.bottom < 0.6 * size)
        if same_row:
            blocks[-1].append(line)
        else:
            blocks.append([line])
        item_lines.append(line)
    close_blocks()

    configs = []
    for gi, g in enumerate(groups, 1):
        rows = []
        for block in g['blocks']:
            anchors = [(i, _sku_split(l)) for i, l in enumerate(block)]
            anchors = [(i, a) for i, a in anchors if a]
            desc_lines = []
            for i, l in enumerate(block):
                hit = next((a for j, a in anchors if j == i), None)
                desc_lines.append(hit[0] if hit else l.text)
            if len(anchors) != 1:
                rows.append({'block': block, 'sku': None, 'desc': ' '.join(desc_lines)})
                continue
            i, (_, sku, _) = anchors[0]
            qty = _qty(block[i], qty_span)
            rows.append({'block': block, 'sku': sku, 'qty': qty,
                         'desc': ' '.join(d for d in desc_lines if d).strip()})
        # Page-break duplicates: the foot-of-page block repeating the next
        # page's first block (clipped: maybe without its SKU line).
        kept = []
        for k, row in enumerate(rows):
            nxt = rows[k + 1] if k + 1 < len(rows) else None
            if (nxt is not None and row['block'][0].page != nxt['block'][0].page
                    and _norm(nxt['desc']).startswith(_norm(row['desc'])[:40])
                    and (row['sku'] is None or row['sku'] == nxt['sku'])):
                ev.notes.append('page-break duplicate dropped: %s' % row['desc'][:60])
                continue
            kept.append(row)
        acc = _Acc()
        model = None
        for row in kept:
            if row['sku'] is None:
                ev.unexplained.append(row['desc'][:160])
                continue
            if row['qty'] is None:
                ev.unexplained.append(row['desc'][:160])
                continue
            ev.rows += 1
            if row['sku'].startswith('210-') and model is None:
                model = _model_from_server_line(row['desc'])
            if should_drop(row['desc'], row['sku']):
                continue
            acc.add(row['sku'], row['desc'], row['qty'], categorize(row['desc']))
        if len(g['prices']) >= 2 and all(p is not None for p in g['prices']):
            ev.check(abs(g['prices'][0] * g['qty'] - g['prices'][-1]) <= 0.01 * max(g['prices'][-1], 1),
                     'group %d: unit price x quantity = subtotal' % gi)
        match = next((q for name, q in summary if _norm(name)[:25] == _norm(g['name'])[:25]), None)
        if summary:
            ev.check(match == g['qty'], 'group %d: quantity matches the quote summary' % gi)
        comps = acc.components()
        if comps:
            configs.append(BOMConfig(name=_config_name(g['name'], gi),
                                     server_model=model or server_model_from_text(g['name']),
                                     components=comps, node_count=g['qty']))
    if summary:
        ev.check(len(summary) == len(groups), 'every system in the summary has its own parts table')
    ev.region(item_lines)
    return NormalizedBOM(vendor='Dell', configs=configs, raw_text=doc.text()[:20000]), ev


def _norm(text: Optional[str]) -> str:
    return re.sub(r'[^a-z0-9]', '', (text or '').lower())
