"""The shared BOM text helpers (app/bom/text.py) and the fit/enrich NIC
speed parser they sit beside: what the replaced private copies did, pinned
so the keys built on both sides of a match stay equal."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from bom.text import alnum_key, decode_text, squash_ws  # noqa: E402


def test_squash_ws_collapses_unicode_whitespace_and_lowercases():
    assert squash_ws("  Dell PERC   H755\tFront ") == "dell perc h755 front"
    assert squash_ws(None) == ""
    assert squash_ws(42) == "42"


def test_alnum_key_strips_everything_but_ascii_alnum():
    assert alnum_key("HBA355i Front (Dell) - 405-AAXX") == "hba355ifrontdell405aaxx"
    assert alnum_key(None) == ""


def test_decode_text_prefers_utf8_then_latin1():
    assert decode_text("café".encode("utf-8")) == "café"
    assert decode_text(b"\xef\xbb\xbfbom") == "bom"          # UTF-8 BOM dropped
    assert decode_text(b"caf\xe9") == "café"            # latin-1 fallback
    assert decode_text(b"a\x00b") == "a\x00b"                # NUL tolerated by default
    assert decode_text(b"a\x00b", reject_nul=True) is None


def test_nic_speeds_and_max_speed_agree():
    from bom.fit import _parse_nic_speed, nic_speeds
    assert nic_speeds("Broadcom 57414 Dual Port 10/25GbE SFP28") == {10.0, 25.0}
    assert _parse_nic_speed("Broadcom 57414 Dual Port 10/25GbE SFP28") == 25.0
    assert nic_speeds("no network here") == set()
    assert _parse_nic_speed("no network here") is None
