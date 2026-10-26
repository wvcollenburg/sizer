"""How sure are we that a column-shape read of a spreadsheet is the whole
BOM? (0-100) — the spreadsheet counterpart of bom/pdf_certainty.py.

The score shares the PDF scale and threshold (admin setting
``bom_pdf_min_certainty``, default 85): at or above it the guessed read is
used and no agent is paid for; below it the file goes to the Claude agent
(or is refused when no agent is configured). The reasons are stored with
the check and shown to reviewers.

Hard stops: no table at all, fewer than four part lines, more than a fifth
of the part-like lines unplaced. Deductions: each unplaced line, each part
number that does not look like the vendor's, no header row (the columns were
inferred from their content alone), no part-number column, no quantity
column (every line then counts once), a config whose node count the sheet
did not state, and — shared with the PDF score — a configuration without a
CPU, memory, drives or a server identity, and implausible quantities.

One unplaced line costs the same as on a PDF, so a clean retype of a known
layout with a header scores 100 and one without a header 90; anything the
read had to guess around drops it below the default threshold.
"""
from typing import Optional

from bom.pdf_certainty import Certainty, _bom_deductions, _finish, passes, threshold  # noqa: F401

__all__ = ['assess', 'passes', 'threshold']


def assess(ev, bom) -> Certainty:
    c = Certainty(score=100)
    cost = c.cost
    if bom is None or not bom.configs or ev.rows <= 0:
        cost('nothing_read', 0, 'No parts table could be found in the spreadsheet.', hard=True)
        return _finish(c)
    if ev.rows < 4:
        cost('too_few', 0, 'Only %d part line(s) found.' % ev.rows, hard=True)
        return _finish(c)
    n = len(ev.unexplained)
    if n:
        if n / float(ev.rows + n) > 0.2:
            cost('unexplained', 0, '%d of %d part-like lines could not be placed.' % (n, ev.rows + n), hard=True)
        else:
            cost('unexplained', min(60, 12 * n), '%d line(s) could not be placed: %s'
                 % (n, '; '.join(ev.unexplained[:3])[:200]))
    if ev.pattern_misses:
        cost('part_pattern', min(40, 8 * len(ev.pattern_misses)),
             "Part number(s) that do not look like the vendor's: %s" % ', '.join(ev.pattern_misses[:5]))
    if not ev.header_found:
        cost('no_header', 10, 'No header row: the columns were inferred from their content.')
    if not ev.part_col:
        cost('no_part_column', 15, 'No part-number column: parts are matched on their descriptions only.')
    if not ev.qty_col:
        cost('no_quantity_column', 20, 'No quantity column: every line counts once.')
    if ev.configs_without_nodes:
        cost('no_node_count', min(30, 15 * ev.configs_without_nodes),
             '%d configuration(s) without a stated machine count.' % ev.configs_without_nodes)
    if ev.configs_nodes_inferred:
        cost('node_count_inferred', min(20, 10 * ev.configs_nodes_inferred),
             '%d configuration(s): machine count taken from the chassis line, not stated.'
             % ev.configs_nodes_inferred)
    if ev.configs_smart_selection:
        cost('smart_selection_no_product_row', min(40, 20 * ev.configs_smart_selection),
             '%d Dell Smart Selection configuration(s) without the product row that states '
             'the machine count.' % ev.configs_smart_selection)
    _bom_deductions(c, bom)
    return _finish(c)


def summary(c: Optional[Certainty]) -> str:
    return '' if c is None else '; '.join(r['text'] for r in c.reasons)
