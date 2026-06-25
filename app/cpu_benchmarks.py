"""Broad CPU benchmark lookup for SOURCE CPUs.

Average published SPECrate2017_int_base (per socket) for ~780 CPUs, aggregated
from *all* published SPEC CPU 2017 integer-rate results. This scores the
arbitrary CPUs detected on a Live Optics / RVTools import — old/odd parts that
are almost never in our own appliance catalog (cpu_specs.py).

Data file: data/specrate_lookup.json (regenerate with
tools/build_specrate_lookup.py from a fresh SPEC results export). It's reference
data, read-only at runtime, so it's loaded into memory here rather than a DB
table — fast O(1) lookup, no migration, swap the JSON to refresh.
"""
import json
import os
import re

_PATH = os.path.join(os.path.dirname(__file__), "data", "specrate_lookup.json")
try:
    with open(_PATH) as _f:
        _DATA = json.load(_f)
except (OSError, ValueError):
    _DATA = {}


def normalize_cpu(name):
    """Normalise a CPU description to a stable match key. Strips (R)/(TM), clock
    speeds, 'CPU'/'Processor', 'N-Core', punctuation; lowercases. So
    'Intel(R) Xeon(R) CPU E5-2680 v4 @ 2.40GHz', 'Intel Xeon E5-2680 v4)' and
    'Intel Xeon E5-2680 v4' all map to the same key."""
    s = (name or "").lower()
    s = re.sub(r"\(r\)|\(tm\)|®|™", " ", s)
    s = re.sub(r"@?\s*[\d.]+\s*[gm]hz", " ", s)   # @ 2.40GHz / 2.10ghz / 2.45 GHz
    s = re.sub(r"\b\d+-core\b", " ", s)            # 64-Core
    s = re.sub(r"\bprocessor\b|\bcpu\b", " ", s)
    s = re.sub(r"[(),]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def lookup(name):
    """Average per-socket SPECrate2017_int_base for a CPU description, or None.
    Returns {'model', 'specrate_int', 'samples'}."""
    return _DATA.get(normalize_cpu(name))


def count():
    """Number of CPUs in the lookup (for diagnostics)."""
    return len(_DATA)
