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
let catalogModalCallback = null;

// Translate-or-fallback: use a translation when the key exists in the loaded
// dictionaries (active locale or the English base), otherwise fall back to the
// server-provided English string. This lets server metadata (tunable labels/help)
// be overridden by translations without showing raw keys when none is present.
function tOr(key, fallback) {
    const langs = window.I18N_LANGS || {};
    const active = langs[window.I18N_ACTIVE] || {};
    const base = langs.en || {};
    if (key in active || key in base) return window.t(key);
    // Defensive: window.t() falls back to the raw key, so if it echoes the key
    // back we know there is no translation and use the server string instead.
    const s = window.t(key);
    return s === key ? fallback : s;
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
    else if (tab === 'users') loadAdminUsers();
    else if (tab === 'stale') loadStaleUsers();
    else if (tab === 'tenants') loadAdminTenants();
    else if (tab === 'sizings') loadAdminSizings();
    else if (tab === 'email') loadEmailSettings();
    else if (tab === 'audit') loadAuditLog();
}

// ── Load Data ──────────────────────────────────────────────────────────────

async function loadModels() {
    const resp = await fetch('/admin/api/models');
    allModels = await resp.json();

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
    const [cpuResp, nicResp, driveResp, iopsResp, sizingResp] = await Promise.all([
        fetch('/admin/api/cpus'),
        fetch('/admin/api/nics'),
        fetch('/admin/api/drives'),
        fetch('/admin/api/drive-iops'),
        fetch('/admin/api/sizing-config'),
    ]);
    cpuCatalog = await cpuResp.json();
    nicCatalog = await nicResp.json();
    driveCatalog = await driveResp.json();
    driveIops = await iopsResp.json();
    sizingConfig = await sizingResp.json();

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
    try {
        const [r1, r2] = await Promise.all([
            fetch('/admin/api/drive-iops', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(iopsPayload)}),
            fetch('/admin/api/sizing-config', {method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(sizingPayload)}),
        ]);
        const d1 = await r1.json(), d2 = await r2.json();
        if (!r1.ok) throw new Error(d1.error || 'Save failed');
        if (!r2.ok) throw new Error(d2.error || 'Save failed');
        driveIops = d1.drive_iops || driveIops;
        sizingConfig = d2.sizing_config || sizingConfig;
        status.textContent = t('admin.msg.saved');
        status.className = 'iops-status ok';
    } catch (e) {
        status.textContent = e.message;
        status.className = 'iops-status err';
    }
    setTimeout(() => { status.textContent = ''; status.className = 'iops-status'; }, 3000);
}

// ── Tuning (sizing & scoring tunables) ───────────────────────────────────────

let tunableDefs = [];

async function loadTunables() {
    // The Tuning tab hosts both the IOPS card and the scoring/sizing tunables.
    const [tunResp, iopsResp, sizingResp] = await Promise.all([
        fetch('/admin/api/tunables'),
        fetch('/admin/api/drive-iops'),
        fetch('/admin/api/sizing-config'),
    ]);
    const data = await tunResp.json();
    driveIops = await iopsResp.json();
    sizingConfig = await sizingResp.json();
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
                    <label title="${esc(help)}">${esc(label)}<button type="button" class="tunable-info-btn" title="${esc(t('admin.tunable.info_btn'))}" data-click='["showTunableInfo","${d.key}"]'>i</button></label>
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
    try {
        const r = await fetch('/admin/api/tunables', {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'Save failed');
        renderTunables(d.values || {});
        status.textContent = t('admin.msg.saved');
        status.className = 'iops-status ok';
    } catch (e) {
        status.textContent = e.message;
        status.className = 'iops-status err';
    }
    setTimeout(() => { status.textContent = ''; status.className = 'iops-status'; }, 3000);
}

// Shared What/How/Beware info modal, driven by a {label, what, how, beware} object.
function showInfo(d) {
    if (!d) return;
    document.getElementById('tun-info-title').textContent = d.label || t('admin.info.default_title');
    document.getElementById('tun-info-what').textContent = d.what || d.help || '';
    document.getElementById('tun-info-how').textContent = d.how || '';
    document.getElementById('tun-info-beware').textContent = d.beware || '';
    document.getElementById('tunable-info-modal').style.display = 'flex';
}

function showTunableInfo(key) {
    showInfo((tunableDefs || []).find(t => t.key === key));
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
    document.getElementById('tunable-info-modal').style.display = 'none';
}

async function resetTunables() {
    if (!confirm(t('admin.tunable.reset_confirm'))) return;
    const status = document.getElementById('tunables-status');
    try {
        const r = await fetch('/admin/api/tunables/reset', {method: 'POST'});
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || 'Reset failed');
        renderTunables(d.values || {});
        status.textContent = t('admin.msg.reset_ok');
        status.className = 'iops-status ok';
    } catch (e) {
        status.textContent = e.message;
        status.className = 'iops-status err';
    }
    setTimeout(() => { status.textContent = ''; status.className = 'iops-status'; }, 3000);
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
        tbody.innerHTML = `<tr><td colspan="11" style="text-align:center;padding:2rem;color:var(--text-muted)">${esc(t('admin.models.none'))}</td></tr>`;
        return;
    }

    filtered.forEach(m => {
        const tr = document.createElement('tr');
        const badgeClass = m.status === 'Active' ? 'badge-active' : m.status === 'EOL' ? 'badge-eol' : 'badge-eos';
        const cpuSummary = m.cpu_options.map(c => c.desc).join('<br>');
        const ramSummary = m.ram_options_gb.join(', ') + ' GB';
        const storType = m.storage?.type || '-';

        tr.innerHTML = `
            <td><strong>${esc(m.name)}</strong>${m.validated_only ? ` <span class="badge badge-validated" title="${esc(t('admin.models.validated_only_title'))}">${esc(t('admin.models.validated_only'))}</span>` : ''}</td>
            <td><span class="badge ${badgeClass}">${m.status}</span></td>
            <td>${esc(m.category)}</td>
            <td>${esc(m.form_factor || '-')}</td>
            <td>${esc(m.socket || '-')}</td>
            <td class="cell-list">${cpuSummary || '-'}</td>
            <td class="cell-list">${ramSummary}</td>
            <td>${esc(storType)}</td>
            <td>${m.min_nodes}</td>
            <td>${m.cost_tier ?? '-'}</td>
            <td class="col-actions">
                <button class="btn-icon" title="${esc(t('common.edit'))}" data-click='["openEditModel",${m.id}]'>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                </button>
                <button class="btn-icon danger" title="${esc(t('common.delete'))}" data-click='["deleteModel",${m.id},"${esc(m.name)}"]'>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function esc(s) {
    if (s == null) return '';
    const d = document.createElement('div');
    d.textContent = String(s);
    return d.innerHTML;
}

// ── Catalog Tables ─────────────────────────────────────────────────────────

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
            <td class="col-actions">
                <button class="btn-icon" title="${esc(t('common.edit'))}" data-click='["openEditCatalogItem","cpu",${c.id}]'>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                </button>
                <button class="btn-icon danger" title="${esc(t('common.delete'))}" data-click='["deleteCatalogItem","cpu",${c.id},"${esc(c.desc)}"]'>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                </button>
            </td>
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
            <td class="col-actions">
                <button class="btn-icon" title="${esc(t('common.edit'))}" data-click='["openEditCatalogItem","nic",${n.id}]'>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                </button>
                <button class="btn-icon danger" title="${esc(t('common.delete'))}" data-click='["deleteCatalogItem","nic",${n.id},"${esc(n.desc)}"]'>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                </button>
            </td>
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
            <td class="col-actions">
                <button class="btn-icon" title="${esc(t('common.edit'))}" data-click='["openEditCatalogItem","drive",${d.id}]'>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                </button>
                <button class="btn-icon danger" title="${esc(t('common.delete'))}" data-click='["deleteCatalogItem","drive",${d.id},"${d.drive_type} ${d.size_tb}TB"]'>
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

// ── Catalog CRUD ───────────────────────────────────────────────────────────

function openAddCatalogItem(type, fromModelEditor = false) {
    catalogModalType = type;
    catalogModalId = null;
    catalogModalCallback = fromModelEditor ? () => refreshPickersAfterCatalogChange(type) : null;

    const titles = { cpu: t('admin.catalog.add_cpu'), nic: t('admin.catalog.add_nic'), drive: t('admin.catalog.add_drive') };
    document.getElementById('catalog-modal-title').textContent = titles[type];
    document.getElementById('catalog-modal-body').innerHTML = catalogFormHtml(type, {});
    document.getElementById('catalog-modal').style.display = 'flex';
}

function openEditCatalogItem(type, id) {
    catalogModalType = type;
    catalogModalId = id;
    catalogModalCallback = null;

    const catalog = { cpu: cpuCatalog, nic: nicCatalog, drive: driveCatalog }[type];
    const item = catalog.find(c => c.id === id);
    if (!item) return;

    const titles = { cpu: t('admin.catalog.edit_cpu'), nic: t('admin.catalog.edit_nic'), drive: t('admin.catalog.edit_drive') };
    document.getElementById('catalog-modal-title').textContent = titles[type];
    document.getElementById('catalog-modal-body').innerHTML = catalogFormHtml(type, item);
    document.getElementById('catalog-modal').style.display = 'flex';
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
    document.getElementById('catalog-modal').style.display = 'none';
    catalogModalCallback = null;
}

// Auto-fill benchmark scores for the CPU being added/edited, from the curated
// catalog or the broad SPECrate2017 lookup (same source as the source-CPU
// comparison). Only fills empty fields, so it never clobbers manual entry.
async function autofillCpuBenchmark() {
    const desc = document.getElementById('cat-desc');
    const status = document.getElementById('cat-bench-status');
    if (!desc || !desc.value.trim()) return;
    try {
        const resp = await fetch('/api/cpu-perf?q=' + encodeURIComponent(desc.value.trim()));
        const d = await resp.json();
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
    } catch (e) { /* best-effort */ }
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
        if (!payload.desc) { alert(t('admin.validate.desc_required')); return; }
    } else if (type === 'nic') {
        payload = {
            desc: document.getElementById('cat-desc').value.trim(),
            ports: parseInt(document.getElementById('cat-ports').value) || 0,
            speed: document.getElementById('cat-speed').value.trim(),
        };
        if (!payload.desc) { alert(t('admin.validate.desc_required')); return; }
    } else if (type === 'drive') {
        payload = {
            drive_type: document.getElementById('cat-drive-type').value,
            size_tb: parseFloat(document.getElementById('cat-size-tb').value) || 0,
        };
        if (!payload.drive_type || payload.size_tb <= 0) { alert(t('admin.validate.type_size_required')); return; }
    }

    const base = `/admin/api/${type === 'cpu' ? 'cpus' : type === 'nic' ? 'nics' : 'drives'}`;
    if (catalogModalId) {
        url = `${base}/${catalogModalId}`;
        method = 'PUT';
    } else {
        url = base;
        method = 'POST';
    }

    try {
        const resp = await fetch(url, {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (data.error) { alert(data.error); return; }

        closeCatalogModal();
        await loadCatalogs();
        if (catalogModalCallback) catalogModalCallback();
    } catch (e) {
        alert(t('admin.msg.save_failed', {error: e.message}));
    }
}

async function deleteCatalogItem(type, id, label) {
    if (!confirm(t('admin.confirm.delete_item', {label: label}))) return;
    const base = `/admin/api/${type === 'cpu' ? 'cpus' : type === 'nic' ? 'nics' : 'drives'}`;
    try {
        const resp = await fetch(`${base}/${id}`, { method: 'DELETE' });
        const data = await resp.json();
        if (data.error) { alert(data.error); return; }
        await loadCatalogs();
    } catch (e) {
        alert(t('admin.msg.delete_failed', {error: e.message}));
    }
}

// ── Delete Model ───────────────────────────────────────────────────────────

async function deleteModel(id, name) {
    if (!confirm(t('admin.confirm.delete_model', {name: name}))) return;
    await fetch(`/admin/api/models/${id}`, { method: 'DELETE' });
    loadModels();
    loadCatalogs();
}

// ── Export / Import ────────────────────────────────────────────────────────

function exportModels() {
    window.location.href = '/admin/api/export-models';
}

// ── Import / Template ──────────────────────────────────────────────────────

function openCatalogImport() {
    document.getElementById('catalog-import-modal').style.display = 'flex';
    document.getElementById('catalog-import-file').value = '';
    document.getElementById('catalog-import-status').style.display = 'none';
}

function closeCatalogImport() {
    document.getElementById('catalog-import-modal').style.display = 'none';
}

async function doCatalogImport() {
    const fileInput = document.getElementById('catalog-import-file');
    if (!fileInput.files.length) { showCatalogImportStatus(t('admin.import.select_file'), true); return; }

    const formData = new FormData();
    formData.append('file', fileInput.files[0]);
    showCatalogImportStatus(t('admin.import.importing'), false);

    try {
        const resp = await fetch('/admin/api/import-catalog', { method: 'POST', body: formData });
        const data = await resp.json();
        if (data.error) { showCatalogImportStatus(data.error, true); return; }
        showCatalogImportStatus(data.message, false);
        loadCatalogs();
        loadModels();
    } catch (e) {
        showCatalogImportStatus(t('admin.import.failed', {error: e.message}), true);
    }
}

function showCatalogImportStatus(msg, isError) {
    const el = document.getElementById('catalog-import-status');
    el.style.display = 'block';
    el.className = isError ? 'error' : 'success';
    el.textContent = msg;
}

function downloadCatalogTemplate() {
    window.location.href = '/admin/api/catalog-template';
}

// ── Model Edit Modal ───────────────────────────────────────────────────────

// CPU/NIC/drive selection now happens in on-demand modals built from the live
// catalog arrays, which loadCatalogs() refreshes after any catalog edit — so
// there are no standing picker controls left to repopulate here.
function refreshPickersAfterCatalogChange(type) {}

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
    document.getElementById('edit-modal').style.display = 'flex';
}

async function openEditModel(id) {
    const resp = await fetch(`/admin/api/models/${id}`);
    const m = await resp.json();

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

    document.getElementById('edit-modal').style.display = 'flex';
}

function closeEdit() {
    document.getElementById('edit-modal').style.display = 'none';
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
};

let itemSelectKind = null;

function openItemSelect(kind) {
    itemSelectKind = kind;
    document.getElementById('item-select-title').textContent = ITEM_SELECT[kind].title();
    renderItemSelectList();
    document.getElementById('item-select-all').checked = false;
    document.getElementById('item-select-modal').style.display = 'flex';
}

function closeItemSelect() {
    document.getElementById('item-select-modal').style.display = 'none';
    itemSelectKind = null;
}

function renderItemSelectList() {
    const cfg = ITEM_SELECT[itemSelectKind];
    const list = document.getElementById('item-select-list');
    const taken = new Set(cfg.selected().map(cfg.key));
    const avail = cfg.catalog().filter(it => !taken.has(cfg.key(it))).sort(cfg.sort);
    if (!avail.length) {
        list.innerHTML = `<p class="drive-select-empty">${esc(t('admin.select.all_added', {noun: cfg.noun()}))}</p>`;
        return;
    }
    list.innerHTML = avail.map(it =>
        `<label class="item-select-row"><input type="checkbox" value="${it.id}"> ${cfg.label(it)}</label>`
    ).join('');
}

function toggleAllItemSelect(el) {
    document.querySelectorAll('#item-select-list input[type=checkbox]')
        .forEach(cb => { cb.checked = el.checked; });
}

function confirmItemSelect() {
    const cfg = ITEM_SELECT[itemSelectKind];
    document.querySelectorAll('#item-select-list input[type=checkbox]:checked').forEach(cb => {
        const it = cfg.catalog().find(x => x.id === parseInt(cb.value));
        if (it && !cfg.selected().some(s => cfg.key(s) === cfg.key(it))) cfg.add(it);
    });
    cfg.renderChips();
    closeItemSelect();
}

// ── Chip Rendering ─────────────────────────────────────────────────────────

function renderCpuChips() {
    const container = document.getElementById('cpu-chips');
    container.innerHTML = '';
    selectedCpus.forEach((c, i) => {
        const chip = document.createElement('span');
        chip.className = 'chip';
        chip.innerHTML = `<select class="qty-select" data-change='["setSelCpuQty",${i},"$value"]'>${qtyOptions(c.qty)}</select> x ${esc(c.desc)} <span class="remove" data-click='["removeCpu",${i}]'>&times;</span>`;
        container.appendChild(chip);
    });
}

function renderNicChips() {
    const container = document.getElementById('nic-chips');
    container.innerHTML = '';
    selectedNics.forEach((n, i) => {
        const chip = document.createElement('span');
        chip.className = 'chip';
        chip.innerHTML = `<select class="qty-select" data-change='["setSelNicQty",${i},"$value"]'>${qtyOptions(n.qty)}</select> x ${esc(n.desc)} <span class="remove" data-click='["removeNic",${i}]'>&times;</span>`;
        container.appendChild(chip);
    });
}

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
function closeTunableInfoBackdrop(el, e) { if (e.target === el) closeTunableInfo(); }

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
    el.innerHTML = hints.map(h => `<span class="hint-tag">${h}</span>`).join(' ');
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

// Drive picker modal: list the catalog drives valid for the current storage type
// (hiding ones already added) as checkboxes, so several can be added at once
// without a bulky inline multi-select.
function openDriveSelect() {
    renderDriveSelectList();
    document.getElementById('drive-select-all').checked = false;
    document.getElementById('drive-select-modal').style.display = 'flex';
}

function closeDriveSelect() {
    document.getElementById('drive-select-modal').style.display = 'none';
}

function renderDriveSelectList() {
    const list = document.getElementById('drive-select-list');
    const allowed = allowedDriveTypes();
    const taken = new Set(selectedDrives.map(d => `${d.drive_type}|${d.size_tb}`));
    const avail = driveCatalog
        .filter(d => allowed.includes(d.drive_type) && !taken.has(`${d.drive_type}|${d.size_tb}`));
    if (!avail.length) {
        list.innerHTML = `<p class="drive-select-empty">${esc(allowed.length
            ? t('admin.drive_select.all_added')
            : t('admin.drive_select.no_options'))}</p>`;
        return;
    }
    // One section per drive type (in the storage type's media order), sizes asc.
    // The type is the section header, so each checkbox only needs its size.
    list.innerHTML = allowed.map(type => {
        const drives = avail.filter(d => d.drive_type === type)
            .sort((a, b) => a.size_tb - b.size_tb);
        if (!drives.length) return '';
        const items = drives.map(d =>
            `<label><input type="checkbox" value="${d.id}"> ${d.size_tb} TB</label>`
        ).join('');
        return `<div class="drive-select-group">`
            + `<h4 class="drive-select-group-title">${esc(type)}</h4>`
            + `<div class="drive-select-grid">${items}</div></div>`;
    }).join('');
}

function toggleAllDriveSelect(el) {
    document.querySelectorAll('#drive-select-list input[type=checkbox]')
        .forEach(cb => { cb.checked = el.checked; });
}

function confirmDriveSelect() {
    document.querySelectorAll('#drive-select-list input[type=checkbox]:checked').forEach(cb => {
        const cat = driveCatalog.find(d => d.id === parseInt(cb.value));
        if (cat && !selectedDrives.some(d => d.drive_type === cat.drive_type && d.size_tb === cat.size_tb)) {
            selectedDrives.push({ ...cat });
        }
    });
    renderDriveChips();
    closeDriveSelect();
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
    if (!name) { alert(t('admin.validate.model_name_required')); return; }

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
        notes: document.getElementById('edit-notes').value.trim() || null,
        cpu_options: cpuOptions,
        ram_options_gb: ramOptions,
        storage,
        nic_options: nicOptions,
    };

    const url = id ? `/admin/api/models/${id}` : '/admin/api/models';
    const method = id ? 'PUT' : 'POST';

    try {
        const resp = await fetch(url, { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
        const data = await resp.json();
        if (data.error) { alert(data.error); return; }
        closeEdit();
        loadModels();
        loadCatalogs();
    } catch (e) {
        alert(t('admin.msg.save_failed', {error: e.message}));
    }
}


// ==================== SUPER-ADMIN: USERS / TENANTS / SIZINGS ====================
// These tabs drive the /api/admin/users and /api/admin/super endpoints. The whole
// /admin area is already super-admin-gated server-side.

function adminEsc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function adminDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return isNaN(d) ? '' : d.toLocaleString();
}

function setStatus(id, msg, isError) {
    const el = document.getElementById(id);
    if (!el) return;
    if (!msg) { el.style.display = 'none'; return; }
    el.textContent = msg;
    el.className = 'admin-status ' + (isError ? 'status-err' : 'status-ok');
    el.style.display = 'block';
}

async function adminApi(url, opts) {
    const resp = await fetch(url, opts);
    let data = null;
    try { data = await resp.json(); } catch (e) {}
    return { ok: resp.ok, status: resp.status, data };
}

// ── Tenants ──────────────────────────────────────────────────────────────────

let tenantCache = [];

async function loadAdminTenants() {
    const { ok, data } = await adminApi('/api/admin/super/tenants');
    if (!ok) { setStatus('tenants-status', t('admin.tenants.load_error'), true); return; }
    tenantCache = data;
    // Keep the user-tab tenant filter in sync.
    const filter = document.getElementById('users-tenant-filter');
    if (filter) {
        const cur = filter.value;
        filter.innerHTML = `<option value="">${adminEsc(t('admin.users.all_tenants'))}</option>`
            + data.map(t => `<option value="${t.id}">${adminEsc(t.domain)}</option>`).join('');
        filter.value = cur;
    }
    const body = document.getElementById('admin-tenants-tbody');
    body.innerHTML = data.map(t => `
        <tr>
            <td>${adminEsc(t.domain)}</td>
            <td>${t.is_scale ? adminEsc(window.t('common.yes')) : ''}</td>
            <td>${t.user_count}</td>
            <td>${t.is_blocked ? `<span class="badge-blocked">${adminEsc(window.t('admin.tenants.blocked'))}</span>` : adminEsc(window.t('admin.status.active'))}</td>
            <td class="col-actions">
                <button class="btn btn-sm ${t.is_blocked ? 'btn-secondary' : 'btn-danger'}"
                        data-click='["toggleBlockTenant",${t.id},${t.is_blocked ? 'false' : 'true'}]'>
                    ${t.is_blocked ? adminEsc(window.t('admin.tenants.unblock')) : adminEsc(window.t('admin.tenants.block'))}
                </button>
            </td>
        </tr>`).join('') || `<tr><td colspan="5">${adminEsc(window.t('admin.tenants.none'))}</td></tr>`;
}

async function toggleBlockTenant(id, block) {
    if (block && !confirm(t('admin.tenants.block_confirm'))) return;
    const { ok, data } = await adminApi(`/api/admin/super/tenants/${id}/block`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ blocked: block }),
    });
    if (!ok) { setStatus('tenants-status', (data && data.error) || t('admin.msg.failed'), true); return; }
    setStatus('tenants-status', data.message, false);
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
    const { ok, data } = await adminApi('/api/admin/super/users?' + params.toString());
    const body = document.getElementById('admin-users-tbody');
    if (!ok) { body.innerHTML = ''; setStatus('admin-status', t('admin.users.load_error'), true); return; }
    const roleOpts = (sel) =>
        `<option value="user"${sel === 'user' ? ' selected' : ''}>${adminEsc(t('admin.role.user'))}</option>`
        + `<option value="tenant_admin"${sel === 'tenant_admin' ? ' selected' : ''}>${adminEsc(t('admin.role.tenant_admin'))}</option>`
        + `<option value="super_admin"${sel === 'super_admin' ? ' selected' : ''}>${adminEsc(t('admin.role.super_admin'))}</option>`;

    body.innerHTML = data.map(u => {
        const actions = [];
        if (!u.is_disabled) {
            actions.push(`<button class="btn btn-sm btn-secondary" data-click='["resetUserPassword",${u.id},"${adminEsc(u.email)}"]'>${adminEsc(t('admin.users.reset_password'))}</button>`);
            if (u.role !== 'super_admin') {
                actions.push(`<button class="btn btn-sm btn-danger" data-click='["disableAdminUser",${u.id}]'>${adminEsc(t('admin.users.disable'))}</button>`);
            }
        } else {
            actions.push(`<button class="btn btn-sm btn-secondary" data-click='["restoreUser",${u.id}]'>${adminEsc(t('admin.users.restore'))}</button>`);
            actions.push(`<button class="btn btn-sm btn-danger" data-click='["deleteUser",${u.id}]'>${adminEsc(t('common.delete'))}</button>`);
        }
        // Role is editable inline for active users; disabled users must be
        // restored before their role can change (server enforces this too).
        const roleCell = u.is_disabled
            ? adminEsc(u.role)
            : `<select class="role-select" data-change='["changeUserRole",${u.id},"$value","${adminEsc(u.role)}"]'>${roleOpts(u.role)}</select>`;
        const statusCell = u.is_disabled ? adminEsc(t('admin.status.disabled'))
            : (u.is_verified ? adminEsc(t('admin.status.active'))
               : `<span class="badge-unverified">${adminEsc(t('admin.status.unactivated'))}</span>`);
        return `<tr class="${u.is_disabled ? 'row-disabled' : ''}">
            <td>${adminEsc(u.email)}</td>
            <td>${adminEsc(u.tenant_domain)}</td>
            <td>${roleCell}${u.is_scale ? ` <span class="badge-scale">${adminEsc(t('admin.users.scale_badge'))}</span>` : ''}</td>
            <td>${statusCell}</td>
            <td>${adminDate(u.last_login_at)}</td>
            <td class="col-actions">${actions.join(' ')}</td>
        </tr>`;
    }).join('') || `<tr><td colspan="6">${adminEsc(t('admin.users.none'))}</td></tr>`;
}

async function changeUserRole(id, role, prevRole) {
    if (role === prevRole) return;
    const label = { user: t('admin.role.user'), tenant_admin: t('admin.role.tenant_admin'), super_admin: t('admin.role.super_admin') }[role] || role;
    if (!confirm(t('admin.users.role_confirm', {role: label}))) { loadAdminUsers(); return; }
    const { ok, data } = await adminApi(`/api/admin/super/users/${id}/role`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ role }),
    });
    if (!ok) { setStatus('admin-status', (data && data.error) || t('admin.msg.failed'), true); loadAdminUsers(); return; }
    setStatus('admin-status', t('admin.users.role_updated'), false);
    loadAdminUsers();
}

async function resetUserPassword(id, email) {
    const pw = prompt(t('admin.users.reset_prompt', {email: email}));
    if (pw === null) return;  // cancelled
    const body = pw.trim() ? { password: pw.trim() } : {};
    if (!pw.trim() && !confirm(t('admin.users.reset_email_confirm'))) return;
    const { ok, data } = await adminApi(`/api/admin/super/users/${id}/reset-password`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    setStatus('admin-status', (data && (data.message || data.error)) || (ok ? t('admin.msg.done') : t('admin.msg.failed')), !ok);
}

async function disableAdminUser(id) {
    if (!confirm(t('admin.users.disable_confirm'))) return;
    const { ok, data } = await adminApi(`/api/admin/users/${id}/disable`, { method: 'POST' });
    if (!ok) { setStatus('admin-status', (data && data.error) || t('admin.msg.failed'), true); return; }
    setStatus('admin-status', t('admin.users.disabled_ok'), false);
    loadAdminUsers();
}

async function restoreUser(id) {
    const { ok, data } = await adminApi(`/api/admin/super/users/${id}/restore`, { method: 'POST' });
    if (!ok) { setStatus('admin-status', (data && data.error) || t('admin.msg.failed'), true); return; }
    setStatus('admin-status', t('admin.users.restored_ok'), false);
    loadAdminUsers();
}

async function deleteUser(id) {
    if (!confirm(t('admin.users.delete_confirm'))) return;
    const { ok, data } = await adminApi(`/api/admin/super/users/${id}`, { method: 'DELETE' });
    if (!ok) { setStatus('admin-status', (data && data.error) || t('admin.msg.failed'), true); return; }
    setStatus('admin-status', t('admin.users.deleted_ok'), false);
    loadAdminUsers();
}

// ── Saved sizings ─────────────────────────────────────────────────────────────

async function loadAdminSizings() {
    const includeDeleted = document.getElementById('sizings-include-deleted').checked;
    const params = new URLSearchParams();
    if (includeDeleted) params.set('include_deleted', 'true');
    const { ok, data } = await adminApi('/api/admin/super/configs?' + params.toString());
    const body = document.getElementById('admin-sizings-tbody');
    if (!ok) { body.innerHTML = ''; setStatus('sizings-status-admin', t('admin.sizings.load_error'), true); return; }
    body.innerHTML = data.map(c => `
        <tr class="${c.is_deleted ? 'row-disabled' : ''}">
            <td>${adminEsc(c.name)}</td>
            <td>${adminEsc(c.owner_email || '')}</td>
            <td>${adminEsc(c.tenant_domain || '')}</td>
            <td><code>${adminEsc(c.code)}</code></td>
            <td>${c.is_deleted ? adminEsc(t('admin.status.deleted')) : adminEsc(t('admin.status.active'))}</td>
            <td>${adminDate(c.updated_at)}</td>
            <td class="col-actions">
                <button class="btn btn-sm btn-danger" data-click='["purgeSizing",${c.id}]'>${adminEsc(t('admin.sizings.purge'))}</button>
            </td>
        </tr>`).join('') || `<tr><td colspan="7">${adminEsc(t('admin.sizings.none'))}</td></tr>`;
}

async function purgeSizing(id) {
    if (!confirm(t('admin.sizings.purge_confirm'))) return;
    const { ok, data } = await adminApi(`/api/admin/super/configs/${id}/purge`, { method: 'DELETE' });
    if (!ok) { setStatus('sizings-status-admin', (data && data.error) || t('admin.msg.failed'), true); return; }
    setStatus('sizings-status-admin', t('admin.sizings.purged_ok'), false);
    loadAdminSizings();
}

async function runPurge() {
    if (!confirm(t('admin.sizings.run_purge_confirm'))) return;
    const { ok, data } = await adminApi('/api/admin/super/purge-run', { method: 'POST' });
    if (!ok) { setStatus('sizings-status-admin', t('admin.sizings.purge_failed'), true); return; }
    setStatus('sizings-status-admin',
        t('admin.sizings.purge_run_ok', {configs: data.configs_purged, users: data.users_purged}), false);
    loadAdminSizings();
}


// ==================== SUPER-ADMIN: EMAIL / SMTP + AUDIT LOG ====================

async function loadEmailSettings() {
    const { ok, data } = await adminApi('/api/admin/super/email-settings');
    if (!ok) { setStatus('email-status', t('admin.email.load_error'), true); return; }
    document.getElementById('smtp-host').value = data.smtp_host || '';
    document.getElementById('smtp-port').value = data.smtp_port || '587';
    document.getElementById('smtp-from').value = data.smtp_from || '';
    document.getElementById('smtp-username').value = data.smtp_username || '';
    document.getElementById('smtp-password').value = '';
    document.getElementById('smtp-pass-set').textContent = data.smtp_password_set ? t('admin.email.pass_set') : t('admin.email.pass_none');
    document.getElementById('smtp-use-tls').checked = !!data.smtp_use_tls;
    document.getElementById('verify-email-enabled').checked = !!data.verify_email_enabled;
    document.getElementById('email-active-state').textContent =
        data.verification_active ? t('admin.email.state_active')
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
    const { ok, data } = await adminApi('/api/admin/super/email-settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
    });
    if (!ok) { setStatus('email-status', (data && data.error) || t('admin.email.save_failed'), true); return; }
    setStatus('email-status', t('admin.email.saved_ok'), false);
    loadEmailSettings();
}

async function sendTestEmail() {
    const to = prompt(t('admin.email.test_prompt'));
    if (!to) return;
    setStatus('email-status', t('admin.email.sending'), false);
    const { ok, data } = await adminApi('/api/admin/super/email-settings/test', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ to }),
    });
    setStatus('email-status', (data && (data.message || data.error)) || (ok ? t('admin.email.sent') : t('admin.msg.failed')), !ok);
}

async function loadAuditLog() {
    const { ok, data } = await adminApi('/api/admin/super/audit');
    const body = document.getElementById('admin-audit-tbody');
    if (!ok) { body.innerHTML = ''; return; }
    body.innerHTML = data.map(e => `
        <tr>
            <td>${adminDate(e.created_at)}</td>
            <td>${adminEsc(e.actor_email || '')}</td>
            <td><code>${adminEsc(e.action)}</code></td>
            <td>${adminEsc(e.detail || '')}</td>
        </tr>`).join('') || `<tr><td colspan="4">${adminEsc(t('admin.audit.none'))}</td></tr>`;
}


// ==================== SUPER-ADMIN: INACTIVE (1yr+) USERS ====================

async function loadStaleUsers() {
    const { ok, data } = await adminApi('/api/admin/super/users/stale');
    const body = document.getElementById('stale-users-tbody');
    document.getElementById('stale-select-all').checked = false;
    if (!ok) { body.innerHTML = ''; setStatus('stale-status', t('admin.stale.load_error'), true); return; }

    // Populate the per-domain selector from the result.
    const domains = [...new Set(data.map(u => u.tenant_domain).filter(Boolean))].sort();
    const sel = document.getElementById('stale-domain-select');
    sel.innerHTML = `<option value="">${adminEsc(t('admin.stale.select_domain'))}</option>`
        + domains.map(d => `<option value="${adminEsc(d)}">${adminEsc(d)} (${data.filter(u => u.tenant_domain === d).length})</option>`).join('');

    body.innerHTML = data.map(u => `
        <tr>
            <td><input type="checkbox" class="stale-check" value="${u.id}" data-domain="${adminEsc(u.tenant_domain)}" data-click='["updateStaleCount"]'></td>
            <td>${adminEsc(u.email)}</td>
            <td>${adminEsc(u.tenant_domain)}</td>
            <td>${adminEsc(u.role)}</td>
            <td>${u.last_login_at ? adminDate(u.last_login_at) : `<span class="muted">${adminEsc(t('admin.stale.never'))}</span>`}</td>
            <td>${adminDate(u.created_at)}</td>
        </tr>`).join('') || `<tr><td colspan="6">${adminEsc(t('admin.stale.none'))}</td></tr>`;
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
    if (!ids.length) { setStatus('stale-status', t('admin.stale.select_first'), true); return; }
    if (!confirm(t('admin.stale.purge_confirm', {n: ids.length}))) return;
    const { ok, data } = await adminApi('/api/admin/super/users/purge', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids }),
    });
    if (!ok) { setStatus('stale-status', (data && data.error) || t('admin.sizings.purge_failed'), true); return; }
    setStatus('stale-status', data.skipped
        ? t('admin.stale.purged_skipped', {purged: data.purged, skipped: data.skipped})
        : t('admin.stale.purged', {purged: data.purged}), false);
    loadStaleUsers();
}
