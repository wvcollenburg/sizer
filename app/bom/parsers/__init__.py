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
export, the Dell solution export (grouped module/option rows, localised
headers), and finally the three hand-typed Dell list layouts. Anything else —
including a PDF — raises UnrecognizedFormat with a hint for the UI.

Both files types are checked by magic before openpyxl/csv see them: an xlsx
must be a ZIP ('PK\\x03\\x04') and a CSV must decode as UTF-8 or latin-1
text; uploads are untrusted and the sheet caps in xlsx_utils apply to every
sheet we read (SheetTooLargeError propagates to the route).
"""
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional, Tuple

from bom.normalize import NormalizedBOM


class UnrecognizedFormat(ValueError):
    """The file matches none of the deterministic parsers."""

    def __init__(self, hint: str = ''):
        self.hint = hint or ('Not a recognised BOM export. Download the template, '
                             'fill it in and upload that instead.')
        super().__init__(self.hint)


FORMAT_LABELS = {
    'template': 'Scale Computing BOM template',
    'lenovo_dcsc': 'Lenovo DCSC quote export',
    'dell_service_tag': 'Dell service-tag component export',
    'dell_quote': 'Dell quote export',
    'dh_bid': 'D&H bid quotation',
    'dell_vnet': 'Dell VNET configurator export',
    'dell_solution': 'Dell Solution (Smart Selection) export',
    'dist_item_list': 'Distributor item list (Item # / Vendor Part # / Qty)',
    'dell_list_sku': 'Dell configuration list (QTY / Config / Available SKUs)',
    'dell_list_qty_desc_pn': 'Dell configuration list (QTY / Description / Part Number)',
    'dell_list_columns': 'Dell configuration list (one column per config)',
    'dell_solution_pdf': 'Dell Solutions Configurator (PDF)',
    'dell_quote_pdf': 'Dell quote e-mail (PDF)',
    'lenovo_build_pdf': 'Lenovo build order (PDF)',
    'lenovo_list_pdf': 'Lenovo DCSC configuration, numbered list (PDF)',
    'supermicro_quote_pdf': 'Supermicro distributor quote (PDF)',
    'dell_arrow_pdf': 'Dell system quote from Arrow (PDF)',
}

XLSX_MAGIC = b'PK\x03\x04'
ACCEPTED_EXTENSIONS = ('.xlsx', '.xls', '.csv')


def _extension(filename: Optional[str], path: str) -> str:
    name = filename or path or ''
    return os.path.splitext(name)[1].lower()


def _is_xlsx(path: str) -> bool:
    try:
        with open(path, 'rb') as fh:
            return fh.read(4) == XLSX_MAGIC
    except OSError:
        return False


@contextmanager
def _as_xlsx(path: str):
    """Temporary .xlsx copy of an .xls (None when it is not a readable one);
    SheetTooLargeError propagates like it does for .xlsx."""
    from bom.parsers.common import is_xls, xls_to_xlsx
    converted = None
    if is_xls(path):
        try:
            converted = xls_to_xlsx(path)
        except ValueError as exc:
            from xlsx_utils import SheetTooLargeError
            if isinstance(exc, SheetTooLargeError):
                raise
            converted = None
    try:
        yield converted
    finally:
        if converted:
            try:
                os.unlink(converted)
            except OSError:
                pass


def _detect_xlsx(path: str) -> Optional[str]:
    from bom.parsers import (dell_lists, dell_quote, dell_service_tag,
                             dell_solution, item_list, lenovo_dcsc, template)
    from bom.parsers.common import load_workbook_safe
    from xlsx_utils import SheetTooLargeError
    try:
        wb = load_workbook_safe(path)
    except SheetTooLargeError:              # decompression bomb: refuse loudly
        raise
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
        # After VNET: the same module/option idea, but grouped under a product
        # row that carries the node count (and localised headers).
        solution = dell_solution.detect(wb)
        if solution:
            return solution
        # Before the hand-typed lists: an 'Item # | … | Vendor Part # |
        # Description | Qty' sheet with N / N.M item numbers.
        if item_list.detect(wb):
            return item_list.FORMAT
        return dell_lists.detect(wb)
    finally:
        wb.close()


def detect_format(path: str, filename: Optional[str] = None) -> Optional[str]:
    """Format id for a file, or None when nothing matches (PDF, docx, an
    unknown spreadsheet, a corrupt upload)."""
    ext = _extension(filename, path)
    if ext == '.xls':
        with _as_xlsx(path) as converted:
            return _detect_xlsx(converted) if converted else None
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
    from bom.parsers import (dell_lists, dell_quote, dell_service_tag,
                             dell_solution, item_list, lenovo_dcsc, template)
    if _extension(filename, path) == '.xls':
        # Legacy workbook: parse its .xlsx copy with the same ladder.
        with _as_xlsx(path) as converted:
            if converted is None:
                raise UnrecognizedFormat('The .xls file could not be read as an Excel workbook.')
            stem = os.path.splitext(os.path.basename(filename or path))[0]
            return parse_file(converted, stem + '.xlsx')
    fmt = detect_format(path, filename)
    if fmt is None:
        ext = _extension(filename, path)
        if ext not in ACCEPTED_EXTENSIONS:
            raise UnrecognizedFormat('Only .xlsx, .xls and .csv BOM exports are parsed directly.')
        raise UnrecognizedFormat()
    if fmt == template.FORMAT:
        bom = template.parse_template(path)
        # The template's Vendor column is authoritative when filled; left
        # blank, the parser reports 'Unknown' by contract and the content
        # markers (detect_vendor was written for exactly this case) decide, so
        # vendor-specific rules and HBA suggestions still apply.
        if bom.vendor == 'Unknown':
            bom.vendor = detect_vendor(bom)
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
    elif fmt == dell_solution.FORMAT_SOLUTION:
        bom = dell_solution.parse(path)
    elif fmt == item_list.FORMAT:
        bom = item_list.parse(path)
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


# ─── PDFs ──────────────────────────────────────────────────────────────────────
# A PDF is read in a sandboxed child process (bom/pdf_doc.py), offered to each
# PDF parser in turn, and the parse is scored (bom/pdf_certainty.py). The
# caller decides what a score means (route: trust it, or hand the file to the
# agent); nothing here trusts a parse on its own.

@dataclass
class PdfOutcome:
    doc: Optional['object'] = None          # bom.pdf_doc.PdfDoc, None when unreadable
    bom: Optional[NormalizedBOM] = None
    fmt: Optional[str] = None
    certainty: Optional['object'] = None    # bom.pdf_certainty.Certainty
    evidence: Optional['object'] = None
    error: Optional[str] = None

    def meta(self) -> dict:
        """What the check keeps about how the PDF was read."""
        out = {'format': self.fmt, 'error': self.error}
        if self.certainty is not None:
            out.update(self.certainty.to_dict())
        if self.evidence is not None:
            out['checks_passed'] = list(self.evidence.checks_passed)[:20]
            out['checks_failed'] = list(self.evidence.checks_failed)[:20]
            out['rows'] = self.evidence.rows
        if self.doc is not None:
            out['producer'] = (self.doc.meta.get('producer') or '')[:80]
            out['pages'] = self.doc.page_count
        return out


def _pdf_parsers():
    from bom.parsers import (pdf_dell_arrow, pdf_dell_quote, pdf_dell_solution,
                             pdf_lenovo_build, pdf_lenovo_list, pdf_scale_quote,
                             pdf_supermicro_quote)
    # The Scale quotation first: it is refused, never parsed as a BOM.
    return [pdf_scale_quote, pdf_lenovo_build, pdf_lenovo_list, pdf_dell_solution,
            pdf_dell_quote, pdf_dell_arrow, pdf_supermicro_quote]


def read_pdf(path: str) -> PdfOutcome:
    """Extract, detect, parse and score a PDF. Raises
    pdf_common.NotAVendorBom for a document that is recognised but is not a
    BOM to check; every other failure is reported in the outcome."""
    from bom import pdf_certainty, pdf_doc
    from bom.parsers.pdf_common import Evidence, NotAVendorBom
    try:
        doc = pdf_doc.read(path)
    except (pdf_doc.PdfExtractError, OSError) as exc:
        return PdfOutcome(error=str(exc)[:200])
    for mod in _pdf_parsers():
        try:
            hit = mod.detect(doc)
        except Exception:                      # a detector must never break the ladder
            hit = False
        if not hit:
            continue
        try:
            bom, ev = mod.parse(doc)
        except NotAVendorBom:
            raise
        except Exception as exc:               # parser bug on an odd file: agent's turn
            ev = Evidence()
            ev.unexplained.append('parser error: %s' % type(exc).__name__)
            return PdfOutcome(doc=doc, fmt=mod.FORMAT, evidence=ev,
                              certainty=pdf_certainty.assess(doc, ev, None))
        return PdfOutcome(doc=doc, bom=bom, fmt=mod.FORMAT, evidence=ev,
                          certainty=pdf_certainty.assess(doc, ev, bom))
    return PdfOutcome(doc=doc, certainty=pdf_certainty.safety(doc))
