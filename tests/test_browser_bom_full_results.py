"""Browser test: "See full validation results" on a BOM check.

Opens a stored check from the project page, opens the full results from the
result step, and finds one row per BOM line with a result chip, quantity and
per-node count; the "only validated parts" filter hides the lines the checker
does not look at. Skipped when no headless Chrome can start.

Run: .venv/bin/python -m pytest tests/test_browser_bom_full_results.py -q
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from browser_support import USER, Session, browser, server  # noqa: E402,F401
from test_browser_multi_import import _project  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "bom", "synthetic_dell_quote.xlsx")


def _upload_check(s, pid):
    with open(FIX, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    return s.page.evaluate("""async ([pid, b64]) => {
        const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
        const fd = new FormData();
        fd.append('file', new Blob([bytes]), 'synthetic_dell_quote.xlsx');
        const r = await fetch(`/api/projects/${pid}/bom-checks`, {method: 'POST', body: fd});
        return {status: r.status, data: await r.json()};
    }""", [pid, b64])


def test_full_results_list_every_line(server, browser):
    s = Session(browser, server, USER)
    pid = _project(s, "BOM full")
    r = _upload_check(s, pid)
    assert r["status"] == 201, r
    check = r["data"]
    lines = sum(len(c["components"]) for c in check["component_results"])
    assert lines > 10

    s.goto("/?project=%d" % pid, settle=1500)
    s.page.evaluate("id => openBomCheckResult(id)", check["id"])
    s.page.wait_for_timeout(1500)
    s.page.click("#bom-full-btn")
    s.page.wait_for_timeout(300)
    assert s.page.is_visible("#bom-full-modal")
    rows = s.page.locator("#bom-full-body .bom-full-table tbody tr")
    assert rows.count() == lines
    first = s.page.locator("#bom-full-body .bom-full-table tbody tr").first
    assert first.locator(".state-badge").count() == 1
    assert "PowerEdge R760" in s.page.inner_text("#bom-full-body")

    not_checked = sum(1 for c in check["component_results"] for l in c["components"]
                      if l["status"] == "not_checked")
    s.page.check("#bom-full-checked-only")
    s.page.wait_for_timeout(200)
    assert rows.count() == lines - not_checked

    s.page.evaluate("closeBomFullResults()")
    assert not s.page.is_visible("#bom-full-modal") and s.page.is_visible("#bom-modal")
    assert not s.errors, s.errors
    s.close()
