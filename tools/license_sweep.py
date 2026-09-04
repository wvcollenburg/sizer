"""Calibration sweep for licence-aware scoring (docs/pricebook-plan.md §10.3).

Produces the before/after report that decides whether `license_scoring` can be
turned on. The rule from the plan: **every changed recommendation must have a
stated reason.** Anything unexplained is a bug, not a tuning target.

Two corpora, because the real one is not enough on its own:

  * The archived LiveOptics exports — real, but only three, all small and all
    83-100% Windows. Good regression anchors, useless for the boundaries.
  * Synthetic cases built to land ON the decision boundaries that actually change
    answers: the licence cap, the Essentials 3-node/256 GB edge, the [3,3] split
    that must NOT qualify, above-the-ladder core counts, and a pure-Linux estate.

Also reports two things the plan calls out as easy to miss:

  * the **w_waste re-scale factor**, because dropping magnitude parity grows the
    cost side and waste must grow with it or the ranker quietly stops caring
    about over-provisioning;
  * the **guest-exposure kill criterion** — if flipping `none` vs `windows`
    moves node counts, the multiplier spread is too wide (§5.6).

Run from the repo root:

    DATABASE_URL=sqlite:///:memory: SECRET_KEY=x ENABLE_SCHEDULER=0 \
        .venv/bin/python tools/license_sweep.py
"""
import glob
import os
import statistics
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "app"))

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "sweep")
os.environ.setdefault("ENABLE_SCHEDULER", "0")

import app as appmod                      # noqa: E402
from database import db                   # noqa: E402
from tunables import refresh_from_db, T   # noqa: E402
import licensing                          # noqa: E402
import pricebook_import                   # noqa: E402
from recommend import generate_recommendations  # noqa: E402

# The wasted-capacity weight as it stood BEFORE licence-aware scoring existed.
# The re-scale suggestion is derived from this, never from the live value.
W_WASTE_BASELINE = 50.0

ARCHIVE = os.path.join(ROOT, "_archive")
PRICELIST = os.path.join(ARCHIVE, "Scale Computing Q4 2025 EUR Master Price List.xlsx")


# ── Synthetic corpus ─────────────────────────────────────────────────────────
# Each case names the boundary it probes, so a changed recommendation can be
# explained rather than merely observed.

def _summary(**kw):
    base = {
        "active_vms": 40, "total_vms": 44, "total_vcpus": 180, "total_ram_gb": 900,
        "used_storage_tb": 20.0, "total_storage_tb": 60.0, "hosts": 4,
        "total_host_ghz": 400.0, "peak_cpu_ghz": 120.0, "total_host_cores": 96,
        "total_host_ram_gb": 1024, "vm_iops": 0, "peak_ram_gb": 700,
        "total_vm_provisioned_memory_gb": 900, "datastore_used_tb": 20.0,
        "nic_speed_mbps": 10000,
    }
    base.update(kw)
    return base


SYNTHETIC = [
    ("cap-just-under",
     "Workload landing just UNDER the per-node licence cap — cores still cost",
     _summary(total_vcpus=240, total_ram_gb=1200, peak_ram_gb=1000,
              total_vm_provisioned_memory_gb=1200), {}),
    ("cap-just-over",
     "Workload landing just OVER the cap — cores 49-64/node are free",
     _summary(total_vcpus=420, total_ram_gb=1800, peak_ram_gb=1500,
              total_vm_provisioned_memory_gb=1800), {}),
    ("essentials-inside",
     "3 nodes INSIDE the 256 GB/node Essentials ceiling — should qualify",
     _summary(total_vcpus=24, total_ram_gb=150, peak_ram_gb=130,
              total_vm_provisioned_memory_gb=150, used_storage_tb=3.0,
              datastore_used_tb=3.0, total_storage_tb=10.0, active_vms=8,
              total_vms=8, hosts=2, total_host_cores=24, total_host_ram_gb=256,
              total_host_ghz=80.0, peak_cpu_ghz=25.0),
     {"target_nodes": 3}),
    ("essentials-over-ram",
     "3 nodes above 256 GB/node — must NOT qualify, must report the near miss",
     _summary(total_vcpus=60, total_ram_gb=760, peak_ram_gb=700,
              total_vm_provisioned_memory_gb=760, used_storage_tb=8.0,
              datastore_used_tb=8.0, active_vms=20, total_vms=20),
     {"target_nodes": 3}),
    ("essentials-six-node-split",
     "6 nodes -> [3,3] capacity split: Essentials must NOT stack across clusters",
     _summary(total_vcpus=200, total_ram_gb=1400, peak_ram_gb=1200,
              total_vm_provisioned_memory_gb=1400), {"target_nodes": 6}),
    ("pure-linux",
     "Pure-Linux estate — guest exposure 'none', per-core burden discounted",
     _summary(), {"guest_licensing": "none"}),
    ("windows-db",
     "Windows plus a core-billed database — the heaviest per-core burden",
     _summary(), {"guest_licensing": "windows_db"}),
]


def _load_real():
    """The three archived LiveOptics exports, if present."""
    from liveoptics import parse_liveoptics
    out = []
    for path in sorted(glob.glob(os.path.join(ARCHIVE, "LiveOptics*.xlsx"))):
        try:
            summary = parse_liveoptics(path)["summary"]
        except Exception as exc:                      # noqa: BLE001
            print(f"  ! could not parse {os.path.basename(path)}: {exc}")
            continue
        out.append((os.path.basename(path)[:34], "archived real sizing",
                    summary, {}))
    return out


def _seed_catalog():
    """Seed the real 43-model catalog into whatever database is configured.

    Uses seed.py's per-table seeders directly rather than seed_all(), which runs
    a Postgres-only raw ALTER in _migrate_schema(). create_all() already builds
    the full ORM schema, so on SQLite the migration step is redundant anyway.

    A sweep against an EMPTY catalog silently reports "0 of 10 changed" and
    proves nothing — the same vacuous-pass trap the security tests guard
    against. So this refuses to continue without a catalog.
    """
    from orm_models import Model, DriveTypeIops, SizingSetting
    import seed as seedmod

    db.create_all()
    for dtype, iops in seedmod.DRIVE_TYPE_IOPS_DEFAULTS.items():
        if not DriveTypeIops.query.filter_by(drive_type=dtype).first():
            db.session.add(DriveTypeIops(drive_type=dtype, iops=iops))
    for key, value in seedmod.SIZING_SETTING_DEFAULTS.items():
        if not SizingSetting.query.filter_by(key=key).first():
            db.session.add(SizingSetting(key=key, value=value))
    db.session.commit()
    if not Model.query.first():
        seedmod.seed_appliance_models()
        seedmod.seed_validated_nics()
        seedmod.seed_switches()
        db.session.commit()
    # Without this the CpuCatalog rows carry no SPECrate, every candidate falls
    # back to the per-core silicon estimate, and the sweep silently measures the
    # fallback path instead of the real one.
    seedmod._backfill_cpu_specs()
    db.session.commit()

    count = Model.query.count()
    if count == 0:
        raise SystemExit("catalog is empty — the sweep would prove nothing")
    from orm_models import CpuCatalog
    total = CpuCatalog.query.count()
    scored = CpuCatalog.query.filter(CpuCatalog.specrate_int.isnot(None)).count()
    print(f"  catalog: {count} models, {scored}/{total} CPUs with a SPECrate")
    if scored == 0:
        raise SystemExit("no CPU benchmarks — the silicon term would be all "
                         "fallback and the sweep would measure nothing real")


def _set(**kw):
    from orm_models import SizingSetting
    for key, value in kw.items():
        row = SizingSetting.query.filter_by(key=key).first()
        if row is None:
            db.session.add(SizingSetting(key=key, value=value))
        else:
            row.value = value
    db.session.commit()
    refresh_from_db()


def _apply_tiers(tiers):
    """Write a tier ladder into the catalog. `None` restores the shipped one.
    Sweep-only: the engine reads Model.cost_tier, so attribution needs the rows
    themselves moved, not a monkeypatch."""
    from orm_models import Model
    from models import MODEL_COSTS
    source = tiers or MODEL_COSTS
    for model in Model.query.all():
        if model.name in source:
            model.cost_tier = source[model.name]
    db.session.commit()


def _top(summary, **kw):
    result = generate_recommendations(summary, **kw)
    recs = result.get("recommendations") or []
    if not recs:
        return None
    r = recs[0]
    return {
        "model": r["model"],
        "nodes": r["node_count"],
        "cpu": r["cpu"],
        "ram": r["ram_per_node_gb"],
        "licensing": r.get("licensing") or {},
    }


def _cost_side(summary, licensing_on, **kw):
    """Total cost-side magnitude of the winning candidate, in tier points.

    Reconstructed here rather than exported by the engine, because no euro
    figure may leave recommend.py (§3). Everything needed is already on the
    candidate — cores_per_node, hci_node_count, cpu_perf_index, cluster_layout —
    and the tier is looked up locally from the catalog.

    The ratio of this with the switch off vs on is what `w_waste` must be scaled
    by: the waste term is unchanged by this feature, so if the cost side grows
    and waste does not, over-provisioning quietly stops being penalised.
    """
    from tunables import T as _T
    from models import MODEL_COSTS
    import licensing as _lic
    from orm_models import load_license_book

    result = generate_recommendations(summary, **kw)
    recs = result.get("recommendations") or []
    if not recs:
        return None
    r = recs[0]
    tier = MODEL_COSTS.get(r["model"], 0)
    fleet_tier = r["node_count"] * (tier + _T.node_overhead)
    total_cores = r["cores_per_node"] * r["hci_node_count"]

    if not licensing_on:
        return fleet_tier + _T.w_core_license * total_cores

    book = load_license_book("EMEA")
    block = _lic.cluster_license(book, r["cluster_layout"], r["cores_per_node"],
                                 r["ram_per_node_gb"],
                                 (r.get("licensing") or {}).get("term_years", 5))
    if block["eur"] is None:
        return None
    exposure = {"none": _T.guest_exposure_none,
                "windows": _T.guest_exposure_windows,
                "windows_db": _T.guest_exposure_windows_db}.get(
                    (r.get("licensing") or {}).get("guest_licensing", "windows"), 1.0)
    per_node_perf = r.get("cpu_perf_index") or (
        r["cores_per_node"] * _T.cpu_perf_per_core_fallback)
    return (fleet_tier
            + block["eur"] / _T.eur_per_tier_point
            + _T.w_core_burden * exposure * total_cores
            + _T.w_cpu_perf * (per_node_perf * r["hci_node_count"]) / 1000.0)


def _describe(before, after):
    """Why did this recommendation change? Unexplained means bug, not tuning."""
    if before is None or after is None:
        return "no candidates on one side"
    reasons = []
    lic = after["licensing"]
    if before["cpu"] != after["cpu"]:
        reasons.append(f"CPU {before['cpu']} -> {after['cpu']}")
    if before["nodes"] != after["nodes"]:
        reasons.append(f"nodes {before['nodes']} -> {after['nodes']}")
    if before["model"] != after["model"]:
        reasons.append(f"model {before['model']} -> {after['model']}")
    if before["ram"] != after["ram"]:
        reasons.append(f"RAM/node {before['ram']} -> {after['ram']}")
    if not reasons:
        return "unchanged"
    if lic.get("basis") == "essentials":
        reasons.append("Essentials flat price now wins")
    if lic.get("license_cap_reached"):
        reasons.append("licence capped: " + lic["license_cap_reached"])
    if lic.get("license_above_ladder"):
        reasons.append("above ladder: " + lic["license_above_ladder"])
    return "; ".join(reasons)


def main():
    if not os.path.exists(PRICELIST):
        print("No archived price list — nothing to calibrate against.")
        return 1

    application = appmod.app
    application.config["TESTING"] = True
    with application.app_context():
        _seed_catalog()
        pricebook_import.seed_feed_from_file(PRICELIST, label="sweep")

        corpus = _load_real() + [(n, d, s, k) for n, d, s, k in SYNTHETIC]

        # THREE stages, not two. The tier re-base and licence-aware scoring
        # shipped together, so a plain before/after superimposes both effects and
        # neither can be explained on its own. Running the old ladder first makes
        # the attribution recoverable.
        print("\n=== three-stage attribution " + "=" * 44)
        print("  A  old tier ladder, licensing off   (the true 'before')")
        print("  B  re-based ladder, licensing off   (isolates the RE-BASE)")
        print("  C  re-based ladder, licensing on    (isolates LICENSING)")

        import platform_tier as _pt
        _set(license_scoring=0)
        _apply_tiers(_pt.LEGACY_TIERS)
        stage_a = {name: _top(s, **k) for name, _d, s, k in corpus}
        _apply_tiers(None)                      # back to the shipped ladder
        stage_b = {name: _top(s, **k) for name, _d, s, k in corpus}
        _set(license_scoring=1)
        stage_c = {name: _top(s, **k) for name, _d, s, k in corpus}

        rebase_changed = licensing_changed = 0
        for name, desc, _s, _k in corpus:
            r1 = _describe(stage_a[name], stage_b[name])
            r2 = _describe(stage_b[name], stage_c[name])
            if r1 not in ("unchanged", "no candidates on one side"):
                rebase_changed += 1
            if r2 not in ("unchanged", "no candidates on one side"):
                licensing_changed += 1
            print(f"\n  {name}")
            print(f"    probes    : {desc}")
            print(f"    A         : {stage_a[name]}")
            print(f"    B         : {stage_b[name]}")
            print(f"    C         : {stage_c[name]}")
            print(f"    re-base   : {r1}")
            print(f"    licensing : {r2}")
        print(f"\n  re-base changed {rebase_changed} of {len(corpus)}; "
              f"licensing changed {licensing_changed} of {len(corpus)}.")
        print("  Every change must have a stated reason. Unexplained = bug.")

        print("\n=== guest-exposure kill criterion (§5.6) " + "=" * 31)
        print("  If flipping none<->windows moves NODE COUNT, the spread is too")
        print("  wide: raise guest_exposure_none, and if it still misbehaves,")
        print("  drop the input for one blended weight. Measured 2026-09-02:")
        print("  0.0 -> 2 offenders, 0.2/0.4 -> 1, 0.6 -> 0. Default is 0.6.")
        offenders = 0
        for name, _d, s, k in corpus:
            kw = {x: y for x, y in k.items() if x != "guest_licensing"}
            none_run = _top(s, guest_licensing="none", **kw)
            win_run = _top(s, guest_licensing="windows", **kw)
            if none_run and win_run and none_run["nodes"] != win_run["nodes"]:
                offenders += 1
                print(f"    ! {name}: {win_run['nodes']} -> {none_run['nodes']} nodes")
        print(f"  {offenders} sizing(s) moved node count. Target: 0.")

        print("\n=== platform_tier divergence (§8.3) " + "=" * 36)
        import platform_tier as pt
        for label, norm in (("as-quoted", False), ("current basis", True)):
            rows, median = pt.divergence_report(normalise_epoch=norm)
            print(f"\n  {label}: basket median EUR {median:,.0f} / tier point")
            print(f"    {'model':<10}{'band':<28}{'tier':>5}{'EUR/pt':>9}"
                  f"{'vs med':>8}{'implied':>9}")
            for r in rows:
                mark = "  <-- REVIEW" if r["flagged"] else ""
                print(f"    {r['model']:<10}{r['band'][:27]:<28}{r['tier']:>5}"
                      f"{r['eur_per_tier_point']:>9,.0f}{r['deviation']:>+7.0%}"
                      f"{r['implied_tier']:>9.0f}{mark}")
        print("\n  A flag means LOOK, not CHANGE: drift (tier was right, price")
        print("  moved) and structural mis-tiering are indistinguishable from")
        print("  one price per band. Do not re-base in the same pass as the")
        print("  scoring change — one variable at a time.")

        print("\n=== w_waste re-scale (§5.4 step 3) " + "=" * 37)
        print("  Dropping magnitude parity grows the cost side, so w_waste must")
        print("  grow with it or over-provisioning stops being penalised.")
        _set(license_scoring=0)
        off = [_cost_side(s, False, **k) for _n, _d, s, k in corpus]
        _set(license_scoring=1)
        on = [_cost_side(s, True, **k) for _n, _d, s, k in corpus]
        pairs = [(a, b) for a, b in zip(off, on) if a and b]
        if pairs:
            ratio = statistics.median(b / a for a, b in pairs)
            print(f"  Median cost-side magnitude grew x{ratio:.2f} across "
                  f"{len(pairs)} sizings.")
            # Scale from the PRE-licensing baseline, not from whatever w_waste
            # currently is. Multiplying the current value re-applies a scaling
            # that has already been applied, and compounds a little more every
            # time someone runs this after a tuning change.
            print(f"  Baseline w_waste (pre-licensing) = {W_WASTE_BASELINE:g}")
            print(f"  -> suggested {W_WASTE_BASELINE * ratio:.0f}"
                  f"   (currently set to {T.w_waste:g})")
            print("  This keeps waste speaking as loudly against cost as it did")
            print("  before. Confirm on the before/after table above: a tighter")
            print("  fit must still beat a looser one at the same node count.")
        else:
            print("  No comparable sizings — cannot compute the factor.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
