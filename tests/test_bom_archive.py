"""Every real partner BOM in _archive/boms/ must keep being read correctly.

The synthetic fixtures (test_bom_parsers.py) pin each layout we know; these
tests run the REAL files partners send, which is where the surprises have come
from — a German header, a stranded HDD line, 'Product Qty' instead of
'Product Quantity', a decimal comma that read 2,8 GHz as 8 GHz.

Two checks per file:

  * sanity — true of any correct read, so it runs even for a file nobody has
    reviewed yet: recognised, a hardware config, a node count, a server model,
    a resolved CPU, RAM, disks with capacities, usable storage. These are the
    silent losses a parse can survive while looking successful.
  * reviewed facts — once a person has checked the read against the sheet and
    signed it off (tools/bom_archive.py --bless), the facts must stay exactly
    that. A file marked unsupported must stay unrecognised.

``_archive`` is gitignored (customer documents), so on a machine without it
everything here skips — visibly, never as a silent pass.

Workflow for a new BOM: drop it into _archive/boms/<vendor>/, run
``.venv/bin/python tools/bom_archive.py --missing``, check the printout against
the spreadsheet, then ``--bless`` it (or ``--unsupported --reason ...``).

Run: .venv/bin/python -m pytest tests/test_bom_archive.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import pytest  # noqa: E402

import bom_archive  # noqa: E402  (tools/bom_archive.py)
from bom.facts import ARCHIVE_EXTENSIONS, bom_facts, sanity_problems  # noqa: E402
from bom.parsers import UnrecognizedFormat  # noqa: E402

ARCHIVE_MISSING = "no partner BOMs in _archive/boms (gitignored)"

FILES = [p for p in bom_archive.archive_files()
         if p.lower().endswith(ARCHIVE_EXTENSIONS)]
PARAMS = [pytest.param(p, id=os.path.relpath(p, bom_archive.ARCHIVE)) for p in FILES] or [
    pytest.param(None, marks=pytest.mark.skip(reason=ARCHIVE_MISSING))]


@pytest.mark.parametrize("path", PARAMS)
def test_archive_bom_is_read_sanely(path):
    expected = bom_archive.load_expectation(path)
    if expected and expected.get("unsupported"):
        with pytest.raises(UnrecognizedFormat):
            bom_facts(path)
        return
    try:
        facts = bom_facts(path)
    except UnrecognizedFormat as exc:
        pytest.fail("not recognised: %s — support the layout, or record it with "
                    "tools/bom_archive.py --unsupported --reason ..." % exc)
    problems = sanity_problems(facts)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("path", PARAMS)
def test_archive_bom_matches_its_reviewed_facts(path):
    expected = bom_archive.load_expectation(path)
    if expected is None:
        pytest.skip("not signed off yet: check `tools/bom_archive.py --missing`, "
                    "then `--bless` it")
    if expected.get("unsupported"):
        pytest.skip("marked unsupported: %s" % expected.get("reason"))
    assert bom_facts(path) == expected


def test_every_non_spreadsheet_in_the_archive_is_accounted_for():
    """A PDF or other file in the folder is never read by the checker; it must
    be recorded as unsupported, so a quote nobody can check does not sit there
    looking covered."""
    stray = [p for p in bom_archive.archive_files()
             if not p.lower().endswith(ARCHIVE_EXTENSIONS)
             and not (bom_archive.load_expectation(p) or {}).get("unsupported")]
    assert not stray, ("files the checker cannot read, not marked unsupported:\n  "
                       + "\n  ".join(os.path.relpath(p, bom_archive.ARCHIVE) for p in stray))


def test_the_harness_reads_a_known_good_bom_cleanly():
    """The harness logic itself, on a committed synthetic fixture, so it is
    covered on machines without the (gitignored) archive too."""
    path = os.path.join(os.path.dirname(__file__), "fixtures", "bom",
                        "synthetic_dell_solution_en.xlsx")
    facts = bom_facts(path)
    assert not sanity_problems(facts)
    assert [(c["name"], c["node_count"]) for c in facts["configs"]] == [
        ("R6615- Full Configuration - All Flash", 2), ("R7615 - Hybrid", 3)]


def test_the_harness_flags_a_silent_loss():
    """A read that 'succeeds' but lost its node count and disks must not pass."""
    facts = {"configs": [{"name": "X", "server_model": "PowerEdge R760", "node_count": None,
                          "cores_per_node": 32, "cpu_source": "catalog", "cpu": "x",
                          "ram_per_node_gb": 512, "drives": [], "usable_storage_tb": 0,
                          "unresolved": []}]}
    problems = sanity_problems(facts)
    assert any("node count" in p for p in problems)
    assert any("no data drives" in p for p in problems)
