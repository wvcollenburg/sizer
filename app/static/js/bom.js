/* BOM checker — project-page UI (docs/bom-checker-build.md §7,
 * docs/bomchecker-polish-plan.md).
 *
 * Pure view layer over:
 *   GET  /api/bom/capabilities            (cached per page load)
 *   GET  /api/bom/template                (plain <a download> in the markup)
 *   GET  /api/projects/<id>/bom-checks    (project section + header badge)
 *   POST /api/projects/<id>/bom-checks    (multipart: file, sizing_id?, name?, notify?)
 *        201 = checked; 202 = {job}: no parser knows the layout, the Claude
 *        agent reads it in the background
 *   GET  /api/projects/<id>/bom-agent-jobs  (jobs still running / failed today)
 *   GET  /api/bom-agent-jobs/<id>          (poll)   POST .../notify   GET .../template
 *   GET  /api/bom-checks/<id>              (full result)   GET .../template
 *   POST /api/bom-checks/<id>/recheck      (json {sizing_id})
 *   DELETE /api/bom-checks/<id>
 *
 * Classic script sharing one global scope with projects.js — everything lives
 * inside this IIFE and only the delegate handlers are exported (all carrying a
 * "bom"/"Bom" name so they cannot collide). Helpers that projects.js already
 * defines (api, escHtml, tt, currentProject) are CALLED, never redeclared.
 * Result payload keys are read defensively in both the §5 snake_case spelling
 * and the camelCase spelling normalize.py emits (config_results /
 * configResults, config_name / configName).
 */
(function () {
    'use strict';

    const bomState = {
        capabilities: null,     // /api/bom/capabilities payload, cached per page load
        capsPromise: null,
        checks: [],             // summary rows for the open project
        jobs: [],               // agent jobs queued/running/failed for the open project
        checksProjectId: null,
        current: null,          // full check shown in the result step
        file: null,             // File chosen for the check
        uploadedCheckId: null,  // check made from bomState.file in this session
        sizingId: '',           // '' = compatibility only; otherwise sizing id as string
        jobId: null,            // agent job the modal is waiting on
        pollTimer: null,
        listTimer: null,
        dropBound: false,
        busy: false,
    };

    const DEFAULT_ACCEPT = ['.xlsx', '.xls', '.csv'];
    const POLL_MS = 3000;
    const LIST_REFRESH_MS = 8000;
    const SEVERITY_RANK = { error: 0, warning: 1, info: 2 };
    const VERDICT_CLASS = { PASS: 'badge-pass', FAIL: 'badge-fail', INCONCLUSIVE: 'badge-warn' };
    const FIT_CLASS = { match: 'badge-pass', bigger: 'badge-info', fits: 'badge-info', smaller: 'badge-fail', unknown: 'badge-neutral' };
    const RELATION_CLASS = { equal: 'badge-pass', bigger: 'badge-info', fits: 'badge-info', smaller: 'badge-fail', unknown: 'badge-neutral' };
    const SEVERITY_CLASS = { error: 'badge-fail', warning: 'badge-warn', info: 'badge-info' };
    const KNOWN_FORMATS = ['lenovo_dcsc', 'dell_service_tag', 'dell_quote', 'template', 'ai_agent'];
    const KNOWN_DIMS = ['nodes', 'cores', 'compute', 'ram', 'largest_vm', 'storage', 'nic'];
    const UNIT_KEYS = { nodes: 'nodes', cores: 'cores', GB: 'gb', TB: 'tb', GbE: 'gbe', '%': 'pct' };

    function t(key, vars) { return window.t ? window.t(key, vars) : key; }

    function $(id) { return document.getElementById(id); }

    function pick(obj, a, b) {
        if (!obj) return undefined;
        if (obj[a] !== undefined && obj[a] !== null) return obj[a];
        return obj[b];
    }

    function fmtNum(v) {
        if (v === null || v === undefined || v === '' || isNaN(Number(v))) return '—';
        const n = Number(v);
        if (Number.isInteger(n)) return n.toLocaleString();
        return n.toLocaleString(undefined, { maximumFractionDigits: 1 });
    }

    function fmtWhen(iso) {
        if (!iso) return '';
        const d = new Date(iso);
        if (isNaN(d.getTime())) return String(iso);
        return d.toLocaleString();
    }

    function unitLabel(unit) {
        if (!unit) return '';
        if (UNIT_KEYS[unit]) return t('bom.unit.' + UNIT_KEYS[unit]);
        return String(unit);
    }

    function formatLabel(fmt) {
        if (!fmt) return t('bom.format.unknown');
        if (KNOWN_FORMATS.indexOf(fmt) >= 0) return t('bom.format.' + fmt);
        // Slugs without their own i18n key (dell_list_*, dell_vnet, dh_bid, …)
        // use the English label served by /api/bom/capabilities (FORMAT_LABELS);
        // the raw slug is the last resort if capabilities haven't loaded.
        const caps = bomState.capabilities || {};
        if (caps.formats && caps.formats[fmt]) return String(caps.formats[fmt]);
        return String(fmt);
    }

    function dimLabel(key) {
        if (KNOWN_DIMS.indexOf(key) >= 0) return t('bom.dim.' + key);
        return String(key || '');
    }

    function verdictChip(verdict) {
        const v = String(verdict || '').toUpperCase();
        const cls = VERDICT_CLASS[v] || 'badge-neutral';
        const label = VERDICT_CLASS[v] ? t('bom.verdict.' + v.toLowerCase()) : (v || '—');
        return `<span class="state-badge bom-chip ${cls}">${escHtml(label)}</span>`;
    }

    function fitChip(verdict, sizingName) {
        const v = String(verdict || '').toLowerCase();
        if (!v) return '';
        const cls = FIT_CLASS[v] || 'badge-neutral';
        const label = FIT_CLASS[v] ? t('bom.fit.verdict.' + v) : v;
        const who = sizingName ? ` · ${escHtml(sizingName)}` : '';
        return `<span class="state-badge bom-chip ${cls}" title="${escHtml(t('bom.fit.chip_title'))}">${escHtml(label)}${who}</span>`;
    }

    // ── errors inside the modal ──────────────────────────────────────────────

    function bomShowError(msg, details) {
        const box = $('bom-error');
        const text = $('bom-error-text');
        const list = $('bom-error-details');
        if (!box) return;
        if (text) text.textContent = msg || '';
        if (list) {
            const items = Array.isArray(details) ? details : [];
            list.innerHTML = items.map(d => `<li>${escHtml(typeof d === 'string' ? d : JSON.stringify(d))}</li>`).join('');
            list.hidden = items.length === 0;
        }
        box.hidden = !msg;
    }

    function bomClearError() { bomShowError('', []); bomShowRetainOffer(false); }

    // ── file-sharing offer ───────────────────────────────────────────────────
    // Consent is the button press: it re-posts the file the user already
    // picked to a dedicated endpoint; nothing is kept by the check itself.
    // Offered when a file was refused (no agent on this server) and on a
    // result the agent had to read: a layout Scale teaches the parsers is one
    // the agent never has to read again.
    function bomShowRetainOffer(on, errorText, textKey) {
        const box = $('bom-retain-offer');
        if (!box) return;
        const done = $('bom-retain-done');
        if (done) { done.hidden = true; done.textContent = ''; }
        const p = box.querySelector('p[data-i18n^="bom.retain.offer"]');
        if (p && on) {
            p.setAttribute('data-i18n', textKey || 'bom.retain.offer');
            p.textContent = t(textKey || 'bom.retain.offer');
            p.hidden = false;
        }
        box.querySelectorAll('button').forEach(b => { b.hidden = false; });
        box.hidden = !on;
        bomState.retainError = on ? (errorText || '') : '';
    }

    async function bomShareRejected() {
        if (!currentProject || !bomState.file || bomState.busy) return;
        const fd = new FormData();
        fd.append('file', bomState.file);
        if (bomState.retainError) fd.append('error', bomState.retainError);
        setBusy(true);
        let res;
        try {
            res = await api(`/api/projects/${currentProject.id}/bom-rejects`, { method: 'POST', body: fd });
        } catch (e) {
            res = { ok: false, data: { error: e.message || String(e) } };
        }
        setBusy(false);
        const done = $('bom-retain-done');
        if (res.ok) {
            const box = $('bom-retain-offer');
            if (box) {
                box.querySelectorAll('button').forEach(b => { b.hidden = true; });
                const p = box.querySelector('p[data-i18n^="bom.retain.offer"]');
                if (p) p.hidden = true;
            }
            if (done) { done.textContent = t('bom.retain.thanks'); done.hidden = false; }
        } else if (done) {
            done.textContent = (res.data && res.data.error) || t('bom.err.failed');
            done.hidden = false;
        }
    }

    function bomDismissRetain() { bomShowRetainOffer(false); }

    // Only files with text can teach the checker a layout: never a picture,
    // and not a PDF the agent had to read as an image (a scan).
    const SHAREABLE_DEFAULT = ['.xlsx', '.xls', '.csv', '.pdf', '.docx', '.txt'];

    function shareable(file, agent) {
        if (!file) return false;
        const caps = bomState.capabilities || {};
        const exts = Array.isArray(caps.shareable_extensions) ? caps.shareable_extensions : SHAREABLE_DEFAULT;
        if (!hasExtension(file.name, exts.map(e => String(e).toLowerCase()))) return false;
        return !(agent && agent.source_kind === 'pdf' && agent.pdf_mode === 'document');
    }

    function setStatus(msg, isError) {
        const el = $('bom-upload-status');
        if (!el) return;
        el.textContent = msg || '';
        el.className = 'upload-status ' + (isError ? 'upload-error' : 'upload-ok');
        el.style.display = msg ? 'block' : 'none';
    }

    function setBusy(on) {
        bomState.busy = !!on;
        ['bom-run-btn', 'bom-recheck-btn', 'bom-delete-btn'].forEach(id => {
            const el = $(id);
            if (el) el.disabled = !!on;
        });
        const run = $('bom-run-btn');
        if (run) run.textContent = on ? t('bom.working_short') : t('bom.run');
        const modal = $('bom-modal');
        if (modal) modal.classList.toggle('bom-busy', !!on);
    }

    // ── capabilities ─────────────────────────────────────────────────────────

    function loadCapabilities() {
        if (bomState.capabilities) return Promise.resolve(bomState.capabilities);
        if (bomState.capsPromise) return bomState.capsPromise;
        bomState.capsPromise = api('/api/bom/capabilities').then(({ ok, data }) => {
            bomState.capabilities = (ok && data) ? data : {
                agent_available: false,
                accepted_extensions: DEFAULT_ACCEPT,
            };
            bomState.capsPromise = null;
            return bomState.capabilities;
        }).catch(() => {
            bomState.capsPromise = null;
            return { agent_available: false, accepted_extensions: DEFAULT_ACCEPT };
        });
        return bomState.capsPromise;
    }

    function acceptedExtensions() {
        const caps = bomState.capabilities || {};
        const list = Array.isArray(caps.accepted_extensions) && caps.accepted_extensions.length
            ? caps.accepted_extensions : DEFAULT_ACCEPT;
        return list.map(e => String(e).toLowerCase());
    }

    function hasExtension(name, exts) {
        const lower = String(name || '').toLowerCase();
        return exts.some(e => lower.endsWith(e));
    }

    function applyCapabilities() {
        const caps = bomState.capabilities || {};
        const input = $('bom-file-input');
        if (input) input.setAttribute('accept', acceptedExtensions().join(','));
        const hint = $('bom-accept-hint');
        if (hint) hint.textContent = t(caps.agent_available ? 'bom.upload.accept_agent' : 'bom.upload.accept_plain');
        const drop = document.querySelector('#bom-upload-area .upload-text');
        if (drop) drop.textContent = t(caps.agent_available ? 'bom.upload.drop_hint2' : 'bom.upload.drop_hint');
        const list = $('bom-formats-list');
        if (list) {
            const labels = Object.keys(caps.formats || {}).map(k => formatLabel(k));
            list.innerHTML = labels.map(l => `<li>${escHtml(l)}</li>`).join('');
        }
        const agentNote = $('bom-formats-agent');
        if (agentNote) agentNote.hidden = !caps.agent_available;
        const notify = $('bom-agent-notify-wrap');
        if (notify) notify.hidden = !caps.notify_available;
        const cat = $('bom-catalog-line');
        if (cat) {
            const c = caps.catalog || null;
            if (c && (c.platforms || c.components)) {
                cat.textContent = t('bom.catalog_line', {
                    platforms: fmtNum(c.platforms || 0),
                    components: fmtNum(c.components || 0),
                    when: c.last_scrape_at ? fmtWhen(c.last_scrape_at) : '—',
                });
                cat.hidden = false;
            } else {
                cat.hidden = true;
            }
        }
    }

    // ── step switching ───────────────────────────────────────────────────────

    function showStep(step) {
        const show = (id, on) => { const el = $(id); if (el) el.style.display = on ? '' : 'none'; };
        show('bom-step-upload', step === 'upload');
        show('bom-step-agent', step === 'agent');
        show('bom-step-result', step === 'result');
        show('bom-run-btn', step === 'upload');
        show('bom-back-btn', step === 'result' || step === 'agent');
        show('bom-delete-btn', step === 'result');
        bomClearError();
    }

    // ── sizing dropdowns ─────────────────────────────────────────────────────
    // One option list feeds both the upload dropdown and the footer's
    // re-check dropdown. A sizing without a calculated result stays listed
    // (so the user sees it exists) but cannot be picked.

    function sizingOptions() {
        return (currentProject && currentProject.sizings) ? currentProject.sizings : [];
    }

    function renderSizingSelect(id) {
        const sel = $(id);
        if (!sel) return;
        const opts = [`<option value="">${escHtml(t('bom.pick.none'))}</option>`];
        sizingOptions().forEach(s => {
            const enabled = !!s.has_result;
            const meta = [];
            if (s.role) meta.push(tt('project.role.' + s.role));
            if (s.is_dr_target) meta.push(tt('project.table.dr_target'));
            if (!enabled) meta.push(t('bom.pick.no_result'));
            const label = s.name + (meta.length ? ' — ' + meta.join(' · ') : '');
            opts.push(`<option value="${Number(s.id)}"${enabled ? '' : ' disabled'}>${escHtml(label)}</option>`);
        });
        sel.innerHTML = opts.join('');
        sel.value = String(bomState.sizingId || '');
        if (sel.value !== String(bomState.sizingId || '')) sel.value = '';
    }

    function renderSizingPicker() {
        renderSizingSelect('bom-sizing-select');
        renderSizingSelect('bom-recheck-sizing');
    }

    function renderRecheckSelect() { renderSizingSelect('bom-recheck-sizing'); }

    function bomPickSizing(value) {
        bomState.sizingId = value == null ? '' : String(value);
    }

    // ── file selection + drag/drop ───────────────────────────────────────────

    function extractDropped(dt) {
        if (!dt) return null;
        if (dt.files && dt.files.length > 0) return dt.files[0];
        if (dt.items) {
            for (let i = 0; i < dt.items.length; i++) {
                const item = dt.items[i];
                if (item.kind === 'file') {
                    const f = item.getAsFile();
                    if (f) return f;
                }
            }
        }
        return null;
    }

    function acceptFile(file) {
        if (!file) return;
        if (!hasExtension(file.name, acceptedExtensions())) {
            bomState.file = null;
            setStatus(t('bom.upload.bad_extension', { formats: acceptedExtensions().join(', ') }), true);
            showFileName();
            return;
        }
        bomState.file = file;
        bomState.uploadedCheckId = null;
        bomShowRetainOffer(false);
        setStatus('', false);
        bomClearError();
        showFileName();
        const nameInput = $('bom-name');
        if (nameInput && !nameInput.value) nameInput.placeholder = file.name;
    }

    function showFileName() {
        const el = $('bom-file-name');
        if (!el) return;
        el.textContent = bomState.file ? bomState.file.name : '';
        el.hidden = !bomState.file;
        const area = $('bom-upload-area');
        if (area) area.classList.toggle('has-file', !!bomState.file);
    }

    function bomFileChosen(input) {
        if (input && input.files && input.files.length > 0) acceptFile(input.files[0]);
    }

    function bindDropZone() {
        if (bomState.dropBound) return;
        const area = $('bom-upload-area');
        const input = $('bom-file-input');
        if (!area || !input) return;
        bomState.dropBound = true;
        area.addEventListener('click', (e) => {
            // The input sits inside the area: its own click must not re-open the picker.
            if (e.target === input || bomState.busy) return;
            input.click();
        });
        area.addEventListener('keydown', (e) => {
            if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (!bomState.busy) input.click(); }
        });
        area.addEventListener('dragover', (e) => { e.preventDefault(); area.classList.add('drag-over'); });
        area.addEventListener('dragleave', () => area.classList.remove('drag-over'));
        area.addEventListener('drop', (e) => {
            e.preventDefault();
            area.classList.remove('drag-over');
            const f = extractDropped(e.dataTransfer);
            if (f) acceptFile(f);
        });
    }

    // ── open / close ─────────────────────────────────────────────────────────

    function resetUploadForm() {
        bomState.file = null;
        bomState.uploadedCheckId = null;
        bomState.sizingId = '';
        const fi = $('bom-file-input');
        if (fi) fi.value = '';
        const nameInput = $('bom-name');
        if (nameInput) { nameInput.value = ''; nameInput.placeholder = t('bom.name_hint'); }
        showFileName();
        setStatus('', false);
    }

    function showModal() {
        bindDropZone();
        const modal = $('bom-modal');
        if (modal) modal.style.display = 'flex';
        loadCapabilities().then(applyCapabilities);
    }

    function openBomChecker() {
        if (!currentProject) return;
        stopPolling();
        bomState.current = null;
        resetUploadForm();
        setBusy(false);
        renderSizingPicker();
        showStep('upload');
        showModal();
    }

    function closeBomChecker() {
        const modal = $('bom-modal');
        if (modal) modal.style.display = 'none';
        stopPolling();
        bomState.current = null;
        setBusy(false);
    }

    function bomBack() {
        // Leaving the agent wait is fine: the job carries on and its result
        // lands in the project's BOM checks list.
        stopPolling();
        bomState.current = null;
        resetUploadForm();
        renderSizingPicker();
        showStep('upload');
    }

    // ── run a check ──────────────────────────────────────────────────────────

    async function bomRunCheck() {
        if (!currentProject || bomState.busy) return;
        bomClearError();
        if (!bomState.file) {
            bomShowError(t('bom.err.no_file'), []);
            return;
        }
        const fd = new FormData();
        fd.append('file', bomState.file);
        if (bomState.sizingId) fd.append('sizing_id', bomState.sizingId);
        const nameInput = $('bom-name');
        const name = nameInput ? nameInput.value.trim() : '';
        if (name) fd.append('name', name);
        fd.append('lang', (currentProject && currentProject.lang) || window.I18N_ACTIVE || 'en');

        setBusy(true);
        setStatus(t('bom.working'), false);
        let res;
        try {
            res = await api(`/api/projects/${currentProject.id}/bom-checks`, { method: 'POST', body: fd });
        } catch (e) {
            setBusy(false);
            setStatus('', false);
            bomShowError(t('bom.err.network', { error: e.message || String(e) }), []);
            return;
        }
        setBusy(false);
        setStatus('', false);
        if (!res.ok || !res.data) {
            const d = res.data || {};
            bomShowError(d.error || t('bom.err.failed'), d.details || []);
            if (d.retainable && shareable(bomState.file, null)) {
                bomShowRetainOffer(true, [d.error, d.hint].filter(Boolean).join(' — '));
            }
            return;
        }
        if (res.data.job) {
            startAgentWait(res.data.job);
            loadBomChecks();
            return;
        }
        showCheck(res.data, true);
        loadBomChecks();
    }

    function showCheck(check, fromUpload) {
        bomState.current = check;
        if (fromUpload) bomState.uploadedCheckId = check.id;
        renderResult(check);
        showStep('result');
        // A file the agent had to read: ask to share it so the layout can be
        // taught to the parsers (only while we still hold the file).
        if (check.agent && bomState.file && bomState.uploadedCheckId === check.id
                && shareable(bomState.file, check.agent)) {
            bomShowRetainOffer(true, 'agent-read: ' + (check.filename || ''), 'bom.retain.offer_agent');
        }
    }

    // ── waiting on the Claude agent ──────────────────────────────────────────

    function stopPolling() {
        if (bomState.pollTimer) clearTimeout(bomState.pollTimer);
        bomState.pollTimer = null;
        bomState.jobId = null;
    }

    function startAgentWait(job) {
        stopPolling();
        bomState.jobId = job.id;
        const box = $('bom-agent-notify');
        if (box) box.checked = !!job.notify_email;
        showStep('agent');
        bomState.pollTimer = setTimeout(pollJob, POLL_MS);
    }

    async function pollJob() {
        const id = bomState.jobId;
        if (!id) return;
        let res;
        try {
            res = await api(`/api/bom-agent-jobs/${Number(id)}`);
        } catch (e) {
            res = { ok: false };
        }
        if (bomState.jobId !== id) return;          // closed or replaced meanwhile
        const job = res.ok ? res.data : null;
        if (job && job.status === 'done' && job.check_id) {
            stopPolling();
            const r = await api(`/api/bom-checks/${Number(job.check_id)}`);
            loadBomChecks();
            if (r.ok && r.data) {
                bomState.uploadedCheckId = r.data.id;
                showCheck(r.data, false);
            }
            return;
        }
        if (job && job.status === 'failed') {
            stopPolling();
            showStep('upload');
            bomShowError(job.error || t('bom.agent.failed'), []);
            if (job.has_template) showJobTemplateLink(job);
            if (bomState.file && shareable(bomState.file, null)) {
                bomShowRetainOffer(true, 'agent failed: ' + (job.error || ''));
            }
            loadBomChecks();
            return;
        }
        bomState.pollTimer = setTimeout(pollJob, POLL_MS);
    }

    function showJobTemplateLink(job) {
        const list = $('bom-error-details');
        if (!list) return;
        list.innerHTML = `<li><a href="/api/bom-agent-jobs/${Number(job.id)}/template" download>${escHtml(t('bom.agent.download_template'))}</a> — ${escHtml(t('bom.agent.template_fix_hint'))}</li>`;
        list.hidden = false;
    }

    async function bomAgentNotify(input) {
        const id = bomState.jobId;
        if (!id || !input) return;
        await api(`/api/bom-agent-jobs/${Number(id)}/notify`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ notify: !!input.checked }),
        });
    }

    function agentBanner(check) {
        const a = check.agent;
        if (!a) return '';
        const lines = [`<p class="bom-agent-banner-title">${escHtml(t('bom.agent.banner'))}</p>`];
        if (a.pdf_certainty !== null && a.pdf_certainty !== undefined) {
            // A PDF layout we parse locally, not trusted this time: say why.
            lines.push(`<p>${escHtml(t('bom.agent.low_certainty', { score: a.pdf_certainty }))}</p>`);
            if ((a.pdf_reasons || []).length) {
                lines.push(`<ul class="bom-agent-dropped">${a.pdf_reasons.map(r => `<li>${escHtml(r)}</li>`).join('')}</ul>`);
            }
        }
        if (a.hidden_words) {
            lines.push(`<p class="bom-agent-alert">${escHtml(t('bom.agent.hidden_words', { n: a.hidden_words }))}</p>`);
        }
        if (!a.grounded) {
            lines.push(`<p>${escHtml(t('bom.agent.not_grounded'))}</p>`);
        }
        if (a.instructions_detected) {
            lines.push(`<p class="bom-agent-alert">${escHtml(t('bom.agent.instructions'))}</p>`);
        }
        const dropped = (a.dropped || []).concat(a.model_changed || []);
        if (dropped.length) {
            lines.push(`<p>${escHtml(t('bom.agent.dropped', { n: dropped.length }))}</p>
                <ul class="bom-agent-dropped">${dropped.map(d => `<li>${escHtml(d)}</li>`).join('')}</ul>`);
        }
        lines.push(`<p class="bom-agent-actions">
            <a class="btn btn-soft btn-xs" href="/api/bom-checks/${Number(check.id)}/template" download>${escHtml(t('bom.agent.download_template'))}</a>
            <span class="field-hint">${escHtml(t('bom.agent.template_hint'))}</span></p>`);
        return `<div class="bom-agent-banner" role="note">${lines.join('')}</div>`;
    }

    // ── result step ──────────────────────────────────────────────────────────

    function configResultsOf(result) {
        const tech = (result && result.technical) || {};
        return pick(tech, 'config_results', 'configResults') || [];
    }

    function configNameOf(obj) {
        return pick(obj, 'config_name', 'configName') || '';
    }

    function platformLine(platform) {
        if (!platform) return `<span class="bom-platform bom-platform-unknown">${escHtml(t('bom.result.platform_unknown'))}</span>`;
        const models = Array.isArray(platform.sc_models) ? platform.sc_models.join(', ') : (platform.sc_model || '');
        const bits = [];
        if (models) bits.push(escHtml(t('bom.result.platform_identified', { models })));
        if (platform.server) bits.push(escHtml(platform.server));
        if (platform.form_factor) bits.push(escHtml(platform.form_factor));
        if (!bits.length) return `<span class="bom-platform bom-platform-unknown">${escHtml(t('bom.result.platform_unknown'))}</span>`;
        return `<span class="bom-platform">${bits.join(' · ')}</span>`;
    }

    function findingsTable(findings) {
        const rows = (findings || []).slice().sort((a, b) => {
            const ra = SEVERITY_RANK[String(a.severity).toLowerCase()];
            const rb = SEVERITY_RANK[String(b.severity).toLowerCase()];
            return (ra === undefined ? 9 : ra) - (rb === undefined ? 9 : rb);
        });
        if (!rows.length) return `<p class="import-info muted bom-no-findings">${escHtml(t('bom.result.no_findings'))}</p>`;
        const body = rows.map(f => {
            const sev = String(f.severity || 'info').toLowerCase();
            const cls = SEVERITY_CLASS[sev] || 'badge-neutral';
            const label = SEVERITY_CLASS[sev] ? t('bom.severity.' + sev) : sev;
            return `<tr>
                <td class="bom-col-sev"><span class="state-badge ${cls}">${escHtml(label)}</span></td>
                <td class="bom-col-comp">${escHtml(f.component || '')}</td>
                <td>${escHtml(f.issue || '')}</td>
                <td class="bom-col-fix">${escHtml(f.remediation || '')}</td>
            </tr>`;
        }).join('');
        return `<div class="compare-scroll"><table class="sizing-table bom-findings">
            <thead><tr>
                <th>${escHtml(t('bom.table.severity'))}</th>
                <th>${escHtml(t('bom.table.component'))}</th>
                <th>${escHtml(t('bom.table.issue'))}</th>
                <th>${escHtml(t('bom.table.remediation'))}</th>
            </tr></thead>
            <tbody>${body}</tbody>
        </table></div>`;
    }

    function candidateLine(c) {
        const bits = [];
        if (c.part_number) bits.push(`<code class="sizing-code">${escHtml(c.part_number)}</code>`);
        if (c.description) bits.push(escHtml(c.description));
        const tags = [];
        if (c.tce) tags.push(`<span class="state-badge badge-accent" title="${escHtml(t('bom.suggest.tce_title'))}">${escHtml(t('bom.suggest.tce'))}</span>`);
        if (c.speed_gbe) tags.push(`<span class="state-badge badge-neutral">${escHtml(fmtNum(c.speed_gbe))} ${escHtml(t('bom.unit.gbe'))}</span>`);
        if (c.ports) tags.push(`<span class="state-badge badge-neutral">${escHtml(t('bom.suggest.ports', { n: fmtNum(c.ports) }))}</span>`);
        if (c.form_factor) tags.push(`<span class="state-badge badge-neutral">${escHtml(c.form_factor)}</span>`);
        return `<li><span class="bom-cand-main">${bits.join(' ')}</span><span class="bom-cand-tags">${tags.join('')}</span></li>`;
    }

    function suggestionCards(suggestions) {
        if (!suggestions || !suggestions.length) return '';
        return `<div class="bom-suggestions">
            <h4 class="rep-heading">${escHtml(t('bom.suggest.heading'))}</h4>
            ${suggestions.map(s => `<div class="bom-suggest-card">
                <div class="bom-suggest-title">${escHtml(t('bom.suggest.instead_of', { component: s.component || '' }))}</div>
                ${(s.candidates && s.candidates.length)
                    ? `<ul class="bom-cand-list">${s.candidates.map(candidateLine).join('')}</ul>`
                    : `<p class="import-info muted">${escHtml(t('bom.suggest.none'))}</p>`}
            </div>`).join('')}
        </div>`;
    }

    function relationBadge(dim) {
        const rel = String(dim.relation || 'unknown').toLowerCase();
        const cls = RELATION_CLASS[rel] || 'badge-neutral';
        const unit = unitLabel(dim.unit);
        const delta = dim.delta_vs_sized;
        const hasDelta = delta !== null && delta !== undefined && !isNaN(Number(delta));
        const abs = hasDelta ? fmtNum(Math.abs(Number(delta))) + (unit ? ' ' + unit : '') : '';
        let label;
        if (rel === 'equal') label = t('bom.rel.equal');
        else if (rel === 'bigger') label = hasDelta ? t('bom.rel.bigger', { delta: '+' + abs }) : t('bom.rel.bigger_plain');
        else if (rel === 'fits') label = hasDelta ? t('bom.rel.fits', { delta: '−' + abs }) : t('bom.rel.fits_plain');
        else if (rel === 'smaller') label = t('bom.rel.smaller');
        else label = t('bom.rel.unknown');
        return `<span class="state-badge bom-rel ${cls}">${escHtml(label)}</span>`;
    }

    function valueCell(v, unit) {
        if (v === null || v === undefined) return '<td class="bom-num bom-num-empty">—</td>';
        const u = unitLabel(unit);
        return `<td class="bom-num">${escHtml(fmtNum(v))}${u ? ` <span class="bom-unit">${escHtml(u)}</span>` : ''}</td>`;
    }

    function fitSection(fit) {
        if (!fit) return '';
        const sizing = fit.sizing || {};
        const dims = Array.isArray(fit.dimensions) ? fit.dimensions : [];
        const rows = dims.map(d => `<tr class="bom-rel-${escHtml(String(d.relation || 'unknown'))}">
            <td class="bom-col-dim">${escHtml(dimLabel(d.key))}</td>
            <td>${relationBadge(d)}</td>
            ${valueCell(d.bom, d.unit)}
            ${valueCell(d.required, d.unit)}
            ${valueCell(d.sized, d.unit)}
            <td class="bom-col-note">${escHtml(d.note || '')}</td>
        </tr>`).join('');
        const notes = Array.isArray(fit.notes) && fit.notes.length
            ? `<ul class="bom-fit-notes">${fit.notes.map(n => `<li>${escHtml(n)}</li>`).join('')}</ul>` : '';
        const compared = configNameOf(fit);
        const intro = t('bom.fit.intro', { sizing: sizing.name || '', config: compared || '—' });
        return `<section class="bom-section bom-fit">
            <h3 class="rep-heading">${escHtml(t('bom.fit.heading'))}</h3>
            <p class="import-info">${escHtml(intro)} ${fitChip(fit.verdict, sizing.name)}</p>
            ${dims.length ? `<div class="compare-scroll"><table class="sizing-table bom-dims">
                <thead><tr>
                    <th>${escHtml(t('bom.fit.col_dimension'))}</th>
                    <th>${escHtml(t('bom.fit.col_relation'))}</th>
                    <th class="bom-num">${escHtml(t('bom.fit.col_bom'))}</th>
                    <th class="bom-num">${escHtml(t('bom.fit.col_required'))}</th>
                    <th class="bom-num">${escHtml(t('bom.fit.col_sized'))}</th>
                    <th>${escHtml(t('bom.fit.col_note'))}</th>
                </tr></thead>
                <tbody>${rows}</tbody>
            </table></div>` : `<p class="import-info muted">${escHtml(t('bom.fit.no_dimensions'))}</p>`}
            ${notes}
        </section>`;
    }

    function reviewLine(check) {
        const status = String(check.review_status || 'none');
        if (status === 'none') return '';
        if (status === 'open') {
            return `<p class="bom-review bom-review-open"><span class="state-badge badge-warn">${escHtml(t('bom.review.open'))}</span> ${escHtml(t('bom.review.open_hint'))}</p>`;
        }
        const cls = status === 'confirmed' ? 'badge-pass' : 'badge-fail';
        const label = status === 'confirmed' ? t('bom.review.confirmed') : t('bom.review.incorrect');
        const note = check.review_note ? ` — ${escHtml(check.review_note)}` : '';
        const when = check.reviewed_at ? ` <span class="muted">(${escHtml(fmtWhen(check.reviewed_at))})</span>` : '';
        return `<p class="bom-review"><span class="state-badge ${cls}">${escHtml(t('bom.review.reviewed'))}: ${escHtml(label)}</span>${note}${when}</p>`;
    }

    function renderResult(check) {
        const host = $('bom-result');
        if (!host || !check) return;
        const result = check.result || {};
        const tech = result.technical || {};
        const techVerdict = check.technical_verdict || tech.verdict;
        const fit = result.fit || null;
        const suggestions = Array.isArray(result.suggestions) ? result.suggestions : [];

        const metaBits = [];
        metaBits.push(escHtml(formatLabel(check.file_format)));
        if (check.pdf && check.pdf.score !== null && check.pdf.score !== undefined) {
            metaBits.push(escHtml(t('bom.result.pdf_certainty', { score: check.pdf.score })));
        }
        if (check.vendor) metaBits.push(escHtml(check.vendor));
        if (check.filename && check.filename !== check.name) metaBits.push(escHtml(check.filename));
        const when = check.checked_at || (result.checked_at) || check.created_at;
        if (when) metaBits.push(escHtml(t('bom.result.checked_at', { when: fmtWhen(when) })));

        const configs = configResultsOf(result);
        const configBlocks = configs.map(cr => {
            const name = configNameOf(cr);
            const mine = suggestions.filter(s => configNameOf(s) === name);
            return `<section class="bom-section bom-config">
                <div class="bom-config-head">
                    <h3 class="bom-config-name">${escHtml(name || t('bom.result.config_unnamed'))}</h3>
                    ${verdictChip(cr.verdict)}
                </div>
                ${platformLine(cr.platform)}
                ${findingsTable(cr.findings)}
                ${suggestionCards(mine)}
            </section>`;
        }).join('');

        // Suggestions that name a config we did not render (defensive).
        const rendered = new Set(configs.map(configNameOf));
        const orphan = suggestions.filter(s => !rendered.has(configNameOf(s)));

        host.innerHTML = `
            ${agentBanner(check)}
            <div class="bom-result-head">
                <div class="bom-result-title">
                    <h3>${escHtml(check.name || check.filename || '')}</h3>
                    <div class="bom-result-meta">${metaBits.join(' · ')}</div>
                </div>
                <div class="bom-result-chips">
                    <span class="bom-chip-label">${escHtml(t('bom.result.technical'))}</span>${verdictChip(techVerdict)}
                    ${fit ? `<span class="bom-chip-label">${escHtml(t('bom.result.fit'))}</span>${fitChip(check.fit_verdict || fit.verdict, '')}` : ''}
                </div>
            </div>
            ${configBlocks || `<p class="import-info muted">${escHtml(t('bom.result.no_configs'))}</p>`}
            ${orphan.length ? `<section class="bom-section">${suggestionCards(orphan)}</section>` : ''}
            ${fitSection(fit)}
            ${reviewLine(check)}
        `;
        bomState.sizingId = check.configuration_id ? String(check.configuration_id) : '';
        renderRecheckSelect();
    }

    // ── project page: BOM checks section + header badge ─────────────────────

    async function loadBomChecks() {
        if (!currentProject) return;
        const pid = currentProject.id;
        const [checks, jobs] = await Promise.all([
            api(`/api/projects/${pid}/bom-checks`),
            api(`/api/projects/${pid}/bom-agent-jobs`),
        ]);
        if (!currentProject || currentProject.id !== pid) return;
        // A missing backend (404) or a transient error: keep the UI usable.
        bomState.checks = (checks.ok && Array.isArray(checks.data)) ? checks.data : [];
        bomState.jobs = (jobs.ok && Array.isArray(jobs.data)) ? jobs.data : [];
        bomState.checksProjectId = pid;
        renderBomSection();
        updateBadge();
        scheduleListRefresh();
    }

    // While an agent job is still running, refresh the section now and then
    // so its result appears without a reload, even with the modal closed.
    function scheduleListRefresh() {
        if (bomState.listTimer) clearTimeout(bomState.listTimer);
        bomState.listTimer = null;
        const pending = bomState.jobs.some(j => j.status === 'queued' || j.status === 'running');
        if (!pending) return;
        const pid = bomState.checksProjectId;
        bomState.listTimer = setTimeout(() => {
            const view = $('project-view');
            if (currentProject && currentProject.id === pid && view && !view.hidden) loadBomChecks();
        }, LIST_REFRESH_MS);
    }

    function updateBadge() {
        const badge = $('bom-badge');
        if (!badge) return;
        const n = bomState.checks.length;
        badge.hidden = n === 0;
        badge.textContent = n;
    }

    function checkRow(c) {
        const when = fmtWhen(c.checked_at || c.created_at);
        const sizing = c.sizing_name
            ? t('bom.history.against', { sizing: c.sizing_name })
            : t('bom.history.compat_only');
        const review = c.review_status === 'open'
            ? `<span class="state-badge badge-warn bom-chip-sm">${escHtml(t('bom.review.open'))}</span>` : '';
        const agent = c.agent
            ? `<span class="state-badge badge-accent bom-chip-sm" title="${escHtml(t('bom.agent.banner'))}">${escHtml(t('bom.agent.chip'))}</span>` : '';
        return `<li class="bom-row">
            <button class="bom-row-open" data-click='["openBomCheckResult",${Number(c.id)}]'>
                <span class="bom-row-name">${escHtml(c.name || c.filename || '')}</span>
                <span class="bom-row-chips">${verdictChip(c.technical_verdict)}${c.fit_verdict ? fitChip(c.fit_verdict, '') : ''}${agent}${review}</span>
                <span class="bom-row-meta">${escHtml(sizing)} · ${escHtml(when)}</span>
            </button>
        </li>`;
    }

    function jobRow(j) {
        if (j.status === 'failed') {
            const tpl = j.has_template
                ? ` <a href="/api/bom-agent-jobs/${Number(j.id)}/template" download>${escHtml(t('bom.agent.download_template'))}</a>` : '';
            return `<li class="bom-row bom-row-failed">
                <span class="bom-row-name">${escHtml(j.name || j.filename || '')}</span>
                <span class="bom-row-chips"><span class="state-badge badge-fail bom-chip-sm">${escHtml(t('bom.agent.failed_chip'))}</span></span>
                <span class="bom-row-meta">${escHtml(j.error || t('bom.agent.failed'))}${tpl}</span>
            </li>`;
        }
        return `<li class="bom-row bom-row-pending">
            <span class="bom-row-name">${escHtml(j.name || j.filename || '')}</span>
            <span class="bom-row-chips"><span class="bom-spinner bom-spinner-sm" aria-hidden="true"></span>
                <span class="state-badge badge-neutral bom-chip-sm">${escHtml(t('bom.agent.reading_chip'))}</span></span>
            <span class="bom-row-meta">${escHtml(fmtWhen(j.created_at))}</span>
        </li>`;
    }

    function renderBomSection() {
        const host = $('project-bom-checks');
        if (!host) return;
        const mine = currentProject && bomState.checksProjectId === currentProject.id;
        const checks = mine ? bomState.checks : [];
        const jobs = mine ? bomState.jobs : [];
        if (!checks.length && !jobs.length) { host.innerHTML = ''; return; }
        host.innerHTML = `<h3 class="rep-heading">${escHtml(t('bom.section.title'))}</h3>
            <ul class="bom-list">${jobs.map(jobRow).join('')}${checks.map(checkRow).join('')}</ul>`;
    }

    async function openBomCheckResult(id) {
        if (!currentProject || bomState.busy) return;
        stopPolling();
        const host = $('bom-result');
        if (host) host.innerHTML = `<p class="project-empty">${escHtml(t('bom.loading'))}</p>`;
        renderSizingPicker();
        showStep('result');
        showModal();
        // formatLabel() reads capabilities.formats; make sure they are in
        // before the header renders (cached after the first load).
        await loadCapabilities();
        const { ok, data } = await api(`/api/bom-checks/${Number(id)}`);
        if (!ok || !data) {
            bomShowError((data && data.error) || t('bom.err.load_failed'), []);
            if (host) host.innerHTML = '';
            return;
        }
        showCheck(data, false);
    }

    // ── re-check / delete ────────────────────────────────────────────────────

    function bomRecheckSizingChanged(value) {
        bomState.sizingId = value == null ? '' : String(value);
    }

    async function bomRecheck() {
        const check = bomState.current;
        if (!check || bomState.busy) return;
        bomClearError();
        setBusy(true);
        const sel = $('bom-recheck-sizing');
        const sizingId = sel ? sel.value : bomState.sizingId;
        const body = { sizing_id: sizingId ? Number(sizingId) : null };
        const { ok, data } = await api(`/api/bom-checks/${check.id}/recheck`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        });
        setBusy(false);
        if (!ok || !data) {
            bomShowError((data && data.error) || t('bom.err.recheck_failed'), (data && data.details) || []);
            return;
        }
        bomState.current = data;
        renderResult(data);
        if (window.toast) window.toast(t('bom.rechecked'), 'success');
        loadBomChecks();
    }

    async function bomDelete() {
        const check = bomState.current;
        if (!check || bomState.busy) return;
        if (!window.confirm(t('bom.confirm_delete', { name: check.name || '' }))) return;
        setBusy(true);
        const { ok, data } = await api(`/api/bom-checks/${check.id}`, { method: 'DELETE' });
        setBusy(false);
        if (!ok) {
            bomShowError((data && data.error) || t('bom.err.delete_failed'), []);
            return;
        }
        bomState.current = null;
        if (window.toast) window.toast(t('bom.deleted'), 'success');
        await loadBomChecks();
        closeBomChecker();
    }

    Object.assign(window, {
        openBomChecker, closeBomChecker, bomBack,
        bomPickSizing, bomFileChosen, bomRunCheck,
        bomShareRejected, bomDismissRetain, bomAgentNotify,
        openBomCheckResult, bomRecheck, bomRecheckSizingChanged, bomDelete,
        loadBomChecks,
    });
})();
