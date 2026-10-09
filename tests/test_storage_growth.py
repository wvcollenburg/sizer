"""Separate storage growth and pre-grown demand in the engine (recommend.py).

project_demand is the pure growth step shared by the engine and the DR path;
generate_recommendations takes an optional storage_growth_pct (storage and the
snapshot reserve grow at it, compute at growth_pct) and an optional pregrown
demand that skips the growth step while the day-one floors stay on the base.

Run:  .venv/bin/python -m pytest tests/test_storage_growth.py -q
"""
import math
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from tests.test_perf_sizing_e2e import _build_app, _seed_catalog, _set, _summary  # noqa: E402
from database import db  # noqa: E402
from recommend import generate_recommendations, project_demand  # noqa: E402


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


BASE = {"vcpus": 100, "ram_gb": 400, "storage_tb": 10}


# ── project_demand ────────────────────────────────────────────────────────────

def test_project_demand_single_rate_matches_old_formula():
    g = project_demand(BASE, 10, None, 20, 5)
    f = 1.1 ** 5
    snap = 0.2 * 1.1 ** 5
    assert approx(g["vcpus"], 100 * f)
    assert approx(g["ram_gb"], 400 * f)
    assert approx(g["storage_tb"], 10 * f * (1 + snap))
    assert approx(g["snapshot_tb"], 10 * f * snap)
    assert approx(g["compute_factor"], g["storage_factor"])


def test_project_demand_separate_rates():
    g = project_demand(BASE, 0, 20, 20, 3)
    s = 1.2 ** 3
    assert approx(g["vcpus"], 100)
    assert approx(g["ram_gb"], 400)
    assert approx(g["storage_tb"], 10 * s * (1 + 0.2 * s))
    assert approx(g["snapshot_tb"], 10 * s * 0.2 * s)
    assert approx(g["compute_factor"], 1.0)
    assert approx(g["storage_factor"], s)


def test_project_demand_zero_base():
    g = project_demand({}, 10, 30, 20, 5)
    assert g["vcpus"] == g["ram_gb"] == g["storage_tb"] == g["snapshot_tb"] == 0


# ── engine ────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def app():
    app = _build_app()
    with app.app_context():
        db.create_all()
        _seed_catalog()
        _set(perf_scaling=1, perf_ghz_balance=0)
    return app


def _run(app, summary=None, **kw):
    kw.setdefault("growth_pct", 10)
    kw.setdefault("snapshot_pct", 20)
    kw.setdefault("years", 5)
    with app.app_context():
        return generate_recommendations(
            summary or _summary(), max_day_one_storage_pct=100,
            max_day_one_ram_pct=100, **kw)


def _strip_svg(result):
    for r in result["recommendations"]:
        r.pop("network_svg", None)
    return result


def test_toggle_off_projection_keys(app):
    p = _run(app)["projection"]
    assert p["separate_storage_growth"] is False
    assert p["storage_growth_pct"] == p["growth_pct"] == 10
    assert p["storage_growth_factor"] == p["growth_factor"]
    assert "pregrown" not in p


def test_equal_explicit_rate_gives_same_sizing(app):
    off = _strip_svg(_run(app))
    on = _strip_svg(_run(app, storage_growth_pct=10))
    assert on["recommendations"] == off["recommendations"]
    po, pn = dict(off["projection"]), dict(on["projection"])
    assert pn.pop("separate_storage_growth") is True
    po.pop("separate_storage_growth")
    assert pn == po


def test_compute_zero_storage_twenty(app):
    s = _summary()
    flat = _run(app, growth_pct=0, years=3)["projection"]
    res = _run(app, growth_pct=0, storage_growth_pct=20, years=3)
    p = res["projection"]
    f = 1.2 ** 3
    assert p["projected_vcpus"] == s["total_vcpus"]
    assert p["projected_ram_gb"] == round(s["total_vm_provisioned_memory_gb"], 1)
    expect = s["datastore_used_tb"] * f * (1 + 0.2 * f)
    assert approx(p["projected_storage_tb"], round(expect, 2))
    assert p["growth_factor"] == 1.0
    assert p["storage_growth_factor"] == round(f, 3)
    assert p["separate_storage_growth"] is True
    assert p["snapshot_pct_at_target"] == round(0.2 * f * 100, 1)
    # The GHz / perf floor is compute: unchanged by the storage rate.
    assert p["projected_ghz"] == flat["projected_ghz"]
    assert p["compute_floor"]["required_ghz"] == flat["compute_floor"]["required_ghz"]


def test_storage_rate_drives_storage_util(app):
    lo = _run(app, growth_pct=10, storage_growth_pct=0)["projection"]
    hi = _run(app, growth_pct=10, storage_growth_pct=40)["projection"]
    assert lo["projected_vcpus"] == hi["projected_vcpus"]
    assert hi["projected_storage_tb"] > lo["projected_storage_tb"]


def test_invalid_storage_rate_falls_back(app):
    p = _run(app, storage_growth_pct="abc")["projection"]
    assert p["separate_storage_growth"] is False
    assert p["storage_growth_pct"] == 10


def test_pregrown_skips_growth(app):
    s = _summary()
    pre = {"vcpus": 200, "ram_gb": 500, "storage_tb": 12, "snapshot_tb": 2, "years": 3}
    res = _run(app, growth_pct=50, snapshot_pct=90, pregrown=pre)
    p = res["projection"]
    assert p["pregrown"] is True
    assert p["years"] == 3
    assert p["projected_vcpus"] == 200
    assert p["projected_ram_gb"] == 500
    assert p["projected_storage_tb"] == 12
    # Base stays the day-one summary, factors are implied.
    assert p["base_vcpus"] == s["total_vcpus"]
    assert p["growth_factor"] == round(200 / s["total_vcpus"], 3)
    assert p["storage_growth_factor"] == round(10 / s["datastore_used_tb"], 3)
    assert approx(p["growth_pct"], round(((200 / 64) ** (1 / 3) - 1) * 100, 2))
    assert res["recommendations"]


def test_pregrown_keeps_day_one_floors_on_base(app):
    # A tiny pre-grown demand must still respect the day-one floor computed
    # from the base: storage capped at 10 % of capacity today means >= 10x
    # the base used TB, whatever the projected figure says.
    pre = {"vcpus": 64, "ram_gb": 200, "storage_tb": 5, "snapshot_tb": 0, "years": 1}
    with app.app_context():
        res = generate_recommendations(_summary(),
                                       growth_pct=0, snapshot_pct=0, years=1,
                                       max_day_one_storage_pct=10,
                                       max_day_one_ram_pct=100, pregrown=pre)
    top = res["recommendations"][0]
    assert top["totals"]["usable_storage_tb"] >= 50 - 1e-6


def _storage_total(res):
    return res["recommendations"][0]["utilization"]["storage"]["abs"]["total"]


def _ram_total(res):
    return res["recommendations"][0]["utilization"]["ram"]["abs"]["total"]


def test_replication_reserve_grows_at_storage_rate(app):
    rep = {"vcpus": 0, "ram_gb": 0, "storage_tb": 4}
    res = _run(app, growth_pct=10, storage_growth_pct=0, snapshot_pct=0,
               replication_reserve=rep)
    p = res["projection"]
    # Storage factor 1: the reserve lands unchanged next to the own storage.
    assert approx(_storage_total(res), round(p["projected_storage_tb"] + 4, 2), 0.02)


def test_pregrown_replication_reserve_not_regrown(app):
    rep = {"vcpus": 32, "ram_gb": 100, "storage_tb": 3, "pregrown": True}
    res = _run(app, growth_pct=50, snapshot_pct=50, years=5,
               replication_reserve=rep)
    p = res["projection"]
    assert approx(_storage_total(res), round(p["projected_storage_tb"] + 3, 2), 0.02)
    assert approx(_ram_total(res), round(p["projected_ram_gb"] + 100, 1), 0.2)


def test_legacy_replication_reserve_still_grown(app):
    rep = {"vcpus": 32, "ram_gb": 100, "storage_tb": 3}
    res = _run(app, growth_pct=10, snapshot_pct=0, years=5,
               replication_reserve=rep)
    p = res["projection"]
    f = 1.1 ** 5
    assert approx(_ram_total(res), round(p["projected_ram_gb"] + 100 * f, 1), 0.2)


# ── /api/recommend passes the storage rate through ───────────────────────────

def test_recommend_route_passes_storage_growth():
    os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
    os.environ.setdefault("ENABLE_SCHEDULER", "0")
    os.environ.setdefault("SECRET_KEY", "test-secret")
    import app as appmod
    from extensions import limiter
    flask_app = appmod.app
    flask_app.config["TESTING"] = True
    limiter.enabled = False
    with flask_app.app_context():
        db.drop_all()
        db.create_all()
    client = flask_app.test_client()
    client.post("/api/auth/signup", json={"email": "sg@examplecorp.com",
                                          "password": "Abcdef1!xy",
                                          "accept_privacy": True})
    body = {"summary": _summary(), "growth_pct": 0, "snapshot_pct": 0, "years": 2}
    off = client.post("/api/recommend", json=body).get_json()["projection"]
    on = client.post("/api/recommend",
                     json=dict(body, storage_growth_pct=50)).get_json()["projection"]
    assert off["separate_storage_growth"] is False
    assert on["separate_storage_growth"] is True
    assert on["storage_growth_pct"] == 50
    assert on["projected_vcpus"] == off["projected_vcpus"]
    assert on["projected_storage_tb"] == round(5 * 1.5 ** 2, 2)
