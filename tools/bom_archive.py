#!/usr/bin/env python
"""Report on, and sign off, the partner BOMs in _archive/boms/.

Every real BOM a partner sends goes into ``_archive/boms/<vendor>/`` (the
folder is gitignored: these are customer documents). This tool reads them all
the way the BOM checker does and prints what it made of each one, so a person
can compare it against the spreadsheet; once it is right, ``--bless`` writes
the reviewed ``<file>.expected.json`` beside the BOM, and
tests/test_bom_archive.py holds every later parser change to it.

    .venv/bin/python tools/bom_archive.py                  # report on everything
    .venv/bin/python tools/bom_archive.py --missing        # only files not yet signed off
    .venv/bin/python tools/bom_archive.py --bless FILE...  # sign off after checking the report
    .venv/bin/python tools/bom_archive.py --unsupported FILE --reason "PDF quote"

``--unsupported`` records a file the checker is NOT expected to read (a PDF
quote, a layout we chose not to support). The test then asserts it stays
unrecognised, so supporting it later is noticed rather than silently skipped.

Only bless what you have checked against the spreadsheet: the expectation is
the claim that the read is correct, and every future change is measured
against it.
"""
import argparse
import glob
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "app"))

from bom.facts import ARCHIVE_EXTENSIONS, bom_facts, sanity_problems  # noqa: E402
from bom.parsers import UnrecognizedFormat  # noqa: E402

ARCHIVE = os.path.join(ROOT, "_archive", "boms")


def archive_files(root=ARCHIVE):
    """Every BOM file under the archive, sorted, skipping the expectations."""
    paths = [p for p in glob.glob(os.path.join(root, "**", "*"), recursive=True)
             if os.path.isfile(p) and not p.endswith(".expected.json")]
    return sorted(paths)


def expectation_path(path):
    return path + ".expected.json"


def load_expectation(path):
    exp = expectation_path(path)
    if not os.path.isfile(exp):
        return None
    with open(exp, encoding="utf-8") as fh:
        return json.load(fh)


def write_expectation(path, data):
    with open(expectation_path(path), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")


def _rel(path):
    return os.path.relpath(path, ARCHIVE)


def report(paths):
    for path in paths:
        print("\n" + "=" * 100)
        exp = load_expectation(path)
        status = ("signed off" if exp and not exp.get("unsupported")
                  else "marked unsupported" if exp else "NOT SIGNED OFF")
        print("%s   [%s]" % (_rel(path), status))
        if not path.lower().endswith(ARCHIVE_EXTENSIONS):
            print("   not a spreadsheet: the checker only reads .xlsx/.csv")
            continue
        try:
            facts = bom_facts(path)
        except UnrecognizedFormat as exc:
            print("   UNRECOGNISED: %s" % exc)
            continue
        print("   format %s, vendor %s, %d hardware config(s)"
              % (facts["format"], facts["vendor"], len(facts["configs"])))
        for c in facts["configs"]:
            print("   - %r: %s x %s nodes (%s HCI)" % (c["name"], c["server_model"],
                                                     c["node_count"], c["hci_node_count"]))
            print("       CPU  %s  [%s]  %s sockets, %s cores / %s threads per node"
                  % (c["cpu"], c["cpu_source"], c["sockets_per_node"],
                     c["cores_per_node"], c["threads_per_node"]))
            print("       RAM  %s GB per node" % c["ram_per_node_gb"])
            print("       disks per node  %s" % (", ".join(
                "%d x %s TB %s" % (qty, cap, kind) for kind, cap, qty in c["drives"]) or "none"))
            print("       storage  %s, raw %s TB, usable %s TB"
                  % (c["storage_category"], c["raw_storage_tb"], c["usable_storage_tb"]))
            print("       NIC  %s GbE x %s ports" % (c["nic_gbe"], c["nic_ports"]))
        problems = sanity_problems(facts)
        for p in problems:
            print("   PROBLEM: %s" % p)
        if exp and not exp.get("unsupported") and exp != facts:
            print("   DIFFERS from the signed-off expectation")


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("files", nargs="*", help="limit to these files")
    parser.add_argument("--missing", action="store_true", help="only files not yet signed off")
    parser.add_argument("--bless", action="store_true",
                        help="write the reviewed expectation for the given files")
    parser.add_argument("--unsupported", action="store_true",
                        help="record the given files as deliberately unsupported")
    parser.add_argument("--reason", default="", help="why a file is unsupported")
    args = parser.parse_args(argv)

    paths = [os.path.abspath(f) for f in args.files] if args.files else archive_files()
    if args.missing:
        paths = [p for p in paths if load_expectation(p) is None]

    if args.bless or args.unsupported:
        if not args.files:
            parser.error("name the files to sign off explicitly")
        for path in paths:
            if args.unsupported:
                if not args.reason:
                    parser.error("--unsupported needs a --reason")
                write_expectation(path, {"unsupported": True, "reason": args.reason})
                print("marked unsupported: %s" % _rel(path))
                continue
            facts = bom_facts(path)
            problems = sanity_problems(facts)
            if problems:
                print("refusing to sign off %s:\n  %s" % (_rel(path), "\n  ".join(problems)))
                continue
            write_expectation(path, facts)
            print("signed off: %s" % _rel(path))
        return 0

    if not paths:
        print("No BOMs in %s" % ARCHIVE)
        return 0
    report(paths)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
