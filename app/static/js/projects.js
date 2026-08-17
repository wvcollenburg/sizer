// Project screens — the container a customer engagement's sizings live in.
//
// Sizing always happens inside a project (docs/projects-plan.md decision 2), so
// this sits in front of the sizer: the home screen picks or creates one, the
// project view lists its sizings, and only then does the existing sizer appear.
// "Quick sizing" skips the naming step by landing in the user's scratch project
// rather than by allowing an unfiled sizing.
//
// A pure view layer over the /api/projects and /api/sizings endpoints — it never
// touches the sizing engine or app.js's calculation state, it only decides which
// screen is showing and which project the next save belongs to. State is
// lexical-global (not on window) apart from the handlers the CSP-safe delegate
// looks up by name, matching the wizard's approach.

let currentProject = null;      // the open project's detail payload
let projectList = [];
let selectedSizings = new Set();
let activeTagFilter = null;
let panelSizing = null;         // the sizing whose panel is open

const escHtml = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));

function tt(key, vars) {
    return (window.t ? window.t(key, vars) : key);
}

async function api(url, opts) {
    const res = await fetch(url, Object.assign({ credentials: 'same-origin' }, opts || {}));
    let data = null;
    try { data = await res.json(); } catch (e) { /* empty body */ }
    return { ok: res.ok, status: res.status, data };
}

// ── screen switching ────────────────────────────────────────────────────────
// One body class decides what's visible: the sizer is hidden while a project
// screen shows, and vice versa. Keeps the existing markup untouched.

function showScreen(which) {
    // The first screen decision ends the boot hold; until now the app body has
    // been hidden so the sizer doesn't flash before the project home replaces it.
    document.body.classList.remove('app-booting');
    document.body.classList.remove('view-projects', 'view-project', 'view-sizer');
    document.body.classList.add('view-' + which);
    const home = document.getElementById('project-home');
    const view = document.getElementById('project-view');
    if (home) home.hidden = which !== 'projects';
    if (view) view.hidden = which !== 'project';
    window.scrollTo(0, 0);
}

// ── history ─────────────────────────────────────────────────────────────────
// Screen changes are JS state, so without this the browser's Back button walks
// out of the app entirely instead of stepping back through it. Each screen gets
// a history entry whose URL can be re-derived on a cold load, which also makes
// a project linkable.
//
//   /                              projects home
//   /?project=<id>                 that project's view
//   /?project=<id>&sizing=<id>     that sizing open in the sizer
//   /?project=<id>&new=1           a blank sizer in that project

function pushScreen(url, state, push) {
    if (push === false) return;                 // replaying history, don't add to it
    if (location.pathname + location.search === url) {
        history.replaceState(state, '', url);
    } else {
        history.pushState(state, '', url);
    }
}

window.addEventListener('popstate', function (e) {
    const state = e.state || {};
    if (state.screen === 'sizer' && state.sizingId) {
        openSizing(state.sizingId, false);
    } else if (state.screen === 'project' && state.projectId) {
        openProject(state.projectId, false, false);
    } else if (state.screen === 'sizer') {
        // An unsaved sizer cannot be restored — its work only ever existed in
        // the page that has since been replaced. Fall back to its project.
        if (state.projectId) openProject(state.projectId, false, false);
        else openProjectsHome(false);
    } else {
        openProjectsHome(false);
    }
});

function openProjectsHome(push) {
    pushScreen('/', { screen: 'projects' }, push);
    showScreen('projects');
    loadProjects();
}

// Resume where the URL says, after a clean-slate reload. Returns true when it
// handled the landing screen, so the caller doesn't also open the project home.
//
//   /?project=<id>        → that project's view
//   /?project=<id>&new=1  → a blank sizer inside that project
async function bootFromUrl() {
    const params = new URLSearchParams(location.search);
    const projectId = parseInt(params.get('project'), 10);
    if (!projectId) return false;
    const blank = params.get('new') === '1';
    const sizingId = parseInt(params.get('sizing'), 10);

    const { ok, data } = await api('/api/projects/' + projectId);
    if (!ok) return false;
    currentProject = data;
    selectedSizings = new Set();
    activeTagFilter = null;

    if (sizingId) {
        await openSizing(sizingId, false);
        return true;
    }
    if (blank) {
        // Drop &new=1 but keep the project, so this entry reads as "the sizer,
        // in this project". Reloading then lands on the project rather than
        // silently re-blanking a sizer the user has since filled in.
        history.replaceState({ screen: 'sizer', projectId: projectId },
                             '', '/?project=' + encodeURIComponent(projectId));
        enterSizer(null);          // no name yet — the bar shows UNSAVED
        return true;
    }
    showScreen('project');
    renderProject();
    refreshStaleSizings(false);
    return true;
}

function backToProjects() {
    currentProject = null;
    openProjectsHome();
}

function backToProject() {
    if (!currentProject) return openProjectsHome();
    openProject(currentProject.id);
}

// ── project home ────────────────────────────────────────────────────────────

async function loadProjects() {
    const { ok, data } = await api('/api/projects/');
    const host = document.getElementById('project-list');
    if (!host) return;
    if (!ok) {
        host.innerHTML = `<p class="project-empty">${escHtml(tt('project.home.load_failed'))}</p>`;
        return;
    }
    projectList = data || [];
    if (!projectList.length) {
        host.innerHTML = `<p class="project-empty">${escHtml(tt('project.home.empty'))}</p>`;
        return;
    }
    host.innerHTML = projectList.map(projectCard).join('');
}

function projectCard(p) {
    const count = p.sizing_count || 0;
    const sub = [
        p.customer_name ? escHtml(p.customer_name) : '',
        tt('project.card.sizings', { count: count }),
        fmtProjectDate(p.updated_at),
    ].filter(Boolean).join(' · ');
    const badge = p.is_scratch
        ? `<span class="project-badge" title="${escHtml(tt('project.card.scratch_hint'))}">${escHtml(tt('project.card.scratch'))}</span>`
        : '';
    const shared = p.source && p.source !== 'owned'
        ? `<span class="project-badge project-badge-shared">${escHtml(tt('project.card.shared'))}</span>` : '';
    return `<button class="project-card" data-click='["openProject",${p.id}]'>
        <span class="project-card-name">${escHtml(p.name)}${badge}${shared}</span>
        <span class="project-card-sub">${sub}</span>
    </button>`;
}

function fmtProjectDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return isNaN(d) ? '' : d.toLocaleDateString();
}

// ── new project ─────────────────────────────────────────────────────────────

function openNewProject() {
    const modal = document.getElementById('new-project-modal');
    const input = document.getElementById('new-project-name');
    hideError('new-project-error');
    if (input) input.value = '';
    if (modal) modal.style.display = 'flex';
    if (input) setTimeout(() => input.focus(), 30);
}

function closeNewProject() {
    const modal = document.getElementById('new-project-modal');
    if (modal) modal.style.display = 'none';
}

function onNewProjectKey(e) {
    if (e && e.key === 'Enter') { e.preventDefault(); submitNewProject(); }
}

async function submitNewProject() {
    const input = document.getElementById('new-project-name');
    const name = (input && input.value || '').trim();
    if (!name) return showError('new-project-error', tt('project.new.name_required'));

    // The project remembers the language it was created in, so an export can
    // later ask which language to use when the session differs (decision 25).
    const { ok, data } = await api('/api/projects/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name, lang: window.I18N_ACTIVE || undefined }),
    });
    if (!ok) return showError('new-project-error', (data && data.error) || tt('auth.generic_error'));
    closeNewProject();
    openProject(data.id);
}

async function openProjectByCodePrompt() {
    const code = window.prompt(tt('project.home.code_prompt'));
    if (!code) return;
    const { ok, data } = await api('/api/projects/code/' + encodeURIComponent(code.trim()));
    if (!ok) {
        return window.showInfoModal
            ? window.showInfoModal(tt('project.home.not_found_title'), (data && data.error) || '')
            : null;
    }
    openProject(data.id);
}

async function startQuickSizing() {
    const { ok, data } = await api('/api/projects/scratch', { method: 'POST' });
    if (!ok) return;
    // Same clean-slate reload as "New sizing" — a quick sizing started after
    // another one must not inherit its import.
    location.href = '/?project=' + encodeURIComponent(data.id) + '&new=1';
}

// ── project view ────────────────────────────────────────────────────────────

async function openProject(projectId, suppressRefresh, push) {
    const { ok, data } = await api('/api/projects/' + projectId);
    if (!ok) return openProjectsHome();
    currentProject = data;
    selectedSizings = new Set();
    activeTagFilter = null;
    pushScreen('/?project=' + encodeURIComponent(projectId),
               { screen: 'project', projectId: projectId }, push);
    showScreen('project');
    renderProject();
    // Bring stale rows current in the background so comparing or exporting
    // doesn't hand back numbers from before the last catalog change.
    if (!suppressRefresh) refreshStaleSizings(false);
}

function renderProject() {
    if (!currentProject) return;
    const nameEl = document.getElementById('project-view-name');
    if (nameEl) nameEl.textContent = currentProject.name;

    const meta = document.getElementById('project-view-meta');
    if (meta) {
        const bits = [];
        if (currentProject.customer_name) bits.push(escHtml(currentProject.customer_name));
        if (currentProject.opportunity_ref) bits.push(escHtml(currentProject.opportunity_ref));
        bits.push(`<code class="sizing-code">${escHtml(currentProject.code)}</code>`);
        if (currentProject.salesforce_url) {
            // Present only for scale users — the server omits the key entirely
            // for everyone else, so this link cannot leak by rendering.
            bits.push(`<a href="${escHtml(currentProject.salesforce_url)}" target="_blank"
                rel="noopener noreferrer" class="sf-link">${escHtml(tt('project.view.salesforce'))}</a>`);
        }
        meta.innerHTML = bits.join(' · ');
    }
    renderTagFilter();
    renderSizingRows();
    renderReplication();
    renderSelectionBar();
    loadExports();
}

function visibleSizings() {
    const rows = (currentProject && currentProject.sizings) || [];
    if (!activeTagFilter) return rows;
    return rows.filter(r => (r.tags || []).some(t => t.id === activeTagFilter));
}

function renderTagFilter() {
    const host = document.getElementById('project-tag-filter');
    if (!host) return;
    const tags = (currentProject && currentProject.tags) || [];
    if (!tags.length) { host.innerHTML = ''; return; }
    host.innerHTML = tags.map(tag => {
        const on = activeTagFilter === tag.id ? ' tag-chip-active' : '';
        return `<button class="tag-chip${on}" data-click='["filterByTag",${tag.id}]'>${escHtml(tag.name)}</button>`;
    }).join('') + (activeTagFilter
        ? `<button class="tag-chip tag-chip-clear" data-click='["filterByTag",null]'>${escHtml(tt('project.view.all_tags'))}</button>`
        : '');
}

function filterByTag(tagId) {
    activeTagFilter = (activeTagFilter === tagId) ? null : tagId;
    renderTagFilter();
    renderSizingRows();
}

function renderSizingRows() {
    const host = document.getElementById('project-sizings');
    if (!host) return;
    const rows = visibleSizings();
    if (!rows.length) {
        host.innerHTML = `<p class="project-empty">${escHtml(tt('project.view.empty'))}</p>`;
        return;
    }
    const canEdit = !!(currentProject && currentProject.can_edit);
    host.innerHTML = `<table class="sizing-table">
        <thead><tr>
            <th class="col-check"></th>
            <th data-i18n="project.table.name">Sizing</th>
            <th data-i18n="project.table.tags">Tags</th>
            <th data-i18n="project.table.role">Role</th>
            <th data-i18n="project.table.source">Source</th>
            <th data-i18n="project.table.state">State</th>
            <th class="col-actions"></th>
        </tr></thead>
        <tbody>${rows.map(r => sizingRow(r, canEdit)).join('')}</tbody>
    </table>`;
}

function sizingRow(s, canEdit) {
    const checked = selectedSizings.has(s.id) ? ' checked' : '';
    const tags = (s.tags || []).map(t => `<span class="tag-chip tag-chip-sm">${escHtml(t.name)}</span>`).join('');
    const role = s.role
        ? `<span class="role-chip role-${escHtml(s.role)}">${escHtml(tt('project.role.' + s.role))}</span>`
        : `<span class="role-chip role-unset">—</span>`;
    const source = s.is_dr_target
        ? escHtml(tt('project.table.dr_target'))
        : escHtml((s.source_meta && s.source_meta.file_name) || tt('project.table.manual'));

    // Three distinct states, deliberately not merged: a re-import cannot be
    // fixed by recalculating (§3.3), so it must not read as ordinary staleness.
    let state;
    if (s.needs_reimport) {
        state = `<span class="state-badge state-reimport" title="${escHtml(tt('project.state.reimport_hint'))}">${escHtml(tt('project.state.reimport'))}</span>`;
    } else if (!s.has_result) {
        state = `<span class="state-badge state-none">${escHtml(tt('project.state.none'))}</span>`;
    } else if (s.stale) {
        state = `<span class="state-badge state-stale" title="${escHtml(tt('project.state.stale_hint'))}">${escHtml(tt('project.state.stale'))}</span>`;
    } else {
        state = `<span class="state-badge state-fresh">${escHtml(tt('project.state.fresh'))}</span>`;
    }

    const actions = canEdit ? `
        <button class="btn btn-xs" data-click='["openSizing",${s.id}]' data-i18n="project.action.open">Open</button>
        <button class="btn btn-xs" data-click='["openSizingPanel",${s.id}]' data-i18n="project.action.panel">Options</button>
        <button class="btn btn-xs" data-click='["duplicateSizing",${s.id}]' data-i18n="project.action.duplicate">Duplicate</button>
        <button class="btn btn-xs btn-danger" data-click='["deleteProjectSizing",${s.id}]' data-i18n="project.action.delete">Delete</button>`
        : `<button class="btn btn-xs" data-click='["openSizing",${s.id}]' data-i18n="project.action.open">Open</button>
           <button class="btn btn-xs" data-click='["duplicateSizing",${s.id}]' data-i18n="project.action.copy">Copy to mine</button>`;

    return `<tr>
        <td class="col-check"><input type="checkbox"${checked} data-change='["toggleSizing",${s.id},"$checked"]'></td>
        <td class="sizing-name">${escHtml(s.name)}${s.notes ? ` <span class="note-dot" title="${escHtml(s.notes)}">●</span>` : ''}</td>
        <td>${tags}</td>
        <td>${role}</td>
        <td class="sizing-source">${source}</td>
        <td>${state}</td>
        <td class="col-actions">${actions}</td>
    </tr>`;
}

function renderReplication() {
    const host = document.getElementById('project-replication');
    if (!host) return;
    const links = (currentProject && currentProject.replication_links) || [];
    if (!links.length) { host.innerHTML = ''; return; }
    host.innerHTML = `<h3 class="rep-heading">${escHtml(tt('project.rep.title'))}</h3>
        <ul class="rep-list">${links.map(l => `<li>
            <strong>${escHtml(l.source_label)}</strong> →
            <strong>${escHtml(l.target_label)}</strong>
            <span class="rep-terms">${l.compute_pct}% ${escHtml(tt('project.rep.compute'))} ·
                ${l.storage_pct}% ${escHtml(tt('project.rep.storage'))} ·
                ${escHtml(tt('project.sizing.rep_' + l.mode))}</span>
        </li>`).join('')}</ul>`;
}

// ── refresh loop (§4) ───────────────────────────────────────────────────────
// Stale sizings are recalculated one hidden iframe at a time. Each iframe gets
// its own copy of the sizer's module-level state and is destroyed afterwards,
// so nothing bleeds between sizings — running them in this page would.
//
// Throttled and lazy on purpose: a refresh pulls that sizing's whole payload
// (VM lists run to megabytes) and then does a full in-browser calculation, so
// refreshing everything on open would be minutes of work nobody asked for.

const REFRESH_PARALLEL = 2;
const REFRESH_TIMEOUT_MS = 30000;
let refreshQueue = [];
let refreshActive = 0;
let refreshDone = 0;
let refreshTotal = 0;
let refreshFailed = 0;
const refreshPending = new Map();   // configId -> {frame, timer, resolve}

function refreshStaleSizings(force) {
    const rows = (currentProject && currentProject.sizings) || [];
    // Anything without a current result: never sized, or sized before the last
    // catalog/tunable/replication change. `has_result` is deliberately NOT
    // required — a sizing with no stored result is precisely the one that needs
    // calculating, and requiring it left every freshly saved sizing stuck on
    // "Not sized" forever.
    //
    // "Needs re-import" is the one exclusion: recalculating cannot repair a
    // parser fix, so pumping it through the loop would just churn.
    const targets = rows.filter(s => !s.needs_reimport && (force || s.stale));
    if (!targets.length) return Promise.resolve();

    refreshQueue = targets.map(s => s.id);
    refreshTotal = refreshQueue.length;
    refreshDone = 0;
    refreshFailed = 0;
    renderRefreshProgress();
    return new Promise(resolve => {
        const pump = () => {
            if (!refreshQueue.length && refreshActive === 0) {
                renderRefreshProgress(true);
                return resolve();
            }
            while (refreshActive < REFRESH_PARALLEL && refreshQueue.length) {
                const id = refreshQueue.shift();
                refreshActive++;
                refreshOne(id).then((ok) => {
                    refreshActive--;
                    refreshDone++;
                    if (!ok) refreshFailed++;
                    renderRefreshProgress();
                    pump();
                });
            }
        };
        pump();
    });
}

function refreshOne(configId) {
    return new Promise(resolve => {
        const frame = document.createElement('iframe');
        frame.setAttribute('aria-hidden', 'true');
        frame.className = 'refresh-frame';
        frame.src = '/?refresh=' + encodeURIComponent(configId);

        const finish = (ok) => {
            const entry = refreshPending.get(configId);
            if (!entry) return;
            clearTimeout(entry.timer);
            refreshPending.delete(configId);
            if (entry.frame.parentNode) entry.frame.parentNode.removeChild(entry.frame);
            resolve(ok);
        };
        // A refresh that never reports back must not wedge the queue: mark it
        // and move on, so the rest of the project still comes current.
        const timer = setTimeout(() => finish(false), REFRESH_TIMEOUT_MS);
        refreshPending.set(configId, { frame, timer, finish });
        document.body.appendChild(frame);
    });
}

window.addEventListener('message', (e) => {
    // Same-origin only — the iframe is ours, anything else is not.
    if (e.origin !== window.location.origin) return;
    const msg = e.data;
    if (!msg || msg.type !== 'sizer:refresh') return;
    const entry = refreshPending.get(msg.configId);
    if (entry) entry.finish(!!msg.ok);
});

function renderRefreshProgress(done) {
    const bar = document.getElementById('project-refresh-bar');
    if (!bar) return;
    if (done || !refreshTotal) {
        if (done && refreshFailed) {
            // Say so rather than leave rows sitting on "Not sized" while the
            // loop silently retries on every visit.
            bar.hidden = false;
            bar.textContent = tt('project.refresh.failed', { count: refreshFailed });
            if (currentProject) openProject(currentProject.id, true);
            return;
        }
        bar.hidden = true;
        // Reload to pick up the new states — with refresh suppressed, or the
        // reload would start the loop again and never settle.
        if (done && currentProject) openProject(currentProject.id, true);
        return;
    }
    bar.hidden = false;
    bar.textContent = tt('project.refresh.progress',
                         { done: refreshDone, total: refreshTotal });
}

async function refreshProjectNow() {
    await refreshStaleSizings(true);
}

// ── selection ───────────────────────────────────────────────────────────────

function toggleSizing(id, on) {
    if (on) selectedSizings.add(id); else selectedSizings.delete(id);
    renderSelectionBar();
}

function clearSizingSelection() {
    selectedSizings = new Set();
    renderSizingRows();
    renderSelectionBar();
}

function renderSelectionBar() {
    const bar = document.getElementById('project-selection-bar');
    const label = document.getElementById('project-selection-count');
    if (!bar || !label) return;
    bar.hidden = selectedSizings.size === 0;
    label.textContent = tt('project.view.selected', { count: selectedSizings.size });
}

// ── comparison (§6) ─────────────────────────────────────────────────────────

async function compareSelected() {
    if (!currentProject || !selectedSizings.size) return;
    const host = document.getElementById('project-compare');
    if (!host) return;
    host.hidden = false;
    host.innerHTML = `<p class="project-empty">${escHtml(tt('project.compare.loading'))}</p>`;

    const { ok, data } = await api(`/api/projects/${currentProject.id}/compare`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sizing_ids: [...selectedSizings] }),
    });
    if (!ok) { host.innerHTML = `<p class="project-empty">${escHtml(tt('project.compare.failed'))}</p>`; return; }

    const rows = data.rows || [];
    const baseline = rows[0];
    const metric = (key, row) => (row.totals && row.totals[key]) || 0;
    const delta = (key, row) => {
        if (!baseline || row.id === baseline.id) return '';
        const d = metric(key, row) - metric(key, baseline);
        if (!d) return '';
        return `<span class="delta ${d > 0 ? 'delta-up' : 'delta-down'}">${d > 0 ? '+' : ''}${round(d)}</span>`;
    };
    const round = (n) => (Math.round(n * 100) / 100);

    const cols = [
        ['project.compare.model', r => escHtml(r.totals.model || '—')],
        // Physical HyperCore clusters, with the node split behind a tooltip —
        // "2" reads better than "8 + 5" in the table, but the split is the
        // thing you argue about in the meeting.
        ['project.compare.clusters', r => {
            const layout = (r.totals.layout || []).join(' + ');
            const title = layout ? ` title="${escHtml(layout)}"` : '';
            return `<span${title}>${metric('clusters', r)}</span> ${delta('clusters', r)}`;
        }],
        ['project.compare.nodes', r => `${metric('nodes', r)} ${delta('nodes', r)}`],
        ['project.compare.cores', r => `${metric('cores', r)} ${delta('cores', r)}`],
        ['project.compare.ram', r => `${metric('ram_gb', r)} GB ${delta('ram_gb', r)}`],
        ['project.compare.storage', r => `${round(metric('usable_tb', r))} TB ${delta('usable_tb', r)}`],
        ['project.compare.n1_cores', r => `${metric('n1_cores', r)}`],
        ['project.compare.n1_ram', r => `${metric('n1_ram_gb', r)} GB`],
    ];

    // Warnings first: a comparison of options sized under different assumptions
    // is invalid, and saying so is the whole point of decision 16.
    const warn = (data.warnings || []).map(w => {
        const key = 'project.compare.warn_' + w.code;
        return `<li>${escHtml(tt(key, { name: w.name || '' }))}</li>`;
    }).join('');

    const rollup = data.rollup ? `<p class="compare-rollup">${escHtml(tt('project.compare.rollup', {
        count: data.rollup.count, nodes: data.rollup.nodes,
        clusters: data.rollup.clusters,
        cores: data.rollup.cores, ram: data.rollup.ram_gb,
        storage: round(data.rollup.usable_tb),
    }))}</p>` : `<p class="compare-rollup compare-rollup-none">${escHtml(tt('project.compare.no_rollup'))}</p>`;

    host.innerHTML = `
        <div class="compare-head">
            <h3>${escHtml(tt('project.compare.title'))}</h3>
            <button class="btn btn-xs" data-click='["closeCompare"]'
                    data-i18n="common.close">Close</button>
        </div>
        ${warn ? `<ul class="compare-warnings">${warn}</ul>` : ''}
        <div class="compare-scroll"><table class="sizing-table compare-table">
            <thead><tr><th>${escHtml(tt('project.compare.metric'))}</th>
                ${rows.map(r => `<th>${escHtml(r.name)}${r.role ? ` <span class="role-chip role-${escHtml(r.role)}">${escHtml(tt('project.role.' + r.role))}</span>` : ''}</th>`).join('')}
            </tr></thead>
            <tbody>${cols.map(([label, render]) => `<tr>
                <td class="compare-metric">${escHtml(tt(label))}</td>
                ${rows.map(r => `<td>${render(r)}</td>`).join('')}
            </tr>`).join('')}
            <tr><td class="compare-metric">${escHtml(tt('project.compare.why'))}</td>
                ${rows.map(r => `<td class="compare-note">${escHtml(r.notes || '—')}</td>`).join('')}</tr>
            </tbody>
        </table></div>
        ${rollup}`;
    host.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function closeCompare() {
    const host = document.getElementById('project-compare');
    if (host) { host.hidden = true; host.innerHTML = ''; }
}

// ── bundle exports (§7.2) ───────────────────────────────────────────────────

let exportPollTimer = null;

async function exportSelected() {
    if (!currentProject || !selectedSizings.size) return;
    const fmt = document.getElementById('project-export-format').value;
    const notify = document.getElementById('project-export-notify').checked;

    const { ok, data } = await api(`/api/projects/${currentProject.id}/export`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            format: fmt, sizing_ids: [...selectedSizings],
            notify_email: notify, lang: currentProject.lang || window.I18N_ACTIVE,
        }),
    });
    if (!ok) return info(tt('project.export.failed'), (data && data.error) || '');
    loadExports();
}

async function loadExports() {
    if (!currentProject) return;
    const host = document.getElementById('project-exports');
    if (!host) return;
    const { ok, data } = await api(`/api/projects/${currentProject.id}/exports`);
    if (!ok) return;
    const jobs = data || [];
    if (!jobs.length) { host.innerHTML = ''; stopExportPolling(); return; }

    host.innerHTML = `<h3 class="rep-heading">${escHtml(tt('project.export.title'))}</h3>
        <ul class="export-list">${jobs.map(exportRow).join('')}</ul>`;

    // Keep polling only while something is actually running.
    if (jobs.some(j => j.status === 'queued' || j.status === 'running')) startExportPolling();
    else stopExportPolling();
}

function exportRow(j) {
    const when = fmtProjectDate(j.created_at);
    let right;
    if (j.status === 'done' && !j.expired) {
        right = `<a class="btn btn-xs btn-primary" href="/api/export-jobs/${j.id}/file">${escHtml(tt('project.export.download'))}</a>`;
    } else if (j.status === 'done') {
        right = `<span class="export-note">${escHtml(tt('project.export.expired'))}</span>`;
    } else if (j.status === 'failed') {
        right = `<span class="export-note export-failed" title="${escHtml(j.error || '')}">${escHtml(tt('project.export.failed_short'))}</span>`;
    } else {
        right = `<span class="export-note">${escHtml(tt('project.export.working'))}</span>`;
    }
    const note = j.status === 'done' && j.error
        ? `<div class="export-skip">${escHtml(j.error)}</div>` : '';
    return `<li><span>${escHtml(j.format.toUpperCase())} · ${escHtml(when)}</span>${right}${note}</li>`;
}

function startExportPolling() {
    if (exportPollTimer) return;
    exportPollTimer = setInterval(loadExports, 4000);
}

function stopExportPolling() {
    if (!exportPollTimer) return;
    clearInterval(exportPollTimer);
    exportPollTimer = null;
}

// ── sizing actions ──────────────────────────────────────────────────────────

// "New sizing" must be a blank sheet. The sizer keeps its state in module-level
// globals — imported VMs, exclusions, per-cluster options, the chosen
// recommendation, the loaded-config handle — and clearing them by hand means
// enumerating every one correctly, forever. Miss one and the new sizing quietly
// inherits the last one's data, which is exactly the bug this fixes.
//
// A reload guarantees the clean slate; the project comes back through the URL.
// Cloning an existing sizing is what Duplicate is for.
function addSizingToProject() {
    if (!currentProject) return;
    location.href = '/?project=' + encodeURIComponent(currentProject.id) + '&new=1';
}

async function addDrTarget() {
    if (!currentProject) return;
    const name = window.prompt(tt('project.view.dr_prompt'), tt('project.view.dr_default'));
    if (!name) return;
    const { ok, data } = await api(`/api/projects/${currentProject.id}/dr-target`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name }),
    });
    if (!ok) return info(tt('project.view.dr_failed'), (data && data.error) || '');
    openProject(currentProject.id);
}

async function openSizing(id, push) {
    const { ok, data } = await api('/api/configs/' + id);
    if (!ok) return;
    const projectId = data.project_id || (currentProject && currentProject.id);
    pushScreen('/?project=' + encodeURIComponent(projectId) +
               '&sizing=' + encodeURIComponent(id),
               { screen: 'sizer', projectId: projectId, sizingId: id }, push);
    enterSizer(data.name);
    if (window.restoreSizingState) await window.restoreSizingState(data.payload);
    if (window.setLoadedConfig) window.setLoadedConfig(data);
}

async function duplicateSizing(id) {
    const { ok, data } = await api(`/api/sizings/${id}/duplicate`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
    });
    if (!ok) return info(tt('project.action.duplicate_failed'), (data && data.error) || '');
    // A copy of someone else's sizing lands in the copier's own project, so go
    // where the copy actually is rather than assuming it stayed here.
    openProject(data.project_id);
}

async function deleteProjectSizing(id) {
    if (!window.confirm(tt('project.action.delete_confirm'))) return;
    const { ok, data } = await api('/api/configs/' + id, { method: 'DELETE' });
    if (!ok) {
        // 409: something replicates into this sizing and would lose its
        // reserve. Name the dependents rather than just refusing.
        const names = (data && data.replicated_from || []).join(', ');
        return info(tt('project.action.delete_blocked_title'),
                    names ? tt('project.action.delete_blocked_body', { names: names })
                          : ((data && data.error) || ''));
    }
    openProject(currentProject.id);
}

// ── per-sizing panel (role, tags, notes, replication) ───────────────────────

function openSizingPanel(id) {
    panelSizing = (currentProject.sizings || []).find(s => s.id === id);
    if (!panelSizing) return;
    hideError('sizing-modal-error');
    document.getElementById('sizing-modal-title').textContent = panelSizing.name;
    document.getElementById('sizing-role').value = panelSizing.role || '';
    document.getElementById('sizing-notes').value = panelSizing.notes || '';
    renderTagPicker();
    renderRepPicker();
    document.getElementById('sizing-modal').style.display = 'flex';
}

function closeSizingPanel() {
    document.getElementById('sizing-modal').style.display = 'none';
    panelSizing = null;
}

function renderTagPicker() {
    const host = document.getElementById('sizing-tags');
    if (!host) return;
    const tags = (currentProject && currentProject.tags) || [];
    if (!tags.length) {
        host.innerHTML = `<span class="field-hint">${escHtml(tt('project.sizing.no_tags'))}</span>`;
        return;
    }
    const mine = new Set((panelSizing.tags || []).map(t => t.id));
    host.innerHTML = tags.map(tag => {
        const on = mine.has(tag.id) ? ' tag-chip-active' : '';
        return `<button class="tag-chip${on}" data-click='["togglePanelTag",${tag.id}]'>${escHtml(tag.name)}</button>`;
    }).join('');
}

function togglePanelTag(tagId) {
    const tags = panelSizing.tags || (panelSizing.tags = []);
    const at = tags.findIndex(t => t.id === tagId);
    if (at >= 0) tags.splice(at, 1);
    else {
        const tag = (currentProject.tags || []).find(t => t.id === tagId);
        if (tag) tags.push(tag);
    }
    renderTagPicker();
}

function onNewTagKey(e) {
    if (e && e.key === 'Enter') { e.preventDefault(); addTagFromInput(); }
}

async function addTagFromInput() {
    const input = document.getElementById('sizing-new-tag');
    const name = (input && input.value || '').trim();
    if (!name || !currentProject) return;
    const { ok, data } = await api(`/api/projects/${currentProject.id}/tags`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name }),
    });
    if (!ok) return showError('sizing-modal-error', (data && data.error) || '');
    input.value = '';
    currentProject.tags = currentProject.tags || [];
    if (!currentProject.tags.some(t => t.id === data.id)) currentProject.tags.push(data);
    (panelSizing.tags = panelSizing.tags || []).push(data);
    renderTagPicker();
}

function renderRepPicker() {
    const select = document.getElementById('sizing-rep-target');
    if (!select) return;
    // Only sizings in THIS project can be partners — a link across projects
    // would pull one customer's demand into another's sizing.
    const others = (currentProject.sizings || []).filter(s => s.id !== panelSizing.id);
    const existing = ((currentProject.replication_links || [])
        .find(l => l.source_configuration_id === panelSizing.id)) || null;

    select.innerHTML = `<option value="">${escHtml(tt('project.sizing.rep_none'))}</option>` +
        others.map(s => {
            const sel = existing && existing.target_configuration_id === s.id ? ' selected' : '';
            return `<option value="${s.id}"${sel}>${escHtml(s.name)}</option>`;
        }).join('');

    document.getElementById('sizing-rep-compute').value = existing ? existing.compute_pct : 100;
    document.getElementById('sizing-rep-storage').value = existing ? existing.storage_pct : 100;
    document.getElementById('sizing-rep-mode').value = existing ? existing.mode : 'reserved';
}

async function submitSizingPanel() {
    if (!panelSizing) return;
    const id = panelSizing.id;
    const role = document.getElementById('sizing-role').value || null;
    const notes = document.getElementById('sizing-notes').value;
    const tagIds = (panelSizing.tags || []).map(t => t.id);

    let failed = null;
    const calls = [
        api(`/api/sizings/${id}/role`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ role: role }),
        }),
        api(`/api/sizings/${id}/notes`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ notes: notes }),
        }),
        api(`/api/sizings/${id}/tags`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tag_ids: tagIds }),
        }),
    ];

    const targetId = document.getElementById('sizing-rep-target').value;
    if (targetId) {
        calls.push(api(`/api/sizings/${id}/replication`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                target_configuration_id: parseInt(targetId, 10),
                source_cluster: '', target_cluster: '',
                compute_pct: parseInt(document.getElementById('sizing-rep-compute').value, 10),
                storage_pct: parseInt(document.getElementById('sizing-rep-storage').value, 10),
                mode: document.getElementById('sizing-rep-mode').value,
            }),
        }));
    } else {
        calls.push(api(`/api/sizings/${id}/replication?source_cluster=`, { method: 'DELETE' }));
    }

    const results = await Promise.all(calls);
    results.forEach(r => { if (!r.ok && !failed) failed = (r.data && r.data.error) || ''; });
    if (failed) return showError('sizing-modal-error', failed);

    closeSizingPanel();
    openProject(currentProject.id);
}

// ── project settings ────────────────────────────────────────────────────────

function openProjectSettings() {
    if (!currentProject) return;
    hideError('project-settings-error');
    document.getElementById('ps-name').value = currentProject.name || '';
    document.getElementById('ps-customer').value = currentProject.customer_name || '';
    document.getElementById('ps-opportunity').value = currentProject.opportunity_ref || '';
    document.getElementById('ps-prepared').value = currentProject.prepared_by || '';
    document.getElementById('ps-description').value = currentProject.description || '';

    // The row exists only when the server sent the field, i.e. for scale users.
    const row = document.getElementById('ps-salesforce-row');
    const hasField = Object.prototype.hasOwnProperty.call(currentProject, 'salesforce_url');
    row.hidden = !hasField;
    if (hasField) document.getElementById('ps-salesforce').value = currentProject.salesforce_url || '';

    document.getElementById('project-settings-modal').style.display = 'flex';
}

function closeProjectSettings() {
    document.getElementById('project-settings-modal').style.display = 'none';
}

async function submitProjectSettings() {
    if (!currentProject) return;
    const body = {
        name: document.getElementById('ps-name').value,
        customer_name: document.getElementById('ps-customer').value,
        opportunity_ref: document.getElementById('ps-opportunity').value,
        prepared_by: document.getElementById('ps-prepared').value,
        description: document.getElementById('ps-description').value,
    };
    const row = document.getElementById('ps-salesforce-row');
    if (row && !row.hidden) body.salesforce_url = document.getElementById('ps-salesforce').value;

    const { ok, data } = await api('/api/projects/' + currentProject.id, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!ok) return showError('project-settings-error', (data && data.error) || '');
    closeProjectSettings();
    openProject(currentProject.id);
}

async function deleteCurrentProject() {
    if (!currentProject) return;
    if (!window.confirm(tt('project.settings.delete_confirm', { name: currentProject.name }))) return;
    const { ok, data } = await api('/api/projects/' + currentProject.id, { method: 'DELETE' });
    if (!ok) return showError('project-settings-error', (data && data.error) || '');
    closeProjectSettings();
    backToProjects();
}

// ── sizer hand-off ──────────────────────────────────────────────────────────

function enterSizer(sizingName) {
    showScreen('sizer');
    const bar = document.getElementById('sizer-project-bar');
    const name = document.getElementById('sizer-project-name');
    if (bar) bar.hidden = !currentProject;
    if (name && currentProject) name.textContent = currentProject.name;
    setSizerSizingName(sizingName);
}

// Which sizing the sizer is working on. An unsaved one says so in capitals
// rather than showing nothing — "no name" and "not saved yet" look identical
// otherwise, and only one of them means your work is at risk.
function setSizerSizingName(sizingName) {
    const el = document.getElementById('sizer-sizing-name');
    if (!el) return;
    const saved = (sizingName || '').trim();
    el.textContent = saved || tt('project.bar.unsaved');
    el.classList.toggle('sizer-unsaved', !saved);
}

// auth.js asks for this when saving, so a new sizing is filed where the user is
// working instead of silently landing in the scratch project.
function activeProjectId() {
    return currentProject ? currentProject.id : null;
}

// "Use in export and save" on a single-sizing recommendation card: record the
// pick, save, and go back to the project. Picking an option IS the decision, so
// making it also the save point removes the step where a chosen option is left
// unsaved and the bundle quietly exports the previous one.
//
// Deliberately not used by the per-cluster picker in separate-clusters mode:
// there you choose an option for each cluster in turn before exporting the
// combined document, and saving on the first pick would drop you out mid-flow.
async function saveAndReturnToProject() {
    if (!window.saveCurrentSizing) return;
    const saved = await window.saveCurrentSizing();
    if (saved && currentProject) backToProject();
}

// ── small helpers ───────────────────────────────────────────────────────────

function showError(id, msg) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = msg || '';
    el.hidden = !msg;
}

function hideError(id) { showError(id, ''); }

function info(title, msg) {
    if (window.showInfoModal) window.showInfoModal(title, msg);
    else window.alert(title + '\n\n' + msg);
}

// ── exports for the delegate + auth.js ──────────────────────────────────────

Object.assign(window, {
    openProjectsHome, backToProjects, backToProject, openProject,
    openNewProject, closeNewProject, submitNewProject, onNewProjectKey,
    openProjectByCodePrompt, startQuickSizing,
    addSizingToProject, addDrTarget, openSizing, duplicateSizing, deleteProjectSizing,
    openSizingPanel, closeSizingPanel, submitSizingPanel, togglePanelTag,
    onNewTagKey, addTagFromInput,
    openProjectSettings, closeProjectSettings, submitProjectSettings,
    deleteCurrentProject,
    toggleSizing, clearSizingSelection, filterByTag,
    activeProjectId, enterSizer, setSizerSizingName, saveAndReturnToProject,
    bootFromUrl,
    compareSelected, closeCompare, exportSelected, refreshProjectNow,
});
