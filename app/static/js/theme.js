/* Light/dark theme switch.
 *
 * SC//Design ships a full dark token set and drives it with next-themes; this
 * is the same behaviour without the framework. All the colour work lives in
 * style.css under :root[data-theme="dark"] — this file only decides which
 * theme is active and stamps it on <html>.
 *
 * Loaded in <head>, BEFORE the stylesheet renders anything, so the attribute
 * is already in place on first paint. Deferring it to page scripts would show
 * a light flash on every load for dark-mode users. It is a separate file
 * rather than an inline <script> because the CSP is script-src 'self' with no
 * 'unsafe-inline' (see delegate.js).
 *
 * Precedence: an explicit choice (localStorage) beats the OS setting. With no
 * stored choice we follow the OS and keep following it if the user changes it
 * mid-session.
 */
(function () {
    var KEY = 'theme';           // 'light' | 'dark' — absent means "follow OS"
    var root = document.documentElement;

    function stored() {
        try {
            var v = localStorage.getItem(KEY);
            return v === 'light' || v === 'dark' ? v : null;
        } catch (e) {
            // Private mode / storage disabled — fall back to the OS setting.
            return null;
        }
    }

    function systemPrefersDark() {
        return !!(window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches);
    }

    function apply(theme) {
        root.setAttribute('data-theme', theme);
    }

    apply(stored() || (systemPrefersDark() ? 'dark' : 'light'));

    // Follow the OS while the user has expressed no preference of their own.
    if (window.matchMedia) {
        var mq = window.matchMedia('(prefers-color-scheme: dark)');
        var onChange = function (e) {
            if (!stored()) apply(e.matches ? 'dark' : 'light');
        };
        if (mq.addEventListener) mq.addEventListener('change', onChange);
        else if (mq.addListener) mq.addListener(onChange);
    }

    // Invoked from the header button via delegate.js (data-click).
    window.toggleTheme = function () {
        var next = root.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
        apply(next);
        try {
            localStorage.setItem(KEY, next);
        } catch (e) {
            // Not persisting is survivable; the theme still applies this session.
        }
    };
})();
