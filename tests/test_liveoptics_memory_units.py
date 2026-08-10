"""Unit tests for liveoptics._vm_memory_gb — the VM memory columns arrive in
MiB, KiB or Bytes depending on which Live Optics collector produced the export
(a 2026 VMware export dropped the "(MiB)" columns for "(KiB)", which read as
0 GB provisioned RAM until the reader learned every unit).

Run from the repo root:
    .venv/bin/python -m pytest tests/test_liveoptics_memory_units.py -q
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from liveoptics import _vm_memory_gb  # noqa: E402


def test_mib_columns():
    assert _vm_memory_gb({"Provisioned Memory (MiB)": 2048}, "Provisioned Memory") == 2.0


def test_kib_columns():
    assert _vm_memory_gb({"Provisioned Memory (KiB)": 2097152}, "Provisioned Memory") == 2.0


def test_bytes_columns():
    assert _vm_memory_gb({"Provisioned Memory (Bytes)": 2147483648}, "Provisioned Memory") == 2.0


def test_bytes_wins_when_several_units_present():
    row = {"Provisioned Memory (MiB)": 2048,
           "Provisioned Memory (KiB)": 2097152,
           "Provisioned Memory (Bytes)": 2147483648}
    assert _vm_memory_gb(row, "Provisioned Memory") == 2.0


def test_zero_and_missing_columns():
    assert _vm_memory_gb({}, "Consumed Memory") == 0.0
    assert _vm_memory_gb({"Consumed Memory (MiB)": 0}, "Consumed Memory") == 0.0
    assert _vm_memory_gb({"Consumed Memory (MiB)": None}, "Consumed Memory") == 0.0


def test_used_active_label_with_parenthesised_metric():
    row = {"Used Memory (active) (KiB)": 1048576}
    assert _vm_memory_gb(row, "Used Memory (active)") == 1.0
