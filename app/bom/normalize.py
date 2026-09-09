"""NormalizedBOM: the one shape every BOM ingestion path must produce.

The validator (bom.rules) never sees a spreadsheet, a PDF or an LLM reply — it
only sees this structure. That is the whole point of the ingestion ladder in
docs/bom-checker-plan.md: deterministic parsers, the strict template and the
AI pre-fill all converge here, so the rules engine has exactly one input
contract to be correct against.

The shape mirrors SC//Design's types.ts field for field, and to_dict() emits
the same camelCase keys (partNumber, serverModel, rawText, configName,
configResults) so their archived fixtures round-trip byte-for-byte modulo key
order. from_dict() is deliberately lenient in the other direction: it accepts
camelCase or snake_case, coerces quantities, and maps anything it does not
recognise onto the 'other'/'Unknown' buckets rather than raising — a BOM with
a surprising category should still get validated, not rejected at the door.

Pure Python, no Flask, no DB: importable from tests and background jobs alike.
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

VENDORS = ('Dell', 'Lenovo', 'Supermicro', 'HPE', 'Unknown')
CATEGORIES = ('cpu', 'memory', 'storage', 'controller', 'nic', 'chassis', 'boss', 'gpu', 'other')
SEVERITIES = ('error', 'warning', 'info')
VERDICTS = ('PASS', 'FAIL', 'INCONCLUSIVE')

# Vendor part numbers sometimes carry a "-P" suffix (Dell "405-AAXX-P" style
# packaging variants) that the HCL lists without. Same regex as the source.
_TRAILING_P_RE = re.compile(r'-P$', re.IGNORECASE)


def normalize_part(part: str) -> str:
    """Strip a trailing '-P' so packaging variants match their HCL entry."""
    return _TRAILING_P_RE.sub('', part)


# ─── lenient dict access ──────────────────────────────────────────────────────

_MISSING = object()


def _pick(d: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    """First present key wins; lets callers hand us camelCase or snake_case."""
    for k in keys:
        if k in d:
            return d[k]
    return default


def _as_int(value: Any, default: int = 1) -> int:
    """Quantities arrive as ints, floats (openpyxl), numeric strings or junk.
    Anything unusable becomes the default rather than aborting the import."""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = '') -> str:
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


def _canon_vendor(value: Any) -> str:
    """Case-insensitive match onto the canonical vendor spelling; anything
    else is 'Unknown' (which the rules treat as 'no vendor-specific advice')."""
    text = _as_str(value).strip().lower()
    for v in VENDORS:
        if v.lower() == text:
            return v
    return 'Unknown'


def _canon_category(value: Any) -> str:
    text = _as_str(value).strip().lower()
    return text if text in CATEGORIES else 'other'


# ─── BOM input ────────────────────────────────────────────────────────────────

@dataclass
class BOMComponent:
    part_number: Optional[str]
    description: str
    quantity: int
    category: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            'partNumber': self.part_number,
            'description': self.description,
            'quantity': self.quantity,
            'category': self.category,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'BOMComponent':
        part = _pick(d, 'partNumber', 'part_number')
        return cls(
            part_number=None if part is None else _as_str(part),
            description=_as_str(_pick(d, 'description')),
            quantity=_as_int(_pick(d, 'quantity', default=1)),
            category=_canon_category(_pick(d, 'category')),
        )


@dataclass
class BOMConfig:
    name: str
    server_model: Optional[str]
    components: List[BOMComponent] = field(default_factory=list)
    # Number of machines the config's quantities are totals for. Set by the
    # deterministic parsers when the source states it (DCSC header row, Dell
    # quote group quantity, "QTY 3" lists); None when it must be inferred.
    # Component quantities stay totals-across-nodes, as in SC//Design, so the
    # archive fixtures and expected verdicts keep their meaning.
    node_count: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            'name': self.name,
            'serverModel': self.server_model,
            'components': [c.to_dict() for c in self.components],
        }
        if self.node_count is not None:
            d['nodeCount'] = self.node_count
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'BOMConfig':
        model = _pick(d, 'serverModel', 'server_model')
        nodes = _pick(d, 'nodeCount', 'node_count')
        try:
            nodes = int(nodes) if nodes not in (None, '') else None
        except (TypeError, ValueError):
            nodes = None
        return cls(
            name=_as_str(_pick(d, 'name')),
            server_model=None if model is None else _as_str(model),
            components=[BOMComponent.from_dict(c) for c in (_pick(d, 'components') or [])],
            node_count=nodes,
        )


@dataclass
class NormalizedBOM:
    vendor: str
    configs: List[BOMConfig] = field(default_factory=list)
    raw_text: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return {
            'vendor': self.vendor,
            'configs': [c.to_dict() for c in self.configs],
            'rawText': self.raw_text,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'NormalizedBOM':
        return cls(
            vendor=_canon_vendor(_pick(d, 'vendor')),
            configs=[BOMConfig.from_dict(c) for c in (_pick(d, 'configs') or [])],
            raw_text=_as_str(_pick(d, 'rawText', 'raw_text')),
        )


# ─── validation output ────────────────────────────────────────────────────────

@dataclass
class Finding:
    severity: str
    component: str
    issue: str
    remediation: str
    # Stable machine key (e.g. 'boss_card') so UI, review queue and swap
    # suggestions can key off a finding without string-matching its issue
    # text — the texts are product copy and will be reworded. Optional so the
    # archived SC//Design fixtures (which predate it) still load.
    code: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'severity': self.severity,
            'component': self.component,
            'issue': self.issue,
            'remediation': self.remediation,
            'code': self.code,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'Finding':
        code = _pick(d, 'code')
        return cls(
            severity=_as_str(_pick(d, 'severity'), 'info'),
            component=_as_str(_pick(d, 'component')),
            issue=_as_str(_pick(d, 'issue')),
            remediation=_as_str(_pick(d, 'remediation')),
            code=None if code is None else _as_str(code),
        )


@dataclass
class ConfigResult:
    config_name: str
    verdict: str
    findings: List[Finding] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'configName': self.config_name,
            'verdict': self.verdict,
            'findings': [f.to_dict() for f in self.findings],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'ConfigResult':
        return cls(
            config_name=_as_str(_pick(d, 'configName', 'config_name')),
            verdict=_as_str(_pick(d, 'verdict'), 'INCONCLUSIVE'),
            findings=[Finding.from_dict(f) for f in (_pick(d, 'findings') or [])],
        )


@dataclass
class ValidationResult:
    verdict: str
    config_results: List[ConfigResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'verdict': self.verdict,
            'configResults': [r.to_dict() for r in self.config_results],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> 'ValidationResult':
        return cls(
            verdict=_as_str(_pick(d, 'verdict'), 'INCONCLUSIVE'),
            config_results=[
                ConfigResult.from_dict(r)
                for r in (_pick(d, 'configResults', 'config_results') or [])
            ],
        )
