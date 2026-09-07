"""Install a quarterly SC//HyperCore price list (§4.4, §5.2).

    .venv/bin/python tools/import_pricebook.py <file.xlsx> [options]

**Dry run by default.** Nothing is written until you pass `--apply`. The diff it
prints first is the point of the tool: these numbers decide which configuration
the sizer recommends, so a silent import of a mis-parsed file would change real
proposals with nobody noticing.

What it does:

  1. Parses the `HCOS-*` licence rows — bands, flat SKUs, and anything it could
     not classify.
  2. Refuses obviously broken files: missing headers, or zero rows in the licence
     product family.
  3. Diffs against the region's current feed — new bands, removed bands, and
     every price that moved, with the percentage.
  4. On `--apply`, stores it as the new current feed. The previous feed is KEPT,
     not deleted, so a saved sizing stamped with it still reproduces.

Only licence rows are read. Hardware prices in the same workbook are deliberately
ignored — see docs/pricebook-plan.md §9.

Examples:

    # Look before you leap (default)
    .venv/bin/python tools/import_pricebook.py "~/Q1 2026 EUR Price List.xlsx"

    # Commit it
    .venv/bin/python tools/import_pricebook.py "~/Q1 2026 EUR Price List.xlsx" \\
        --label "Q1 2026 EUR" --effective-date 2026-04-01 --apply

    # A different region
    .venv/bin/python tools/import_pricebook.py "~/Q1 2026 NA.xlsx" \\
        --region NA --label "Q1 2026 USD" --apply
"""
import argparse
import datetime
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "app"))

os.environ.setdefault("ENABLE_SCHEDULER", "0")


def _fmt(v):
    return f"{v:,.2f}" if v is not None else "—"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Install a quarterly licence price list (dry run by default).")
    ap.add_argument("file", help="the .xlsx price list")
    ap.add_argument("--region", default="EMEA",
                    help="region this list prices (default: EMEA)")
    ap.add_argument("--label", help="human label; defaults to the filename")
    ap.add_argument("--effective-date", help="YYYY-MM-DD, optional")
    ap.add_argument("--sheet", help="sheet name; defaults to the first")
    ap.add_argument("--apply", action="store_true",
                    help="actually write it. Without this, nothing is stored.")
    args = ap.parse_args(argv)

    path = os.path.expanduser(args.file)
    if not os.path.exists(path):
        print(f"file not found: {path}")
        return 1

    import app as appmod
    from database import db
    import pricebook_import

    with appmod.app.app_context():
        db.create_all()
        try:
            parsed = pricebook_import.parse_pricebook(path, sheet=args.sheet)
        except pricebook_import.PricebookFormatError as exc:
            print(f"REFUSED: {exc}")
            print("\nNothing was changed. This usually means the export format "
                  "moved:\ncheck the column headers and the product-family name.")
            return 2

        c = parsed["counts"]
        print(f"file    : {os.path.basename(path)}")
        print(f"region  : {args.region}")
        print(f"currency: {parsed['currency'] or 'mixed/unknown'}")
        print(f"parsed  : {c['license_rows']} licence rows -> {c['banded']} bands, "
              f"{c['flat']} flat, {c['unmatched']} unmatched")

        if parsed["unmatched"]:
            print("\nUNMATCHED rows (recorded, not priced — check none of these "
                  "should have been):")
            for row in parsed["unmatched"]:
                print(f"   {row['sku']:<24} {row['description'][:60]}")

        editions = sorted({b["edition"] for b in parsed["bands"]})
        print(f"\neditions found: {', '.join(editions) or '(none)'}")
        import licensing
        unknown = [e for e in editions if e not in licensing.EDITION_NAMES]
        if unknown:
            print(f"   NEW edition letter(s) {', '.join(unknown)} — stored and "
                  "priced, but not selectable until a rule is added.")

        diff = pricebook_import.diff_feed(parsed, args.region)
        added, removed, moved = diff["added"], diff["removed"], diff["moved"]
        if not diff["current"]:
            print(f"\nNo current feed for {args.region} — this would be the first.")
        else:
            print(f"\ndiff vs current feed ({diff['current']['label']}):")
            print(f"   {len(added)} added, {len(removed)} removed, {len(moved)} changed")
            for m in moved[:40]:
                print(f"   ~ {m['kind']} {m['key']:<28} {_fmt(m['old']):>12} -> "
                      f"{_fmt(m['new']):>12}  ({m['pct']:+.1f}%)")
            if len(moved) > 40:
                print(f"   ... and {len(moved) - 40} more price changes")
            for a in added[:15]:
                print(f"   + {a['kind']} {a['key']:<28} {_fmt(a['new']):>12}")
            if len(added) > 15:
                print(f"   ... and {len(added) - 15} more additions")
            for r in removed[:15]:
                print(f"   - {r['kind']} {r['key']:<28} {_fmt(r['old']):>12}")
            if len(removed) > 15:
                print(f"   ... and {len(removed) - 15} more removals")

        if not args.apply:
            print("\nDRY RUN — nothing written. Re-run with --apply to install.")
            return 0

        eff = None
        if args.effective_date:
            eff = datetime.date.fromisoformat(args.effective_date)
        feed, _ = pricebook_import.seed_feed_from_file(
            path, region=args.region,
            label=args.label or os.path.splitext(os.path.basename(path))[0],
            effective_date=eff, sheet=args.sheet)
        print(f"\nINSTALLED as feed #{feed.id} ({feed.region}, {feed.currency}). "
              "The previous feed is kept for saved sizings.")
        print("Next: re-run tools/license_sweep.py and check that no "
              "recommendation moved for a reason you cannot state.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
