let allModels = [];
let cpuCatalog = [];
let nicCatalog = [];
let driveCatalog = [];
let driveIops = [];

let selectedCpus = [];
let selectedNics = [];
let selectedDrives = [];

let catalogModalType = null;
let catalogModalId = null;
let catalogModalCallback = null;

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
}

// ── Load Data ──────────────────────────────────────────────────────────────

async function loadModels() {
    const resp = await fetch('/admin/api/models');
    allModels = await resp.json();

    const categories = [...new Set(allModels.map(m => m.category))].sort();
    const catFilter = document.getElementById('category-filter');
    const curCat = catFilter.value;
    catFilter.innerHTML = '<option value="all">All Categories</option>';
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
    const [cpuResp, nicResp, driveResp, iopsResp] = await Promise.all([
        fetch('/admin/api/cpus'),
        fetch('/admin/api/nics'),
        fetch('/admin/api/drives'),
        fetch('/admin/api/drive-iops'),
    ]);
    cpuCatalog = await cpuResp.json();
    nicCatalog = await nicResp.json();
    driveCatalog = await driveResp.json();
    driveIops = await iopsResp.json();

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
}

async function saveDriveIops() {
    const status = document.getElementById('iops-status');
    const payload = {};
    for (const [type, id] of Object.entries(IOPS_INPUTS)) {
        payload[type] = parseInt(document.getElementById(id).value, 10);
    }
    try {
        const resp = await fetch('/admin/api/drive-iops', {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || 'Save failed');
        driveIops = data.drive_iops || driveIops;
        status.textContent = 'Saved';
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
        tbody.innerHTML = '<tr><td colspan="10" style="text-align:center;padding:2rem;color:var(--text-muted)">No models found</td></tr>';
        return;
    }

    filtered.forEach(m => {
        const tr = document.createElement('tr');
        const badgeClass = m.status === 'Active' ? 'badge-active' : m.status === 'EOL' ? 'badge-eol' : 'badge-eos';
        const cpuSummary = m.cpu_options.map(c => c.desc).join('<br>');
        const ramSummary = m.ram_options_gb.join(', ') + ' GB';
        const storType = m.storage?.type || '-';

        tr.innerHTML = `
            <td><strong>${esc(m.name)}</strong>${m.validated_only ? ' <span class="badge badge-validated" title="Validated-only: no certified equivalent">Validated-only</span>' : ''}</td>
            <td><span class="badge ${badgeClass}">${m.status}</span></td>
            <td>${esc(m.category)}</td>
            <td>${esc(m.form_factor || '-')}</td>
            <td>${esc(m.socket || '-')}</td>
            <td class="cell-list">${cpuSummary || '-'}</td>
            <td class="cell-list">${ramSummary}</td>
            <td>${esc(storType)}</td>
            <td>${m.min_nodes}</td>
            <td class="col-actions">
                <button class="btn-icon" title="Edit" onclick="openEditModel(${m.id})">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                </button>
                <button class="btn-icon danger" title="Delete" onclick="deleteModel(${m.id}, '${esc(m.name)}')">
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
    document.getElementById('cpu-count').textContent = `${cpuCatalog.length} CPUs`;
    const tbody = document.getElementById('cpu-catalog-tbody');
    tbody.innerHTML = '';
    cpuCatalog.forEach(c => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${esc(c.desc)}</td>
            <td>${c.cores}</td>
            <td>${c.threads}</td>
            <td>${c.ghz}</td>
            <td><span class="count-pill">${c.used_by}</span></td>
            <td class="col-actions">
                <button class="btn-icon" title="Edit" onclick="openEditCatalogItem('cpu', ${c.id})">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                </button>
                <button class="btn-icon danger" title="Delete" onclick="deleteCatalogItem('cpu', ${c.id}, '${esc(c.desc)}')">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function renderNicCatalog() {
    document.getElementById('nic-count').textContent = `${nicCatalog.length} NICs`;
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
                <button class="btn-icon" title="Edit" onclick="openEditCatalogItem('nic', ${n.id})">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                </button>
                <button class="btn-icon danger" title="Delete" onclick="deleteCatalogItem('nic', ${n.id}, '${esc(n.desc)}')">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                </button>
            </td>
        `;
        tbody.appendChild(tr);
    });
}

function renderDriveCatalog() {
    document.getElementById('drive-count').textContent = `${driveCatalog.length} Drives`;
    const tbody = document.getElementById('drive-catalog-tbody');
    tbody.innerHTML = '';
    driveCatalog.forEach(d => {
        const tr = document.createElement('tr');
        tr.innerHTML = `
            <td>${esc(d.drive_type)}</td>
            <td>${d.size_tb}</td>
            <td><span class="count-pill">${d.used_by}</span></td>
            <td class="col-actions">
                <button class="btn-icon" title="Edit" onclick="openEditCatalogItem('drive', ${d.id})">
                    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
                </button>
                <button class="btn-icon danger" title="Delete" onclick="deleteCatalogItem('drive', ${d.id}, '${d.drive_type} ${d.size_tb}TB')">
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

    const titles = { cpu: 'Add CPU', nic: 'Add NIC', drive: 'Add Drive' };
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

    const titles = { cpu: 'Edit CPU', nic: 'Edit NIC', drive: 'Edit Drive' };
    document.getElementById('catalog-modal-title').textContent = titles[type];
    document.getElementById('catalog-modal-body').innerHTML = catalogFormHtml(type, item);
    document.getElementById('catalog-modal').style.display = 'flex';
}

function catalogFormHtml(type, item) {
    if (type === 'cpu') return `
        <div class="form-group"><label>Description *</label>
            <input type="text" id="cat-desc" value="${esc(item.desc || '')}" placeholder="e.g. Xeon Gold 6442Y 24C/48T 2.6GHz"></div>
        <div class="form-row" style="margin-top:0.75rem">
            <div class="form-group"><label>Cores</label><input type="number" id="cat-cores" value="${item.cores || ''}" min="1"></div>
            <div class="form-group"><label>Threads</label><input type="number" id="cat-threads" value="${item.threads || ''}" min="1"></div>
            <div class="form-group"><label>GHz</label><input type="number" id="cat-ghz" value="${item.ghz || ''}" step="0.1" min="0.1"></div>
        </div>`;
    if (type === 'nic') return `
        <div class="form-group"><label>Description *</label>
            <input type="text" id="cat-desc" value="${esc(item.desc || '')}" placeholder="e.g. 10GbE SFP+ 4-port Network Card (Intel X710)"></div>
        <div class="form-row" style="margin-top:0.75rem">
            <div class="form-group"><label>Ports</label><input type="number" id="cat-ports" value="${item.ports || ''}" min="1"></div>
            <div class="form-group"><label>Speed</label><input type="text" id="cat-speed" value="${esc(item.speed || '')}" placeholder="e.g. 10GbE"></div>
        </div>`;
    if (type === 'drive') return `
        <div class="form-row">
            <div class="form-group"><label>Type *</label>
                <select id="cat-drive-type">
                    <option value="HDD" ${item.drive_type === 'HDD' ? 'selected' : ''}>HDD</option>
                    <option value="SSD" ${item.drive_type === 'SSD' ? 'selected' : ''}>SSD</option>
                    <option value="NVMe" ${item.drive_type === 'NVMe' ? 'selected' : ''}>NVMe</option>
                </select></div>
            <div class="form-group"><label>Size (TB) *</label>
                <input type="number" id="cat-size-tb" value="${item.size_tb || ''}" step="0.01" min="0.01"></div>
        </div>`;
    return '';
}

function closeCatalogModal() {
    document.getElementById('catalog-modal').style.display = 'none';
    catalogModalCallback = null;
}

async function saveCatalogItem() {
    const type = catalogModalType;
    let payload, url, method;

    if (type === 'cpu') {
        payload = {
            desc: document.getElementById('cat-desc').value.trim(),
            cores: parseInt(document.getElementById('cat-cores').value) || 0,
            threads: parseInt(document.getElementById('cat-threads').value) || 0,
            ghz: parseFloat(document.getElementById('cat-ghz').value) || 0,
        };
        if (!payload.desc) { alert('Description is required'); return; }
    } else if (type === 'nic') {
        payload = {
            desc: document.getElementById('cat-desc').value.trim(),
            ports: parseInt(document.getElementById('cat-ports').value) || 0,
            speed: document.getElementById('cat-speed').value.trim(),
        };
        if (!payload.desc) { alert('Description is required'); return; }
    } else if (type === 'drive') {
        payload = {
            drive_type: document.getElementById('cat-drive-type').value,
            size_tb: parseFloat(document.getElementById('cat-size-tb').value) || 0,
        };
        if (!payload.drive_type || payload.size_tb <= 0) { alert('Type and size are required'); return; }
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
        alert('Save failed: ' + e.message);
    }
}

async function deleteCatalogItem(type, id, label) {
    if (!confirm(`Delete "${label}"? This cannot be undone.`)) return;
    const base = `/admin/api/${type === 'cpu' ? 'cpus' : type === 'nic' ? 'nics' : 'drives'}`;
    try {
        const resp = await fetch(`${base}/${id}`, { method: 'DELETE' });
        const data = await resp.json();
        if (data.error) { alert(data.error); return; }
        await loadCatalogs();
    } catch (e) {
        alert('Delete failed: ' + e.message);
    }
}

// ── Delete Model ───────────────────────────────────────────────────────────

async function deleteModel(id, name) {
    if (!confirm(`Delete model "${name}"? This cannot be undone.`)) return;
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
    if (!fileInput.files.length) { showCatalogImportStatus('Please select a file', true); return; }

    const formData = new FormData();
    formData.append('file', fileInput.files[0]);
    showCatalogImportStatus('Importing...', false);

    try {
        const resp = await fetch('/admin/api/import-catalog', { method: 'POST', body: formData });
        const data = await resp.json();
        if (data.error) { showCatalogImportStatus(data.error, true); return; }
        showCatalogImportStatus(data.message, false);
        loadCatalogs();
        loadModels();
    } catch (e) {
        showCatalogImportStatus('Import failed: ' + e.message, true);
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

function populatePickers() {
    const cpuPicker = document.getElementById('cpu-picker');
    cpuPicker.innerHTML = '<option value="">Select a CPU to add...</option>';
    cpuCatalog.forEach(c => {
        const opt = document.createElement('option');
        opt.value = c.id;
        opt.textContent = `${c.desc} (${c.cores}C/${c.threads}T ${c.ghz}GHz)`;
        cpuPicker.appendChild(opt);
    });

    const nicPicker = document.getElementById('nic-picker');
    nicPicker.innerHTML = '<option value="">Select a NIC to add...</option>';
    nicCatalog.forEach(n => {
        const opt = document.createElement('option');
        opt.value = n.id;
        opt.textContent = `${n.desc} (${n.ports}p ${n.speed})`;
        nicPicker.appendChild(opt);
    });

    const drivePicker = document.getElementById('drive-picker');
    drivePicker.innerHTML = '<option value="">Select a drive to add...</option>';
    driveCatalog.forEach(d => {
        const opt = document.createElement('option');
        opt.value = d.id;
        opt.textContent = `${d.drive_type} ${d.size_tb} TB`;
        drivePicker.appendChild(opt);
    });
}

function refreshPickersAfterCatalogChange(type) {
    populatePickers();
}

function openAddModel() {
    document.getElementById('edit-id').value = '';
    document.getElementById('edit-title').textContent = 'Add Model';
    document.getElementById('edit-name').value = '';
    document.getElementById('edit-status').value = 'Active';
    document.getElementById('edit-category').value = '';
    document.getElementById('edit-form-factor').value = '';
    document.getElementById('edit-chassis').value = '';
    document.getElementById('edit-socket').value = 'single';
    document.getElementById('edit-psu').value = '';
    document.getElementById('edit-ram-slots').value = '0';
    document.getElementById('edit-min-nodes').value = '1';
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
    populatePickers();
    document.getElementById('edit-modal').style.display = 'flex';
}

async function openEditModel(id) {
    const resp = await fetch(`/admin/api/models/${id}`);
    const m = await resp.json();

    document.getElementById('edit-id').value = id;
    document.getElementById('edit-title').textContent = `Edit: ${m.name}`;
    document.getElementById('edit-name').value = m.name;
    document.getElementById('edit-status').value = m.status;
    document.getElementById('edit-category').value = m.category || '';
    document.getElementById('edit-form-factor').value = m.form_factor || '';
    document.getElementById('edit-chassis').value = m.chassis || '';
    document.getElementById('edit-socket').value = m.socket || 'single';
    document.getElementById('edit-psu').value = m.psu || '';
    document.getElementById('edit-ram-slots').value = m.ram_slots || 0;
    document.getElementById('edit-min-nodes').value = m.min_nodes || 1;
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

    populatePickers();
    document.getElementById('edit-modal').style.display = 'flex';
}

function closeEdit() {
    document.getElementById('edit-modal').style.display = 'none';
}

// ── Picker: Add from dropdown ──────────────────────────────────────────────

function addCpuFromPicker() {
    const sel = document.getElementById('cpu-picker');
    const id = parseInt(sel.value);
    if (!id) return;
    const cat = cpuCatalog.find(c => c.id === id);
    if (cat) {
        const flags = parseModelName(document.getElementById('edit-name').value.trim());
        selectedCpus.push({ ...cat, qty: flags.isDual ? 2 : 1 });
        renderCpuChips();
    }
    sel.value = '';
}

function addNicFromPicker() {
    const sel = document.getElementById('nic-picker');
    const id = parseInt(sel.value);
    if (!id) return;
    const cat = nicCatalog.find(n => n.id === id);
    if (cat) {
        selectedNics.push({ ...cat, qty: 1 });
        renderNicChips();
    }
    sel.value = '';
}

function addDriveFromPicker() {
    const sel = document.getElementById('drive-picker');
    const id = parseInt(sel.value);
    if (!id) return;
    const cat = driveCatalog.find(d => d.id === id);
    if (cat) {
        selectedDrives.push({ ...cat });
        renderDriveChips();
    }
    sel.value = '';
}

// ── Chip Rendering ─────────────────────────────────────────────────────────

function renderCpuChips() {
    const container = document.getElementById('cpu-chips');
    container.innerHTML = '';
    selectedCpus.forEach((c, i) => {
        const chip = document.createElement('span');
        chip.className = 'chip';
        chip.innerHTML = `<select class="qty-select" onchange="selectedCpus[${i}].qty=parseInt(this.value)">${qtyOptions(c.qty)}</select> x ${esc(c.desc)} <span class="remove" onclick="removeCpu(${i})">&times;</span>`;
        container.appendChild(chip);
    });
}

function renderNicChips() {
    const container = document.getElementById('nic-chips');
    container.innerHTML = '';
    selectedNics.forEach((n, i) => {
        const chip = document.createElement('span');
        chip.className = 'chip';
        chip.innerHTML = `<select class="qty-select" onchange="selectedNics[${i}].qty=parseInt(this.value)">${qtyOptions(n.qty)}</select> x ${esc(n.desc)} <span class="remove" onclick="removeNic(${i})">&times;</span>`;
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
        chip.innerHTML = `${esc(d.drive_type)} ${d.size_tb} TB <span class="remove" onclick="removeDrive(${i})">&times;</span>`;
        container.appendChild(chip);
    });
}

function removeCpu(i) { selectedCpus.splice(i, 1); renderCpuChips(); }
function removeNic(i) { selectedNics.splice(i, 1); renderNicChips(); }
function removeDrive(i) { selectedDrives.splice(i, 1); renderDriveChips(); }

// ── RAM Chips ──────────────────────────────────────────────────────────────

function addRamChip(size) {
    const list = document.getElementById('ram-list');
    const inputWrap = list.querySelector('.ram-input-wrap');
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.dataset.size = size;
    chip.innerHTML = `${size} GB <span class="remove" onclick="this.parentElement.remove()">&times;</span>`;
    if (inputWrap) list.insertBefore(chip, inputWrap);
    else list.appendChild(chip);
}

function addRamInput() {
    const list = document.getElementById('ram-list');
    if (list.querySelector('.ram-input-wrap')) return;
    const wrap = document.createElement('span');
    wrap.className = 'ram-input-wrap';
    wrap.innerHTML = `<input type="number" placeholder="GB" min="1" onkeydown="if(event.key==='Enter'){event.preventDefault();addRamFromInput(this)}">
        <button class="btn btn-small" onclick="addRamFromInput(this.previousElementSibling)">+</button>`;
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
        hints.push('Dual CPU (D) → socket set to dual, CPU qty defaults to 2');
    } else if (flags.isEdge || flags.isCore) {
        document.getElementById('edit-socket').value = 'single';
        selectedCpus.forEach(c => { c.qty = 1; });
        renderCpuChips();
        hints.push('Single CPU → socket set to single, CPU qty defaults to 1');
    }

    if (flags.isFlash) {
        const storType = document.getElementById('edit-stor-type').value;
        if (['hdd_only', 'hybrid', 'hybrid_nvme'].includes(storType)) {
            document.getElementById('edit-stor-type').value = 'nvme_only';
            updateStorageFields();
        }
        selectedDrives = selectedDrives.filter(d => d.drive_type !== 'HDD');
        renderDriveChips();
        filterDrivePickerNoHDD(true);
        hints.push('All-Flash (F) → no HDD allowed, storage type set to flash');
    } else {
        filterDrivePickerNoHDD(false);
    }

    if (flags.isEdge) hints.unshift('Edge model (HE)');
    if (flags.isCore) hints.unshift('Core model (HC)');

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

function filterDrivePickerNoHDD(noHdd) {
    const picker = document.getElementById('drive-picker');
    const current = picker.value;
    picker.innerHTML = '<option value="">Select a drive to add...</option>';
    driveCatalog.forEach(d => {
        if (noHdd && d.drive_type === 'HDD') return;
        const opt = document.createElement('option');
        opt.value = d.id;
        opt.textContent = `${d.drive_type} ${d.size_tb} TB`;
        picker.appendChild(opt);
    });
    picker.value = current;
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
}

// ── Save Model ─────────────────────────────────────────────────────────────

async function saveModel() {
    const id = document.getElementById('edit-id').value;
    const name = document.getElementById('edit-name').value.trim();
    if (!name) { alert('Model name is required.'); return; }

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
        alert('Save failed: ' + e.message);
    }
}
