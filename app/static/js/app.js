let currentMode = 'appliance';
// Which workload flow currently owns the shared sizing block: 'import' or
// 'manual'. recalcRecommendations() reads the matching summary and keys results
// (lastRecommendations/lastProjection/lastSummary) by it.
let activeMode = null;
let modelsCache = {};
let currentModel = null;
let lastRecommendations = {};
let lastProjection = {};
let lastSummary = {};
let lastConfigResult = null;

let importVms = [];
let originalImportSummary = null;
let includeLocalStorage = false;   // include per-host local datastores in sizing
let vmExclusions = { compute: new Set(), storage: new Set() };
// Per-VM edits keyed by index: { [idx]: { model?, vcpus?, provisioned_memory_gb? } }.
// Lets the user right-size individual workloads (e.g. underused physical servers)
// without mutating the pristine import, which the summary is recomputed from.
let vmConfig = {};
let vmSortField = 'name';
let vmSortAsc = true;

document.addEventListener('DOMContentLoaded', () => {
    loadModels();
    loadValidatedNics();
    initDiskTiers();
    populateSizingModelDropdown('sizing-model-select', false);
});

function switchMode(mode) {
    currentMode = mode;
    document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
    document.querySelector(`[data-mode="${mode}"]`).classList.add('active');
    document.getElementById('appliance-form').style.display = mode === 'appliance' ? 'block' : 'none';
    document.getElementById('validated-form').style.display = mode === 'validated' ? 'block' : 'none';
    document.getElementById('import-form').style.display = mode === 'import' ? 'block' : 'none';
    document.getElementById('manual-form').style.display = mode === 'manual' ? 'block' : 'none';
    document.getElementById('results').style.display = 'none';

    // The shared Sizing Options + Growth + Recommendations block is shown for
    // whichever workload flow (import/manual) already has a summary; hidden for
    // the appliance/validated builders. Re-running recalc keeps the displayed
    // results consistent with the (shared) control values.
    const summary = mode === 'import' ? importSummary
                  : mode === 'manual' ? manualSummary : null;
    const sizing = document.getElementById('sizing-results');
    if (summary) {
        activeMode = mode;
        sizing.style.display = 'block';
        recalcRecommendations();
    } else {
        sizing.style.display = 'none';
    }
}

async function loadModels() {
    const status = document.getElementById('status-filter').value;
    const resp = await fetch(`/api/models?mode=appliance&status=${status}`);
    if (!resp.ok) return;  // not signed in yet — the login gate will prompt
    modelsCache = await resp.json();

    const select = document.getElementById('model-select');
    select.innerHTML = '<option value="">-- Select Model --</option>';

    const categories = {};
    for (const [name, data] of Object.entries(modelsCache)) {
        const cat = data.category;
        if (!categories[cat]) categories[cat] = [];
        categories[cat].push(name);
    }

    for (const [cat, models] of Object.entries(categories)) {
        const group = document.createElement('optgroup');
        group.label = cat;
        models.forEach(m => {
            const opt = document.createElement('option');
            opt.value = m;
            const status = modelsCache[m].status;
            opt.textContent = status !== 'Active' ? `${m} (${status})` : m;
            group.appendChild(opt);
        });
        select.appendChild(group);
    }

    document.getElementById('model-details').style.display = 'none';
    document.getElementById('results').style.display = 'none';
}

// Populate a "Size For Model" dropdown (import/manual). Lists certified models
// grouped by category; status shown for non-Active. EOL/EOS models appear only
// when includeEolEos is set. Preserves the current selection when still valid.
async function populateSizingModelDropdown(selectId, includeEolEos) {
    const select = document.getElementById(selectId);
    if (!select) return;
    const prev = select.value;
    const status = includeEolEos ? 'all' : 'active';
    let models;
    try {
        const resp = await fetch(`/api/models?mode=appliance&status=${status}`);
        if (!resp.ok) return;
        models = await resp.json();
    } catch (e) {
        return;
    }
    const categories = {};
    for (const [name, data] of Object.entries(models)) {
        (categories[data.category] = categories[data.category] || []).push(name);
    }
    let html = '<option value="">All models (best fit)</option>';
    for (const [cat, names] of Object.entries(categories)) {
        html += `<optgroup label="${cat}">`;
        names.forEach(m => {
            const st = models[m].status;
            const label = st !== 'Active' ? `${m} (${st})` : m;
            html += `<option value="${m}">${label}</option>`;
        });
        html += '</optgroup>';
    }
    select.innerHTML = html;
    select.value = (prev && models[prev]) ? prev : '';
}

function onEolToggle() {
    const include = document.getElementById('sizing-include-eol').checked;
    populateSizingModelDropdown('sizing-model-select', include).then(recalcRecommendations);
}

function loadModelDetails() {
    const modelName = document.getElementById('model-select').value;
    if (!modelName || !modelsCache[modelName]) {
        document.getElementById('model-details').style.display = 'none';
        return;
    }

    currentModel = modelsCache[modelName];
    const details = document.getElementById('model-details');
    details.style.display = 'block';

    document.getElementById('detail-model-name').textContent = modelName;

    const statusBadge = document.getElementById('detail-status');
    statusBadge.textContent = currentModel.status;
    statusBadge.className = 'status-badge status-' + currentModel.status.toLowerCase().replace(/\s/g, '');

    document.getElementById('detail-category').textContent = currentModel.category;
    document.getElementById('detail-chassis').textContent = currentModel.chassis;

    const cpuSelect = document.getElementById('cpu-select');
    cpuSelect.innerHTML = '';
    currentModel.cpu_options.forEach((cpu, i) => {
        const opt = document.createElement('option');
        opt.value = i;
        opt.textContent = `${cpu.desc} (${cpu.cores}C/${cpu.threads}T @ ${cpu.ghz}GHz)`;
        cpuSelect.appendChild(opt);
    });

    const ramSelect = document.getElementById('ram-select');
    ramSelect.innerHTML = '';
    currentModel.ram_options_gb.forEach(r => {
        const opt = document.createElement('option');
        opt.value = r;
        opt.textContent = `${r} GB`;
        ramSelect.appendChild(opt);
    });

    const nicSelect = document.getElementById('nic-select');
    nicSelect.innerHTML = '';
    currentModel.nic_options.forEach((nic, i) => {
        const opt = document.createElement('option');
        opt.value = i;
        opt.textContent = nic.desc;
        nicSelect.appendChild(opt);
    });

    buildStorageSection(currentModel.storage);
    buildStorageOnlySection(currentModel);

    const minNodes = currentModel.min_nodes || 1;
    const nodeInput = document.getElementById('node-count');
    nodeInput.min = minNodes;
    if (parseInt(nodeInput.value) < minNodes) nodeInput.value = minNodes;

    calculate();
}

// Populate the storage-only CPU (single-socket variants) and RAM dropdowns from
// the selected model, and reset the toggle to off on each model change.
function buildStorageOnlySection(model) {
    const enable = document.getElementById('so-enable');
    if (enable) enable.checked = false;
    const cfg = document.getElementById('so-config');
    if (cfg) cfg.style.display = 'none';

    const cpuSelect = document.getElementById('so-cpu-select');
    if (cpuSelect) {
        cpuSelect.innerHTML = '';
        (model.storage_only_cpu_options || []).forEach((cpu, i) => {
            const opt = document.createElement('option');
            opt.value = i;
            opt.textContent = `${cpu.desc} (${cpu.cores}C/${cpu.threads}T @ ${cpu.ghz}GHz)`;
            cpuSelect.appendChild(opt);
        });
    }
    const ramSelect = document.getElementById('so-ram-select');
    if (ramSelect) {
        ramSelect.innerHTML = '';
        (model.ram_options_gb || []).forEach(r => {
            const opt = document.createElement('option');
            opt.value = r;
            opt.textContent = `${r} GB`;
            ramSelect.appendChild(opt);
        });
    }
}

function toggleStorageOnly() {
    const on = document.getElementById('so-enable').checked;
    document.getElementById('so-config').style.display = on ? 'flex' : 'none';
    calculate();
}

function buildStorageSection(storage) {
    const section = document.getElementById('storage-section');
    section.innerHTML = '';

    const stype = storage.type;

    if (stype === 'nvme_only') {
        section.innerHTML = `
            <div class="form-group">
                <label>NVMe Drive Size (TB) &times; ${storage.drives_per_node || 1}</label>
                <select id="stor-nvme" onchange="calculate()">
                    ${storage.nvme_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>`;
    } else if (stype === 'ssd_only') {
        section.innerHTML = `
            <div class="form-group">
                <label>SSD Drive Size (TB) &times; ${storage.drives_per_node || 4}</label>
                <select id="stor-ssd" onchange="calculate()">
                    ${storage.ssd_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>`;
    } else if (stype === 'hdd_only') {
        section.innerHTML = `
            <div class="form-group">
                <label>HDD Drive Size (TB) &times; ${storage.drives_per_node || 4}</label>
                <select id="stor-hdd" onchange="calculate()">
                    ${storage.hdd_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>`;
    } else if (stype === 'hybrid') {
        section.innerHTML = `
            <div class="form-group">
                <label>HDD Size (TB) &times; ${storage.hdd_count}</label>
                <select id="stor-hdd" onchange="calculate()">
                    ${storage.hdd_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>
            <div class="form-group">
                <label>SSD Cache Size (TB) &times; ${storage.ssd_count}</label>
                <select id="stor-ssd" onchange="calculate()">
                    ${storage.ssd_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>`;
    } else if (stype === 'hybrid_nvme') {
        section.innerHTML = `
            <div class="form-group">
                <label>HDD Size (TB) &times; ${storage.hdd_count}</label>
                <select id="stor-hdd" onchange="calculate()">
                    ${storage.hdd_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>
            <div class="form-group">
                <label>NVMe Cache Size (TB) &times; ${storage.nvme_count}</label>
                <select id="stor-nvme" onchange="calculate()">
                    ${storage.nvme_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>`;
    } else if (stype === 'nvme_and_ssd') {
        section.innerHTML = `
            <div class="form-group">
                <label>NVMe Drive Size (TB)</label>
                <select id="stor-nvme" onchange="calculate()">
                    ${storage.nvme_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>
            <div class="form-group">
                <label>SSD Drive Size (TB)</label>
                <select id="stor-ssd" onchange="calculate()">
                    ${storage.ssd_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>`;
    } else if (stype === 'cloud') {
        section.innerHTML = `
            <div class="form-group">
                <label>Storage Tier</label>
                <select id="stor-cloud" onchange="calculate()">
                    ${storage.options.map(s => `<option value="${s}">${s}</option>`).join('')}
                </select>
            </div>`;
    }
}

async function calculate() {
    const modelName = document.getElementById('model-select').value;
    if (!modelName || !currentModel) return;

    const nodeCount = parseInt(document.getElementById('node-count').value);
    const cpuIndex = parseInt(document.getElementById('cpu-select').value);
    const ramGb = parseInt(document.getElementById('ram-select').value);

    const payload = {
        mode: 'appliance',
        model: modelName,
        node_count: nodeCount,
        cpu_index: cpuIndex,
        ram_gb: ramGb,
    };

    const hddEl = document.getElementById('stor-hdd');
    const ssdEl = document.getElementById('stor-ssd');
    const nvmeEl = document.getElementById('stor-nvme');

    if (hddEl) payload.hdd_tb = parseFloat(hddEl.value);
    if (ssdEl) payload.ssd_tb = parseFloat(ssdEl.value);
    if (nvmeEl) payload.nvme_tb = parseFloat(nvmeEl.value);

    const soEnable = document.getElementById('so-enable');
    if (soEnable && soEnable.checked) {
        payload.storage_only = {
            count: parseInt(document.getElementById('so-count').value) || 0,
            cpu_index: parseInt(document.getElementById('so-cpu-select').value) || 0,
            ram_gb: parseInt(document.getElementById('so-ram-select').value) || 0,
        };
    }

    const resp = await fetch('/api/calculate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
    });

    const result = await resp.json();
    displayResults(result);
}

async function calculateValidated() {
    const nodeCount = parseInt(document.getElementById('val-node-count').value);
    const cores = parseInt(document.getElementById('val-cores').value);
    const threads = parseInt(document.getElementById('val-threads').value);
    const ghz = parseFloat(document.getElementById('val-ghz').value);
    const ramGb = parseInt(document.getElementById('val-ram').value);

    const disks = collectValidatedDisks();

    const validation = validateDisks(disks);
    const totalClusterDisks = disks.length * nodeCount;
    if (totalClusterDisks > 100) {
        validation.errors.push(
            `Cluster disk limit exceeded: ${totalClusterDisks} disks ` +
            `(${disks.length} per node × ${nodeCount} nodes). The maximum is ` +
            `100 disks per cluster. When more storage capacity is required, the ` +
            `recommendation is to deploy multiple clusters or use bigger disks.`
        );
    }
    const valDiv = document.getElementById('disk-validation');
    if (validation.errors.length > 0) {
        valDiv.innerHTML = validation.errors.map(e => `<div class="val-error">${e}</div>`).join('');
        valDiv.style.display = 'block';
    } else {
        valDiv.innerHTML = validation.warnings.map(w => `<div class="val-ok">${w}</div>`).join('');
        valDiv.style.display = validation.warnings.length > 0 ? 'block' : 'none';
    }

    const payload = {
        mode: 'validated',
        node_count: nodeCount,
        cores_per_node: cores,
        threads_per_node: threads,
        ghz: ghz,
        ram_gb: ramGb,
        disks: disks,
    };

    const valSoEnable = document.getElementById('val-so-enable');
    if (valSoEnable && valSoEnable.checked) {
        payload.storage_only = {
            count: parseInt(document.getElementById('val-so-count').value) || 0,
            cores: parseInt(document.getElementById('val-so-cores').value) || 1,
            threads: parseInt(document.getElementById('val-so-threads').value) || 2,
            ghz: parseFloat(document.getElementById('val-so-ghz').value) || 2.0,
            ram_gb: parseInt(document.getElementById('val-so-ram').value) || 16,
        };
    }

    const resp = await fetch('/api/calculate', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
    });

    const result = await resp.json();
    displayResults(result);
}

function validateDisks(disks) {
    const errors = [];
    const warnings = [];

    if (disks.length === 0) {
        errors.push('At least 1 disk required');
        return {errors, warnings};
    }
    if (disks.length === 2) {
        errors.push('2 disks not supported. Use 1 or 3+ disks.');
    }

    const hasSpinning = disks.some(d => ['SAS', 'NLSAS', 'SATA', 'HDD'].includes(d.type));
    const hasFlash = disks.some(d => ['SSD', 'NVMe'].includes(d.type));

    if (hasSpinning && hasFlash) {
        const total = disks.reduce((s, d) => s + d.size_tb, 0);
        const flash = disks.filter(d => ['SSD', 'NVMe'].includes(d.type)).reduce((s, d) => s + d.size_tb, 0);
        const pct = (flash / total) * 100;
        if (pct < 7) errors.push(`Flash tier too small: ${pct.toFixed(1)}% (min 7%)`);
        if (pct > 24.3) errors.push(`Flash tier too large: ${pct.toFixed(1)}% (max 24.3%)`);
        if (pct >= 7 && pct <= 24.3) warnings.push(`Hybrid config OK: flash tier is ${pct.toFixed(1)}% of total`);
    }

    if (disks.length >= 3 && !hasSpinning && hasFlash) {
        warnings.push('All-Flash configuration detected');
    }

    return {errors, warnings};
}

const DISK_SIZES = {
    spinning: [1, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24],
    SSD: [0.24, 0.48, 0.96, 1.92, 3.84, 7.68, 15.36, 30.72],
    NVMe: [0.25, 0.5, 0.96, 1, 1.92, 2, 3.84, 4, 7.68, 8, 15.36, 30.72],
};

function sizesForType(type) {
    if (['SAS', 'NLSAS', 'SATA', 'HDD'].includes(type)) return DISK_SIZES.spinning;
    if (type === 'SSD') return DISK_SIZES.SSD;
    return DISK_SIZES.NVMe;
}

// Fill the size dropdown that belongs to the same tier row as this type select,
// preserving the current selection when the new media still offers that size.
function populateDiskSizes(typeSelect) {
    const sizeSelect = typeSelect.closest('.tier-row').querySelector('.disk-size');
    const sizes = sizesForType(typeSelect.value);
    const prev = sizeSelect.value;
    sizeSelect.innerHTML = sizes.map(s => `<option value="${s}">${s} TB</option>`).join('');
    if (sizes.map(String).includes(prev)) sizeSelect.value = prev;
}

function toggleValidatedStorageOnly() {
    const on = document.getElementById('val-so-enable').checked;
    document.getElementById('val-so-config').style.display = on ? 'flex' : 'none';
}

function setTierMode(mode) {
    document.getElementById('single-tier-config').style.display = mode === 'single' ? 'block' : 'none';
    document.getElementById('dual-tier-config').style.display = mode === 'dual' ? 'block' : 'none';
}

// Seed the size dropdowns and pick sensible defaults (single = all-flash NVMe,
// dual = a valid hybrid with the fast tier inside the 7-24.3% band).
function initDiskTiers() {
    document.getElementById('st-type').value = 'NVMe';
    populateDiskSizes(document.getElementById('st-type'));
    document.getElementById('st-size').value = '3.84';

    document.getElementById('dt-cap-type').value = 'SATA';
    populateDiskSizes(document.getElementById('dt-cap-type'));
    document.getElementById('dt-cap-size').value = '8';

    document.getElementById('dt-fast-type').value = 'NVMe';
    populateDiskSizes(document.getElementById('dt-fast-type'));
    document.getElementById('dt-fast-size').value = '3.84';

    setTierMode('single');
}

// Expand the tier selections into the flat per-disk list the API expects.
function collectValidatedDisks() {
    const mode = document.querySelector('input[name="disk-tier-mode"]:checked').value;
    const disks = [];
    const addTier = (typeId, sizeId, qtyId) => {
        const type = document.getElementById(typeId).value;
        const size = parseFloat(document.getElementById(sizeId).value);
        const qty = parseInt(document.getElementById(qtyId).value, 10) || 0;
        for (let i = 0; i < qty; i++) disks.push({type, size_tb: size});
    };
    if (mode === 'single') {
        addTier('st-type', 'st-size', 'st-qty');
    } else {
        addTier('dt-cap-type', 'dt-cap-size', 'dt-cap-qty');
        addTier('dt-fast-type', 'dt-fast-size', 'dt-fast-qty');
    }
    return disks;
}

async function loadValidatedNics() {
    const resp = await fetch('/api/models?mode=validated');
    if (!resp.ok) return;  // not signed in yet
    const data = await resp.json();
    const select = document.getElementById('val-nic');
    select.innerHTML = '';
    data.nics.forEach(nic => {
        const opt = document.createElement('option');
        opt.value = nic.desc;
        opt.textContent = `${nic.desc} (${nic.speed}, ${nic.ports} ports)`;
        select.appendChild(opt);
    });
}

const WITNESS_MESSAGE =
    'A 2-node cluster requires a separate witness device and HyperCore 9.7.5 or ' +
    'later. The witness can be almost any device with an Intel Core i3 (or newer) ' +
    'CPU, 16 GB of RAM, and about 100 GB of free disk to install HyperCore.';

function witnessBarHtml() {
    return '<div class="info-bar">' +
        '<span class="info-bar-icon">i</span>' +
        `<span><strong>Witness device required.</strong> ${WITNESS_MESSAGE}</span>` +
        '</div>';
}

function displayResults(result) {
    const section = document.getElementById('results');
    const errorDiv = document.getElementById('error-msg');
    const witnessDiv = document.getElementById('witness-info');
    witnessDiv.style.display = 'none';

    if (result.error) {
        errorDiv.textContent = result.error;
        errorDiv.style.display = 'block';
        section.style.display = 'block';
        document.querySelector('.results-grid').style.display = 'none';
        return;
    }

    errorDiv.style.display = 'none';
    document.querySelector('.results-grid').style.display = 'grid';
    section.style.display = 'block';

    lastConfigResult = result;
    const exportBtn = document.getElementById('config-export-btn');
    if (exportBtn) exportBtn.style.display = 'inline-block';

    const so = result.storage_only;
    const numClusters = result.num_clusters || 1;
    const totalNodes = result.total_node_count || result.node_count;
    let nodesText = so
        ? `${result.node_count} HCI + ${so.count} storage-only (${result.total_node_count} total)`
        : `${totalNodes} node${totalNodes === 1 ? '' : 's'}`;
    if (numClusters > 1 && result.cluster_layout) {
        nodesText += `, ${numClusters} clusters: ${result.cluster_layout.join(' + ')}`;
    }
    document.getElementById('result-nodes').textContent = nodesText;

    // A witness is needed only when the whole cluster is exactly 2 nodes
    // (storage-only nodes add quorum, so 2 HCI + storage-only does not).
    if ((result.total_node_count || result.node_count) === 2) {
        witnessDiv.innerHTML = witnessBarHtml();
        witnessDiv.style.display = 'block';
    }

    const n1Desc = document.getElementById('n1-desc');
    if (n1Desc) {
        n1Desc.textContent = numClusters > 1
            ? 'Resources available with one node per cluster offline'
            : 'Resources available with one node offline';
    }

    const pn = result.per_node;
    const perNodeTable = document.getElementById('per-node-table');
    let perNodeHtml = '';
    if (result.mode === 'appliance') {
        perNodeHtml = `
            <tr><td>CPU</td><td>${pn.cpu}</td></tr>
            <tr><td>Cores</td><td>${pn.cores}</td></tr>
            <tr><td>Threads</td><td>${pn.threads}</td></tr>
            <tr><td>Clock Speed</td><td>${pn.ghz} GHz</td></tr>
            <tr><td>RAM</td><td>${pn.ram_gb} GB</td></tr>
            <tr><td>RAW Storage</td><td>${pn.raw_storage_tb} TB</td></tr>`;
        if (result.form_factor) {
            perNodeHtml += `<tr><td>Form Factor</td><td>${result.form_factor}</td></tr>`;
        }
    } else {
        perNodeHtml = `
            <tr><td>Cores</td><td>${pn.cores}</td></tr>
            <tr><td>Threads</td><td>${pn.threads}</td></tr>
            <tr><td>Clock Speed</td><td>${pn.ghz} GHz</td></tr>
            <tr><td>RAM</td><td>${pn.ram_gb} GB</td></tr>
            <tr><td>Disks</td><td>${pn.disk_count} drives</td></tr>
            <tr><td>RAW Storage</td><td>${pn.raw_storage_tb} TB</td></tr>`;
        if (result.storage_type) {
            perNodeHtml += `<tr><td>Storage Type</td><td>${result.storage_type}</td></tr>`;
        }
    }
    if (so) {
        const cpuRow = so.cpu ? `<tr><td>CPU</td><td>${so.cpu}</td></tr>` : '';
        perNodeHtml += `
            <tr class="so-divider"><td colspan="2">Storage-Only Node &times; ${so.count} (no VMs)</td></tr>
            ${cpuRow}
            <tr><td>Cores</td><td>${so.cores}</td></tr>
            <tr><td>Threads</td><td>${so.threads}</td></tr>
            <tr><td>RAM</td><td>${so.ram_gb} GB</td></tr>
            <tr><td>RAW Storage</td><td>${so.raw_storage_tb} TB</td></tr>`;
    }
    perNodeTable.innerHTML = perNodeHtml;

    const cl = result.cluster_total;
    document.getElementById('cluster-table').innerHTML = `
        <tr><td>Total Cores</td><td>${cl.cores}</td></tr>
        <tr><td>Total Threads</td><td>${cl.threads}</td></tr>
        <tr><td>Total GHz</td><td>${cl.total_ghz} GHz</td></tr>
        <tr><td>Total RAM</td><td>${formatRam(cl.ram_gb)}</td></tr>
        <tr><td>Total RAW Storage</td><td>${cl.raw_storage_tb} TB</td></tr>
        <tr><td>Usable Storage</td><td class="usable">${cl.usable_storage_tb} TB</td></tr>`;

    const n1 = result.n_minus_1;
    const n1Card = document.querySelector('.result-card.n1');
    const n1Desc = document.getElementById('n1-desc');
    if (result.single_node) {
        // A single-node system has no peer to fail over to, so N-1 is meaningless.
        // Grey the card out and replace the figures with the no-redundancy notice.
        n1Card.classList.add('no-redundancy');
        n1Desc.textContent = '';
        document.getElementById('n1-table').innerHTML = `
            <tr><td class="no-redundancy-msg" colspan="2">
                <strong>No redundancy.</strong> ${result.redundancy_note
                    ? result.redundancy_note.replace(/^No redundancy[^a-zA-Z]*/, '')
                    : 'Ensure workloads are protected with replication or a properly configured backup.'}
            </td></tr>`;
    } else {
        n1Card.classList.remove('no-redundancy');
        n1Desc.textContent = 'Resources available with one node offline';
        document.getElementById('n1-table').innerHTML = `
            <tr><td>Available Cores</td><td>${n1.cores}</td></tr>
            <tr><td>Available Threads</td><td>${n1.threads}</td></tr>
            <tr><td>Available GHz</td><td>${n1.total_ghz} GHz</td></tr>
            <tr><td>Available RAM</td><td>${formatRam(n1.ram_gb)}</td></tr>
            <tr><td>Usable Storage</td><td class="usable">${n1.usable_storage_tb} TB</td></tr>`;
    }

    section.scrollIntoView({behavior: 'smooth'});
}

function formatRam(gb) {
    if (gb >= 1024) return `${(gb / 1024).toFixed(1)} TB`;
    return `${gb} GB`;
}

function updateP95Display() {
    const input = document.getElementById('p95-iops');
    if (!input) return;
    const val = parseInt(input.value) || 0;
    if (importSummary) {
        importSummary.p95_iops = val;
        // Refresh recommendations so the IOPS demand/headroom reflects the new P95.
        recalcRecommendations();
    }
}

// ==================== LIVE OPTICS IMPORT ====================

let importSummary = null;

document.addEventListener('DOMContentLoaded', () => {
    const area = document.getElementById('upload-area');
    if (area) {
        area.addEventListener('click', () => document.getElementById('file-input').click());
        // Linux/GTK browsers (Firefox, Brave) require dragenter to be prevented too,
        // not just dragover, before they will accept a drop.
        const allow = e => { e.preventDefault(); e.stopPropagation(); area.classList.add('drag-over'); };
        area.addEventListener('dragenter', allow);
        area.addEventListener('dragover', allow);
        area.addEventListener('dragleave', () => area.classList.remove('drag-over'));
        area.addEventListener('drop', e => {
            e.preventDefault();
            e.stopPropagation();
            area.classList.remove('drag-over');
            const file = extractDroppedFile(e.dataTransfer);
            if (file) uploadFile(file);
        });
    }

    // Stop the browser from opening a file dropped just outside the upload area
    // (a common Linux miss that otherwise navigates away from the page).
    ['dragover', 'drop'].forEach(evt =>
        window.addEventListener(evt, e => e.preventDefault()));
});

// dataTransfer.files is reliable on macOS/Windows but is sometimes empty on Linux,
// where the dropped file arrives via dataTransfer.items instead.
function extractDroppedFile(dt) {
    if (dt.files && dt.files.length > 0) return dt.files[0];
    if (dt.items) {
        for (const item of dt.items) {
            if (item.kind === 'file') {
                const file = item.getAsFile();
                if (file) return file;
            }
        }
    }
    return null;
}

function handleFileSelect(input) {
    if (input.files.length > 0) uploadFile(input.files[0]);
}

async function uploadFile(file) {
    if (!file.name.endsWith('.xlsx')) {
        showUploadStatus('File must be an .xlsx Excel file', true);
        return;
    }

    showUploadStatus('Analyzing workload...', false);
    document.getElementById('import-results').style.display = 'none';
    document.getElementById('sizing-results').style.display = 'none';

    const formData = new FormData();
    formData.append('file', file);

    try {
        const resp = await fetch('/api/import-liveoptics', { method: 'POST', body: formData });
        const data = await resp.json();

        if (data.error) {
            showUploadStatus(data.error, true);
            return;
        }

        importSummary = data.summary;
        originalImportSummary = JSON.parse(JSON.stringify(data.summary));
        importVms = data.vms || [];
        includeLocalStorage = false;
        vmExclusions = { compute: new Set(), storage: new Set() };
        vmConfig = {};
        updateExclusionCountBadge();
        activeMode = 'import';
        document.getElementById('target-nodes').value = '';  // fresh upload starts uncapped
        document.getElementById('storage-pref').value = 'auto';
        document.getElementById('size-full-cluster').checked = false;
        document.getElementById('sizing-include-eol').checked = false;
        document.getElementById('sizing-model-select').value = '';
        populateSizingModelDropdown('sizing-model-select', false);
        updateFullClusterInfo(false, null);
        const sourceLabel = data.source === 'rvtools' ? 'RVTools' : 'Live Optics';
        const scanNote = data.summary && data.summary.scan_type === 'general'
            ? ' — server-level scan: each server sized 1:1 (no overcommit/NIC data; peak metrics only)'
            : '';
        showUploadStatus(`Analyzed (${sourceLabel}): ${file.name}${scanNote}`, false);
        displayImportResults(data);
    } catch (e) {
        showUploadStatus('Upload failed: ' + e.message, true);
    }
}

function showUploadStatus(msg, isError) {
    const el = document.getElementById('upload-status');
    el.textContent = msg;
    el.className = 'upload-status ' + (isError ? 'upload-error' : 'upload-ok');
    el.style.display = 'block';
}

let ratioDebounce = null;

function updateRatioDisplay() {
    const val = parseFloat(document.getElementById('ratio-slider').value);
    document.getElementById('ratio-value').textContent = `${val.toFixed(2)} : 1`;

    const pct = ((val - 1) / 7) * 100;
    document.getElementById('ratio-bar-fill').style.width = pct + '%';

    if (ratioDebounce) clearTimeout(ratioDebounce);
    ratioDebounce = setTimeout(recalcRecommendations, 250);
}

// Single recalc path shared by both the VMware Import and Manual Input flows.
// The active workload summary is chosen by activeMode; every sizing control is
// read from the one shared block, so the two flows can't diverge.
async function recalcRecommendations() {
    const summary = activeMode === 'manual' ? manualSummary : importSummary;
    if (!summary) return;
    const ratio = parseFloat(document.getElementById('ratio-slider').value);
    const years = parseInt(document.getElementById('growth-years').value);
    const growthPct = parseFloat(document.getElementById('growth-pct').value);
    const snapshotPct = parseFloat(document.getElementById('snapshot-pct').value);
    const targetNodesRaw = document.getElementById('target-nodes').value;
    const targetNodes = targetNodesRaw ? parseInt(targetNodesRaw, 10) : null;
    const storagePref = document.getElementById('storage-pref').value;
    const sizeFullCluster = document.getElementById('size-full-cluster').checked;
    const sizingMode = document.getElementById('sizing-mode').value;
    const allowStorageOnly = document.getElementById('allow-storage-only').checked;
    const targetModel = document.getElementById('sizing-model-select').value || null;
    const includeEolEos = document.getElementById('sizing-include-eol').checked;

    try {
        const resp = await fetch('/api/recommend', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                summary: summary,
                vcpu_ratio: ratio,
                years: years,
                growth_pct: growthPct,
                snapshot_pct: snapshotPct,
                target_nodes: targetNodes,
                storage_pref: storagePref,
                size_full_cluster: sizeFullCluster,
                sizing_mode: sizingMode,
                allow_storage_only: allowStorageOnly,
                target_model: targetModel,
                include_eol_eos: includeEolEos,
            }),
        });
        const data = await resp.json();
        // Store projection first: renderRecommendationsTo reads lastProjection
        // for the IOPS demand/headroom line.
        if (data.projection) lastProjection[activeMode] = data.projection;
        if (data.recommendations) {
            lastRecommendations[activeMode] = data.recommendations;
            lastSummary[activeMode] = summary;
            renderRecommendationsTo(data.recommendations, 'rec-list', 'ratio-slider', activeMode, data.warnings);
            updateFullClusterInfo(sizeFullCluster, data.recommendations);
        }
        if (data.projection) {
            renderProjectionTo(data.projection, 'projection-summary');
        }
    } catch (e) {
        console.error('Recalc failed:', e);
    }
}

const FULL_CLUSTER_INFO_BASE =
    'By default CPU is sized for N-1, so the cluster keeps full performance even ' +
    'if a node fails. Enabling this sizes CPU across all nodes — lowering node ' +
    'count and cost, but during a node failure performance can degrade as the ' +
    'effective vCPU:core ratio rises.';

// Append the worst-case degraded ratio across the current recommendations to the
// (i) tooltip when full-cluster sizing is active.
function updateFullClusterInfo(enabled, recommendations) {
    const icon = document.getElementById('full-cluster-info');
    if (!icon) return;
    if (enabled && recommendations && recommendations.length > 0) {
        const worst = Math.max(...recommendations.map(r => r.vcpu_ratio_degraded || 0));
        setInfoTip(icon, FULL_CLUSTER_INFO_BASE +
            ` During a node failure the vCPU:core ratio rises to up to ${worst.toFixed(2)}:1.`);
    } else {
        setInfoTip(icon, FULL_CLUSTER_INFO_BASE);
    }
}

function renderProjectionTo(p, targetId) {
    document.getElementById(targetId).innerHTML = `
        <div class="proj-grid">
            <div class="proj-card">
                <div class="proj-label">Current vCPUs</div>
                <div class="proj-base">${p.base_vcpus}</div>
                <div class="proj-arrow">&#8594;</div>
                <div class="proj-label">Year ${p.years} vCPUs</div>
                <div class="proj-projected">${p.projected_vcpus}</div>
            </div>
            <div class="proj-card">
                <div class="proj-label">Current RAM</div>
                <div class="proj-base">${formatRam(p.base_ram_gb)}</div>
                <div class="proj-arrow">&#8594;</div>
                <div class="proj-label">Year ${p.years} RAM</div>
                <div class="proj-projected">${formatRam(p.projected_ram_gb)}</div>
            </div>
            <div class="proj-card">
                <div class="proj-label">Current GHz</div>
                <div class="proj-base">${p.base_ghz} GHz</div>
                <div class="proj-arrow">&#8594;</div>
                <div class="proj-label">Year ${p.years} GHz</div>
                <div class="proj-projected">${p.projected_ghz} GHz</div>
            </div>
            <div class="proj-card">
                <div class="proj-label">Current Storage</div>
                <div class="proj-base">${p.base_storage_tb} TiB</div>
                <div class="proj-arrow">&#8594;</div>
                <div class="proj-label">Year ${p.years} + Snapshots</div>
                <div class="proj-projected">${p.projected_storage_tb} TiB</div>
            </div>
        </div>
        <div class="proj-note">
            Growth: ${p.growth_factor}x over ${p.years}yr &mdash;
            Snapshot overhead at year ${p.years}: ${p.snapshot_pct_at_target}%
        </div>
        ${iopsDemandNote(p.iops_demand)}
    `;
}

// Workload IOPS demand note (the measured IOPS the workload needs). Shown when
// P95 and/or Average are known.
function iopsDemandNote(d) {
    if (!d) return '';
    const bits = [];
    if (d.p95) bits.push(`P95 ${d.p95.toLocaleString()}`);
    if (d.avg) bits.push(`Avg ${d.avg.toLocaleString()}`);
    if (!bits.length) return '';
    return `<div class="proj-note">Workload IOPS demand: ${bits.join(' &middot; ')}</div>`;
}

function displayImportResults(data) {
    const s = data.summary;
    activeMode = 'import';
    document.getElementById('import-results').style.display = 'block';
    document.getElementById('sizing-results').style.display = 'block';

    const currentRatio = s.vcpu_per_core_ratio || 3.0;
    const slider = document.getElementById('ratio-slider');
    slider.value = currentRatio;
    updateRatioDisplay();

    const markerPct = ((currentRatio - 1) / 7) * 100;
    const marker = document.getElementById('ratio-bar-marker');
    marker.style.left = Math.min(markerPct, 100) + '%';

    if (s.vcpu_ratio_assumed) {
        // Server-level scan: no overcommit was measured, so this is a default.
        marker.title = `Assumed default: ${currentRatio.toFixed(2)}:1`;
        document.getElementById('ratio-current').innerHTML =
            `Assumed default: <strong>${currentRatio.toFixed(2)} : 1</strong> vCPU per core ` +
            `(no overcommit data in this server-level scan)`;
    } else {
        marker.title = `Current environment: ${currentRatio.toFixed(2)}:1`;
        document.getElementById('ratio-current').innerHTML =
            `Current environment: <strong>${currentRatio.toFixed(2)} : 1</strong> vCPU per core ` +
            `(${s.total_vcpus} vCPUs / ${s.total_host_cores} cores)`;
    }

    document.getElementById('env-summary').innerHTML = `
        <div class="summary-card">
            <div class="summary-label">Current Platform</div>
            <div class="summary-value">${s.current_platform}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">Cluster</div>
            <div class="summary-value">${s.cluster_name}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">Hosts</div>
            <div class="summary-value">${s.host_count}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">Total VMs</div>
            <div class="summary-value">${s.total_vms} (${s.active_vms} active)</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">Current Cores</div>
            <div class="summary-value">${s.total_host_cores}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">Current Threads</div>
            <div class="summary-value">${s.total_host_threads}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">Current RAM</div>
            <div class="summary-value">${formatRam(s.total_host_ram_gb)}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">Peak CPU</div>
            <div class="summary-value">${s.peak_cpu_pct}% (avg ${s.avg_cpu_pct}%)</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">Peak Memory</div>
            <div class="summary-value">${s.peak_mem_pct}% (avg ${s.avg_mem_pct}%)</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">IOPS</div>
            <div class="summary-value">${s.total_avg_iops.toLocaleString()} avg (${s.total_peak_iops.toLocaleString()} peak)</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">P95 IOPS (from LO Dashboard)</div>
            <div class="summary-value">
                <input type="number" id="p95-iops" value="${s.p95_iops || 0}" min="0" step="1"
                       class="inline-input" placeholder="0 = unknown"
                       onchange="updateP95Display()">
            </div>
        </div>
    `;

    document.getElementById('workload-summary').innerHTML = `
        <div class="summary-card">
            <div class="summary-label">vCPUs Required</div>
            <div class="summary-value accent">${s.total_vcpus}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">Provisioned RAM</div>
            <div class="summary-value accent">${formatRam(s.total_vm_provisioned_memory_gb)}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">Used RAM</div>
            <div class="summary-value">${formatRam(s.total_vm_used_memory_gb)}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">Provisioned Storage</div>
            <div class="summary-value">${s.total_vm_provisioned_storage_tb} TiB</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">Datastore Used</div>
            <div class="summary-value accent">${s.datastore_used_tb} TiB</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">Datastore Total</div>
            <div class="summary-value">${s.datastore_total_tb} TiB</div>
        </div>
    `;

    renderLocalStorageOption(s);

    lastRecommendations['import'] = data.recommendations;
    lastSummary['import'] = data.summary;
    lastProjection['import'] = data.projection;
    renderRecommendationsTo(data.recommendations, 'rec-list', 'ratio-slider', 'import', data.warnings);
    if (data.projection) renderProjectionTo(data.projection, 'projection-summary');
    document.getElementById('import-results').scrollIntoView({behavior: 'smooth'});
}

// "Determined by" line: which resource drove this config's node count, with the
// required-vs-achieved figures.
function formatDeterminant(det) {
    if (!det) return '';
    if (det.resource === 'minimum') {
        return '<div class="rec-determinant"><strong>Determined by</strong> minimum cluster size</div>';
    }
    const u = det.unit;
    const fmt = v => u === 'GB' ? formatRam(v)
        : (u === 'cores' ? `${Math.round(v).toLocaleString()} cores` : `${v} TB`);
    return `<div class="rec-determinant"><strong>Determined by ${det.resource}</strong>`
        + ` &mdash; ${fmt(det.required)} required vs ${fmt(det.achieved)} available`
        + ` (${det.headroom_pct}% headroom)</div>`;
}

function renderRecommendationsTo(recommendations, listId, sliderId, mode, warnings) {
    const recList = document.getElementById(listId);
    if (!recommendations || recommendations.length === 0) {
        // A warning here (e.g. an infeasible target node count) explains the
        // empty result better than the generic capacity message.
        if (warnings && warnings.length > 0) {
            recList.innerHTML = '<div class="rec-warnings">' +
                warnings.map(w => `<div class="rec-warning">${w}</div>`).join('') +
                '</div>';
        } else {
            recList.innerHTML = '<div class="no-recs">No matching configurations found. The workload may exceed available appliance capacities. Consider Software Only (Validated) mode.</div>';
        }
        return;
    }

    const targetRatio = parseFloat(document.getElementById(sliderId).value);
    const demand = (lastProjection[mode] || {}).iops_demand || null;

    let warningsHtml = '';
    if (warnings && warnings.length > 0) {
        warningsHtml = '<div class="rec-warnings">' +
            warnings.map(w => `<div class="rec-warning">${w}</div>`).join('') +
            '</div>';
    }

    // Show the witness requirement once if any recommendation is a 2-node cluster
    // (storage-only nodes add quorum, so those never count as 2-node here).
    const recTotalNodes = r => r.storage_only
        ? (r.hci_node_count || r.node_count) + r.storage_only.count
        : r.node_count;
    const witnessHtml = recommendations.some(r => recTotalNodes(r) === 2) ? witnessBarHtml() : '';

    recList.innerHTML = witnessHtml + warningsHtml + recommendations.map((r, i) => {
        const clusterInfo = r.num_clusters > 1
            ? `${r.num_clusters} clusters (${r.cluster_layout.join(' + ')})`
            : '1 cluster';
        const n1Label = r.num_clusters > 1
            ? `N-1 per Cluster (${r.num_clusters} spares)`
            : 'N-1 Available';
        const modelLabel = r.validated_only
            ? r.model
            : (r.validated ? `Validated &ndash; based off ${r.model}` : r.model);
        const ratioBadge = r.sized_full_cluster
            ? `<span class="rec-ratio-badge degraded" title="Normal vCPU:core ratio (full cluster). Rises to ${r.vcpu_ratio_degraded.toFixed(2)}:1 during a node failure.">${r.vcpu_ratio.toFixed(2)}:1 &rarr; ${r.vcpu_ratio_degraded.toFixed(2)}:1</span>`
            : `<span class="rec-ratio-badge" title="Actual vCPU:core ratio at N-1">${r.vcpu_ratio.toFixed(2)}:1</span>`;
        const iops = r.iops || null;
        const iopsRow = (val) => iops ? `<tr><td>Net IOPS</td><td>${Math.round(val).toLocaleString()}</td></tr>` : '';
        const iopsHeadroom = buildIopsHeadroom(iops, demand);
        const so = r.storage_only || null;
        const nodesLabel = so
            ? `${r.hci_node_count} HCI + ${so.count} storage-only`
            : `${r.node_count} nodes`;
        const soRows = so ? `
                        <tr class="so-divider"><td colspan="2">Storage-Only &times; ${so.count} (no VMs)</td></tr>
                        <tr><td>CPU</td><td>${so.cpu}</td></tr>
                        <tr><td>RAM</td><td>${formatRam(so.ram_gb)}</td></tr>
                        <tr><td>Storage</td><td>${r.storage_config.desc}</td></tr>` : '';
        return `
        <div class="rec-card ${i === 0 ? 'rec-best' : ''}">
            <div class="rec-header">
                <span class="rec-rank">#${i + 1}</span>
                <span class="rec-model">${modelLabel}</span>
                <span class="rec-category">${r.category}</span>
                ${ratioBadge}
                <span class="rec-nodes">${nodesLabel}</span>
                <span class="rec-clusters" title="${clusterInfo}">${clusterInfo}</span>
            </div>
            ${formatDeterminant(r.determinant)}
            <div class="rec-details">
                <div class="rec-col">
                    <h4>Per Node</h4>
                    <table>
                        <tr><td>CPU</td><td>${r.cpu}</td></tr>
                        <tr><td>Cores</td><td>${r.cores_per_node}</td></tr>
                        <tr><td>Threads</td><td>${r.threads_per_node}</td></tr>
                        <tr><td>RAM</td><td>${formatRam(r.ram_per_node_gb)}</td></tr>
                        <tr><td>Storage</td><td>${r.storage_config.desc}</td></tr>
                        ${iops ? iopsRow(iops.per_node) : ''}
                        ${soRows}
                    </table>
                </div>
                <div class="rec-col">
                    <h4>Total (all clusters)</h4>
                    <table>
                        <tr><td>Cores</td><td>${r.totals.cores}</td></tr>
                        <tr><td>Threads</td><td>${r.totals.threads}</td></tr>
                        <tr><td>GHz</td><td>${r.totals.total_ghz}</td></tr>
                        <tr><td>RAM</td><td>${formatRam(r.totals.ram_gb)}</td></tr>
                        <tr><td>Usable Storage</td><td class="usable">${r.totals.usable_storage_tb} TB</td></tr>
                        ${iops ? iopsRow(iops.total) : ''}
                    </table>
                </div>
                <div class="rec-col">
                    <h4>${n1Label}</h4>
                    <table>
                        <tr><td>Cores</td><td>${r.n_minus_1.cores}</td></tr>
                        <tr><td>Threads</td><td>${r.n_minus_1.threads}</td></tr>
                        <tr><td>GHz</td><td>${r.n_minus_1.total_ghz}</td></tr>
                        <tr><td>RAM</td><td>${formatRam(r.n_minus_1.ram_gb)}</td></tr>
                        <tr><td>Usable Storage</td><td class="usable">${r.n_minus_1.usable_storage_tb} TB</td></tr>
                        ${iops ? iopsRow(iops.n_minus_1) : ''}
                    </table>
                </div>
            </div>
            ${iopsHeadroom}
            <div class="rec-footer">
                <span>${r.form_factor} &mdash; ${r.chassis}</span>
                <button class="btn btn-export" onclick="exportProposal('${mode}', ${i})" title="Export PowerPoint proposal"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>Export PPTX</button>
            </div>
        </div>
    `}).join('');
}

// Informational line comparing the config's net available IOPS against the
// workload's measured demand at P95/Avg. Returns '' when there is no IOPS data
// or no measured demand.
function buildIopsHeadroom(iops, demand) {
    if (!iops || !demand) return '';
    const parts = [];
    const fmtMetric = (label, value) => {
        if (!value || value <= 0) return;
        const ratio = iops.total / value;
        const ok = ratio >= 1;
        parts.push(
            `<span class="${ok ? 'iops-ok' : 'iops-short'}" title="${label} demand ${value.toLocaleString()} IOPS; net available ${iops.total.toLocaleString()}">` +
            `${label}: ${ratio.toFixed(1)}&times; ${ok ? '&#10003;' : '&#9888;'}</span>`
        );
    };
    fmtMetric('P95', demand.p95);
    fmtMetric('Avg', demand.avg);
    if (!parts.length) return '';
    return `<div class="rec-iops-headroom">Net IOPS headroom (available &divide; demand): ${parts.join(' &middot; ')}</div>`;
}

// ==================== MANUAL INPUT MODE ====================

let manualSummary = null;

function calculateManual() {
    const vcpus = parseInt(document.getElementById('man-vcpus').value) || 0;
    const provRam = parseFloat(document.getElementById('man-prov-ram').value) || 0;
    const dsUsed = parseFloat(document.getElementById('man-ds-used').value) || 0;

    if (vcpus < 1 || provRam < 1 || dsUsed <= 0) {
        alert('Please fill in the required fields: Total vCPUs, Provisioned RAM, and Datastore Used.');
        return;
    }

    const cores = parseInt(document.getElementById('man-cores').value) || 0;
    const currentRatio = cores > 0 ? vcpus / cores : 3.0;

    manualSummary = {
        host_count: parseInt(document.getElementById('man-hosts').value) || 0,
        cluster_name: document.getElementById('man-cluster').value || '',
        current_platform: document.getElementById('man-platform').value || '',
        total_host_cores: cores,
        total_host_threads: parseInt(document.getElementById('man-threads').value) || 0,
        total_host_ghz: parseFloat(document.getElementById('man-ghz').value) || 0,
        total_host_ram_gb: parseFloat(document.getElementById('man-host-ram').value) || 0,
        total_vms: parseInt(document.getElementById('man-total-vms').value) || 0,
        active_vms: parseInt(document.getElementById('man-active-vms').value) || 0,
        total_vcpus: vcpus,
        total_vm_provisioned_memory_gb: provRam,
        total_vm_used_memory_gb: parseFloat(document.getElementById('man-used-ram').value) || 0,
        total_vm_provisioned_storage_tb: parseFloat(document.getElementById('man-prov-storage').value) || 0,
        datastore_used_tb: dsUsed,
        datastore_total_tb: parseFloat(document.getElementById('man-ds-total').value) || 0,
        peak_cpu_pct: parseFloat(document.getElementById('man-peak-cpu').value) || 0,
        avg_cpu_pct: parseFloat(document.getElementById('man-avg-cpu').value) || 0,
        peak_mem_pct: parseFloat(document.getElementById('man-peak-mem').value) || 0,
        avg_mem_pct: parseFloat(document.getElementById('man-avg-mem').value) || 0,
        total_peak_iops: parseInt(document.getElementById('man-peak-iops').value) || 0,
        total_avg_iops: parseInt(document.getElementById('man-avg-iops').value) || 0,
        p95_iops: parseInt(document.getElementById('man-p95-iops').value) || 0,
        nic_speed_mbps: parseInt(document.getElementById('man-nic-speed').value) || 10000,
        vcpu_per_core_ratio: Math.round(currentRatio * 100) / 100,
    };

    // Drive the SHARED sizing block (same controls/markup the import flow uses).
    activeMode = 'manual';
    const slider = document.getElementById('ratio-slider');
    slider.value = currentRatio;
    updateRatioDisplay();

    const marker = document.getElementById('ratio-bar-marker');
    if (cores > 0) {
        const markerPct = ((currentRatio - 1) / 7) * 100;
        marker.style.left = Math.min(markerPct, 100) + '%';
        marker.style.display = 'block';
        marker.title = `Current environment: ${currentRatio.toFixed(2)}:1`;
        document.getElementById('ratio-current').innerHTML =
            `Current environment: <strong>${currentRatio.toFixed(2)} : 1</strong> vCPU per core ` +
            `(${vcpus} vCPUs / ${cores} cores)`;
    } else {
        marker.style.display = 'none';
        document.getElementById('ratio-current').innerHTML =
            `No current core count provided &mdash; using slider value`;
    }

    const sizing = document.getElementById('sizing-results');
    sizing.style.display = 'block';
    recalcRecommendations();
    sizing.scrollIntoView({behavior: 'smooth'});
}

async function exportProposal(mode, recIndex) {
    const recs = lastRecommendations[mode];
    const summary = lastSummary[mode];
    const projection = lastProjection[mode];

    if (!recs || !recs[recIndex] || !summary || !projection) {
        alert('Missing data for export. Please recalculate first.');
        return;
    }

    const btn = event.target;
    const origText = btn.textContent;
    btn.textContent = 'Generating...';
    btn.disabled = true;

    try {
        const resp = await fetch('/api/export-proposal', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                summary: summary,
                recommendation: recs[recIndex],
                projection: projection,
            }),
        });

        if (!resp.ok) {
            const err = await resp.json();
            alert(err.error || 'Export failed');
            return;
        }

        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = resp.headers.get('content-disposition')?.match(/filename="?(.+?)"?$/)?.[1]
            || `SC_Proposal_${recs[recIndex].model}_${recs[recIndex].node_count}N.pptx`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    } catch (e) {
        alert('Export failed: ' + e.message);
    } finally {
        btn.textContent = origText;
        btn.disabled = false;
    }
}

async function exportConfig() {
    if (!lastConfigResult) {
        alert('No configuration to export. Please calculate first.');
        return;
    }

    const btn = document.getElementById('config-export-btn');
    const origText = btn.textContent;
    btn.textContent = 'Generating...';
    btn.disabled = true;

    try {
        const resp = await fetch('/api/export-config', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(lastConfigResult),
        });

        if (!resp.ok) {
            const err = await resp.json();
            alert(err.error || 'Export failed');
            return;
        }

        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = resp.headers.get('content-disposition')?.match(/filename="?(.+?)"?$/)?.[1]
            || `SC_Config_${lastConfigResult.node_count}N.pptx`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    } catch (e) {
        alert('Export failed: ' + e.message);
    } finally {
        btn.textContent = origText;
        btn.disabled = false;
    }
}

// ==================== VM EXCLUSION MODAL ====================

const CVM_PATTERNS = [
    /^stctlvm/i, /^scvm/i, /^cvm/i, /^ntnx/i, /^nutanix/i,
    /^svt-/i, /^omnistack/i, /simplivity/i, /^vsa-/i, /^vsa\d/i,
    /^hpe\s*simplivity/i, /witness/i,
];

function openVmExclusionModal() {
    if (!importVms.length) return;
    renderVmTable();
    updateVmExclusionSummary();
    document.getElementById('vm-exclusion-modal').style.display = 'flex';
    document.getElementById('vm-search').value = '';
    document.getElementById('vm-search').focus();
}

function closeVmExclusionModal() {
    document.getElementById('vm-exclusion-modal').style.display = 'none';
}

// Effective (possibly user-edited) value for a VM field, falling back to the
// pristine imported value when there is no override.
function vmVal(vm, idx, field) {
    const ov = vmConfig[idx];
    return (ov && ov[field] !== undefined) ? ov[field] : vm[field];
}

function renderVmTable() {
    const sorted = importVms.map((vm, i) => ({ ...vm, _idx: i }));
    sorted.sort((a, b) => {
        // Sort on the effective (possibly edited) values so order matches the
        // displayed Model / Cores / RAM.
        let va = vmVal(a, a._idx, vmSortField), vb = vmVal(b, b._idx, vmSortField);
        if (vmSortField === 'model') { va = (va || '').toLowerCase(); vb = (vb || '').toLowerCase(); }
        else if (typeof va === 'string') { va = va.toLowerCase(); vb = (vb || '').toLowerCase(); }
        else if (typeof va === 'boolean') { va = va ? 1 : 0; vb = vb ? 1 : 0; }
        if (va < vb) return vmSortAsc ? -1 : 1;
        if (va > vb) return vmSortAsc ? 1 : -1;
        return 0;
    });

    const tbody = document.getElementById('vm-table-body');
    tbody.innerHTML = sorted.map(vm => {
        const compChecked = vmExclusions.compute.has(vm._idx) ? 'checked' : '';
        const storChecked = vmExclusions.storage.has(vm._idx) ? 'checked' : '';
        const excluded = vmExclusions.compute.has(vm._idx) || vmExclusions.storage.has(vm._idx);
        const powerClass = vm.powered_on ? 'vm-power-on' : 'vm-power-off';
        const powerLabel = vm.powered_on ? 'On' : 'Off';
        const edited = vmConfig[vm._idx] && Object.keys(vmConfig[vm._idx]).length > 0;
        const rowClass = [excluded ? 'vm-excluded' : '', edited ? 'vm-edited' : ''].filter(Boolean).join(' ');
        const model = vmVal(vm, vm._idx, 'model') || '';
        const cores = vmVal(vm, vm._idx, 'vcpus');
        const ram = vmVal(vm, vm._idx, 'provisioned_memory_gb');
        return `<tr class="${rowClass}" data-idx="${vm._idx}">
            <td class="vm-col-check"><input type="checkbox" ${compChecked} onchange="toggleVmExclusion(${vm._idx},'compute',this.checked)"></td>
            <td class="vm-col-check"><input type="checkbox" ${storChecked} onchange="toggleVmExclusion(${vm._idx},'storage',this.checked)"></td>
            <td class="vm-col-name" title="${vm.name}">${vm.name}</td>
            <td class="vm-col-model"><input type="text" class="vm-edit vm-edit-text" value="${model.replace(/"/g, '&quot;')}" onchange="setVmConfig(${vm._idx},'model',this.value)"></td>
            <td class="vm-col-power"><span class="${powerClass}">${powerLabel}</span></td>
            <td class="vm-col-num"><input type="number" class="vm-edit vm-edit-num" min="1" step="1" value="${cores}" onchange="setVmConfig(${vm._idx},'vcpus',this.value)"></td>
            <td class="vm-col-num"><input type="number" class="vm-edit vm-edit-num" min="0" step="0.1" value="${ram}" onchange="setVmConfig(${vm._idx},'provisioned_memory_gb',this.value)"></td>
            <td class="vm-col-num">${vm.vdisk_used_gb.toFixed(1)}</td>
            <td class="vm-col-os" title="${vm.os || ''}">${vm.os || ''}</td>
        </tr>`;
    }).join('');

    document.querySelectorAll('.vm-table th.sortable').forEach(th => {
        const arrows = th.querySelector('.sort-arrow');
        if (arrows) arrows.remove();
    });
    const headers = document.querySelectorAll('.vm-table th.sortable');
    headers.forEach(th => {
        const field = th.getAttribute('onclick').match(/'(.+?)'/)?.[1];
        if (field === vmSortField) {
            const arrow = document.createElement('span');
            arrow.className = 'sort-arrow';
            arrow.textContent = vmSortAsc ? ' ▲' : ' ▼';
            th.appendChild(arrow);
        }
    });
}

function sortVmTable(field) {
    if (vmSortField === field) {
        vmSortAsc = !vmSortAsc;
    } else {
        vmSortField = field;
        vmSortAsc = true;
    }
    renderVmTable();
    filterVmTable();
}

function filterVmTable() {
    const q = document.getElementById('vm-search').value.toLowerCase();
    document.querySelectorAll('#vm-table-body tr').forEach(row => {
        const name = row.children[2].textContent.toLowerCase();
        const model = (row.children[3].querySelector('input')?.value || '').toLowerCase();
        const os = row.children[8].textContent.toLowerCase();
        row.classList.toggle('vm-hidden', q && !name.includes(q) && !os.includes(q) && !model.includes(q));
    });
}

function toggleVmExclusion(idx, type, checked) {
    if (checked) {
        vmExclusions[type].add(idx);
    } else {
        vmExclusions[type].delete(idx);
    }
    const row = document.querySelector(`#vm-table-body tr[data-idx="${idx}"]`);
    if (row) {
        const excluded = vmExclusions.compute.has(idx) || vmExclusions.storage.has(idx);
        row.classList.toggle('vm-excluded', excluded);
    }
    updateVmExclusionSummary();
}

// Stage a per-VM edit (model / cores / RAM). Overrides that match the original
// imported value are dropped so the "edited" state stays accurate.
function setVmConfig(idx, field, value) {
    const vm = importVms[idx];
    let v;
    if (field === 'vcpus') {
        v = Math.max(1, Math.round(parseFloat(value) || 0));
    } else if (field === 'provisioned_memory_gb') {
        v = Math.max(0, Math.round((parseFloat(value) || 0) * 10) / 10);
    } else {
        v = (value || '').trim();
    }
    const orig = field === 'model' ? (vm.model || '') : vm[field];
    if (!vmConfig[idx]) vmConfig[idx] = {};
    if (v === orig) {
        delete vmConfig[idx][field];
        if (Object.keys(vmConfig[idx]).length === 0) delete vmConfig[idx];
    } else {
        vmConfig[idx][field] = v;
    }
    const row = document.querySelector(`#vm-table-body tr[data-idx="${idx}"]`);
    if (row) row.classList.toggle('vm-edited', !!vmConfig[idx]);
    updateVmExclusionSummary();
}

function resetVmConfig() {
    vmConfig = {};
    renderVmTable();
    filterVmTable();
    updateVmExclusionSummary();
}

function selectPoweredOffVms() {
    const includeStorage = document.getElementById('excl-include-storage')?.checked;
    importVms.forEach((vm, i) => {
        if (!vm.powered_on) {
            vmExclusions.compute.add(i);
            if (includeStorage) vmExclusions.storage.add(i);
        }
    });
    renderVmTable();
    filterVmTable();
    updateVmExclusionSummary();
}

function selectLikelyCVMs() {
    importVms.forEach((vm, i) => {
        if (CVM_PATTERNS.some(rx => rx.test(vm.name))) {
            vmExclusions.compute.add(i);
            vmExclusions.storage.add(i);
        }
    });
    renderVmTable();
    filterVmTable();
    updateVmExclusionSummary();
}

function clearAllVmExclusions() {
    vmExclusions.compute.clear();
    vmExclusions.storage.clear();
    renderVmTable();
    filterVmTable();
    updateVmExclusionSummary();
}

function updateVmExclusionSummary() {
    let compVcpus = 0, compRam = 0, compActive = 0;
    vmExclusions.compute.forEach(i => {
        const vm = importVms[i];
        if (vm.powered_on && !vm.is_template) {
            compVcpus += vm.vcpus;
            compRam += vm.provisioned_memory_gb;
            compActive++;
        }
    });

    let storGb = 0;
    vmExclusions.storage.forEach(i => {
        storGb += importVms[i].vdisk_used_gb;
    });

    const editedCount = Object.keys(vmConfig).length;

    const el = document.getElementById('vm-exclusion-summary');
    const parts = [];
    if (vmExclusions.compute.size > 0) {
        let label = `Compute: ${vmExclusions.compute.size} excluded`;
        if (compActive > 0) label += ` (-${compVcpus} cores, -${compRam.toFixed(1)} GB RAM)`;
        if (compActive < vmExclusions.compute.size) label += ` (${vmExclusions.compute.size - compActive} already off)`;
        parts.push(label);
    }
    if (vmExclusions.storage.size > 0) {
        parts.push(`Storage: ${vmExclusions.storage.size} excluded (-${(storGb / 1024).toFixed(2)} TB)`);
    }
    if (editedCount > 0) {
        parts.push(`${editedCount} VM(s) edited`);
    }
    if (parts.length === 0) {
        el.textContent = 'No changes';
    } else {
        el.innerHTML = parts.join(' &nbsp;|&nbsp; ');
    }
}

function updateExclusionCountBadge() {
    const el = document.getElementById('vm-exclusion-count');
    const total = new Set([
        ...vmExclusions.compute, ...vmExclusions.storage,
        ...Object.keys(vmConfig).map(Number),
    ]).size;
    el.innerHTML = total > 0 ? `<span class="exclusion-active">${total}</span>` : '';
}

// Rebuild the working summary from the pristine import: apply VM exclusions,
// then add per-host local storage when opted in. Used by both the Exclude-VMs
// flow and the include-local toggle so the two compose.
function computeAdjustedImportSummary() {
    const adjusted = JSON.parse(JSON.stringify(originalImportSummary));

    // Recompute compute/RAM totals straight from the (possibly user-edited) VM
    // list rather than subtracting from the original, so per-VM Cores/RAM edits
    // flow through. With no edits or exclusions this reproduces the import totals.
    let sumVcpus = 0, sumProvRam = 0, sumUsedRam = 0, activeIncluded = 0;
    let maxVmRam = 0, maxVmCores = 0;
    importVms.forEach((vm, i) => {
        if (!vm.powered_on || vm.is_template || vmExclusions.compute.has(i)) return;
        const cores = vmVal(vm, i, 'vcpus');
        const ram = vmVal(vm, i, 'provisioned_memory_gb');
        sumVcpus += cores;
        sumProvRam += ram;
        sumUsedRam += vm.consumed_memory_gb;
        activeIncluded++;
        if (ram > maxVmRam) maxVmRam = ram;
        if (cores > maxVmCores) maxVmCores = cores;
    });

    adjusted.total_vcpus = sumVcpus;
    adjusted.total_vm_provisioned_memory_gb = Math.round(sumProvRam * 10) / 10;
    adjusted.total_vm_used_memory_gb = Math.round(sumUsedRam * 10) / 10;
    adjusted.active_vms = activeIncluded;

    // Server-level scans have no real overcommit (hosts == VMs), so keep the
    // assumed 3:1 default instead of recomputing a meaningless 1:1.
    if (!adjusted.vcpu_ratio_assumed && adjusted.total_host_cores > 0) {
        adjusted.vcpu_per_core_ratio = Math.round((adjusted.total_vcpus / adjusted.total_host_cores) * 100) / 100;
    }

    adjusted.max_vm_ram_gb = maxVmRam;
    adjusted.max_vm_cores = maxVmCores;

    let exclStorGbAll = 0, exclProvStorGbActive = 0, exclStorGbActive = 0;
    vmExclusions.storage.forEach(i => {
        const vm = importVms[i];
        exclStorGbAll += vm.vdisk_used_gb;
        if (vm.powered_on && !vm.is_template) {
            exclStorGbActive += vm.vdisk_used_gb;
            exclProvStorGbActive += vm.vdisk_size_gb;
        }
    });

    adjusted.datastore_used_tb = Math.round((originalImportSummary.datastore_used_tb - exclStorGbAll / 1024) * 100) / 100;
    adjusted.total_vm_provisioned_storage_gb = Math.round((originalImportSummary.total_vm_provisioned_storage_gb - exclProvStorGbActive) * 10) / 10;
    adjusted.total_vm_provisioned_storage_tb = Math.round(adjusted.total_vm_provisioned_storage_gb / 1024 * 100) / 100;
    adjusted.total_vm_used_storage_gb = Math.round(((originalImportSummary.total_vm_used_storage_gb || 0) - exclStorGbActive) * 10) / 10;
    adjusted.total_vm_used_storage_tb = Math.round(adjusted.total_vm_used_storage_gb / 1024 * 100) / 100;

    if (adjusted.datastore_used_tb < 0) adjusted.datastore_used_tb = 0;

    // Per-host local storage (ISOs/templates etc.) — added to the cluster basis
    // only when the user opts in via the checkbox.
    if (includeLocalStorage) {
        adjusted.datastore_used_tb = Math.round((adjusted.datastore_used_tb + (originalImportSummary.local_used_tb || 0)) * 100) / 100;
        adjusted.datastore_total_tb = Math.round(((adjusted.datastore_total_tb || 0) + (originalImportSummary.local_total_tb || 0)) * 100) / 100;
    }

    return adjusted;
}

function applyVmExclusions() {
    if (!originalImportSummary) return;
    importSummary = computeAdjustedImportSummary();
    updateExclusionCountBadge();
    displayImportResults({ summary: importSummary, recommendations: [], projection: lastProjection['import'] });
    recalcRecommendations();
    closeVmExclusionModal();
}

// Off-by-default toggle to fold per-host local storage into the sizing basis.
function toggleLocalStorage() {
    const cb = document.getElementById('include-local-cb');
    includeLocalStorage = cb ? cb.checked : false;
    if (!originalImportSummary) return;
    importSummary = computeAdjustedImportSummary();
    displayImportResults({ summary: importSummary, recommendations: [], projection: lastProjection['import'] });
    recalcRecommendations();
}

// Render the "include local storage" checkbox (hidden when there is none, e.g.
// RVTools imports). Reflects current toggle state across re-renders.
function renderLocalStorageOption(s) {
    const el = document.getElementById('local-storage-option');
    if (!el) return;
    const localGb = s.local_used_gb || 0;
    if (localGb <= 0) { el.innerHTML = ''; return; }
    el.innerHTML = `
        <label class="checkbox-inline local-storage-toggle">
            <input type="checkbox" id="include-local-cb" ${includeLocalStorage ? 'checked' : ''}
                   onchange="toggleLocalStorage()">
            Include ${localGb.toLocaleString()} GB found on local storage
        </label>`;
}


// ==================== SAVE / RESTORE FULL WORKING STATE ====================
// Captures the entire sizing screen so a signed-in user can reload it later.
// Exposed on window for auth.js (a separate script) to drive.

const SNAPSHOT_VERSION = 1;

// Controls in the shared Sizing Options + Growth block — captured for BOTH the
// import and manual flows (single source of truth, so they stay in lock-step).
const _SHARED_SIZING_FIELDS = ['ratio-slider', 'growth-years', 'growth-pct',
    'snapshot-pct', 'target-nodes', 'storage-pref', 'sizing-mode',
    'size-full-cluster', 'allow-storage-only', 'sizing-model-select',
    'sizing-include-eol'];

const SNAP_FIELDS = {
    appliance: ['status-filter', 'model-select', 'node-count', 'cpu-select',
        'ram-select', 'nic-select', 'stor-hdd', 'stor-ssd', 'stor-nvme',
        'stor-cloud', 'so-enable', 'so-count', 'so-cpu-select', 'so-ram-select'],
    validated: ['val-node-count', 'val-cores', 'val-threads', 'val-ghz', 'val-ram',
        'val-nic', 'st-type', 'st-size', 'st-qty', 'dt-cap-type', 'dt-cap-size',
        'dt-cap-qty', 'dt-fast-type', 'dt-fast-size', 'dt-fast-qty', 'val-so-enable',
        'val-so-count', 'val-so-cores', 'val-so-threads', 'val-so-ghz', 'val-so-ram'],
    import: [..._SHARED_SIZING_FIELDS, 'p95-iops'],
    manual: ['man-platform', 'man-cluster', 'man-hosts', 'man-cores', 'man-threads',
        'man-ghz', 'man-host-ram', 'man-peak-cpu', 'man-avg-cpu', 'man-peak-mem',
        'man-avg-mem', 'man-avg-iops', 'man-peak-iops', 'man-p95-iops', 'man-nic-speed',
        'man-total-vms', 'man-active-vms', 'man-vcpus', 'man-prov-ram', 'man-used-ram',
        'man-prov-storage', 'man-ds-used', 'man-ds-total', ..._SHARED_SIZING_FIELDS],
};

function _snapById(id) { return document.getElementById(id); }

function _readField(id) {
    const el = _snapById(id);
    if (!el) return undefined;
    return el.type === 'checkbox' ? el.checked : el.value;
}

function _writeField(id, v) {
    const el = _snapById(id);
    if (!el || v === undefined) return;
    if (el.type === 'checkbox') el.checked = v;
    else el.value = v;
}

function _captureFields(mode) {
    const out = {};
    (SNAP_FIELDS[mode] || []).forEach(id => {
        const v = _readField(id);
        if (v !== undefined) out[id] = v;
    });
    return out;
}

// Build a complete, restorable snapshot of the current screen.
function captureSizingState() {
    const snap = { version: SNAPSHOT_VERSION, mode: currentMode, fields: _captureFields(currentMode) };

    if (currentMode === 'validated') {
        const tier = document.querySelector('input[name="disk-tier-mode"]:checked');
        snap.tierMode = tier ? tier.value : 'single';
    }

    if (currentMode === 'import') {
        if (!originalImportSummary) return null;  // nothing imported yet
        snap.import = {
            originalImportSummary,
            importSummary,
            importVms,
            vmConfig,
            exclCompute: [...vmExclusions.compute],
            exclStorage: [...vmExclusions.storage],
            includeLocalStorage,
            lastProjection: lastProjection['import'] || null,
        };
    }

    if (currentMode === 'manual') {
        if (!manualSummary) return null;
        snap.manual = { manualSummary };
    }

    return snap;
}

// Does the current screen hold something worth saving?
function hasSizingToSave() {
    if (currentMode === 'import') return !!originalImportSummary;
    if (currentMode === 'manual') return !!manualSummary;
    return !!lastConfigResult;  // appliance / validated
}

async function restoreSizingState(snap) {
    if (!snap || !snap.mode) return;
    switchMode(snap.mode);
    const f = snap.fields || {};

    if (snap.mode === 'appliance') {
        _writeField('status-filter', f['status-filter']);
        await loadModels();
        _writeField('model-select', f['model-select']);
        if (f['model-select']) {
            loadModelDetails();  // builds storage + storage-only sections, calls calculate()
            ['cpu-select', 'ram-select', 'nic-select', 'node-count', 'stor-hdd',
             'stor-ssd', 'stor-nvme', 'stor-cloud', 'so-enable', 'so-count',
             'so-cpu-select', 'so-ram-select'].forEach(id => _writeField(id, f[id]));
            const soCfg = _snapById('so-config');
            if (soCfg) soCfg.style.display = _snapById('so-enable').checked ? 'flex' : 'none';
            calculate();
        }
        return;
    }

    if (snap.mode === 'validated') {
        (SNAP_FIELDS.validated).forEach(id => _writeField(id, f[id]));
        const tier = snap.tierMode || 'single';
        const radio = document.querySelector(`input[name="disk-tier-mode"][value="${tier}"]`);
        if (radio) radio.checked = true;
        setTierMode(tier);
        // Type selects drive their size dropdowns; repopulate then re-apply sizes.
        [['st-type', 'st-size'], ['dt-cap-type', 'dt-cap-size'], ['dt-fast-type', 'dt-fast-size']]
            .forEach(([t, sz]) => {
                const tsel = _snapById(t);
                if (tsel) { populateDiskSizes(tsel); _writeField(sz, f[sz]); }
            });
        const soCfg = _snapById('val-so-config');
        if (soCfg) soCfg.style.display = _snapById('val-so-enable').checked ? 'flex' : 'none';
        calculateValidated();
        return;
    }

    if (snap.mode === 'import') {
        const im = snap.import || {};
        originalImportSummary = im.originalImportSummary;
        importVms = im.importVms || [];
        vmConfig = im.vmConfig || {};
        vmExclusions = { compute: new Set(im.exclCompute || []), storage: new Set(im.exclStorage || []) };
        includeLocalStorage = !!im.includeLocalStorage;
        lastProjection['import'] = im.lastProjection || null;
        importSummary = computeAdjustedImportSummary();
        updateExclusionCountBadge();
        showUploadStatus('Restored a saved sizing.', false);
        // Re-render the env/workload cards from the adjusted summary, then re-apply
        // the saved options and recompute recommendations.
        displayImportResults({ summary: importSummary, recommendations: [], projection: lastProjection['import'] });
        (SNAP_FIELDS.import).forEach(id => _writeField(id, f[id]));
        updateRatioDisplay();
        recalcRecommendations();
        return;
    }

    if (snap.mode === 'manual') {
        (SNAP_FIELDS.manual).forEach(id => _writeField(id, f[id]));
        calculateManual();  // rebuilds manualSummary + shows the shared block, then recalcs
        // Re-apply saved sizing controls (calculateManual reset the ratio to derived).
        _SHARED_SIZING_FIELDS.forEach(id => _writeField(id, f[id]));
        updateRatioDisplay();
        recalcRecommendations();
        return;
    }
}

window.captureSizingState = captureSizingState;
window.restoreSizingState = restoreSizingState;
window.hasSizingToSave = hasSizingToSave;

// (Re)load catalog data after sign-in, since the initial page-load fetches are
// rejected (401) while anonymous under the mandatory-login gate.
window.initSizer = function () {
    loadModels();
    loadValidatedNics();
};


// ==================== INFO-ICON TOOLTIPS ====================
// Turn every .info-icon's `title` into a styled tooltip that shows on hover,
// keyboard focus, AND click/tap — native title tooltips are slow and don't work
// on touch. The text lives in data-tip (moved off `title` so the OS tooltip
// doesn't also fire); kept in aria-label for screen readers.

function setInfoTip(el, text) {
    if (!el) return;
    el.dataset.tip = text;
    el.setAttribute('aria-label', text);
    el.removeAttribute('title');
}

let _infoTipEl = null;
let _infoTipPinned = null;

function _tipText(el) {
    if (!el.dataset.tip && el.getAttribute('title')) setInfoTip(el, el.getAttribute('title'));
    return el.dataset.tip || '';
}

function showInfoTip(el) {
    const text = _tipText(el);
    if (!text) return;
    if (!_infoTipEl) {
        _infoTipEl = document.createElement('div');
        _infoTipEl.className = 'info-tooltip';
        document.body.appendChild(_infoTipEl);
    }
    const tip = _infoTipEl;
    tip.textContent = text;
    tip.style.display = 'block';
    const r = el.getBoundingClientRect();
    const tr = tip.getBoundingClientRect();
    const margin = 8;
    let left = r.left + r.width / 2 - tr.width / 2 + window.scrollX;
    const maxLeft = window.scrollX + document.documentElement.clientWidth - tr.width - margin;
    left = Math.max(window.scrollX + margin, Math.min(left, maxLeft));
    // Below the icon, or above if it would overflow the viewport bottom.
    let top = (r.bottom + 6 + tr.height > window.innerHeight ? r.top - tr.height - 6 : r.bottom + 6)
              + window.scrollY;
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
}

function hideInfoTip() {
    if (_infoTipEl) _infoTipEl.style.display = 'none';
}

function _iconFrom(e) {
    return e.target && e.target.closest ? e.target.closest('.info-icon') : null;
}

document.addEventListener('mouseover', e => { const i = _iconFrom(e); if (i) showInfoTip(i); });
document.addEventListener('mouseout', e => { if (_iconFrom(e) && !_infoTipPinned) hideInfoTip(); });
document.addEventListener('focusin', e => { const i = _iconFrom(e); if (i) showInfoTip(i); });
document.addEventListener('focusout', e => { if (_iconFrom(e) && !_infoTipPinned) hideInfoTip(); });
document.addEventListener('click', e => {
    const icon = _iconFrom(e);
    if (icon) {
        e.preventDefault();
        if (_infoTipPinned === icon) { _infoTipPinned = null; hideInfoTip(); }
        else { _infoTipPinned = icon; showInfoTip(icon); }
    } else if (_infoTipPinned) {
        _infoTipPinned = null; hideInfoTip();
    }
});
document.addEventListener('keydown', e => { if (e.key === 'Escape') { _infoTipPinned = null; hideInfoTip(); } });
window.addEventListener('scroll', () => { _infoTipPinned = null; hideInfoTip(); }, true);

// Move static titles into data-tip up front so the native tooltip never fires.
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.info-icon[title]').forEach(el => setInfoTip(el, el.getAttribute('title')));
});
