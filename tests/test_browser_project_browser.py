"""Browser test: the project browser on the Projects home.

Folders (Active projects / Archived / My Company), sorting by column header,
search, closing and reopening a project, and the customer name that New project
now requires. Skipped when no headless Chrome can start (see browser_support).

Set SHOTS_DIR to also save screenshots of each state for a visual check.

Run: .venv/bin/python -m pytest tests/test_browser_project_browser.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from browser_support import TADMIN, USER, Session, browser, server  # noqa: E402,F401


def _shot(s, name):
    out = os.environ.get("SHOTS_DIR")
    if out:
        s.page.screenshot(path=os.path.join(out, name + ".png"), full_page=True)


def _names(s):
    return s.page.eval_on_selector_all(
        "#project-list tbody .sizing-open", "els => els.map(e => e.textContent.trim())")


def _folder(s, folder, settle=600):
    s.page.click(".project-folder[data-folder='%s']" % folder)
    s.page.wait_for_timeout(settle)


def test_project_browser_folders_sort_and_close(server, browser):
    s = Session(browser, server, USER)
    s.page.evaluate("localStorage.clear()")
    made = {}
    for name, customer in (("Zeta rollout", "Initech"), ("Alpha refresh", "Umbrella"),
                           ("Mid campus", "Globex")):
        r = s.api("POST", "/api/projects/", {"name": name, "customer_name": customer})
        assert r["ok"], r
        made[name] = r["data"]["id"]
    assert s.api("POST", "/api/projects/%d/archive" % made["Mid campus"])["ok"]

    s.goto("/", settle=1500)
    # Active: the closed project is not here.
    active = _names(s)
    assert "Zeta rollout" in active and "Alpha refresh" in active
    assert "Mid campus" not in active
    _shot(s, "browser-active")

    # Sort by project name (A→Z), then reverse on a second click.
    s.page.click("#project-list th button[data-click*='\"name\"']")
    s.page.wait_for_timeout(200)
    names = [n for n in _names(s) if n in made]
    assert names == sorted(names)
    s.page.click("#project-list th button[data-click*='\"name\"']")
    s.page.wait_for_timeout(200)
    assert [n for n in _names(s) if n in made] == sorted(names, reverse=True)

    # Sort by customer: Initech before Umbrella.
    s.page.click("#project-list th button[data-click*='\"customer\"']")
    s.page.wait_for_timeout(200)
    order = [n for n in _names(s) if n in made]
    assert order.index("Zeta rollout") < order.index("Alpha refresh")

    # Search narrows the table.
    s.page.fill("#project-search", "umbrel")
    s.page.wait_for_timeout(200)
    assert _names(s) == ["Alpha refresh"]
    s.page.fill("#project-search", "")
    s.page.wait_for_timeout(200)

    # Archived holds the closed project, with its date filled in.
    _folder(s, "archived")
    assert _names(s) == ["Mid campus"]
    assert s.page.inner_text("#project-folder-count-archived").strip() == "1"
    _shot(s, "browser-archived")

    # Reopen from the row: it goes back to Active.
    s.page.click("#project-list tbody .col-actions .icon-btn")
    s.page.wait_for_timeout(900)
    assert _names(s) == []
    _folder(s, "active")
    assert "Mid campus" in _names(s)

    # Close from inside the project: Details → Close project, banner on reopen.
    s.goto("/?project=%d" % made["Zeta rollout"], settle=1200)
    assert s.page.is_hidden("#project-archived-bar")
    s.page.click("button[data-click='[\"openProjectSettings\"]']")
    s.page.wait_for_timeout(300)
    s.page.click("#ps-archive-btn")
    s.page.wait_for_timeout(1200)
    assert s.page.is_visible("#project-home")
    _folder(s, "archived")
    assert "Zeta rollout" in _names(s)
    s.goto("/?project=%d" % made["Zeta rollout"], settle=1200)
    assert s.page.is_visible("#project-archived-bar")
    _shot(s, "project-closed-banner")

    # New project: the customer is required.
    s.goto("/", settle=1200)
    s.page.click("button[data-click='[\"openNewProject\"]']")
    s.page.wait_for_timeout(300)
    s.page.fill("#new-project-name", "No customer yet")
    s.page.click("button[data-click='[\"submitNewProject\"]']")
    s.page.wait_for_timeout(400)
    assert s.page.is_visible("#new-project-error")
    _shot(s, "new-project-required")
    s.page.fill("#new-project-customer", "Hooli")
    s.page.click("button[data-click='[\"submitNewProject\"]']")
    s.page.wait_for_timeout(1200)
    assert s.page.inner_text("#project-view-meta").startswith("Hooli")

    assert s.errors == []
    s.close()


def test_my_company_folder_shows_colleagues_projects(server, browser):
    owner = Session(browser, server, USER)
    r = owner.api("POST", "/api/projects/", {"name": "Owner's engagement",
                                             "customer_name": "Contoso"})
    assert r["ok"], r
    owner.close()

    s = Session(browser, server, TADMIN)
    s.page.evaluate("localStorage.clear()")
    s.goto("/", settle=1200)
    assert "Owner's engagement" not in _names(s)        # not their own work
    _folder(s, "company", settle=1200)
    assert "Owner's engagement" in _names(s)
    headers = s.page.eval_on_selector_all("#project-list th", "els => els.length")
    assert headers == 7                                  # + Owner column
    # Read-only for them: no close/reopen control on that row.
    row = s.page.locator("#project-list tbody tr", has_text="Owner's engagement")
    assert row.locator(".col-actions .icon-btn").count() == 0
    _shot(s, "browser-company")

    # The folder is remembered across visits.
    s.goto("/", settle=1500)
    assert "is-active" in s.page.get_attribute(".project-folder[data-folder='company']", "class")
    assert s.errors == []
    s.close()
