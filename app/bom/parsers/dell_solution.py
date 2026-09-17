"""Dell Solution (Smart Selection) export — the grouped module/option sheet.

Dell's solution builder exports one row per configured option, grouped under a
product line that carries the node count:

    Group Name | Group ID | Product Name | Product Quantity |
    Module Name | Option ID | Option Name | SKUs | Qty | Parent Order Code

which the German UI writes as

    Gruppenname | Gruppen-ID | Produktname | Produktmenge |
    Name des Moduls | Options-ID | Name der Option | SKUs | Menge | …

Close kin of the VNET export (dell_quote.parse_vnet), but NOT the same sheet:
there the module column is column 1 and the quantity IS the node count, while
here the node count sits on the group row (Produktmenge = 3) and each option's
Menge is **per node** — 8 DIMMs and 4 drives per machine, not per cluster. Read
the VNET way, a 3-node cluster came out as one node with 8 DIMMs.

Quantities are emitted as totals across the config's machines (qty x nodes),
the same convention as every other parser here.

Language: the header, the module names and the absence wording are localised by
Dell, and the three files this was built from are German with inconsistent
translations between generations ('RAID/Interne Storage-Controller' in one,
'RAID/Interne Speichercontroller' in the next; 'Zusätzliche Prozessor' vs
'Zusätzlicher Prozessor'). So modules are matched on normalised keywords rather
than exact strings, in both languages, and anything unmatched stays 'other'
(visible to the rules) rather than being guessed at. The German absence and
drive wording lives in parsers/common.py and rules.py with the Portuguese that
arrived the same way.

The SKUs column is what the HCL check actually matches on, so a German
description never stops a part being identified — it only affects the
human-readable text and the capacity parsing in bom/fit.py.
"""
import re
from typing import Dict, List, Optional, Tuple

from bom.normalize import BOMComponent, BOMConfig, NormalizedBOM
from bom.parsers.common import (
    cell,
    head_rows,
    is_absence,
    raw_text_from_rows,
    s,
    server_model_from_text,
    sheet_matrix,
    should_drop,
    to_int,
)

FORMAT_SOLUTION = 'dell_solution'

# Header labels, per column role, lower-cased. A sheet is this shape when the
# module, option and quantity columns are all present on one row together with
# the product-quantity column that carries the node count — that last one is
# what separates it from the VNET export.
_HEADERS = {
    'group': ('group name', 'gruppenname'),
    'product': ('product name', 'produktname'),
    # 'Product Qty' is what the English export actually says (2026-09-17,
    # Darksite DACH); 'Product Quantity' kept in case another locale spells
    # it out.
    'nodes': ('product qty', 'product quantity', 'produktmenge', 'produkt menge'),
    'module': ('module name', 'name des moduls', 'modulname'),
    'option': ('option name', 'name der option', 'optionsname'),
    'skus': ('skus', 'sku'),
    'qty': ('qty', 'quantity', 'menge', 'anzahl'),
}


def _norm(text: str) -> str:
    """Lower-case, strip decoration and collapse separators, so 'Gehäuse-
    Konfiguration' and 'Gehäusekonfiguration' compare equal enough to match on
    keywords."""
    text = (text or '').strip().lower().rstrip('*').strip()
    return re.sub(r'[\s\-/:]+', ' ', text).strip()


# Ordered rules: the FIRST match wins, so the narrow settings modules are
# listed before the broad hardware keywords they would otherwise fall into
# ('RAID-Konfiguration' is a setting, 'RAID/Interne Speichercontroller' is the
# controller; 'Kabel für Netzwerkkarten' is cabling, not a NIC).
_DROP = object()
_OTHER = object()

_MODULE_RULES: List[Tuple[str, object]] = [
    # ── settings and services: no hardware meaning ──────────────────────────
    (r'thermal|thermisch|temperatur|kuhl|kühl|cooling|heat ?sink', _DROP),
    (r'dimm (speichertyp|type)|memory dimm|speicher dimm', _DROP),
    (r'(memory|speicher) ?konfigurations?typ|typ von speicherkonfiguration'
     r'|arbeitsspeicherkonfigurationstyp|memory configuration type', _DROP),
    (r'^bios|erweiterte systemkonfiguration|advanced system configuration', _DROP),
    (r'raid ?konfiguration|raid configuration', _DROP),
    (r'kennwort|passwort|password|group manager', _DROP),
    (r'betriebssystem|operating system|os media|datentrager|datenträger', _DROP),
    (r'secure onboarding|dokumentation|documentation|eccn', _DROP),
    (r'versand|shipping|verpackung|packaging|regulat|gesetzliche|regulierung', _DROP),
    (r'service|bereitstellung|deployment|diebstahlschutz|bestandskennzeichnung', _DROP),
    (r'anti theft|asset tag|systems? management|systemverwaltung', _DROP),
    (r'smart selection|additional processor features', _DROP),
    # ── cabling and optics: hardware, but never a NIC ───────────────────────
    (r'kabel|cable|optic', _OTHER),
    # ── hardware ────────────────────────────────────────────────────────────
    # The base module is 'Base' / 'Basis', or — in the English solution export
    # — simply the server's name ('PowerEdge R6615').
    (r'^basis$|^base$|^poweredge\b|hauptplatine|motherboard', 'chassis'),
    (r'gehause|gehäuse|chassis', 'chassis'),
    (r'prozessor|processor|cpu', 'cpu'),
    (r'speicherkapazitat|speicherkapazität|memory capacity|arbeitsspeicher', 'memory'),
    (r'(storage|speicher)ontroller|interne (storage|speicher)|storage controller'
     r'|speichercontroller|hba|perc', 'controller'),
    (r'boot ?optimierte|boot optimized|boss', 'boss'),
    (r'festplatte|hard drive|hard disk|pcie ?ssd|flex ?bay|laufwerk', 'storage'),
    (r'netzwerkadapter|netzwerkkarte|network adapter|network card|ocp|nic|lom', 'nic'),
    (r'gpu|fpga|grafik|graphics', 'gpu'),
]

_MODULE_RULES_COMPILED = [(re.compile(rx), cat) for rx, cat in _MODULE_RULES]


def _category_for_module(module: str) -> Optional[str]:
    """Category for a module name, or None when the row should be dropped."""
    key = _norm(module)
    for rx, cat in _MODULE_RULES_COMPILED:
        if rx.search(key):
            if cat is _DROP:
                return None
            return 'other' if cat is _OTHER else cat
    return 'other'


def _header(rows) -> Optional[Dict[str, int]]:
    """{role: column} for the header row, or None when this is not the shape.

    Scans further down than the other parsers (Dell puts four solution-metadata
    rows above it) and requires the node-count column, which is what the VNET
    export does not have.
    """
    for r in range(1, min(len(rows), 12) + 1):
        found: Dict[str, int] = {}
        for c in range(1, 16):
            label = _norm(cell(rows, r, c))
            if not label:
                continue
            for role, labels in _HEADERS.items():
                if role not in found and label in labels:
                    found[role] = c
        if all(role in found for role in ('module', 'option', 'qty', 'nodes')):
            return found
    return None


def _sheet(wb):
    for ws in wb.worksheets:
        if _header(head_rows(ws, 12, 16)) is not None:
            return ws
    return None


def detect(wb) -> Optional[str]:
    return FORMAT_SOLUTION if _sheet(wb) is not None else None


class _Group:
    """One product group: a machine and how many of it."""

    def __init__(self, name: str, product: str, nodes: int):
        self.name = name
        self.product = product
        self.nodes = max(nodes, 1)
        self.order: List[str] = []
        self.items: Dict[str, BOMComponent] = {}
        self.model: Optional[str] = server_model_from_text(product)

    def add(self, sku: Optional[str], desc: str, qty: int, category: str):
        # Per-node quantity x machines: the rest of the checker (rules, fit)
        # reads quantities as totals across the config.
        total = max(qty, 1) * self.nodes
        key = sku or ('desc:' + desc)
        if key in self.items:
            self.items[key].quantity += total
        else:
            self.items[key] = BOMComponent(part_number=sku, description=desc,
                                           quantity=total, category=category)
            self.order.append(key)

    def config(self, index: int) -> BOMConfig:
        label = self.product or self.name or ('Config %d' % index)
        return BOMConfig(name=label.strip()[:120] or ('Config %d' % index),
                         server_model=self.model,
                         components=[self.items[k] for k in self.order],
                         node_count=self.nodes)


def parse(path: str) -> NormalizedBOM:
    from bom.parsers import UnrecognizedFormat
    from bom.parsers.common import load_workbook_safe

    wb = load_workbook_safe(path)
    try:
        ws = _sheet(wb)
        if ws is None:
            raise UnrecognizedFormat('Not a Dell solution export.')
        rows = sheet_matrix(ws)
    finally:
        wb.close()

    cols = _header(rows)
    hdr_row = None
    for r in range(1, min(len(rows), 12) + 1):
        if _norm(cell(rows, r, cols['module'])) in _HEADERS['module']:
            hdr_row = r
            break
    if hdr_row is None:                                   # pragma: no cover
        raise UnrecognizedFormat('Not a Dell solution export.')

    groups: List[_Group] = []
    current: Optional[_Group] = None
    for r in range(hdr_row + 1, len(rows) + 1):
        product = cell(rows, r, cols['product']) if 'product' in cols else ''
        nodes_cell = cell(rows, r, cols['nodes'])
        # A group row names the machine and its count; option rows leave both
        # empty and belong to the group above them.
        if product or (nodes_cell and not cell(rows, r, cols['module'])):
            name = cell(rows, r, cols['group']) if 'group' in cols else ''
            current = _Group(name, product, to_int(nodes_cell, default=1))
            groups.append(current)
            continue

        module = cell(rows, r, cols['module'])
        option = cell(rows, r, cols['option'])
        if not module or not option or current is None:
            continue
        skus = [t.strip() for t in cell(rows, r, cols['skus']).split(',')
                if t.strip()] if 'skus' in cols else []
        sku = skus[0] if skus else None
        qty = to_int(cell(rows, r, cols['qty']), default=1)

        if is_absence(option):
            # Kept, as 'other', so "Ohne BOSS-Karte" stays visible to the rules
            # instead of looking like a BOSS card that was never quoted.
            category = 'other'
        else:
            category = _category_for_module(module)
            if category is None or should_drop(option, sku):
                continue
        if current.model is None and category == 'chassis':
            current.model = server_model_from_text(option)
        current.add(sku, option, qty, category)

    groups = [g for g in groups if g.order]
    if not groups:
        raise UnrecognizedFormat('Dell solution export has no option rows.')
    return NormalizedBOM(vendor='Dell',
                         configs=[g.config(i + 1) for i, g in enumerate(groups)],
                         raw_text=raw_text_from_rows(rows))
