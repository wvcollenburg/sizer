let allModels = [];
let cpuCatalog = [];
let nicCatalog = [];
let driveCatalog = [];
let driveIops = [];
let sizingConfig = {};

let selectedCpus = [];
let selectedNics = [];
let selectedDrives = [];

let catalogModalType = null;
let catalogModalId = null;

// Shared helpers — esc(), fetchJson(), sendJson(), adminStatus(), flashStatus(),
// withBusy(), openModal(), fmtDateTime() — come from common.js; tOr() (translate
// or keep the server's English text) from i18n.js.

// Message for a refused request: the server's error, else the generic one.
function errorText(data) {
    return (data && data.error) || t('admin.msg.failed');
}

// Status line under the super-admin tabs (hcl-status, bom-review-status, …).
function adminStatus(id, msg, isError) {
    setStatus(id, msg, isError, { okClass: 'status-ok', errClass: 'status-err' });
}

document.addEventListener('DOMContentLoaded', () => {
    loadModels();
    loadCatalogs();
});

// ── Tab Switching ──────────────────────────────────────────────────────────

function switchTab(tab) {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    document.querySelector(`.tab[data-tab="${tab}"]`).classList.add('active');
    document.getElementById(`tab-${tab}`).classList.add('active');
    if (tab === 'tuning') loadTunables();
    else if (tab === 'pricebook') loadPricebook();
    else if (tab === 'hcl') loadHcl();
    else if (tab === 'bomreviews') { loadBomReviews(); loadBomAgent(); loadBomNotes(); loadBomRejects(); }
    else if (tab === 'users') loadAdminUsers();
    else if (tab === 'stale') loadStaleUsers();
    else if (tab === 'tenants') loadAdminTenants();
    else if (tab === 'sizings') loadAdminSizings();
    else if (tab === 'email') loadEmailSettings();
    else if (tab === 'audit') loadAuditLog();
}

// ── Load Data ──────────────────────────────────────────────────────────────

async function loadModels() {
    const { ok, data } = await fetchJson('/admin/api/models');
    if (!ok || !Array.isArray(data)) { toastError(errorText(data)); return; }
    allModels = data;

    const categories = [...new Set(allModels.map(m => m.category))].sort();
    const catFilter = document.getElementById('category-filter');
    const curCat = catFilter.value;
    catFilter.innerHTML = `<option value="all">${esc(t('admin.filter.all_categories'))}</option>`;
    categories.forEach(c => {
        const opt = document.createElement('option');
        opt.value = c;
        opt.textContent = c;
        catFilter.appendChild(opt);
    });
    catFilter.value = curCat;
    renderModelTable();
}

async function loadCatalogs() {
    const results = await Promise.all(['cpus', 'nics', 'drives', 'drive-iops', 'sizing-config']
        .map(path => fetchJson('/admin/api/' + path)));
    const failed = results.find(r => !r.ok);
    if (failed) { toastError(errorText(failed.data)); return; }
    [cpuCatalog, nicCatalog, driveCatalog, driveIops, sizingConfig] = results.map(r => r.data);

    renderCpuCatalog();
    renderNicCatalog();
    renderDriveCatalog();
    renderDriveIops();
}

const IOPS_INPUTS = {HDD: 'iops-hdd', SSD: 'iops-ssd', NVMe: 'iops-nvme'};

function renderDriveIops() {
    const byType = Object.fromEntries((driveIops || []).map(r => [r.drive_type, r.iops]));
    for (const [type, id] of Object.entries(IOPS_INPUTS)) {
        const el = document.getElementById(id);
        if (el && byType[type] != null) el.value = byType[type];
    }
    // Cluster adjustments: stored as fractions; shown as whole-number percents.
    const c = sizingConfig || {};
    if (c.iops_derating_pct != null) document.getElementById('iops-derating').value = Math.round(c.iops_derating_pct * 100);
    if (c.iops_replication_factor != null) document.getElementById('iops-rf').value = c.iops_replication_factor;
    if (c.iops_read_fraction != null) document.getElementById('iops-read').value = Math.round(c.iops_read_fraction * 100);
}

async function saveDriveIops() {
    const status = document.getElementById('iops-status');
    const iopsPayload = {};
    for (const [type, id] of Object.entries(IOPS_INPUTS)) {
        iopsPayload[type] = parseInt(document.getElementById(id).value, 10);
    }
    const sizingPayload = {
        iops_derating_pct: (parseFloat(document.getElementById('iops-derating').value) || 0) / 100,
        iops_replication_factor: parseInt(document.getElementById('iops-rf').value, 10),
        iops_read_fraction: (parseFloat(document.getElementById('iops-read').value) || 0) / 100,
    };
    const [r1, r2] = await Promise.all([
        sendJson('/admin/api/drive-iops', 'PUT', iopsPayload),
        sendJson('/admin/api/sizing-config', 'PUT', sizingPayload),
    ]);
    const failed = [r1, r2].find(r => !r.ok);
    if (failed) { flashStatus(status, errorText(failed.data), false); return; }
    driveIops = (r1.data && r1.data.drive_iops) || driveIops;
    sizingConfig = (r2.data && r2.data.sizing_config) || sizingConfig;
    flashStatus(status, t('admin.msg.saved'), true);
}

// ── Tuning (sizing & scoring tunables) ───────────────────────────────────────

let tunableDefs = [];

async function loadTunables() {
    // The Tuning tab hosts both the IOPS card and the scoring/sizing tunables.
    const [tun, iops, sizing] = await Promise.all(['tunables', 'drive-iops', 'sizing-config']
        .map(path => fetchJson('/admin/api/' + path)));
    const failed = [tun, iops, sizing].find(r => !r.ok);
    if (failed) { toastError(errorText(failed.data)); return; }
    const data = tun.data || {};
    driveIops = iops.data;
    sizingConfig = sizing.data;
    tunableDefs = data.defs || [];
    renderTunables(data.values || {});
    renderDriveIops();
}

function renderTunables(values) {
    const container = document.getElementById('tunables-groups');
    // Preserve group order as defined in the metadata.
    const groups = [];
    const byGroup = {};
    for (const d of tunableDefs) {
        if (!byGroup[d.group]) { byGroup[d.group] = []; groups.push(d.group); }
        byGroup[d.group].push(d);
    }
    container.innerHTML = groups.map(g => `
        <div class="iops-card-header" style="margin-top:14px"><h3>${esc(tOr('admin.tunable.group.' + g, g))}</h3></div>
        <div class="iops-inputs">
            ${byGroup[g].map(d => {
                const label = tOr('admin.tunable.' + d.key + '.label', d.label);
                const help = tOr('admin.tunable.' + d.key + '.help', d.help || '');
                return `
                <div class="form-group">
                    <label title="${esc(help)}">${esc(label)}<button type="button" class="tunable-info-btn" title="${esc(t('admin.tunable.info_btn'))}" data-click='["showTunableInfo",${tunableDefs.indexOf(d)}]'>i</button></label>
                    <input type="number" id="tun-${d.key}"
                           ${d.min != null ? `min="${d.min}"` : ''}
                           ${d.max != null ? `max="${d.max}"` : ''}
                           step="${d.step != null ? d.step : (d.type === 'int' ? 1 : 'any')}"
                           value="${values[d.key]}"
                           title="${esc(help)}">
                </div>`;
            }).join('')}
        </div>`).join('');
}

async function saveTunables() {
    const status = document.getElementById('tunables-status');
    const payload = {};
    for (const d of tunableDefs) {
        const el = document.getElementById('tun-' + d.key);
        if (el && el.value !== '') payload[d.key] = parseFloat(el.value);
    }
    const { ok, data } = await sendJson('/admin/api/tunables', 'PUT', payload);
    if (!ok) { flashStatus(status, errorText(data), false); return; }
    renderTunables((data && data.values) || {});
    flashStatus(status, t('admin.msg.saved'), true);
}

// Shared What/How/Beware info modal, driven by a {label, what, how, beware} object.
function showInfo(d) {
    if (!d) return;
    document.getElementById('tun-info-title').textContent = d.label || t('admin.info.default_title');
    document.getElementById('tun-info-what').textContent = d.what || d.help || '';
    document.getElementById('tun-info-how').textContent = d.how || '';
    document.getElementById('tun-info-beware').textContent = d.beware || '';
    openModal('tunable-info-modal');
}

function showTunableInfo(idx) {
    showInfo((tunableDefs || [])[idx]);
}

// IOPS fields are hand-built (not metadata-driven), so their info text lives here.
const IOPS_INFO = {
    get hdd() {
        return {
            label: t('admin.iops.hdd.label'),
            what: t('admin.iops.hdd.what'),
            how: t('admin.iops.hdd.how'),
            beware: t('admin.iops.hdd.beware'),
        };
    },
    get ssd() {
        return {
            label: t('admin.iops.ssd.label'),
            what: t('admin.iops.ssd.what'),
            how: t('admin.iops.ssd.how'),
            beware: t('admin.iops.ssd.beware'),
        };
    },
    get nvme() {
        return {
            label: t('admin.iops.nvme.label'),
            what: t('admin.iops.nvme.what'),
            how: t('admin.iops.nvme.how'),
            beware: t('admin.iops.nvme.beware'),
        };
    },
    get derating() {
        return {
            label: t('admin.iops.derating.label'),
            what: t('admin.iops.derating.what'),
            how: t('admin.iops.derating.how'),
            beware: t('admin.iops.derating.beware'),
        };
    },
    get rf() {
        return {
            label: t('admin.iops.rf.label'),
            what: t('admin.iops.rf.what'),
            how: t('admin.iops.rf.how'),
            beware: t('admin.iops.rf.beware'),
        };
    },
    get read() {
        return {
            label: t('admin.iops.read.label'),
            what: t('admin.iops.read.what'),
            how: t('admin.iops.read.how'),
            beware: t('admin.iops.read.beware'),
        };
    },
};

function showIopsInfo(key) {
    showInfo(IOPS_INFO[key]);
}

function closeTunableInfo() {
    closeModal('tunable-info-modal');
}

async function resetTunables() {
    if (!confirm(t('admin.tunable.reset_confirm'))) return;
    const status = document.getElementById('tunables-status');
    const { ok, data } = await fetchJson('/admin/api/tunables/reset', {method: 'POST'});
    if (!ok) { flashStatus(status, errorText(data), false); return; }
    renderTunables((data && data.values) || {});
    flashStatus(status, t('admin.msg.reset_ok'), true);
}

// ── Model Table ────────────────────────────────────────────────────────────

function renderModelTable() {
    const statusFilter = document.getElementById('status-filter').value;
    const catFilter = document.getElementById('category-filter').value;

    let filtered = allModels;
    if (statusFilter !== 'all') filtered = filtered.filter(m => m.status === statusFilter);
    if (catFilter !== 'all') filtered = filtered.filter(m => m.category === catFilter);

    const tbody = document.getElementById('model-tbody');
    tbody.innerHTML = '';

    if (filtered.length === 0) {
        tbody.innerHTML = `<tr><td colspan="12" style="text-align:center;padding:2rem;color:var(--text-muted)">${esc(t('admin.models.none'))}</td></tr>`;
        return;
    }

    filtered.forEach(m => {
        const tr = document.createElement('tr');
        const badgeClass = m.status === 'Active' ? 'badge-active' : m.status === 'EOL' ? 'badge-eol' : 'badge-eos';
        const cpuSummary = m.cpu_options.map(c => c.desc).join('<br>');
        const ramSummary = m.ram_options_gb.join(', ') + ' GB';
        const storType = m.storage?.type || '-';

        tr.innerHTML = `
            <td><strong>${esc(m.name)}</strong>${m.validated_only ? ` <span class="badge badge-validated" title="${esc(t('admin.models.validated_only_title'))}">${esc(t('admin.models.validated_only'))}</span>` : ''}${m.validated_only && !m.vendor ? ` <span class="badge badge-eos" title="${esc(t('admin.models.no_vendor_title'))}">${esc(t('admin.models.no_vendor'))}</span>` : ''}</td>
            <td><span class="badge ${badgeClass}">${m.status}</span></td>
            <td>${esc(m.category)}</td>
            <td>${esc(m.form_factor || '-')}</td>
            <td>${esc(m.socket || '-')}</td>
            <td class="cell-list">${cpuSummary || '-'}</td>
            <td class="cell-list">${ramSummary}</td>
            <td>${esc(storType)}</td>
            <td>${m.min_nodes}</td>
            <td>${m.cost_tier ?? '-'}</td>
            <td class="col-no-recommend">
                <input type="checkbox" ${m.exclude_from_recommendations ? 'checked' : ''}
                       title="${esc(t('admin.models.col_no_recommend_title'))}"
                       aria-label="${esc(t('admin.models.col_no_recommend_title'))}"
                       data-change='["toggleModelRecommend",${m.id},"$checked"]'>
            </td>
            <td class="col-actions">
                <button class="btn-icon" title="${esc(t('common.edit'))}" data-click='["openEditModel",${m.id}]'>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                </button>
                <button class="btn-icon danger" title="${esc(t('common.delete'))}" data-click='["deleteModel",${m.id}]'>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

// The Vendor field belongs to validated-only models; show it only for them.
function syncModelVendorField() {
    const group = document.getElementById('edit-vendor-group');
    if (group) group.hidden = !document.getElementById('edit-validated-only').checked;
}

// ── Catalog Tables ─────────────────────────────────────────────────────────

const ICON_EDIT = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    + '<path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>'
    + '<path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>';
const ICON_TRASH = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">'
    + '<polyline points="3 6 5 6 21 6"/>'
    + '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>';

// Edit / delete cell of a catalog row. The delegate specs carry only the type
// and the numeric id — never user text.
function catalogActionsHtml(type, id) {
    return `<td class="col-actions">
                <button class="btn-icon" title="${esc(t('common.edit'))}" data-click='["openEditCatalogItem","${type}",${id}]'>${ICON_EDIT}</button>
                <button class="btn-icon danger" title="${esc(t('common.delete'))}" data-click='["deleteCatalogItem","${type}",${id}]'>${ICON_TRASH}</button>
            </td>`;
}

function renderCpuCatalog() {
    document.getElementById('cpu-count').textContent = t('admin.catalog.cpu_count', {n: cpuCatalog.length});
    const tbody = document.getElementById('cpu-catalog-tbody');
    tbody.innerHTML = '';
    cpuCatalog.forEach(c => {
        const tr = document.createElement('tr');
        // Clocks: sizing clock (ghz = all-core turbo for known CPUs) + factual
        // base/all-core/max where back-filled. Perf: native SPECrate (server) or
        // PassMark CPU Mark (desktop).
        const clocks = (c.base_ghz != null)
            ? `<strong>${c.ghz}</strong> <span class="muted">${c.base_ghz}/${c.all_core_turbo_ghz != null ? c.all_core_turbo_ghz : '–'}/${c.max_turbo_ghz}</span>`
            : `${c.ghz}`;
        const perf = (c.specrate_int != null)
            ? `${c.specrate_int} <span class="muted">SPECrate</span>`
            : (c.passmark_cpu_mark != null
                ? `${c.passmark_cpu_mark} <span class="muted">PassMark</span>`
                : '<span class="muted">–</span>');
        tr.innerHTML = `
            <td>${esc(c.desc)}</td>
            <td>${c.generation ? esc(c.generation) : '<span class="muted">–</span>'}</td>
            <td>${c.cores}</td>
            <td>${c.threads}</td>
            <td>${clocks}</td>
            <td>${perf}</td>
            <td><span class="count-pill">${c.used_by}</span></td>
            ${catalogActionsHtml('cpu', c.id)}
        `;
        tbody.appendChild(tr);
    });
}

function renderNicCatalog() {
    document.getElementById('nic-count').textContent = t('admin.catalog.nic_count', {n: nicCatalog.length});
    const tbody = document.getElementById('nic-catalog-tbody');
    tbody.innerHTML = '';
    nicCatalog.forEach(n => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${esc(n.desc)}</td>
            <td>${n.ports}</td>
            <td>${esc(n.speed)}</td>
            <td><span class="count-pill">${n.used_by}</span></td>
            ${catalogActionsHtml('nic', n.id)}
        `;
        tbody.appendChild(tr);
    });
}

function renderDriveCatalog() {
    document.getElementById('drive-count').textContent = t('admin.catalog.drive_count', {n: driveCatalog.length});
    const tbody = document.getElementById('drive-catalog-tbody');
    tbody.innerHTML = '';
    driveCatalog.forEach(d => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${esc(d.drive_type)}</td>
            <td>${d.size_tb}</td>
            <td><span class="count-pill">${d.used_by}</span></td>
            ${catalogActionsHtml('drive', d.id)}
        `;
        tbody.appendChild(tr);
    });
}

// ── Catalog CRUD ───────────────────────────────────────────────────────────

function openAddCatalogItem(type) {
    catalogModalType = type;
    catalogModalId = null;

    const titles = { cpu: t('admin.catalog.add_cpu'), nic: t('admin.catalog.add_nic'), drive: t('admin.catalog.add_drive') };
    document.getElementById('catalog-modal-title').textContent = titles[type];
    document.getElementById('catalog-modal-body').innerHTML = catalogFormHtml(type, {});
    openModal('catalog-modal');
}

function openEditCatalogItem(type, id) {
    catalogModalType = type;
    catalogModalId = id;

    const catalog = { cpu: cpuCatalog, nic: nicCatalog, drive: driveCatalog }[type];
    const item = catalog.find(c => c.id === id);
    if (!item) return;

    const titles = { cpu: t('admin.catalog.edit_cpu'), nic: t('admin.catalog.edit_nic'), drive: t('admin.catalog.edit_drive') };
    document.getElementById('catalog-modal-title').textContent = titles[type];
    document.getElementById('catalog-modal-body').innerHTML = catalogFormHtml(type, item);
    openModal('catalog-modal');
}

function catalogFormHtml(type, item) {
    if (type === 'cpu') return `
        <div class="form-group"><label>${esc(t('admin.form.description_req'))}</label>
            <input type="text" id="cat-desc" value="${esc(item.desc || '')}" placeholder="${esc(t('admin.form.cpu_desc_ph'))}" data-change='["autofillCpuBenchmark"]'></div>
        <div class="muted cat-bench-status" id="cat-bench-status">${esc(t('admin.form.bench_hint'))}</div>
        <div class="form-row" style="margin-top:0.75rem">
            <div class="form-group"><label>${esc(t('admin.form.cores'))}</label><input type="number" id="cat-cores" value="${item.cores || ''}" min="1"></div>
            <div class="form-group"><label>${esc(t('admin.form.threads'))}</label><input type="number" id="cat-threads" value="${item.threads || ''}" min="1"></div>
            <div class="form-group"><label>${esc(t('admin.form.generation'))}</label><input type="text" id="cat-generation" value="${esc(item.generation || '')}" placeholder="${esc(t('admin.form.generation_ph'))}"></div>
        </div>
        <div class="form-row" style="margin-top:0.75rem">
            <div class="form-group"><label title="${esc(t('admin.form.pcores_title'))}">${esc(t('admin.form.pcores'))}</label><input type="number" id="cat-pcores" value="${item.p_cores != null ? item.p_cores : ''}" min="0"></div>
            <div class="form-group"><label title="${esc(t('admin.form.ecores_title'))}">${esc(t('admin.form.ecores'))}</label><input type="number" id="cat-ecores" value="${item.e_cores != null ? item.e_cores : ''}" min="0"></div>
        </div>
        <div class="form-row" style="margin-top:0.75rem">
            <div class="form-group"><label title="${esc(t('admin.form.sizing_ghz_title'))}">${esc(t('admin.form.sizing_ghz'))}</label><input type="number" id="cat-ghz" value="${item.ghz || ''}" step="0.1" min="0.1"></div>
            <div class="form-group"><label>${esc(t('admin.form.base_ghz'))}</label><input type="number" id="cat-base-ghz" value="${item.base_ghz != null ? item.base_ghz : ''}" step="0.1" min="0.1"></div>
            <div class="form-group"><label>${esc(t('admin.form.allcore_turbo'))}</label><input type="number" id="cat-allcore-ghz" value="${item.all_core_turbo_ghz != null ? item.all_core_turbo_ghz : ''}" step="0.1" min="0.1"></div>
            <div class="form-group"><label>${esc(t('admin.form.max_turbo'))}</label><input type="number" id="cat-max-ghz" value="${item.max_turbo_ghz != null ? item.max_turbo_ghz : ''}" step="0.1" min="0.1"></div>
        </div>
        <div class="form-row" style="margin-top:0.75rem">
            <div class="form-group"><label title="${esc(t('admin.form.specrate_title'))}">SPECrate2017</label><input type="number" id="cat-specrate" value="${item.specrate_int != null ? item.specrate_int : ''}" step="0.1" min="0"></div>
            <div class="form-group"><label title="${esc(t('admin.form.passmark_title'))}">PassMark CPU Mark</label><input type="number" id="cat-passmark" value="${item.passmark_cpu_mark != null ? item.passmark_cpu_mark : ''}" min="0"></div>
        </div>`;
    if (type === 'nic') return `
        <div class="form-group"><label>${esc(t('admin.form.description_req'))}</label>
            <input type="text" id="cat-desc" value="${esc(item.desc || '')}" placeholder="${esc(t('admin.form.nic_desc_ph'))}"></div>
        <div class="form-row" style="margin-top:0.75rem">
            <div class="form-group"><label>${esc(t('admin.form.ports'))}</label><input type="number" id="cat-ports" value="${item.ports || ''}" min="1"></div>
            <div class="form-group"><label>${esc(t('admin.form.speed'))}</label><input type="text" id="cat-speed" value="${esc(item.speed || '')}" placeholder="${esc(t('admin.form.speed_ph'))}"></div>
        </div>`;
    if (type === 'drive') return `
        <div class="form-row">
            <div class="form-group"><label>${esc(t('admin.form.type_req'))}</label>
                <select id="cat-drive-type">
                    <option value="HDD" ${item.drive_type === 'HDD' ? 'selected' : ''}>HDD</option>
                    <option value="SSD" ${item.drive_type === 'SSD' ? 'selected' : ''}>SSD</option>
                    <option value="NVMe" ${item.drive_type === 'NVMe' ? 'selected' : ''}>NVMe</option>
                </select></div>
            <div class="form-group"><label>${esc(t('admin.form.size_tb_req'))}</label>
                <input type="number" id="cat-size-tb" value="${item.size_tb || ''}" step="0.01" min="0.01"></div>
        </div>`;
    return '';
}

function closeCatalogModal() {
    closeModal('catalog-modal');
}

// Auto-fill benchmark scores for the CPU being added/edited, from the curated
// catalog or the broad SPECrate2017 lookup (same source as the source-CPU
// comparison). Only fills empty fields, so it never clobbers manual entry.
async function autofillCpuBenchmark() {
    const desc = document.getElementById('cat-desc');
    const status = document.getElementById('cat-bench-status');
    if (!desc || !desc.value.trim()) return;
    // Best-effort: a lookup that fails just leaves the fields for the admin.
    const { ok, data: d } = await fetchJson('/api/cpu-perf?q=' + encodeURIComponent(desc.value.trim()));
    if (!ok || !d) return;
    {
        if (!d.found) {
            if (status) status.textContent = t('admin.bench.none');
            return;
        }
        const sr = document.getElementById('cat-specrate');
        const pm = document.getElementById('cat-passmark');
        const filled = [];
        if (d.specrate_int != null && sr && !sr.value) { sr.value = d.specrate_int; filled.push('SPECrate'); }
        if (d.passmark_cpu_mark != null && pm && !pm.value) { pm.value = d.passmark_cpu_mark; filled.push('PassMark'); }
        const src = d.source === 'spec-cpu2017' ? t('admin.bench.src_spec', {n: d.samples}) : t('admin.bench.src_catalog');
        if (status) status.textContent = filled.length
            ? t('admin.bench.filled', {fields: filled.join(' + '), src: src, model: d.model})
            : t('admin.bench.kept', {src: src});
    }
}

const _catNum = id => { const el = document.getElementById(id); const n = el ? parseFloat(el.value) : NaN; return isFinite(n) ? n : null; };
const _catInt = id => { const el = document.getElementById(id); const n = el ? parseInt(el.value, 10) : NaN; return isFinite(n) ? n : null; };
const _catStr = id => { const el = document.getElementById(id); const s = el ? el.value.trim() : ''; return s || null; };

async function saveCatalogItem() {
    const type = catalogModalType;
    let payload, url, method;

    if (type === 'cpu') {
        payload = {
            desc: document.getElementById('cat-desc').value.trim(),
            cores: parseInt(document.getElementById('cat-cores').value) || 0,
            threads: parseInt(document.getElementById('cat-threads').value) || 0,
            ghz: parseFloat(document.getElementById('cat-ghz').value) || 0,
            generation: _catStr('cat-generation'),
            p_cores: _catInt('cat-pcores'),
            e_cores: _catInt('cat-ecores'),
            base_ghz: _catNum('cat-base-ghz'),
            all_core_turbo_ghz: _catNum('cat-allcore-ghz'),
            max_turbo_ghz: _catNum('cat-max-ghz'),
            specrate_int: _catNum('cat-specrate'),
            passmark_cpu_mark: _catInt('cat-passmark'),
        };
        if (!payload.desc) { toastError(t('admin.validate.desc_required')); return; }
    } else if (type === 'nic') {
        payload = {
            desc: document.getElementById('cat-desc').value.trim(),
            ports: parseInt(document.getElementById('cat-ports').value) || 0,
            speed: document.getElementById('cat-speed').value.trim(),
        };
        if (!payload.desc) { toastError(t('admin.validate.desc_required')); return; }
    } else if (type === 'drive') {
        payload = {
            drive_type: document.getElementById('cat-drive-type').value,
            size_tb: parseFloat(document.getElementById('cat-size-tb').value) || 0,
        };
        if (!payload.drive_type || payload.size_tb <= 0) { toastError(t('admin.validate.type_size_required')); return; }
    }

    const base = `/admin/api/${type === 'cpu' ? 'cpus' : type === 'nic' ? 'nics' : 'drives'}`;
    if (catalogModalId) {
        url = `${base}/${catalogModalId}`;
        method = 'PUT';
    } else {
        url = base;
        method = 'POST';
    }

    const { ok, data } = await sendJson(url, method, payload);
    if (data && data.error) { toastError(data.error); return; }
    if (!ok) { toastError(t('admin.msg.save_failed', {error: errorText(data)})); return; }
    closeCatalogModal();
    await loadCatalogs();
}

// Label for the confirm comes from the in-memory catalog, keyed by id, so the
// delegate spec stays free of user-supplied strings.
function catalogItemLabel(type, id) {
    const list = type === 'cpu' ? cpuCatalog : type === 'nic' ? nicCatalog : driveCatalog;
    const item = (list || []).find(x => x.id === id);
    if (!item) return String(id);
    return type === 'drive' ? `${item.drive_type} ${item.size_tb}TB` : item.desc;
}

async function deleteCatalogItem(type, id) {
    if (!confirm(t('admin.confirm.delete_item', {label: catalogItemLabel(type, id)}))) return;
    const base = `/admin/api/${type === 'cpu' ? 'cpus' : type === 'nic' ? 'nics' : 'drives'}`;
    const { ok, data } = await fetchJson(`${base}/${id}`, { method: 'DELETE' });
    if (data && data.error) { toastError(data.error); return; }
    if (!ok) { toastError(t('admin.msg.delete_failed', {error: errorText(data)})); return; }
    await loadCatalogs();
}

// ── Delete Model ───────────────────────────────────────────────────────────

// The list's "Do not recommend" checkbox saves on its own — only that flag is
// sent, so the rest of the model is untouched. On failure the box snaps back.
async function toggleModelRecommend(id, excluded) {
    const box = this;
    const { ok } = await sendJson(`/admin/api/models/${id}`, 'PUT', { exclude_from_recommendations: !!excluded });
    if (!ok) {
        if (box) box.checked = !excluded;
        toastError(t('admin.models.no_recommend_failed'));
        return;
    }
    const m = allModels.find(x => x.id === id);
    if (m) m.exclude_from_recommendations = !!excluded;
}

async function deleteModel(id) {
    // The delegate spec carries only the id; the name comes from the list
    // state so no user-supplied text is ever baked into the JSON attribute.
    const m = allModels.find(x => x.id === id);
    if (!confirm(t('admin.confirm.delete_model', {name: m ? m.name : id}))) return;
    const { ok, data } = await fetchJson(`/admin/api/models/${id}`, { method: 'DELETE' });
    if (!ok) { toastError(t('admin.msg.delete_failed', {error: errorText(data)})); return; }
    loadModels();
    loadCatalogs();
}

// ── Export / Import ────────────────────────────────────────────────────────

function exportModels() {
    window.location.href = '/admin/api/export-models';
}

// ── Import / Template ──────────────────────────────────────────────────────

function openCatalogImport() {
    openModal('catalog-import-modal');
    document.getElementById('catalog-import-file').value = '';
    document.getElementById('catalog-import-replace').checked = false;
    showCatalogImportStatus('');
}

function closeCatalogImport() {
    closeModal('catalog-import-modal');
}

async function doCatalogImport() {
    const fileInput = document.getElementById('catalog-import-file');
    if (!fileInput.files.length) { showCatalogImportStatus(t('admin.import.select_file'), true); return; }

    const formData = new FormData();
    formData.append('file', fileInput.files[0]);
    // "replace" makes the uploaded file authoritative for rows that already
    // exist — what restoring a backup has to mean. Default stays additive.
    formData.append('mode',
        document.getElementById('catalog-import-replace').checked ? 'replace' : 'add');
    showCatalogImportStatus(t('admin.import.importing'), false);

    const { ok, data } = await fetchJson('/admin/api/import-catalog', { method: 'POST', body: formData });
    if (data && data.error) { showCatalogImportStatus(data.error, true); return; }
    if (!ok || !data) { showCatalogImportStatus(t('admin.import.failed', {error: errorText(data)}), true); return; }
    showCatalogImportStatus(data.message, false);
    loadCatalogs();
    loadModels();
}

function showCatalogImportStatus(msg, isError) {
    setStatus('catalog-import-status', msg, isError, { okClass: 'success', errClass: 'error' });
}

function downloadCatalogTemplate() {
    window.location.href = '/admin/api/catalog-template';
}

// ── Licence Pricebook ──────────────────────────────────────────────────────
// Upload -> diff -> apply, mirroring tools/import_pricebook.py: nothing is
// written until the reviewed file is re-posted with apply=1.

async function loadPricebook() {
    const rows = document.getElementById('pricebook-feed-rows');
    const empty = document.getElementById('pricebook-no-feed');
    const { ok, data } = await fetchJson('/admin/api/pricebook');
    if (ok && data) {
        const feeds = data.feeds || [];
        rows.innerHTML = feeds.map(f => `<tr>
            <td>${esc(f.region)}</td>
            <td>${esc(f.label)}</td>
            <td>${esc(f.currency)}</td>
            <td>${f.bands}</td>
            <td>${f.flats}</td>
            <td>${f.uploaded_at ? esc(f.uploaded_at.slice(0, 10)) : ''}</td>
            <td>${esc(f.source_filename || '')}</td>
        </tr>`).join('');
        empty.style.display = feeds.length ? 'none' : 'block';
    } else {
        rows.innerHTML = '';
        empty.style.display = 'block';
    }
}

function resetPricebookPreview() {
    // A new file invalidates the reviewed diff: force a fresh preview before
    // Apply becomes available again.
    document.getElementById('pricebook-apply-btn').style.display = 'none';
    document.getElementById('pricebook-preview').style.display = 'none';
    setPricebookStatus('', false);
}

function setPricebookStatus(msg, isError) {
    setStatus('pricebook-status', msg, isError, { okClass: 'success', errClass: 'error', hide: false });
}

function pricebookFormData(apply) {
    const fileInput = document.getElementById('pricebook-file');
    if (!fileInput.files.length) return null;
    const fd = new FormData();
    fd.append('file', fileInput.files[0]);
    fd.append('region', document.getElementById('pricebook-region').value.trim() || 'EMEA');
    fd.append('label', document.getElementById('pricebook-label').value.trim());
    if (apply) fd.append('apply', '1');
    return fd;
}

function renderPricebookPreview(d) {
    const eur = v => v == null ? '—' : v.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2});
    const c = d.counts || {};
    let html = `<p class="import-info">${esc(t('admin.pricebook.parsed_summary', {
        rows: c.license_rows, bands: c.banded, flats: c.flat,
        unmatched: c.unmatched, currency: d.currency || '?'}))}</p>`;
    if ((d.unmatched || []).length) {
        html += `<p class="import-info">${esc(t('admin.pricebook.unmatched_title'))}</p>`
            + '<ul class="import-info">'
            + d.unmatched.map(u => `<li><code>${esc(u.sku)}</code> ${esc(u.description || '')}</li>`).join('')
            + '</ul>';
    }
    if ((d.new_editions || []).length) {
        html += `<p class="import-info">${esc(t('admin.pricebook.new_editions', {letters: d.new_editions.join(', ')}))}</p>`;
    }
    const diff = d.diff || {};
    if (!diff.current) {
        html += `<p class="import-info">${esc(t('admin.pricebook.first_feed', {region: d.region}))}</p>`;
    } else {
        html += `<p class="import-info">${esc(t('admin.pricebook.diff_title', {label: diff.current.label}))} `
            + `${esc(t('admin.pricebook.diff_counts', {
                added: (diff.added || []).length,
                removed: (diff.removed || []).length,
                changed: (diff.moved || []).length}))}</p>`;
        const rows = []
            .concat((diff.moved || []).map(m => ['~', m.kind, m.key, eur(m.old), eur(m.new),
                m.pct == null ? '' : (m.pct > 0 ? '+' : '') + m.pct + '%']))
            .concat((diff.added || []).map(a => ['+', a.kind, a.key, '—', eur(a.new), '']))
            .concat((diff.removed || []).map(r => ['−', r.kind, r.key, eur(r.old), '—', '']));
        if (rows.length) {
            html += `<div class="catalog-table-wrap"><table class="model-table"><thead><tr>
                <th></th>
                <th>${esc(t('admin.pricebook.col_kind'))}</th>
                <th>${esc(t('admin.pricebook.col_item'))}</th>
                <th>${esc(t('admin.pricebook.col_old'))}</th>
                <th>${esc(t('admin.pricebook.col_new'))}</th>
                <th>${esc(t('admin.pricebook.col_change'))}</th>
            </tr></thead><tbody>`
                + rows.map(r => `<tr>${r.map(cell => `<td>${esc(String(cell))}</td>`).join('')}</tr>`).join('')
                + '</tbody></table></div>';
        } else {
            html += `<p class="import-info">${esc(t('admin.pricebook.no_changes'))}</p>`;
        }
    }
    const el = document.getElementById('pricebook-preview');
    el.innerHTML = html;
    el.style.display = 'block';
}

async function previewPricebook() {
    const fd = pricebookFormData(false);
    if (!fd) { setPricebookStatus(t('admin.pricebook.select_file'), true); return; }
    setPricebookStatus(t('admin.pricebook.previewing'), false);
    const { ok, data } = await fetchJson('/admin/api/import-pricebook', {method: 'POST', body: fd});
    if (data && data.error) {
        resetPricebookPreview();
        setPricebookStatus(data.error, true);
        return;
    }
    if (!ok || !data) { setPricebookStatus(t('admin.pricebook.failed', {error: errorText(data)}), true); return; }
    renderPricebookPreview(data);
    document.getElementById('pricebook-apply-btn').style.display = '';
    setPricebookStatus('', false);
}

async function applyPricebook() {
    const fd = pricebookFormData(true);
    if (!fd) { setPricebookStatus(t('admin.pricebook.select_file'), true); return; }
    setPricebookStatus(t('admin.pricebook.applying'), false);
    const { ok, data } = await fetchJson('/admin/api/import-pricebook', {method: 'POST', body: fd});
    if (data && data.error) { setPricebookStatus(data.error, true); return; }
    if (!ok || !data) { setPricebookStatus(t('admin.pricebook.failed', {error: errorText(data)}), true); return; }
    document.getElementById('pricebook-apply-btn').style.display = 'none';
    if (data.already_current) {
        setPricebookStatus(t('admin.pricebook.already_current'), false);
    } else {
        setPricebookStatus(t('admin.pricebook.applied', {
            label: data.feed.label, region: data.feed.region}), false);
    }
    loadPricebook();
}

// ── Model Edit Modal ───────────────────────────────────────────────────────

function openAddModel() {
    document.getElementById('edit-id').value = '';
    document.getElementById('edit-title').textContent = t('admin.model.add_title');
    document.getElementById('edit-name').value = '';
    document.getElementById('edit-status').value = 'Active';
    document.getElementById('edit-category').value = '';
    document.getElementById('edit-form-factor').value = '';
    document.getElementById('edit-chassis').value = '';
    document.getElementById('edit-socket').value = 'single';
    document.getElementById('edit-psu').value = '';
    document.getElementById('edit-ram-slots').value = '0';
    document.getElementById('edit-min-nodes').value = '1';
    document.getElementById('edit-cost-tier').value = '5';
    document.getElementById('edit-validated-only').checked = false;
    document.getElementById('edit-vendor').value = '';
    syncModelVendorField();
    document.getElementById('edit-exclude-recommend').checked = false;
    document.getElementById('edit-notes').value = '';

    selectedCpus = [];
    selectedNics = [];
    selectedDrives = [];

    document.getElementById('cpu-chips').innerHTML = '';
    document.getElementById('nic-chips').innerHTML = '';
    document.getElementById('drive-chips').innerHTML = '';
    document.getElementById('ram-list').innerHTML = '';

    document.getElementById('edit-stor-type').value = 'nvme_only';
    document.getElementById('edit-stor-dpn').value = '1';
    document.getElementById('edit-stor-hdd-count').value = '3';
    document.getElementById('edit-stor-ssd-count').value = '1';
    document.getElementById('edit-stor-nvme-count').value = '1';

    updateStorageFields();
    addRamInput();
    openModal('edit-modal');
}

async function openEditModel(id) {
    const { ok, data: m } = await fetchJson(`/admin/api/models/${id}`);
    if (!ok || !m) { toastError(errorText(m)); return; }

    document.getElementById('edit-id').value = id;
    document.getElementById('edit-title').textContent = t('admin.model.edit_title', {name: m.name});
    document.getElementById('edit-name').value = m.name;
    document.getElementById('edit-status').value = m.status;
    document.getElementById('edit-category').value = m.category || '';
    document.getElementById('edit-form-factor').value = m.form_factor || '';
    document.getElementById('edit-chassis').value = m.chassis || '';
    document.getElementById('edit-socket').value = m.socket || 'single';
    document.getElementById('edit-psu').value = m.psu || '';
    document.getElementById('edit-ram-slots').value = m.ram_slots || 0;
    document.getElementById('edit-min-nodes').value = m.min_nodes || 1;
    document.getElementById('edit-cost-tier').value =
        (m.cost_tier !== undefined && m.cost_tier !== null) ? m.cost_tier : 5;
    document.getElementById('edit-validated-only').checked = !!m.validated_only;
    document.getElementById('edit-vendor').value = m.vendor || '';
    syncModelVendorField();
    document.getElementById('edit-exclude-recommend').checked = !!m.exclude_from_recommendations;
    document.getElementById('edit-notes').value = m.notes || '';

    selectedCpus = (m.cpu_options || []).map(c => {
        const cat = cpuCatalog.find(cc => cc.desc === c.desc);
        const base = cat ? { ...cat } : { id: null, desc: c.desc, cores: c.cores, threads: c.threads, ghz: c.ghz };
        base.qty = c.qty || 1;
        return base;
    });
    renderCpuChips();

    const ramList = document.getElementById('ram-list');
    ramList.innerHTML = '';
    (m.ram_options_gb || []).forEach(r => addRamChip(r));
    addRamInput();

    const stor = m.storage || {};
    document.getElementById('edit-stor-type').value = stor.type || 'nvme_only';
    document.getElementById('edit-stor-dpn').value = stor.drives_per_node || '';
    document.getElementById('edit-stor-hdd-count').value = stor.hdd_count || '';
    document.getElementById('edit-stor-ssd-count').value = stor.ssd_count || '';
    document.getElementById('edit-stor-nvme-count').value = stor.nvme_count || '';
    updateStorageFields();

    selectedDrives = [];
    for (const [key, label] of [['hdd_options_tb', 'HDD'], ['ssd_options_tb', 'SSD'], ['nvme_options_tb', 'NVMe']]) {
        (stor[key] || []).forEach(size => {
            const cat = driveCatalog.find(d => d.drive_type === label && d.size_tb === size);
            selectedDrives.push(cat ? { ...cat } : { id: null, drive_type: label, size_tb: size });
        });
    }
    renderDriveChips();

    selectedNics = (m.nic_options || []).map(n => {
        const cat = nicCatalog.find(nn => nn.desc === n.desc);
        const base = cat ? { ...cat } : { id: null, desc: n.desc, ports: n.ports, speed: n.speed };
        base.qty = n.qty || 1;
        return base;
    });
    renderNicChips();

    openModal('edit-modal');
}

function closeEdit() {
    closeModal('edit-modal');
}

// ── CPU / NIC picker modal ─────────────────────────────────────────────────
// A shared scrolling checkbox modal (long descriptions), mirroring the grouped
// drive picker. Per-kind config drives the title, ordering, label, dedup key and
// how a checked item is added. NICs sort fastest-first; CPUs biggest-first.

function nicSpeedVal(speed) {
    // "25GbE" -> 25, "1GbE" -> 1; unparseable -> 0 so it sorts last.
    const m = /([\d.]+)/.exec(speed || '');
    return m ? parseFloat(m[1]) : 0;
}

const ITEM_SELECT = {
    cpu: {
        title: () => t('admin.select.add_cpus'),
        noun: () => t('admin.select.noun_cpus'),
        catalog: () => cpuCatalog,
        selected: () => selectedCpus,
        key: c => c.desc,
        label: c => `${esc(c.desc)}<span class="item-select-meta">${esc(t('admin.select.cpu_meta', {cores: c.cores, threads: c.threads, ghz: c.ghz}))}</span>`,
        sort: (a, b) => b.cores - a.cores || b.ghz - a.ghz || a.desc.localeCompare(b.desc),
        add: c => {
            const flags = parseModelName(document.getElementById('edit-name').value.trim());
            selectedCpus.push({ ...c, qty: flags.isDual ? 2 : 1 });
        },
        renderChips: () => renderCpuChips(),
    },
    nic: {
        title: () => t('admin.select.add_nics'),
        noun: () => t('admin.select.noun_nics'),
        catalog: () => nicCatalog,
        selected: () => selectedNics,
        key: n => n.desc,
        label: n => `${esc(n.desc)}<span class="item-select-meta">${esc(t('admin.select.nic_meta', {ports: n.ports, speed: n.speed}))}</span>`,
        sort: (a, b) => nicSpeedVal(b.speed) - nicSpeedVal(a.speed) || a.desc.localeCompare(b.desc),
        add: n => { selectedNics.push({ ...n, qty: 1 }); },
        renderChips: () => renderNicChips(),
    },
    // Drives: only the media the current storage type allows, grouped by type
    // (in the storage type's media order) with sizes ascending — so each
    // checkbox needs only its size. Its own modal markup (drive-select-modal,
    // static title), hence the element ids and the renderList override.
    drive: {
        catalog: () => driveCatalog.filter(d => allowedDriveTypes().includes(d.drive_type)),
        selected: () => selectedDrives,
        key: d => `${d.drive_type}|${d.size_tb}`,
        sort: (a, b) => a.size_tb - b.size_tb,
        add: d => { selectedDrives.push({ ...d }); },
        renderChips: () => renderDriveChips(),
        modalId: 'drive-select-modal',
        listId: 'drive-select-list',
        allId: 'drive-select-all',
        emptyText: () => allowedDriveTypes().length
            ? t('admin.drive_select.all_added') : t('admin.drive_select.no_options'),
        renderList: avail => allowedDriveTypes().map(type => {
            const drives = avail.filter(d => d.drive_type === type);
            if (!drives.length) return '';
            const items = drives.map(d =>
                `<label><input type="checkbox" value="${d.id}"> ${d.size_tb} TB</label>`
            ).join('');
            return `<div class="drive-select-group">`
                + `<h4 class="drive-select-group-title">${esc(type)}</h4>`
                + `<div class="drive-select-grid">${items}</div></div>`;
        }).join(''),
    },
};

// Per-kind modal / list / select-all element ids; the CPU and NIC kinds share
// the item-select modal, drives have their own.
function itemSelectIds(cfg) {
    return {
        modal: cfg.modalId || 'item-select-modal',
        list: cfg.listId || 'item-select-list',
        all: cfg.allId || 'item-select-all',
    };
}

let itemSelectKind = null;

function openItemSelect(kind) {
    itemSelectKind = kind;
    const cfg = ITEM_SELECT[kind];
    const ids = itemSelectIds(cfg);
    // The shared CPU/NIC modal takes the kind's title; the drive modal has its own.
    if (cfg.title) document.getElementById('item-select-title').textContent = cfg.title();
    renderItemSelectList();
    document.getElementById(ids.all).checked = false;
    openModal(ids.modal);
}

function closeItemSelect() {
    if (itemSelectKind) closeModal(itemSelectIds(ITEM_SELECT[itemSelectKind]).modal);
    itemSelectKind = null;
}

function renderItemSelectList() {
    const cfg = ITEM_SELECT[itemSelectKind];
    const list = document.getElementById(itemSelectIds(cfg).list);
    const taken = new Set(cfg.selected().map(cfg.key));
    const avail = cfg.catalog().filter(it => !taken.has(cfg.key(it))).sort(cfg.sort);
    if (!avail.length) {
        const text = cfg.emptyText ? cfg.emptyText() : t('admin.select.all_added', {noun: cfg.noun()});
        list.innerHTML = `<p class="drive-select-empty">${esc(text)}</p>`;
        return;
    }
    list.innerHTML = cfg.renderList ? cfg.renderList(avail) : avail.map(it =>
        `<label class="item-select-row"><input type="checkbox" value="${it.id}"> ${cfg.label(it)}</label>`
    ).join('');
}

function toggleAllItemSelect(el) {
    const cfg = ITEM_SELECT[itemSelectKind];
    document.querySelectorAll(`#${itemSelectIds(cfg).list} input[type=checkbox]`)
        .forEach(cb => { cb.checked = el.checked; });
}

function confirmItemSelect() {
    const cfg = ITEM_SELECT[itemSelectKind];
    document.querySelectorAll(`#${itemSelectIds(cfg).list} input[type=checkbox]:checked`).forEach(cb => {
        const it = cfg.catalog().find(x => x.id === parseInt(cb.value));
        if (it && !cfg.selected().some(s => cfg.key(s) === cfg.key(it))) cfg.add(it);
    });
    cfg.renderChips();
    closeItemSelect();
}

// The drive picker's markup names these three; same picker, kind 'drive'.
function openDriveSelect() { openItemSelect('drive'); }
function closeDriveSelect() { closeItemSelect(); }
function toggleAllDriveSelect(el) { toggleAllItemSelect(el); }
function confirmDriveSelect() { confirmItemSelect(); }

// ── Chip Rendering ─────────────────────────────────────────────────────────

// Chips with a quantity dropdown, one per selected item. `setFn` / `removeFn`
// name the delegate handlers (index and value / index are the only args).
function renderQtyChips(containerId, list, setFn, removeFn) {
    const container = document.getElementById(containerId);
    container.innerHTML = '';
    list.forEach((item, i) => {
        const chip = document.createElement('span');
        chip.className = 'chip';
        chip.innerHTML = `<select class="qty-select" data-change='["${setFn}",${i},"$value"]'>${qtyOptions(item.qty)}</select> x ${esc(item.desc)} <span class="remove" data-click='["${removeFn}",${i}]'>&times;</span>`;
        container.appendChild(chip);
    });
}

function renderCpuChips() { renderQtyChips('cpu-chips', selectedCpus, 'setSelCpuQty', 'removeCpu'); }
function renderNicChips() { renderQtyChips('nic-chips', selectedNics, 'setSelNicQty', 'removeNic'); }

function qtyOptions(selected) {
    return [1,2,3,4].map(n => `<option value="${n}" ${n===selected?'selected':''}>${n}</option>`).join('');
}

function renderDriveChips() {
    const container = document.getElementById('drive-chips');
    container.innerHTML = '';
    selectedDrives.forEach((d, i) => {
        const chip = document.createElement('span');
        chip.className = 'chip';
        chip.innerHTML = `${esc(d.drive_type)} ${d.size_tb} TB <span class="remove" data-click='["removeDrive",${i}]'>&times;</span>`;
        container.appendChild(chip);
    });
}

function removeCpu(i) { selectedCpus.splice(i, 1); renderCpuChips(); }
function removeNic(i) { selectedNics.splice(i, 1); renderNicChips(); }
function removeDrive(i) { selectedDrives.splice(i, 1); renderDriveChips(); }

// Delegation wrappers (replace former inline handlers; CSP-safe).
function setSelCpuQty(i, v) { selectedCpus[i].qty = parseInt(v) || 1; }
function setSelNicQty(i, v) { selectedNics[i].qty = parseInt(v) || 1; }
function removeParentChip(el) { el.parentElement.remove(); }
function ramInputEnter(el, e) { if (e.key === 'Enter') { e.preventDefault(); addRamFromInput(el); } }
function addRamFromPrev(el) { addRamFromInput(el.previousElementSibling); }
// ── RAM Chips ──────────────────────────────────────────────────────────────

function addRamChip(size) {
    const list = document.getElementById('ram-list');
    const inputWrap = list.querySelector('.ram-input-wrap');
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.dataset.size = size;
    chip.innerHTML = `${size} GB <span class="remove" data-click='["removeParentChip","$this"]'>&times;</span>`;
    if (inputWrap) list.insertBefore(chip, inputWrap);
    else list.appendChild(chip);
}

function addRamInput() {
    const list = document.getElementById('ram-list');
    if (list.querySelector('.ram-input-wrap')) return;
    const wrap = document.createElement('span');
    wrap.className = 'ram-input-wrap';
    wrap.innerHTML = `<input type="number" placeholder="GB" min="1" data-keydown='["ramInputEnter","$this","$event"]'>
        <button class="btn btn-small" data-click='["addRamFromPrev","$this"]'>+</button>`;
    list.appendChild(wrap);
}

function addRamFromInput(input) {
    const val = parseInt(input.value);
    if (val > 0) { addRamChip(val); input.value = ''; }
}

// ── Model Name Intelligence ────────────────────────────────────────────────

function parseModelName(name) {
    const upper = name.toUpperCase();
    const flags = {
        isEdge: upper.startsWith('HE'),
        isCore: upper.startsWith('HC'),
        isDual: /D/.test(upper.replace(/^H[EC]/, '')),
        isFlash: /F/.test(upper.replace(/^H[EC]/, '')),
        isGpu: /G/.test(upper.replace(/^H[EC]/, '')),
    };
    return flags;
}

function onModelNameInput() {
    const id = document.getElementById('edit-id').value;
    if (id) return;

    const name = document.getElementById('edit-name').value.trim();
    if (!name) { hideNameHints(); return; }

    const flags = parseModelName(name);
    const hints = [];

    if (flags.isDual) {
        document.getElementById('edit-socket').value = 'dual';
        selectedCpus.forEach(c => { c.qty = 2; });
        renderCpuChips();
        hints.push(t('admin.hint.dual_cpu'));
    } else if (flags.isEdge || flags.isCore) {
        document.getElementById('edit-socket').value = 'single';
        selectedCpus.forEach(c => { c.qty = 1; });
        renderCpuChips();
        hints.push(t('admin.hint.single_cpu'));
    }

    if (flags.isFlash) {
        const storType = document.getElementById('edit-stor-type').value;
        if (['hdd_only', 'hybrid', 'hybrid_nvme'].includes(storType)) {
            document.getElementById('edit-stor-type').value = 'nvme_only';
            updateStorageFields();
        }
        selectedDrives = selectedDrives.filter(d => d.drive_type !== 'HDD');
        renderDriveChips();
        hints.push(t('admin.hint.all_flash'));
    }

    if (flags.isEdge) hints.unshift(t('admin.hint.edge_model'));
    if (flags.isCore) hints.unshift(t('admin.hint.core_model'));

    showNameHints(hints);
}

function showNameHints(hints) {
    let el = document.getElementById('name-hints');
    if (!el) {
        el = document.createElement('div');
        el.id = 'name-hints';
        el.className = 'name-hints';
        const nameGroup = document.getElementById('edit-name').closest('.form-row');
        nameGroup.parentNode.insertBefore(el, nameGroup.nextSibling);
    }
    if (hints.length === 0) { el.style.display = 'none'; return; }
    el.style.display = '';
    // Hints are translated literals today, but escape defensively so this never
    // becomes an injection sink if a caller ever passes model-derived text.
    el.innerHTML = hints.map(h => `<span class="hint-tag">${esc(h)}</span>`).join(' ');
}

function hideNameHints() {
    const el = document.getElementById('name-hints');
    if (el) el.style.display = 'none';
}

// Each storage type permits only certain drive media. This gates both the picker
// and what can be added, so e.g. a Hybrid (HDD+SSD) model can never receive an
// NVMe drive. Mirrors the keys saveModel writes and the engine reads (the cause
// of the "VxRAIL01 cannot meet this workload" trap was a hybrid model whose
// flash drives were NVMe — impossible to construct now).
const STORAGE_DRIVE_TYPES = {
    nvme_only: ['NVMe'],
    ssd_only: ['SSD'],
    hdd_only: ['HDD'],
    hybrid: ['HDD', 'SSD'],
    hybrid_nvme: ['HDD', 'NVMe'],
    nvme_and_ssd: ['NVMe', 'SSD'],
    cloud: [],
};

function allowedDriveTypes() {
    return STORAGE_DRIVE_TYPES[document.getElementById('edit-stor-type').value] || [];
}

// ── Storage Fields ─────────────────────────────────────────────────────────

function updateStorageFields() {
    const type = document.getElementById('edit-stor-type').value;
    const flags = parseModelName(document.getElementById('edit-name').value.trim());
    const hddTypes = ['hdd_only', 'hybrid', 'hybrid_nvme'];

    if (flags.isFlash && hddTypes.includes(type)) {
        document.getElementById('edit-stor-type').value = 'nvme_only';
        updateStorageFields();
        return;
    }

    const showHdd = hddTypes.includes(type);
    const showSsd = ['hybrid'].includes(type);
    const showNvme = ['hybrid_nvme'].includes(type);
    const showDpn = !['cloud', 'hybrid', 'hybrid_nvme', 'nvme_and_ssd'].includes(type);

    document.getElementById('stor-hdd-count-wrap').style.display = showHdd ? '' : 'none';
    document.getElementById('stor-ssd-count-wrap').style.display = showSsd ? '' : 'none';
    document.getElementById('stor-nvme-count-wrap').style.display = showNvme ? '' : 'none';
    document.getElementById('stor-drives-per-node-wrap').style.display = showDpn ? '' : 'none';

    const storSelect = document.getElementById('edit-stor-type');
    storSelect.querySelectorAll('option').forEach(opt => {
        opt.disabled = flags.isFlash && hddTypes.includes(opt.value);
    });

    // Cloud models have no physical drives.
    const driveWrap = document.getElementById('drive-options-wrap');
    if (driveWrap) driveWrap.style.display = type === 'cloud' ? 'none' : '';

    // Guard: drop any selected drives whose media is invalid for the new storage
    // type (e.g. switching Hybrid -> NVMe-only strips HDD/SSD picks).
    const allowed = STORAGE_DRIVE_TYPES[type] || [];
    const kept = selectedDrives.filter(d => allowed.includes(d.drive_type));
    if (kept.length !== selectedDrives.length) {
        selectedDrives = kept;
        renderDriveChips();
    }
}

// ── Save Model ─────────────────────────────────────────────────────────────

async function saveModel() {
    const id = document.getElementById('edit-id').value;
    const name = document.getElementById('edit-name').value.trim();
    if (!name) { toastError(t('admin.validate.model_name_required')); return; }

    const cpuOptions = selectedCpus.map(c => ({
        desc: c.desc, cores: c.cores, threads: c.threads, ghz: c.ghz, qty: c.qty || 1,
    }));

    const ramOptions = [];
    document.querySelectorAll('#ram-list .chip').forEach(chip => {
        ramOptions.push(parseInt(chip.dataset.size));
    });

    const storType = document.getElementById('edit-stor-type').value;
    const storage = { type: storType };
    if (['hdd_only', 'hybrid', 'hybrid_nvme'].includes(storType))
        storage.hdd_count = parseInt(document.getElementById('edit-stor-hdd-count').value) || null;
    if (['hybrid'].includes(storType))
        storage.ssd_count = parseInt(document.getElementById('edit-stor-ssd-count').value) || null;
    if (['hybrid_nvme'].includes(storType))
        storage.nvme_count = parseInt(document.getElementById('edit-stor-nvme-count').value) || null;
    if (!['cloud', 'hybrid', 'hybrid_nvme', 'nvme_and_ssd'].includes(storType))
        storage.drives_per_node = parseInt(document.getElementById('edit-stor-dpn').value) || null;

    selectedDrives.forEach(d => {
        const key = `${d.drive_type.toLowerCase()}_options_tb`;
        if (!storage[key]) storage[key] = [];
        storage[key].push(d.size_tb);
    });

    const nicOptions = selectedNics.map(n => ({
        desc: n.desc, ports: n.ports, speed: n.speed, qty: n.qty || 1,
    }));

    const payload = {
        name,
        status: document.getElementById('edit-status').value,
        category: document.getElementById('edit-category').value.trim(),
        form_factor: document.getElementById('edit-form-factor').value.trim() || null,
        chassis: document.getElementById('edit-chassis').value.trim() || null,
        socket: document.getElementById('edit-socket').value,
        psu: document.getElementById('edit-psu').value.trim() || null,
        ram_slots: parseInt(document.getElementById('edit-ram-slots').value) || 0,
        min_nodes: parseInt(document.getElementById('edit-min-nodes').value) || 1,
        cost_tier: parseFloat(document.getElementById('edit-cost-tier').value) || 5,
        validated_only: document.getElementById('edit-validated-only').checked,
        // Only a validated-only model carries its own vendor (HCL models get
        // theirs from the HCL); clear it otherwise so it cannot linger.
        vendor: document.getElementById('edit-validated-only').checked
            ? (document.getElementById('edit-vendor').value.trim().toLowerCase() || null)
            : null,
        exclude_from_recommendations: document.getElementById('edit-exclude-recommend').checked,
        notes: document.getElementById('edit-notes').value.trim() || null,
        cpu_options: cpuOptions,
        ram_options_gb: ramOptions,
        storage,
        nic_options: nicOptions,
    };

    const url = id ? `/admin/api/models/${id}` : '/admin/api/models';
    const method = id ? 'PUT' : 'POST';

    const { ok, data } = await sendJson(url, method, payload);
    if (data && data.error) { toastError(data.error); return; }
    if (!ok) { toastError(t('admin.msg.save_failed', {error: errorText(data)})); return; }
    closeEdit();
    loadModels();
    loadCatalogs();
}


// ==================== SUPER-ADMIN: USERS / TENANTS / SIZINGS ====================
// These tabs drive the /api/admin/users and /api/admin/super endpoints. The whole
// /admin area is already super-admin-gated server-side.

// ── Tenants ──────────────────────────────────────────────────────────────────

let tenantCache = [];
let userCache = [];     // last /users list, so row actions can look a user up by id

async function loadAdminTenants() {
    const { ok, data } = await fetchJson('/api/admin/super/tenants');
    if (!ok) { adminStatus('tenants-status', t('admin.tenants.load_error'), true); return; }
    tenantCache = data;
    // Keep the user-tab tenant filter in sync.
    const filter = document.getElementById('users-tenant-filter');
    if (filter) {
        const cur = filter.value;
        filter.innerHTML = `<option value="">${esc(t('admin.users.all_tenants'))}</option>`
            + data.map(t => `<option value="${t.id}">${esc(t.domain)}</option>`).join('');
        filter.value = cur;
    }
    const body = document.getElementById('admin-tenants-tbody');
    body.innerHTML = data.map(t => `
        <tr>
            <td>${esc(t.domain)}</td>
            <td>${t.is_scale ? esc(window.t('common.yes')) : ''}</td>
            <td>${t.user_count}</td>
            <td>${t.is_blocked ? `<span class="badge-blocked">${esc(window.t('admin.tenants.blocked'))}</span>` : esc(window.t('admin.status.active'))}</td>
            <td class="col-actions">
                <button class="btn btn-sm ${t.is_blocked ? 'btn-secondary' : 'btn-danger'}"
                        data-click='["toggleBlockTenant",${t.id},${t.is_blocked ? 'false' : 'true'}]'>
                    ${t.is_blocked ? esc(window.t('admin.tenants.unblock')) : esc(window.t('admin.tenants.block'))}
                </button>
            </td>
        </tr>`).join('') || `<tr><td colspan="5">${esc(window.t('admin.tenants.none'))}</td></tr>`;
}

async function toggleBlockTenant(id, block) {
    if (block && !confirm(t('admin.tenants.block_confirm'))) return;
    const { ok, data } = await sendJson(`/api/admin/super/tenants/${id}/block`, 'POST', { blocked: block });
    if (!ok) { adminStatus('tenants-status', (data && data.error) || t('admin.msg.failed'), true); return; }
    adminStatus('tenants-status', data.message, false);
    loadAdminTenants();
}

// ── Users ────────────────────────────────────────────────────────────────────

async function loadAdminUsers() {
    if (!tenantCache.length) await loadAdminTenants();
    const includeDisabled = document.getElementById('users-include-disabled').checked;
    const unverifiedOnly = document.getElementById('users-unverified-only').checked;
    const tenant = document.getElementById('users-tenant-filter').value;
    const params = new URLSearchParams();
    if (includeDisabled) params.set('include_disabled', 'true');
    if (unverifiedOnly) params.set('unverified', 'true');
    if (tenant) params.set('tenant', tenant);
    const { ok, data } = await fetchJson('/api/admin/super/users?' + params.toString());
    const body = document.getElementById('admin-users-tbody');
    if (!ok) { body.innerHTML = ''; adminStatus('admin-status', t('admin.users.load_error'), true); return; }
    userCache = data;
    const roleOpts = (sel) =>
        `<option value="user"${sel === 'user' ? ' selected' : ''}>${esc(t('admin.role.user'))}</option>`
        + `<option value="tenant_admin"${sel === 'tenant_admin' ? ' selected' : ''}>${esc(t('admin.role.tenant_admin'))}</option>`
        + `<option value="super_admin"${sel === 'super_admin' ? ' selected' : ''}>${esc(t('admin.role.super_admin'))}</option>`;

    body.innerHTML = data.map(u => {
        const actions = [];
        if (!u.is_disabled) {
            actions.push(`<button class="btn btn-sm btn-secondary" data-click='["resetUserPassword",${u.id}]'>${esc(t('admin.users.reset_password'))}</button>`);
            if (u.role !== 'super_admin') {
                actions.push(`<button class="btn btn-sm btn-danger" data-click='["disableAdminUser",${u.id}]'>${esc(t('admin.users.disable'))}</button>`);
            }
        } else {
            actions.push(`<button class="btn btn-sm btn-secondary" data-click='["restoreUser",${u.id}]'>${esc(t('admin.users.restore'))}</button>`);
            actions.push(`<button class="btn btn-sm btn-danger" data-click='["deleteUser",${u.id}]'>${esc(t('common.delete'))}</button>`);
        }
        // Role is editable inline for active users; disabled users must be
        // restored before their role can change (server enforces this too).
        const roleCell = u.is_disabled
            ? esc(u.role)
            : `<select class="role-select" data-change='["changeUserRole",${u.id},"$value"]'>${roleOpts(u.role)}</select>`;
        const statusCell = u.is_disabled ? esc(t('admin.status.disabled'))
            : (u.is_verified ? esc(t('admin.status.active'))
               : `<span class="badge-unverified">${esc(t('admin.status.unactivated'))}</span>`);
        return `<tr class="${u.is_disabled ? 'row-disabled' : ''}">
            <td>${esc(u.email)}</td>
            <td>${esc(u.tenant_domain)}</td>
            <td>${roleCell}${u.is_scale ? ` <span class="badge-scale">${esc(t('admin.users.scale_badge'))}</span>` : ''}</td>
            <td>${statusCell}</td>
            <td>${fmtDateTime(u.last_login_at)}</td>
            <td class="col-actions">${actions.join(' ')}</td>
        </tr>`;
    }).join('') || `<tr><td colspan="6">${esc(t('admin.users.none'))}</td></tr>`;
}

async function changeUserRole(id, role) {
    const u = userCache.find(x => x.id === id);
    if (u && role === u.role) return;
    const label = { user: t('admin.role.user'), tenant_admin: t('admin.role.tenant_admin'), super_admin: t('admin.role.super_admin') }[role] || role;
    if (!confirm(t('admin.users.role_confirm', {role: label}))) { loadAdminUsers(); return; }
    const { ok, data } = await sendJson(`/api/admin/super/users/${id}/role`, 'POST', { role });
    if (!ok) { adminStatus('admin-status', (data && data.error) || t('admin.msg.failed'), true); loadAdminUsers(); return; }
    adminStatus('admin-status', t('admin.users.role_updated'), false);
    loadAdminUsers();
}

async function resetUserPassword(id) {
    const u = userCache.find(x => x.id === id);
    const pw = prompt(t('admin.users.reset_prompt', {email: u ? u.email : id}));
    if (pw === null) return;  // cancelled
    const body = pw.trim() ? { password: pw.trim() } : {};
    if (!pw.trim() && !confirm(t('admin.users.reset_email_confirm'))) return;
    const { ok, data } = await sendJson(`/api/admin/super/users/${id}/reset-password`, 'POST', body);
    adminStatus('admin-status', (data && (data.message || data.error)) || (ok ? t('admin.msg.done') : t('admin.msg.failed')), !ok);
}

async function disableAdminUser(id) {
    if (!confirm(t('admin.users.disable_confirm'))) return;
    const { ok, data } = await fetchJson(`/api/admin/users/${id}/disable`, { method: 'POST' });
    if (!ok) { adminStatus('admin-status', (data && data.error) || t('admin.msg.failed'), true); return; }
    adminStatus('admin-status', t('admin.users.disabled_ok'), false);
    loadAdminUsers();
}

async function restoreUser(id) {
    const { ok, data } = await fetchJson(`/api/admin/super/users/${id}/restore`, { method: 'POST' });
    if (!ok) { adminStatus('admin-status', (data && data.error) || t('admin.msg.failed'), true); return; }
    adminStatus('admin-status', t('admin.users.restored_ok'), false);
    loadAdminUsers();
}

async function deleteUser(id) {
    if (!confirm(t('admin.users.delete_confirm'))) return;
    const { ok, data } = await fetchJson(`/api/admin/super/users/${id}`, { method: 'DELETE' });
    if (!ok) { adminStatus('admin-status', (data && data.error) || t('admin.msg.failed'), true); return; }
    adminStatus('admin-status', t('admin.users.deleted_ok'), false);
    loadAdminUsers();
}

// ── Saved sizings ─────────────────────────────────────────────────────────────

async function loadAdminSizings() {
    const includeDeleted = document.getElementById('sizings-include-deleted').checked;
    const params = new URLSearchParams();
    if (includeDeleted) params.set('include_deleted', 'true');
    const { ok, data } = await fetchJson('/api/admin/super/configs?' + params.toString());
    const body = document.getElementById('admin-sizings-tbody');
    if (!ok) { body.innerHTML = ''; adminStatus('sizings-status-admin', t('admin.sizings.load_error'), true); return; }
    body.innerHTML = data.map(c => `
        <tr class="${c.is_deleted ? 'row-disabled' : ''}">
            <td>${esc(c.name)}</td>
            <td>${esc(c.owner_email || '')}</td>
            <td>${esc(c.tenant_domain || '')}</td>
            <td><code>${esc(c.code)}</code></td>
            <td>${c.is_deleted ? esc(t('admin.status.deleted')) : esc(t('admin.status.active'))}</td>
            <td>${fmtDateTime(c.updated_at)}</td>
            <td class="col-actions">
                <button class="btn btn-sm btn-danger" data-click='["purgeSizing",${c.id}]'>${esc(t('admin.sizings.purge'))}</button>
            </td>
        </tr>`).join('') || `<tr><td colspan="7">${esc(t('admin.sizings.none'))}</td></tr>`;
}

async function purgeSizing(id) {
    if (!confirm(t('admin.sizings.purge_confirm'))) return;
    const { ok, data } = await fetchJson(`/api/admin/super/configs/${id}/purge`, { method: 'DELETE' });
    if (!ok) { adminStatus('sizings-status-admin', (data && data.error) || t('admin.msg.failed'), true); return; }
    adminStatus('sizings-status-admin', t('admin.sizings.purged_ok'), false);
    loadAdminSizings();
}

async function runPurge() {
    if (!confirm(t('admin.sizings.run_purge_confirm'))) return;
    const { ok, data } = await fetchJson('/api/admin/super/purge-run', { method: 'POST' });
    if (!ok) { adminStatus('sizings-status-admin', t('admin.sizings.purge_failed'), true); return; }
    adminStatus('sizings-status-admin',
        t('admin.sizings.purge_run_ok', {configs: data.configs_purged, users: data.users_purged}), false);
    loadAdminSizings();
}


// ==================== SUPER-ADMIN: EMAIL / SMTP + AUDIT LOG ====================

async function loadEmailSettings() {
    const { ok, data } = await fetchJson('/api/admin/super/email-settings');
    if (!ok) { adminStatus('email-status', t('admin.email.load_error'), true); return; }
    document.getElementById('smtp-host').value = data.smtp_host || '';
    document.getElementById('smtp-port').value = data.smtp_port || '587';
    document.getElementById('smtp-from').value = data.smtp_from || '';
    document.getElementById('smtp-username').value = data.smtp_username || '';
    document.getElementById('smtp-password').value = '';
    document.getElementById('smtp-pass-set').textContent = data.smtp_password_set ? t('admin.email.pass_set') : t('admin.email.pass_none');
    document.getElementById('smtp-use-tls').checked = !!data.smtp_use_tls;
    // The checkbox reflects the EFFECTIVE state: once SMTP is configured,
    // verification is on unless it's within a temporary suspension window.
    document.getElementById('verify-email-enabled').checked = !!data.verification_active;
    const offMins = data.verify_off_minutes_remaining || 0;
    document.getElementById('email-active-state').textContent =
        data.verification_active ? t('admin.email.state_active')
        : (data.configured && offMins > 0) ? t('admin.email.state_off_temp', { mins: offMins })
        : data.configured ? t('admin.email.state_off')
        : t('admin.email.state_unconfigured');
}

async function saveEmailSettings() {
    const payload = {
        smtp_host: document.getElementById('smtp-host').value.trim(),
        smtp_port: document.getElementById('smtp-port').value.trim(),
        smtp_from: document.getElementById('smtp-from').value.trim(),
        smtp_username: document.getElementById('smtp-username').value.trim(),
        smtp_use_tls: document.getElementById('smtp-use-tls').checked,
        verify_email_enabled: document.getElementById('verify-email-enabled').checked,
    };
    const pw = document.getElementById('smtp-password').value;
    if (pw) payload.smtp_password = pw;
    const { ok, data } = await sendJson('/api/admin/super/email-settings', 'PUT', payload);
    if (!ok) { adminStatus('email-status', (data && data.error) || t('admin.email.save_failed'), true); return; }
    adminStatus('email-status', t('admin.email.saved_ok'), false);
    loadEmailSettings();
}

async function sendTestEmail() {
    const to = prompt(t('admin.email.test_prompt'));
    if (!to) return;
    adminStatus('email-status', t('admin.email.sending'), false);
    const { ok, data } = await sendJson('/api/admin/super/email-settings/test', 'POST', { to });
    adminStatus('email-status', (data && (data.message || data.error)) || (ok ? t('admin.email.sent') : t('admin.msg.failed')), !ok);
}

// Audit details are one line for almost every action. The email-failure rows
// carry three parts, separated by a blank line: a summary, a few lines of
// diagnosis, and a stack trace.
//
// They are rendered differently on purpose. The diagnosis is the sentence that
// decides who has to act, so it wraps as prose — inside the monospace trace box
// it was the line getting clipped by the horizontal scroll. The trace keeps its
// own formatting and scrolls, because reflowed tracebacks are unreadable.
function auditDetailCell(detail) {
    const text = String(detail || '').trim();
    const firstBreak = text.indexOf('\n');
    if (firstBreak === -1) return esc(text);

    const summary = text.slice(0, firstBreak);
    const rest = text.slice(firstBreak + 1);
    const split = rest.indexOf('\n\n');
    const cause = split === -1 ? '' : rest.slice(0, split).trim();
    const trace = split === -1 ? rest.trim() : rest.slice(split + 2).trim();

    let html = esc(summary);
    if (cause) html += `<div class="audit-cause">${esc(cause)}</div>`;
    if (trace) html += `<pre class="audit-trace">${esc(trace)}</pre>`;
    return html;
}

async function loadAuditLog() {
    const { ok, data } = await fetchJson('/api/admin/super/audit');
    const body = document.getElementById('admin-audit-tbody');
    if (!ok) { body.innerHTML = ''; return; }
    body.innerHTML = data.map(e => {
        // A failed outbound email is not a routine admin action — it is
        // something nobody asked for and nobody would otherwise see.
        const warn = String(e.action || '').indexOf('email_failed') === 0;
        return `
        <tr${warn ? ' class="audit-row-warn"' : ''}>
            <td>${fmtDateTime(e.created_at)}</td>
            <td>${esc(e.actor_email || '')}</td>
            <td><code>${esc(e.action)}</code></td>
            <td>${auditDetailCell(e.detail)}</td>
        </tr>`;
    }).join('') || `<tr><td colspan="4">${esc(t('admin.audit.none'))}</td></tr>`;
}


// ==================== SUPER-ADMIN: INACTIVE (1yr+) USERS ====================

async function loadStaleUsers() {
    const { ok, data } = await fetchJson('/api/admin/super/users/stale');
    const body = document.getElementById('stale-users-tbody');
    document.getElementById('stale-select-all').checked = false;
    if (!ok) { body.innerHTML = ''; adminStatus('stale-status', t('admin.stale.load_error'), true); return; }

    // Populate the per-domain selector from the result.
    const domains = [...new Set(data.map(u => u.tenant_domain).filter(Boolean))].sort();
    const sel = document.getElementById('stale-domain-select');
    sel.innerHTML = `<option value="">${esc(t('admin.stale.select_domain'))}</option>`
        + domains.map(d => `<option value="${esc(d)}">${esc(d)} (${data.filter(u => u.tenant_domain === d).length})</option>`).join('');

    body.innerHTML = data.map(u => `
        <tr>
            <td><input type="checkbox" class="stale-check" value="${u.id}" data-domain="${esc(u.tenant_domain)}" data-click='["updateStaleCount"]'></td>
            <td>${esc(u.email)}</td>
            <td>${esc(u.tenant_domain)}</td>
            <td>${esc(u.role)}</td>
            <td>${u.last_login_at ? fmtDateTime(u.last_login_at) : `<span class="muted">${esc(t('admin.stale.never'))}</span>`}</td>
            <td>${fmtDateTime(u.created_at)}</td>
        </tr>`).join('') || `<tr><td colspan="6">${esc(t('admin.stale.none'))}</td></tr>`;
    updateStaleCount();
}

function staleChecks() {
    return Array.from(document.querySelectorAll('.stale-check'));
}

function updateStaleCount() {
    const all = staleChecks();
    const checked = all.filter(c => c.checked);
    document.getElementById('stale-selected-count').textContent =
        checked.length ? t('admin.stale.selected_count', {n: checked.length}) : '';
    const head = document.getElementById('stale-select-all');
    head.checked = all.length > 0 && checked.length === all.length;
    head.indeterminate = checked.length > 0 && checked.length < all.length;
}

function toggleSelectAllStale(cb) {
    staleChecks().forEach(c => { c.checked = cb.checked; });
    updateStaleCount();
}

// Additively check every row of the chosen domain (so several domains can be
// built up), then reset the selector.
function selectStaleByDomain() {
    const sel = document.getElementById('stale-domain-select');
    const domain = sel.value;
    if (!domain) return;
    staleChecks().forEach(c => { if (c.dataset.domain === domain) c.checked = true; });
    sel.value = '';
    updateStaleCount();
}

async function purgeSelectedStale() {
    const ids = staleChecks().filter(c => c.checked).map(c => parseInt(c.value, 10));
    if (!ids.length) { adminStatus('stale-status', t('admin.stale.select_first'), true); return; }
    if (!confirm(t('admin.stale.purge_confirm', {n: ids.length}))) return;
    const { ok, data } = await sendJson('/api/admin/super/users/purge', 'POST', { ids });
    if (!ok) { adminStatus('stale-status', (data && data.error) || t('admin.sizings.purge_failed'), true); return; }
    adminStatus('stale-status', data.skipped
        ? t('admin.stale.purged_skipped', {purged: data.purged, skipped: data.skipped})
        : t('admin.stale.purged', {purged: data.purged}), false);
    loadStaleUsers();
}


// ==================== SUPER-ADMIN: HCL CATALOG / SCRAPE QUEUE / BOM REVIEWS ====================
// Front-end for docs/bom-checker-build.md §6 (admin endpoints) and §7. Every
// top-level name here is prefixed hcl… / bomReview… because admin.js shares one
// global scope with theme.js, i18n.js, delegate.js and toast.js. All server
// strings pass through esc() before they touch innerHTML.

let hclPollTimer = null;          // 3 s status poll while a scrape runs
let hclWasRunning = false;        // to refresh the queue once a run finishes
let hclLastRunId = null;          // from /stats or /scrape/status; drives the "latest run" filter
let hclRunFilterDefaulted = false; // one-time: the queue's Run select starts on "All runs"
let hclSearchTimer = null;        // debounce for the component search box
let hclExpandedPlatform = null;   // platform id whose detail row is open
let bomReviewRows = [];           // last list from /admin/api/bom-reviews
let bomReviewCurrent = null;      // row open in the modal
let bomReviewFull = null;         // its full check (result + normalised BOM), for part notes
let bomPrevData = null;           // /acceptable payload for the open review modal
let hclPreviewCache = { platforms: [], components: [] };  // last /hcl/preview payload, for row actions

const HCL_KIND_BADGE = { add: 'badge-validated', update: 'badge-eol', delist: 'badge-eos', relist: 'badge-active', merge: 'badge-accent' };
const HCL_VERDICT_BADGE = { PASS: 'badge-active', FAIL: 'badge-eos', INCONCLUSIVE: 'badge-eol' };
const HCL_FIT_BADGE = { match: 'badge-active', bigger: 'badge-validated', smaller: 'badge-eos', unknown: 'hcl-badge-plain' };
const HCL_REVIEW_BADGE = { open: 'badge-eol', confirmed: 'badge-active', incorrect: 'badge-eos', none: 'hcl-badge-plain' };
const HCL_SEVERITY_BADGE = { error: 'badge-eos', warning: 'badge-eol', info: 'badge-validated' };

// ── shared helpers ────────────────────────────────────────────────────────


function hclYesNo(v) {
    return v ? t('common.yes') : t('common.no');
}

// "3 days ago" for the stats line; falls back to the absolute date beyond a month.
function hclRelTime(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d)) return '';
    const mins = Math.round((Date.now() - d.getTime()) / 60000);
    if (mins < 1) return t('admin.hcl.ago_just_now');
    if (mins < 60) return t('admin.hcl.ago_minutes', { n: mins });
    const hours = Math.round(mins / 60);
    if (hours < 48) return t('admin.hcl.ago_hours', { n: hours });
    const days = Math.round(hours / 24);
    if (days <= 31) return t('admin.hcl.ago_days', { n: days });
    return fmtDateTime(iso);
}

function hclTruncate(s, max) {
    s = String(s == null ? '' : s);
    return s.length > max ? s.slice(0, max - 1) + '…' : s;
}

// Compact HTML rendering of a pending-change value (JSON): lists become
// "N items" with the items in a tooltip, dicts become key: value lines,
// long strings are cut with the full text in the title.
function hclFmtValue(v) {
    if (v == null || v === '') return '<span class="muted">—</span>';
    if (Array.isArray(v)) {
        const items = v.map(x => typeof x === 'object' ? JSON.stringify(x) : String(x));
        return `<span class="hcl-val" title="${esc(items.join('\n'))}">${esc(t('admin.hcl.n_items', { n: v.length }))}</span>`;
    }
    if (typeof v === 'object') {
        const keys = Object.keys(v);
        if (!keys.length) return '<span class="muted">—</span>';
        const lines = keys.map(k => {
            const val = v[k];
            const text = (val != null && typeof val === 'object') ? JSON.stringify(val) : String(val == null ? '—' : val);
            return `<div class="hcl-kv" title="${esc(text)}"><span class="hcl-kv-key">${esc(k)}</span>: ${esc(hclTruncate(text, 48))}</div>`;
        });
        return lines.join('');
    }
    if (typeof v === 'boolean') return esc(hclYesNo(v));
    const s = String(v);
    if (s.length > 60) return `<span class="hcl-val" title="${esc(s)}">${esc(hclTruncate(s, 60))}</span>`;
    return esc(s);
}

function hclStatusBadge(status) {
    const active = status === 'active';
    const label = active ? t('admin.hcl.status_active') : t('admin.hcl.status_delisted');
    return `<span class="badge ${active ? 'badge-active' : 'badge-eos'}">${esc(label)}</span>`;
}

// Parts accepted from a BOM review that the HCL team has confirmed but not
// yet published on the HCL carry origin "preview" until a scrape sees them.
function hclPreviewBadge(origin) {
    if (origin !== 'preview') return '';
    return ` <span class="badge badge-validated hcl-badge-tight" title="${esc(t('admin.bomprev.badge_title'))}">${esc(t('admin.bomprev.badge'))}</span>`;
}

function hclComponentKindLabel(kind) {
    return tOr('admin.hcl.ckind_' + kind, String(kind || '').toUpperCase());
}

function hclEntityLabel(entityType) {
    return tOr('admin.hcl.entity_' + entityType, String(entityType || ''));
}

// ── tab entry ─────────────────────────────────────────────────────────────

async function loadHcl() {
    // Rows from earlier runs stay pending when a later run doesn't re-diff
    // their entities, so the "Latest run" view can hide changes the pending
    // pill counts. Default to "All runs" so the table matches the pill; the
    // admin can still narrow to the latest run by hand.
    if (!hclRunFilterDefaulted) {
        const runSel = document.getElementById('hcl-pending-run');
        if (runSel) runSel.value = 'all';
        hclRunFilterDefaulted = true;
    }
    hclCatalogKindChanged(true);      // sync filter visibility without reloading twice
    await hclLoadStats();             // first: it supplies hclLastRunId for the queue filter
    await Promise.all([hclLoadScrapeStatus(), loadHclPending(), loadHclCatalog(),
                       hclLoadSettings(), hclLoadPreviewCount()]);
}

// Fills the feed-token field from the settings route. Errors are silent: the
// stats loader already reports an unreachable backend on this tab.
async function hclLoadSettings() {
    const { ok, data } = await fetchJson('/admin/api/hcl/settings');
    if (!ok || !data) return;
    const input = document.getElementById('hcl-feed-token');
    if (input && typeof data.preview_feed_token === 'string' && document.activeElement !== input) {
        input.value = data.preview_feed_token;
    }
}

// Small "N pre-publication parts" line in the settings card, from the same
// data the HCL team's pull feed serves.
async function hclLoadPreviewCount() {
    const el = document.getElementById('hcl-preview-count');
    const body = document.getElementById('hcl-preview-tbody');
    const { ok, data } = await fetchJson('/admin/api/hcl/preview');
    const comps = (ok && data && Array.isArray(data.components)) ? data.components : [];
    const plats = (ok && data && Array.isArray(data.platforms)) ? data.platforms : [];
    hclPreviewCache = { platforms: plats, components: comps };
    if (el) el.textContent = ok ? t('admin.bomprev.count', { n: comps.length }) : '';
    if (!body) return;
    const platRows = plats.map(p => `
        <tr>
            <td><span class="badge badge-accent">${esc(t('admin.bomprev.row_platform'))}</span></td>
            <td><code>${esc(p.sc_model)}</code></td>
            <td>${esc(p.server || '')}</td>
            <td>${esc(p.brand || '')}</td>
            <td>${fmtDateTime(p.first_seen)}</td>
            <td class="col-actions">
                <button class="btn btn-sm btn-danger"
                    data-click='["hclWithdrawPreview","platform",${p.id}]'
                    >${esc(t('admin.bomprev.withdraw'))}</button>
            </td>
        </tr>`).join('');
    const compRows = comps.map(c => {
        const on = (c.platforms || []).map(p => p.sc_model).join(', ');
        return `
        <tr>
            <td>${esc(hclComponentKindLabel(c.kind))}</td>
            <td><code>${esc(c.part_number)}</code></td>
            <td>${esc(c.description || '')}</td>
            <td>${esc(on) || `<span class="muted">${esc(t('admin.bomprev.unlinked'))}</span>`}</td>
            <td>${fmtDateTime(c.first_seen)}</td>
            <td class="col-actions">
                <button class="btn btn-sm btn-danger"
                    data-click='["hclWithdrawPreview","component",${c.id}]'
                    >${esc(t('admin.bomprev.withdraw'))}</button>
            </td>
        </tr>`;
    }).join('');
    body.innerHTML = (platRows + compRows) ||
        `<tr><td colspan="6">${esc(t('admin.bomprev.list_none'))}</td></tr>`;
}

// Taking an acceptance back. Spelled out in the confirm because a BOM check
// that passed on this part will report its finding again after a re-check.
async function hclWithdrawPreview(entityType, id) {
    const list = entityType === 'platform' ? hclPreviewCache.platforms : hclPreviewCache.components;
    const row = (list || []).find(x => x.id === id);
    const label = row ? (entityType === 'platform' ? row.key : row.part_number) : String(id);
    if (!confirm(t('admin.bomprev.withdraw_confirm', { name: label }))) return;
    const { ok, data } = await sendJson(
        '/admin/api/hcl/preview/' + encodeURIComponent(entityType) + '/' + id + '/withdraw', 'POST', {});
    if (!ok) { adminStatus('hcl-status', errorText(data), true); return; }
    adminStatus('hcl-status', t('admin.bomprev.withdraw_done', { name: label }), false);
    hclLoadPreviewCount();
    hclLoadStats();
    loadHclCatalog();
}

async function hclLoadStats() {
    const { ok, data } = await fetchJson('/admin/api/hcl/stats');
    const el = document.getElementById('hcl-stats');
    if (!ok || !data) { el.textContent = t('admin.hcl.load_error'); return; }
    const active = o => (o && o.active != null) ? o.active : 0;
    const last = data.last_run || null;
    // A run still in progress has no changes yet; keep filtering on the previous one.
    if (last && last.id != null && !data.running) hclLastRunId = last.id;
    const parts = [
        t('admin.hcl.stats_platforms', { n: active(data.platforms) }),
        t('admin.hcl.stats_components', { n: active(data.components) }),
        t('admin.hcl.stats_devices', { n: active(data.devices) }),
        t('admin.hcl.stats_pending', { n: data.pending || 0 }),
    ];
    if (data.running) parts.push(t('admin.hcl.stats_running'));
    else if (!last) parts.push(t('admin.hcl.stats_never'));
    else if (last.status === 'failed') parts.push(t('admin.hcl.stats_last_failed', { when: hclRelTime(last.finished_at || last.started_at) }));
    else parts.push(t('admin.hcl.stats_last', { when: hclRelTime(last.finished_at || last.started_at) }));
    el.textContent = parts.join(' · ');
    const pill = document.getElementById('hcl-pending-count');
    if (pill) pill.textContent = String(data.pending || 0);
    const vendors = document.getElementById('hcl-blocked-vendors');
    if (vendors && Array.isArray(data.blocked_vendors) && document.activeElement !== vendors) {
        vendors.value = data.blocked_vendors.join(', ');
    }
}

// ── scrape card ───────────────────────────────────────────────────────────

async function hclLoadScrapeStatus() {
    const { ok, data } = await fetchJson('/admin/api/hcl/scrape/status');
    const wrap = document.getElementById('hcl-progress-wrap');
    const bar = document.getElementById('hcl-progress');
    const state = document.getElementById('hcl-scrape-state');
    const btn = document.getElementById('hcl-scrape-btn');
    const importBtn = document.getElementById('hcl-import-btn');
    clearTimeout(hclPollTimer);
    if (!ok || !data) {
        wrap.hidden = true;
        adminStatus('hcl-status', t('admin.hcl.load_error'), true);
        return;
    }
    const last = data.last || null;
    const running = !!data.running;
    if (last && last.id != null && !running) hclLastRunId = last.id;
    btn.disabled = running;
    importBtn.disabled = running;
    if (running && last) {
        const pct = Math.max(0, Math.min(100, last.progress || 0));
        wrap.hidden = false;
        bar.setAttribute('aria-valuenow', String(pct));
        bar.firstElementChild.style.width = pct + '%';
        state.className = 'iops-status';
        state.textContent = t('admin.hcl.scrape_running', { done: last.pages_done || 0, total: last.pages_total || 0, pct: pct });
        hclPollTimer = setTimeout(hclLoadScrapeStatus, 3000);
    } else {
        wrap.hidden = true;
        state.textContent = '';
    }
    hclRenderLastRun(last, running);
    if (hclWasRunning && !running) {
        // A run just finished: the queue, the stats line and the catalog counts all moved.
        toast(last && last.status === 'failed' ? t('admin.hcl.scrape_failed') : t('admin.hcl.scrape_finished'),
              last && last.status === 'failed' ? 'error' : 'success');
        hclLoadStats();
        loadHclPending();
        loadHclCatalog();
    }
    hclWasRunning = running;
}

function hclRenderLastRun(last, running) {
    const el = document.getElementById('hcl-last-run');
    if (!last) { el.innerHTML = `<p class="muted">${esc(t('admin.hcl.never_scraped'))}</p>`; return; }
    const summary = last.summary || {};
    const changes = summary.changes || {};
    const errors = Array.isArray(last.errors) ? last.errors : [];
    const statusKey = 'admin.hcl.run_status_' + (last.status || 'queued');
    const statusCls = last.status === 'succeeded' ? 'badge-active' : last.status === 'failed' ? 'badge-eos' : 'badge-eol';
    const rows = [
        [t('admin.hcl.run_when'), esc(fmtDateTime(last.finished_at || last.started_at))],
        [t('common.status'), `<span class="badge ${statusCls}">${esc(tOr(statusKey, last.status || ''))}</span>`],
        [t('admin.hcl.run_source'), esc(tOr('admin.hcl.source_' + (last.source || 'scrape'), last.source || ''))],
        [t('admin.hcl.run_pages'), esc(`${last.pages_done || 0} / ${last.pages_total || 0}`)],
        [t('admin.hcl.run_complete'), last.complete
            ? esc(t('common.yes'))
            : `<span class="hcl-warn" title="${esc(t('admin.hcl.run_incomplete_hint'))}">${esc(t('admin.hcl.run_incomplete'))}</span>`],
        [t('admin.hcl.run_changes'), esc(t('admin.hcl.run_changes_detail', {
            add: changes.add || 0, update: changes.update || 0, delist: changes.delist || 0, relist: changes.relist || 0 }))],
    ];
    let html = `<h4 class="hcl-last-run-title">${esc(running ? t('admin.hcl.current_run') : t('admin.hcl.last_run'))}</h4>`
        + '<dl class="hcl-run-grid">'
        + rows.map(r => `<dt>${esc(r[0])}</dt><dd>${r[1]}</dd>`).join('')
        + '</dl>';
    if (last.error) html += `<p class="hcl-run-error">${esc(last.error)}</p>`;
    if (errors.length) {
        html += `<details class="hcl-run-errors"><summary>${esc(t('admin.hcl.run_errors', { n: errors.length }))}</summary><ul>`
            + errors.map(e => `<li>${esc(typeof e === 'string' ? e : JSON.stringify(e))}</li>`).join('')
            + '</ul></details>';
    } else {
        html += `<p class="muted">${esc(t('admin.hcl.run_no_errors'))}</p>`;
    }
    el.innerHTML = html;
}

async function hclStartScrape() {
    if (!confirm(t('admin.hcl.scrape_confirm'))) return;
    const btn = document.getElementById('hcl-scrape-btn');
    const { ok, data } = await withBusy(btn, () => fetchJson('/admin/api/hcl/scrape', { method: 'POST' }));
    if (!ok) { adminStatus('hcl-status', errorText(data), true); return; }
    btn.disabled = true;   // stays off until the status poll reports the run finished
    adminStatus('hcl-status', t('admin.hcl.scrape_started'), false);
    hclWasRunning = true;
    hclLoadScrapeStatus();
}

async function hclImportSnapshot() {
    const input = document.getElementById('hcl-snapshot-file');
    if (!input.files.length) { adminStatus('hcl-status', t('admin.hcl.import_pick_file'), true); return; }
    const fd = new FormData();
    fd.append('file', input.files[0]);
    const result = await withBusy('hcl-import-btn',
        () => fetchJson('/admin/api/hcl/import-snapshot', { method: 'POST', body: fd }));
    if (!result.ok) { adminStatus('hcl-status', errorText(result.data), true); return; }
    input.value = '';
    adminStatus('hcl-status', t('admin.hcl.import_started'), false);
    hclWasRunning = true;
    await hclLoadScrapeStatus();
    // A synchronous import is already finished at this point; make sure the queue reflects it.
    hclLoadStats();
    loadHclPending();
    loadHclCatalog();
}

// ── pending changes ───────────────────────────────────────────────────────

function hclPendingFilter() {
    const filter = { status: 'pending' };
    const entity = document.getElementById('hcl-pending-entity').value;
    const kind = document.getElementById('hcl-pending-kind').value;
    const run = document.getElementById('hcl-pending-run').value;
    if (entity) filter.entity_type = entity;
    if (kind) filter.kind = kind;
    if (run === 'latest' && hclLastRunId != null) filter.run_id = hclLastRunId;
    return filter;
}

async function loadHclPending() {
    const filter = hclPendingFilter();
    const params = new URLSearchParams();
    Object.keys(filter).forEach(k => params.set(k, String(filter[k])));
    const { ok, data } = await fetchJson('/admin/api/hcl/pending?' + params.toString());
    const body = document.getElementById('hcl-pending-tbody');
    const head = document.getElementById('hcl-pending-select-all');
    head.checked = false; head.indeterminate = false;
    if (!ok || !Array.isArray(data)) { body.innerHTML = ''; adminStatus('hcl-status', t('admin.hcl.load_error'), true); return; }
    body.innerHTML = data.map(p => `
        <tr>
            <td><input type="checkbox" class="hcl-pending-check" value="${p.id}" aria-label="${esc(t('admin.hcl.select_row'))}" data-click='["hclUpdatePendingCount"]'></td>
            <td><span class="badge ${HCL_KIND_BADGE[p.change_kind] || 'badge-validated'}">${esc(tOr('admin.hcl.kind_' + p.change_kind, p.change_kind))}</span></td>
            <td>${esc(hclEntityLabel(p.entity_type))}</td>
            <td><strong>${esc(p.label || p.entity_key || '')}</strong>${p.label && p.entity_key ? `<div class="muted hcl-key">${esc(p.entity_key)}</div>` : ''}</td>
            <td>${esc(p.field || '')}</td>
            <td class="hcl-val-cell">${hclFmtValue(p.old)}</td>
            <td class="hcl-val-cell">${hclFmtValue(p.new)}</td>
            <td class="hcl-nowrap">${esc(fmtDateTime(p.created_at))}</td>
            <td class="col-actions hcl-col-actions">
                <button class="btn btn-sm btn-secondary" data-click='["hclPendingAction",${p.id},"approve"]'>${esc(t('admin.hcl.approve'))}</button>
                <button class="btn btn-sm btn-danger" data-click='["hclPendingAction",${p.id},"reject"]'>${esc(t('admin.hcl.reject'))}</button>
            </td>
        </tr>`).join('') || `<tr><td colspan="9" class="muted">${esc(t('admin.hcl.pending_none'))}</td></tr>`;
    hclUpdatePendingCount();
}

function hclPendingChecks() {
    return Array.from(document.querySelectorAll('.hcl-pending-check'));
}

function hclUpdatePendingCount() {
    const all = hclPendingChecks();
    const checked = all.filter(c => c.checked);
    document.getElementById('hcl-pending-selected').textContent =
        checked.length ? t('admin.hcl.selected_count', { n: checked.length }) : '';
    const head = document.getElementById('hcl-pending-select-all');
    head.checked = all.length > 0 && checked.length === all.length;
    head.indeterminate = checked.length > 0 && checked.length < all.length;
}

function hclToggleSelectAllPending(cb) {
    hclPendingChecks().forEach(c => { c.checked = cb.checked; });
    hclUpdatePendingCount();
}

function hclAfterQueueChange() {
    hclLoadStats();
    loadHclPending();
    loadHclCatalog();
}

async function hclPendingAction(id, action) {
    const { ok, data } = await fetchJson(`/admin/api/hcl/pending/${id}/${action}`, { method: 'POST' });
    if (!ok) { adminStatus('hcl-status', errorText(data), true); return; }
    toast((data && data.message) || t(action === 'approve' ? 'admin.hcl.approved_one' : 'admin.hcl.rejected_one'), 'success');
    hclAfterQueueChange();
}

async function hclBulkPending(action) {
    const ids = hclPendingChecks().filter(c => c.checked).map(c => parseInt(c.value, 10));
    if (!ids.length) { adminStatus('hcl-status', t('admin.hcl.select_first'), true); return; }
    if (!confirm(t(action === 'approve' ? 'admin.hcl.bulk_confirm_approve' : 'admin.hcl.bulk_confirm_reject', { n: ids.length }))) return;
    const { ok, data } = await sendJson('/admin/api/hcl/pending/bulk', 'POST', { ids: ids, action: action });
    if (!ok) { adminStatus('hcl-status', errorText(data), true); return; }
    adminStatus('hcl-status', t('admin.hcl.bulk_done', {
        approved: data.approved || 0, rejected: data.rejected || 0, skipped: data.skipped || 0 }), false);
    hclAfterQueueChange();
}

// "Approve all pending" approves everything the current filters MATCH on the
// server, not just the rendered page (the list endpoint caps at 500 rows), so
// the confirm count comes from the server-side stats total whenever the view
// is unfiltered; a narrowed view falls back to the rendered rows.
async function hclApproveAllPending() {
    if (!hclPendingChecks().length) { adminStatus('hcl-status', t('admin.hcl.pending_none'), true); return; }
    const filter = hclPendingFilter();
    let n = hclPendingChecks().length;
    if (!filter.entity_type && !filter.kind && filter.run_id == null) {
        // Re-fetch so a stale page can't understate what the bulk will apply.
        const stats = await fetchJson('/admin/api/hcl/stats');
        if (stats.ok && stats.data && stats.data.pending != null) n = stats.data.pending;
    }
    if (!n) { adminStatus('hcl-status', t('admin.hcl.pending_none'), true); return; }
    if (!confirm(t('admin.hcl.approve_all_confirm', { n: n }))) return;
    const { ok, data } = await sendJson('/admin/api/hcl/pending/bulk', 'POST', { all: true, filter: filter, action: 'approve' });
    if (!ok) { adminStatus('hcl-status', errorText(data), true); return; }
    adminStatus('hcl-status', t('admin.hcl.bulk_done', {
        approved: data.approved || 0, rejected: data.rejected || 0, skipped: data.skipped || 0 }), false);
    hclAfterQueueChange();
}

// ── catalog ───────────────────────────────────────────────────────────────

function hclCatalogKindChanged(skipLoad) {
    const kind = document.getElementById('hcl-catalog-kind').value;
    const isComponents = kind === 'components';
    document.getElementById('hcl-component-kind-wrap').hidden = !isComponents;
    document.getElementById('hcl-origin-wrap').hidden = !isComponents;
    document.getElementById('hcl-search-wrap').hidden = !isComponents;
    hclExpandedPlatform = null;
    if (skipLoad !== true) loadHclCatalog();
}

function hclCatalogSearchInput() {
    clearTimeout(hclSearchTimer);
    hclSearchTimer = setTimeout(loadHclCatalog, 300);
}

async function loadHclCatalog() {
    const kind = document.getElementById('hcl-catalog-kind').value;
    const status = document.getElementById('hcl-catalog-status').value;
    const params = new URLSearchParams({ status: status });
    if (kind === 'components') {
        const ck = document.getElementById('hcl-component-kind').value;
        const q = document.getElementById('hcl-catalog-search').value.trim();
        if (ck) params.set('kind', ck);
        if (q) params.set('q', q);
    }
    const { ok, data } = await fetchJson(`/admin/api/hcl/${kind}?` + params.toString());
    const thead = document.getElementById('hcl-catalog-thead');
    const body = document.getElementById('hcl-catalog-tbody');
    const count = document.getElementById('hcl-catalog-count');
    if (!ok || !Array.isArray(data)) {
        thead.innerHTML = ''; body.innerHTML = ''; count.textContent = '';
        adminStatus('hcl-status', t('admin.hcl.load_error'), true);
        return;
    }
    count.textContent = t('admin.hcl.catalog_count', { n: data.length });
    const th = keys => '<tr>' + keys.map(k => `<th>${esc(t(k))}</th>`).join('') + '</tr>';
    const empty = cols => `<tr><td colspan="${cols}" class="muted">${esc(t('admin.hcl.catalog_none'))}</td></tr>`;
    if (kind === 'platforms') {
        thead.innerHTML = th(['admin.hcl.col_brand', 'admin.hcl.col_sc_model', 'admin.hcl.col_server', 'admin.hcl.col_form_factor',
            'admin.hcl.col_sockets', 'admin.hcl.col_memory_type', 'admin.hcl.col_max_ram', 'admin.hcl.col_components',
            'common.status', 'admin.hcl.col_last_seen']);
        body.innerHTML = data.map(p => hclPlatformRow(p)).join('') || empty(10);
        if (hclExpandedPlatform != null && !data.some(p => p.id === hclExpandedPlatform)) hclExpandedPlatform = null;
        if (hclExpandedPlatform != null) hclTogglePlatform(hclExpandedPlatform, true);
    } else if (kind === 'components') {
        thead.innerHTML = th(['admin.hcl.col_kind', 'admin.hcl.col_part_number', 'admin.hcl.col_description', 'admin.hcl.col_tce',
            'common.status', 'admin.hcl.col_last_seen', 'admin.hcl.col_platform_count']);
        // Origin is not a server-side filter; the rows carry it, so filter here.
        const originWanted = document.getElementById('hcl-origin-filter').value;
        const rows = originWanted ? data.filter(c => (c.origin || 'scrape') === originWanted) : data;
        count.textContent = t('admin.hcl.catalog_count', { n: rows.length });
        body.innerHTML = rows.map(c => {
            const platforms = Array.isArray(c.platforms) ? c.platforms : null;
            const pc = c.platform_count != null ? c.platform_count : (platforms ? platforms.length : null);
            const title = platforms ? platforms.map(p => p.key || `${p.brand}/${p.sc_model}`).join('\n') : '';
            return `
        <tr class="${c.status === 'active' ? '' : 'row-disabled'}">
            <td>${esc(hclComponentKindLabel(c.kind))}</td>
            <td><code>${esc(c.part_number)}</code>${hclPreviewBadge(c.origin)}</td>
            <td>${esc(c.description || '')}</td>
            <td>${c.tce ? `<span class="badge badge-validated hcl-badge-tight">TCE</span>` : '<span class="muted">—</span>'}</td>
            <td>${hclStatusBadge(c.status)}</td>
            <td class="hcl-nowrap">${esc(fmtDateTime(c.last_seen))}</td>
            <td${title ? ` title="${esc(title)}"` : ''}>${pc == null ? '<span class="muted">—</span>' : esc(String(pc))}</td>
        </tr>`; }).join('') || empty(7);
    } else {
        thead.innerHTML = th(['admin.hcl.col_vendor_id', 'admin.hcl.col_device_id', 'admin.hcl.col_description', 'admin.hcl.col_driver',
            'admin.hcl.col_type', 'admin.hcl.col_supported', 'admin.hcl.col_since', 'common.status']);
        body.innerHTML = data.map(d => `
        <tr class="${d.status === 'active' ? '' : 'row-disabled'}">
            <td><code>${esc(d.ven_id)}</code></td>
            <td><code>${esc(d.dev_id)}</code></td>
            <td>${esc(d.description || '')}</td>
            <td>${esc(d.driver || '')}</td>
            <td>${esc(d.dev_type || '')}</td>
            <td>${esc(hclYesNo(d.supported))}</td>
            <td>${esc(d.since || '')}</td>
            <td>${hclStatusBadge(d.status)}</td>
        </tr>`).join('') || empty(8);
    }
}

function hclPlatformRow(p) {
    const open = hclExpandedPlatform === p.id;
    return `
        <tr class="hcl-row-click ${p.status === 'active' ? '' : 'row-disabled'}" id="hcl-platform-row-${p.id}" tabindex="0" role="button"
            aria-expanded="${open ? 'true' : 'false'}" aria-controls="hcl-platform-detail-${p.id}"
            data-click='["hclTogglePlatform",${p.id}]' data-keydown='["hclPlatformKeydown","$event",${p.id}]'>
            <td>${esc(p.brand || '')}</td>
            <td><strong>${esc(p.sc_model || '')}</strong></td>
            <td>${esc(p.server || '')}</td>
            <td>${esc(p.form_factor || '')}</td>
            <td>${esc(p.sockets == null ? '' : String(p.sockets))}</td>
            <td>${esc(p.memory_type || '')}</td>
            <td>${p.max_ram_gb == null ? '' : esc(t('admin.hcl.gb', { n: p.max_ram_gb }))}</td>
            <td>${esc(String(p.component_count == null ? '' : p.component_count))}</td>
            <td>${hclStatusBadge(p.status)}</td>
            <td class="hcl-nowrap">${esc(fmtDateTime(p.last_seen))}</td>
        </tr>`;
}

function hclPlatformKeydown(e, id) {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); hclTogglePlatform(id); }
}

// Click a platform row: open (or close) an inline detail row underneath it
// listing every part validated for that platform, grouped by kind.
async function hclTogglePlatform(id, forceOpen) {
    const row = document.getElementById(`hcl-platform-row-${id}`);
    if (!row) return;
    const existing = document.getElementById(`hcl-platform-detail-${id}`);
    if (existing && forceOpen !== true) {
        existing.remove();
        row.setAttribute('aria-expanded', 'false');
        hclExpandedPlatform = null;
        return;
    }
    // Only one detail open at a time.
    document.querySelectorAll('.hcl-detail-row').forEach(r => r.remove());
    document.querySelectorAll('.hcl-row-click[aria-expanded="true"]').forEach(r => r.setAttribute('aria-expanded', 'false'));
    hclExpandedPlatform = id;
    row.setAttribute('aria-expanded', 'true');
    const detail = document.createElement('tr');
    detail.className = 'hcl-detail-row';
    detail.id = `hcl-platform-detail-${id}`;
    detail.innerHTML = `<td colspan="10"><span class="muted">${esc(t('common.loading'))}</span></td>`;
    row.insertAdjacentElement('afterend', detail);
    const { ok, data } = await fetchJson(`/admin/api/hcl/platforms/${id}`);
    if (hclExpandedPlatform !== id) return;   // closed or switched meanwhile
    if (!ok || !data) {
        detail.innerHTML = `<td colspan="10"><span class="hcl-run-error">${esc(errorText(data))}</span></td>`;
        return;
    }
    detail.innerHTML = `<td colspan="10">${hclPlatformDetailHtml(data)}</td>`;
}

function hclPlatformDetailHtml(p) {
    const facts = [
        ['admin.hcl.col_socket', p.socket],
        ['admin.hcl.col_max_cores', p.max_cores],
        ['admin.hcl.col_ram_slots', p.ram_slots],
        ['admin.hcl.col_hdd_max', p.hdd_max],
        ['admin.hcl.col_ssd_max', p.ssd_max],
        ['admin.hcl.col_power_supply', p.power_supply],
        ['admin.hcl.col_cooling', p.cooling],
        ['admin.hcl.col_tpm', p.tpm],
        ['admin.hcl.col_risers', p.risers],
        ['admin.hcl.col_oob_license', p.oob_license],
        ['admin.hcl.col_nic_listed', p.nic_listed == null ? null : hclYesNo(p.nic_listed)],
        ['admin.hcl.col_first_seen', p.first_seen ? fmtDateTime(p.first_seen) : null],
        ['admin.hcl.col_delisted_at', p.delisted_at ? fmtDateTime(p.delisted_at) : null],
    ].filter(f => f[1] != null && f[1] !== '');
    let html = '<div class="hcl-detail">';
    if (facts.length) {
        html += '<dl class="hcl-run-grid hcl-detail-facts">'
            + facts.map(f => `<dt>${esc(t(f[0]))}</dt><dd>${esc(String(f[1]))}</dd>`).join('')
            + '</dl>';
    }
    const comps = Array.isArray(p.components) ? p.components : [];
    if (!comps.length) {
        html += `<p class="muted">${esc(t('admin.hcl.platform_no_components'))}</p></div>`;
        return html;
    }
    const groups = {};
    comps.forEach(c => { (groups[c.kind] = groups[c.kind] || []).push(c); });
    const order = ['cpu', 'nic', 'hba', 'hdd', 'ssd', 'gpu'].concat(Object.keys(groups).filter(k => ['cpu', 'nic', 'hba', 'hdd', 'ssd', 'gpu'].indexOf(k) === -1));
    html += '<div class="hcl-detail-groups">';
    order.forEach(kind => {
        const list = groups[kind];
        if (!list) return;
        html += `<section class="hcl-detail-group"><h4>${esc(hclComponentKindLabel(kind))} <span class="count-pill">${list.length}</span></h4><ul>`
            + list.map(c => `<li class="${(c.link_status || c.status) === 'active' ? '' : 'row-disabled'}">`
                + `<code>${esc(c.part_number)}</code> ${esc(c.description || '')}`
                + (c.tce ? ' <span class="badge badge-validated hcl-badge-tight">TCE</span>' : '')
                + ((c.link_status || c.status) === 'active' ? '' : ` <span class="badge badge-eos hcl-badge-tight">${esc(t('admin.hcl.status_delisted'))}</span>`)
                + '</li>').join('')
            + '</ul></section>';
    });
    html += '</div></div>';
    return html;
}

// ── settings ──────────────────────────────────────────────────────────────

async function hclSaveSettings() {
    const input = document.getElementById('hcl-blocked-vendors');
    const vendors = input.value.split(',').map(s => s.trim().toLowerCase()).filter(Boolean);
    const unique = vendors.filter((v, i) => vendors.indexOf(v) === i);
    const tokenInput = document.getElementById('hcl-feed-token');
    const token = tokenInput.value.trim();
    if (token && token.length < 16) {
        adminStatus('hcl-status', t('admin.hcl.feed_too_short'), true);
        return;
    }
    const { ok, data } = await withBusy('hcl-settings-save', () => sendJson('/admin/api/hcl/settings', 'PUT',
        { blocked_vendors: unique, preview_feed_token: token }));
    if (!ok) { adminStatus('hcl-status', errorText(data), true); return; }
    if (data && Array.isArray(data.blocked_vendors)) input.value = data.blocked_vendors.join(', ');
    else input.value = unique.join(', ');
    if (data && typeof data.preview_feed_token === 'string') tokenInput.value = data.preview_feed_token;
    else tokenInput.value = token;
    toast(t('admin.msg.saved'), 'success');
}

// ==================== SUPER-ADMIN: BOM REVIEW QUEUE ====================
// Checks whose result carried flag_reasons wait here. The admin confirms the
// findings, marks them incorrect (with a note the user sees) or reopens.

function bomReviewVerdictChip(v) {
    if (!v) return '<span class="muted">—</span>';
    const cls = HCL_VERDICT_BADGE[v] || 'hcl-badge-plain';
    return `<span class="badge ${cls}">${esc(tOr('admin.bom.verdict_' + String(v).toLowerCase(), v))}</span>`;
}

function bomReviewFitChip(v) {
    if (!v) return '<span class="muted">—</span>';
    const cls = HCL_FIT_BADGE[v] || 'hcl-badge-plain';
    return `<span class="badge ${cls}">${esc(tOr('admin.bom.fit_' + v, v))}</span>`;
}

function bomReviewStatusChip(s) {
    s = s || 'none';
    const cls = HCL_REVIEW_BADGE[s] || 'hcl-badge-plain';
    return `<span class="badge ${cls}">${esc(tOr('admin.bom.status_' + s, s))}</span>`;
}

function bomReviewReasonText(code) {
    return tOr('admin.bom.reason_' + code, code);
}

async function loadBomReviews() {
    const status = document.getElementById('bom-review-filter').value;
    const { ok, data } = await fetchJson('/admin/api/bom-reviews?status=' + encodeURIComponent(status));
    const body = document.getElementById('bom-reviews-tbody');
    const count = document.getElementById('bom-review-count');
    if (!ok || !Array.isArray(data)) {
        body.innerHTML = ''; count.textContent = '';
        adminStatus('bom-review-status', t('admin.bom.load_error'), true);
        return;
    }
    bomReviewRows = data;
    count.textContent = t('admin.bom.count', { n: data.length });
    body.innerHTML = data.map(r => {
        const project = r.project || {};
        const reasons = Array.isArray(r.flag_reasons) ? r.flag_reasons : [];
        const reasonTitle = reasons.map(bomReviewReasonText).join('\n');
        return `
        <tr>
            <td class="hcl-nowrap">${esc(fmtDateTime(r.created_at))}</td>
            <td>${esc(project.name || '')}${project.code ? ` <code>${esc(project.code)}</code>` : ''}</td>
            <td>${esc(r.owner_email || '')}</td>
            <td>${esc(r.tenant_domain || '')}</td>
            <td title="${esc(r.filename || '')}">${esc(hclTruncate(r.filename || r.name || '', 40))}</td>
            <td>${bomReviewVerdictChip(r.technical_verdict)}</td>
            <td>${bomReviewFitChip(r.fit_verdict)}</td>
            <td class="hcl-reasons" title="${esc(reasonTitle)}">${reasons.length ? esc(reasons.join(', ')) : '<span class="muted">—</span>'}</td>
            <td>${bomReviewStatusChip(r.review_status)}</td>
            <td class="col-actions hcl-col-actions">
                <button class="btn btn-sm btn-secondary" data-click='["bomReviewOpen",${r.id}]'>${esc(t('admin.bom.review_btn'))}</button>
            </td>
        </tr>`; }).join('') || `<tr><td colspan="10" class="muted">${esc(t('admin.bom.none'))}</td></tr>`;
}

async function bomReviewOpen(id) {
    const row = bomReviewRows.find(r => r.id === id);
    if (!row) return;
    bomReviewCurrent = row;
    bomPrevData = null;
    document.getElementById('bom-prev-accept').innerHTML = '';
    const summary = document.getElementById('bom-review-summary');
    summary.innerHTML = bomReviewHeaderHtml(row) + `<p class="muted">${esc(t('common.loading'))}</p>`;
    document.getElementById('bom-review-note').value = row.review_note || '';
    openModal('bom-review-modal');
    // Flagged checks may carry parts the HCL team has verified but not yet
    // published; offer to accept those into the catalog as pre-publication.
    if (Array.isArray(row.flag_reasons) && row.flag_reasons.length) bomPrevLoad(id, row);
    // The full result (per-config findings) lives on the user-side check route;
    // if it is not reachable the row summary alone is still enough to decide.
    let full = null;
    try {
        const res = await fetchJson(`/api/bom-checks/${id}`);
        if (res.ok && res.data) full = res.data;
    } catch (e) { full = null; }
    if (bomReviewCurrent !== row) return;
    bomReviewFull = full;
    summary.innerHTML = bomReviewHeaderHtml(row) + bomReviewResultHtml(full && full.result ? full.result : null, row);
    document.getElementById('bom-review-note').focus();
}

function bomReviewHeaderHtml(r) {
    const project = r.project || {};
    const reasons = Array.isArray(r.flag_reasons) ? r.flag_reasons : [];
    const rows = [
        [t('admin.bom.col_project'), esc(project.name || '') + (project.code ? ` <code>${esc(project.code)}</code>` : '')],
        [t('admin.sizings.col_owner'), esc(r.owner_email || '')],
        [t('admin.bom.col_file'), esc(r.filename || r.name || '')],
        [t('admin.bom.col_when'), esc(fmtDateTime(r.created_at))],
        [t('admin.bom.col_technical'), bomReviewVerdictChip(r.technical_verdict)],
        [t('admin.bom.col_fit'), bomReviewFitChip(r.fit_verdict)],
        [t('admin.bom.col_review'), bomReviewStatusChip(r.review_status)],
    ];
    let html = '<dl class="hcl-run-grid bom-review-facts">'
        + rows.map(x => `<dt>${esc(x[0])}</dt><dd>${x[1]}</dd>`).join('')
        + '</dl>';
    if (reasons.length) {
        html += `<h4 class="bom-review-h">${esc(t('admin.bom.col_reason'))}</h4><ul class="bom-review-reasons">`
            + reasons.map(c => `<li><code>${esc(c)}</code> ${esc(bomReviewReasonText(c))}</li>`).join('')
            + '</ul>';
    }
    return html;
}

function bomReviewResultHtml(result, row) {
    if (!result || !result.technical) {
        return `<p class="muted">${esc(t('admin.bom.no_details'))}</p>`;
    }
    const configs = Array.isArray(result.technical.config_results) ? result.technical.config_results : [];
    let html = `<h4 class="bom-review-h">${esc(t('admin.bom.findings_title'))}</h4>`;
    if (!configs.length) html += `<p class="muted">${esc(t('admin.bom.no_findings'))}</p>`;
    configs.forEach((cfg, ci) => {
        const findings = Array.isArray(cfg.findings) ? cfg.findings : [];
        const platform = cfg.platform || null;
        html += `<div class="bom-review-config"><div class="bom-review-config-head"><strong>${esc(cfg.config_name || '')}</strong> ${bomReviewVerdictChip(cfg.verdict)}`
            + (platform ? ` <span class="muted">${esc([platform.brand, (platform.sc_models || []).join('/'), platform.server].filter(Boolean).join(' · '))}</span>` : '')
            + '</div>';
        if (!findings.length) {
            html += `<p class="muted">${esc(t('admin.bom.no_findings'))}</p></div>`;
            return;
        }
        html += '<table class="model-table bom-review-findings"><thead><tr>'
            + `<th>${esc(t('admin.bom.col_severity'))}</th><th>${esc(t('admin.bom.col_component'))}</th>`
            + `<th>${esc(t('admin.bom.col_issue'))}</th><th>${esc(t('admin.bom.col_remediation'))}</th>`
            + `<th class="col-actions"></th>`
            + '</tr></thead><tbody>'
            + findings.map((f, fi) => `<tr>
                <td><span class="badge ${HCL_SEVERITY_BADGE[f.severity] || 'hcl-badge-plain'}">${esc(tOr('admin.bom.severity_' + f.severity, f.severity || ''))}</span></td>
                <td>${esc(f.component || '')}</td>
                <td>${esc(f.issue || '')}${f.code ? ` <code class="muted">${esc(f.code)}</code>` : ''}</td>
                <td>${esc(f.remediation || '')}</td>
                <td class="col-actions">${bomNoteLineFor(f.component) ? `<button class="btn btn-sm btn-secondary bom-note-add" data-click='["bomNoteFromFinding",${ci},${fi}]'>${esc(t('admin.bomnote.add_btn'))}</button>` : ''}</td>
            </tr>`).join('')
            + '</tbody></table></div>';
    });
    const fit = result.fit || null;
    if (fit && Array.isArray(fit.dimensions) && fit.dimensions.length) {
        html += `<h4 class="bom-review-h">${esc(t('admin.bom.fit_title'))} ${bomReviewFitChip(fit.verdict)}`
            + (fit.sizing && fit.sizing.name ? ` <span class="muted">${esc(fit.sizing.name)}</span>` : '') + '</h4>';
        html += '<table class="model-table bom-review-findings"><thead><tr>'
            + `<th>${esc(t('admin.bom.col_dimension'))}</th><th>${esc(t('admin.bom.col_relation'))}</th>`
            + `<th>${esc(t('admin.bom.col_bom'))}</th><th>${esc(t('admin.bom.col_required'))}</th><th>${esc(t('admin.bom.col_sized'))}</th>`
            + '</tr></thead><tbody>'
            + fit.dimensions.map(d => {
                const unit = d.unit ? ' ' + d.unit : '';
                const num = v => v == null ? '—' : String(v) + unit;
                return `<tr>
                <td title="${esc(d.note || '')}">${esc(tOr('admin.bom.dim_' + d.key, d.key || ''))}</td>
                <td>${esc(tOr('admin.bom.rel_' + d.relation, d.relation || ''))}</td>
                <td>${esc(num(d.bom))}</td><td>${esc(num(d.required))}</td><td>${esc(num(d.sized))}</td>
            </tr>`; }).join('')
            + '</tbody></table>';
    }
    return html;
}

function bomReviewClose() {
    closeModal('bom-review-modal');
    document.getElementById('bom-prev-accept').innerHTML = '';
    bomReviewCurrent = null;
    bomReviewFull = null;
    bomPrevData = null;
}

// ── accept pre-publication parts out of a flagged check ───────────────────
// The HCL team sometimes confirms a part as validated before it appears on
// hcl.scalecomputing.com. These flow: accept here -> catalog rows with
// origin "preview" -> the team pulls them from /api/hcl/preview-feed.

async function bomPrevLoad(id, row) {
    const box = document.getElementById('bom-prev-accept');
    const { ok, data } = await fetchJson(`/admin/api/bom-reviews/${id}/acceptable`);
    if (bomReviewCurrent !== row) return;      // modal moved on meanwhile
    if (!ok || !data || !Array.isArray(data.candidates)) {
        box.innerHTML = `<p class="muted">${esc(t('admin.bomprev.load_error'))}</p>`;
        return;
    }
    bomPrevData = data;
    box.innerHTML = data.candidates.length ? bomPrevSectionHtml(data) : '';
}

function bomPrevSectionHtml(data) {
    const platforms = Array.isArray(data.platforms) ? data.platforms : [];
    let html = `<h4 class="bom-review-h">${esc(t('admin.bomprev.title'))}</h4>`
        + `<p class="muted bom-prev-explain">${esc(t('admin.bomprev.explain'))}</p>`
        + '<div class="bom-prev-list">';
    html += data.candidates.map((c, i) => {
        const inCat = !!c.already_in_catalog;
        const noPart = !c.part_number || String(c.part_number).indexOf('no-part:') === 0;
        const part = noPart
            ? `<span class="muted">${esc(t('admin.bomprev.no_part'))}</span>`
            : `<code>${esc(c.part_number)}</code>`;
        const origin = t('admin.bomprev.found_in', { code: c.from_code || '', config: c.config_name || '' });
        return `<label class="bom-prev-item${inCat ? ' bom-prev-item-dim' : ''}">
            <input type="checkbox" class="bom-prev-check" data-idx="${i}"${inCat ? ' disabled' : ' checked'}>
            <span class="badge hcl-badge-plain">${esc(hclComponentKindLabel(c.kind))}</span>
            <span class="bom-prev-part">${part}</span>
            <span class="bom-prev-desc">${esc(c.description || '')}
                <span class="muted bom-prev-origin">${esc(origin)}</span></span>
            ${inCat ? `<span class="muted bom-prev-incat">${esc(t('admin.bomprev.already'))}</span>` : ''}
        </label>`;
    }).join('');
    html += '</div>';
    if (platforms.length) {
        const names = platforms.map(p => [p.brand, p.sc_model, p.server].filter(Boolean).join(' '));
        html += `<p class="muted bom-prev-platform">${esc(t('admin.bomprev.link_to', { platforms: names.join(', ') }))}</p>`;
    } else if (data.platform_suggestion) {
        // No platform identified but the backend can seed one: offer to create
        // it as pre-publication so the accepted parts are linked (scoped to the
        // platform) instead of unlinked.
        html += bomPrevPlatFormHtml(data.platform_suggestion);
    } else {
        html += `<p class="muted bom-prev-platform">${esc(t('admin.bomprev.no_platform'))}</p>`;
    }
    html += `<div class="bom-prev-actions"><button class="btn btn-primary btn-sm" id="bom-prev-accept-btn"
        data-click='["bomPrevAcceptSelected"]'>${esc(t('admin.bomprev.accept_btn'))}</button></div>`;
    return html;
}

// Sub-form offering to create the (missing) platform as pre-publication so
// the accepted parts land linked. Default ON: unlinked acceptance is exactly
// what this steers away from.
function bomPrevPlatFormHtml(sug) {
    const brands = [['dell', 'Dell'], ['lenovo', 'Lenovo'], ['hpe', 'HPE'], ['supermicro', 'Supermicro']];
    const cur = String(sug.brand || '').toLowerCase();
    return `<div class="bom-prev-plat" id="bom-prev-plat">
        <label class="bom-prev-plat-enable">
            <input type="checkbox" id="bom-prev-plat-enable" checked data-change='["bomPrevPlatToggle","$checked"]'>
            <strong>${esc(t('admin.bomprev.plat_title'))}</strong>
        </label>
        <div class="bom-prev-plat-grid" id="bom-prev-plat-grid">
            <label for="bom-prev-plat-brand">${esc(t('admin.bomprev.plat_brand'))}</label>
            <select id="bom-prev-plat-brand">${brands.map(b =>
                `<option value="${b[0]}"${b[0] === cur ? ' selected' : ''}>${b[1]}</option>`).join('')}</select>
            <label for="bom-prev-plat-sc">${esc(t('admin.bomprev.plat_sc_model'))}</label>
            <input type="text" id="bom-prev-plat-sc" maxlength="40"
                value="${esc(sug.sc_model || '')}"
                placeholder="${esc(t('admin.bomprev.plat_sc_ph'))}" data-input='["bomPrevPlatNameTyped"]'>
            <label for="bom-prev-plat-server">${esc(t('admin.bomprev.plat_server'))}</label>
            <input type="text" id="bom-prev-plat-server" maxlength="120" value="${esc(sug.server || '')}">
        </div>
        <p class="muted bom-prev-plat-hint">${esc(t('admin.bomprev.plat_hint'))}</p>
        <div class="bom-prev-plat-near" id="bom-prev-plat-near" hidden></div>
        <p class="admin-status status-err bom-prev-plat-err" id="bom-prev-plat-err" style="display:none"></p>
    </div>`;
}

// ── near-match warning ───────────────────────────────────────────────────────
// The platform name is free text on purpose (validated builds are named after
// the manufacturer's model), so two admins can spell the same box differently
// days apart. While the name is typed we look for platforms that already look
// like it and offer to link to one instead of creating a twin. A warning, never
// a block: only the admin knows whether it really is the same box.
let bomPrevNearTimer = null;
let bomPrevNearMatches = [];
let bomPrevLinkExisting = null;      // {id, key} once "link to this" is chosen

function bomPrevPlatNameTyped() {
    bomPrevPlatError('');
    if (bomPrevLinkExisting) return;          // already linking; nothing to warn about
    clearTimeout(bomPrevNearTimer);
    bomPrevNearTimer = setTimeout(bomPrevPlatLookup, 350);
}

async function bomPrevPlatLookup() {
    const nameEl = document.getElementById('bom-prev-plat-sc');
    const serverEl = document.getElementById('bom-prev-plat-server');
    const brandEl = document.getElementById('bom-prev-plat-brand');
    if (!nameEl) return;
    const name = (nameEl.value || '').trim();
    if (name.length < 3) { bomPrevRenderNear([]); return; }
    const q = new URLSearchParams({ brand: brandEl ? brandEl.value : '', name: name });
    if (serverEl && serverEl.value.trim()) q.set('server', serverEl.value.trim());
    const { ok, data } = await fetchJson('/admin/api/hcl/platform-matches?' + q.toString());
    if (!ok || !data) return;
    bomPrevRenderNear(data.matches || []);
}

function bomPrevRenderNear(matches) {
    bomPrevNearMatches = matches || [];
    const host = document.getElementById('bom-prev-plat-near');
    if (!host) return;
    if (!bomPrevNearMatches.length) { host.hidden = true; host.innerHTML = ''; return; }
    const rows = bomPrevNearMatches.map((m, i) => {
        const what = [m.brand, m.sc_model].filter(Boolean).join(' ');
        const server = m.server ? ` <span class="muted">(${esc(m.server)})</span>` : '';
        const flag = m.origin === 'preview'
            ? ` <span class="badge badge-validated hcl-badge-tight">${esc(t('admin.bomprev.badge'))}</span>` : '';
        return `<li><strong>${esc(what)}</strong>${server}${flag}
            <button class="btn btn-sm btn-secondary" data-click='["bomPrevLinkInstead",${i}]'
                >${esc(t('admin.bomprev.near_use'))}</button></li>`;
    }).join('');
    host.innerHTML = `<p class="bom-prev-near-title">${esc(t('admin.bomprev.near_title'))}</p>
        <ul class="bom-prev-near-list">${rows}</ul>`;
    host.hidden = false;
}

// Chose an existing platform: collapse the create form and link to it instead.
function bomPrevLinkInstead(idx) {
    const m = bomPrevNearMatches[idx];
    if (!m) return;
    bomPrevLinkExisting = { id: m.id, key: m.key };
    const grid = document.getElementById('bom-prev-plat-grid');
    if (grid) grid.hidden = true;
    const host = document.getElementById('bom-prev-plat-near');
    if (host) {
        host.innerHTML = `<p class="bom-prev-near-title">${esc(t('admin.bomprev.near_linking', { name: m.key }))}
            <button class="btn btn-sm btn-secondary" data-click='["bomPrevLinkUndo"]'
                >${esc(t('admin.bomprev.near_undo'))}</button></p>`;
        host.hidden = false;
    }
    bomPrevPlatError('');
}

function bomPrevLinkUndo() {
    bomPrevLinkExisting = null;
    const grid = document.getElementById('bom-prev-plat-grid');
    if (grid) grid.hidden = false;
    bomPrevRenderNear([]);
    bomPrevPlatLookup();
}

function bomPrevPlatToggle(enabled) {
    const box = document.getElementById('bom-prev-plat');
    if (box) box.classList.toggle('bom-prev-plat-off', !enabled);
    const grid = document.getElementById('bom-prev-plat-grid');
    if (grid) grid.querySelectorAll('input, select').forEach(el => { el.disabled = !enabled; });
    if (!enabled) bomPrevPlatError('');
}

// Inline error under the platform sub-form (also the data-input clear hook).
function bomPrevPlatError(msg) {
    const el = document.getElementById('bom-prev-plat-err');
    if (!el) return;
    el.textContent = msg || '';
    el.style.display = msg ? 'block' : 'none';
}

// Returns undefined when the sub-form is absent/disabled, the platform object
// to POST when it is filled in, and null after flagging an inline error.
function bomPrevPlatPayload() {
    const enable = document.getElementById('bom-prev-plat-enable');
    if (!enable || !enable.checked) return undefined;
    if (bomPrevLinkExisting) return { existing_id: bomPrevLinkExisting.id };
    const sc = (document.getElementById('bom-prev-plat-sc').value || '').trim();
    if (!sc) { bomPrevPlatError(t('admin.bomprev.plat_sc_required')); return null; }
    bomPrevPlatError('');
    return {
        brand: document.getElementById('bom-prev-plat-brand').value,
        sc_model: sc,
        server: (document.getElementById('bom-prev-plat-server').value || '').trim() || null,
    };
}

async function bomPrevAcceptSelected() {
    if (!bomReviewCurrent || !bomPrevData) return;
    const row = bomReviewCurrent;
    const checked = document.querySelectorAll('#bom-prev-accept .bom-prev-check:checked:not(:disabled)');
    const keys = Array.from(checked).map(cb => {
        const c = bomPrevData.candidates[parseInt(cb.getAttribute('data-idx'), 10)];
        return c ? c.key : null;
    }).filter(Boolean);
    if (!keys.length) { toastError(t('admin.bomprev.none_selected')); return; }
    const platform = bomPrevPlatPayload();
    if (platform === null) return;             // sub-form enabled but invalid
    const body = { keys: keys };
    if (platform) body.platform = platform;
    const { ok, data } = await withBusy('bom-prev-accept-btn',
        () => sendJson(`/admin/api/bom-reviews/${row.id}/accept-parts`, 'POST', body));
    if (!ok) { toastError(errorText(data)); return; }
    const n = x => Array.isArray(x) ? x.length : 0;
    let done = t('admin.bomprev.accept_done', { created: n(data.created), linked: n(data.linked), skipped: n(data.skipped) });
    if (data.platform_created) done += ' ' + t('admin.bomprev.plat_created', { name: data.platform_created });
    if (data.platform_linked) done += ' ' + t('admin.bomprev.plat_linked', { name: data.platform_linked });
    if ((data.platform_warnings || []).length) {
        toast(t('admin.bomprev.near_after', {
            names: data.platform_warnings.map(m => m.key).join(', ') }), 'warn', 10000);
    }
    toast(done, 'success');
    toast(t('admin.bomprev.recheck_hint'), 'info', 8000);
    // The catalog, its stats and the feed count all just moved.
    loadBomReviews();
    hclLoadStats();
    loadHclCatalog();
    hclLoadPreviewCount();
    if (bomReviewCurrent === row) bomPrevLoad(row.id, row);   // re-render as "already in catalog"
}

async function bomReviewSave(status) {
    if (!bomReviewCurrent) return;
    const id = bomReviewCurrent.id;
    const note = document.getElementById('bom-review-note').value.trim();
    const { ok, data } = await sendJson(`/admin/api/bom-reviews/${id}`, 'POST', { status: status, note: note });
    if (!ok) { toastError(errorText(data)); return; }
    toast((data && data.message) || t('admin.bom.saved'), 'success');
    bomReviewClose();
    loadBomReviews();
}

// ==================== SUPER-ADMIN: REJECTED BOM UPLOADS ====================
// Consent-shared files the checker refused (bom_models.BomRejectedFile):
// download to teach the parsers the format, delete once handled.

function bomRejSize(bytes) {
    if (bytes == null) return '';
    if (bytes >= 1048576) return (bytes / 1048576).toFixed(1) + ' MB';
    return Math.max(1, Math.round(bytes / 1024)) + ' KB';
}

// ── part notes ───────────────────────────────────────────────────────────────
// A reviewer's standing verdict on one specific part ("disable this LOM in the
// BIOS"). The server applies it to new checks and re-checks in place of the
// generic finding for that part (bom/part_notes.py). Existing checks keep their
// stored result until re-checked.
let bomNoteRows = [];
let bomNoteEditing = null;     // {id?, source_check_id?}

// The BOM line a finding is about, from the check's normalised BOM — only
// findings that name a real line can carry a note ("Storage", "Platform" and
// other summary findings cannot).
function bomNoteLineFor(component) {
    const configs = bomReviewFull && bomReviewFull.normalized && bomReviewFull.normalized.configs;
    if (!component || !Array.isArray(configs)) return null;
    for (const cfg of configs) {
        const hit = (cfg.components || []).find(c => c.description === component);
        if (hit) return hit;
    }
    return null;
}

function bomNoteMatchLabel(n) {
    if (n.match_mode === 'contains') return t('admin.bomnote.contains', {text: n.match_text || ''});
    return [n.part_number, n.description].filter(Boolean).join(' · ');
}

async function loadBomNotes() {
    const body = document.getElementById('bom-notes-tbody');
    if (!body) return;
    const { ok, data } = await fetchJson('/admin/api/bom-part-notes');
    if (!ok || !Array.isArray(data)) {
        body.innerHTML = `<tr><td colspan="6">${esc(t('admin.bom.load_error'))}</td></tr>`;
        return;
    }
    bomNoteRows = data;
    body.innerHTML = data.map(n => `
        <tr class="${n.active ? '' : 'muted'}">
            <td>${esc(bomNoteMatchLabel(n))}</td>
            <td>${esc(n.category ? tOr('admin.bomnote.cat_' + n.category, n.category) : t('admin.bomnote.cat_any'))}</td>
            <td><span class="badge ${HCL_SEVERITY_BADGE[n.severity] || 'hcl-badge-plain'}">${esc(tOr('admin.bom.severity_' + n.severity, n.severity))}</span></td>
            <td title="${esc(n.remediation || '')}">${esc(n.issue)}</td>
            <td><input type="checkbox" ${n.active ? 'checked' : ''} data-change='["bomNoteToggle",${n.id},"$checked"]'
                       aria-label="${esc(t('admin.bomnote.active_label'))}"></td>
            <td class="col-actions">
                <button class="btn btn-sm btn-secondary" data-click='["bomNoteEdit",${n.id}]'>${esc(t('common.edit'))}</button>
                <button class="btn btn-sm btn-danger" data-click='["bomNoteDelete",${n.id}]'>${esc(t('common.delete'))}</button>
            </td>
        </tr>`).join('') || `<tr><td colspan="6" class="muted">${esc(t('admin.bomnote.none'))}</td></tr>`;
}

function bomNoteFill(n) {
    const set = (id, v) => { document.getElementById(id).value = v == null ? '' : v; };
    set('bom-note-mode', n.match_mode || 'exact');
    set('bom-note-part', n.part_number);
    set('bom-note-desc', n.description);
    set('bom-note-text', n.match_text);
    set('bom-note-category', n.category || '');
    set('bom-note-severity', n.severity || 'warning');
    set('bom-note-issue', n.issue);
    set('bom-note-remediation', n.remediation);
    document.getElementById('bom-note-active').checked = n.active !== false;
    bomNoteModeChanged();
    adminStatus('bom-note-status', '', false);
    openModal('bom-note-modal', { focus: 'bom-note-issue' });
}

function bomNoteOpen() {
    bomNoteEditing = {};
    bomNoteFill({});
}

function bomNoteEdit(id) {
    const n = bomNoteRows.find(r => r.id === id);
    if (!n) return;
    bomNoteEditing = {id: n.id};
    bomNoteFill(n);
}

// From a finding in the review modal: the part is already known, so the
// admin only has to write the note.
function bomNoteFromFinding(ci, fi) {
    const configs = bomReviewFull && bomReviewFull.result && bomReviewFull.result.technical
        && bomReviewFull.result.technical.config_results;
    const f = configs && configs[ci] && (configs[ci].findings || [])[fi];
    const line = f && bomNoteLineFor(f.component);
    if (!line) return;
    bomNoteEditing = {source_check_id: bomReviewCurrent ? bomReviewCurrent.id : null};
    bomNoteFill({
        match_mode: 'exact',
        part_number: line.partNumber || '',
        description: line.description || '',
        category: line.category || '',
        severity: 'warning',
    });
}

function bomNoteModeChanged() {
    const contains = document.getElementById('bom-note-mode').value === 'contains';
    document.querySelectorAll('#bom-note-modal .bom-note-exact').forEach(el => { el.style.display = contains ? 'none' : ''; });
    document.querySelectorAll('#bom-note-modal .bom-note-contains').forEach(el => { el.style.display = contains ? '' : 'none'; });
}

function bomNoteClose() {
    closeModal('bom-note-modal');
    bomNoteEditing = null;
}

async function bomNoteSave() {
    if (!bomNoteEditing) return;
    const val = id => document.getElementById(id).value;
    const payload = {
        match_mode: val('bom-note-mode'),
        part_number: val('bom-note-part'),
        description: val('bom-note-desc'),
        match_text: val('bom-note-text'),
        category: val('bom-note-category'),
        severity: val('bom-note-severity'),
        issue: val('bom-note-issue'),
        remediation: val('bom-note-remediation'),
        active: document.getElementById('bom-note-active').checked,
    };
    let res;
    if (bomNoteEditing.id) {
        res = await sendJson(`/admin/api/bom-part-notes/${bomNoteEditing.id}`, 'PUT', payload);
    } else {
        payload.source_check_id = bomNoteEditing.source_check_id || null;
        res = await sendJson('/admin/api/bom-part-notes', 'POST', payload);
    }
    if (!res.ok) {
        adminStatus('bom-note-status', (res.data && res.data.error) || t('admin.msg.failed'), true);
        return;
    }
    bomNoteClose();
    adminStatus('bom-review-status', t('admin.bomnote.saved'), false);
    loadBomNotes();
}

async function bomNoteToggle(id, checked) {
    const n = bomNoteRows.find(r => r.id === id);
    if (!n) return;
    const res = await sendJson(`/admin/api/bom-part-notes/${id}`, 'PUT', Object.assign({}, n, {active: !!checked}));
    if (!res.ok) adminStatus('bom-review-status', (res.data && res.data.error) || t('admin.msg.failed'), true);
    loadBomNotes();
}

async function bomNoteDelete(id) {
    if (!confirm(t('admin.bomnote.delete_confirm'))) return;
    const { ok, data } = await fetchJson(`/admin/api/bom-part-notes/${id}`, { method: 'DELETE' });
    if (!ok) { adminStatus('bom-review-status', (data && data.error) || t('admin.msg.failed'), true); return; }
    loadBomNotes();
}

// ── Claude agent: settings + usage (bom_routes agent-settings / agent-usage) ─

async function loadBomAgent() {
    const state = document.getElementById('bom-agent-state');
    const [settings, usage] = await Promise.all([
        fetchJson('/admin/api/bom-reviews/agent-settings'),
        fetchJson('/admin/api/bom-reviews/agent-usage'),
    ]);
    if (settings.ok && settings.data) {
        const d = settings.data;
        document.getElementById('bom-agent-tenant-cap').value = d.tenant_daily_cap;
        document.getElementById('bom-agent-global-cap').value = d.global_daily_cap;
        document.getElementById('bom-agent-always-review').checked = !!d.always_review;
        document.getElementById('bom-agent-pdf-threshold').value = d.pdf_threshold;
        if (state) {
            state.textContent = d.available
                ? t('admin.bomagent.on', { model: d.model || '' })
                : t('admin.bomagent.off');
        }
    }
    const body = document.getElementById('bom-agent-usage-tbody');
    if (!body) return;
    if (!usage.ok || !Array.isArray(usage.data)) {
        body.innerHTML = `<tr><td colspan="7">${esc(t('admin.bom.load_error'))}</td></tr>`;
        return;
    }
    const n = (v) => Number(v || 0).toLocaleString();
    body.innerHTML = usage.data.map(r => `
        <tr>
            <td>${esc(r.day)}</td>
            <td>${esc(r.tenant || '')}</td>
            <td>${n(r.jobs)}</td>
            <td>${n(r.done)}</td>
            <td>${n(r.failed)}</td>
            <td>${n(r.input_tokens)}</td>
            <td>${n(r.output_tokens)}</td>
        </tr>`).join('') || `<tr><td colspan="7">${esc(t('admin.bomagent.no_usage'))}</td></tr>`;
}

async function bomAgentSaveSettings() {
    const cap = (id) => {
        const v = document.getElementById(id).value.trim();
        const num = v === '' ? 0 : Number(v);
        return Number.isInteger(num) && num >= 0 ? num : NaN;
    };
    const tenant = cap('bom-agent-tenant-cap');
    const global = cap('bom-agent-global-cap');
    const threshold = cap('bom-agent-pdf-threshold');
    if (isNaN(tenant) || isNaN(global) || isNaN(threshold) || threshold > 100) {
        adminStatus('bom-review-status', t('admin.bomagent.cap_invalid'), true);
        return;
    }
    const { ok, data } = await withBusy('bom-agent-save', () => sendJson('/admin/api/bom-reviews/agent-settings', 'PUT', {
        tenant_daily_cap: tenant, global_daily_cap: global, pdf_threshold: threshold,
        always_review: document.getElementById('bom-agent-always-review').checked,
    }));
    if (!ok) { adminStatus('bom-review-status', (data && data.error) || t('admin.msg.failed'), true); return; }
    toast(t('admin.msg.saved'), 'success');
    loadBomAgent();
}

async function loadBomRejects() {
    const body = document.getElementById('bom-rejects-tbody');
    if (!body) return;
    const { ok, data } = await fetchJson('/admin/api/bom-rejects');
    if (!ok || !Array.isArray(data)) {
        body.innerHTML = `<tr><td colspan="7">${esc(t('admin.bom.load_error'))}</td></tr>`;
        return;
    }
    body.innerHTML = data.map(r => `
        <tr>
            <td>${fmtDateTime(r.created_at)}</td>
            <td>${esc(r.project_name || '')}</td>
            <td>${esc(r.owner_email || '')}<br><span class="muted">${esc(r.tenant_domain || '')}</span></td>
            <td>${esc(r.filename)}</td>
            <td>${bomRejSize(r.size_bytes)}</td>
            <td title="${esc(r.error || '')}">${esc((r.error || '').slice(0, 70))}</td>
            <td class="col-actions">
                <a class="btn btn-sm btn-secondary" href="/admin/api/bom-rejects/${r.id}/file">${esc(t('admin.bomrej.download'))}</a>
                <button class="btn btn-sm btn-danger" data-click='["deleteBomReject",${r.id}]'>${esc(t('common.delete'))}</button>
            </td>
        </tr>`).join('') || `<tr><td colspan="7">${esc(t('admin.bomrej.none'))}</td></tr>`;
}

async function deleteBomReject(id) {
    if (!confirm(t('admin.bomrej.delete_confirm'))) return;
    const { ok, data } = await fetchJson(`/admin/api/bom-rejects/${id}`, { method: 'DELETE' });
    if (!ok) { adminStatus('bom-review-status', (data && data.error) || t('admin.msg.failed'), true); return; }
    loadBomRejects();
}
