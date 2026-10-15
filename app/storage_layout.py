"""One description of a model's drive layout, shared by the calculator and
the recommendation engine.

A storage config (``Model.to_dict()["storage"]``, or the seed/admin dict it
was built from) names a ``type`` and the per-tier drive counts that type
needs. Every consumer used to re-derive "how many drives of which kind" with
its own fallbacks, and the fallbacks disagreed (4 SSDs here, 1 there, 0 for
the RAM tier). A missing count on a type that needs one is a catalog error
(owner decision), so this module raises instead of guessing.

Drive kinds are the lower-case option-key prefixes: ``hdd``, ``ssd``, ``nvme``
(``<kind>_options_tb`` lists the certified sizes, ``<kind>_tb`` is a chosen
size).
"""

# type -> [(kind, count_key)]. A None count_key is a fixed single drive.
_LAYOUTS = {
    "nvme_only": [("nvme", "drives_per_node")],
    "ssd_only": [("ssd", "drives_per_node")],
    "hdd_only": [("hdd", "drives_per_node")],
    "hybrid": [("hdd", "hdd_count"), ("ssd", "ssd_count")],
    "hybrid_nvme": [("hdd", "hdd_count"), ("nvme", "nvme_count")],
    "nvme_and_ssd": [("nvme", None), ("ssd", None)],
    "cloud": [],
}

STORAGE_TYPES = tuple(_LAYOUTS)


class StorageLayoutError(ValueError):
    """A storage config lacks a drive count its type requires."""


def _describe(storage):
    return "storage config %r" % ({k: v for k, v in storage.items()
                                   if not k.endswith("_options_tb")},)


def drive_slots(storage):
    """[(kind, count)] for one node, e.g. hybrid -> [("hdd", 3), ("ssd", 1)],
    nvme_and_ssd -> [("nvme", 1), ("ssd", 1)], ssd_only -> [("ssd", n)].
    Cloud and unrecognised types have no drives ([]). Raises
    StorageLayoutError when a required count is missing or not a positive
    integer."""
    stype = storage.get("type", "")
    layout = _LAYOUTS.get(stype)
    if layout is None:
        return []
    slots = []
    for kind, key in layout:
        if key is None:
            count = 1
        else:
            count = storage.get(key)
            if count is None:
                raise StorageLayoutError(
                    "%s storage needs %r, missing from %s" % (stype, key, _describe(storage)))
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise StorageLayoutError(
                    "%s storage needs a positive integer %r, got %r in %s"
                    % (stype, key, count, _describe(storage)))
        slots.append((kind, count))
    return slots


def slot_count(storage, kind):
    """Drives of one kind in a node (0 when the type has no such tier)."""
    return dict(drive_slots(storage)).get(kind, 0)


def drive_count(storage):
    """Physical drives in one node (0 for cloud)."""
    return sum(count for _, count in drive_slots(storage))


def raw_per_node(storage, sizes_tb):
    """Raw TB in one node given a chosen size per kind ({"hdd": 8, "ssd": 1.92})."""
    return sum(sizes_tb[kind] * count for kind, count in drive_slots(storage))


def biggest_disk_tb(storage, sizes_tb):
    """Largest chosen drive in the node (0 when it has none)."""
    sizes = [sizes_tb[kind] for kind, _ in drive_slots(storage)]
    return max(sizes) if sizes else 0


def max_raw_per_node(storage):
    """(raw_tb, biggest_disk_tb) with every tier at its largest certified
    size — the most storage one node of this model can hold. (0, 0) when the
    model has no drives or any tier lists no sizes."""
    slots = drive_slots(storage)
    if not slots:
        return 0, 0
    raw = 0
    biggest = 0
    for kind, count in slots:
        opts = storage.get("%s_options_tb" % kind, [])
        if not opts:
            return 0, 0
        size = max(opts)
        raw += size * count
        biggest = max(biggest, size)
    return raw, biggest
