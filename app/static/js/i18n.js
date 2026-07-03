// Client-side i18n engine for the SC// Infrastructure Sizer.
//
// The active language is chosen SERVER-SIDE (see current_lang() in app.py) and
// baked into <html lang="..">, so this file never has to guess — it just reads
// that code and swaps text. Locale dictionaries are plain data files loaded
// before this script; each registers window.I18N_LANGS[<code>] = { key: str }.
// English (en.js) is always loaded as the fallback base.
//
// Contract used by the rest of the app:
//   window.t(key, vars)      -> translated string with {placeholder} interpolation
//   window.translateDOM(root) -> apply data-i18n* attributes under `root`
//   window.setLang(code)     -> remember choice in a cookie and reload
//
// A cookie is written ONLY by setLang (i.e. only when the user explicitly picks
// a language). Browser auto-detection happens server-side from Accept-Language
// and never persists a cookie.
(function () {
    var LANGS = window.I18N_LANGS || (window.I18N_LANGS = {});
    var SUPPORTED = ['en', 'de', 'fr', 'nl'];
    var DEFAULT = 'en';

    // Active code comes from the server-rendered <html lang="..">. Guard against
    // anything unexpected so we always land on a supported dictionary.
    var active = (document.documentElement.getAttribute('lang') || DEFAULT).toLowerCase();
    if (SUPPORTED.indexOf(active) === -1) active = DEFAULT;
    window.I18N_ACTIVE = active;
    window.I18N_SUPPORTED = SUPPORTED;

    var base = LANGS[DEFAULT] || {};
    var dict = LANGS[active] || base;

    // Translate a key. Falls back active -> English -> the raw key, so a missing
    // translation degrades to English rather than blanking out or showing
    // "undefined". `vars` fills {name} placeholders.
    function t(key, vars) {
        var s = dict[key];
        if (s == null) s = base[key];
        if (s == null) s = key;
        if (vars) {
            s = s.replace(/\{(\w+)\}/g, function (m, name) {
                return (vars[name] != null) ? vars[name] : m;
            });
        }
        return s;
    }
    window.t = t;

    // Apply translations to every data-i18n* element under `root` (default: whole
    // document). Safe to call repeatedly and on freshly-built subtrees.
    function translateDOM(root) {
        root = root || document;
        var el, i, nodes;

        nodes = root.querySelectorAll('[data-i18n]');
        for (i = 0; i < nodes.length; i++) {
            el = nodes[i];
            el.textContent = t(el.getAttribute('data-i18n'));
        }
        nodes = root.querySelectorAll('[data-i18n-html]');
        for (i = 0; i < nodes.length; i++) {
            el = nodes[i];
            el.innerHTML = t(el.getAttribute('data-i18n-html'));
        }
        nodes = root.querySelectorAll('[data-i18n-ph]');
        for (i = 0; i < nodes.length; i++) {
            el = nodes[i];
            el.setAttribute('placeholder', t(el.getAttribute('data-i18n-ph')));
        }
        nodes = root.querySelectorAll('[data-i18n-title]');
        for (i = 0; i < nodes.length; i++) {
            el = nodes[i];
            el.setAttribute('title', t(el.getAttribute('data-i18n-title')));
        }
    }
    window.translateDOM = translateDOM;

    // Persist an explicit choice and reload so the server re-renders with the new
    // locale (correct <html lang>, right dictionary file). This is the only place
    // the cookie is set. One year, lax so it survives normal navigation.
    function setLang(code) {
        code = (code || '').toLowerCase();
        if (SUPPORTED.indexOf(code) === -1) return;
        document.cookie = 'lang=' + code + '; path=/; max-age=31536000; SameSite=Lax';
        window.location.reload();
    }
    window.setLang = setLang;

    // FOUC guard: for non-English the server marks <html data-i18n-pending> and
    // CSS hides the body until we've swapped the text, so users never see an
    // English flash. English needs no swap and is never hidden. Clear the flag
    // once the initial pass is done.
    document.addEventListener('DOMContentLoaded', function () {
        translateDOM(document);
        document.documentElement.removeAttribute('data-i18n-pending');
    });
})();
