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
from typing import List, Optional, Tuple

from bom.pdf_doc import Line, PdfDoc


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

    def region(self, lines: List[Line]):
        by_page = {}
        for line in lines:
            lo, hi = by_page.get(line.page, (line.top, line.bottom))
            by_page[line.page] = (min(lo, line.top), max(hi, line.bottom))
        self.regions.extend((p, lo, hi) for p, (lo, hi) in sorted(by_page.items()))


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


def has_words(line: Line, *labels: str) -> bool:
    text = " %s " % line.text.lower()
    return all((" %s" % l.lower()) in text for l in labels)


def doc_head(doc: PdfDoc, n: int = 40) -> str:
    """Lower-cased text of the first ``n`` lines, for format detection."""
    return "\n".join(l.text for l in doc.lines[:n]).lower()


# Dell SKU: three digits and four characters (405-AAXY), or five digits for
# bundles (486-85584). Wrapped SKU cells run together ('405-AAXY750-ACFR'),
# so the four-character form must not swallow the next SKU's first digit.
DELL_SKU = re.compile(r"\d{3}-(?:\d{5}(?![0-9])|[A-Z0-9]{4})")
LENOVO_FEATURE = re.compile(r"^[A-Z0-9]{4}$")
LENOVO_PART = re.compile(r"^[0-9A-Z]{2,4}[A-Z0-9]{3,8}$")
