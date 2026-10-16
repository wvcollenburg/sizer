"""Best-effort read of a spreadsheet no strict parser recognises.

The strict parsers (the rest of this package) each know one exact layout and
refuse anything else, because a guessed parse must never pass as a sure one.
This module is the tier between them and the Claude agent, the spreadsheet
counterpart of the PDF ladder: partners keep sending the layouts we know
retyped a little differently — a DCSC export with the price columns removed
and a header added, a Dell quote without its 'Quote number' cell, the columns
in another order. Those are nearly a known layout, and a column-shape read
gets them right far more often than not. So the read is attempted, scored
(bom/sheet_certainty.py), and trusted only at or above the admin threshold;
below it the file goes to the agent as before, with the reasons kept.

How the columns are found: by content, not position. Across a sheet's rows
the column whose cells are mostly short upper-case codes is the part number,
the one whose cells are mostly long wordy text is the description, the one
whose cells are mostly small integers is the quantity (a column that simply
counts 1, 2, 3 … is an item index, not a quantity). A header row with the
usual words (Part number / Description / Qty, in the languages the DCSC and
Dell exports use) takes precedence and raises the score. Server rows start a
config — a Lenovo CTO code ('7DG9CTO1WW'), a Dell 'PowerEdge R660 Server'
line, a 'Name : ThinkSystem SR630 V4' title — and their quantity is the node
count; every other row with a description and a quantity or a part number is
a component of the current config, with quantities kept as the totals the
sheet carries (every parser here does). Rows that look like parts but could
not be placed are counted against the score, as the PDF parsers' unexplained
lines are.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from bom.normalize import BOMConfig, NormalizedBOM
from bom.parsers.common import (
    LENOVO_CTO,
    LENOVO_FC,
    SERVER_LINE,
    ComponentAcc,
    categorize_line,
    cell,
    raw,
    raw_text_from_rows,
    s as text_of,
    server_model_from_text,
    sheet_matrix,
    should_drop,
    split_nx,
)
from bom.text import squash_ws

FORMAT = 'sheet_guess'

PART_WORDS = {
    'part number', 'part no', 'part no.', 'part #', 'part', 'parts', 'sku', 'feature code',
    'feature', 'code', 'mfr part #', 'mfr part number', 'vendor part #', 'vendor part number',
    'manufacturer part number', 'item number', 'material', 'artikelnummer', 'artikel',
    'référence', 'reference', 'referencia', 'número de peça', 'numero de peca', 'part nr',
    'p/n', 'pn', 'option', 'fc', 'teilenummer', 'onderdeelnummer',
}
DESC_WORDS = {
    'description', 'product description', 'item description', 'desc', 'beschreibung',
    'descripción', 'descripcion', 'descrição', 'descricao', 'omschrijving', 'descriptif',
    'designation', 'désignation', 'product', 'produkt', 'item', 'component', 'komponente',
    'config', 'configuration',
}
QTY_WORDS = {
    'qty', 'qty.', 'quantity', 'menge', 'quantité', 'quantite', 'cantidad', 'quantità',
    'quantita', 'aantal', 'qtd', 'quant', 'count', 'units', 'anzahl', 'stück', 'stuks',
    'q.ty', 'qte', 'qté',
}

_PART_TOKEN = re.compile(r'^[A-Z0-9][A-Z0-9.\-/]{2,24}$')
_SUPERMICRO = re.compile(r'^(?:SYS|AOC|MEM|HDS|PWS|MCP|CBL|CSE|MBD|SNK|RSC|BPN)-', re.I)
_DELL_SKU_FULL = re.compile(r'^\d{3}-(?:\d{5}|[A-Z0-9]{4})$')   # incl. the five-digit bundle form
_KNOWN_PART = re.compile(r'^(?:\d{3}-(?:\d{5}|[A-Z0-9]{4})|[A-Z0-9]{4}|7[0-9A-Z]{3}CTO\d+WW)$')
_WORDY = re.compile(r'[A-Za-z]{2,}.*\s.*[A-Za-z0-9]{2,}')
_ITEMISH = re.compile(
    r'\d+\s*(?:GB|TB|GbE|W|MHz|GHz)\b|\b(?:Xeon|EPYC|RDIMM|DIMM|SSD|HDD|NVMe|Ethernet|'
    r'Adapter|Chassis|Processor|Server|PowerEdge|ThinkSystem|ProLiant|Backplane|Riser|'
    r'Power Supply|Heatsink|Rail|Cable)\b', re.I)
_MACHINE_WORDS = re.compile(r'\b(?:ThinkSystem|PowerEdge|ProLiant|SYS-\d{3})\b', re.I)
# A Dell configurator's product row: the model and the selection name,
# without the word PowerEdge ('R660 - Smart Selection Flexi [EMEA_PER6601A]').
_DELL_PRODUCT = re.compile(r'^(?:PowerEdge\s+)?[RTX]R?\d{3,4}[A-Za-z]*\b.*\b(?:Smart Selection|Configuration|Konfiguration|Flexi|Full Config)', re.I)
_GENERIC_SHEET = re.compile(
    r'^(?:sheet|blad|tabelle|feuil|hoja|foglio|planilha|工作表)\s*\d*$'
    r'|^(?:quote|cotação|cotacao|quotation|summary|power report|bom|parts?|config(?:uration)?s?|'
    r'quote w(?:ith)? availability|cotação com disponibilidade|hw breakdown|breakdown)$', re.I)
# Lenovo service / instruction codes: not parts, as the DCSC parser drops them.
_LENOVO_SERVICE = re.compile(r'^(?:7Q01CTS|5WS7|5PS7|5641|5374CM|5MS7|5AS7)')
_GENERIC_NAMES = ('server', 'servidor', 'serveur', 'machine')

MIN_ROWS = 4           # fewer part lines than this is not a BOM
HARDWARE = {'cpu', 'memory', 'storage', 'nic', 'chassis', 'controller', 'gpu'}
MIN_COMPONENTS = 3     # a config with fewer lines is noise, not a machine


@dataclass
class SheetEvidence:
    """What the read saw, for the certainty score and the reviewer."""
    rows: int = 0                                    # component lines placed
    unexplained: List[str] = field(default_factory=list)
    pattern_misses: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    header_found: bool = False
    part_col: bool = False
    qty_col: bool = False
    configs_without_nodes: int = 0
    configs_nodes_inferred: int = 0           # machine count taken from the chassis line
    configs_smart_selection: int = 0          # Dell Smart Selection lines, no product row
    sheets_read: int = 0
    sheets_skipped: int = 0


# ── column discovery ───────────────────────────────────────────────────────────

def _is_int(v: Any) -> bool:
    if isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return float(v).is_integer() and 0 < v <= 100000
    if isinstance(v, str):
        return re.fullmatch(r'\s*\d{1,6}\s*', v) is not None and int(v) > 0
    return False


def _as_int(v: Any) -> Optional[int]:
    return int(float(v)) if _is_int(v) else None


def _profile(rows: List[Tuple], first: int) -> Dict[int, dict]:
    """Per column (1-based): how many non-empty cells, and how many of them
    look like a part code, an integer, or wordy text."""
    ncols = min(max((len(r) for r in rows), default=0), 30)
    stats = {c: {'n': 0, 'part': 0, 'int': 0, 'text': 0, 'ints': []} for c in range(1, ncols + 1)}
    for r in range(first, len(rows) + 1):
        for c in range(1, ncols + 1):
            v = raw(rows, r, c)
            t = text_of(v).strip()
            if not t:
                continue
            st = stats[c]
            st['n'] += 1
            if _is_int(v):
                st['int'] += 1
                st['ints'].append(_as_int(v))
            elif _PART_TOKEN.match(t):
                st['part'] += 1
            if len(t) >= 12 and _WORDY.search(t):
                st['text'] += 1
    return stats


def _is_index_column(ints: List[int]) -> bool:
    """1, 2, 3 … (an item number), not a quantity."""
    if len(ints) < 3:
        return False
    steps = sum(1 for a, b in zip(ints, ints[1:]) if b == a + 1)
    return steps >= 0.8 * (len(ints) - 1)


def _header(rows: List[Tuple]) -> Tuple[Optional[int], Dict[str, int]]:
    """(row, {role: column}) for the first row in the top 15 that names at
    least two of the three roles."""
    for r in range(1, min(len(rows), 15) + 1):
        roles: Dict[str, int] = {}
        for c in range(1, min(len(rows[r - 1]), 30) + 1):
            label = squash_ws(cell(rows, r, c))
            if not label:
                continue
            if label in QTY_WORDS and 'qty' not in roles:
                roles['qty'] = c
            elif label in DESC_WORDS and 'desc' not in roles:
                roles['desc'] = c
            elif label in PART_WORDS and 'part' not in roles:
                roles['part'] = c
        if len(roles) >= 2:
            return r, roles
    return None, {}


def _columns(rows: List[Tuple]) -> Tuple[Dict[str, int], int, bool]:
    """({role: column}, first data row, header found). Roles the header did
    not name come from the content profile; a sheet without a description
    column, or without both a quantity and a part column, is not a table."""
    hdr, roles = _header(rows)
    first = (hdr + 1) if hdr else 1
    stats = _profile(rows, first)
    used = set(roles.values())

    def best(score):
        cands = [(score(st), c) for c, st in stats.items() if c not in used and st['n'] >= 3]
        cands = [(v, c) for v, c in cands if v > 0]
        if not cands:
            return None
        v, c = max(cands)
        return c

    if 'desc' not in roles:
        c = best(lambda st: st['text'] / st['n'] if st['text'] / st['n'] >= 0.5 else 0)
        if c:
            roles['desc'] = c
            used.add(c)
    if 'part' not in roles:
        c = best(lambda st: st['part'] / st['n']
                 if st['part'] / st['n'] >= 0.6 and st['text'] / st['n'] < 0.2 else 0)
        if c:
            roles['part'] = c
            used.add(c)
    if 'qty' in roles and not stats.get(roles['qty'], {}).get('int'):
        del roles['qty']                      # the header says Qty; the cells say nothing
    if 'qty' not in roles:
        c = best(lambda st: st['int'] / st['n']
                 if st['int'] / st['n'] >= 0.7 and not _is_index_column(st['ints']) else 0)
        if c:
            roles['qty'] = c
            used.add(c)
    return roles, first, hdr is not None


# ── rows → configs ─────────────────────────────────────────────────────────────

def _first_part(text: str) -> str:
    """'338-CPCS, 379-BDCO' → '338-CPCS': a Dell export lists the option's
    SKUs together; the first is the part, the rest are its bundle
    (parsers/dell_lists.py does the same)."""
    tokens = [t.strip() for t in text.replace(';', ',').split(',')]
    tokens = [t for t in tokens if t]
    if not tokens:
        return ''
    known = [t for t in tokens if _KNOWN_PART.match(t)]
    return (known or tokens)[0]


def _machine_row(part: str, desc: str) -> bool:
    if LENOVO_CTO.match(part):
        return _MACHINE_WORDS.search(desc) is not None or ' : ' in desc
    if SERVER_LINE.match(desc):
        return True
    if ' : ' in desc and _MACHINE_WORDS.search(desc):
        return True
    if _DELL_PRODUCT.match(desc) and not _KNOWN_PART.match(part):
        return True
    return bool(re.match(r'^210-', part)) and 'PowerEdge' in desc


def _config_name(desc: str, index: int, sheet_title: str, single_per_sheet: bool) -> str:
    if ' : ' in desc:
        name = desc.partition(' : ')[0].strip()
        if name and name.lower() not in _GENERIC_NAMES:
            return name[:120]
    elif _DELL_PRODUCT.match(desc):
        return desc[:120]                      # the configurator's own title
    if single_per_sheet and sheet_title and not _GENERIC_SHEET.match(sheet_title.strip()):
        return sheet_title.strip()[:120]
    return 'Config %d' % index


class _Config:
    def __init__(self, name: str, model: Optional[str], nodes: Optional[int]):
        self.name = name
        self.model = model
        self.nodes = nodes
        self.acc = ComponentAcc('part+desc')
        self.parts: List[str] = []
        self.nodes_inferred = False
        self.from_product_row = False
        self.smart_selection = False

    def add(self, part: Optional[str], desc: str, qty: int):
        if should_drop(desc, part):
            return
        cat = categorize_line(desc)
        if cat == 'chassis' and self.model is None:
            self.model = server_model_from_text(desc)
        if (part or '').startswith('486-') or 'Smart Selection' in desc:
            self.smart_selection = True
        self.acc.add(part, desc, qty, cat)
        if part:
            self.parts.append(part)

    def build(self) -> BOMConfig:
        comps = self.acc.components()
        nodes = self.nodes
        if nodes is None:
            # The chassis line is one per machine in every layout we know —
            # a fair guess, scored as one.
            chassis = [c for c in comps if c.category == 'chassis']
            if chassis:
                nodes = chassis[0].quantity
                self.nodes_inferred = True
        return BOMConfig(name=self.name, server_model=self.model, components=comps,
                         node_count=nodes)


def _header_words_row(part: str, desc: str, qty_text: str) -> bool:
    """A header row met inside the table (the Dell quote repeats 'SKU |
    Description | Qty' before every group)."""
    return (squash_ws(desc) in DESC_WORDS or squash_ws(part) in PART_WORDS
            or squash_ws(qty_text) in QTY_WORDS)


def _read_sheet(title: str, rows: List[Tuple]) -> Tuple[List[BOMConfig], SheetEvidence]:
    ev = SheetEvidence()
    roles, first, has_header = _columns(rows)
    if 'desc' not in roles or ('qty' not in roles and 'part' not in roles):
        return [], ev
    ev.sheets_read = 1
    ev.header_found = has_header
    ev.part_col = 'part' in roles
    ev.qty_col = 'qty' in roles
    dc, pc, qc = roles['desc'], roles.get('part'), roles.get('qty')

    configs: List[_Config] = []
    current: Optional[_Config] = None
    machine_rows = sum(1 for r in range(first, len(rows) + 1)
                       if _machine_row(cell(rows, r, pc) if pc else '', cell(rows, r, dc)))
    in_service_block = False
    for r in range(first, len(rows) + 1):
        desc = ' '.join(cell(rows, r, dc).split())
        part = _first_part(cell(rows, r, pc)) if pc else ''
        qraw = raw(rows, r, qc) if qc else None
        qty = _as_int(qraw)
        if not desc and not part:
            in_service_block = False          # a blank row ends a DCSC block
            continue
        if _header_words_row(part, desc, cell(rows, r, qc) if qc else ''):
            continue
        if _LENOVO_SERVICE.match(part) or (LENOVO_CTO.match(part) and not _machine_row(part, desc)
                                           and not _MACHINE_WORDS.search(desc)):
            # Services, instructions and software CTOs: the block's own rows
            # are not parts either (the DCSC parser drops them wholesale).
            in_service_block = True
            continue
        if in_service_block and not _machine_row(part, desc):
            continue
        in_service_block = False
        if not desc:
            # A code with no text: a line we cannot name.
            if _PART_TOKEN.match(part):
                ev.unexplained.append(part)
            continue
        if _machine_row(part, desc):
            name = _config_name(desc, len(configs) + 1, title, machine_rows == 1)
            model = server_model_from_text(desc.partition(' : ')[2] if ' : ' in desc else desc)
            if current is not None and not current.acc:
                # A title or product row opened this config ('PowerEdge R760
                # Smart Selection - […]', 'R660 - Smart Selection Flexi' x 3);
                # the server line that follows completes it. The product row's
                # count is the machine count; the server line's is per machine.
                current.model = current.model or model
                if current.nodes is None:
                    current.nodes = qty
                else:
                    current.from_product_row = True
                if current.name.startswith('Config '):
                    current.name = name
            else:
                current = _Config(name, model, qty)
                configs.append(current)
            ev.rows += 1
            continue
        titled = (qty is None and not part and _MACHINE_WORDS.search(desc) is not None)
        if titled and (current is None or current.acc):
            # A config title without a code or a count: the Dell quote's
            # group heading. It names the config the next rows fill.
            current = _Config(desc[:120], server_model_from_text(desc), None)
            configs.append(current)
            continue
        if qc is not None and qty is None and not (part and _PART_TOKEN.match(part)):
            # Text without a quantity or a code: a note, a title, a total …
            # unless it reads like a part, which then went unplaced.
            if _ITEMISH.search(desc) and qraw not in (None, ''):
                ev.unexplained.append(desc[:160])
            continue
        if qty is None:
            if qc is not None and part:
                # The sheet states quantities, this part line does not.
                ev.unexplained.append(('%s %s' % (part, desc))[:160])
                continue
            n, desc2 = split_nx(desc)
            qty, desc = n, desc2
        if current is None:
            current = _Config(_config_name('', 1, title, machine_rows <= 1), None, None)
            configs.append(current)
        current.add(part or None, desc, max(qty, 1))
        ev.rows += 1

    out: List[BOMConfig] = []
    for cfg in configs:
        built = cfg.build()
        cats = {c.category for c in built.components}
        if not cats & HARDWARE:
            # No hardware at all: a software or services block, a power
            # report. Not a machine, and not evidence either way.
            ev.rows -= len(built.components)
            continue
        if len(built.components) < MIN_COMPONENTS:
            # Too small to be a machine: stray lines before the first server
            # row. Its lines count as unplaced.
            ev.unexplained.extend(c.description[:160] for c in built.components)
            ev.rows -= len(built.components)
            continue
        if built.node_count is None:
            ev.configs_without_nodes += 1
        elif cfg.nodes_inferred:
            ev.configs_nodes_inferred += 1
        elif cfg.smart_selection and not cfg.from_product_row and built.node_count == 1:
            # A Dell configurator export states the machine count on its
            # product row and lists every quantity per machine; without that
            # row the server line's '1' may be one of three. A count above
            # one can only be a quote's total, so only the '1' is doubted.
            ev.configs_smart_selection += 1
        out.append(built)
    return out, ev


def _signature(cfg: BOMConfig) -> Tuple:
    return (cfg.node_count, cfg.server_model,
            tuple(sorted((c.part_number or c.description, c.quantity) for c in cfg.components)))


def _merge(into: SheetEvidence, ev: SheetEvidence) -> None:
    into.rows += ev.rows
    into.unexplained.extend(ev.unexplained)
    into.notes.extend(ev.notes)
    into.header_found = into.header_found or ev.header_found
    into.part_col = into.part_col or ev.part_col
    into.qty_col = into.qty_col or ev.qty_col
    into.configs_without_nodes += ev.configs_without_nodes
    into.configs_nodes_inferred += ev.configs_nodes_inferred
    into.configs_smart_selection += ev.configs_smart_selection
    into.sheets_read += ev.sheets_read


def _vendor_pattern(vendor: str):
    return {'Lenovo': lambda p: bool(LENOVO_FC.match(p) or LENOVO_CTO.match(p)),
            'Dell': lambda p: bool(_DELL_SKU_FULL.match(p)),
            'Supermicro': lambda p: bool(_SUPERMICRO.match(p))}.get(vendor)


def read(path: str) -> Tuple[Optional[NormalizedBOM], SheetEvidence]:
    """(NormalizedBOM or None, evidence). None when no sheet holds a table
    with a description column and a quantity or part column."""
    from bom.parsers import detect_vendor
    from bom.parsers.common import load_workbook_safe
    ev = SheetEvidence()
    wb = load_workbook_safe(path)
    try:
        sheets = [(ws.title, sheet_matrix(ws)) for ws in wb.worksheets]
    finally:
        wb.close()
    configs: List[BOMConfig] = []
    raw_parts = []
    seen = set()
    for title, rows in sheets:
        found, local = _read_sheet(title, rows)
        if not found:
            if any(any(text_of(v).strip() for v in row) for row in rows):
                ev.sheets_skipped += 1
            continue
        # The same machines again on another sheet (a DCSC export's summary
        # and availability sheets): the first reading stands.
        fresh = [c for c in found if _signature(c) not in seen]
        if not fresh:
            continue
        seen.update(_signature(c) for c in fresh)
        if len(fresh) < len(found):
            local.rows = sum(len(c.components) + 1 for c in fresh)
        configs.extend(fresh)
        _merge(ev, local)
        raw_parts.append('=== Sheet: %s ===\n%s' % (title, raw_text_from_rows(rows)))
    if not configs:
        return None, ev
    bom = NormalizedBOM(vendor='Unknown', configs=configs, raw_text='\n'.join(raw_parts)[:60000])
    bom.vendor = detect_vendor(bom)
    check = _vendor_pattern(bom.vendor)
    if check:
        for cfg in configs:
            for comp in cfg.components:
                if comp.part_number and not check(comp.part_number):
                    ev.pattern_misses.append(comp.part_number)
    if ev.sheets_skipped:
        ev.notes.append('%d sheet(s) held no parts table.' % ev.sheets_skipped)
    return bom, ev
