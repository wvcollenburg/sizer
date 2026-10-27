"""Browser test: licence term and guest licensing survive save -> reopen.

Neither control was in the saved fields, so a reopened sizing fell back to
5 years and the detected/default guest licensing, and could recalculate
differently from what was saved (docs/mcp/01-tool-design.md §8, point 11).
Locks in: both are captured, both reach /api/recommend after a reopen, a
saved guest-licensing choice is not overridden by detection, and a payload
saved before the fix opens as before. Skipped when no headless Chrome can
start.

Run: .venv/bin/python -m pytest tests/test_browser_licensing_fields.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from browser_support import USER, Session, browser, server  # noqa: E402,F401
from test_browser_storage_growth import MANUAL, _recommend_bodies, _sizing  # noqa: E402


def _controls(s):
    return s.page.evaluate("""() => ({
        term: document.getElementById('license-term-years').value,
        guest: document.getElementById('guest-licensing').value,
        userSet: document.getElementById('guest-licensing').dataset.userSet || '',
    })""")


def test_saved_choices_reach_the_engine_after_reopen(server, browser):
    s = Session(browser, server, USER)
    fields = {**MANUAL, "license-term-years": "3", "guest-licensing": "windows_db"}
    pid, sid = _sizing(s, fields)
    bodies = _recommend_bodies(s)
    s.goto("/?project=%d&sizing=%d" % (pid, sid), settle=2500)
    assert bodies
    assert bodies[-1]["license_term_years"] == 3
    assert bodies[-1]["guest_licensing"] == "windows_db"
    st = _controls(s)
    assert st == {"term": "3", "guest": "windows_db", "userSet": "1"}
    snap = s.page.evaluate("captureSizingState()")
    assert snap["fields"]["license-term-years"] == "3"
    assert snap["fields"]["guest-licensing"] == "windows_db"
    assert not s.errors, s.errors
    s.close()


def test_old_payload_opens_with_the_defaults(server, browser):
    s = Session(browser, server, USER)
    _, new_id = _sizing(s, {**MANUAL, "license-term-years": "2",
                            "guest-licensing": "none"})
    pid, old_id = _sizing(s, MANUAL)
    bodies = _recommend_bodies(s)
    s.goto("/?project=%d&sizing=%d" % (pid, new_id), settle=2500)
    assert _controls(s)["guest"] == "none"
    # Open the pre-fix payload in the same page: defaults, not the previous
    # sizing's values.
    s.page.evaluate("(async () => { const r = await fetch('/api/configs/%d'); "
                    "const d = await r.json(); await restoreSizingState(d.payload); })()"
                    % old_id)
    s.page.wait_for_timeout(2500)
    st = _controls(s)
    assert st == {"term": "5", "guest": "windows", "userSet": ""}
    assert bodies[-1]["license_term_years"] == 5
    assert bodies[-1]["guest_licensing"] == "windows"
    assert not s.errors, s.errors
    s.close()
