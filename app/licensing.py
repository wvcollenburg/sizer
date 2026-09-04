"""SC//HyperCore licence pricing — the banded, capped cost model.

This is the ONLY place in the sizer where real money exists. Everything else on
the cost side of the score is a relative weight (see docs/pricebook-plan.md §5.4
and §9): hardware prices move daily and non-uniformly, so they are deliberately
not tracked. Licence bands are product policy, set by Scale on a slow cadence,
which is exactly what makes them safe to encode.

Two things live here:

  * **The pricing math** — band lookup, the cap, the Essentials cliff. Pure
    functions over plain data, so they are testable without a database.
  * **The eligibility vocabulary** — a CLOSED set of named predicates. No
    expression strings, no eval. A rule the vocabulary cannot express must fail
    loudly at import rather than silently mis-price (§11).

Prices never reach a browser. `cluster_license()` returns euro for the scorer;
what crosses the wire is the annotation block, which carries booleans and
strings only.
"""

# ── Editions ─────────────────────────────────────────────────────────────────
# The SKU letter in HCOS-<edition>-<term>-<cores>C. Parsed as [A-Z]+ rather than
# a fixed set so a future edition lands as data marked "priced, not yet
# selectable" instead of as an unmatched row.
EDITION_STANDARD = "S"
EDITION_BRS = "L"
EDITION_VIDEO = "V"

EDITION_NAMES = {
    EDITION_STANDARD: "Standard",
    EDITION_BRS: "BRS (Business Resilience System)",
    EDITION_VIDEO: "Video Surveillance",
}

# BRS and Video Surveillance are deferred (§6) — too niche for the first build.
# Their bands are still parsed and stored so the tables are current whenever the
# profiles are picked up; nothing in the engine offers them.
SELECTABLE_EDITIONS = (EDITION_STANDARD,)

# Support tiers. Only Standard carries a Premium ladder; BRS and Video are
# SS-only. The plan fixes scoring on Standard Support (§5.3).
SUPPORT_STANDARD = "SS"
SUPPORT_PREMIUM = "PS"
DEFAULT_SUPPORT = SUPPORT_STANDARD

# Flat (non-banded) SKU kinds.
FLAT_ESSENTIALS = "SE"           # Essentials Kit
FLAT_PRO_ESSENTIALS = "PE"       # Professional Essentials
FLAT_SITE_WORKLOAD = "WL"        # N-workload single-site licence (not selectable)

ESSENTIALS_KINDS = (FLAT_ESSENTIALS, FLAT_PRO_ESSENTIALS)

# Essentials mirrors the banded ladder's support split rather than being a
# cheapest-wins choice: Essentials Kit is the Standard-Support tier and
# Professional Essentials the Premium one. Confirmed by a real quote, which
# pairs HCOS-S-5-16C-**PS** on the production clusters with HCOS-5-**PE** on the
# Essentials clusters — the customer bought Professional throughout. Selecting
# by price instead would silently mix support tiers within one sizing.
ESSENTIALS_BY_SUPPORT = {
    SUPPORT_STANDARD: FLAT_ESSENTIALS,
    SUPPORT_PREMIUM: FLAT_PRO_ESSENTIALS,
}

# Essentials ceilings. Product policy, not derived from the feed — the feed
# carries the price, these carry the shape. Overridable per-feed via
# PriceLicenseRule so a policy change is one admin edit (§11).
ESSENTIALS_EXACT_NODES = 3
ESSENTIALS_MAX_RAM_GB_PER_NODE = 256

MIN_TERM_YEARS = 1
MAX_TERM_YEARS = 5


# ── Eligibility vocabulary ───────────────────────────────────────────────────
# A fixed set of named fields evaluated against a candidate. Adding a band,
# term, edition or region is zero code; a genuinely new KIND of constraint is a
# new entry here plus a predicate, and that is deliberate (§11).
ELIGIBILITY_FIELDS = (
    "exact_nodes",             # int   — cluster must have exactly this many nodes
    "max_nodes",               # int   — cluster must have at most this many
    "min_hci_nodes",           # int   — at least this many VM-running nodes
    "max_ram_gb_per_node",     # int   — per-node RAM ceiling
    "max_cores_per_node",      # int   — per-node core ceiling
    "requires_single_node",    # bool  — must be a single-node system (BRS)
    "bundleable",              # bool  — may be combined with other licences
    "role_gated",              # bool  — Scale staff / super-admin only
    "workload_class",          # str   — e.g. "cctv"
)


class UnrepresentableRule(ValueError):
    """A licence rule the closed vocabulary cannot express.

    Raised at import or admin-save, never at sizing time. "We cannot represent
    this" must be a visible error rather than a silent misprice (§11).
    """


def validate_rule(rule):
    """Check a rule dict against the closed vocabulary. Returns it unchanged."""
    unknown = sorted(set(rule) - set(ELIGIBILITY_FIELDS))
    if unknown:
        raise UnrepresentableRule(
            "licence rule uses field(s) outside the closed eligibility "
            f"vocabulary: {', '.join(unknown)}. Add a predicate to "
            "licensing.ELIGIBILITY_FIELDS (a code change, reviewed) rather than "
            "widening the data."
        )
    return rule


# ── Band lookup ──────────────────────────────────────────────────────────────

def clamp_term(term_years):
    """Licence term is its own input (§5.3), independent of the growth horizon."""
    try:
        t = int(term_years)
    except (TypeError, ValueError):
        t = MAX_TERM_YEARS
    return max(MIN_TERM_YEARS, min(t, MAX_TERM_YEARS))


def band_price(bands, cores):
    """Price one node at `cores` licensable cores against an ascending band map.

    `bands` is {core_band: price}. Returns (price, annotation_or_None).

    Two behaviours that matter to sizing:

      * **The cap is data, not a constant.** 48C/52C/56C/64C all carry the same
        price in the real table, so the cap emerges from the feed and moves on
        its own when Scale moves it. Nothing here knows "48".
      * **Above the top band we clamp** (§5.5). A dual-socket EPYC node can be
        192 physical cores with no SKU. Clamping is only safe because
        `w_core_burden` is uncapped and prices cores independently of the
        licence — with the licence as the sole brake, this would make the
        largest CPU the cheapest choice on every axis.
    """
    if not bands:
        return None, None
    ladder = sorted(bands)
    for band in ladder:
        if cores <= band:
            return bands[band], None
    top = ladder[-1]
    return bands[top], (
        f"{cores} cores/node exceeds the published {top}-core band; "
        f"priced at the top band"
    )


def cap_annotation(bands, cores):
    """Human-readable note when a node sits in the flat part of the curve.

    Returns None below the cap. This is what tells an SA that cores beyond the
    cap are free, which is a genuinely useful sizing fact and the reason the
    banded model changes recommendations at all.
    """
    if not bands or cores is None:
        return None
    ladder = sorted(bands)
    top_price = bands[ladder[-1]]
    # The cap is the smallest band already charging the top price.
    cap = next((b for b in ladder if bands[b] == top_price), None)
    if cap is None or cores <= cap:
        return None
    return f"cores {cap + 1}–{ladder[-1]} per node carry no licence cost"


# ── Essentials ───────────────────────────────────────────────────────────────

def essentials_eligibility(cluster_layout, ram_gb_per_node,
                           exact_nodes=ESSENTIALS_EXACT_NODES,
                           max_ram_gb=ESSENTIALS_MAX_RAM_GB_PER_NODE):
    """Is this sizing an Essentials candidate? Returns (eligible, near_miss).

    Essentials is aimed at SMB and is a genuine design attractor — roughly 2.6x
    cheaper than per-core at 16C/node — so when it fits it should win. It wins
    on price through the score; no thumb on the scale is needed (§5.5).

    It is a per-CLUSTER licence and **cannot be stacked across clusters**. A
    6-node result that `recommend._cluster_layout` splits into [3, 3] is a
    capacity split, not two Essentials clusters, and must not qualify — that is
    not the intended use. Primary + DR falls out for free, because a DR cluster
    is a separate sizing and evaluates its own eligibility.
    """
    layout = list(cluster_layout or [])
    if len(layout) != 1:
        if layout.count(exact_nodes) == len(layout) and len(layout) > 1:
            return False, (
                f"{len(layout)} clusters of {exact_nodes} nodes — Essentials is "
                f"per-cluster and cannot be stacked across clusters"
            )
        return False, None

    nodes = layout[0]
    ram = ram_gb_per_node or 0

    if nodes != exact_nodes:
        # Only worth reporting when it is close enough to be actionable.
        if abs(nodes - exact_nodes) == 1:
            return False, (f"{nodes} nodes — Essentials requires exactly "
                           f"{exact_nodes}")
        return False, None

    if ram > max_ram_gb:
        return False, (f"{ram - max_ram_gb} GB/node over the {max_ram_gb} GB "
                       f"Essentials ceiling")

    return True, None


# ── The book ─────────────────────────────────────────────────────────────────

class LicenseBook:
    """A region- and feed-scoped view of licence prices.

    Built once per request from the current feed and passed into the scorer.
    Holds no database session, so the pricing math stays testable with plain
    dicts.

    `bands`: {(edition, term_years, support): {core_band: price}}
    `flats`: {(kind, term_years): price}
    """

    def __init__(self, bands=None, flats=None, region=None, feed_label=None,
                 currency="EUR"):
        self.bands = bands or {}
        self.flats = flats or {}
        self.region = region
        self.feed_label = feed_label
        self.currency = currency

    def __bool__(self):
        return bool(self.bands)

    def band_map(self, edition, term_years, support=DEFAULT_SUPPORT):
        """Bands for one (edition, term, support), falling back across support
        tier. BRS and Video are SS-only, so a PS request there must not yield an
        empty map and a silent zero."""
        key = (edition, clamp_term(term_years), support)
        if key in self.bands:
            return self.bands[key]
        other = SUPPORT_PREMIUM if support == SUPPORT_STANDARD else SUPPORT_STANDARD
        return self.bands.get((edition, clamp_term(term_years), other), {})

    def flat_price(self, kind, term_years):
        return self.flats.get((kind, clamp_term(term_years)))

    def essentials(self, term_years, support=DEFAULT_SUPPORT):
        """Essentials price for a term at the matching support tier.

        Matched to the support tier, NOT to whichever is cheaper — mixing
        Essentials Kit with Premium-support banded licences inside one sizing
        would misprice the deal in a way nobody would spot. Falls back to the
        other tier only when the matching one is absent from the feed.
        """
        kind = ESSENTIALS_BY_SUPPORT.get(support, FLAT_ESSENTIALS)
        price = self.flat_price(kind, term_years)
        if price is not None:
            return price, kind
        for other in ESSENTIALS_KINDS:
            price = self.flat_price(other, term_years)
            if price is not None:
                return price, other
        return None, None


def cluster_license(book, cluster_layout, cores_per_node, ram_gb_per_node,
                    term_years, edition=EDITION_STANDARD,
                    support=DEFAULT_SUPPORT):
    """Licence cost for one sizing, plus the annotations that may cross the wire.

    Returns a dict:
        eur           float | None   total licence cost (None = not priceable)
        basis         "per_node" | "essentials"
        annotations   dict of booleans/strings — NEVER numbers with a currency

    `cores_per_node` is the licensable core count — the P-weighted figure from
    `recommend._effective_cores`, matching how sizing counts cores. E-cores carry
    no Scale licence. Microsoft counts every physical core, but Windows Server's
    16-core-per-server minimum means the distinction never bites at the HE1xx
    scale where hybrid parts appear, so one basis serves both (§5.5).
    """
    layout = list(cluster_layout or [])
    node_count = sum(layout)
    term = clamp_term(term_years)
    ann = {}

    if not book or node_count <= 0:
        return {"eur": None, "basis": None, "annotations": ann}

    bands = book.band_map(edition, term, support)
    per_node, above = band_price(bands, cores_per_node)
    if above:
        ann["license_above_ladder"] = above
    cap_note = cap_annotation(bands, cores_per_node)
    if cap_note:
        ann["license_cap_reached"] = cap_note

    per_node_total = (per_node * node_count) if per_node is not None else None

    eligible, near_miss = essentials_eligibility(layout, ram_gb_per_node)
    if near_miss:
        ann["essentials_near_miss"] = near_miss

    if eligible:
        flat, kind = book.essentials(term, support)
        if flat is not None:
            ann["essentials_eligible"] = True
            # Essentials only wins if it is actually cheaper. It normally is by a
            # wide margin, but a 2-core cluster on a long term could invert it.
            if per_node_total is None or flat < per_node_total:
                ann["essentials_applied"] = True
                ann["essentials_kind"] = kind
                return {"eur": float(flat), "basis": "essentials",
                        "annotations": ann}
            ann["essentials_applied"] = False

    if per_node_total is None:
        return {"eur": None, "basis": None, "annotations": ann}

    return {"eur": float(per_node_total), "basis": "per_node", "annotations": ann}
