/* Helpers shared by every page script (index.html and admin.html).
 *
 * Loaded right after toast.js and before any page script, so app.js, auth.js,
 * projects.js, bom.js, wizard.js and admin.js can all call these as globals.
 * Kept in an IIFE like toast.js: nothing leaks but the names assigned to
 * window at the bottom (see the note in delegate.js about the shared scope).
 *
 *   esc(v)                          HTML-escape before innerHTML interpolation
 *   fetchJson(url, opts)            -> {ok, status, data}; never throws
 *   sendJson(url, method, body)     JSON request through fetchJson
 *   setStatus(elOrId, msg, isError, opts)   inline status / error line
 *   flashStatus(el, msg, ok, ms)    status line that clears itself
 *   withBusy(btn, fn)               disable button(s) while fn() runs
 *   openModal(id, {focus}) / closeModal(id)   .modal-overlay show / hide
 *   fmtDateTime(iso, {dateOnly})    locale date(+time); '' when not a date
 *   saveErrorText(status, data, vmCount)      message for a refused save
 */
(function () {
    'use strict';

    // HTML-escape any value before interpolating it into an innerHTML string.
    // Imported file fields (cluster/platform/VM names, OS strings) and other
    // user-supplied text are untrusted and must never reach innerHTML raw —
    // they can be saved into a shared sizing and fire as stored XSS for
    // whoever opens it. Both quote characters are escaped because most call
    // sites sit inside ="..." attribute values.
    function esc(v) {
        return String(v == null ? '' : v)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    }

    // ── JSON requests ───────────────────────────────────────────────────────
    // Every API call resolves to {ok, status, data}. A network failure (server
    // down, offline, aborted) resolves to ok:false / status 0 with the error
    // message in data.error, so call sites can treat it like a refused request
    // instead of wrapping each fetch in try/catch. A body that is not JSON
    // (204, HTML error page) gives data null. Cookies go along by default
    // (same-origin), so no call needs `credentials`.
    async function fetchJson(url, opts) {
        let resp;
        try {
            resp = await fetch(url, opts);
        } catch (e) {
            return { ok: false, status: 0, data: { error: String((e && e.message) || e) } };
        }
        let data = null;
        try { data = await resp.json(); } catch (e) { data = null; }
        return { ok: resp.ok, status: resp.status, data: data };
    }

    // POST/PUT/… a JSON body. `body` defaults to {} so an endpoint that expects
    // an object never gets an empty request.
    function sendJson(url, method, body, opts) {
        return fetchJson(url, Object.assign({
            method: method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body === undefined ? {} : body),
        }, opts || {}));
    }

    // ── inline status lines ─────────────────────────────────────────────────
    // Show `msg` in the element (id or node), hide it when msg is empty. The
    // ok/error class pair is swapped on top of whatever base classes the
    // element already carries in the markup, so each page keeps its own CSS:
    //   okClass / errClass   default 'upload-ok' / 'upload-error'; '' for none
    //   hide                 'display' (default, block/none), 'hidden'
    //                        (the hidden attribute) or false (never toggled —
    //                        inline spans that just empty their text)
    function setStatus(elOrId, msg, isError, opts) {
        const el = typeof elOrId === 'string' ? document.getElementById(elOrId) : elOrId;
        if (!el) return;
        const o = opts || {};
        const okClass = o.okClass === undefined ? 'upload-ok' : o.okClass;
        const errClass = o.errClass === undefined ? 'upload-error' : o.errClass;
        const show = !!msg;
        el.textContent = msg || '';
        if (okClass) el.classList.toggle(okClass, show && !isError);
        if (errClass) el.classList.toggle(errClass, show && !!isError);
        if (o.hide === 'hidden') el.hidden = !show;
        else if (o.hide !== false) el.style.display = show ? 'block' : 'none';
    }

    // A status that clears itself after `ms` (default 3 s): "Saved" next to a
    // save button. Classes 'ok' / 'err' on top of the element's base class.
    function flashStatus(el, msg, ok, ms) {
        if (typeof el === 'string') el = document.getElementById(el);
        if (!el) return;
        clearTimeout(el._flashTimer);
        el.textContent = msg;
        el.classList.toggle('ok', !!ok);
        el.classList.toggle('err', !ok);
        el._flashTimer = setTimeout(function () {
            el.textContent = '';
            el.classList.remove('ok', 'err');
        }, ms === undefined ? 3000 : ms);
    }

    // Disable one button (node, id, or an array of either) while the async
    // work runs; re-enabled in `finally`, so a thrown error still releases it.
    // Resolves to fn()'s result.
    async function withBusy(btn, fn) {
        const list = (Array.isArray(btn) ? btn : [btn])
            .map(function (b) { return typeof b === 'string' ? document.getElementById(b) : b; })
            .filter(Boolean);
        list.forEach(function (b) { b.disabled = true; });
        try {
            return await fn();
        } finally {
            list.forEach(function (b) { b.disabled = false; });
        }
    }

    // ── modals ──────────────────────────────────────────────────────────────
    // Every dialog is a .modal-overlay shown with display:flex. openModal /
    // closeModal are the only two things the page functions need; Escape and
    // a click on the backdrop are handled once, here, for all of them.
    //
    // Dismissing goes through the page's own close function — the one the
    // dialog's X button names in its data-click spec — never by hiding the
    // overlay blindly, so a close with side effects (closeResetModal drops
    // the token from the URL, closeCloneModal resets its state) behaves the
    // same from Escape as from the button, and a dialog that refuses to close
    // (the mandatory login modal) stays put.
    const openOrder = [];   // overlays in the order they were opened

    function overlayOf(id) {
        return typeof id === 'string' ? document.getElementById(id) : id;
    }

    function openModal(id, opts) {
        const el = overlayOf(id);
        if (!el) return null;
        el.style.display = 'flex';
        const i = openOrder.indexOf(el);
        if (i >= 0) openOrder.splice(i, 1);
        openOrder.push(el);
        const focus = opts && opts.focus;
        if (focus) {
            const f = typeof focus === 'string' ? document.getElementById(focus) : focus;
            if (f && f.focus) f.focus();
        }
        return el;
    }

    function closeModal(id) {
        const el = overlayOf(id);
        if (!el) return;
        el.style.display = 'none';
        const i = openOrder.indexOf(el);
        if (i >= 0) openOrder.splice(i, 1);
    }

    function isOpen(el) {
        return el.isConnected && el.style.display === 'flex';
    }

    // The dialog on top: the most recently opened one still showing, falling
    // back to DOM order for an overlay shown without openModal.
    function topModal() {
        for (let i = openOrder.length - 1; i >= 0; i--) {
            if (isOpen(openOrder[i])) return openOrder[i];
        }
        const all = document.querySelectorAll('.modal-overlay');
        for (let i = all.length - 1; i >= 0; i--) {
            if (isOpen(all[i])) return all[i];
        }
        return null;
    }

    function dismissModal(el) {
        const x = el.querySelector('.modal-close[data-click]');
        let fn = null;
        if (x) {
            try { fn = window[JSON.parse(x.getAttribute('data-click'))[0]]; } catch (e) { fn = null; }
        }
        if (typeof fn === 'function') fn.call(x);
        else closeModal(el);
    }

    document.addEventListener('keydown', function (e) {
        if (e.key !== 'Escape' || e.defaultPrevented) return;
        const el = topModal();
        if (el) dismissModal(el);
    });

    // Backdrop click: only when the press started on the backdrop too, so a
    // drag-select that ends outside the dialog box does not close it.
    let pressedOn = null;
    document.addEventListener('mousedown', function (e) { pressedOn = e.target; });
    document.addEventListener('click', function (e) {
        const el = e.target;
        if (el !== pressedOn || !el.classList || !el.classList.contains('modal-overlay')) return;
        if (isOpen(el)) dismissModal(el);
    });

    // ── formatting ──────────────────────────────────────────────────────────
    // Locale date and time for an ISO timestamp; '' for an empty or unparsable
    // value (an invalid date is never echoed raw into the page).
    function fmtDateTime(iso, opts) {
        if (!iso) return '';
        const d = new Date(iso);
        if (isNaN(d.getTime())) return '';
        return opts && opts.dateOnly ? d.toLocaleDateString() : d.toLocaleString();
    }

    // Message for a refused save. A save refused for size (413) names the VM
    // count, so the user knows what to cut (a merged estate of many files is
    // the case that reaches the cap); vmCount defaults to the loaded sizing's.
    function saveErrorText(status, data, vmCount) {
        if (status === 413) {
            const vms = vmCount != null ? vmCount
                : (window.currentVmCount ? window.currentVmCount() : 0);
            return window.t('merge.too_large', { vms: vms });
        }
        return (data && data.error) || window.t('auth.generic_error');
    }

    Object.assign(window, {
        esc, fetchJson, sendJson, setStatus, flashStatus, withBusy,
        openModal, closeModal, fmtDateTime, saveErrorText,
    });
})();
