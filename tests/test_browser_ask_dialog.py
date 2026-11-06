"""Browser test: the in-page confirm / text dialogs (askConfirm / askText in
common.js) that replaced window.confirm and window.prompt.

A native dialog showing up anywhere here fails the test: the page would block
on it in real use. Skipped when no headless Chrome can start (see
browser_support).

Run: .venv/bin/python -m pytest tests/test_browser_ask_dialog.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from browser_support import USER, Session, browser, server  # noqa: E402,F401
from manual_shots import SUPER  # noqa: E402

ASK = "#ask-dialog"


def _session(browser, server, email):
    s = Session(browser, server, email)
    s.native = []
    s.page.on("dialog", lambda d: (s.native.append(d.message), d.dismiss()))
    return s


def _asking(s):
    return s.page.is_visible(ASK)


def test_delete_sizing_and_dr_target_use_the_in_page_dialog(server, browser):
    s = _session(browser, server, USER)
    pid = s.api("POST", "/api/projects/", {"name": "Ask project",
                                           "customer_name": "Acme Corp"})["data"]["id"]
    sid = s.api("POST", "/api/configs/", {"name": "Doomed", "project_id": pid,
                                          "payload": {"mode": "manual", "fields": {}}})["data"]["id"]
    s.goto("/?project=%d" % pid, settle=1500)
    delete_btn = "button[data-click='[\"deleteProjectSizing\",%d]']" % sid

    # Cancel keeps the sizing.
    s.page.click(delete_btn)
    s.page.wait_for_timeout(300)
    assert _asking(s)
    assert "btn-danger" in s.page.get_attribute("#ask-dialog-ok", "class")
    assert s.page.inner_text("#ask-dialog-ok") == "Delete"
    s.page.click("#ask-dialog-cancel")
    s.page.wait_for_timeout(500)
    assert not _asking(s)
    assert s.api("GET", "/api/configs/%d" % sid)["ok"]

    # Escape is a cancel too.
    s.page.click(delete_btn)
    s.page.wait_for_timeout(300)
    s.page.keyboard.press("Escape")
    s.page.wait_for_timeout(300)
    assert not _asking(s)
    assert s.api("GET", "/api/configs/%d" % sid)["ok"]

    # Confirm deletes it.
    s.page.click(delete_btn)
    s.page.wait_for_timeout(300)
    s.page.click("#ask-dialog-ok")
    s.page.wait_for_timeout(1200)
    assert not s.api("GET", "/api/configs/%d" % sid)["ok"]

    # Add DR target asks for a name, prefilled; Enter submits.
    s.page.click("button[data-click='[\"addDrTarget\"]']")
    s.page.wait_for_timeout(300)
    assert _asking(s)
    assert s.page.input_value("#ask-dialog-input") == "DR site"
    s.page.fill("#ask-dialog-input", "Far site")
    s.page.press("#ask-dialog-input", "Enter")
    s.page.wait_for_timeout(1500)
    assert not _asking(s)
    sizings = s.api("GET", "/api/projects/%d" % pid)["data"]["sizings"]
    assert any(z["name"] == "Far site" for z in sizings), sizings

    assert s.native == []
    assert s.errors == []
    s.close()


def test_admin_page_confirm_is_in_page(server, browser):
    s = _session(browser, server, SUPER)
    s.goto("/admin", settle=1500)
    # Any admin confirm will do; resetting tunables is harmless when cancelled.
    s.page.evaluate("() => { resetTunables(); }")   # do not await the answer
    s.page.wait_for_timeout(300)
    assert _asking(s)
    assert "admin.tunable" not in s.page.inner_text("#ask-dialog-msg")   # translated
    s.page.click("#ask-dialog-cancel")
    s.page.wait_for_timeout(300)
    assert not _asking(s)
    assert s.native == []
    assert s.errors == []
    s.close()
