"""Browser test: choosing the datastores a sizing counts.

Drives the real page: a Live Optics import with two shared datastores shows
the Datastores button; unticking one in the modal and applying lowers the
storage basis, lists it in the summary, shows the badge and the sizing-options
line, and survives save -> reopen. Cancel discards staged changes. A sizing
saved before the list, with includeLocalStorage on, opens with every local
datastore ticked. Skipped when no headless Chrome can start.

Run: .venv/bin/python -m pytest tests/test_browser_datastores.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402

import merge_fixtures as F  # noqa: E402
from browser_support import USER, Session, browser, server  # noqa: E402,F401
from parser_common import GIB_PER_TB  # noqa: E402
from test_browser_multi_import import _new_import, _project, _save_new, _upload  # noqa: E402


@pytest.fixture(scope="module")
def lo_file(tmp_path_factory):
    d = tmp_path_factory.mktemp("ds")
    return F.liveoptics(
        d / "lo.xlsx", [F.host("esx1"), F.host("esx2")],
        [F.vm("vm%d" % k, "esx%d" % (1 + k % 2), used_gb=200) for k in range(6)],
        [("san", 8000, 3000, ["esx1", "esx2"]), ("archive", 40000, 30000, ["esx1", "esx2"])])


def _rows(s):
    return s.page.evaluate("""() => [...document.querySelectorAll('#ds-table-body tr')].map(r => ({
        name: r.children[1].textContent.trim(), checked: r.querySelector('input').checked}))""")


def _uncheck(s, name):
    s.page.locator("#ds-table-body tr", has_text=name).locator("input[type=checkbox]").uncheck()


def test_exclude_a_datastore_and_reopen(server, browser, lo_file):
    s = Session(browser, server, USER)
    pid = _project(s, "Datastores")
    _new_import(s, pid)
    _upload(s, [lo_file])
    base = s.page.evaluate("originalImportSummary.datastore_used_tb")
    # Guided view (the default): the environment step carries the button.
    wiz_btn = s.page.locator("#wiz-after-2 button", has_text="Datastores")
    assert wiz_btn.is_visible()
    assert "storage_gap" in s.page.evaluate("lastImportWarnings.map(w => w.code).join()")

    wiz_btn.click()
    s.page.wait_for_timeout(300)
    assert _rows(s) == [{"name": "archive", "checked": True}, {"name": "san", "checked": True}]
    # The fixture's VMs name a datastore ("ds") the list does not have: no
    # placement to count, so the modal says the export lacks it.
    assert s.page.is_visible("#ds-no-placement")
    _uncheck(s, "archive")
    assert "1 shared left out" in s.page.inner_text("#ds-summary")
    # Cancel throws the staged change away.
    s.page.evaluate("closeDatastoreModal()")
    assert s.page.evaluate("dsExcluded.size") == 0

    wiz_btn.click()
    s.page.wait_for_timeout(300)
    _uncheck(s, "archive")
    s.page.evaluate("applyDatastoreSelection()")
    s.page.wait_for_timeout(1500)
    summary = s.page.evaluate("importSummary")
    assert summary["datastore_used_tb"] == pytest.approx(base - 30000 / GIB_PER_TB, abs=0.011)
    assert [d["name"] for d in summary["excluded_datastores"]] == ["archive"]
    assert s.page.inner_text("#datastore-count").strip() == "1"
    assert "1 of 2 datastores" in s.page.inner_text("#local-storage-option")

    snap = s.page.evaluate("captureSizingState()")
    assert len(snap["import"]["exclDatastores"]) == 1
    assert snap["import"]["exclDatastores"][0].endswith("|archive")
    row = _save_new(s, pid, "No archive")
    s.goto("/?project=%d&sizing=%d" % (pid, row["id"]), settle=3000)
    again = s.page.evaluate("importSummary")
    assert again["datastore_used_tb"] == summary["datastore_used_tb"]
    assert s.page.evaluate("[...dsExcluded]") == snap["import"]["exclDatastores"]
    assert not s.errors, s.errors
    s.close()


def test_old_include_local_storage_opens_with_local_datastores_in(server, browser, lo_file):
    s = Session(browser, server, USER)
    pid = _project(s, "Legacy local")
    _new_import(s, pid)
    _upload(s, [lo_file])
    snap = s.page.evaluate("captureSizingState()")
    base = snap["import"]["originalImportSummary"]["datastore_used_tb"]
    im = snap["import"]
    im["sources"][0]["datastores"].append(
        {"name": "local-esx1", "type": "local", "capacity_gib": 900, "used_gib": 450,
         "mounts": ["esx1"]})
    im["includeLocalStorage"] = True
    for k in ("exclDatastores", "inclDatastores"):
        im.pop(k)
    row = s.api("POST", "/api/configs/", {"name": "Old", "project_id": pid, "payload": snap,
                                          "source_meta": None})["data"]
    s.goto("/?project=%d&sizing=%d" % (pid, row["id"]), settle=3000)
    keys = s.page.evaluate("[...dsIncluded]")
    assert len(keys) == 1 and keys[0].endswith("|local-esx1")
    assert s.page.evaluate("includeLocalStorage") is False
    assert s.page.evaluate("importSummary.datastore_used_tb") == pytest.approx(
        base + 450 / GIB_PER_TB, abs=0.011)
    assert not s.errors, s.errors
    s.close()


def test_vm_counts_shown_when_the_export_places_vms(server, browser, tmp_path):
    # The fixture's VMs all sit on a datastore named "ds".
    path = F.liveoptics(tmp_path / "placed.xlsx", [F.host("esx1")],
                        [F.vm("vm%d" % k, "esx1") for k in range(3)],
                        [("ds", 4000, 900, ["esx1"])])
    s = Session(browser, server, USER)
    pid = _project(s, "Placed")
    _new_import(s, pid)
    _upload(s, [path])
    s.page.evaluate("openDatastoreModal()")
    s.page.wait_for_timeout(300)
    assert not s.page.is_visible("#ds-no-placement")
    assert s.page.inner_text("#ds-table-body tr td:last-child").strip() == "3"
    assert not s.errors, s.errors
    s.close()
