"""Numeric coercion of untrusted scalars: JSON payload fields, form values,
spreadsheet cells.

Every route and importer had its own three-line ``try: int(v) except: ...``
variant; they differed only in what an empty value and junk turn into and in
whether the result is clamped, which is what the parameters express:

* ``None`` and ``""`` are *empty* and yield ``default`` (so a blank form field
  never raises) — ``0``/``False`` are values, not empties.
* junk (``"abc"``, ``[]``, ``inf`` for int) yields ``default``.
* ``lo``/``hi`` clamp a *parsed* number; the default is returned as given.
"""


def _clamp(n, lo, hi):
    if lo is not None and n < lo:
        return lo
    if hi is not None and n > hi:
        return hi
    return n


def to_float(v, default=None, lo=None, hi=None):
    """``float(v)``, or ``default`` for an empty or non-numeric value,
    clamped into [lo, hi] when given."""
    if v is None or v == "":
        return default
    try:
        n = float(v)
    except (TypeError, ValueError, OverflowError):
        return default
    return _clamp(n, lo, hi)


def to_int(v, default=None, lo=None, hi=None):
    """``int(v)`` (truncating a float, refusing ``"3.5"``), or ``default``
    for an empty or non-numeric value, clamped into [lo, hi] when given."""
    if v is None or v == "":
        return default
    try:
        n = int(v)
    except (TypeError, ValueError, OverflowError):
        return default
    return _clamp(n, lo, hi)
