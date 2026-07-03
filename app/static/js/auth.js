// Account / auth UI. Login is optional — anonymous users keep full sizer access;
// signing in unlocks saving and sharing sizings. Drives the header account bar
// and the auth / My Sizings / Organization modals. Talks to the /api/auth,
// /api/configs and /api/admin/users endpoints.

let currentAccount = null;   // the logged-in user object, or null when anonymous
let authTab = 'login';
// Product requirement: an account is required for ALL functionality. When
// anonymous, the UI is locked behind a non-dismissable login modal and the
// backend rejects every non-auth request.
const LOGIN_REQUIRED = true;
// The sizing currently loaded into the screen (if any), so "Save" can offer to
// update it in place. {id, name, canUpdate} — canUpdate only when we own it.
let loadedConfig = null;

document.addEventListener('DOMContentLoaded', () => {
    refreshAccount();
    const params = new URLSearchParams(location.search);
    // If a non-super-admin was bounced from /admin, nudge them to sign in.
    if (params.get('admin') === '1') {
        openAuthModal();
        showAuthError(t('auth.admin_signin_required'));
    }
    // Result of clicking an email-verification link.
    const v = params.get('verify');
    if (v === 'ok') {
        showInfoModal(t('auth.verify_ok_title'), t('auth.verify_ok_body'));
    } else if (v === 'expired') {
        showInfoModal(t('auth.verify_expired_title'), t('auth.verify_expired_body'));
    } else if (v === 'invalid') {
        showInfoModal(t('auth.verify_invalid_title'), t('auth.verify_invalid_body'));
    }
    // Password-reset link.
    const resetToken = params.get('reset');
    if (resetToken) openResetModal(resetToken);
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
    updateGate();
}

// Lock the app behind the login modal while anonymous; reveal it once signed in.
function updateGate() {
    if (LOGIN_REQUIRED && !currentAccount) {
        document.body.classList.add('auth-required');
        // If the user arrived via a password-reset link, let that modal take the
        // foreground instead of the login modal.
        if (!new URLSearchParams(location.search).get('reset')) {
            openAuthModal();
        }
    } else {
        document.body.classList.remove('auth-required');
    }
}

function renderAccountBar() {
    const bar = document.getElementById('account-bar');
    if (!bar) return;
    if (!currentAccount) {
        // Login is mandatory, so the (non-dismissable) login modal is always
        // shown while signed out — no header button needed.
        bar.innerHTML = '';
        return;
    }
    const u = currentAccount;
    const badge = accountBadge(u);

    // Left group: actions. Right group: identity (email · badge · sign out).
    const actions = [
        `<button class="btn btn-sm btn-account" data-click='["saveCurrentSizing"]'>${esc(t('auth.btn.save_sizing'))}</button>`,
        `<button class="btn btn-sm" data-click='["openSizingsModal"]'>${esc(t('auth.btn.my_sizings'))}</button>`,
    ];
    if (u.role === 'tenant_admin') {
        actions.push(`<button class="btn btn-sm" data-click='["openOrgModal"]'>${esc(t('auth.btn.organization'))}</button>`);
    }
    if (u.role === 'super_admin') {
        actions.push(`<a class="btn btn-sm" href="/admin/">${esc(t('auth.btn.admin'))}</a>`);
    }

    bar.innerHTML =
        `<div class="account-actions">${actions.join('')}</div>`
        + `<span class="account-sep">|</span>`
        + `<div class="account-identity">`
        +   `<span class="account-email">${esc(u.email)}</span>`
        +   `<span class="account-badge ${badge.cls}">${esc(badge.label)}</span>`
        +   `<button class="btn btn-sm btn-muted" data-click='["doLogout"]'>${esc(t('auth.btn.sign_out'))}</button>`
        + `</div>`;
}

// Editable exports (Word, PPTX) are limited to Scale users and super admins;
// everyone else gets read-only PDFs. Mirrors the server-side gate.
function canExportEditable() {
    return !!(currentAccount && (currentAccount.is_scale || currentAccount.role === 'super_admin'));
}

// Badge label + colour class: purple super admin, blue scale user, green others.
function accountBadge(u) {
    if (u.role === 'super_admin') return { label: t('auth.badge.super_admin'), cls: 'super' };
    if (u.is_scale) return { label: t('auth.badge.scale'), cls: 'scale' };
    if (u.role === 'tenant_admin') return { label: t('auth.badge.admin'), cls: 'user' };
    return { label: t('auth.badge.user'), cls: 'user' };
}

function esc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

// ── password policy (mirrors backend validate_password) ──────────────────────

function passwordChecks(pw) {
    return {
        len: pw.length >= 10,
        upper: /[A-Z]/.test(pw),
        lower: /[a-z]/.test(pw),
        digit: /[0-9]/.test(pw),
        special: /[^A-Za-z0-9]/.test(pw),
    };
}

// Update the green-check list for a password + confirm pair.
function updatePwRules(pwId, confirmId, listId) {
    const list = document.getElementById(listId);
    if (!list) return;
    const pw = document.getElementById(pwId).value;
    const confirm = confirmId ? document.getElementById(confirmId).value : null;
    const c = passwordChecks(pw);
    list.querySelectorAll('[data-rule]').forEach(li => {
        const rule = li.dataset.rule;
        const met = rule === 'match'
            ? (confirm !== null && pw.length > 0 && pw === confirm)
            : !!c[rule];
        li.classList.toggle('met', met);
    });
}

// True when the password satisfies every rule (and matches confirm, if given).
function pwAllValid(pwId, confirmId) {
    const pw = document.getElementById(pwId).value;
    const c = passwordChecks(pw);
    const rulesOk = c.len && c.upper && c.lower && c.digit && c.special;
    if (confirmId) {
        const confirm = document.getElementById(confirmId).value;
        return rulesOk && pw.length > 0 && pw === confirm;
    }
    return rulesOk;
}

function onAuthPwInput() {
    if (authTab === 'signup') updatePwRules('auth-password', 'auth-confirm', 'auth-pw-rules');
}
function onResetPwInput() {
    updatePwRules('reset-password', 'reset-confirm', 'reset-pw-rules');
}

// ── auth modal ───────────────────────────────────────────────────────────────

function openAuthModal() {
    setAuthTab('login');
    document.getElementById('auth-email').value = '';
    document.getElementById('auth-password').value = '';
    hideAuthError();
    // When login is mandatory and nobody is signed in, the modal can't be closed.
    const mandatory = LOGIN_REQUIRED && !currentAccount;
    const x = document.querySelector('#auth-modal .modal-close');
    if (x) x.style.display = mandatory ? 'none' : '';
    document.getElementById('auth-modal').style.display = 'flex';
    document.getElementById('auth-email').focus();
}

function closeAuthModal() {
    if (LOGIN_REQUIRED && !currentAccount) return;  // can't dismiss while locked
    document.getElementById('auth-modal').style.display = 'none';
}

function setAuthTab(tab) {
    authTab = tab;
    const signup = tab === 'signup';
    document.getElementById('auth-tab-login').classList.toggle('active', !signup);
    document.getElementById('auth-tab-signup').classList.toggle('active', signup);
    document.getElementById('auth-modal-title').textContent = signup ? t('auth.signup_title') : t('auth.signin_title');
    document.getElementById('auth-submit').textContent = signup ? t('auth.create_account') : t('auth.signin_title');
    document.getElementById('auth-signup-hint').style.display = signup ? 'block' : 'none';
    document.getElementById('auth-password').setAttribute(
        'autocomplete', signup ? 'new-password' : 'current-password');
    // Confirm field, rules checklist, and privacy consent only on signup.
    document.getElementById('auth-confirm-group').style.display = signup ? 'block' : 'none';
    document.getElementById('auth-pw-rules').style.display = signup ? 'block' : 'none';
    document.getElementById('auth-consent-row').style.display = signup ? 'block' : 'none';
    document.getElementById('auth-confirm').value = '';
    document.getElementById('auth-accept-privacy').checked = false;
    if (signup) updatePwRules('auth-password', 'auth-confirm', 'auth-pw-rules');
    hideAuthError();
}

function showAuthError(msg) {
    const el = document.getElementById('auth-error');
    el.textContent = msg;
    el.style.display = 'block';
}
function hideAuthError() {
    document.getElementById('auth-error').style.display = 'none';
    const resend = document.getElementById('auth-resend');
    if (resend) resend.style.display = 'none';
}

async function submitAuth(event) {
    event.preventDefault();
    hideAuthError();
    document.getElementById('auth-resend').style.display = 'none';
    const email = document.getElementById('auth-email').value.trim();
    const password = document.getElementById('auth-password').value;

    // Enforce the password policy + match on signup before hitting the server.
    if (authTab === 'signup') {
        if (password !== document.getElementById('auth-confirm').value) {
            showAuthError(t('auth.pw_mismatch'));
            return;
        }
        if (!pwAllValid('auth-password', 'auth-confirm')) {
            showAuthError(t('auth.pw_requirements'));
            return;
        }
        if (!document.getElementById('auth-accept-privacy').checked) {
            showAuthError(t('auth.accept_privacy'));
            return;
        }
    }

    const url = authTab === 'login' ? '/api/auth/login' : '/api/auth/signup';
    const body = { email, password };
    if (authTab === 'signup') body.accept_privacy = true;

    // Give immediate feedback and block double-submits while the request is in
    // flight — signup includes a (potentially slow) email send, and without this
    // an impatient user keeps clicking, firing duplicate requests.
    const submitBtn = document.getElementById('auth-submit');
    const submitLabel = submitBtn.textContent;
    submitBtn.disabled = true;
    submitBtn.textContent = authTab === 'signup' ? t('auth.creating') : t('auth.signing_in');

    let ok, data;
    try {
        ({ ok, data } = await apiJson(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        }));
    } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = submitLabel;
    }
    if (!ok) {
        showAuthError((data && data.error) || t('auth.generic_error'));
        // Offer a resend link when the account exists but isn't verified.
        if (data && data.needs_verification) {
            document.getElementById('auth-resend').style.display = 'block';
        }
        return;
    }

    // Signup may require email verification before the account is usable.
    if (data && data.pending_verification) {
        closeAuthModal();
        // While login is mandatory the modal can't actually be dismissed, so
        // flip it back to the Sign in tab — that's what the user needs next,
        // once they've verified via email — and clear the entered credentials.
        setAuthTab('login');
        document.getElementById('auth-email').value = '';
        document.getElementById('auth-password').value = '';
        document.getElementById('auth-confirm').value = '';
        document.getElementById('auth-accept-privacy').checked = false;
        const note = data.email_sent === false
            ? ' ' + t('auth.check_email_note')
            : '';
        showInfoModal(t('auth.check_email_title'),
            t('auth.check_email_body', { email: data.email, note }));
        return;
    }

    currentAccount = data.user;
    renderAccountBar();
    updateGate();
    closeAuthModal();
    if (window.initSizer) window.initSizer();  // load catalog data now that we're in
    if (authTab === 'signup' && data.is_tenant_admin) {
        showInfoModal(t('auth.account_created_title'), t('auth.account_created_body'));
    }
}

async function forgotPassword(event) {
    if (event) event.preventDefault();
    const email = document.getElementById('auth-email').value.trim();
    if (!email) { showAuthError(t('auth.forgot_need_email')); return; }
    await apiJson('/api/auth/forgot-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email }),
    });
    closeAuthModal();
    showInfoModal(t('auth.check_email_title'), t('auth.forgot_sent_body'));
}

// ── Reset password (from an emailed ?reset=<token> link) ─────────────────────

let _resetToken = null;

function openResetModal(token) {
    _resetToken = token;
    document.getElementById('reset-password').value = '';
    document.getElementById('reset-confirm').value = '';
    document.getElementById('reset-error').style.display = 'none';
    updatePwRules('reset-password', 'reset-confirm', 'reset-pw-rules');
    document.getElementById('reset-modal').style.display = 'flex';
    document.getElementById('reset-password').focus();
}

function closeResetModal() {
    document.getElementById('reset-modal').style.display = 'none';
    // Drop the token from the URL so a refresh doesn't reopen the modal.
    if (location.search.includes('reset=')) {
        history.replaceState(null, '', location.pathname);
    }
}

async function submitReset(event) {
    event.preventDefault();
    const err = document.getElementById('reset-error');
    err.style.display = 'none';
    const password = document.getElementById('reset-password').value;
    if (password !== document.getElementById('reset-confirm').value) {
        err.textContent = t('auth.pw_mismatch');
        err.style.display = 'block';
        return;
    }
    if (!pwAllValid('reset-password', 'reset-confirm')) {
        err.textContent = t('auth.pw_requirements');
        err.style.display = 'block';
        return;
    }
    const { ok, data } = await apiJson('/api/auth/reset-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: _resetToken, password }),
    });
    if (!ok) {
        err.textContent = (data && data.error) || t('auth.reset_failed');
        err.style.display = 'block';
        return;
    }
    closeResetModal();
    showInfoModal(t('auth.reset_ok_title'), (data && data.message) || t('auth.reset_ok_body'));
    openAuthModal();
}

async function resendVerification(event) {
    if (event) event.preventDefault();
    const email = document.getElementById('auth-email').value.trim();
    if (!email) { showAuthError(t('auth.resend_need_email')); return; }
    await apiJson('/api/auth/resend-verification', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email }),
    });
    document.getElementById('auth-resend').style.display = 'none';
    closeAuthModal();
    showInfoModal(t('auth.resend_title'), t('auth.resend_body'));
}

async function doLogout() {
    await apiJson('/api/auth/logout', { method: 'POST' });
    currentAccount = null;
    renderAccountBar();
    updateGate();  // re-lock the UI
}

// ── My Sizings ───────────────────────────────────────────────────────────────

let sizingsCache = [];
let sizingsTab = 'mine';

// Which sources belong to each tab. "Mine" = configs I own plus any I pulled in
// by code; "Organization" = configs shared within my tenant (or, for scale
// users, other scale users' configs).
const SIZINGS_TAB_SOURCES = {
    mine: ['owned', 'linked'],
    org: ['tenant', 'scale'],
};

function openSizingsModal() {
    if (!currentAccount) { openAuthModal(); return; }
    // The code box is only useful to scale users (cross-tenant retrieval). The
    // search box is always shown.
    document.getElementById('sizings-code-box').style.display =
        currentAccount.is_scale ? 'flex' : 'none';
    document.getElementById('sizings-search').value = '';
    setSizingsTab('mine');
    document.getElementById('sizings-modal').style.display = 'flex';
    loadSizingsList();
}

function setSizingsTab(tab) {
    sizingsTab = tab;
    document.getElementById('sizings-tab-mine').classList.toggle('active', tab === 'mine');
    document.getElementById('sizings-tab-org').classList.toggle('active', tab === 'org');
    renderSizings();
}

function filterSizings() { renderSizings(); }

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
    if (!ok) {
        sizingsCache = [];
        document.getElementById('sizings-table-body').innerHTML = '';
        sizingsStatus(t('auth.sizings_load_error'), true);
        return;
    }
    sizingsCache = data;
    renderSizings();
}

// Render the active tab, filtered by the name/code search. The search only ever
// sees the loaded list (which is already scoped to what the user may view), so
// it can't surface names or codes outside the user's organisation.
function renderSizings() {
    const body = document.getElementById('sizings-table-body');
    const sources = SIZINGS_TAB_SOURCES[sizingsTab] || [];
    const term = (document.getElementById('sizings-search').value || '').trim().toLowerCase();

    // Tab counts (independent of the search filter).
    const mineCount = sizingsCache.filter(c => SIZINGS_TAB_SOURCES.mine.includes(c.source)).length;
    const orgCount = sizingsCache.filter(c => SIZINGS_TAB_SOURCES.org.includes(c.source)).length;
    document.getElementById('sizings-tab-mine').textContent = t('auth.sizings_tab_mine', { count: mineCount });
    document.getElementById('sizings-tab-org').textContent = t('auth.sizings_tab_org', { count: orgCount });

    let rows = sizingsCache.filter(c => sources.includes(c.source));
    if (term) {
        rows = rows.filter(c =>
            (c.name || '').toLowerCase().includes(term) ||
            (c.code || '').toLowerCase().includes(term));
    }

    if (!rows.length) {
        const msg = term ? t('auth.sizings_none_search')
            : (sizingsTab === 'mine' ? t('auth.sizings_none_mine')
                                     : t('auth.sizings_none_org'));
        body.innerHTML = `<tr><td colspan="6" class="no-recs">${esc(msg)}</td></tr>`;
        return;
    }

    const sourceLabel = {
        owned: t('auth.source.owned'),
        tenant: t('auth.source.tenant'),
        scale: t('auth.source.scale'),
        linked: t('auth.source.linked'),
    };
    body.innerHTML = rows.map(c => {
        const del = c.can_delete
            ? `<button class="btn btn-sm btn-muted" data-click='["deleteSizing",${c.id},"${esc(c.source)}"]'>${esc(c.source === 'linked' ? t('common.remove') : t('common.delete'))}</button>`
            : '';
        return `<tr>
            <td>${esc(c.name)}</td>
            <td>${esc(c.owner_email || '')}</td>
            <td><code class="sizing-code" title="${esc(t('auth.share_code_title'))}">${esc(c.code)}</code></td>
            <td>${esc(sourceLabel[c.source] || c.source)}</td>
            <td>${fmtDate(c.updated_at)}</td>
            <td><button class="btn btn-sm btn-primary" data-click='["loadSizing",${c.id}]'>${esc(t('auth.load'))}</button> ${del}</td>
        </tr>`;
    }).join('');
}

function fmtDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    return isNaN(d) ? '' : d.toLocaleString();
}

// Invoked from the header button or the My Sizings modal. All feedback goes
// through the in-app info modal (no native alerts).
// Returns true only when a sizing was actually persisted (so callers can chain
// a "save then continue" flow); false if it was cancelled or failed.
async function saveCurrentSizing() {
    if (!currentAccount) { openAuthModal(); return false; }
    const modalOpen = document.getElementById('sizings-modal').style.display === 'flex';

    if (!window.hasSizingToSave || !window.hasSizingToSave()) {
        showInfoModal(t('auth.nothing_to_save_title'), t('auth.nothing_to_save_body'));
        return false;
    }
    const snap = window.captureSizingState();
    if (!snap) {
        showInfoModal(t('auth.nothing_to_save_title'), t('auth.nothing_to_save_body'));
        return false;
    }
    // If a sizing we own is loaded, offer to update it in place.
    let action = 'new';
    if (loadedConfig && loadedConfig.canUpdate) {
        action = await askSaveChoice(loadedConfig.name);
        if (!action) return false;
    }

    if (action === 'update') {
        const { ok, data } = await apiJson('/api/configs/' + loadedConfig.id, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ payload: snap }),
        });
        if (!ok) {
            showInfoModal(t('auth.save_failed_title'), (data && data.error) || t('auth.generic_error'));
            return false;
        }
        loadedConfig = { id: data.id, name: data.name, canUpdate: true };
        showInfoModal(t('auth.sizing_updated_title'), t('auth.sizing_updated_body', { name: data.name }), data.code);
        if (modalOpen) loadSizingsList();
        return true;
    }

    const name = await promptSizingName();
    if (!name) return false;

    const { ok, data } = await apiJson('/api/configs/', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, payload: snap }),
    });
    if (!ok) {
        showInfoModal(t('auth.save_failed_title'), (data && data.error) || t('auth.generic_error'));
        return false;
    }
    loadedConfig = { id: data.id, name: data.name, canUpdate: true };
    showInfoModal(t('auth.sizing_saved_title'), t('auth.sizing_saved_body', { name: data.name }), data.code);
    if (modalOpen) loadSizingsList();
    return true;
}

// ── Save-choice modal (update loaded sizing vs save as new) ───────────────────

let _saveChoiceResolver = null;

function askSaveChoice(name) {
    return new Promise(resolve => {
        _saveChoiceResolver = resolve;
        document.getElementById('save-choice-msg').textContent =
            t('auth.save_choice_msg', { name });
        document.getElementById('save-choice-modal').style.display = 'flex';
    });
}

function _resolveSaveChoice(value) {
    document.getElementById('save-choice-modal').style.display = 'none';
    const r = _saveChoiceResolver;
    _saveChoiceResolver = null;
    if (r) r(value);
}

function closeSaveChoice() { _resolveSaveChoice(null); }
function chooseSave(which) { _resolveSaveChoice(which); }

async function loadSizing(id) {
    const { ok, data } = await apiJson('/api/configs/' + id);
    if (!ok) { sizingsStatus((data && data.error) || t('auth.load_failed'), true); return; }
    closeSizingsModal();
    await window.restoreSizingState(data.payload);
    loadedConfig = { id: data.id, name: data.name, canUpdate: data.source === 'owned' };
}

async function retrieveByCode() {
    const code = document.getElementById('sizings-code-input').value.trim();
    if (!code) return;
    const { ok, data } = await apiJson('/api/configs/code/' + encodeURIComponent(code));
    if (!ok) { sizingsStatus((data && data.error) || t('auth.code_not_found'), true); return; }
    document.getElementById('sizings-code-input').value = '';
    sizingsStatus(t('auth.code_loaded', { name: data.name }), false);
    loadSizingsList();
    closeSizingsModal();
    await window.restoreSizingState(data.payload);
    loadedConfig = { id: data.id, name: data.name, canUpdate: data.source === 'owned' };
}

async function deleteSizing(id, source) {
    const msg = source === 'linked' ? t('auth.confirm_remove') : t('auth.confirm_delete');
    if (!confirm(msg)) return;
    const { ok, data } = await apiJson('/api/configs/' + id, { method: 'DELETE' });
    if (!ok) { sizingsStatus((data && data.error) || t('auth.delete_failed'), true); return; }
    sizingsStatus(data.message || t('auth.done'), false);
    loadSizingsList();
}

// ── Name-sizing modal (replaces the native prompt) ───────────────────────────

let _nameResolver = null;

// Returns a Promise that resolves to the entered name, or null if cancelled.
function promptSizingName() {
    return new Promise(resolve => {
        _nameResolver = resolve;
        const input = document.getElementById('name-sizing-input');
        input.value = '';
        document.getElementById('name-sizing-modal').style.display = 'flex';
        input.focus();
    });
}

function _resolveName(value) {
    document.getElementById('name-sizing-modal').style.display = 'none';
    const r = _nameResolver;
    _nameResolver = null;
    if (r) r(value);
}

function closeNameModal() { _resolveName(null); }

function submitNameSizing(event) {
    event.preventDefault();
    const name = (document.getElementById('name-sizing-input').value || '').trim();
    if (!name) return;
    _resolveName(name);
}

// ── Info / confirmation modal (replaces native alert) ────────────────────────

function showInfoModal(title, message, code) {
    document.getElementById('info-modal-title').textContent = title;
    document.getElementById('info-modal-msg').textContent = message;
    const row = document.getElementById('info-modal-code-row');
    if (code) {
        document.getElementById('info-modal-code').textContent = code;
        const btn = document.getElementById('info-copy-btn');
        btn.textContent = t('auth.copy');
        row.style.display = 'flex';
    } else {
        row.style.display = 'none';
    }
    document.getElementById('info-modal').style.display = 'flex';
}

function closeInfoModal() {
    document.getElementById('info-modal').style.display = 'none';
}

function copyInfoCode() {
    const code = document.getElementById('info-modal-code').textContent;
    const btn = document.getElementById('info-copy-btn');
    const done = () => { btn.textContent = t('auth.copied'); setTimeout(() => { btn.textContent = t('auth.copy'); }, 1500); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(code).then(done, () => {});
    } else {
        // Fallback for non-secure contexts (plain http).
        const ta = document.createElement('textarea');
        ta.value = code;
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); done(); } catch (e) {}
        document.body.removeChild(ta);
    }
}

// ── Organization (tenant admin) ──────────────────────────────────────────────

function openOrgModal() {
    document.getElementById('org-modal').style.display = 'flex';
    document.getElementById('org-desc').textContent = t('auth.org_desc');
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
    if (!ok) { body.innerHTML = ''; orgStatus(t('auth.org_load_error'), true); return; }
    body.innerHTML = data.map(u => {
        const isSelf = currentAccount && u.id === currentAccount.id;
        const canDisable = !isSelf && u.role !== 'super_admin';
        const action = canDisable
            ? `<button class="btn btn-sm btn-muted" data-click='["disableOrgUser",${u.id}]'>${esc(t('auth.disable'))}</button>`
            : (isSelf ? `<span class="muted">${esc(t('auth.you'))}</span>` : '');
        return `<tr>
            <td>${esc(u.email)}</td>
            <td>${esc(u.role)}</td>
            <td>${fmtDate(u.last_login_at)}</td>
            <td>${action}</td>
        </tr>`;
    }).join('');
}

async function disableOrgUser(id) {
    if (!confirm(t('auth.confirm_disable'))) return;
    const { ok, data } = await apiJson('/api/admin/users/' + id + '/disable', { method: 'POST' });
    if (!ok) { orgStatus((data && data.error) || t('auth.disable_failed'), true); return; }
    orgStatus(t('auth.member_disabled'), false);
    loadOrgUsers();
}
