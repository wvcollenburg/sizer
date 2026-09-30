#!/usr/bin/env python3
"""The online manual as a Word document and PDF, on the export Word template.

    .venv/bin/python tools/manual_docx.py [--edition user|scale|both] [--out DIR]

For the documentation portal. The source is the manual's own HTML, rendered by
the real /manual/ route, so the document says exactly what the web manual
says; this only re-lays it out on resources/TMPL - Generic Document
Template_2025.docx with the export helpers (export_docx.py): a cover, a
contents page with page numbers, one chapter per page, the screenshots from
app/manual/img/, tables and call-outs in the export style.

PDF needs LibreOffice (soffice) on PATH — on the Fedora PC that is the host,
so run it there: `flatpak-spawn --host .venv/bin/python tools/manual_docx.py`.
The brand fonts are handed to LibreOffice from resources/fonts for the run,
nothing is installed. Page numbers in the contents are read back from a
first conversion, so they are right for the PDF; in Word they are close but
follow Word's own layout.

The staff edition is internal: its cover says so, and the file name carries
"staff". Never put it where partners can read it.
"""
import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
APP = os.path.join(ROOT, "app")
IMG = os.path.join(APP, "manual", "img")
FONTS = os.path.join(ROOT, "resources", "fonts")

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "manual-docx")
os.environ.pop("SESSION_COOKIE_SECURE", None)
sys.path.insert(0, APP)

from docx import Document  # noqa: E402
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402
from docx.shared import Inches, Pt, RGBColor  # noqa: E402
from lxml import html as lxml_html  # noqa: E402

import export_docx as ex  # noqa: E402
import palette as _p  # noqa: E402

EDITIONS = {
    "user": {"path": "/manual/", "email": "manual@partner.example",
             "subtitle": "User manual", "file": "SC-Infrastructure-Sizer-User-Manual"},
    "scale": {"path": "/manual/scale", "email": "manual@scalecomputing.com",
              "subtitle": "Staff manual", "file": "SC-Infrastructure-Sizer-Staff-Manual-INTERNAL"},
}
WHITE = RGBColor(*_p.rgb(_p.WHITE))
CALLOUT_FILL = {"manual-tip": _p.SURFACE_3, "manual-warn": _p.SURFACE_3, "manual-staff": _p.SURFACE_3}
CALLOUT_BAR = {"manual-tip": "166534", "manual-warn": "B7791F", "manual-staff": _p.SC_DARK_NAVY}
TOC_MARK = "{{p}}"


# ── the source ─────────────────────────────────────────────────────────────

def render_html(edition):
    """The manual page as a signed-in reader of that edition sees it."""
    import app as appmod
    from database import db
    from extensions import limiter
    app = appmod.app
    app.config["TESTING"] = True
    limiter.enabled = False
    with app.app_context():
        db.create_all()
    c = app.test_client()
    ed = EDITIONS[edition]
    pw = "Manual-Docx-1!"
    c.post("/api/auth/signup", json={"email": ed["email"], "password": pw,
                                     "accept_privacy": True})
    c.post("/api/auth/login", json={"email": ed["email"], "password": pw})
    resp = c.get(ed["path"])
    assert resp.status_code == 200, (edition, resp.status_code)
    return resp.get_data(as_text=True)


def app_version():
    with open(os.path.join(APP, "templates", "index.html"), encoding="utf-8") as fh:
        m = re.search(r'class="version">v([\d.]+)<', fh.read())
    return m.group(1) if m else ""


# ── inline text ────────────────────────────────────────────────────────────

def _classes(el):
    return set((el.get("class") or "").split())


def add_inline(par, el, bold=False, italic=False, mono=False, skip_tags=()):
    """Append el's text and children to par as runs, keeping bold / italic /
    code and the manual's on-screen-label and Staff markers."""
    def run(text, b=bold, i=italic, m=mono, color=None):
        if not text:
            return
        text = re.sub(r"\s+", " ", text)
        r = par.add_run(text)
        r.bold = b or None
        r.italic = i or None
        if m:
            r.font.name = "Consolas"
        if color is not None:
            r.font.color.rgb = color

    run(el.text)
    for ch in el:
        tag = ch.tag if isinstance(ch.tag, str) else ""
        cls = _classes(ch)
        if tag in skip_tags:
            pass
        elif tag in ("strong", "b", "dt"):
            add_inline(par, ch, True, italic, mono)
        elif tag in ("em", "i"):
            add_inline(par, ch, bold, True, mono)
        elif tag == "code":
            add_inline(par, ch, bold, italic, True)
        elif tag == "span" and "manual-ui" in cls:
            add_inline(par, ch, True, italic, mono)
        elif tag == "span" and "manual-staff-tag" in cls:
            run(" STAFF ", True, False, False, ex.DK2)
        elif tag == "br":
            par.add_run().add_break()
        elif tag in ("ul", "ol", "table", "figure", "div", "dl"):
            pass  # blocks inside inline content are handled by the caller
        else:
            add_inline(par, ch, bold, italic, mono)
        run(ch.tail, bold, italic, mono)


def _tidy(par):
    """Trim the whitespace the HTML left at the ends of a paragraph."""
    runs = [r for r in par.runs if r.text]
    if runs:
        runs[0].text = runs[0].text.lstrip()
        runs[-1].text = runs[-1].text.rstrip()
    return par


# ── blocks ─────────────────────────────────────────────────────────────────

def _stops_float(el):
    """What the web page clears below a floated picture (manual.css)."""
    cls = _classes(el)
    return (el.tag in ("h2", "h3", "h4", "figure", "dl")
            or (el.tag == "div" and "manual-callout" in cls))


def _drop_leading_empty(cell):
    """A new cell starts with an empty paragraph; drop it once filled."""
    first = cell.paragraphs[0] if cell.paragraphs else None
    if first is not None and not first.text and len(cell._tc) > 2 \
            and not first._p.xpath(".//w:drawing"):
        first._p.getparent().remove(first._p)


class Writer:
    def __init__(self, doc, edition):
        self.doc = doc
        self.edition = edition
        self.cw = ex._content_width(doc)
        # where blocks go and how wide they may be: the page, or the text
        # column beside a dialog-sized picture (see side_by_side)
        self.out = doc
        self.width = self.cw
        self.chapters = []          # (number-and-title, toc paragraph)

    def para(self, el, style=None, **kw):
        p = self.out.add_paragraph(style=style)
        add_inline(p, el, **kw)
        return _tidy(p)

    def heading(self, el, level):
        h = self.doc.add_heading(level=level)
        add_inline(h, el, skip_tags=("span",))
        return _tidy(h)

    def figure(self, fig):
        img = fig.find(".//img")
        if img is None:
            return
        src = img.get("src") or ""
        m = re.match(r"/manual/img/(user|scale)/(.+)$", src)
        if not m:
            return
        path = os.path.join(IMG, m.group(1), m.group(2))
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        from PIL import Image
        with Image.open(path) as im:
            w_px, h_px = im.size
        caption = fig.find("figcaption")
        caption = re.sub(r"\s+", " ", caption.text_content()).strip() if caption is not None else ""
        # a screenshot at ~150 dpi, never wider than the text or taller than
        # most of a page
        width = min(self.width, w_px / 150.0)
        if width * h_px / w_px > 7.2:
            width = 7.2 * w_px / h_px
        p = self.out.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.keep_with_next = True
        p.paragraph_format.space_before = Pt(6)
        p.add_run().add_picture(path, width=Inches(width))
        if caption:
            self._caption(self.out.add_paragraph(), caption)

    @staticmethod
    def _caption(par, text):
        par.alignment = WD_ALIGN_PARAGRAPH.CENTER
        r = par.add_run(text)
        r.italic = True
        r.font.size = Pt(8.5)
        r.font.color.rgb = ex.MUTED
        par.paragraph_format.space_after = Pt(10)

    def side_by_side(self, fig, followers):
        """A dialog-sized screenshot beside the text that follows it, like the
        web page's float: a borderless two-column table, text on the left,
        picture and caption on the right. Not a Word float: LibreOffice will
        not set a table beside a floating one, and a float that misses the
        page leaves its heading stranded; a plain table lands the same way in
        Word and LibreOffice every time."""
        img = fig.find(".//img")
        m = re.match(r"/manual/img/(user|scale)/(.+)$", img.get("src") or "")
        path = os.path.join(IMG, m.group(1), m.group(2))
        from PIL import Image
        with Image.open(path) as im:
            w_px, h_px = im.size
        pic_w = min(0.42 * self.cw, w_px / 150.0)
        if pic_w * h_px / w_px > 7.0:
            pic_w = 7.0 * w_px / h_px
        gap = 0.25
        text_w = self.cw - pic_w - gap
        t = self.out.add_table(1, 2)
        ex._cell_margins(t, 0, 0, 0, 0)
        ex._fixed_layout(t, [text_w + gap, pic_w])
        left, right = t.rows[0].cells
        # the text column: the same block writers, aimed at the cell
        outer = (self.out, self.width)
        self.out, self.width = left, text_w
        try:
            for el in followers:
                self.block(el)
        finally:
            self.out, self.width = outer
        _drop_leading_empty(left)
        for par in left.paragraphs:
            par.paragraph_format.right_indent = Inches(gap)
        p = right.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.add_run().add_picture(path, width=Inches(pic_w))
        cap = fig.find("figcaption")
        if cap is not None:
            self._caption(right.add_paragraph(),
                          re.sub(r"\s+", " ", cap.text_content()).strip())
        ex._pin_heading_to_table(t)
        self._space(4)

    def blocks(self, children):
        """Write a run of blocks. A dialog-sized picture takes the blocks after
        it, up to the next heading, picture or call-out, into its text column
        (as the web page floats it); with nothing to sit beside it stands
        alone."""
        children = [c for c in children if isinstance(c.tag, str)]
        i = 0
        while i < len(children):
            el = children[i]
            if el.tag == "figure" and "manual-shot-narrow" in _classes(el):
                j = i + 1
                while j < len(children) and not _stops_float(children[j]):
                    j += 1
                if j > i + 1:
                    self.side_by_side(el, children[i + 1:j])
                    i = j
                    continue
            self.block(el)
            i += 1

    def _space(self, pts):
        p = self.out.add_paragraph()
        p.paragraph_format.space_after = Pt(pts)
        p.paragraph_format.space_before = Pt(0)

    def table(self, tbl):
        rows = tbl.findall(".//tr")
        if not rows:
            return
        ncols = max(len(r.findall("./th") + r.findall("./td")) for r in rows)
        t = self.out.add_table(0, ncols)
        ex._set_table_borders(t)
        ex._cell_margins(t)
        for r in rows:
            cells_el = [c for c in r if c.tag in ("th", "td")]
            cells = t.add_row().cells
            for i, c_el in enumerate(cells_el[:ncols]):
                cell = cells[i]
                cell.text = ""
                p = cell.paragraphs[0]
                if c_el.tag == "th":
                    add_inline(p, c_el, bold=True)
                    for run in p.runs:
                        run.font.color.rgb = WHITE
                    ex._shade(cell, _p.SC_DARK_NAVY)
                else:
                    add_inline(p, c_el)
                    if i == 0 and ncols > 1:
                        ex._shade(cell, _p.SURFACE_3)
                _tidy(p)
                for run in p.runs:
                    run.font.size = Pt(9)
        # the first column holds labels: narrower than the explanations
        weights = {2: [1, 2.6], 3: [1, 0.8, 2.6]}.get(ncols, [1] * ncols)
        scale = self.width / sum(weights)
        ex._fixed_layout(t, [w * scale for w in weights])
        # header row repeats on every page a long table runs over
        trPr = t.rows[0]._tr.get_or_add_trPr()
        trPr.append(OxmlElement("w:tblHeader"))
        for row in t.rows:
            row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
        ex._pin_heading_to_table(t)
        self._space(4)

    def callout(self, div):
        kind = next((k for k in CALLOUT_FILL if k in _classes(div)), "manual-tip")
        t = self.out.add_table(1, 1)
        ex._cell_margins(t, 90, 90, 160, 160)
        borders = ex._set_tblpr_child(t._tbl.tblPr, "w:tblBorders")
        left = OxmlElement("w:left")
        left.set(qn("w:val"), "single")
        left.set(qn("w:sz"), "24")
        left.set(qn("w:color"), CALLOUT_BAR[kind])
        borders.append(left)
        cell = t.rows[0].cells[0]
        ex._shade(cell, CALLOUT_FILL[kind])
        p = cell.paragraphs[0]
        add_inline(p, div)
        _tidy(p)
        for run in p.runs:
            run.font.size = Pt(9.5)
        ex._fixed_layout(t, [self.width])
        t.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
        self._space(4)

    def list(self, lst, depth=0):
        ordered = lst.tag == "ol"
        for n, li in enumerate(lst.findall("./li"), 1):
            p = self.out.add_paragraph()
            pf = p.paragraph_format
            indent = 0.25 + 0.3 * depth
            pf.left_indent = Inches(indent + 0.22)
            pf.first_line_indent = Inches(-0.22)
            pf.space_after = Pt(3)
            p.add_run(("%d.\t" % n) if ordered else "•\t")
            tabs = pf.tab_stops
            tabs.add_tab_stop(Inches(indent + 0.22))
            add_inline(p, li)
            # trim the stray space after the marker the HTML left
            if len(p.runs) > 1:
                p.runs[1].text = p.runs[1].text.lstrip()
            if p.runs:
                p.runs[-1].text = p.runs[-1].text.rstrip()
            for sub in li:
                if sub.tag in ("ul", "ol"):
                    self.list(sub, depth + 1)

    def faq(self, dl):
        for el in dl:
            if el.tag == "dt":
                p = self.para(el, bold=True)
                p.paragraph_format.keep_with_next = True
                p.paragraph_format.space_before = Pt(8)
                p.paragraph_format.space_after = Pt(2)
            elif el.tag == "dd":
                self.para(el)

    def block(self, el):
        tag = el.tag if isinstance(el.tag, str) else ""
        cls = _classes(el)
        if tag == "h3":
            self.heading(el, 2)
        elif tag == "h4":
            self.heading(el, 3)
        elif tag == "p":
            if el.text_content().strip():
                self.para(el)
        elif tag in ("ul", "ol"):
            self.list(el)
        elif tag == "table":
            self.table(el)
        elif tag == "figure":
            self.figure(el)
        elif tag == "div" and "manual-callout" in cls:
            self.callout(el)
        elif tag == "dl":
            self.faq(el)
        elif tag in ("div", "section"):
            self.blocks(list(el))

    def chapter(self, sec):
        h2 = sec.find("h2")
        title = re.sub(r"\s+", " ", h2.text or "").strip()   # without the Staff tag
        self.page_break()
        h = self.doc.add_heading(title, level=1)
        self.chapters.append(title)
        self.blocks([ch for ch in sec if ch is not h2])

    def page_break(self):
        p = self.doc.add_paragraph()
        p.add_run().add_break(WD_BREAK.PAGE)
        p.paragraph_format.space_after = Pt(0)


# ── the document ───────────────────────────────────────────────────────────

def build(edition, pages=None):
    """The .docx bytes. `pages` maps chapter title -> page number; without it
    the contents carry placeholders of the same width (first pass)."""
    page = lxml_html.fromstring(render_html(edition))
    body = page.find_class("manual-body")[0]
    ed = EDITIONS[edition]

    doc = Document(ex._TEMPLATE)
    ex._clear_body(doc)
    ex._doc_setup(doc, "en", "SC// Infrastructure Sizer — " + ed["subtitle"]
                  + (" (internal)" if edition == "scale" else ""))
    w = Writer(doc, edition)

    # cover
    ex._spacer(doc, 60)
    doc.add_paragraph("SC// Infrastructure Sizer", style="Title")
    doc.add_paragraph(ed["subtitle"], style="Subtitle")
    meta = doc.add_paragraph()
    r = meta.add_run("Version %s · %s" % (app_version(), date.today().strftime("%-d %B %Y")))
    r.font.color.rgb = ex.MUTED
    if edition == "scale":
        warn = doc.add_paragraph()
        r = warn.add_run("Internal — for Scale Computing staff only. Do not share with "
                         "partners or customers; send them the user manual instead.")
        r.bold = True
        r.font.color.rgb = ex.DK2
    ex._spacer(doc, 24)

    # the intro under the title (the page's own <h1> is the cover now)
    intro = body.find_class("manual-intro")[0]
    w.blocks([ch for ch in intro if ch.tag != "h1"])

    # contents: filled in on the second pass
    chapters = body.find_class("manual-chapter")
    w.page_break()
    doc.add_heading("Contents", level=1)
    toc_right = Inches(w.cw)
    for sec in chapters:
        title = re.sub(r"\s+", " ", sec.find("h2").text or "").strip()
        p = doc.add_paragraph()
        pf = p.paragraph_format
        pf.space_after = Pt(4)
        pf.tab_stops.add_tab_stop(toc_right, alignment=2, leader=1)  # right, dots
        p.add_run(title + "\t")
        num = (pages or {}).get(title)
        p.add_run(str(num) if num else "00")

    for sec in chapters:
        w.chapter(sec)

    ex._finalize_doc(doc)
    # _finalize_doc zeroes every indent; the lists need theirs back
    _restore_list_indents(doc)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue(), w.chapters


def _restore_list_indents(doc):
    for p in doc.paragraphs:
        if p.runs and re.match(r"^(•|\d+\.)\t$", p.runs[0].text or ""):
            tabs = p.paragraph_format.tab_stops
            stop = tabs[0].position if len(tabs) else Inches(0.47)
            p.paragraph_format.left_indent = stop
            p.paragraph_format.first_line_indent = Inches(-0.22)


# ── PDF ────────────────────────────────────────────────────────────────────

def to_pdf(docx_bytes):
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise SystemExit("LibreOffice (soffice) not found — run this where it is installed")
    with tempfile.TemporaryDirectory() as d:
        # a private HOME whose ~/.fonts holds the brand fonts, for this run only
        shutil.copytree(FONTS, os.path.join(d, ".fonts"))
        src = os.path.join(d, "manual.docx")
        with open(src, "wb") as fh:
            fh.write(docx_bytes)
        subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", d, src],
                       check=True, timeout=300, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, env={**os.environ, "HOME": d})
        with open(os.path.join(d, "manual.pdf"), "rb") as fh:
            return fh.read()


def chapter_pages(pdf_bytes, titles):
    """Page number of each chapter heading, read back from the PDF. The
    contents page lists every title too, so take the first page after it."""
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTTextContainer
    texts = []
    for layout in extract_pages(io.BytesIO(pdf_bytes)):
        lines = []
        for el in layout:
            if isinstance(el, LTTextContainer):
                lines.extend(l.strip() for l in el.get_text().splitlines())
        texts.append(lines)
    toc_page = next(i for i, lines in enumerate(texts) if "Contents" in lines)
    pages, start = {}, toc_page + 1
    for title in titles:
        for i in range(start, len(texts)):
            if title in texts[i]:
                pages[title] = i + 1
                start = i + 1
                break
    return pages


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--edition", choices=("user", "scale", "both"), default="both")
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "manual-pdf"))
    ap.add_argument("--docx-only", action="store_true")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)
    for edition in (("user", "scale") if args.edition == "both" else (args.edition,)):
        name = EDITIONS[edition]["file"]
        docx_bytes, titles = build(edition)
        if not args.docx_only:
            pages = chapter_pages(to_pdf(docx_bytes), titles)
            missing = [t for t in titles if t not in pages]
            if missing:
                print("  warning: no page found for", missing)
            docx_bytes, _ = build(edition, pages)
            pdf = to_pdf(docx_bytes)
            with open(os.path.join(args.out, name + ".pdf"), "wb") as fh:
                fh.write(pdf)
        with open(os.path.join(args.out, name + ".docx"), "wb") as fh:
            fh.write(docx_bytes)
        print("%s: %s.docx%s" % (edition, os.path.join(args.out, name),
                                 "" if args.docx_only else " + .pdf"))


if __name__ == "__main__":
    main()
