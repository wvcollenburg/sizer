// CSP-safe event delegation — replaces inline on* handlers so the Content-
// Security-Policy can forbid inline script entirely (script-src 'self', no
// 'unsafe-inline'). Loaded before the page scripts; the handler functions stay
// global and unchanged.
//
// Markup contract: an element opts in with a data-<event> attribute whose value
// is a JSON array [ "functionName", ...args ]. Args are passed straight through
// except these sentinels, resolved per-event against the element/event:
//   "$value"   -> el.value
//   "$checked" -> el.checked
//   "$this"    -> the element
//   "$event"   -> the DOM event
// The function is looked up on window and called with `this` = the element.
// No eval / new Function (both blocked by the strict CSP and unnecessary).
//
// Only literals / numeric ids / sentinels are ever baked into these specs — no
// user-supplied strings — so the JSON can't be broken or injected.
(function () {
    function resolve(arg, el, e) {
        switch (arg) {
            case "$value": return el.value;
            case "$checked": return el.checked;
            case "$this": return el;
            case "$event": return e;
            default: return arg;
        }
    }

    function invoke(el, e, raw) {
        var spec;
        try {
            spec = JSON.parse(raw);
        } catch (err) {
            console.error("delegate: bad spec", raw, err);
            return;
        }
        if (!Array.isArray(spec) || !spec.length) return;
        var fn = window[spec[0]];
        if (typeof fn !== "function") {
            console.error("delegate: no handler named", spec[0]);
            return;
        }
        var args = spec.slice(1).map(function (a) { return resolve(a, el, e); });
        fn.apply(el, args);
    }

    function bind(type, attr, preventDefault) {
        document.addEventListener(type, function (e) {
            var el = e.target.closest ? e.target.closest("[" + attr + "]") : null;
            if (!el) return;
            if (preventDefault) e.preventDefault();
            invoke(el, e, el.getAttribute(attr));
        }, type === "submit");  // capture submit so preventDefault is reliable
    }

    bind("click", "data-click", false);
    bind("change", "data-change", false);
    bind("input", "data-input", false);
    bind("keydown", "data-keydown", false);
    bind("submit", "data-submit", true);
})();
