"""Catalog truth: which Active models can actually be quoted (§4.2).

The recommender filters on `Model.status == "Active"`, so a wrong status either
recommends hardware that cannot be sold or hides hardware that can. Comparing our
catalog against a price list finds both — but only if you read the comparison
correctly, and the original analysis did not.

**A model absent from the price list means one of two OPPOSITE things:**

  * it is **end-of-sale** — the list is current and the model is no longer
    quotable; or
  * it is **NEW** — the model postdates the list, and the LIST is stale.

Note the first is EOS, never EOL: a price list evidences what can be SOLD. A
model can stop being sellable while remaining fully supported, and only product
management knows which. `status` carries both values and the recommender
excludes either, but they mean different things to a customer.

Treating every absence as end-of-sale would mark new products unsellable, which is the
worst failure this tool could cause: the sizer would silently stop recommending
the newest hardware. Confirmed by the product owner on 2026-09-02 that the
16XX(D) line is exactly this case — an update of the 14XX(D), newer than the
Q4 2025 list.

So absences are classified by model number within the family: a model numbered
ABOVE the highest priced peer in its family is probably new; one numbered below
is probably EOL. That is a heuristic, and it is reported as such — nothing here
edits a status. A status change is a product assertion and needs a human.

Run from the repo root:

    .venv/bin/python tools/catalog_check.py [path/to/pricelist.xlsx]
"""
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "app"))

DEFAULT_LIST = os.path.join(
    ROOT, "_archive", "Scale Computing Q4 2025 EUR Master Price List.xlsx")

COL_FAMILY, COL_SERIES = 11, 12


def priced_models(path):
    """Model names appearing as a chassis row in the price list.

    Returned keyed by UPPERCASE name. The price list and our catalog disagree on
    case — the list says `HE153P`, we say `HE153p` — and a case-sensitive
    comparison reports that model as both "missing from our catalog" and
    "end-of-life" simultaneously, which is how you end up EOL-ing a product that
    is on sale.
    """
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    try:
        ws = wb[wb.sheetnames[0]]
        names = {str(r[COL_SERIES] or "").strip() for r in ws.values
                 if str(r[COL_FAMILY] or "").strip() == "Chassis"}
    finally:
        wb.close()
    names.discard("")
    return {n.upper(): n for n in names}


def _number(name):
    m = re.match(r"^H[EC](\d+)", (name or "").upper())
    return int(m.group(1)) if m else None


def _family(name):
    m = re.match(r"^H[EC]\d", (name or "").upper())
    return m.group(0) if m else None


def classify(catalog, priced):
    """Split Active-but-unpriced models into likely-new, likely-EOS, unknown."""
    new, eol, unknown = [], [], []
    for name, model in sorted(catalog.items()):
        if model.get("status") != "Active" or name.upper() in priced:
            continue
        if model.get("category") == "Cloud":
            continue                      # virtual, never has a chassis SKU
        num, fam = _number(name), _family(name)
        peers = [(_number(o), o) for o in priced.values()
                 if _family(o) == fam and _number(o)]
        if num is None or not peers:
            unknown.append((name, "no priced peer in its family"))
            continue
        top_num, top_name = max(peers)
        if num > top_num:
            new.append((name, f"numbered above {top_name}, the newest priced peer"))
        elif num == top_num:
            # Same number, different variant (HE155-1 vs HE155-2). Numbering says
            # nothing about which is current.
            unknown.append((name, f"same number as {top_name}; a variant, not a generation"))
        else:
            eol.append((name, f"{top_name} is newer and priced"))
    return new, eol, unknown


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_LIST
    if not os.path.exists(path):
        print(f"price list not found: {path}")
        return 1
    from models import APPLIANCE_MODELS

    priced = priced_models(path)
    new, eol, unknown = classify(APPLIANCE_MODELS, priced)

    print(f"price list : {os.path.basename(path)}  ({len(priced)} chassis SKUs)")
    print(f"catalog    : {len(APPLIANCE_MODELS)} models\n")

    print("LIKELY NEW — keep Active; the PRICE LIST is stale, not the catalog")
    for name, why in new or [("(none)", "")]:
        print(f"   {name:<10} {why}")

    print("\nLIKELY END-OF-SALE — candidates for status EOS, NEEDS CONFIRMATION")
    for name, why in eol or [("(none)", "")]:
        print(f"   {name:<10} {why}")

    print("\nUNDECIDABLE — numbering cannot tell; ask")
    for name, why in unknown or [("(none)", "")]:
        print(f"   {name:<10} {why}")

    ours = {n.upper() for n in APPLIANCE_MODELS}
    missing = sorted(orig for up, orig in priced.items() if up not in ours)
    # A model here is either genuinely missing, or present in the list only
    # because the list has gone stale. As of the Q4 2025 list the two entries are
    # HC1250DFG and HC5250DFG, both confirmed end-of-sale — so this section needs
    # the same "is the list current?" judgement as the one above it.
    print("\nPRICED BUT NOT IN OUR CATALOG — either a real gap, or the list is stale")
    for name in missing or ["(none)"]:
        print(f"   {name}")

    print("\nNothing above has been changed. A status is a product assertion;")
    print("this tool only says where to look.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
