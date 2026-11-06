"""Lenovo bid quote ("Bid Request No. BRDND…") printed to PDF.

The same document as parsers/lenovo_bid.py reads from Word, printed: the
customer table, PRODUCT AND SERVICE DETAILS (Part Number | Description | F/B |
Qty | Start Date | End Date), CONFIGURATION DETAILS (Components | Description
| Qty), the distributor and reseller tables, the terms. Word prints its table
borders, so a row is the band between two rules (a wrapped description stays
in its row); an unruled print falls back to one text line per row, with a
line that has neither a code nor a quantity taken as the previous row's
wrapped description.

The rows go through lenovo_bid.configs_from_rows, so the Word file and its
PDF are read by the same code: per-server feature-code quantities times the
machine line's server count.

Cross-checks from the quote itself: each machine line's count equals the
product table's quantity of the same part, and every server part in the
product table has a configuration.
"""
import re
from typing import List, Optional

from bom.parsers import lenovo_bid
from bom.parsers.pdf_common import (Evidence, doc_head, finish, first_word_x, ruled_bands,
                                    whole_number)

FORMAT = 'lenovo_bid_pdf'

_END = re.compile(r'^(?:distributor\b|reseller\b|terms and conditions\b|thank you\b)')
_PAGE_NO = re.compile(r'^page\s+\d+(?:\s+of\s+\d+)?$', re.I)


def _words(line):
    return [w.text.lower() for w in line.words]


def _config_header(line) -> bool:
    w = _words(line)
    return len(w) >= 3 and w[0] == 'components' and 'description' in w and w[-1] == 'qty'


def _product_header(line) -> bool:
    low = line.text.lower()
    return low.startswith('part number') and 'description' in low and ' qty' in low


def detect(doc) -> bool:
    return (lenovo_bid.BID_MARK.search(doc_head(doc, 40)) is not None
            and any(_config_header(l) for l in doc.lines))


def _bands(doc, start):
    """Row bands of every page from ``start`` (a line) on."""
    out = []
    for page in doc.pages:
        if page.index < start.page:
            continue
        start_y = start.top - 1 if page.index == start.page else float('-inf')
        out.extend(ruled_bands(page, start_y, keep_loose=True, min_rules=3))
    return out


def _read_row(band, desc_x, qty_x):
    """(code, description, qty) of one band; code '' when the band has no
    word left of the description column."""
    words = [w for l in band for w in l.words]
    code = ''.join(w.text for w in words if w.x0 < desc_x - 2)
    qty_words = [w for w in words if w.x0 >= qty_x - 15 and whole_number(w.text) is not None]
    qty = whole_number(qty_words[0].text) if len(qty_words) == 1 else None
    desc = ' '.join(w.text for w in words if desc_x - 2 <= w.x0 < qty_x - 15).strip()
    return code, desc, qty, len(qty_words)


def parse(doc):
    ev = Evidence()
    config_header = next(l for l in doc.lines if _config_header(l))
    product_header = next((l for l in doc.lines if _product_header(l)), None)

    products = []
    if product_header is not None:
        p_desc = first_word_x(product_header, 'Description')
        p_qty = first_word_x(product_header, 'Qty')
        for band in _bands(doc, product_header):
            if (band[0].page, band[0].top) >= (config_header.page, config_header.top):
                break
            if any(_product_header(l) for l in band):
                continue
            code, desc, qty, _n = _read_row(band, p_desc, p_qty)
            if code and lenovo_bid.is_block_code(code):
                products.append((code, desc, qty))

    desc_x = first_word_x(config_header, 'Description')
    qty_x = first_word_x(config_header, 'Qty')
    rows: List[lenovo_bid.Row] = []
    item_lines = []
    for band in _bands(doc, config_header):
        text = ' '.join(l.text for l in band).strip()
        low = text.lower()
        if any(_config_header(l) for l in band):          # repeated on every page
            continue
        if _END.match(low):
            break
        if _PAGE_NO.match(text):
            continue
        code, desc, qty, n_qty = _read_row(band, desc_x, qty_x)
        if not code:
            if qty is None and n_qty == 0 and rows and desc:
                # An unruled print: the previous row's description wrapped.
                prev = rows[-1]
                rows[-1] = (prev[0], (prev[1] + ' ' + desc).strip(), prev[2])
                item_lines.extend(band)
            else:
                ev.unexplained.append(text[:160])
            continue
        if n_qty != 1:
            ev.unexplained.append(text[:160])
            continue
        if not (lenovo_bid.is_block_code(code) or lenovo_bid.is_feature_code(code)):
            ev.pattern_misses.append(code)
        rows.append((code, desc, qty))
        item_lines.extend(band)
        ev.rows += 1

    problems: List[str] = []
    configs = lenovo_bid.configs_from_rows(rows, problems)
    ev.unexplained.extend(p[:160] for p in problems)

    if products:
        counts = lenovo_bid.product_quantities(products)
        mismatches = lenovo_bid.machine_count_problems(configs, counts)
        for cfg in configs:
            line = _machine_line(cfg)
            if line is not None and line.part_number in counts:
                bad = [m for m in mismatches if m.startswith(line.part_number + ':')]
                ev.check(not bad, bad[0] if bad else
                         '%s: %d servers, as in the product list' % (line.part_number, cfg.node_count))
        machines = {(_machine_line(c).part_number if _machine_line(c) else None) for c in configs}
        servers = [code for code, desc, _q in products if lenovo_bid.is_machine(code, desc)]
        missing = [code for code in servers if code not in machines]
        ev.check(not missing, 'every server in the product list has a configuration%s'
                 % (' (not: %s)' % ', '.join(missing) if missing else ''))
    return finish(doc, ev, 'Lenovo', configs, item_lines)


def _machine_line(cfg) -> Optional[object]:
    return next((c for c in cfg.components if c.category == 'chassis' and c.part_number
                 and lenovo_bid.is_block_code(c.part_number)), None)
