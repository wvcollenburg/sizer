# Parity gap analysis — measured against the live SC//Design instance

Revised after logging into the running instance (10.8.12.43:9443) and capturing
its real screens. **This supersedes the earlier version of this document**,
which was based on their source plus the two HTML mockups — and the mockups
turned out to be wrong about several things.

Credentials were supplied by the owner for their own account and passed via the
environment; they are not stored in this repo.

---

## 0. Corrections to what we assumed

| Assumed | Actually |
|---|---|
| Table headers are uppercase + letterspaced (from `mockups/project-tabs.html`) | **Data tables are sentence case.** Their `SortableHeader` is `px-4 py-3 font-medium`, nothing more. Only the *spec* table ("Build": RESOURCE / PER NODE / CLUSTER TOTAL) uses small uppercase. |
| Project screens ~960px | `max-w-5xl` = **1152px**; content 180→1260 at a 1440 viewport, confirmed by measurement |
| Sizing screen needs a wide container | `max-w-7xl` = **1440px** — we are already at 1400px |
| Nav is `#1e2a3a` (mockup) | `oklch(0.289 0.036 281.638)` = **#28293d** — exactly what we shipped |
| Page background | Dashboard wrapper is `bg-gray-50`; `--background` itself is white. Our `--bg: #f9fafb` is right |

Measured live: `html` font-size **18px**, body font-family **Raleway**, rail
width **420px**, nav background **#28293d**. Our Phase 0/1 work matches all four.

---

## 1. Where we now match

Nav bar, palette, Raleway at 18px root, buttons, inputs, labels, cards, badges,
dialogs, focus rings, dark mode, the 420px sticky rail, and the segmented
utilisation bars (their Current / Growth / Usable / Total in sc-700 → sc-400 →
gray-300 → stripe is what we implemented, including the "Primary driver" chip).

---

## 2. Gaps — cosmetic

### A. Our data-table headers are uppercase; theirs are not

Self-inflicted, from the stale mockup. Their rule is:

- **Data tables** (sortable, row-per-record): sentence case, `font-medium`.
  → our `.vm-table th`, `.sizing-table th` should follow.
- **Spec tables** (label / value pairs): small uppercase, letterspaced.
  → our "PER NODE / TOTAL / N-1 AVAILABLE" headers are already correct.

### B. Project view header

Theirs: back text-link → centred title + badge → tab bar. Ours renders
"← All projects" as a **315px-wide grey pill button**, because it is a
`.btn-muted` inside a `flex-direction: column` block that stretches it. Fix the
stretch and make it a link.

### C. Toolbars at full strength when empty

Our project view shows the whole export/selection bar at "0 selected". Theirs
shows count + primary action only.

### D. Container widths in px, not rem

Ours: `1400px` / `1100px`. Theirs: `80rem` / `64rem` → 1440 / 1152 at the 18px
root. Ours are frozen; theirs scale with the type.

---

## 3. Gaps — structural, and more interesting

### E. Their sizing screen leads with the answer, not the controls

Both parameter panels ("Sizing Parameters", "Hardware Filters") are **collapsed
accordions by default**. The screen opens on the recommendation; you expand the
rail only to tune. Ours opens with a fully expanded parameter panel and the
recommendations pushed right.

This is the single biggest felt difference between the two tools, and it is
about three CSS/JS lines plus a decision.

### F. Their controls are self-documenting

Every parameter carries a description sentence underneath:

> **Growth Buffer** ⓘ 20% — *20% typical. Above 30% is aggressive for most
> environments.*
> **vCPU:Core Ratio** — *Common ratios: 2:1 database | 4:1 general | 6–8:1 VDI/DR*
> **Reserve CPU capacity for node failure** — *Ensures enough CPU on remaining
> nodes to run all workloads at full speed if a node fails…*

We put the same knowledge behind ⓘ tooltips. Theirs is better for a partner who
has never used the tool; ours is denser for someone who has. Worth a decision,
and it is content work more than styling.

### G. Patterns we simply do not have

1. **Cluster Capacity stat tiles** — four icon tiles (CPU / Memory / Storage /
   Network) with a big value and a muted sub-line. High-impact summary above
   the detail tables.
2. **Assumptions list** — a plain muted list at the bottom of the
   recommendation: vCPU:core ratio, growth buffer, overheads, RF, IOPS derating,
   read/write ratio. Excellent for defensibility in front of a customer; we hold
   all of these values already and never show them together.
3. **Inline actionable warnings** — a red line directly under the constrained
   bar: *"During node failure: 114% — CPU is overcommitted… Enable 'Reserve CPU
   for node failure' or add a node."* Names the fix, next to the evidence.
4. **Toasts** (sonner, top-right). We have no notification layer.
5. **Absolute values in the bar legend** — "Current: 122 cores · Growth: 24
   cores · Usable: 192 cores · Total: 192 cores". Ours shows a colour key
   without numbers.

### H. One recommendation vs a ranked list

Theirs shows **the** recommendation, tuned via filters. Ours shows **eight**
ranked cards (#1…#8) and lets you pick.

This is a genuine product difference, not a cosmetic one, and ours is arguably
the stronger position for a pre-sales conversation. Do not "fix" this to match
them — but note their single-result layout is why their detail (Build table,
capacity tiles, assumptions) can be so rich: they only have to render it once.

---

## 4. What was done

All of the above is implemented except where noted. `pytest tests/` — 203 passed;
verified in the browser in both themes.

| Gap | Status |
|---|---|
| A — data-table headers | Sentence case on `.vm-table` / `.sizing-table`; spec tables keep small uppercase |
| B — back-link | Now a text link; `.project-title-block` gets `align-items: flex-start` so it stops stretching |
| C — empty toolbars | Fixed at the root: `[hidden] { display: none !important }` in tokens.css |
| D — widths | `80rem` / `64rem` |
| E — rail collapsed | Both panels collapse; the wizard re-expands them when it portals one in as a step |
| F — inline descriptions | Generated from the controls' existing `data-i18n-title` copy |
| G1 — capacity tiles | **Skipped, deliberately** — see below |
| G2 — assumptions | Rendered once beneath the list, 9 rows |
| G3 — inline warnings | Under the constrained bar, with the remedy named |
| G4 — toasts | New `toast.js`; 11 blocking `alert()` calls replaced |
| G5 — legend values | Real quantities per bar, from a new `abs` block on the server |
| H — ranked results | Kept. Owner's call: the first solution is not always the right one, and a human has to choose |

### Notes on three of these

**C was not what it looked like.** `projects.js` already set `bar.hidden = true`
at zero selection. The `hidden` attribute is only `display: none` in the UA
stylesheet, so `.project-selection-bar { display: flex }` outranked it. There
was already a one-off workaround for the same bug on `.lang-dropdown`. One
defensive rule in tokens.css fixes both and any future instance.

**G1 skipped.** Their capacity tiles exist because they render exactly one
recommendation and have room to spare. We render eight. Four tiles per card is
32 tiles of largely duplicate information, and the "Cluster Total" column
already carries it. This gap is a consequence of the H difference, and since we
are keeping H deliberately, the tile pattern does not follow.

**G2 belongs to the run, not the card.** Assumptions are properties of the
sizing run, so they render once beneath the list rather than eight times.

### Translation

Five new GUI strings, translated across all 15 locales:
`results.util.capacity`, `results.util.advice_fix`, `results.util.advice_tight`,
`results.assumptions`, `results.assumption_yes` / `_no`. Everything else reuses
strings that were already translated — the advice line, for instance, is
`results.full_cluster_info_degraded` (existing) plus one new remedy clause, and
the inline descriptions are the `data-i18n-title` tooltips already in place.

## 5. Still open

- Projects list cards → table: parked until the merge conversation settles whose
  IA wins. Rewriting it twice would be waste.
- Feature parity (BOM validator, HCL catalog sync, scenarios-per-project,
  tenants) remains out of scope.
- The inline descriptions make the rail tall. It is collapsed by default, which
  is why the two changes were made together — if the rail is ever expanded by
  default again, revisit.
