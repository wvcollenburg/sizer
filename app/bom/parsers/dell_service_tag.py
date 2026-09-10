"""Dell service-tag component export (CSV, and its xlsx twin).

Why this shape needs its own parser: it is not a quote but the factory bill
of a *shipped* machine — 'Component,Part Number,Description,Quantity' where
each option group is one row ('405-AAZF : Dell HBA355i Adapter, Low Prof
ile') that also carries the group's first piece part, followed by the other
piece parts (labels, screws, drivers, the actual card) on rows with an empty
Component column. Two quirks drive the design:

  - The option text is hard-wrapped every 30 characters and rejoined with a
    space ('Redundant Powe r Supply'); common.dewrap_dell reverses it exactly.
  - The option row carries no quantity of its own. We take the largest
    per-part sum among the group's *non-junk* piece rows (the drive rows,
    not the 28 screws), defaulting to 1 — this reproduces the archived
    normalisations for every load-bearing line (7 SSDs, 5 DIMMs, 2 NICs,
    4 HDDs, 2 CPUs, 2 H755N, 2 NVMe) and is deterministic where the LLM was
    not (PSU 2 vs 1, BOSS M.2 sticks 2 vs 1).

The export describes exactly one machine, so node_count = 1 and the
quantities are per machine. Matching downstream happens by Dell SKU, never
by the (de-wrapped) description.
"""
import csv
import io
import re
from typing import Dict, List, Optional, Tuple

from bom.normalize import BOMComponent, BOMConfig, NormalizedBOM
from bom.parsers.common import (
    DELL_PIECE,
    categorize,
    cell,
    dewrap_dell,
    head_rows,
    raw_text_from_rows,
    s,
    sheet_matrix,
    should_drop,
    to_int,
)

FORMAT = 'dell_service_tag'

HEADER = ('Component', 'Part Number', 'Description', 'Quantity')

# Piece-part descriptors that are packaging, labels, software, screws,
# carriers or info lines — never the countable hardware of the group.
JUNK_PIECE = re.compile(
    r'^(?:INFO|LBL|SRV|DSK PROG|GDE|SHP MTL|PREP MTL|KIT,SHP|Screw|SCR,|Label|Filler|FILLER|'
    r'Assembly,Filler|ASSY,LBL|ASSY,FIL|Assembly,Carrier|ASSY,CARR|System Integration|'
    r'Service Charge|SI,|Information|DIAG|DPK|Mylar|AW,LBL|Bracket|Clip|CLP|INSTR|'
    r'ASSY,SHRD|ASSY,MECH|ASSY,SHR|Assembly,Shroud|\*+)',
    re.I)


# ─── detection ────────────────────────────────────────────────────────────────

def _read_csv_text(path: str) -> Optional[str]:
    """UTF-8 (with or without BOM) first, then latin-1. NUL bytes mean it is
    not a text file at all."""
    with open(path, 'rb') as fh:
        data = fh.read()
    if b'\x00' in data[:65536]:
        return None
    for enc in ('utf-8-sig', 'latin-1'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return None


def detect_csv(path: str) -> bool:
    text = _read_csv_text(path)
    if text is None:
        return False
    first = text.lstrip('﻿').split('\n', 1)[0].strip()
    return first.replace('"', '') == ','.join(HEADER)


def detect_xlsx(wb) -> bool:
    if len(wb.worksheets) != 1:
        return False
    rows = head_rows(wb.worksheets[0], 1, 5)
    return tuple(cell(rows, 1, c) for c in range(1, 5)) == HEADER


def detect(wb) -> bool:
    return detect_xlsx(wb)


# ─── parsing ──────────────────────────────────────────────────────────────────

def _piece_number(v) -> str:
    """Numeric-looking piece parts come back from openpyxl as floats
    (8123.0 for '08123'); Dell piece parts are always five characters."""
    if isinstance(v, float) and v == int(v):
        return str(int(v)).zfill(5)
    text = s(v)
    if text.isdigit() and len(text) < 5:
        return text.zfill(5)
    return text


def _groups_from_records(records: List[Tuple[str, str, str, object]]) -> List[dict]:
    groups: List[dict] = []
    for comp, pn, desc, qty in records:
        comp = s(comp)
        if comp:
            sku, _, option = comp.partition(' : ')
            groups.append({'sku': sku.strip(), 'option': dewrap_dell(option.strip()),
                           'pieces': []})
            # The option row carries the group's FIRST piece part itself
            # ('400-ARRH : 1.6TB SSD…' | DMF5Y | 'Solid State Drive,…' | 7);
            # the remaining pieces follow on rows with an empty Component.
            if s(pn) or s(desc):
                groups[-1]['pieces'].append((_piece_number(pn), s(desc), to_int(qty, default=1)))
        elif groups and (s(pn) or s(desc)):
            groups[-1]['pieces'].append((_piece_number(pn), s(desc), to_int(qty, default=1)))
    return groups


def _group_quantity(pieces) -> int:
    sums: Dict[str, int] = {}
    for pn, desc, qty in pieces:
        if not pn or JUNK_PIECE.match(desc):
            continue
        sums[pn] = sums.get(pn, 0) + qty
    return max(sums.values()) if sums else 1


def _build(groups: List[dict], config_name: str, raw_text: str) -> NormalizedBOM:
    components: List[BOMComponent] = []
    server_model: Optional[str] = None
    for g in groups:
        sku, option = g['sku'], g['option']
        if not option:
            continue
        if sku.startswith('210-'):
            server_model = re.sub(r'\s+Server\s*$', '', option).strip() or None
        if should_drop(option, sku):
            continue
        components.append(BOMComponent(
            part_number=sku or None,
            description=option,
            quantity=_group_quantity(g['pieces']),
            category=categorize(option),
        ))
    config = BOMConfig(name=config_name, server_model=server_model,
                       components=components, node_count=1)
    return NormalizedBOM(vendor='Dell', configs=[config], raw_text=raw_text)


def parse_csv(path: str) -> NormalizedBOM:
    from bom.parsers import UnrecognizedFormat
    text = _read_csv_text(path)
    if text is None or not detect_csv(path):
        raise UnrecognizedFormat('Not a Dell service-tag component export.')
    reader = csv.DictReader(io.StringIO(text.lstrip('﻿')))
    records = []
    for row in reader:
        records.append((row.get('Component') or '', row.get('Part Number') or '',
                        row.get('Description') or '', row.get('Quantity')))
    groups = _groups_from_records(records)
    return _build(groups, 'Config 1', text[:60000])


def parse_xlsx(path: str) -> NormalizedBOM:
    from bom.parsers import UnrecognizedFormat
    from bom.parsers.common import load_workbook_safe
    wb = load_workbook_safe(path)
    try:
        if not detect_xlsx(wb):
            raise UnrecognizedFormat('Not a Dell service-tag component export.')
        ws = wb.worksheets[0]
        rows = sheet_matrix(ws)
        title = ws.title.strip()
    finally:
        wb.close()
    records = []
    for r in range(2, len(rows) + 1):
        comp = cell(rows, r, 1)
        pn = rows[r - 1][1] if len(rows[r - 1]) > 1 else None
        desc = cell(rows, r, 3)
        qty = rows[r - 1][3] if len(rows[r - 1]) > 3 else None
        if not comp and not s(pn) and not desc:
            continue
        records.append((comp, pn, desc, qty))
    groups = _groups_from_records(records)
    # The sheet is named after the service tag — the most useful config
    # name this shape can offer.
    name = title if re.match(r'^[0-9A-Z]{5,10}$', title) else (title or 'Config 1')
    return _build(groups, name, raw_text_from_rows(rows))


def parse(path: str) -> NormalizedBOM:
    if path.lower().endswith('.csv'):
        return parse_csv(path)
    return parse_xlsx(path)
