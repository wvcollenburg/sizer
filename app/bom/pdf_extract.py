"""PDF → positioned words, rules and safety signals, in a separate process.

Why a separate process: an uploaded PDF is hostile until proven otherwise, and
a PDF parser is a big attack surface (compressed object streams, fonts,
recursion). This file is run by bom/pdf_doc.py as

    python -I bom/pdf_extract.py <file>

under hard resource limits (address space, CPU seconds, open files, no file
writes) and a wall-clock timeout. A crash, a memory bomb or an endless loop
kills only this child; the web worker gets "extraction failed" and the file
goes to the agent or is refused. It imports nothing from the app on purpose,
so ``-I`` (isolated mode) can drop the environment and the working directory
from sys.path.

Output (stdout, JSON): pages with words (text, box, font size, and the flags
that make text invisible to a human reader), horizontal/vertical rules and
filled boxes (row separators, header shading), document metadata, and an
inventory of active content (JavaScript, launch actions, embedded files ...)
found by walking every object — keywords inside compressed object streams
are invisible to a byte scan, not to this.
"""
import json
import sys

MAX_PAGES = 40
MAX_WORDS = 60000
MAX_OBJECTS = 50000

# PDF keys/actions that have no business in a hardware quote.
ACTIVE_KEYS = {"JavaScript", "JS", "Launch", "EmbeddedFile", "EmbeddedFiles",
               "RichMedia", "XFA", "SubmitForm", "ImportData", "GoToE", "GoToR"}
ACTIVE_ACTIONS = {"JavaScript", "Launch", "SubmitForm", "ImportData", "GoToE",
                  "GoToR", "RichMediaExecute", "Rendition", "Sound", "Movie"}

INVISIBLE_RENDER_MODES = {3, 7}     # neither fill nor stroke (7 = clip only)
TINY_FONT = 1.5                      # points


def _limits():
    import resource
    mb = 1024 * 1024
    for res, value in ((resource.RLIMIT_AS, 1024 * mb), (resource.RLIMIT_CPU, 25),
                       (resource.RLIMIT_FSIZE, 0), (resource.RLIMIT_NOFILE, 32)):
        try:
            resource.setrlimit(res, (value, value))
        except (ValueError, OSError):
            pass


def _text(value):
    """Metadata strings: bytes with a UTF-16 BOM, PDFDocEncoding, or str."""
    if isinstance(value, bytes):
        if value.startswith(b"\xfe\xff"):
            return value[2:].decode("utf-16-be", "replace")
        if value.startswith(b"\xff\xfe"):
            return value[2:].decode("utf-16-le", "replace")
        return value.decode("latin-1", "replace")
    return str(value) if value is not None else ""


def _is_white(color):
    """Fill colour that reads as white (gray 1, rgb ~1,1,1, cmyk ~0,0,0,0)."""
    if color is None:
        return False
    if isinstance(color, (int, float)):
        return color >= 0.95
    try:
        values = [float(v) for v in color]
    except (TypeError, ValueError):
        return False
    if len(values) == 1:
        return values[0] >= 0.95
    if len(values) == 3:
        return all(v >= 0.95 for v in values)
    if len(values) == 4:
        return all(v <= 0.05 for v in values)
    return False


def _active_content(doc):
    """Names of active features found anywhere in the object graph."""
    from pdfminer.psparser import PSLiteral
    from pdfminer.pdftypes import PDFStream
    found = set()
    seen = 0
    for xref in doc.xrefs:
        for objid in xref.get_objids():
            seen += 1
            if seen > MAX_OBJECTS:
                found.add("TooManyObjects")
                return sorted(found)
            try:
                obj = doc.getobj(objid)
            except Exception:
                continue
            if isinstance(obj, PDFStream):
                obj = obj.attrs
            if not isinstance(obj, dict):
                continue
            for key, value in obj.items():
                if key in ACTIVE_KEYS:
                    found.add(key)
                if key in ("OpenAction", "AA"):
                    found.add(key)
                if key == "S" and isinstance(value, PSLiteral) and value.name in ACTIVE_ACTIONS:
                    found.add(value.name)
    return sorted(found)


def extract(path):
    from pdfminer.converter import PDFPageAggregator
    from pdfminer.layout import LAParams, LTChar, LTCurve, LTFigure, LTLine, LTRect
    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfinterp import PDFPageInterpreter, PDFResourceManager
    from pdfminer.pdfpage import PDFPage
    from pdfminer.pdfparser import PDFParser
    from pdfminer.pdftypes import resolve1

    class Aggregator(PDFPageAggregator):
        """Tags every character with the text render mode in force."""
        _render = 0

        def render_string(self, textstate, seq, ncs, graphicstate):
            self._render = textstate.render
            return super().render_string(textstate, seq, ncs, graphicstate)

        def render_char(self, *args, **kwargs):
            adv = super().render_char(*args, **kwargs)
            self.cur_item._objs[-1].render_mode = self._render
            return adv

    out = {"pages": [], "meta": {}, "active": [], "encrypted": False,
           "truncated": False, "page_count": 0}
    with open(path, "rb") as fh:
        parser = PDFParser(fh)
        doc = PDFDocument(parser)
        out["encrypted"] = bool(getattr(doc, "encryption", None))
        for info in doc.info or []:
            for key in ("Producer", "Creator", "Title", "Author"):
                if key in info:
                    out["meta"][key.lower()] = _text(resolve1(info[key]))[:200]
        out["active"] = _active_content(doc)

        rsrc = PDFResourceManager()
        device = Aggregator(rsrc, laparams=LAParams())
        interp = PDFPageInterpreter(rsrc, device)
        words_total = 0
        for index, page in enumerate(PDFPage.create_pages(doc)):
            out["page_count"] = index + 1
            if index >= MAX_PAGES:
                out["truncated"] = True
                continue
            interp.process_page(page)
            layout = device.get_result()
            x0, y0, x1, y1 = page.mediabox
            width, height = x1 - x0, y1 - y0
            chars, rules, fills = [], [], []

            def walk(item):
                for obj in item:
                    if isinstance(obj, LTChar):
                        chars.append(obj)
                    elif isinstance(obj, (LTLine, LTRect, LTCurve)):
                        w, h = obj.x1 - obj.x0, obj.y1 - obj.y0
                        top, bottom = height - obj.y1, height - obj.y0
                        if h <= 2.0 and w > 4:
                            rules.append(["h", round(obj.x0, 1), round(obj.x1, 1), round((top + bottom) / 2, 1)])
                        elif w <= 2.0 and h > 4:
                            rules.append(["v", round((obj.x0 + obj.x1) / 2, 1), round(top, 1), round(bottom, 1)])
                        elif isinstance(obj, LTRect) and getattr(obj, "fill", False):
                            fills.append([round(obj.x0, 1), round(top, 1), round(obj.x1, 1), round(bottom, 1),
                                          _is_white(getattr(obj, "non_stroking_color", None))])
                    elif isinstance(obj, LTFigure) or hasattr(obj, "_objs"):
                        walk(obj)

            walk(layout)
            words = _words(chars, width, height)
            words_total += len(words)
            if words_total > MAX_WORDS:
                out["truncated"] = True
                break
            out["pages"].append({"width": round(width, 1), "height": round(height, 1),
                                 "words": words, "rules": rules, "fills": fills})
    return out


def _flags(ch, width, height):
    flags = []
    if getattr(ch, "render_mode", 0) in INVISIBLE_RENDER_MODES:
        flags.append("invisible")
    if ch.size < TINY_FONT:
        flags.append("tiny")
    if ch.x1 < 0 or ch.x0 > width or ch.y1 < 0 or ch.y0 > height:
        flags.append("offpage")
    if not ch.upright:
        flags.append("rotated")
    color = getattr(ch.graphicstate, "ncolor", None)
    if _is_white(color):
        flags.append("white")
    return flags


def _words(chars, width, height):
    """Characters → words: same line (baseline within half a font size),
    split on spaces and on gaps wider than a quarter of the font size."""
    rows = []
    for ch in sorted(chars, key=lambda c: (-round(c.y0, 1), c.x0)):
        if rows and abs(rows[-1][0] - ch.y0) <= max(ch.size, 1) * 0.5:
            rows[-1][1].append(ch)
        else:
            rows.append([ch.y0, [ch]])
    words = []
    for _, row in rows:
        row.sort(key=lambda c: c.x0)
        cur = []
        for ch in row:
            text = ch.get_text()
            gap = (ch.x0 - cur[-1].x1) if cur else 0
            if text.isspace() or (cur and gap > max(ch.size, 1) * 0.25):
                if cur:
                    words.append(_word(cur, width, height))
                cur = [] if text.isspace() else [ch]
            else:
                cur.append(ch)
        if cur:
            words.append(_word(cur, width, height))
    return words


def _word(chars, width, height):
    flags = sorted({f for ch in chars for f in _flags(ch, width, height)})
    sizes = sorted(ch.size for ch in chars)
    return {
        "t": "".join(ch.get_text() for ch in chars),
        "x0": round(min(c.x0 for c in chars), 1),
        "x1": round(max(c.x1 for c in chars), 1),
        "top": round(height - max(c.y1 for c in chars), 1),
        "bottom": round(height - min(c.y0 for c in chars), 1),
        "size": round(sizes[len(sizes) // 2], 1),
        "flags": flags,
        # Per-character centres: two table columns that almost touch can
        # merge into one "word" ('…1 or 2' + qty '1' -> '21'); the parent
        # cuts words at column edges with these.
        "cx": [round((c.x0 + c.x1) / 2, 1) for c in chars],
    }


def main(argv):
    _limits()
    try:
        result = extract(argv[1])
        result["ok"] = True
    except Exception as exc:      # reported, never raised: the parent decides
        result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
    sys.stdout.write(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
