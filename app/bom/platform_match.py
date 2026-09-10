"""Map a BOM config to the HC-Ready platform(s) it is built on.

Why this exists: the scraped catalog is organised per platform (an SC model on
a vendor's server), and the plan's swap suggestions must come from *that
platform's own validated list* — never from the HCL at large. So before a BOM
is validated we try to answer "which server is this?" from the config's
server model (parsers fill it from the CTO/quote header) or, failing that,
from chassis and motherboard lines that name the model.

Matching is deliberately strict: exact match of the normalised server key
(``hcl_models.server_key``) within the vendor's brand. Prefix matching would
turn a PowerEdge R760 into an R760XD2, which is a different chassis with a
different validated list. Supermicro is the one exception — their SERVER line
is a composite ("815TQC-R504WB + X11SPW-CTF") so a BOM's system or board model
may match a part of it.

No platform → no suggestions, and the technical check falls back to the
description-based form-factor heuristic. That is the plan's "plain red flag,
no guessed suggestions" rule.
"""
import re
from typing import Iterable, List, Optional

from bom.normalize import BOMConfig
from hcl_models import HclPlatform, STATUS_ACTIVE, server_key

# Model tokens as they appear in BOM text. Each yields a server_key candidate.
_MODEL_RES = (
    re.compile(r"\b(S[RT]\d{3}\s*V\d)\b", re.I),                 # Lenovo SR630 V3
    re.compile(r"\b(S[RT]\d{3})\b(?!\s*V\d)", re.I),              # Lenovo SR650 (no gen)
    re.compile(r"\bPowerEdge\s+(R\d{3}[A-Za-z]{0,4}\d?)\b", re.I),  # Dell R760, R660xs, R760XD2
    re.compile(r"\b(R\d{3}(?:xs|xd\d?|xa)?)\b(?=.*\b(?:Server|Chassis|PowerEdge)\b)", re.I),
    re.compile(r"\b(DL\d{3}\s*Gen\s*\d+)\b", re.I),               # HPE DL320 Gen11
    re.compile(r"\b(SYS-[A-Z0-9-]+)\b", re.I),                    # Supermicro SYS-511R-M
    re.compile(r"\b(X1[0-9][A-Z]{2,5}(?:-[A-Z0-9]+)*)\b"),         # Supermicro boards X11SPW-CTF
    re.compile(r"\b(\d{3}[A-Z]{2,4}-[A-Z0-9]+)\b"),                # Supermicro chassis 815TQC-R504WB
    re.compile(r"\bThinkCentre\s+(M\d{2}[a-z]?\s+\w+(?:\s+Gen\s*\d+)?)", re.I),
)

# ThinkCentre M-series models ending in 'q' (M70q, M90q): the 'q' IS the Tiny
# form factor by definition, but quotes phrase the model inconsistently —
# 'TC M70q G6', 'Desktop TC M70q G6', 'ThinkCentre M70q Gen 6' — while the
# HCL's SERVER line says 'ThinkCentre M70q Tiny Gen6'. So every M\d{2}q +
# generation sighting emits BOTH the plain and the 'tiny'-inserted key
# ('m70qgen6' and 'm70qtinygen6'), normalising 'G6'/'Gen 6'/'Gen6' to 'gen6'.
# Deliberately restricted to \bM\d{2}q\b so no other family (SR/ST/R/DL/...)
# ever gains an invented variant. (\d+ carries no trailing \b: OEM strings
# glue suffixes on with underscores, 'G6_OEM_Q870_ES_R'.)
_TINY_Q_RE = re.compile(r"\b(M\d{2}q)\b[\s_-]*(?:Gen\s*|G)(\d+)", re.I)

_VENDOR_BRAND = {"dell": "dell", "lenovo": "lenovo", "hpe": "hpe",
                 "supermicro": "supermicro"}


def model_tokens(text: Optional[str]) -> List[str]:
    """Server-model tokens found in a BOM string, normalised with server_key."""
    if not text:
        return []
    out = []

    def add(key):
        if key and key not in out:
            out.append(key)

    for rx in _MODEL_RES:
        for m in rx.finditer(text):
            add(server_key(m.group(1)))
    for m in _TINY_Q_RE.finditer(text):
        model, gen = m.group(1).lower(), m.group(2)
        add("%sgen%s" % (model, gen))
        add("%stinygen%s" % (model, gen))
    return out


def candidate_keys(config: BOMConfig) -> List[str]:
    """Server keys to try for a config, most trustworthy first: the parsed
    server model, then chassis/server lines, then motherboard/other lines that
    name a model (Lenovo's "ThinkSystem SR630 V3 MB")."""
    keys = []  # type: List[str]

    def add(values):
        for v in values:
            if v and v not in keys:
                keys.append(v)

    if config.server_model:
        add([server_key(config.server_model)])
        add(model_tokens(config.server_model))
    for c in config.components:
        if c.category == "chassis":
            add(model_tokens(c.description))
    for c in config.components:
        if c.category == "other" and re.search(r"\b(MB|Motherboard|Planar|System Board)\b",
                                                c.description or "", re.I):
            add(model_tokens(c.description))
    return keys


def identify(config: BOMConfig, vendor: Optional[str],
             platforms: Optional[Iterable[HclPlatform]] = None) -> List[HclPlatform]:
    """Active platforms whose server matches the config. When ``platforms`` is
    None the active rows are loaded from the database."""
    if platforms is None:
        platforms = HclPlatform.query.filter_by(status=STATUS_ACTIVE).all()
    brand = _VENDOR_BRAND.get((vendor or "").lower())
    keys = candidate_keys(config)
    if not keys:
        return []
    pool = [p for p in platforms
            if p.status == STATUS_ACTIVE and (brand is None or p.brand == brand)]
    by_exact = []  # type: List[HclPlatform]
    for key in keys:
        for p in pool:
            pk = server_key(p.server)
            if pk and pk == key and p not in by_exact:
                by_exact.append(p)
        if by_exact:
            return by_exact
    # Supermicro composite SERVER lines: a system or board token from the BOM
    # may be one half of "815TQC-R504WB + X11SPW-CTF".
    loose = []  # type: List[HclPlatform]
    for key in keys:
        if len(key) < 6:
            continue
        for p in pool:
            if p.brand != "supermicro":
                continue
            pk = server_key(p.server)
            if pk and key in pk and p not in loose:
                loose.append(p)
    return loose


def platform_summary(platforms: List[HclPlatform]) -> Optional[dict]:
    """The per-config 'platform' block stored in a check result."""
    if not platforms:
        return None
    first = platforms[0]
    return {
        "brand": first.brand,
        "sc_models": sorted({p.sc_model for p in platforms}),
        "server": first.server,
        "form_factor": first.form_factor,
        "platform_ids": [p.id for p in platforms],
    }
