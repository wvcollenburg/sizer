"""Browser tests: switching the sizing option (mode buttons) away from unsaved
work.

A confirmed discard reloads to a clean page and resumes on the target mode.
The reload boots from the URL after sign-in, and with a saved sizing open that
URL is ?project=<id>&sizing=<id>: it reopened the sizing just left, in its own
mode, so the click seemed to do nothing and had to be repeated. Skipped when
no headless Chrome can start (see browser_support).

Run: .venv/bin/python -m pytest tests/test_browser_mode_switch.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from browser_support import USER, Session, browser, server  # noqa: E402,F401


def _manual_sizing(s, pid):
    s.goto("/?project=%d&new=1" % pid, settle=1000)
    s.page.evaluate("void requestSwitchMode('manual')")
    s.page.wait_for_timeout(500)
    for field, value in (("man-vcpus", "64"), ("man-prov-ram", "256"), ("man-ds-used", "5")):
        s.page.fill("#" + field, value)
    s.page.evaluate("calculateManual()")
    s.page.wait_for_timeout(1500)
    return s.api("POST", "/api/configs/", {
        "name": "Manual one", "project_id": pid,
        "payload": s.page.evaluate("captureSizingState()")})["data"]["id"]


def _discard_switch(s, mode):
    s.page.evaluate("void requestSwitchMode('%s')" % mode)
    s.page.wait_for_timeout(300)
    assert s.page.evaluate(
        "document.getElementById('leave-page-modal').style.display") == "flex"
    with s.page.expect_navigation():
        s.page.click("#leave-page-modal [data-click='[\"chooseLeave\",\"discard\"]']")
    s.page.wait_for_load_state("networkidle")
    s.page.wait_for_timeout(1500)


def test_discard_from_a_saved_sizing_lands_on_the_target_mode(server, browser):
    s = Session(browser, server, USER)
    try:
        pid = s.api("POST", "/api/projects/", {"name": "Mode switch"})["data"]["id"]
        sid = _manual_sizing(s, pid)
        s.goto("/?project=%d&sizing=%d" % (pid, sid), settle=1500)
        assert s.page.evaluate("currentMode") == "manual"
        s.page.fill("#man-vcpus", "96")           # unsaved edit
        s.page.dispatch_event("#man-vcpus", "change")
        assert s.page.evaluate("hasUnsavedWork()")

        _discard_switch(s, "appliance")

        # One click: the target mode, in a blank sizer of the same project.
        assert s.page.evaluate("currentMode") == "appliance"
        assert s.page.evaluate("document.body.classList.contains('view-sizer')")
        assert "sizing=" not in s.page.url
        assert "project=%d" % pid in s.page.url
        assert not s.errors, s.errors
    finally:
        s.close()


def test_discard_from_an_unsaved_sizer_stays_in_the_sizer(server, browser):
    s = Session(browser, server, USER)
    try:
        pid = s.api("POST", "/api/projects/", {"name": "Mode switch 2"})["data"]["id"]
        s.goto("/?project=%d&new=1" % pid, settle=1000)
        s.page.evaluate("void requestSwitchMode('manual')")
        s.page.wait_for_timeout(500)
        s.page.fill("#man-vcpus", "64")

        _discard_switch(s, "validated")

        assert s.page.evaluate("currentMode") == "validated"
        assert s.page.evaluate("document.body.classList.contains('view-sizer')")
        assert not s.errors, s.errors
    finally:
        s.close()
