"""A Lenovo DCSC configuration retyped into a numbered PDF table.

Partners and distributors re-issue the DCSC quote under their own letterhead
(first seen 2026-09-30, an Acrobat PDF):

    S/N  Part Number  Description                                        Qty
         Lenovo Server - 12C                                             <- title
    1    7D73CTO1WW   Server : ThinkSystem SR630 V3-3yr Base Warranty    35
    2    BLK3         ThinkSystem V3 1U 4x3.5" Chassis                   35
    5    BQ67         Intel Xeon Silver 4410Y 12C 150W 2.0GHz Processor  35
    6    BKTM         ThinkSystem 32GB TruDDR5 4800MHz (2Rx8) RDIMM      140
    …
    57   7S0XCTO5WW   XClarity Controller Platin-FOD                     35  <- software
    59   7Q01CTS2WW   SERVER PREMIER NBD RESP                            35  <- service

It is the DCSC export without its blank separator rows: a machine CTO row
starts a block and carries the node count, feature-code rows follow with
quantities totalled across the machines, and software / service CTO rows
start blocks of their own. So the rows are regrouped into DCSC blocks and
handed to lenovo_dcsc.configs_from_blocks — the xlsx and the PDF of the
same quote are read by the same code.

Cross-checks: the S/N column must run 1..N without a gap or a repeat (a row
the reader missed or merged shows up here), and every hardware row of a
machine block must be a whole multiple of the machine count (DCSC totals
are per-node quantities x nodes).
"""
import re
from typing import List, Optional

from bom.normalize import NormalizedBOM
from bom.parsers.common import LENOVO_CTO
from bom.parsers.lenovo_dcsc import configs_from_blocks
from bom.parsers.pdf_common import Evidence, first_word_x, whole_number

FORMAT = 'lenovo_list_pdf'

# Block starters: machine / software CTOs (7D73CTO1WW, 7S0XCTO5WW) and
# service CTOs (7Q01CTS2WW).
_BLOCK = re.compile(r'^[0-9A-Z]{4}CT[OS0-9][0-9A-Z]WW$')
_PART = re.compile(r'^(?:[A-Z0-9]{4}|[0-9A-Z]{4}CT[OS0-9][0-9A-Z]WW|\d{4}[A-Z]{2}\d|[0-9][A-Z0-9]{9})$')


def _header(line) -> bool:
    words = [w.text.lower() for w in line.words]
    return (len(words) >= 5 and words[0] in ('s/n', 'sn', 'no', 'no.', '#') and 'part' in words
            and 'description' in words and words[-1] in ('qty', 'quantity'))


def detect(doc) -> bool:
    header = next((l for l in doc.lines[:30] if _header(l)), None)
    if header is None:
        return False
    return any(LENOVO_CTO.match(w.text) for l in doc.lines[:80] for w in l.words)


def _rows(doc, header):
    """(line groups) per table row: the bands between horizontal rules when
    the table is ruled, one text line per row otherwise."""
    out = []
    for page in doc.pages:
        lines = [l for l in page.lines if page.index > header.page or l.top > header.bottom]
        rules = [y for y in page.hrules if page.index > header.page or y >= header.bottom - 1]
        if len(rules) >= 3:
            used = set()
            for a, b in zip(rules, rules[1:]):
                band = [l for l in lines if a < (l.top + l.bottom) / 2 < b]
                if band:
                    out.append(band)
                    used.update(id(l) for l in band)
            out.extend([l] for l in lines if id(l) not in used)
        else:
            out.extend([l] for l in lines)
    out.sort(key=lambda band: (band[0].page, band[0].top))
    return out


def parse(doc):
    ev = Evidence()
    header = next(l for l in doc.lines if _header(l))
    part_x = first_word_x(header, 'Part')
    qty_x = first_word_x(header, 'Qty', 'Quantity')
    blocks: List[dict] = []
    names: List[str] = []
    serials: List[int] = []
    item_lines = []
    title: Optional[str] = None
    for band in _rows(doc, header):
        words = [w for l in band for w in l.words]
        first = band[0].words[0]
        sn = whole_number(first.text) if first.x0 < part_x - 3 else None
        qty_word = next((w for w in reversed(words) if w.x0 >= qty_x - 15), None)
        qty = whole_number(qty_word.text) if qty_word is not None else None
        if sn is None:
            text = ' '.join(w.text for w in words)
            if qty is None and not blocks and title is None:
                title = text                          # 'Lenovo Server - 12C'
            elif qty is None and text:
                if blocks:
                    ev.unexplained.append(text[:160])
            else:
                ev.unexplained.append(text[:160])
            continue
        body = [w for w in words if w is not first and w is not qty_word]
        if qty is None or not body:
            ev.unexplained.append(' '.join(w.text for w in words)[:160])
            continue
        part = body[0].text
        desc = ' '.join(w.text for w in body[1:]).strip()
        if not _PART.match(part):
            ev.pattern_misses.append(part)
        serials.append(sn)
        item_lines.extend(band)
        ev.rows += 1
        if _BLOCK.match(part):
            blocks.append({'sku': part, 'title': desc, 'qty': qty, 'row': sn, 'rows': []})
            if LENOVO_CTO.match(part) and 'thinksystem' in desc.lower():
                names.append(title or '')
                title = None
        elif blocks:
            blocks[-1]['rows'].append((sn, part, desc, qty))
        else:
            ev.unexplained.append(' '.join(w.text for w in words)[:160])

    ev.check(serials == list(range(1, len(serials) + 1)),
             'the S/N column runs 1..%d without gaps' % len(serials))
    for block in blocks:
        if LENOVO_CTO.match(block['sku']) and block['qty'] and 'thinksystem' in block['title'].lower():
            odd = [fc for _sn, fc, _d, q in block['rows'] if q % block['qty']]
            ev.check(not odd, '%s: every row is a multiple of %d machines%s'
                     % (block['sku'], block['qty'], (' (not: %s)' % ', '.join(odd)) if odd else ''))
    configs = configs_from_blocks(blocks, True, names=[n or None for n in names] if any(names) else None)
    ev.region(item_lines)
    return NormalizedBOM(vendor='Lenovo', configs=configs, raw_text=doc.text()[:20000]), ev
