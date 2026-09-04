"""Seed the database with appliance model data from models.py."""
import os
# Seeding imports the app factory; don't spin up the background scheduler in this
# one-off process (the web server, a separate process, runs it).
os.environ["ENABLE_SCHEDULER"] = "0"
import sys
from sqlalchemy import text
from app import create_app
from database import db
from orm_models import (
    Model, RamOption, StorageConfig,
    CpuCatalog, NicCatalog, DriveCatalog,
    ModelCpuOption, ModelNicOption, StorageConfigDrive,
    ValidatedNic, Switch, DriveTypeIops, SizingSetting,
)
# Imported so db.create_all() discovers the auth/multitenancy tables.
from auth_models import (
    Tenant, User, Configuration, AppSetting, AdminAuditLog, PiiErasure,
    ROLE_SUPER_ADMIN,
)
# Likewise for the project tables (docs/projects-plan.md §2).
from project_models import (  # noqa: F401
    Project, ProjectTag, ConfigurationTag, ScaleProjectLink, ExportJob,
    ReplicationLink, ensure_scratch_project,
)

# Product-supplied per-drive-type IOPS defaults (admin-editable thereafter).
DRIVE_TYPE_IOPS_DEFAULTS = {"HDD": 150, "SSD": 20000, "NVMe": 75000}

# Cluster-level IOPS sizing defaults (admin-editable thereafter).
SIZING_SETTING_DEFAULTS = {
    "iops_derating_pct": 0.35,      # SCRIBE derating
    "iops_replication_factor": 2,   # RF2
    "iops_read_fraction": 0.70,     # 70/30 read/write
}
# Scoring/sizing/topology tunables also live in the SizingSetting table so they
# show up on the admin Tuning page; defaults come from tunables.DEFAULTS.
from tunables import DEFAULTS as TUNABLE_DEFAULTS
SIZING_SETTING_DEFAULTS.update(TUNABLE_DEFAULTS)
from models import (
    APPLIANCE_MODELS, VALIDATED_NICS, SWITCHING,
    MODEL_COSTS, DEFAULT_MODEL_COST,
)

from xlsx_utils import parse_quantity as _parse_quantity

_cpu_cache = {}
_nic_cache = {}
_drive_cache = {}


def _get_or_create_cpu(desc, cores, threads, ghz):
    if desc in _cpu_cache:
        return _cpu_cache[desc]
    cpu = CpuCatalog.query.filter_by(description=desc).first()
    if not cpu:
        cpu = CpuCatalog(description=desc, cores=cores, threads=threads, ghz=ghz)
        db.session.add(cpu)
        db.session.flush()
    _cpu_cache[desc] = cpu
    return cpu


def _get_or_create_nic(desc, ports, speed):
    if desc in _nic_cache:
        return _nic_cache[desc]
    nic = NicCatalog.query.filter_by(description=desc).first()
    if not nic:
        nic = NicCatalog(description=desc, ports=ports, speed=speed)
        db.session.add(nic)
        db.session.flush()
    _nic_cache[desc] = nic
    return nic


def _get_or_create_drive(drive_type, size_tb):
    key = (drive_type, size_tb)
    if key in _drive_cache:
        return _drive_cache[key]
    drive = DriveCatalog.query.filter_by(drive_type=drive_type, size_tb=size_tb).first()
    if not drive:
        drive = DriveCatalog(drive_type=drive_type, size_tb=size_tb)
        db.session.add(drive)
        db.session.flush()
    _drive_cache[key] = drive
    return drive


def _migrate_schema():
    """Idempotent lightweight migrations for already-seeded databases.
    create_all() only adds missing tables, never new columns, so additive
    column changes are applied here. Safe to run on every boot."""
    stmts = [
        "ALTER TABLE models ADD COLUMN IF NOT EXISTS "
        "validated_only BOOLEAN NOT NULL DEFAULT false",
        # Per-model relative cost weight. Nullable on purpose: freshly added rows
        # start NULL and get back-filled from MODEL_COSTS just below, but only
        # while NULL — so an admin's later per-model cost edit is never clobbered
        # on the next boot.
        "ALTER TABLE models ADD COLUMN IF NOT EXISTS cost_tier DOUBLE PRECISION",
        # Auth columns added after the users table first shipped — additive so
        # existing test/prod databases pick them up on boot.
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "is_verified BOOLEAN NOT NULL DEFAULT true",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_token VARCHAR(64)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "verification_sent_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "failed_login_count INTEGER NOT NULL DEFAULT 0",
        # Per-account lockout was removed (a DoS vector; the per-IP rate limit is
        # the brake). Drop the now-unused column from databases that had it.
        "ALTER TABLE users DROP COLUMN IF EXISTS locked_until",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token VARCHAR(64)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "reset_sent_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "privacy_accepted_at TIMESTAMP WITH TIME ZONE",
        # GDPR marketing-email consent (opt-in timestamp; NULL = not opted in).
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "marketing_consent_at TIMESTAMP WITH TIME ZONE",
        # Authoritative CPU spec columns (feature/add-real-cpu-details). Additive,
        # nullable; back-filled from cpu_specs.py by _backfill_cpu_specs().
        "ALTER TABLE cpu_catalog ADD COLUMN IF NOT EXISTS make VARCHAR(20)",
        "ALTER TABLE cpu_catalog ADD COLUMN IF NOT EXISTS family VARCHAR(40)",
        "ALTER TABLE cpu_catalog ADD COLUMN IF NOT EXISTS generation VARCHAR(60)",
        "ALTER TABLE cpu_catalog ADD COLUMN IF NOT EXISTS model VARCHAR(80)",
        "ALTER TABLE cpu_catalog ADD COLUMN IF NOT EXISTS p_cores INTEGER",
        "ALTER TABLE cpu_catalog ADD COLUMN IF NOT EXISTS e_cores INTEGER",
        "ALTER TABLE cpu_catalog ADD COLUMN IF NOT EXISTS base_ghz DOUBLE PRECISION",
        "ALTER TABLE cpu_catalog ADD COLUMN IF NOT EXISTS all_core_turbo_ghz DOUBLE PRECISION",
        "ALTER TABLE cpu_catalog ADD COLUMN IF NOT EXISTS max_turbo_ghz DOUBLE PRECISION",
        "ALTER TABLE cpu_catalog ADD COLUMN IF NOT EXISTS ecore_base_ghz DOUBLE PRECISION",
        "ALTER TABLE cpu_catalog ADD COLUMN IF NOT EXISTS ecore_turbo_ghz DOUBLE PRECISION",
        "ALTER TABLE cpu_catalog ADD COLUMN IF NOT EXISTS specrate_int DOUBLE PRECISION",
        "ALTER TABLE cpu_catalog ADD COLUMN IF NOT EXISTS passmark_cpu_mark INTEGER",
        "ALTER TABLE cpu_catalog ADD COLUMN IF NOT EXISTS passmark_single INTEGER",
        # Project membership + cached results on existing sizings
        # (docs/projects-plan.md §2.2). All nullable/defaulted so the ALTER is
        # safe on a populated table; _backfill_projects() then files every
        # existing sizing into its owner's scratch project.
        "ALTER TABLE configurations ADD COLUMN IF NOT EXISTS project_id INTEGER "
        "REFERENCES projects(id)",
        "ALTER TABLE configurations ADD COLUMN IF NOT EXISTS "
        "position INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE configurations ADD COLUMN IF NOT EXISTS role VARCHAR(12)",
        "ALTER TABLE configurations ADD COLUMN IF NOT EXISTS notes TEXT",
        "ALTER TABLE configurations ADD COLUMN IF NOT EXISTS result_snapshot JSONB",
        "ALTER TABLE configurations ADD COLUMN IF NOT EXISTS "
        "result_fingerprint VARCHAR(64)",
        "ALTER TABLE configurations ADD COLUMN IF NOT EXISTS "
        "result_computed_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE configurations ADD COLUMN IF NOT EXISTS parser_version VARCHAR(64)",
        "ALTER TABLE configurations ADD COLUMN IF NOT EXISTS source_meta JSONB",
        "CREATE INDEX IF NOT EXISTS ix_configurations_project "
        "ON configurations (project_id)",
        # Cross-sizing replication partners (§8.5).
        # Display name for "Prepared by" on a project (optional).
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name VARCHAR(120)",
        "ALTER TABLE configurations ADD COLUMN IF NOT EXISTS payload_digest VARCHAR(64)",
        "ALTER TABLE configurations ADD COLUMN IF NOT EXISTS "
        "is_dr_target BOOLEAN NOT NULL DEFAULT false",
    ]
    for sql in stmts:
        db.session.execute(text(sql))
    db.session.commit()

    # Back-fill per-model cost weights on existing databases (insert-if-NULL,
    # never overwrite an admin edit). Fresh seeds set cost_tier directly in
    # seed_appliance_models(); this catches rows that predate the column.
    db.session.execute(
        text("UPDATE models SET cost_tier = :c WHERE cost_tier IS NULL "
             "AND name = :n"),
        [{"c": cost, "n": name} for name, cost in MODEL_COSTS.items()],
    )
    # Any model with no entry in MODEL_COSTS still gets a sane default.
    db.session.execute(
        text("UPDATE models SET cost_tier = :c WHERE cost_tier IS NULL"),
        {"c": DEFAULT_MODEL_COST},
    )
    db.session.commit()

    # Back-fill per-drive-type IOPS defaults (insert-if-missing, never overwrite
    # admin edits). create_all() makes the table; this seeds its rows on existing
    # databases too.
    for dtype, iops in DRIVE_TYPE_IOPS_DEFAULTS.items():
        if not DriveTypeIops.query.filter_by(drive_type=dtype).first():
            db.session.add(DriveTypeIops(drive_type=dtype, iops=iops))
    for key, value in SIZING_SETTING_DEFAULTS.items():
        if not SizingSetting.query.filter_by(key=key).first():
            db.session.add(SizingSetting(key=key, value=value))
    db.session.commit()

    _backfill_cpu_specs()
    _backfill_categories()
    _rebase_platform_tiers()
    _retire_confirmed_eos()
    _backfill_projects()
    _seed_license_pricebook()
    _bootstrap_super_admin()
    _purge_on_boot()


# Old free-text categories -> the numbering-based scheme. The model number
# already carries the family AND the generation (14XX -> 16XX is an update of the
# same line), so naming the category after it makes the ladder legible in a way
# "Datacenter 1U All-Flash" never did.
LEGACY_CATEGORIES = {
    "Edge": None,                      # split by model number, see below
    "1U Rack": "5XX Edge",
    "Datacenter 1U": "1XXX Core",
    "Datacenter 1U All-Flash": "3XXX Core",
    "Datacenter 1U All-Flash GPU": "3XXX Core",
    "Datacenter 2U": "5XXX Core",
}


def _category_for(name):
    """Category from the model number. None when the name is not ours."""
    import re
    m = re.match(r"^HE(\d)", name or "")
    if m:
        return {"1": "1XX SFF", "2": "2XX SFF", "5": "5XX Edge"}.get(m.group(1))
    m = re.match(r"^HC(\d)", name or "")
    if m:
        return {"1": "1XXX Core", "3": "3XXX Core", "5": "5XXX Core"}.get(m.group(1))
    if name == "SE100":
        return "1XX SFF"
    return None


def _backfill_categories():
    """Move existing rows onto the numbering-based category scheme.

    Only rewrites rows still holding a KNOWN legacy string. A category an admin
    has already customised is left alone — this is an editable field, and
    clobbering a deliberate edit on every boot would be worse than a stale name.
    """
    changed = 0
    for model in Model.query.all():
        if model.category not in LEGACY_CATEGORIES:
            continue                      # already migrated, or admin-edited
        target = _category_for(model.name)
        if target and target != model.category:
            model.category = target
            changed += 1
    if changed:
        db.session.commit()
        print(f"  categories migrated to the numbering scheme: {changed} models")


# One-time marker so the tier re-base runs exactly once per database. Without
# it, a re-base on every boot would clobber genuine admin edits forever.
TIER_REBASE_KEY = "platform_tier_rebased_2026_09"


# Models confirmed end-of-SALE after the catalog-truth review (§4.2). Kept as an
# explicit list rather than a blanket re-sync from APPLIANCE_MODELS, because
# `status` is admin-editable and a wholesale overwrite would undo deliberate
# local decisions on every boot.
#
# EOS, not EOL: the evidence is absence from a price list, which says a model can
# no longer be SOLD. It says nothing about support ending. The recommender
# excludes both, but they mean different things to a customer.
CONFIRMED_EOS = ("HC3350F", "HC3350DF")


def _retire_confirmed_eos():
    """Move confirmed end-of-sale models off Active, once."""
    changed = 0
    for model in Model.query.filter(Model.name.in_(CONFIRMED_EOS)).all():
        if model.status == "Active":
            model.status = "EOS"
            changed += 1
    if changed:
        db.session.commit()
        print(f"  end-of-sale models retired: {changed}")


def _rebase_platform_tiers():
    """Move existing rows onto the price-proportional tier ladder.

    `cost_tier` changed MEANING on 2026-09-02: it was a hand-set capability
    weight whose absolute units did not matter, and it is now proportional to
    what a configured node costs, because `eur_per_tier_point` bridges it to real
    licence euro. Old values are not merely stale, they are in different units —
    a database left on the old ladder would trade licence euro against a scale
    that means something else.

    So this is a FORCED overwrite, unlike `_backfill_categories()` which respects
    admin edits. It cannot respect them: there is no way to tell an old
    capability value from a deliberately-tuned one, and keeping a capability
    value would be the more wrong of the two outcomes. It runs once, guarded by a
    marker row, and the admin UI remains authoritative afterwards.
    """
    if SizingSetting.query.filter_by(key=TIER_REBASE_KEY).first():
        return
    changed = 0
    for model in Model.query.all():
        target = MODEL_COSTS.get(model.name)
        if target is not None and model.cost_tier != target:
            model.cost_tier = target
            changed += 1
    db.session.add(SizingSetting(key=TIER_REBASE_KEY, value=1))
    db.session.commit()
    if changed:
        print(f"  platform_tier re-based to the price ladder: {changed} models "
              f"(one-time; admin edits are authoritative from here)")


def _seed_license_pricebook():
    """Bootstrap a licence feed from any price list sitting in `_archive/`.

    A convenience for fresh dev and test databases only. **The supported way to
    install a new quarterly price list is `tools/import_pricebook.py`** — it
    shows a diff before it commits anything, which matters when the numbers being
    replaced drive what the sizer recommends.

    Picks the most recently modified `*Price List*.xlsx`, so dropping a newer file
    into `_archive/` is enough to bootstrap with it. Idempotent by file hash.
    Absent file is not an error: a deployment with no feed simply falls back to
    the legacy per-core term.
    """
    import glob
    import os

    pattern = os.path.join(os.path.dirname(__file__), "..", "_archive",
                           "*Price List*.xlsx")
    candidates = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    if not candidates:
        return
    path = candidates[0]
    try:
        from pricebook_import import seed_feed_from_file, PricebookFormatError
        feed, parsed = seed_feed_from_file(
            path, label=os.path.splitext(os.path.basename(path))[0])
    except PricebookFormatError as exc:
        # Loud, but never fatal: a malformed price list must not stop the app
        # booting and sizing.
        print(f"  licence pricebook NOT loaded: {exc}")
        return
    if parsed is not None:
        c = parsed["counts"]
        print(f"  licence pricebook: {c['banded']} bands, {c['flat']} flat, "
              f"{c['unmatched']} unmatched -> feed #{feed.id} ({feed.region})")


def _backfill_projects():
    """File every pre-projects sizing into its owner's scratch project.

    Guarded on "any configuration still has project_id IS NULL", so it is a
    no-op from the second boot onward. Soft-deleted rows are included on
    purpose: they are restorable and still visible to super admins, so leaving
    them unfiled would strand rows the app now expects to have a project
    (docs/projects-plan.md §2.3).
    """
    unfiled = Configuration.query.filter(
        Configuration.project_id.is_(None)).order_by(
            Configuration.owner_id, Configuration.updated_at).all()
    if not unfiled:
        return

    by_owner = {}
    for config in unfiled:
        by_owner.setdefault(config.owner_id, []).append(config)

    filed = 0
    for owner_id, configs in by_owner.items():
        project = ensure_scratch_project(owner_id, configs[0].tenant_id)
        for position, config in enumerate(configs):
            config.project_id = project.id
            config.position = position
            filed += 1
    db.session.commit()
    print(f"[seed] filed {filed} pre-existing sizing(s) into "
          f"{len(by_owner)} scratch project(s)")


def _backfill_cpu_specs():
    """Populate the authoritative CPU spec columns from cpu_specs.py for every
    recognised catalog CPU, and flip its `ghz` (the engine's sizing clock) to the
    all-core turbo. Runs once per row (guarded on `make IS NULL`) so later admin
    edits are never clobbered on reboot. Logs a catalog-vs-spec discrepancy
    report — cores/threads are NOT auto-corrected (that would move sizing beyond
    the clock change), only surfaced for review."""
    from cpu_specs import CPU_SPECS, cpu_model_key, sizing_ghz
    matched, unmatched, discrepancies = 0, [], []
    for cpu in CpuCatalog.query.all():
        if cpu.make is not None:
            continue  # already back-filled; respect later admin edits
        spec = CPU_SPECS.get(cpu_model_key(cpu.description) or "")
        if not spec:
            unmatched.append(cpu.description)
            continue
        if cpu.cores != spec["cores"] or cpu.threads != spec["threads"]:
            discrepancies.append(
                f"{cpu.description!r}: cores/threads {cpu.cores}C/{cpu.threads}T -> "
                f"{spec['cores']}C/{spec['threads']}T (corrected to total incl. "
                f"E-cores; licensing handled by P/E core weights, not undercounting)")
        sizing = sizing_ghz(spec)
        if abs((cpu.ghz or 0) - sizing) >= 0.05:
            discrepancies.append(
                f"{cpu.description!r}: sizing ghz {cpu.ghz} -> {sizing} (all-core turbo)")
        # cores/threads corrected to the authoritative TOTAL; the engine licenses
        # via the w_pcore/w_ecore tunables (E-cores weight 0 by default), so this
        # no longer needs to be undercounted to P-cores.
        for col in ("make", "family", "generation", "model", "cores", "threads",
                    "p_cores", "e_cores", "base_ghz", "all_core_turbo_ghz",
                    "max_turbo_ghz", "ecore_base_ghz", "ecore_turbo_ghz",
                    "specrate_int", "passmark_cpu_mark", "passmark_single"):
            setattr(cpu, col, spec[col])
        cpu.ghz = sizing  # all-core clock LIVE
        matched += 1
    db.session.commit()
    print(f"  CPU specs back-filled: {matched} matched, {len(unmatched)} unmatched")
    if unmatched:
        print(f"    unmatched (kept as-is): {unmatched}")
    if discrepancies:
        print(f"  CPU catalog-vs-authoritative discrepancies ({len(discrepancies)}):")
        for d in discrepancies:
            print(f"    - {d}")


def _bootstrap_super_admin():
    """Create the super admin from env if absent. Seeded out-of-band, so the
    public-domain ban does not apply. Insert-if-missing — never overwrites an
    existing account's password on boot (avoids surprise lockouts)."""
    from werkzeug.security import generate_password_hash
    from email_domains import normalize_email, domain_of
    from auth import PWHASH_METHOD

    email = normalize_email(os.environ.get("SUPER_ADMIN_EMAIL"))
    password = os.environ.get("SUPER_ADMIN_PASSWORD")
    if not email or not password:
        return
    if User.query.filter_by(email=email).first():
        return

    domain = domain_of(email)
    tenant = Tenant.query.filter_by(domain=domain).first()
    if tenant is None:
        tenant = Tenant(domain=domain, is_scale=Tenant.domain_is_scale(domain))
        db.session.add(tenant)
        db.session.flush()
    db.session.add(User(
        email=email,
        password_hash=generate_password_hash(password, method=PWHASH_METHOD),
        tenant_id=tenant.id,
        role=ROLE_SUPER_ADMIN,
    ))
    db.session.commit()
    print(f"  Bootstrapped super admin: {email}")


def _purge_on_boot():
    """Best-effort retention purge at startup (soft-deleted configs / disabled
    users past 90 days). Never fatal to boot."""
    try:
        from auth import purge_expired
        result = purge_expired()
        if result.get("configs_purged") or result.get("users_purged"):
            print(f"  Purged expired: {result}")
    except Exception as e:  # noqa: BLE001
        db.session.rollback()
        print(f"  Purge skipped: {e}")


def seed_all():
    app = create_app()
    with app.app_context():
        db.create_all()
        _migrate_schema()

        if Model.query.first():
            print("Database already seeded. Use --force to re-seed.")
            if "--force" not in sys.argv:
                return
            print("Force re-seeding...")
            db.drop_all()
            db.create_all()

        seed_appliance_models()
        seed_validated_nics()
        seed_switches()
        db.session.commit()
        print("Seed complete.")
        print(f"  Models: {Model.query.count()}")
        print(f"  CPU catalog: {CpuCatalog.query.count()}")
        print(f"  NIC catalog: {NicCatalog.query.count()}")
        print(f"  Drive catalog: {DriveCatalog.query.count()}")
        print(f"  CPU assignments: {ModelCpuOption.query.count()}")
        print(f"  NIC assignments: {ModelNicOption.query.count()}")
        print(f"  Drive assignments: {StorageConfigDrive.query.count()}")
        print(f"  RAM options: {RamOption.query.count()}")
        print(f"  Storage configs: {StorageConfig.query.count()}")
        print(f"  Validated NICs: {ValidatedNic.query.count()}")
        print(f"  Switches: {Switch.query.count()}")


def seed_appliance_models():
    for name, data in APPLIANCE_MODELS.items():
        model = Model(
            name=name,
            status=data["status"],
            category=data["category"],
            form_factor=data.get("form_factor"),
            chassis=data.get("chassis"),
            socket=data.get("socket"),
            psu=data.get("psu"),
            ram_slots=data.get("ram_slots", 0),
            min_nodes=data.get("min_nodes", 1),
            cost_tier=MODEL_COSTS.get(name, DEFAULT_MODEL_COST),
            notes=data.get("notes"),
        )
        db.session.add(model)
        db.session.flush()

        for i, cpu_data in enumerate(data["cpu_options"]):
            qty, base_desc = _parse_quantity(cpu_data["desc"])
            cpu = _get_or_create_cpu(
                base_desc, cpu_data["cores"] // qty,
                cpu_data["threads"] // qty, cpu_data["ghz"],
            )
            db.session.add(ModelCpuOption(
                model_id=model.id, cpu_id=cpu.id,
                quantity=qty, sort_order=i,
            ))

        for ram_gb in data["ram_options_gb"]:
            db.session.add(RamOption(model_id=model.id, size_gb=ram_gb))

        storage = data["storage"]
        sc = StorageConfig(
            model_id=model.id,
            storage_type=storage["type"],
            hdd_count=storage.get("hdd_count"),
            ssd_count=storage.get("ssd_count"),
            nvme_count=storage.get("nvme_count"),
            drives_per_node=storage.get("drives_per_node"),
        )
        if storage["type"] == "cloud" and "options" in storage:
            sc.cloud_tiers = "|".join(storage["options"])
        db.session.add(sc)
        db.session.flush()

        for dtype_key, dtype_label in [
            ("hdd_options_tb", "HDD"),
            ("ssd_options_tb", "SSD"),
            ("nvme_options_tb", "NVMe"),
        ]:
            for size in storage.get(dtype_key, []):
                drive = _get_or_create_drive(dtype_label, size)
                db.session.add(StorageConfigDrive(
                    storage_config_id=sc.id, drive_id=drive.id,
                ))

        for i, nic_data in enumerate(data["nic_options"]):
            qty, base_desc = _parse_quantity(nic_data["desc"])
            nic = _get_or_create_nic(
                base_desc, nic_data["ports"], nic_data["speed"],
            )
            db.session.add(ModelNicOption(
                model_id=model.id, nic_id=nic.id,
                quantity=qty, sort_order=i,
            ))

    print(f"  Seeded {len(APPLIANCE_MODELS)} appliance models")


def seed_validated_nics():
    for nic in VALIDATED_NICS:
        manufacturer = "Intel" if "Intel" in nic["desc"] else "Broadcom"
        chipset = nic["desc"].split("(")[0].strip() if "(" in nic["desc"] else None
        db.session.add(ValidatedNic(
            description=nic["desc"],
            speed=nic["speed"],
            ports=nic["ports"],
            chipset=chipset,
            manufacturer=manufacturer,
        ))
    print(f"  Seeded {len(VALIDATED_NICS)} validated NICs")


def seed_switches():
    for sw in SWITCHING:
        db.session.add(Switch(
            manufacturer=sw["make"],
            model=sw["model"],
            sku=sw.get("sku"),
            rj45_ports=sw.get("rj45"),
            sfp_ports=sw.get("sfp"),
        ))
    print(f"  Seeded {len(SWITCHING)} switches")


if __name__ == "__main__":
    seed_all()
