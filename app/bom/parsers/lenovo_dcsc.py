"""Lenovo DCSC (Data Center Solution Configurator) quote export.

Why a dedicated parser: this is the shape partners send most (10 of the 15
archived xlsx BOMs) and its layout is rigid — one 'Quote' (pt-BR 'Cotação')
sheet, header row with the Qty column in E, then blocks separated by exactly
one blank row. The first row of a block is the CTO header ('7DGDCTO1WW',
'<config name> : ThinkSystem SR650 V4-3yr Base Warranty', machine count);
the child rows carry four-character feature codes, descriptions and the
quantity *summed over all machines of the block*. We keep those totals
(SC//Design did too, and the archived expected findings were computed on
them) and record the machine count as node_count so fit.py can divide.

Config splitting follows the machine header rows, not the ConfigGroupView
sheet (which lumps every machine under 'Config 1'). Software blocks between
machines ('7S0NCTO1WW Scale Computing Software' → a HyperCore licence line)
are folded into the preceding machine as 'other'; service blocks
(7Q01CTS*, 5WS7*, 5641*, 5374CM1 configuration instructions) are dropped
wholesale — SC//Design's LLM turned some of these into pseudo-configs with
no server model, which only added noise to the verdict.

M.2 boot media (the Arrow quote's 'ThinkSystem M.2 VA 480GB … NHS SSD' next
to the B540p adapter) is emitted with category 'other' and the description
intact: it is not a data drive, and the archived expected findings prove the
validator never counted it.
"""
import re
from typing import Dict, List, Optional, Tuple

from bom.normalize import BOMComponent, BOMConfig, NormalizedBOM
from bom.parsers.common import (
    LENOVO_CTO,
    categorize,
    cell,
    head_rows,
    is_m2_media,
    raw,
    raw_text_from_rows,
    s,
    server_model_from_text,
    sheet_matrix,
    should_drop,
    to_int,
)

FORMAT = 'lenovo_dcsc'

TITLE = 'Data Center Solution Configurator Quote'
QTY_WORDS = ('qty', 'qtd', 'menge', 'quantité', 'quantite', 'cantidad', 'quantità', 'quantity')
_GENERIC_NAMES = ('server', 'servidor', 'serveur')
_MACHINE_HINT = re.compile(r'\bS[RT]\d{3}\b')
_TERMS = re.compile(r'^TERM(?:S|OS)\b', re.I)


def _header_row(rows) -> Optional[int]:
    """Row whose column E says Qty and column D is empty (the availability
    sheet shifts Qty to F and fills D with 'Supply Status')."""
    for r in range(1, min(len(rows), 15) + 1):
        if cell(rows, r, 5).lower() in QTY_WORDS and cell(rows, r, 4) == '':
            return r
    return None


def _sheet_matches(rows) -> bool:
    return cell(rows, 1, 3) == TITLE and _header_row(rows) is not None


def pick_sheet(wb):
    """The Quote/Cotação sheet; any other sheet with the same signature as a
    fallback (single-sheet exports keep the name, but be lenient on it)."""
    candidates = []
    for ws in wb.worksheets:
        if _sheet_matches(head_rows(ws, 15, 10)):
            candidates.append(ws)
    if not candidates:
        return None
    for ws in candidates:
        if ws.title.strip().lower() in ('quote', 'cotação', 'cotacao'):
            return ws
    return candidates[0]


def detect(wb) -> bool:
    return pick_sheet(wb) is not None


def _split_title(title: str) -> Tuple[Optional[str], str]:
    if ' : ' in title:
        name, _, rest = title.partition(' : ')
        return name.strip(), rest.strip()
    return None, title.strip()


def _blocks(rows, start: int) -> List[dict]:
    """Group the rows into blank-row-separated blocks; stop at the Total /
    terms tail. Each block: header (sku, title, qty, row) + child rows."""
    blocks = []
    cur = None
    for r in range(start, len(rows) + 1):
        a, c = cell(rows, r, 1), cell(rows, r, 3)
        if cell(rows, r, 6) == 'Total' or _TERMS.match(a):
            break
        if not a and not c:
            cur = None
            continue
        if cur is None:
            cur = {'sku': a, 'title': c, 'qty': raw(rows, r, 5), 'row': r, 'rows': []}
            blocks.append(cur)
        else:
            cur['rows'].append((r, a, c, raw(rows, r, 5)))
    return blocks


def _is_machine(block: dict) -> bool:
    return bool(LENOVO_CTO.match(block['sku'])) and (
        'ThinkSystem' in block['title'] or _MACHINE_HINT.search(block['title']) is not None)


def _components(child_rows, drop_m2: bool = True) -> List[BOMComponent]:
    """Child rows → components, duplicates summed per feature code (the
    Frazier export lists 'C1YK … OCP Cable Kit' twice)."""
    order: List[str] = []
    acc: Dict[str, BOMComponent] = {}
    for _r, fc, desc, qty in child_rows:
        if not fc or not desc or should_drop(desc):
            continue
        cat = categorize(desc)
        if cat == 'storage' and drop_m2 and is_m2_media(desc):
            cat = 'other'
        q = to_int(qty, default=1)
        if fc in acc:
            acc[fc].quantity += q
        else:
            acc[fc] = BOMComponent(part_number=fc, description=desc, quantity=q, category=cat)
            order.append(fc)
    return [acc[k] for k in order]


def parse(path: str) -> NormalizedBOM:
    from bom.parsers import UnrecognizedFormat  # local import: package imports us
    from bom.parsers.common import load_workbook_safe

    wb = load_workbook_safe(path)
    try:
        ws = pick_sheet(wb)
        if ws is None:
            raise UnrecognizedFormat('Not a Lenovo DCSC quote export.')
        rows = sheet_matrix(ws)
    finally:
        wb.close()

    hdr = _header_row(rows)
    blocks = _blocks(rows, hdr + 1)
    machines = [b for b in blocks if _is_machine(b)]
    if not machines:
        raise UnrecognizedFormat('DCSC quote has no ThinkSystem machine block.')

    configs: List[BOMConfig] = []
    generic = 0
    current: Optional[BOMConfig] = None
    pending: List[BOMComponent] = []      # software rows seen before the first machine
    for block in blocks:
        if _is_machine(block):
            name, rest = _split_title(block['title'])
            if not name or name.lower() in _GENERIC_NAMES:
                generic += 1
                name = 'Config %d' % len(configs + [None])
            model = server_model_from_text(rest) or server_model_from_text(block['title'])
            current = BOMConfig(
                name=name,
                server_model=model,
                components=pending + _components(block['rows']),
                node_count=to_int(block['qty'], default=1),
            )
            pending = []
            configs.append(current)
        elif LENOVO_CTO.match(block['sku']):
            # Software CTOs (Scale Computing Software, XClarity FOD): fold the
            # surviving children into the machine they were quoted under.
            extra = _components(block['rows'])
            for comp in extra:
                comp.category = 'other'
            if current is not None:
                current.components.extend(extra)
            else:
                pending.extend(extra)
        # Service / instruction blocks (7Q01CTS*, 5WS7*, 5641*, 5374CM1): dropped.
    if pending and configs:
        configs[0].components.extend(pending)

    return NormalizedBOM(vendor='Lenovo', configs=configs,
                         raw_text=raw_text_from_rows(rows[:max(len(rows) and (blocks[-1]['row'] + len(blocks[-1]['rows']) + 2), 0)]))
