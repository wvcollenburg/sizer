# Multi-Site Sizing + Replication — Implementation Plan

Branch: `feature/multi-site`

## 0. Terminology (read first)

Two unrelated concepts both called "cluster" live in this codebase. This plan
keeps them strictly separate:

- **Source cluster** — the vSphere cluster a VM/host belonged to in the source
  data (e.g. `Print`, `Prosess`), read from the `Cluster` column. This is the
  NEW grouping dimension this feature introduces.
- **Output cluster** — how `recommend.py` splits one recommendation's node
  count into physical appliance clusters of ≤ `T.max_nodes_per_cluster`
  (`_cluster_layout` → `[12,12,11]`, exposed as `num_clusters` / `cluster_layout`).
  This already exists and is unrelated.

"Keep individual clusters separated" = size each **source cluster** on its own.
Each source-cluster sizing can still fan out into multiple **output clusters**.

Sample data for testing: `_archive/LiveOptics_3381660_VMWARE_07_03_2026.xlsx`
— source clusters `Print` (2 hosts / 8 VMs) and `Prosess` (3 hosts / 70 VMs),
datastores cleanly cluster-scoped.

Suggested UI label: **"Size each cluster separately"** (clearer than "keep
separated"; the tooltip explains it produces one recommendation per source
cluster on its own tab).

---

## Part A — Multi-Site ("size each cluster separately")

### A.0 Architecture decision

The server is **stateless** for sizing: parsers produce a big dict, the browser
keeps `vms`/`hosts`/`datastores` + `summary`, and re-posts `summary` to
`/api/recommend` on every re-size. Crucially, after VM edits the browser
**recomputes** the summary itself in `computeAdjustedImportSummary()`
(`app/static/js/app.js:2382`).

Therefore the cluster split must live in **client state**, with the server
providing per-cluster base summaries. Chosen approach:

> **Server tags + per-cluster base summaries; client groups and loops.**
> Parsers already stamp `cluster` on every VM/host. Refactor `_build_summary`
> to accept a filtered subset, and at import return a per-cluster base summary
> map alongside today's aggregate. The client holds sizing state **per source
> cluster** and runs the existing recalc/render/export paths once per cluster.

This keeps the single-cluster path 100% intact (one cluster ⇒ current behavior).

### A.1 Backend — parsing & summary

**Files:** `app/liveoptics.py`, `app/rvtools.py`, `app/app.py`

1. **Refactor `_build_summary`** (`liveoptics.py:464`, `rvtools.py:167`) to take
   pre-filtered `hosts`/`vms`/`datastores` lists instead of reading the whole
   dataset. Today it already aggregates over the passed lists, so this is mostly
   parameter threading. The aggregate summary becomes "build over all lists."
2. **Add cluster grouping** in `parse_liveoptics` / `parse_rvtools` after the
   lists are built:
   - Derive the set of distinct `cluster` values across hosts+VMs.
   - For each cluster name, filter hosts/vms/datastores and call the refactored
     `_build_summary` → per-cluster summary.
   - Datastore→cluster attribution: datastores carry no cluster field directly.
     Map each datastore to the cluster(s) of the VMs that reference it
     (`vm["datastore"]`), matching the existing dedup logic
     (`[[liveoptics-datastore-dedup]]`). A datastore used by multiple clusters is
     split proportionally by used-capacity or VM count (log the assumption).
   - Handle the degenerate cases: empty cluster name → bucket as
     `"(unclustered)"`; single distinct cluster → return just that one (no UI
     change downstream). GENERAL/Hyper-V scans have blank cluster → single bucket.
3. **Extend the import response** (`app.py:247`) with:
   ```
   "clusters": [
     {"name": "Print",   "summary": {...}, "host_count": 2, "vm_count": 8},
     {"name": "Prosess", "summary": {...}, "host_count": 3, "vm_count": 70}
   ]
   ```
   Keep top-level `summary`/`vms`/`hosts`/`datastores` exactly as today for the
   combined view and backward compat. Each VM already carries `cluster`, so the
   client can filter `vms` per cluster without a separate per-cluster VM list.

**No change to `recommend.py` is required for Part A** — it's already called once
per summary. Multi-site just calls it N times with N summaries.

### A.2 Frontend — state model (the core refactor)

**File:** `app/static/js/app.js`

Today the import flow uses flat globals: `importVms`, `originalImportSummary`,
`importSummary`, `vmExclusions`, `vmConfig`, `vmAdded`, `vmRemoved`,
`includeLocalStorage`, plus `lastRecommendations/lastProjection/lastSummary`
keyed by mode (`app.js:20-42`).

Introduce a **per-source-cluster state container**, active only when
"size each cluster separately" is on:

```
clusterState = {
  <clusterName>: {
    originalSummary, summary,
    vms,                       // filtered view of importVms for this cluster
    exclusions: {compute:Set, storage:Set},
    vmConfig, vmAdded, vmRemoved,
    recommendations, projection, selectedRecIndex
  }, ...
}
activeClusterTab = <clusterName>   // which tab is showing
separateClusters = <bool>          // the checkbox
```

Least-risky implementation path:
- When the checkbox is **off** → behave exactly as today (single implicit
  cluster = whole dataset). Zero behavior change.
- When **on** → build `clusterState` from the import response's `clusters[]`,
  partition `importVms` by `vm.cluster`, and make the existing functions
  cluster-aware by routing through the active tab's slice.

Functions to make cluster-aware (route to `clusterState[activeClusterTab]`):
- `computeAdjustedImportSummary()` (`:2382`) — operate on the active cluster's
  vms + its own `originalSummary` (fixes the datastore-total base problem: each
  cluster subtracts/adds against its OWN base).
- `renderVmTable()` (`:2111`) — filter to active cluster's VMs.
- `recalcRecommendations()` (`:1121`) — when separate, loop clusters (or recalc
  just the active tab and lazily recalc others); post one `/api/recommend` per
  cluster.
- `renderRecommendationsTo()` (`:1410`) — render into the active cluster's panel.
- Save/restore (`captureSizingState` `:2550`, `restoreSizingState` `:2587`) —
  serialize `clusterState`; bump `SNAPSHOT_VERSION` and migrate old (flat)
  snapshots into a single-cluster `clusterState`.

### A.3 Frontend — recommendation tabs

**Files:** `app/templates/index.html`, `app/static/js/app.js`, CSS

- Add a tab bar above `#rec-list` (`index.html:588`). Model it on the existing
  **saved-sizings tabs**: `.sizings-tabs`/`.sizings-tab` + `setSizingsTab()`
  (`auth.js:413`) — cleanest existing pattern (module var + `.active` toggle +
  re-render). New global `setClusterTab(name)`.
- One tab per source cluster; tab label = cluster name + node/VM count badge.
- Only show the tab bar when `separateClusters` is on and >1 cluster exists.
- Each tab renders that cluster's own `.rec-card` list, ratio/options, and
  env-summary cards (`#env-summary`, `app.js:1278`). Decide: shared sizing
  options (ratio, growth, storage-pref) across tabs vs per-cluster. Recommend
  **per-cluster** options (a Print cluster and a Prosess cluster legitimately
  differ), with a "apply to all tabs" convenience button.
- Add a **"Combined (all clusters)"** tab as well, so the user can still see the
  single-blob sizing — this is the current behavior and a useful comparison.

### A.4 Frontend — Configure-VMs tabs

**Files:** `app/templates/index.html` (`#vm-exclusion-modal` `:634`), `app/static/js/app.js`

- Add a tab bar inside the modal header, one tab per source cluster (+ maybe
  "All"). New global `setVmModalTab(name)` storing active tab and calling
  `renderVmTable()`.
- `renderVmTable()` (`:2111`) filters `importVms` to the active tab's cluster.
- Per-VM edits/exclusions write into that cluster's slice of `clusterState`.
- `applyVmExclusions()` (`:2461`) recomputes the affected cluster's summary and
  triggers that cluster's recalc.
- Show a per-tab exclusion count badge; keep the global badge as a sum.

### A.5 Exports (multi-cluster)

**Files:** `app/app.py` (export routes `:381-456`), `app/static/js/app.js`
(`exportProposal` `:1988`), `app/export_pptx.py`, `app/export_docx.py`

`generate_proposal` / `build_proposal_docx` each take a single
`summary`+`recommendation`+`projection` and emit a fixed slide/section set.
DOCX already uses `summary.cluster_name` in the header — convenient.

Two viable output shapes (offer as a toggle, default **A**):
- **A. One combined document, per-cluster sections/slides.** Change the export
  routes to accept `clusters: [{summary, recommendation, projection}, ...]`,
  loop the existing slide/section builders once per cluster (prefixing titles
  with the cluster name), and add a leading "Multi-site overview" slide/section
  totaling all clusters. Wrap the per-cluster loop inside `generate_proposal` /
  `build_proposal_docx` (or a thin new `generate_multisite_proposal`).
- **B. One document per cluster** (zip download). Simpler server-side (call the
  existing builder N times) but worse UX.

Client `exportProposal` (`:1988`) posts each cluster's `selectedRecIndex` rec.
`_proposal_payload()` (`app.py:404`) and the 4 export routes need the new
`clusters[]` shape. Filenames: `SC_Proposal_MultiSite_<n>clusters.pptx`.

Cluster network diagram (`cluster_diagram.py`, `_slide_network`) is already
per-recommendation, so it just runs once per cluster.

### A.6 i18n

Every new label needs keys in **all** locale JS files (`app/static/js/lang/*.js`)
AND server locale JSON (`app/locales/*.json`) — per `[[add-language-gui-and-exports]]`.
New keys: the checkbox + tooltip, tab labels, "Combined" tab, per-cluster
export strings, multi-site overview headings.

### A.7 Test / verify

- Unit: `_build_summary` on filtered subsets reproduces per-cluster totals;
  grouping produces `Print`/`Prosess` with correct counts (8/70 VMs, 2/3 hosts).
- Datastore attribution: `ME5024VOL*`→Prosess, `vol1`→Print in the sample.
- E2E: import the sample, toggle the checkbox, confirm two rec tabs + two
  configure-VMs tabs, edit a VM in one tab and confirm only that cluster's
  sizing changes, export and confirm both clusters appear.
- Regression: checkbox OFF must byte-for-byte match today's single sizing.
- Note deploy rebuild gotcha (`[[deploy-rebuild-gotcha]]`): JS/template changes
  need an image rebuild+recreate on the test box, not just a restart.

### A.8 Suggested sequencing

1. Backend: `_build_summary` refactor + per-cluster summaries + response field
   (behind the scenes; aggregate unchanged). Ship + verify no regression.
2. Frontend state container + checkbox (recommendations tabs only, VM modal
   still whole-dataset). Verify two tabs size correctly.
3. Configure-VMs tabs.
4. Exports.
5. i18n sweep + polish.

---

## Part B — Replication Relationships (pre-work + design)

Replication is a **relationship between two source clusters/sites**, so Part A
(source cluster as a first-class entity) is a hard prerequisite. Below is the
design space and the concrete pre-work.

### B.1 The design space (what "replication" can mean for sizing)

A replication relationship adds the replicated workload's demand onto the
**target** cluster. The knobs that matter for sizing:

1. **Scope** — which VMs replicate:
   - All VMs in the source cluster, OR
   - A **percentage** (%) of the cluster (approximation), OR
   - **Per-VM "selected for replication"** flags (precise; drives automatic
     sizing). Recommend supporting all three, with per-VM as the accurate mode
     and % as a quick estimate.
2. **Resource dimension** — what the target must provide:
   - **Storage-only** (async replication landing zone / backup target): target
     needs the replicated **storage** (+ change-rate/journal reserve) but no
     running compute. Fits the existing storage-only-node concept
     (`[[storage-only-nodes]]`).
   - **All resources** (active DR / failover): target must also carry
     **compute + RAM** to run the replicated VMs on failover — full or partial
     (e.g. run at reduced ratio during DR).
3. **Failover assumption** — for all-resource replication, does the target run
   the replica VMs concurrently (active/active, always-on demand) or only on
   failover (demand counts toward N-1/headroom, not steady state)?
4. **Topology / direction:**
   - One-way `A → B` (B is DR for A).
   - Bidirectional `A ↔ B` (each site is DR for the other; each sizes for its
     own workload **plus** the peer's replicated demand).
   - Hub-spoke / many-to-one (a central DR target for several sites).
5. **Storage multipliers:**
   - **Replication factor / retention** — number of point-in-time copies /
     snapshot journal depth (extra storage = base × retention factor).
   - **Change rate** — daily change % × retention days for delta/journal space.
   - Interacts with existing RF write-amp / IOPS model (`[[iops-sizing-model]]`)
     and day-one caps (`[[day-one-consumption-caps]]`).

### B.2 Pre-work required before building replication

**PW-1 — Cluster identity (Part A).** Source clusters must be first-class,
addressable entities with stable IDs. Delivered by Part A.

**PW-2 — Per-VM replication attributes in the data model.** Extend the VM dict
with `replicate: bool` and optional `replication_target: <clusterId>`. Thread
through parsers (default false), client state (`clusterState[*].vms`), save/restore
snapshot, and the summary recompute.

**PW-3 — "Selected for replication" in the edit-VMs modal.** Add a column /
checkbox in `#vm-exclusion-modal` (mirrors the existing Excl.Compute /
Excl.Storage checkboxes and `toggleVmExclusion` at `app.js:2252`). Bulk actions
("select all", "select by tag/OS") like the existing `selectPoweredOffVms` /
`selectLikelyCVMs`. This is what makes sizing **automatic** — the sum of
selected VMs' resources becomes the inbound-replicated demand.

**PW-4 — Replication topology model.** A small structure describing relationships:
```
replication: [
  { source: "Print", target: "Prosess", mode: "storage-only"|"all-resources",
    scope: "selected"|"percent"|"all", percent: 100,
    failover: "active"|"on-failover", retention_copies: N, change_rate_pct: X }
]
```
Stored in client state + snapshot; posted to the sizing endpoint.

**PW-5 — Sizing engine: additive inbound demand.** `generate_recommendations`
computes a cluster's `needs` from its own summary today. Add an optional
`replicated_in` demand block (storage always; vCPU/RAM only when
`mode == all-resources`) that is added to the target cluster's needs before
model fitting. For `on-failover`, feed it into the N-1/headroom path rather than
steady-state (leverages existing `_n_minus_1_block`). For storage-only, prefer
adding storage-only nodes (`[[storage-only-nodes]]`) rather than full nodes.

**PW-6 — New tunables** (`app/tunables.py`): replication journal/snapshot reserve
%, default change rate, default retention. Keep them tunable (distrust of fixed
vendor numbers, per `[[iops-sizing-model]]`).

**PW-7 — UI for relationships.** A "Replication" section in sizing options
(checkbox to enable) + a small relationship editor (source→target dropdowns,
mode, scope). Only meaningful when >1 source cluster exists (i.e. after Part A).

**PW-8 — Exports & diagram.** Show replication arrows between clusters in the
network diagram (`cluster_diagram.py`) and a "Replication" section in
PPTX/DOCX summarizing what replicates where and the storage/compute uplift.

### B.3 Suggested replication sequencing (after Part A ships)

1. PW-2 + PW-3 (per-VM flag + modal checkbox) — no sizing effect yet, just
   captures intent.
2. PW-4 + PW-6 (topology model + tunables).
3. PW-5 storage-only mode first (simpler, maps to existing storage-only nodes),
   then all-resources / failover.
4. PW-7 UI, PW-8 exports/diagram, i18n.

### B.4 Decisions (confirmed)

- **Sizing options are per-cluster**, with an "apply to all tabs" convenience
  button (§A.3). Each cluster tab owns its ratio/growth/storage/day-one caps.
- **Exports are one combined document** — a multi-site overview slide/section
  plus per-cluster sections (§A.5 option A).
- **Replication topology model supports both** storage-only and full-failover
  resource modes; **build storage-only first** (simpler, maps to existing
  storage-only nodes), then add full-failover (§B.3). Persistence needs no DB
  migration — `Configuration.payload` is schemaless JSON and recommendations
  are recomputed on restore, so only the client snapshot grows.
