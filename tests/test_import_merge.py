"""Several assessment files into one site (plan B, phase 4: the server merge).

Covers import_merge.merge, the raw import mode, /api/import-merge, and the
first unit tests for parse_rvtools and cluster_split. Workbooks are small
synthetic files (tests/merge_fixtures.py) that go through the real parsers;
the real Live Optics exports in _archive/ are used only when present.

Run: .venv/bin/python -m pytest tests/test_import_merge.py -q
"""
import glob
import json
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.pop("SESSION_COOKIE_SECURE", None)

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402

import merge_fixtures as F  # noqa: E402
from cluster_split import UNCLUSTERED, split_by_cluster  # noqa: E402
from import_merge import STANDALONE, STANDALONE_TARGET, host_key, merge  # noqa: E402
from liveoptics import parse_liveoptics  # noqa: E402
from parser_common import build_summary, collection_date  # noqa: E402
from rvtools import parse_rvtools  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _ds(parsed, sid, file_type, name, collected, sha=None):
    return dict(parsed, id=sid, file_type=file_type,
                source_meta={"file_name": name, "file_type": file_type,
                             "file_sha256": sha or sid, "collected_at": collected})


def _rv(tmp_path, name, hosts, vms, stores, date="2026/09/01 10:00:00"):
    p = F.rvtools(tmp_path / name, hosts, vms, stores, date=date)
    parsed = parse_rvtools(p)
    return parsed, collection_date(parsed["project"])


def _lo(tmp_path, name, hosts, vms, stores, date="2026/09/07", **kw):
    p = F.liveoptics(tmp_path / name, hosts, vms, stores, date=date, **kw)
    parsed = parse_liveoptics(p)
    return parsed, collection_date(parsed["project"])


_STRIP = ("src", "source_cluster")


def _bare(records, rename_cluster=True):
    out = []
    for r in records:
        r = {k: v for k, v in r.items() if k not in _STRIP}
        out.append(r)
    return out


# ── parse_rvtools (first unit tests) ─────────────────────────────────────────

def test_parse_rvtools_reads_vms_hosts_and_identity(tmp_path):
    parsed, date = _rv(tmp_path, "one.xlsx",
                       [F.host("esx1.lab.local", cores=16, ghz=2.5, cpu_pct=40)],
                       [F.vm("a", "esx1.lab.local", vcpus=4, ram_gb=16, used_gb=100,
                             uuid="5001-A", bios="4201-A"),
                        F.vm("b", "esx1.lab.local", vcpus=2, on=False)],
                       [("ds1", 1000, 400)])
    s = parsed["summary"]
    assert s["source"] == "rvtools"
    assert s["host_count"] == 1 and s["total_vms"] == 2 and s["active_vms"] == 1
    assert s["total_vcpus"] == 4
    assert s["datastore_used_tb"] == round(400 / 1024, 2)
    # vHost CPU usage % is a one-off snapshot: counted as measured CPU.
    assert s["peak_cpu_ghz"] == 16.0
    assert s["p95_iops"] == 0
    vm = parsed["vms"][0]
    assert vm["uuid"] == "5001-a" and vm["bios_uuid"] == "4201-a"
    assert "uuid" not in parsed["vms"][1]
    assert date == "2026-09-01"


# ── cluster_split (first unit tests) ─────────────────────────────────────────

def _cs_data(stores):
    return {
        "hosts": [{"name": "h1", "cluster": "A"}, {"name": "h2", "cluster": "B"}],
        "vms": [{"name": "v1", "cluster": "A", "powered_on": True, "is_template": False,
                 "vdisk_used_gb": 300},
                {"name": "v2", "cluster": "B", "powered_on": True, "is_template": False,
                 "vdisk_used_gb": 100}],
        "host_performance": [{"host": "h1"}, {"host": "h2"}],
        "datastores": stores, "host_nics": []}


def test_split_single_cluster_returns_none():
    d = _cs_data([])
    d["hosts"][1]["cluster"] = "A"
    d["vms"][1]["cluster"] = "A"
    assert split_by_cluster(d) is None


def test_split_attributes_by_mounts_evenly():
    out = dict(split_by_cluster(_cs_data([
        {"name": "shared", "capacity_gib": 100, "used_gib": 40, "free_gib": 60,
         "vm_count": 2, "mounts": ["h1", "h2"]},
        {"name": "onlyA", "capacity_gib": 10, "used_gib": 5, "free_gib": 5,
         "vm_count": 1, "mounts": ["h1"]}])))
    assert sorted(d["name"] for d in out["A"]["datastores"]) == ["onlyA", "shared"]
    assert [d["used_gib"] for d in out["B"]["datastores"]] == [20]


def test_split_without_mounts_is_proportional_to_vm_used():
    out = dict(split_by_cluster(_cs_data([
        {"name": "ds", "capacity_gib": 1000, "used_gib": 400, "free_gib": 600,
         "vm_count": 2}])))
    assert out["A"]["datastores"][0]["used_gib"] == 300
    assert out["B"]["datastores"][0]["used_gib"] == 100


def test_split_blank_cluster_is_unclustered():
    d = _cs_data([])
    d["hosts"][1]["cluster"] = ""
    d["vms"][1]["cluster"] = None
    assert [n for n, _ in split_by_cluster(d)] == ["A", UNCLUSTERED]


def test_host_key_strips_the_domain_not_an_ip():
    assert host_key("ESX01.corp.example") == "esx01"
    assert host_key("10.0.25.242") == "10.0.25.242"


# ── merge: the plan's scenarios ───────────────────────────────────────────────

def test_two_disjoint_rvtools_single_hosts_make_one_target(tmp_path):
    a, da = _rv(tmp_path, "esx1.xlsx", [F.host("esx1")],
                [F.vm("a1", "esx1"), F.vm("a2", "esx1", vcpus=8)], [("ds-esx1", 2000, 500)])
    b, db = _rv(tmp_path, "esx2.xlsx", [F.host("esx2", cores=24)],
                [F.vm("b1", "esx2", ram_gb=64)], [("ds-esx2", 1000, 300)])
    out = merge([_ds(a, "A", "rvtools", "esx1.xlsx", da),
                 _ds(b, "B", "rvtools", "esx2.xlsx", db)])
    assert out["overlaps"] == []
    # Standalone hosts from every file: one group, one target.
    assert [g["key"] for g in out["groups"]] == [STANDALONE]
    assert len(out["targets"]) == 1
    t = out["targets"][0]
    assert t["name"] == STANDALONE_TARGET
    concat = {k: a[k] + b[k] for k in ("hosts", "vms", "host_performance",
                                       "datastores", "host_nics")}
    expect = build_summary(concat, source="rvtools")
    got = dict(t["summary"])
    for k in ("cluster_name",):
        got.pop(k), expect.pop(k)
    assert got == expect
    assert {v["src"] for v in t["vms"]} == {"A", "B"}


def _vcenter(tmp_path, name, date, extra_vm=False):
    hosts = [F.host("h1.corp", "PROD"), F.host("h2.corp", "PROD")]
    vms = [F.vm("web", "h1.corp", "PROD", uuid="50-web"),
           F.vm("db", "h2.corp", "PROD", vcpus=16, ram_gb=128, uuid="50-db")]
    if extra_vm:
        vms.append(F.vm("new", "h2.corp", "PROD", uuid="50-new"))
    return _lo(tmp_path, name, hosts, vms, [("san", 10000, 4000, ["h1.corp", "h2.corp"])],
               date=date)


def test_two_collections_of_one_vcenter_keep_the_newest(tmp_path):
    old, d_old = _vcenter(tmp_path, "old.xlsx", "2026/06/01")
    new, d_new = _vcenter(tmp_path, "new.xlsx", "2026/09/01", extra_vm=True)
    out = merge([_ds(old, "O", "liveoptics", "old.xlsx", d_old),
                 _ds(new, "N", "liveoptics", "new.xlsx", d_new)])
    kinds = sorted((o["kind"], o["name"]) for o in out["overlaps"])
    assert kinds == [("host", "h1.corp"), ("host", "h2.corp"), ("vm", "db"), ("vm", "web")]
    assert all(o["choice"] == "N" and o["default"] == "N" for o in out["overlaps"])
    assert all(not o["possible"] for o in out["overlaps"] if o["kind"] == "vm")
    # Same cluster name in both files: two groups, flagged as likely duplicates.
    assert len(out["groups"]) == 2
    assert all(g["duplicate_of"] for g in out["groups"])
    # The old file's group has nothing left after "keep newest".
    assert len(out["targets"]) == 1
    t = out["targets"][0]
    expect = build_summary(new)
    got = dict(t["summary"])
    got.pop("cluster_name"), expect.pop("cluster_name")
    assert got == expect


def test_overlap_choices_keep_older_or_both(tmp_path):
    old, d_old = _vcenter(tmp_path, "old.xlsx", "2026/06/01")
    new, d_new = _vcenter(tmp_path, "new.xlsx", "2026/09/01")
    dsets = [_ds(old, "O", "liveoptics", "old.xlsx", d_old),
             _ds(new, "N", "liveoptics", "new.xlsx", d_new)]
    first = merge(dsets)
    web = next(o for o in first["overlaps"] if o["name"] == "web")
    host = next(o for o in first["overlaps"] if o["kind"] == "host")
    out = merge(dsets, resolutions={web["key"]: "both", host["key"]: "O",
                                    "vm:nonsense": "O"})
    vm_srcs = sorted((v["name"], v["src"]) for t in out["targets"] for v in t["vms"])
    assert ("web", "N") in vm_srcs and ("web", "O") in vm_srcs
    hosts = sorted((h["name"], h["src"]) for t in out["targets"] for h in t["hosts"])
    assert ("h1.corp", "O") in hosts and ("h2.corp", "N") in hosts
    # "Keep both" is a VM-only choice; on a host it falls back to the default.
    out2 = merge(dsets, resolutions={host["key"]: "both"})
    h = next(o for o in out2["overlaps"] if o["key"] == host["key"])
    assert h["choice"] == "N"


def test_name_only_match_is_a_possible_duplicate(tmp_path):
    lo, dl = _lo(tmp_path, "lo.xlsx", [F.host("h1")],
                 [F.vm("App01", "h1", uuid="50-app")], [("ds", 100, 10, ["h1"])])
    rv, dr = _rv(tmp_path, "rv.xlsx", [F.host("h9")], [F.vm("app01", "h9")],
                 [("ds9", 100, 10)])
    out = merge([_ds(lo, "L", "liveoptics", "lo.xlsx", dl),
                 _ds(rv, "R", "rvtools", "rv.xlsx", dr)])
    (ov,) = out["overlaps"]
    assert ov["kind"] == "vm" and ov["possible"] is True


def test_different_uuids_with_the_same_name_do_not_match(tmp_path):
    a, da = _lo(tmp_path, "a.xlsx", [F.host("h1")], [F.vm("app", "h1", uuid="50-1")],
                [("ds", 100, 10, ["h1"])])
    b, db = _lo(tmp_path, "b.xlsx", [F.host("h2")], [F.vm("app", "h2", uuid="50-2")],
                [("ds2", 100, 10, ["h2"])])
    out = merge([_ds(a, "A", "liveoptics", "a.xlsx", da),
                 _ds(b, "B", "liveoptics", "b.xlsx", db)])
    assert out["overlaps"] == []


def test_mixed_sources_extrapolate_iops_not_cpu(tmp_path):
    lo, dl = _lo(tmp_path, "lo.xlsx", [F.host("lo-h1")],
                 [F.vm("lo%02d" % i, "lo-h1") for i in range(10)],
                 [("lo-ds", 5000, 1000, ["lo-h1"])], iops_per_host=1000.0)
    rv, dr = _rv(tmp_path, "rv.xlsx", [F.host("rv-h1")],
                 [F.vm("rv%02d" % i, "rv-h1") for i in range(10)], [("rv-ds", 5000, 1000)])
    out = merge([_ds(lo, "L", "liveoptics", "lo.xlsx", dl),
                 _ds(rv, "R", "rvtools", "rv.xlsx", dr)])
    (t,) = out["targets"]
    assert lo["summary"]["p95_iops"] == 1000
    assert t["summary"]["p95_iops"] == 2000          # 10 + 10 equal VMs: doubled
    assert t["summary"]["total_peak_iops"] == 2 * lo["summary"]["total_peak_iops"]
    # CPU: RVTools carries a CPU snapshot, so both halves are measured.
    assert t["summary"]["peak_cpu_ghz"] == round(
        lo["summary"]["peak_cpu_ghz"] + rv["summary"]["peak_cpu_ghz"], 1)
    codes = [w["code"] for w in t["import_warnings"]]
    assert "mixed_sources" in codes and "no_iops" not in codes
    ext = [w["params"] for w in t["import_warnings"] if w["code"] == "perf_extrapolated"]
    assert ext == [{"metric": "io", "measured_pct": 50}]
    top = [w["code"] for w in out["import_warnings"]]
    assert top[0] == "mixed_sources" and "perf_extrapolated" in top
    srcs = {s["id"]: s for s in out["sources"]}
    assert srcs["L"]["has_iops"] and not srcs["R"]["has_iops"]
    assert srcs["R"]["has_cpu_perf"]


def test_metric_measured_nowhere_stays_zero(tmp_path):
    a, da = _rv(tmp_path, "a.xlsx", [F.host("a1")], [F.vm("x", "a1")], [("d", 100, 10)])
    b, db = _rv(tmp_path, "b.xlsx", [F.host("b1")], [F.vm("y", "b1")], [("e", 100, 10)])
    (t,) = merge([_ds(a, "A", "rvtools", "a.xlsx", da),
                  _ds(b, "B", "rvtools", "b.xlsx", db)])["targets"]
    assert t["summary"]["p95_iops"] == 0
    assert not [w for w in t["import_warnings"] if w["code"] == "perf_extrapolated"]
    assert "no_iops" in [w["code"] for w in t["import_warnings"]]


def _three_clusters(tmp_path):
    hosts = [F.host("a1", "BOCluster"), F.host("b1", "PROD"), F.host("b2", "PROD"),
             F.host("c1", "DB")]
    vms = ([F.vm("a-%d" % i, "a1", "BOCluster", used_gb=50) for i in range(3)]
           + [F.vm("b-%d" % i, "b%d" % (1 + i % 2), "PROD", used_gb=200) for i in range(6)]
           + [F.vm("c-%d" % i, "c1", "DB", vcpus=16, used_gb=500) for i in range(2)])
    stores = [("san", 20000, 6000, ["a1", "b1", "b2", "c1"]),
              ("prod-only", 4000, 1500, ["b1", "b2"]), ("db-local", 2000, 900, ["c1"])]
    return _lo(tmp_path, "three.xlsx", hosts, vms, stores)


def test_mapping_a_and_b_plus_c(tmp_path):
    parsed, d = _three_clusters(tmp_path)
    dsets = [_ds(parsed, "T", "liveoptics", "three.xlsx", d)]
    first = merge(dsets)
    keys = {g["cluster"]: g["key"] for g in first["groups"]}
    assert [t["name"] for t in first["targets"]] == ["BOCluster", "PROD", "DB"]
    out = merge(dsets, mapping={keys["PROD"]: "PROD+DB", keys["DB"]: "PROD+DB"})
    names = [t["name"] for t in out["targets"]]
    assert names == ["BOCluster", "PROD+DB"]
    a, bc = out["targets"]
    # The VM partition is exact.
    assert sorted(v["name"] for v in a["vms"]) == ["a-0", "a-1", "a-2"]
    assert len(bc["vms"]) == 8 and {v["source_cluster"] for v in bc["vms"]} == {"PROD", "DB"}
    assert {v["cluster"] for v in bc["vms"]} == {"PROD+DB"}
    # Datastore fractions sum to the combined set.
    total = sum(s["used_gib"] for s in parsed["datastores"])
    got = sum(s["used_gib"] for t in out["targets"] for s in t["datastores"])
    assert abs(got - total) < 0.05
    assert a["summary"]["host_count"] == 1 and bc["summary"]["host_count"] == 3
    # "All in one" is the same call with one name everywhere.
    one = merge(dsets, mapping={k: "Site" for k in keys.values()})
    assert len(one["targets"]) == 1
    assert one["targets"][0]["summary"]["datastore_used_tb"] == parsed["summary"]["datastore_used_tb"]


def test_same_cluster_name_in_two_files_gets_distinct_default_targets(tmp_path):
    a, da = _lo(tmp_path, "a.xlsx", [F.host("x1", "PROD")], [F.vm("v1", "x1", "PROD")],
                [("d1", 100, 10, ["x1"])])
    b, db = _lo(tmp_path, "b.xlsx", [F.host("y1", "PROD")], [F.vm("v2", "y1", "PROD")],
                [("d2", 100, 10, ["y1"])])
    out = merge([_ds(a, "A", "liveoptics", "a.xlsx", da),
                 _ds(b, "B", "liveoptics", "b.xlsx", db)])
    assert sorted(t["name"] for t in out["targets"]) == ["PROD · a", "PROD · b"]
    assert not any(g["duplicate_of"] for g in out["groups"])    # no shared hosts


def test_identical_sha256_is_dropped(tmp_path):
    a, da = _rv(tmp_path, "a.xlsx", [F.host("a1")], [F.vm("x", "a1")], [("d", 100, 10)])
    out = merge([_ds(a, "A", "rvtools", "a.xlsx", da, sha="same"),
                 _ds(a, "B", "rvtools", "copy.xlsx", da, sha="same")])
    assert [s["dropped"] for s in out["sources"]] == [None, "duplicate"]
    assert out["overlaps"] == []
    assert out["targets"][0]["summary"]["total_vms"] == 1
    assert {"code": "duplicate_file", "params": {"file": "copy.xlsx"}} in out["import_warnings"]


def test_merge_records_carry_src_and_source_cluster(tmp_path):
    parsed, d = _three_clusters(tmp_path)
    keys = {g["cluster"]: g["key"] for g in merge([_ds(parsed, "T", "liveoptics", "t", d)])["groups"]}
    out = merge([_ds(parsed, "T", "liveoptics", "t", d)], mapping={keys["DB"]: "BOCluster"})
    for t in out["targets"]:
        for rec in t["hosts"] + t["vms"]:
            assert rec["src"] == "T" and rec["source_cluster"]
        for rec in t["datastores"] + t["host_performance"]:
            assert rec["src"] == "T"


# ── routes ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def client():
    import app as appmod
    from database import db
    from extensions import limiter
    flask_app = appmod.app
    flask_app.config["TESTING"] = True
    limiter.enabled = False
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
    c = flask_app.test_client()
    c.post("/api/auth/signup", json={"email": "mi@examplecorp.com",
                                     "password": "Abcdef1!xy", "accept_privacy": True})
    return c


def test_raw_import_returns_records_without_sizing(client, tmp_path):
    p = F.rvtools(tmp_path / "esx1.xlsx", [F.host("esx1")], [F.vm("a", "esx1", uuid="50-a")],
                  [("d", 100, 10)])
    r = F.dataset(client, p)
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert "recommendations" not in d
    assert d["host_performance"] and d["file_type"] == "rvtools"
    assert d["vms"][0]["uuid"] == "50-a"
    assert d["source_meta"]["collected_at"] == "2026-09-01"
    assert len(d["source_meta"]["file_sha256"]) == 64


def test_a_bad_file_in_a_batch_is_rejected_alone(client, tmp_path):
    good = F.rvtools(tmp_path / "good.xlsx", [F.host("g1")], [F.vm("a", "g1")],
                     [("d", 100, 10)])
    # A Live Optics VMware export whose datastores cover too few VMs.
    bad = F.liveoptics(tmp_path / "bad.xlsx", [F.host("b1")],
                       [F.vm("b%d" % i, "b1") for i in range(10)], [])
    rb = F.dataset(client, bad)
    assert rb.status_code == 422 and rb.get_json()["error_code"] == "lo_incomplete"
    junk = tmp_path / "junk.xlsx"
    junk.write_bytes(b"not a workbook")
    assert F.dataset(client, str(junk)).status_code == 400
    rg = F.dataset(client, good)
    assert rg.status_code == 200
    ds = dict(rg.get_json(), id="good")
    out = client.post("/api/import-merge", json={"datasets": [ds]})
    assert out.status_code == 200
    assert out.get_json()["targets"][0]["summary"]["total_vms"] == 1


def test_merge_route_round_trip(client, tmp_path):
    a = F.dataset(client, F.rvtools(tmp_path / "a.xlsx", [F.host("a1")], [F.vm("x", "a1")],
                                    [("d", 100, 10)])).get_json()
    b = F.dataset(client, F.rvtools(tmp_path / "b.xlsx", [F.host("b1")], [F.vm("y", "b1")],
                                    [("e", 100, 10)])).get_json()
    body = {"datasets": [dict(a, id="a"), dict(b, id="b")], "resolutions": {},
            "mapping": {}}
    r = client.post("/api/import-merge", json=body)
    assert r.status_code == 200
    d = r.get_json()
    assert len(d["targets"]) == 1 and d["targets"][0]["summary"]["host_count"] == 2
    # Split the standalone hosts after all: not possible per host, they are
    # ONE group; a rename still applies.
    r2 = client.post("/api/import-merge", json=dict(body, mapping={STANDALONE: "Branches"}))
    assert r2.get_json()["targets"][0]["name"] == "Branches"


@pytest.mark.parametrize("body", [None, {}, {"datasets": "x"}, {"datasets": []},
                                  {"datasets": [{}], "mapping": []},
                                  {"datasets": [{"hosts": "nope", "vms": 5}]}])
def test_merge_route_rejects_bad_input(client, body):
    r = client.post("/api/import-merge", data=json.dumps(body) if body is not None else "x",
                    content_type="application/json")
    assert r.status_code == 400


# ── the real exports, when present ────────────────────────────────────────────

_REAL = os.path.join(ROOT, "_archive", "LiveOptics_3455045_VMWARE_09_07_2026.xlsx")


@pytest.mark.skipif(not os.path.exists(_REAL), reason="real Live Optics export not present")
def test_real_three_cluster_export_a_and_b_plus_c():
    parsed = parse_liveoptics(_REAL)
    dsets = [_ds(parsed, "R", "liveoptics", "real.xlsx", collection_date(parsed["project"]))]
    keys = {g["cluster"]: g["key"] for g in merge(dsets)["groups"]}
    assert {"BOCluster", "PROD", "DB"} <= set(keys)
    out = merge(dsets, mapping={keys["PROD"]: "PROD+DB", keys["DB"]: "PROD+DB"})
    names = {t["name"] for t in out["targets"]}
    assert "PROD+DB" in names and "BOCluster" in names
    total = sum(s["used_gib"] for s in parsed["datastores"])
    got = sum(s["used_gib"] for t in out["targets"] for s in t["datastores"])
    assert abs(got - total) < 1.0
    n_vms = sum(len(t["vms"]) for t in out["targets"])
    assert n_vms == len(parsed["vms"])
    assert all(v.get("uuid") for v in parsed["vms"])


@pytest.mark.skipif(not glob.glob(os.path.join(ROOT, "_archive", "LiveOptics_*VMWARE*.xlsx")),
                    reason="real Live Optics exports not present")
def test_real_export_merged_with_itself_is_all_overlap():
    parsed = parse_liveoptics(_REAL if os.path.exists(_REAL) else
                              glob.glob(os.path.join(ROOT, "_archive", "LiveOptics_*VMWARE*.xlsx"))[0])
    d = collection_date(parsed["project"])
    out = merge([_ds(parsed, "A", "liveoptics", "a.xlsx", d, sha="a"),
                 _ds(parsed, "B", "liveoptics", "b.xlsx", d, sha="b")])
    n_vm = sum(1 for o in out["overlaps"] if o["kind"] == "vm")
    assert n_vm == len({v["uuid"] for v in parsed["vms"]})
    total = sum(t["summary"]["total_vms"] for t in out["targets"])
    assert total == parsed["summary"]["total_vms"]
