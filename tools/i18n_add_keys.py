#!/usr/bin/env python3
"""Add GUI translation keys to every app/static/js/lang/<code>.js safely.

Usage:
    .venv/bin/python tools/i18n_add_keys.py manifest.json [translations.json]

``manifest.json`` is {key: English text}. ``translations.json`` (optional) is
{code: {key: text}}; a language without a translation for a key gets the
English text (the parity tests accept that; translate before shipping).

Why a tool: the lang files are JavaScript, not JSON, and the header itself
contains ``{}`` — rewriting them by slicing on the first brace corrupts them
(see the frontend-global-scope-traps note). This edits only the entry body
between the assignment header and the closing ``};``, one ``"key": "value",``
per line, which is exactly the shape tests/test_i18n_parity.py parses.
"""
import json
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "app"))
from i18n import SUPPORTED_LANGS  # noqa: E402

LANG_DIR = os.path.join(ROOT, "app", "static", "js", "lang")
HEADER = re.compile(r"^\(window\.I18N_LANGS = window\.I18N_LANGS \|\| \{\}\)\.(\w+) = \{\n", re.M)
ENTRY = re.compile(r'^\s*"([^"]+)":\s*"(.*)",?$', re.M)


def js_str(s):
    return json.dumps(s, ensure_ascii=False)


def add_keys(new, translations=None, replace=False):
    """new: {key: english}; translations: {code: {key: text}}. Returns the
    number of keys added per language. With ``replace`` an existing key's
    value is overwritten (used to drop in real translations later)."""
    translations = translations or {}
    added = {}
    for code in SUPPORTED_LANGS:
        path = os.path.join(LANG_DIR, code + ".js")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        m = HEADER.search(text)
        assert m and m.group(1) == code, path
        body_start = m.end()
        tail = text.rstrip()
        assert tail.endswith("};"), path
        body = text[body_start:len(tail) - 2]
        lines = [l for l in body.rstrip("\n").split("\n") if l.strip()]
        existing = {}
        for i, line in enumerate(lines):
            em = ENTRY.match(line)
            if em:
                existing[em.group(1)] = i
        per_lang = translations.get(code) or {}
        count = 0
        for key, english in new.items():
            val = per_lang.get(key, english)
            if "\n" in val:
                raise ValueError("multi-line value for %s" % key)
            if key in existing:
                if replace:
                    lines[existing[key]] = "  %s: %s," % (js_str(key), js_str(val))
                continue
            lines.append("  %s: %s," % (js_str(key), js_str(val)))
            count += 1
        # normalise trailing commas: every line but the last ends with one
        for i, line in enumerate(lines):
            stripped = line.rstrip()
            if i < len(lines) - 1 and not stripped.endswith(","):
                stripped += ","
            if i == len(lines) - 1 and stripped.endswith(","):
                stripped = stripped[:-1]
            lines[i] = stripped
        out = text[:body_start] + "\n".join(lines) + "\n};\n"
        assert out.count("{") == out.count("}"), path
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(out)
        added[code] = count
    return added


def main(argv):
    replace = "--replace" in argv
    args = [a for a in argv[1:] if a != "--replace"]
    if not args:
        print(__doc__)
        return 2
    with open(args[0], encoding="utf-8") as fh:
        new = json.load(fh)
    translations = None
    if len(args) > 1:
        with open(args[1], encoding="utf-8") as fh:
            translations = json.load(fh)
    added = add_keys(new, translations, replace=replace)
    print("added:", added)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
