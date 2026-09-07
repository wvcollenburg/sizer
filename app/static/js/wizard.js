// Guided Import Wizard — a VIEW LAYER over the existing import flow.
//
// This file adds NO sizing/exclusion logic. It relocates the existing import
// panels (upload, environment cards, VM-triage panel, sizing options, growth,
// recommendation list) into a stepped, one-at-a-time wizard and orchestrates
// the existing global functions in app.js:
//   uploadFile / displayImportResults / renderEnvWorkloadCards /
//   recalcRecommendations / saveAndReturnToProject ...
//
// A sizing holds ONE cluster (multi-cluster imports fan out into separate
// sizings at upload time via the fan-out chooser in app.js), so the wizard is
// five steps: Upload -> Environment -> VMs -> Options -> Recommendations.
//
// The wizard is the DEFAULT import experience; the untouched all-at-once page is
// kept as a "classic / advanced view" the user can switch to (and back) at any
// time. State is shared, so switching never loses work.
//
// Scope note on globals: app.js declares its FUNCTIONS with `function` (so they
// are properties of window and callable as window.foo / foo), but its STATE is
// declared with let/const and lives in the shared global lexical environment,
// NOT on window. wizard.js loads after app.js, so the bindings already exist.
//
// Mechanism: each existing panel is "portaled" (moved in the DOM) into a wizard
// step pane on activation and moved back to its original home on classic/exit.
// Only one pane is visible at a time, which naturally sandboxes the existing
// functions' in-place show/hide of the classic containers.
//
// CSP: every interactive element uses delegate.js data-click/data-change specs.
(function () {
    'use strict';

    var VIEW_KEY = 'sizerImportView';           // 'guided' (default) | 'classic'
    var PANES = 5;
    var state = { active: false, step: 1, reached: 1, imported: false, advOpen: false };

    function lastStep() { return PANES; }

    function t(k, vars) { return window.t ? window.t(k, vars) : k; }
    function esc(s) {
        if (window.esc) return window.esc(s);
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }
    function $(id) { return document.getElementById(id); }
    function qs(sel) { return document.querySelector(sel); }

    // ---- Portal helpers -----------------------------------------------------
    function portal(node, dest) {
        if (!node || !dest) return;
        if (!node._wizHome) {
            node._wizHome = { parent: node.parentNode, next: node.nextSibling };
        }
        dest.appendChild(node);
    }
    var _portaled = [];
    function track(node) { if (node && _portaled.indexOf(node) === -1) _portaled.push(node); }
    function portalInto(sel, destId) {
        var node = typeof sel === 'string' ? qs(sel) : sel;
        var dest = $(destId);
        if (node && dest) { portal(node, dest); track(node); }
    }
    function restoreAll() {
        for (var i = _portaled.length - 1; i >= 0; i--) {
            var n = _portaled[i];
            if (n._wizHome && n._wizHome.parent) {
                n._wizHome.parent.insertBefore(n, n._wizHome.next);
            }
        }
        _portaled = [];
    }

    // ---- Shell construction -------------------------------------------------
    var STEP_KEYS = [
        'wizard.step.upload', 'wizard.step.environment', 'wizard.step.vms',
        'wizard.step.options', 'wizard.step.recommendations'
    ];

    function buildShell() {
        var host = $('import-wizard');
        if (!host || host._built) return;
        var panes = '';
        for (var n = 1; n <= PANES; n++) {
            panes += '<div class="wiz-pane" id="wiz-pane-' + n + '" style="display:none">' +
                '<div class="wiz-pane-head"><h3 id="wiz-title-' + n + '"></h3>' +
                '<p class="wiz-intro" id="wiz-intro-' + n + '"></p></div>' +
                '<div class="wiz-chrome" id="wiz-chrome-' + n + '"></div>' +
                '<div class="wiz-body" id="wiz-body-' + n + '"></div>' +
                '<div class="wiz-chrome-after" id="wiz-after-' + n + '"></div>' +
                '</div>';
        }
        host.innerHTML =
            '<div class="wiz-topbar">' +
                '<div class="wiz-brand" data-i18n="wizard.title">Guided Import</div>' +
                '<button class="btn btn-sm btn-muted wiz-viewswitch" data-click=\'["wizardToClassic"]\'>' +
                    '<span data-i18n="wizard.to_classic">Switch to classic view</span></button>' +
            '</div>' +
            '<ol class="wizard-rail" id="wizard-rail"></ol>' +
            '<div class="wizard-steps">' + panes + '</div>' +
            '<div class="wizard-nav" id="wizard-nav"></div>';
        host._built = true;
    }

    function renderRail() {
        var rail = $('wizard-rail');
        if (!rail) return;
        var html = '';
        var last = lastStep();
        for (var n = 1; n <= last; n++) {
            var cls = 'wiz-railstep';
            if (n === state.step) cls += ' active';
            if (n < state.step) cls += ' done';
            var clickable = n <= state.reached;
            if (!clickable) cls += ' locked';
            var attr = clickable ? ' data-click=\'["wizardGoto",' + n + ']\'' : '';
            html += '<li class="' + cls + '"' + attr + '>' +
                '<span class="wiz-railnum">' + n + '</span>' +
                '<span class="wiz-raillabel">' + esc(t(STEP_KEYS[n - 1])) + '</span></li>';
        }
        rail.innerHTML = html;
    }

    function renderNav() {
        var nav = $('wizard-nav');
        if (!nav) return;
        var back = state.step > 1
            ? '<button class="btn btn-muted" data-click=\'["wizardBack"]\'>' + esc(t('wizard.back')) + '</button>'
            : '<span></span>';
        var next;
        if (state.step < lastStep()) {
            var disabled = (state.step === 1 && !state.imported) ? ' disabled' : '';
            next = '<button class="btn btn-primary" data-click=\'["wizardNext"]\'' + disabled + '>' +
                   esc(t('wizard.next')) + '</button>';
        } else {
            next = '<button class="btn btn-muted" data-click=\'["wizardGoto",1]\'>' + esc(t('wizard.start_over')) + '</button>';
        }
        nav.innerHTML = back + next;
    }

    // ---- Static portal of panels into panes ---------------------------------
    function portalPanels() {
        portalInto('.import-upload', 'wiz-body-1');
        portalInto('#env-summary', 'wiz-body-2');
        portalInto('.import-workload', 'wiz-body-2');
        portalInto('#vm-triage-panel', 'wiz-body-3');
        portalInto('.ratio-control', 'wiz-body-4');
        portalInto('.growth-control', 'wiz-body-4');
        // In the rail these panels start collapsed; as a wizard step they ARE
        // the content, so nothing should be hidden behind a chevron here.
        ['.ratio-control', '.growth-control'].forEach(function (sel) {
            var el = document.querySelector(sel);
            if (el) el.classList.remove('is-collapsed');
            var btn = el && el.querySelector('.panel-toggle');
            if (btn) btn.setAttribute('aria-expanded', 'true');
        });
        portalInto('#primary-recommendations', 'wiz-body-5');
    }

    // ---- Per-step chrome ----------------------------------------------------
    function stepChrome(n) {
        var chrome = $('wiz-chrome-' + n);
        var after = $('wiz-after-' + n);
        if (chrome) chrome.innerHTML = '';
        if (after) after.innerHTML = '';
        if (n === 2) return renderEnvCaveats();
        if (n === 4) return renderOptionsChrome();
        if (n === 5) return renderSaveClose(chrome ? chrome.parentNode : null);
    }

    function renderEnvCaveats() {
        var after = $('wiz-after-2');
        if (!after) return;
        var w = window._wizImportWarnings || [];
        if (!w.length) { after.innerHTML = ''; return; }
        // Each item is {code, params} from import_checks.py; translate here.
        after.innerHTML = '<div class="wiz-caveats"><div class="wiz-caveats-title">' +
            esc(t('wizard.env.caveats')) + '</div><ul>' +
            w.map(function (m) {
                var txt = m && m.code ? t('wizard.warn.' + m.code, m.params || {}) : String(m);
                return '<li>' + esc(txt) + '</li>';
            }).join('') + '</ul></div>';
    }

    function renderOptionsChrome() {
        var after = $('wiz-after-4');
        if (after) {
            after.innerHTML = '<button class="btn btn-link wiz-adv-toggle" data-click=\'["wizardToggleAdvanced"]\'>' +
                esc(state.advOpen ? t('wizard.options.hide_advanced') : t('wizard.options.show_advanced')) + '</button>';
        }
        applyAdvState();
    }

    function applyAdvState() {
        document.body.classList.toggle('wiz-adv-collapsed', !state.advOpen);
    }
    window.wizardToggleAdvanced = function () {
        state.advOpen = !state.advOpen;
        renderOptionsChrome();
    };

    // The flow ends on the recommendations step: picking an option on a card
    // already saves-and-returns, and this button is the explicit way out for a
    // user who is done without changing the pick.
    function renderSaveClose(pane) {
        if (!pane || typeof window.saveAndReturnToProject !== 'function') return;
        var host = $('wiz-save-close');
        if (!host) {
            host = document.createElement('div');
            host.id = 'wiz-save-close';
            host.className = 'wiz-export-actions wiz-save-close';
        }
        pane.appendChild(host);
        host.innerHTML = '<button class="btn btn-primary" data-click=\'["wizSaveAndClose"]\'>'
            + esc(t('wizard.export.save_close')) + '</button>';
    }

    window.wizSaveAndClose = function () {
        if (typeof window.saveAndReturnToProject === 'function') {
            window.saveAndReturnToProject();
        }
    };

    // ---- Navigation ---------------------------------------------------------
    function showPane(n) {
        for (var i = 1; i <= PANES; i++) {
            var p = $('wiz-pane-' + i);
            if (p) p.style.display = (i === n) ? 'block' : 'none';
        }
    }

    function onLeave(n) {
        if (n === 3 && typeof applyVmExclusions === 'function') {
            applyVmExclusions();       // commit exclusions/edits for later steps
        }
    }

    function onEnter(n) {
        if (n === 3) {
            if (typeof renderVmTable === 'function') renderVmTable();
            if (typeof filterVmTable === 'function') filterVmTable();
            if (typeof updateVmExclusionSummary === 'function') updateVmExclusionSummary();
        }
        if (n === 4) {
            if (typeof recalcRecommendations === 'function') recalcRecommendations();
            applyAdvState();
        } else {
            document.body.classList.remove('wiz-adv-collapsed');
        }
        if (n === 5 && typeof recalcRecommendations === 'function') recalcRecommendations();
    }

    function renderStep(n) {
        state.step = n;
        if (n > state.reached) state.reached = n;
        var titleEl = $('wiz-title-' + n); if (titleEl) titleEl.textContent = t(STEP_KEYS[n - 1]);
        var introEl = $('wiz-intro-' + n); if (introEl) introEl.textContent = t('wizard.intro.' + n);
        showPane(n);
        stepChrome(n);
        onEnter(n);
        renderRail();
        renderNav();
        var host = $('import-wizard');
        if (host && host.scrollIntoView) host.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function goto(n) {
        if (n < 1 || n > lastStep()) return;
        if (n > state.reached) return;
        if (n === 1 || state.imported) {
            if (n !== state.step) onLeave(state.step);
            renderStep(n);
        }
    }
    window.wizardGoto = function (n) { goto(n); };
    window.wizardBack = function () {
        if (state.step <= 1) return;
        // Capture the target BEFORE onLeave: onLeave side-effects (e.g.
        // applyVmExclusions → displayImportResults) can mutate state.step.
        var target = state.step - 1;
        onLeave(state.step);
        renderStep(target);
    };
    window.wizardNext = function () {
        if (state.step === 1 && !state.imported) return;
        if (state.step >= lastStep()) return;
        var target = state.step + 1;
        onLeave(state.step);
        renderStep(target);
    };

    // ---- Activation / view switching ---------------------------------------
    function viewPref() {
        try { return localStorage.getItem(VIEW_KEY) || 'guided'; } catch (e) { return 'guided'; }
    }
    function setViewPref(v) { try { localStorage.setItem(VIEW_KEY, v); } catch (e) { /* ignore */ } }

    function activate(startStep) {
        buildShell();
        portalPanels();
        state.active = true;
        document.body.classList.add('wiz-active');
        var wiz = $('import-wizard'); if (wiz) wiz.style.display = 'block';
        var classic = $('import-classic'); if (classic) classic.style.display = 'none';
        var sizing = $('sizing-results'); if (sizing) sizing.style.display = 'none';
        window.translateDOM && window.translateDOM(wiz);
        renderStep(startStep || (state.imported ? 2 : 1));
    }

    function deactivate() {
        if (!state.active) return;
        state.active = false;
        document.body.classList.remove('wiz-active', 'wiz-adv-collapsed');
        restoreAll();
        var wiz = $('import-wizard'); if (wiz) wiz.style.display = 'none';
        var classic = $('import-classic'); if (classic) classic.style.display = '';
    }

    // Public API used by app.js hooks.
    window.WizardAPI = {
        isActive: function () { return state.active; },
        onModeEnter: function (hasSummary) {
            state.imported = !!hasSummary;
            if (viewPref() === 'classic') {
                deactivate();
                var classic = $('import-classic'); if (classic) classic.style.display = '';
                showClassicPanels();
                ensureClassicSwitchLink();
                return;
            }
            activate(hasSummary ? 2 : 1);
        },
        onModeLeave: function () {
            deactivate();
            var sizing = $('sizing-results'); if (sizing) sizing.style.display = 'none';
        },
        // Snapshot the current step so a saved sizing can resume where the user
        // left off (see captureSizingState / restoreSizingState in app.js).
        getStep: function () { return { step: state.step, reached: state.reached }; },
        // Jump to a saved step after restore has rebuilt the import state. No-op
        // in classic view (the wizard isn't active).
        restoreToStep: function (n) {
            if (!state.active) return;
            state.imported = true;
            n = Math.max(1, Math.min(lastStep(), n || 2));
            state.reached = Math.max(state.reached, n);
            renderStep(n);
        },
        onImported: function (data) {
            state.imported = true;
            // Only auto-advance on a genuine new upload (user is on the upload
            // step). displayImportResults is ALSO called for internal re-renders
            // (applyVmExclusions, toggleLocalStorage) — those must not navigate,
            // or leaving the VM step would bounce the wizard back to step 2.
            if (state.active && state.step === 1) {
                window._wizImportWarnings = (data && data.import_warnings) || [];
                state.reached = Math.max(state.reached, 2);
                renderStep(2);
            }
        }
    };

    // Reveal the classic import panels. #import-results (environment summary +
    // workload) and #sizing-results are both hidden in guided mode (their
    // children are portaled into wizard panes), so classic must un-hide them.
    function showClassicPanels() {
        if (!state.imported) return;
        var ir = $('import-results'); if (ir) ir.style.display = 'block';
        var sizing = $('sizing-results'); if (sizing) sizing.style.display = 'block';
    }

    // Switch guided <-> classic within import mode (no reload; shared state).
    window.wizardToClassic = function () {
        setViewPref('classic');
        deactivate();
        showClassicPanels();
        ensureClassicSwitchLink();
    };
    window.wizardToGuided = function () {
        setViewPref('guided');
        // Resume on the step the user was on when they switched to classic, not
        // always step 2. (state persists across the toggle; deactivate doesn't
        // reset it.)
        activate(state.imported ? Math.max(2, Math.min(state.step, lastStep())) : 1);
    };

    // Inject a "Switch to guided wizard" control atop the classic import layout.
    function ensureClassicSwitchLink() {
        var classic = $('import-classic');
        if (!classic || $('wiz-to-guided-bar')) return;
        var bar = document.createElement('div');
        bar.id = 'wiz-to-guided-bar';
        bar.className = 'wiz-to-guided-bar';
        bar.innerHTML = '<button class="btn btn-sm btn-muted" data-click=\'["wizardToGuided"]\'>' +
            esc(t('wizard.to_guided')) + '</button>';
        classic.insertBefore(bar, classic.firstChild);
    }

    document.addEventListener('DOMContentLoaded', function () {
        buildShell();
        window.translateDOM && window.translateDOM($('import-wizard'));
    });
})();
