# Export work plan — C2 (PDF/Excel/CSV) + C4 (cluster visualization)

_Prepared overnight 2026-06-23. Branch: `feature/UI-and-engine-updates`._

## What's already done (tonight)

- **PPTX export now derives from `resources/template.pptx`.** `export_pptx._new_deck()`
  loads the branded SC template, strips its 4 sample slides (dropping both the
  `sldId` and the relationship — otherwise orphaned slide parts collide as
  "Duplicate name: slide1.xml" on save), and our content draws on the template's
  `BLANK` layout. Graceful fallback to a plain deck if the template is missing.
- **Colours remapped to the template's exact theme palette** (accent1 `009ADE`,
  dk2 `113859`, accent2 `194F90`, accent5 `97CAEB`, accent4 `3FB748`, lt2
  `E9EAF0`). Fonts now inherit Arial from the theme.
- **Dockerfile** updated to `COPY resources/ /resources/` so the template is
  present in the container (it wasn't before — exports would have silently fallen
  back to unbranded).
- Verified end-to-end: 4-slide proposal + 1-slide config generate cleanly, no
  duplicate-name warnings, content intact, theme = Arial. Samples:
  `/tmp/sizer_proposal_sample.pptx`, `/tmp/sizer_config_sample.pptx`.

### Known tradeoff to confirm
- Output decks are **~8 MB** because the template's embedded media (section
  dividers, intro/outro art) rides along even though we only use BLANK. Options:
  (a) accept it (normal for a branded corporate deck), or (b) later strip unused
  masters/layouts/media to slim exports. Recommend (a) for now.

---

## Decisions needed from you (top of the morning)

1. **PDF technology** — three viable paths, tradeoffs below. My recommendation:
   **WeasyPrint (HTML→PDF)** for cohesion with the web UI, *unless* you want the
   PDF to be a pixel-exact copy of the PPTX deck (then LibreOffice convert).
2. **PDF style** — "document/report" (flowing A4/Letter pages, our web look) vs
   "slide deck mirror" (same as the PPTX). WeasyPrint → document; LibreOffice →
   deck mirror.
3. **Cluster viz rendering** — shared **SVG** (one diagram, reused in web + PDF,
   rasterized for PPTX) vs **native PPTX shapes** (crispest in the deck, separate
   web/PDF renderer). Recommendation: shared SVG (see C4).
4. **Container weight** — are cairo/pango system deps (WeasyPrint/cairosvg) OK in
   the image, or keep it lean with pure-python (reportlab + native shapes)?

---

## C4 — cluster visualization (do FIRST or in parallel; feeds the exports)

Goal: a node/rack diagram + capacity bar for a recommendation, shown in the web
recommendation card AND embedded in the PPTX/PDF proposal.

**Core idea: one spec, rendered to the surfaces that need it.**

- `cluster_diagram_spec(rec)` → pure data: list of nodes (model, cores, RAM,
  storage, role = HCI / storage-only / N+1-spare), per-cluster grouping, rack
  units, capacity totals. No rendering — just the shape of the picture.
- Renderers:
  - **SVG** (`render_cluster_svg(spec)`) → string. Used directly in the web card
    and inlined into the WeasyPrint PDF. Resolution-independent, themeable with
    the same palette.
  - **PPTX**: either (a) rasterize the SVG → PNG via `cairosvg` and
    `slide.shapes.add_picture`, or (b) draw native shapes (rounded-rect nodes,
    dashed N+1 spare, capacity bar). Native = crisper/editable; SVG-raster = one
    visual everywhere. If we pick WeasyPrint, cairo is already present so cairosvg
    is nearly free → prefer SVG-raster for consistency.

**MVP scope**: per-node tiles (model + cores/RAM/storage), N+1 spare visually
distinct (dashed/lighter), multi-cluster grouped, a capacity summary bar
(usable CPU/RAM/storage after overhead + N+1). Rack view is a nice-to-have v2.

**Reuse**: the utilization "now/sized/HA-reserve" bars (B5) and the diagram share
the brand palette; keep a single source of palette constants.

---

## C2 — PDF / Excel / CSV

### PDF (the hard one)

| Option | Pros | Cons |
|---|---|---|
| **WeasyPrint** (HTML/CSS → PDF) | reuses our CSS + utilization bars + SVG diagram; clean document feel; moderate deps | needs cairo/pango in image (~tens of MB); build an HTML proposal template |
| **LibreOffice** `--headless --convert-to pdf` of the PPTX | pixel-exact to the branded deck; the C4 viz comes free | heavy (~400 MB in image); slower; flakier headless |
| **reportlab** / fpdf2 (pure-python) | no system deps; portable on slim base | rebuild all layout by hand; most code |

**Recommendation:** WeasyPrint. Build `export_pdf.py` with an HTML/Jinja proposal
template (cover, current env, workload, BOM table, utilization bars as HTML, the
SVG cluster diagram, assumptions, warnings, notes). Reuses the visual language we
already have on the web. Add `weasyprint` + apt deps to the image.
Fallback consideration: if image weight is a hard no, go reportlab.

### Excel (easy — openpyxl already a dependency)

`export_xlsx.py` with sheets:
- **Summary** — project/customer, current platform, headline recommendation.
- **Build (BOM)** — per-node specs, node count incl. N+1, replication factor.
- **Utilization** — CPU/RAM/storage/IOPS now vs sized vs HA reserve.
- **Assumptions** — vCPU:core ratio, OS overhead, growth, snapshot, RF.
- **Workload** — per-VM rows (from import/manual), excluded rows flagged.

### CSV (trivial — stdlib `csv`)

BOM line items only (one row per node-type/qty), for quick import into quoting
tools. Single route, no styling.

### Routes
Mirror the existing PPTX routes: `/api/export-pdf`, `/api/export-xlsx`,
`/api/export-csv` (POST the same `result`/`summary`/`recommendation`/`projection`
payloads). Add format buttons next to the existing "Export PPTX".

---

## Suggested sequencing

1. **C4 cluster viz** — spec + SVG renderer + web card embed + PPTX embed. High
   demo value, makes the now-branded deck land.
2. **C2 Excel + CSV** — independent, low risk, openpyxl already present. Quick win.
3. **C2 PDF** — once PDF tech is chosen; reuse the C4 SVG + web CSS.

## Dependencies summary
- Already have: `openpyxl` (xlsx), `python-pptx`, stdlib `csv`.
- Add for the recommended path: `weasyprint` (+ apt: libpango/libcairo) and
  `cairosvg` (PPTX raster of the SVG). Both lean on cairo, so they share deps.
- Pure-python alternative (no system deps): `reportlab` + native PPTX shapes.
