"""Browser test: what mcp_payload writes opens and re-sizes in the real UI
(docs/mcp/01-tool-design.md §3.6; the test that holds the writer to app.js).

A manual payload and an import payload (with VM exclusions) are built the
way save_sizing builds them, stored through the API as the browser would
store its own, then opened in headless Chrome: the controls come back with
the saved values, the UI's recalculation sends the same engine summary the
canonical block holds, and the exclusions survive. Skipped when no headless
Chrome can start.

Run: .venv/bin/python -m pytest tests/test_browser_mcp_payload.py -q
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from browser_support import USER, Session, browser, server  # noqa: E402,F401
from test_browser_storage_growth import _recommend_bodies  # noqa: E402
from merge_fixtures import host, rvtools, vm  # noqa: E402

SUMMARY = {"total_vcpus": 148, "total_vm_provisioned_memory_gb": 423,
           "datastore_used_tb": 8.2, "nic_speed_mbps": 25000, "active_vms": 59,
           "p95_iops": 16000, "max_vm_cores": 16, "max_vm_ram_gb": 64,
           "total_host_cores": 72, "total_host_ghz": 151.2, "host_count": 3}
PARAMS = {"vcpu_ratio": 3.5, "growth_pct": 12, "storage_growth_pct": 25, "snapshot_pct": 15,
          "years": 3, "max_day_one_storage_pct": 60.0, "max_day_one_ram_pct": 55.0,
          "target_nodes": None, "storage_preference": "auto", "sizing_mode": "certified",
          "vendor": None, "size_cpu_for_full_cluster": False, "allow_storage_only_nodes": False,
          "target_model": None, "include_eol_eos": False, "license_term_years": 3}


def _store(s, name, payload, source_meta=None):
    pid = s.api("POST", "/api/projects/", {"name": "MCP payloads"})["data"]["id"]
    body = {"name": name, "project_id": pid, "payload": payload}
    if source_meta:
        body["source_meta"] = source_meta
    row = s.api("POST", "/api/configs/", body)["data"]
    return pid, row["id"]


def test_manual_payload_round_trips(server, browser):
    import mcp_payload
    s = Session(browser, server, USER)
    payload = mcp_payload.manual_payload(dict(SUMMARY, guest_licensing="windows_db"), PARAMS, 1)
    pid, sid = _store(s, "MCP manual", payload)
    bodies = _recommend_bodies(s)
    s.goto("/?project=%d&sizing=%d" % (pid, sid), settle=3000)
    assert bodies, "the UI must re-size on open"
    sent = bodies[-1]
    for key in ("total_vcpus", "total_vm_provisioned_memory_gb", "datastore_used_tb",
                "nic_speed_mbps", "max_vm_cores", "max_vm_ram_gb", "p95_iops", "active_vms"):
        assert sent["summary"][key] == SUMMARY[key], key
    assert sent["vcpu_ratio"] == 3.5 and sent["years"] == 3 and sent["growth_pct"] == 12
    assert sent["storage_growth_pct"] == 25 and sent["snapshot_pct"] == 15
    assert sent["max_day_one_storage_pct"] == 60 and sent["max_day_one_ram_pct"] == 55
    assert sent["license_term_years"] == 3 and sent["guest_licensing"] == "windows_db"
    assert s.page.input_value("#man-largest-vcpus") == "16"
    assert s.page.input_value("#ratio-slider") == "3.5"
    # The UI's own save keeps the canonical block alongside its fields.
    snap = s.page.evaluate("captureSizingState()")
    assert snap["mode"] == "manual" and snap["fields"]["man-vcpus"] == "148"
    assert not s.errors, s.errors
    s.close()


def test_import_payload_round_trips_with_exclusions(server, browser, tmp_path):
    import import_service
    import mcp_payload
    s = Session(browser, server, USER)
    path = rvtools(str(tmp_path / "est.xlsx"),
                   [host("esx1", "Prod"), host("esx2", "Prod")],
                   [vm("web01", "esx1", "Prod", vcpus=4, ram_gb=16, used_gb=100),
                    vm("db01", "esx2", "Prod", vcpus=8, ram_gb=32, used_gb=200),
                    vm("vCLS-1", "esx2", "Prod", vcpus=1, ram_gb=0.25, used_gb=2)],
                   [("ds1", 2048, 1024)])
    data = import_service.parse_assessment(path, "est.xlsx")
    spec = {"upload_id": "u_test", "exclude_compute": [2], "exclude_storage": [2]}
    adjusted = import_service.adjust_summary(data["summary"], data["vms"], [2], [2])
    row = SimpleNamespace(kind="assessment", token="testtoken00", public_id="u_test",
                          data=dict(data, id="ftest"))
    ctx = {"row": row, "dataset": data, "spec": spec}
    params = dict(PARAMS, vcpu_ratio=3.0, storage_growth_pct=None)
    payload, meta = mcp_payload.import_payload(ctx, adjusted, params, 1, None)
    pid, sid = _store(s, "MCP import", payload, meta)
    bodies = _recommend_bodies(s)
    s.goto("/?project=%d&sizing=%d" % (pid, sid), settle=3000)
    assert bodies
    sent = bodies[-1]["summary"]
    assert sent["total_vcpus"] == adjusted["total_vcpus"] == 12
    assert sent["max_vm_cores"] == 8 and sent["active_vms"] == 2
    assert sent["datastore_used_tb"] == adjusted["datastore_used_tb"]
    state = s.page.evaluate("""() => ({
        vms: importVms.length, excl: [...vmExclusions.compute], mode: currentMode,
        adjusted: computeAdjustedImportSummary().total_vcpus })""")
    assert state == {"vms": 3, "excl": [2], "mode": "import", "adjusted": 12}
    assert not s.errors, s.errors
    s.close()
