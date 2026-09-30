"""How sure are we that a PDF parse is the whole, honest BOM? (0-100)

A deterministic PDF parser is only worth trusting when the document is
clean and the parser explained everything it saw. This scores both, and the
check route compares the score with the admin threshold (setting
``bom_pdf_min_certainty``, default 85): at or above it the parse is used and
no agent is paid for; below it the file goes to the Claude agent (or is
refused when no agent is configured). The reasons are stored with the check
and shown to reviewers.

Hard stops (score 0 — the parse is never trusted):
  * active content: JavaScript, launch actions, embedded files, forms that
    submit, rich media — nothing a generated quote contains;
  * encrypted PDFs, and PDFs cut off at the page/word limit;
  * text a person cannot see (invisible render mode, text the colour of
    what is behind it, text painted over, sub-1.5 pt, off-page) *inside the
    parts table* — the classic way to smuggle
    parts or instructions past a human reviewer;
  * more than 5% of the text unreadable (missing font maps), or more than a
    fifth of the table's lines unexplained, or nothing read at all.

Deductions (the rest):
  * hidden text elsewhere on the page (low contrast, covered, tiny, off-
    page), document actions (OpenAction / AA),
    an incremental update (the file was edited after it was generated);
  * each table line the parser could not place, each part number that does
    not look like the vendor's, each cross-check of the document's own
    totals that failed, having no cross-check to run at all;
  * stray item-like lines outside the table;
  * a configuration without a CPU, memory, drives or a server identity, and
    quantities no quote would carry.

Weights are deliberately blunt: one unexplained row or one failed total
drops a clean document below the default threshold, which is the point —
when the document's own arithmetic disagrees with what we read, a person or
the agent should look.
"""
from dataclasses import dataclass, field
from typing import List, Optional

DEFAULT_THRESHOLD = 85
THRESHOLD_SETTING = "bom_pdf_min_certainty"

_DANGEROUS = {"JavaScript", "JS", "Launch", "EmbeddedFile", "EmbeddedFiles", "RichMedia",
              "XFA", "SubmitForm", "ImportData", "GoToE", "GoToR", "RichMediaExecute",
              "Rendition", "Sound", "Movie", "TooManyObjects"}


@dataclass
class Certainty:
    score: int
    reasons: List[dict] = field(default_factory=list)
    hard_stop: bool = False
    hidden_words: int = 0

    def to_dict(self):
        return {"score": self.score, "hard_stop": self.hard_stop,
                "reasons": self.reasons[:30], "hidden_words": self.hidden_words}


def threshold() -> int:
    try:
        from auth import get_setting
        return max(0, min(100, int(get_setting(THRESHOLD_SETTING, str(DEFAULT_THRESHOLD)))))
    except Exception:
        return DEFAULT_THRESHOLD


def safety(doc) -> Certainty:
    """File-level signals only: used on its own for PDFs no parser knows
    (the agent path records them) and as the start of assess()."""
    c = Certainty(score=100)

    def cost(code, points, text, hard=False):
        c.reasons.append({"code": code, "points": 100 if hard else points, "text": text})
        c.hard_stop = c.hard_stop or hard

    active = set(doc.active)
    dangerous = sorted(active & _DANGEROUS)
    if dangerous:
        cost("active_content", 0, "The PDF contains active content (%s)." % ", ".join(dangerous), hard=True)
    if active & {"OpenAction", "AA"}:
        cost("pdf_actions", 10, "The PDF runs actions when opened.")
    if doc.encrypted:
        cost("encrypted", 0, "The PDF is encrypted.", hard=True)
    if doc.truncated:
        cost("too_long", 0, "The PDF is longer than the reader's limit.", hard=True)
    if doc.incremental_updates:
        cost("edited", 15, "The PDF was edited after it was generated.")
    garbled = doc.garbled_ratio()
    if garbled >= 0.05:
        cost("garbled", 0, "Too much of the text is unreadable (%.0f%%)." % (garbled * 100), hard=True)
    elif garbled >= 0.01:
        cost("garbled", 30, "Some of the text is unreadable (%.1f%%)." % (garbled * 100))
    c.hidden_words = len(doc.hidden_words)
    return _finish(c)


def _in_regions(word, regions) -> bool:
    return any(word.page == page and word.bottom >= top - 2 and word.top <= bottom + 2
               for page, top, bottom in regions)


def assess(doc, ev, bom) -> Certainty:
    c = safety(doc)

    def cost(code, points, text, hard=False):
        c.reasons.append({"code": code, "points": 100 if hard else points, "text": text})
        c.hard_stop = c.hard_stop or hard

    hidden = doc.hidden_words
    if hidden:
        inside = [w for w in hidden if _in_regions(w, ev.regions)]
        if inside:
            cost("hidden_text_in_table", 0, "Invisible text inside the parts table: %s"
                 % " ".join(w.text for w in inside[:12])[:200], hard=True)
        else:
            # Outside the parts table hidden text cannot change the parse (the
            # parsers and the agent only see visible text), so it is a mark
            # against the file, not a stop: e.g. Arrow's quote template keeps
            # three stale lines in black on its black totals band.
            cost("hidden_text", 10, "The PDF contains %d invisible word(s) outside the parts table: %s"
                 % (len(hidden), " ".join(w.text for w in hidden[:12])[:160]))

    if bom is None or not bom.configs or ev.rows == 0:
        cost("nothing_read", 0, "No parts could be read from the table.", hard=True)
        return _finish(c)

    n = len(ev.unexplained)
    if n:
        if n / float(ev.rows + n) > 0.2:
            cost("unexplained", 0, "%d of %d table lines could not be read." % (n, ev.rows + n), hard=True)
        else:
            cost("unexplained", min(60, 12 * n), "%d table line(s) could not be read: %s"
                 % (n, "; ".join(ev.unexplained[:3])[:200]))
    if ev.pattern_misses:
        cost("part_pattern", min(40, 8 * len(ev.pattern_misses)),
             "Part number(s) that do not look like the vendor's: %s" % ", ".join(ev.pattern_misses[:5]))
    for label in ev.checks_failed[:3]:
        cost("cross_check", 25, "The document's own figures disagree: %s" % label)
    if not ev.checks_passed and not ev.checks_failed:
        cost("no_cross_check", 10, "The document has no totals to verify the parts list against.")
    stray = [n for n in ev.notes if n.startswith("stray:")]
    if stray:
        cost("stray_lines", min(20, 4 * len(stray)), "%d item-like line(s) outside the table." % len(stray))

    for cfg in bom.configs:
        cats = {comp.category for comp in cfg.components}
        label = cfg.name or "Config"
        if "cpu" not in cats:
            cost("no_cpu", 15, "%s: no processor found." % label)
        if "memory" not in cats:
            cost("no_memory", 10, "%s: no memory found." % label)
        if "storage" not in cats:
            cost("no_storage", 10, "%s: no drives found." % label)
        if "chassis" not in cats and not cfg.server_model:
            cost("no_server", 15, "%s: no server model or chassis found." % label)
        big = [comp for comp in cfg.components if comp.quantity > 1000]
        if big:
            cost("quantity", 20, "%s: implausible quantity (%d)." % (label, big[0].quantity))
    return _finish(c)


def _finish(c: Certainty) -> Certainty:
    c.score = 0 if c.hard_stop else max(0, 100 - sum(r["points"] for r in c.reasons))
    return c


def passes(c: Optional[Certainty]) -> bool:
    return c is not None and not c.hard_stop and c.score >= threshold()
