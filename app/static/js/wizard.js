// Guided Import Wizard — a VIEW LAYER over the existing import flow.
//
// This file adds NO sizing/exclusion logic. It relocates the existing import
// panels (upload, environment cards, VM-triage panel, sizing options, growth,
// recommendation lists, multi-site review) into a stepped, one-at-a-time wizard
// and orchestrates the existing global functions in app.js:
//   uploadFile / displayImportResults / renderEnvWorkloadCards /
//   recalcRecommendations / toggleSeparateClusters / _selectClusterKey /
//   addDedicatedCluster / renderSelectedClustersTab / exportProposal ...
//
// The wizard is the DEFAULT import experience; the untouched all-at-once page is
// kept as a "classic / advanced view" the user can switch to (and back) at any
// time. State is shared, so switching never loses work.
//
// Scope note on globals: app.js declares its FUNCTIONS with `function` (so they
// are properties of window and callable as window.foo / foo), but its STATE is
// declared with let/const (sourceClusters, separateClusters, activeCluster,
// dedicatedClusters, SELECTED_KEY, ...). Those live in the shared global lexical
// environment, NOT on window, so we read them by BARE name. wizard.js loads
// after app.js, so the bindings already exist.
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
    var LAST = 7;                                // number of steps
    var state = { active: false, step: 1, reached: 1, imported: false, advOpen: false };

    function t(k, vars) { return window.t ? window.t(k, vars) : k; }
    function esc(s) {
        if (window.esc) return window.esc(s);
        return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
        });
    }
    function $(id) { return document.getElementById(id); }
    function qs(sel) { return document.querySelector(sel); }

    // Safe reads of app.js's lexical-global state (undefined if app.js not yet
    // loaded, which never happens in practice since we load after it).
    function S() {
        return {
            clusters: (typeof sourceClusters !== 'undefined') ? sourceClusters : [],
            separate: (typeof separateClusters !== 'undefined') ? separateClusters : false,
            active: (typeof activeCluster !== 'undefined') ? activeCluster : null,
            dedicated: (typeof dedicatedClusters !== 'undefined') ? dedicatedClusters : [],
            selectedKey: (typeof SELECTED_KEY !== 'undefined') ? SELECTED_KEY : '__selected__'
        };
    }

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
        'wizard.step.upload', 'wizard.step.environment', 'wizard.step.layout',
        'wizard.step.vms', 'wizard.step.options', 'wizard.step.recommendations',
        'wizard.step.export'
    ];

    function buildShell() {
        var host = $('import-wizard');
        if (!host || host._built) return;
        var panes = '';
        for (var n = 1; n <= LAST; n++) {
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
        for (var n = 1; n <= LAST; n++) {
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
        if (state.step < LAST) {
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
        portalInto('#vm-triage-panel', 'wiz-body-4');
        portalInto('.ratio-control', 'wiz-body-5');
        portalInto('.growth-control', 'wiz-body-5');
        portalInto('#dr-tabs', 'wiz-body-6');
        portalInto('#primary-recommendations', 'wiz-body-6');
        portalInto('#dr-recommendations', 'wiz-body-6');
        portalInto('#cluster-review', 'wiz-body-7');
    }

    // A wizard-owned per-cluster tab bar (steps 5/6). Deliberately separate from
    // the classic #cluster-tabs bar (which carries a "Selected clusters" review
    // tab that would break mid-wizard); this one only switches the active editing
    // cluster via the existing _selectClusterKey().
    function clusterBarHtml() {
        var s = S();
        if (!s.separate || !s.clusters || s.clusters.length < 2) return '';
        var tabs = s.clusters.map(function (c, i) {
            var cls = 'cluster-tab' + (c.name === s.active ? ' active' : '') +
                (s.dedicated && s.dedicated.indexOf(c.name) !== -1 ? ' cluster-tab-dedicated' : '');
            var badge = '<span class="cluster-tab-badge">' +
                t('cluster.tab_badge', { hosts: c.host_count, vms: c.vm_count }) + '</span>';
            return '<button class="' + cls + '" data-click=\'["wizSelectCluster",' + i + ']\'>' +
                esc(c.name) + badge + '</button>';
        }).join('');
        return '<div class="cluster-tabs wiz-clusterbar" style="display:flex"><div class="cluster-tab-row">' +
            tabs + '</div></div>';
    }

    window.wizSelectCluster = function (i) {
        var s = S();
        if (!s.clusters || !s.clusters[i]) return;
        if (typeof _selectClusterKey === 'function') _selectClusterKey(s.clusters[i].name);
        renderStep(state.step);   // follow the highlight / per-cluster chrome
    };

    // ---- Per-step chrome ----------------------------------------------------
    function stepChrome(n) {
        var chrome = $('wiz-chrome-' + n);
        var after = $('wiz-after-' + n);
        if (chrome) chrome.innerHTML = '';
        if (after) after.innerHTML = '';
        if (n === 2) return renderEnvCaveats();
        if (n === 3) return renderLayoutStep();
        if (n === 5) return renderOptionsChrome();
        if (n === 6) { if (chrome) { chrome.innerHTML = clusterBarHtml(); window.translateDOM && window.translateDOM(chrome); } return; }
        if (n === 7) return renderExportStep();
    }

    function renderEnvCaveats() {
        var after = $('wiz-after-2');
        if (!after) return;
        var w = window._wizImportWarnings || [];
        if (!w.length) { after.innerHTML = ''; return; }
        after.innerHTML = '<div class="wiz-caveats"><div class="wiz-caveats-title">' +
            esc(t('wizard.env.caveats')) + '</div><ul>' +
            w.map(function (m) { return '<li>' + esc(m) + '</li>'; }).join('') + '</ul></div>';
    }

    function renderLayoutStep() {
        var chrome = $('wiz-chrome-3');
        if (!chrome) return;
        var s = S();
        var multi = s.clusters && s.clusters.length > 1;
        if (multi) {
            var head = '<p class="wiz-layout-detected">' +
                esc(t('wizard.layout.detected_multi', { count: s.clusters.length })) + '</p>';
            var toggle = '<label class="checkbox-inline wiz-sep-toggle">' +
                '<input type="checkbox"' + (s.separate ? ' checked' : '') +
                ' data-change=\'["wizToggleSeparate","$checked"]\'>' +
                '<span>' + esc(t('import.separate_clusters')) + '</span></label>';
            var list = '';
            if (s.separate) {
                list = '<div class="wiz-cluster-list">' + s.clusters.map(function (c) {
                    var ded = s.dedicated && s.dedicated.indexOf(c.name) !== -1;
                    return '<div class="wiz-cluster-row' + (ded ? ' dedicated' : '') + '">' +
                        '<span class="wiz-cluster-name">' + esc(c.name) + '</span>' +
                        '<span class="wiz-cluster-meta">' + t('cluster.tab_badge', { hosts: c.host_count, vms: c.vm_count }) + '</span>' +
                        (ded ? '<span class="wiz-cluster-tag">' + esc(t('cluster.dedicated_platform')) + '</span>' : '') +
                        '</div>';
                }).join('') + '</div>' +
                '<div class="wiz-layout-actions">' +
                '<button class="btn btn-sm btn-muted" data-click=\'["wizAddDedicated"]\'>' +
                esc(t('cluster.add_dedicated')) + '</button></div>';
            }
            chrome.innerHTML = head + toggle + list;
        } else {
            chrome.innerHTML = '<p class="wiz-layout-single">' + esc(t('wizard.layout.single')) + '</p>';
        }
        window.translateDOM && window.translateDOM(chrome);
    }

    window.wizToggleSeparate = function (checked) {
        if (typeof toggleSeparateClusters === 'function') toggleSeparateClusters(checked);
        renderStep(3);
    };
    window.wizAddDedicated = function () {
        if (typeof addDedicatedCluster === 'function') addDedicatedCluster();
        renderStep(3);
    };

    function renderOptionsChrome() {
        var chrome = $('wiz-chrome-5');
        if (!chrome) return;
        var s = S();
        var applyAll = (s.separate && s.clusters && s.clusters.length > 1)
            ? '<button class="btn btn-sm btn-muted" data-click=\'["applyOptionsToAllClusters"]\'>' +
              esc(t('cluster.apply_all')) + '</button>' : '';
        chrome.innerHTML = clusterBarHtml() + '<div class="wiz-opts-actions">' + applyAll + '</div>';
        window.translateDOM && window.translateDOM(chrome);
        var after = $('wiz-after-5');
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

    function renderExportStep() {
        var chrome = $('wiz-chrome-7');
        if (!chrome) return;
        var s = S();
        var multi = s.separate && s.clusters && s.clusters.length > 1;
        if (multi) {
            chrome.innerHTML = '<p class="wiz-intro">' + esc(t('wizard.export.multi_intro')) + '</p>';
            // Enter the existing "Selected clusters" review, which populates
            // #cluster-review (portaled into this pane) with per-cluster cards and
            // the combined multi-site export buttons.
            if (typeof _selectClusterKey === 'function') _selectClusterKey(s.selectedKey);
            var rev = $('cluster-review');
            if (rev) rev.style.display = 'block';
        } else {
            // Editable formats (PowerPoint/Word) are Scale-users-only, exactly as
            // the classic recommendation cards gate them; PDF is always offered.
            var editable = (typeof canExportEditable === 'function') ? canExportEditable() : false;
            var buttons = '';
            if (editable) {
                buttons += '<button class="btn btn-export" data-click=\'["wizExport","pptx"]\'>PowerPoint</button>' +
                           '<button class="btn btn-export" data-click=\'["wizExport","docx"]\'>Word</button>';
            }
            buttons += '<button class="btn btn-export" data-click=\'["wizExport","pdf"]\'>PDF</button>';
            chrome.innerHTML =
                '<p class="wiz-intro">' + esc(t('wizard.export.single_intro')) + '</p>' +
                '<div class="wiz-export-actions">' + buttons + '</div>' +
                '<p class="wiz-export-note">' + esc(t('wizard.export.per_card_note')) + '</p>';
        }
    }

    window.wizExport = function (fmt) {
        if (typeof exportProposal === 'function') exportProposal('import', 0, fmt);
    };

    // ---- Navigation ---------------------------------------------------------
    function showPane(n) {
        for (var i = 1; i <= LAST; i++) {
            var p = $('wiz-pane-' + i);
            if (p) p.style.display = (i === n) ? 'block' : 'none';
        }
    }

    function onLeave(n) {
        if (n === 4 && typeof applyVmExclusions === 'function') {
            applyVmExclusions();       // commit exclusions/edits for later steps
        }
        if (n === 7) {
            var s = S();
            if (s.separate && typeof _selectClusterKey === 'function' && s.clusters && s.clusters.length) {
                _selectClusterKey(s.clusters[0].name);   // leave review mode
            }
        }
    }

    function onEnter(n) {
        if (n === 4) {
            if (typeof renderVmTable === 'function') renderVmTable();
            if (typeof filterVmTable === 'function') filterVmTable();
            if (typeof updateVmExclusionSummary === 'function') updateVmExclusionSummary();
        }
        if (n === 5) {
            if (typeof renderReplicationOptions === 'function') renderReplicationOptions();
            if (typeof renderDrClusterOption === 'function') renderDrClusterOption();
            if (typeof recalcRecommendations === 'function') recalcRecommendations();
            applyAdvState();
        } else {
            document.body.classList.remove('wiz-adv-collapsed');
        }
        if (n === 6 && typeof recalcRecommendations === 'function') recalcRecommendations();
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
        if (n < 1 || n > LAST) return;
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
        if (state.step >= LAST) return;
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
                var sizing = $('sizing-results'); if (sizing && hasSummary) sizing.style.display = 'block';
                ensureClassicSwitchLink();
                return;
            }
            activate(hasSummary ? 2 : 1);
        },
        onModeLeave: function () {
            deactivate();
            var sizing = $('sizing-results'); if (sizing) sizing.style.display = 'none';
        },
        onImported: function (data) {
            state.imported = true;
            // Only auto-advance on a genuine new upload (user is on the upload
            // step). displayImportResults is ALSO called for internal re-renders
            // (applyVmExclusions, toggleLocalStorage) — those must not navigate,
            // or leaving step 4 would bounce the wizard back to step 2.
            if (state.active && state.step === 1) {
                window._wizImportWarnings = (data && data.warnings) || [];
                state.reached = Math.max(state.reached, 2);
                renderStep(2);
            }
        }
    };

    // Switch guided <-> classic within import mode (no reload; shared state).
    window.wizardToClassic = function () {
        setViewPref('classic');
        deactivate();
        var sizing = $('sizing-results');
        if (sizing && state.imported) sizing.style.display = 'block';
        ensureClassicSwitchLink();
    };
    window.wizardToGuided = function () {
        setViewPref('guided');
        activate(state.imported ? 2 : 1);
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
