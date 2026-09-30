"""A PDF, read safely, as the words a person would see.

bom/pdf_extract.py does the parsing in a locked-down child process; this
module runs it and turns its JSON into small objects the PDF parsers work
with: pages of *visible* words, text lines, horizontal rules. Everything a
human reader would not see (invisible render mode, white text that is not
sitting on a coloured box, sub-1.5 pt text, text off the page) is kept
aside as ``hidden`` — it is evidence against the file, never input for a
parser, the agent or grounding.
"""
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

EXTRACTOR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdf_extract.py")
TIMEOUT_SECONDS = 30
MAX_OUTPUT_BYTES = 40 * 1024 * 1024

HIDING_FLAGS = {"invisible", "tiny", "offpage"}
_GARBLE = re.compile(r"\(cid:\d+\)|�|[-]")


class PdfExtractError(Exception):
    """The PDF could not be read safely (crash, limit, timeout, not a PDF)."""


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float
    size: float
    page: int
    cx: Optional[List[float]] = None     # per-character centres (see pdf_extract)

    @property
    def mid_y(self):
        return (self.top + self.bottom) / 2

    def part(self, x0: float, x1: float) -> str:
        """The characters of this word whose centre lies in [x0, x1)."""
        if self.cx and len(self.cx) == len(self.text):
            return "".join(ch for ch, c in zip(self.text, self.cx) if x0 <= c < x1)
        return self.text if x0 <= (self.x0 + self.x1) / 2 < x1 else ""


@dataclass
class Line:
    words: List[Word]
    page: int

    @property
    def top(self):
        return min(w.top for w in self.words)

    @property
    def bottom(self):
        return max(w.bottom for w in self.words)

    @property
    def x0(self):
        return min(w.x0 for w in self.words)

    @property
    def text(self):
        return " ".join(w.text for w in self.words)

    def between(self, x0: float, x1: float) -> str:
        """The text in the column [x0, x1), cut at character level so two
        columns that touch still come apart."""
        return " ".join(p for p in (w.part(x0, x1) for w in self.words) if p)


@dataclass
class Page:
    index: int
    width: float
    height: float
    words: List[Word]
    hidden: List[Word]
    hrules: List[float]                  # y of horizontal rules, sorted
    lines: List[Line] = field(default_factory=list)


@dataclass
class PdfDoc:
    pages: List[Page]
    page_count: int
    meta: Dict[str, str]
    active: List[str]
    encrypted: bool
    truncated: bool
    incremental_updates: int

    @property
    def lines(self) -> List[Line]:
        return [line for page in self.pages for line in page.lines]

    @property
    def hidden_words(self) -> List[Word]:
        return [w for page in self.pages for w in page.hidden]

    def text(self) -> str:
        """Visible text, one PDF line per line, columns kept apart by a
        double space. What the agent gets instead of the PDF itself when the
        extraction is healthy, and what grounding compares against."""
        out = []
        for page in self.pages:
            for line in page.lines:
                parts, prev = [], None
                for w in line.words:
                    if prev is not None:
                        parts.append("  " if w.x0 - prev.x1 > 2 * max(w.size, 4) else " ")
                    parts.append(w.text)
                    prev = w
                out.append("".join(parts))
            out.append("")
        return "\n".join(out).strip()

    def garbled_ratio(self) -> float:
        """Share of visible characters that did not map to real text
        (missing font Unicode tables, private-use glyphs)."""
        text = "".join(w.text for p in self.pages for w in p.words)
        if not text:
            return 1.0
        bad = sum(len(m.group(0)) for m in _GARBLE.finditer(text))
        return bad / len(text)


MIN_CONTRAST = 0.2          # luminance difference below which text does not read


def _luminance(rgb):
    r, g, b = rgb
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _unreadable(w, fills) -> Optional[str]:
    """Why a person cannot read this word, or None.

    'covered': a filled box painted after the text lies over it.
    'low_contrast': the text colour is (nearly) the colour behind it — the
    last box painted under it, or the white page. Black on black, white on
    white, grey on grey: all the same trick."""
    x0, top, x1, bottom = w["x0"], w["top"], w["x1"], w["bottom"]
    cx, cy = (x0 + x1) / 2, (top + bottom) / 2
    behind = None
    for f in fills:
        if len(f) < 7:
            continue
        fx0, ftop, fx1, fbottom, _white, rgb, z = f[:7]
        if not (fx0 - 0.5 <= cx <= fx1 + 0.5 and ftop - 0.5 <= cy <= fbottom + 0.5):
            continue
        if z > w.get("z", 0):
            # Painted over the word: hides it unless the box is tiny.
            if (fx1 - fx0) * (fbottom - ftop) >= 0.5 * (x1 - x0) * max(bottom - top, 1):
                return "covered"
            continue
        if behind is None or z > behind[1]:
            behind = (rgb, z)
    text = w.get("color")
    if text is None:
        return None
    background = behind[0] if behind and behind[0] is not None else [1.0, 1.0, 1.0]
    if abs(_luminance(text) - _luminance(background)) < MIN_CONTRAST:
        return "low_contrast"
    return None


def _lines(words: List[Word], page: int) -> List[Line]:
    """Group words into lines: overlapping vertically by more than half the
    smaller height."""
    lines: List[Line] = []
    for w in sorted(words, key=lambda w: (w.mid_y, w.x0)):
        placed = False
        for line in reversed(lines[-3:]):
            top, bottom = line.top, line.bottom
            overlap = min(bottom, w.bottom) - max(top, w.top)
            if overlap > 0.5 * min(bottom - top, w.bottom - w.top):
                line.words.append(w)
                placed = True
                break
        if not placed:
            lines.append(Line(words=[w], page=page))
    for line in lines:
        line.words.sort(key=lambda w: w.x0)
    lines.sort(key=lambda l: l.top)
    return lines


def from_json(data: dict, raw: bytes = b"") -> PdfDoc:
    pages = []
    for index, p in enumerate(data.get("pages") or []):
        visible, hidden = [], []
        for w in p.get("words") or []:
            word = Word(text=w["t"], x0=w["x0"], x1=w["x1"], top=w["top"], bottom=w["bottom"],
                        size=w["size"], page=index, cx=w.get("cx"))
            flags = set(w.get("flags") or [])
            if not word.text.strip():
                continue
            if flags & HIDING_FLAGS or _unreadable(w, p.get("fills") or []):
                hidden.append(word)
            else:
                visible.append(word)
        hrules = sorted(r[3] for r in (p.get("rules") or []) if r[0] == "h")
        page = Page(index=index, width=p["width"], height=p["height"], words=visible,
                    hidden=hidden, hrules=hrules)
        page.lines = _lines(visible, index)
        pages.append(page)
    return PdfDoc(pages=pages, page_count=int(data.get("page_count") or len(pages)),
                  meta=dict(data.get("meta") or {}), active=list(data.get("active") or []),
                  encrypted=bool(data.get("encrypted")), truncated=bool(data.get("truncated")),
                  incremental_updates=incremental_updates(raw))


def incremental_updates(raw: bytes) -> int:
    """How many times the file was changed after it was first written.

    Each save of an edit appends new objects and a new trailer ending in
    %%EOF. Not every second %%EOF is an edit, though: Microsoft Office's
    hybrid-reference files end with a small cross-reference-stream trailer
    (no objects), and a linearized ("fast web view") file has an early
    %%EOF after its first-page section by design. So only segments that add
    objects count, minus the linearization split."""
    marks, i = [], raw.find(b"%%EOF")
    while i != -1:
        marks.append(i)
        i = raw.find(b"%%EOF", i + 5)
    edits = 0
    for start, end in zip(marks, marks[1:]):
        if b" obj" in raw[start:end]:
            edits += 1
    if edits and b"/Linearized" in raw[:2048]:
        edits -= 1
    return edits


def read(path: str) -> PdfDoc:
    """Extract in the sandboxed child process. Raises PdfExtractError."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if not raw.startswith(b"%PDF"):
        raise PdfExtractError("not a PDF")
    try:
        proc = subprocess.run(
            [sys.executable, "-I", EXTRACTOR, os.path.abspath(path)],
            capture_output=True, timeout=TIMEOUT_SECONDS, env={"PATH": os.environ.get("PATH", "")},
            stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise PdfExtractError("the PDF took too long to read")
    if len(proc.stdout) > MAX_OUTPUT_BYTES:
        raise PdfExtractError("the PDF produced too much text")
    try:
        data = json.loads(proc.stdout or b"{}")
    except ValueError:
        data = {}
    if not data.get("ok"):
        raise PdfExtractError(data.get("error") or ("the PDF reader stopped (exit %s)" % proc.returncode))
    return from_json(data, raw)


def try_read(path: str) -> Optional[PdfDoc]:
    try:
        return read(path)
    except (PdfExtractError, OSError):
        return None
