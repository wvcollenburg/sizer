"""Distributor item list: numbered systems with numbered sub-items.

The shape a distributor's quoting system exports (first seen from Ingram
Micro, 2026-09-30, 'IM Material' column):

    Item # | IM Material | Vendor Part # | Description                | Qty
    1      | 210-BEQQ    | 210-BEQQ      | PowerEdge R660             | 35
    1.1    |             | 210-BEQQ      | PowerEdge R660 Server      | 35
    1.4    |             | 338-CHTK      | Intel Xeon Gold 6430 …     | 35
    1.5    |             | 338-CHTK      | Intel Xeon Gold 6430 …     | 35
    1.11   |             | 370-BCCY      | 32GB RDIMM, 6400MT/s …     | 70
           |             |               | Propose Alternative Model  |
    2      | 210-BNMR    | 210-BNMR      | PowerEdge R470 - [ASPER470]| 1
    2.1    | …

A whole item number ('1', '2') is a system and its Qty the node count; the
'N.M' rows under it are its parts, with quantities already TOTALS across
the systems (35 servers x 2 DIMMs = 70) — the convention of every parser
here. Duplicate part lines (two CPU rows, one per socket) are summed. The
distributor's own material number is ignored: the vendor part number is
what the HCL knows. A text-only row before a system ('Propose Alternative
Model') becomes part of that config's name. The vendor is read from the
parts (Dell SKUs, Lenovo feature codes, …), so the shape is not tied to one
vendor.
"""
import re
from typing import Dict, List, Optional

from bom.normalize import BOMComponent, BOMConfig, NormalizedBOM
from bom.parsers.common import (
    categorize,
    cell,
    head_rows,
    is_m2_media,
    raw_text_from_rows,
    server_model_from_text,
    sheet_matrix,
    should_drop,
    to_int,
)

FORMAT = 'dist_item_list'

_SYSTEM_ITEM = re.compile(r'^\d{1,3}$')
_PART_ITEM = re.compile(r'^(\d{1,3})\.\d{1,3}$')
_ROLES = {
    'item': ('item #', 'item', 'item no', 'item no.', 'line'),
    'part': ('vendor part #', 'vendor part number', 'mfr part #', 'manufacturer part #',
             'manufacturer part number', 'vpn'),
    'desc': ('description',),
    'qty': ('qty', 'quantity'),
}


def _norm(value) -> str:
    return ' '.join(str(value or '').strip().lower().split())


def _header(rows) -> Optional[Dict[str, int]]:
    """{role: column} when row 1-5 carries item, vendor part, description
    and quantity headers."""
    for r in range(1, min(len(rows), 5) + 1):
        found: Dict[str, int] = {}
        for c in range(1, min(len(rows[r - 1]), 12) + 1):
            label = _norm(cell(rows, r, c))
            for role, labels in _ROLES.items():
                if role not in found and label in labels:
                    found[role] = c
        if len(found) == len(_ROLES):
            found['row'] = r
            return found
    return None


def _sheet(wb):
    for ws in wb.worksheets:
        rows = head_rows(ws, 8, 12)
        cols = _header(rows)
        if cols is None:
            continue
        # The shape proper: a whole item number followed by N.M sub-items.
        items = [cell(rows, r, cols['item']) for r in range(cols['row'] + 1, len(rows) + 1)]
        if any(_SYSTEM_ITEM.match(i) for i in items) and any(_PART_ITEM.match(i) for i in items):
            return ws
    return None


def detect(wb) -> bool:
    return _sheet(wb) is not None


class _System:
    def __init__(self, item: str, title: str, nodes: int, label: str):
        self.item = item
        self.title = title
        self.nodes = max(nodes, 1)
        self.label = label
        self.model = server_model_from_text(title)
        self.order: List[str] = []
        self.items: Dict[str, BOMComponent] = {}

    def add(self, part: Optional[str], desc: str, qty: int):
        if should_drop(desc, part):
            return
        cat = categorize(desc)
        if cat == 'storage' and is_m2_media(desc):
            cat = 'other'
        if cat == 'chassis' and self.model is None:
            self.model = server_model_from_text(desc)
        key = (part or '') + '|' + desc
        if key in self.items:
            self.items[key].quantity += qty
        else:
            self.items[key] = BOMComponent(part_number=part, description=desc,
                                           quantity=qty, category=cat)
            self.order.append(key)

    def config(self) -> BOMConfig:
        name = ' '.join(self.title.split())[:120] or ('Config %s' % self.item)
        if self.label:
            name = ('%s: %s' % (self.label, name))[:120]
        return BOMConfig(name=name, server_model=self.model,
                         components=[self.items[k] for k in self.order],
                         node_count=self.nodes)


def parse(path: str) -> NormalizedBOM:
    from bom.parsers import UnrecognizedFormat, detect_vendor
    from bom.parsers.common import load_workbook_safe
    wb = load_workbook_safe(path)
    try:
        ws = _sheet(wb)
        if ws is None:
            raise UnrecognizedFormat('Not a distributor item list.')
        rows = sheet_matrix(ws)
    finally:
        wb.close()
    cols = _header(rows)
    systems: List[_System] = []
    label = ''
    for r in range(cols['row'] + 1, len(rows) + 1):
        item = cell(rows, r, cols['item'])
        desc = cell(rows, r, cols['desc'])
        part = cell(rows, r, cols['part']) or None
        qty = to_int(cell(rows, r, cols['qty']), default=0)
        if _SYSTEM_ITEM.match(item):
            systems.append(_System(item, desc, qty or 1, label))
            label = ''
            continue
        m = _PART_ITEM.match(item)
        if m and systems and m.group(1) == systems[-1].item:
            if desc:
                systems[-1].add(part, desc, max(qty, 1))
            continue
        if not item and desc and not part and not qty:
            label = desc.strip()                  # 'Propose Alternative Model'
    systems = [s for s in systems if s.order]
    if not systems:
        raise UnrecognizedFormat('The item list has no systems with parts.')
    bom = NormalizedBOM(vendor='Unknown', configs=[s.config() for s in systems],
                        raw_text=raw_text_from_rows(rows))
    bom.vendor = detect_vendor(bom)
    return bom
