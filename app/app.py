import os
import secrets
import tempfile

from flask import Flask, render_template, jsonify, request, send_file
from database import db, init_db
from auth import register_auth, start_scheduler
from sqlalchemy.orm import joinedload
from orm_models import (
    Model, RamOption, StorageConfig,
    CpuCatalog, NicCatalog, DriveCatalog,
    ModelCpuOption, ModelNicOption, StorageConfigDrive,
    ValidatedNic, ValidatedPlatform, Switch,
)
from models import DISK_SIZES_TB, RAM_SIZES_GB
from liveoptics import parse_liveoptics
from rvtools import parse_rvtools
from recommend import (
    generate_recommendations, OS_CORE_OVERHEAD, USABLE_RAM_OVERHEAD,
    MAX_NODES_PER_CLUSTER, _cluster_layout, _cluster_usable_storage,
)
from export_pptx import generate_proposal, generate_config_slide
from storage_only import (
    MIN_HCI_NODES_PER_CLUSTER, STORAGE_ONLY_RAM_FLOOR_GB,
)
from admin_routes import admin_bp


def create_app():
    app = Flask(__name__)

    # Signed-cookie sessions. SECRET_KEY must be set in production (and shared
    # across gunicorn workers, since each validates the same cookie signature).
    # Fall back to an ephemeral key for dev with a loud warning — sessions then
    # won't survive a restart or span multiple workers.
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        secret = secrets.token_hex(32)
        app.logger.warning(
            "SECRET_KEY not set — generated an ephemeral key. Logins will not "
            "persist across restarts or gunicorn workers. Set SECRET_KEY in prod."
        )
    app.config["SECRET_KEY"] = secret
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    # Only require HTTPS for the cookie when explicitly told we're behind TLS.
    app.config["SESSION_COOKIE_SECURE"] = (
        os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"
    )

    init_db(app)
    register_auth(app)
    app.register_blueprint(admin_bp)

    # Daily retention/GDPR-anonymization scheduler. Disabled (ENABLE_SCHEDULER=0)
    # for one-off processes like seeding/CLI; on by default for the web server.
    if os.environ.get("ENABLE_SCHEDULER", "1") != "0":
        start_scheduler(app)

    # Cache-bust static assets by file mtime so a rebuild always serves fresh
    # JS/CSS (no more stale-cache surprises during iteration).
    @app.context_processor
    def _asset_helper():
        def asset(path):
            full = os.path.join(app.static_folder, path)
            try:
                v = int(os.path.getmtime(full))
            except OSError:
                v = 0
            return f"/static/{path}?v={v}"
        return {"asset": asset}

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/privacy")
    def privacy():
        return render_template("privacy.html")

    @app.route("/api/models")
    def get_models():
        mode = request.args.get("mode", "appliance")
        status_filter = request.args.get("status", "active")

        if mode == "appliance":
            query = Model.query.options(
                joinedload(Model.cpu_links).joinedload(ModelCpuOption.cpu),
                joinedload(Model.nic_links).joinedload(ModelNicOption.nic),
                joinedload(Model.ram_options),
                joinedload(Model.storage_config)
                    .joinedload(StorageConfig.drive_links)
                    .joinedload(StorageConfigDrive.drive),
            )
            if status_filter == "active":
                query = query.filter(Model.status == "Active")
            elif status_filter == "all_current":
                query = query.filter(Model.status.in_(["Active", "EOL"]))
            # Validated-only platforms have no certified equivalent, so they
            # don't belong in the certified appliance picker.
            query = query.filter(Model.validated_only == False)  # noqa: E712

            models = {}
            for m in query.order_by(Model.category, Model.name).all():
                models[m.name] = m.to_dict()
            return jsonify(models)
        else:
            nics = [n.to_dict() for n in ValidatedNic.query.all()]
            platforms = [p.to_dict() for p in ValidatedPlatform.query.filter_by(status="Active").all()]
            return jsonify({
                "nics": nics,
                "disk_sizes": DISK_SIZES_TB,
                "ram_sizes": RAM_SIZES_GB,
                "platforms": platforms,
            })

    @app.route("/api/model/<model_name>")
    def get_model(model_name):
        m = Model.query.filter_by(name=model_name).first()
        if m:
            return jsonify(m.to_dict())
        return jsonify({"error": "Model not found"}), 404

    @app.route("/api/switches")
    def get_switches():
        switches = [s.to_dict() for s in Switch.query.all()]
        return jsonify(switches)

    @app.route("/api/validated-platforms")
    def get_validated_platforms():
        platforms = [p.to_dict() for p in ValidatedPlatform.query.all()]
        return jsonify(platforms)

    @app.route("/api/validated-platforms/<int:platform_id>")
    def get_validated_platform(platform_id):
        p = ValidatedPlatform.query.get_or_404(platform_id)
        return jsonify(p.to_dict())

    @app.route("/api/calculate", methods=["POST"])
    def calculate():
        data = request.json
        mode = data.get("mode", "appliance")
        node_count = data.get("node_count", 3)

        if node_count < 1:
            return jsonify({"error": "Minimum 1 node required"}), 400

        if mode == "appliance":
            return jsonify(calculate_appliance(data, node_count))
        else:
            return jsonify(calculate_validated(data, node_count))

    @app.route("/api/import-liveoptics", methods=["POST"])
    def import_liveoptics():
        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        f = request.files["file"]
        if not f.filename or not f.filename.endswith(".xlsx"):
            return jsonify({"error": "File must be an .xlsx Excel file"}), 400

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
        try:
            f.save(tmp.name)
            tmp.close()

            file_type = _detect_file_type(tmp.name)
            if file_type == "rvtools":
                data = parse_rvtools(tmp.name)
            elif file_type == "liveoptics":
                data = parse_liveoptics(tmp.name)
            else:
                return jsonify({"error": "Unrecognised file format. Please upload a Live Optics or RVTools Excel export."}), 400

            vcpu_ratio = request.form.get("vcpu_ratio", type=float)
            result = generate_recommendations(data["summary"], vcpu_ratio)
            return jsonify({
                "summary": data["summary"],
                "project": data["project"],
                "hosts": data["hosts"],
                "datastores": data["datastores"],
                "vms": data["vms"],
                "vm_count": len(data["vms"]),
                "active_vm_count": data["summary"]["active_vms"],
                "recommendations": result["recommendations"],
                "projection": result["projection"],
                "warnings": result.get("warnings", []),
                "source": file_type,
            })
        except Exception as e:
            return jsonify({"error": f"Failed to parse file: {str(e)}"}), 400
        finally:
            os.unlink(tmp.name)

    @app.route("/api/recommend", methods=["POST"])
    def recommend():
        data = request.json
        summary = data.get("summary")
        if not summary:
            return jsonify({"error": "No summary provided"}), 400
        vcpu_ratio = data.get("vcpu_ratio")
        growth_pct = data.get("growth_pct", 10)
        snapshot_pct = data.get("snapshot_pct", 20)
        years = data.get("years", 5)
        target_nodes = data.get("target_nodes")
        storage_pref = data.get("storage_pref")
        size_full_cluster = data.get("size_full_cluster", False)
        sizing_mode = data.get("sizing_mode", "certified")
        allow_storage_only = data.get("allow_storage_only", False)
        result = generate_recommendations(summary, vcpu_ratio,
                                          growth_pct, snapshot_pct, years,
                                          target_nodes=target_nodes,
                                          storage_pref=storage_pref,
                                          size_full_cluster=size_full_cluster,
                                          sizing_mode=sizing_mode,
                                          allow_storage_only=allow_storage_only)
        return jsonify(result)

    @app.route("/api/export-config", methods=["POST"])
    def export_config():
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400
        try:
            buf = generate_config_slide(data)
            mode = data.get("mode", "config")
            model = data.get("model", mode)
            nodes = data.get("node_count", "")
            filename = f"SC_Config_{model}_{nodes}N.pptx"
            return send_file(buf, as_attachment=True, download_name=filename,
                             mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation")
        except Exception as e:
            return jsonify({"error": f"Failed to generate config slide: {str(e)}"}), 500

    @app.route("/api/export-proposal", methods=["POST"])
    def export_proposal():
        data = request.json
        summary = data.get("summary")
        recommendation = data.get("recommendation")
        projection = data.get("projection")
        if not summary or not recommendation or not projection:
            return jsonify({"error": "Missing summary, recommendation, or projection"}), 400

        try:
            buf = generate_proposal(summary, recommendation, projection)
            model_name = recommendation.get("model", "proposal")
            filename = f"SC_Proposal_{model_name}_{recommendation.get('node_count', '')}N.pptx"
            return send_file(buf, as_attachment=True, download_name=filename,
                             mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation")
        except Exception as e:
            return jsonify({"error": f"Failed to generate proposal: {str(e)}"}), 500

    return app


def _detect_file_type(file_path):
    from openpyxl import load_workbook
    wb = load_workbook(file_path, read_only=True)
    sheets = set(wb.sheetnames)
    wb.close()
    if "vInfo" in sheets or "vMetaData" in sheets:
        return "rvtools"
    if "ESX Hosts" in sheets or "Details" in sheets:
        return "liveoptics"
    return None


def calculate_appliance(data, node_count):
    model_name = data.get("model")
    m = Model.query.filter_by(name=model_name).first()
    if not m:
        return {"error": "Invalid model"}

    model = m.to_dict()
    min_nodes = model.get("min_nodes", 1)
    if node_count < min_nodes:
        return {"error": f"Minimum {min_nodes} nodes required for {model_name}"}

    cpu_idx = data.get("cpu_index", 0)
    if cpu_idx >= len(model["cpu_options"]):
        return {"error": "Invalid CPU selection"}
    cpu = model["cpu_options"][cpu_idx]

    ram_gb = data.get("ram_gb", model["ram_options_gb"][0])
    if ram_gb not in model["ram_options_gb"]:
        return {"error": "Invalid RAM selection"}

    storage = model["storage"]
    raw_per_node = compute_raw_per_node_appliance(data, storage)
    if isinstance(raw_per_node, dict) and "error" in raw_per_node:
        return raw_per_node

    biggest_disk = compute_biggest_disk_appliance(data, storage)

    # Optional storage-only nodes: same model/drives, virtualization disabled.
    # They add raw capacity (and disks) to the cluster but no usable compute.
    so_block, so_err = _appliance_storage_only(model, data, raw_per_node, node_count)
    if so_err:
        return so_err
    so_count = so_block["count"] if so_block else 0
    total_nodes = node_count + so_count

    # A HyperCore cluster holds at most 8 nodes, so larger builds split into
    # several clusters. Each cluster needs >=2 full HCI nodes (HA + rolling
    # updates), so the HCI floor scales with the cluster count.
    layout = _cluster_layout(total_nodes)
    num_clusters = len(layout)
    min_hci = MIN_HCI_NODES_PER_CLUSTER * num_clusters
    if (so_count > 0 or num_clusters > 1) and node_count < min_hci:
        plural = "s" if num_clusters != 1 else ""
        return {"error": (
            f"{num_clusters} cluster{plural} ({' + '.join(map(str, layout))} nodes, "
            f"max {MAX_NODES_PER_CLUSTER} per cluster) require at least {min_hci} full "
            f"HCI nodes — 2 per cluster for HA and rolling updates. You have {node_count}."
        )}

    total_raw = raw_per_node * total_nodes
    usable = (_cluster_usable_storage(raw_per_node, biggest_disk, layout)
              if total_nodes > 1 else raw_per_node)

    # Apply HyperCore OS overhead. Compute capacity comes from the HCI nodes only.
    usable_cores = cpu["cores"] - OS_CORE_OVERHEAD
    usable_ram = ram_gb - USABLE_RAM_OVERHEAD

    total_cores = usable_cores * node_count
    total_threads = cpu["threads"] * node_count
    total_ghz = cpu["ghz"] * cpu["cores"] * node_count
    total_ram = usable_ram * node_count

    # N-1: one HCI node offline per cluster.
    n1_hci = max(node_count - num_clusters, 0)
    n1_cores = usable_cores * n1_hci if node_count > 1 else usable_cores
    n1_threads = cpu["threads"] * n1_hci if node_count > 1 else cpu["threads"]
    n1_ghz = cpu["ghz"] * cpu["cores"] * n1_hci if node_count > 1 else total_ghz
    n1_ram = usable_ram * n1_hci if node_count > 1 else usable_ram

    return {
        "mode": "appliance",
        "model": model_name,
        "node_count": node_count,
        "total_node_count": total_nodes,
        "num_clusters": num_clusters,
        "cluster_layout": layout,
        "storage_only": so_block,
        "per_node": {
            "cpu": cpu["desc"],
            "cores": usable_cores,
            "threads": cpu["threads"],
            "ghz": cpu["ghz"],
            "ram_gb": usable_ram,
            "raw_storage_tb": round(raw_per_node, 2),
        },
        "cluster_total": {
            "cores": total_cores,
            "threads": total_threads,
            "total_ghz": round(total_ghz, 2),
            "ram_gb": total_ram,
            "raw_storage_tb": round(total_raw, 2),
            "usable_storage_tb": round(usable, 2),
        },
        "n_minus_1": {
            "cores": n1_cores,
            "threads": n1_threads,
            "total_ghz": round(n1_ghz, 2),
            "ram_gb": n1_ram,
            "usable_storage_tb": round(usable, 2),
        },
        "form_factor": model["form_factor"],
        "chassis": model["chassis"],
        "status": model["status"],
    }


def _appliance_storage_only(model, data, raw_per_node, hci_count):
    """Build the storage-only-node block for an appliance config, or (None, None)
    when none requested. Returns (block, error_dict). Storage-only nodes reuse
    the model's drives (so raw_per_node is shared), take a single lowest-tier CPU
    and a compliant RAM option, and require >=2 full HCI nodes in the cluster."""
    so = data.get("storage_only") or {}
    count = int(so.get("count", 0) or 0)
    if count <= 0:
        return None, None
    if hci_count < MIN_HCI_NODES_PER_CLUSTER:
        return None, {"error": (
            f"At least {MIN_HCI_NODES_PER_CLUSTER} full HCI nodes are required "
            f"when adding storage-only nodes (for HA and rolling updates)."
        )}

    # Certified: real single-CPU SKUs only (sibling model) — falls back to the
    # model's own CPUs (dual when no single sibling exists). Never fabricated.
    cpu_opts = model.get("storage_only_cpu_options") or model["cpu_options"]
    if not cpu_opts:
        return None, {"error": "No storage-only CPU option for this model."}
    ci = int(so.get("cpu_index", 0) or 0)
    if ci < 0 or ci >= len(cpu_opts):
        return None, {"error": "Invalid storage-only CPU selection"}
    cpu = cpu_opts[ci]

    ram_options = model["ram_options_gb"]
    # Certified: the compliant minimum is the model's smallest RAM option (often
    # >16 GB). Editable upward, but only to a real model option.
    ram_gb = so.get("ram_gb", ram_options[0] if ram_options else STORAGE_ONLY_RAM_FLOOR_GB)
    if ram_options and ram_gb not in ram_options:
        return None, {"error": "Invalid storage-only RAM selection"}

    return {
        "count": count,
        "cpu": cpu["desc"],
        "cpu_index": ci,
        "cores": cpu["cores"],
        "threads": cpu["threads"],
        "ghz": cpu["ghz"],
        "ram_gb": ram_gb,
        "raw_storage_tb": round(raw_per_node, 2),
    }, None


def compute_raw_per_node_appliance(data, storage):
    stype = storage["type"]
    if stype == "nvme_only":
        nvme_tb = data.get("nvme_tb", storage["nvme_options_tb"][0])
        count = storage.get("drives_per_node", 1)
        return nvme_tb * count
    elif stype == "ssd_only":
        ssd_tb = data.get("ssd_tb", storage["ssd_options_tb"][0])
        count = storage.get("drives_per_node", 4)
        return ssd_tb * count
    elif stype == "hdd_only":
        hdd_tb = data.get("hdd_tb", storage["hdd_options_tb"][0])
        count = storage.get("drives_per_node", 4)
        return hdd_tb * count
    elif stype == "hybrid":
        hdd_tb = data.get("hdd_tb", storage["hdd_options_tb"][0])
        ssd_tb = data.get("ssd_tb", storage["ssd_options_tb"][0])
        return (hdd_tb * storage["hdd_count"]) + (ssd_tb * storage["ssd_count"])
    elif stype == "hybrid_nvme":
        hdd_tb = data.get("hdd_tb", storage["hdd_options_tb"][0])
        nvme_tb = data.get("nvme_tb", storage["nvme_options_tb"][0])
        return (hdd_tb * storage["hdd_count"]) + (nvme_tb * storage["nvme_count"])
    elif stype == "nvme_and_ssd":
        nvme_tb = data.get("nvme_tb", storage["nvme_options_tb"][0])
        ssd_tb = data.get("ssd_tb", storage["ssd_options_tb"][0])
        return nvme_tb + ssd_tb
    elif stype == "cloud":
        return 0
    return 0


def compute_biggest_disk_appliance(data, storage):
    stype = storage["type"]
    if stype == "nvme_only":
        return data.get("nvme_tb", storage["nvme_options_tb"][0])
    elif stype == "ssd_only":
        return data.get("ssd_tb", storage["ssd_options_tb"][0])
    elif stype == "hdd_only":
        return data.get("hdd_tb", storage["hdd_options_tb"][0])
    elif stype == "hybrid":
        return max(data.get("hdd_tb", storage["hdd_options_tb"][0]),
                   data.get("ssd_tb", storage["ssd_options_tb"][0]))
    elif stype == "hybrid_nvme":
        return max(data.get("hdd_tb", storage["hdd_options_tb"][0]),
                   data.get("nvme_tb", storage["nvme_options_tb"][0]))
    elif stype == "nvme_and_ssd":
        return max(data.get("nvme_tb", storage["nvme_options_tb"][0]),
                   data.get("ssd_tb", storage["ssd_options_tb"][0]))
    return 0


def calculate_validated(data, node_count):
    if node_count < 2:
        return {"error": "Software-only (validated) requires minimum 2 nodes"}

    cores = data.get("cores_per_node", 4)
    threads = data.get("threads_per_node", 8)
    ghz = data.get("ghz", 2.0)
    ram_gb = data.get("ram_gb", 64)

    disks = data.get("disks", [])
    if not disks:
        return {"error": "At least 1 disk required per node"}

    disk_count = len(disks)
    if disk_count == 2:
        return {"error": "Disk count must be 1 or 3+. 2 disks is not supported."}

    # Optional storage-only nodes: same disks, virtualization disabled. They add
    # capacity and disks to the cluster but no usable compute.
    so_block, so_err = _validated_storage_only(data, disk_count=disk_count,
                                               hci_count=node_count)
    if so_err:
        return so_err
    so_count = so_block["count"] if so_block else 0
    total_nodes = node_count + so_count

    # A HyperCore cluster holds at most 8 nodes, so larger builds split into
    # several clusters, each needing >=2 full HCI nodes (HA + rolling updates).
    layout = _cluster_layout(total_nodes)
    num_clusters = len(layout)
    min_hci = MIN_HCI_NODES_PER_CLUSTER * num_clusters
    if (so_count > 0 or num_clusters > 1) and node_count < min_hci:
        plural = "s" if num_clusters != 1 else ""
        return {"error": (
            f"{num_clusters} cluster{plural} ({' + '.join(map(str, layout))} nodes, "
            f"max {MAX_NODES_PER_CLUSTER} per cluster) require at least {min_hci} full "
            f"HCI nodes — 2 per cluster for HA and rolling updates. You have {node_count}."
        )}

    # 100-disk hard limit binds on the LARGEST cluster, not the total node
    # count. Storage-only nodes carry the same disks, so they count too.
    largest_cluster = max(layout)
    max_cluster_disks = disk_count * largest_cluster
    if max_cluster_disks > 100:
        return {
            "error": (
                f"Cluster disk limit exceeded: {max_cluster_disks} disks "
                f"({disk_count} per node × {largest_cluster} nodes in the largest "
                f"cluster). The maximum is 100 disks per cluster. When more storage "
                f"capacity is required, deploy more clusters or use bigger disks."
            )
        }

    has_spinning = any(d["type"] in ("SAS", "NLSAS", "SATA", "HDD") for d in disks)
    has_flash = any(d["type"] in ("SSD", "NVMe") for d in disks)
    is_hybrid = has_spinning and has_flash

    if is_hybrid:
        total_cap = sum(d["size_tb"] for d in disks)
        flash_cap = sum(d["size_tb"] for d in disks if d["type"] in ("SSD", "NVMe"))
        if total_cap > 0:
            flash_pct = (flash_cap / total_cap) * 100
            if flash_pct < 7 or flash_pct > 24.3:
                return {
                    "error": f"Hybrid fast tier must be 7-24.3% of total capacity. Currently {flash_pct:.1f}%",
                    "flash_percentage": round(flash_pct, 1),
                }

    # Apply HyperCore OS overhead
    usable_cores = cores - OS_CORE_OVERHEAD
    usable_ram = ram_gb - USABLE_RAM_OVERHEAD

    raw_per_node = sum(d["size_tb"] for d in disks)
    biggest_disk = max(d["size_tb"] for d in disks)
    # Storage spans all nodes (HCI + storage-only), per cluster; compute spans
    # the HCI nodes only.
    total_raw = raw_per_node * total_nodes
    usable = (_cluster_usable_storage(raw_per_node, biggest_disk, layout)
              if total_nodes > 1 else raw_per_node)
    if so_block:
        so_block["raw_storage_tb"] = round(raw_per_node, 2)

    total_cores = usable_cores * node_count
    total_threads = threads * node_count
    total_ghz = ghz * cores * node_count
    total_ram = usable_ram * node_count

    # N-1: one HCI node offline per cluster.
    n1_hci = max(node_count - num_clusters, 0)
    n1_cores = usable_cores * n1_hci
    n1_threads = threads * n1_hci
    n1_ghz = ghz * cores * n1_hci
    n1_ram = usable_ram * n1_hci

    storage_type = "All-Flash"
    if is_hybrid:
        storage_type = "Hybrid"
    elif has_spinning:
        storage_type = "HDD-Only"

    return {
        "mode": "validated",
        "node_count": node_count,
        "total_node_count": total_nodes,
        "num_clusters": num_clusters,
        "cluster_layout": layout,
        "storage_only": so_block,
        "storage_type": storage_type,
        "per_node": {
            "cores": usable_cores,
            "threads": threads,
            "ghz": ghz,
            "ram_gb": usable_ram,
            "disk_count": disk_count,
            "raw_storage_tb": round(raw_per_node, 2),
            "disks": disks,
        },
        "cluster_total": {
            "cores": total_cores,
            "threads": total_threads,
            "total_ghz": round(total_ghz, 2),
            "ram_gb": total_ram,
            "raw_storage_tb": round(total_raw, 2),
            "usable_storage_tb": round(usable, 2),
        },
        "n_minus_1": {
            "cores": n1_cores,
            "threads": n1_threads,
            "total_ghz": round(n1_ghz, 2),
            "ram_gb": n1_ram,
            "usable_storage_tb": round(usable, 2),
        },
        "validation": {
            "disk_count_valid": disk_count == 1 or disk_count >= 3,
            "hybrid_ratio_valid": True,
            "no_raid": True,
            "internal_only": True,
        },
    }


def _validated_storage_only(data, disk_count, hci_count):
    """Build the storage-only-node block for a validated (software-only) config,
    or (None, None) when none requested. Storage-only nodes carry the same disks
    as the HCI nodes; the caller fills in raw_storage_tb. A single low CPU and
    >=16 GB RAM are user-supplied; requires >=2 full HCI nodes."""
    so = data.get("storage_only") or {}
    count = int(so.get("count", 0) or 0)
    if count <= 0:
        return None, None
    if hci_count < MIN_HCI_NODES_PER_CLUSTER:
        return None, {"error": (
            f"At least {MIN_HCI_NODES_PER_CLUSTER} full HCI nodes are required "
            f"when adding storage-only nodes (for HA and rolling updates)."
        )}

    cores = int(so.get("cores", 1) or 1)
    threads = int(so.get("threads", cores * 2) or cores * 2)
    ghz = float(so.get("ghz", 2.0) or 2.0)
    ram_gb = int(so.get("ram_gb", STORAGE_ONLY_RAM_FLOOR_GB) or STORAGE_ONLY_RAM_FLOOR_GB)
    if ram_gb < STORAGE_ONLY_RAM_FLOOR_GB:
        return None, {"error": (
            f"Storage-only nodes require at least {STORAGE_ONLY_RAM_FLOOR_GB} GB RAM."
        )}

    return {
        "count": count,
        "cores": cores,
        "threads": threads,
        "ghz": ghz,
        "ram_gb": ram_gb,
        "disk_count": disk_count,
        "raw_storage_tb": 0,  # filled in once raw_per_node is known
    }, None


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
