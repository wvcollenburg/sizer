"""Browser test: the "Separate storage growth" toggle (plan A, phase 3).

Toggle off sends no storage rate; on reveals the field (seeded with the
current growth rate), relabels the main rate as compute and sends the storage
rate; the choice survives save -> reopen; a payload saved before the toggle
existed opens with it off. Skipped when no headless Chrome can start.

Run: .venv/bin/python -m pytest tests/test_browser_storage_growth.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from browser_support import USER, Session, browser, server  # noqa: E402,F401

MANUAL = {"man-platform": "VMware", "man-cluster": "Prod", "man-hosts": "3",
          "man-cores": "96", "man-threads": "192", "man-ghz": "240",
          "man-host-ram": "1536", "man-total-vms": "60", "man-active-vms": "60",
          "man-vcpus": "240", "man-prov-ram": "960", "man-used-ram": "700",
          "man-prov-storage": "40", "man-ds-used": "30", "man-ds-total": "60",
          "man-nic-speed": "10000", "growth-years": "5", "growth-pct": "10",
          "snapshot-pct": "20"}


def _sizing(s, fields):
    pid = s.api("POST", "/api/projects/", {"name": "Growth"})["data"]["id"]
    row = s.api("POST", "/api/configs/", {
        "name": "Manual", "project_id": pid,
        "payload": {"version": 3, "mode": "manual", "fields": fields,
                    "manual": {"selectedRec": {}}}})["data"]
    return pid, row["id"]


def _recommend_bodies(s):
    bodies = []

    def on_request(req):
        if req.url.endswith("/api/recommend") and req.method == "POST":
            bodies.append(json.loads(req.post_data))
    s.page.on("request", on_request)
    return bodies


def _state(s):
    return s.page.evaluate("""() => ({
        on: document.getElementById('separate-storage-growth').checked,
        hidden: document.getElementById('storage-growth-group').hidden,
        value: document.getElementById('storage-growth-pct').value,
        label: document.getElementById('growth-pct-label').textContent.trim(),
        note: document.querySelector('#projection-summary .proj-note')?.textContent || '',
    })""")


def test_toggle_flow(server, browser):
    s = Session(browser, server, USER)
    pid, sid = _sizing(s, MANUAL)
    bodies = _recommend_bodies(s)
    s.goto("/?project=%d&sizing=%d" % (pid, sid), settle=2500)
    st = _state(s)
    assert st["on"] is False and st["hidden"] is True
    assert st["label"] == "YoY Growth %"
    assert bodies and "storage_growth_pct" not in bodies[-1]

    # Switch on: seeded from the growth rate, relabelled, sent.
    s.page.evaluate("document.querySelectorAll('.growth-control').forEach(e => e.classList.remove('is-collapsed'))")
    s.page.check("#separate-storage-growth")
    s.page.wait_for_timeout(1500)
    st = _state(s)
    assert st["hidden"] is False and st["value"] == "10"
    assert st["label"] == "Compute growth %"
    assert bodies[-1]["storage_growth_pct"] == 10

    s.page.fill("#storage-growth-pct", "30")
    s.page.dispatch_event("#storage-growth-pct", "change")
    s.page.wait_for_timeout(1500)
    assert bodies[-1]["storage_growth_pct"] == 30
    assert "storage" in _state(s)["note"].lower()

    # Save the screen's state, reopen: the toggle and rate come back.
    snap = s.page.evaluate("captureSizingState()")
    assert snap["fields"]["separate-storage-growth"] is True
    assert snap["fields"]["storage-growth-pct"] == "30"
    assert s.api("PUT", "/api/configs/%d" % sid, {"payload": snap})["ok"]
    s.goto("/?project=%d&sizing=%d" % (pid, sid), settle=2500)
    st = _state(s)
    assert st["on"] is True and st["value"] == "30" and st["hidden"] is False
    assert bodies[-1]["storage_growth_pct"] == 30

    # Switch off: hidden again, no storage rate sent.
    s.page.evaluate("document.querySelectorAll('.growth-control').forEach(e => e.classList.remove('is-collapsed'))")
    s.page.uncheck("#separate-storage-growth")
    s.page.wait_for_timeout(1500)
    st = _state(s)
    assert st["hidden"] is True and st["label"] == "YoY Growth %"
    assert "storage_growth_pct" not in bodies[-1]
    assert not s.errors, s.errors
    s.close()


def test_old_payload_opens_with_the_toggle_off(server, browser):
    s = Session(browser, server, USER)
    _, on_id = _sizing(s, dict(MANUAL, **{"separate-storage-growth": True,
                                          "storage-growth-pct": "40"}))
    pid, old_id = _sizing(s, MANUAL)
    # Open the toggled one first, then the old one in the same page.
    s.goto("/?project=%d&sizing=%d" % (pid, on_id), settle=2500)
    assert _state(s)["on"] is True
    s.page.evaluate("(async () => { const r = await fetch('/api/configs/%d'); "
                    "const d = await r.json(); await restoreSizingState(d.payload); })()" % old_id)
    s.page.wait_for_timeout(2000)
    st = _state(s)
    assert st["on"] is False and st["hidden"] is True
    assert not s.errors, s.errors
    s.close()
