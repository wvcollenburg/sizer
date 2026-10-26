"""Lenovo "Build Order" sheet (ReportLab PDF).

A one-page-and-a-bit cluster build order for a ThinkSystem server:

    HC5650D Build Order                     3 Hosts | Generated: March 18, 2026
    CPU: 2x Intel Xeon 6507P … | RAM: 16x … | Storage: 3x 12TB SAS HDD + 1x 3.84TB NVMe | NIC: …
    Feature Code  Description                           Lenovo Part  Notes                  Qty
    FIXED PARTS — ThinkSystem SR650 V4 (x3 hosts)
    C3QL          ThinkSystem SR650 V4 12x3.5" Chassis  7DGDCTO1WW                          3
    SELECTED OPTIONS (x3 hosts)
    Processor
    C5R6          Intel Xeon 6507P 8C 150W 3.5GHz …                  2x per host (dual …)   6

The table is ruled: every row sits between two horizontal lines, and a cell
that wraps is vertically centred, so the feature code and quantity sit on
the middle line of a three-line description. Rows are therefore the bands
between the rules, never text lines. Quantities are cluster totals (the
sheet says so), the host count is the node count.

Part number: the Lenovo part number when the row gives one (that is the key
the HCL lists), the feature code otherwise — the DCSC convention.

Cross-checks from the sheet itself: the host count in the title and in the
section headers agree; the base server row's quantity is the host count;
"Nx per host" notes times the hosts equal the row's quantity; and the
summary line's CPU / DIMM / drive counts per host match the parsed rows.
"""
import re
from typing import List

from bom.normalize import BOMComponent, BOMConfig
from bom.parsers.common import LENOVO_BUILD_CODE, LENOVO_PART, categorize_line, server_model_from_text
from bom.parsers.pdf_common import (Evidence, doc_head, finish, first_word_x, ruled_bands,
                                    whole_number)

FORMAT = 'lenovo_build_pdf'

_HOSTS = re.compile(r'\b(\d{1,3})\s+hosts?\b', re.I)
_SECTION_HOSTS = re.compile(r'\(x(\d{1,3})\s+hosts?\)', re.I)
_PER_HOST = re.compile(r'\b(\d{1,3})x\s+per\s+(?:host|node)\b', re.I)


def _header(line) -> bool:
    low = line.text.lower()
    return low.startswith('feature code') and 'description' in low and low.rstrip().endswith('qty')


def detect(doc) -> bool:
    return 'build order' in doc_head(doc, 12) and any(_header(l) for l in doc.lines[:40])


def _columns(line):
    return {
        'desc': first_word_x(line, 'Description'),
        'part': first_word_x(line, 'Lenovo'),
        'notes': first_word_x(line, 'Notes'),
        'qty': first_word_x(line, 'Qty'),
    }


def parse(doc):
    ev = Evidence()
    head = doc_head(doc, 6)
    m = _HOSTS.search(head)
    hosts = int(m.group(1)) if m else None
    summary = ' '.join(l.text for l in doc.lines[:8])
    model = None
    section_hosts = set()
    comps: List[BOMComponent] = []
    per_host_notes = []
    item_lines = []
    cols = None

    for page in doc.pages:
        header = next((l for l in page.lines if _header(l)), None)
        if header is None:
            continue
        cols = _columns(header)
        if None in cols.values():
            ev.unexplained_line(header)
            continue
        qty_lo = cols['qty'] - 12
        # Rules closer than 5pt are the double rule under a section heading.
        for band in ruled_bands(page, header.bottom, min_height=5):
            text = ' '.join(l.text for l in band)
            low = text.lower()
            if low.startswith('note:'):
                break
            fc = ' '.join(l.between(0, cols['desc'] - 2) for l in band).split()
            qty_words = [w for l in band for w in l.words
                         if w.x0 >= qty_lo and whole_number(w.text) is not None]
            if not qty_words:
                # A section or category heading — unless it looks like an item
                # row that lost its quantity (a feature code in the first
                # column), which must cost certainty, not vanish.
                if fc and LENOVO_BUILD_CODE.match(fc[0]):
                    ev.unexplained.append(text[:160])
                    continue
                sec = _SECTION_HOSTS.search(text)
                if sec:
                    section_hosts.add(int(sec.group(1)))
                    if low.startswith('fixed parts') and model is None:
                        model = server_model_from_text(text)
                continue
            if len(fc) != 1 or len(qty_words) != 1:
                ev.unexplained.append(text[:160])
                continue
            desc = ' '.join(filter(None, (l.between(cols['desc'] - 2, cols['part'] - 2).strip()
                                          for l in band)))
            part = ''.join(l.between(cols['part'] - 2, cols['notes'] - 2).replace(' ', '')
                           for l in band)
            notes = ' '.join(filter(None, (l.between(cols['notes'] - 2, qty_lo).strip() for l in band)))
            qty = int(qty_words[0].text)
            code = fc[0]
            if not LENOVO_BUILD_CODE.match(code):
                ev.pattern_misses.append(code)
            if part and not LENOVO_PART.match(part):
                ev.pattern_misses.append(part)
            comps.append(BOMComponent(part_number=part or code, description=desc,
                                      quantity=qty, category=categorize_line(desc)))
            ev.rows += 1
            item_lines.extend(band)
            per = _PER_HOST.search(notes)
            if per:
                per_host_notes.append((code, int(per.group(1)), qty))

    nodes = hosts or (min(section_hosts) if section_hosts else None)
    if hosts is not None and section_hosts:
        ev.check(section_hosts == {hosts}, 'host count in the title matches the sections')
    base = next((c for c in comps if c.category == 'chassis'
                 and re.search(r'base server|chassis', c.description, re.I)), None)
    if base is not None and nodes:
        ev.check(base.quantity == nodes, 'base server quantity equals the host count')
    for code, per, qty in per_host_notes:
        if nodes:
            ev.check(per * nodes == qty, '%s: %dx per host x %d hosts = %d' % (code, per, nodes, qty))
    if nodes:
        _summary_checks(ev, summary, comps, nodes)

    configs = [BOMConfig(name='Config 1', server_model=model, components=comps,
                         node_count=nodes)] if comps else []
    return finish(doc, ev, 'Lenovo', configs, item_lines)


def _summary_checks(ev: Evidence, summary: str, comps: List[BOMComponent], nodes: int):
    """'CPU: 2x … | RAM: 16x … | Storage: 3x … + 1x …' against the rows."""
    def total(cat):
        return sum(c.quantity for c in comps if c.category == cat)

    for label, cat in (('CPU', 'cpu'), ('RAM', 'memory')):
        m = re.search(r'\b%s:\s*(\d+)x' % label, summary)
        if m:
            ev.check(int(m.group(1)) * nodes == total(cat),
                     'summary %s count per host matches the rows' % label)
    m = re.search(r'\bStorage:\s*([^|]+)', summary)
    if m:
        drives = sum(int(n) for n in re.findall(r'(\d+)x', m.group(1)))
        if drives:
            ev.check(drives * nodes == total('storage'), 'summary drive count per host matches the rows')
