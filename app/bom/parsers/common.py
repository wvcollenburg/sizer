"""Helpers every deterministic BOM parser shares.

Why this module exists: the eight file shapes we recognise (Lenovo DCSC,
Dell service-tag export, Dell quote, D&H bid, Dell VNET module export and
three hand-made Dell lists) all end up asking the same three questions of a
line item — "is this junk?", "is this an absence indicator the rules engine
needs?" and "which of the nine categories is it?" — and they all have to
coerce the same kinds of sloppy cells (floats for integers, '3,0', NBSP).
Keeping the answers here means a category fix lands in every parser at once
and the archive-optional parity test (tests/test_bom_parsers.py) exercises
one implementation.

Design rules that matter downstream:

  - categorize() reproduces the categories SC//Design's LLM extractor gave
    the 26 archived BOMs (survey §4), with the survey's documented fixes:
    spec-only lines ('6400MT/s RDIMMs') are dropped, 'GB Base-T' (with a
    space) is a NIC, and a motherboard *with an on-board LOM* is a NIC so
    the LOM/backplane-over-VLAN info finding still fires.
  - Absence indicators ('No BOSS', 'BOSS Blank', 'No Controller', 'No RAID',
    'LOM Blank', 'No Additional Processor'...) are NEVER dropped: bom.rules
    treats them as evidence that a component was deliberately left out
    (is_absence_indicator), so they must survive as category 'other'.
  - Everything else that is pure order-entry noise (labels, fillers, shipping
    material, documentation, passwords, BIOS/iDRAC settings, support SKUs)
    is dropped before categorisation so it can never be mistaken for
    hardware by a keyword hit ('Power Saving Dell Active Power Controller'
    is not a controller).
  - Descriptions stay exactly as the source wrote them (after the Dell
    30-character de-wrap); we classify pt-BR DCSC exports by adding the
    Portuguese keywords rather than translating.

Pure Python + openpyxl; no Flask, no DB.
"""
import math
import re
import warnings
from typing import Any, List, Optional, Sequence, Tuple

from xlsx_utils import MAX_SHEET_COLS, MAX_SHEET_ROWS, SheetTooLargeError

# ─── identifiers ─────────────────────────────────────────────────────────────

DELL_SKU = re.compile(r'^\d{3}-[A-Z0-9]{4}$')          # 405-AAZF, 892-9155
LENOVO_FC = re.compile(r'^[0-9A-Z]{4}$')                # C3QL, BM50, 5977, 6400
LENOVO_CTO = re.compile(r'^7[0-9A-Z]{3}CTO\d+WW$')      # 7DGDCTO1WW, 7D73CTO3WW
DELL_PIECE = re.compile(r'^[0-9A-Z]{5}$')               # DMF5Y, 08123

# A leading "8x " / "4 X " multiplier on a hand-typed description.
NX_PREFIX = re.compile(r'^(\d+)\s*[xX]\s+(.*)$')


# ─── cell coercion ────────────────────────────────────────────────────────────

def s(v: Any) -> str:
    """Cell value as a stripped string. None -> ''. Integral floats print as
    ints ('3.0' cells are a common openpyxl artefact) so text comparisons
    such as `== 'Qty'` and part-number regexes behave."""
    if v is None:
        return ''
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        if math.isfinite(v) and v == int(v):
            return str(int(v))
        return str(v)
    return str(v).replace('\xa0', ' ').strip()


def to_int(v: Any, default: int = 1) -> int:
    """Quantity coercion: 3, 3.0, '3', '3,0', ' 3 ' -> 3; None/''/junk ->
    default. Booleans are never quantities."""
    if v is None or isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if math.isfinite(v) else default
    text = str(v).strip().replace('\xa0', '')
    if not text:
        return default
    try:
        return int(float(text.replace(',', '.')))
    except ValueError:
        return default


# ─── workbook access (bounded) ────────────────────────────────────────────────

def load_workbook_safe(path: str):
    """read_only + data_only like every other importer in the app; openpyxl's
    'no default style' warning on some Lenovo exports is noise."""
    from openpyxl import load_workbook
    with warnings.catch_warnings():
        warnings.filterwarnings('ignore', module='openpyxl')
        return load_workbook(path, read_only=True, data_only=True)


def sheet_matrix(ws, max_rows: int = MAX_SHEET_ROWS) -> List[Tuple]:
    """Materialise a worksheet as a list of row tuples, bounded by the same
    caps xlsx_utils applies to RVTools/LiveOptics uploads: an .xlsx is a ZIP
    and a declared used-range of millions of empty cells would otherwise
    expand until the worker OOMs. Trailing all-None rows are kept (row
    numbers must stay 1:1 with the sheet for error messages)."""
    rows = []
    for i, row in enumerate(ws.iter_rows(values_only=True, max_col=MAX_SHEET_COLS)):
        if i >= max_rows:
            raise SheetTooLargeError(
                f"Sheet '{ws.title}' exceeds the maximum of {max_rows} rows.")
        rows.append(tuple(row))
    return rows


def head_rows(ws, n: int = 16, cols: int = 24) -> List[Tuple]:
    """First n rows only — what detection needs, without reading the sheet."""
    out = []
    for row in ws.iter_rows(min_row=1, max_row=n, max_col=cols, values_only=True):
        out.append(tuple(row))
    return out


def cell(rows: Sequence[Tuple], r: int, c: int) -> str:
    """1-based (row, col) -> s(value); out of range -> ''."""
    if r < 1 or r > len(rows):
        return ''
    row = rows[r - 1]
    if c < 1 or c > len(row):
        return ''
    return s(row[c - 1])


def raw(rows: Sequence[Tuple], r: int, c: int) -> Any:
    if r < 1 or r > len(rows):
        return None
    row = rows[r - 1]
    if c < 1 or c > len(row):
        return None
    return row[c - 1]


def row_is_blank(rows: Sequence[Tuple], r: int) -> bool:
    if r < 1 or r > len(rows):
        return True
    return all(s(v) == '' for v in rows[r - 1])


def raw_text_from_rows(rows: Sequence[Tuple], limit: int = 60000) -> str:
    """Tab-joined dump of the non-blank rows, capped — the rawText field is
    for humans reviewing a flagged check, not for the validator."""
    lines = []
    size = 0
    for row in rows:
        vals = [s(v) for v in row]
        if not any(vals):
            continue
        line = '\t'.join(vals).rstrip('\t')
        size += len(line) + 1
        if size > limit:
            lines.append('…')
            break
        lines.append(line)
    return '\n'.join(lines)


# ─── Dell 30-character wrap artefact ─────────────────────────────────────────

_WRAP = 30


def dewrap_dell(text: str) -> str:
    """Undo the service-tag export's hard wrap.

    The exporter cuts the option text into 30-character chunks, right-trims
    each chunk and joins them with a single space. Verified against the three
    archived exports: 'UEFI BIOS Boot Mode with GPT P artition', 'C13 , 3M',
    '10GbE  BASE-T' (original had a space at the chunk start) and '512e 3.5in'
    (original chunk ended in a space, so the join space *is* the original
    space) all reproduce from exactly that model. Walking 30 characters at a
    time therefore recovers the original: after a full chunk that does not
    end in a space, the next character is the inserted join space and is
    removed; a chunk that ends in a space was trimmed-then-rejoined, so the
    following character already belongs to the next chunk.

    Only apply this to the Dell service-tag shape — on unwrapped text it
    would eat a legitimate space every 30 characters.
    """
    out = []
    pos = 0
    n = len(text)
    while pos < n:
        chunk = text[pos:pos + _WRAP]
        out.append(chunk)
        pos += _WRAP
        if len(chunk) == _WRAP and not chunk.endswith(' ') and pos < n and text[pos] == ' ':
            pos += 1
    return ''.join(out)


# ─── server model ─────────────────────────────────────────────────────────────

_MODEL_PATTERNS = [
    (re.compile(r'\bThinkSystem\s+(S[RT]\d{3}[a-z]?)\s*(V\d)\b', re.I),
     lambda m: 'ThinkSystem %s %s' % (m.group(1).upper(), m.group(2).upper())),
    (re.compile(r'\b(S[RT]\d{3})\s*(V\d)\b'),
     lambda m: 'ThinkSystem %s %s' % (m.group(1), m.group(2))),
    (re.compile(r'\b(PowerEdge\s+[A-Z]{1,2}\d{3,4}[A-Za-z]*)\b'),
     lambda m: m.group(1)),
    (re.compile(r'\bDell\s+(R\d{3}[A-Za-z]*)\b'),
     lambda m: 'PowerEdge ' + m.group(1)),
    (re.compile(r'\b(ProLiant\s+[A-Z]{2}\d{3,4}[a-z]*(?:\s+Gen\s?\d+)?)', re.I),
     lambda m: m.group(1)),
    (re.compile(r'\b(SYS-[0-9A-Z]+(?:-[0-9A-Z]+)*)\b'),
     lambda m: m.group(1)),
]


def server_model_from_text(text: Optional[str]) -> Optional[str]:
    """'Server : ThinkSystem SR650 V4-3yr Base Warranty' -> 'ThinkSystem SR650 V4';
    'PowerEdge R760 Server' -> 'PowerEdge R760'; 'Dell R750XS 12x 3.5in LFF'
    -> 'PowerEdge R750XS'; None when nothing recognisable is present."""
    if not text:
        return None
    for rx, fmt in _MODEL_PATTERNS:
        m = rx.search(text)
        if m:
            return fmt(m)
    return None


# ─── junk / absence / category rules ─────────────────────────────────────────

# Absence indicators the rules engine relies on (rules.is_absence_indicator,
# is_boss_card, is_controller). These win over DROP: a BOM that explicitly
# says 'No BOSS Card' must keep saying so.
ABSENCE = re.compile(
    r'^(?:No\s+(?:BOSS|Controller|HBA|RAID|PERC|OCP|Additional Processor|Second Processor|'
    r'Hard Drive|Rear Storage|GPU|Internal|Trusted Platform|TPM)\b'
    r'|(?:Assembly\s+)?BOSS Blank|LOM Blank|Riser Blank|OCP Blank|.*\bBlank\s*$'
    r'|Unconfigured RAID|C\d+,\s*No RAID|No RAID|Select Storage devices'
    r'|Dispositivos de armazenamento)',
    re.I)

# Pure order-entry noise. Anchored alternatives first, then whole-word hits
# anywhere, then spec-only memory lines. Support/service SKU families are
# handled by part-number prefix in should_drop().
DROP = re.compile(
    r'(?:^(?:UEFI|Performance BIOS|Performance Optimized|RAID\s+\d|Configuration Services|'
    r'System Box|Custom Configuration|Quick Sync|Power Saving|Dell Connectivity|'
    r'Secure Onboarding|Secured Component|Server Secured|OpenManage|DHCP|'
    r'Additional Processor Selected|No HBM|None Required|Decline Selection|'
    r'Basic Next Business Day|ProSupport|Keep Your Drive|Diversified Supply|'
    r'Data Center Environment|Operating mode selection|Disable IPMI|Trigger MFG|'
    r'Registration only|Top Choice|ERP LOT9|Low voltage|High voltage|N\+N Redundancy|'
    r'Feature Enable TPM|Notice for Advanced Format|Configuration ID|'
    r'Configuration Instruction|Controller 0\d|Months|KYD|Premier|Next Business Day|'
    r'A/C Power Recovery|Power Management|Apenas registro|Redund[âa]ncia|Baixa voltagem|'
    r'Alta voltagem|Aviso para|Desativar IPMI|Ambiente de data center|Computa[çc][ãa]o geral|'
    r'Recurso Habilitar|CPK para|Indicador de servi[çc]o|HV \d+U)\b'
    r'|\b(?:Shipping|Marking|Label|Etiqueta|Luggage|Password|Blanks? for|Documentation|'
    r'Media Required|Operating System|Utility Partition|Placement|Filler|Preenchimento|'
    r'Dummy|Sponge|Esponja|Air Duct|Duto de ar|PKG BOM|PACOTE BOM|Packag(?:e|ing)|Welcome Kit|'
    r'EULA|Preload|Laser service|Custom MFG|Customer Solution Center|Service Label|'
    r'Field Deployment|Deployment Services|Energy Star|Support Services?|Warranty|'
    r'XClarity|XCC|FOD|Root of Trust|Power Efficiency|efici[êe]ncia de energia|LPK|'
    r'iDRAC[ ,]+(?:Legacy|Factory|Group)|Group Manager|Factory Generated|Mylar)\b'
    r'|\bATE\s*$'
    r'|^\d+\s*MT/s\s+RDIMMs?\s*$)',
    re.I)

# Dell SKU families that never carry hardware: services/support (709/865/883/
# 892/989/900), iDRAC-and-flavour info lines (379-) and BIOS boot mode (800-).
_DROP_SKU_PREFIX = re.compile(r'^(?:709|865|883|892|989|900|379|800)-')


def is_absence(description: str) -> bool:
    return bool(ABSENCE.match(description or ''))


def should_drop(description: str, part_number: Optional[str] = None) -> bool:
    """True for rows that carry no hardware meaning. Absence indicators are
    protected first; then the description junk list; then Dell SKU families."""
    desc = (description or '').strip()
    if not desc:
        return True
    if is_absence(desc):
        return False
    if DROP.search(desc):
        return True
    if part_number and _DROP_SKU_PREFIX.match(part_number.strip()):
        return True
    return False


# 'No X' / 'X Blank' lines classify as 'other' regardless of keywords so
# 'No BOSS Card' never counts as a BOSS card.
NEGATED = re.compile(
    r'^(?:No\b|None\b|Nenhum\b|Assembly BOSS Blank|BOSS Blank|LOM Blank|Riser Blank|'
    r'.*\bBlank\s*$|Decline|C\d+,\s*No RAID|Unconfigured|no configured|'
    r'Select Storage devices|Dispositivos de armazenamento)',
    re.I)

# A line that IS the server (the chassis/base line) — checked before the
# 'other' rule so 'Dell R750XS 12x 3.5in LFF, Riser 4 Config' is not 'other'
# because of 'Riser'.
SERVER_LINE = re.compile(
    r'^(?:Dell\s+)?PowerEdge\s+[A-Z]{1,2}\d{3,4}[A-Za-z]*(?:\s+Server)?\s*$'
    r'|^Dell\s+R\d{3}[A-Za-z]*\b.*\b(?:Configure to Order|LFF|SFF|Server)\b'
    r'|^ThinkSystem\s+S[RT]\d{3}\s*V\d\s*$'
    r'|^(?:HPE\s+)?ProLiant\s+[A-Z]{2}\d{3}',
    re.I)

_CAP = r'\d+(?:[.,]\d+)?\s*(?:TB|GB)\b'
_DRV = (r'(?:SSD|HDD|NVMe|NVME|Hard Drive|Hard Disk|Solid State|Drive|Disk|'
        r'disco r[íi]gido|estado s[óo]lido)')

RULES = [
    ('other', re.compile(
        r'\b(?:Label|Filler|Dummy|Blank|Sponge|Air Duct|Cable|CBL|Cabo|Cord|Clip|Clipe|Latch|Trava|'
        r'Luggage|Bezel|Rails?|ReadyRails|Trilho|Heatsink|Heat Sink|Dissipador|Fans?|Ventilador|'
        r'Power Supply|Fonte de alimenta[çc][ãa]o|PSU|Riser|Cage|Compartimento|TPM|'
        r'Trusted Platform|iDRAC|XClarity|XCC|Shipping|Marking|Warranty|Support|Services?|'
        r'License|Licen[çc]a|Subscription|Placement|Mechanical Parts|Bracket|Tray|Foam|'
        r'Interposer|Duct|Duto|Placa do processador|Placa de E/S|MB|'
        r'Board\b(?!.*\bLOM\b)|Motherboard(?!.*\bLOM\b))\b', re.I)),
    ('boss', re.compile(
        r'\b(?:BOSS(?:-[NS]\d)?|M\.2 RAID|B540p|M\.2 Mirroring|M\.2 .*Adapter|'
        r'Boot Optimized)\b', re.I)),
    ('gpu', re.compile(
        r'\b(?:GPU|NVIDIA|Tesla|RTX|L40S?|L4|A\d{2}|H100|H200|Instinct)\b', re.I)),
    ('controller', re.compile(
        r'\b(?:HBA\s?\d{3}[a-z]*|PERC\s?H\d{3}[A-Z]?|4[34]0-\d+[ie]|4350-\d+[ie]|'
        r'9\d{3}-\d+[ie]|SAS3?\s+HBA|HBA ThinkSystem|RAID\s+(?:Controller|Adapter|Card)|'
        r'Storage Controller|MR216i|MR416i|AOC-S38\d\d|AOC-S3008|HBA\s*$)\b', re.I)),
    ('nic', re.compile(
        r'\b(?:Ethernet Adapter|Adaptador Ethernet|Ethernet|Network (?:Card|Adapter|Interface)|'
        r'OCP NIC|OCP Ethernet|rNDC|LOM|\d+\s*GbE|\d+\s*GBase-T|\d+\s*GB\s*Base-T|SFP28|SFP\+|'
        r'X710|E810|E610|XL710|X557|I350|5750\d|5741[46]|5720|ConnectX|NIC|BCM5\d{4})\b', re.I)),
    ('chassis', re.compile(r'\b(?:Chassis|Chassi|Backplane|Media Bay)\b', re.I)),
    ('cpu', re.compile(r'\b(?:Xeon|EPYC|Processor|Processador|Ryzen)\b', re.I)),
    ('memory', re.compile(r'\b(?:RDIMM|UDIMM|LRDIMM|TruDDR\d|DDR[45]|DIMM|MRDIMM)\b', re.I)),
    ('storage', re.compile(r'(?:' + _CAP + r'.*\b' + _DRV + r'\b)|(?:\b' + _DRV + r'\b.*' + _CAP + r')')),
    ('chassis', re.compile(
        r'(?:\bServer\s*$|^(?:Dell\s+)?PowerEdge\s+\w+|^ThinkSystem\s+S[RT]\d{3}|\bBase Server\b|'
        r'\bSYS-\d{3}|Optimized System|X13SCH|^(?:HPE\s+)?ProLiant|^Dell\s+R\d{3})', re.I)),
]


def categorize(description: str) -> str:
    """Nine-way classification of a line item description (survey §4).
    First hit wins; order matters and is documented in RULES."""
    d = (description or '').strip()
    if not d:
        return 'other'
    if NEGATED.match(d):
        return 'other'
    if SERVER_LINE.match(d):
        return 'chassis'
    for cat, rx in RULES:
        if rx.search(d):
            return cat
    return 'other'


def is_m2_media(description: str) -> bool:
    """M.2 boot sticks are not data drives: SC//Design's validator ignored
    them (the Arrow expected findings carry no 2-drive/flash-ratio noise)."""
    return re.search(r'\bM\.2\b', description or '') is not None


def split_nx(description: str) -> Tuple[int, str]:
    """'8x 16GB RDIMM…' -> (8, '16GB RDIMM…'); no prefix -> (1, description)."""
    m = NX_PREFIX.match((description or '').strip())
    if m:
        return int(m.group(1)), m.group(2).strip()
    return 1, (description or '').strip()
