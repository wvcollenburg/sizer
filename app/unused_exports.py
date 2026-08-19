"""Parked (unregistered) single-sizing / inline export endpoints.

These synchronous export routes powered the per-recommendation, per-config and
in-sizer multi-site export buttons that were REMOVED when exports moved to the
project level (the async bundle path in projects.queue_export + export_worker).

They are kept here — importable but NOT wired into the app — so the behaviour can
be restored without digging through git history. To re-enable, add this to
app.create_app() (just before ``return app``):

    from unused_exports import register_unused_exports
    register_unused_exports(app, pick_lang)

Nothing imports this module by default, so these routes do not exist at runtime.
"""
import io

from flask import jsonify, request, send_file

from extensions import limiter
from export_gate import export_gate
from auth import current_user
from export_pptx import (generate_proposal, generate_config_slide,
                         generate_bundle_proposal)
from export_docx import (build_proposal_docx, build_bundle_proposal_docx,
                         convert_docx_to_pdf, convert_pptx_to_pdf)


def register_unused_exports(app, pick_lang):
    """Re-attach the parked synchronous export routes to ``app``.

    ``pick_lang`` is app.create_app's request-language resolver, passed in rather
    than imported back out of the app module. Not called by default.
    """
    from app import MAX_EXPORT_SECTIONS  # noqa: F401 (used by _bundle_payload)

    @app.route("/api/export-config", methods=["POST"])
    @limiter.limit("20 per minute")
    @export_gate
    def export_config():
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400
        if not _can_export_editable():
            return jsonify({"error": "The editable PowerPoint is available to Scale users only. Use the PDF instead."}), 403
        try:
            buf = generate_config_slide(data, lang=pick_lang())
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
    @limiter.limit("20 per minute")
    @export_gate
    def export_config_pdf():
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400
        try:
            pptx_buf = generate_config_slide(data, lang=pick_lang())
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
    @limiter.limit("20 per minute")
    @export_gate
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
            buf = generate_proposal(summary, recommendation, projection, source_perf,
                                    lang=pick_lang())
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
    @limiter.limit("20 per minute")
    @export_gate
    def export_docx_route():
        summary, recommendation, projection, source_perf = _proposal_payload()
        if not summary or not recommendation or not projection:
            return jsonify({"error": "Missing summary, recommendation, or projection"}), 400
        if not _can_export_editable():
            return jsonify({"error": "The editable Word document is available to Scale users only. Use the PDF instead."}), 403
        try:
            buf = build_proposal_docx(summary, recommendation, projection, source_perf,
                                      lang=pick_lang())
            fn = f"SC_Proposal_{recommendation.get('model', 'proposal')}_{recommendation.get('node_count', '')}N.docx"
            return send_file(buf, as_attachment=True, download_name=fn,
                             mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        except Exception as e:
            app.logger.exception("Document generation failed: %s", e)
            return jsonify({"error": "Failed to generate the document."}), 500

    @app.route("/api/export-pdf", methods=["POST"])
    @limiter.limit("20 per minute")
    @export_gate
    def export_pdf_route():
        summary, recommendation, projection, source_perf = _proposal_payload()
        if not summary or not recommendation or not projection:
            return jsonify({"error": "Missing summary, recommendation, or projection"}), 400
        try:
            docx_buf = build_proposal_docx(summary, recommendation, projection, source_perf,
                                           lang=pick_lang())
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
    @limiter.limit("20 per minute")
    @export_gate
    def export_presentation_pdf_route():
        summary, recommendation, projection, source_perf = _proposal_payload()
        if not summary or not recommendation or not projection:
            return jsonify({"error": "Missing summary, recommendation, or projection"}), 400
        try:
            pptx_buf = generate_proposal(summary, recommendation, projection, source_perf,
                                         lang=pick_lang())
            pdf = convert_pptx_to_pdf(pptx_buf.getvalue())
            if not pdf:
                return jsonify({"error": "PDF conversion is unavailable on this server."}), 503
            fn = f"SC_Presentation_{recommendation.get('model', 'proposal')}_{recommendation.get('node_count', '')}N.pdf"
            return send_file(io.BytesIO(pdf), as_attachment=True, download_name=fn,
                             mimetype="application/pdf")
        except Exception as e:
            app.logger.exception("Presentation PDF generation failed: %s", e)
            return jsonify({"error": "Failed to generate the presentation PDF."}), 500

    # ---- Combined multi-site exports (one document, per-cluster sections) ----
    def _bundle_payload():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return None
        clusters = data.get("clusters")
        if not isinstance(clusters, list) or not clusters:
            return None
        if len(clusters) > MAX_EXPORT_SECTIONS:
            return None
        for cl in clusters:
            if not (cl.get("summary") and cl.get("recommendation") and cl.get("projection")):
                return None
        return clusters

    @app.route("/api/export-bundle-proposal", methods=["POST"])
    @limiter.limit("20 per minute")
    @export_gate
    def export_bundle_proposal():
        clusters = _bundle_payload()
        if not clusters:
            return jsonify({"error": "Missing or incomplete cluster data"}), 400
        if not _can_export_editable():
            return jsonify({"error": "The editable PowerPoint is available to Scale users only. Use the PDF instead."}), 403
        try:
            buf = generate_bundle_proposal(clusters, lang=pick_lang())
            fn = f"SC_Proposal_MultiSite_{len(clusters)}clusters.pptx"
            return send_file(buf, as_attachment=True, download_name=fn,
                             mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation")
        except Exception as e:
            app.logger.exception("Multi-site proposal generation failed: %s", e)
            return jsonify({"error": "Failed to generate the proposal."}), 500

    @app.route("/api/export-bundle-docx", methods=["POST"])
    @limiter.limit("20 per minute")
    @export_gate
    def export_bundle_docx():
        clusters = _bundle_payload()
        if not clusters:
            return jsonify({"error": "Missing or incomplete cluster data"}), 400
        if not _can_export_editable():
            return jsonify({"error": "The editable Word document is available to Scale users only. Use the PDF instead."}), 403
        try:
            buf = build_bundle_proposal_docx(clusters, lang=pick_lang())
            fn = f"SC_Proposal_MultiSite_{len(clusters)}clusters.docx"
            return send_file(buf, as_attachment=True, download_name=fn,
                             mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        except Exception as e:
            app.logger.exception("Multi-site document generation failed: %s", e)
            return jsonify({"error": "Failed to generate the document."}), 500

    @app.route("/api/export-bundle-pdf", methods=["POST"])
    @limiter.limit("20 per minute")
    @export_gate
    def export_bundle_pdf():
        clusters = _bundle_payload()
        if not clusters:
            return jsonify({"error": "Missing or incomplete cluster data"}), 400
        try:
            docx_buf = build_bundle_proposal_docx(clusters, lang=pick_lang())
            pdf = convert_docx_to_pdf(docx_buf.getvalue())
            if not pdf:
                return jsonify({"error": "PDF conversion is unavailable on this server."}), 503
            fn = f"SC_Proposal_MultiSite_{len(clusters)}clusters.pdf"
            return send_file(io.BytesIO(pdf), as_attachment=True, download_name=fn,
                             mimetype="application/pdf")
        except Exception as e:
            app.logger.exception("Multi-site PDF generation failed: %s", e)
            return jsonify({"error": "Failed to generate the PDF."}), 500

    @app.route("/api/export-bundle-presentation-pdf", methods=["POST"])
    @limiter.limit("20 per minute")
    @export_gate
    def export_bundle_presentation_pdf():
        clusters = _bundle_payload()
        if not clusters:
            return jsonify({"error": "Missing or incomplete cluster data"}), 400
        try:
            pptx_buf = generate_bundle_proposal(clusters, lang=pick_lang())
            pdf = convert_pptx_to_pdf(pptx_buf.getvalue())
            if not pdf:
                return jsonify({"error": "PDF conversion is unavailable on this server."}), 503
            fn = f"SC_Presentation_MultiSite_{len(clusters)}clusters.pdf"
            return send_file(io.BytesIO(pdf), as_attachment=True, download_name=fn,
                             mimetype="application/pdf")
        except Exception as e:
            app.logger.exception("Multi-site presentation PDF generation failed: %s", e)
            return jsonify({"error": "Failed to generate the presentation PDF."}), 500
