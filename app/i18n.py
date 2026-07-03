"""Server-side i18n for generated exports (PPTX / DOCX / PDF / charts / diagrams).

The web UI is translated client-side (app/static/js/lang/<code>.js). Documents are
built in Python, so they need their own catalog + translator. Strings live in
app/locales/<code>.json as flat {key: value} maps (English is the base/fallback).

Usage in a generator:
    from i18n import translator, font_for
    t = translator(lang)
    title = t("export.pptx.current_environment")
    subtitle = t("export.pptx.capacity_planning", years=p["years"])
    run.font.name = font_for(lang, TITLE_FONT)   # CJK-safe font selection

This module has no Flask dependency so export code can import it without a cycle.
The active language is resolved from the request in app.py (pick_lang) and passed
down — exports follow whatever language the UI is showing, cookie or not.
"""
import json
import os

# The languages we ship translations for. English first (base/fallback). Single
# source of truth — app.py imports this list rather than keeping its own.
SUPPORTED_LANGS = ["en", "de", "fr", "nl", "es", "it", "pt", "ja",
                   "sv", "da", "no", "fi", "et", "lv", "lt"]
DEFAULT_LANG = "en"

# Endonyms (each language's own name) for the header language menu.
LANG_NAMES = {
    "en": "English", "de": "Deutsch", "fr": "Français", "nl": "Nederlands",
    "es": "Español", "it": "Italiano", "pt": "Português", "ja": "日本語",
    "sv": "Svenska", "da": "Dansk", "no": "Norsk", "fi": "Suomi",
    "et": "Eesti", "lv": "Latviešu", "lt": "Lietuvių",
}

# Languages whose script the branding font (Martel Sans) can't render, so document
# text runs must switch to a CJK-capable font. Extend as zh/ko are added.
CJK_LANGS = {"ja"}
# Installed via the Dockerfile (fonts-noto-cjk); covers JP/KR/SC/TC + Latin.
CJK_FONT = "Noto Sans CJK JP"

_LOCALES_DIR = os.path.join(os.path.dirname(__file__), "locales")


def _load_catalogs():
    """Load every app/locales/<code>.json into {lang: {key: str}}. Missing or
    unreadable files degrade to empty (translator then falls back to English or the
    raw key) so a partial rollout never crashes export generation."""
    catalogs = {}
    for code in SUPPORTED_LANGS:
        path = os.path.join(_LOCALES_DIR, code + ".json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                catalogs[code] = json.load(f)
        except (OSError, ValueError):
            catalogs[code] = {}
    return catalogs


CATALOGS = _load_catalogs()


def translator(lang):
    """Return a t(key, **vars) callable for `lang`.

    Lookup order: active locale -> English base -> the raw key. `{name}`
    placeholders are filled from vars via str.format; a formatting error (e.g. a
    stray brace or a missing var) falls back to the unformatted string rather than
    raising mid-document."""
    lang = (lang or DEFAULT_LANG).lower()
    if lang not in CATALOGS:
        lang = DEFAULT_LANG
    active = CATALOGS.get(lang) or {}
    base = CATALOGS.get(DEFAULT_LANG) or {}

    def t(key, **vars):
        s = active.get(key)
        if s is None:
            s = base.get(key)
        if s is None:
            return key
        if vars and "{" in s:
            try:
                return s.format(**vars)
            except (KeyError, IndexError, ValueError):
                return s
        return s

    return t


def is_cjk(lang):
    return (lang or "").lower() in CJK_LANGS


def font_for(lang, latin_name):
    """Font to use for document text runs: the branding font for Latin scripts, a
    CJK-capable font for languages Martel Sans can't render."""
    return CJK_FONT if is_cjk(lang) else latin_name
