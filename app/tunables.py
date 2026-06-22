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
    {"key": "node_overhead", "default": 12.0, "type": "float", "group": "Scoring weights",
     "label": "Per-node overhead", "min": 0, "step": 1,
     "help": "Fixed cost added to each node (switches, rack U, power, ops). Raise to favour fewer, bigger nodes.",
     "what": "A fixed cost added to every node on top of its appliance price — a stand-in for switch ports, rack units, power, and the operational effort each node carries.",
     "how": "Raise it to push toward fewer, larger nodes (consolidation). Lower it to make many small nodes look cheaper.",
     "beware": "Too high over-consolidates onto a few big boxes — coarser failure domains and a bigger hit when one node fails. Too low (or zero) fragments the workload into many tiny nodes, inflating switch/rack/licensing costs the model can't otherwise see."},
    {"key": "w_core_license", "default": 1.5, "type": "float", "group": "Scoring weights",
     "label": "Core-licensing weight", "min": 0, "step": 0.1,
     "help": "Weight on total physical HCI cores (per-core licensing). Raise to favour fewer cores.",
     "what": "Cost per physical CPU core across the VM-running (HCI) nodes — models per-core licensing such as HyperCore plus guest OS/DB (Windows Datacenter, SQL Server, Oracle).",
     "how": "Raise it to favour configurations with fewer total cores (smaller CPUs, fewer VM-running nodes). Lower it to make core count matter less.",
     "beware": "Very high values minimise cores at the expense of everything else — spreading onto many small, low-core nodes (watch for node sprawl; balance with per-node overhead). Zero ignores licensing, often a customer's largest recurring cost."},
    {"key": "w_waste", "default": 50.0, "type": "float", "group": "Scoring weights",
     "label": "Wasted-capacity weight", "min": 0, "step": 1,
     "help": "Weight on aggregate over-provisioning across CPU/RAM/storage.",
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
     "label": "OS RAM per node (GB)", "min": 0, "step": 1,
     "help": "RAM reserved per node for the HyperCore OS.",
     "what": "RAM reserved on each node for the HyperCore OS. With the safety buffer, it's subtracted from each node's RAM before VM capacity is counted.",
     "how": "Raise it to reserve more — usable RAM per node drops, pushing toward larger RAM options or more nodes.",
     "beware": "Too high wastes RAM and inflates sizing. Too low overstates usable RAM and can leave the OS starved in practice."},
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
    {"key": "hybrid_flash_max_pct", "default": 24.3, "type": "float", "group": "Validated limits",
     "label": "Hybrid flash ceiling (%)", "min": 0, "max": 100, "step": 0.1,
     "help": "Validated hybrid: maximum flash tier as a % of raw capacity.",
     "what": "The maximum flash tier, as a percentage of raw capacity, before a configuration is treated as all-flash rather than hybrid.",
     "how": "Raise it to let hybrids carry more flash. Lower it to restrict hybrids to a thinner flash tier.",
     "beware": "Must stay above the floor — the engine rejects a floor greater than the ceiling. Extreme values distort the hybrid/all-flash boundary and can exclude valid designs."},
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
        """Per-node RAM not available to VMs = OS RAM + safety buffer."""
        return self._v["os_ram_gb"] + self._v["ram_buffer_gb"]

    def as_dict(self):
        return dict(self._v)

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
