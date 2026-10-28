"""Browser test: the exclusion maths has one answer (docs/plan_enableMCP_sizingtool.md
Phase 5.7). The browser's computeAdjustedImportSummary and its Python port,
import_service.adjust_summary (which MCP's from_upload uses), give identical
summaries for every assessment format, under seeded random exclusion sets,
per-VM edits, hand-added and removed VMs, and the local-storage toggle.

The page's own function runs on the page's own globals: each case loads the
parsed dataset into importVms / originalImportSummary and the exclusion
state, then calls computeAdjustedImportSummary(). Skipped when no headless
Chrome can start.

Run: .venv/bin/python -m pytest tests/test_browser_exclusion_parity.py -q
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402

from browser_support import USER, Session, browser, server  # noqa: E402,F401
from merge_fixtures import host, liveoptics, rvtools, vm  # noqa: E402

CASES_PER_FILE = 25

HOSTS = [host("esx1", "Prod", cores=24), host("esx2", "Prod", cores=24),
         host("esx3", "Dev", cores=16)]
VMS = [vm("web%02d" % i, "esx%d" % (1 + i % 3), "Prod" if i % 3 < 2 else "Dev",
          vcpus=1 + i % 8, ram_gb=[2, 4, 8, 12, 16, 24, 32, 48][i % 8] + 0.5 * (i % 2),
          used_gb=37.3 * (i + 1), on=i % 7 != 3)
       for i in range(24)]


@pytest.fixture(scope="module")
def datasets(tmp_path_factory):
    import import_service
    from test_nutanix_collector import _workbook_bytes
    d = tmp_path_factory.mktemp("excl")
    paths = {
        "rvtools": rvtools(str(d / "rv.xlsx"), HOSTS, VMS,
                           [("ds-prod", 8192, 3100), ("ds-dev", 2048, 700)]),
        "liveoptics": liveoptics(str(d / "lo.xlsx"), HOSTS, VMS,
                                 [("ds-prod", 8192, 3100, ["esx1", "esx2"]),
                                  ("ds-dev", 2048, 700, ["esx3"])]),
    }
    nx = d / "Nutanix Collector.xlsx"
    nx.write_bytes(_workbook_bytes())
    paths["nutanix"] = str(nx)
    return {k: import_service.parse_assessment(p, os.path.basename(p)) for k, p in paths.items()}


def _cases(data, seed):
    """Seeded random exclusion states over one dataset's VM list."""
    rnd = random.Random(seed)
    n = len(data["vms"])
    yield {"compute": [], "storage": [], "config": {}, "added": [], "removed": [], "local": False}
    for _ in range(CASES_PER_FILE):
        compute = sorted(rnd.sample(range(n), rnd.randint(0, n)))
        storage = sorted(rnd.sample(range(n), rnd.randint(0, n)))
        config = {}
        for i in rnd.sample(range(n), rnd.randint(0, min(3, n))):
            edit = {}
            if rnd.random() < 0.7:
                edit["vcpus"] = rnd.randint(1, 16)
            if rnd.random() < 0.7:
                edit["provisioned_memory_gb"] = round(rnd.uniform(0.5, 96), 2)
            config[i] = edit
        yield {"compute": compute, "storage": storage, "config": config,
               "added": [], "removed": [], "local": rnd.random() < 0.5}


def _with_added_vm(data, case):
    """The browser's hand-added VM: appended to the list, net-new storage."""
    vms = list(data["vms"]) + [{"name": "new01", "vcpus": 6, "provisioned_memory_gb": 20,
                                "consumed_memory_gb": 0, "vdisk_used_gb": 250,
                                "vdisk_size_gb": 250, "powered_on": True, "is_template": False}]
    idx = len(vms) - 1
    return vms, dict(case, added=[idx], config=dict(case["config"], **{idx: {"vdisk_used_gb": 250}}))


JS = """([summary, vms, c]) => {
    originalImportSummary = summary;
    importVms = vms;
    vmExclusions = { compute: new Set(c.compute), storage: new Set(c.storage) };
    vmConfig = c.config;
    vmAdded = new Set(c.added);
    vmRemoved = new Set(c.removed);
    includeLocalStorage = c.local;
    return computeAdjustedImportSummary();
}"""


def _same(js, py, where):
    assert set(js) == set(py), (where, set(js) ^ set(py))
    for k in py:
        a, b = js[k], py[k]
        if isinstance(b, float) or isinstance(a, float):
            assert a == pytest.approx(b, abs=1e-9), (where, k, a, b)
        else:
            assert a == b, (where, k, a, b)


@pytest.mark.parametrize("kind", ["rvtools", "liveoptics", "nutanix"])
def test_browser_and_python_exclusions_agree(server, browser, datasets, kind):
    import import_service
    data = datasets[kind]
    s = Session(browser, server, USER)
    s.goto("/")
    seed = {"rvtools": 1, "liveoptics": 2, "nutanix": 3}[kind]
    checked = 0
    for n, case in enumerate(_cases(data, seed)):
        vms = data["vms"]
        if n % 5 == 4:
            vms, case = _with_added_vm(data, case)
            if n % 10 == 9:
                case = dict(case, removed=list(case["added"]),
                            compute=sorted(set(case["compute"]) | set(case["added"])))
        js = s.page.evaluate(JS, [data["summary"], vms,
                                  dict(case, config={str(k): v for k, v in case["config"].items()})])
        py = import_service.adjust_summary(
            data["summary"], vms, case["compute"], case["storage"],
            include_local_storage=case["local"], vm_config=case["config"],
            added=case["added"], removed=case["removed"])
        _same(json.loads(json.dumps(js)), json.loads(json.dumps(py)), (kind, n, case))
        checked += 1
    assert checked == CASES_PER_FILE + 1
    assert not s.errors, s.errors
    s.close()
