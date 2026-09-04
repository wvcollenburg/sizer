"""Admin-tunable sizing & scoring constants, persisted in the SizingSetting
key/value table and editable on the super-admin Tuning page.

Why a singleton with attribute access instead of plain module constants:
the recommendation math reads these in many places across recommend.py and
the manual builder in app.py. Importing them by value (``from x import C``)
freezes each importer's copy, so an admin edit wouldn't take effect without a
restart. Instead callers import the ``T`` object and read ``T.<name>``;
``refresh_from_db()`` swaps the whole value dict in one reference assignment
(atomic in CPython — safe for this read-mostly pattern under sync workers, and
under threaded workers a reader always sees a complete old-or-new snapshot).

Defaults below are the canonical product values; they are also seeded into
SizingSetting so the values are visible/editable in the admin UI.
"""

# Each tunable: key, default, type ("int"/"float"), group, label, help, and
# optional min/max/step for the UI + server-side validation. The group/label/
# order drive the admin Tuning page, which is rendered from this metadata so a
# new tunable shows up automatically once added here.
TUNABLE_DEFS = [
    # ── Scoring weights ──────────────────────────────────────────────────────
    {"key": "w_cost", "default": 1.0, "type": "float", "group": "Scoring weights",
     "label": "Fleet-cost weight", "min": 0, "step": 0.1,
     "help": "Weight on total fleet cost = node_count × (model cost + per-node overhead).",
     "what": "How strongly the total hardware cost of a cluster influences its ranking. Fleet cost = number of nodes × (the model's per-node cost + per-node overhead).",
     "how": "Raise it to favour cheaper clusters (cheaper models, fewer nodes). Lower it to let fit and quality outweigh price.",
     "beware": "Very high values chase the cheapest box even when it fits poorly (tight headroom, more nodes to license). Zero ignores price entirely and can recommend needlessly expensive hardware."},
    {"key": "node_overhead", "default": 20.0, "type": "float", "group": "Scoring weights",
     "label": "Per-node overhead", "min": 0, "step": 1,
     "help": "Fixed cost added to each node (switches, rack U, power, ops). Raise to favour fewer, bigger nodes. THIS is the anti-sprawl knob since the 2026-09-02 tier re-base — platform_tier is now a price proxy and no longer carries that job.",
     "what": "A fixed cost added to every node on top of its appliance price — a stand-in for switch ports, rack units, power, and the operational effort each node carries.",
     "how": "Raise it to push toward fewer, larger nodes (consolidation). Lower it to make many small nodes look cheaper.",
     "beware": "Too high over-consolidates onto a few big boxes — coarser failure domains and a bigger hit when one node fails. Too low fragments the workload into many tiny nodes. Raised 12 -> 20 on 2026-09-02: re-basing platform_tier to price made 5XX Edge nodes ~3x cheaper in the score, and at 12 an 18-VM workload sprawled to SEVEN HE502 nodes with licensing off. At 20 that stops, and it changes NOTHING with licensing on — measured, so it is free insurance for the fallback path."},
    {"key": "w_core_license", "default": 1.5, "type": "float", "group": "Scoring weights",
     "label": "Core-licensing weight", "min": 0, "step": 0.1,
     "help": "Weight on total physical HCI cores (per-core licensing). Raise to favour fewer cores.",
     "what": "Cost per physical CPU core across the VM-running (HCI) nodes — models per-core licensing such as HyperCore plus guest OS/DB (Windows Datacenter, SQL Server, Oracle).",
     "how": "Raise it to favour configurations with fewer total cores (smaller CPUs, fewer VM-running nodes). Lower it to make core count matter less.",
     "beware": "Very high values minimise cores at the expense of everything else — spreading onto many small, low-core nodes (watch for node sprawl; balance with per-node overhead). Zero ignores licensing, often a customer's largest recurring cost."},

    # ── Licence-aware scoring (docs/pricebook-plan.md §5.4, §7) ──────────────
    # Live only when `license_scoring` is on. Until then the legacy
    # `w_core_license` term above governs, unchanged — so the switch is
    # reversible and no existing tuning is lost.
    {"key": "eur_per_tier_point", "default": 837.0, "type": "float", "group": "Scoring weights",
     "label": "Euro per platform-tier point", "min": 1, "step": 10,
     "help": "Bridges real licence cost to the abstract platform-tier scale. A POLICY DIAL, not a tracked price.",
     "what": "How much one platform-tier point is worth in euro, purely so the banded licence cost can be traded against hardware in the same score. Derived from configured-node prices divided by platform_tier ALONE (not tier + per-node overhead). 837 is the MEDIAN of the six-model reference basket in platform_tier.py; four of those six prices are approximate, so treat it as one significant figure. The whole platform_tier ladder was re-based to this rate on 2026-09-02, so every reference now agrees with it to within 0.4% BY CONSTRUCTION — that agreement is not evidence, it is arithmetic. Real evidence is a NEW price that still lands near the median.",
     "how": "Raise it to make licence cost matter LESS relative to hardware; lower it to make licence dominate. Review it deliberately, on evidence — do not chase the market with it.",
     "beware": "This is the one number relating money to the tier ladder, and the ladder is hand-set. Hardware prices move daily and non-uniformly (an HC1450D roughly doubled in 7 months while an HE153 did not), so the true balance drifts on its own. That drift is expected and is reported, not corrected — see the platform-tier divergence report."},
    {"key": "w_core_burden", "default": 0.90, "type": "float", "group": "Scoring weights",
     "label": "Per-core burden weight", "min": 0, "step": 0.05,
     "help": "Tier points per physical core for costs that NEVER cap — guest OS and database licensing.",
     "what": "The uncapped cost of a core once the Scale licence is priced separately: Windows Datacenter, SQL Server and Oracle all bill per physical core with no ceiling, and on a database estate that line routinely exceeds hardware and HyperCore combined.",
     "how": "Raise it to push toward fewer total cores. Scaled automatically by the sizing's guest-licensing exposure, so a pure-Linux estate already pays less here.",
     "beware": "This is the ONLY brake on core count once licence bands are live, because the Scale licence goes flat above its cap and the waste term saturates. Setting it to zero makes the largest available CPU free above the cap."},
    {"key": "w_cpu_perf", "default": 13.0, "type": "float", "group": "Scoring weights",
     "label": "CPU silicon weight", "min": 0, "step": 1,
     "help": "Tier points per 1000 SPECrate points — what the CPU itself costs, as opposed to what it is licensed at. Price-fitted 2026-09-02 to the real EUR 300-4,000 per-socket range.",
     "what": "Silicon cost, proxied by the benchmark index already on every candidate. A per-core term cannot tell a 3.6GHz Gold from a 2.9GHz Silver of the same core count; this can.",
     "how": "Raise it to favour slower/cheaper CPUs at equal core count. Tune together with the per-core burden weight — both push the same way below the licence cap and diverge above it. Sanity-check any change by multiplying it by a CPU's SPECrate/1000 and the euro-per-tier-point rate: the answer should look like that CPU's real price.",
     "beware": "KNOWN LIMITATION: SPECrate measures throughput, not SKU tier, so this underprices low-core high-tier parts — a Gold 5515+ (8C, perf 88) reads cheaper than a Xeon 6505P (12C, perf 142) when it actually costs more. Do not derive per-socket or per-family price deltas from this weight; ask someone who knows the price list. Was 72.7, set purely to preserve the anti-over-buy brake before the tier ladder became price-denominated. At that value the top catalog CPU implied EUR 21,480 against a real ceiling of EUR 4,000 — 5.4x — which quietly inflated every candidate and distorted the licence-vs-hardware balance. At 13 the implied range is EUR 163-3,841, matching reality, and the brake now rests where it honestly belongs: the uncapped per-core burden term. Uses a benchmark, not a price, so it still does not move with the market."},
    {"key": "cpu_perf_per_core_fallback", "default": 9.0, "type": "float", "group": "Scoring weights",
     "label": "Fallback perf index per core", "min": 0, "step": 0.5,
     "help": "Stand-in SPECrate-per-core when a catalog CPU has neither a SPECrate nor a PassMark score.",
     "what": "Keeps an unbenchmarked CPU from contributing zero silicon cost, which would make it look free and win on price.",
     "how": "Set near the catalog average (a 48-core dual node indexes around 438, i.e. ~9 per core).",
     "beware": "Every appliance CPU currently resolves to a real benchmark, so this should never fire. If it starts firing, the catalog has gained a CPU that needs benchmark data, not a tuning change."},
    {"key": "guest_exposure_none", "default": 0.6, "type": "float", "group": "Scoring weights",
     "label": "Guest exposure: none", "min": 0, "step": 0.1,
     "help": "Per-core burden multiplier when no core-licensed guest OS is present (e.g. a pure-Linux estate). Measured, not guessed — see below.",
     "what": "Multiplier applied to the per-core burden weight when the sizing declares no Windows and no core-billed database. A Linux estate still pays most of the per-core burden, because silicon and support scale with cores even when Microsoft does not.",
     "how": "Lower it to widen the gap between Linux and Windows estates. Re-run tools/license_sweep.py afterwards and check the kill-criterion section.",
     "beware": "0.6 is MEASURED, not chosen. Below it, toggling this dropdown starts moving NODE COUNTS on real sizings: at 0.0 two corpus sizings moved (3->2 and 6->4 nodes), at 0.2 and 0.4 one still did, and 0.6 is the first value where none do. A soft declared input that silently changes node count is worse than a coarse one — an SA cannot explain it."},
    {"key": "guest_exposure_windows", "default": 1.0, "type": "float", "group": "Scoring weights",
     "label": "Guest exposure: Windows", "min": 0, "step": 0.1,
     "help": "Per-core burden multiplier when Windows is present. The reference value.",
     "what": "The baseline. Windows Datacenter licenses every physical core of any host that might run a Windows VM, so exposure is near-binary rather than proportional to the Windows share.",
     "how": "Leave at 1.0 and express the other two relative to it.",
     "beware": "A customer running a few per-VM Standard licences instead of Datacenter is over-weighted here. The manual override is the escape hatch."},
    {"key": "guest_exposure_windows_db", "default": 1.6, "type": "float", "group": "Scoring weights",
     "label": "Guest exposure: Windows + core-billed DB", "min": 0, "step": 0.1,
     "help": "Per-core burden multiplier when a core-licensed database (SQL Server, Oracle) is also present.",
     "what": "Databases licensed per physical core, on top of the OS. Cannot be auto-detected — a database is invisible in the guest OS string — so this is always a declared value.",
     "how": "Raise it for Oracle-heavy estates, where the database line dwarfs everything else.",
     "beware": "Proposed, not validated against real deals. Treat the 1.6 as a starting point."},
    {"key": "w_waste", "default": 57.0, "type": "float", "group": "Scoring weights",
     "label": "Wasted-capacity weight", "min": 0, "step": 1,
     "help": "Weight on aggregate over-provisioning across CPU/RAM/storage. Raised 50 -> 57 on 2026-09-02 to match the 1.13x larger cost side that licence-aware scoring produces (measured by tools/license_sweep.py).",
     "what": "How heavily over-provisioning — buying more CPU, RAM or storage than the workload needs — is penalised overall.",
     "how": "Raise it to favour tight, right-sized fits. Lower it to tolerate loose fits when they're cheaper or use fewer nodes.",
     "beware": "Too high chases the tightest possible fit with almost no headroom, leaving the cluster fragile under growth or a node failure. Zero stops penalising oversizing — the original over-sizing behaviour returns."},
    {"key": "w_cpu", "default": 1.0, "type": "float", "group": "Scoring weights",
     "label": "CPU waste weight", "min": 0, "step": 0.1,
     "help": "Per-dimension weight on CPU over-provisioning within the waste term.",
     "what": "The share of the waste penalty that comes from spare CPU cores above what the workload requires.",
     "how": "Raise it to care more about not over-buying CPU. Lower it to tolerate CPU headroom.",
     "beware": "Extremes skew the balance between CPU, RAM and storage tightness — a very high value may pick a storage- or RAM-loose config just to shave CPU headroom."},
    {"key": "w_ram", "default": 1.0, "type": "float", "group": "Scoring weights",
     "label": "RAM waste weight", "min": 0, "step": 0.1,
     "help": "Per-dimension weight on RAM over-provisioning within the waste term.",
     "what": "The share of the waste penalty that comes from spare RAM above the workload's requirement.",
     "how": "Raise it to right-size RAM tightly. Lower it to tolerate RAM headroom.",
     "beware": "Very high values can starve the RAM headroom needed for bursts and HA. Imbalance versus the CPU/storage weights distorts which dimension drives the choice."},
    {"key": "w_stor", "default": 1.0, "type": "float", "group": "Scoring weights",
     "label": "Storage waste weight", "min": 0, "step": 0.1,
     "help": "Per-dimension weight on storage over-provisioning within the waste term.",
     "what": "The share of the waste penalty that comes from usable storage above the workload's requirement.",
     "how": "Raise it to right-size capacity tightly. Lower it to tolerate spare TB.",
     "beware": "High values penalise the large fixed disk sizes of hybrid and 2U nodes (which inherently over-provision capacity), potentially pushing toward flash even when bulk HDD is cheaper per TB."},
    {"key": "waste_cap", "default": 1.0, "type": "float", "group": "Scoring weights",
     "label": "Per-dimension waste cap", "min": 0, "step": 0.1,
     "help": "Caps each dimension's over-provisioning (1.0 = +100%) so one oversized axis can't dominate.",
     "what": "The maximum over-provisioning, per dimension, that counts toward waste. 1.0 means anything beyond +100% over the requirement is treated the same.",
     "how": "Lower it so even modest over-provisioning hits the ceiling quickly (waste differences flatten out). Raise it to let one very oversized dimension keep accumulating penalty.",
     "beware": "Very high (or effectively no) cap lets a single wildly-oversized axis — e.g. a hybrid node's 80 TB for a 4 TB need — dominate the whole score and crowd out cost and licensing. Very low cap makes every config look equally wasteful, neutralising the waste term."},
    {"key": "w_ghz_shortfall", "default": 40.0, "type": "float", "group": "Scoring weights",
     "label": "GHz-shortfall penalty", "min": 0, "step": 1,
     "help": "Penalty per 100% raw-GHz shortfall vs the source cluster (guards against under-powering).",
     "what": "A penalty applied when the cluster's total raw GHz falls below the source environment's, scaled by how far short it is.",
     "how": "Raise it to guard harder against under-powering when a high vCPU:core ratio is used (favours more or faster cores). Lower it to let aggressive consolidation through.",
     "beware": "Too high blocks legitimate consolidation and inflates node counts. Zero lets a high vCPU:core ratio quietly recommend a cluster with far less raw compute than the source."},

    # ── Sizing overheads ─────────────────────────────────────────────────────
    {"key": "os_core_overhead", "default": 1, "type": "int", "group": "Sizing overheads",
     "label": "OS cores per node", "min": 0, "step": 1,
     "help": "Cores reserved per node for the HyperCore OS.",
     "what": "Physical cores reserved on each node for the HyperCore OS, removed before counting the cores usable by VMs.",
     "how": "Raise it to reserve more — usable cores per node drop, so the engine needs bigger CPUs or more nodes to meet the workload.",
     "beware": "Too high and every node loses usable capacity (more or larger nodes, higher cost and licensing). Too low overstates usable compute and risks under-sizing."},
    {"key": "os_ram_gb", "default": 4, "type": "int", "group": "Sizing overheads",
     "label": "OS RAM per node (GB) — flat fallback", "min": 0, "step": 1,
     "help": "Flat OS RAM used when bay-count tiering can't apply (unknown bays, or a tier set to 0).",
     "what": "RAM reserved on each node for the HyperCore OS, used as the flat fallback when the bay-count tiers below don't apply (drive-bay count unknown, or the matching tier is set to 0). With the safety buffer, it's subtracted from each node's RAM before VM capacity is counted.",
     "how": "Raise it to reserve more — usable RAM per node drops, pushing toward larger RAM options or more nodes. The bay-count tiers below override this for nodes whose bay count is known.",
     "beware": "Too high wastes RAM and inflates sizing. Too low overstates usable RAM and can leave the OS starved in practice."},
    {"key": "os_ram_bays_1_4_gb", "default": 4, "type": "int", "group": "Sizing overheads",
     "label": "OS RAM — 1–4 drive bays (GB)", "min": 0, "step": 1,
     "help": "OS RAM for small nodes (1–4 drive bays). 0 = use the flat fallback.",
     "what": "OS RAM reserved on nodes with 1–4 drive bays. SCRIBE (the storage layer) consumes more RAM as drive count grows, so OS overhead is tiered by bay count.",
     "how": "Raise it to reserve more on small nodes. Set to 0 to disable this tier and fall back to the flat OS RAM value.",
     "beware": "Too low can starve SCRIBE on small edge nodes; too high needlessly inflates their sizing."},
    {"key": "os_ram_bays_5_12_gb", "default": 8, "type": "int", "group": "Sizing overheads",
     "label": "OS RAM — 5–12 drive bays (GB)", "min": 0, "step": 1,
     "help": "OS RAM for mid-size nodes (5–12 drive bays). 0 = use the flat fallback.",
     "what": "OS RAM reserved on nodes with 5–12 drive bays — the typical datacenter node range.",
     "how": "Raise it to reserve more on mid-size nodes. Set to 0 to disable this tier and fall back to the flat OS RAM value.",
     "beware": "Too low risks SCRIBE memory pressure under heavy I/O on densely-populated nodes."},
    {"key": "os_ram_bays_13_plus_gb", "default": 16, "type": "int", "group": "Sizing overheads",
     "label": "OS RAM — 13+ drive bays (GB)", "min": 0, "step": 1,
     "help": "OS RAM for large nodes (13+ drive bays). 0 = use the flat fallback.",
     "what": "OS RAM reserved on nodes with 13 or more drive bays — the largest, drive-dense configurations.",
     "how": "Raise it to reserve more on large nodes. Set to 0 to disable this tier and fall back to the flat OS RAM value.",
     "beware": "Too low under-reserves for SCRIBE on drive-dense nodes; too high wastes RAM that workloads could use."},
    {"key": "ram_buffer_gb", "default": 2, "type": "int", "group": "Sizing overheads",
     "label": "RAM safety buffer (GB)", "min": 0, "step": 1,
     "help": "Extra per-node RAM headroom. Usable-RAM overhead = OS RAM + this buffer.",
     "what": "Extra per-node RAM kept free beyond the OS reservation. Usable-RAM overhead = OS RAM + this buffer.",
     "how": "Raise it to leave more breathing room (lower usable RAM, possibly bigger or more nodes). Lower it to pack RAM tighter.",
     "beware": "Too low risks memory pressure under load or during HA failover. Too high needlessly grows RAM sizing and cost."},
    {"key": "default_cost_tier", "default": 5.0, "type": "float", "group": "Sizing overheads",
     "label": "Fallback model cost", "min": 0, "step": 1,
     "help": "Cost weight used when a model has no cost set.",
     "what": "The cost weight applied to any model that has no per-model cost configured.",
     "how": "It only affects models missing a cost value; raising or lowering it shifts how those unpriced models rank.",
     "beware": "If set far from real model costs, any unpriced model is unfairly favoured or buried. Best practice is to set explicit per-model costs so this is rarely used."},

    # ── Sizing defaults ──────────────────────────────────────────────────────
    {"key": "default_vcpu_ratio", "default": 3.0, "type": "float", "group": "Sizing defaults",
     "label": "Default vCPU:core ratio", "min": 1, "max": 10, "step": 0.5,
     "help": "Starting vCPU:core consolidation ratio every sizing opens at (the UI slider default).",
     "what": "The vCPU-to-physical-core consolidation ratio every new sizing starts from — the initial position of the ratio slider and the value used for the first recommendation, regardless of the source environment's measured ratio (which is still shown for reference).",
     "how": "Raise it to consolidate harder by default (fewer/denser nodes, less hardware). Lower it toward 1:1 to size more conservatively out of the box. Users can still override per-sizing with the slider.",
     "beware": "Set well above what the workloads tolerate and the default recommendation under-provisions CPU until the user notices and dials it back. Set to 1:1 and every sizing starts as large as the source estate, defeating the point of a consolidation default."},
    {"key": "max_day_one_storage_pct", "default": 50, "type": "int", "group": "Sizing defaults",
     "label": "Max day-one storage consumption (%)", "min": 1, "max": 100, "step": 1,
     "help": "Cap on today's storage use as a % of full-cluster usable capacity. Sizes in extra headroom on top of growth/snapshots. 100 = no cap.",
     "what": "The maximum share of full-cluster usable storage that today's (pre-growth) workload may consume. The engine adds nodes/disks until day-one use sits at or below this, on top of the growth + snapshot reserve. Measured against the full cluster because RF replication already covers a node loss, so storage capacity is the same at N-1.",
     "how": "Lower it to demand more day-one storage headroom (bigger clusters, more breathing room before growth bites). Raise it toward 100 to size storage tight to projected demand only (100 disables the cap).",
     "beware": "Low values inflate every recommendation's node/disk count and cost. At 100 the cap is off and a fast-filling workload can ship near-full on day one."},
    {"key": "max_day_one_ram_pct", "default": 50, "type": "int", "group": "Sizing defaults",
     "label": "Max day-one RAM consumption (%)", "min": 1, "max": 100, "step": 1,
     "help": "Cap on today's RAM use as a % of N-1 available RAM. Sizes in headroom so the workload still fits with a node down, even at projected end-state. 100 = no cap.",
     "what": "The maximum share of N-1 available RAM that today's (pre-growth) workload may consume. Measured against N-1 (one node down) so that even at the projected end-state — roughly 2× day-one over a typical 5-year growth — the workload still fits during a node failure. Because N-1 capacity is smaller than the full cluster, the on-card RAM bar (measured vs full cluster) reads lower than this percentage.",
     "how": "Lower it to demand more RAM failover headroom. Raise it toward 100 to size RAM tight to projected demand only (100 disables the cap).",
     "beware": "Low values noticeably enlarge RAM (more or larger DIMMs, more nodes) and cost. At 100 the cap is off and RAM is sized only to fit projected demand at N-1."},

    # ── Advisory thresholds ──────────────────────────────────────────────────
    {"key": "vm_density_warn_per_node", "default": 100, "type": "int", "group": "Advisory thresholds",
     "label": "VM-density warning (VMs/node)", "min": 1, "step": 5,
     "help": "Warn when every recommendation packs more than this many VMs onto each VM-running node.",
     "what": "The VMs-per-node ceiling above which a sizing is flagged as dense. When even the most-spread recommendation exceeds it, an advisory warns about scheduling/management overhead.",
     "how": "Lower it to flag density sooner (favouring more nodes). Raise it to tolerate denser packing before warning.",
     "beware": "Set very high and the advisory never fires, so genuinely over-dense clusters ship unflagged. Set very low and almost every sizing carries a noise warning users learn to ignore."},

    # ── Cluster topology ─────────────────────────────────────────────────────
    {"key": "max_nodes_per_cluster", "default": 8, "type": "int", "group": "Cluster topology",
     "label": "Max nodes per cluster", "min": 1, "step": 1,
     "help": "Nodes per HyperCore cluster before the build splits into multiple clusters.",
     "what": "The largest number of nodes in a single HyperCore cluster before a build is split into multiple clusters.",
     "how": "Lower it to split big deployments into more, smaller clusters (more witnesses and management domains). Raise it to pack more nodes per cluster.",
     "beware": "Above the platform's supported maximum it produces configurations that can't actually be deployed. Very low values multiply cluster count, overhead and spare nodes."},
    {"key": "min_hci_nodes_per_cluster", "default": 2, "type": "int", "group": "Cluster topology",
     "label": "Min HCI nodes per cluster", "min": 1, "step": 1,
     "help": "Minimum VM-running nodes per cluster (HA + rolling updates).",
     "what": "The minimum number of VM-running (HCI) nodes each cluster must have, for HA and rolling updates.",
     "how": "Raise it to force more compute nodes per cluster (limiting how much can be offloaded to storage-only nodes). Lower it to allow leaner clusters.",
     "beware": "Below 2 removes the HA/rolling-update safety margin — a single node loss or an update can take the cluster down. High values block storage-only offload and inflate node counts."},
    {"key": "storage_only_ram_floor_gb", "default": 16, "type": "int", "group": "Cluster topology",
     "label": "Storage-only RAM floor (GB)", "min": 0, "step": 1,
     "help": "Minimum RAM on a storage-only node.",
     "what": "The minimum RAM configured on a storage-only node (which runs no VMs but participates in the storage cluster).",
     "how": "Raise it to give storage-only nodes more RAM (at extra cost). Lower it to trim them toward the minimum the platform tolerates.",
     "beware": "Too low may starve the storage stack on those nodes. Too high wastes money on nodes that run no workloads."},

    # ── Validated (software-only) limits ─────────────────────────────────────
    {"key": "max_cluster_disks", "default": 100, "type": "int", "group": "Validated limits",
     "label": "Max disks per cluster", "min": 1, "step": 1,
     "help": "Software-only hard cap on data disks per cluster.",
     "what": "The hard cap on data disks per cluster used when sizing software-only (Validated) configurations.",
     "how": "Lower it to force fewer disks per cluster (more nodes or clusters for the same capacity). Raise it to allow denser disk counts.",
     "beware": "Above the supported limit it yields unsupportable Validated configs. Set very low, capacity-heavy workloads become infeasible or split into many clusters."},
    {"key": "hybrid_flash_min_pct", "default": 7.0, "type": "float", "group": "Validated limits",
     "label": "Hybrid flash floor (%)", "min": 0, "max": 100, "step": 0.1,
     "help": "Validated hybrid: minimum flash tier as a % of raw capacity.",
     "what": "The minimum flash tier, as a percentage of raw capacity, for a Validated hybrid configuration to be considered valid.",
     "how": "Raise it to require more flash relative to spinning capacity (filtering out flash-light hybrids). Lower it to permit leaner flash.",
     "beware": "Too high eliminates most hybrid options, forcing all-flash or no fit. Too low allows hybrids with too little cache to perform as expected."},
    {"key": "hybrid_flash_max_pct", "default": 25.0, "type": "float", "group": "Validated limits",
     "label": "Hybrid flash ceiling (%)", "min": 0, "max": 100, "step": 0.1,
     "help": "Validated hybrid: maximum flash tier as a % of raw capacity.",
     "what": "The maximum flash tier, as a percentage of raw capacity, before a configuration is treated as all-flash rather than hybrid.",
     "how": "Raise it to let hybrids carry more flash. Lower it to restrict hybrids to a thinner flash tier.",
     "beware": "Must stay above the floor — the engine rejects a floor greater than the ceiling. Extreme values distort the hybrid/all-flash boundary and can exclude valid designs."},
    {"key": "hybrid_min_hdd_per_flash", "default": 3, "type": "int", "group": "Validated limits",
     "label": "Min HDDs per flash disk (hybrid)", "min": 1, "step": 1,
     "help": "Validated hybrid: minimum slow-tier (HDD) disks required per fast-tier (SSD/NVMe) disk. Best practice is 3.",
     "what": "The minimum ratio of slow-tier (HDD) to fast-tier (SSD/NVMe) disks in a Validated hybrid node. At the default of 3, every flash disk must be backed by at least 3 HDDs. Certified appliances already encode this in their fixed disk layouts; this enforces it on Validated configs, where disk counts can flex.",
     "how": "Raise it to demand even more spinning capacity behind each flash disk. Lower it toward 1 to only require more HDDs than flash. Set to 1 to effectively disable the best-practice floor.",
     "beware": "This guards HEAT down-tiering: too few HDDs and the spinning tier can't absorb cold data evicted from flash, so the HDDs bottleneck under tiering pressure. Raising it can make some Validated hybrids infeasible (more disks/nodes needed)."},
    {"key": "license_scoring", "default": 1, "type": "int", "group": "Sizing defaults",
     "label": "Licence-aware scoring (0/1)", "min": 0, "max": 1, "step": 1,
     "help": "When 1 (default since 2026-09-02), score using the real banded SC//HyperCore licence cost plus the uncapped per-core burden and CPU silicon terms, instead of the legacy flat per-core weight.",
     "what": "Replaces the single invented per-core term with the real cost structure: the Scale licence banded and CAPPED (cores past the cap are genuinely free), guest OS/DB licensing which never caps, and CPU silicon proxied by benchmark. It also makes the Essentials Kit cliff visible, so a 3-node cluster at or under 256 GB/node can win on price the way it does in a real quote.",
     "how": "Set to 0 to fall back to the legacy flat per-core term. If you do, also drop the wasted-capacity weight back to ~50: it was raised to 67 to match the larger cost side this switch produces, and leaving it high with the switch off over-penalises over-provisioning.",
     "beware": "Needs a current licence price feed for the sizing's region; with no feed the licence term contributes nothing and the sizing silently falls back to the legacy path. The cost side is 1.34x larger with this on (measured), which is why the wasted-capacity weight ships at 67 rather than 50."},
    {"key": "default_license_term_years", "default": 5, "type": "int", "group": "Sizing defaults",
     "label": "Default licence term (years)", "min": 1, "max": 5, "step": 1,
     "help": "Default term for licence pricing. Independent of the growth projection horizon — customers routinely buy 3 years of licence while sizing 5 years of growth.",
     "what": "How many years of licence and support the cost model prices. Deliberately separate from the projection horizon, which sizes capacity.",
     "how": "Leave at 5 to match the default growth horizon; an SA can override per sizing in advanced options.",
     "beware": "Scale renews to 7 years but does not sell 7 up front, so the range stops at 5. Changing the term moves licence cost roughly proportionally, which shifts the licence/hardware balance in the score."},
    {"key": "perf_scaling", "default": 1, "type": "int", "group": "Sizing defaults",
     "label": "CPU performance scaling (0/1)", "min": 0, "max": 1, "step": 1,
     "help": "When 1 (default), add an active compute floor so the cluster must deliver at least the source environment's measured (peak-utilization) compute demand, grown to the horizon — blended between raw clock (GHz×cores) and benchmark throughput (SPECrate/PassMark) per the Perf vs GHz balance. Core sizing (vCPU:core) still applies as a separate floor. Set to 0 to disable (sizing uses cores and clock only; the perf comparison becomes advisory).",
     "what": "A switch that turns the CPU performance index from advisory (display only) into an active sizing floor. When on, the engine sizes compute to the throughput the source actually consumes (its nameplate scaled by measured peak CPU utilization, then grown), instead of trusting raw core counts across CPU generations — so an old-but-many-core source isn't over-provisioned, and a lightly-loaded source isn't sized to 100% of its nameplate. The floor is a blend of clock and benchmark throughput controlled by the Perf vs GHz balance, and sits alongside (does not replace) the vCPU:core core floor.",
     "how": "Leave at 1 to let measured compute demand influence node counts, then tune the Perf vs GHz balance. Set to 0 to fall back to pure core/clock-based sizing and treat the perf comparison as informational only. With no source benchmark entered the floor degrades to a pure GHz-utilization floor.",
     "beware": "The SPECrate<->PassMark conversion is ~20% approximate and PassMark is burst-oriented, so validate the resulting node counts against a few known sizings. With no measured utilization available the floor conservatively assumes 100% (nameplate), which over-sizes — confirm the import carries peak CPU figures."},
    {"key": "perf_ghz_balance", "default": 0.5, "type": "float", "group": "Sizing defaults",
     "label": "Perf vs GHz balance (0–1)", "min": 0, "max": 1, "step": 0.05,
     "help": "Only used when CPU performance scaling is on. Weights the active compute floor between raw clock (GHz×cores) and benchmark throughput (SPECrate/PassMark). 0 = pure GHz, 1 = pure benchmark, 0.5 = equal blend. The floor requires a weighted average of the two coverage ratios to reach 1.0.",
     "what": "How much the active compute floor trusts the benchmark (SPECrate/PassMark) versus raw clock (GHz×cores). Each signal is turned into a coverage ratio — cluster compute ÷ source demand — and this is the benchmark's weight in the blend of the two (GHz gets the remainder). The floor is met when the blended coverage reaches 100%.",
     "how": "Raise it toward 1 to lean on the benchmark, which captures generational IPC (a modern core out-works an old one at the same GHz). Lower it toward 0 to lean on raw clock, the more conservative signal. 0.5 weights them equally.",
     "beware": "Only has an effect when CPU performance scaling is on AND a source benchmark is entered — otherwise the floor falls back to whichever signal has data (pure GHz with no benchmark). Leaning fully on the benchmark (1) inherits the SPECrate/PassMark approximation; leaning fully on GHz (0) ignores IPC and can over-size on modern hardware."},
    {"key": "w_pcore", "default": 1.0, "type": "float", "group": "Core weighting",
     "label": "P-core weight", "min": 0, "max": 2, "step": 0.05,
     "help": "Weight applied to each Performance core when computing a CPU's effective (licensable) core count for sizing. Default 1.0 = a P-core counts as one core.",
     "what": "How much each Intel Performance core (or any core on a non-hybrid CPU) counts toward the effective core count the engine sizes and licenses against. Hybrid CPUs store their true total cores; this weight plus the E-core weight derive the count that actually drives node sizing.",
     "how": "Leave at 1.0 normally. Lower it only if you want P-cores to count for less than a full core in sizing.",
     "beware": "0 here would make P-cores free — almost never what you want. Non-hybrid CPUs are all P-cores, so this scales their whole core count."},
    {"key": "w_ecore", "default": 0.0, "type": "float", "group": "Core weighting",
     "label": "E-core weight", "min": 0, "max": 2, "step": 0.05,
     "help": "Weight applied to each Efficiency core when computing a CPU's effective (licensable) core count. Default 0.0 = E-cores carry no weight (only P-cores are licensed today).",
     "what": "How much each Intel Efficiency core counts toward the effective core count used for sizing/licensing. At the default 0.0, hybrid CPUs are sized purely on their P-cores even though their true total core count is stored.",
     "how": "Raise it (e.g. toward 0.5 or 1.0) if/when the product licenses E-cores or you want them to contribute to compute sizing.",
     "beware": "Raising this increases the effective cores of hybrid CPUs, which lowers the node count they need — only change it when the licensing model actually changes."},
]

DEFAULTS = {d["key"]: d["default"] for d in TUNABLE_DEFS}
_TYPES = {d["key"]: d["type"] for d in TUNABLE_DEFS}


def _coerce(key, value):
    """Cast a stored/incoming value to the tunable's declared type. Ints stay
    ints so they remain valid in range()/count contexts and don't render as
    '23.0' in the UI."""
    return int(round(float(value))) if _TYPES.get(key) == "int" else float(value)


class _Tunables:
    """Holds the live tunable values. Reads via attribute access (``T.w_cost``);
    writes only via ``set_values`` (whole-dict swap)."""
    __slots__ = ("_v",)

    def __init__(self):
        object.__setattr__(self, "_v", dict(DEFAULTS))

    def __getattr__(self, name):
        # Only reached for names not found normally (i.e. tunable keys).
        try:
            return self._v[name]
        except KeyError as e:
            raise AttributeError(name) from e

    @property
    def usable_ram_overhead(self):
        """Per-node RAM not available to VMs = flat OS RAM + safety buffer. Used
        where a node's drive-bay count isn't known; prefer
        ``usable_ram_overhead_for(bays)`` when it is."""
        return self._v["os_ram_gb"] + self._v["ram_buffer_gb"]

    def os_ram_for_bays(self, bays):
        """OS RAM reserved per node, tiered by drive-bay count (SCRIBE scales with
        drive count). Falls back to the flat ``os_ram_gb`` when the bay count is
        unknown (<=0) or the matching tier is disabled (set to 0)."""
        flat = self._v["os_ram_gb"]
        if not bays or bays <= 0:
            return flat
        if bays <= 4:
            tier = self._v["os_ram_bays_1_4_gb"]
        elif bays <= 12:
            tier = self._v["os_ram_bays_5_12_gb"]
        else:
            tier = self._v["os_ram_bays_13_plus_gb"]
        return tier if tier > 0 else flat

    def usable_ram_overhead_for(self, bays):
        """Per-node RAM not available to VMs for a node with ``bays`` drive bays =
        bay-tiered OS RAM + safety buffer."""
        return self.os_ram_for_bays(bays) + self._v["ram_buffer_gb"]

    def set_values(self, overrides):
        """Atomically replace the value set: defaults overlaid with any provided
        (and recognised) overrides, each coerced to its declared type."""
        v = dict(DEFAULTS)
        for k, val in (overrides or {}).items():
            if k in DEFAULTS and val is not None:
                try:
                    v[k] = _coerce(k, val)
                except (TypeError, ValueError):
                    pass  # keep the default for an unparseable stored value
        object.__setattr__(self, "_v", v)


T = _Tunables()


def refresh_from_db():
    """Reload tunables from SizingSetting into ``T``. Call at the top of each
    sizing entry point. Missing keys fall back to defaults."""
    from orm_models import SizingSetting
    rows = {s.key: s.value for s in SizingSetting.query
            .filter(SizingSetting.key.in_(list(DEFAULTS.keys()))).all()}
    T.set_values(rows)
