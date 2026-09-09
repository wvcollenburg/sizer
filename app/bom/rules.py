"""Deterministic BOM validation — a 1:1 port of SC//Design's validator.ts.

Why a verbatim port rather than a rewrite: the finding texts, ordering and
severities in the source were tuned against real partner BOMs and signed off
by product (see _archive/SC-Sizing-main/tests/fixtures/bom/eval-manifest.json).
tests/test_bom_rules.py replays all 26 of those fixtures against the archived
HCL snapshot and demands byte-identical findings, so every helper here keeps
the JS semantics even where Python would naturally differ:

  - JS regexes without the /u flag treat \\b, \\w and \\d as ASCII-only but \\s
    as Unicode whitespace (NBSP is common in xlsx/PDF extractions). We compile
    with re.ASCII and substitute a JS-equivalent whitespace class for \\s.
  - /\\bSPR\\b/, /\\bEMR\\b/, /\\bGNR\\b/ and /\\bE-2\\d{3}\\b/ are case-SENSITIVE in
    the source while their neighbours are not. Kept as-is.
  - Array.find returns the first match in catalog order; `qty || 1` treats a
    zero quantity as one; Math.round is half-up; template literals print 2
    not 2.0. All mirrored explicitly.

Two additive extensions the plan calls for, both off by default so the
fixtures still match: validate_config(..., form_factor=) overrides the DWPD
threshold heuristic with the platform's real rack-unit size, and every
Finding carries a stable `code` so UI/queue/swap logic never string-matches
product copy. The numbered section comments are the source's own.

Pure functions, no Flask, no DB. HclData is whatever the catalog layer hands
us (or the archived snapshot in tests).
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

from bom.normalize import (
    BOMComponent,
    BOMConfig,
    ConfigResult,
    Finding,
    NormalizedBOM,
    ValidationResult,
    normalize_part,
)

# ─── HCL shapes (mirror hcl.ts) ──────────────────────────────────────────────


def _pick(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in d:
            return d[k]
    return default


@dataclass
class HclHba:
    part: str
    type: str = ''
    description: str = ''
    eol: bool = False
    supported: bool = True

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'HclHba':
        return cls(
            part=str(_pick(d, 'part', default='') or ''),
            type=str(_pick(d, 'type', default='') or ''),
            description=str(_pick(d, 'description', default='') or ''),
            eol=bool(_pick(d, 'eol', default=False)),
            supported=bool(_pick(d, 'supported', default=True)),
        )


@dataclass
class HclNic:
    part: str
    type: str = ''
    speed: str = ''
    description: str = ''
    form_factor: str = ''
    # Platform keys ('lenovo/HE155') the entry is scoped to for
    # description-based matching; None = unrestricted (every scraped part,
    # and every archived-snapshot fixture). See _platform_scope_allows.
    platforms: Optional[Tuple[str, ...]] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'HclNic':
        return cls(
            part=str(_pick(d, 'part', default='') or ''),
            type=str(_pick(d, 'type', default='') or ''),
            speed=str(_pick(d, 'speed', default='') or ''),
            description=str(_pick(d, 'description', default='') or ''),
            form_factor=str(_pick(d, 'formFactor', 'form_factor', default='') or ''),
        )


@dataclass
class HclCpu:
    model: str
    description: str = ''
    socket: str = ''
    # See HclNic.platforms.
    platforms: Optional[Tuple[str, ...]] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'HclCpu':
        return cls(
            model=str(_pick(d, 'model', default='') or ''),
            description=str(_pick(d, 'description', default='') or ''),
            socket=str(_pick(d, 'socket', default='') or ''),
        )


@dataclass
class HclGpu:
    part: str
    model: str = ''
    description: str = ''
    vram: int = 0
    eol: bool = False
    supported: bool = True
    # See HclNic.platforms.
    platforms: Optional[Tuple[str, ...]] = None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'HclGpu':
        vram = _pick(d, 'vram', default=0)
        try:
            vram = int(vram or 0)
        except (TypeError, ValueError):
            vram = 0
        return cls(
            part=str(_pick(d, 'part', default='') or ''),
            model=str(_pick(d, 'model', default='') or ''),
            description=str(_pick(d, 'description', default='') or ''),
            vram=vram,
            eol=bool(_pick(d, 'eol', default=False)),
            supported=bool(_pick(d, 'supported', default=True)),
        )


@dataclass
class HclData:
    hbas: List[HclHba] = field(default_factory=list)
    nics: List[HclNic] = field(default_factory=list)
    cpus: List[HclCpu] = field(default_factory=list)
    gpus: List[HclGpu] = field(default_factory=list)
    # Vendors we refuse to validate at all (e.g. HPE while not certified).
    # Compared case-insensitively.
    blocked_vendors: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'HclData':
        """Accepts the hcl-snapshot.json shape (camelCase) or snake_case."""
        blocked = _pick(d, 'blockedVendors', 'blocked_vendors', default=None) or []
        if isinstance(blocked, str):
            # Same parsing as their BLOCKED_VENDORS env var: comma-separated.
            blocked = blocked.split(',')
        return cls(
            hbas=[HclHba.from_dict(x) for x in (_pick(d, 'hbas') or [])],
            nics=[HclNic.from_dict(x) for x in (_pick(d, 'nics') or [])],
            cpus=[HclCpu.from_dict(x) for x in (_pick(d, 'cpus') or [])],
            gpus=[HclGpu.from_dict(x) for x in (_pick(d, 'gpus') or [])],
            blocked_vendors=[str(v).strip().lower() for v in blocked if str(v).strip()],
        )


# ─── JS-faithful regex compilation ───────────────────────────────────────────

# JS \s (no /u flag needed): ASCII whitespace plus the Unicode Zs/line/para
# separators and BOM. Python's re.ASCII shrinks \s to ASCII only, so we spell
# the JS class out and substitute it for every \s in the source patterns.
_JS_WS = '[\\t\\n\\x0b\\x0c\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]'


def _js_re(pattern: str, ignore_case: bool = False):
    """Compile a validator.ts regex with JS word-boundary/digit/whitespace
    semantics. re.ASCII makes \\b, \\w and \\d ASCII-only like JS; \\s is
    widened back to the JS class."""
    flags = re.ASCII | (re.IGNORECASE if ignore_case else 0)
    return re.compile(pattern.replace('\\s', _JS_WS), flags)


def _js_num(value: float) -> str:
    """Render a number the way a JS template literal would: 2 not 2.0."""
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def _first(items, pred):
    """Array.prototype.find — the FIRST match in catalog order, or None."""
    for item in items:
        if pred(item):
            return item
    return None


# ─── component classification helpers ───────────────────────────────────────


def finding(severity: str, component: str, issue: str, remediation: str,
            code: Optional[str] = None) -> Finding:
    return Finding(severity, component, issue, remediation, code)


def matches_any(text: str, keywords: List[str]) -> bool:
    lower = text.lower()
    return any(k.lower() in lower for k in keywords)


def is_absence_indicator(c: BOMComponent) -> bool:
    """"No BOSS", "BOSS Blank", "No Controller", "Riser Blank", etc. are absence
    indicators — the customer explicitly chose not to include that component."""
    d = c.description.lower()
    return d.startswith('no ') or ' blank' in d or 'blank ' in d


def is_boss_card(c: BOMComponent) -> bool:
    if is_absence_indicator(c):
        return False
    return c.category == 'boss' or matches_any(c.description, ['BOSS', 'Boot Optimized Server Storage'])


def is_controller(c: BOMComponent) -> bool:
    if is_absence_indicator(c):
        return False
    return c.category == 'controller'


def is_nvme_drive(c: BOMComponent) -> bool:
    return c.category == 'storage' and matches_any(c.description, ['NVMe', 'U.2', 'M.2', 'PCIe SSD'])


def is_sas_drive(c: BOMComponent) -> bool:
    return c.category == 'storage' and matches_any(c.description, ['SAS', 'NL-SAS'])


def is_hdd(c: BOMComponent) -> bool:
    return c.category == 'storage' and matches_any(
        c.description,
        ['HDD', '7.2K', '7200', 'NL-SAS', 'SAS HDD', 'SATA HDD', 'spinning', 'Hard Drive', 'Hard Disk'],
    )


def is_ssd(c: BOMComponent) -> bool:
    return c.category == 'storage' and not is_hdd(c) and matches_any(
        c.description, ['SSD', 'NVMe', 'Solid State', 'Flash'])


_SED_RE = _js_re(r'\bSED\b', ignore_case=True)


def is_sed_drive(c: BOMComponent) -> bool:
    return _SED_RE.search(c.description) is not None


_DWPD_RE = _js_re(r'(<?)(\d+(?:\.\d+)?)\s*DWPD', ignore_case=True)


def parse_dwpd(description: str) -> Optional[Dict[str, Any]]:
    """Returns the DWPD value and whether it's a "<N" (less-than) notation.
    "<2DWPD" → {value: 2, lessThan: True} — actual endurance is somewhere below 2.
    "1DWPD"  → {value: 1, lessThan: False}"""
    m = _DWPD_RE.search(description)
    if not m:
        return None
    return {'value': float(m.group(2)), 'lessThan': m.group(1) == '<'}


_ONE_U_RE = _js_re(r'\b1u\b')
_SFF_RE = _js_re(r'\bsff\b')


def dwpd_threshold(config: BOMConfig, form_factor: Optional[str] = None) -> float:
    """0.2 DWPD for small form factor, 0.3 otherwise.

    form_factor is our extension: when the platform's rack-unit size is known
    from the HCL ('1U', '2U', 'DT') it beats guessing from the model string.
    """
    if form_factor:
        return 0.2 if form_factor.strip().upper() in ('1U', 'DT') else 0.3
    model = (config.server_model or '').lower()
    # Small form factor chassis typically appear as 1U or contain "sff" in the model string
    is_sff = _ONE_U_RE.search(model) is not None or _SFF_RE.search(model) is not None
    return 0.2 if is_sff else 0.3


def get_drive_count(components: List[BOMComponent]) -> Dict[str, int]:
    nvme = ssd = hdd = 0
    for c in components:
        if c.category != 'storage':
            continue
        qty = c.quantity or 1
        if is_nvme_drive(c):
            nvme += qty
        elif is_hdd(c):
            hdd += qty
        elif is_ssd(c):
            ssd += qty
    return {'nvme': nvme, 'ssd': ssd, 'hdd': hdd, 'total': nvme + ssd + hdd}


# ─── HCL lookup helpers ───────────────────────────────────────────────────────


def _platform_scope_allows(entry_platforms: Optional[Tuple[str, ...]],
                           platform_keys: Optional[Iterable[str]]) -> bool:
    """May a description-based match use this catalog entry for this BOM?

    Description equality is weak identity — 'Integrated Graphics' accepted
    for one Tiny platform must not validate on every platform's BOMs — so
    entries accepted ahead of publication (origin='preview') carry the
    platform keys they were linked to and are only matched in that scope.
    Part-number and keyword-family matches never come here: a real part
    number or known chip family is unambiguous identity, so they stay global.

    * ``entry_platforms is None``: an unrestricted entry (every scraped part)
      — always allowed.
    * non-empty tuple: allowed only when the BOM's identified platform keys
      intersect it.
    * empty tuple (a preview part accepted with no platform links): allowed
      only when the BOM's platform is unidentified too (``platform_keys``
      falsy) — an unlinked generic part validates only equally anonymous BOMs.
    """
    if entry_platforms is None:
        return True
    if entry_platforms:
        return bool(platform_keys) and bool(set(entry_platforms) & set(platform_keys))
    return not platform_keys


VENDOR_HBA_PREFIXES: Dict[str, List[str]] = {
    'Dell': ['405-'],
    'Lenovo': ['4Y37A', '7Y37A'],
    'Supermicro': ['AOC-S'],
}


def suggested_hbas_for_vendor(vendor: str, hcl: HclData) -> str:
    prefixes = VENDOR_HBA_PREFIXES.get(vendor, [])
    if not prefixes:
        return ''
    matches = [
        h for h in hcl.hbas
        if h.supported and any(h.part.upper().startswith(p.upper()) for p in prefixes)
    ]
    if not matches:
        return ''
    return (' Supported options for ' + vendor + ': '
            + ', '.join('%s (%s)' % (h.part, h.description) for h in matches) + '.')


# Match controller model identifiers: HBA355i, HBA345, PERC H355, H750, etc.
_HBA_MODEL_RE = _js_re(r'\b(hba\d{3,4}[ie]?|perc\s+h\d{3,4}[ie]?|h\d{3,4}[ie]?(?=\s|$))\b', ignore_case=True)
# Lenovo 4xx-xxxi style model numbers: 440-16i, 440-8i, 430-16i, 4350-16i, etc.
_HBA_LENOVO_RE = _js_re(r'\b(\d{3,4}-\d+[ie])\b', ignore_case=True)
_WS_RUN_RE = _js_re(r'\s+')


def extract_hba_keywords(desc: str) -> Optional[List[str]]:
    m = _HBA_MODEL_RE.search(desc)
    if m:
        return [_WS_RUN_RE.sub(' ', m.group(1).lower())]
    m = _HBA_LENOVO_RE.search(desc)
    if m:
        return [m.group(1).lower()]
    return None


def _search_terms(c: BOMComponent) -> List[str]:
    # [partNumber, description].filter(Boolean)
    return [t for t in (c.part_number, c.description) if t]


def find_hba_in_hcl(c: BOMComponent, hcl: HclData) -> Optional[HclHba]:
    for term in _search_terms(c):
        lower = term.lower()
        normalized = normalize_part(lower)
        exact = _first(hcl.hbas, lambda h: h.part.lower() in (lower, normalized))
        if exact:
            return exact
        keywords = extract_hba_keywords(lower)
        if keywords:
            kw = _first(hcl.hbas, lambda h: all(k in h.description.lower() for k in keywords))
            if kw:
                return kw
    # Fall back to exact description match — handles cases where the BOM uses a vendor
    # feature code (e.g. Lenovo "BM50") instead of the catalog part number, but the
    # description text matches exactly.
    if c.description:
        desc_lower = c.description.lower()
        by_desc = _first(hcl.hbas, lambda h: h.description.lower() == desc_lower)
        if by_desc:
            return by_desc
    return None


_NIC_MODEL_RE = _js_re(r'\b(xl710|x710|e[68]\d{2}|57\d{3}|i350|bcm\d+|cx[456]\d*)\b', ignore_case=True)


def extract_nic_keywords(desc: str) -> Optional[List[str]]:
    m = _NIC_MODEL_RE.search(desc)
    if not m:
        return None
    return [m.group(1).lower()]


def find_nic_in_hcl(c: BOMComponent, hcl: HclData,
                    platform_keys: Optional[Iterable[str]] = None) -> Optional[HclNic]:
    for term in _search_terms(c):
        lower = term.lower()
        normalized = normalize_part(lower)
        exact = _first(hcl.nics, lambda n: n.part.lower() in (lower, normalized))
        if exact:
            return exact
        keywords = extract_nic_keywords(lower)
        if keywords:
            desc = _first(hcl.nics, lambda n: all(k in n.description.lower() for k in keywords))
            if desc:
                return desc
    # Exact description match, mirroring the upstream HBA fallback: parts
    # accepted from description-only quotes (pre-publication, Lenovo DCSC
    # without part numbers) carry a synthetic part key, so the verbatim
    # description is their only stable identity. Scoped to the entry's
    # platforms (_platform_scope_allows) because that identity is weak.
    if c.description:
        desc_lower = c.description.lower()
        by_desc = _first(hcl.nics, lambda n: n.description.lower() == desc_lower
                         and _platform_scope_allows(n.platforms, platform_keys))
        if by_desc:
            return by_desc
    return None


# ─── GPU HCL lookup ──────────────────────────────────────────────────────────

# Match GPU model identifiers: T4, L4, A1000, RTX A1000, A100, H100, etc.
_GPU_MODEL_RE = _js_re(r'\b(RTX\s+)?([ATLH]\d{1,4})\b', ignore_case=True)


def extract_gpu_keywords(desc: str) -> Optional[List[str]]:
    m = _GPU_MODEL_RE.search(desc)
    if m:
        return [m.group(2).lower()]
    return None


def find_gpu_in_hcl(c: BOMComponent, hcl: HclData,
                    platform_keys: Optional[Iterable[str]] = None) -> Optional[HclGpu]:
    for term in _search_terms(c):
        lower = term.lower()
        normalized = normalize_part(lower)
        exact = _first(hcl.gpus, lambda g: g.part.lower() in (lower, normalized))
        if exact:
            return exact
        keywords = extract_gpu_keywords(lower)
        if keywords:
            kw = _first(hcl.gpus, lambda g: all(
                g.model.lower() == k or k in g.description.lower() for k in keywords))
            if kw:
                return kw
    # Exact description fallback — see find_nic_in_hcl (scoped the same way).
    if c.description:
        desc_lower = c.description.lower()
        by_desc = _first(hcl.gpus, lambda g: g.description.lower() == desc_lower
                         and _platform_scope_allows(g.platforms, platform_keys))
        if by_desc:
            return by_desc
    return None


# ─── CPU generation check ─────────────────────────────────────────────────────

_OLD_XEON_RE = _js_re(r'xeon\s+e[357]-')
_OLD_E_SERIES_RE = _js_re(r'\be[357]-\d{4}\b')


def is_clearly_old_cpu(description: str) -> bool:
    d = description.lower()
    if _OLD_XEON_RE.search(d):
        return True
    if _OLD_E_SERIES_RE.search(d):
        return True
    return False


_SCALABLE_TIER_RE = _js_re(r'xeon\s+(platinum|gold|silver|bronze)\s+\d', ignore_case=True)
# Xeon 6 P/E/H-core — model number may be preceded by a marketing name
# e.g. "Xeon 6 Performance 6325P", "Xeon 6960P", "Xeon 6 6780E"
_XEON6_RE = _js_re(r'xeon\s+6\s*(?:\w+\s+)?\d{3,4}[peh]\b', ignore_case=True)
# These three and the bare E-2xxx check are case-sensitive in the source.
_SPR_RE = _js_re(r'\bSPR\b')
_EMR_RE = _js_re(r'\bEMR\b')
_GNR_RE = _js_re(r'\bGNR\b')
_RAPIDS_RE = _js_re(r'sapphire rapids|emerald rapids|granite rapids', ignore_case=True)
# Intel Xeon E-2xxx series (Coffee Lake-E through Raptor Lake-E, 2018+)
# Descriptions may omit "Xeon": "Intel Raptor Lake-E E-2434" or "Xeon E-2434"
_XEON_E2_RE = _js_re(r'\bxeon\s+e-?2\d{3}\b', ignore_case=True)
_E2_RE = _js_re(r'\bE-2\d{3}\b')
_RAPTOR_RE = _js_re(r'raptor lake', ignore_case=True)


def is_scalable_cpu(description: str) -> bool:
    if _SCALABLE_TIER_RE.search(description):
        return True
    if _XEON6_RE.search(description):
        return True
    if _SPR_RE.search(description):
        return True
    if _EMR_RE.search(description):
        return True
    if _GNR_RE.search(description):
        return True
    if _RAPIDS_RE.search(description):
        return True
    if _XEON_E2_RE.search(description):
        return True
    if _E2_RE.search(description):
        return True
    if _RAPTOR_RE.search(description):
        return True
    return False


_XEON_PREFIX_RE = _js_re(r'^Xeon\s+', ignore_case=True)


def find_cpu_in_hcl(c: BOMComponent, hcl: HclData,
                    platform_keys: Optional[Iterable[str]] = None) -> bool:
    search_text = ('%s %s' % (c.part_number or '', c.description)).lower()
    # An empty model key matches everything, exactly as `includes('')` does.
    if any(_XEON_PREFIX_RE.sub('', cpu.model).lower() in search_text for cpu in hcl.cpus):
        return True
    # Exact description fallback — see find_nic_in_hcl. A pre-publication CPU
    # accepted from a description-only quote is stored with the BOM line
    # verbatim, so the next identical line matches even when no model token
    # can be parsed out of it (e.g. Core Ultra parts). Description equality
    # is scoped (_platform_scope_allows); the model branch above stays global.
    desc_lower = c.description.lower()
    return bool(desc_lower) and any(
        cpu.description and cpu.description.lower() == desc_lower
        and _platform_scope_allows(cpu.platforms, platform_keys) for cpu in hcl.cpus)


# ─── per-config validation ────────────────────────────────────────────────────

HARDWARE_CATEGORIES = ('cpu', 'memory', 'storage', 'controller', 'nic', 'boss', 'gpu')


def is_hardware_config(config: BOMConfig) -> bool:
    return any(c.category in HARDWARE_CATEGORIES for c in config.components)


_HBA_MODE_RE = _js_re(r'\bhba\d', ignore_case=True)
_NVME_WORD_RE = _js_re(r'\bnvme\b', ignore_case=True)
_SAS_WORD_RE = _js_re(r'\bsas\b', ignore_case=True)
_1RX8_RE = _js_re(r'\b1Rx8\b', ignore_case=True)

_LOM_KEYWORDS = ['On Board LOM', 'On-Board LOM', 'Onboard LOM', 'Integrated NIC', 'Integrated LOM', 'integriertes LOM']
_LOM_FORM_FACTORS = ('rndc', 'lom', 'mezz', 'mezzanine')


def validate_config(config: BOMConfig, vendor: str, hcl: HclData,
                    form_factor: Optional[str] = None,
                    platform_keys: Optional[Iterable[str]] = None) -> ConfigResult:
    """``platform_keys`` (our extension, like form_factor): the BOM's
    identified platform keys, consulted only by the description-based
    fallbacks (_platform_scope_allows). None = no platform context, so every
    existing caller behaves exactly as before."""
    findings: List[Finding] = []
    components = config.components

    # 1. BOSS card
    for b in [c for c in components if is_boss_card(c)]:
        findings.append(finding('error', b.description, 'BOSS card is not supported', 'Remove BOSS card and its associated M.2 drives from the BOM', 'boss_card'))

    # 2. Controller checks
    controllers = [c for c in components if is_controller(c)]
    nvme_drives = [c for c in components if is_nvme_drive(c)]
    sas_drives = [c for c in components if is_sas_drive(c)]
    hdd_drives = [c for c in components if is_hdd(c)]
    drives = get_drive_count(components)

    has_nvme_only = len(nvme_drives) > 0 and len(sas_drives) == 0 and len(hdd_drives) == 0
    if has_nvme_only and len(controllers) > 0:
        for ctrl in controllers:
            if not is_boss_card(ctrl):
                findings.append(finding('error', ctrl.description, 'NVMe-only system should not have an HBA or RAID controller', 'Remove the controller — all-NVMe systems do not need an HBA or RAID card. The only valid exception is when the config also includes SAS HDDs.', 'nvme_only_controller'))

    non_boss_controllers = [c for c in controllers if not is_boss_card(c)]

    # Non-NVMe drives (SAS, SATA, or other controller-attached storage) require a storage controller
    non_nvme_storage_drives = [
        c for c in components
        if c.category == 'storage' and not is_nvme_drive(c) and not is_absence_indicator(c)
    ]
    if len(non_nvme_storage_drives) > 0 and len(non_boss_controllers) == 0:
        findings.append(finding('warning', 'Storage Controller', 'No storage controller (HBA/RAID) found for SAS/SATA drives', 'This BOM contains SAS or SATA drives but no storage controller. A supported HBA in passthrough mode is required to connect these drives. Verify the controller is included in the final build.' + suggested_hbas_for_vendor(vendor, hcl), 'controller_missing'))
    if len(non_boss_controllers) > 1:
        findings.append(finding('error', ', '.join(c.description for c in non_boss_controllers), 'Multiple controllers detected', 'Only one storage controller is expected. Review the BOM and remove duplicate controllers.', 'controller_multiple'))

    for ctrl in non_boss_controllers:
        hba_record = find_hba_in_hcl(ctrl, hcl)
        if not hba_record:
            findings.append(finding('error', ctrl.description, 'Controller not found in the Hardware Compatibility List', 'This controller has not been validated by Scale Computing and may not work as expected. It may function correctly in HBA/passthrough mode if it shares a supported chip family, but confirm with the product team before quoting.' + suggested_hbas_for_vendor(vendor, hcl), 'controller_not_in_hcl'))
        elif hba_record.supported:
            already_hba_mode = _HBA_MODE_RE.search(ctrl.description) is not None
            if already_hba_mode:
                findings.append(finding('info', ctrl.description, 'Supported controller — HBA mode active by default', 'This controller ships in HBA/passthrough mode by default and does not require additional configuration before deployment.', 'controller_hba_default'))
            else:
                findings.append(finding('info', ctrl.description, 'Supported controller must be configured in passthrough/HBA mode', 'Before deployment, fully wipe any RAID configuration, reconfigure the card in HBA/passthrough mode, and verify drive ingest settings.', 'controller_passthrough'))
            if hba_record.eol:
                findings.append(finding('warning', ctrl.description, 'Controller is end-of-life (EOL)', 'This controller is still functional but consider replacing it in future builds with a current-generation HBA.', 'controller_eol'))
        else:
            findings.append(finding('error', ctrl.description, 'Controller is in the HCL but not marked as supported', 'Contact the product team for guidance on this controller.', 'controller_unsupported'))

    # 3. CPU checks
    for cpu in [c for c in components if c.category == 'cpu']:
        if is_clearly_old_cpu(cpu.description):
            findings.append(finding('error', cpu.description, 'CPU is too old — pre-Xeon Scalable generation', 'Requires Intel Xeon 1st Gen Scalable (Skylake-SP) or newer. E5/E7/E3-series processors are not supported.', 'cpu_too_old'))
        elif not is_scalable_cpu(cpu.description) and not find_cpu_in_hcl(
                cpu, hcl, platform_keys=platform_keys):
            findings.append(finding('warning', cpu.description, 'CPU generation could not be determined', 'Unable to confirm this is a supported CPU. Verify it is Xeon Gold/Platinum/Silver/Bronze (1st Gen Scalable / Skylake-SP or newer) or another HCL-listed family.', 'cpu_unknown'))

    # 4. Storage checks
    if len([c for c in components if c.category == 'storage']) == 0 and len(controllers) == 0:
        findings.append(finding('error', 'Storage', 'Diskless configuration — no storage components found', 'There is no way to add meaningful storage to this system. This configuration cannot be used.', 'diskless'))

    # Contradictory protocol: "NVMe SAS" is ambiguous — NVMe (PCIe) and SAS are mutually exclusive
    for drive in [c for c in components if c.category == 'storage']:
        d = drive.description
        if _NVME_WORD_RE.search(d) and _SAS_WORD_RE.search(d):
            findings.append(finding('warning', d, 'Contradictory storage protocol — drive description contains both "NVMe" and "SAS"', 'NVMe (PCIe) and SAS are mutually exclusive bus interfaces. Confirm the actual drive interface with the vendor before quoting.', 'storage_protocol_contradiction'))

    tier0_count = drives['nvme'] + drives['ssd']
    tier1_count = drives['hdd']

    if drives['total'] > 0:
        if tier0_count == 1 and tier1_count == 0:
            findings.append(finding('warning', 'Storage Tier', 'Single flash drive (Tier 0) — requires 3+ node cluster deployment', 'A single drive per tier requires deployment in a 3-node or larger cluster configuration. SNS with 1 drive is not supported.', 'single_flash_drive'))
        elif tier1_count == 1 and tier0_count == 0:
            findings.append(finding('warning', 'Storage Tier', 'Single HDD (Tier 1) — requires 3+ node cluster deployment', 'A single drive per tier requires deployment in a 3-node or larger cluster configuration.', 'single_hdd'))
        if drives['total'] == 2:
            findings.append(finding('warning', 'Storage', '2-drive configuration detected', '2-drive SNS is not currently supported. If deploying as part of a 3+ node cluster, this is fully supported. Confirm deployment topology.', 'two_drives'))
        if drives['total'] >= 4 and tier0_count > 0 and tier1_count > 0:
            flash_ratio = tier0_count / drives['total']
            if flash_ratio > 0.5:
                # Math.round is half-up; Python's round() is banker's.
                pct = int(flash_ratio * 100 + 0.5)
                findings.append(finding('info', 'Storage', 'High flash ratio (%d%% flash) — storage is lopsided' % pct, 'More SSDs/NVMe than HDDs may impact SCRIBE tiering efficiency. Verify this matches the intended workload profile.', 'high_flash_ratio'))
        sata_drives = [c for c in components if c.category == 'storage' and matches_any(c.description, ['SATA']) and is_hdd(c)]
        if len(sata_drives) > 0:
            findings.append(finding('info', 'Storage', 'SATA HDDs detected', 'SAS HDDs are strongly recommended over SATA: they are dual-ported, have better error detection, handle atomic actions more reliably, and Scale has better drive health reporting for SAS.', 'sata_hdd'))

    # 5. Drive endurance (DWPD) and SED checks
    threshold = dwpd_threshold(config, form_factor)
    for drive in [c for c in components if is_ssd(c)]:
        dwpd = parse_dwpd(drive.description)
        if dwpd is not None:
            # Flag if: explicit value below threshold, OR ceiling of a "<N" rating is at/below threshold
            below_threshold = dwpd['value'] <= threshold if dwpd['lessThan'] else dwpd['value'] < threshold
            if below_threshold:
                findings.append(finding(
                    'warning',
                    drive.description,
                    'Drive endurance (%s%s DWPD) is below the recommended minimum of %s DWPD' % (
                        '<' if dwpd['lessThan'] else '', _js_num(dwpd['value']), _js_num(threshold)),
                    'Replace with a drive rated at %s DWPD or higher. Scale Computing recommends 0.3 DWPD minimum for server nodes, 0.2 DWPD for small form factor systems.' % _js_num(threshold),
                    'dwpd_low',
                ))
        if is_sed_drive(drive):
            findings.append(finding(
                'info',
                drive.description,
                'Self-encrypting drive (SED) — HyperCore does not support data-at-rest encryption',
                'SED functionality will not be used. Consider a standard (non-SED) drive to avoid paying for unused encryption capability.',
                'sed',
            ))

    # 7. Memory rank checks
    for mem in [c for c in components if c.category == 'memory']:
        if _1RX8_RE.search(mem.description):
            findings.append(finding('warning', mem.description, '1Rx8 memory module — suboptimal rank configuration', '2Rx8 is the ideal sweet spot for DDR5 on Xeon-SP in 16GB–64GB capacities, providing better channel rank utilization. 1Rx8 modules work correctly but reduce performance by ~5–15% depending on workload and limit future upgrade flexibility.', 'memory_1rx8'))

    # 8. NIC checks
    nics = [c for c in components if c.category == 'nic']
    if len(nics) == 0:
        findings.append(finding('warning', 'NIC', 'No NIC found in this BOM', 'Verify a supported NIC (Intel X710, E810, or Broadcom 57504 series) is included in the final build. If the NIC is being sourced separately, confirm it is on the HCL before deployment.', 'nic_missing'))
    # Insertion-ordered like a JS Set: the families list in the finding text
    # follows BOM order.
    discrete_nic_families: Dict[str, None] = {}
    for nic in nics:
        # Check description-based LOM/onboard patterns first — these are never discrete HCL entries
        # and have thousands of vendor-specific part number variants (e.g. BCM5720 LOM on Dell motherboards)
        if matches_any(nic.description, _LOM_KEYWORDS):
            findings.append(finding('info', nic.description, 'Onboard/mezzanine NIC (LOM/rNDC) — limited to Backplane over VLAN', 'This NIC is supported but results in Backplane over VLAN networking. A dedicated 10GbE or 25GbE PCIe/OCP NIC is strongly recommended.', 'nic_lom'))
            continue
        nic_record = find_nic_in_hcl(nic, hcl, platform_keys=platform_keys)
        if nic_record:
            if nic_record.form_factor.lower() in _LOM_FORM_FACTORS:
                findings.append(finding('info', nic.description, 'Onboard/mezzanine NIC (LOM/rNDC) — limited to Backplane over VLAN', 'This NIC is supported but results in Backplane over VLAN networking. A dedicated 10GbE or 25GbE PCIe/OCP NIC is strongly recommended.', 'nic_lom'))
            else:
                # Track discrete (non-LOM) NIC families for the "select one" check
                family = extract_nic_keywords(nic.description) or extract_nic_keywords(nic_record.description)
                if family:
                    discrete_nic_families[family[0]] = None
        else:
            findings.append(finding('error', nic.description, 'NIC not found in the Hardware Compatibility List', 'This NIC is not supported. Replace with a supported NIC from the HCL (Intel X710, E810, or Broadcom 57504 series recommended).', 'nic_not_in_hcl'))
    if len(discrete_nic_families) > 1:
        families = ', '.join(f.upper() for f in discrete_nic_families)
        findings.append(finding('info', 'NIC', 'Multiple discrete NIC types detected — select one', 'HyperCore works with multiple add-on NIC types, but a single NIC type simplifies deployment and reduces driver complexity. This BOM contains %s. Consider standardizing on one.' % families, 'nic_multiple_families'))

    # 9. GPU checks
    gpus = [c for c in components if c.category == 'gpu' and not is_absence_indicator(c)]
    for gpu in gpus:
        gpu_record = find_gpu_in_hcl(gpu, hcl, platform_keys=platform_keys)
        if gpu_record:
            if not gpu_record.supported:
                findings.append(finding('error', gpu.description, 'GPU is in the HCL but not marked as supported', 'Contact the product team for guidance on this GPU model.', 'gpu_unsupported'))
            elif gpu_record.eol:
                findings.append(finding('warning', gpu.description, 'GPU is end-of-life (EOL)', 'This GPU is still functional but consider replacing it in future builds with a current-generation model.', 'gpu_eol'))
        else:
            findings.append(finding('warning', gpu.description, 'GPU not found in the Hardware Compatibility List', 'This GPU has not been validated by Scale Computing. Confirm compatibility with the product team before quoting.', 'gpu_not_in_hcl'))

    # 10. Blocked vendors
    if vendor.lower() in [v.lower() for v in hcl.blocked_vendors]:
        findings.append(finding('error', 'Vendor', '%s hardware is not approved at this time' % vendor, 'We are not currently certifying %s hardware. Please use a supported vendor.' % vendor, 'vendor_blocked'))

    return ConfigResult(config_name=config.name, verdict=determine_verdict(findings), findings=findings)


INCONCLUSIVE_ISSUES = (
    'CPU generation could not be determined',
    'CPU not found in the Hardware Compatibility List',
    'Contradictory storage protocol',
)


def determine_verdict(findings: List[Finding]) -> str:
    if any(f.severity == 'error' for f in findings):
        return 'FAIL'
    if any(any(i in f.issue for i in INCONCLUSIVE_ISSUES) for f in findings):
        return 'INCONCLUSIVE'
    return 'PASS'


# ─── main entry point ─────────────────────────────────────────────────────────


def validate_bom(bom: NormalizedBOM, hcl: HclData,
                 form_factor: Optional[str] = None) -> ValidationResult:
    """Validate every hardware-bearing config; a BOM with none (services-only
    quote, or a parser that found nothing) is INCONCLUSIVE, never PASS."""
    config_results = [
        validate_config(config, bom.vendor, hcl, form_factor=form_factor)
        for config in bom.configs if is_hardware_config(config)
    ]

    verdict = 'PASS'
    if len(config_results) == 0:
        verdict = 'INCONCLUSIVE'
    elif any(r.verdict == 'FAIL' for r in config_results):
        verdict = 'FAIL'
    elif any(r.verdict == 'INCONCLUSIVE' for r in config_results):
        verdict = 'INCONCLUSIVE'

    return ValidationResult(verdict=verdict, config_results=config_results)
