"""The company is "Scale Computing", never bare "Scale" (marketing rule,
2026-09-30: another US company is called Scale).

Covers everything a user or a reader of an export can see: every GUI
string in all 15 languages (static/js/lang), the export catalogs
(locales), the visible text of the page templates, and every string
literal in the Python code (server messages, finding texts, admin help)
except docstrings. Internal comments and docstrings may say "Scale".

Other languages inflect the name (Finnish "Scale Computingille", Estonian
"Scale Computingule", German "Scale-Computing-Benutzer"); those count as
the full name.

Run: .venv/bin/python -m pytest tests/test_brand_name.py -q
"""
import ast
import glob
import json
import os
import re

APP = os.path.join(os.path.dirname(__file__), "..", "app")
# Not followed by the rest of the name; the English words Scaled / Scales /
# Scaler are not the company. No \b on purpose: Japanese attaches particles
# directly ("Scaleが") and Finnish suffixes ("Scalelle") must still be caught.
BARE = re.compile(r"Scale(?!d\b|s\b|rs?\b)(?! Computing)(?!-Computing)")
# Not the company: code identifiers and file names that happen to contain it.
ALLOWED = re.compile(r"Scale_O\d|ScaleConfigLink|ScaleProjectLink|ScaleCare")


def _bare(text):
    return BARE.search(ALLOWED.sub("", text or ""))


def test_gui_strings_in_every_language():
    bad = []
    for path in sorted(glob.glob(os.path.join(APP, "static", "js", "lang", "*.js"))):
        text = open(path, encoding="utf-8").read()
        for key, value in re.findall(r'^\s*"([^"]+)":\s*"(.*)",?$', text, re.M):
            if _bare(value):
                bad.append("%s %s: %s" % (os.path.basename(path), key, value[:80]))
    assert not bad, "\n".join(bad)


def test_export_catalogs():
    bad = []
    for path in sorted(glob.glob(os.path.join(APP, "locales", "*.json"))):
        for key, value in json.load(open(path, encoding="utf-8")).items():
            if isinstance(value, str) and _bare(value):
                bad.append("%s %s: %s" % (os.path.basename(path), key, value[:80]))
    assert not bad, "\n".join(bad)


def test_visible_template_text():
    bad = []
    # recursive: the manual (templates/manual/) is the most prose of all
    for path in sorted(glob.glob(os.path.join(APP, "templates", "**", "*.html"), recursive=True)):
        html = re.sub(r"<!--.*?-->", "", open(path, encoding="utf-8").read(), flags=re.S)
        html = re.sub(r"<script\b.*?</script>", "", html, flags=re.S)
        for m in BARE.finditer(ALLOWED.sub("", html)):
            bad.append("%s: …%s…" % (os.path.basename(path), html[max(0, m.start() - 40):m.end() + 40]))
    assert not bad, "\n".join(bad)


def _docstrings(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                ids.add(id(body[0].value))
    return ids


def test_python_string_literals():
    bad = []
    for path in sorted(glob.glob(os.path.join(APP, "**", "*.py"), recursive=True)):
        tree = ast.parse(open(path, encoding="utf-8").read())
        skip = _docstrings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in skip and _bare(node.value):
                bad.append("%s:%d: %s" % (os.path.relpath(path, APP), node.lineno, node.value[:80]))
    assert not bad, "\n".join(bad)
