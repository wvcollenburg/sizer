"""Lenovo hardware breakdown: feature code | description | quantity, no header.

The shape a partner sent on 2026-10-16 ('… HC3450F Lenovo HW.xlsx'): the DCSC
configuration retyped as three bare columns, one sheet per drive option
('7.68TB HW Breakdown'), with the option's name alone in A1 and nothing else
— no header row, no prices, no DCSC title cell:

    7.68TB
    7DG9CTO1WW   Server : ThinkSystem SR630 V4-3yr Base Warranty   3
    C1XE         ThinkSystem 1U V4 10x2.5" Chassis                  3
    C5R5         Intel Xeon 6724P 16C 210W 3.6GHz Processor         3
    C0TQ         ThinkSystem 64GB TruDDR5 6400MHz (2Rx4) RDIMM      24
    …
                                                                 (blank row)
    7S0XCTO6WW   XClarity One                                       3
    SCJD         XClarity One - Standard, Per Endpoint w/3 Yr SW S&S 3

The blocks are the DCSC blocks (parsers/lenovo_dcsc.py) with the columns
moved: the CTO machine row carries the machine count, the feature rows carry
quantities already summed over the machines, a blank row separates blocks,
and software CTO blocks (XClarity) fold into the machine they follow. So the
rows are reshaped into DCSC blocks and handed to the same
``configs_from_blocks``. The A1 title (or the sheet name) names the config,
since the machine row's own name is the generic 'Server'.
"""
import re
from typing import List, Optional, Tuple

from bom.normalize import BOMConfig, NormalizedBOM
from bom.parsers.common import (
    LENOVO_CTO,
    LENOVO_FC,
    cell,
    head_rows,
    raw,
    raw_text_from_rows,
    sheet_matrix,
)
from bom.parsers.lenovo_dcsc import configs_from_blocks

FORMAT = 'lenovo_hw_breakdown'

_MACHINE = re.compile(r'ThinkSystem|\bS[RT]\d{3}\b')
_HEADER_WORDS = ('part number', 'part', 'description', 'qty', 'quantity', 'item')
_BREAKDOWN_SUFFIX = re.compile(r'\s*(?:HW|hardware)\s*breakdown\s*$', re.I)


def _is_int(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and float(v).is_integer()


def _machine_row(rows, r: int) -> bool:
    return (bool(LENOVO_CTO.match(cell(rows, r, 1)))
            and _MACHINE.search(cell(rows, r, 2)) is not None
            and _is_int(raw(rows, r, 3)))


def _feature_row(rows, r: int) -> bool:
    return (bool(LENOVO_FC.match(cell(rows, r, 1)))
            and cell(rows, r, 2) != ''
            and _is_int(raw(rows, r, 3)))


def _sheet_matches(rows) -> bool:
    """A machine row in the first rows, followed by feature rows, and no
    header labels anywhere above it (a headed sheet is another layout)."""
    top = min(len(rows), 12)
    machine = next((r for r in range(1, top + 1) if _machine_row(rows, r)), None)
    if machine is None:
        return False
    for r in range(1, machine):
        for c in (1, 2, 3):
            if cell(rows, r, c).strip().lower() in _HEADER_WORDS:
                return False
    features = sum(1 for r in range(machine + 1, min(len(rows), machine + 12) + 1)
                   if _feature_row(rows, r))
    return features >= 3


def _matching_sheets(wb) -> list:
    return [ws for ws in wb.worksheets if _sheet_matches(head_rows(ws, 24, 4))]


def detect(wb) -> bool:
    return bool(_matching_sheets(wb))


def _label(rows, title: str) -> Tuple[str, int]:
    """The config name and the first row of the table: a lone text in A1
    ('7.68TB') names the option; otherwise the sheet name does, minus its
    'HW Breakdown' suffix."""
    first = 1
    label = ''
    while first <= len(rows) and not _machine_row(rows, first) and not _feature_row(rows, first):
        a, b, c = cell(rows, first, 1), cell(rows, first, 2), cell(rows, first, 3)
        if a and not b and not c and not label:
            label = a
        first += 1
    if not label:
        label = _BREAKDOWN_SUFFIX.sub('', title).strip() or title.strip()
    return label, first


def _blocks(rows, start: int) -> List[dict]:
    """DCSC-shaped blocks (sku, title, qty, row, child rows) from the three
    columns, separated by blank rows."""
    blocks: List[dict] = []
    cur: Optional[dict] = None
    for r in range(start, len(rows) + 1):
        a, b = cell(rows, r, 1), cell(rows, r, 2)
        if not a and not b:
            cur = None
            continue
        if cur is None:
            cur = {'sku': a, 'title': b, 'qty': raw(rows, r, 3), 'row': r, 'rows': []}
            blocks.append(cur)
        else:
            cur['rows'].append((r, a, b, raw(rows, r, 3)))
    return blocks


def parse(path: str) -> NormalizedBOM:
    from bom.parsers import UnrecognizedFormat   # local import: the package imports us
    from bom.parsers.common import load_workbook_safe
    wb = load_workbook_safe(path)
    try:                                           # every option sheet is a config
        sheets = [(ws.title, sheet_matrix(ws)) for ws in _matching_sheets(wb)]
    finally:
        wb.close()
    configs: List[BOMConfig] = []
    raw_parts = []
    for title, rows in sheets:
        label, first = _label(rows, title)
        blocks = _blocks(rows, first)
        names = [label] + ['%s (%d)' % (label, n) for n in range(2, 10)]
        configs.extend(configs_from_blocks(blocks, has_part_col=True, names=names))
        raw_parts.append('=== Sheet: %s ===\n%s' % (title, raw_text_from_rows(rows)))
    if not configs:
        raise UnrecognizedFormat('The hardware breakdown has no ThinkSystem machine block.')
    return NormalizedBOM(vendor='Lenovo', configs=configs, raw_text='\n'.join(raw_parts)[:60000])
