"""Locale parity guards for the 15 shipped languages.

A half-finished rename or a new feature's strings landing in only one file is
invisible at runtime: `translator()` falls back to English and `window.t()`
falls back to the key, so a missing translation looks like a working app in
English rather than an error. These tests make that drift fail in CI instead.

Two independent catalogs are covered (see [[add-language-gui-and-exports]]):
  * export strings — app/locales/<code>.json, consumed by i18n.translator()
  * GUI strings    — app/static/js/lang/<code>.js, consumed by window.t()

Run from the repo root:

    .venv/bin/python -m pytest tests/test_i18n_parity.py -q
"""
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from i18n import SUPPORTED_LANGS  # noqa: E402

APP = os.path.join(os.path.dirname(__file__), "..", "app")
LOCALES = os.path.join(APP, "locales")
LANG_JS = os.path.join(APP, "static", "js", "lang")
BASE = "en"


# ── loading ──────────────────────────────────────────────────────────────────

def _export_catalog(code):
    with open(os.path.join(LOCALES, code + ".json"), encoding="utf-8") as f:
        return json.load(f)


def _gui_catalog(code):
    """Parse app/static/js/lang/<code>.js. The file is an assignment of a flat
    object literal, so the keys are read straight off the `"key":` lines rather
    than by evaluating JavaScript."""
    with open(os.path.join(LANG_JS, code + ".js"), encoding="utf-8") as f:
        text = f.read()
    body = text[text.index("{"):text.rindex("}") + 1]
    return dict(re.findall(r'^\s*"([^"]+)":\s*"(.*)",?$', body, re.M))


def _placeholders(s):
    return set(re.findall(r"\{(\w+)\}", s))


def _source_files(dirname, suffix):
    return [os.path.join(dirname, n) for n in sorted(os.listdir(dirname))
            if n.endswith(suffix)]


TRANSLATED = [c for c in SUPPORTED_LANGS if c != BASE]


# ── key parity ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code", TRANSLATED)
def test_export_locale_matches_english_keys(code):
    base, other = _export_catalog(BASE), _export_catalog(code)
    assert not set(base) - set(other), (
        f"{code}.json is missing keys present in en.json: "
        f"{sorted(set(base) - set(other))}")
    assert not set(other) - set(base), (
        f"{code}.json has keys that no longer exist in en.json (stale after a "
        f"rename?): {sorted(set(other) - set(base))}")


@pytest.mark.parametrize("code", TRANSLATED)
def test_gui_locale_matches_english_keys(code):
    base, other = _gui_catalog(BASE), _gui_catalog(code)
    assert not set(base) - set(other), (
        f"{code}.js is missing keys present in en.js: "
        f"{sorted(set(base) - set(other))}")
    assert not set(other) - set(base), (
        f"{code}.js has keys that no longer exist in en.js (stale after a "
        f"rename?): {sorted(set(other) - set(base))}")


# ── placeholder parity ───────────────────────────────────────────────────────
# A translation carrying a placeholder English doesn't have can never be filled:
# str.format raises KeyError, translator() swallows it, and the reader sees a
# literal "{count}" in a customer-facing document.

@pytest.mark.parametrize("code", TRANSLATED)
def test_export_translations_add_no_unknown_placeholders(code):
    base, other = _export_catalog(BASE), _export_catalog(code)
    bad = {
        k: sorted(_placeholders(v) - _placeholders(base[k]))
        for k, v in other.items()
        if k in base and isinstance(v, str) and _placeholders(v) - _placeholders(base[k])
    }
    assert not bad, f"{code}.json has placeholders English lacks: {bad}"


@pytest.mark.parametrize("code", TRANSLATED)
def test_gui_translations_add_no_unknown_placeholders(code):
    base, other = _gui_catalog(BASE), _gui_catalog(code)
    bad = {
        k: sorted(_placeholders(v) - _placeholders(base[k]))
        for k, v in other.items()
        if k in base and _placeholders(v) - _placeholders(base[k])
    }
    assert not bad, f"{code}.js has placeholders English lacks: {bad}"


# ── referenced keys exist ────────────────────────────────────────────────────
# Catches the other half of a rename: code still asking for a key that no
# catalog defines. Only string literals are checked — a key built by
# concatenation (t('wizard.intro.' + step)) leaves a trailing dot and is skipped.

def _literal_keys(text, patterns):
    found = set()
    for pat in patterns:
        found |= set(re.findall(pat, text))
    return {k for k in found if not k.endswith(".")}


def test_python_referenced_export_keys_exist():
    base = _export_catalog(BASE)
    missing = {}
    for path in _source_files(APP, ".py"):
        with open(path, encoding="utf-8") as f:
            keys = _literal_keys(f.read(), [r'\bt9?n?\(\s*"([a-z][\w.]*)"'])
        absent = sorted(keys - set(base))
        if absent:
            missing[os.path.basename(path)] = absent
    assert not missing, f"export keys referenced but not defined in en.json: {missing}"


def test_js_referenced_gui_keys_exist():
    base = _gui_catalog(BASE)
    js_dir = os.path.join(APP, "static", "js")
    missing = {}
    for path in _source_files(js_dir, ".js"):
        with open(path, encoding="utf-8") as f:
            keys = _literal_keys(f.read(), [r"\bt\(\s*'([a-z][\w.]*)'",
                                            r'\bt\(\s*"([a-z][\w.]*)"'])
        absent = sorted(keys - set(base))
        if absent:
            missing[os.path.basename(path)] = absent
    assert not missing, f"GUI keys referenced but not defined in en.js: {missing}"


def test_template_referenced_gui_keys_exist():
    base = _gui_catalog(BASE)
    with open(os.path.join(APP, "templates", "index.html"), encoding="utf-8") as f:
        keys = set(re.findall(r'data-i18n="([^"]+)"', f.read()))
    assert not keys - set(base), (
        f"index.html references GUI keys not in en.js: {sorted(keys - set(base))}")


def test_every_supported_language_ships_both_catalogs():
    """A language in SUPPORTED_LANGS with only one of its two files is the
    failure mode [[add-language-gui-and-exports]] exists to prevent."""
    for code in SUPPORTED_LANGS:
        assert os.path.exists(os.path.join(LOCALES, code + ".json")), \
            f"{code} is in SUPPORTED_LANGS but app/locales/{code}.json is missing"
        assert os.path.exists(os.path.join(LANG_JS, code + ".js")), \
            f"{code} is in SUPPORTED_LANGS but app/static/js/lang/{code}.js is missing"
