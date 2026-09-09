"""Hand-made Dell configuration lists (three layouts a reseller types up).

Why they get deterministic parsers at all: they are tiny, but they arrived
three times in the archive and each time SC//Design's LLM read the
quantities differently ('QTY 3' applied to every line while the '8x' prefix
was left inside the description). The layouts are rigid enough to recognise
from row 1 alone, so we can do better than a best-effort guess:

  dell_list_sku          A1 'QTY n', B1 '<config name>', C1 'Available SKUs';
                         rows: B description (optional 'Nx ' prefix), C SKU
                         or comma list, or '(Dell - Hynix PN) HMCG78AEBRA107N'.
  dell_list_qty_desc_pn  A1 'QTY', C1 'Description', D1 'Part Number'; rows:
                         A per-node quantity, C description, D part number
                         (reseller/OEM numbers, not always Dell SKUs).
  dell_list_columns      Row 1 = 'Config 1', 'Config 2', … one column per
                         config; row 2 '3x PowerEdge R660xs' gives node
                         count + model; other rows plain descriptions with
                         optional 'Nx ' prefixes; no SKUs at all.

Quantity convention (the same as every other parser): totals across the
config's machines. A 'Nx' prefix is the per-machine count, so the emitted
quantity is N × node_count, and the prefix is stripped from the description
so categorize() and the rules see '3.84TB NVMe …' rather than '4x 3.84TB'.

A workbook may mix layouts across sheets (the 'Additional AFCO' file has one
of each); configs are numbered across the workbook and suffixed with the
sheet title when there is more than one sheet ('Config 2 - R750XS').
"""
import re
from typing import Dict, List, Optional

from bom.normalize import BOMComponent, BOMConfig, NormalizedBOM
from bom.parsers.common import (
    categorize,
    cell,
    head_rows,
    raw_text_from_rows,
    s,
    server_model_from_text,
    sheet_matrix,
    should_drop,
    split_nx,
    to_int,
)

FORMAT_SKU = 'dell_list_sku'
FORMAT_QTY_DESC_PN = 'dell_list_qty_desc_pn'
FORMAT_COLUMNS = 'dell_list_columns'

_QTY_N = re.compile(r'^QTY\s+(\d+)$', re.I)
_CONFIG_LABEL = re.compile(r'^Config\s+\d+$', re.I)
_PN_PREFIX = re.compile(r'^\([^)]*\)\s*')


def _layout(rows) -> Optional[str]:
    a1, b1, c1, d1 = (cell(rows, 1, c) for c in range(1, 5))
    if _QTY_N.match(a1) and c1 == 'Available SKUs':
        return FORMAT_SKU
    if a1.upper() == 'QTY' and c1 == 'Description' and d1 == 'Part Number':
        return FORMAT_QTY_DESC_PN
    if rows:
        labels = [s(v) for v in rows[0] if s(v)]
        if labels and all(_CONFIG_LABEL.match(v) for v in labels):
            return FORMAT_COLUMNS
    return None


def detect(wb) -> Optional[str]:
    """Format id of the first recognisable sheet, else None."""
    for ws in wb.worksheets:
        fmt = _layout(head_rows(ws, 2, 12))
        if fmt:
            return fmt
    return None


class _Acc:
    def __init__(self):
        self.order: List[str] = []
        self.items: Dict[str, BOMComponent] = {}

    def add(self, pn: Optional[str], desc: str, qty: int, category: str):
        key = (pn or '') + '|' + desc
        if key in self.items:
            self.items[key].quantity += qty
        else:
            self.items[key] = BOMComponent(part_number=pn, description=desc,
                                           quantity=qty, category=category)
            self.order.append(key)

    def components(self) -> List[BOMComponent]:
        return [self.items[k] for k in self.order]


def _first_pn(cell_text: str) -> Optional[str]:
    tokens = [_PN_PREFIX.sub('', t.strip()).strip() for t in cell_text.split(',')]
    tokens = [t for t in tokens if t]
    return tokens[0] if tokens else None


def _parse_sku_sheet(rows, index: int, suffix: str) -> BOMConfig:
    node_count = int(_QTY_N.match(cell(rows, 1, 1)).group(1))
    name = cell(rows, 1, 2) or 'Config %d' % index
    acc = _Acc()
    model = None
    for r in range(2, len(rows) + 1):
        desc = cell(rows, r, 2)
        if not desc:
            continue
        n, desc = split_nx(desc)
        pn = _first_pn(cell(rows, r, 3))
        if model is None and desc.startswith('PowerEdge'):
            model = server_model_from_text(desc)
        if should_drop(desc, pn):
            continue
        acc.add(pn, desc, n * node_count, categorize(desc))
    return BOMConfig(name=name + suffix, server_model=model, components=acc.components(),
                     node_count=node_count)


def _parse_qty_desc_pn_sheet(rows, index: int, suffix: str) -> BOMConfig:
    acc = _Acc()
    model = None
    for r in range(2, len(rows) + 1):
        desc = cell(rows, r, 3)
        if not desc:
            continue
        # Hand-typed lists put junk in the quantity column ('2 ea', 'TBD', a
        # repeated 'QTY' sub-header); coerce like every other parser instead
        # of crashing the whole upload on one cell.
        qty = max(1, to_int(cell(rows, r, 1), default=1))
        pn = cell(rows, r, 4) or None
        if model is None:
            model = server_model_from_text(desc)
        if should_drop(desc, pn):
            continue
        acc.add(pn, desc, qty, categorize(desc))
    return BOMConfig(name='Config %d' % index + suffix, server_model=model,
                     components=acc.components(), node_count=1)


def _parse_columns_sheet(rows, index: int) -> List[BOMConfig]:
    configs = []
    for c in range(1, len(rows[0]) + 1):
        label = cell(rows, 1, c)
        if not _CONFIG_LABEL.match(label):
            continue
        acc = _Acc()
        node_count = 1
        model = None
        for r in range(2, len(rows) + 1):
            desc = cell(rows, r, c)
            if not desc:
                continue
            n, desc = split_nx(desc)
            if model is None and server_model_from_text(desc) and categorize(desc) == 'chassis':
                # '3x PowerEdge R660xs' — the node count and the model.
                model = server_model_from_text(desc)
                node_count = n
                acc.add(None, desc, n, 'chassis')
                continue
            if should_drop(desc):
                continue
            acc.add(None, desc, n, categorize(desc))
        # Per-machine counts become totals once the node count is known.
        for comp in acc.components():
            if comp.category != 'chassis' or comp.description != (model or ''):
                comp.quantity *= node_count
        configs.append(BOMConfig(name=label, server_model=model,
                                 components=acc.components(), node_count=node_count))
        index += 1
    return configs


def parse(path: str) -> NormalizedBOM:
    from bom.parsers import UnrecognizedFormat
    from bom.parsers.common import load_workbook_safe
    wb = load_workbook_safe(path)
    try:
        sheets = [(ws.title, sheet_matrix(ws)) for ws in wb.worksheets]
    finally:
        wb.close()
    configs: List[BOMConfig] = []
    raw_parts = []
    multi = len(sheets) > 1
    for title, rows in sheets:
        fmt = _layout(rows)
        if fmt is None:
            continue
        index = len(configs) + 1
        suffix = ' - ' + title.strip() if multi else ''
        if fmt == FORMAT_SKU:
            configs.append(_parse_sku_sheet(rows, index, suffix))
        elif fmt == FORMAT_QTY_DESC_PN:
            configs.append(_parse_qty_desc_pn_sheet(rows, index, suffix))
        else:
            configs.extend(_parse_columns_sheet(rows, index))
        raw_parts.append('=== Sheet: %s ===\n%s' % (title, raw_text_from_rows(rows)))
    if not configs:
        raise UnrecognizedFormat('Not a recognised Dell configuration list.')
    return NormalizedBOM(vendor='Dell', configs=configs, raw_text='\n'.join(raw_parts)[:60000])
