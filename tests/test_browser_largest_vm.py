"""Browser test: the manual form's largest-VM fields.

Left empty, the manual summary sends 0 (unknown) and the recommendation list
shows the server's "Largest VM ... unknown" warning; filled in, the values
reach /api/recommend as max_vm_cores / max_vm_ram_gb, the warning is gone, and
the fields survive save -> reopen. Skipped when no headless Chrome can start.

Run: .venv/bin/python -m pytest tests/test_browser_largest_vm.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from browser_support import USER, Session, browser, server  # noqa: E402,F401
from test_browser_storage_growth import MANUAL, _recommend_bodies, _sizing  # noqa: E402


def _warning_shown(s):
    return s.page.evaluate("""() => [...document.querySelectorAll('#rec-list .rec-warning')]
        .some(e => e.textContent.startsWith('Largest VM'))""")


def test_empty_fields_send_unknown_and_warn(server, browser):
    s = Session(browser, server, USER)
    pid, sid = _sizing(s, MANUAL)
    bodies = _recommend_bodies(s)
    s.goto("/?project=%d&sizing=%d" % (pid, sid), settle=2500)
    assert bodies
    summary = bodies[-1]["summary"]
    assert summary["max_vm_cores"] == 0 and summary["max_vm_ram_gb"] == 0
    assert _warning_shown(s)


def test_filled_fields_reach_engine_and_reopen(server, browser):
    s = Session(browser, server, USER)
    fields = {**MANUAL, "man-largest-vcpus": "16", "man-largest-ram": "128"}
    pid, sid = _sizing(s, fields)
    bodies = _recommend_bodies(s)
    s.goto("/?project=%d&sizing=%d" % (pid, sid), settle=2500)
    assert bodies
    summary = bodies[-1]["summary"]
    assert summary["max_vm_cores"] == 16 and summary["max_vm_ram_gb"] == 128
    assert not _warning_shown(s)
    assert s.page.input_value("#man-largest-vcpus") == "16"
    assert s.page.input_value("#man-largest-ram") == "128"
