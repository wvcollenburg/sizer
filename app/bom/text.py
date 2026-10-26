"""Small text helpers the BOM modules share.

Pure functions, no Flask, no DB. Each existed as a private copy in two or
three modules (the enrichment pass, the catalog sync, the agent ingest and
the parsers); a key that is built differently on each side never matches.
"""
import re
from typing import Optional

_NON_ALNUM_RE = re.compile(r"[^0-9a-z]")

# Decoders tried in order for a text upload. utf-8-sig accepts UTF-8 with or
# without a BOM; latin-1 accepts any byte, so the last one never fails.
_TEXT_ENCODINGS = ("utf-8-sig", "latin-1")


def squash_ws(s: Optional[str]) -> str:
    """Lower-cased with runs of whitespace (Unicode, NBSP included) collapsed
    to one space and none at the ends: the key under which two descriptions
    of the same part compare equal."""
    return " ".join(str(s or "").lower().split())


def alnum_key(s: Optional[str]) -> str:
    """Lower-cased with everything but ASCII letters and digits removed:
    'HBA355i Front (Dell)' -> 'hba355ifrontdell'. For matching text the
    agent or a PDF may have re-spaced or re-punctuated."""
    return _NON_ALNUM_RE.sub("", (s or "").lower())


def decode_text(raw: bytes, reject_nul: bool = False) -> Optional[str]:
    """``raw`` as text: UTF-8 (with or without BOM) first, then latin-1.
    With ``reject_nul``, a NUL byte in the first 64 KiB means it is not a
    text file at all and None is returned."""
    if reject_nul and b"\x00" in raw[:65536]:
        return None
    for enc in _TEXT_ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return None
