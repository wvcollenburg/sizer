"""Dell quote-style workbooks: the 'Your quote is ready' export, the D&H
distributor bid sheet and the VNET configurator module export.

Three shapes share this module because they are the same idea — one SKU per
line with a description and a quantity, grouped into systems — expressed
in three layouts:

  dell_quote  Sheet2 of Dell's own export. Header 'SKU | Description | Qty'
              at row 1 or, when a "Dear Customer" letter precedes it, at row
              12; the Qty column moves (K vs H) so it is located by header
              text, never by letter. A group header is a row with no SKU, an
              item name and a digit quantity (= number of systems); a second
              group is introduced by a *repeated* header row. Quantities are
              totals for the group (two CPU rows × 3 systems → 6), summed per
              SKU. Detection also requires a 'Quote number:' cell somewhere
              in the workbook so an arbitrary SKU/Description sheet is not
              mistaken for a Dell quote.
  dh_bid      'DH Quotation' sheet, header at row ~22 ('Bid Line No.',
              'Manufacturer Part #', 'Description', 'Quantity'). A config
              starts on the all-caps 'POWEREDGE R760, CHAMPION PE' bid line;
              the following 210- line names the server. Quantities are as
              listed for the champion line's system count.
  dell_vnet   'Module Name | Option ID | Option Name | SKUs | Qty'. The
              module name is the category key (Processor, Memory Capacity,
              Hard Drives, OCP 3.0 Network Adapters, Boot Optimized Storage
              Cards...). Comma-listed SKUs keep the first as part number.

Every shape drops the same junk via common.should_drop and keeps absence
indicators ('LOM Blank', 'BOSS Blank', 'No RAID') as 'other'.
"""
import re
from typing import Dict, List, Optional, Tuple

from bom.normalize import BOMConfig, NormalizedBOM
from bom.parsers.common import (
    DELL_SKU,
    ComponentAcc,
    categorize_line,
    cell,
    head_rows,
    is_absence,
    model_from_server_line,
    raw_text_from_rows,
    read_sheet,
    s,
    server_model_from_text,
    should_drop,
    to_int,
)
from bom.parsers.dell_solution import vnet_module_category

FORMAT_QUOTE = 'dell_quote'
FORMAT_DH = 'dh_bid'
FORMAT_VNET = 'dell_vnet'


# ═══ dell_quote ══════════════════════════════════════════════════════════════

def _find_quote_header(rows) -> Optional[Tuple[int, int]]:
    """(header row, qty column) for a 'SKU | Description | … Qty' row within
    the first 15 rows."""
    for r in range(1, min(len(rows), 15) + 1):
        if cell(rows, r, 1) == 'SKU' and cell(rows, r, 3) == 'Description':
            row = rows[r - 1]
            for c in range(4, len(row) + 1):
                if cell(rows, r, c) == 'Qty':
                    return r, c
    return None


def _has_quote_number(wb) -> bool:
    for ws in wb.worksheets:
        for row in head_rows(ws, 40, 20):
            for v in row:
                if 'quote number' in s(v).lower():
                    return True
    return False


def _quote_sheet(wb):
    for ws in wb.worksheets:
        if _find_quote_header(head_rows(ws, 15, 20)) is not None:
            return ws
    return None


def detect_quote(wb) -> bool:
    return _quote_sheet(wb) is not None and _has_quote_number(wb)


def config_name(item_name: str, index: int) -> str:
    """The quote's item name minus its '[amer_r760_…]' tag; 'Config N' when
    nothing is left. Shared with the PDF print of the quote."""
    m = re.search(r'\]\s*(.+)$', item_name)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return item_name.strip() or 'Config %d' % index


def parse_quote(path: str) -> NormalizedBOM:
    from bom.parsers import UnrecognizedFormat
    rows, _title = read_sheet(path, _quote_sheet, 'Not a Dell quote export.')
    hdr, qty_col = _find_quote_header(rows)

    groups: List[dict] = []
    cur: Optional[dict] = None
    for r in range(hdr + 1, len(rows) + 1):
        a, c, q = cell(rows, r, 1), cell(rows, r, 3), cell(rows, r, qty_col)
        if a == 'SKU':
            continue                                  # repeated header before the next group
        if not a and c and q.isdigit():
            cur = {'name': c, 'qty': int(q), 'acc': ComponentAcc(), 'model': None}
            groups.append(cur)
            continue
        if not a or cur is None:
            continue                                  # delivery date, contract, subtotal rows
        if not DELL_SKU.match(a):
            continue
        if a.startswith('210-') and cur['model'] is None:
            cur['model'] = model_from_server_line(c)
        if should_drop(c, a):
            continue
        cur['acc'].add(a, c, to_int(q, default=1), categorize_line(c))

    if not groups:
        raise UnrecognizedFormat('Dell quote has no system group.')
    configs = []
    for i, g in enumerate(groups, start=1):
        model = g['model'] or server_model_from_text(g['name'])
        configs.append(BOMConfig(name=config_name(g['name'], i), server_model=model,
                                 components=g['acc'].components(), node_count=g['qty']))
    return NormalizedBOM(vendor='Dell', configs=configs, raw_text=raw_text_from_rows(rows))


# ═══ dh_bid ═════════════════════════════════════════════════════════════════

_DH_HEADERS = {'line': 'Bid Line No.', 'dh': 'D&H Part #', 'mfr': 'Manufacturer Part #',
               'desc': 'Description', 'qty': 'Quantity'}


def _dh_header(rows) -> Optional[Tuple[int, Dict[str, int]]]:
    for r in range(1, min(len(rows), 40) + 1):
        row = rows[r - 1]
        labels = {s(v): i + 1 for i, v in enumerate(row) if s(v)}
        if all(h in labels for h in _DH_HEADERS.values()):
            return r, {k: labels[v] for k, v in _DH_HEADERS.items()}
    return None


def _dh_sheet(wb):
    for ws in wb.worksheets:
        if ws.title.strip().lower() == 'dh quotation' or _dh_header(head_rows(ws, 40, 30)) is not None:
            return ws
    return None


def detect_dh(wb) -> bool:
    ws = _dh_sheet(wb)
    return ws is not None and _dh_header(head_rows(ws, 40, 30)) is not None


def parse_dh(path: str) -> NormalizedBOM:
    from bom.parsers import UnrecognizedFormat
    rows, _title = read_sheet(path, _dh_sheet, 'Not a D&H bid quotation.')
    found = _dh_header(rows)
    if found is None:
        raise UnrecognizedFormat('Not a D&H bid quotation.')
    hdr, col = found

    configs: List[Tuple[BOMConfig, ComponentAcc]] = []
    for r in range(hdr + 1, len(rows) + 1):
        line = cell(rows, r, col['line'])
        mfr, desc = cell(rows, r, col['mfr']), cell(rows, r, col['desc'])
        qty = to_int(cell(rows, r, col['qty']), default=1)
        if not line or not line.isdigit():
            if 'subtotal' in cell(rows, r, col['qty']).lower():
                break
            continue
        if mfr.startswith('210-') and desc == desc.upper() and cell(rows, r, col['dh']):
            # 'POWEREDGE R760, CHAMPION PE' — the system line that opens a config.
            configs.append((BOMConfig(name='Config %d' % (len(configs) + 1),
                                      server_model=server_model_from_text(desc.title()),
                                      components=[], node_count=qty), ComponentAcc()))
            continue
        if not configs:
            continue
        config, acc = configs[-1]
        if mfr.startswith('210-'):
            config.server_model = model_from_server_line(desc)
        if not mfr or should_drop(desc, mfr):
            continue
        acc.add(mfr, desc, qty, categorize_line(desc))
    if not configs:
        raise UnrecognizedFormat('D&H bid has no system line.')
    for config, acc in configs:
        config.components = acc.components()
    return NormalizedBOM(vendor='Dell', configs=[c for c, _ in configs],
                         raw_text=raw_text_from_rows(rows))


# ═══ dell_vnet ══════════════════════════════════════════════════════════════

_VNET_HEADER = ('Module Name', None, 'Option Name', 'SKUs', 'Qty')
# The module → category table lives with its keyword-matched twin in
# parsers/dell_solution.py (vnet_module_category), which says why the two
# are kept apart.


def _vnet_header(rows) -> Optional[int]:
    for r in range(1, min(len(rows), 10) + 1):
        if (cell(rows, r, 1) == 'Module Name' and cell(rows, r, 3) == 'Option Name'
                and cell(rows, r, 4) == 'SKUs' and cell(rows, r, 5) == 'Qty'):
            return r
    return None


def _vnet_sheet(wb):
    for ws in wb.worksheets:
        if _vnet_header(head_rows(ws, 10, 8)) is not None:
            return ws
    return None


def detect_vnet(wb) -> bool:
    return _vnet_sheet(wb) is not None


def parse_vnet(path: str) -> NormalizedBOM:
    from bom.parsers import UnrecognizedFormat
    rows, _title = read_sheet(path, _vnet_sheet, 'Not a Dell VNET module export.')
    hdr = _vnet_header(rows)

    acc = ComponentAcc()
    model: Optional[str] = None
    node_count = 1
    for r in range(hdr + 1, len(rows) + 1):
        module, option = cell(rows, r, 1), cell(rows, r, 3)
        skus = [t.strip() for t in cell(rows, r, 4).split(',') if t.strip()]
        qty = to_int(cell(rows, r, 5), default=1)
        if not module or not option:
            continue
        key = module.lower()
        sku = skus[0] if skus else None
        if key == 'base':
            model = model_from_server_line(option)
            node_count = qty
        if is_absence(option):
            category = 'other'
        else:
            category = vnet_module_category(module, option)
            if category is None or should_drop(option, sku):
                continue
        acc.add(sku, option, qty, category)
    if not acc:
        raise UnrecognizedFormat('VNET export has no option rows.')
    config = BOMConfig(name='Config 1', server_model=model, components=acc.components(),
                       node_count=node_count)
    return NormalizedBOM(vendor='Dell', configs=[config], raw_text=raw_text_from_rows(rows))
