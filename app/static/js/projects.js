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

// Whose projects the home screen lists. Defaults to your own work; the toggle
// widens it to the whole organization. Remembered across visits, since it is a
// standing preference rather than a per-visit choice.
function projectScope() {
    try {
        return localStorage.getItem('sizer.projectScope') === 'tenant' ? 'tenant' : 'mine';
    } catch (e) {
        return 'mine';      // private mode / storage disabled
    }
}

function toggleProjectScope(on) {
    try {
        localStorage.setItem('sizer.projectScope', on ? 'tenant' : 'mine');
    } catch (e) { /* preference just won't persist */ }
    loadProjects();
}

async function loadProjects() {
    const scope = projectScope();
    const box = document.getElementById('project-scope-all');
    if (box) box.checked = scope === 'tenant';
    const { ok, data } = await api('/api/projects/?scope=' + scope);
    const host = document.getElementById('project-list');
    if (!host) return;
    if (!ok) {
        host.innerHTML = `<p class="project-empty">${escHtml(tt('project.home.load_failed'))}</p>`;
        return;
    }
    projectList = data || [];
    if (!projectList.length) {
        // Distinguish "you have none" from "none of yours, but colleagues have
        // some" — otherwise the toggle looks broken when the list stays empty.
        const key = scope === 'tenant' ? 'project.home.empty' : 'project.home.empty_mine';
        host.innerHTML = `<p class="project-empty">${escHtml(tt(key))}</p>`;
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

// lucide icons used by the sizing rows. Defined once: these strings are
// interpolated into every row, and lucide's own markup is the reference
// (24px box, currentColor stroke, round caps, stroke-width 2).
const _svg = (paths) =>
    `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"`
    + ` stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
const ICON_SLIDERS = _svg('<line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/>');
const ICON_COPY = _svg('<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>');
const ICON_TRASH = _svg('<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" x2="10" y1="11" y2="17"/><line x1="14" x2="14" y1="11" y2="17"/>');

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
    if (s.untouched) {
        // Machine-created from a multi-cluster import; no human has reviewed
        // it. Takes precedence: an unreviewed sizing must not read as merely
        // "no result yet".
        state = `<span class="state-badge state-untouched" title="${escHtml(tt('project.state.untouched_hint'))}">${escHtml(tt('project.state.untouched'))}</span>`;
    } else if (s.needs_reimport) {
        state = `<span class="state-badge state-reimport" title="${escHtml(tt('project.state.reimport_hint'))}">${escHtml(tt('project.state.reimport'))}</span>`;
    } else if (!s.has_result) {
        state = `<span class="state-badge state-none">${escHtml(tt('project.state.none'))}</span>`;
    } else if (s.stale) {
        state = `<span class="state-badge state-stale" title="${escHtml(tt('project.state.stale_hint'))}">${escHtml(tt('project.state.stale'))}</span>`;
    } else {
        state = `<span class="state-badge state-fresh">${escHtml(tt('project.state.fresh'))}</span>`;
    }

    // Icon actions, following SC//Design's scenario card (pencil / copy / trash).
    // The row's name is the way in, so "Open" stops being a button — four filled
    // buttons per row shouted louder than the data they belonged to.
    //
    // Icon-only controls carry their whole accessible name in title/aria-label.
    // Resolved with tt() here rather than left as data-i18n-* attributes: this
    // markup is injected after i18n's one-shot translateDOM pass, so those
    // attributes would never be swapped and the buttons would ship nameless.
    // All four labels already exist, so no new strings.
    const iconBtn = (fn, key, icon, extra = '') => {
        const label = escHtml(tt(key));
        return `<button class="icon-btn ${extra}" data-click='["${fn}",${s.id}]'`
            + ` title="${label}" aria-label="${label}">${icon}</button>`;
    };

    const actions = canEdit
        ? iconBtn('openSizingPanel', 'project.action.panel', ICON_SLIDERS)
          + iconBtn('duplicateSizing', 'project.action.duplicate', ICON_COPY)
          + iconBtn('deleteProjectSizing', 'project.action.delete', ICON_TRASH, 'icon-btn-danger')
        : iconBtn('duplicateSizing', 'project.action.copy', ICON_COPY);

    return `<tr>
        <td class="col-check"><input type="checkbox"${checked} data-change='["toggleSizing",${s.id},"$checked"]'></td>
        <td class="sizing-name"><button class="sizing-open" data-click='["openSizing",${s.id}]'`
        + ` title="${escHtml(tt('project.action.open'))}">${escHtml(s.name)}</button>`
        + `${s.notes ? ` <span class="note-dot" title="${escHtml(s.notes)}">●</span>` : ''}</td>
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
// Which project a refresh run belongs to, and a token that invalidates it.
// Opening another project (or a sizing) while one is running must not have its
// results applied to the new screen — the counters are shared, so a second run
// would otherwise corrupt the first one's progress and navigate the user away.
let refreshRunId = 0;
let refreshProjectId = null;
// In-flight refresh frames, keyed by config id: { frame, timer, finish }.
// refreshOne() writes an entry before appending the iframe; the postMessage
// listener and the timeout both resolve through it, and whichever arrives first
// clears the other. Without this the very first refresh threw a ReferenceError,
// so no frame ever reported back and the progress bar sat on "0 of N" forever.
const refreshPending = new Map();

function refreshStaleSizings(force) {
    const rows = (currentProject && currentProject.sizings) || [];
    // Anything without a current result: never sized, or sized before the last
    // catalog/tunable/replication change. `has_result` is deliberately NOT
    // required — a sizing with no stored result is precisely the one that needs
    // calculating, and requiring it left every freshly saved sizing stuck on
    // "Not sized" forever.
    //
    // Two exclusions: "needs re-import" (recalculating cannot repair a parser
    // fix) and DR targets (their sizing runs through the DR view's own
    // dr-recommend flow — the refresh iframe cannot restore a dr_target payload,
    // so queueing one only produces a false "could not be recalculated" banner).
    const targets = rows.filter(s => !s.needs_reimport && !s.is_dr_target
                                     && (force || s.stale));
    if (!targets.length) return Promise.resolve();

    // Supersede any run still in flight: its remaining frames are abandoned and
    // its completion is ignored, so two projects can't share these counters.
    const runId = ++refreshRunId;
    refreshProjectId = currentProject && currentProject.id;
    refreshQueue = targets.map(s => s.id);
    refreshTotal = refreshQueue.length;
    refreshDone = 0;
    refreshFailed = 0;
    refreshActive = 0;
    renderRefreshProgress();
    return new Promise(resolve => {
        const pump = () => {
            if (runId !== refreshRunId) return resolve();   // superseded
            if (!refreshQueue.length && refreshActive === 0) {
                renderRefreshProgress(true);
                return resolve();
            }
            while (refreshActive < REFRESH_PARALLEL && refreshQueue.length) {
                const id = refreshQueue.shift();
                refreshActive++;
                refreshOne(id).then((ok) => {
                    if (runId !== refreshRunId) return resolve();
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
        // Only refresh the screen if the user is still on the project this run
        // was for. They may have opened a sizing meanwhile, and reloading the
        // project view under them would throw away what they were doing.
        const stillHere = document.body.classList.contains('view-project')
            && currentProject && currentProject.id === refreshProjectId;

        if (done && refreshFailed) {
            // Say so rather than leave rows sitting on "Not sized" while the
            // loop silently retries on every visit.
            bar.hidden = false;
            bar.textContent = tt('project.refresh.failed', { count: refreshFailed });
            if (stillHere) openProject(currentProject.id, true);
            return;
        }
        bar.hidden = true;
        // Reload to pick up the new states — with refresh suppressed, or the
        // reload would start the loop again and never settle.
        if (done && stillHere) openProject(currentProject.id, true);
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

// ── Comparisons wizard (modal) ──────────────────────────────────────────────
// One entry point for every comparison: pick WHAT (individual sizings or tag
// groups), pick the items, see the table — all inside the modal. Closing the
// modal clears everything; row checkboxes on the project page mean "export"
// and nothing else.

let cmpState = { mode: null, lastBody: null, lastData: null };

function openComparisons() {
    if (!currentProject) return;
    cmpState = { mode: null, lastBody: null, lastData: null };
    _cmpShowStep('mode');
    document.getElementById('comparisons-modal').style.display = 'flex';
}

function closeComparisons() {
    cmpState = { mode: null, lastBody: null, lastData: null };
    const list = document.getElementById('cmp-pick-list');
    if (list) list.innerHTML = '';
    const res = document.getElementById('cmp-step-result');
    if (res) res.innerHTML = '';
    document.getElementById('comparisons-modal').style.display = 'none';
}

function _cmpShowStep(step) {
    const steps = { mode: 'cmp-step-mode', pick: 'cmp-step-pick', result: 'cmp-step-result' };
    Object.entries(steps).forEach(([name, id]) => {
        const el = document.getElementById(id);
        if (el) el.style.display = name === step ? '' : 'none';
    });
    const show = (id, on) => {
        const el = document.getElementById(id);
        if (el) el.style.display = on ? '' : 'none';
    };
    show('cmp-back-btn', step !== 'mode');
    show('cmp-run-btn', step === 'pick');
    show('cmp-csv-btn', step === 'result');
    show('cmp-xlsx-btn', step === 'result');
    if (step === 'pick') updateCmpPickCount();
}

function cmpChooseMode(mode) {
    cmpState.mode = mode === 'tags' ? 'tags' : 'sizings';
    const list = document.getElementById('cmp-pick-list');
    const hint = document.getElementById('cmp-pick-hint');
    if (cmpState.mode === 'tags') {
        const seen = new Map();   // id -> {name, count}
        (currentProject.sizings || []).forEach(s => (s.tags || []).forEach(t => {
            const e = seen.get(t.id) || { name: t.name, count: 0 };
            e.count++;
            seen.set(t.id, e);
        }));
        hint.textContent = tt(seen.size < 2 ? 'project.compare.need_two_tags'
                                            : 'cmp.pick_tags');
        const tags = [...seen.entries()].sort((a, b) =>
            a[1].name.toLowerCase() < b[1].name.toLowerCase() ? -1 : 1);
        list.innerHTML = tags.map(([id, t]) => `
            <label class="fanout-row">
                <input type="checkbox" checked data-cmp-id="${id}" data-change='["updateCmpPickCount"]'>
                <span class="fanout-name">${escHtml(t.name)}</span>
                <span class="fanout-meta">${escHtml(tt('project.compare.members', {count: t.count}))}</span>
            </label>`).join('');
    } else {
        hint.textContent = tt('cmp.pick_sizings');
        list.innerHTML = (currentProject.sizings || []).map(s => `
            <label class="fanout-row">
                <input type="checkbox" data-cmp-id="${s.id}" data-change='["updateCmpPickCount"]'>
                <span class="fanout-name">${escHtml(s.name)}</span>
                <span class="fanout-meta">${s.role ? escHtml(tt('project.role.' + s.role)) : ''}${s.is_dr_target ? ' · ' + escHtml(tt('project.table.dr_target')) : ''}</span>
            </label>`).join('');
    }
    _cmpShowStep('pick');
}

function cmpBack() {
    const onResult = document.getElementById('cmp-step-result').style.display !== 'none';
    _cmpShowStep(onResult ? 'pick' : 'mode');
}

function _cmpChosenIds() {
    return [...document.querySelectorAll('#cmp-pick-list input[data-cmp-id]')]
        .filter(cb => cb.checked)
        .map(cb => parseInt(cb.dataset.cmpId, 10));
}

function updateCmpPickCount() {
    const btn = document.getElementById('cmp-run-btn');
    if (btn) btn.disabled = _cmpChosenIds().length < 2;
}

async function runComparison() {
    const ids = _cmpChosenIds();
    if (ids.length < 2) return;
    const body = cmpState.mode === 'tags' ? { tag_ids: ids } : { sizing_ids: ids };
    const host = document.getElementById('cmp-step-result');
    _cmpShowStep('result');
    host.innerHTML = `<p class="project-empty">${escHtml(tt('project.compare.loading'))}</p>`;

    const { ok, data } = await api(`/api/projects/${currentProject.id}/compare`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!ok) { host.innerHTML = `<p class="project-empty">${escHtml(tt('project.compare.failed'))}</p>`; return; }
    cmpState.lastBody = body;
    cmpState.lastData = data;
    host.innerHTML = _compareTableHtml(data);
}

// The metric rows shared by the on-screen table and the CSV export. Each entry
// is [i18n key, value renderer, numeric accessor (null = text-only)].
function _compareMetricRows() {
    const round = (n) => (Math.round(n * 100) / 100);
    return [
        ['project.compare.model', (r) => {
            const so = r.totals.software_only ? tt('project.compare.software_only') : '';
            const m = r.totals.model;
            return (m && so ? `${m}, ${so}` : (m || so || '—'));
        }, null],
        ['project.compare.clusters', (r) => String(r.totals.clusters || 0), (r) => r.totals.clusters || 0],
        ['project.compare.nodes', (r) => String(r.totals.nodes || 0), (r) => r.totals.nodes || 0],
        ['project.compare.cores', (r) => String(r.totals.cores || 0), (r) => r.totals.cores || 0],
        ['project.compare.ram', (r) => `${r.totals.ram_gb || 0} GB`, (r) => r.totals.ram_gb || 0],
        ['project.compare.storage', (r) => `${round(r.totals.usable_tb || 0)} TB`, (r) => round(r.totals.usable_tb || 0)],
        ['project.compare.n1_cores', (r) => String(r.totals.n1_cores || 0), (r) => r.totals.n1_cores || 0],
        ['project.compare.n1_ram', (r) => `${r.totals.n1_ram_gb || 0} GB`, (r) => r.totals.n1_ram_gb || 0],
    ];
}

function _compareTableHtml(data) {
    const rows = data.rows || [];
    const baseline = rows[0];
    const round = (n) => (Math.round(n * 100) / 100);
    const delta = (num, row) => {
        if (!num || !baseline || row.id === baseline.id) return '';
        const d = num(row) - num(baseline);
        if (!d) return '';
        return `<span class="delta ${d > 0 ? 'delta-up' : 'delta-down'}">${d > 0 ? '+' : ''}${round(d)}</span>`;
    };

    const warn = (data.warnings || []).map(w => {
        const key = 'project.compare.warn_' + w.code;
        return `<li>${escHtml(tt(key, { name: w.name || '' }))}</li>`;
    }).join('');

    const rollup = data.mode === 'tags' ? '' : data.rollup ? `<p class="compare-rollup">${escHtml(tt('project.compare.rollup', {
        count: data.rollup.count, nodes: data.rollup.nodes,
        clusters: data.rollup.clusters,
        cores: data.rollup.cores, ram: data.rollup.ram_gb,
        storage: round(data.rollup.usable_tb),
    }))}</p>` : `<p class="compare-rollup compare-rollup-none">${escHtml(tt('project.compare.no_rollup'))}</p>`;

    return `
        ${warn ? `<ul class="compare-warnings">${warn}</ul>` : ''}
        <div class="compare-scroll"><table class="sizing-table compare-table">
            <thead><tr><th>${escHtml(tt('project.compare.metric'))}</th>
                ${rows.map(r => {
                    const chip = r.member_count != null
                        ? ` <span class="role-chip role-additive">${escHtml(tt('project.compare.members', {count: r.member_count}))}</span>`
                        : (r.role ? ` <span class="role-chip role-${escHtml(r.role)}">${escHtml(tt('project.role.' + r.role))}</span>` : '');
                    return `<th>${escHtml(r.name)}${chip}</th>`;
                }).join('')}
            </tr></thead>
            <tbody>${_compareMetricRows().map(([label, render, num]) => `<tr>
                <td class="compare-metric">${escHtml(tt(label))}</td>
                ${rows.map(r => `<td>${escHtml(render(r))} ${delta(num, r)}</td>`).join('')}
            </tr>`).join('')}
            <tr><td class="compare-metric">${escHtml(tt(data.mode === 'tags' ? 'project.compare.sizings_row' : 'project.compare.why'))}</td>
                ${rows.map(r => `<td class="compare-note">${escHtml(r.notes || '—')}</td>`).join('')}</tr>
            </tbody>
        </table></div>
        ${rollup}`;
}

// CSV of the comparison on screen: metrics as rows, one column per option,
// plus a delta column against the first option for numeric metrics. UTF-8 BOM
// so Excel opens it with correct encoding.
function exportComparisonCsv() {
    const data = cmpState.lastData;
    if (!data) return;
    const rows = data.rows || [];
    const baseline = rows[0];
    const q = (v) => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
    const lines = [];
    const header = [tt('project.compare.metric')];
    rows.forEach((r, i) => {
        header.push(r.name);
        if (i > 0) header.push('Δ');
    });
    lines.push(header.map(q).join(','));
    _compareMetricRows().forEach(([label, render, num]) => {
        const line = [tt(label)];
        rows.forEach((r, i) => {
            line.push(render(r));
            if (i > 0) line.push(num ? (Math.round((num(r) - num(baseline)) * 100) / 100) : '');
        });
        lines.push(line.map(q).join(','));
    });
    const noteLabel = tt(data.mode === 'tags' ? 'project.compare.sizings_row' : 'project.compare.why');
    lines.push([noteLabel, ...rows.flatMap((r, i) => i > 0 ? [r.notes || '', ''] : [r.notes || ''])].map(q).join(','));
    const blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
    _downloadBlob(blob, `${currentProject.name} - comparison.csv`);
}

async function exportComparisonXlsx() {
    if (!cmpState.lastBody || !currentProject) return;
    const resp = await fetch(`/api/projects/${currentProject.id}/compare.xlsx`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(cmpState.lastBody),
    });
    if (!resp.ok) return;
    _downloadBlob(await resp.blob(), `${currentProject.name} - comparison.xlsx`);
}

function _downloadBlob(blob, filename) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
}

// ── bundle exports (§7.2) ───────────────────────────────────────────────────

let exportPollTimer = null;

// ── Exports wizard (modal) ──────────────────────────────────────────────────
// Mirrors the Comparisons wizard: pick WHAT (individual sizings or a tag
// group's members), choose format + notification, queue — and collect the
// finished downloads, all in one modal. The header button carries a badge
// while ready downloads exist.

let expState = { mode: null };
let lastExportJobs = [];

function openExports() {
    if (!currentProject) return;
    expState = { mode: null };
    _expShowStep('mode');
    document.getElementById('exports-modal').style.display = 'flex';
    loadExports();
}

function closeExports() {
    expState = { mode: null };
    const list = document.getElementById('exp-pick-list');
    if (list) list.innerHTML = '';
    document.getElementById('exports-modal').style.display = 'none';
}

function _expShowStep(step) {
    const modeEl = document.getElementById('exp-step-mode');
    const pickEl = document.getElementById('exp-step-pick');
    if (modeEl) modeEl.style.display = step === 'mode' ? '' : 'none';
    if (pickEl) pickEl.style.display = step === 'pick' ? '' : 'none';
    const show = (id, on) => {
        const el = document.getElementById(id);
        if (el) el.style.display = on ? '' : 'none';
    };
    show('exp-back-btn', step === 'pick');
    show('exp-run-btn', step === 'pick');
    if (step === 'pick') updateExpPickCount();
}

function expChooseMode(mode) {
    expState.mode = mode === 'tags' ? 'tags' : 'sizings';
    const list = document.getElementById('exp-pick-list');
    const hint = document.getElementById('exp-pick-hint');
    if (expState.mode === 'tags') {
        const seen = new Map();
        (currentProject.sizings || []).forEach(s => (s.tags || []).forEach(t => {
            const e = seen.get(t.id) || { name: t.name, count: 0 };
            e.count++;
            seen.set(t.id, e);
        }));
        hint.textContent = tt('exp.pick_tags');
        const tags = [...seen.entries()].sort((a, b) =>
            a[1].name.toLowerCase() < b[1].name.toLowerCase() ? -1 : 1);
        list.innerHTML = tags.map(([id, t]) => `
            <label class="fanout-row">
                <input type="checkbox" data-exp-id="${id}" data-change='["updateExpPickCount"]'>
                <span class="fanout-name">${escHtml(t.name)}</span>
                <span class="fanout-meta">${escHtml(tt('project.compare.members', {count: t.count}))}</span>
            </label>`).join('');
    } else {
        hint.textContent = tt('exp.pick_sizings');
        list.innerHTML = (currentProject.sizings || []).map(s => `
            <label class="fanout-row">
                <input type="checkbox" data-exp-id="${s.id}" data-change='["updateExpPickCount"]'>
                <span class="fanout-name">${escHtml(s.name)}</span>
                <span class="fanout-meta">${s.role ? escHtml(tt('project.role.' + s.role)) : ''}${s.is_dr_target ? ' · ' + escHtml(tt('project.table.dr_target')) : ''}</span>
            </label>`).join('');
    }
    _expShowStep('pick');
}

function expBack() { _expShowStep('mode'); }

function _expChosenIds() {
    return [...document.querySelectorAll('#exp-pick-list input[data-exp-id]')]
        .filter(cb => cb.checked)
        .map(cb => parseInt(cb.dataset.expId, 10));
}

function updateExpPickCount() {
    const btn = document.getElementById('exp-run-btn');
    if (btn) btn.disabled = _expChosenIds().length < 1;
}

async function runExport() {
    const ids = _expChosenIds();
    if (!ids.length) return;
    // Tag mode exports the UNION of the chosen tags' member sizings, in
    // project order (position) — the server takes sizing ids either way.
    let sizingIds;
    if (expState.mode === 'tags') {
        const chosen = new Set(ids);
        sizingIds = (currentProject.sizings || [])
            .filter(s => (s.tags || []).some(t => chosen.has(t.id)))
            .map(s => s.id);
        if (!sizingIds.length) return;
    } else {
        sizingIds = ids;
    }
    const fmt = document.getElementById('exp-format').value;
    const notify = document.getElementById('exp-notify').checked;
    const { ok, data } = await api(`/api/projects/${currentProject.id}/export`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            format: fmt, sizing_ids: sizingIds,
            notify_email: notify, lang: currentProject.lang || window.I18N_ACTIVE,
        }),
    });
    if (!ok) return info(tt('project.export.failed'), (data && data.error) || '');
    // Land on the downloads view so the new job is the next thing seen.
    _expShowStep('mode');
    loadExports();
}

// Downloads list (inside the modal) + the header button's ready-count badge.
// Polling runs while jobs are queued/running, whether or not the modal is
// open, so the badge appears the moment a download is ready.
async function loadExports() {
    if (!currentProject) return;
    const { ok, data } = await api(`/api/projects/${currentProject.id}/exports`);
    if (!ok) return;
    lastExportJobs = data || [];
    renderExportsList();
    updateExportsBadge();
    if (lastExportJobs.some(j => j.status === 'queued' || j.status === 'running')) startExportPolling();
    else stopExportPolling();
}

function renderExportsList() {
    const host = document.getElementById('exp-downloads-list');
    const empty = document.getElementById('exp-downloads-empty');
    if (!host) return;
    host.innerHTML = lastExportJobs.map(exportRow).join('');
    if (empty) empty.hidden = lastExportJobs.length > 0;
}

function updateExportsBadge() {
    const badge = document.getElementById('exports-badge');
    if (!badge) return;
    const ready = lastExportJobs.filter(j => j.status === 'done' && !j.expired).length;
    badge.hidden = ready === 0;
    badge.textContent = ready;
}

function exportRow(j) {
    const when = fmtProjectDate(j.created_at);
    let right;
    if (j.status === 'done' && !j.expired) {
        right = `<a class="btn btn-cta btn-xs" href="/api/export-jobs/${j.id}/file">${escHtml(tt('project.export.download'))}</a>`;
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

// ── batch edit (selected sizings) ───────────────────────────────────────────
// Apply detail changes to every selected sizing at once, reusing the
// per-sizing endpoints (role / tags / replication). Empty controls mean
// "leave unchanged".

function openBatchEdit() {
    if (!currentProject || !selectedSizings.size) return;
    hideError('batch-error');
    document.getElementById('batch-summary').textContent =
        tt('batch.summary', { count: selectedSizings.size });
    document.getElementById('batch-role').value = '';
    document.getElementById('batch-new-tag').value = '';

    // Existing project tags as add-chips (click to toggle), and the tags on
    // any selected sizing as remove-chips.
    const all = (currentProject.tags || []);
    const addHost = document.getElementById('batch-add-tags');
    addHost.innerHTML = all.map(t =>
        `<button class="tag-chip tag-chip-add" data-batch-add="${t.id}"
                 data-click='["toggleBatchTag","add",${t.id}]'>${escHtml(t.name)}</button>`).join('');
    const onSelected = new Map();
    (currentProject.sizings || []).forEach(s => {
        if (!selectedSizings.has(s.id)) return;
        (s.tags || []).forEach(t => onSelected.set(t.id, t.name));
    });
    const remHost = document.getElementById('batch-remove-tags');
    remHost.innerHTML = [...onSelected.entries()].map(([id, name]) =>
        `<button class="tag-chip tag-chip-add" data-batch-remove="${id}"
                 data-click='["toggleBatchTag","remove",${id}]'>${escHtml(name)}</button>`).join('')
        || `<span class="tag-empty">${escHtml(tt('project.sizing.tag_none_on_sizing'))}</span>`;

    // Replication targets: any sizing NOT in the selection (a link to a
    // selected source would be fine, but to itself is not — filtered on apply).
    const target = document.getElementById('batch-rep-target');
    target.innerHTML = `<option value="">${escHtml(tt('batch.keep'))}</option>`
        + `<option value="none">${escHtml(tt('batch.rep_none'))}</option>`
        + (currentProject.sizings || [])
            .filter(s => !selectedSizings.has(s.id))
            .map(s => `<option value="${s.id}">${escHtml(s.name)}</option>`).join('');
    updateBatchRepFields();
    document.getElementById('batch-edit-modal').style.display = 'flex';
}

function closeBatchEdit() {
    document.getElementById('batch-edit-modal').style.display = 'none';
}

function toggleBatchTag(kind, id) {
    const sel = kind === 'add' ? `[data-batch-add="${id}"]` : `[data-batch-remove="${id}"]`;
    const chip = document.querySelector(sel);
    if (chip) chip.classList.toggle('tag-chip-assigned');
}

function updateBatchRepFields() {
    const v = document.getElementById('batch-rep-target').value;
    document.getElementById('batch-rep-fields').style.display =
        (v && v !== 'none') ? '' : 'none';
}

async function applyBatchEdit() {
    const ids = [...selectedSizings];
    if (!ids.length) return;
    const role = document.getElementById('batch-role').value;
    const addIds = [...document.querySelectorAll('#batch-add-tags .tag-chip-assigned')]
        .map(el => parseInt(el.dataset.batchAdd, 10));
    const removeIds = new Set([...document.querySelectorAll('#batch-remove-tags .tag-chip-assigned')]
        .map(el => parseInt(el.dataset.batchRemove, 10)));
    const newTag = document.getElementById('batch-new-tag').value.trim();
    const repTarget = document.getElementById('batch-rep-target').value;

    // A typed tag is created once at project level, then added like a chip.
    if (newTag) {
        const { ok, data } = await api(`/api/projects/${currentProject.id}/tags`, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: newTag }),
        });
        if (ok && data && data.id) addIds.push(data.id);
    }

    const rows = (currentProject.sizings || []).filter(s => selectedSizings.has(s.id));
    let failed = null;
    for (const s of rows) {
        const calls = [];
        if (role) {
            calls.push(api(`/api/sizings/${s.id}/role`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ role }),
            }));
        }
        if (addIds.length || removeIds.size) {
            const tagIds = new Set((s.tags || []).map(t => t.id));
            addIds.forEach(id => tagIds.add(id));
            removeIds.forEach(id => tagIds.delete(id));
            calls.push(api(`/api/sizings/${s.id}/tags`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ tag_ids: [...tagIds] }),
            }));
        }
        if (repTarget === 'none') {
            calls.push(api(`/api/sizings/${s.id}/replication?source_cluster=`, { method: 'DELETE' }));
        } else if (repTarget) {
            calls.push(api(`/api/sizings/${s.id}/replication`, {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    target_configuration_id: parseInt(repTarget, 10),
                    source_cluster: '', target_cluster: '',
                    compute_pct: parseInt(document.getElementById('batch-rep-compute').value, 10) || 100,
                    storage_pct: parseInt(document.getElementById('batch-rep-storage').value, 10) || 100,
                    mode: document.getElementById('batch-rep-mode').value,
                }),
            }));
        }
        const results = await Promise.all(calls);
        results.forEach(r => { if (!r.ok && !failed) failed = `${s.name}: ${(r.data && r.data.error) || ''}`; });
    }
    if (failed) return showError('batch-error', failed);
    closeBatchEdit();
    clearSizingSelection();
    openProject(currentProject.id);
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
    // A DR target has no workload of its own — it opens in its dedicated view
    // (sized from inbound replication), not the classic sizer, which has no
    // 'dr_target' mode and would otherwise crash on restore.
    if (data.is_dr_target && window.enterDrTarget) {
        if (window.setLoadedConfig) window.setLoadedConfig(data);
        await window.enterDrTarget(data);
        return;
    }
    // Inbound replication reserve (links targeting this sizing) must be set
    // BEFORE restore: restore's recalc is what folds it into the sizing.
    if (window.setInboundReserve) {
        const inb = await api(`/api/sizings/${id}/inbound-reserve`);
        window.setInboundReserve(inb.ok ? inb.data : null);
    }
    if (window.restoreSizingState) await window.restoreSizingState(data.payload);
    if (window.setLoadedConfig) window.setLoadedConfig(data);
    if (window.markSizingClean) window.markSizingClean();
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

// Two groups, not one toggling row: what is on this sizing (with an explicit ×
// to take it off) and what else the project offers (click to add). A single row
// of "active/inactive" chips hid both facts — you couldn't see at a glance which
// tags were assigned, and removing one meant guessing that clicking it again
// would do that.
function renderTagPicker() {
    const host = document.getElementById('sizing-tags');
    if (!host) return;
    const all = (currentProject && currentProject.tags) || [];
    const mine = (panelSizing && panelSizing.tags) || [];
    const mineIds = new Set(mine.map(t => t.id));
    const available = all.filter(t => !mineIds.has(t.id));

    const assignedHtml = mine.length
        ? mine.map(tag => `<button class="tag-chip tag-chip-assigned"
                data-click='["togglePanelTag",${tag.id}]'
                title="${escHtml(tt('project.sizing.tag_remove'))}">${escHtml(tag.name)}
                <span class="tag-chip-remove">×</span></button>`).join('')
        : `<span class="tag-empty">${escHtml(tt('project.sizing.tag_none_on_sizing'))}</span>`;

    let html = `<div class="tag-group">
        <div class="tag-group-label">${escHtml(tt('project.sizing.tag_assigned'))}</div>
        <div class="tag-picker">${assignedHtml}</div>
    </div>`;

    if (available.length) {
        html += `<div class="tag-group">
            <div class="tag-group-label">${escHtml(tt('project.sizing.tag_available'))}</div>
            <div class="tag-picker">${available.map(tag =>
                `<button class="tag-chip tag-chip-add"
                    data-click='["togglePanelTag",${tag.id}]'
                    title="${escHtml(tt('project.sizing.tag_add'))}">${escHtml(tag.name)}</button>`
            ).join('')}</div>
        </div>`;
    }
    host.innerHTML = html;
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
    // Falls back to the language the app is being used in, which is also what a
    // project created before this setting existed was stamped with.
    document.getElementById('ps-lang').value =
        currentProject.lang || window.I18N_ACTIVE || 'en';

    // The row exists only when the server sent the field, i.e. for scale users.
    const row = document.getElementById('ps-salesforce-row');
    const hasField = Object.prototype.hasOwnProperty.call(currentProject, 'salesforce_url');
    row.hidden = !hasField;
    if (hasField) document.getElementById('ps-salesforce').value = currentProject.salesforce_url || '';

    refreshPreparedByHint();
    document.getElementById('project-settings-modal').style.display = 'flex';
}

// Offer to drop your own name into "Prepared by", but only when it isn't
// already there. Projects created before the name existed carry a derived
// value, and retyping it by hand for each one is exactly the sort of chore
// the field should absorb.
function refreshPreparedByHint() {
    const input = document.getElementById('ps-prepared');
    const action = document.getElementById('ps-prepared-refill');
    if (!input || !action) return;
    const mine = (window.currentUserName && window.currentUserName()) || '';
    const differs = mine && input.value.trim() !== mine;
    action.hidden = !differs;
    if (differs) action.textContent = tt('project.settings.use_my_name', { name: mine });
}

function usePreparedByMe() {
    const input = document.getElementById('ps-prepared');
    const mine = (window.currentUserName && window.currentUserName()) || '';
    if (!input || !mine) return;
    input.value = mine;
    refreshPreparedByHint();          // the offer no longer applies
    input.focus();
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
        lang: document.getElementById('ps-lang').value,
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

// The open project's sizing rows (with tags), for app.js features that need
// them — e.g. the fan-out's default-tag numbering.
function currentProjectSizings() {
    return (currentProject && currentProject.sizings) || [];
}

function currentProjectName() {
    return (currentProject && currentProject.name) || '';
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
    else toastError(title + '\n\n' + msg);
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
    refreshPreparedByHint, usePreparedByMe,
    deleteCurrentProject,
    toggleSizing, clearSizingSelection, filterByTag, toggleProjectScope,
    activeProjectId, currentProjectSizings, enterSizer, setSizerSizingName,
    saveAndReturnToProject,
    bootFromUrl,
    openComparisons, closeComparisons, cmpChooseMode, cmpBack, runComparison,
    updateCmpPickCount, exportComparisonCsv, exportComparisonXlsx,
    openExports, closeExports, expChooseMode, expBack, updateExpPickCount,
    runExport, openBatchEdit, closeBatchEdit, toggleBatchTag,
    updateBatchRepFields, applyBatchEdit, refreshProjectNow,
});
