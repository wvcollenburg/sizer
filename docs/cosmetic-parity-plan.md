# Cosmetic parity with SC//Design

Layering the competing sizer's look and feel onto our engine.

Branch: `feature/cosmetic-parity`
Source reviewed: `_archive/SC-Sizing-main` (Next.js 15 / React 19 / Tailwind 4 / shadcn "new-york" / Radix / lucide)

---

## 1. What their "look and feel" actually is

Stripped of the framework, their visual identity is a small, portable set of decisions. None
of it requires React.

| Layer | Their choice |
|---|---|
| **Shell** | Sticky full-width nav bar in SC navy, white logo left, text links, user email + Sign out right. Page body on `gray-50`. |
| **Container** | Centred, narrow — `max-width: 960px` (project pages) / `640px` (forms). Not full-bleed. |
| **Typography** | Raleway (Google), root font-size 112.5% (18px), so their `text-sm` lands at ~16px. |
| **Palette** | shadcn slate neutrals + a custom SC blue ramp (`sc-50`…`sc-700`) + `sc-navy`. Declared in OKLCH. |
| **Radius** | `--radius: 0.625rem` (10px); cards `rounded-xl` (~14px), buttons/inputs `rounded-md` (~6px), badges fully round. |
| **Cards** | `bg-card` + 1px border + `shadow-sm` + generous 24px padding. Very flat — shadow is a whisper, the border does the work. |
| **Buttons** | Solid primary (SC blue), `outline`, `secondary`, `ghost`, `link`. Height 36px, `text-sm`, `font-medium`. 3px focus ring at `ring/50`. |
| **Inputs** | 36px, 1px border, `shadow-xs`, focus = border colour change + 3px translucent ring. |
| **Badges** | Pill, `text-xs font-medium`, pastel-bg/dark-text pairs (`green-100/green-800` style) for PASS/FAIL/pending. |
| **Tabs** | Underline style — 2px bottom border in SC blue on the active tab, muted grey otherwise, with count pills. |
| **Tables** | Card-wrapped, `f9fafb` header, uppercase 12px letterspaced column labels, hairline row dividers, hover tint. |
| **Icons** | lucide, 16px, inline with text at `gap-2`. |
| **Dark mode** | Full token set defined (`.dark`), driven by `next-themes`. |

Their brand ramp, converted from OKLCH to hex for us:

| Token | Hex | Used for |
|---|---|---|
| `sc-navy` | `#28293d` | nav bar |
| `sc-700` | `#194f90` | emphasis text, filled bar segments |
| `sc-600` / `--primary` | `#2d5f99` | primary buttons, active tab, links |
| `sc-500` / `--ring` | `#3d72ad` | focus ring |
| `sc-400` | `#6a96cb` | secondary bar segments, node-card border |
| `sc-300` | `#a1bddf` | |
| `sc-100` | `#dae5f2` | "Primary driver" chip bg |
| `sc-50` | `#f0f5fa` | node-card bg |
| `--foreground` | `#020618` | body text |
| `--muted-foreground` | `#62748e` | secondary text |
| `--border` / `--input` | `#e2e8f0` | all hairlines |
| `--muted` / `--secondary` | `#f1f5f9` | subtle fills |
| page background | `#f9fafb` (gray-50) | body |
| `--destructive` | `#e7000b` | |

Note their mockups (`mockups/*.html`) use slightly different hand-picked hexes (`#1e2a3a` nav,
`#3a5a9e` primary). **The `globals.css` values above are the shipped truth** — the mockups are older.

---

## 2. Why this is cheap for us

I measured our front end against the job:

- **Our markup is already component-classed, not utility-classed.** `.config-form`, `.result-card`,
  `.modal-content`, `.btn`/`.btn-primary`/`.btn-soft`/`.btn-ghost`, `.form-group`, `.text-input`,
  `.mode-btn`, `.auth-tab` — every visual concept already has a hook.
- **Our CSS is already tokenised.** 417 `var(--…)` usages vs 159 raw hex literals in
  [style.css](app/static/css/style.css), and most of those literals are state colours (warn amber,
  danger red, gold) plus 28 `#fff`. Swapping the `:root` block moves the majority of the UI in one edit.
- **Almost nothing is styled from JavaScript.** 9 inline `style="` across all of `app.js`,
  `projects.js`, `wizard.js`, `auth.js` combined, and zero hardcoded hex in `app.js`.

**Conclusion: this is a stylesheet project, not a rewrite.** We should not port a single React
component. `app.js` (189KB of behaviour), `recommend.py`, `calc.py`, the wizard, i18n, exports —
all untouched. The plan below touches [style.css](app/static/css/style.css), the header/shell block
of [index.html](app/templates/index.html), and adds font + logo assets.

---

## 3. What was built

All phases were approved and are implemented on `feature/cosmetic-parity`.
`pytest tests/` — 203 passed.

### Phase 0 — Foundation

- **Raleway self-hosted** at `app/static/fonts/` (latin + latin-ext, variable
  400–700, ~143KB). Not linked from Google Fonts: we deploy onto self-hosted
  infrastructure that can't be assumed to reach a CDN. Only the latin subsets
  ship — Raleway has no CJK glyphs, so `ja` falls through to a CJK system stack
  declared in `body`. Re-fetch with `tools/fetch_fonts.py`.
- **SC logos** copied to `app/static/img/`.
- **`app/static/css/tokens.css`** is new and holds the whole design foundation:
  `@font-face`, the light palette, the dark palette, the type scale, radii,
  shadows and the element baseline. `style.css` and `admin.css` both consumed
  their own copy of the palette before — which is precisely how the admin
  screens drifted a full redesign behind the main app. Every template links
  `tokens.css` first.
- **18px root adopted** (`html { font-size: 112.5% }`), matching theirs exactly,
  with the Tailwind steps resolved against it as `--fs-xs` … `--fs-2xl`.

### Phase 1 — Shell

- Navy sticky nav with the white SC logo, the product name as a muted wordmark,
  and their sc-500 → sc-400 gradient hairline.
- The account bar is restyled into nav links **without touching `auth.js`** —
  it already emitted destinations left of a divider and identity right, which is
  their nav's exact structure.
- **Sticky left parameter rail** on the sizing screen (420px, `top: 6rem`,
  own scroll), with recommendations beside it. Three implementation notes worth
  keeping:
  - The grid lives on a new `.sizing-inner` **inside** `#sizing-results`, not on
    the section itself, because `app.js` and `wizard.js` drive that section's
    visibility with an inline `style.display` that would overwrite a
    `display: grid`.
  - `.sizing-rail` / `.sizing-output` split on exactly the seam the guided
    wizard already portals across (parameters → step 5, recommendations →
    step 6). Portals restore to whatever parent they were captured under, so
    the new wrappers are transparent to it.
  - Multi-site "Selected clusters" review hides everything in the rail, so a
    `:has()` rule collapses the grid to one column rather than leaving a 420px
    void. Without `:has()` support the rule is dropped and the layout stays
    two-column, which is the right fallback.

### Phase 2 — Component primitives

Buttons (`h-9`/`h-8`, `--radius-sm`, weight 500), inputs (36px, white fill,
3px focus ring), labels (**sentence case** — our uppercase letterspaced labels
were the loudest single tell that this was a different tool), cards, a shared
badge system, segmented tabs, light table headers (`.vm-table th` was a filled
navy block), and dialogs.

A **single focus treatment** now applies across every control. We had almost no
visible focus state before; theirs is both on-brand and a real accessibility
improvement.

### Phase 3 — Screens and utilisation bars

- Mode selector → their left-aligned "action card".
- **Utilisation bars** recoloured onto their ramp and geometry (`h-2.5`,
  `rounded-full`, sc-700 current fill, sc-300/sc-400 hatches, slate track) with
  their "Primary driver" chip. The *segmentation* is unchanged: current / N-1
  reserve / replication reserve / free / HA reserve is richer than their
  four-part split and is the sizing model talking.
- Threshold colouring is kept for the current-demand fill. At normal load the
  bar is their flat blue, so the common case matches theirs exactly; amber and
  red only appear when the engine has a finding to report.
- ~55 ad-hoc pastel literals across the stylesheet collapsed onto the shared
  semantic pairs.

### Phase 4 — Dark mode, admin, icons

- **Dark mode**: their full `.dark` set mapped onto our names under
  `:root[data-theme="dark"]`, applied by `app/static/js/theme.js` (loaded in
  `<head>` so there is no light flash; a separate file, not inline, because the
  CSP is `script-src 'self'`). Explicit choice beats the OS setting, and with no
  choice stored it follows the OS live.
  Two families **flip** rather than darken: the pale end of the SC ramp, and
  each state colour with its `-bg` partner — both are used as fill/ink pairs.
  A dedicated `--accent-hover` exists because their `bg-primary/90` composites
  toward the background, so hover lightens on white and darkens on the dark
  ground.
- **Admin** now shares `tokens.css`, gets the same navy bar and dark mode, and
  its dead `var(--card-bg)` fallback is fixed.
- **Icons**: our SVGs were already lucide-shaped; stroke widths normalised to
  lucide's 2, the 16 `&times;` close glyphs replaced with lucide `x`, and
  `.info-icon` redrawn as an outlined ring approximating lucide `circle-help`
  (CSS rather than a swap, to avoid churning ~18 call sites that pass tooltip
  text via `title`).
- **New i18n keys** `header.theme` and `header.language`, translated across all
  15 locales. `data-i18n-aria` support was added to `i18n.js` — icon-only
  controls carry their whole accessible name in `aria-label`, and ours were
  hardcoded English regardless of locale.

### Phase 5 — Exports

**`app/palette.py`** is the single source for the Python side, mirroring
`tokens.css`. `export_pptx.py`, `export_docx.py`, `export_gauges.py` and
`cluster_diagram.py` all read from it.

The important finding: the exporters' **brand blues were already correct** —
`009ADE`, `113859`, `194F90` are pinned to the theme slots of
`resources/template.pptx`, and SC//Design's own exports use the same blues.
Changing them would desync the deck from the corporate template, so they are
documented as fixed. What actually differed was the **neutrals and semantic
pastels**, which now come from the app's palette.

`export_gauges.py` mattered most — its header said "kept in sync with style.css
.util-* rules" while still carrying the pre-restyle palette.

One bug caught by rendering a gauge and looking at it: the mid-load fill and the
replication band had both landed on `#b45309`, so a resource that is 70–90%
loaded *and* carries replication reserve drew two adjacent bands in one hue.
Mid-load is now orange `#c2410c`, replication gold `#a16207`.

## 4. Verification

- `pytest tests/` — **203 passed**, including `test_i18n_parity` and
  `test_frontend_wiring` (which resolve the new locale keys and the
  `toggleTheme` handler).
- All three stylesheets: braces balanced, no undefined `var()`, no token
  defined only inside the dark block, no pre-restyle colour literals left.
- Gauge PNGs and cluster SVGs rendered and inspected directly.
- **Browser pass done**, light and dark, through a real logged-in session:
  login, project home, mode selector, manual entry, and the sizing screen with
  the sticky rail, 8 recommendation cards and 24 utilisation bars.

Screenshots need `chrome-headless-shell` — regular Chrome cannot start under
the agent sandbox (its ProcessSingleton needs a unix-socket `bind()`, and the
headless shell needs a Mach port bootstrap). `tools/shots.sh` fetches the binary
into `.tools/` (gitignored) and writes PNGs to `docs/shots/` (also gitignored).

### Defects the browser pass caught

None of these were visible to the structural checks:

1. **Card titles still coloured** — "Sizing Options" was orange and "Growth
   Projection" green, left over from the accent stripes removed in Phase 1.
2. **Ratio row overflowed the rail by 38px**, clipping the ":1" off the readout.
   Label, slider and value do not fit across 420px; the label now takes its own
   line.
3. **"CURRENT" caption collided with the ratio value** — the marker's `::after`
   is positioned above the bar and had no headroom reserved.
4. **Uppercase label survived** on the ratio slider.
5. **Three-hue ratio bar** (green→orange→red) read as a warning scale; it is a
   position indicator, so it now uses the brand ramp.
6. **"IN DEVELOPMENT" tag was unreadable in dark** — it fills with `--orange`
   but inked with `--on-accent` (near-white), which collided once `--orange`
   inverted to light amber. Ink is now the `--orange-bg` partner.
7. **Best-pick rank disc was green**, the last off-palette marker on the screen.

8. **The rail scrolled horizontally.** `.proj-grid` was a hard
   `repeat(3, 1fr)`; three un-shrinkable cards overflowed the 420px rail by
   217px. Because a vertical `overflow` makes the horizontal one `auto` too,
   that put a scrollbar across the whole rail and made the replication fields
   *look* clipped when they were merely scrolled out of view. Now
   `repeat(auto-fit, minmax(min(100%, 13rem), 1fr))`.
9. **Ratio marker rendered off-scale** — `((currentRatio - 1) / 7) * 100` is
   negative for any environment below 1:1 (a 0.25:1 estate gave -10.7%), and it
   was clamped at the top end only. The marker and its "current" caption sat
   outside the bar. Now clamped at both ends in `app.js`. This one is a
   correctness bug, not a cosmetic one: the marker was misreporting position.

Two of these were self-inflicted mid-fix and caught on the next screenshot: a
`min-width: 8rem` floor that let the projection cards squeeze to ~70px per
column, and an `overflow-wrap: anywhere` that then shattered "1.19 TiB" into one
character per line. Fixing overflow by letting content shrink further is usually
the wrong direction — give the content a workable floor and let the *container*
reflow instead.

The pattern in 6 is worth generalising: **any element that paints a token as its
fill must ink itself with that token's partner**, never with a fixed light or
dark value, or it breaks in one of the two themes.

## 5. Notes for next time

- `lang/*.js` are JS, not JSON — the first `{` belongs to `|| {}` in the
  assignment header, so never slice on it.
- Rebuild + recreate the container for JS/CSS changes on testenv; a bind-mount
  won't pick them up.
- Feature parity (BOM validator, HCL catalog sync, scenarios, tenants) remains
  out of scope. This changed how the tool looks, not what it does.
- Their `docs/ux-review/` contains a persona-driven friction-scoring
  methodology that is worth stealing separately from any of this.
