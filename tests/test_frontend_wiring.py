"""Static guards for the CSP-safe event delegation.

Handlers are referenced from markup by name (``data-click='["openProject",4]'``)
and looked up on ``window`` at click time. A typo therefore fails *silently in
the browser* — delegate.js logs to the console and the button simply does
nothing. These tests turn that into a build failure instead.

Run: .venv/bin/python -m pytest tests/test_frontend_wiring.py -q
"""
import json
import os
import re

APP = os.path.join(os.path.dirname(__file__), "..", "app")
TEMPLATES = os.path.join(APP, "templates")
JS_DIR = os.path.join(APP, "static", "js")

DELEGATED = ("click", "change", "input", "keydown", "submit")


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _js_sources():
    """Every page script, plus the dynamically rendered markup inside them."""
    out = {}
    for name in sorted(os.listdir(JS_DIR)):
        if name.endswith(".js"):
            out[name] = _read(os.path.join(JS_DIR, name))
    return out


def _defined_handlers(sources):
    """Names reachable as window.<name> at click time: top-level function
    declarations (implicitly global in a classic script), explicit
    window.<name> = assignments, and Object.assign(window, {...}) exports."""
    names = set()
    for text in sources.values():
        names |= set(re.findall(r"^(?:async\s+)?function\s+(\w+)", text, re.M))
        names |= set(re.findall(r"^\s*window\.(\w+)\s*=", text, re.M))
        for blob in re.findall(r"Object\.assign\(window,\s*\{(.*?)\}\)", text, re.S):
            names |= set(re.findall(r"(\w+)\s*[,:]", blob))
    return names


def _referenced_handlers(text):
    """(handler, raw spec) for every delegated attribute in ``text``."""
    found = []
    for event in DELEGATED:
        for raw in re.findall(r"data-%s='([^']*)'" % event, text):
            found.append(raw)
    return found


def test_every_template_handler_exists():
    defined = _defined_handlers(_js_sources())
    missing = {}
    for name in sorted(os.listdir(TEMPLATES)):
        if not name.endswith(".html"):
            continue
        for raw in _referenced_handlers(_read(os.path.join(TEMPLATES, name))):
            spec = json.loads(raw)          # malformed JSON fails loudly here
            if spec[0] not in defined:
                missing.setdefault(name, []).append(spec[0])
    assert not missing, (
        "markup references handlers that no script defines — the button would "
        f"silently do nothing: {missing}")


def test_every_rendered_handler_exists():
    """Rows built in JS carry the same contract as the static markup."""
    sources = _js_sources()
    defined = _defined_handlers(sources)
    missing = {}
    for name, text in sources.items():
        for raw in re.findall(r"data-(?:click|change|input|keydown)='\[([^\]]*)\]'", text):
            handler = re.match(r'\s*"(\w+)"', raw)
            if handler and handler.group(1) not in defined:
                missing.setdefault(name, []).append(handler.group(1))
    assert not missing, f"rendered markup references undefined handlers: {missing}"


def test_delegate_specs_are_valid_json():
    """The delegate JSON.parses each spec; a broken one disables that control."""
    bad = {}
    for name in sorted(os.listdir(TEMPLATES)):
        if not name.endswith(".html"):
            continue
        for raw in _referenced_handlers(_read(os.path.join(TEMPLATES, name))):
            try:
                spec = json.loads(raw)
            except ValueError as exc:
                bad.setdefault(name, []).append((raw[:60], str(exc)))
                continue
            if not isinstance(spec, list) or not spec:
                bad.setdefault(name, []).append((raw[:60], "not a non-empty array"))
    assert not bad, f"malformed delegate specs: {bad}"


def test_no_top_level_name_collisions_between_scripts():
    """Page scripts are classic scripts sharing one global scope.

    Two files declaring the same top-level `function` do not error — the later
    one silently replaces the earlier, so the *other* file's feature breaks with
    no clue in the console. Two `const`/`let` declarations of the same name do
    throw, which kills the whole script. Both are worth failing the build over.
    """
    sources = _js_sources()
    failures = {}
    # Only scripts loaded by the SAME template share a scope: admin.js and
    # app.js never meet, so identical names there are harmless.
    for template in sorted(n for n in os.listdir(TEMPLATES) if n.endswith(".html")):
        html = _read(os.path.join(TEMPLATES, template))
        loaded = [os.path.basename(m) for m in
                  re.findall(r"asset\('(js/[a-z_]+\.js)'\)", html)]
        declared = {}
        for name in loaded:
            text = sources.get(name)
            if text is None:
                continue
            found = set(re.findall(r"^(?:async\s+)?function\s+(\w+)", text, re.M))
            found |= set(re.findall(r"^(?:const|let|var)\s+(\w+)", text, re.M))
            for symbol in found:
                declared.setdefault(symbol, []).append(name)
        clashes = {s: files for s, files in declared.items() if len(files) > 1}
        if clashes:
            failures[template] = clashes

    assert not failures, (
        "these top-level names are declared by more than one script on the same "
        f"page and will collide in the shared global scope: {failures}")


def test_locale_files_are_syntactically_valid_javascript():
    """The locale files are executable JS, not JSON.

    Checking only the object body (as the parity tests do) cannot see a broken
    assignment header — which is exactly how a rewrite once shipped 15 files
    that parsed as data and crashed as script.
    """
    lang_dir = os.path.join(JS_DIR, "lang")
    for name in sorted(os.listdir(lang_dir)):
        if not name.endswith(".js"):
            continue
        code = name[:-3]
        text = _read(os.path.join(lang_dir, name))
        expected = "(window.I18N_LANGS = window.I18N_LANGS || {})." + code + " = {"
        assert expected in text, (
            f"{name} is missing its assignment header `{expected}` — the file "
            f"would raise a SyntaxError in the browser")
        # Braces must balance, so the object actually closes before the `;`.
        assert text.count("{") == text.count("}"), f"{name} has unbalanced braces"
        assert text.rstrip().endswith("};"), f"{name} does not close its object"


def test_project_screens_are_present_and_scripted():
    """The project layer is only reachable if its markup and script both ship."""
    html = _read(os.path.join(TEMPLATES, "index.html"))
    for marker in ('id="project-home"', 'id="project-view"',
                   'id="new-project-modal"', 'id="sizing-modal"',
                   'id="project-settings-modal"', "js/projects.js"):
        assert marker in html, f"index.html is missing {marker}"
