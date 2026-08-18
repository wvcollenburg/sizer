# Cosmetic parity with SC//Design

`feature/cosmetic-parity` → `master`
45 files, +2,533 / −539. `pytest tests/` — **203 passed**.

Adopts SC//Design's look and feel across the sizer while leaving the engine,
the sizing logic and the import pipeline untouched. Both tools are ours; the
agreed direction is their presentation on our engine.

Verified against the **live** SC//Design instance, not just its source — several
things we had matched against `mockups/*.html` turned out to be wrong there, and
are corrected here (see *Corrections* below).

---

## What changed

**Foundation** — new `app/static/css/tokens.css` holds the entire design
foundation: `@font-face`, the light and dark palettes, the type scale, radii,
shadows and the element baseline. `style.css` and `admin.css` each carried their
own copy of the palette before, which is exactly how the admin screens drifted a
redesign behind the main app. Both now link `tokens.css` first.

- Raleway self-hosted (`app/static/fonts/`, latin + latin-ext, variable 400–700,
  ~143 KB). Not linked from Google Fonts — we deploy onto self-hosted
  infrastructure that cannot be assumed to reach a CDN. Only the latin subsets
  ship; Raleway has no CJK glyphs, so `ja` falls through to a CJK system stack.
  Re-fetch with `tools/fetch_fonts.py`.
- 18px root (`html { font-size: 112.5% }`), matching theirs exactly, with the
  Tailwind steps resolved against it as `--fs-xs` … `--fs-2xl`.
- Scale Computing blue ramp converted from their OKLCH to hex.

**Shell** — navy sticky nav with the SC logo and their sc-500 → sc-400 gradient
hairline. The account bar becomes nav links **without touching `auth.js`**: it
already emitted destinations left of a divider and identity right, which is
their nav's exact structure.

**Sizing screen** — sticky 420px parameter rail with results beside it, matching
their scenario page. Both rail panels are collapsible and **start collapsed**, so
the screen opens on the recommendations rather than on a wall of controls.

**Components** — buttons, inputs, sentence-case labels, cards, a shared badge
system, segmented tabs, light table headers, dialogs, and one consistent focus
treatment across every control (we had almost no visible focus state before —
this is a real accessibility improvement, not only a cosmetic one).

**Dark mode** — full token set plus a toggle (`app/static/js/theme.js`).

**Exports** — new `app/palette.py` is the single source of colour for the PPTX,
Word, gauge and cluster-diagram exporters, which each carried their own drifting
literals. Brand blues stay pinned to the `template.pptx` theme slots; the UI
neutrals mirror `tokens.css` so a proposal looks like the tool that produced it.

**Borrowed from them, on top of our engine**

- **Assumptions list** beneath the recommendations — ratio, growth, snapshot
  overhead, day-one caps, sizing mode. We held every one of these values and
  never showed them together; it is the single best borrow for defending a
  number in front of a customer. Rendered once per run, not per card.
- **Utilisation bars** carry real quantities per bar ("now: 207 cores · growth +
  snapshot: 126 cores · full cluster: 403 cores") from a new `abs` block on the
  server, rather than a colour key with no numbers.
- **Inline actionable warnings** under a constrained bar, naming the remedy:
  *"During a node failure the vCPU:core ratio rises to up to 3.17:1. Untick
  'Size CPU for full cluster' or raise the target node count."*
- **Inline control descriptions** in the rail, generated from each control's
  existing `data-i18n-title` copy — so the two cannot drift and nothing needed
  retranslating.
- **Toasts** (`app/static/js/toast.js`), replacing 11 blocking `alert()` calls
  on failure paths.
- **Icon row actions** on the project's sizing table: the name opens the sizing,
  and Options / Duplicate / Delete become lucide icons.

---

## Bugs found and fixed along the way

All pre-existing on `master`; none were introduced by this branch.

1. **The project auto-refresh never worked.** `refreshPending` is used in four
   places in `projects.js` and was declared nowhere, so the first `refreshOne()`
   threw `ReferenceError` before the iframe was appended. Nothing was ever
   tracked, nothing reported back, and the progress bar could not leave
   "Recalculating 0 of N…". One `const refreshPending = new Map()` fixes it;
   verified end-to-end (stale → "Recalculating 1 of 1…" → Current).
2. **The `hidden` attribute was being overridden.** `projects.js` correctly set
   `bar.hidden = true` at zero selection, but `hidden` is only `display: none` in
   the UA stylesheet, so `.project-selection-bar { display: flex }` outranked it
   and the whole export toolbar showed on an empty project. There was already a
   one-off workaround for the same bug on `.lang-dropdown`. Fixed once, for
   everything, with `[hidden] { display: none !important }`.
3. **The ratio marker rendered off-scale.** `((currentRatio - 1) / 7) * 100` goes
   negative below 1:1 — a 0.25:1 estate produced −10.7% — and was clamped at the
   top end only, so the marker and its caption sat outside the bar. Clamped at
   both ends.
4. **`--danger` was never defined**, so `.modal-error` had always silently fallen
   back to a hardcoded hex. Repointed at `--red`.

---

## Corrections to earlier assumptions

Logging into the live instance corrected several things taken from their
mockups, which predate their `globals.css`:

| Assumed (from mockups) | Actually |
|---|---|
| Table headers uppercase + letterspaced | **Data tables are sentence case.** Only spec tables use small uppercase |
| Project screens ~960px | `max-w-5xl` = 1152px |
| Nav `#1e2a3a` | `#28293d` |

Measured live and matched: 18px root, Raleway, 420px rail, `#28293d` nav,
`gray-50` page.

---

## Deliberately not done

- **Ranked recommendations are kept.** They show one recommendation; we show
  eight. That is a product difference, not a cosmetic one — the first solution is
  not always the right one and a human has to choose. Their capacity stat tiles
  were skipped for the same reason: four tiles × eight cards is 32 tiles of
  duplicate data, and the pattern only works because they render a single result.
- **Projects list stays cards, not their table** — parked until the merge
  conversation settles whose IA wins, rather than rewriting it twice.
- Feature parity (BOM validator, HCL catalog sync, scenarios-per-project,
  tenants) remains out of scope.

---

## Reviewer notes

- **`app.js` line endings.** It is one of only three CRLF files in the repo and
  scripted edits normalised it to LF, which turned a ~200-line change into a
  7,880-line whole-file diff. It has been restored to CRLF so this diff is
  readable. Normalising line endings repo-wide (with a `.gitattributes`) is worth
  doing — as its own commit, not buried here.
- **Translations.** Five new GUI strings across all 15 locales. Everything else
  reuses strings that were already translated; the advice line, for instance, is
  the existing `results.full_cluster_info_degraded` plus one new remedy clause.
  Worth a native-speaker glance at `results.util.advice_*` and
  `results.assumptions`.
- **`tools/shots.sh`** fetches a headless browser into `.tools/` (gitignored) and
  writes screenshots to `docs/shots/` (also gitignored) for UI review.

## Verification

- `pytest tests/` — 203 passed, including locale parity across all 15 languages.
- Structural checks on all three stylesheets: braces balanced, no undefined
  `var()`, no token defined only inside the dark block, no pre-restyle literals.
- Browser pass in both themes through a real logged-in session: login, project
  home, project view, mode selector, manual entry, the sizing screen with the
  rail and eight recommendation cards, assumptions, toasts, and the refresh loop.

## Follow-ups (separate branch)

Remaining technical issues are deliberately left out of this PR and will be
handled separately.
