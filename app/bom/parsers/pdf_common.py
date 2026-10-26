"""Shared pieces of the PDF parsers (parsers/pdf_*.py).

Every PDF parser returns its NormalizedBOM together with an ``Evidence``
record: what it saw that it could explain, what it could not, and which of
the document's own cross-checks (group totals, quantity multiples, header
counts) held. bom/pdf_certainty.py turns that plus the file's safety signals
into the certainty score that decides between "trust the parse" and "send it
to the agent". A parser never guesses to fill a gap: a line it cannot place
is recorded as unexplained, which costs certainty, instead of being dropped
silently or forced into a row.
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional, Pattern, Tuple

from bom.normalize import BOMConfig, NormalizedBOM
from bom.pdf_doc import Line, Page, PdfDoc

# rawText kept from a PDF for the human reviewing a flagged check (the
# spreadsheet parsers keep 60000: a sheet dump is denser than page text).
PDF_RAW_TEXT_LIMIT = 20000


@dataclass
class Evidence:
    rows: int = 0                                   # item rows read
    unexplained: List[str] = field(default_factory=list)
    pattern_misses: List[str] = field(default_factory=list)
    checks_passed: List[str] = field(default_factory=list)
    checks_failed: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    # (page, top, bottom) of the parts table, per page: hidden text inside it
    # is treated as an attack, not as noise.
    regions: List[Tuple[int, float, float]] = field(default_factory=list)

    def unexplained_line(self, line: Line):
        self.unexplained.append(line.text[:160])

    def check(self, ok: bool, label: str):
        (self.checks_passed if ok else self.checks_failed).append(label)

    def check_close(self, actual: float, expected: float, label: str,
                    rel: float = 0.01, floor: float = 1.0):
        """A price cross-check: ``actual`` within ``rel`` of ``expected``
        (never tighter than ``floor``, so cent rounding on a small total
        cannot fail it)."""
        self.check(abs(actual - expected) <= max(floor, rel * expected), label)

    def region(self, lines: List[Line]):
        by_page = {}
        for line in lines:
            lo, hi = by_page.get(line.page, (line.top, line.bottom))
            by_page[line.page] = (min(lo, line.top), max(hi, line.bottom))
        self.regions.extend((p, lo, hi) for p, (lo, hi) in sorted(by_page.items()))


def finish(doc: PdfDoc, ev: Evidence, vendor: str, configs: List[BOMConfig],
           item_lines: List[Line]) -> Tuple[NormalizedBOM, Evidence]:
    """The return tail of every PDF parser: the item lines become the table
    region the safety checks guard, the text is kept for the reviewer."""
    ev.region(item_lines)
    return NormalizedBOM(vendor=vendor, configs=configs, raw_text=doc.text()[:PDF_RAW_TEXT_LIMIT]), ev


class NotAVendorBom(ValueError):
    """A document we recognise, that is not a hardware BOM to check (a Scale
    Computing quotation for certified appliances). Refused without the agent."""


# ── text helpers ─────────────────────────────────────────────────────────────

_MONEY = re.compile(r"[-+]?[\d.,\s']*\d")


def money(text: str) -> Optional[float]:
    """'$5,263.00' / '7.400,00 €' / '1 234,50' → float. The decimal mark is
    the last '.' or ',' followed by exactly two digits at the end."""
    if not text:
        return None
    m = _MONEY.search(text.replace(" ", " "))
    if not m:
        return None
    raw = m.group(0).strip().replace(" ", "").replace("'", "")
    dec = re.search(r"[.,](\d{2})$", raw)
    if dec:
        whole = re.sub(r"[.,]", "", raw[:dec.start()])
        raw = "%s.%s" % (whole or "0", dec.group(1))
    else:
        raw = re.sub(r"[.,]", "", raw)
    try:
        return float(raw)
    except ValueError:
        return None


def whole_number(text: str) -> Optional[int]:
    text = (text or "").strip()
    return int(text) if re.fullmatch(r"\d{1,5}", text) else None


def first_word_x(line: Line, *labels: str) -> Optional[float]:
    """x0 of the first word equal (case-insensitively, trailing ':' ignored)
    to one of ``labels``."""
    wanted = {l.lower() for l in labels}
    for w in line.words:
        if w.text.lower().rstrip(":") in wanted:
            return w.x0
    return None


def doc_head(doc: PdfDoc, n: int = 40) -> str:
    """Lower-cased text of the first ``n`` lines, for format detection."""
    return "\n".join(l.text for l in doc.lines[:n]).lower()


def qty_in_column(line: Line, x: Optional[float], tol: float = 25) -> Optional[int]:
    """The whole number whose word starts within ``tol`` of column ``x``."""
    if x is None:
        return None
    return next((int(w.text) for w in line.words
                 if abs(w.x0 - x) < tol and whole_number(w.text) is not None), None)


def continues(prev: Optional[Line], line: Line, factor: float = 0.6, default_size: float = 9) -> bool:
    """``line`` is the wrapped continuation of ``prev``: same page and sitting
    tighter under it than ``factor`` of its own font size (a new row or a
    heading has the row gap above it)."""
    if prev is None or prev.page != line.page:
        return False
    size = max((w.size for w in line.words), default=default_size)
    return line.top - prev.bottom < factor * size


def ruled_bands(page: Page, start_y: float, min_height: float = 0,
                keep_loose: bool = False, min_rules: int = 0) -> List[List[Line]]:
    """The lines of ``page`` below ``start_y`` grouped into the bands between
    its horizontal rules (a cell that wraps is vertically centred, so the row
    is the band, never a text line). Bands lower than ``min_height`` are
    skipped. With fewer than ``min_rules`` rules the page is not ruled and
    every line is a band of its own; with ``keep_loose`` lines outside every
    band are also kept as single-line bands, in reading order."""
    lines = [l for l in page.lines if l.top > start_y]
    rules = [y for y in page.hrules if y >= start_y - 1]
    if len(rules) < min_rules:
        return sorted(([l] for l in lines), key=lambda band: (band[0].page, band[0].top))
    out = []
    used = set()
    for a, b in zip(rules, rules[1:]):
        if b - a < min_height:
            continue
        band = [l for l in lines if a < (l.top + l.bottom) / 2 < b]
        if band:
            out.append(band)
            used.update(id(l) for l in band)
    if keep_loose:
        out.extend([l] for l in lines if id(l) not in used)
        out.sort(key=lambda band: (band[0].page, band[0].top))
    return out


# ── the Arrow quote sheet ────────────────────────────────────────────────────
# Arrow prints its Excel quote to PDF with one header for every vendor:
# 'Product | Part / Project Description | Qty | Unit Price | Total Price',
# then the lines of the quote, then 'Integration services', 'Freight',
# 'Sub-Total' and the totals. The three parsers that read it (Dell, Lenovo,
# Supermicro) share the header, the column positions and the end-of-table
# bookkeeping here and differ only in how they read the body lines.

_SUBTOTAL = re.compile(r"sub-?total", re.I)
# The lines that close the Dell and Lenovo tables (lower-cased text).
ARROW_END = re.compile(r"^(?:integration services|freight_charge|sub-total)")
# A stray under the table: a line number and a description long enough to
# be an item (the Arrow template carries three stale ones).
ARROW_STRAY_NUMBERED = re.compile(r"^(?=.{13})\d{1,3}\s+\S")


def arrow_header(line: Line) -> bool:
    low = line.text.lower()
    return low.startswith("product") and "description" in low and "qty" in low and "total" in low


@dataclass
class ArrowCols:
    desc: float
    qty: Optional[float]
    unit: Optional[float]
    total: Optional[float]


def read_arrow_table(doc: PdfDoc, ev: Evidence, *, stray_re: Pattern,
                     end_re: Optional[Pattern] = None, end_on_label_column: bool = False
                     ) -> Tuple[Line, ArrowCols, List[Line], Optional[float]]:
    """``(header, cols, body_lines, subtotal)`` of the Arrow quote table.

    The table ends on the first line ``end_re`` matches (lower-cased text),
    or, with ``end_on_label_column``, on the first line that starts in the
    Product column (the Supermicro sheet lists its parts inline and never
    labels that column). The Sub-Total amount is picked up wherever it
    appears. Lines under the table that ``stray_re`` matches (their text left
    of the Qty column) look like items that were not read: they are recorded
    as notes, which costs a little certainty, instead of being ignored."""
    header = next(l for l in doc.lines if arrow_header(l))
    cols = ArrowCols(desc=first_word_x(header, "Part") or header.words[1].x0,
                     qty=first_word_x(header, "Qty"),
                     unit=first_word_x(header, "Unit"),
                     total=first_word_x(header, "Total"))
    body: List[Line] = []
    subtotal = None
    ended = False
    for line in doc.lines:
        if (line.page, line.top) <= (header.page, header.top):
            continue
        low = line.text.lower()
        m = _SUBTOTAL.search(line.text)
        if m:
            subtotal = money(line.text[m.end():])
        if not ended:
            ended = ((end_re is not None and end_re.search(low) is not None)
                     or (end_on_label_column and line.words[0].x0 < cols.desc - 10))
            if not ended:
                body.append(line)
                continue
            if end_re is None:
                continue                  # a label-column row closes the table, it is no stray
        left = line.between(0, cols.qty - 40).strip()
        if stray_re.match(left):
            ev.notes.append("stray: %s" % left[:80])
    return header, cols, body, subtotal


def check_grand_total(ev: Evidence, subtotal: Optional[float], grand: float, label: str):
    """The priced lines of the table add up to its Sub-Total (when both are
    there to compare)."""
    if subtotal is not None and grand:
        ev.check_close(grand, subtotal, label)
