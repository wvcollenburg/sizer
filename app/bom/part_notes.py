"""Apply reviewers' part notes to a BOM check's findings.

A note (bom_models.BomPartNote) is a super admin's standing verdict on one
part — "the Broadcom 5720 LOM must be disabled in the BIOS". For every BOM
line a note matches, the generic finding the rules gave that line (not on the
HCL, CPU unknown, a LOM notice) is REPLACED by the note, at the note's
severity; a line with no such finding (a part that is on the HCL) gets the
note as an extra finding (owner decisions 2026-09-17).

Runs in bom/check.py after the ported rules and BEFORE the verdict, swap
suggestions and review flags are worked out, so a note that downgrades an
error stops the BOM failing, stops offering swaps for that part and stops
opening a review for it. rules.py is untouched, so the 26-BOM replay is too.

Matching, per note:
  * exact    — the normalised part number when the line has one (and the note
               too); otherwise the description, case- and space-insensitive;
  * contains — every word of the note's match text appears in the line's part
               number + description (e.g. "5720 LOM"), across vendors.
An optional category limits a note to one kind of line. Exact notes win over
"contains" notes; among equals the newest wins.
"""
import re
from typing import List, Optional

from bom.normalize import BOMComponent, BOMConfig, Finding, normalize_part

CODE_PART_NOTE = "part_note"

# The generic "we could not vouch for this part" findings a note may replace.
# Anything else the rules say about a line (DWPD, SED, passthrough mode, ...)
# still stands next to the note.
REPLACEABLE_CODES = frozenset([
    "nic_not_in_hcl", "controller_not_in_hcl", "gpu_not_in_hcl", "cpu_unknown",
    "nic_lom", "component_delisted",
])

_WS_RE = re.compile(r"\s+")


def _squash(text: Optional[str]) -> str:
    return _WS_RE.sub(" ", (text or "")).strip().lower()


def load_notes() -> List:
    """Active notes, most specific first (exact before contains, newest first).
    Never raises: no table yet, no app context -> no notes."""
    try:
        from bom_models import BomPartNote, NOTE_MATCH_EXACT
        rows = BomPartNote.query.filter_by(active=True).order_by(
            BomPartNote.updated_at.desc(), BomPartNote.id.desc()).all()
    except Exception:
        return []
    return sorted(rows, key=lambda n: 0 if n.match_mode == NOTE_MATCH_EXACT else 1)


def note_matches(note, component: BOMComponent) -> bool:
    if note.category and note.category != component.category:
        return False
    if note.match_mode == "contains":
        words = _squash(note.match_text).split()
        if not words:
            return False
        haystack = _squash("%s %s" % (component.part_number or "", component.description))
        return all(w in haystack for w in words)
    # exact
    if note.part_number and component.part_number:
        return (normalize_part(note.part_number.strip().upper())
                == normalize_part(component.part_number.strip().upper()))
    if note.description:
        return _squash(note.description) == _squash(component.description)
    return False


def note_for(component: BOMComponent, notes) -> Optional[object]:
    for note in notes:
        if note_matches(note, component):
            return note
    return None


def apply_notes(config: BOMConfig, findings: List[Finding], notes) -> List[Finding]:
    """Findings with the notes applied (a new list; the input is not changed)."""
    if not notes:
        return findings
    out = list(findings)
    for component in config.components:
        note = note_for(component, notes)
        if note is None:
            continue
        desc = component.description
        replaced = [f for f in out if f.component == desc and f.code in REPLACEABLE_CODES]
        out = [f for f in out if f not in replaced]
        out.append(Finding(
            severity=note.severity,
            component=desc,
            issue=note.issue,
            remediation=note.remediation or "",
            code=CODE_PART_NOTE,
        ))
    return out
