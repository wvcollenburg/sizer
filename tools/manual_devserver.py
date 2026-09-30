#!/usr/bin/env python3
"""A throwaway, seeded sizer for tools/manual_shots.py to photograph.

    .venv/bin/python tools/manual_devserver.py DB_PATH [--port 5101] [--pricebook XLSX]

Starts from an empty SQLite file (delete it to start over) and seeds:
  - the appliance catalog (seed.py), as the Postgres seed would
  - the HCL, from the test fixtures (tests/fixtures/hcl), all approved
  - a licence pricebook when --pricebook is given, so licence lines show
  - the four accounts manual_shots.py signs in as (see its docstring)

Local only: debug off, rate limits off, no scheduler, no mail.
"""
import argparse
import json
import os
import sys
import types

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--port", type=int, default=5101)
    ap.add_argument("--pricebook")
    args = ap.parse_args()

    fresh = not os.path.exists(args.db)
    os.environ["DATABASE_URL"] = "sqlite:///" + os.path.abspath(args.db)
    os.environ["ENABLE_SCHEDULER"] = "0"
    os.environ.setdefault("SECRET_KEY", "manual-shots")
    os.environ.pop("SESSION_COOKIE_SECURE", None)
    sys.path.insert(0, os.path.join(ROOT, "app"))
    sys.path.insert(0, os.path.join(ROOT, "tools"))

    from app import create_app
    from database import db
    from extensions import limiter

    app = create_app()
    app.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    if fresh:
        seed(app, db, args.pricebook)
    app.run(host="127.0.0.1", port=args.port, debug=False, threaded=True)


def seed(app, db, pricebook):
    import seed as seedmod
    from orm_models import Model, DriveTypeIops, SizingSetting
    from auth_models import User, ROLE_USER, ROLE_TENANT_ADMIN, ROLE_SUPER_ADMIN
    from bom import hcl_scrape, hcl_sync
    from manual_shots import USER, TADMIN, STAFF, SUPER, PW

    with app.app_context():
        # create_all builds the full schema, so seed.py's Postgres-only
        # migration step is skipped (see the seed-migrate note in tests).
        db.create_all()
        for dtype, iops in seedmod.DRIVE_TYPE_IOPS_DEFAULTS.items():
            db.session.add(DriveTypeIops(drive_type=dtype, iops=iops))
        for key, value in seedmod.SIZING_SETTING_DEFAULTS.items():
            db.session.add(SizingSetting(key=key, value=value))
        seedmod.seed_appliance_models()
        seedmod.seed_validated_nics()
        seedmod.seed_switches()
        db.session.commit()
        print("models:", Model.query.count())

        fixtures = os.path.join(ROOT, "tests", "fixtures", "hcl")
        pages = {p["url"]: p["file"] for p in
                 json.load(open(os.path.join(fixtures, "manifest.json")))["pages"]}

        def fetch(path):
            with open(os.path.join(fixtures, pages[path]), encoding="utf-8") as fh:
                return fh.read()
        snap = hcl_scrape.scrape_all(fetch=fetch, sleep=lambda s: None)
        admin = types.SimpleNamespace(id=None, email="manual-seed", is_super_admin=True)
        hcl_sync.build_run(snap, user=admin, source="import")
        print("hcl:", hcl_sync.bulk(None, "approve", admin, all_pending=True))

        if pricebook:
            import pricebook_import
            print("pricebook:", pricebook_import.seed_feed_from_file(pricebook)[0])
        db.session.commit()

    c = app.test_client()
    accounts = [(USER, ROLE_USER, "Pat Partner"), (TADMIN, ROLE_TENANT_ADMIN, "Alex Admin"),
                (STAFF, ROLE_USER, "Sam Engineer"), (SUPER, ROLE_SUPER_ADMIN, "Sue Super")]
    for email, _, _ in accounts:
        c.post("/api/auth/signup", json={"email": email, "password": PW,
                                         "accept_privacy": True})
    with app.app_context():
        for email, role, name in accounts:
            u = User.query.filter_by(email=email).first()
            u.is_verified, u.full_name, u.role = True, name, role
        db.session.commit()
    print("accounts:", ", ".join(a[0] for a in accounts))


if __name__ == "__main__":
    main()
