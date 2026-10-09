"""Browser tests: several assessment files into one site (plan B, phase 5).

Drives the real page against a throwaway dev server: multi-file upload, the
overlap / cluster mapping dialog, save and reload (snapshot v4 with the
per-file sources and the import warnings), appending a file to a saved
sizing, the P4 message for an older sizing, and a refused (413) save.
Skipped when no headless Chrome can start (see browser_support).

Run: .venv/bin/python -m pytest tests/test_browser_multi_import.py -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402

import merge_fixtures as F  # noqa: E402
from browser_support import USER, Session, browser, server  # noqa: E402,F401


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("files")
    out = {}
    for i in (1, 2, 3, 4):
        out["esx%d" % i] = F.rvtools(
            d / ("esx%d.xlsx" % i), [F.host("esx%d.branch.local" % i)],
            [F.vm("b%d-vm%d" % (i, k), "esx%d.branch.local" % i, uuid="50-%d-%d" % (i, k))
             for k in range(3)],
            [("local-esx%d" % i, 2000, 300 + 10 * i)],
            date="2026/09/0%d 10:00:00" % i)
    out["lo"] = F.liveoptics(
        d / "lo.xlsx", [F.host("lo1")], [F.vm("lo-vm%d" % k, "lo1") for k in range(4)],
        [("lo-ds", 4000, 800, ["lo1"])])
    out["rv"] = F.rvtools(d / "rv.xlsx", [F.host("rv1")], [F.vm("rv-vm%d" % k, "rv1") for k in range(4)],
                          [("rv-ds", 4000, 800)])
    out["three"] = F.liveoptics(
        d / "three.xlsx",
        [F.host("a1", "BOCluster"), F.host("b1", "PROD"), F.host("c1", "DB")],
        [F.vm("a-vm", "a1", "BOCluster"), F.vm("b-vm", "b1", "PROD"), F.vm("c-vm", "c1", "DB")],
        [("san", 9000, 3000, ["a1", "b1", "c1"])])
    return out


def _project(s, name):
    return s.api("POST", "/api/projects/", {"name": name})["data"]["id"]


def _new_import(s, pid):
    s.goto("/?project=%d&new=1" % pid, settle=1000)
    s.page.evaluate("requestSwitchMode('import')")
    s.page.wait_for_timeout(800)


def _upload(s, paths, wait=2500):
    s.page.set_input_files("#file-input", paths)
    s.page.wait_for_timeout(wait)


def _modal_open(s):
    return s.page.evaluate(
        "document.getElementById('cluster-fanout-modal').style.display === 'flex'")


def _save_new(s, pid, name):
    """What Save does for a new sizing: the screen's state + provenance."""
    return s.api("POST", "/api/configs/", {
        "name": name, "project_id": pid,
        "payload": s.page.evaluate("captureSizingState()"),
        "source_meta": s.page.evaluate("currentSourceMeta()")})["data"]


def _caveats(s):
    return s.page.evaluate("(window._wizImportWarnings || []).map(w => importWarningText(w))")


def test_three_rvtools_hosts_become_one_sizing(server, browser, files):
    s = Session(browser, server, USER)
    pid = _project(s, "Branches")
    _new_import(s, pid)
    _upload(s, [files["esx1"], files["esx2"], files["esx3"]])
    assert _modal_open(s)
    groups = s.page.locator("#fanout-cluster-list .merge-group").count()
    assert groups == 1                                  # standalone hosts: one row
    assert "Continue" in s.page.inner_text("#fanout-create-btn")
    s.page.click("#fanout-create-btn")
    s.page.wait_for_timeout(3500)
    summary = s.page.evaluate("importSummary")
    assert summary["host_count"] == 3 and summary["total_vms"] == 9
    recs = s.page.evaluate("(lastRecommendations['import'] || []).length")
    assert recs > 0
    row = _save_new(s, pid, "Branches")
    meta = row["source_meta"]
    assert len(meta["files"]) == 3 and meta["file_name"] == "esx1.xlsx"
    snap = s.page.evaluate("captureSizingState()")
    assert snap["version"] == 4 and len(snap["import"]["sources"]) == 3
    assert all("datastore" not in v and v.get("src") for v in snap["import"]["importVms"])
    # The project table shows the provenance as "first +2".
    s.goto("/?project=%d" % pid, settle=1500)
    assert "esx1.xlsx +2" in s.page.inner_text("body")
    # Sized and exported: the result the browser stores goes through both
    # exporters.
    s.goto("/?project=%d&sizing=%d" % (pid, row["id"]), settle=3500)
    result = s.page.evaluate("buildResultSnapshot()")
    from export_docx import build_bundle_proposal_docx
    from export_pptx import generate_bundle_proposal
    assert build_bundle_proposal_docx(result["clusters"]).getvalue()[:2] == b"PK"
    assert generate_bundle_proposal(result["clusters"]).getvalue()[:2] == b"PK"
    assert not s.errors, s.errors
    s.close()


def test_mixed_sources_warn_and_survive_a_reload(server, browser, files):
    s = Session(browser, server, USER)
    pid = _project(s, "Mixed")
    _new_import(s, pid)
    _upload(s, [files["lo"], files["rv"]])
    assert "different tools" in s.page.inner_text("#merge-warnings")
    s.page.click("#fanout-create-btn")
    s.page.wait_for_timeout(3000)
    text = " ".join(_caveats(s))
    assert "different tools" in text and "IOPS was measured for only 50%" in text
    assert "different tools" in s.page.inner_text("#import-wizard")
    row = _save_new(s, pid, "Mixed")
    s.goto("/?project=%d&sizing=%d" % (pid, row["id"]), settle=3500)
    s.page.evaluate("WizardAPI.restoreToStep(2)")
    s.page.wait_for_timeout(500)
    assert "different tools" in s.page.inner_text("#import-wizard")
    assert not s.errors, s.errors
    s.close()


def test_a_and_b_plus_c_make_two_sizings(server, browser, files):
    s = Session(browser, server, USER)
    pid = _project(s, "Mapping")
    _new_import(s, pid)
    _upload(s, [files["three"]])
    assert _modal_open(s)
    assert "Create 3 sizings" in s.page.inner_text("#fanout-create-btn")
    # DB joins PROD.
    s.page.select_option("#fanout-cluster-list .merge-group:nth-child(3) select", "PROD")
    s.page.wait_for_timeout(1200)
    assert "Create 2 sizings" in s.page.inner_text("#fanout-create-btn")
    s.page.click("#fanout-create-btn")
    s.page.wait_for_timeout(3000)
    proj = s.api("GET", "/api/projects/%d" % pid)["data"]
    names = sorted(z["name"] for z in proj["sizings"])
    assert names == ["BOCluster", "PROD"]
    prod = next(z for z in proj["sizings"] if z["name"] == "PROD")
    payload = s.api("GET", "/api/configs/%d" % prod["id"])["data"]["payload"]
    assert sorted(v["source_cluster"] for v in payload["import"]["importVms"]) == ["DB", "PROD"]
    assert not s.errors, s.errors
    s.close()


def test_append_a_file_keeps_exclusions(server, browser, files):
    s = Session(browser, server, USER)
    pid = _project(s, "Append")
    _new_import(s, pid)
    _upload(s, [files["esx1"], files["esx2"]])
    s.page.click("#fanout-create-btn")
    s.page.wait_for_timeout(3000)
    # Exclude the second VM of esx2 from compute and storage.
    idx = s.page.evaluate("importVms.findIndex(v => v.name === 'b2-vm1')")
    s.page.evaluate("i => { vmExclusions.compute.add(i); vmExclusions.storage.add(i); }", idx)
    row = _save_new(s, pid, "Append")
    s.goto("/?project=%d&sizing=%d" % (pid, row["id"]), settle=3500)
    s.page.evaluate("document.getElementById('append-file-input')")
    s.page.set_input_files("#append-file-input", [files["esx4"]])
    s.page.wait_for_timeout(2500)
    assert _modal_open(s)
    assert "in this sizing" in s.page.inner_text("#merge-sources")
    s.page.click("#fanout-create-btn")
    s.page.wait_for_timeout(3000)
    vms = s.page.evaluate("importVms.map(v => v.name)")
    assert len(vms) == 9 and vms[idx] == "b2-vm1"
    assert vms[-3:] == ["b4-vm0", "b4-vm1", "b4-vm2"]
    excl = s.page.evaluate("[...vmExclusions.compute]")
    assert excl == [idx]
    assert s.page.evaluate("currentSourceMeta().files.length") == 3
    # The in-place save records the appended file in the sizing's provenance.
    files_now = s.page.evaluate("window.takePendingSourceFiles()")
    assert [f["file_name"] for f in files_now] == ["esx4.xlsx"]
    r = s.api("POST", "/api/sizings/%d/sources" % row["id"], {"files": files_now})
    assert [f["file_name"] for f in r["data"]["source_meta"]["files"]] == \
        ["esx1.xlsx", "esx2.xlsx", "esx4.xlsx"]
    assert not s.errors, s.errors
    s.close()


def test_v3_payload_add_files_shows_the_p4_message(server, browser, files):
    s = Session(browser, server, USER)
    pid = _project(s, "Old")
    from rvtools import parse_rvtools
    summary = parse_rvtools(files["esx1"])["summary"]
    row = s.api("POST", "/api/configs/", {"name": "Old", "project_id": pid, "payload": {
        "version": 3, "mode": "import", "fields": {},
        "import": {"originalImportSummary": summary, "importSummary": summary,
                   "importVms": [{"name": "v", "vcpus": 8, "provisioned_memory_gb": 32,
                                  "powered_on": True, "is_template": False,
                                  "consumed_memory_gb": 16, "vdisk_used_gb": 1024,
                                  "vdisk_size_gb": 1024}]}}})["data"]
    s.goto("/?project=%d&sizing=%d" % (pid, row["id"]), settle=3000)
    s.page.evaluate("startAppendFiles()")
    s.page.wait_for_timeout(500)
    assert "Re-import the original file together with the new one" in s.page.inner_text("body")
    assert not s.errors, s.errors
    s.close()


def test_a_too_large_save_names_the_vm_count(server, browser, files):
    s = Session(browser, server, USER)
    pid = _project(s, "Big")
    _new_import(s, pid)
    _upload(s, [files["esx1"], files["esx2"]])
    s.page.click("#fanout-create-btn")
    s.page.wait_for_timeout(3000)
    s.page.route("**/api/configs/", lambda route: route.fulfill(
        status=413, content_type="application/json",
        body='{"error": "Configuration is too large to save"}'))
    s.page.evaluate("""(async () => {
        window.promptSizingName = async () => 'Big';
        await saveCurrentSizing();
    })()""")
    s.page.wait_for_timeout(1500)
    assert "too large to save (6 VMs)" in s.page.inner_text("body")
    s.close()
