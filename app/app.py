import os
import secrets
from urllib.parse import urlparse

from flask import Flask, render_template, jsonify, request, send_file
from werkzeug.middleware.proxy_fix import ProxyFix
from database import init_db
from extensions import limiter
from auth import register_auth, start_scheduler
from projects import register_projects
from sqlalchemy.orm import joinedload
from orm_models import (
    Model, StorageConfig,
    ModelCpuOption, ModelNicOption, StorageConfigDrive,
    ValidatedNic, ValidatedPlatform,
    DriveCatalog, RamOption,
)
from models import RAM_SIZES_GB
from import_merge import merge as merge_datasets
import catalog_service
import hcl_vendor
import import_service
from import_service import AssessmentError, detect_file_type as _detect_file_type
import sizing_service
from sizing_service import SizingRequestError
from tunables import T, refresh_from_db
# NOTE: exports are project-level only (export_worker + the project export
# queue); the old synchronous in-sizer export routes were removed with the
# per-cluster-sizing restructure.
from admin_routes import admin_bp
from mcp_server import mcp_bp
from i18n import SUPPORTED_LANGS, LANG_NAMES
from calc import calculate_appliance, calculate_validated
from request_utils import json_body, sha256_file, spooled_xlsx, UploadError
from storage_layout import StorageLayoutError
from fingerprint import PARSER_VERSION

# Upper bounds on client-supplied sizing counts. The recommendation engine is
# internally bounded (it searches a small node range), but the direct
# /api/calculate path builds per-node structures + an SVG straight from these, so
# an unclamped value (e.g. node_count=1e8) is a trivial CPU/memory DoS. These
# ceilings are far above any real cluster while keeping a hostile request cheap.
MAX_NODE_COUNT = 1000
# A bundle export iterates every section into one document; bound it so a giant
# sections list can't drive an unbounded (and soffice-backed) render. A "section"
# is one source cluster today; with projects it is also one sizing's cluster
# (docs/projects-plan.md §7.1), which is why the limit is no longer named after
# clusters.
MAX_EXPORT_SECTIONS = 50
# Files one multi-file import may merge (each parsed in its own request).
MAX_MERGE_FILES = 50


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


# The supported-language list and endonyms live in i18n.py (single source of truth,
# shared with the export translator) and are imported above.


def pick_lang():
    """Resolve the active UI language for the current request — READ ONLY, never
    writes a cookie. Precedence: the `lang` cookie (set only when the user
    explicitly picks a language) -> the browser's Accept-Language -> English. Used
    both to render pages and to language-match generated exports."""
    cookie = (request.cookies.get("lang") or "").lower()
    if cookie in SUPPORTED_LANGS:
        return cookie
    return request.accept_languages.best_match(SUPPORTED_LANGS) or "en"


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
        # In production (signalled by SESSION_COOKIE_SECURE=true behind TLS) an
        # ephemeral key is dangerous: it churns logins across workers/restarts and
        # makes the Fernet-encrypted SMTP secret undecryptable. Refuse to boot.
        if os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true":
            raise RuntimeError(
                "SECRET_KEY must be set in production. Refusing to start with an "
                "ephemeral key (SESSION_COOKIE_SECURE=true indicates prod)."
            )
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

    # CSRF defence-in-depth: reject state-changing requests whose Origin/Referer
    # is a different host. SameSite=Lax already blocks cross-site cookie sending,
    # so this is a second layer. Fail-open when no Origin/Referer is present (non-
    # browser clients like curl) — those can't be a browser CSRF vector anyway.
    def _allowed_hosts():
        hosts = set()
        base = (os.environ.get("APP_BASE_URL") or "").strip()
        if base:
            h = urlparse(base if "://" in base else "https://" + base).hostname
            if h:
                hosts.add(h.lower())
        try:
            h = urlparse("http://" + request.host).hostname
            if h:
                hosts.add(h.lower())
        except Exception:
            pass
        return hosts

    @app.before_request
    def _csrf_origin_guard():
        if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
            return
        if request.blueprint == "mcp":
            # Not a cookie-authenticated request: the MCP endpoint takes a
            # bearer token and validates Origin per the MCP transport spec.
            return
        if request.blueprint == "oauth":
            # Token, registration and revocation are machine calls from the
            # client's servers (or a browser-based client with a foreign
            # Origin); the consent POST is protected by its session nonce.
            return
        src = request.headers.get("Origin") or request.headers.get("Referer")
        if not src:
            return
        try:
            host = urlparse(src).hostname
        except Exception:
            return
        if host and host.lower() not in _allowed_hosts():
            return jsonify({"error": "Cross-origin request blocked"}), 403

    register_auth(app)
    register_projects(app)
    app.register_blueprint(admin_bp)
    from hcl_admin_routes import hcl_admin_bp  # HCL catalog admin API (docs/bom-checker-build.md §6)
    app.register_blueprint(hcl_admin_bp)
    from bom_routes import register_bom  # BOM checks on projects + review queue
    register_bom(app)
    from manual_routes import manual_bp  # the online user manual, two editions
    app.register_blueprint(manual_bp)
    # The MCP endpoint (docs/mcp/01-tool-design.md): bearer-authenticated,
    # exempt from the session login gate and from the Origin guard above,
    # both of which it enforces itself.
    app.register_blueprint(mcp_bp)
    from mcp_uploads import upload_bp  # the upload page behind start_upload (session login)
    app.register_blueprint(upload_bp)
    from mcp_auth import connections_bp, oauth_bp  # OAuth AS + discovery (docs/mcp/03-auth.md)
    app.register_blueprint(oauth_bp)
    app.register_blueprint(connections_bp)

    # Daily retention/GDPR-anonymization scheduler. Disabled (ENABLE_SCHEDULER=0)
    # for one-off processes like seeding/CLI; on by default for the web server.
    if os.environ.get("ENABLE_SCHEDULER", "1") != "0":
        start_scheduler(app)
        # Drains queued bundle exports. Claims are atomic, so the one thread
        # per gunicorn worker cooperate rather than build the same job several
        # times over (docs/projects-plan.md §7.2).
        from export_worker import start_export_worker
        start_export_worker(app)
        # BOM uploads no parser recognises, read by the Claude agent
        # (bom/agent_worker.py). Same claim discipline as the exports.
        from bom.agent_worker import start_bom_agent_worker
        start_bom_agent_worker(app)

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
        return {"lang": pick_lang(), "supported_langs": SUPPORTED_LANGS,
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
                               max_day_one_ram_pct=T.max_day_one_ram_pct,
                               # Validated (software-only) rule limits, surfaced so
                               # the client's live rule indicators mirror the same
                               # admin-tuned values the calculator enforces server-
                               # side (rather than hardcoded constants that drift).
                               max_cluster_disks=T.max_cluster_disks,
                               hybrid_flash_min_pct=T.hybrid_flash_min_pct,
                               hybrid_flash_max_pct=T.hybrid_flash_max_pct,
                               hybrid_min_hdd_per_flash=T.hybrid_min_hdd_per_flash,
                               # Rendered into the Vendor selects rather than
                               # fetched, so a restored sizing can set its saved
                               # vendor without racing an async option load.
                               hcl_vendors=hcl_vendor.list_vendors())

    @app.route("/favicon.ico")
    def favicon():
        """Browsers ask for /favicon.ico unprompted, regardless of the <link>
        tags, and a 404 on every page load is noise in the console and the
        access log."""
        return send_file(
            os.path.join(app.static_folder, "img", "favicon.ico"),
            mimetype="image/vnd.microsoft.icon")

    @app.route("/privacy")
    def privacy():
        return render_template("privacy.html")

    @app.route("/api/models")
    def get_models():
        mode = request.args.get("mode", "appliance")
        status_filter = request.args.get("status", "active")

        if mode == "appliance":
            # The picker list lives in catalog_service so the MCP server
            # lists exactly what the engine will consider. The "Size For
            # Model" pickers always pass `sizing`; the Appliance calculator
            # (no `sizing`) still lists admin-excluded models.
            return jsonify(catalog_service.appliance_models(
                status_filter=status_filter,
                validated=request.args.get("sizing") == "validated",
                for_sizing="sizing" in request.args,
                vendor=request.args.get("vendor")))
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
        data = json_body()
        mode = data.get("mode", "appliance")
        # Coerce + bound node_count: it drives per-node loops and SVG generation,
        # so a non-int or absurd value must be rejected before any work.
        try:
            node_count = int(data.get("node_count", 3))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid node count"}), 400

        if node_count < 1:
            return jsonify({"error": "Minimum 1 node required"}), 400
        if node_count > MAX_NODE_COUNT:
            return jsonify({"error": f"Node count must be {MAX_NODE_COUNT} or fewer"}), 400

        try:
            if mode == "appliance":
                return jsonify(calculate_appliance(data, node_count))
            return jsonify(calculate_validated(data, node_count))
        except StorageLayoutError as exc:
            # The model's catalog entry lacks a drive count: a data error for
            # the admin to fix, not a server fault.
            app.logger.warning("Calculate refused: %s", exc)
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/import-liveoptics", methods=["POST"])
    def import_liveoptics():
        try:
            with spooled_xlsx(request, bad_type_msg="File must be an .xlsx Excel file") as path:
                return _import_assessment(path, request.files["file"].filename)
        except UploadError as exc:
            return jsonify({"error": str(exc)}), exc.status

    def _import_assessment(path, file_name):
        # Detection, parsing, rejection, provenance and the import warnings
        # live in import_service, shared with the MCP upload page.
        try:
            try:
                data = import_service.parse_assessment(path, file_name)
            except AssessmentError as exc:
                body = {"error": str(exc)}
                if exc.code:
                    body["error_code"] = exc.code
                    body["error_params"] = exc.params
                return jsonify(body), exc.status
            file_type = data["file_type"]
            source_meta = data["source_meta"]
            if request.args.get("raw"):
                # One file of a multi-file import: the parsed records only,
                # for /api/import-merge. No sizing here; that runs on the
                # merged estate.
                return jsonify({
                    "summary": data["summary"],
                    "project": data["project"],
                    "hosts": data["hosts"],
                    "host_performance": data.get("host_performance") or [],
                    "host_nics": data.get("host_nics") or [],
                    "datastores": data["datastores"],
                    "vms": data["vms"],
                    "clusters": data.get("clusters", []),
                    "scan_type": data.get("scan_type"),
                    "file_type": file_type,
                    "import_warnings": data["import_warnings"],
                    "source": file_type,
                    "source_meta": source_meta,
                })

            vcpu_ratio = request.form.get("vcpu_ratio", type=float)
            result = sizing_service.run_recommendation(data["summary"],
                                                       vcpu_ratio=vcpu_ratio)
            return jsonify({
                "summary": data["summary"],
                "project": data["project"],
                "hosts": data["hosts"],
                # With these the response doubles as a dataset for a later
                # multi-file merge (a sizing stores its sources).
                "host_performance": data.get("host_performance") or [],
                "host_nics": data.get("host_nics") or [],
                "scan_type": data.get("scan_type"),
                "file_type": file_type,
                "datastores": data["datastores"],
                "vms": data["vms"],
                "vm_count": len(data["vms"]),
                "active_vm_count": data["summary"]["active_vms"],
                # Per-source-cluster summaries; empty unless the source held
                # more than one vSphere cluster. Drives the "size each cluster
                # separately" option in the UI.
                "clusters": data.get("clusters", []),
                "recommendations": result["recommendations"],
                "projection": result["projection"],
                # Engine warnings plus the input caveats (sizing_service).
                "warnings": result["warnings"],
                # Data-quality caveats about the import itself (no IOPS/perf,
                # assumed ratio, unclustered hosts, ...). {code, params} objects
                # translated client-side; shown on the wizard's Environment step.
                "import_warnings": data["import_warnings"],
                "source": file_type,
                # Provenance for the project view and the export appendix
                # (docs/projects-plan.md §8). Combining several customer files
                # into one proposal is exactly when the reader needs to know
                # which number came from where. The digest also lets the client
                # warn when the same file is imported into a project twice.
                "source_meta": source_meta,
            })
        except Exception as e:
            app.logger.warning("Import parse failed: %s", e)
            return jsonify({"error": import_service.PARSE_FAILED_MESSAGE}), 400

    @app.route("/api/recommend", methods=["POST"])
    def recommend():
        # Parsing, defaults, error mapping and the input caveats live in
        # sizing_service so the MCP server sizes exactly as this route does.
        try:
            return jsonify(sizing_service.recommend(request.get_json(silent=True)))
        except SizingRequestError as exc:
            if exc.code == "catalog_error":
                app.logger.warning("Recommend refused: %s", exc)
            elif exc.code == "invalid_input":
                app.logger.warning("Recommend rejected malformed input")
            return jsonify({"error": str(exc)}), exc.status

    @app.route("/api/import-merge", methods=["POST"])
    def import_merge_route():
        """Merge several raw-parsed files (POST /api/import-liveoptics?raw=1)
        into one estate, cut into the mapped target clusters. Stateless: the
        client posts every dataset with its current overlap choices and
        mapping, and calls again whenever one changes."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or not isinstance(data.get("datasets"), list):
            return jsonify({"error": "No datasets provided"}), 400
        datasets = data["datasets"]
        if not datasets or len(datasets) > MAX_MERGE_FILES:
            return jsonify({"error": f"Merge 1 to {MAX_MERGE_FILES} files"}), 400
        resolutions = data.get("resolutions")
        mapping = data.get("mapping")
        resolutions = {} if resolutions is None else resolutions
        mapping = {} if mapping is None else mapping
        if not isinstance(resolutions, dict) or not isinstance(mapping, dict):
            return jsonify({"error": "Invalid merge request"}), 400
        try:
            return jsonify(merge_datasets(datasets, resolutions, mapping))
        except (TypeError, ValueError, KeyError, AttributeError):
            app.logger.warning("Import merge rejected malformed input")
            return jsonify({"error": "The imported files are incomplete or "
                                     "malformed. Import them again."}), 400

    @app.route("/api/cpu-perf")
    def cpu_perf():
        """Look up a CPU's benchmark score by (fuzzy) description, to auto-fill
        the source-benchmark field on import/manual. Per-CPU/socket value — the
        caller scales by the source socket count. The lookup order lives in
        sizing_service.lookup_cpu_perf, shared with the MCP server."""
        return jsonify(sizing_service.lookup_cpu_perf(request.args.get("q", "")))

    return app


app = create_app()

if __name__ == "__main__":
    # Debug (Werkzeug interactive debugger = RCE) is OFF unless explicitly enabled
    # via FLASK_DEBUG=1 for local dev. Production runs under gunicorn, not this.
    app.run(host=os.environ.get("FLASK_RUN_HOST", "0.0.0.0"), port=5000,
            debug=os.environ.get("FLASK_DEBUG", "") == "1")
