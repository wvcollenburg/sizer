// Account / auth UI. Login is optional — anonymous users keep full sizer access;
// signing in unlocks saving and sharing sizings. Drives the header account bar
// and the auth / My Sizings / Organization modals. Talks to the /api/auth,
// /api/configs and /api/admin/users endpoints.

let currentAccount = null;   // the logged-in user object, or null when anonymous
let authTab = 'login';

document.addEventListener('DOMContentLoaded', () => {
    refreshAccount();
    // If a non-super-admin was bounced from /admin, nudge them to sign in.
    if (new URLSearchParams(location.search).get('admin') === '1') {
        openAuthModal();
        showAuthError('Sign in with a super-admin account to manage models.');
    }
});

async function apiJson(url, opts) {
    const resp = await fetch(url, opts);
    let data = null;
    try { data = await resp.json(); } catch (e) { data = null; }
    return { ok: resp.ok, status: resp.status, data };
}

async function refreshAccount() {
    const { data } = await apiJson('/api/auth/me');
    currentAccount = (data && data.user) || null;
    renderAccountBar();
}

function renderAccountBar() {
    const bar = document.getElementById('account-bar');
    if (!bar) return;
    if (!currentAccount) {
        bar.innerHTML =
            `<button class="btn btn-sm btn-account" onclick="openAuthModal()">Sign in / Sign up</button>`;
        return;
    }
    const u = currentAccount;
    const tag = u.role === 'super_admin' ? 'Super admin'
        : u.role === 'tenant_admin' ? 'Admin'
        : u.is_scale ? 'Scale' : '';
    const buttons = [
        `<button class="btn btn-sm btn-account" onclick="saveCurrentSizing()">Save sizing</button>`,
        `<button class="btn btn-sm" onclick="openSizingsModal()">My Sizings</button>`,
    ];
    if (u.role === 'tenant_admin') {
        buttons.push(`<button class="btn btn-sm" onclick="openOrgModal()">Organization</button>`);
    }
    if (u.role === 'super_admin') {
        buttons.push(`<a class="btn btn-sm" href="/admin/">Admin</a>`);
    }
    buttons.push(`<button class="btn btn-sm btn-muted" onclick="doLogout()">Sign out</button>`);
    bar.innerHTML =
        `<span class="account-email">${esc(u.email)}${tag ? ` <span class="account-tag">${tag}</span>` : ''}</span>`
        + buttons.join('');
}

function esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// ── auth modal ───────────────────────────────────────────────────────────────

function openAuthModal() {
    setAuthTab('login');
    document.getElementById('auth-email').value = '';
    document.getElementById('auth-password').value = '';
    hideAuthError();
    document.getElementById('auth-modal').style.display = 'flex';
    document.getElementById('auth-email').focus();
}

function closeAuthModal() {
    document.getElementById('auth-modal').style.display = 'none';
}

function setAuthTab(tab) {
    authTab = tab;
    document.getElementById('auth-tab-login').classList.toggle('active', tab === 'login');
    document.getElementById('auth-tab-signup').classList.toggle('active', tab === 'signup');
    document.getElementById('auth-modal-title').textContent = tab === 'login' ? 'Sign in' : 'Sign up';
    document.getElementById('auth-submit').textContent = tab === 'login' ? 'Sign in' : 'Create account';
    document.getElementById('auth-signup-hint').style.display = tab === 'signup' ? 'block' : 'none';
    document.getElementById('auth-password').setAttribute(
        'autocomplete', tab === 'login' ? 'current-password' : 'new-password');
    hideAuthError();
}

function showAuthError(msg) {
    const el = document.getElementById('auth-error');
    el.textContent = msg;
    el.style.display = 'block';
}
function hideAuthError() {
    document.getElementById('auth-error').style.display = 'none';
}

async function submitAuth(event) {
    event.preventDefault();
    hideAuthError();
    const email = document.getElementById('auth-email').value.trim();
    const password = document.getElementById('auth-password').value;
    const url = authTab === 'login' ? '/api/auth/login' : '/api/auth/signup';

    const { ok, data } = await apiJson(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
    });
    if (!ok) {
        showAuthError((data && data.error) || 'Something went wrong. Try again.');
        return;
    }
    currentAccount = data.user;
    renderAccountBar();
    closeAuthModal();
    if (authTab === 'signup' && data.is_tenant_admin) {
        alert('Account created. You are the admin for your organisation.');
    }
}

async function doLogout() {
    await apiJson('/api/auth/logout', { method: 'POST' });
    currentAccount = null;
    renderAccountBar();
}

// ── My Sizings ───────────────────────────────────────────────────────────────

function openSizingsModal() {
    if (!currentAccount) { openAuthModal(); return; }
    // The code box (and thus the toolbar) is only useful to scale users, who can
    // retrieve any config cross-tenant by its 12-digit code.
    document.getElementById('sizings-code-box').style.display =
        currentAccount.is_scale ? 'flex' : 'none';
    document.getElementById('sizings-toolbar').style.display =
        currentAccount.is_scale ? 'flex' : 'none';
    document.getElementById('sizings-modal').style.display = 'flex';
    loadSizingsList();
}

function closeSizingsModal() {
    document.getElementById('sizings-modal').style.display = 'none';
}

function sizingsStatus(msg, isError) {
    const el = document.getElementById('sizings-status');
    if (!msg) { el.style.display = 'none'; return; }
    el.textContent = msg;
    el.className = 'sizings-status ' + (isError ? 'upload-error' : 'upload-ok');
    el.style.display = 'block';
}

async function loadSizingsList() {
    const { ok, data } = await apiJson('/api/configs/');
    const body = document.getElementById('sizings-table-body');
    if (!ok) { body.innerHTML = ''; sizingsStatus('Could not load sizings.', true); return; }
    if (!data.length) {
        body.innerHTML = `<tr><td colspan="6" class="no-recs">No saved sizings yet.</td></tr>`;
        return;
    }
    const sourceLabel = { owned: 'Mine', tenant: 'Team', scale: 'Scale', linked: 'By code' };
    body.innerHTML = data.map(c => {
        const del = c.can_delete
            ? `<button class="btn btn-sm btn-muted" onclick="deleteSizing(${c.id}, '${esc(c.source)}')">${c.source === 'linked' ? 'Remove' : 'Delete'}</button>`
            : '';
        return `<tr>
            <td>${esc(c.name)}</td>
            <td>${esc(c.owner_email || '')}</td>
            <td><code class="sizing-code" title="Share this code">${esc(c.code)}</code></td>
            <td>${sourceLabel[c.source] || c.source}</td>
            <td>${fmtDate(c.updated_at)}</td>
            <td><button class="btn btn-sm btn-primary" onclick="loadSizing(${c.id})">Load</button> ${del}</td>
        </tr>`;
    }).join('');
}

function fmtDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return isNaN(d) ? '' : d.toLocaleString();
}

// Invoked from the header button (modal closed) or — historically — the modal.
// Reports via the modal status line when it's open, otherwise via alert.
async function saveCurrentSizing() {
    if (!currentAccount) { openAuthModal(); return; }
    const modalOpen = document.getElementById('sizings-modal').style.display === 'flex';
    const fail = (msg) => modalOpen ? sizingsStatus(msg, true) : alert(msg);
    const ok_ = (msg) => modalOpen ? sizingsStatus(msg, false) : alert(msg);

    if (!window.hasSizingToSave || !window.hasSizingToSave()) {
        fail('Run a sizing first — there is nothing to save yet.');
        return;
    }
    const snap = window.captureSizingState();
    if (!snap) {
        fail('Run a sizing first — there is nothing to save yet.');
        return;
    }
    const name = (prompt('Name this sizing:') || '').trim();
    if (!name) return;

    const { ok, data } = await apiJson('/api/configs/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, payload: snap }),
    });
    if (!ok) { fail((data && data.error) || 'Could not save.'); return; }
    ok_(`Saved "${data.name}".\nShare code: ${data.code}`);
    if (modalOpen) loadSizingsList();
}

async function loadSizing(id) {
    const { ok, data } = await apiJson('/api/configs/' + id);
    if (!ok) { sizingsStatus((data && data.error) || 'Could not load.', true); return; }
    closeSizingsModal();
    await window.restoreSizingState(data.payload);
}

async function retrieveByCode() {
    const code = document.getElementById('sizings-code-input').value.trim();
    if (!code) return;
    const { ok, data } = await apiJson('/api/configs/code/' + encodeURIComponent(code));
    if (!ok) { sizingsStatus((data && data.error) || 'No configuration found for that code.', true); return; }
    document.getElementById('sizings-code-input').value = '';
    sizingsStatus(`Loaded "${data.name}" and added it to your list.`, false);
    loadSizingsList();
    closeSizingsModal();
    await window.restoreSizingState(data.payload);
}

async function deleteSizing(id, source) {
    const verb = source === 'linked' ? 'remove this from your list' : 'delete this sizing';
    if (!confirm(`Are you sure you want to ${verb}?`)) return;
    const { ok, data } = await apiJson('/api/configs/' + id, { method: 'DELETE' });
    if (!ok) { sizingsStatus((data && data.error) || 'Could not delete.', true); return; }
    sizingsStatus(data.message || 'Done.', false);
    loadSizingsList();
}

// ── Organization (tenant admin) ──────────────────────────────────────────────

function openOrgModal() {
    document.getElementById('org-modal').style.display = 'flex';
    document.getElementById('org-desc').textContent =
        'Active members of your organisation. Disabling a member revokes their access.';
    loadOrgUsers();
}
function closeOrgModal() {
    document.getElementById('org-modal').style.display = 'none';
}
function orgStatus(msg, isError) {
    const el = document.getElementById('org-status');
    if (!msg) { el.style.display = 'none'; return; }
    el.textContent = msg;
    el.className = 'sizings-status ' + (isError ? 'upload-error' : 'upload-ok');
    el.style.display = 'block';
}

async function loadOrgUsers() {
    const { ok, data } = await apiJson('/api/admin/users/');
    const body = document.getElementById('org-table-body');
    if (!ok) { body.innerHTML = ''; orgStatus('Could not load members.', true); return; }
    body.innerHTML = data.map(u => {
        const isSelf = currentAccount && u.id === currentAccount.id;
        const canDisable = !isSelf && u.role !== 'super_admin';
        const action = canDisable
            ? `<button class="btn btn-sm btn-muted" onclick="disableOrgUser(${u.id})">Disable</button>`
            : (isSelf ? '<span class="muted">You</span>' : '');
        return `<tr>
            <td>${esc(u.email)}</td>
            <td>${esc(u.role)}</td>
            <td>${fmtDate(u.last_login_at)}</td>
            <td>${action}</td>
        </tr>`;
    }).join('');
}

async function disableOrgUser(id) {
    if (!confirm('Disable this member? They will lose access immediately.')) return;
    const { ok, data } = await apiJson('/api/admin/users/' + id + '/disable', { method: 'POST' });
    if (!ok) { orgStatus((data && data.error) || 'Could not disable.', true); return; }
    orgStatus('Member disabled.', false);
    loadOrgUsers();
}
