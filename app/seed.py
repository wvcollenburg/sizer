"""Seed the database with appliance model data from models.py."""
import os
import re
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
from auth_models import Tenant, User, AppSetting, AdminAuditLog, ROLE_SUPER_ADMIN

# Product-supplied per-drive-type IOPS defaults (admin-editable thereafter).
DRIVE_TYPE_IOPS_DEFAULTS = {"HDD": 150, "SSD": 20000, "NVMe": 75000}

# Cluster-level IOPS sizing defaults (admin-editable thereafter).
SIZING_SETTING_DEFAULTS = {
    "iops_derating_pct": 0.35,      # SCRIBE derating
    "iops_replication_factor": 2,   # RF2
    "iops_read_fraction": 0.70,     # 70/30 read/write
}
from models import APPLIANCE_MODELS, VALIDATED_NICS, SWITCHING

_cpu_cache = {}
_nic_cache = {}
_drive_cache = {}

_QTY_RE = re.compile(r'^(\d+)\s*x\s+', re.IGNORECASE)


def _parse_quantity(desc):
    m = _QTY_RE.match(desc)
    if m:
        return int(m.group(1)), desc[m.end():]
    return 1, desc


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
        # Auth columns added after the users table first shipped — additive so
        # existing test/prod databases pick them up on boot.
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "is_verified BOOLEAN NOT NULL DEFAULT true",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS verification_token VARCHAR(64)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "verification_sent_at TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "failed_login_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "locked_until TIMESTAMP WITH TIME ZONE",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS reset_token VARCHAR(64)",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS "
        "reset_sent_at TIMESTAMP WITH TIME ZONE",
    ]
    for sql in stmts:
        db.session.execute(text(sql))
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

    _bootstrap_super_admin()
    _purge_on_boot()


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
