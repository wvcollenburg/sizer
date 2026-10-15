"""DR follows production (plan A, G2/P1/P2).

A DR target and a workload sizing that receives replicas both grow the inbound
reserve at each SOURCE's own growth options (years, compute %, storage %,
snapshot %), read from the source's stored projection, else its saved form
fields, else the defaults. The DR request's own growth fields are ignored.

Run: .venv/bin/python -m pytest tests/test_dr_follows_production.py -q
"""
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.pop("SESSION_COOKIE_SECURE", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402
import app as appmod  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402


@pytest.fixture()
def client():
    app = appmod.app
    app.config["TESTING"] = True
    app.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    with app.app_context():
        db.drop_all()
        db.create_all()
    c = app.test_client()
    c.post("/api/auth/signup", json={"email": "dr@examplecorp.com",
                                     "password": "Abcdef1!xy",
                                     "accept_privacy": True})
    return c


def _project(c):
    return c.post("/api/projects/", json={"name": "P"}).get_json()["id"]


def _source(c, pid, name, projection=None, fields=None, vcpus=100, ram_gb=400,
            storage_tb=10, sized=True, appliance=False):
    payload = {"mode": "import", "fields": fields or {},
               "import": {"importSummary": {
                   "total_vcpus": vcpus, "total_vm_provisioned_memory_gb": ram_gb,
                   "datastore_used_tb": storage_tb}}}
    src = c.post("/api/configs/", json={"name": name, "payload": payload,
                                        "project_id": pid}).get_json()
    if sized:
        cluster = {"name": "", "recommendation": {"refs": {"mode": "import"}},
                   "projection": projection or {}, "refs": {"mode": "import"}}
        if appliance:
            cluster["summary"] = None
            cluster["config"] = {"cluster_total": {
                "cores": vcpus, "ram_gb": ram_gb, "usable_storage_tb": storage_tb}}
        else:
            cluster["summary"] = {"total_vcpus": vcpus,
                                  "total_vm_provisioned_memory_gb": ram_gb,
                                  "datastore_used_tb": storage_tb}
        r = c.put(f"/api/sizings/{src['id']}/result",
                  json={"clusters": [cluster], "totals": None})
        assert r.status_code in (200, 201), r.get_data(as_text=True)
    return src


def _link(c, src, tgt, cpct=100, spct=100):
    r = c.post(f"/api/sizings/{src['id']}/replication", json={
        "target_configuration_id": tgt["id"], "source_cluster": "",
        "target_cluster": "", "compute_pct": cpct, "storage_pct": spct,
        "mode": "reserved"})
    assert r.status_code in (200, 201), r.get_data(as_text=True)


def _dr(c, pid):
    return c.post(f"/api/projects/{pid}/dr-target", json={"name": "DR"}).get_json()


def _grow(base, g, years, gs=None, snap=0):
    gs = g if gs is None else gs
    sf = (1 + gs / 100) ** years
    st = base[2] * sf * (1 + snap / 100 * sf)
    cf = (1 + g / 100) ** years
    return base[0] * cf, base[1] * cf, st


def test_dr_sizes_at_the_source_rate_not_the_old_default(client):
    pid = _project(client)
    src = _source(client, pid, "Prod",
                  projection={"years": 5, "growth_pct": 5, "snapshot_pct": 0})
    dr = _dr(client, pid)
    _link(client, src, dr)
    # The request's growth fields (the old DR form) are ignored.
    out = client.post(f"/api/sizings/{dr['id']}/dr-recommend",
                      json={"growth_pct": 10, "years": 5, "snapshot_pct": 20}).get_json()
    v, r, s = _grow((100, 400, 10), 5, 5)
    g = out["grown_reserve"]
    assert g["years"] == 5
    assert g["vcpus"] == round(v, 1)
    assert g["ram_gb"] == round(r, 1)
    assert g["storage_tb"] == round(s, 2)
    p = out["projection"]
    assert p["pregrown"] is True
    assert p["years"] == 5
    assert p["projected_storage_tb"] == round(s, 2)
    assert abs(p["growth_pct"] - 5) < 0.2       # effective rate, base is rounded
    row = out["sources"][0]
    assert (row["years"], row["growth_pct"], row["storage_growth_pct"],
            row["snapshot_pct"]) == (5, 5, 5, 0)


def test_two_sources_grow_by_their_own_options(client):
    pid = _project(client)
    a = _source(client, pid, "A", vcpus=100, ram_gb=400, storage_tb=10,
                projection={"years": 3, "growth_pct": 5, "snapshot_pct": 10})
    b = _source(client, pid, "B", vcpus=50, ram_gb=200, storage_tb=20,
                projection={"years": 5, "growth_pct": 15, "storage_growth_pct": 30,
                            "snapshot_pct": 20})
    dr = _dr(client, pid)
    _link(client, a, dr)
    _link(client, b, dr, cpct=50, spct=100)
    out = client.post(f"/api/sizings/{dr['id']}/dr-recommend", json={}).get_json()
    av, ar, as_ = _grow((100, 400, 10), 5, 3, snap=10)
    bv, br, bs = _grow((25, 100, 20), 15, 5, gs=30, snap=20)
    g = out["grown_reserve"]
    assert g["years"] == 5                       # P1: the longest horizon
    assert g["vcpus"] == round(av + bv, 1)
    assert g["ram_gb"] == round(ar + br, 1)
    assert g["storage_tb"] == round(as_ + bs, 2)
    assert out["projection"]["years"] == 5
    rows = {r["sizing_name"]: r for r in out["sources"]}
    assert rows["A"]["years"] == 3 and rows["B"]["years"] == 5
    # The stored projection names them too, for the exports.
    names = {d["sizing_name"]: d for d in out["projection"]["dr_sources"]}
    assert names["B"]["storage_growth_pct"] == 30 and names["A"]["snapshot_pct"] == 10
    assert rows["B"]["storage_growth_pct"] == 30


def test_appliance_source_does_not_grow(client):
    pid = _project(client)
    src = _source(client, pid, "Box", appliance=True, vcpus=64, ram_gb=512,
                  storage_tb=20, projection={"years": 5, "growth_pct": 20})
    dr = _dr(client, pid)
    _link(client, src, dr)
    out = client.post(f"/api/sizings/{dr['id']}/dr-recommend", json={}).get_json()
    assert out["grown_reserve"]["vcpus"] == 64
    assert out["grown_reserve"]["ram_gb"] == 512
    assert out["grown_reserve"]["storage_tb"] == 20
    assert out["sources"][0]["growth_pct"] == 0


def test_unsized_source_falls_back_to_its_fields(client):
    pid = _project(client)
    src = _source(client, pid, "New", sized=False, fields={
        "growth-years": "2", "growth-pct": "20", "snapshot-pct": "0",
        "separate-storage-growth": True, "storage-growth-pct": "50"})
    dr = _dr(client, pid)
    _link(client, src, dr)
    out = client.post(f"/api/sizings/{dr['id']}/dr-recommend", json={}).get_json()
    v, r, s = _grow((100, 400, 10), 20, 2, gs=50)
    g = out["grown_reserve"]
    assert g["years"] == 2
    assert g["vcpus"] == round(v, 1)
    assert g["storage_tb"] == round(s, 2)
    assert out["sources"][0]["sized"] is False
    assert out["sources"][0]["storage_growth_pct"] == 50


def test_storage_toggle_off_in_fields_ignores_stale_storage_rate(client):
    pid = _project(client)
    src = _source(client, pid, "New", sized=False, fields={
        "growth-years": "2", "growth-pct": "20", "snapshot-pct": "0",
        "separate-storage-growth": False, "storage-growth-pct": "50"})
    dr = _dr(client, pid)
    _link(client, src, dr)
    out = client.post(f"/api/sizings/{dr['id']}/dr-recommend", json={}).get_json()
    assert out["sources"][0]["storage_growth_pct"] == 20


def test_workload_receiver_reserve_grows_at_the_source_rate(client):
    pid = _project(client)
    src = _source(client, pid, "Prod",
                  projection={"years": 3, "growth_pct": 0, "storage_growth_pct": 25,
                              "snapshot_pct": 0})
    rcv = _source(client, pid, "Receiver",
                  projection={"years": 5, "growth_pct": 40, "snapshot_pct": 50})
    _link(client, src, rcv, cpct=50, spct=50)
    d = client.get(f"/api/sizings/{rcv['id']}/inbound-reserve").get_json()
    assert d["reserve"]["pregrown"] is True
    assert d["reserve"]["vcpus"] == 50              # compute 0 %: not grown
    assert d["reserve"]["ram_gb"] == 200
    assert d["reserve"]["storage_tb"] == round(5 * 1.25 ** 3, 2)
    assert d["base_reserve"] == {"vcpus": 50, "ram_gb": 200, "storage_tb": 5}

    # The receiver's engine call holds it as-is, whatever its own rates.
    summ = {"total_vcpus": 10, "total_vm_provisioned_memory_gb": 40,
            "datastore_used_tb": 1, "nic_speed_mbps": 10000}
    a = client.post("/api/recommend", json={
        "summary": summ, "growth_pct": 40, "years": 5, "snapshot_pct": 50,
        "replication_reserve": d["reserve"]}).get_json()
    b = client.post("/api/recommend", json={
        "summary": summ, "growth_pct": 0, "years": 1, "snapshot_pct": 0,
        "replication_reserve": d["reserve"]}).get_json()
    assert a["projection"]["years"] == 5 and b["projection"]["years"] == 1


def test_can_edit_flag_mirrors_the_put_rule(client):
    pid = _project(client)
    dr = _dr(client, pid)
    assert client.get(f"/api/configs/{dr['id']}").get_json()["can_edit"] is True
    # A colleague in the same tenant sees it read-only.
    other = appmod.app.test_client()
    other.post("/api/auth/signup", json={"email": "colleague@examplecorp.com",
                                         "password": "Abcdef1!xy",
                                         "accept_privacy": True})
    row = other.get(f"/api/configs/{dr['id']}").get_json()
    assert row["can_edit"] is False
    r = other.put(f"/api/configs/{dr['id']}", json={"payload": {"mode": "dr_target"}})
    assert r.status_code == 403


def test_saved_dr_payload_keeps_follows_sources(client):
    pid = _project(client)
    dr = _dr(client, pid)
    payload = {"mode": "dr_target", "dr": {"vcpu_ratio": 3, "follows_sources": True}}
    assert client.put(f"/api/configs/{dr['id']}", json={"payload": payload}).status_code == 200
    assert client.get(f"/api/configs/{dr['id']}").get_json()["payload"] == payload
