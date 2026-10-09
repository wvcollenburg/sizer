"""Browser test: the one-time "DR now follows production" notice (plan A, G3).

Skipped when no headless Chrome can start (see browser_support).

Run: .venv/bin/python -m pytest tests/test_browser_dr_notice.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from browser_support import TADMIN, USER, Session, browser, server  # noqa: E402,F401

LEGACY = {"mode": "dr_target",
          "dr": {"vcpu_ratio": 3, "years": 4, "growth_pct": 12, "snapshot_pct": 25,
                 "sizing_mode": "certified"}}


def _visible(s):
    return s.page.evaluate(
        "getComputedStyle(document.getElementById('dr-growth-notice-modal')).display") != "none"


def _make_dr(s, payload=None):
    pid = s.api("POST", "/api/projects/", {"name": "Notice"})["data"]["id"]
    dr = s.api("POST", "/api/projects/%d/dr-target" % pid, {"name": "DR"})["data"]
    if payload is not None:
        assert s.api("PUT", "/api/configs/%d" % dr["id"], {"payload": payload})["ok"]
    return pid, dr["id"]


def _open(s, pid, sid):
    s.goto("/?project=%d&sizing=%d" % (pid, sid), settle=1500)


def test_legacy_dr_target_notice_flow(server, browser):
    owner = Session(browser, server, USER)
    pid, sid = _make_dr(owner, LEGACY)

    # A read-only colleague sees it, and dismissing stores nothing.
    viewer = Session(browser, server, TADMIN)
    _open(viewer, pid, sid)
    assert _visible(viewer)
    text = viewer.page.inner_text("#dr-growth-notice-old")
    assert "12" in text and "4" in text and "25" in text
    viewer.page.click("#dr-growth-notice-modal .btn-primary")
    viewer.page.wait_for_timeout(500)
    assert not _visible(viewer)
    still = owner.api("GET", "/api/configs/%d" % sid)["data"]["payload"]
    assert still == LEGACY
    _open(viewer, pid, sid)
    assert _visible(viewer), "a viewer sees it on every open until an editor dismisses"
    viewer.close()

    # The owner dismisses it: the flag is stored, the old fields are gone.
    _open(owner, pid, sid)
    assert _visible(owner)
    owner.page.click("#dr-growth-notice-modal .btn-primary")
    owner.page.wait_for_timeout(800)
    dr = owner.api("GET", "/api/configs/%d" % sid)["data"]["payload"]["dr"]
    assert dr["follows_sources"] is True
    assert not {"years", "growth_pct", "snapshot_pct"} & set(dr)
    assert dr["vcpu_ratio"] == 3
    _open(owner, pid, sid)
    assert not _visible(owner)
    # The DR form no longer has growth fields.
    assert owner.page.query_selector("#dr-growth-pct") is None
    assert not owner.errors, owner.errors
    owner.close()


def test_new_dr_target_never_shows_the_notice(server, browser):
    s = Session(browser, server, USER)
    pid, sid = _make_dr(s)
    _open(s, pid, sid)
    assert not _visible(s)
    assert not s.errors, s.errors
    s.close()
