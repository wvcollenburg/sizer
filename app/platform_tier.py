"""Keeping `platform_tier` honest (docs/pricebook-plan.md §8.2, §8.3).

`platform_tier` is a hand-set capability weight, and `eur_per_tier_point` is the
policy dial that lets a real licence euro be traded against it in the score. Both
are only meaningful while the ladder still roughly tracks what hardware costs.

It does not, and this module is how we find out.

**Nothing here feeds a price into the engine.** It derives a *recommended* rate
from a basket of real configured-node prices, and it reports which models' tiers
have drifted away from that basket. A human decides what to do. Chasing hardware
prices automatically is exactly what §9 parked: they move daily and
non-uniformly, and a ranker that followed them would produce recommendations
that change every quarter for reasons unrelated to the workload.

**Reference prices are configured-node prices from quotes**, i.e. the chassis
line where CPU, RAM, drives and NIC all appear at EUR 0.00 because they are rolled
in. NOT the price-list chassis row, which is a bare frame: `CHA-3-1A` is EUR 19,385
on a quote and EUR 2,937 in the price list, a 6.6x difference.

Only model, tier, date, price and a note are stored — never the quote, the end
customer or the partner.
"""
import statistics

# Divergence beyond this fraction of the basket median flags a model as needing a
# `platform_tier` review. 0.25 is a starting point, not a measured value.
DEFAULT_THRESHOLD = 0.25

# Measured Jan -> Aug 2026 multiplier for the HC1450D configured node, from the
# HW-ADJ quote (EUR 127,264.08 against six nodes originally at EUR 126,777.90 =
# +100.4%). Used ONLY to put an older reference on a current basis for
# comparison; it is not applied to anything the engine reads.
HC1450D_JAN_TO_AUG = 2.004

REFERENCE_CONFIGS = [
    {
        "band": "1XX SFF",
        "model": "HE153",
        "price_eur": 1588.00,
        "as_of": "2026-01",
        "source": "quote Q-102372-1 (configured node, MSRP)",
        "note": "3-node Essentials cluster; carried no HW-ADJ in August.",
    },
    {
        "band": "5XX Edge (hybrid)",
        "model": "HE552",
        "price_eur": 2500.00,
        "as_of": "2026-09",
        "source": "stated by product owner, approximate",
        "note": "Shares a bare-frame SKU price with HE502 and HE552F, so the "
                "whole 500 series sits close together.",
    },
    {
        "band": "5XX Edge (all-flash)",
        "model": "HE552F",
        "price_eur": 4000.00,
        "as_of": "2026-09",
        "source": "stated by product owner, approximate",
        "note": "Approximate. Its hybrid sibling HE552 shares the same bare "
                "frame SKU price, so the two are expected to be close.",
    },
    {
        "band": "1XXX Core (dual)",
        "model": "HC1450D",
        "price_eur": 19385.00,
        "as_of": "2026-01",
        "source": "quote Q-102372-1 (configured node, MSRP)",
        "note": "Roughly DOUBLED by August 2026 (quote Q-113979-1, HW-ADJ). The "
                "January figure is kept as the reference because it is the one "
                "we can evidence to the cent; see HC1450D_JAN_TO_AUG.",
    },
    {
        "band": "3XXX Core",
        "model": "HC3450DF",
        "price_eur": 45000.00,
        "as_of": "2026-09",
        "source": "stated by product owner, approximate",
        "note": "Approximate. Band spans tier 26-40; HC3450DF (34) taken as the "
                "representative workhorse.",
    },
    {
        "band": "5XXX Core",
        "model": "HC5450D",
        "price_eur": 30000.00,
        "as_of": "2026-09",
        "source": "stated by product owner, approximate",
        "note": "Approximate. Band spans tier 22-40; HC5450D (34) taken as the "
                "representative.",
    },
    # NOT FILLED — deliberately:
    #
    #   "1XXX Core (single)"        tier 14-18.  No priced reference. HC1600 and
    #       HC1650D are the UPDATED 14XX line and postdate the Q4 2025 price
    #       list, so their absence there means the price list is stale rather
    #       than the catalog being wrong — the opposite of what §4.2 assumed.
    #       Per the product owner the 16XX(D) sits in the same range as the
    #       14XX(D), so HC1650D can be read against the HC1450D entry above; the
    #       SINGLE-socket half of the band still has no price of its own.
]


def _live_tiers():
    """Tiers the engine actually uses: the DB column, falling back to the seed
    constants when there is no database (tests, tooling)."""
    try:
        from orm_models import Model
        rows = {m.name: m.cost_tier for m in Model.query.all()
                if m.cost_tier is not None}
        if rows:
            return rows
    except Exception:                                   # noqa: BLE001
        pass
    from models import MODEL_COSTS
    return dict(MODEL_COSTS)


def implied_rates(configs=None, tiers=None, normalise_epoch=False):
    """Euro per tier point implied by each reference configuration.

    `normalise_epoch` re-prices the January HC1450D reference onto a current
    basis so the basket is not comparing across a period in which one member
    doubled. Off by default: the adjustment is itself an estimate from a single
    deal, and it should be a visible choice rather than a hidden one.
    """
    tiers = tiers or _live_tiers()
    out = []
    for cfg in (configs or REFERENCE_CONFIGS):
        tier = tiers.get(cfg["model"])
        if not tier:
            continue
        price = cfg["price_eur"]
        adjusted = False
        if normalise_epoch and cfg["model"] == "HC1450D" and cfg["as_of"] < "2026-08":
            price *= HC1450D_JAN_TO_AUG
            adjusted = True
        out.append({**cfg, "tier": tier, "effective_price_eur": price,
                    "eur_per_tier_point": price / tier,
                    "epoch_adjusted": adjusted})
    return out


def basket_rate(configs=None, tiers=None, normalise_epoch=False):
    """The MEDIAN implied rate — a recommendation, not an automatic setting.

    Median rather than mean on purpose: with five points and a known outlier or
    two, one badly mis-tiered model must not drag the whole scale.
    """
    rows = implied_rates(configs, tiers, normalise_epoch)
    if not rows:
        return None
    return statistics.median(r["eur_per_tier_point"] for r in rows)


def divergence_report(configs=None, tiers=None, threshold=DEFAULT_THRESHOLD,
                      normalise_epoch=False):
    """Which models' tiers no longer match what the market charges for them.

    Returns (rows, median). Each row carries `deviation` (signed fraction of the
    median), `flagged`, and `implied_tier` — what the tier WOULD be at the basket
    rate, which is the actionable number for a re-basing decision.

    Two causes look identical here and cannot be told apart from one data point
    per band, so the report must not claim to distinguish them:

      * **drift** — the tier was right and the price moved (HC1450D: correct in
        January, out by 2x in August);
      * **structural mis-tiering** — the tier was never right.

    Telling them apart needs more than one price per band over time. Until then
    a flag means "look at this", not "change this".
    """
    rows = implied_rates(configs, tiers, normalise_epoch)
    if not rows:
        return [], None
    median = statistics.median(r["eur_per_tier_point"] for r in rows)
    for r in rows:
        r["deviation"] = r["eur_per_tier_point"] / median - 1
        r["flagged"] = abs(r["deviation"]) > threshold
        r["implied_tier"] = r["effective_price_eur"] / median
    rows.sort(key=lambda r: r["deviation"])
    return rows, median


# The pre-2026-09-02 capability ladder, kept ONLY so the calibration sweep can
# attribute a changed recommendation to the re-base or to licence-aware scoring.
# The two shipped together, so without this the before/after table shows both
# effects superimposed and neither can be explained on its own — which is what
# §10.3 forbids. Nothing in the engine reads this.
LEGACY_TIERS = {
    "HE150": 2, "HE151": 2, "HE153": 2, "HE153s": 2, "HE153p": 3,
    "HE155-1": 3, "HE155-2": 4, "HE250": 5, "SE100": 5,
    "HE500": 8, "HE501": 8, "HE502": 9,
    "HE550": 9, "HE551": 9, "HE552": 10,
    "HE550F": 11, "HE551F": 11, "HE552F": 12,
    "HC1200": 14, "HC1300": 15, "HC1400": 16, "HC1600": 18,
    "HC1250": 15, "HC1350": 17, "HC1450": 18,
    "HC1250D": 19, "HC1450D": 22, "HC1650D": 24,
    "HC3250DF": 24, "HC3350F": 26, "HC3350DF": 30,
    "HC3450F": 28, "HC3450DF": 34, "HC3450FG": 40,
    "HC3650F": 32, "HC3650DF": 38,
    "HC5200": 22, "HC5250D": 28, "HC5400": 26,
    "HC5450D": 34, "HC5600": 30, "HC5650D": 40,
    "Cloud Unity": 12,
}
