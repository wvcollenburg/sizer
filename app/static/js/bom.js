/* BOM checker — project-page UI (docs/bom-checker-build.md §7).
 *
 * Pure view layer over:
 *   GET  /api/bom/capabilities            (cached per page load)
 *   GET  /api/bom/template                (plain <a download> in the markup)
 *   POST /api/bom/prefill                 (AI pre-fill, only when capabilities allow)
 *   GET  /api/projects/<id>/bom-checks    (history list + header badge)
 *   POST /api/projects/<id>/bom-checks    (multipart: file, sizing_id?, name?)
 *   GET  /api/bom-checks/<id>             (full result)
 *   POST /api/bom-checks/<id>/recheck     (json {sizing_id})
 *   DELETE /api/bom-checks/<id>
 *
 * Classic script sharing one global scope with projects.js — everything lives
 * inside this IIFE and only the delegate handlers are exported (all carrying a
 * "bom"/"Bom" name so they cannot collide). Helpers that projects.js already
 * defines (api, escHtml, tt, info, _downloadBlob, currentProject) are CALLED,
 * never redeclared. Result payload keys are read defensively in both the §5
 * snake_case spelling and the camelCase spelling normalize.py emits
 * (config_results / configResults, config_name / configName).
 */
(function () {
    'use strict';

    const bomState = {
        capabilities: null,     // /api/bom/capabilities payload, cached per page load
        capsPromise: null,
        checks: [],             // summary rows for the open project
        checksProjectId: null,
        current: null,          // full check shown in the result step
        file: null,             // File chosen for the check
        prefillFile: null,      // File chosen for the AI pre-fill
        sizingId: '',           // '' = compatibility only; otherwise sizing id as string
        dropBound: false,
        busy: false,
    };

    const DEFAULT_ACCEPT = ['.xlsx', '.csv'];
    const DEFAULT_PREFILL_ACCEPT = ['.xlsx', '.csv', '.pdf', '.docx', '.txt'];
    const SEVERITY_RANK = { error: 0, warning: 1, info: 2 };
    const VERDICT_CLASS = { PASS: 'badge-pass', FAIL: 'badge-fail', INCONCLUSIVE: 'badge-warn' };
    const FIT_CLASS = { match: 'badge-pass', bigger: 'badge-info', fits: 'badge-info', smaller: 'badge-fail', unknown: 'badge-neutral' };
    const RELATION_CLASS = { equal: 'badge-pass', bigger: 'badge-info', fits: 'badge-info', smaller: 'badge-fail', unknown: 'badge-neutral' };
    const SEVERITY_CLASS = { error: 'badge-fail', warning: 'badge-warn', info: 'badge-info' };
    const KNOWN_FORMATS = ['lenovo_dcsc', 'dell_service_tag', 'dell_quote', 'template'];
    const KNOWN_DIMS = ['nodes', 'cores', 'compute', 'ram', 'largest_vm', 'storage', 'nic'];
    const UNIT_KEYS = { nodes: 'nodes', cores: 'cores', GB: 'gb', TB: 'tb', GbE: 'gbe', '%': 'pct' };

    // Local alias so string literals are statically discoverable by the i18n
    // parity gate (`t('bom.…')`); resolves through the page's translator.
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

    function bomClearError() { bomShowError('', []); }

    function setStatus(msg, isError) {
        const el = $('bom-upload-status');
        if (!el) return;
        el.textContent = msg || '';
        el.className = 'upload-status ' + (isError ? 'upload-error' : 'upload-ok');
        el.style.display = msg ? 'block' : 'none';
    }

    function setBusy(on) {
        bomState.busy = !!on;
        const run = $('bom-run-btn');
        const recheck = $('bom-recheck-btn');
        const del = $('bom-delete-btn');
        if (run) run.disabled = !!on;
        if (recheck) recheck.disabled = !!on;
        if (del) del.disabled = !!on;
        const modal = $('bom-modal');
        if (modal) modal.classList.toggle('bom-busy', !!on);
    }

    // ── capabilities ─────────────────────────────────────────────────────────

    function loadCapabilities() {
        if (bomState.capabilities) return Promise.resolve(bomState.capabilities);
        if (bomState.capsPromise) return bomState.capsPromise;
        bomState.capsPromise = api('/api/bom/capabilities').then(({ ok, data }) => {
            bomState.capabilities = (ok && data) ? data : {
                ai_prefill_available: false,
                accepted_extensions: DEFAULT_ACCEPT,
                prefill_extensions: DEFAULT_PREFILL_ACCEPT,
            };
            bomState.capsPromise = null;
            return bomState.capabilities;
        }).catch(() => {
            bomState.capsPromise = null;
            return { ai_prefill_available: false, accepted_extensions: DEFAULT_ACCEPT };
        });
        return bomState.capsPromise;
    }

    function acceptedExtensions() {
        const caps = bomState.capabilities || {};
        const list = Array.isArray(caps.accepted_extensions) && caps.accepted_extensions.length
            ? caps.accepted_extensions : DEFAULT_ACCEPT;
        return list.map(e => String(e).toLowerCase());
    }

    function prefillExtensions() {
        const caps = bomState.capabilities || {};
        const list = Array.isArray(caps.prefill_extensions) && caps.prefill_extensions.length
            ? caps.prefill_extensions : DEFAULT_PREFILL_ACCEPT;
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
        if (hint) hint.textContent = t('bom.upload.accept_hint', { formats: acceptedExtensions().join(', ') });
        const panel = $('bom-prefill-panel');
        if (panel) {
            panel.hidden = !caps.ai_prefill_available;
            const pin = $('bom-prefill-input');
            if (pin) pin.setAttribute('accept', prefillExtensions().join(','));
        }
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
        const upload = $('bom-step-upload');
        const result = $('bom-step-result');
        if (upload) upload.style.display = step === 'upload' ? '' : 'none';
        if (result) result.style.display = step === 'result' ? '' : 'none';
        const show = (id, on) => { const el = $(id); if (el) el.style.display = on ? '' : 'none'; };
        show('bom-run-btn', step === 'upload');
        show('bom-back-btn', step === 'result');
        show('bom-recheck-btn', step === 'result');
        show('bom-delete-btn', step === 'result');
        show('bom-recheck-wrap', step === 'result');
        bomClearError();
    }

    // ── sizing picker ────────────────────────────────────────────────────────

    function sizingOptions() {
        return (currentProject && currentProject.sizings) ? currentProject.sizings : [];
    }

    function renderSizingPicker() {
        const host = $('bom-sizing-pick');
        if (!host) return;
        const rows = [];
        const noneChecked = bomState.sizingId === '' ? ' checked' : '';
        rows.push(`<label class="fanout-row bom-pick-row">
            <input type="radio" name="bom-sizing" value=""${noneChecked} data-change='["bomPickSizing","$value"]'>
            <span class="fanout-name">${escHtml(t('bom.pick.none'))}</span>
            <span class="fanout-meta">${escHtml(t('bom.pick.none_hint'))}</span>
        </label>`);
        sizingOptions().forEach(s => {
            const enabled = !!s.has_result;
            const checked = String(s.id) === String(bomState.sizingId) ? ' checked' : '';
            const meta = [];
            if (s.role) meta.push(escHtml(tt('project.role.' + s.role)));
            if (s.is_dr_target) meta.push(escHtml(tt('project.table.dr_target')));
            if (!enabled) meta.push(escHtml(t('bom.pick.no_result')));
            rows.push(`<label class="fanout-row bom-pick-row${enabled ? '' : ' bom-pick-disabled'}">
                <input type="radio" name="bom-sizing" value="${s.id}"${checked}${enabled ? '' : ' disabled'} data-change='["bomPickSizing","$value"]'>
                <span class="fanout-name">${escHtml(s.name)}</span>
                <span class="fanout-meta">${meta.join(' · ')}</span>
            </label>`);
        });
        host.innerHTML = rows.join('');
        renderRecheckSelect();
    }

    function renderRecheckSelect() {
        const sel = $('bom-recheck-sizing');
        if (!sel) return;
        const opts = [`<option value="">${escHtml(t('bom.pick.none'))}</option>`];
        sizingOptions().forEach(s => {
            const enabled = !!s.has_result;
            opts.push(`<option value="${s.id}"${enabled ? '' : ' disabled'}>${escHtml(s.name)}${enabled ? '' : ' — ' + escHtml(t('bom.pick.no_result'))}</option>`);
        });
        sel.innerHTML = opts.join('');
        sel.value = String(bomState.sizingId || '');
        if (sel.value !== String(bomState.sizingId || '')) sel.value = '';
    }

    function bomPickSizing(value) {
        bomState.sizingId = value == null ? '' : String(value);
        const sel = $('bom-recheck-sizing');
        if (sel) sel.value = bomState.sizingId;
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
        setStatus('', false);
        bomClearError();
        showFileName();
        const nameInput = $('bom-name');
        if (nameInput && !nameInput.value) nameInput.placeholder = file.name;
    }

    function showFileName() {
        const el = $('bom-file-name');
        if (!el) return;
        if (bomState.file) {
            el.textContent = bomState.file.name;
            el.hidden = false;
        } else {
            el.textContent = '';
            el.hidden = true;
        }
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
        area.addEventListener('click', () => { if (!bomState.busy) input.click(); });
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

    function openBomChecker() {
        if (!currentProject) return;
        bomState.file = null;
        bomState.prefillFile = null;
        bomState.current = null;
        bomState.sizingId = '';
        const fi = $('bom-file-input');
        if (fi) fi.value = '';
        const pi = $('bom-prefill-input');
        if (pi) pi.value = '';
        const pn = $('bom-prefill-name');
        if (pn) { pn.textContent = ''; pn.hidden = true; }
        const nameInput = $('bom-name');
        if (nameInput) { nameInput.value = ''; nameInput.placeholder = ''; }
        showFileName();
        setStatus('', false);
        setBusy(false);
        renderSizingPicker();
        renderHistory();
        showStep('upload');
        bindDropZone();
        const modal = $('bom-modal');
        if (modal) modal.style.display = 'flex';
        loadCapabilities().then(applyCapabilities);
        loadBomChecks();
    }

    function closeBomChecker() {
        const modal = $('bom-modal');
        if (modal) modal.style.display = 'none';
        bomState.current = null;
    }

    function bomBack() {
        bomState.current = null;
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
            return;
        }
        bomState.current = res.data;
        renderResult(res.data);
        showStep('result');
        loadBomChecks();
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
        const sizingName = check.sizing_name || (fit && fit.sizing && fit.sizing.name) || '';

        const metaBits = [];
        metaBits.push(escHtml(formatLabel(check.file_format)));
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
            <div class="bom-result-head">
                <div class="bom-result-title">
                    <h3>${escHtml(check.name || check.filename || '')}</h3>
                    <div class="bom-result-meta">${metaBits.join(' · ')}</div>
                </div>
                <div class="bom-result-chips">
                    <span class="bom-chip-label">${escHtml(t('bom.result.technical'))}</span>${verdictChip(techVerdict)}
                    ${fit ? `<span class="bom-chip-label">${escHtml(t('bom.result.fit'))}</span>${fitChip(check.fit_verdict || fit.verdict, sizingName)}` : ''}
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

    // ── history + badge ──────────────────────────────────────────────────────

    async function loadBomChecks() {
        if (!currentProject) return;
        const pid = currentProject.id;
        const { ok, data } = await api(`/api/projects/${pid}/bom-checks`);
        if (!currentProject || currentProject.id !== pid) return;
        if (!ok) {
            // A missing backend (404) or a transient error: keep the UI usable.
            bomState.checks = [];
            bomState.checksProjectId = pid;
            renderHistory();
            updateBadge();
            return;
        }
        bomState.checks = Array.isArray(data) ? data : [];
        bomState.checksProjectId = pid;
        renderHistory();
        updateBadge();
    }

    function updateBadge() {
        const badge = $('bom-badge');
        if (!badge) return;
        const n = bomState.checks.length;
        badge.hidden = n === 0;
        badge.textContent = n;
    }

    function historyRow(c) {
        const when = fmtWhen(c.checked_at || c.created_at);
        const sizing = c.sizing_name
            ? `<span class="export-note">${escHtml(t('bom.history.against', { sizing: c.sizing_name }))}</span>`
            : `<span class="export-note">${escHtml(t('bom.history.compat_only'))}</span>`;
        const review = c.review_status === 'open'
            ? `<span class="state-badge badge-warn bom-chip-sm">${escHtml(t('bom.review.open'))}</span>` : '';
        const openLabel = escHtml(t('bom.history.open'));
        return `<li>
            <span class="bom-history-main">
                <span class="bom-history-name">${escHtml(c.name || c.filename || '')}</span>
                ${verdictChip(c.technical_verdict)}${c.fit_verdict ? fitChip(c.fit_verdict, '') : ''}${review}
            </span>
            ${sizing}
            <span class="export-note">${escHtml(when)}</span>
            <button class="btn btn-soft btn-xs" data-click='["openBomCheckResult",${Number(c.id)}]' aria-label="${openLabel} ${escHtml(c.name || '')}">${openLabel}</button>
        </li>`;
    }

    function renderHistory() {
        const host = $('bom-history-list');
        const empty = $('bom-history-empty');
        if (!host) return;
        const rows = (currentProject && bomState.checksProjectId === currentProject.id) ? bomState.checks : [];
        host.innerHTML = rows.map(historyRow).join('');
        if (empty) empty.hidden = rows.length > 0;
    }

    async function openBomCheckResult(id) {
        if (!currentProject || bomState.busy) return;
        const host = $('bom-result');
        if (host) host.innerHTML = `<p class="project-empty">${escHtml(t('bom.loading'))}</p>`;
        showStep('result');
        const { ok, data } = await api(`/api/bom-checks/${Number(id)}`);
        if (!ok || !data) {
            bomShowError((data && data.error) || t('bom.err.load_failed'), []);
            if (host) host.innerHTML = '';
            return;
        }
        bomState.current = data;
        renderResult(data);
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
        bomBack();
    }

    // ── AI pre-fill (Tier 3) ─────────────────────────────────────────────────

    function bomPrefillFileChosen(input) {
        const el = $('bom-prefill-name');
        if (!input || !input.files || !input.files.length) {
            bomState.prefillFile = null;
            if (el) { el.textContent = ''; el.hidden = true; }
            return;
        }
        const f = input.files[0];
        if (!hasExtension(f.name, prefillExtensions())) {
            bomState.prefillFile = null;
            bomShowError(t('bom.prefill.bad_extension', { formats: prefillExtensions().join(', ') }), []);
            if (el) { el.textContent = ''; el.hidden = true; }
            return;
        }
        bomClearError();
        bomState.prefillFile = f;
        if (el) { el.textContent = f.name; el.hidden = false; }
    }

    function filenameFromDisposition(resp, fallback) {
        const cd = resp.headers && resp.headers.get ? resp.headers.get('Content-Disposition') : null;
        if (!cd) return fallback;
        const m = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(cd);
        return m ? decodeURIComponent(m[1]) : fallback;
    }

    async function bomRunPrefill() {
        if (bomState.busy) return;
        bomClearError();
        if (!bomState.prefillFile) {
            bomShowError(t('bom.prefill.no_file'), []);
            return;
        }
        const btn = $('bom-prefill-btn');
        const status = $('bom-prefill-status');
        if (btn) btn.disabled = true;
        if (status) { status.textContent = t('bom.prefill.working'); status.hidden = false; }
        const fd = new FormData();
        fd.append('file', bomState.prefillFile);
        try {
            const resp = await fetch('/api/bom/prefill', { method: 'POST', body: fd, credentials: 'same-origin' });
            if (!resp.ok) {
                let msg = t('bom.prefill.failed');
                try { const j = await resp.json(); if (j && j.error) msg = j.error; } catch (e) { /* not json */ }
                bomShowError(msg, []);
                if (status) status.hidden = true;
                return;
            }
            const blob = await resp.blob();
            _downloadBlob(blob, filenameFromDisposition(resp, 'sc-bom-template-prefilled.xlsx'));
            if (status) { status.textContent = t('bom.prefill.done'); status.hidden = false; }
        } catch (e) {
            bomShowError(t('bom.err.network', { error: e.message || String(e) }), []);
            if (status) status.hidden = true;
        } finally {
            if (btn) btn.disabled = false;
        }
    }

    Object.assign(window, {
        openBomChecker, closeBomChecker, bomBack,
        bomPickSizing, bomFileChosen, bomRunCheck,
        openBomCheckResult, bomRecheck, bomRecheckSizingChanged, bomDelete,
        bomPrefillFileChosen, bomRunPrefill,
        loadBomChecks,
    });
})();
