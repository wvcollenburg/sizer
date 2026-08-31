# Pricebook & licensing plan

Branch: `feature/pricebook`

> **Status: not agreed. A further discussion with the team is required before any
> of this is implemented in full.** Scope was cut on 2026-08-26 after review with
> peers — hardware component pricing is parked (§9), licensing and catalog work
> continue. Sections below marked **[needs sign-off]** are the ones still open.

## What this is now

Three separable pieces of work, in priority order:

1. **Catalog truth** — which models are actually orderable, and whether our CPU /
   DIMM / drive option lists match reality. Needs a product feed, **no prices**.
2. **Licensing model** — replace our linear per-core cost term with the real
   banded, capped structure, and add the BRS / Video Surveillance / Essentials
   sizing profiles. Needs licence pricing only.
3. **Weighting model** — replace the per-model `cost_tier` with an explicit,
   stable capability-tier weighting that is aware of platform steps.

**Hardware component pricing is parked.** See §9 for why and for the re-entry
condition.

Pricing of any kind is never displayed, exported, or sent to the browser.

---

## Background: why the scope changed

The original plan used averaged component prices from the quarterly price list as
the ranking weight. Review raised a decisive objection:

> "Right now, pricing is so distorted that I suspect there will be some baffling
> results if using cost as a weight. […] it will end up producing some very
> strange / unexpected results that will need additional oversight — at least
> until we land into somewhat stable/rational prices again. (not necessarily
> lower, just sensical and stratified by the value of the product/component
> rather than whatever is in tight supply at the moment)"
> — Chris Nietzold

That is correct, and the reason is sharper than volatility alone. The cost weight
was never really about cost — it is a proxy for *"don't reach for a bigger box
than the workload needs"*. A proxy is only useful while it tracks the thing you
actually care about, which is **capability tier**. When component prices decouple
from capability, real prices make the ranker confidently wrong, and wrong in a way
that changes every quarter for reasons unrelated to the workload. A hand-set
weight is at least stable, auditable, and explainable to a customer.

The existing design already knew this. `MODEL_COSTS` in `app/models.py:1081` says
so explicitly — *"Used purely as a relative ranking tiebreaker … absolute units
don't matter, only the spacing between models"* — and the bands are shaped by
product family (edge → 1U → datacenter → all-flash → 2U-dual), not by price.
**It is a capability tier that is merely named `cost`.** That misnaming is why
"shouldn't we put real prices in it?" was a natural question, and it will be a
natural question again. §8 renames it.

**The volatility objection does not apply to licensing.** Licence bands are set by
Scale's product and pricing organisation on a deliberate cadence; they do not
swing with component supply. The €228/core linearity, the 48C cap, the 24C caps
and the Essentials cliff are pricing *policy*, and policy is exactly the kind of
thing that is safe to encode. Keeping licensing while parking hardware is not a
compromise — it splits the work along the stable/unstable seam.

---

## Decisions taken

| Question | Decision |
|---|---|
| Hardware component pricing as a ranking weight | **Parked** (§9). Revisit when component pricing re-stratifies by value. |
| Licensing model | **Proceed.** Banded/capped licence replaces the linear core term. |
| Catalog / orderability | **Proceed**, via a price-free product feed. |
| `cost_tier` | **Rename to `platform_tier`** and keep as a hand-set capability weight (§8). |
| Third-party guest licensing (Windows/SQL/Oracle) | **Out of scope.** Separate exercise. |
| Orderability vs `status` | Pricebook feed **overwrites `Model.status`** on apply. |
| Schema | Versioned + currency + **region** (LATAM / NA / EMEA / APAC). |
| Does the catalog differ per region? | No — same models everywhere. |
| Where is region set? | User/tenant profile, resolved user → tenant → global default. |
| Are licence bands regional? | **Yes.** |
| Missing regional pricebook | Fall back to a nominated default region, with a visible notice. |
| BRS / Video Surveillance | **Advanced sizing profiles**, never default, Scale staff and super-admin only (§6). |
| Licence term for scoring | Same as the growth projection horizon (`years`, already 1–5). Standard Support. |
| €1 pre-sale SKUs | Model stays in catalog and picker; recommender will not propose it unprompted. |

---

## Priority order

| # | Phase | Blocked on | Why here |
|---|---|---|---|
| **1** | Security baseline (§3) | Nothing | `cost_tier` already leaks to every browser. Do this regardless of everything else. |
| **2** | Catalog feed + orderability (§4) | A price-free product report | Biggest correctness win per unit of effort. Corrects 17 of 43 model statuses, adds 2 missing chassis, validates CPU/DIMM/drive lists. No pricing, so approval is near-trivial. |
| **3** | Licence bands + scoring (§5) | Licence pricing feed | The one change that meaningfully improves *which* configuration is recommended. |
| **4** | Sizing profiles: BRS / Video / Essentials (§6) | §5, and sign-off | Builds on the licence tables. Reuses existing engine mechanisms. |
| **5** | Weighting model (§7, §8) | Sign-off | Replaces what component pricing would have provided. Can proceed independently of any feed. |
| — | Hardware component pricing (§9) | **Parked** | Revisit on market conditions. |

Phases 1 and 2 are safe to start now. Phases 3–5 change recommendations and
should not ship before the discussion in §12.

---

## 3. Phase 1 — security baseline (do now)

`cost_tier` reaches every logged-in browser and is unused by the front end.

- Remove `cost_tier` from `Model.to_dict()` — `app/orm_models.py:302`.
- Remove `"cost_tier": cost_tier` from the candidate dict — `app/recommend.py:1144`.
- Add to `tests/test_security.py`: assert no price-shaped key (`cost`, `price`,
  `tier`, `eur`, `list_price`) appears in the serialized JSON of `/api/models`
  or `/api/recommend`, or in generated export fixtures. Assert on the response
  body, not the model definition.

`app/static/js/app.js` references neither and performs no arithmetic (0
occurrences of `Math.ceil`/`Math.floor` across 4,089 lines), so this is safe.

Note for context: `/api/` is gated by login (`app/auth.py:377`), but registration
is a **blocklist** — any unblocked email domain can self-register. Adequate for a
ranking weight; it is why nothing price-shaped may reach a user-facing endpoint.

---

## 4. Phase 2 — catalog feed and orderability

### 4.1 The Salesforce ask — feed A (no prices)

This is the easy conversation: a product catalog extract with **no price column
at all**.

| # | Column | Status | Why |
|---|---|---|---|
| 1 | `Product: Product Code` | exists | The SKU. Classifies each row (`CHA-`/`CPU-`/`RAM-`/`HDD-`/`SSD-`/`NVM-`/`NIC-`/`GPU-`). |
| 2 | `Product: Product Description` | exists | **The parse target.** Cores/threads/clock, DIMM capacity + form factor + DDR generation, drive capacity + interface, chassis bay layout. |
| 3 | `Product: Product Family` | exists | Whitelist filter. |
| 4 | `Product: Product Series` | exists | The **model name** on chassis rows (`HC1450D`, `HE153s`) — the join key to our catalog. |
| 5 | **Effective date / catalog version** | **new** | Currently only in the filename. Needed to version imports and date the diff. Can be entered at upload instead. |
| 6 | **Region** *(or one file per region)* | **new** | File-per-region is fine and probably matches how they are generated. |

**Highly desirable:** a **product lifecycle status** column (Active / EOL /
Discontinued / Pre-release). It makes orderability explicit instead of inferred
from row presence, and resolves the pre-sale placeholder problem properly rather
than by heuristic. This is the single most valuable addition to either feed.

**Explicitly exclude:** both `Product: Distributor Description` columns — they
share a header and their contents **swap between rows** (`CHA-3-1A` has the name
first, `CHA-3-1E` has it second). Actively harmful. Also drop shipping
dimensions.

### 4.2 What it corrects

The recommender filters `Model.status == "Active"` (`app/recommend.py:302`).
Against the Q4 2025 catalog, 17 of 43 models are wrong:

- **Active today, absent from the catalog** (we recommend what cannot be quoted):
  HC1600, HC1650D, HC3350F, HC3350DF, HC5600, HC5650D, HE155-1, SE100
- **EOL/EOS today, still in the catalog** (we exclude what is still sellable):
  HC1300, HC1350, HC1400, HC1450, HC1450D, HC3250DF, HE151, HE153, HE153s

Plus two chassis missing from our catalog entirely: **HC1250DFG** and
**HC5250DFG**, both GPU-enabled — we currently have no candidate for a GPU/VDI
request.

### 4.3 HCL validation

The component rows are a de-facto hardware compatibility list. Bay counts already
match our `drives_per_node` / `hdd_count` / `nvme_count` for every chassis, which
makes the feed usable as a standing regression check. Where they do not match:

- **HE550F** offers Xeon E-2224 / E-2234 / E-2236 — none exist in the Q4 2025
  catalog. We are sizing against CPUs that are not sold.
- **HE153** offers only the i7-1370P; the i5-1350P and i9-13900H exist. Missing
  rungs distort the fit in both directions.
- A 48 GB DDR5 SODIMM exists, so HE1xx can reach 96 GB/node; we cap at 64.

### 4.4 Import flow

`Upload → parse to staging → diff report → admin approves → apply.` Never
straight into live tables.

The diff report shows models gaining or losing orderability, catalog options with
no matching SKU, SKUs with no catalog entry, and the unmatched-row count.
**Unmatched rows are recorded and surfaced, never silently dropped** — that count
is the early warning that the export format moved.

Sanity gates that block apply: required headers missing, row count off by >30%,
zero chassis rows parsed.

Parser rules: build the column index **by header name**, not position; classify by
SKU prefix; extract structure from the **description**, never from the SKU family
digit (which is a carrier/generation code we cannot map).

Routes: `POST /api/admin/catalog/upload`, `GET .../diff/<id>`,
`POST .../apply/<id>` — all `super_admin_required`, audited via `AdminAuditLog`.

### 4.5 Pre-sale models

A chassis present only as a pre-sale/placeholder row sets `Model.is_pre_sale`.
The model stays `Active` and in the "Size For Model" picker so an SA can position
it deliberately, but the recommender skips it when building candidates unless the
sizing names it explicitly — mirroring the existing "an explicit model wins over
the status filter" rule at `app/recommend.py:142`. Currently **HC3650F** and
**HC3650DF**.

---

## 5. Phase 3 — licence bands and the scoring change **[needs sign-off]**

### 5.1 The real curve

SC//HyperCore Standard, per node, banded by that node's physical core count.
Linear at ~€228/core (Standard Support, 1 year) up to 48 cores, then **flat**:

```
48C = 52C = 56C = 64C = €10,957 (SS 1Y) / €13,869 (PS 1Y)
```

Cores 49–64 on a node carry no licence cost. Our current
`w_core_license × total_cores` is linear and unbounded, so it over-charges a
2×32C node by roughly a third of its licence line — steering us *away* from the
configurations that are most licence-efficient.

BRS and Video Surveillance editions cap far earlier, at **24C** (€5,096 flat, 1Y).

Essentials Kit / Professional Essentials is a **cliff, not a curve**: a flat
per-cluster SKU limited to *exactly 3 nodes, max 256 GB RAM per node, cannot be
bundled* — €4,212 SE / €5,319 PE at 1 year, against €10,959 for the same cluster
licensed per-core at 16C/node. That makes 3 nodes and 256 GB/node genuine design
attractors.

### 5.2 The Salesforce ask — feed B (licence pricing)

Narrow and defensible: `HCOS-*` rows only.

`Product Code`, `Product Description`, `Currency`, `List Price`, `Product Family`,
effective date, region. Six columns, one family, no hardware pricing.

The licence SKU grammar parses with one regex —
`HCOS-([A-Z]+)-(\d)-(\d+)C(?:-(PS|SS))?`. Deliberately `[A-Z]+` rather than
`[SLV]` so a future edition letter lands as data marked *"priced, not yet
selectable"* rather than as an unmatched row.

The flat SKUs (`HCOS-{1,3,5}-{PE,SE}`, `-1S-{5,10,15}WL`) are picked up by a
second, narrower pass.

### 5.3 Term

`term_years` = the sizing's growth projection horizon (`years`, already clamped
to 1–5 at `app/recommend.py:120`). Standard Support. No new input, no new
tunable, and the licence term automatically matches the horizon the cluster was
sized for. All five terms exist as SKUs for every edition.

### 5.4 Scoring

Current — `app/recommend.py:1011-1019`:

```python
fleet_cost = node_count * (cost_tier + T.node_overhead)
core_cost  = T.w_core_license * total_cores
score      = T.w_cost * fleet_cost + core_cost + T.w_waste * waste
```

New:

```python
fleet_tier = node_count * (platform_tier + T.node_overhead)      # unitless, §8
lic_index  = license_cost_eur(...) / 1000.0                      # k€, §5.5
score      = (T.w_cost   * fleet_tier
            + T.w_license * lic_index
            + T.w_waste  * waste
            + T.w_step   * step_penalty)                          # §7
```

`w_core_license` is removed. `w_license` (new) multiplies the real banded cost.

**Units.** The licence term is now the only monetary quantity in the score, so it
needs an explicit bridge to the tier scale. Dividing by 1,000 puts a typical
3-node licence in the 15–75 range, comparable to `fleet_tier` at
`3 × (18 + 12) = 90`. `w_license` then defaults near 1.0 and is tuned in §10.

### 5.5 Licence cost

Per cluster: the sum of per-node band prices, **or** the flat Essentials price
when that cluster has exactly 3 nodes at ≤256 GB/node and the flat price is
lower. Applied per cluster, so a multi-cluster layout can qualify on some
clusters and not others.

### 5.6 Annotation

The candidate carries booleans and strings, **never numbers**:

- `essentials_eligible: true`
- `essentials_near_miss: "32 GB/node over the 256 GB Essentials ceiling"`
- `license_cap_reached: "cores 49–64 per node carry no licence cost"`

These are the only licensing facts that cross the wire.

---

## 6. Phase 4 — sizing profiles **[needs sign-off]**

Both are **advanced options, never selected by default**, visible only to Scale
staff and super-admins — gate on `user.is_scale or user.is_super_admin` (both
exist; `is_scale` is derived from the tenant domain at `app/auth.py:445`).

Neither is merely a price band. Each carries structural sizing rules, and both
map onto engine capabilities we already have.

### 6.1 BRS — Business Resilience System

Scale + Acronis DR target for a **non-Scale source**: Acronis' convert-to-VM
produces a passive DR VM for an other-technology workload. The node **must be an
SNS (single node system)**.

- Forces `allow_single_node = True` and `node_count = 1` —
  `app/recommend.py:715-716` already implements exactly this.
- Licence priced on the BRS band, flat above 24C, so a single fat node is both
  the only permitted shape and the economically correct one.

**Source input is already built.** Manual entry is a full peer of import:
`manual-form` plus the per-VM modal produce a `manualSummary` identical in shape
to `importSummary` (`app/static/js/app.js:2273`), including a free-text
`current_platform`. BRS can equally take an import — a non-Scale source is often
still VMware or Hyper-V.

Acronis protects physical and foreign-hypervisor machines, so a BRS source often
has no host GHz, peak CPU% or IOPS telemetry. This degrades cleanly today:
`_compute_coverage` returns `None` when neither compute signal has demand and
`_compute_floor_nodes` then returns 0, so no compute floor applies; the legacy
GHz-shortfall penalty is separately guarded by `if needs["current_total_ghz"] > 0`.
Sizing falls back to vCPU:core ratio, RAM and storage — correct for a target
whose VMs are not running.

#### BRS sizing rules

Compute is sized to a **stated concurrent-failover fraction**; storage to **full
protection**. Two protection classes:

| Class | Multiplier | Composition |
|---|---|---|
| DR copy | **× 2.5** | 1× backup + 1× DR copy + 0.5× snapshots / incrementals |
| Storage backup only | **× 1.5** | 1× backup + 0.5× incrementals |

```
base_storage = 2.5 × dr_protected_tb + 1.5 × backup_only_tb
base_vcpus   = concurrent_fraction × protected_vcpus
base_ram     = concurrent_fraction × protected_ram_gb
```

Growth still applies to all three. Dedupe on the additional copies is
deliberately not modelled — it trends high and unpredictably, and ignoring it is
conservative.

Three interactions with existing behaviour that must be handled explicitly:

1. **Force `snapshot_pct = 0` for BRS.** The 0.5 in both multipliers already
   covers snapshots and incrementals. Leaving the normal snapshot reserve on top
   double-counts — at the 20% default that is a further 20% of an already 2.5×
   figure.
2. **Day-one consumption caps must be reconsidered.** `max_day_one_storage_pct`
   (50% default) would size the node so today's protected estate occupies at most
   half its capacity, roughly doubling an SNS whose whole point is to be
   right-sized. Recommendation: BRS ignores the day-one caps. **[needs sign-off]**
3. **The largest-VM RAM guard still applies, and should.** RAM options that
   cannot fit the largest protected VM are filtered at `app/recommend.py:750`
   regardless of the fraction — so a low fraction can never produce a node that
   physically cannot start the biggest machine it protects. This is the safety
   net that makes the fraction safe to expose.

**New inputs:** concurrent-failover fraction (%), and a split of the protected
estate into DR-copy vs backup-only storage. Everything else reuses existing
workload entry.

*Assumption:* the fraction scales **both** vCPU and RAM, since a passive VM
consumes neither until failover. One-line flip if RAM should be sized for the
full protected set.

### 6.2 Video Surveillance

EULA-limited to CCTV and adjacent technology — DVR, VMS, door access. Heavy on
disk, very light on virtualization.

- Typical shape: **2 virtualization nodes, the remainder storage-only** with
  minimum compute. That is precisely the storage-only split we already build
  (`app/storage_only.py`, minimum 2 full HCI nodes per cluster, tunable via
  `min_hci_nodes_per_cluster`).
- The profile pins HCI nodes at the minimum and pushes the remainder to
  storage-only rather than letting the ranker choose the split.
- Licence priced on the Video band — flat above 24C, matching a design that
  deliberately keeps compute small.
- Surface an EULA notice when selected.

### 6.3 Why this is a small build

Both profiles are a **preset plus a band selection**, not new sizing logic: set
existing flags, pick the licence table, add the BRS storage multipliers.

---

## 7. Component weighting — where new weights are needed **[needs sign-off]**

With component pricing parked, the score loses the only signal that would have
distinguished two configurations *of the same chassis*. This section is the
answer to "where do we need new weights to cover for components?"

### 7.1 The gap

`platform_tier` is per-model. Today an HC3450DF at 256 GB / 4×0.96 TB and the
same chassis at 2 TB / 10×15.36 TB score **identically** on the tier term. The
waste term partially compensates, but only as a continuous fraction of
over-provisioning — it cannot see that some steps up the ladder are cheap and
others cross a platform boundary.

This is the same principle the licensing analysis produced, applied to hardware:
**the discontinuities change the answer; the smooth terms only scale it.** It is
also Chris's point about "clear steps at ~128GB, then ~1TB, then >1TB" — the
steps are the structural, stable fact, and they do not move with spot pricing.

### 7.2 Proposed new weights

The option ladders are already ordered ascending in `models.py`
(`ram_options_gb`, `cpu_options` by core count, drive size lists), and the
engine already carries the chosen index — `cpu_idx` at `app/recommend.py:781`,
and the RAM choice comes from `_pick_ram` over an ordered list. So an **ordinal**
penalty is directly implementable with no new data.

| Weight | Applies to | Default | What it expresses |
|---|---|---|---|
| `w_cpu_tier` | index into `cpu_options` | ~1.0 | How far up the CPU ladder this config reached. |
| `w_ram_tier` | index into `ram_options_gb` | ~1.0 | How far up the memory ladder. Captures the 128 GB / 1 TB / >1 TB steps for free, because the ladder *is* the steps. |
| `w_drive_tier` | index into the drive size list | ~1.0 | How far up the capacity ladder per drive. |
| `w_storage_only` | storage-only nodes in `fleet_tier` | ~0.5 | A storage-only node carries one low-tier CPU and minimum RAM. Today it weighs the same as a full HCI node, which overstates the cost of offloading. |
| `w_step` | crossing a declared platform boundary | ~0 initially | Optional explicit boundary penalty, for steps the ordinal index does not capture (e.g. RDIMM→LRDIMM, or a RAM total only reachable on a larger chassis). Start at 0; enable only if the ordinal weights prove insufficient. |
| `w_license` | banded licence cost (§5.4) | ~1.0 | Replaces `w_core_license`. |

Being ordinal rather than monetary is the point: an index into a ladder is a
structural property of the platform. It does not change when DRAM spikes.

### 7.3 Overlaps that must be disentangled during tuning

These are honest caveats, not reasons to skip the work:

- **`w_ram_tier` vs the waste term.** `_pick_ram` already selects the *smallest
  sufficient* RAM option for a given node count, so within one model the tier
  penalty is partly redundant. It bites across node counts and across models —
  3 nodes × 1024 GB versus 4 nodes × 768 GB — which is exactly where waste alone
  gives the wrong answer.
- **`w_cpu_tier` vs the licence bands.** Fewer cores is now already cheaper on
  licence up to 48C, so both terms push the same way below the cap. Above the
  cap they diverge: licence goes flat while `w_cpu_tier` keeps climbing. That
  divergence is correct — a 64C CPU still costs more to buy even when it is
  licence-free — but the two must be tuned together or the CPU ladder will be
  double-penalised.
- **`w_storage_only` vs the existing tiebreak.** `_rank_key` already prefers the
  most storage-only offload among equal-scoring candidates. The weight makes
  that a first-class term rather than a tiebreak, so the tiebreak may become
  redundant.

### 7.4 Setting the defaults

The ordinal weights are relative, so they need one calibration pass against real
sizings — §10. Starting position: set all three tier weights equal, confirm no
regression, then adjust individually based on which dimension the corpus shows
being over-bought.

---

## 8. Rename `cost_tier` → `platform_tier` **[needs sign-off]**

The field is a capability-tier weight that is merely named `cost`. That misnaming
generated the "shouldn't we put real prices in it?" question once and will again.

- Column rename on `Model`, with a migration.
- `MODEL_COSTS` → `PLATFORM_TIERS`, `DEFAULT_MODEL_COST` → `DEFAULT_PLATFORM_TIER`
  in `app/models.py:1081-1127`.
- `T.default_cost_tier` → `T.default_platform_tier` in `app/tunables.py:115`.
- Admin UI label: "Platform tier" with help text stating it is a **relative
  capability weight, not a price**, and that lower means a smaller/simpler
  platform.
- The docstring block above `_rank_key` (`app/recommend.py:547-560`) is updated
  to match.

Cheap, and it makes the design self-documenting.

---

## 9. Parked: hardware component pricing

Not being built. Retained here so the analysis is not lost.

**What it would have provided:** per-configuration hardware cost, by averaging
component prices over (class, capacity, form factor, DDR generation). Measured
spread in the Q4 2025 file: drives 1.07–1.83×, CPUs 1.10–1.33×, RAM 1.19–1.32×
once split by form factor and DDR generation (2.5–3.6× if averaged on capacity
alone, because SODIMM at €88 and RDIMM at €314 land in the same bucket). The SKU
family digit is a carrier/generation code with no published model mapping, which
is why averaging was the approach rather than exact matching.

**Why parked:** component prices are currently stratified by supply scarcity
rather than by product value, so using them as a ranking weight would produce
recommendations that change quarter to quarter for reasons unrelated to the
workload, and that no one can explain to a customer.

**Re-entry condition:** not a date. Revisit when component pricing has
re-stratified by capability for a couple of quarters running — a judgement call
for the team, not a calendar item. §7's ordinal weights are the stable
substitute in the meantime, and if pricing is ever adopted it would replace those
weights rather than sit alongside them.

**What would need building if resumed:** `price_component` / `price_chassis`
tables keyed by the averaging key; a `Model.memory_class` field
(`sodimm-ddr4` … `lrdimm-ddr4`) to pick the right DIMM ladder; a node cost model
summing chassis + CPU×sockets + DIMM×count + drives×count + NIC; and a widened
Salesforce feed carrying hardware prices.

---

## 10. Region, schema and testing

### 10.1 Region

- `User.region`, `Tenant.region` — both nullable (matching the existing
  `full_name` / `is_verified` precedent for fields that postdate accounts).
- `AppSetting`: `default_region`, `fallback_region`.
- Resolution: `user.region → tenant.region → default_region`.
- Tenant admins set an organisation default; users may override their own.
- `Configuration` (saved sizing) stamps `region` and the feed version at save
  time, so a reopened sizing reproduces exactly after the next import.
- No current feed for a region → use `fallback_region` and show a notice.
- Regions: `LATAM`, `NA`, `EMEA`, `APAC`.

**Note:** Cloud Unity DRaaS is priced per *datacenter* (Belgium / Frankfurt /
London / Montreal) — the DR target location, a separate axis from the customer's
purchasing region. Kept distinct.

### 10.2 Schema

```
catalog_feed        id, region, effective_date, label, uploaded_by,
                    uploaded_at, source_sha256, is_current
catalog_row         catalog_feed_id, sku, family, series, raw_description,
                    status(parsed|unmatched|placeholder), note
price_license_band  catalog_feed_id, edition, term_years, support_tier,
                    core_band, price
price_license_flat  catalog_feed_id, sku, kind, max_nodes, max_ram_gb,
                    term_years, price
price_license_rule  catalog_feed_id, edition, eligibility fields (§11)
```

**No price column on `Model`, `CpuCatalog`, `DriveCatalog` or any other
serialized table** — that is exactly how `cost_tier` leaked. Licence prices join
in `recommend.py` only.

The raw upload is **discarded after parsing**; store `source_sha256`, filename,
effective date and uploader for audit.

### 10.3 Re-tuning and verification

The score's terms change, so `w_cost` / `w_waste` / `node_overhead` /
`w_license` / the new tier weights need one calibration pass:

1. Snapshot current recommendations across the archived LiveOptics and RVTools
   files in `_archive/` — top-3 model and node count per sizing.
2. Apply the new scoring and sweep the tunables (adapt `_archive/sweep.py`).
3. Produce a before/after report. **Every changed recommendation must have a
   stated reason** — "2×32C now correctly licence-capped", "3-node Essentials
   config now wins", "no longer over-buying RAM two rungs up". Anything
   unexplained is a bug, not a tuning target.
4. Add the resulting expectations to `tests/` as regression cases.

### 10.4 Testing

| Area | Test |
|---|---|
| Parser | Golden-file test against the Q4 2025 sheet: expected row counts per family, expected licence band table, unmatched count of 0. |
| Parser robustness | Mangled fixtures — missing header, swapped D/E, extra family. Must fail loudly, not mis-parse. |
| Licence | Band lookup including both caps (48C standard, 24C BRS/Video) and Essentials eligibility at the 3-node / 256 GB boundaries. |
| Weights | Ordinal tier penalties move the expected direction; `w_storage_only` makes offloading cheaper than it is today. |
| Scoring | Regression corpus from §10.3 — recommendations change only where intended. |
| Profiles | BRS forces a single-node result; Video pins HCI nodes to the minimum. Both refused for non-Scale, non-super-admin users. |
| Pre-sale | Never appears in an unprompted recommendation; still sizes correctly when named explicitly. |
| Region | Fallback chain, saved-sizing reproducibility across a feed version bump. |
| Security | No price-shaped key in any API response or export. |

---

## 11. Licensing extensibility — what is data and what is code

Does every new licence type require a code change? Mostly no — but only if this
decomposition is respected.

| Layer | Example | Data or code? |
|---|---|---|
| **Price surface** | bands, caps, terms, support tiers, editions, regions | **Data.** Automatic — the parser stores whatever the feed contains. |
| **Eligibility constraints** | "max 3 nodes", "≤256 GB/node", "must be an SNS" | **Data**, given a small closed vocabulary. |
| **Sizing semantics** | BRS's 2.5×/1.5× multipliers, the failover fraction, `snapshot_pct = 0` | **Code.** And correctly so. |

### The key distinction

BRS's storage multipliers are **not a licence rule**. They are a fact about
Acronis backup topology — a backup plus a DR copy plus incrementals — and would
be true if the licence were free. They belong to a **workload profile**, not to a
licence SKU.

- **Licence definitions** are data: price bands plus an eligibility predicate.
  Adding a band, term, edition or region is zero code.
- **Workload profiles** (Standard, BRS, Video Surveillance) are a small explicit
  set of code-level presets. Adding one is code.
- A licence may *reference* a profile. A profile may exist with no licence.

New profiles correspond to genuinely new product motions, not to pricing changes,
so they are rare — and when one appears the sizing semantics are the real work.

### Eligibility vocabulary — closed, not a mini-language

A fixed set of named fields evaluated against a candidate. **No expression
strings, no `eval`.**

```
exact_nodes            max_nodes              min_hci_nodes
max_ram_gb_per_node    requires_single_node   max_cores_per_node
bundleable (bool)      role_gated (bool)      workload_class
```

That covers everything in the current catalog: Essentials
(`exact_nodes=3, max_ram_gb_per_node=256, bundleable=false`), BRS
(`requires_single_node=true, role_gated=true`), Video
(`min_hci_nodes=2, role_gated=true, workload_class=cctv`), Standard (no
constraints).

A rule the vocabulary cannot express must **fail loudly** at import or
admin-save. "We cannot represent this" is a visible error, not a silent
misprice.

### Where it lives

Mirror the existing tunables pattern — `app/tunables.py` declares schema and
defaults in code, the database holds live editable values, the super-admin page
edits them.

- `app/licensing.py` declares the **rule vocabulary and seeded rules** —
  version-controlled, reviewable, testable.
- `price_license_rule` rows hold the **live values**, regional and versioned,
  edited through the admin UI and audited.

"Where do I edit this?" then has one answer: the admin page for values, the repo
for a new predicate type.

### Expected maintenance burden

| Event | Work |
|---|---|
| Quarterly catalog change | None. Upload, review diff, apply. |
| New core band / term / region | None. Parser picks it up. |
| New edition (new SKU letter) | One admin row: name it, set eligibility. |
| Essentials RAM cap changes | One field edit. |
| Genuinely new product motion | Code — a new workload profile. Rare. |

---

## 12. Open items requiring a further discussion

**Nothing in §§5–8 should be implemented before the team agrees on these.**

1. **Does licensing get to change recommendations?** §5.4 replaces the linear
   core term with the real banded one. This will change which configuration wins
   in real deals. Everyone who fields "why did the tool pick that?" should agree
   in advance.
2. **The Essentials cliff.** Should the engine merely annotate a near-miss, or
   actively prefer 3-node / ≤256 GB designs? The plan currently annotates and lets
   the licence cost speak through the score.
3. **BRS day-one caps** (§6.1, item 2) — recommendation is to ignore them for
   BRS. Needs confirmation.
4. **BRS concurrent fraction: does it scale RAM as well as vCPU?** Assumed yes.
5. **The new ordinal weights** (§7) and their overlaps with the waste term and
   the licence bands. Needs agreement on the calibration corpus before tuning,
   because tuning against the wrong sizings bakes in the wrong preferences.
6. **`platform_tier` rename** (§8) — trivial but user-visible in the admin UI.
7. **Who owns the quarterly upload**, and what happens when it is missed.
8. **Re-entry condition for hardware pricing** (§9) — who decides, and on what
   evidence.

---

## Appendix: source

Analysis performed against
`_archive/Scale Computing Q4 2025 EUR Master Price List.xlsx` — 977 rows, one
sheet. Family breakdown: HyperCore License and Support 370, Component 195,
Subscription 129, 3rd Party Software 81, Services 62, Spare Parts 42,
Shipping 24, Chassis 24, Software License 13, Fleet Manager 11, other 26.
