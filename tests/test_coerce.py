"""Pins the edge cases the shared numeric coercion (app/coerce.py) absorbed
from the per-route helpers it replaced: an empty value and junk take the
default, ``0``/``False`` are values, clamping only touches a parsed number,
and the xlsx_utils wrappers keep their "blank -> 0" contract."""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402

from coerce import to_float, to_int  # noqa: E402
import xlsx_utils  # noqa: E402


@pytest.mark.parametrize("value", [None, ""])
def test_empty_takes_the_default_not_zero(value):
    assert to_int(value) is None
    assert to_float(value) is None
    assert to_int(value, 100) == 100
    assert to_float(value, 2.5) == 2.5


@pytest.mark.parametrize("value", [0, False, "0", 0.0])
def test_zero_and_false_are_values(value):
    assert to_int(value, default=7) == 0
    assert to_float(value, default=7.0) == 0.0


@pytest.mark.parametrize("value", ["abc", [], {}, "3.5", float("inf"), math.nan])
def test_junk_takes_the_default_for_int(value):
    assert to_int(value, default=-1) == -1


@pytest.mark.parametrize("value", ["abc", [], {}, "1,5"])
def test_junk_takes_the_default_for_float(value):
    assert to_float(value, default=-1.0) == -1.0


def test_float_truncates_for_int_and_strings_parse():
    assert to_int(3.9) == 3
    assert to_int("12") == 12
    assert to_float("12.5") == 12.5


def test_clamp_applies_to_parsed_numbers_only():
    assert to_int(150, lo=0, hi=100) == 100
    assert to_int(-5, lo=0, hi=100) == 0
    assert to_int(42, lo=0, hi=100) == 42
    assert to_float("1e9", hi=5.0) == 5.0
    # The default is returned as given, even outside [lo, hi].
    assert to_int("junk", default=999, lo=0, hi=100) == 999


def test_xlsx_wrappers_keep_blank_means_zero():
    assert xlsx_utils.to_float(None) == 0.0
    assert xlsx_utils.to_float("") == 0.0
    assert xlsx_utils.to_float("x") == 0.0
    assert xlsx_utils.to_float("2.5") == 2.5
    assert xlsx_utils.to_int(None) == 0
    assert xlsx_utils.to_int("3.5") == 0
    assert xlsx_utils.to_int(3.5) == 3
