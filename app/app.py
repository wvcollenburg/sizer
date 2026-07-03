import io
import os
import secrets
import tempfile

from flask import Flask, render_template, jsonify, request, send_file
from werkzeug.middleware.proxy_fix import ProxyFix
from database import init_db
from extensions import limiter
from auth import register_auth, start_scheduler, current_user
from sqlalchemy.orm import joinedload
from orm_models import (
    Model, StorageConfig,
    ModelCpuOption, ModelNicOption, StorageConfigDrive,
    ValidatedNic, ValidatedPlatform,
    DriveCatalog, RamOption,
)
from models import RAM_SIZES_GB
from liveoptics import parse_liveoptics
from rvtools import parse_rvtools
from recommend import (
    generate_recommendations, _cluster_layout, _cluster_usable_storage,
)
from tunables import T, refresh_from_db
from export_pptx import generate_proposal, generate_config_slide
from export_docx import build_proposal_docx, convert_docx_to_pdf, convert_pptx_to_pdf
from cluster_diagram import network_svg_for
from admin_routes import admin_bp


def _validated_disk_sizes():
    """Disk-size options for the validated picker, read live from the
    admin-editable drive catalog so a newly added drive size is immediately
    selectable without a code change. Keyed by performance bucket; the front end
    maps the spinning interface types (SAS/NLSAS/SATA) onto the HDD bucket."""
    buckets = {"HDD": set(), "SSD": set(), "NVMe": set()}
    for drive in DriveCatalog.query.all():
        if drive.drive_type in buckets:
            buckets[drive.drive_type].add(drive.size_tb)
    return {bucket: sorted(sizes) for bucket, sizes in buckets.items()}


def _validated_ram_sizes():
    """RAM options for the validated picker: every size the hardware catalog
    offers (across all models) unioned with the standard baseline, so an
    admin-added RAM size shows up while the generic list never shrinks."""
    catalog = {
        row.size_gb
        for row in RamOption.query.with_entities(RamOption.size_gb).distinct()
    }
    return sorted(set(RAM_SIZES_GB) | catalog)


# UI languages the sizer ships translations for (app/static/js/lang/<code>.js).
# English is the base/fallback and must stay first. Codes are the primary
# language subtag (Accept-Language best_match maps e.g. sv-SE -> sv); "pt" ships
# Brazilian Portuguese. Each needs an entry in LANG_NAMES below.
SUPPORTED_LANGS = ["en", "de", "fr", "nl", "es", "it", "pt", "ja",
                   "sv", "da", "no", "fi", "et", "lv", "lt"]

# Endonyms (each language's own name) for the header language menu.
LANG_NAMES = {
    "en": "English", "de": "Deutsch", "fr": "Français", "nl": "Nederlands",
    "es": "Español", "it": "Italiano", "pt": "Português", "ja": "日本語",
    "sv": "Svenska", "da": "Dansk", "no": "Norsk", "fi": "Suomi",
    "et": "Eesti", "lv": "Latviešu", "lt": "Lietuvių",
}


def create_app():
    app = Flask(__name__)

    # Behind nginx: trust one proxy hop for the client IP (X-Forwarded-For) and
    # scheme (X-Forwarded-Proto) so rate limiting buckets per real client and
    # Flask knows requests are HTTPS. X-Forwarded-Host is deliberately NOT
    # trusted — email links use a configured base URL (see auth.app_base_url) to
    # avoid Host-header poisoning.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=0)

    # Cap request bodies (defense against memory-exhaustion uploads). Generous
    # enough for a large RVTools/Live Optics export; the saved-config payload has
    # its own tighter 4 MB check in the configs blueprint.
    app.config["MAX_CONTENT_LENGTH"] = int(
        os.environ.get("MAX_CONTENT_LENGTH_BYTES", str(32 * 1024 * 1024)))

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
    limiter.init_app(app)
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

    # UI language selection. Order of precedence:
    #   1. the `lang` cookie, but only if it names a supported language — this is
    #      written client-side (i18n.js setLang) ONLY when the user explicitly
    #      picks a language, so it always reflects a deliberate choice;
    #   2. otherwise the browser's Accept-Language header (auto-detection) —
    #      read-only, never persisted;
    #   3. English as the final fallback.
    # Every template gets `lang` (the active code) and `supported_langs` (for the
    # switcher); client-side i18n.js reads the code back from <html lang="..">.
    @app.context_processor
    def _lang_helper():
        cookie = (request.cookies.get("lang") or "").lower()
        if cookie in SUPPORTED_LANGS:
            lang = cookie
        else:
            lang = request.accept_languages.best_match(SUPPORTED_LANGS) or "en"
        return {"lang": lang, "supported_langs": SUPPORTED_LANGS,
                "lang_names": LANG_NAMES}

    @app.route("/")
    def index():
        # Surface the admin-tuned sizing defaults the client needs at load time
        # (the ratio slider's starting position). Resilient: if the settings read
        # fails, T keeps its last/default values so the page still renders.
        try:
            refresh_from_db()
        except Exception:
            pass
        return render_template("index.html", default_vcpu_ratio=T.default_vcpu_ratio,
                               max_day_one_storage_pct=T.max_day_one_storage_pct,
                               max_day_one_ram_pct=T.max_day_one_ram_pct)

    @app.route("/privacy")
    def privacy():
        return render_template("privacy.html")

    @app.route("/api/models")
    def get_models():
        mode = request.args.get("mode", "appliance")
        status_filter = request.args.get("status", "active")

        if mode == "appliance":
            # The "Size For Model" picker must list exactly the models the
            # recommendation engine will consider for the chosen sizing mode
            # (see recommend.generate_recommendations): validated mode includes
            # validated-only platforms but drops NVMe+SSD (1+1) models, while
            # certified mode is the inverse.
            validated = request.args.get("sizing") == "validated"
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
            if not validated:
                # Validated-only platforms have no certified equivalent, so they
                # don't belong in the certified appliance picker.
                query = query.filter(Model.validated_only == False)  # noqa: E712

            models = {}
            for m in query.order_by(Model.category, Model.name).all():
                # Validated mode can't use NVMe+SSD models (inherently 2-disk),
                # so exclude them to match what the engine will actually size.
                if validated and m.storage_config \
                        and m.storage_config.storage_type == "nvme_and_ssd":
                    continue
                models[m.name] = m.to_dict()
            return jsonify(models)
        else:
            nics = [n.to_dict() for n in ValidatedNic.query.all()]
            platforms = [p.to_dict() for p in ValidatedPlatform.query.filter_by(status="Active").all()]
            return jsonify({
                "nics": nics,
                "disk_sizes": _validated_disk_sizes(),
                "ram_sizes": _validated_ram_sizes(),
                "platforms": platforms,
            })

    @app.route("/api/calculate", methods=["POST"])
    def calculate():
        # Load the current admin-tuned overheads/limits for this request.
        refresh_from_db()
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
            app.logger.warning("Import parse failed: %s", e)
            return jsonify({"error": "Could not parse the file. Upload a valid Live Optics or RVTools .xlsx export."}), 400
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
        target_model = data.get("target_model")
        include_eol_eos = data.get("include_eol_eos", False)
        max_day_one_storage_pct = data.get("max_day_one_storage_pct")
        max_day_one_ram_pct = data.get("max_day_one_ram_pct")
        # Optional source-environment CPU benchmark for the perf comparison
        # (SPECrate2017 or PassMark, per the detected source CPU class).
        source_perf_index = data.get("source_perf_index")
        source_perf_type = data.get("source_perf_type")
        result = generate_recommendations(summary, vcpu_ratio,
                                          growth_pct, snapshot_pct, years,
                                          target_nodes=target_nodes,
                                          storage_pref=storage_pref,
                                          size_full_cluster=size_full_cluster,
                                          sizing_mode=sizing_mode,
                                          allow_storage_only=allow_storage_only,
                                          target_model=target_model,
                                          include_eol_eos=include_eol_eos,
                                          max_day_one_storage_pct=max_day_one_storage_pct,
                                          max_day_one_ram_pct=max_day_one_ram_pct,
                                          source_perf_index=source_perf_index,
                                          source_perf_type=source_perf_type)
        return jsonify(result)

    @app.route("/api/cpu-perf")
    def cpu_perf():
        """Look up a CPU's benchmark score by (fuzzy) description, to auto-fill
        the source-benchmark field on import/manual. Per-CPU/socket value — the
        caller scales by the source socket count. Tries our curated appliance
        catalog first (precise), then the broad SPECrate2017 lookup (~625 CPUs
        averaged from all published SPEC CPU 2017 int-rate results) so arbitrary
        SOURCE CPUs resolve too. found=false only when neither knows it."""
        import cpu_benchmarks
        from cpu_specs import CPU_SPECS, cpu_model_key, perf_index as _perf_index
        q = request.args.get("q", "")
        spec = CPU_SPECS.get(cpu_model_key(q) or "")
        if spec:
            ptype = "specrate" if spec.get("specrate_int") is not None else "passmark"
            return jsonify({
                "found": True,
                "model": spec["model"],
                "perf_type": ptype,
                "perf_index": _perf_index(spec),
                "specrate_int": spec.get("specrate_int"),
                "passmark_cpu_mark": spec.get("passmark_cpu_mark"),
                "passmark_single": spec.get("passmark_single"),
                "source": "catalog",
            })
        hit = cpu_benchmarks.lookup(q)
        if hit:
            return jsonify({
                "found": True,
                "model": hit["model"],
                "perf_type": "specrate",
                "perf_index": hit["specrate_int"],
                "specrate_int": hit["specrate_int"],
                "passmark_cpu_mark": None,
                "passmark_single": None,
                "source": "spec-cpu2017",
                "samples": hit["samples"],
            })
        return jsonify({"found": False})

    @app.route("/api/export-config", methods=["POST"])
    def export_config():
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400
        if not _can_export_editable():
            return jsonify({"error": "The editable PowerPoint is available to Scale users only. Use the PDF instead."}), 403
        try:
            buf = generate_config_slide(data)
            mode = data.get("mode", "config")
            model = data.get("model", mode)
            nodes = data.get("node_count", "")
            filename = f"SC_Config_{model}_{nodes}N.pptx"
            return send_file(buf, as_attachment=True, download_name=filename,
                             mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation")
        except Exception as e:
            app.logger.exception("Config slide generation failed: %s", e)
            return jsonify({"error": "Failed to generate the configuration slide."}), 500

    @app.route("/api/export-config-pdf", methods=["POST"])
    def export_config_pdf():
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400
        try:
            pptx_buf = generate_config_slide(data)
            pdf = convert_pptx_to_pdf(pptx_buf.getvalue())
            if not pdf:
                return jsonify({"error": "PDF conversion is unavailable on this server."}), 503
            model = data.get("model", data.get("mode", "config"))
            nodes = data.get("node_count", "")
            filename = f"SC_Config_{model}_{nodes}N.pdf"
            return send_file(io.BytesIO(pdf), as_attachment=True, download_name=filename,
                             mimetype="application/pdf")
        except Exception as e:
            app.logger.exception("Config PDF generation failed: %s", e)
            return jsonify({"error": "Failed to generate the configuration PDF."}), 500

    @app.route("/api/export-proposal", methods=["POST"])
    def export_proposal():
        data = request.json
        summary = data.get("summary")
        recommendation = data.get("recommendation")
        projection = data.get("projection")
        source_perf = data.get("source_perf")
        if not summary or not recommendation or not projection:
            return jsonify({"error": "Missing summary, recommendation, or projection"}), 400
        if not _can_export_editable():
            return jsonify({"error": "The editable PowerPoint is available to Scale users only. Use the PDF instead."}), 403

        try:
            buf = generate_proposal(summary, recommendation, projection, source_perf)
            model_name = recommendation.get("model", "proposal")
            filename = f"SC_Proposal_{model_name}_{recommendation.get('node_count', '')}N.pptx"
            return send_file(buf, as_attachment=True, download_name=filename,
                             mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation")
        except Exception as e:
            app.logger.exception("Proposal generation failed: %s", e)
            return jsonify({"error": "Failed to generate the proposal."}), 500

    def _proposal_payload():
        data = request.json or {}
        return (data.get("summary"), data.get("recommendation"),
                data.get("projection"), data.get("source_perf"))

    def _can_export_editable():
        # Editable source files (Word, PPTX) are limited to Scale users and super
        # admins; everyone else is restricted to read-only PDFs.
        u = current_user()
        return bool(u and (u.is_scale or u.is_super_admin))

    @app.route("/api/export-docx", methods=["POST"])
    def export_docx_route():
        summary, recommendation, projection, source_perf = _proposal_payload()
        if not summary or not recommendation or not projection:
            return jsonify({"error": "Missing summary, recommendation, or projection"}), 400
        if not _can_export_editable():
            return jsonify({"error": "The editable Word document is available to Scale users only. Use the PDF instead."}), 403
        try:
            buf = build_proposal_docx(summary, recommendation, projection, source_perf)
            fn = f"SC_Proposal_{recommendation.get('model', 'proposal')}_{recommendation.get('node_count', '')}N.docx"
            return send_file(buf, as_attachment=True, download_name=fn,
                             mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        except Exception as e:
            app.logger.exception("Document generation failed: %s", e)
            return jsonify({"error": "Failed to generate the document."}), 500

    @app.route("/api/export-pdf", methods=["POST"])
    def export_pdf_route():
        summary, recommendation, projection, source_perf = _proposal_payload()
        if not summary or not recommendation or not projection:
            return jsonify({"error": "Missing summary, recommendation, or projection"}), 400
        try:
            docx_buf = build_proposal_docx(summary, recommendation, projection, source_perf)
            pdf = convert_docx_to_pdf(docx_buf.getvalue())
            if not pdf:
                return jsonify({"error": "PDF conversion is unavailable on this server."}), 503
            fn = f"SC_Proposal_{recommendation.get('model', 'proposal')}_{recommendation.get('node_count', '')}N.pdf"
            return send_file(io.BytesIO(pdf), as_attachment=True, download_name=fn,
                             mimetype="application/pdf")
        except Exception as e:
            app.logger.exception("PDF generation failed: %s", e)
            return jsonify({"error": "Failed to generate the PDF."}), 500

    @app.route("/api/export-presentation-pdf", methods=["POST"])
    def export_presentation_pdf_route():
        summary, recommendation, projection, source_perf = _proposal_payload()
        if not summary or not recommendation or not projection:
            return jsonify({"error": "Missing summary, recommendation, or projection"}), 400
        try:
            pptx_buf = generate_proposal(summary, recommendation, projection, source_perf)
            pdf = convert_pptx_to_pdf(pptx_buf.getvalue())
            if not pdf:
                return jsonify({"error": "PDF conversion is unavailable on this server."}), 503
            fn = f"SC_Presentation_{recommendation.get('model', 'proposal')}_{recommendation.get('node_count', '')}N.pdf"
            return send_file(io.BytesIO(pdf), as_attachment=True, download_name=fn,
                             mimetype="application/pdf")
        except Exception as e:
            app.logger.exception("Presentation PDF generation failed: %s", e)
            return jsonify({"error": "Failed to generate the presentation PDF."}), 500

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


# Shown (GUI + PPTX) in place of the N-1 block for a Single Node System, which
# has no peer to fail over to. Surfaced via the response so both renderers stay
# in sync on wording.
SNS_NO_REDUNDANCY_MSG = (
    "No redundancy — a single-node system cannot tolerate a node failure. "
    "Ensure workloads are protected with replication or a properly configured backup."
)


def _cluster_min_hci_error(node_count, so_count, layout):
    """Multi-cluster or storage-only builds need >=2 full HCI nodes per cluster
    (HA + rolling updates), so the HCI floor scales with the cluster count.
    Returns an error dict if that floor isn't met, else None. Shared by the
    appliance and validated calculators."""
    num_clusters = len(layout)
    min_hci = T.min_hci_nodes_per_cluster * num_clusters
    if (so_count > 0 or num_clusters > 1) and node_count < min_hci:
        plural = "s" if num_clusters != 1 else ""
        return {"error": (
            f"{num_clusters} cluster{plural} ({' + '.join(map(str, layout))} nodes, "
            f"max {T.max_nodes_per_cluster} per cluster) require at least {min_hci} full "
            f"HCI nodes — 2 per cluster for HA and rolling updates. You have {node_count}."
        )}
    return None


def _n_minus_1_block(node_count, num_clusters, cores, threads, ghz, ram, usable_tb):
    """Surviving capacity with one HCI node offline per cluster. For a single
    node there's no peer to fail over to, so the node's own full capacity is
    reported (the GUI/PPTX label it as no-redundancy separately)."""
    n1_hci = max(node_count - num_clusters, 0)
    mult = n1_hci if node_count > 1 else 1
    return {
        "cores": cores * mult,
        "threads": threads * mult,
        "total_ghz": round(ghz * mult, 2),
        "ram_gb": ram * mult,
        "usable_storage_tb": round(usable_tb, 2),
    }


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
    # several clusters (the HCI floor scales with the cluster count).
    layout = _cluster_layout(total_nodes)
    num_clusters = len(layout)
    hci_err = _cluster_min_hci_error(node_count, so_count, layout)
    if hci_err:
        return hci_err

    total_raw = raw_per_node * total_nodes
    if total_nodes > 1:
        usable = _cluster_usable_storage(raw_per_node, biggest_disk, layout)
    else:
        # Single Node System. A hybrid SNS must mirror each tier within the one
        # node, which needs >=2 disks of every type; a 3+1 layout is out of scope.
        sns_err = _sns_storage_error(storage, model_name)
        if sns_err:
            return sns_err
        # RF2 still mirrors across the node's own drives (usable = raw/2), but
        # reserves no rebuild disk — there's no peer node to rebuild onto, so the
        # largest-disk reserve that multi-node clusters hold back doesn't apply. A
        # single-disk SNS (e.g. HE153) can't mirror at all, so its raw capacity is
        # fully usable.
        usable = (raw_per_node if compute_drive_count_appliance(data, storage) <= 1
                  else raw_per_node / 2)

    # Apply HyperCore OS overhead. Compute capacity comes from the HCI nodes only.
    # OS RAM overhead is tiered by this node's drive-bay count.
    usable_cores = cpu["cores"] - T.os_core_overhead
    usable_ram = ram_gb - T.usable_ram_overhead_for(
        compute_drive_count_appliance(data, storage))

    total_cores = usable_cores * node_count
    total_threads = cpu["threads"] * node_count
    total_ghz = cpu["ghz"] * cpu["cores"] * node_count
    total_ram = usable_ram * node_count

    n_minus_1 = _n_minus_1_block(node_count, num_clusters, usable_cores,
                                 cpu["threads"], cpu["ghz"] * cpu["cores"],
                                 usable_ram, usable)

    _nic_ports = max((o.get("ports", 2) for o in model.get("nic_options", [])), default=2)
    network_svg = network_svg_for(node_count, so_block["count"] if so_block else 0, _nic_ports)

    return {
        "mode": "appliance",
        "model": model_name,
        "node_count": node_count,
        "total_node_count": total_nodes,
        "num_clusters": num_clusters,
        "cluster_layout": layout,
        "storage_only": so_block,
        "network_svg": network_svg,
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
        "n_minus_1": n_minus_1,
        "single_node": total_nodes == 1,
        "redundancy_note": SNS_NO_REDUNDANCY_MSG if total_nodes == 1 else None,
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
    if hci_count < T.min_hci_nodes_per_cluster:
        return None, {"error": (
            f"At least {T.min_hci_nodes_per_cluster} full HCI nodes are required "
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
    ram_gb = so.get("ram_gb", ram_options[0] if ram_options else T.storage_only_ram_floor_gb)
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


def _sns_storage_error(storage, model_name):
    """Validate that a model can run as a Single Node System (SNS). A hybrid SNS
    must mirror each storage tier within the one node (RF2), which requires at
    least two disks of every type. A 3+1 layout (a single disk in one tier) can't
    be mirrored, so it's out of scope for SNS — return an error pointing the user
    at a multi-node build."""
    stype = storage["type"]
    if stype == "hybrid":
        tiers = {"HDD": storage["hdd_count"], "SSD": storage["ssd_count"]}
    elif stype == "hybrid_nvme":
        tiers = {"HDD": storage["hdd_count"], "NVMe": storage["nvme_count"]}
    elif stype == "nvme_and_ssd":
        tiers = {"NVMe": 1, "SSD": 1}
    else:
        return None
    if any(c < 2 for c in tiers.values()):
        layout = ", ".join("%d× %s" % (c, t) for t, c in tiers.items())
        return {"error": (
            f"{model_name} can't be configured as a single node: a hybrid Single "
            f"Node System must mirror each storage tier locally, which needs at "
            f"least 2 disks of every type (this layout is {layout}). Use 2 or more "
            f"nodes for this model."
        )}
    return None


def compute_drive_count_appliance(data, storage):
    """Number of physical drives in one node — used to decide whether a Single
    Node System can mirror (RF2). A single-disk node has no second drive to
    mirror to, so it runs unprotected (usable = raw)."""
    stype = storage["type"]
    if stype in ("nvme_only", "ssd_only", "hdd_only"):
        return storage.get("drives_per_node", 1)
    elif stype == "hybrid":
        return storage["hdd_count"] + storage["ssd_count"]
    elif stype == "hybrid_nvme":
        return storage["hdd_count"] + storage["nvme_count"]
    elif stype == "nvme_and_ssd":
        return 2
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
    # several clusters (the HCI floor scales with the cluster count).
    layout = _cluster_layout(total_nodes)
    num_clusters = len(layout)
    hci_err = _cluster_min_hci_error(node_count, so_count, layout)
    if hci_err:
        return hci_err

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
            if flash_pct < 7 or flash_pct > 25:
                return {
                    "error": f"Hybrid fast tier must be 7-25% of total capacity. Currently {flash_pct:.1f}%",
                    "flash_percentage": round(flash_pct, 1),
                }
        # HEAT best practice: enough HDD spindles per flash disk so the slow tier
        # can absorb cold data evicted from flash (Certified appliances already
        # encode this; enforce it on Validated configs too).
        hdd_n = sum(1 for d in disks if d["type"] in ("SAS", "NLSAS", "SATA", "HDD"))
        flash_n = sum(1 for d in disks if d["type"] in ("SSD", "NVMe"))
        min_ratio = T.hybrid_min_hdd_per_flash
        if flash_n > 0 and hdd_n < min_ratio * flash_n:
            return {
                "error": (f"Hybrid tiered layout needs at least {min_ratio} HDDs per "
                          f"flash disk for HEAT down-tiering. Currently {hdd_n}× HDD : "
                          f"{flash_n}× flash."),
            }

    # Apply HyperCore OS overhead; OS RAM is tiered by the node's drive count.
    usable_cores = cores - T.os_core_overhead
    usable_ram = ram_gb - T.usable_ram_overhead_for(disk_count)

    raw_per_node = sum(d["size_tb"] for d in disks)
    biggest_disk = max(d["size_tb"] for d in disks)
    # Storage spans all nodes (HCI + storage-only), per cluster; compute spans
    # the HCI nodes only.
    total_raw = raw_per_node * total_nodes
    if total_nodes > 1:
        usable = _cluster_usable_storage(raw_per_node, biggest_disk, layout)
    else:
        # Single Node System: RF2 mirrors across the node's own drives (raw/2)
        # but reserves no rebuild disk; a single-disk SNS can't mirror at all.
        usable = raw_per_node if disk_count <= 1 else raw_per_node / 2
    if so_block:
        so_block["raw_storage_tb"] = round(raw_per_node, 2)

    total_cores = usable_cores * node_count
    total_threads = threads * node_count
    total_ghz = ghz * cores * node_count
    total_ram = usable_ram * node_count

    n_minus_1 = _n_minus_1_block(node_count, num_clusters, usable_cores,
                                 threads, ghz * cores, usable_ram, usable)

    storage_type = "All-Flash"
    if is_hybrid:
        storage_type = "Hybrid"
    elif has_spinning:
        storage_type = "HDD-Only"

    # Software-only configs carry no model NIC count; default to dedicated (4)
    # unless the request specifies otherwise.
    _nic_ports = int(data.get("nic_ports", 4) or 4)
    network_svg = network_svg_for(node_count, so_block["count"] if so_block else 0, _nic_ports)

    return {
        "mode": "validated",
        "node_count": node_count,
        "total_node_count": total_nodes,
        "num_clusters": num_clusters,
        "cluster_layout": layout,
        "storage_only": so_block,
        "storage_type": storage_type,
        "network_svg": network_svg,
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
        "n_minus_1": n_minus_1,
        "single_node": total_nodes == 1,
        "redundancy_note": SNS_NO_REDUNDANCY_MSG if total_nodes == 1 else None,
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
    if hci_count < T.min_hci_nodes_per_cluster:
        return None, {"error": (
            f"At least {T.min_hci_nodes_per_cluster} full HCI nodes are required "
            f"when adding storage-only nodes (for HA and rolling updates)."
        )}

    cores = int(so.get("cores", 1) or 1)
    threads = int(so.get("threads", cores * 2) or cores * 2)
    ghz = float(so.get("ghz", 2.0) or 2.0)
    ram_gb = int(so.get("ram_gb", T.storage_only_ram_floor_gb) or T.storage_only_ram_floor_gb)
    if ram_gb < T.storage_only_ram_floor_gb:
        return None, {"error": (
            f"Storage-only nodes require at least {T.storage_only_ram_floor_gb} GB RAM."
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
