"""Admin blueprint – model CRUD + Excel import/export."""
import io
import re
import tempfile
import os

from flask import Blueprint, render_template, jsonify, request, send_file
from sqlalchemy.orm import joinedload
from database import db
from orm_models import (
    Model, RamOption, StorageConfig,
    CpuCatalog, NicCatalog, DriveCatalog, DriveTypeIops, SizingSetting,
    ModelCpuOption, ModelNicOption, StorageConfigDrive,
)

DRIVE_IOPS_TYPES = ["HDD", "SSD", "NVMe"]

_QTY_RE = re.compile(r'^(\d+)\s*x\s+', re.IGNORECASE)


def _parse_quantity(desc):
    m = _QTY_RE.match(desc)
    if m:
        return int(m.group(1)), desc[m.end():]
    return 1, desc

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _model_query():
    return Model.query.options(
        joinedload(Model.cpu_links).joinedload(ModelCpuOption.cpu),
        joinedload(Model.nic_links).joinedload(ModelNicOption.nic),
        joinedload(Model.ram_options),
        joinedload(Model.storage_config)
            .joinedload(StorageConfig.drive_links)
            .joinedload(StorageConfigDrive.drive),
    )


@admin_bp.route("/")
def admin_page():
    return render_template("admin.html")


# ── Catalog endpoints ────────────────────────────────────────────────────────

@admin_bp.route("/api/cpus")
def list_cpus():
    cpus = CpuCatalog.query.order_by(CpuCatalog.cores, CpuCatalog.ghz).all()
    result = []
    for c in cpus:
        used = ModelCpuOption.query.filter_by(cpu_id=c.id).count()
        result.append({"id": c.id, **c.to_dict(), "used_by": used})
    return jsonify(result)


@admin_bp.route("/api/cpus", methods=["POST"])
def create_cpu():
    data = request.json
    if not data or not data.get("desc"):
        return jsonify({"error": "Description is required"}), 400
    if CpuCatalog.query.filter_by(description=data["desc"]).first():
        return jsonify({"error": "CPU already exists"}), 409
    cpu = CpuCatalog(description=data["desc"], cores=int(data.get("cores", 0)),
                     threads=int(data.get("threads", 0)), ghz=float(data.get("ghz", 0)))
    db.session.add(cpu)
    db.session.commit()
    return jsonify({"id": cpu.id, **cpu.to_dict()}), 201


@admin_bp.route("/api/cpus/<int:cpu_id>", methods=["PUT"])
def update_cpu(cpu_id):
    cpu = CpuCatalog.query.get_or_404(cpu_id)
    data = request.json
    if data.get("desc") and data["desc"] != cpu.description:
        if CpuCatalog.query.filter_by(description=data["desc"]).first():
            return jsonify({"error": "CPU already exists"}), 409
    cpu.description = data.get("desc", cpu.description)
    cpu.cores = int(data.get("cores", cpu.cores))
    cpu.threads = int(data.get("threads", cpu.threads))
    cpu.ghz = float(data.get("ghz", cpu.ghz))
    db.session.commit()
    return jsonify({"id": cpu.id, **cpu.to_dict()})


@admin_bp.route("/api/cpus/<int:cpu_id>", methods=["DELETE"])
def delete_cpu(cpu_id):
    cpu = CpuCatalog.query.get_or_404(cpu_id)
    used = ModelCpuOption.query.filter_by(cpu_id=cpu.id).count()
    if used:
        return jsonify({"error": f"CPU is used by {used} model(s). Remove it from those models first."}), 409
    db.session.delete(cpu)
    db.session.commit()
    return jsonify({"message": "CPU deleted"})


@admin_bp.route("/api/nics")
def list_nics():
    nics = NicCatalog.query.order_by(NicCatalog.speed, NicCatalog.description).all()
    result = []
    for n in nics:
        used = ModelNicOption.query.filter_by(nic_id=n.id).count()
        result.append({"id": n.id, **n.to_dict(), "used_by": used})
    return jsonify(result)


@admin_bp.route("/api/nics", methods=["POST"])
def create_nic():
    data = request.json
    if not data or not data.get("desc"):
        return jsonify({"error": "Description is required"}), 400
    if NicCatalog.query.filter_by(description=data["desc"]).first():
        return jsonify({"error": "NIC already exists"}), 409
    nic = NicCatalog(description=data["desc"], ports=int(data.get("ports", 0)),
                     speed=data.get("speed", ""))
    db.session.add(nic)
    db.session.commit()
    return jsonify({"id": nic.id, **nic.to_dict()}), 201


@admin_bp.route("/api/nics/<int:nic_id>", methods=["PUT"])
def update_nic(nic_id):
    nic = NicCatalog.query.get_or_404(nic_id)
    data = request.json
    if data.get("desc") and data["desc"] != nic.description:
        if NicCatalog.query.filter_by(description=data["desc"]).first():
            return jsonify({"error": "NIC already exists"}), 409
    nic.description = data.get("desc", nic.description)
    nic.ports = int(data.get("ports", nic.ports))
    nic.speed = data.get("speed", nic.speed)
    db.session.commit()
    return jsonify({"id": nic.id, **nic.to_dict()})


@admin_bp.route("/api/nics/<int:nic_id>", methods=["DELETE"])
def delete_nic(nic_id):
    nic = NicCatalog.query.get_or_404(nic_id)
    used = ModelNicOption.query.filter_by(nic_id=nic.id).count()
    if used:
        return jsonify({"error": f"NIC is used by {used} model(s). Remove it from those models first."}), 409
    db.session.delete(nic)
    db.session.commit()
    return jsonify({"message": "NIC deleted"})


@admin_bp.route("/api/drives")
def list_drives():
    drives = DriveCatalog.query.order_by(DriveCatalog.drive_type, DriveCatalog.size_tb).all()
    result = []
    for d in drives:
        used = StorageConfigDrive.query.filter_by(drive_id=d.id).count()
        result.append({"id": d.id, "drive_type": d.drive_type, "size_tb": d.size_tb, "used_by": used})
    return jsonify(result)


@admin_bp.route("/api/drives", methods=["POST"])
def create_drive():
    data = request.json
    dtype = data.get("drive_type", "")
    size = float(data.get("size_tb", 0))
    if not dtype or size <= 0:
        return jsonify({"error": "Drive type and size are required"}), 400
    if DriveCatalog.query.filter_by(drive_type=dtype, size_tb=size).first():
        return jsonify({"error": "Drive already exists"}), 409
    drive = DriveCatalog(drive_type=dtype, size_tb=size)
    db.session.add(drive)
    db.session.commit()
    return jsonify({"id": drive.id, "drive_type": drive.drive_type, "size_tb": drive.size_tb}), 201


@admin_bp.route("/api/drives/<int:drive_id>", methods=["PUT"])
def update_drive(drive_id):
    drive = DriveCatalog.query.get_or_404(drive_id)
    data = request.json
    new_type = data.get("drive_type", drive.drive_type)
    new_size = float(data.get("size_tb", drive.size_tb))
    if (new_type != drive.drive_type or new_size != drive.size_tb):
        if DriveCatalog.query.filter_by(drive_type=new_type, size_tb=new_size).first():
            return jsonify({"error": "Drive already exists"}), 409
    drive.drive_type = new_type
    drive.size_tb = new_size
    db.session.commit()
    return jsonify({"id": drive.id, "drive_type": drive.drive_type, "size_tb": drive.size_tb})


@admin_bp.route("/api/drives/<int:drive_id>", methods=["DELETE"])
def delete_drive(drive_id):
    drive = DriveCatalog.query.get_or_404(drive_id)
    used = StorageConfigDrive.query.filter_by(drive_id=drive.id).count()
    if used:
        return jsonify({"error": f"Drive is used by {used} storage config(s). Remove it from those models first."}), 409
    db.session.delete(drive)
    db.session.commit()
    return jsonify({"message": "Drive deleted"})


# ── Per-drive-type IOPS (configurable) ───────────────────────────────────────

@admin_bp.route("/api/drive-iops")
def list_drive_iops():
    rows = {r.drive_type: r for r in DriveTypeIops.query.all()}
    # Return in a stable, known order regardless of insertion order.
    return jsonify([rows[t].to_dict() for t in DRIVE_IOPS_TYPES if t in rows])


@admin_bp.route("/api/drive-iops", methods=["PUT"])
def update_drive_iops():
    data = request.json or {}
    # Accept either {"HDD": n, ...} or [{"drive_type": "HDD", "iops": n}, ...].
    if isinstance(data, list):
        data = {d.get("drive_type"): d.get("iops") for d in data}

    updates = {}
    for dtype in DRIVE_IOPS_TYPES:
        if dtype not in data:
            continue
        try:
            val = int(data[dtype])
        except (TypeError, ValueError):
            return jsonify({"error": f"{dtype} IOPS must be a whole number"}), 400
        if val < 0:
            return jsonify({"error": f"{dtype} IOPS cannot be negative"}), 400
        updates[dtype] = val

    if not updates:
        return jsonify({"error": "No valid IOPS values provided"}), 400

    for dtype, val in updates.items():
        row = DriveTypeIops.query.filter_by(drive_type=dtype).first()
        if row:
            row.iops = val
        else:
            db.session.add(DriveTypeIops(drive_type=dtype, iops=val))
    db.session.commit()
    return jsonify({"message": "Drive IOPS updated",
                    "drive_iops": [r.to_dict() for r in DriveTypeIops.query.all()]})


# ── Cluster-level IOPS sizing config (configurable) ──────────────────────────

def _sizing_config_dict():
    return {s.key: s.value for s in SizingSetting.query.all()}


@admin_bp.route("/api/sizing-config")
def get_sizing_config():
    return jsonify(_sizing_config_dict())


@admin_bp.route("/api/sizing-config", methods=["PUT"])
def update_sizing_config():
    data = request.json or {}
    # (key, parser/validator) — each returns the stored float or raises ValueError.
    def frac(v):
        f = float(v)
        if not 0 <= f <= 1:
            raise ValueError
        return f

    def derate(v):
        f = float(v)
        if not 0 <= f < 0.9:
            raise ValueError
        return f

    def rf(v):
        f = int(v)
        if f < 1:
            raise ValueError
        return float(f)

    validators = {
        "iops_derating_pct": (derate, "Derating must be between 0 and 0.9"),
        "iops_replication_factor": (rf, "Replication factor must be a whole number ≥ 1"),
        "iops_read_fraction": (frac, "Read fraction must be between 0 and 1"),
    }

    updates = {}
    for key, (parse, msg) in validators.items():
        if key not in data:
            continue
        try:
            updates[key] = parse(data[key])
        except (TypeError, ValueError):
            return jsonify({"error": msg}), 400

    if not updates:
        return jsonify({"error": "No valid sizing settings provided"}), 400

    for key, value in updates.items():
        row = SizingSetting.query.filter_by(key=key).first()
        if row:
            row.value = value
        else:
            db.session.add(SizingSetting(key=key, value=value))
    db.session.commit()
    return jsonify({"message": "Sizing config updated", "sizing_config": _sizing_config_dict()})


# ── List all models ──────────────────────────────────────────────────────────

@admin_bp.route("/api/models")
def list_models():
    models = _model_query().order_by(Model.category, Model.name).all()
    result = []
    for m in models:
        d = m.to_dict()
        d["id"] = m.id
        d["name"] = m.name
        result.append(d)
    return jsonify(result)


# ── Get single model ─────────────────────────────────────────────────────────

@admin_bp.route("/api/models/<int:model_id>")
def get_model(model_id):
    m = _model_query().get_or_404(model_id)
    d = m.to_dict()
    d["id"] = m.id
    d["name"] = m.name
    d["cpu_options"] = [
        {**link.cpu.to_dict(), "qty": link.quantity, "desc": link.cpu.description}
        for link in m.cpu_links
    ]
    d["nic_options"] = [
        {**link.nic.to_dict(), "qty": link.quantity, "desc": link.nic.description}
        for link in m.nic_links
    ]
    return jsonify(d)


# ── Create model ─────────────────────────────────────────────────────────────

@admin_bp.route("/api/models", methods=["POST"])
def create_model():
    data = request.json
    if not data or not data.get("name"):
        return jsonify({"error": "Model name is required"}), 400

    if Model.query.filter_by(name=data["name"]).first():
        return jsonify({"error": f"Model '{data['name']}' already exists"}), 409

    model = _build_model(data)
    db.session.add(model)
    db.session.commit()
    return jsonify({"id": model.id, "message": f"Model '{model.name}' created"}), 201


# ── Update model ─────────────────────────────────────────────────────────────

@admin_bp.route("/api/models/<int:model_id>", methods=["PUT"])
def update_model(model_id):
    model = Model.query.get_or_404(model_id)
    data = request.json

    if data.get("name") and data["name"] != model.name:
        if Model.query.filter_by(name=data["name"]).first():
            return jsonify({"error": f"Model '{data['name']}' already exists"}), 409

    model.name = data.get("name", model.name)
    model.status = data.get("status", model.status)
    model.category = data.get("category", model.category)
    model.form_factor = data.get("form_factor", model.form_factor)
    model.chassis = data.get("chassis", model.chassis)
    model.socket = data.get("socket", model.socket)
    model.psu = data.get("psu", model.psu)
    model.ram_slots = data.get("ram_slots", model.ram_slots)
    model.min_nodes = data.get("min_nodes", model.min_nodes)
    if "validated_only" in data:
        model.validated_only = bool(data["validated_only"])
    model.notes = data.get("notes", model.notes)

    if "cpu_options" in data:
        ModelCpuOption.query.filter_by(model_id=model.id).delete()
        for i, cpu_data in enumerate(data["cpu_options"]):
            qty, base_desc = _parse_quantity(cpu_data["desc"])
            qty = cpu_data.get("qty", qty)
            cpu = _get_or_create_cpu(base_desc, cpu_data["cores"],
                                     cpu_data["threads"], cpu_data["ghz"])
            db.session.add(ModelCpuOption(
                model_id=model.id, cpu_id=cpu.id,
                quantity=qty, sort_order=i,
            ))

    if "ram_options_gb" in data:
        RamOption.query.filter_by(model_id=model.id).delete()
        for size in data["ram_options_gb"]:
            db.session.add(RamOption(model_id=model.id, size_gb=size))

    if "storage" in data:
        if model.storage_config:
            StorageConfigDrive.query.filter_by(
                storage_config_id=model.storage_config.id
            ).delete()
            db.session.delete(model.storage_config)
            db.session.flush()
        _add_storage(model, data["storage"])

    if "nic_options" in data:
        ModelNicOption.query.filter_by(model_id=model.id).delete()
        for i, nic_data in enumerate(data["nic_options"]):
            qty, base_desc = _parse_quantity(nic_data["desc"])
            qty = nic_data.get("qty", qty)
            nic = _get_or_create_nic(base_desc, nic_data["ports"],
                                     nic_data["speed"])
            db.session.add(ModelNicOption(
                model_id=model.id, nic_id=nic.id,
                quantity=qty, sort_order=i,
            ))

    db.session.commit()
    return jsonify({"message": f"Model '{model.name}' updated"})


# ── Delete model ─────────────────────────────────────────────────────────────

@admin_bp.route("/api/models/<int:model_id>", methods=["DELETE"])
def delete_model(model_id):
    model = Model.query.get_or_404(model_id)
    name = model.name
    db.session.delete(model)
    db.session.commit()
    return jsonify({"message": f"Model '{name}' deleted"})


# ── Export to Excel ──────────────────────────────────────────────────────────

@admin_bp.route("/api/export-models")
def export_models():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF", size=10)
    header_fill = PatternFill("solid", fgColor="003A70")
    header_align = Alignment(horizontal="center", wrap_text=True)

    def style_headers(ws):
        for c in ws[1]:
            c.font = header_font
            c.fill = header_fill
            c.alignment = header_align

    ws = wb.active
    ws.title = "Models"
    ws.append(["Name", "Status", "Category", "Form Factor", "Chassis",
               "Socket", "PSU", "RAM Slots", "Min Nodes", "Validated Only", "Notes"])
    style_headers(ws)

    ws_cpu = wb.create_sheet("CPU Options")
    ws_cpu.append(["Model Name", "Qty", "Description", "Cores", "Threads", "GHz"])
    style_headers(ws_cpu)

    ws_ram = wb.create_sheet("RAM Options")
    ws_ram.append(["Model Name", "Size GB"])
    style_headers(ws_ram)

    ws_stor = wb.create_sheet("Storage")
    ws_stor.append(["Model Name", "Type", "HDD Count", "SSD Count",
                    "NVMe Count", "Drives Per Node"])
    style_headers(ws_stor)

    ws_drv = wb.create_sheet("Drive Options")
    ws_drv.append(["Model Name", "Drive Type", "Size TB"])
    style_headers(ws_drv)

    ws_nic = wb.create_sheet("NIC Options")
    ws_nic.append(["Model Name", "Qty", "Description", "Ports", "Speed"])
    style_headers(ws_nic)

    models = _model_query().order_by(Model.category, Model.name).all()
    for m in models:
        ws.append([m.name, m.status, m.category, m.form_factor, m.chassis,
                   m.socket, m.psu, m.ram_slots, m.min_nodes,
                   "Yes" if m.validated_only else "No", m.notes])

        for link in sorted(m.cpu_links, key=lambda l: l.sort_order):
            ws_cpu.append([m.name, link.quantity, link.cpu.description,
                           link.cpu.cores, link.cpu.threads, link.cpu.ghz])

        for ram in sorted(m.ram_options, key=lambda r: r.size_gb):
            ws_ram.append([m.name, ram.size_gb])

        sc = m.storage_config
        if sc:
            ws_stor.append([m.name, sc.storage_type, sc.hdd_count, sc.ssd_count,
                            sc.nvme_count, sc.drives_per_node])
            for link in sc.drive_links:
                ws_drv.append([m.name, link.drive.drive_type, link.drive.size_tb])

        for link in sorted(m.nic_links, key=lambda l: l.sort_order):
            ws_nic.append([m.name, link.quantity, link.nic.description,
                           link.nic.ports, link.nic.speed])

    for sheet in wb.worksheets:
        for col in sheet.columns:
            max_len = max(len(str(c.value or "")) for c in col)
            sheet.column_dimensions[col[0].column_letter].width = min(max_len + 4, 40)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="SC_Models_Export.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation")


# ── Import from Excel ────────────────────────────────────────────────────────

@admin_bp.route("/api/import-models", methods=["POST"])
def import_models():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]
    if not f.filename or not f.filename.endswith(".xlsx"):
        return jsonify({"error": "File must be .xlsx"}), 400

    mode = request.form.get("mode", "add")

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    try:
        f.save(tmp.name)
        tmp.close()
        result = _import_from_excel(tmp.name, mode)
        return jsonify(result)
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Import failed: {str(e)}"}), 400
    finally:
        os.unlink(tmp.name)


# ── Catalog import (CPUs / NICs / Drives in one file) ───────────────────────

@admin_bp.route("/api/import-catalog", methods=["POST"])
def import_catalog():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]
    if not f.filename or not f.filename.endswith(".xlsx"):
        return jsonify({"error": "File must be .xlsx"}), 400

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    try:
        f.save(tmp.name)
        tmp.close()
        result = _import_catalog_from_excel(tmp.name)
        return jsonify(result)
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"Import failed: {str(e)}"}), 400
    finally:
        os.unlink(tmp.name)


def _import_catalog_from_excel(file_path):
    from openpyxl import load_workbook
    wb = load_workbook(file_path, read_only=True, data_only=True)

    cpu_rows = _sheet_rows(wb, "CPUs")
    nic_rows = _sheet_rows(wb, "NICs")
    drive_rows = _sheet_rows(wb, "Drives")
    model_rows = _sheet_rows(wb, "Models")
    model_cpu_rows = _sheet_rows(wb, "Model CPU Options")
    model_ram_rows = _sheet_rows(wb, "Model RAM Options")
    model_stor_rows = _sheet_rows(wb, "Model Storage")
    model_drv_rows = _sheet_rows(wb, "Model Drive Options")
    model_nic_rows = _sheet_rows(wb, "Model NIC Options")
    wb.close()

    has_catalog = cpu_rows or nic_rows or drive_rows
    has_models = bool(model_rows)

    if not has_catalog and not has_models:
        return {"error": "No recognized sheets found. Expected: CPUs, NICs, Drives, Models"}

    parts = []

    cpus_added = cpus_skipped = 0
    for r in cpu_rows:
        desc = str(r.get("Description", "")).strip()
        if not desc:
            continue
        if CpuCatalog.query.filter_by(description=desc).first():
            cpus_skipped += 1
            continue
        db.session.add(CpuCatalog(
            description=desc,
            cores=int(r.get("Cores", 0) or 0),
            threads=int(r.get("Threads", 0) or 0),
            ghz=float(r.get("GHz", 0) or 0),
        ))
        cpus_added += 1
    if cpu_rows:
        parts.append(f"CPUs: {cpus_added} added, {cpus_skipped} skipped")

    nics_added = nics_skipped = 0
    for r in nic_rows:
        desc = str(r.get("Description", "")).strip()
        if not desc:
            continue
        if NicCatalog.query.filter_by(description=desc).first():
            nics_skipped += 1
            continue
        db.session.add(NicCatalog(
            description=desc,
            ports=int(r.get("Ports", 0) or 0),
            speed=str(r.get("Speed", "")).strip(),
        ))
        nics_added += 1
    if nic_rows:
        parts.append(f"NICs: {nics_added} added, {nics_skipped} skipped")

    drives_added = drives_skipped = 0
    for r in drive_rows:
        dtype = str(r.get("Type", "")).strip()
        size = float(r.get("Size TB", 0) or 0)
        if not dtype or size <= 0:
            continue
        if DriveCatalog.query.filter_by(drive_type=dtype, size_tb=size).first():
            drives_skipped += 1
            continue
        db.session.add(DriveCatalog(drive_type=dtype, size_tb=size))
        drives_added += 1
    if drive_rows:
        parts.append(f"Drives: {drives_added} added, {drives_skipped} skipped")

    db.session.flush()

    models_created = models_skipped = 0
    if model_rows:
        cpus_by_model = {}
        for r in model_cpu_rows:
            name = str(r.get("Model Name", "")).strip()
            if name:
                cpus_by_model.setdefault(name, []).append({
                    "desc": str(r.get("Description", "")),
                    "qty": int(r.get("Qty", 1) or 1),
                    "cores": int(r.get("Cores", 0) or 0),
                    "threads": int(r.get("Threads", 0) or 0),
                    "ghz": float(r.get("GHz", 0) or 0),
                })

        ram_by_model = {}
        for r in model_ram_rows:
            name = str(r.get("Model Name", "")).strip()
            if name:
                ram_by_model.setdefault(name, []).append(int(r.get("Size GB", 0) or 0))

        stor_by_model = {}
        for r in model_stor_rows:
            name = str(r.get("Model Name", "")).strip()
            if name:
                stor_by_model[name] = {
                    "type": str(r.get("Type", "nvme_only")).strip(),
                    "hdd_count": int(r.get("HDD Count", 0) or 0) or None,
                    "ssd_count": int(r.get("SSD Count", 0) or 0) or None,
                    "nvme_count": int(r.get("NVMe Count", 0) or 0) or None,
                    "drives_per_node": int(r.get("Drives Per Node", 0) or 0) or None,
                }

        mdrives_by_model = {}
        for r in model_drv_rows:
            name = str(r.get("Model Name", "")).strip()
            if name:
                mdrives_by_model.setdefault(name, []).append({
                    "type": str(r.get("Drive Type", "")).strip(),
                    "size_tb": float(r.get("Size TB", 0) or 0),
                })

        nics_by_model = {}
        for r in model_nic_rows:
            name = str(r.get("Model Name", "")).strip()
            if name:
                nics_by_model.setdefault(name, []).append({
                    "desc": str(r.get("Description", "")),
                    "qty": int(r.get("Qty", 1) or 1),
                    "ports": int(r.get("Ports", 0) or 0),
                    "speed": str(r.get("Speed", "")),
                })

        for r in model_rows:
            name = str(r.get("Name", "")).strip()
            if not name:
                continue
            if Model.query.filter_by(name=name).first():
                models_skipped += 1
                continue

            storage_data = stor_by_model.get(name, {})
            for drv in mdrives_by_model.get(name, []):
                key = f"{drv['type'].lower()}_options_tb"
                storage_data.setdefault(key, []).append(drv["size_tb"])

            model_data = {
                "name": name,
                "status": str(r.get("Status", "Active")).strip(),
                "category": str(r.get("Category", "")).strip(),
                "form_factor": str(r.get("Form Factor", "") or "").strip() or None,
                "chassis": str(r.get("Chassis", "") or "").strip() or None,
                "socket": str(r.get("Socket", "single") or "single").strip(),
                "psu": str(r.get("PSU", "") or "").strip() or None,
                "ram_slots": int(r.get("RAM Slots", 0) or 0),
                "min_nodes": int(r.get("Min Nodes", 1) or 1),
                "validated_only": str(r.get("Validated Only", "")).strip().lower()
                                  in ("yes", "true", "1"),
                "notes": str(r.get("Notes", "") or "").strip() or None,
                "cpu_options": cpus_by_model.get(name, []),
                "ram_options_gb": ram_by_model.get(name, []),
                "storage": storage_data,
                "nic_options": nics_by_model.get(name, []),
            }
            _build_model(model_data)
            models_created += 1

        parts.append(f"Models: {models_created} created, {models_skipped} skipped (existing)")

    db.session.commit()

    return {"message": ". ".join(parts)}


# ── Catalog template download ──────────────────────────────────────────────

@admin_bp.route("/api/catalog-template")
def catalog_template():
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF", size=10)
    header_fill = PatternFill("solid", fgColor="003A70")
    header_align = Alignment(horizontal="center", wrap_text=True)
    example_font = Font(italic=True, color="888888")

    def style_headers(ws):
        for c in ws[1]:
            c.font = header_font
            c.fill = header_fill
            c.alignment = header_align

    def example_rows(ws, rows):
        for row in rows:
            ws.append(row)
        for r in range(2, 2 + len(rows)):
            for c in ws[r]:
                c.font = example_font

    ex = "HC9999F"

    ws_cpu = wb.active
    ws_cpu.title = "CPUs"
    ws_cpu.append(["Description", "Cores", "Threads", "GHz"])
    style_headers(ws_cpu)
    example_rows(ws_cpu, [
        ["Xeon Gold 6526Y 16C/32T 3.5GHz", 16, 32, 3.5],
        ["Silver 4516Y+ 24C/48T 2.9GHz", 24, 48, 2.9],
    ])

    ws_nic = wb.create_sheet("NICs")
    ws_nic.append(["Description", "Ports", "Speed"])
    style_headers(ws_nic)
    example_rows(ws_nic, [
        ["10GbE SFP+ 4-port Network Card (Intel X710)", 4, "10GbE"],
        ["25GbE SFP28 2-port OCP Network Card (Intel E810)", 2, "25GbE"],
    ])

    ws_drv = wb.create_sheet("Drives")
    ws_drv.append(["Type", "Size TB"])
    style_headers(ws_drv)
    example_rows(ws_drv, [
        ["NVMe", 3.84],
        ["SSD", 1.92],
    ])

    ws_mod = wb.create_sheet("Models")
    ws_mod.append(["Name", "Status", "Category", "Form Factor", "Chassis",
                   "Socket", "PSU", "RAM Slots", "Min Nodes", "Notes"])
    style_headers(ws_mod)
    example_rows(ws_mod, [
        [ex, "Active", "1U All-Flash", "1U Rack", "Dell PowerEdge R660",
         "single", "2x 800W", 16, 3, None],
    ])

    ws_mcpu = wb.create_sheet("Model CPU Options")
    ws_mcpu.append(["Model Name", "Qty", "Description", "Cores", "Threads", "GHz"])
    style_headers(ws_mcpu)
    example_rows(ws_mcpu, [
        [ex, 1, "Xeon Gold 6526Y 16C/32T 3.5GHz", 16, 32, 3.5],
        [ex, 1, "Silver 4516Y+ 24C/48T 2.9GHz", 24, 48, 2.9],
    ])

    ws_mram = wb.create_sheet("Model RAM Options")
    ws_mram.append(["Model Name", "Size GB"])
    style_headers(ws_mram)
    example_rows(ws_mram, [
        [ex, 64], [ex, 128], [ex, 256],
    ])

    ws_mstor = wb.create_sheet("Model Storage")
    ws_mstor.append(["Model Name", "Type", "HDD Count", "SSD Count",
                     "NVMe Count", "Drives Per Node"])
    style_headers(ws_mstor)
    example_rows(ws_mstor, [
        [ex, "nvme_only", None, None, None, 10],
    ])

    ws_mdrv = wb.create_sheet("Model Drive Options")
    ws_mdrv.append(["Model Name", "Drive Type", "Size TB"])
    style_headers(ws_mdrv)
    example_rows(ws_mdrv, [
        [ex, "NVMe", 3.84],
        [ex, "NVMe", 7.68],
    ])

    ws_mnic = wb.create_sheet("Model NIC Options")
    ws_mnic.append(["Model Name", "Qty", "Description", "Ports", "Speed"])
    style_headers(ws_mnic)
    example_rows(ws_mnic, [
        [ex, 1, "10GbE SFP+ 4-port Network Card (Intel X710)", 4, "10GbE"],
        [ex, 1, "25GbE SFP28 2-port OCP Network Card (Intel E810)", 2, "25GbE"],
    ])

    for sheet in wb.worksheets:
        for col in sheet.columns:
            max_len = max(len(str(c.value or "")) for c in col)
            sheet.column_dimensions[col[0].column_letter].width = min(max_len + 4, 50)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="SC_Import_Template.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_or_create_cpu(desc, cores, threads, ghz):
    cpu = CpuCatalog.query.filter_by(description=desc).first()
    if not cpu:
        cpu = CpuCatalog(description=desc, cores=cores, threads=threads, ghz=ghz)
        db.session.add(cpu)
        db.session.flush()
    return cpu


def _get_or_create_nic(desc, ports, speed):
    nic = NicCatalog.query.filter_by(description=desc).first()
    if not nic:
        nic = NicCatalog(description=desc, ports=ports, speed=speed)
        db.session.add(nic)
        db.session.flush()
    return nic


def _get_or_create_drive(drive_type, size_tb):
    drive = DriveCatalog.query.filter_by(drive_type=drive_type, size_tb=size_tb).first()
    if not drive:
        drive = DriveCatalog(drive_type=drive_type, size_tb=size_tb)
        db.session.add(drive)
        db.session.flush()
    return drive


def _build_model(data):
    model = Model(
        name=data["name"],
        status=data.get("status", "Active"),
        category=data.get("category", ""),
        form_factor=data.get("form_factor"),
        chassis=data.get("chassis"),
        socket=data.get("socket", "single"),
        psu=data.get("psu"),
        ram_slots=data.get("ram_slots", 0),
        min_nodes=data.get("min_nodes", 1),
        validated_only=bool(data.get("validated_only", False)),
        notes=data.get("notes"),
    )
    db.session.add(model)
    db.session.flush()

    for i, cpu_data in enumerate(data.get("cpu_options", [])):
        qty, base_desc = _parse_quantity(cpu_data["desc"])
        qty = cpu_data.get("qty", qty)
        cpu = _get_or_create_cpu(base_desc, cpu_data["cores"],
                                 cpu_data["threads"], cpu_data["ghz"])
        db.session.add(ModelCpuOption(
            model_id=model.id, cpu_id=cpu.id,
            quantity=qty, sort_order=i,
        ))

    for size in data.get("ram_options_gb", []):
        db.session.add(RamOption(model_id=model.id, size_gb=size))

    if "storage" in data:
        _add_storage(model, data["storage"])

    for i, nic_data in enumerate(data.get("nic_options", [])):
        qty, base_desc = _parse_quantity(nic_data["desc"])
        qty = nic_data.get("qty", qty)
        nic = _get_or_create_nic(base_desc, nic_data["ports"],
                                 nic_data["speed"])
        db.session.add(ModelNicOption(
            model_id=model.id, nic_id=nic.id,
            quantity=qty, sort_order=i,
        ))

    return model


def _add_storage(model, storage):
    sc = StorageConfig(
        model_id=model.id,
        storage_type=storage.get("type", "nvme_only"),
        hdd_count=storage.get("hdd_count"),
        ssd_count=storage.get("ssd_count"),
        nvme_count=storage.get("nvme_count"),
        drives_per_node=storage.get("drives_per_node"),
    )
    if storage.get("type") == "cloud" and "options" in storage:
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


def _sheet_rows(wb, name):
    if name not in wb.sheetnames:
        return []
    ws = wb[name]
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return []
    headers = [str(h).strip() if h else f"col_{i}" for i, h in enumerate(rows[0])]
    return [dict(zip(headers, row)) for row in rows[1:] if any(v is not None for v in row)]


def _import_from_excel(file_path, mode):
    from openpyxl import load_workbook
    wb = load_workbook(file_path, read_only=True, data_only=True)

    model_rows = _sheet_rows(wb, "Models")
    cpu_rows = _sheet_rows(wb, "CPU Options")
    ram_rows = _sheet_rows(wb, "RAM Options")
    stor_rows = _sheet_rows(wb, "Storage")
    drv_rows = _sheet_rows(wb, "Drive Options")
    nic_rows = _sheet_rows(wb, "NIC Options")
    wb.close()

    if not model_rows:
        return {"error": "No models found in the 'Models' sheet"}

    cpus_by_model = {}
    for r in cpu_rows:
        name = str(r.get("Model Name", "")).strip()
        if name:
            cpus_by_model.setdefault(name, []).append({
                "desc": str(r.get("Description", "")),
                "qty": int(r.get("Qty", 1) or 1),
                "cores": int(r.get("Cores", 0) or 0),
                "threads": int(r.get("Threads", 0) or 0),
                "ghz": float(r.get("GHz", 0) or 0),
            })

    ram_by_model = {}
    for r in ram_rows:
        name = str(r.get("Model Name", "")).strip()
        if name:
            ram_by_model.setdefault(name, []).append(int(r.get("Size GB", 0) or 0))

    stor_by_model = {}
    for r in stor_rows:
        name = str(r.get("Model Name", "")).strip()
        if name:
            stor_by_model[name] = {
                "type": str(r.get("Type", "nvme_only")).strip(),
                "hdd_count": int(r.get("HDD Count", 0) or 0) or None,
                "ssd_count": int(r.get("SSD Count", 0) or 0) or None,
                "nvme_count": int(r.get("NVMe Count", 0) or 0) or None,
                "drives_per_node": int(r.get("Drives Per Node", 0) or 0) or None,
            }

    drives_by_model = {}
    for r in drv_rows:
        name = str(r.get("Model Name", "")).strip()
        if name:
            drives_by_model.setdefault(name, []).append({
                "type": str(r.get("Drive Type", "")).strip(),
                "size_tb": float(r.get("Size TB", 0) or 0),
            })

    nics_by_model = {}
    for r in nic_rows:
        name = str(r.get("Model Name", "")).strip()
        if name:
            nics_by_model.setdefault(name, []).append({
                "desc": str(r.get("Description", "")),
                "qty": int(r.get("Qty", 1) or 1),
                "ports": int(r.get("Ports", 0) or 0),
                "speed": str(r.get("Speed", "")),
            })

    created = 0
    updated = 0
    skipped = []

    for r in model_rows:
        name = str(r.get("Name", "")).strip()
        if not name:
            continue

        existing = Model.query.filter_by(name=name).first()

        if existing and mode == "add":
            skipped.append(name)
            continue

        storage_data = stor_by_model.get(name, {})
        drives = drives_by_model.get(name, [])
        for drv in drives:
            key = f"{drv['type'].lower()}_options_tb"
            storage_data.setdefault(key, []).append(drv["size_tb"])

        model_data = {
            "name": name,
            "status": str(r.get("Status", "Active")).strip(),
            "category": str(r.get("Category", "")).strip(),
            "form_factor": str(r.get("Form Factor", "") or "").strip() or None,
            "chassis": str(r.get("Chassis", "") or "").strip() or None,
            "socket": str(r.get("Socket", "single") or "single").strip(),
            "psu": str(r.get("PSU", "") or "").strip() or None,
            "ram_slots": int(r.get("RAM Slots", 0) or 0),
            "min_nodes": int(r.get("Min Nodes", 1) or 1),
            "notes": str(r.get("Notes", "") or "").strip() or None,
            "cpu_options": cpus_by_model.get(name, []),
            "ram_options_gb": ram_by_model.get(name, []),
            "storage": storage_data,
            "nic_options": nics_by_model.get(name, []),
        }

        if existing and mode == "replace":
            db.session.delete(existing)
            db.session.flush()
            _build_model(model_data)
            updated += 1
        else:
            _build_model(model_data)
            created += 1

    db.session.commit()

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "total_in_file": len(model_rows),
    }
