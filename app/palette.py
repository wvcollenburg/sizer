"""The one place the exporters get their colours.

Every exporter used to carry its own literals, so the PPTX, the Word document,
the cluster diagram and the utilisation gauges each drifted to a different set
of greys and a different green. This module holds the canonical values and each
exporter converts them into whatever its library wants (python-pptx RGBColor,
python-docx RGBColor or hex fills, SVG strings, PIL strings).

Two families, with different rules:

**Brand** — the Scale Computing blues. These are pinned to the theme slots of
resources/template.pptx (dk1/dk2/lt2/accent1..6). Changing one here without
changing the template desynchronises exports from the corporate deck, so treat
them as fixed. SC//Design uses the same blues in its own exports.

**UI** — neutrals and semantic pastels, mirroring app/static/css/tokens.css so a
proposal looks like the tool that produced it. These are the values to change if
the web palette moves; keep the two files in step.

Values are uppercase hex without the leading '#', which is what the OOXML fill
attributes want; use css() / rgb() for the other forms.
"""

# ── Brand (locked to resources/template.pptx theme slots) ───────────────────
SC_BRIGHT_BLUE = "009ADE"   # accent1 — primary SC blue
SC_DARK_NAVY = "113859"     # dk2 — title bars, headings
SC_DEEP_BLUE = "194F90"     # accent2 — secondary accent, rules (web --sc-700)
SC_LIGHT_BLUE = "97CAEB"    # accent5 — "SC//" prefix on dark grounds
SC_MID_BLUE = "6A96CB"      # web --sc-400; used where a lighter brand blue reads better
CHARCOAL = "272727"         # dk1
TEMPLATE_GREEN = "3FB748"   # accent4
TEMPLATE_ORANGE = "F78D2C"  # accent6

# ── UI (mirrors app/static/css/tokens.css) ──────────────────────────────────
WHITE = "FFFFFF"
SURFACE = "FFFFFF"          # --surface
SURFACE_2 = "F1F5F9"        # --surface2 — subtle fills
SURFACE_3 = "F8FAFC"        # --surface3 — table headers
BORDER = "E2E8F0"           # --border
BORDER_STRONG = "CBD5E1"    # --border-strong
TEXT = "020618"             # --text
TEXT_MUTED = "62748E"       # --text-muted
TEXT_SUBTLE = "94A3B8"      # --text-subtle

# Semantic pairs: ink first, then the pastel it sits on.
GREEN = "166534"
GREEN_BG = "DCFCE7"
ORANGE = "854D0E"
ORANGE_BG = "FEF9C3"
RED = "991B1B"
RED_BG = "FEE2E2"
INFO = "1D4ED8"
INFO_BG = "DBEAFE"

# The SC blue ramp, for anything that shades by depth (gauges, diagrams).
SC_50 = "F0F5FA"
SC_100 = "DAE5F2"
SC_300 = "A1BDDF"
SC_400 = "6A96CB"
SC_500 = "3D72AD"
SC_600 = "2D5F99"
SC_700 = "194F90"

# ── Utilisation bars ────────────────────────────────────────────────────────
# The print counterpart of the .util-* rules in style.css. Keep the two in
# step: the gauge PNG and the on-screen bar are meant to be the same picture.
UTIL_NOW_LOW = SC_700        # < 70% load — their flat sc-700 "current" fill
UTIL_NOW_MID = "C2410C"      # 70-90% — orange, distinct from the gold rep band
UTIL_NOW_HIGH = "B91C1C"     # > 90%
UTIL_RESERVE_HATCH = (SC_300, SC_100)     # growth + snapshot, +45deg
UTIL_REPLICATION_HATCH = ("A16207", "FDE047")  # replication (DR) reserve, +45deg gold
UTIL_HA_HATCH = (SC_400, SC_100)          # HA failover reserve, -45deg
UTIL_TRACK = BORDER          # free / unused
UTIL_HAIRLINE = BORDER_STRONG


def css(value):
    """'009ADE' -> '#009ade', for SVG and PIL."""
    return "#" + value.lower()


def rgb(value):
    """'009ADE' -> (0, 154, 222), for RGBColor(*rgb(...))."""
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
