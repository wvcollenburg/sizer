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
     "help": "Weight on total fleet cost = node_count × (model cost + per-node overhead)."},
    {"key": "node_overhead", "default": 12.0, "type": "float", "group": "Scoring weights",
     "label": "Per-node overhead", "min": 0, "step": 1,
     "help": "Fixed cost added to each node (switches, rack U, power, ops). Raise to favour fewer, bigger nodes."},
    {"key": "w_core_license", "default": 1.5, "type": "float", "group": "Scoring weights",
     "label": "Core-licensing weight", "min": 0, "step": 0.1,
     "help": "Weight on total physical HCI cores (per-core licensing). Raise to favour fewer cores."},
    {"key": "w_waste", "default": 50.0, "type": "float", "group": "Scoring weights",
     "label": "Wasted-capacity weight", "min": 0, "step": 1,
     "help": "Weight on aggregate over-provisioning across CPU/RAM/storage."},
    {"key": "w_cpu", "default": 1.0, "type": "float", "group": "Scoring weights",
     "label": "CPU waste weight", "min": 0, "step": 0.1,
     "help": "Per-dimension weight on CPU over-provisioning within the waste term."},
    {"key": "w_ram", "default": 1.0, "type": "float", "group": "Scoring weights",
     "label": "RAM waste weight", "min": 0, "step": 0.1,
     "help": "Per-dimension weight on RAM over-provisioning within the waste term."},
    {"key": "w_stor", "default": 1.0, "type": "float", "group": "Scoring weights",
     "label": "Storage waste weight", "min": 0, "step": 0.1,
     "help": "Per-dimension weight on storage over-provisioning within the waste term."},
    {"key": "waste_cap", "default": 1.0, "type": "float", "group": "Scoring weights",
     "label": "Per-dimension waste cap", "min": 0, "step": 0.1,
     "help": "Caps each dimension's over-provisioning (1.0 = +100%) so one oversized axis can't dominate."},
    {"key": "w_ghz_shortfall", "default": 40.0, "type": "float", "group": "Scoring weights",
     "label": "GHz-shortfall penalty", "min": 0, "step": 1,
     "help": "Penalty per 100% raw-GHz shortfall vs the source cluster (guards against under-powering)."},

    # ── Sizing overheads ─────────────────────────────────────────────────────
    {"key": "os_core_overhead", "default": 1, "type": "int", "group": "Sizing overheads",
     "label": "OS cores per node", "min": 0, "step": 1,
     "help": "Cores reserved per node for the HyperCore OS."},
    {"key": "os_ram_gb", "default": 4, "type": "int", "group": "Sizing overheads",
     "label": "OS RAM per node (GB)", "min": 0, "step": 1,
     "help": "RAM reserved per node for the HyperCore OS."},
    {"key": "ram_buffer_gb", "default": 2, "type": "int", "group": "Sizing overheads",
     "label": "RAM safety buffer (GB)", "min": 0, "step": 1,
     "help": "Extra per-node RAM headroom. Usable-RAM overhead = OS RAM + this buffer."},
    {"key": "default_cost_tier", "default": 5.0, "type": "float", "group": "Sizing overheads",
     "label": "Fallback model cost", "min": 0, "step": 1,
     "help": "Cost weight used when a model has no cost set."},

    # ── Cluster topology ─────────────────────────────────────────────────────
    {"key": "max_nodes_per_cluster", "default": 8, "type": "int", "group": "Cluster topology",
     "label": "Max nodes per cluster", "min": 1, "step": 1,
     "help": "Nodes per HyperCore cluster before the build splits into multiple clusters."},
    {"key": "min_hci_nodes_per_cluster", "default": 2, "type": "int", "group": "Cluster topology",
     "label": "Min HCI nodes per cluster", "min": 1, "step": 1,
     "help": "Minimum VM-running nodes per cluster (HA + rolling updates)."},
    {"key": "storage_only_ram_floor_gb", "default": 16, "type": "int", "group": "Cluster topology",
     "label": "Storage-only RAM floor (GB)", "min": 0, "step": 1,
     "help": "Minimum RAM on a storage-only node."},

    # ── Validated (software-only) limits ─────────────────────────────────────
    {"key": "max_cluster_disks", "default": 100, "type": "int", "group": "Validated limits",
     "label": "Max disks per cluster", "min": 1, "step": 1,
     "help": "Software-only hard cap on data disks per cluster."},
    {"key": "hybrid_flash_min_pct", "default": 7.0, "type": "float", "group": "Validated limits",
     "label": "Hybrid flash floor (%)", "min": 0, "max": 100, "step": 0.1,
     "help": "Validated hybrid: minimum flash tier as a % of raw capacity."},
    {"key": "hybrid_flash_max_pct", "default": 24.3, "type": "float", "group": "Validated limits",
     "label": "Hybrid flash ceiling (%)", "min": 0, "max": 100, "step": 0.1,
     "help": "Validated hybrid: maximum flash tier as a % of raw capacity."},
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
