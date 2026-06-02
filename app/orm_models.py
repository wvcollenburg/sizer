from database import db


# ── Catalog tables (shared reference data) ───────────────────────────────────

class CpuCatalog(db.Model):
    __tablename__ = "cpu_catalog"

    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(200), nullable=False, unique=True)
    cores = db.Column(db.Integer, nullable=False)
    threads = db.Column(db.Integer, nullable=False)
    ghz = db.Column(db.Float, nullable=False)

    def to_dict(self):
        return {
            "desc": self.description,
            "cores": self.cores,
            "threads": self.threads,
            "ghz": self.ghz,
        }


class NicCatalog(db.Model):
    __tablename__ = "nic_catalog"

    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(200), nullable=False, unique=True)
    ports = db.Column(db.Integer, nullable=False)
    speed = db.Column(db.String(30), nullable=False)

    def to_dict(self):
        return {
            "desc": self.description,
            "ports": self.ports,
            "speed": self.speed,
        }


class DriveCatalog(db.Model):
    __tablename__ = "drive_catalog"

    id = db.Column(db.Integer, primary_key=True)
    drive_type = db.Column(db.String(10), nullable=False)
    size_tb = db.Column(db.Float, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("drive_type", "size_tb", name="uq_drive_type_size"),
    )


class DriveTypeIops(db.Model):
    """Configurable per-drive-type IOPS used by IOPS sizing. One row per type
    (HDD/SSD/NVMe). Admin-editable; seeded with product defaults."""
    __tablename__ = "drive_type_iops"

    id = db.Column(db.Integer, primary_key=True)
    drive_type = db.Column(db.String(10), nullable=False, unique=True)
    iops = db.Column(db.Integer, nullable=False)

    def to_dict(self):
        return {"drive_type": self.drive_type, "iops": self.iops}


# ── Junction tables (many-to-many) ──────────────────────────────────────────

class ModelCpuOption(db.Model):
    __tablename__ = "model_cpu_options"

    id = db.Column(db.Integer, primary_key=True)
    model_id = db.Column(db.Integer, db.ForeignKey("models.id"), nullable=False)
    cpu_id = db.Column(db.Integer, db.ForeignKey("cpu_catalog.id"), nullable=False)
    quantity = db.Column(db.Integer, default=1)
    sort_order = db.Column(db.Integer, default=0)

    __table_args__ = (
        db.UniqueConstraint("model_id", "cpu_id", "quantity", name="uq_model_cpu_qty"),
    )

    model = db.relationship("Model", back_populates="cpu_links")
    cpu = db.relationship("CpuCatalog")


class ModelNicOption(db.Model):
    __tablename__ = "model_nic_options"

    id = db.Column(db.Integer, primary_key=True)
    model_id = db.Column(db.Integer, db.ForeignKey("models.id"), nullable=False)
    nic_id = db.Column(db.Integer, db.ForeignKey("nic_catalog.id"), nullable=False)
    quantity = db.Column(db.Integer, default=1)
    sort_order = db.Column(db.Integer, default=0)

    __table_args__ = (
        db.UniqueConstraint("model_id", "nic_id", "quantity", name="uq_model_nic_qty"),
    )

    model = db.relationship("Model", back_populates="nic_links")
    nic = db.relationship("NicCatalog")


class StorageConfigDrive(db.Model):
    __tablename__ = "storage_config_drives"

    id = db.Column(db.Integer, primary_key=True)
    storage_config_id = db.Column(db.Integer, db.ForeignKey("storage_configs.id"), nullable=False)
    drive_id = db.Column(db.Integer, db.ForeignKey("drive_catalog.id"), nullable=False)

    __table_args__ = (
        db.UniqueConstraint("storage_config_id", "drive_id", name="uq_storage_drive"),
    )

    storage_config = db.relationship("StorageConfig", back_populates="drive_links")
    drive = db.relationship("DriveCatalog")


# ── Appliance (Certified) tables ────────────────────────────────────────────

class Model(db.Model):
    __tablename__ = "models"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False)
    category = db.Column(db.String(60), nullable=False)
    form_factor = db.Column(db.String(60))
    chassis = db.Column(db.String(120))
    socket = db.Column(db.String(20))
    psu = db.Column(db.String(60))
    ram_slots = db.Column(db.Integer)
    min_nodes = db.Column(db.Integer, default=1)
    # Software-only platform with no certified equivalent: hidden from Certified
    # recommendations, shown (disk-flexed, plain-named) only in Validated mode.
    validated_only = db.Column(db.Boolean, nullable=False, default=False)
    notes = db.Column(db.Text)

    cpu_links = db.relationship(
        "ModelCpuOption", back_populates="model",
        cascade="all, delete-orphan",
        order_by="ModelCpuOption.sort_order",
    )
    ram_options = db.relationship(
        "RamOption", back_populates="model",
        cascade="all, delete-orphan",
    )
    storage_config = db.relationship(
        "StorageConfig", back_populates="model",
        uselist=False, cascade="all, delete-orphan",
    )
    nic_links = db.relationship(
        "ModelNicOption", back_populates="model",
        cascade="all, delete-orphan",
        order_by="ModelNicOption.sort_order",
    )

    def to_dict(self):
        sc = self.storage_config
        storage = {}
        if sc:
            storage = {"type": sc.storage_type}
            if sc.hdd_count:
                storage["hdd_count"] = sc.hdd_count
            if sc.ssd_count:
                storage["ssd_count"] = sc.ssd_count
            if sc.nvme_count:
                storage["nvme_count"] = sc.nvme_count
            if sc.drives_per_node:
                storage["drives_per_node"] = sc.drives_per_node

            for link in sc.drive_links:
                key = f"{link.drive.drive_type.lower()}_options_tb"
                if key not in storage:
                    storage[key] = []
                storage[key].append(link.drive.size_tb)
            for key in storage:
                if key.endswith("_options_tb"):
                    storage[key].sort()

            if sc.storage_type == "cloud" and sc.cloud_tiers:
                storage["options"] = [t.strip() for t in sc.cloud_tiers.split("|")]

        return {
            "status": self.status,
            "category": self.category,
            "form_factor": self.form_factor,
            "chassis": self.chassis,
            "socket": self.socket,
            "psu": self.psu,
            "ram_slots": self.ram_slots,
            "min_nodes": self.min_nodes,
            "validated_only": self.validated_only,
            "notes": self.notes,
            "cpu_options": [
                {
                    "desc": f"{link.quantity} x {link.cpu.description}",
                    "cores": link.cpu.cores * link.quantity,
                    "threads": link.cpu.threads * link.quantity,
                    "ghz": link.cpu.ghz,
                }
                for link in self.cpu_links
            ],
            "ram_options_gb": sorted([r.size_gb for r in self.ram_options]),
            "storage": storage,
            "nic_options": [
                {**link.nic.to_dict(), "desc": f"{link.quantity} x {link.nic.description}"}
                for link in self.nic_links
            ],
        }


class RamOption(db.Model):
    __tablename__ = "ram_options"

    id = db.Column(db.Integer, primary_key=True)
    model_id = db.Column(db.Integer, db.ForeignKey("models.id"), nullable=False)
    size_gb = db.Column(db.Integer, nullable=False)

    model = db.relationship("Model", back_populates="ram_options")


class StorageConfig(db.Model):
    __tablename__ = "storage_configs"

    id = db.Column(db.Integer, primary_key=True)
    model_id = db.Column(db.Integer, db.ForeignKey("models.id"), nullable=False, unique=True)
    storage_type = db.Column(db.String(30), nullable=False)
    hdd_count = db.Column(db.Integer)
    ssd_count = db.Column(db.Integer)
    nvme_count = db.Column(db.Integer)
    drives_per_node = db.Column(db.Integer)
    cloud_tiers = db.Column(db.Text)

    model = db.relationship("Model", back_populates="storage_config")
    drive_links = db.relationship(
        "StorageConfigDrive", back_populates="storage_config",
        cascade="all, delete-orphan",
    )


# ── Validated (Software-Only) tables ────────────────────────────────────────

class ValidatedNic(db.Model):
    __tablename__ = "validated_nics"

    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(200), nullable=False)
    speed = db.Column(db.String(30), nullable=False)
    ports = db.Column(db.Integer, nullable=False)
    chipset = db.Column(db.String(60))
    manufacturer = db.Column(db.String(60))

    def to_dict(self):
        return {
            "id": self.id,
            "desc": self.description,
            "speed": self.speed,
            "ports": self.ports,
            "chipset": self.chipset,
            "manufacturer": self.manufacturer,
        }


class ValidatedPlatform(db.Model):
    __tablename__ = "validated_platforms"

    id = db.Column(db.Integer, primary_key=True)
    manufacturer = db.Column(db.String(60), nullable=False)
    model = db.Column(db.String(120), nullable=False)
    generation = db.Column(db.String(60))
    form_factor = db.Column(db.String(30))
    socket_count = db.Column(db.Integer, default=1)
    max_ram_gb = db.Column(db.Integer)
    ram_slots = db.Column(db.Integer)
    max_drives = db.Column(db.Integer)
    drive_bays_35 = db.Column(db.Integer, default=0)
    drive_bays_25 = db.Column(db.Integer, default=0)
    nvme_slots = db.Column(db.Integer, default=0)
    pcie_slots = db.Column(db.Integer)
    ocp_slots = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default="Active")
    notes = db.Column(db.Text)

    cpu_compatibility = db.relationship("ValidatedCpu", back_populates="platform", cascade="all, delete-orphan")
    ram_compatibility = db.relationship("ValidatedRam", back_populates="platform", cascade="all, delete-orphan")
    drive_compatibility = db.relationship("ValidatedDrive", back_populates="platform", cascade="all, delete-orphan")
    nic_compatibility = db.relationship("ValidatedNicCompat", back_populates="platform", cascade="all, delete-orphan")

    def to_dict(self):
        return {
            "id": self.id,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "generation": self.generation,
            "form_factor": self.form_factor,
            "socket_count": self.socket_count,
            "max_ram_gb": self.max_ram_gb,
            "ram_slots": self.ram_slots,
            "max_drives": self.max_drives,
            "drive_bays_35": self.drive_bays_35,
            "drive_bays_25": self.drive_bays_25,
            "nvme_slots": self.nvme_slots,
            "pcie_slots": self.pcie_slots,
            "ocp_slots": self.ocp_slots,
            "status": self.status,
            "notes": self.notes,
            "cpus": [c.to_dict() for c in self.cpu_compatibility],
            "ram": [r.to_dict() for r in self.ram_compatibility],
            "drives": [d.to_dict() for d in self.drive_compatibility],
            "nics": [n.to_dict() for n in self.nic_compatibility],
        }


class ValidatedCpu(db.Model):
    __tablename__ = "validated_cpus"

    id = db.Column(db.Integer, primary_key=True)
    platform_id = db.Column(db.Integer, db.ForeignKey("validated_platforms.id"), nullable=False)
    manufacturer = db.Column(db.String(30), default="Intel")
    family = db.Column(db.String(60))
    model_name = db.Column(db.String(120), nullable=False)
    cores = db.Column(db.Integer, nullable=False)
    threads = db.Column(db.Integer, nullable=False)
    base_ghz = db.Column(db.Float, nullable=False)
    turbo_ghz = db.Column(db.Float)
    tdp_watts = db.Column(db.Integer)
    socket = db.Column(db.String(30))
    generation = db.Column(db.String(60))

    platform = db.relationship("ValidatedPlatform", back_populates="cpu_compatibility")

    def to_dict(self):
        return {
            "id": self.id,
            "manufacturer": self.manufacturer,
            "family": self.family,
            "model_name": self.model_name,
            "cores": self.cores,
            "threads": self.threads,
            "base_ghz": self.base_ghz,
            "turbo_ghz": self.turbo_ghz,
            "tdp_watts": self.tdp_watts,
            "socket": self.socket,
            "generation": self.generation,
        }


class ValidatedRam(db.Model):
    __tablename__ = "validated_ram"

    id = db.Column(db.Integer, primary_key=True)
    platform_id = db.Column(db.Integer, db.ForeignKey("validated_platforms.id"), nullable=False)
    type = db.Column(db.String(10), nullable=False)
    speed_mhz = db.Column(db.Integer)
    size_gb = db.Column(db.Integer, nullable=False)
    ecc = db.Column(db.Boolean, default=True)
    rdimm = db.Column(db.Boolean, default=True)

    platform = db.relationship("ValidatedPlatform", back_populates="ram_compatibility")

    def to_dict(self):
        return {
            "id": self.id,
            "type": self.type,
            "speed_mhz": self.speed_mhz,
            "size_gb": self.size_gb,
            "ecc": self.ecc,
            "rdimm": self.rdimm,
        }


class ValidatedDrive(db.Model):
    __tablename__ = "validated_drives"

    id = db.Column(db.Integer, primary_key=True)
    platform_id = db.Column(db.Integer, db.ForeignKey("validated_platforms.id"), nullable=False)
    drive_type = db.Column(db.String(10), nullable=False)
    interface = db.Column(db.String(20))
    form_factor = db.Column(db.String(10))
    size_tb = db.Column(db.Float, nullable=False)
    manufacturer = db.Column(db.String(60))
    model_name = db.Column(db.String(120))

    platform = db.relationship("ValidatedPlatform", back_populates="drive_compatibility")

    def to_dict(self):
        return {
            "id": self.id,
            "drive_type": self.drive_type,
            "interface": self.interface,
            "form_factor": self.form_factor,
            "size_tb": self.size_tb,
            "manufacturer": self.manufacturer,
            "model_name": self.model_name,
        }


class ValidatedNicCompat(db.Model):
    __tablename__ = "validated_nic_compat"

    id = db.Column(db.Integer, primary_key=True)
    platform_id = db.Column(db.Integer, db.ForeignKey("validated_platforms.id"), nullable=False)
    nic_id = db.Column(db.Integer, db.ForeignKey("validated_nics.id"), nullable=False)
    slot_type = db.Column(db.String(20))

    platform = db.relationship("ValidatedPlatform", back_populates="nic_compatibility")
    nic = db.relationship("ValidatedNic")

    def to_dict(self):
        return {
            "nic": self.nic.to_dict() if self.nic else None,
            "slot_type": self.slot_type,
        }


# ── Switching ───────────────────────────────────────────────────────────────

class Switch(db.Model):
    __tablename__ = "switches"

    id = db.Column(db.Integer, primary_key=True)
    manufacturer = db.Column(db.String(60), nullable=False)
    model = db.Column(db.String(120), nullable=False)
    sku = db.Column(db.String(60))
    rj45_ports = db.Column(db.String(200))
    sfp_ports = db.Column(db.String(200))

    def to_dict(self):
        return {
            "id": self.id,
            "make": self.manufacturer,
            "model": self.model,
            "sku": self.sku,
            "rj45": self.rj45_ports,
            "sfp": self.sfp_ports,
        }
