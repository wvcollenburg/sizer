"""Deterministic BOM file parsers (Tier 1) and the strict template (Tier 2).

Entry points:

    detect_format(path, filename) -> Optional[str]   which shape, or None
    parse_file(path, filename)    -> (NormalizedBOM, fmt)

Why detection is a fixed ladder and never a guess: the plan
(docs/bom-checker-plan.md) forbids a best-effort parse — an unrecognised
file is the *trigger* for the template / AI pre-fill path, not something to
approximate. So each parser has a narrow structural signature (survey §2)
and the ladder returns the first match, in an order where the more specific
signatures come first: our own template marker, then the Lenovo DCSC title
cell, then the Dell service-tag header, the Dell quote (SKU/Description/Qty
header AND a 'Quote number:' cell), the D&H bid sheet, the VNET module
export, and finally the three hand-typed Dell list layouts. Anything else —
including a PDF — raises UnrecognizedFormat with a hint for the UI.

Both files types are checked by magic before openpyxl/csv see them: an xlsx
must be a ZIP ('PK\\x03\\x04') and a CSV must decode as UTF-8 or latin-1
text; uploads are untrusted and the sheet caps in xlsx_utils apply to every
sheet we read (SheetTooLargeError propagates to the route).
"""
import os
from typing import Optional, Tuple

from bom.normalize import NormalizedBOM


class UnrecognizedFormat(ValueError):
    """The file matches none of the deterministic parsers."""

    def __init__(self, hint: str = ''):
        self.hint = hint or ('Not a recognised BOM export. Download the template, '
                             'fill it in and upload that instead.')
        super().__init__(self.hint)


FORMAT_LABELS = {
    'template': 'Scale BOM template',
    'lenovo_dcsc': 'Lenovo DCSC quote export',
    'dell_service_tag': 'Dell service-tag component export',
    'dell_quote': 'Dell quote export',
    'dh_bid': 'D&H bid quotation',
    'dell_vnet': 'Dell VNET configurator export',
    'dell_list_sku': 'Dell configuration list (QTY / Config / Available SKUs)',
    'dell_list_qty_desc_pn': 'Dell configuration list (QTY / Description / Part Number)',
    'dell_list_columns': 'Dell configuration list (one column per config)',
}

XLSX_MAGIC = b'PK\x03\x04'
ACCEPTED_EXTENSIONS = ('.xlsx', '.csv')


def _extension(filename: Optional[str], path: str) -> str:
    name = filename or path or ''
    return os.path.splitext(name)[1].lower()


def _is_xlsx(path: str) -> bool:
    try:
        with open(path, 'rb') as fh:
            return fh.read(4) == XLSX_MAGIC
    except OSError:
        return False


def _detect_xlsx(path: str) -> Optional[str]:
    from bom.parsers import dell_lists, dell_quote, dell_service_tag, lenovo_dcsc, template
    from bom.parsers.common import load_workbook_safe
    try:
        wb = load_workbook_safe(path)
    except Exception:                       # not a workbook openpyxl can read
        return None
    try:
        if template.detect(wb):
            return template.FORMAT
        if lenovo_dcsc.detect(wb):
            return lenovo_dcsc.FORMAT
        if dell_service_tag.detect(wb):
            return dell_service_tag.FORMAT
        if dell_quote.detect_quote(wb):
            return dell_quote.FORMAT_QUOTE
        if dell_quote.detect_dh(wb):
            return dell_quote.FORMAT_DH
        if dell_quote.detect_vnet(wb):
            return dell_quote.FORMAT_VNET
        return dell_lists.detect(wb)
    finally:
        wb.close()


def detect_format(path: str, filename: Optional[str] = None) -> Optional[str]:
    """Format id for a file, or None when nothing matches (PDF, docx, an
    unknown spreadsheet, a corrupt upload)."""
    ext = _extension(filename, path)
    if ext == '.csv':
        from bom.parsers import dell_service_tag
        return dell_service_tag.FORMAT if dell_service_tag.detect_csv(path) else None
    if ext == '.xlsx' and _is_xlsx(path):
        return _detect_xlsx(path)
    return None


def parse_file(path: str, filename: Optional[str] = None) -> Tuple[NormalizedBOM, str]:
    """Detect and parse. Raises UnrecognizedFormat (nothing matched),
    template.TemplateError (our template, but invalid) or
    xlsx_utils.SheetTooLargeError (oversized sheet)."""
    from bom.parsers import dell_lists, dell_quote, dell_service_tag, lenovo_dcsc, template
    fmt = detect_format(path, filename)
    if fmt is None:
        ext = _extension(filename, path)
        if ext not in ACCEPTED_EXTENSIONS:
            raise UnrecognizedFormat('Only .xlsx and .csv BOM exports are parsed directly; '
                                     'use the template (or the AI pre-fill) for other files.')
        raise UnrecognizedFormat()
    if fmt == template.FORMAT:
        bom = template.parse_template(path)
    elif fmt == lenovo_dcsc.FORMAT:
        bom = lenovo_dcsc.parse(path)
    elif fmt == dell_service_tag.FORMAT:
        bom = dell_service_tag.parse(path)
    elif fmt == dell_quote.FORMAT_QUOTE:
        bom = dell_quote.parse_quote(path)
    elif fmt == dell_quote.FORMAT_DH:
        bom = dell_quote.parse_dh(path)
    elif fmt == dell_quote.FORMAT_VNET:
        bom = dell_quote.parse_vnet(path)
    else:
        bom = dell_lists.parse(path)
    return bom, fmt


def detect_vendor(bom: NormalizedBOM) -> str:
    """Vendor from the content, for shapes that do not imply one (the
    template): Lenovo/Dell/Supermicro/HPE markers in models, parts and
    descriptions; 'Unknown' otherwise (survey §2)."""
    import re
    texts = []
    for cfg in bom.configs:
        texts.append(cfg.server_model or '')
        for comp in cfg.components:
            texts.append(comp.part_number or '')
            texts.append(comp.description or '')
    blob = '\n'.join(texts)
    if re.search(r'ThinkSystem|\b7[0-9A-Z]{3}CTO\d+WW\b', blob):
        return 'Lenovo'
    if re.search(r'PowerEdge|^\d{3}-[A-Z0-9]{4}$', blob, re.M):
        return 'Dell'
    if re.search(r'\b(?:SYS-|AOC-|MEM-DR|HDS-|PWS-|MCP-)', blob):
        return 'Supermicro'
    if re.search(r'ProLiant|^P\d{5}-B21$', blob, re.M):
        return 'HPE'
    return 'Unknown'
