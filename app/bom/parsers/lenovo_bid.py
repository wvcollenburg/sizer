"""Lenovo bid quote ("Bid Request No. BRDND…"), as a Word document (.docx).

US partners have started sending these instead of a DCSC export. The
document is Lenovo's preliminary bid quote; its tables, in order:

    Customer Name / Customer Number / Bid Request No. / Created On / Contract …
    PRODUCT AND SERVICE DETAILS
      Part Number | Description                             | F/B | Qty | Start Date | End Date
      7DG9U9ER00  | ThinkSystem SR630 V4-3yr Base Warranty  | F   | 3   | …
      5WS7C20030  | 3Yr Premier NBD Resp SR630 V4           | F   | 3   | …
    CONFIGURATION DETAILS
      Components  | Description                             | Qty
      7DG9U9ER00  | ThinkSystem SR630 V4-3yr Base Warranty  | 3     <- machine line: the server count
      C1XE        | ThinkSystem 1U V4 10x2.5" Chassis       | 1     <- feature codes, PER SERVER
      C5QV        | Intel Xeon 6517P 16C 190W 3.2GHz …      | 1
      C0ZU        | ThinkSystem 2.5" U.2 VA 3.84TB … SSD    | 4
      …
      7S0XCTO8WW  | XClarity Controller Prem-FOD            | 3     <- software block
      SCY0        | Lenovo XClarity XCC3 premier - FOD      | 1
    Distributor / Reseller tables, terms.

What makes it different from the DCSC export: the feature-code quantities
are per server (one CPU, four DIMMs plus twelve memory fillers — the sixteen
slots of one socket — and four drives in a three-server quote), where DCSC
prints totals. The rest of the checker works on totals (fit.py divides by the
node count), so every child quantity is multiplied by its machine line's
count here. Reading them as totals is exactly the mistake that made a
3-server, 4-drive-per-node quote come out as one drive per node.

A machine line is a Lenovo part number (7DG9U9ER00 — a pre-configured model,
not only a 7DGxCTO1WW CTO) whose description names a ThinkSystem/ThinkAgile
server; its quantity is the node count. Software CTOs after it (7S0XCTO8WW
XClarity) are folded into that machine as 'other', service lines are not in
this table at all. The machine line's quantity must equal the same part's
quantity in PRODUCT AND SERVICE DETAILS; the PDF print checks that, and the
.docx parser refuses a file where it does not hold.

The block logic lives in ``configs_from_rows`` so the PDF print of the same
quote (parsers/pdf_lenovo_bid.py) reads exactly the same way.
"""
import re
from typing import Dict, List, Optional, Tuple

from bom.normalize import BOMConfig, NormalizedBOM
from bom.parsers.common import (LENOVO_BLOCK, LENOVO_FC, LENOVO_PART, ComponentAcc,
                                categorize_line, server_model_from_text, should_drop)

FORMAT = 'lenovo_bid_docx'

BID_MARK = re.compile(r'\bbid\s+request\s+no\b', re.I)
# A header row's cells, lower-cased.
CONFIG_HEADER = ('components', 'description', 'qty')
PRODUCT_HEADER = ('part number', 'description')

# One table row after cleaning: (code, description, quantity).
Row = Tuple[str, str, Optional[int]]


def is_block_code(code: str) -> bool:
    """A row that starts a block: a part number (machine / pre-configured
    model) or a CTO (machine, software). Feature codes are four characters."""
    return bool(LENOVO_PART.match(code) or LENOVO_BLOCK.match(code))


def is_feature_code(code: str) -> bool:
    return bool(LENOVO_FC.match(code))


_THINK = re.compile(r'\bThink(?:System|Agile)\b', re.I)


def is_machine(code: str, description: str) -> bool:
    """A server line: a part number naming a ThinkSystem/ThinkAgile model.
    The word matters: the service line '5WS7C20030 3Yr Premier NBD Resp
    SR630 V4' names the model too, but is not a server."""
    return (is_block_code(code) and _THINK.search(description or '') is not None
            and server_model_from_text(description) is not None)


def to_qty(text) -> Optional[int]:
    text = str(text or '').strip()
    return int(text) if re.fullmatch(r'\d{1,5}', text) else None


def configs_from_rows(rows: List[Row], problems: List[str]) -> List[BOMConfig]:
    """CONFIGURATION DETAILS rows -> configs, child quantities turned into
    totals (x the machine line's count). Rows that cannot be placed are
    reported in ``problems`` instead of being guessed at."""
    configs: List[BOMConfig] = []
    current: Optional[BOMConfig] = None
    acc: Optional[ComponentAcc] = None
    software = False                  # inside a software CTO block
    machines = 0

    def close():
        if current is not None and acc is not None:
            current.components = acc.components() + current.components

    for code, desc, qty in rows:
        if qty is None:
            problems.append('no quantity: %s %s' % (code, desc[:80]))
            continue
        if is_machine(code, desc):
            close()
            machines += 1
            current = BOMConfig(name='Config %d' % machines,
                                server_model=server_model_from_text(desc),
                                components=[], node_count=qty)
            configs.append(current)
            acc = ComponentAcc('part_or_desc', multiplier=qty)
            # The machine line itself is the server: it carries the count.
            acc.add(code, desc, 1, 'chassis')
            software = False
            continue
        if is_block_code(code):
            # Software CTO (XClarity, a Scale Computing licence): its children
            # are licences, kept as 'other' on the machine they follow.
            software = True
            continue
        if not is_feature_code(code):
            problems.append('not a feature code: %s %s' % (code, desc[:80]))
            continue
        if current is None:
            problems.append('part before any machine line: %s %s' % (code, desc[:80]))
            continue
        if software:
            current.components.append(_other(code, desc, qty))
            continue
        if not desc or should_drop(desc, code):
            continue
        acc.add(code, desc, qty, categorize_line(desc))
    close()
    return configs


def _other(code, desc, qty):
    from bom.normalize import BOMComponent
    return BOMComponent(part_number=code, description=desc, quantity=qty, category='other')


def product_quantities(rows: List[Row]) -> Dict[str, int]:
    """Part number -> Qty from PRODUCT AND SERVICE DETAILS (summed, a part
    may be listed twice)."""
    out: Dict[str, int] = {}
    for code, _desc, qty in rows:
        if code and qty is not None:
            out[code] = out.get(code, 0) + qty
    return out


def machine_count_problems(configs: List[BOMConfig], products: Dict[str, int]) -> List[str]:
    """Each machine line's count must be the product table's quantity of the
    same part — the one cross-check the quote offers on the node count."""
    out = []
    for cfg in configs:
        line = next((c for c in cfg.components if c.category == 'chassis' and c.part_number
                     and is_block_code(c.part_number)), None)
        if line is None:
            continue
        listed = products.get(line.part_number)
        if listed is not None and listed != cfg.node_count:
            out.append('%s: %d servers in the configuration, %d in the product list'
                       % (line.part_number, cfg.node_count, listed))
    return out


# ── the Word document ────────────────────────────────────────────────────────

def _cells(row) -> List[str]:
    """A row's cell texts, merged cells once (python-docx repeats a cell for
    every grid column it spans)."""
    out, seen = [], set()
    for c in row.cells:
        if id(c._tc) in seen:
            continue
        seen.add(id(c._tc))
        out.append(' '.join(c.text.split()))
    return out


def _table_rows(table) -> List[List[str]]:
    return [_cells(r) for r in table.rows]


def _header_index(cells: List[str], wanted) -> bool:
    low = [c.lower() for c in cells]
    return all(any(c.startswith(w) for c in low) for w in wanted)


def _open(path: str):
    import docx
    from bom.parsers.common import check_zip_bomb
    check_zip_bomb(path)
    try:
        return docx.Document(path)
    except Exception:
        return None


def _tables(document):
    """(product rows, config rows) as raw cell lists, None when missing."""
    product = config = None
    for table in document.tables:
        rows = _table_rows(table)
        if not rows:
            continue
        if config is None and _header_index(rows[0], CONFIG_HEADER):
            config = rows
        elif product is None and _header_index(rows[0], PRODUCT_HEADER) \
                and any(c.lower() == 'qty' for c in rows[0]):
            product = rows
    return product, config


def _marked(document) -> bool:
    for table in document.tables[:3]:
        for row in table.rows[:8]:
            if any(BID_MARK.search(c) for c in _cells(row)):
                return True
    return any(BID_MARK.search(p.text) for p in document.paragraphs[:30])


def detect(path: str) -> bool:
    document = _open(path)
    if document is None:
        return False
    return _marked(document) and _tables(document)[1] is not None


def _rows(raw: List[List[str]], qty_col: int) -> List[Row]:
    out = []
    for cells in raw[1:]:
        if not any(cells):
            continue
        code = cells[0].replace(' ', '') if cells else ''
        desc = cells[1] if len(cells) > 1 else ''
        qty = to_qty(cells[qty_col]) if len(cells) > qty_col else None
        out.append((code, desc, qty))
    return out


def _qty_col(header: List[str]) -> int:
    low = [c.lower() for c in header]
    return low.index('qty') if 'qty' in low else len(header) - 1


def raw_text(document) -> str:
    """The document's text in reading order (paragraphs and tables
    interleaved), for the reviewer and the agent."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    parts = []
    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit('}', 1)[-1]
        if tag == 'p':
            text = Paragraph(child, document).text.strip()
            if text:
                parts.append(text)
        elif tag == 'tbl':
            for cells in _table_rows(Table(child, document)):
                if any(cells):
                    parts.append('\t'.join(cells))
    return '\n'.join(parts)


def parse(path: str) -> NormalizedBOM:
    from bom.parsers import UnrecognizedFormat  # local import: package imports us
    document = _open(path)
    if document is None:
        raise UnrecognizedFormat('Not a readable Word document.')
    product_raw, config_raw = _tables(document)
    if config_raw is None:
        raise UnrecognizedFormat('Lenovo bid quote without a CONFIGURATION DETAILS table.')
    problems: List[str] = []
    configs = configs_from_rows(_rows(config_raw, _qty_col(config_raw[0])), problems)
    if not configs:
        raise UnrecognizedFormat('Lenovo bid quote with no ThinkSystem machine line.')
    if product_raw is not None:
        products = product_quantities(_rows(product_raw, _qty_col(product_raw[0])))
        problems += machine_count_problems(configs, products)
    if problems:
        # A strict parser: a row it cannot place or a node count the quote
        # contradicts is the agent's (or the template's) case, never a guess.
        raise UnrecognizedFormat('Lenovo bid quote the parser cannot read with confidence: '
                                 + '; '.join(problems[:3]))
    return NormalizedBOM(vendor='Lenovo', configs=configs, raw_text=raw_text(document)[:60000])
