# Projects — Implementation Plan

Branch: `feature/projectsizing`

A **project** is a container for several sizings built for one customer
engagement — typically several LiveOptics / RVTools imports plus hand-built
systems — that are compared, rolled up, and exported together.

## 0. Terminology (read first)

Three levels, strictly separated. The existing code calls level 2 and level 3
both "multi-site", which this plan fixes (§7.4).

- **Project** — NEW. A named container owned by a user, visible to their
  organization, holding many sizings.
- **Sizing** — an existing saved `Configuration`: one import (or manual entry)
  with its options and its recommendation. Already has a name and a share code.
- **Source cluster** — a cluster *inside* one sizing, from the source data's
  `Cluster` column, when "size each cluster separately" is on. See
  `docs/multi-site-plan.md` §0; unchanged by this feature.

An **output cluster** (how `recommend.py` splits a node count into appliance
clusters of ≤ `max_nodes_per_cluster`) is unrelated to all of the above and is
never a selectable unit.

---

## 1. Decisions (locked)

| # | Decision | Choice |
|---|---|---|
| 1 | Where export numbers come from | Store the calculated result with the sizing; validate it with a fingerprint (§3) |
| 2 | Is a project mandatory | Yes — with an auto-created personal **scratch** project as the quick path |
| 3 | Alternatives vs additive | Explicit `role` on each sizing; tags stay freeform |
| 4 | Default role for a new sizing | Unset; **ask once** when the second sizing is added, then use that as the project default |
| 5 | Bundle output | One combined document by default; "download all" zip as an extra |
| 6 | Catalog fingerprint scope | Only the catalog rows that sizing actually used |
| 7 | Stale sizings | Recalculate automatically on project open, with progress |
| 8 | How recalculation runs | One hidden iframe per sizing |
| 9 | Sizing-math changes | Content hash of the sizing source files |
| 10 | Tag storage | Real tag records, scoped **per project** |
| 11 | Sharing | One project share code; organization visibility as today |
| 12 | Export selection unit | The sizing, expandable to its source clusters |
| 13 | First version includes | Duplicate sizing, comparison view, customer details on cover, source provenance |
| 14 | Pre-existing saved sizings | Migrated into a per-owner "Unfiled" project |
| 15 | Deleting a project | Soft-deletes the project and its sizings together |
| 16 | Assumption consistency | Warn on differing tunables; do **not** inherit project defaults |
| 17 | Project lifecycle status | None — sort by last touched |
| 18 | Slow bundle exports | Background job with progress |
| 19 | Naming | Rename internals to Project / Sizing / Cluster |
| 20 | Export artifact retention | 24 hours, swept by the daily purge |
| 21 | Sizings per project | No cap; only the per-bundle section limit |
| 22 | Rights granted by a project code | Read-only; editing copies the sizing into the viewer's own project |
| 23 | Comparison scope | One project at a time |
| 24 | Project creation dialog | Name only; everything else optional and editable later |
| 25 | Export language | Project remembers its creation language; on export, if the UI language differs, ask which to use |
| 26 | Free-text description | Yes, on the project |
| 27 | Salesforce opportunity link | Scale-user field only, enforced server-side and never rendered into an export |
| 28 | Waiting for a bundle | Optional "email me when it's ready" — a link back to the app, to the user's own verified address; plus an always-present Exports panel |
| 29 | Replication partners | A cluster may replicate to a cluster in **another sizing**, within one project only (§8.5) |
| 30 | Workload-less DR sizings | Supported: a sizing can exist purely as a replication target, sized from what replicates into it |
| 31 | Source changes | The target's fingerprint includes the source's demand, so editing a source marks its DR targets stale |
| 32 | Deleting a replication target | Blocked while anything replicates to it; deleting the whole project still cascades |

---

## 2. Data model

### 2.1 New tables

```
projects
  id, code (VARCHAR(12) unique, indexed)      -- mirrors Configuration.code
  name                                        -- the ONLY field asked at creation
  customer_name, opportunity_ref, prepared_by -- optional, for the export cover
  description TEXT                            -- optional free text (decision 26)
  lang VARCHAR(5)                             -- UI language at creation (decision 25)
  salesforce_url TEXT                         -- scale-only, see §9 (decision 27)
  owner_id → users.id, tenant_id → tenants.id -- denormalised, as Configuration
  is_scratch BOOLEAN                          -- the per-user quick-sizing project
  default_role VARCHAR(12)                    -- 'alternative' | 'additive' | NULL (not yet asked)
  is_deleted, deleted_at, deleted_by_user_id
  created_at, updated_at
  INDEX (tenant_id, is_deleted), INDEX (owner_id, is_deleted)

project_tags
  id, project_id → projects.id, name, color
  UNIQUE (project_id, name)                   -- tags are project-scoped (decision 10)

configuration_tags
  configuration_id → configurations.id, tag_id → project_tags.id
  PRIMARY KEY (configuration_id, tag_id)

export_jobs                                   -- §7.2
  id, user_id, project_id, format, status, progress, sizing_ids JSON,
  filename, error, created_at, finished_at, expires_at

scale_project_links                           -- mirrors ScaleConfigLink
  id, user_id, project_id, linked_at
```

### 2.2 Columns added to `configurations`

```
project_id → projects.id   NOT NULL after migration, indexed
position                   INTEGER, ordering within the project
role                       VARCHAR(12) 'alternative' | 'additive' | NULL
notes                      TEXT        -- "why this option", lands in the export
result_snapshot            JSON        -- §3.1
result_fingerprint         VARCHAR(64) -- §3.2
result_computed_at         TIMESTAMPTZ
source_meta                JSON        -- §8
```

### 2.3 Migration

Additive `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements in
`_migrate_schema()` ([seed.py:91](../app/seed.py#L91)); new tables come from
`db.create_all()`. Postgres-only, as the rest of that path is — tests build
schema with `create_all()` on SQLite (`[[seed-migrate-postgres-only]]`).

One-off backfill in the same function, guarded by "only if any configuration
has `project_id IS NULL`":

1. For each distinct `owner_id` among unfiled configurations, create a project
   named `Unfiled`, `is_scratch = true`, owner's `tenant_id`.
2. Set `project_id` on that owner's configurations, `position` by `updated_at`.
3. Leave `role` NULL — decision 4 asks when a second sizing appears.

**Soft-deleted configurations must be included.** They are still rows, they stay
visible to super admins, and they are restorable — so a backfill that only
covers `is_deleted = false` leaves NULLs behind and the `NOT NULL` constraint
fails on the next boot. File them in the same owner's Unfiled project.

`result_snapshot` stays NULL for migrated rows: they are simply stale and get
recalculated on first open (§4).

---

## 3. Result cache and staleness

The current save payload is **inputs only**; results are recomputed in the
browser by `restoreSizingState()` → `calculate()`
([app.js:3490](../app/static/js/app.js#L3490)). Projects need results without
opening each sizing, so results are stored — and guarded by a fingerprint so a
stored result can never silently disagree with the current engine or catalog.

### 3.1 Snapshot shape

Exactly what the existing bundle exports already consume — `{summary,
recommendation, projection, source_perf}` per cluster ([app.py:568](../app/app.py#L568)) —
plus the references needed to fingerprint it:

```json
{
  "clusters": [{ "name": "...", "summary": {...}, "recommendation": {...},
                 "projection": {...}, "source_perf": {...},
                 "refs": { "mode": "appliance",
                           "model_id": 4, "cpu_id": 31, "ram_id": 7,
                           "nic_id": 2, "drive_ids": [11, 14],
                           "drive_type_iops": ["nvme", "ssd"],
                           "storage_config_id": 9 } }],
  "totals": { ... }
}
```

`refs` carries **identity, not values** — the model's name, the description of
the chosen CPU option, the selected sizes. The hash is taken over what that
identity resolves to in the catalog *now* (§3.2). Storing the resolved values
instead would be self-referential: they never change, so nothing could ever go
stale.

`refs` is **mode-dependent**. Validated (software-only) sizings turn out to need
no catalog identity at all: `calculate_validated()` runs on the disk specs,
core counts and clocks posted with the request, and reads no catalog row, so
their refs are simply `{"mode": "validated"}` and their fingerprint rests on the
engine and tunables. That is correct rather than a gap — a catalog edit cannot
move a number that was never read from the catalog. (The catalog still
constrains what the *picker* offers; it just doesn't feed the maths.) Manual
sizings are the same: `{"mode": "manual"}`.

Written by the client on save (it already holds every one of these values) and
on refresh (§4). The server computes and stores the fingerprint — never the
client. An unknown or absent `mode` is treated as permanently stale rather than
silently trusted.

### 3.2 Fingerprint

`sha256` over three parts, concatenated:

1. **Engine** — content hash of `recommend.py`, `models.py`, `cluster_split.py`,
   `storage_only.py`, `tunables.py`, `cpu_benchmarks.py`, computed once at import
   time in a new `app/engine_version.py`. Automatic, so a bug fix in the sizing
   math can't be forgotten (decision 9). A comment-only edit triggers a harmless
   mass refresh.

   **Caveat: not all the math lives in those files.** `calculate_validated()`
   and the appliance calculator sit in [app.py:968](../app/app.py#L968) and
   around it, mixed in with the routes. Hashing all of `app.py` would invalidate
   every stored result on an unrelated route tweak. Two ways out, both fine:
   hash those functions' source individually with `inspect.getsource`, or move
   the calculators into their own module during step 1 of the build order. The
   module move is cleaner and the rename step is already touching this area.
2. **Tunables** — hash of all `SizingSetting` rows sorted by key
   ([orm_models.py:142](../app/orm_models.py#L142)). Global and rare; any change
   genuinely alters every result.
3. **Catalog** — for each id in `refs`, a hash of that row's value columns (not
   `updated_at`, which the catalog tables don't have). Adding a 626th CPU
   changes nothing; editing the Edge node's spec invalidates only sizings that
   used it (decision 6).

Cache row hashes in a process-local dict keyed by `(table, id)`, cleared by the
admin write paths in [admin_routes.py](../app/admin_routes.py). Recomputing 20
sizings' fingerprints on project open is then a handful of cached lookups.

`GET /api/projects/<id>` returns `stale: true|false` per sizing (fingerprint
mismatch, or no snapshot at all).

### 3.3 What the fingerprint deliberately does not cover

**Parser fixes do not invalidate anything.** The imported summary lives inside
the saved payload; refresh replays that stored summary through the engine and
never re-reads the source file. So a LiveOptics or RVTools parser correction —
exactly like commit `6dae54f`, which fixed a RAM label being misread — leaves
every already-saved sizing carrying the old, wrong input, and no amount of
recalculation will fix it. The source file isn't stored, so the app cannot fix
it either.

Rather than pretend otherwise, hash `liveoptics.py` and `rvtools.py` separately
as a **parser version**, stored alongside the result. A mismatch marks the
sizing "imported with an older reader — re-import to pick up the correction",
which is a distinct state from stale: it is a prompt to a human, not something
auto-refresh can clear. Without this, a parser fix silently leaves bad numbers
in projects that look perfectly current.

---

## 4. Refresh (hidden iframe)

The calculate path is DOM-driven over module-level globals (`importVms`,
`sourceClusters`, `clusterOptions`, `activeCluster`, `drCluster` —
[app.js:66](../app/static/js/app.js#L66)), so running it repeatedly in one page
would bleed state between sizings. Instead:

1. Project view opens → server reports which sizings are stale.
2. For each, the project page creates a hidden `<iframe src="/refresh?config=<id>">`
   (same origin, so the session cookie applies), one or two at a time.
3. That page loads the catalogs, calls `restoreSizingState(payload)`, waits for
   calculation to settle, and `postMessage`s `{clusters, refs}` to the parent.
4. Parent `PUT`s it to `/api/configs/<id>/result`; server fingerprints and
   stores. The iframe is destroyed — globals go with it.
5. Progress bar: "Refreshing 3 of 7".

**Failure path** (needed, since refresh is automatic): a 30 s timeout or a
postMessage error marks the sizing `refresh_failed`. It is excluded from the
bundle with a visible reason rather than blocking the export forever. The
`message` handler must check `event.origin` and ignore anything else.

**A read-only viewer must still be able to refresh.** Under decision 22 a
project opened by code is read-only — but if that also blocked the result write,
a partner opening a shared project full of stale sizings could never compare or
export it, since refresh is the only path to current numbers. So
`PUT /api/configs/<id>/result` is allowed for **anyone who can see the sizing**,
and is not an edit: it accepts only `clusters` + `refs`, never payload, name,
tags or role, and the server re-derives the fingerprint itself. Results are
derived data; the user content stays locked.

**Refresh is throttled and lazy.** Each refresh fetches that sizing's full
payload, which can approach the 4 MB cap, and then does a full in-browser
calculation. On a large project, refreshing everything on open would be minutes
of work nobody asked for. So: refresh the rows on screen first, at most two at a
time, and always force-refresh the selected sizings before a comparison or an
export rather than assuming the open-time pass finished.

---

## 5. Project UI

### 5.1 Project home (new screen, before the wizard)

Recent projects sorted by last touched (decision 17), New project, Open by code.
The wizard is untouched and starts *inside* the chosen project, which also makes
it the "add another sizing" path. **Quick sizing** goes straight into the user's
scratch project; its sizings can be moved out later.

**Scratch and "Unfiled" are the same project** — one personal bucket per user,
`is_scratch = true`, created lazily on first quick sizing (and by the migration
in §2.3 for anyone who already has saved sizings). Two separate personal buckets
would be one too many places to lose a sizing in.

**The save path must carry `project_id`.** The wizard and the classic view both
end at `saveCurrentSizing()` ([auth.js:506](../app/static/js/auth.js#L506)),
which today posts name + payload. It now also posts the active project, and the
server rejects a save with no project rather than quietly inventing one — with
the single exception of the quick-sizing path, which resolves to the scratch
project server-side.

**Creation asks for a name and nothing else** (decision 24). Customer,
opportunity reference, prepared-by and description are optional and live in a
project settings panel, filled in whenever the user wants. This is deliberate:
partners sizing for their own end customers must never feel that the tool is
extracting their customer list as the price of using it. The export cover simply
omits any field left blank — it never shows a "Customer: —" placeholder.

`lang` is stamped from `pick_lang()` ([app.py:74](../app/app.py#L74)) when the
project is created, and drives the export-language prompt in §7.3.

### 5.2 Project view

Per row: name, tags, role chip (alternative / part of total), model + node count
from the snapshot, source file, updated, stale badge, checkbox. An expand arrow
reveals source clusters with their own checkboxes (decision 12).

Actions: New sizing · **Duplicate** · Rename · Move to project · Delete ·
Tag · Set role · Compare selected · Export selected.

Duplicate is a server-side copy of payload + snapshot with a new code and
` (copy)` name — the fastest way to build "Option 2 = Option 1 with RF3".

### 5.3 Tags and roles

Tags are freeform with autocomplete inside the project, colored chips, multiple
per sizing, filter chips at the top, and a "select all tagged X" action feeding
the export selection.

`role` drives the rollup only. When the **second** sizing is added and
`project.default_role` is NULL, ask once — "are these competing options, or
parts of one estate?" — store the answer as the project default and apply it to
both existing sizings; every later sizing inherits it and can be overridden per
row.

Rollup rule: sum only `additive` sizings; list `alternative` ones side by side.
A bundle mixing both gets a per-group total, never one grand total.

---

## 6. Comparison view

A table over the selected sizings: model, nodes, cores, RAM, usable TB, IOPS,
rack units, power, N-1 headroom, and delta against a chosen baseline row.
Exports as an "options considered" section in the bundle.

**A sizing with several source clusters has no single node count**, so it gets a
total row (summed across its clusters) with its clusters as optional sub-rows —
mixing a three-cluster sizing into a flat table as if it were one appliance
would misstate every column.

**`cost_tier` stays out of the exported table.** It is an internal relative cost
weight from `MODEL_COSTS`, not a price, and it does not belong in a document a
customer reads. Show it in the on-screen comparison if it helps you choose;
strip it from the export, the same way `salesforce_url` is stripped (§9.1).

Scope is one project (decision 23): the selection comes from the project view,
so there is no cross-project picker and no way to put two customers' estates in
one table by accident.

**Consistency warning** (decision 16, warn-only): compare each sizing's stored
tunables hash and its day-one caps / RF settings. Where they differ, mark the
column and add a footnote. Nothing is inherited or enforced — deliberately
comparing 50% vs 70% day-one stays possible, it just becomes visible.

---

## 7. Exports

### 7.1 Bundle content

Cover (project name, customer, opportunity ref, prepared-by, date — decision 13)
→ rollup / comparison → one section per selected sizing → provenance appendix.
Reuses `generate_multisite_proposal` and `build_multisite_proposal_docx`
([export_pptx.py:221](../app/export_pptx.py#L221),
[export_docx.py:558](../app/export_docx.py#L558)) after the refactor below.

Sections follow `configurations.position`, so the order you arrange in the
project view is the order the document reads in — dragging Option 1 above
Option 2 must not be undone by the export sorting on id or name.

Those generators currently take a flat `clusters` list. Generalise to
**sections** with an optional group label, so the same code serves both
"sections = source clusters of one sizing" (today's behaviour, unchanged) and
"sections = sizings of a project, each possibly holding clusters".

`MAX_EXPORT_CLUSTERS = 50` ([app.py:44](../app/app.py#L44)) now counts flattened
sections across the bundle and must be enforced at selection time, with a clear
message, not at POST time.

### 7.2 Background jobs

Document building is CPU-bound and admitted one at a time per worker
([export_gate.py](../app/export_gate.py)); PDFs add a LibreOffice pass. A
ten-sizing bundle must not block a request.

- `POST /api/projects/<id>/export` → `202 {job_id}`
- `GET /api/export-jobs/<job_id>` → status, progress, error
- `GET /api/export-jobs/<job_id>/file` → the artifact

A single worker thread started alongside the existing daily scheduler
([auth.py:79](../app/auth.py#L79)) drains the `export_jobs` table; it *is* the
concurrency limit, so `export_gate` stays as-is for the per-sizing routes.

**"A single worker thread" is per gunicorn process, not per deployment.** The
app runs ~3 workers, so three threads will poll the same table and, done naïvely,
three of them will build the same bundle at once — the exact CPU storm
`export_gate` was written to prevent. Claim jobs atomically:
`UPDATE export_jobs SET status='running', claimed_by=:pid WHERE id = (SELECT id
FROM export_jobs WHERE status='queued' ORDER BY created_at FOR UPDATE SKIP
LOCKED LIMIT 1) RETURNING id`. A job whose claimer dies mid-build (container
restart) must also be reclaimable — a `claimed_at` older than the gunicorn
timeout goes back to `queued`, or it sits "running" forever and its owner waits
for an email that never comes.

**Artifacts on local disk assume one container.** With a second app container,
the worker that built the file and the process serving the download may not
share a filesystem, and the download 404s intermittently. Fine today (single
container per `docker-compose.yml`); if that changes, point
`EXPORT_ARTIFACT_DIR` at a shared volume. The default is a temp directory
outside the source tree — bundles are regenerable, expire in 24 hours and carry
customer names, so they belong neither in the repo nor in a backup.

**Access control on the artifact** — `GET /api/export-jobs/<id>/file` checks
that the job belongs to the requesting user (or a super admin) and does not rely
on the id being hard to guess; ids are random regardless. A bundle is customer
sizing data, not a public download.

**Jobs outlive their inputs.** A sizing deleted while a bundle is building must
not crash the worker or, worse, half-render: resolve and snapshot the selected
results at claim time, then build from that snapshot.
Artifacts land in a temp dir with `expires_at = created_at + 24h` (decision 20),
cleaned by `purge_expired()` (§9) — long enough to fetch a bundle after a
meeting, short enough that customer documents don't pile up on disk. Because the
daily sweep can leave a file up to a day past expiry, the download route must
check `expires_at` itself and refuse an expired artifact rather than trusting
the file's presence. No Redis or Celery — this stays in-app, in keeping with
`[[in-app-daily-scheduler]]` and `[[no-dedicated-ops-design-for-autonomy]]`;
if a third background job ever appears, move all of them to a real scheduler.

`format=zip` runs the per-sizing generators and zips the results (decision 5).

**Exports panel.** The project view lists that project's recent jobs — format,
status, progress, when it expires, download. This exists regardless of email:
a job runs server-side whether or not anyone is watching, and without a panel a
user who navigates away has no route back to a finished bundle.

**Optional email notification** (decision 28). A checkbox on the export dialog,
"email me when it's ready", off by default:

- The mail is written in the project's `lang` (§7.3), not the language of
  whatever session happens to trigger it — so it needs its own locale entries
  alongside the existing verification and reset mails.
- The mail says the export is ready, names the project and format, states the
  expiry in plain words, and links to the project's exports panel. The file is
  never attached — a bundle runs to tens of megabytes, and a customer's full
  sizing would then live in a mailbox long after the 24-hour retention expired.
- It goes **only to the requesting user's own verified address**, taken from the
  session. There is no recipient field: a "send this to…" box would turn the
  sizer into an open mail relay and a one-click exfiltration path for customer
  data.
- A **failed** job emails too, when the box was ticked. Silence after opting in
  reads as "still running".
- The checkbox is hidden when SMTP isn't configured — reuse the existing mail
  settings and sender from the auth flows
  ([auth.py](../app/auth.py), super-admin email settings).
- Send after the artifact is on disk, never before, so the link is live when the
  mail arrives.

Because retention is 24 hours (decision 20), a mail read two days later points
at an expired artifact. The download route must answer "this export has expired,
re-run it from the project" rather than a bare 404, and the mail body states the
deadline explicitly.

### 7.3 Export language

Exports currently follow whatever the UI is showing, resolved per request by
`pick_lang()` ([app.py:74](../app/app.py#L74)) and passed into the generators.
That silently produces a mixed-language pile when a project built in Dutch is
exported later from an English session.

- The project stores `lang`, stamped at creation.
- The export request carries an explicit `lang`; the server no longer falls back
  to `pick_lang()` for project bundles.
- If `project.lang != pick_lang()` at export time, the UI asks which to use —
  naming both languages by their endonym from `LANG_NAMES`
  ([i18n.py](../app/i18n.py)) — and remembers the answer for the rest of that
  session's exports of that project.
- If they match, no prompt.
- Per-sizing exports keep today's behaviour unless the sizing is opened from a
  project, in which case the same rule applies.

Changing a project's language never rewrites stored results: language affects
document strings only, not the fingerprint (§3.2), so no refresh is triggered.

### 7.4 Rename — **done**, as "bundle" rather than "project"

Two corrections to the original wording, both made while implementing:

**The combined document is a "bundle", not a "project".** Today's caller
exports the source clusters of a *single* sizing, which is not a project at
all; naming those routes `export-project-*` would have created exactly the
ambiguity decision 19 exists to remove. "Bundle" is level-neutral — its
sections are clusters today and sizings tomorrow — and matches §7.1's own
wording. Shipped:

- `/api/export-multisite-*` → `/api/export-bundle-*` (the only caller was
  [app.js:2214](../app/static/js/app.js#L2214))
- `generate_multisite_proposal` → `generate_bundle_proposal`,
  `build_multisite_proposal_docx` → `build_bundle_proposal_docx`,
  `_slide_multisite_overview` → `_slide_bundle_overview`,
  `_multisite_payload` → `_bundle_payload`
- `MAX_EXPORT_CLUSTERS` → `MAX_EXPORT_SECTIONS`

**The i18n keys were deliberately left alone.** They are not generic labels:
`export.docx.multisite_site_heading` is "Site — {name}",
`export.pptx.multisite_col_replicates` is "Replicates to", and there is a whole
"Replication & Disaster Recovery" section. That wording is *accurate* — those
documents describe several physical sites with replication between them.
Renaming 26 keys across 15 locales would have churned 390 translated entries
for no user-visible gain, while renaming the *text* would have broken genuine
multi-site decks. When bundles gain project-flavoured sections (step 7), add
new keys beside these ("Option — {name}") rather than repurposing the site
ones.

Prose in the UI still moves to Project / Sizing / Cluster as new screens land.

Fifteen locales ship (`SUPPORTED_LANGS`, [i18n.py](../app/i18n.py)) and a
half-renamed key falls back to English silently — a rename is exactly how a
translation quietly disappears. Add a **key-parity test** with this step: every
locale JSON has the same key set as English, and no source file references a key
that no longer exists. The repo has no such test today, and this feature adds a
large batch of new strings on top of the rename.

---

## 8. Provenance

Captured at import and stored in `configuration.source_meta`: file name, type
(LiveOptics / RVTools / manual), `sha256` of the uploaded bytes, import
timestamp, host count, VM count. Shown as a column in the project view and as an
export appendix — combining several customer LiveOptics files is exactly the
case where the reader needs to know which number came from where.

Duplicate guard: on import, if a file hash already exists in this project, warn
before creating a second sizing from it.

---

## 8.5 Replication partners (cross-sizing)

Replication already works — but only between source clusters that arrived in a
**single import**. `clusterReplication` is keyed by cluster name
([app.js:3043](../app/static/js/app.js#L3043)), dedicated workload-less DR
clusters exist ([app.js:3158](../app/static/js/app.js#L3158)), the engine sizes
the inbound reserve ([recommend.py:243](../app/recommend.py#L243)), and both the
"Replicates to" column and `render_replication_topology_svg` render it.

None of that is reachable when the two sites came from two different LiveOptics
files — which is the case projects exist to serve. So this is a gap, not an
addition: a project lets you say "these belong together", and the first thing a
customer asks about two sites is how they protect each other.

**The reserve depends on the source's demand, not its result.** The engine sizes
inbound capacity from the source's vCPU/RAM/storage *demand*
([recommend.py:243](../app/recommend.py#L243)), which is input data. So a target
never depends on another sizing's output: there is no refresh ordering to get
right, and mutual A↔B replication — what customers actually buy — stays
well-defined instead of circular. Had it depended on the sized cluster, this
feature would be far more dangerous.

### Model

```
replication_links
  id, project_id → projects.id
  source_configuration_id → configurations.id, source_cluster VARCHAR
  target_configuration_id → configurations.id, target_cluster VARCHAR
  compute_pct, storage_pct, mode ('reserved' | 'failover')
  UNIQUE (source_configuration_id, source_cluster)   -- one target per source cluster
```

Both ends must sit in the same project (decision 29) — a link across projects
would drag one customer's demand into another's sizing. `configurations` also
gains `is_dr_target` (a sizing with no workload of its own, decision 30) and
`payload_digest` (§3.2 below).

### Fingerprint consequence (decision 31)

This is the one place the feature breaks an assumption elsewhere in this plan:
a sizing is no longer fingerprint-independent. Exclude a few VMs from site A and
site B's inbound reserve is wrong, with nothing on screen to say so.

So a target's fingerprint folds in, for every inbound link, the source's
`payload_digest` plus that link's percentages and mode. A `payload_digest`
column is maintained on save rather than hashing a 4 MB payload on every project
open. Renaming a source doesn't touch its payload, so it doesn't churn its
targets; changing its workload does.

### Guards

- Deleting a sizing that others replicate to is **refused**, naming them
  (decision 32). Same for moving it out of the project. Deleting the whole
  project still cascades — source and target go together, nothing dangles.
- A link's two ends must share a project, and the user must own it.
- Self-links are refused; mutual links are allowed on purpose.

### Exports

The bundle generators already consume `replicates_to` and draw the topology.
Cross-sizing targets need qualified names — "Site B — Prosess" — so a cluster
called "Prod" in two different sizings can't be confused for one.

---

## 9. Retention, sharing, permissions

- **Sharing** — `project.code` retrieved like a config code
  ([auth.py:1040](../app/auth.py#L1040)); pulling a project by code links the
  whole set via `scale_project_links`, mirroring `ScaleConfigLink`
  ([auth_models.py:194](../app/auth_models.py#L194)). Per-sizing codes keep
  working unchanged.
- **Rights from a code** — read-only (decision 22): view, compare and export.
  Any edit — changing options, retagging, setting a role — first copies that
  sizing into the viewer's own project and edits the copy, so a project can
  never change under someone who is presenting it. Enforce this server-side on
  every write path (`PUT /api/configs/<id>`, tag, role, position, delete), not
  only by hiding buttons: a linked project is never a writable target. The copy
  carries payload, snapshot and provenance, and gets a fresh code.
- **No sizing cap** (decision 21) — the project view lists from lean snapshots
  and pages; only the bundle is bounded, by the flattened section limit in §7.1.
  Full payloads (which carry entire VM lists, up to `MAX_PAYLOAD_BYTES` of 4 MB
  each — [auth.py:75](../app/auth.py#L75)) load only when a sizing is opened.
- **Visibility** — a `_project_source_for(user, project)` helper mirroring
  `_config_source_for` ([auth.py:967](../app/auth.py#L967)). A sizing is visible
  if its project is; existing per-config rules stay as the fallback.
- **Delete** — soft-delete the project and cascade `is_deleted` to its sizings
  (decision 15), so the existing recovery and audit paths apply unchanged.
- **Purge** — `purge_expired()` ([auth.py:462](../app/auth.py#L462)) gains
  projects past `RETENTION_DAYS`, their tags, their links, and expired export
  artifacts. Every existing config-purge query must also survive a project that
  outlives its sizings.
- **Scratch projects** are ordinary projects; they are excluded from the
  "recent" list only when empty.

### 9.1 Salesforce opportunity link (scale-only)

`projects.salesforce_url` holds a link to the opportunity in Scale's own
Salesforce instance. A partner or customer could not open it without an account
there, but it must never *appear* to them either — an internal deal reference
sitting on a project a partner can read is bad practice regardless of whether
the link resolves.

Enforced in four places, none of which is the UI:

1. **Serialization** — `Project.to_summary()` / `to_dict()` take `current_user`
   (as `Configuration.to_summary` already does,
   [auth_models.py:165](../app/auth_models.py#L165)) and include the key only
   when `user.is_scale or user.is_super_admin`. The field is *omitted*, not
   nulled: an always-present `salesforce_url: null` still tells a partner the
   concept exists. This mirrors the gate already used for editable exports
   (`_can_export_editable`, [app.py:501](../app/app.py#L501)).
2. **Write path** — `POST` / `PUT` on a project silently drops the field for
   non-scale users; it is never settable, not even on a project they own.
3. **Copies** — the copy-on-edit path (§9) and Duplicate never carry it into
   another user's project. A scale user duplicating their own project keeps it.
4. **Exports** — never rendered into any document, for anyone. The bundle is a
   customer-facing deliverable; an internal CRM URL has no business on it even
   when a scale user is the one exporting. It exists for navigation inside the
   sizer only.

Validation on write: require `https` and a host under `salesforce.com` /
`force.com`, so the field cannot be repurposed as an arbitrary link store or a
phishing vector inside a shared project view. Store as given otherwise; log
changes to the admin audit trail like other privileged writes.

Purge is unchanged — the value dies with the project row.

---

## 10. Build order

Everything above is in scope for the first release; this is the order that keeps
the branch usable at each step.

1. Rename (§7.4) — do it first, while the surface is small; move the appliance
   and validated calculators out of `app.py` into their own module in the same
   pass (§3.2), and add the locale key-parity test.
2. Data model + migration + project home + scratch project + assign/move/duplicate.
3. Result snapshot on save + fingerprint + stale badge (no auto-refresh yet).
4. Iframe refresh + progress.
5. Tags, roles, the second-sizing prompt, selection by tag.
6. Comparison view + consistency warnings.
7. Bundle export — synchronously behind the new job API first, then the worker,
   then the exports panel and the optional email notification.
8. Provenance + duplicate guard + cover metadata.

## 11. Tests

- Fingerprint: editing a referenced catalog row flips `stale`; editing an
  unreferenced one does not; changing a tunable flips everything; touching a
  sizing source file flips everything. **Run the same four cases against a
  validated sizing**, whose refs point at `validated_*` tables — that path is
  where a missed ref would go unnoticed.
- Parser version: bumping the LiveOptics/RVTools reader marks affected sizings
  "re-import needed" and does *not* mark them merely stale (§3.3).
- Refresh rights: a read-only viewer can PUT a result for a sizing they can see,
  and the same request cannot change payload, name, tags or role.
- Job claiming: two worker threads racing the same queued job produce exactly
  one build; a job stuck in `running` past the timeout is reclaimed.
- Rollup: alternatives are never summed; a mixed selection yields per-group
  totals.
- Migration: unfiled configurations — **including soft-deleted ones** — land in
  exactly one Unfiled project per owner, `project_id` ends up NOT NULL, and a
  second boot changes nothing.
- Comparison: a sizing with three source clusters shows one total row that
  matches the sum of its sub-rows; `cost_tier` appears on screen and in no
  generated document.
- Order: reordering sizings in the project view changes the section order in the
  exported bundle.
- Purge: deleting a project soft-deletes its sizings; purge removes projects,
  tags, links, and expired artifacts without orphans.
- Bundle: section count enforcement at the 50 limit; zip contains one file per
  selected sizing.
- Sharing: every write path against a project reached by code is refused; an
  edit produces a copy in the viewer's own project with a new code, leaving the
  original untouched.
- Artifacts: an export past `expires_at` is refused by the download route even
  if the file is still on disk.
- **Salesforce field visibility** (decision 27) — the key is absent from every
  response for a non-scale user: as owner of the project, as a tenant colleague,
  as a tenant admin, and as someone who opened it by code. Assert on the raw
  JSON body, not a parsed object, so an accidental `null` still fails. A
  non-scale write attempt is dropped without error and the stored value is
  unchanged; a copy taken by a non-scale user carries no trace of it; no
  generated document contains the URL for any role. A scale user who is *not*
  the owner still sees it — the gate is role, not ownership.
- Export language: a project created in `nl` and exported from an `en` session
  produces Dutch strings when Dutch is chosen and English when English is, with
  no prompt when the two already match.
- Creation: a project created with only a name has no blank placeholders on its
  export cover.
- Notification: the mail goes to the session user's address and to no other,
  regardless of what the request body contains; a failed job still notifies; the
  link resolves while the artifact lives and gives the expiry message after it
  doesn't.

## 12. Open questions

None outstanding — decisions 1-28 cover the design. Anything new gets appended
to §1 with its rationale rather than decided in code.

---

## 13. Build log

### Done (184 tests green, `.venv/bin/python -m pytest -q`)

**Step 1 — rename + engine split.**
`app/calc.py` now holds the appliance and validated calculators, extracted from
`app.py` so the engine hash can't be moved by an unrelated route edit (§3.2).
Bundle rename per §7.4. `tests/test_i18n_parity.py` (60 cases) guards all 15
locales in both catalogs — key parity, placeholder parity, and referenced-key
existence — and was mutation-checked by dropping a key and adding a bad
placeholder to confirm it actually fails.

**Step 2 — data model and project API.**
`app/project_models.py` (Project, ProjectTag, ConfigurationTag,
ScaleProjectLink, ExportJob), the new Configuration columns, additive migration
and the `_backfill_projects()` one-off in `seed.py`, and `app/projects.py` with
project CRUD, share codes, scratch resolution, duplicate/move/reorder, roles,
notes and tags. `POST /api/configs/` now requires a project and resolves the
quick path to the scratch project server-side.

**Step 3 — result cache and fingerprint.**
`app/fingerprint.py` plus `PUT /api/sizings/<id>/result`. Both calculators emit
`refs`. Project detail reports `stale` / `cache` / `needs_reimport` per sizing.

**Step 8 (part) — provenance.**
The import route returns `source_meta` (file name, type, sha256, counts, parser
version); it is stored on save, and `POST /api/projects/<id>/source-check`
flags a re-imported file by digest rather than by name.

**§8.5 — cross-sizing replication partners.**
`ReplicationLink` plus `payload_digest` and `is_dr_target` on Configuration;
link set/clear, workload-less DR-target creation, same-project enforcement,
mutual links, and the delete/move guards. `inbound_replication_digest()` folds
each source's payload digest and link terms into the target's fingerprint, so
editing a source marks its DR targets stale while renaming one does not.
17 tests in `tests/test_replication.py`.

**Step 2c / 5 — the GUI.**
`app/static/js/projects.js` plus markup in `index.html` and styles in
`style.css`: project home (recent projects, new, open by code, quick sizing),
project view (sizing table with tags, roles, cache state, selection, the
replication summary), the new-project modal (name only), project details, and
the per-sizing panel carrying role, tags, notes and the replication partner.
Signing in lands on the project home; the sizer gains a bar naming the project
it is working in. Saving carries `project_id` and the import's `source_meta`.
83 new UI strings added to all 15 locales, translated into Dutch;
`tests/test_frontend_wiring.py` fails the build if markup references a handler
no script defines (which otherwise fails silently in the browser).

**Step 4 — refresh loop.**
`buildResultSnapshot()` in app.js captures what the current inputs calculate to,
reusing the bundle payload builder. Loading the page with `?refresh=<id>` inside
an iframe restores that sizing, waits for the calculation, PUTs the result and
posts back; the project view drives the queue two at a time with a 30 s timeout
per frame and an origin-checked `message` handler. Recommendations now carry
`refs`, so imported sizings notice catalog changes — without that they would
have been permanently "fresh".

**Step 6 — comparison.**
`POST /api/projects/<id>/compare` returns per-sizing totals, a baseline delta in
the UI, and the warnings that make a comparison honest: stale rows, unsized
rows, re-import needed, mixed tunables, mixed roles. Only `additive` sizings are
summed into the rollup.

**Step 7 — bundle export.**
`app/export_worker.py` drains `export_jobs` with an atomic claim
(`FOR UPDATE SKIP LOCKED` on Postgres) and requeues jobs orphaned by a restart.
`POST /api/projects/<id>/export` → 202, the exports panel polls while work is
outstanding, artifacts are owner- and expiry-checked at download and swept by
`purge_expired()`. Optional email goes to the requester's own verified address,
on failure as well as success. Editable formats stay a scale-user privilege.

### Not started

Cover metadata from project details is stored but not yet rendered onto the
export cover (§7.1) — the generators still produce their standard cover. The
export-language prompt (§7.3) is not wired: a bundle uses the project's language
without asking when the session differs.

### Deviations from the written order

Step 3 was built before step 2c. The build order sequenced UI first to keep the
branch usable, but the branch isn't deployable mid-feature either way, and
unattended work is better spent where correctness is provable by test than on
screens nobody can look at. The UI is now the largest remaining piece and the
natural next task.
