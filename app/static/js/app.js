// HTML-escape any value before interpolating it into an innerHTML string.
// Imported file fields (cluster/platform/VM names, OS strings) and other
// user-supplied text are untrusted and must never reach innerHTML raw — they
// can be saved into a shared sizing and fire as stored XSS for whoever opens it.
function esc(v) {
    return String(v == null ? '' : v)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

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
// Source-environment perf context (from the last /api/recommend response), used
// to render a per-card CPU-performance comparison inside each recommendation.
let lastPerfSource = null;
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
// VMs the user added (net-new workload not in the import) — indices into
// importVms. Their storage is additive; compute/RAM fall out of the recompute.
let vmAdded = new Set();
// VMs the user removed from the dataset. Implemented as a full exclusion plus a
// UI marker so the row can be struck through and restored.
let vmRemoved = new Set();
let vmPowerFilter = 'all';   // table view filter: 'all' | 'on' | 'off'

// ---- Multi-site (source-cluster) state ----------------------------------
// When an import holds more than one source (vSphere) cluster, the user can
// opt to size each cluster separately. importVms stays a single whole-dataset
// list (VM state is keyed by index into it); a cluster is a filtered VIEW of
// it, and each cluster carries its own base summary, sizing options, and
// results. Everything below is inert when separateClusters is false — the
// single-cluster/combined path is unchanged.
const COMBINED_KEY = '__combined__';         // internal key: the whole dataset (non-separate path)
const SELECTED_KEY = '__selected__';         // review tab: the per-cluster export selection
const UNCLUSTERED_KEY = '(unclustered)';     // mirrors cluster_split.UNCLUSTERED
let sourceClusters = [];                     // [{name, host_count, vm_count}] from import
let clusterBase = {};                        // name -> pristine per-cluster summary
let separateClusters = false;                // the "size each cluster separately" toggle
let activeCluster = COMBINED_KEY;            // active recommendation tab (a name or COMBINED_KEY)
let clusterOptions = {};                     // name -> captured sizing-option fields
let clusterResults = {};                     // name -> {recommendations, projection, perfSource}
let clusterSelectedRec = {};                 // name -> chosen recommendation index for the combined export
// Replication topology: source cluster -> { target, computePct, storagePct, mode }.
// A cluster has at most one outbound target (star / circular / bidirectional all
// fall out of each cluster naming its own target). mode is how THIS cluster
// hosts inbound replicas: 'reserved' (held steady-state) or 'failover'.
let clusterReplication = {};
let dedicatedClusters = [];                  // names of workload-less DR target clusters
// Single/combined-workload DR cluster (shown when NOT sizing each cluster
// separately): one replication target for the whole workload.
let drCluster = { enabled: false, computePct: 100, storagePct: 100, mode: 'reserved', allowSingleNode: false };
let drTab = 'primary';                       // active tab in single-workload mode: 'primary' | 'dr'
let vmModalCluster = COMBINED_KEY;           // active tab inside the Configure-VMs modal

// The source-cluster a VM belongs to, blanks bucketed like the backend.
function vmClusterKey(vm) {
    return ((vm && vm.cluster) || '').trim() || UNCLUSTERED_KEY;
}

// Is this VM in scope for the given cluster? COMBINED_KEY (or null) = all VMs.
function vmInCluster(vm, clusterName) {
    if (!clusterName || clusterName === COMBINED_KEY) return true;
    return vmClusterKey(vm) === clusterName;
}

document.addEventListener('DOMContentLoaded', () => {
    loadModels();
    // Seed the tier defaults only after the disk-size catalog has loaded.
    loadValidatedNics().then(initDiskTiers);
    populateSizingModelDropdown('sizing-model-select', false);
    // A page switch with unsaved data reloads to a clean slate; resume on the
    // page the user was switching to.
    let pending = null;
    try { pending = sessionStorage.getItem('sizerPendingMode'); } catch (e) { /* ignore */ }
    if (pending) {
        try { sessionStorage.removeItem('sizerPendingMode'); } catch (e) { /* ignore */ }
        if (pending !== currentMode) switchMode(pending);
    }
});

// User-initiated page switch (the four mode buttons). Switching pages discards
// all unsaved data so nothing bleeds across modes — e.g. a prior import's
// source CPUs must never feed a Manual-Input sizing. We confirm first, then
// hard-reset via reload to the target page so it starts completely clean.
// Programmatic switches (config load, restore) call switchMode() directly and
// skip this.
async function requestSwitchMode(mode) {
    if (mode === currentMode) return;
    if (hasUnsavedWork()) {
        const choice = await confirmLeavePage();
        if (choice === 'cancel') return;
        if (choice === 'save') {
            // Only proceed once the save actually completes — the user may
            // cancel the name prompt, or not be signed in.
            const saved = await window.saveCurrentSizing();
            if (!saved) return;
        }
        try { sessionStorage.setItem('sizerPendingMode', mode); } catch (e) { /* private mode */ }
        location.reload();
        return;
    }
    switchMode(mode);
}

// Styled "discard / save / stay" prompt shown before a page switch that would
// lose data. Resolves to 'cancel' | 'discard' | 'save'.
let _leaveResolver = null;
function confirmLeavePage() {
    return new Promise(resolve => {
        _leaveResolver = resolve;
        document.getElementById('leave-page-modal').style.display = 'flex';
    });
}
function chooseLeave(choice) {
    document.getElementById('leave-page-modal').style.display = 'none';
    const r = _leaveResolver;
    _leaveResolver = null;
    if (r) r(choice);
}
function closeLeavePage() { chooseLeave('cancel'); }

// Is there entered or loaded work that a page switch would throw away?
function hasUnsavedWork() {
    if (window.hasSizingToSave && window.hasSizingToSave()) return true;
    if (currentMode === 'manual') {           // figures typed but not yet sized
        const v = document.getElementById('man-vcpus');
        if (v && v.value) return true;
    }
    return false;
}

function switchMode(mode) {
    currentMode = mode;
    document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
    document.querySelector(`[data-mode="${mode}"]`).classList.add('active');
    document.getElementById('appliance-form').style.display = mode === 'appliance' ? 'block' : 'none';
    document.getElementById('validated-form').style.display = mode === 'validated' ? 'block' : 'none';
    if (mode === 'validated') updateValidatedRules();
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
    select.innerHTML = `<option value="">${window.t('results.select_model_option')}</option>`;

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

// Populate a "Size For Model" dropdown (import/manual). Lists the models grouped
// by category; status shown for non-Active. EOL/EOS models appear only when
// includeEolEos is set. The candidate set tracks the current Sizing Mode so it
// matches what the engine will size (validated mode adds validated-only models
// and drops NVMe+SSD). Preserves the current selection when still valid.
async function populateSizingModelDropdown(selectId, includeEolEos) {
    const select = document.getElementById(selectId);
    if (!select) return;
    const prev = select.value;
    const status = includeEolEos ? 'all' : 'active';
    const sizing = document.getElementById('sizing-mode')?.value || 'certified';
    let models;
    try {
        const resp = await fetch(`/api/models?mode=appliance&status=${status}&sizing=${sizing}`);
        if (!resp.ok) return;
        models = await resp.json();
    } catch (e) {
        return;
    }
    const categories = {};
    for (const [name, data] of Object.entries(models)) {
        (categories[data.category] = categories[data.category] || []).push(name);
    }
    let html = `<option value="">${window.t('results.all_models_option')}</option>`;
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

// Switching Certified <-> Validated changes which models the engine considers,
// so rebuild the "Size For Model" list (dropping a now-invalid selection) before
// recalculating.
function onSizingModeChange() {
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
                <label>${window.t('results.storage.nvme_drive_size', {count: storage.drives_per_node || 1})}</label>
                <select id="stor-nvme" data-change='["calculate"]'>
                    ${storage.nvme_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>`;
    } else if (stype === 'ssd_only') {
        section.innerHTML = `
            <div class="form-group">
                <label>${window.t('results.storage.ssd_drive_size', {count: storage.drives_per_node || 4})}</label>
                <select id="stor-ssd" data-change='["calculate"]'>
                    ${storage.ssd_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>`;
    } else if (stype === 'hdd_only') {
        section.innerHTML = `
            <div class="form-group">
                <label>${window.t('results.storage.hdd_drive_size', {count: storage.drives_per_node || 4})}</label>
                <select id="stor-hdd" data-change='["calculate"]'>
                    ${storage.hdd_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>`;
    } else if (stype === 'hybrid') {
        section.innerHTML = `
            <div class="form-group">
                <label>${window.t('results.storage.hdd_size', {count: storage.hdd_count})}</label>
                <select id="stor-hdd" data-change='["calculate"]'>
                    ${storage.hdd_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>
            <div class="form-group">
                <label>${window.t('results.storage.ssd_cache_size', {count: storage.ssd_count})}</label>
                <select id="stor-ssd" data-change='["calculate"]'>
                    ${storage.ssd_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>`;
    } else if (stype === 'hybrid_nvme') {
        section.innerHTML = `
            <div class="form-group">
                <label>${window.t('results.storage.hdd_size', {count: storage.hdd_count})}</label>
                <select id="stor-hdd" data-change='["calculate"]'>
                    ${storage.hdd_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>
            <div class="form-group">
                <label>${window.t('results.storage.nvme_cache_size', {count: storage.nvme_count})}</label>
                <select id="stor-nvme" data-change='["calculate"]'>
                    ${storage.nvme_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>`;
    } else if (stype === 'nvme_and_ssd') {
        section.innerHTML = `
            <div class="form-group">
                <label>${window.t('results.storage.nvme_drive_size_plain')}</label>
                <select id="stor-nvme" data-change='["calculate"]'>
                    ${storage.nvme_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>
            <div class="form-group">
                <label>${window.t('results.storage.ssd_drive_size_plain')}</label>
                <select id="stor-ssd" data-change='["calculate"]'>
                    ${storage.ssd_options_tb.map(s => `<option value="${s}">${s} TB</option>`).join('')}
                </select>
            </div>`;
    } else if (stype === 'cloud') {
        section.innerHTML = `
            <div class="form-group">
                <label>${window.t('results.storage.storage_tier')}</label>
                <select id="stor-cloud" data-change='["calculate"]'>
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
    // Refresh the live rule indicators / button state (also covers the
    // snapshot-restore path, which calls this directly).
    updateValidatedRules();
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
            window.t('validated.disk_limit_exceeded', {
                total: totalClusterDisks, perNode: disks.length, nodes: nodeCount,
            })
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
        errors.push(window.t('validated.at_least_one_disk'));
        return {errors, warnings};
    }
    if (disks.length === 2) {
        errors.push(window.t('validated.two_disks_unsupported'));
    }

    const hasSpinning = disks.some(d => ['SAS', 'NLSAS', 'SATA', 'HDD'].includes(d.type));
    const hasFlash = disks.some(d => ['SSD', 'NVMe'].includes(d.type));

    if (hasSpinning && hasFlash) {
        const total = disks.reduce((s, d) => s + d.size_tb, 0);
        const flash = disks.filter(d => ['SSD', 'NVMe'].includes(d.type)).reduce((s, d) => s + d.size_tb, 0);
        const pct = (flash / total) * 100;
        if (pct < 7) errors.push(window.t('validated.flash_too_small', {pct: pct.toFixed(1)}));
        if (pct > 24.3) errors.push(window.t('validated.flash_too_large', {pct: pct.toFixed(1)}));
        if (pct >= 7 && pct <= 24.3) warnings.push(window.t('validated.hybrid_ok', {pct: pct.toFixed(1)}));
    }

    if (disks.length >= 3 && !hasSpinning && hasFlash) {
        warnings.push(window.t('validated.all_flash_detected'));
    }

    return {errors, warnings};
}

// Client-side mirror of the hybrid_min_hdd_per_flash tunable (default 3). The
// server is authoritative; this only drives the live rule indicators.
const VALIDATED_MIN_HDD_PER_FLASH = 3;

// Live state of the Validated Installer Rules that can be derived from the disk
// + node form. Toggles the green/red/dimmed markers and disables Calculate while
// any hard rule is violated. Hardware advisories (JBOD, internal-only, the
// hybrid definition) aren't evaluated here — they stay as static notes.
function updateValidatedRules() {
    const list = document.getElementById('validated-rules-list');
    if (!list) return;
    const disks = collectValidatedDisks();
    const nodeEl = document.getElementById('val-node-count');
    const nodeCount = nodeEl ? (parseInt(nodeEl.value, 10) || 0) : 0;

    const isSpin = t => ['SAS', 'NLSAS', 'SATA', 'HDD'].includes(t);
    const isFlash = t => ['SSD', 'NVMe'].includes(t);
    const hddN = disks.filter(d => isSpin(d.type)).length;
    const flashN = disks.filter(d => isFlash(d.type)).length;
    const isHybrid = hddN > 0 && flashN > 0;

    let flashPct = null;
    if (isHybrid) {
        const total = disks.reduce((s, d) => s + d.size_tb, 0);
        const flashCap = disks.filter(d => isFlash(d.type)).reduce((s, d) => s + d.size_tb, 0);
        flashPct = total > 0 ? (flashCap / total) * 100 : 0;
    }

    // 'met' (green ✓), 'bad' (red ✗, blocks Calculate), or 'na' (dimmed —
    // hybrid-only rules don't apply to a single-tier / non-hybrid config).
    const states = {
        disks: (disks.length === 0 || disks.length === 2) ? 'bad' : 'met',
        nodes: nodeCount >= 2 ? 'met' : 'bad',
        band: !isHybrid ? 'na' : (flashPct >= 7 && flashPct <= 24.3 ? 'met' : 'bad'),
        ratio: !isHybrid ? 'na'
            : (hddN >= VALIDATED_MIN_HDD_PER_FLASH * flashN ? 'met' : 'bad'),
    };

    let anyBad = false;
    list.querySelectorAll('[data-rule]').forEach(li => {
        const st = states[li.dataset.rule] || 'na';
        li.classList.remove('met', 'bad', 'na');
        li.classList.add(st);
        if (st === 'bad') anyBad = true;
    });

    const btn = document.getElementById('val-calculate-btn');
    if (btn) {
        btn.disabled = anyBad;
        btn.title = anyBad
            ? window.t('validated.resolve_rules_tooltip') : '';
    }
}

// Disk-size options per performance bucket, loaded live from the editable drive
// catalog by loadValidatedNics() (see /api/models?mode=validated) so admin-added
// sizes appear without a code change. Empty until that fetch resolves.
let VALIDATED_DISK_SIZES = { HDD: [], SSD: [], NVMe: [] };

function sizesForType(type) {
    if (['SAS', 'NLSAS', 'SATA', 'HDD'].includes(type)) return VALIDATED_DISK_SIZES.HDD;
    if (type === 'SSD') return VALIDATED_DISK_SIZES.SSD;
    return VALIDATED_DISK_SIZES.NVMe;
}

// Fill the size dropdown that belongs to the same tier row as this type select,
// preserving the current selection when the new media still offers that size.
function populateDiskSizes(typeSelect) {
    const sizeSelect = typeSelect.closest('.tier-row').querySelector('.disk-size');
    const sizes = sizesForType(typeSelect.value);
    const prev = sizeSelect.value;
    sizeSelect.innerHTML = sizes.map(s => `<option value="${s}">${s} TB</option>`).join('');
    if (sizes.map(String).includes(prev)) sizeSelect.value = prev;
    updateValidatedRules();
}

function toggleValidatedStorageOnly() {
    const on = document.getElementById('val-so-enable').checked;
    document.getElementById('val-so-config').style.display = on ? 'flex' : 'none';
}

function setTierMode(mode) {
    document.getElementById('single-tier-config').style.display = mode === 'single' ? 'block' : 'none';
    document.getElementById('dual-tier-config').style.display = mode === 'dual' ? 'block' : 'none';
    updateValidatedRules();
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

// Fill the validated RAM dropdown from the catalog, preserving the current pick
// (defaults to 64 GB the first time, matching the prior hardcoded default).
function populateRamOptions(sizes) {
    const select = document.getElementById('val-ram');
    if (!select || !sizes.length) return;
    const prev = select.value || '64';
    select.innerHTML = sizes.map(s => `<option value="${s}">${s} GB</option>`).join('');
    if (sizes.map(String).includes(prev)) select.value = prev;
    else if (sizes.includes(64)) select.value = '64';
}

// Loads everything the validated picker needs in one request: NICs, plus the
// disk-size and RAM option lists that now come from the editable catalogs rather
// than hardcoded arrays. Safe to call again after sign-in or a catalog change —
// existing selections are preserved.
async function loadValidatedNics() {
    const resp = await fetch('/api/models?mode=validated');
    if (!resp.ok) return;  // not signed in yet
    const data = await resp.json();

    VALIDATED_DISK_SIZES = data.disk_sizes || VALIDATED_DISK_SIZES;
    // Refresh any already-rendered size dropdowns against the fresh catalog,
    // keeping the current pick where it still exists.
    document.querySelectorAll('.tier-row .disk-type').forEach(populateDiskSizes);
    populateRamOptions(data.ram_sizes || []);

    const select = document.getElementById('val-nic');
    select.innerHTML = '';
    data.nics.forEach(nic => {
        const opt = document.createElement('option');
        opt.value = nic.desc;
        opt.textContent = `${nic.desc} (${nic.speed}, ${nic.ports} ports)`;
        select.appendChild(opt);
    });
}

// Every sizing starts the target vCPU:core slider here (a standard consolidation
// ratio), independent of the source environment's detected ratio — which is still
// shown via the marker/label. The value is the admin-tuned default, injected into
// the page by the server via <body data-vcpu-ratio>; 3.0 is the fallback.
const DEFAULT_SIZING_RATIO =
    parseFloat(document.body.dataset.vcpuRatio) || 3.0;

function witnessBarHtml() {
    return '<div class="info-bar">' +
        '<span class="info-bar-icon">i</span>' +
        `<span>${window.t('results.witness_message')}</span>` +
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
    // PDF export is open to everyone; the editable PPTX is Scale-only.
    const exportPdfBtn = document.getElementById('config-export-pdf-btn');
    if (exportPdfBtn) exportPdfBtn.style.display = 'inline-block';
    const exportBtn = document.getElementById('config-export-btn');
    if (exportBtn) exportBtn.style.display = canExportEditable() ? 'inline-block' : 'none';

    const so = result.storage_only;
    const numClusters = result.num_clusters || 1;
    const totalNodes = result.total_node_count || result.node_count;
    let nodesText = so
        ? window.t('results.nodes_hci_storage', {hci: result.node_count, so: so.count, total: result.total_node_count})
        : window.t('results.nodes_count', {count: totalNodes});
    if (numClusters > 1 && result.cluster_layout) {
        nodesText += window.t('results.nodes_clusters_suffix', {clusters: numClusters, layout: result.cluster_layout.join(' + ')});
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
            ? window.t('results.n1_desc_multi')
            : window.t('results.n1_desc_single');
    }

    const pn = result.per_node;
    const perNodeTable = document.getElementById('per-node-table');
    let perNodeHtml = '';
    if (result.mode === 'appliance') {
        perNodeHtml = `
            <tr><td>${window.t('results.row.cpu')}</td><td>${pn.cpu}</td></tr>
            <tr><td>${window.t('results.row.cores')}</td><td>${pn.cores}</td></tr>
            <tr><td>${window.t('results.row.threads')}</td><td>${pn.threads}</td></tr>
            <tr><td>${window.t('results.row.clock_speed')}</td><td>${pn.ghz} GHz</td></tr>
            <tr><td>${window.t('results.row.ram')}</td><td>${pn.ram_gb} GB</td></tr>
            <tr><td>${window.t('results.row.raw_storage')}</td><td>${pn.raw_storage_tb} TB</td></tr>`;
        if (result.form_factor) {
            perNodeHtml += `<tr><td>${window.t('results.row.form_factor')}</td><td>${result.form_factor}</td></tr>`;
        }
    } else {
        perNodeHtml = `
            <tr><td>${window.t('results.row.cores')}</td><td>${pn.cores}</td></tr>
            <tr><td>${window.t('results.row.threads')}</td><td>${pn.threads}</td></tr>
            <tr><td>${window.t('results.row.clock_speed')}</td><td>${pn.ghz} GHz</td></tr>
            <tr><td>${window.t('results.row.ram')}</td><td>${pn.ram_gb} GB</td></tr>
            <tr><td>${window.t('results.row.disks')}</td><td>${window.t('results.disks_drives', {count: pn.disk_count})}</td></tr>
            <tr><td>${window.t('results.row.raw_storage')}</td><td>${pn.raw_storage_tb} TB</td></tr>`;
        if (result.storage_type) {
            perNodeHtml += `<tr><td>${window.t('results.row.storage_type')}</td><td>${result.storage_type}</td></tr>`;
        }
    }
    if (so) {
        const cpuRow = so.cpu ? `<tr><td>${window.t('results.row.cpu')}</td><td>${so.cpu}</td></tr>` : '';
        perNodeHtml += `
            <tr class="so-divider"><td colspan="2">${window.t('results.storage_only_node_divider', {count: so.count})}</td></tr>
            ${cpuRow}
            <tr><td>${window.t('results.row.cores')}</td><td>${so.cores}</td></tr>
            <tr><td>${window.t('results.row.threads')}</td><td>${so.threads}</td></tr>
            <tr><td>${window.t('results.row.ram')}</td><td>${so.ram_gb} GB</td></tr>
            <tr><td>${window.t('results.row.raw_storage')}</td><td>${so.raw_storage_tb} TB</td></tr>`;
    }
    perNodeTable.innerHTML = perNodeHtml;

    const cl = result.cluster_total;
    document.getElementById('cluster-table').innerHTML = `
        <tr><td>${window.t('results.row.total_cores')}</td><td>${cl.cores}</td></tr>
        <tr><td>${window.t('results.row.total_threads')}</td><td>${cl.threads}</td></tr>
        <tr><td>${window.t('results.row.total_ghz')}</td><td>${cl.total_ghz} GHz</td></tr>
        <tr><td>${window.t('results.row.total_ram')}</td><td>${formatRam(cl.ram_gb)}</td></tr>
        <tr><td>${window.t('results.row.total_raw_storage')}</td><td>${cl.raw_storage_tb} TB</td></tr>
        <tr><td>${window.t('results.row.usable_storage')}</td><td class="usable">${cl.usable_storage_tb} TB</td></tr>`;

    const n1 = result.n_minus_1;
    // n1Desc is declared and set above (with the multi-cluster wording).
    const n1Card = document.querySelector('.result-card.n1');
    if (result.single_node) {
        // A single-node system has no peer to fail over to, so N-1 is meaningless.
        // Grey the card out and replace the figures with the no-redundancy notice.
        n1Card.classList.add('no-redundancy');
        if (n1Desc) n1Desc.textContent = '';
        document.getElementById('n1-table').innerHTML = `
            <tr><td class="no-redundancy-msg" colspan="2">
                <strong>${window.t('results.no_redundancy_label')}</strong> ${result.redundancy_note
                    ? result.redundancy_note.replace(/^No redundancy[^a-zA-Z]*/, '')
                    : window.t('results.no_redundancy_default')}
            </td></tr>`;
    } else {
        n1Card.classList.remove('no-redundancy');
        document.getElementById('n1-table').innerHTML = `
            <tr><td>${window.t('results.row.available_cores')}</td><td>${n1.cores}</td></tr>
            <tr><td>${window.t('results.row.available_threads')}</td><td>${n1.threads}</td></tr>
            <tr><td>${window.t('results.row.available_ghz')}</td><td>${n1.total_ghz} GHz</td></tr>
            <tr><td>${window.t('results.row.available_ram')}</td><td>${formatRam(n1.ram_gb)}</td></tr>
            <tr><td>${window.t('results.row.usable_storage')}</td><td class="usable">${n1.usable_storage_tb} TB</td></tr>`;
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
        showUploadStatus(window.t('upload.must_be_xlsx'), true);
        return;
    }

    showUploadStatus(window.t('upload.analyzing'), false);
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
        vmAdded = new Set();
        vmRemoved = new Set();
        vmPowerFilter = 'all';
        initClusters(data);  // sets up (or clears) the multi-site cluster tabs
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
            ? window.t('upload.scan_note_general')
            : '';
        showUploadStatus(window.t('upload.analyzed', {source: sourceLabel, file: file.name, note: scanNote}), false);
        displayImportResults(data);
    } catch (e) {
        showUploadStatus(window.t('upload.failed', {error: e.message}), true);
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
// Per-recommendation CPU-performance comparison, as a readable line under the
// card header with an "Explanation" button (opens a plain-language modal).
// Advisory only — it never changes the sizing (active scaling is gated
// server-side by perf_scaling, default off). Empty when no source score is
// entered or this config's CPU has no perf data.
function formatPerfLine(r) {
    const src = lastPerfSource;
    const tgt = r.totals && r.totals.perf_index;
    if (!src || !src.source_index_specrate || !tgt) return '';
    // Benchmark-vs-benchmark (apples to apples): both sides are rated SPECrate.
    const ratio = tgt / src.source_index_specrate;
    const phrase = ratio >= 1
        ? window.t('perf.phrase_better', {ratio: ratio.toFixed(1)})
        : window.t('perf.phrase_percent_of', {pct: Math.round(ratio * 100)});
    const usesPM = (sourceUsedPassmark() || r.cpu_perf_is_passmark) ? 1 : 0;
    return `<div class="rec-perf-line">${window.t('perf.benchmark_line', {phrase: phrase})}`
        + ` <button type="button" class="rec-perf-explain"`
        + ` data-click='["explainPerf",${ratio.toFixed(3)},${tgt},${src.source_index_specrate},${usesPM}]'>`
        + `${window.t('perf.explanation_btn')}</button></div>`;
}

// Plain-language modal explaining a recommendation's CPU-performance comparison.
function explainPerf(ratio, tgt, src, usesPM) {
    const delivers = ratio >= 1
        ? window.t('perf.delivers_better', {ratio: (+ratio).toFixed(1)})
        : window.t('perf.delivers_percent', {pct: Math.round(ratio * 100)});
    let msg = window.t('perf.explain_intro', {src: src, tgt: tgt, delivers: delivers});
    if (usesPM) {
        msg += window.t('perf.explain_passmark');
    }
    const floorActive = !!(lastProjection[activeMode]
        && lastProjection[activeMode].compute_floor
        && lastProjection[activeMode].compute_floor.active);
    msg += floorActive ? window.t('perf.explain_floor_on') : window.t('perf.explain_floor_off');
    msg += window.t('perf.explain_disclaimer');
    showInfoModal(window.t('perf.modal_title'), msg);
}

// Per-CPU source benchmark breakdown for the exportables (the "where you are
// now" side of the performance slide). Mirrors computeSourcePerf()'s maths but
// keeps the line items. Returns null when nothing is entered.
function buildSourcePerfExport() {
    const cpus = [];
    let total = 0;
    document.querySelectorAll('#source-cpu-panel .source-cpu-row').forEach(row => {
        const scoreEl = row.querySelector('.source-cpu-score');
        const v = scoreEl ? parseFloat(scoreEl.value) : NaN;
        if (!v) return;
        const sockets = parseInt(scoreEl.dataset.sockets, 10) || 1;
        const typeEl = row.querySelector('.source-cpu-type');
        const type = typeEl ? typeEl.value : 'specrate';
        const nameEl = row.querySelector('.source-cpu-name');
        const model = nameEl ? (nameEl.childNodes[0].textContent || '').trim() : '';
        const specrate = type === 'passmark' ? v * 0.00386 : v;
        const contrib = specrate * sockets;
        total += contrib;
        cpus.push({
            model, sockets, type, score: v,
            specrate: Math.round(specrate * 10) / 10,
            total: Math.round(contrib * 10) / 10,
        });
    });
    if (!cpus.length) return null;
    return { total_specrate: Math.round(total * 10) / 10, cpus };
}

// Whether any entered source CPU used a PassMark (rather than SPECrate) score —
// gates the conversion caveat in the comparison tooltip.
function sourceUsedPassmark() {
    let used = false;
    document.querySelectorAll('#source-cpu-panel .source-cpu-score').forEach(inp => {
        if (!parseFloat(inp.value)) return;
        const t = document.querySelector(`#source-cpu-panel .source-cpu-type[data-srcidx="${inp.dataset.srcidx}"]`);
        if (t && t.value === 'passmark') used = true;
    });
    return used;
}

// Render the source CPUs detected from the import (Environment Summary), each
// with a per-CPU benchmark input. Auto-fills the score where we recognise the
// part (limited to our catalog SKUs — most old source CPUs are unknown and need
// manual entry). computeSourcePerf() sums these (socket-weighted, normalised to
// SPECrate) to feed the comparison shown above the recommendations.
async function renderSourceCpus(sourceCpus) {
    const panel = document.getElementById('source-cpu-panel');
    if (!panel) return;
    if (!sourceCpus || !sourceCpus.length) { panel.innerHTML = ''; return; }
    panel.innerHTML = `<div class="source-cpu-head">
        <span class="muted">${window.t('import.source_cpu_help')}</span></div>`
        + sourceCpus.map((c, i) => `
        <div class="source-cpu-row">
            <div class="source-cpu-name">${esc(c.model)} <span class="muted">${window.t('import.source_cpu_sockets', {count: c.sockets})}</span></div>
            <select class="source-cpu-type" data-srcidx="${i}" data-change='["recalcRecommendations"]'>
                <option value="specrate">SPECrate2017</option>
                <option value="passmark">PassMark</option>
            </select>
            <input type="number" class="source-cpu-score" data-srcidx="${i}" data-sockets="${c.sockets}"
                   min="0" step="1" placeholder="${window.t('import.source_cpu_score_ph')}" data-change='["recalcRecommendations"]'>
            <span class="source-cpu-status muted" id="src-cpu-status-${i}"></span>
        </div>`).join('');
    await Promise.all(sourceCpus.map(async (c, i) => {
        const status = document.getElementById('src-cpu-status-' + i);
        try {
            const resp = await fetch('/api/cpu-perf?q=' + encodeURIComponent(c.model));
            const d = await resp.json();
            if (!d.found) { if (status) status.textContent = window.t('import.source_cpu_not_found'); return; }
            panel.querySelector(`.source-cpu-type[data-srcidx="${i}"]`).value = d.perf_type;
            panel.querySelector(`.source-cpu-score[data-srcidx="${i}"]`).value = d.perf_index;
            if (status) status.textContent = d.source === 'spec-cpu2017'
                ? window.t('import.source_cpu_auto_spec', {samples: d.samples})
                : window.t('import.source_cpu_auto_catalog');
        } catch (e) { /* best-effort */ }
    }));
    updateSourcePerfCard();
}

// The Environment-Summary card shows the summed source SPECrate; the per-CPU
// inputs live in the modal it opens. Keep the card in sync as scores change.
function updateSourcePerfCard() {
    const el = document.getElementById('source-perf-total');
    if (!el) return;
    const total = computeSourcePerf();
    el.textContent = total != null ? Math.round(total).toLocaleString() : '—';
}

function openSourceCpuModal() {
    const m = document.getElementById('source-cpu-modal');
    if (m) m.style.display = 'flex';
}

function closeSourceCpuModal() {
    const m = document.getElementById('source-cpu-modal');
    if (m) m.style.display = 'none';
    updateSourcePerfCard();
}

// Total source-environment throughput on the SPECrate scale (per-CPU score x
// sockets, PassMark normalised at 0.00386), summed across detected CPUs. null
// when nothing is entered.
function computeSourcePerf() {
    let total = 0, any = false;
    document.querySelectorAll('#source-cpu-panel .source-cpu-score').forEach(inp => {
        const v = parseFloat(inp.value);
        if (!v) return;
        const i = inp.dataset.srcidx;
        const typeEl = document.querySelector(`#source-cpu-panel .source-cpu-type[data-srcidx="${i}"]`);
        const sockets = parseInt(inp.dataset.sockets, 10) || 1;
        total += (typeEl && typeEl.value === 'passmark' ? v * 0.00386 : v) * sockets;
        any = true;
    });
    return any ? Math.round(total * 10) / 10 : null;
}

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
    const maxDayOneStorage = parseFloat(document.getElementById('max-day-one-storage').value);
    const maxDayOneRam = parseFloat(document.getElementById('max-day-one-ram').value);
    // Source-environment CPU benchmark (from the detected-CPU inputs in the
    // Source CPU modal), summed socket-weighted and normalised to SPECrate.
    const sourcePerfIndex = computeSourcePerf();
    const sourcePerfType = 'specrate';
    updateSourcePerfCard();

    // Multi-site: the inbound replication reserve this cluster must host, and
    // how it reserves the compute for it.
    let replicationReserve = null, replicationMode = 'reserved', allowSingleNode = false;
    if (activeMode === 'import' && separateClusters
        && activeCluster !== COMBINED_KEY && activeCluster !== SELECTED_KEY) {
        replicationReserve = inboundReserveFor(activeCluster);
        replicationMode = _repCfg(activeCluster).mode || 'reserved';
        // Single-node is only offered on dedicated DR clusters.
        allowSingleNode = !!_repCfg(activeCluster).singleNode;
    }

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
                max_day_one_storage_pct: maxDayOneStorage,
                max_day_one_ram_pct: maxDayOneRam,
                source_perf_index: sourcePerfIndex,
                source_perf_type: sourcePerfType,
                replication_reserve: replicationReserve,
                replication_compute_mode: replicationMode,
                allow_single_node: allowSingleNode,
            }),
        });
        const data = await resp.json();
        // Store projection first: renderRecommendationsTo reads lastProjection
        // for the IOPS demand/headroom line.
        if (data.projection) lastProjection[activeMode] = data.projection;
        lastPerfSource = data.perf_comparison || null;
        if (data.recommendations) {
            lastRecommendations[activeMode] = data.recommendations;
            lastSummary[activeMode] = summary;
            // Cache per-cluster so a multi-cluster export can gather each
            // cluster's sized result (only meaningful in separate-clusters mode).
            if (activeMode === 'import' && separateClusters) {
                clusterResults[activeCluster] = {
                    recommendations: data.recommendations,
                    projection: data.projection,
                    perfSource: data.perf_comparison || null,
                    summary: summary,
                };
            }
            renderRecommendationsTo(data.recommendations, 'rec-list', 'ratio-slider', activeMode, data.warnings);
            updateFullClusterInfo(sizeFullCluster, data.recommendations);
        }
        if (data.projection) {
            renderProjectionTo(data.projection, 'projection-summary');
        }
        // Single/combined-workload DR cluster (no-op in separate mode / when off).
        renderDrClusterOption();
        sizeDrCluster(summary);
    } catch (e) {
        console.error('Recalc failed:', e);
    }
}

// Append the worst-case degraded ratio across the current recommendations to the
// (i) tooltip when full-cluster sizing is active.
function updateFullClusterInfo(enabled, recommendations) {
    const icon = document.getElementById('full-cluster-info');
    if (!icon) return;
    if (enabled && recommendations && recommendations.length > 0) {
        const worst = Math.max(...recommendations.map(r => r.vcpu_ratio_degraded || 0));
        setInfoTip(icon, window.t('results.full_cluster_info_base') +
            window.t('results.full_cluster_info_degraded', {ratio: worst.toFixed(2)}));
    } else {
        setInfoTip(icon, window.t('results.full_cluster_info_base'));
    }
}

function renderProjectionTo(p, targetId) {
    document.getElementById(targetId).innerHTML = `
        <div class="proj-grid">
            <div class="proj-card">
                <div class="proj-label">${window.t('results.proj.current_vcpus')}</div>
                <div class="proj-base">${p.base_vcpus}</div>
                <div class="proj-arrow">&#8594;</div>
                <div class="proj-label">${window.t('results.proj.year_vcpus', {years: p.years})}</div>
                <div class="proj-projected">${p.projected_vcpus}</div>
            </div>
            <div class="proj-card">
                <div class="proj-label">${window.t('results.proj.current_ram')}</div>
                <div class="proj-base">${formatRam(p.base_ram_gb)}</div>
                <div class="proj-arrow">&#8594;</div>
                <div class="proj-label">${window.t('results.proj.year_ram', {years: p.years})}</div>
                <div class="proj-projected">${formatRam(p.projected_ram_gb)}</div>
            </div>
            <div class="proj-card">
                <div class="proj-label">${window.t('results.proj.current_ghz')}</div>
                <div class="proj-base">${p.base_ghz} GHz</div>
                <div class="proj-arrow">&#8594;</div>
                <div class="proj-label">${window.t('results.proj.year_ghz', {years: p.years})}</div>
                <div class="proj-projected">${p.projected_ghz} GHz</div>
            </div>
            <div class="proj-card">
                <div class="proj-label">${window.t('results.proj.current_storage')}</div>
                <div class="proj-base">${p.base_storage_tb} TiB</div>
                <div class="proj-arrow">&#8594;</div>
                <div class="proj-label">${window.t('results.proj.year_snapshots', {years: p.years})}</div>
                <div class="proj-projected">${p.projected_storage_tb} TiB</div>
            </div>
        </div>
        <div class="proj-note">
            ${window.t('results.proj.growth_note', {factor: p.growth_factor, years: p.years, snapPct: p.snapshot_pct_at_target})}
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
    if (d.avg) bits.push(window.t('results.iops_avg', {value: d.avg.toLocaleString()}));
    if (!bits.length) return '';
    return `<div class="proj-note">${window.t('results.workload_iops_demand', {values: bits.join(' &middot; ')})}</div>`;
}

// The current-environment ratio marker + label under the slider. resetSlider
// starts a fresh sizing at the default ratio (used on first display); tab
// switches leave the slider on the cluster's own saved value.
function renderRatioContext(s, resetSlider) {
    const currentRatio = s.vcpu_per_core_ratio || 3.0;
    if (resetSlider) {
        // Start sizing at the standard default ratio; the detected ratio is
        // still reported below via the marker and label.
        document.getElementById('ratio-slider').value = DEFAULT_SIZING_RATIO;
        updateRatioDisplay();
    }

    const markerPct = ((currentRatio - 1) / 7) * 100;
    const marker = document.getElementById('ratio-bar-marker');
    marker.style.left = Math.min(markerPct, 100) + '%';

    if (s.vcpu_ratio_assumed) {
        // Server-level scan: no overcommit was measured, so this is a default.
        marker.title = window.t('results.ratio_assumed_tooltip', {ratio: currentRatio.toFixed(2)});
        document.getElementById('ratio-current').innerHTML =
            window.t('results.ratio_assumed', {ratio: currentRatio.toFixed(2)});
    } else {
        marker.title = window.t('results.ratio_current_tooltip', {ratio: currentRatio.toFixed(2)});
        document.getElementById('ratio-current').innerHTML =
            window.t('results.ratio_current', {ratio: currentRatio.toFixed(2), vcpus: s.total_vcpus, cores: s.total_host_cores});
    }
}

// Env-summary + workload cards for a summary. Factored out so a cluster tab
// switch can re-render them without re-running the whole import display.
function renderEnvWorkloadCards(s) {
    document.getElementById('env-summary').innerHTML = `
        <div class="summary-card">
            <div class="summary-label">${window.t('import.current_platform')}</div>
            <div class="summary-value">${esc(s.current_platform)}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">${window.t('import.cluster')}</div>
            <div class="summary-value">${esc(s.cluster_name)}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">${window.t('import.hosts')}</div>
            <div class="summary-value">${s.host_count}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">${window.t('import.total_vms')}</div>
            <div class="summary-value">${window.t('import.vms_active', {total: s.total_vms, active: s.active_vms})}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">${window.t('import.current_cores')}</div>
            <div class="summary-value">${s.total_host_cores}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">${window.t('import.current_threads')}</div>
            <div class="summary-value">${s.total_host_threads}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">${window.t('import.current_ram')}</div>
            <div class="summary-value">${formatRam(s.total_host_ram_gb)}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">${window.t('import.peak_cpu')}</div>
            <div class="summary-value">${window.t('import.peak_avg_pct', {peak: s.peak_cpu_pct, avg: s.avg_cpu_pct})}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">${window.t('import.peak_memory')}</div>
            <div class="summary-value">${window.t('import.peak_avg_pct', {peak: s.peak_mem_pct, avg: s.avg_mem_pct})}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">${window.t('import.iops')}</div>
            <div class="summary-value">${window.t('import.iops_avg_peak', {avg: s.total_avg_iops.toLocaleString(), peak: s.total_peak_iops.toLocaleString()})}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">${window.t('import.p95_iops')}</div>
            <div class="summary-value">
                <input type="number" id="p95-iops" value="${s.p95_iops || 0}" min="0" step="1"
                       class="inline-input" placeholder="${window.t('import.p95_placeholder')}"
                       data-change='["updateP95Display"]'>
            </div>
        </div>
        ${(s.source_cpus && s.source_cpus.length) ? `
        <div class="summary-card">
            <div class="summary-label">${window.t('import.source_cpu')}</div>
            <div class="summary-value"><span id="source-perf-total">—</span>
                <a class="card-edit-link" data-click='["openSourceCpuModal"]'>${window.t('import.edit_add')}</a></div>
        </div>` : ''}
    `;

    document.getElementById('workload-summary').innerHTML = `
        <div class="summary-card">
            <div class="summary-label">${window.t('import.vcpus_required')}</div>
            <div class="summary-value accent">${s.total_vcpus}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">${window.t('import.provisioned_ram')}</div>
            <div class="summary-value accent">${formatRam(s.total_vm_provisioned_memory_gb)}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">${window.t('import.used_ram')}</div>
            <div class="summary-value">${formatRam(s.total_vm_used_memory_gb)}</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">${window.t('import.provisioned_storage')}</div>
            <div class="summary-value">${s.total_vm_provisioned_storage_tb} TiB</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">${window.t('import.datastore_used')}</div>
            <div class="summary-value accent">${s.datastore_used_tb} TiB</div>
        </div>
        <div class="summary-card">
            <div class="summary-label">${window.t('import.datastore_total')}</div>
            <div class="summary-value">${s.datastore_total_tb} TiB</div>
        </div>
    `;

    renderLocalStorageOption(s);
}

function displayImportResults(data) {
    const s = data.summary;
    activeMode = 'import';
    document.getElementById('import-results').style.display = 'block';
    document.getElementById('sizing-results').style.display = 'block';

    renderRatioContext(s, true);
    renderEnvWorkloadCards(s);

    lastRecommendations['import'] = data.recommendations;
    lastSummary['import'] = data.summary;
    lastProjection['import'] = data.projection;
    lastPerfSource = data.perf_comparison || null;
    // Show the detected source CPUs + benchmark inputs (auto-filled where known),
    // then re-run sizing so the comparison reflects any auto-filled scores.
    renderSourceCpus(data.summary && data.summary.source_cpus).then(() => {
        if (computeSourcePerf() != null) recalcRecommendations();
    });
    renderRecommendationsTo(data.recommendations, 'rec-list', 'ratio-slider', 'import', data.warnings);
    if (data.projection) renderProjectionTo(data.projection, 'projection-summary');
    document.getElementById('import-results').scrollIntoView({behavior: 'smooth'});
}

// "Determined by" line: which resource drove this config's node count, with the
// required-vs-achieved figures.
function formatDeterminant(det) {
    if (!det) return '';
    if (det.resource === 'minimum') {
        return `<div class="rec-determinant">${window.t('results.determined_by_minimum')}</div>`;
    }
    // Compute floor (perf-based sizing): the value is a coverage % of the
    // source's utilized, grown compute demand, not a single-unit capacity.
    if (det.resource === 'Compute') {
        return `<div class="rec-determinant">${window.t('results.determined_by_compute', {achieved: det.achieved, headroom: det.headroom_pct})}</div>`;
    }
    const u = det.unit;
    const fmt = v => u === 'GB' ? formatRam(v)
        : (u === 'cores' ? window.t('results.cores_value', {value: Math.round(v).toLocaleString()}) : `${v} TB`);
    return `<div class="rec-determinant">${window.t('results.determined_by_resource', {resource: det.resource, required: fmt(det.required), achieved: fmt(det.achieved), headroom: det.headroom_pct})}</div>`;
}

// Compute-floor coverage line (perf-based sizing). Shown only when the active
// floor is on (perf_scaling) and this config has a coverage figure. Breaks the
// blended coverage into its clock (GHz) and benchmark (SPECrate) components so
// the user can see what drove it.
function formatComputeFloorLine(r) {
    const cf = r.compute_floor;
    if (!cf || cf.coverage_pct == null) return '';
    const parts = [];
    if (cf.ghz_pct != null) parts.push(window.t('results.compute_floor_clock', {pct: cf.ghz_pct}));
    if (cf.perf_pct != null) parts.push(window.t('results.compute_floor_benchmark', {pct: cf.perf_pct}));
    const detail = parts.length ? ` <span class="muted">${window.t('results.compute_floor_detail', {parts: parts.join(', '), util: cf.source_cpu_util_pct})}</span>` : '';
    return `<div class="rec-compute-floor">${window.t('results.compute_floor_line', {pct: cf.coverage_pct})}${detail}</div>`;
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
            recList.innerHTML = `<div class="no-recs">${window.t('results.no_matching_configs')}</div>`;
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

    // The witness requirement applies per recommendation: only a config whose
    // cluster is exactly 2 nodes needs one (storage-only nodes add quorum, so
    // those never count as 2-node here). It's shown inside that specific card.
    const recTotalNodes = r => r.storage_only
        ? (r.hci_node_count || r.node_count) + r.storage_only.count
        : r.node_count;

    // In separate-clusters mode each source cluster contributes one chosen
    // recommendation to the combined export; surface a per-card picker (the
    // Combined tab isn't part of that export, so no picker there).
    const showRecPicker = mode === 'import' && separateClusters
        && activeCluster !== COMBINED_KEY && activeCluster !== SELECTED_KEY;
    const selIdx = showRecPicker ? (clusterSelectedRec[activeCluster] ?? 0) : -1;

    recList.innerHTML = warningsHtml + recommendations.map((r, i) =>
        recCardHtml(r, i, mode, demand, { showPicker: showRecPicker, selIdx })
    ).join('');
}

// Build one recommendation card's HTML. Shared by the per-cluster / manual rec
// lists and the multi-site "Selected clusters" review tab. opts:
//   showPicker    — show the per-card "use in export" selector
//   selIdx        — currently-selected index (for the picker highlight)
//   footerActions — show export/diagram action buttons (false on review cards,
//                   which are for screenshotting the finished solution)
function recCardHtml(r, i, mode, demand, opts) {
    opts = opts || {};
    const showRecPicker = !!opts.showPicker;
    const selIdx = opts.selIdx == null ? -1 : opts.selIdx;
    const footerActions = opts.footerActions !== false;
    const recTotalNodes = rr => rr.storage_only
        ? (rr.hci_node_count || rr.node_count) + rr.storage_only.count
        : rr.node_count;

    const isSelected = i === selIdx;
    const recPicker = showRecPicker
        ? `<button class="rec-select ${isSelected ? 'selected' : ''}" data-click='["selectClusterRec",${i}]'
                title="${window.t('cluster.select_for_export_title')}">${isSelected ? window.t('cluster.selected_for_export') : window.t('cluster.select_for_export')}</button>`
        : '';
    const clusterInfo = r.num_clusters > 1
        ? window.t('results.clusters_layout', {count: r.num_clusters, layout: r.cluster_layout.join(' + ')})
        : window.t('results.one_cluster');
    const n1Label = r.num_clusters > 1
        ? window.t('results.n1_per_cluster', {spares: r.num_clusters})
        : window.t('results.n1_available');
    const modelLabel = r.validated_only
        ? r.model
        : (r.validated ? window.t('results.validated_based_off', {model: r.model}) : r.model);
    const ratioBadge = r.sized_full_cluster
        ? `<span class="rec-ratio-badge degraded" title="${window.t('results.ratio_badge_degraded_tooltip', {ratio: r.vcpu_ratio_degraded.toFixed(2)})}">${r.vcpu_ratio.toFixed(2)}:1 &rarr; ${r.vcpu_ratio_degraded.toFixed(2)}:1</span>`
        : `<span class="rec-ratio-badge" title="${window.t('results.ratio_badge_tooltip')}">${r.vcpu_ratio.toFixed(2)}:1</span>`;
    const iops = r.iops || null;
    const iopsRow = (val) => iops ? `<tr><td>${window.t('results.row.net_iops')}</td><td>${Math.round(val).toLocaleString()}</td></tr>` : '';
    const iopsHeadroom = buildIopsHeadroom(iops, demand);
    const utilBars = buildUtilizationBars(r);
    const witnessNote = recTotalNodes(r) === 2 ? witnessBarHtml() : '';
    const singleNodeNote = r.single_node
        ? `<div class="info-bar"><span class="info-bar-icon">i</span><span><strong>${window.t('results.single_node_title')}</strong> ${window.t('results.single_node_note')}</span></div>`
        : '';
    const so = r.storage_only || null;
    const nodesLabel = so
        ? window.t('results.nodes_hci_so_short', {hci: r.hci_node_count, so: so.count})
        : window.t('results.nodes_count', {count: r.node_count});
    const soRows = so ? `
                    <tr class="so-divider"><td colspan="2">${window.t('results.storage_only_divider', {count: so.count})}</td></tr>
                    <tr><td>${window.t('results.row.cpu')}</td><td>${so.cpu}</td></tr>
                    <tr><td>${window.t('results.row.ram')}</td><td>${formatRam(so.ram_gb)}</td></tr>
                    <tr><td>${window.t('results.row.storage')}</td><td>${esc(r.storage_config.desc)}</td></tr>` : '';
    const footerActionsHtml = footerActions ? `
                <div class="rec-footer-actions">
                    ${r.network_svg ? `<button class="btn btn-muted btn-sm" data-click='["openClusterDiagram","${mode}",${i}]' title="${window.t('results.btn_network_title')}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><rect x="2" y="2" width="8" height="8" rx="1"/><rect x="14" y="2" width="8" height="8" rx="1"/><rect x="8" y="14" width="8" height="8" rx="1"/><path d="M6 10v2a2 2 0 0 0 2 2h0M18 10v2a2 2 0 0 1-2 2h0M12 14v-2"/></svg>${window.t('results.btn_network')}</button>` : ''}
                    ${canExportEditable() ? `<button class="btn btn-muted btn-sm" data-click='["exportProposal","${mode}",${i},"docx"]' title="${window.t('results.btn_word_title')}">Word</button>` : ''}
                    ${canExportEditable() ? `<button class="btn btn-muted btn-sm" data-click='["exportProposal","${mode}",${i},"pptx"]' title="${window.t('results.btn_pptx_title')}">PPTX</button>` : ''}
                    <button class="btn btn-muted btn-sm" data-click='["exportProposal","${mode}",${i},"presentation-pdf"]' title="${window.t('results.btn_slides_pdf_title')}">${window.t('results.btn_slides_pdf')}</button>
                    <button class="btn btn-export" data-click='["exportProposal","${mode}",${i},"pdf"]' title="${window.t('results.btn_proposal_pdf_title')}"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:4px"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>${window.t('results.btn_proposal_pdf')}</button>
                </div>` : '';
    return `
        <div class="rec-card ${i === 0 ? 'rec-best' : ''} ${isSelected ? 'rec-selected' : ''}">
            <div class="rec-header">
                <span class="rec-rank">#${i + 1}</span>
                <span class="rec-model">${modelLabel}</span>
                <span class="rec-category">${r.category}</span>
                ${ratioBadge}
                <span class="rec-nodes">${nodesLabel}</span>
                <span class="rec-clusters" title="${clusterInfo}">${clusterInfo}</span>
                ${recPicker}
            </div>
            ${formatPerfLine(r)}
            ${formatDeterminant(r.determinant)}
            ${formatComputeFloorLine(r)}
            <div class="rec-details">
                <div class="rec-col">
                    <h4>${window.t('results.per_node')}</h4>
                    <table>
                        <tr><td>${window.t('results.row.cpu')}</td><td>${r.cpu}</td></tr>
                        <tr><td>${window.t('results.row.cores')}</td><td>${r.cores_per_node}</td></tr>
                        <tr><td>${window.t('results.row.threads')}</td><td>${r.threads_per_node}</td></tr>
                        <tr><td>${window.t('results.row.ram')}</td><td>${formatRam(r.ram_per_node_gb)}</td></tr>
                        <tr><td>${window.t('results.row.storage')}</td><td>${esc(r.storage_config.desc)}</td></tr>
                        ${iops ? iopsRow(iops.per_node) : ''}
                        ${soRows}
                    </table>
                </div>
                <div class="rec-col">
                    <h4>${window.t('results.total_all_clusters')}</h4>
                    <table>
                        <tr><td>${window.t('results.row.cores')}</td><td>${r.totals.cores}</td></tr>
                        <tr><td>${window.t('results.row.threads')}</td><td>${r.totals.threads}</td></tr>
                        <tr><td>${window.t('results.row.ghz')}</td><td>${r.totals.total_ghz}</td></tr>
                        <tr><td>${window.t('results.row.ram')}</td><td>${formatRam(r.totals.ram_gb)}</td></tr>
                        <tr><td>${window.t('results.row.usable_storage')}</td><td class="usable">${r.totals.usable_storage_tb} TB</td></tr>
                        ${iops ? iopsRow(iops.total) : ''}
                    </table>
                </div>
                <div class="rec-col">
                    <h4>${n1Label}</h4>
                    <table>
                        <tr><td>${window.t('results.row.cores')}</td><td>${r.n_minus_1.cores}</td></tr>
                        <tr><td>${window.t('results.row.threads')}</td><td>${r.n_minus_1.threads}</td></tr>
                        <tr><td>${window.t('results.row.ghz')}</td><td>${r.n_minus_1.total_ghz}</td></tr>
                        <tr><td>${window.t('results.row.ram')}</td><td>${formatRam(r.n_minus_1.ram_gb)}</td></tr>
                        <tr><td>${window.t('results.row.usable_storage')}</td><td class="usable">${r.n_minus_1.usable_storage_tb} TB</td></tr>
                        ${iops ? iopsRow(iops.n_minus_1) : ''}
                    </table>
                </div>
            </div>
            ${utilBars}
            ${iopsHeadroom}
            ${witnessNote}
            ${singleNodeNote}
            <div class="rec-footer">
                <span>${r.form_factor} &mdash; ${r.chassis}</span>${footerActionsHtml}
            </div>
        </div>
    `;
}

// Per-resource utilization bars (demand / available capacity at N-1) for a
// recommendation. The constraining resource — determinant.resource — is flagged
// "limiting". IOPS is intentionally left to the headroom line below (its ratios
// are usually huge, so a near-empty bar would mislead).
function buildUtilizationBars(r) {
    const u = r.utilization;
    if (!u) return '';
    const binding = (r.determinant && r.determinant.resource) || '';
    const rows = [['CPU', u.cpu], ['RAM', u.ram], ['Storage', u.storage]];
    const labelFor = k => k === 'Storage' ? window.t('results.util.storage') : k;
    let anyHa = false, anyRep = false;
    const bars = rows.map(([key, val]) => {
        if (!val) return '';
        // Bar = full (all-nodes) capacity. `current` is today's load; up to
        // `total` is growth + snapshot reserve (which now includes any inbound
        // replication reserve, carved out as its own dark-yellow band);
        // `ha_reserve` is capacity held for failover (the N-1→full gap, CPU/RAM
        // only). Colour by CURRENT load (the real risk now). Failover sits at the
        // right edge; replication reserve sits just before free space.
        const cur = Math.max(0, Math.round(val.current || 0));
        const tot = Math.max(cur, Math.round(val.total || 0));
        const rep = Math.max(0, Math.round(val.replication || 0));
        const reserve = Math.max(0, tot - cur - rep);   // own growth + snapshot
        if (rep > 0) anyRep = true;
        const ha = Math.max(0, Math.round(val.ha_reserve || 0));
        if (ha > 0) anyHa = true;
        const curW = Math.min(cur, 100);
        const resW = Math.min(reserve, 100 - curW);
        const repW = Math.min(rep, 100 - curW - resW);
        const haW = Math.min(ha, 100 - curW - resW - repW);
        const freeW = Math.max(0, 100 - curW - resW - repW - haW);
        const cls = cur > 90 ? 'util-high' : (cur >= 70 ? 'util-mid' : 'util-low');
        const label = labelFor(key);
        const bind = key === binding
            ? ` <span class="util-bind" title="${window.t('results.util.limiting_tooltip')}">${window.t('results.util.limiting')}</span>`
            : '';
        const tipParts = [
            window.t('results.util.tip_now', {pct: cur}),
            window.t('results.util.tip_reserve', {pct: reserve}),
        ];
        if (rep > 0) tipParts.push(window.t('results.util.tip_replication', {pct: rep}));
        if (ha > 0) tipParts.push(window.t('results.util.tip_ha', {pct: ha}));
        tipParts.push(window.t('results.util.tip_free', {pct: freeW}));
        const tip = window.t('results.util.tip', {label, parts: tipParts.join(' · '), tot});
        return `<div class="util-row" title="${tip}">
            <span class="util-label">${label}${bind}</span>
            <span class="util-track">
                <span class="util-fill ${cls}" style="width:${curW}%"></span>
                <span class="util-fill util-reserve" style="width:${resW}%"></span>
                <span class="util-fill util-replication" style="width:${repW}%"></span>
                <span class="util-fill util-free" style="width:${freeW}%"></span>
                <span class="util-fill util-ha" style="width:${haW}%"></span>
            </span>
            <span class="util-pct" title="${window.t('results.util.pct_tooltip', {cur, tot})}">${cur}%<span class="util-pct-sized"> / ${tot}%</span></span>
        </div>`;
    }).join('');
    const repKey = anyRep
        ? `<span class="util-key"><i class="util-sw util-sw-replication"></i>${window.t('results.util.replication_reserve')}</span>`
        : '';
    const haKey = anyHa
        ? `<span class="util-key"><i class="util-sw util-sw-ha"></i>${window.t('results.util.ha_reserve')}</span>`
        : '';
    return `<div class="rec-utilization">
        <div class="util-head">
            <span class="util-title">${window.t('results.util.title')}</span>
            <span class="util-legend">
                <span class="util-key"><i class="util-sw util-sw-now"></i>${window.t('results.util.now')}</span>
                <span class="util-key"><i class="util-sw util-sw-reserve"></i>${window.t('results.util.growth_snapshot')}</span>
                ${repKey}
                ${haKey}
            </span>
        </div>${bars}
    </div>`;
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
            `<span class="${ok ? 'iops-ok' : 'iops-short'}" title="${window.t('results.iops_headroom_tooltip', {label, demand: value.toLocaleString(), available: iops.total.toLocaleString()})}">` +
            `${label}: ${ratio.toFixed(1)}&times; ${ok ? '&#10003;' : '&#9888;'}</span>`
        );
    };
    fmtMetric('P95', demand.p95);
    fmtMetric(window.t('results.avg_label'), demand.avg);
    if (!parts.length) return '';
    return `<div class="rec-iops-headroom">${window.t('results.iops_headroom_label', {parts: parts.join(' &middot; ')})}</div>`;
}

// ==================== MANUAL INPUT MODE ====================

let manualSummary = null;

// Per-VM manual entry. The user builds a VM list in a modal; the totals fill the
// Workload Requirements fields and act as a floor (those fields can be raised but
// not set below the entered VMs).
let manualVms = [];
let manualVmFloors = {};   // { vcpus, prov_ram, prov_storage_tb, ds_used_tb, total_vms, active_vms }
let manualVmSort = { field: 'name', asc: true };   // default: VM name A→9

function openManualVmModal() {
    if (!manualVms.length) addManualVm();   // start with one editable row
    renderManualVmTable();
    updateManualVmSummary();
    document.getElementById('manual-vm-modal').style.display = 'flex';
}

function closeManualVmModal() {
    document.getElementById('manual-vm-modal').style.display = 'none';
}

function addManualVm() {
    manualVms.push({
        name: `VM ${manualVms.length + 1}`, powered_on: true,
        vcpus: 2, ram_gb: 4, storage_gb: 0,
    });
    const newIdx = manualVms.length - 1;
    renderManualVmTable();
    updateManualVmSummary();
    const row = document.querySelector(`#manual-vm-body tr[data-idx="${newIdx}"]`);
    if (row) {
        row.scrollIntoView({ block: 'center' });
        const input = row.querySelector('input.vm-edit-text');
        if (input) input.select();
    }
}

function removeManualVm(i) {
    manualVms.splice(i, 1);
    renderManualVmTable();
    updateManualVmSummary();
}

let cloneSrcIdx = null;

function openCloneModal(i) {
    cloneSrcIdx = i;
    const vm = manualVms[i];
    document.getElementById('clone-count').value = 1;
    document.getElementById('clone-autoinc').checked = true;
    document.getElementById('clone-source-name').textContent = vm ? (vm.name || window.t('manual.default_vm_name')) : window.t('manual.default_vm_name');
    document.getElementById('clone-vm-modal').style.display = 'flex';
    document.getElementById('clone-count').focus();
}

function closeCloneModal() {
    document.getElementById('clone-vm-modal').style.display = 'none';
    cloneSrcIdx = null;
}

function confirmCloneVm() {
    const count = Math.max(1, Math.min(500, parseInt(document.getElementById('clone-count').value) || 1));
    const autoInc = document.getElementById('clone-autoinc').checked;
    cloneManualVm(cloneSrcIdx, count, autoInc);
    closeCloneModal();
}

function _escapeRegExp(s) {
    return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// Insert `count` copies of manualVms[srcIdx] right after it. With auto-increment,
// the trailing number on the name is bumped (preserving zero-padding), starting
// after the highest existing number in that name series to avoid collisions
// (DC01 with DC02 present -> DC03, DC04). Names with no trailing number get a
// " N" suffix; without auto-increment the name is duplicated as-is.
function cloneManualVm(srcIdx, count, autoInc) {
    const src = manualVms[srcIdx];
    if (!src) return;
    const m = String(src.name || '').match(/^(.*?)(\d+)$/);
    let prefix = '', width = 0, start = 0, mode = 'plain';
    if (autoInc && m) {
        prefix = m[1];
        width = m[2].length;
        let maxVal = parseInt(m[2], 10);
        const re = new RegExp('^' + _escapeRegExp(prefix) + '(\\d+)$');
        manualVms.forEach(vm => {
            const mm = String(vm.name || '').match(re);
            if (mm) maxVal = Math.max(maxVal, parseInt(mm[1], 10));
        });
        start = maxVal + 1;
        mode = 'number';
    } else if (autoInc) {
        mode = 'suffix';
    }
    for (let k = 0; k < count; k++) {
        let name;
        if (mode === 'number') {
            name = prefix + String(start + k).padStart(width, '0');
        } else if (mode === 'suffix') {
            name = `${src.name} ${k + 2}`;
        } else {
            name = src.name;
        }
        manualVms.splice(srcIdx + 1 + k, 0, {
            name, powered_on: src.powered_on, vcpus: src.vcpus,
            ram_gb: src.ram_gb, storage_gb: src.storage_gb,
        });
    }
    renderManualVmTable();
    updateManualVmSummary();
}

function setManualVm(i, field, value) {
    const vm = manualVms[i];
    if (!vm) return;
    if (field === 'name') {
        vm.name = (value || '').trim();
    } else if (field === 'powered_on') {
        vm.powered_on = value === 'on' || value === true;
    } else if (field === 'vcpus') {
        vm.vcpus = Math.max(1, Math.round(parseFloat(value) || 0));
    } else {  // ram_gb, storage_gb
        vm[field] = Math.max(0, Math.round((parseFloat(value) || 0) * 10) / 10);
    }
    updateManualVmSummary();
}

function sortManualVm(field) {
    if (manualVmSort.field === field) manualVmSort.asc = !manualVmSort.asc;
    else manualVmSort = { field, asc: true };
    renderManualVmTable();
}

function renderManualVmTable() {
    const body = document.getElementById('manual-vm-body');
    // Sort a display copy so edits/clone/remove still address the real array
    // index (carried on data-idx), exactly like the import VM table.
    const f = manualVmSort.field, dir = manualVmSort.asc ? 1 : -1;
    const view = manualVms.map((vm, i) => ({ vm, i }));
    view.sort((a, b) => {
        let c;
        if (f === 'name') {
            c = String(a.vm.name || '').localeCompare(String(b.vm.name || ''),
                undefined, { numeric: true, sensitivity: 'base' });
        } else if (f === 'powered_on') {
            c = (a.vm.powered_on ? 1 : 0) - (b.vm.powered_on ? 1 : 0);
        } else {
            c = (a.vm[f] || 0) - (b.vm[f] || 0);
        }
        return c * dir;
    });
    body.innerHTML = view.map(({ vm, i }) => `
        <tr data-idx="${i}">
            <td class="vm-col-name"><input type="text" class="vm-edit vm-edit-text" value="${esc(vm.name)}" data-change='["setManualVm",${i},"name","$value"]'></td>
            <td class="vm-col-power">
                <select class="vm-edit" data-change='["setManualVm",${i},"powered_on","$value"]'>
                    <option value="on"${vm.powered_on ? ' selected' : ''}>${window.t('manual.power_on')}</option>
                    <option value="off"${vm.powered_on ? '' : ' selected'}>${window.t('manual.power_off')}</option>
                </select>
            </td>
            <td class="vm-col-num"><input type="number" class="vm-edit vm-edit-num" min="1" step="1" value="${vm.vcpus}" data-change='["setManualVm",${i},"vcpus","$value"]'></td>
            <td class="vm-col-num"><input type="number" class="vm-edit vm-edit-num" min="0" step="0.1" value="${vm.ram_gb}" data-change='["setManualVm",${i},"ram_gb","$value"]'></td>
            <td class="vm-col-num"><input type="number" class="vm-edit vm-edit-num" min="0" step="1" value="${vm.storage_gb}" data-change='["setManualVm",${i},"storage_gb","$value"]'></td>
            <td class="vm-col-action">
                <button class="vm-action-btn vm-clone" title="${window.t('manual.clone_vm_title')}" data-click='["openCloneModal",${i}]'><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg></button>
                <button class="vm-action-btn vm-remove" title="${window.t('manual.remove_vm_title')}" data-click='["removeManualVm",${i}]'>&times;</button>
            </td>
        </tr>`).join('');

    document.querySelectorAll('#manual-vm-table th.sortable').forEach(th => {
        const old = th.querySelector('.sort-arrow');
        if (old) old.remove();
        const field = JSON.parse(th.getAttribute('data-click') || '[]')[1];
        if (field === manualVmSort.field) {
            const arrow = document.createElement('span');
            arrow.className = 'sort-arrow';
            arrow.textContent = manualVmSort.asc ? ' ▲' : ' ▼';
            th.appendChild(arrow);
        }
    });
}

function manualVmTotals() {
    let vcpus = 0, ram = 0, storage = 0, active = 0;
    manualVms.forEach(vm => {
        vcpus += vm.vcpus || 0;
        ram += vm.ram_gb || 0;
        storage += vm.storage_gb || 0;
        if (vm.powered_on) active++;
    });
    return {
        count: manualVms.length, active, vcpus,
        ram_gb: Math.round(ram * 10) / 10,
        storage_gb: Math.round(storage * 10) / 10,
        storage_tb: Math.round(storage / 1024 * 100) / 100,
    };
}

function updateManualVmSummary() {
    const t = manualVmTotals();
    document.getElementById('manual-vm-summary').textContent =
        window.t('manual.vm_summary', {count: t.count, active: t.active, vcpus: t.vcpus, ram: t.ram_gb, storage: t.storage_gb});
}

// Apply the entered VMs: set the workload floors and fill the fields (raising any
// that are below the new totals; leaving higher manual overrides intact).
function applyManualVms() {
    const t = manualVmTotals();
    manualVmFloors = {
        total_vms: t.count, active_vms: t.active, vcpus: t.vcpus,
        prov_ram: t.ram_gb, prov_storage_tb: t.storage_tb, ds_used_tb: t.storage_tb,
    };
    const raiseTo = (id, floor, dec) => {
        const el = document.getElementById(id);
        if (!el) return;
        const cur = parseFloat(el.value) || 0;
        if (cur < floor || el.value === '') el.value = dec ? floor.toFixed(2) : floor;
    };
    raiseTo('man-total-vms', t.count);
    raiseTo('man-active-vms', t.active);
    raiseTo('man-vcpus', t.vcpus);
    raiseTo('man-prov-ram', t.ram_gb);
    raiseTo('man-prov-storage', t.storage_tb, true);
    raiseTo('man-ds-used', t.storage_tb, true);

    const badge = document.getElementById('manual-vm-count');
    const note = document.getElementById('manual-vm-note');
    if (t.count > 0) {
        badge.textContent = ` (${t.count})`;
        badge.style.display = '';
        note.textContent = window.t('manual.vms_entered_note', {count: t.count});
        note.style.display = '';
    } else {
        badge.style.display = 'none';
        note.style.display = 'none';
    }
    closeManualVmModal();
}

// Keep a workload field at or above its entered-VM total. Wired to the field's
// onchange; a no-op when no VMs have been entered (floor 0/undefined).
function clampManualField(id, floorKey) {
    const floor = manualVmFloors[floorKey] || 0;
    if (!floor) return;
    const el = document.getElementById(id);
    if (!el) return;
    const v = parseFloat(el.value) || 0;
    if (v < floor) {
        el.value = floorKey.endsWith('_tb') ? floor.toFixed(2) : floor;
        el.classList.add('field-clamped');
        setTimeout(() => el.classList.remove('field-clamped'), 1200);
    }
}

function calculateManual() {
    const vcpus = parseInt(document.getElementById('man-vcpus').value) || 0;
    const provRam = parseFloat(document.getElementById('man-prov-ram').value) || 0;
    const dsUsed = parseFloat(document.getElementById('man-ds-used').value) || 0;

    if (vcpus < 1 || provRam < 1 || dsUsed <= 0) {
        alert(window.t('manual.fill_required_fields'));
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
    // Start sizing at the standard default ratio; the detected ratio (currentRatio)
    // is still reported via the marker and label below.
    slider.value = DEFAULT_SIZING_RATIO;
    updateRatioDisplay();

    const marker = document.getElementById('ratio-bar-marker');
    if (cores > 0) {
        const markerPct = ((currentRatio - 1) / 7) * 100;
        marker.style.left = Math.min(markerPct, 100) + '%';
        marker.style.display = 'block';
        marker.title = window.t('results.ratio_current_tooltip', {ratio: currentRatio.toFixed(2)});
        document.getElementById('ratio-current').innerHTML =
            window.t('results.ratio_current', {ratio: currentRatio.toFixed(2), vcpus: vcpus, cores: cores});
    } else {
        marker.style.display = 'none';
        document.getElementById('ratio-current').innerHTML =
            window.t('results.ratio_no_cores');
    }

    const sizing = document.getElementById('sizing-results');
    sizing.style.display = 'block';
    recalcRecommendations();
    sizing.scrollIntoView({behavior: 'smooth'});
}

// Open the cluster network diagram for a recommendation in a modal (the SVG is
// generated server-side and rides on rec.network_svg).
function openClusterDiagram(mode, recIndex) {
    const rec = (lastRecommendations[mode] || [])[recIndex];
    if (!rec || !rec.network_svg) return;
    const nodes = rec.node_count;
    document.getElementById('diagram-modal-title').textContent =
        window.t('results.diagram_title', {model: rec.model, nodes: nodes});
    const body = document.getElementById('diagram-modal-body');
    body.innerHTML = rec.network_svg;
    body.dataset.filename =
        `SC_Network_${String(rec.model).replace(/[^A-Za-z0-9]+/g, '')}_${nodes}node`;
    document.getElementById('diagram-modal').style.display = 'flex';
}

function closeClusterDiagram() {
    document.getElementById('diagram-modal').style.display = 'none';
    document.getElementById('diagram-modal-body').innerHTML = '';
}

// Download the currently-shown diagram as an SVG file.
function downloadClusterDiagram() {
    const body = document.getElementById('diagram-modal-body');
    const svg = body.innerHTML.trim();
    if (!svg) return;
    const blob = new Blob([svg], { type: 'image/svg+xml;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = (body.dataset.filename || 'SC_Network_diagram') + '.svg';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
}

const _EXPORT_ENDPOINTS = {
    pptx: '/api/export-proposal',
    pdf: '/api/export-pdf',
    docx: '/api/export-docx',
    'presentation-pdf': '/api/export-presentation-pdf',
};

async function exportProposal(mode, recIndex, fmt = 'pptx') {
    const recs = lastRecommendations[mode];
    const summary = lastSummary[mode];
    const projection = lastProjection[mode];

    if (!recs || !recs[recIndex] || !summary || !projection) {
        alert(window.t('results.export_missing_data'));
        return;
    }

    const btn = (event.target.closest && event.target.closest('button')) || event.target;
    const origHtml = btn.innerHTML;
    btn.textContent = window.t('results.generating');
    btn.disabled = true;

    try {
        const resp = await fetch(_EXPORT_ENDPOINTS[fmt] || _EXPORT_ENDPOINTS.pptx, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                summary: summary,
                recommendation: recs[recIndex],
                projection: projection,
                source_perf: buildSourcePerfExport(),
            }),
        });

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            alert(err.error || window.t('results.export_failed'));
            return;
        }

        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = resp.headers.get('content-disposition')?.match(/filename="?(.+?)"?$/)?.[1]
            || `SC_Proposal_${recs[recIndex].model}_${recs[recIndex].node_count}N.${fmt}`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    } catch (e) {
        alert(window.t('results.export_failed_detail', {error: e.message}));
    } finally {
        btn.innerHTML = origHtml;
        btn.disabled = false;
    }
}

// ---- Combined multi-site export (one document, all clusters) --------------
const _MULTISITE_ENDPOINTS = {
    pptx: '/api/export-multisite-proposal',
    docx: '/api/export-multisite-docx',
    pdf: '/api/export-multisite-pdf',
    'presentation-pdf': '/api/export-multisite-presentation-pdf',
};

function _optVal(opts, id, dflt) {
    const v = opts ? opts[id] : undefined;
    return v === undefined ? dflt : v;
}

// Build a /api/recommend body from a captured options object (field-id -> value)
// rather than the live DOM, so clusters not currently in view can be sized.
function _recommendBodyFromOpts(summary, opts) {
    const targetNodes = _optVal(opts, 'target-nodes', '');
    return {
        summary,
        vcpu_ratio: parseFloat(_optVal(opts, 'ratio-slider', DEFAULT_SIZING_RATIO)),
        years: parseInt(_optVal(opts, 'growth-years', 5), 10),
        growth_pct: parseFloat(_optVal(opts, 'growth-pct', 10)),
        snapshot_pct: parseFloat(_optVal(opts, 'snapshot-pct', 20)),
        target_nodes: targetNodes ? parseInt(targetNodes, 10) : null,
        storage_pref: _optVal(opts, 'storage-pref', 'auto'),
        size_full_cluster: !!_optVal(opts, 'size-full-cluster', false),
        sizing_mode: _optVal(opts, 'sizing-mode', 'certified'),
        allow_storage_only: !!_optVal(opts, 'allow-storage-only', false),
        target_model: _optVal(opts, 'sizing-model-select', '') || null,
        include_eol_eos: !!_optVal(opts, 'sizing-include-eol', false),
        max_day_one_storage_pct: parseFloat(_optVal(opts, 'max-day-one-storage', 100)),
        max_day_one_ram_pct: parseFloat(_optVal(opts, 'max-day-one-ram', 100)),
        source_perf_index: null,
        source_perf_type: 'specrate',
    };
}

// Size any cluster the user hasn't opened yet, so the combined export covers
// all of them. The active cluster's options are captured first so its latest
// tuning is used.
async function ensureAllClusterResults() {
    if (separateClusters) clusterOptions[activeCluster] = _captureFields('import');
    for (const c of sourceClusters) {
        const cached = clusterResults[c.name];
        if (cached && cached.recommendations && cached.recommendations.length) continue;
        const summary = computeAdjustedImportSummary(c.name);
        const opts = clusterOptions[c.name] || clusterOptions[COMBINED_KEY];
        const body = _recommendBodyFromOpts(summary, opts);
        body.replication_reserve = inboundReserveFor(c.name);
        body.replication_compute_mode = (clusterReplication[c.name] || {}).mode || 'reserved';
        body.allow_single_node = !!(clusterReplication[c.name] || {}).singleNode;
        const resp = await fetch('/api/recommend', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
        });
        const data = await resp.json();
        clusterResults[c.name] = {
            recommendations: data.recommendations || [],
            projection: data.projection,
            perfSource: data.perf_comparison || null,
            summary,
        };
    }
}

// Export one combined document covering every source cluster (each uses its
// top recommendation). Only meaningful in separate-clusters mode.
async function exportMultisite(fmt = 'pptx') {
    if (!separateClusters) return;
    const btn = (event && event.target.closest && event.target.closest('button')) || (event && event.target);
    const origHtml = btn && btn.innerHTML;
    if (btn) { btn.textContent = window.t('results.generating'); btn.disabled = true; }
    try {
        await ensureAllClusterResults();
        const payloadClusters = sourceClusters.map(c => {
            const res = clusterResults[c.name];
            if (!res || !res.recommendations || !res.recommendations.length) return null;
            // Use the cluster's chosen recommendation (defaults to #1), clamped
            // in case a re-size shortened the list.
            const sel = Math.min(clusterSelectedRec[c.name] ?? 0, res.recommendations.length - 1);
            return {
                name: c.name,
                summary: res.summary,
                recommendation: res.recommendations[sel],
                projection: res.projection,
                source_perf: null,
            };
        }).filter(Boolean);

        if (!payloadClusters.length) {
            alert(window.t('results.export_missing_data'));
            return;
        }

        const resp = await fetch(_MULTISITE_ENDPOINTS[fmt] || _MULTISITE_ENDPOINTS.pptx, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ clusters: payloadClusters }),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            alert(err.error || window.t('results.export_failed'));
            return;
        }
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = resp.headers.get('content-disposition')?.match(/filename="?(.+?)"?$/)?.[1]
            || `SC_Proposal_MultiSite.${fmt}`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    } catch (e) {
        alert(window.t('results.export_failed_detail', {error: e.message}));
    } finally {
        if (btn) { btn.innerHTML = origHtml; btn.disabled = false; }
    }
}

async function exportConfig(fmt = 'pptx') {
    if (!lastConfigResult) {
        alert(window.t('results.no_config_to_export'));
        return;
    }

    const endpoint = fmt === 'pdf' ? '/api/export-config-pdf' : '/api/export-config';
    const btn = (event && event.target.closest && event.target.closest('button'))
        || document.getElementById('config-export-btn');
    const origHtml = btn.innerHTML;
    btn.textContent = window.t('results.generating');
    btn.disabled = true;

    try {
        const resp = await fetch(endpoint, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(lastConfigResult),
        });

        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            alert(err.error || window.t('results.export_failed'));
            return;
        }

        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = resp.headers.get('content-disposition')?.match(/filename="?(.+?)"?$/)?.[1]
            || `SC_Config_${lastConfigResult.node_count}N.${fmt}`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    } catch (e) {
        alert(window.t('results.export_failed_detail', {error: e.message}));
    } finally {
        btn.innerHTML = origHtml;
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
    // Open the modal on the tab matching the cluster being sized; on the review
    // tab (no single active cluster) default to All VMs.
    if (separateClusters) {
        vmModalCluster = (activeCluster === SELECTED_KEY || activeCluster === COMBINED_KEY)
            ? COMBINED_KEY : activeCluster;
    }
    renderClusterTabs();
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
    // In separate-clusters mode the modal shows one cluster's VMs per tab
    // (COMBINED_KEY = all). Indices are preserved into the full importVms list.
    const modalKey = separateClusters ? vmModalCluster : COMBINED_KEY;
    const sorted = importVms
        .map((vm, i) => ({ ...vm, _idx: i }))
        .filter(vm => vmInCluster(vm, modalKey));
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
        const powerLabel = vm.powered_on ? window.t('import.power_on') : window.t('import.power_off');
        const edited = vmConfig[vm._idx] && Object.keys(vmConfig[vm._idx]).length > 0;
        const isAdded = vmAdded.has(vm._idx);
        const isRemoved = vmRemoved.has(vm._idx);
        const rowClass = [excluded ? 'vm-excluded' : '', edited ? 'vm-edited' : '',
                          isAdded ? 'vm-added' : '', isRemoved ? 'vm-removed' : '']
                         .filter(Boolean).join(' ');
        const model = vmVal(vm, vm._idx, 'model') || '';
        const cores = vmVal(vm, vm._idx, 'vcpus');
        const ram = vmVal(vm, vm._idx, 'provisioned_memory_gb');
        const stor = vmVal(vm, vm._idx, 'vdisk_used_gb');
        const name = vmVal(vm, vm._idx, 'name') || '';
        const nameCell = isAdded
            ? `<span class="vm-name-edit"><input type="text" class="vm-edit vm-edit-text" value="${esc(name)}" data-change='["setVmConfig",${vm._idx},"name","$value"]'><span class="vm-tag">${window.t('import.vm_tag_new')}</span></span>`
            : `<span title="${esc(name)}">${esc(name)}</span>`;
        const storCell = isAdded
            ? `<input type="number" class="vm-edit vm-edit-num" min="0" step="1" value="${stor}" data-change='["setVmConfig",${vm._idx},"vdisk_used_gb","$value"]'>`
            : `${(stor || 0).toFixed(1)}`;
        const action = isRemoved
            ? `<button class="vm-action-btn vm-restore" title="${window.t('import.restore_vm_title')}" data-click='["restoreVm",${vm._idx}]'>↺</button>`
            : `<button class="vm-action-btn vm-remove" title="${window.t('import.remove_vm_title')}" data-click='["removeVm",${vm._idx}]'>&times;</button>`;
        return `<tr class="${rowClass}" data-idx="${vm._idx}" data-power="${vm.powered_on ? 'on' : 'off'}">
            <td class="vm-col-check"><input type="checkbox" ${compChecked} data-change='["toggleVmExclusion",${vm._idx},"compute","$checked"]'></td>
            <td class="vm-col-check"><input type="checkbox" ${storChecked} data-change='["toggleVmExclusion",${vm._idx},"storage","$checked"]'></td>
            <td class="vm-col-name">${nameCell}</td>
            <td class="vm-col-model"><input type="text" class="vm-edit vm-edit-text" value="${esc(model)}" data-change='["setVmConfig",${vm._idx},"model","$value"]'></td>
            <td class="vm-col-power"><span class="${powerClass}">${powerLabel}</span></td>
            <td class="vm-col-num"><input type="number" class="vm-edit vm-edit-num" min="1" step="1" value="${cores}" data-change='["setVmConfig",${vm._idx},"vcpus","$value"]'></td>
            <td class="vm-col-num"><input type="number" class="vm-edit vm-edit-num" min="0" step="0.1" value="${ram}" data-change='["setVmConfig",${vm._idx},"provisioned_memory_gb","$value"]'></td>
            <td class="vm-col-num">${storCell}</td>
            <td class="vm-col-os" title="${esc(vm.os)}">${esc(vm.os)}</td>
            <td class="vm-col-action">${action}</td>
        </tr>`;
    }).join('');

    document.querySelectorAll('.vm-table th.sortable').forEach(th => {
        const arrows = th.querySelector('.sort-arrow');
        if (arrows) arrows.remove();
    });
    const headers = document.querySelectorAll('.vm-table th.sortable');
    headers.forEach(th => {
        const field = JSON.parse(th.getAttribute('data-click') || '[]')[1];
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
        const matchText = !q || name.includes(q) || os.includes(q) || model.includes(q);
        const matchPower = vmPowerFilter === 'all'
            || vmPowerFilter === row.getAttribute('data-power');
        row.classList.toggle('vm-hidden', !(matchText && matchPower));
    });
}

function setVmPowerFilter(val) {
    vmPowerFilter = val;
    filterVmTable();
}

// Append a blank, fully-editable VM (net-new workload not in the import). Its
// compute/RAM fall out of the summary recompute; its storage is added there.
function addVm() {
    const idx = importVms.length;
    // Tag net-new VMs with the cluster whose tab is active so they count toward
    // that cluster's sizing (harmless '' when not sizing separately).
    const cluster = (separateClusters && vmModalCluster !== COMBINED_KEY) ? vmModalCluster : '';
    importVms.push({
        name: window.t('import.new_vm_name'), powered_on: true, is_template: false, os: '', model: '',
        vcpus: 2, provisioned_memory_gb: 4, consumed_memory_gb: 0, used_memory_gb: 0,
        disk_capacity_gb: 0, disk_used_gb: 0, vdisk_size_gb: 0, vdisk_used_gb: 0,
        cluster: cluster,
    });
    vmAdded.add(idx);
    renderVmTable();
    filterVmTable();
    updateVmExclusionSummary();
    const row = document.querySelector(`#vm-table-body tr[data-idx="${idx}"]`);
    if (row) {
        row.scrollIntoView({ block: 'center' });
        const input = row.querySelector('input.vm-edit-text');
        if (input) input.select();
    }
}

// Remove a VM from the dataset. Reuses the exclusion math (drop from every total)
// and marks it so the row can be struck through and restored.
function removeVm(idx) {
    vmRemoved.add(idx);
    vmExclusions.compute.add(idx);
    vmExclusions.storage.add(idx);
    renderVmTable();
    filterVmTable();
    updateVmExclusionSummary();
}

function restoreVm(idx) {
    vmRemoved.delete(idx);
    vmExclusions.compute.delete(idx);
    vmExclusions.storage.delete(idx);
    renderVmTable();
    filterVmTable();
    updateVmExclusionSummary();
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
    } else if (field === 'provisioned_memory_gb' || field === 'vdisk_used_gb') {
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
        let label = window.t('import.excl_compute', {count: vmExclusions.compute.size});
        if (compActive > 0) label += window.t('import.excl_compute_detail', {cores: compVcpus, ram: compRam.toFixed(1)});
        if (compActive < vmExclusions.compute.size) label += window.t('import.excl_already_off', {count: vmExclusions.compute.size - compActive});
        parts.push(label);
    }
    if (vmExclusions.storage.size > 0) {
        parts.push(window.t('import.excl_storage', {count: vmExclusions.storage.size, tb: (storGb / 1024).toFixed(2)}));
    }
    if (editedCount > 0) {
        parts.push(window.t('import.excl_edited', {count: editedCount}));
    }
    if (parts.length === 0) {
        el.textContent = window.t('import.excl_no_changes');
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
// Rebuild a working summary from the pristine base. With no clusterName (or
// COMBINED_KEY) this is the whole dataset against originalImportSummary — the
// original single-cluster behavior. With a source-cluster name it uses that
// cluster's base summary and only its VMs, so each cluster sizes independently.
function computeAdjustedImportSummary(clusterName) {
    const scoped = clusterName && clusterName !== COMBINED_KEY;
    const base = scoped ? clusterBase[clusterName] : originalImportSummary;
    const adjusted = JSON.parse(JSON.stringify(base));

    // Recompute compute/RAM totals straight from the (possibly user-edited) VM
    // list rather than subtracting from the original, so per-VM Cores/RAM edits
    // flow through. With no edits or exclusions this reproduces the import totals.
    let sumVcpus = 0, sumProvRam = 0, sumUsedRam = 0, activeIncluded = 0;
    let maxVmRam = 0, maxVmCores = 0;
    importVms.forEach((vm, i) => {
        if (!vmInCluster(vm, clusterName)) return;
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

    // Excluded/removed IMPORTED VMs are subtracted from the measured datastore
    // totals (added VMs aren't in those totals, so they're skipped here).
    let exclStorGbAll = 0, exclProvStorGbActive = 0, exclStorGbActive = 0;
    vmExclusions.storage.forEach(i => {
        if (vmAdded.has(i)) return;
        const vm = importVms[i];
        if (!vmInCluster(vm, clusterName)) return;
        exclStorGbAll += vm.vdisk_used_gb;
        if (vm.powered_on && !vm.is_template) {
            exclStorGbActive += vm.vdisk_used_gb;
            exclProvStorGbActive += vm.vdisk_size_gb;
        }
    });

    // Added VMs contribute net-new storage (unless removed or storage-excluded).
    // A new VM is given a single storage figure, used for both used and provisioned.
    let addStorGbAll = 0, addProvStorGbActive = 0, addStorGbActive = 0;
    vmAdded.forEach(i => {
        if (vmRemoved.has(i) || vmExclusions.storage.has(i)) return;
        const vm = importVms[i];
        if (!vmInCluster(vm, clusterName)) return;
        const used = vmVal(vm, i, 'vdisk_used_gb') || 0;
        addStorGbAll += used;
        if (vm.powered_on && !vm.is_template) {
            addStorGbActive += used;
            addProvStorGbActive += used;
        }
    });

    adjusted.datastore_used_tb = Math.round((base.datastore_used_tb - exclStorGbAll / 1024 + addStorGbAll / 1024) * 100) / 100;
    adjusted.total_vm_provisioned_storage_gb = Math.round((base.total_vm_provisioned_storage_gb - exclProvStorGbActive + addProvStorGbActive) * 10) / 10;
    adjusted.total_vm_provisioned_storage_tb = Math.round(adjusted.total_vm_provisioned_storage_gb / 1024 * 100) / 100;
    adjusted.total_vm_used_storage_gb = Math.round(((base.total_vm_used_storage_gb || 0) - exclStorGbActive + addStorGbActive) * 10) / 10;
    adjusted.total_vm_used_storage_tb = Math.round(adjusted.total_vm_used_storage_gb / 1024 * 100) / 100;

    if (adjusted.datastore_used_tb < 0) adjusted.datastore_used_tb = 0;

    // Per-host local storage (ISOs/templates etc.) — added to the cluster basis
    // only when the user opts in via the checkbox.
    if (includeLocalStorage) {
        adjusted.datastore_used_tb = Math.round((adjusted.datastore_used_tb + (base.local_used_tb || 0)) * 100) / 100;
        adjusted.datastore_total_tb = Math.round(((adjusted.datastore_total_tb || 0) + (base.local_total_tb || 0)) * 100) / 100;
    }

    return adjusted;
}

function applyVmExclusions() {
    if (!originalImportSummary) return;
    const key = activeClusterKey();
    importSummary = computeAdjustedImportSummary(key === COMBINED_KEY ? null : key);
    updateExclusionCountBadge();
    renderClusterTabs();  // per-tab counts may have shifted
    displayImportResults({ summary: importSummary, recommendations: [], projection: lastProjection['import'] });
    // displayImportResults resets the ratio slider to default; in separate mode
    // re-apply the active cluster's own saved options so its tuning survives.
    if (separateClusters && clusterOptions[activeCluster]) {
        const opts = clusterOptions[activeCluster];
        Object.keys(opts).forEach(id => _writeField(id, opts[id]));
        updateRatioDisplay();
    }
    recalcRecommendations();
    closeVmExclusionModal();
}

// Off-by-default toggle to fold per-host local storage into the sizing basis.
function toggleLocalStorage() {
    const cb = document.getElementById('include-local-cb');
    includeLocalStorage = cb ? cb.checked : false;
    if (!originalImportSummary) return;
    const key = activeClusterKey();
    importSummary = computeAdjustedImportSummary(key === COMBINED_KEY ? null : key);
    displayImportResults({ summary: importSummary, recommendations: [], projection: lastProjection['import'] });
    recalcRecommendations();
}

// ==================== MULTI-SITE (SOURCE CLUSTER) CONTROL ====================
// Everything here is inert unless the import held >1 source cluster AND the
// user enabled "size each cluster separately". Tab bars are index-driven (the
// tab key arrays below) so arbitrary cluster names can't break click handlers.

let _recTabKeys = [];
let _modalTabKeys = [];

// The cluster whose summary/results are currently in view (COMBINED_KEY when
// not sizing separately).
function activeClusterKey() {
    return separateClusters ? activeCluster : COMBINED_KEY;
}

function clusterDisplayName(key) {
    if (key === COMBINED_KEY) return window.t('cluster.combined');
    if (key === SELECTED_KEY) return window.t('cluster.selected_tab');
    if (key === UNCLUSTERED_KEY) return window.t('cluster.unclustered');
    return key;
}

// Toggle the per-cluster editing sections (env/workload cards, sizing options,
// growth, recommendation list) off in favour of the "Selected clusters" review
// panel — and back.
function setClusterReviewMode(on) {
    document.querySelectorAll('.import-workload, .ratio-control, .growth-control, #primary-recommendations')
        .forEach(el => { el.style.display = on ? 'none' : ''; });
    const env = document.getElementById('env-summary');
    if (env) env.style.display = on ? 'none' : '';
    const review = document.getElementById('cluster-review');
    if (review) review.style.display = on ? 'block' : 'none';
}

// The "Selected clusters" review tab: a summary of each source cluster's chosen
// recommendation, with the combined-export buttons. Sizes any not-yet-viewed
// cluster so the review (and export) is complete.
async function renderSelectedClustersTab() {
    const review = document.getElementById('cluster-review');
    if (!review) return;
    review.innerHTML = `<div class="review-loading">${window.t('cluster.review_loading')}</div>`;
    await ensureAllClusterResults();
    if (activeCluster !== SELECTED_KEY) return;  // user tabbed away while sizing

    // One full recommendation card per cluster (the selected one) — a
    // screenshot-ready view of the finished multi-site solution.
    const blocks = sourceClusters.map(c => {
        const res = clusterResults[c.name];
        if (!res || !res.recommendations || !res.recommendations.length) {
            return `<div class="review-cluster-block">
                <h4 class="review-cluster-title">${esc(c.name)}</h4>
                <div class="review-none">${window.t('cluster.review_no_rec')}</div>
            </div>`;
        }
        const sel = Math.min(clusterSelectedRec[c.name] ?? 0, res.recommendations.length - 1);
        const r = res.recommendations[sel];
        const demand = (res.projection || {}).iops_demand || null;
        return `<div class="review-cluster-block">
            <h4 class="review-cluster-title">${esc(c.name)} <span class="review-cluster-rank">${window.t('cluster.review_selected_rank', {rank: sel + 1})}</span></h4>
            ${recCardHtml(r, sel, 'import', demand, { showPicker: false, footerActions: false })}
        </div>`;
    }).join('');

    review.innerHTML = `
        <div class="review-header">
            <h3>${window.t('cluster.review_title')}</h3>
            <span class="cluster-export-all">
                <span class="cluster-export-label">${window.t('cluster.export_all')}</span>
                <button class="btn btn-sm" data-click='["exportMultisite","pptx"]'>PPTX</button>
                <button class="btn btn-sm" data-click='["exportMultisite","docx"]'>Word</button>
                <button class="btn btn-sm" data-click='["exportMultisite","pdf"]'>PDF</button>
            </span>
        </div>
        <p class="rec-desc">${window.t('cluster.review_desc')}</p>
        ${blocks}`;
}

// Seed cluster state from an import response. Called on every upload.
function initClusters(data) {
    sourceClusters = (data.clusters || []).map(c => ({
        name: c.name, host_count: c.host_count, vm_count: c.vm_count,
    }));
    clusterBase = {};
    (data.clusters || []).forEach(c => {
        clusterBase[c.name] = JSON.parse(JSON.stringify(c.summary));
    });
    separateClusters = false;
    activeCluster = COMBINED_KEY;
    vmModalCluster = COMBINED_KEY;
    clusterOptions = {};
    clusterResults = {};
    clusterReplication = {};
    dedicatedClusters = [];
    drCluster = { enabled: false, computePct: 100, storagePct: 100, mode: 'reserved', allowSingleNode: false };
    const cb = document.getElementById('separate-clusters-cb');
    if (cb) cb.checked = false;
    const toggle = document.getElementById('cluster-separate-toggle');
    if (toggle) toggle.style.display = sourceClusters.length > 1 ? 'inline-flex' : 'none';
    setClusterReviewMode(false);  // clear any leftover review panel from a prior import
    renderClusterTabs();
}

function toggleSeparateClusters(checked) {
    separateClusters = !!checked && sourceClusters.length > 1;
    if (separateClusters) {
        // Seed each cluster's (and the Combined view's) options from the current
        // shared option values, without clobbering any the user already tuned.
        const cur = _captureFields('import');
        [COMBINED_KEY, ...sourceClusters.map(c => c.name)].forEach(k => {
            if (!clusterOptions[k]) clusterOptions[k] = { ...cur };
        });
        activeCluster = sourceClusters[0].name;
        vmModalCluster = activeCluster;
    } else {
        activeCluster = COMBINED_KEY;
        vmModalCluster = COMBINED_KEY;
    }
    renderClusterTabs();
    _selectClusterKey(activeClusterKey(), /*skipSave=*/true);
}

// Recommendation-area tab click (by index into _recTabKeys).
function selectCluster(i) {
    _selectClusterKey(_recTabKeys[i]);
}

// Switch the active cluster: save the current tab's options, restore the
// target's, recompute its summary, re-render its cards, and re-size it.
function _selectClusterKey(key, skipSave) {
    if (!key) return;
    // Capture the outgoing tab's options (unless leaving the review tab, which
    // has no active per-cluster options).
    if (!skipSave && separateClusters && activeCluster !== SELECTED_KEY) {
        clusterOptions[activeCluster] = _captureFields('import');
    }
    activeCluster = key;
    renderClusterTabs();

    if (key === SELECTED_KEY) {
        setClusterReviewMode(true);
        renderSelectedClustersTab();
        return;
    }
    setClusterReviewMode(false);

    importSummary = computeAdjustedImportSummary(key === COMBINED_KEY ? null : key);
    lastSummary['import'] = importSummary;
    renderRatioContext(importSummary, false);
    // Rebuild the cards first (they reset per-summary inputs like p95-iops),
    // then restore this cluster's saved option values so they win.
    renderEnvWorkloadCards(importSummary);
    if (separateClusters) {
        const opts = clusterOptions[key];
        if (opts) Object.keys(opts).forEach(id => _writeField(id, opts[id]));
    }
    updateRatioDisplay();
    renderReplicationOptions();
    renderSourceCpus(importSummary && importSummary.source_cpus).then(() => recalcRecommendations());
}

// "Apply options to all clusters" — copy the active tab's sizing options to
// every other cluster (and the Combined view).
function applyOptionsToAllClusters() {
    if (!separateClusters) return;
    const cur = _captureFields('import');
    [COMBINED_KEY, ...sourceClusters.map(c => c.name)].forEach(k => {
        clusterOptions[k] = { ...cur };
    });
    showUploadStatus(window.t('cluster.applied_all'), false);
}

// Pick which recommendation the active cluster contributes to the combined
// multi-site export, then re-render the cards to reflect the selection.
function selectClusterRec(i) {
    if (!separateClusters || activeCluster === COMBINED_KEY || activeCluster === SELECTED_KEY) return;
    clusterSelectedRec[activeCluster] = i;
    renderRecommendationsTo(lastRecommendations['import'], 'rec-list', 'ratio-slider', 'import', []);
}

// Configure-VMs modal tab click (by index into _modalTabKeys).
function selectVmModalCluster(i) {
    vmModalCluster = _modalTabKeys[i];
    renderClusterTabs();
    renderVmTable();
    filterVmTable();
}

function renderClusterTabs() {
    const bar = document.getElementById('cluster-tabs');
    const modalBar = document.getElementById('vm-cluster-tabs');
    const show = separateClusters && sourceClusters.length > 1;
    if (bar) bar.style.display = show ? 'flex' : 'none';
    if (modalBar) modalBar.style.display = show ? 'flex' : 'none';
    if (!show) return;

    // Recommendation tabs: each source cluster, then the "Selected clusters"
    // review tab (which hosts the combined export).
    _recTabKeys = [...sourceClusters.map(c => c.name), SELECTED_KEY];
    if (bar) {
        const tabs = _recTabKeys.map((k, i) => {
            const c = sourceClusters.find(x => x.name === k);
            const badge = c ? `<span class="cluster-tab-badge">${window.t('cluster.tab_badge', {hosts: c.host_count, vms: c.vm_count})}</span>` : '';
            const cls = 'cluster-tab' + (k === activeCluster ? ' active' : '')
                        + (k === SELECTED_KEY ? ' cluster-tab-review' : '')
                        + (dedicatedClusters.includes(k) ? ' cluster-tab-dedicated' : '');
            return `<button class="${cls}" data-click='["selectCluster",${i}]'>${esc(clusterDisplayName(k))}${badge}</button>`;
        }).join('');
        // Apply-options-to-all + add-dedicated-cluster live in the bar for real
        // cluster tabs; hidden on the review tab.
        const actions = activeCluster === SELECTED_KEY ? '' :
            `<button class="btn btn-sm btn-muted cluster-apply-all" data-click='["applyOptionsToAllClusters"]'
                     data-i18n-title="cluster.apply_all_info"
                     title="Copy this tab's sizing options to every cluster.">${window.t('cluster.apply_all')}</button>
             <button class="btn btn-sm btn-muted" data-click='["addDedicatedCluster"]'
                     data-i18n-title="cluster.add_dedicated_info"
                     title="Add a dedicated DR target that hosts only replicated data.">${window.t('cluster.add_dedicated')}</button>`;
        bar.innerHTML = `<div class="cluster-tab-row">${tabs}</div>
            <div class="cluster-tab-actions">${actions}</div>`;
    }

    // Modal tabs: All (combined) first, then each source cluster.
    _modalTabKeys = [COMBINED_KEY, ...sourceClusters.map(c => c.name)];
    if (modalBar) {
        modalBar.innerHTML = _modalTabKeys.map((k, i) => {
            const cls = 'cluster-tab' + (k === vmModalCluster ? ' active' : '');
            const label = k === COMBINED_KEY ? window.t('cluster.all_vms') : clusterDisplayName(k);
            return `<button class="${cls}" data-click='["selectVmModalCluster",${i}]'>${esc(label)}</button>`;
        }).join('');
    }
}

// ---- Replication topology (per-cluster) -----------------------------------

function _repCfg(name) {
    if (!clusterReplication[name]) {
        clusterReplication[name] = { target: '', computePct: 100, storagePct: 100, mode: 'reserved' };
    }
    return clusterReplication[name];
}

// Inbound replication reserve a target cluster must host = Σ over sources that
// replicate to it of (source's current demand × that source's compute/storage %).
function inboundReserveFor(targetName) {
    let vcpus = 0, ram = 0, storage = 0;
    for (const src of sourceClusters) {
        const rep = clusterReplication[src.name];
        if (!rep || rep.target !== targetName) continue;
        const s = computeAdjustedImportSummary(src.name);
        vcpus += (s.total_vcpus || 0) * (rep.computePct || 0) / 100;
        ram += (s.total_vm_provisioned_memory_gb || 0) * (rep.computePct || 0) / 100;
        storage += (s.datastore_used_tb || 0) * (rep.storagePct || 0) / 100;
    }
    return { vcpus, ram_gb: ram, storage_tb: storage };
}

// Render the replication config for the active cluster into #replication-options
// (shown only when sizing clusters separately, on a real/dedicated cluster tab).
function renderReplicationOptions() {
    const el = document.getElementById('replication-options');
    if (!el) return;
    const onRealTab = separateClusters && activeCluster !== COMBINED_KEY
        && activeCluster !== SELECTED_KEY && sourceClusters.length > 1;
    if (!onRealTab) { el.style.display = 'none'; el.innerHTML = ''; return; }
    el.style.display = 'block';

    const cfg = _repCfg(activeCluster);
    const isDedicated = dedicatedClusters.includes(activeCluster);
    // Target options: every other cluster (source or dedicated).
    const targetOpts = ['<option value="">' + esc(window.t('cluster.rep_target_none')) + '</option>']
        .concat(sourceClusters.filter(c => c.name !== activeCluster).map(c =>
            `<option value="${esc(c.name)}" ${cfg.target === c.name ? 'selected' : ''}>${esc(clusterDisplayName(c.name))}</option>`))
        .join('');

    const inbound = inboundReserveFor(activeCluster);
    const hasInbound = inbound.vcpus > 0 || inbound.ram_gb > 0 || inbound.storage_tb > 0;
    const inboundNote = hasInbound
        ? `<div class="rep-inbound">${window.t('cluster.rep_inbound', {
              vcpus: Math.round(inbound.vcpus),
              ram: formatRam(Math.round(inbound.ram_gb)),
              storage: Math.round(inbound.storage_tb * 10) / 10})}</div>`
        : `<div class="rep-inbound rep-inbound-none">${window.t('cluster.rep_inbound_none')}</div>`;

    const removeBtn = isDedicated
        ? `<button class="btn btn-sm btn-muted rep-remove" data-click='["removeDedicatedCluster"]'>${window.t('cluster.remove_dedicated')}</button>`
        : '';

    el.innerHTML = `
        <div class="rep-head"><h4>${window.t('cluster.rep_title')}</h4>${removeBtn}</div>
        <div class="rep-grid">
            <div class="form-group">
                <label>${window.t('cluster.rep_target')}</label>
                <select id="rep-target" data-change='["setReplicationTarget","$value"]'>${targetOpts}</select>
            </div>
            <div class="form-group">
                <label>${window.t('cluster.rep_compute_pct')}</label>
                <input type="number" id="rep-compute" min="0" max="100" step="1" value="${cfg.computePct}"
                       ${cfg.target ? '' : 'disabled'} data-change='["setReplicationPct","compute","$value"]'>
            </div>
            <div class="form-group">
                <label>${window.t('cluster.rep_storage_pct')}</label>
                <input type="number" id="rep-storage" min="0" max="100" step="1" value="${cfg.storagePct}"
                       ${cfg.target ? '' : 'disabled'} data-change='["setReplicationPct","storage","$value"]'>
            </div>
            <div class="form-group">
                <label>${window.t('cluster.rep_mode')}
                    <span class="info-icon" tabindex="0" data-i18n-title="cluster.rep_mode_info"
                          title="Applies to replication compute (CPU and RAM). Reserved holds it at N-1 (always available). Failover-only sizes it against the full cluster (replicas run only on failover) — smaller target. Storage is always held.">i</span>
                </label>
                <select id="rep-mode" data-change='["setReplicationMode","$value"]'>
                    <option value="reserved" ${cfg.mode !== 'failover' ? 'selected' : ''}>${window.t('cluster.rep_mode_reserved')}</option>
                    <option value="failover" ${cfg.mode === 'failover' ? 'selected' : ''}>${window.t('cluster.rep_mode_failover')}</option>
                </select>
            </div>
        </div>
        ${isDedicated ? `<div class="toggle-item">
            <label class="checkbox-inline">
                <input type="checkbox" id="rep-single-node" ${cfg.singleNode ? 'checked' : ''} data-change='["setReplicationSingleNode","$checked"]'>
                <span>${window.t('cluster.allow_single_node')}</span>
            </label>
            <span class="info-icon" tabindex="0" data-i18n-title="cluster.allow_single_node_info"
                  title="Allow a single-node DR target (no failover). A DR cluster is already a redundancy tier, so a single larger-disk node can be a valid, lower-cost target.">i</span>
        </div>` : ''}
        ${inboundNote}`;
}

function setReplicationSingleNode(checked) {
    _repCfg(activeCluster).singleNode = !!checked;
    recalcRecommendations();
}

function setReplicationTarget(value) {
    const cfg = _repCfg(activeCluster);
    cfg.target = value || '';
    renderReplicationOptions();  // enable/disable %, refresh inbound notes elsewhere
    recalcRecommendations();
}

function setReplicationPct(which, value) {
    const cfg = _repCfg(activeCluster);
    const v = Math.max(0, Math.min(100, Math.round(parseFloat(value) || 0)));
    if (which === 'compute') cfg.computePct = v; else cfg.storagePct = v;
    recalcRecommendations();
}

function setReplicationMode(value) {
    _repCfg(activeCluster).mode = (value === 'failover') ? 'failover' : 'reserved';
    recalcRecommendations();  // mode affects THIS cluster's inbound sizing
}

// Add a dedicated DR target cluster (no own workload) that other clusters can
// replicate to. It gets its own tab and is sized purely from inbound replicas.
function addDedicatedCluster() {
    if (!separateClusters || !originalImportSummary) return;
    let n = dedicatedClusters.length + 1;
    let name = window.t('cluster.dedicated_name', {n});
    const existing = new Set(sourceClusters.map(c => c.name));
    while (existing.has(name)) { n++; name = window.t('cluster.dedicated_name', {n}); }

    const base = JSON.parse(JSON.stringify(originalImportSummary));
    ['total_vcpus', 'total_vm_provisioned_memory_gb', 'total_vm_used_memory_gb',
     'datastore_used_tb', 'datastore_total_tb', 'total_vm_provisioned_storage_gb',
     'total_vm_provisioned_storage_tb', 'total_vm_used_storage_gb', 'total_vm_used_storage_tb',
     'active_vms', 'total_vms', 'host_count', 'total_host_cores', 'total_host_threads',
     'total_host_ghz', 'total_host_ram_gb', 'peak_cpu_ghz', 'peak_cpu_pct', 'avg_cpu_pct',
     'peak_mem_pct', 'avg_mem_pct', 'total_peak_iops', 'total_avg_iops', 'p95_iops',
     'max_vm_ram_gb', 'max_vm_cores', 'local_used_tb', 'local_total_tb', 'local_used_gb',
    ].forEach(k => { if (k in base) base[k] = 0; });
    base.cluster_name = name;
    base.current_platform = window.t('cluster.dedicated_platform');
    base.source_cpus = [];

    clusterBase[name] = base;
    sourceClusters.push({ name, host_count: 0, vm_count: 0 });
    dedicatedClusters.push(name);
    clusterOptions[name] = { ...(clusterOptions[activeCluster] || clusterOptions[COMBINED_KEY] || _captureFields('import')) };
    _selectClusterKey(name);  // switch to the new tab
}

function removeDedicatedCluster() {
    if (!dedicatedClusters.includes(activeCluster)) return;
    const name = activeCluster;
    dedicatedClusters = dedicatedClusters.filter(n => n !== name);
    sourceClusters = sourceClusters.filter(c => c.name !== name);
    delete clusterBase[name];
    delete clusterOptions[name];
    delete clusterResults[name];
    delete clusterReplication[name];
    // Clear any cluster that was replicating to the removed target.
    Object.values(clusterReplication).forEach(cfg => { if (cfg.target === name) cfg.target = ''; });
    activeCluster = sourceClusters.length ? sourceClusters[0].name : COMBINED_KEY;
    _selectClusterKey(activeCluster, /*skipSave=*/true);
}

// ---- Single/combined-workload DR cluster ----------------------------------
// A replication target for the whole workload, available when NOT sizing each
// cluster separately (in separate mode the per-cluster replication UI is used).

function renderDrClusterOption() {
    const el = document.getElementById('dr-cluster-option');
    if (!el) return;
    const show = !separateClusters && (activeMode === 'import' || activeMode === 'manual');
    if (!show) { el.style.display = 'none'; el.innerHTML = ''; return; }
    el.style.display = 'block';
    const on = drCluster.enabled;
    el.innerHTML = `
        <div class="rep-head">
            <label class="checkbox-inline">
                <input type="checkbox" id="dr-enable" ${on ? 'checked' : ''} data-change='["toggleDrCluster","$checked"]'>
                <span>${window.t('cluster.dr_enable')}</span>
            </label>
            <span class="info-icon" tabindex="0" data-i18n-title="cluster.dr_info"
                  title="Add a replication (DR) target sized to host this workload's replica. Compute (CPU + RAM) and storage reserves are set separately; storage always includes the snapshot reserve.">i</span>
        </div>
        ${on ? `<div class="rep-grid">
            <div class="form-group">
                <label>${window.t('cluster.rep_compute_pct')}</label>
                <input type="number" id="dr-compute" min="0" max="100" step="1" value="${drCluster.computePct}" data-change='["setDrPct","compute","$value"]'>
            </div>
            <div class="form-group">
                <label>${window.t('cluster.rep_storage_pct')}</label>
                <input type="number" id="dr-storage" min="0" max="100" step="1" value="${drCluster.storagePct}" data-change='["setDrPct","storage","$value"]'>
            </div>
            <div class="form-group">
                <label>${window.t('cluster.rep_mode')}
                    <span class="info-icon" tabindex="0" data-i18n-title="cluster.rep_mode_info"
                          title="Applies to replication compute (CPU and RAM). Reserved holds it at N-1; Failover-only sizes it against the full cluster. Storage is always held.">i</span>
                </label>
                <select id="dr-mode" data-change='["setDrMode","$value"]'>
                    <option value="reserved" ${drCluster.mode !== 'failover' ? 'selected' : ''}>${window.t('cluster.rep_mode_reserved')}</option>
                    <option value="failover" ${drCluster.mode === 'failover' ? 'selected' : ''}>${window.t('cluster.rep_mode_failover')}</option>
                </select>
            </div>
        </div>
        <div class="toggle-item">
            <label class="checkbox-inline">
                <input type="checkbox" id="dr-single-node" ${drCluster.allowSingleNode ? 'checked' : ''} data-change='["toggleDrSingleNode","$checked"]'>
                <span>${window.t('cluster.allow_single_node')}</span>
            </label>
            <span class="info-icon" tabindex="0" data-i18n-title="cluster.allow_single_node_info"
                  title="Allow a single-node DR target (no failover). A DR cluster is already a redundancy tier, so a single larger-disk node can be a valid, lower-cost target.">i</span>
        </div>` : ''}`;
}

function toggleDrSingleNode(checked) {
    drCluster.allowSingleNode = !!checked;
    recalcRecommendations();
}

function toggleDrCluster(checked) {
    drCluster.enabled = !!checked;
    if (!drCluster.enabled) drTab = 'primary';
    renderDrClusterOption();
    recalcRecommendations();
}

// Primary / Replication-DR tab bar (single-workload mode). Swaps which
// recommendation list is shown; leaves the shared env/workload/options above.
function renderDrTabs() {
    const tabs = document.getElementById('dr-tabs');
    const primary = document.getElementById('primary-recommendations');
    const drSec = document.getElementById('dr-recommendations');
    // Separate mode owns .import-recommendations (via the review tab); only hide
    // the DR-specific bits here and let that path manage the primary list.
    if (separateClusters) {
        if (tabs) tabs.style.display = 'none';
        if (drSec) drSec.style.display = 'none';
        return;
    }
    const active = drCluster.enabled && (activeMode === 'import' || activeMode === 'manual');
    if (!active) {
        if (tabs) tabs.style.display = 'none';
        if (drSec) drSec.style.display = 'none';
        if (primary) primary.style.display = '';
        return;
    }
    if (tabs) {
        tabs.style.display = 'flex';
        tabs.innerHTML = `<div class="cluster-tab-row">
            <button class="cluster-tab ${drTab !== 'dr' ? 'active' : ''}" data-click='["selectDrTab","primary"]'>${window.t('cluster.dr_tab_primary')}</button>
            <button class="cluster-tab ${drTab === 'dr' ? 'active' : ''}" data-click='["selectDrTab","dr"]'>${window.t('cluster.dr_tab_dr')}</button>
        </div>`;
    }
    const showPrimary = drTab !== 'dr';
    if (primary) primary.style.display = showPrimary ? '' : 'none';
    if (drSec) drSec.style.display = showPrimary ? 'none' : 'block';
}

function selectDrTab(which) {
    drTab = (which === 'dr') ? 'dr' : 'primary';
    renderDrTabs();
}
function setDrPct(which, value) {
    const v = Math.max(0, Math.min(100, Math.round(parseFloat(value) || 0)));
    if (which === 'compute') drCluster.computePct = v; else drCluster.storagePct = v;
    recalcRecommendations();
}
function setDrMode(value) {
    drCluster.mode = (value === 'failover') ? 'failover' : 'reserved';
    recalcRecommendations();
}

// A zeroed sizing summary (no own workload) derived from a primary summary,
// keeping the largest-VM constraints so DR nodes can host the biggest replica.
function makeZeroBaseFrom(summary) {
    const b = JSON.parse(JSON.stringify(summary));
    ['total_vcpus', 'total_vm_provisioned_memory_gb', 'total_vm_used_memory_gb',
     'datastore_used_tb', 'datastore_total_tb', 'total_vm_provisioned_storage_gb',
     'total_vm_provisioned_storage_tb', 'total_vm_used_storage_gb', 'total_vm_used_storage_tb',
     'active_vms', 'total_vms', 'host_count', 'total_host_cores', 'total_host_threads',
     'total_host_ghz', 'total_host_ram_gb', 'peak_cpu_ghz', 'peak_cpu_pct', 'avg_cpu_pct',
     'peak_mem_pct', 'avg_mem_pct', 'total_peak_iops', 'total_avg_iops', 'p95_iops',
     'local_used_tb', 'local_total_tb', 'local_used_gb',
    ].forEach(k => { if (k in b) b[k] = 0; });
    b.source_cpus = [];
    return b;
}

// Size + render the single-mode DR cluster from the primary summary. Fire-and-
// forget from recalcRecommendations after the primary render.
async function sizeDrCluster(primarySummary) {
    const sec = document.getElementById('dr-recommendations');
    if (!sec) return;
    const active = !separateClusters && drCluster.enabled && primarySummary
        && (activeMode === 'import' || activeMode === 'manual');
    if (!active) { renderDrTabs(); return; }

    const reserve = {
        vcpus: (primarySummary.total_vcpus || 0) * drCluster.computePct / 100,
        ram_gb: (primarySummary.total_vm_provisioned_memory_gb || 0) * drCluster.computePct / 100,
        storage_tb: (primarySummary.datastore_used_tb || 0) * drCluster.storagePct / 100,
    };
    const body = _recommendBodyFromOpts(makeZeroBaseFrom(primarySummary), _captureFields('import'));
    body.replication_reserve = reserve;
    body.replication_compute_mode = drCluster.mode;
    body.allow_single_node = drCluster.allowSingleNode;

    renderDrTabs();  // reveal the tab bar; visibility of the section follows drTab
    const list = document.getElementById('dr-rec-list');
    list.innerHTML = `<div class="review-loading">${window.t('results.generating')}</div>`;
    try {
        const resp = await fetch('/api/recommend', {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
        });
        const data = await resp.json();
        const demand = (data.projection || {}).iops_demand || null;
        const recs = (data.recommendations || []).slice(0, 3);
        list.innerHTML = recs.length
            ? recs.map((r, i) => recCardHtml(r, i, '__dr__', demand, { showPicker: false, footerActions: false })).join('')
            : `<div class="no-recs">${window.t('results.no_matching_configs')}</div>`;
    } catch (e) {
        list.innerHTML = `<div class="rec-warning">${window.t('results.export_failed')}</div>`;
    }
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
                   data-change='["toggleLocalStorage"]'>
            ${window.t('storage.include_local', {gb: localGb.toLocaleString()})}
        </label>`;
}


// ==================== SAVE / RESTORE FULL WORKING STATE ====================
// Captures the entire sizing screen so a signed-in user can reload it later.
// Exposed on window for auth.js (a separate script) to drive.

const SNAPSHOT_VERSION = 2;

// Controls in the shared Sizing Options + Growth block — captured for BOTH the
// import and manual flows (single source of truth, so they stay in lock-step).
const _SHARED_SIZING_FIELDS = ['ratio-slider', 'growth-years', 'growth-pct',
    'snapshot-pct', 'max-day-one-storage', 'max-day-one-ram', 'target-nodes',
    'storage-pref', 'sizing-mode', 'size-full-cluster', 'allow-storage-only',
    'sizing-model-select', 'sizing-include-eol'];

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
    // Single/combined-workload DR cluster (shared across import + manual).
    snap.drCluster = drCluster;

    if (currentMode === 'validated') {
        const tier = document.querySelector('input[name="disk-tier-mode"]:checked');
        snap.tierMode = tier ? tier.value : 'single';
    }

    if (currentMode === 'import') {
        if (!originalImportSummary) return null;  // nothing imported yet
        // Persist the active cluster's latest option values before snapshotting.
        if (separateClusters) clusterOptions[activeCluster] = _captureFields('import');
        snap.import = {
            originalImportSummary,
            importSummary,
            importVms,
            vmConfig,
            exclCompute: [...vmExclusions.compute],
            exclStorage: [...vmExclusions.storage],
            includeLocalStorage,
            lastProjection: lastProjection['import'] || null,
            // Multi-site state (absent/ignored for single-cluster imports).
            sourceClusters,
            clusterBase,
            separateClusters,
            clusterOptions,
            clusterSelectedRec,
            clusterReplication,
            dedicatedClusters,
            activeCluster,
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
    drCluster = snap.drCluster || { enabled: false, computePct: 100, storagePct: 100, mode: 'reserved', allowSingleNode: false };
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
        // Restore multi-site state (older v1 snapshots have none → single cluster).
        sourceClusters = im.sourceClusters || [];
        clusterBase = im.clusterBase || {};
        separateClusters = !!im.separateClusters;
        clusterOptions = im.clusterOptions || {};
        clusterSelectedRec = im.clusterSelectedRec || {};
        clusterReplication = im.clusterReplication || {};
        dedicatedClusters = im.dedicatedClusters || [];
        clusterResults = {};
        activeCluster = im.activeCluster || COMBINED_KEY;
        // Restore into a concrete cluster tab, not the review tab (which needs a
        // full re-size pass); the user can reopen it.
        if (activeCluster === SELECTED_KEY) {
            activeCluster = sourceClusters.length ? sourceClusters[0].name : COMBINED_KEY;
        }
        setClusterReviewMode(false);
        vmModalCluster = separateClusters ? activeCluster : COMBINED_KEY;
        const sepCb = document.getElementById('separate-clusters-cb');
        if (sepCb) sepCb.checked = separateClusters;
        const sepToggle = document.getElementById('cluster-separate-toggle');
        if (sepToggle) sepToggle.style.display = sourceClusters.length > 1 ? 'inline-flex' : 'none';
        const key = activeClusterKey();
        importSummary = computeAdjustedImportSummary(key === COMBINED_KEY ? null : key);
        updateExclusionCountBadge();
        renderClusterTabs();
        showUploadStatus(window.t('upload.restored'), false);
        // Re-render the env/workload cards from the adjusted summary, then re-apply
        // the saved options and recompute recommendations.
        displayImportResults({ summary: importSummary, recommendations: [], projection: lastProjection['import'] });
        (SNAP_FIELDS.import).forEach(id => _writeField(id, f[id]));
        // In separate mode the active cluster's own options override the shared fields.
        if (separateClusters && clusterOptions[activeCluster]) {
            Object.keys(clusterOptions[activeCluster]).forEach(id => _writeField(id, clusterOptions[activeCluster][id]));
        }
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
    // This is the real first successful catalog load under the login gate, so
    // seed the tier defaults once the disk-size catalog is in.
    loadValidatedNics().then(initDiskTiers);
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
