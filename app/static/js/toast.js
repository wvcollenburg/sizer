/* Transient notifications.
 *
 * SC//Design uses sonner (top-right, rich colours) for every success and
 * failure; we had inline status lines in some places, window.alert in others,
 * and nothing at all in most. This is the same affordance without the
 * dependency — one global stack, auto-dismissing, dismissible by click.
 *
 * Deliberately NOT a replacement for inline validation: a field error belongs
 * next to the field. Toasts are for the outcome of an action the user has
 * already moved on from — "sizing saved", "export queued", "rename failed".
 *
 * Usage (global, like the rest of the page scripts):
 *   toast('Sizing saved');
 *   toast('Could not rename project', 'error');
 *   toast('Export queued', 'info', 8000);
 *
 * Kept in its own IIFE so nothing leaks but `toast` — see the note in
 * delegate.js about the shared global scope these page scripts run in.
 */
(function () {
    var HOST_ID = 'toast-host';
    var DEFAULT_MS = 5000;

    function host() {
        var el = document.getElementById(HOST_ID);
        if (!el) {
            el = document.createElement('div');
            el.id = HOST_ID;
            el.className = 'toast-host';
            // Announced by screen readers without stealing focus; 'polite' so it
            // waits for the user to finish what they are doing.
            el.setAttribute('role', 'status');
            el.setAttribute('aria-live', 'polite');
            document.body.appendChild(el);
        }
        return el;
    }

    function dismiss(node) {
        if (!node || node.dataset.leaving) return;
        node.dataset.leaving = '1';
        node.classList.add('is-leaving');
        // Matches the .toast transition; remove after it has played out.
        setTimeout(function () {
            if (node.parentNode) node.parentNode.removeChild(node);
        }, 200);
    }

    /**
     * @param {string} message  plain text (never HTML — callers pass user data)
     * @param {string} [kind]   'success' | 'error' | 'warn' | 'info'
     * @param {number} [ms]     auto-dismiss delay; 0 keeps it until clicked
     */
    window.toast = function (message, kind, ms) {
        if (!message) return;
        var node = document.createElement('div');
        node.className = 'toast toast-' + (kind || 'info');
        // textContent, not innerHTML: messages carry project names, file names
        // and server errors, none of which are trusted markup.
        node.textContent = String(message);
        node.addEventListener('click', function () { dismiss(node); });
        host().appendChild(node);
        // Next frame, so the entry transition has a start state to animate from.
        requestAnimationFrame(function () { node.classList.add('is-in'); });

        var delay = ms === undefined ? DEFAULT_MS : ms;
        if (delay > 0) setTimeout(function () { dismiss(node); }, delay);
        return node;
    };

    // Replaced the blocking window.alert() calls on the failure paths (export
    // failed, required fields missing). alert() froze the page and had to be
    // dismissed before the user could act on what it said, which is the wrong
    // shape for "that didn't work, try again". Errors linger longer than the
    // default so they are not missed.
    window.toastError = function (message) {
        return window.toast(message, 'error', 9000);
    };
})();
