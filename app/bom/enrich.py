"""Second pass over a technical validation, using what the scraped catalog
knows beyond SC//Design's rule set.

Three things happen here, all additive (the rules port stays a faithful
1:1 port so its fixture parity holds):

* delisted parts — a BOM line that matches a component the HCL has since
  dropped gets a warning with the delist date. The rules engine only sees the
  live lists; this is where "it used to be validated" surfaces.
* swap suggestions — for "not in the HCL" findings on a config whose platform
  we identified, offer that platform's own validated parts of the same kind
  (NICs at the same or higher speed, same form factor preferred). Constrained
  to the identified platform so the suggestion is something the vendor will
  actually configure on that server.
* flag reasons — which findings need a human (the admin review queue). Only
  the uncertain cases: INCONCLUSIVE verdicts, "not found" lookups where the
  catalog may simply be incomplete, and delisted parts. Well-known hard
  failures (BOSS, pre-Scalable CPU, diskless) go straight to the user.
"""
import re
from typing import Dict, List, Optional

from bom.normalize import BOMComponent, BOMConfig, ConfigResult, Finding, normalize_part
from bom.hcl_scrape import component_attrs
from hcl_models import (HclComponent, HclPlatform, HclPlatformComponent, ORIGIN_PREVIEW,
                        STATUS_ACTIVE, STATUS_DELISTED)

# finding code -> component kind in the catalog
SUGGESTABLE = {
    "nic_not_in_hcl": "nic",
    # Too few ports: offer the platform's 4-port adapters of the same speed.
    "nic_ports_vlan": "nic",
    "controller_not_in_hcl": "hba",
    "gpu_not_in_hcl": "gpu",
}

# Codes whose presence sends a check to the admin review queue.
REVIEW_CODES = frozenset([
    "controller_not_in_hcl", "nic_not_in_hcl", "gpu_not_in_hcl", "cpu_unknown",
    "component_delisted", "controller_unsupported", "gpu_unsupported",
])

CODE_DELISTED = "component_delisted"
CODE_PLATFORM_IDENTIFIED = "platform_identified"
CODE_PLATFORM_UNKNOWN = "platform_unknown"
CODE_SINGLE_DISK_MULTI_BAY = "single_disk_multi_bay"
CODE_HYBRID_RATIO = "hybrid_hdd_ratio"
CODE_NIC_SPEED_MISMATCH = "nic_speed_mismatch"
CODE_NIC_PORTS_VLAN = "nic_ports_vlan"
# Ports per node HyperCore wants for LAN and backplane on their own ports.
MIN_NODE_PORTS = 4

# Ported findings the owner has since overruled (apply_owner_rules).
_RESOLVED_CODES = frozenset()  # type: frozenset
# On a NUC the NIC is onboard and a single disk is a supported build.
_NUC_EXEMPT_CODES = frozenset([
    "nic_missing", "nic_not_in_hcl", "nic_lom", "nic_multiple_families",
    "single_flash_drive", "single_hdd", CODE_NIC_SPEED_MISMATCH, CODE_NIC_PORTS_VLAN,
])

_CATEGORY_KINDS = {"nic": ("nic",), "controller": ("hba",), "gpu": ("gpu",),
                   "storage": ("hdd", "ssd"), "cpu": ("cpu",)}


def _norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _matches_component(c: BOMComponent, comp: HclComponent) -> bool:
    """A BOM line names a catalog component when the part numbers agree (with
    Dell's '-P' suffix tolerated) or the descriptions are identical — the
    Lenovo DCSC case, where feature codes carry no HCL key but the
    descriptions are copied verbatim from the same configurator."""
    if c.part_number and comp.part_number:
        a = _norm(normalize_part(c.part_number))
        b = _norm(normalize_part(comp.part_number))
        if a and a == b:
            return True
    return bool(c.description) and _norm(c.description) == _norm(comp.description)


def delisted_findings(config: BOMConfig,
                      delisted: List[HclComponent]) -> List[Finding]:
    out = []  # type: List[Finding]
    for c in config.components:
        kinds = _CATEGORY_KINDS.get(c.category)
        if not kinds:
            continue
        for comp in delisted:
            if comp.kind in kinds and _matches_component(c, comp):
                when = comp.delisted_at.date().isoformat() if comp.delisted_at else "an earlier date"
                out.append(Finding(
                    severity="warning",
                    component=c.description,
                    issue="Component was removed from the Hardware Compatibility List on %s" % when,
                    remediation="This part (%s) was validated once but is no longer listed. "
                                "Confirm with the product team before quoting, or pick a currently "
                                "listed alternative." % comp.part_number,
                    code=CODE_DELISTED,
                ))
                break
    return out


def platform_finding(platforms: List[HclPlatform]) -> Finding:
    if platforms:
        models = ", ".join(sorted({p.sc_model for p in platforms}))
        first = platforms[0]
        return Finding(
            severity="info",
            component=first.server or models,
            issue="Platform identified: %s (%s)" % (models, first.server or first.brand),
            remediation="Component checks and swap suggestions use the validated part list of "
                        "this platform.",
            code=CODE_PLATFORM_IDENTIFIED,
        )
    return Finding(
        severity="info",
        component="Platform",
        issue="Server platform could not be matched to an HC-Ready platform",
        remediation="Checks run against the whole HCL; no platform-specific swap suggestions "
                    "are possible. Confirm the server model is on the HC-Ready list.",
        code=CODE_PLATFORM_UNKNOWN,
    )


def platform_bays(platforms: List[HclPlatform]) -> Optional[int]:
    """Drive bays of the identified platform(s), from the HCL card's "up to N
    HDD / SSD" lines; None when the HCL does not say."""
    bays = max([max(p.hdd_max or 0, p.ssd_max or 0) for p in platforms] or [0])
    return bays or None


def is_nuc(platforms: List[HclPlatform]) -> bool:
    """True when the identified platform is a NUC-class box (the HCL's "DT"
    form factor, e.g. HE155 on a ThinkCentre M70q Tiny)."""
    return any((p.form_factor or "").strip().upper() == "DT"
               or re.search(r"\b(NUC|Tiny)\b", p.server or "", re.I)
               for p in platforms)


def _drives_per_node(config: BOMConfig) -> Optional[Dict[str, int]]:
    """Per-node drive counts by kind ('hdd'/'ssd'/'nvme'), or None when the
    BOM's node count could not be established — per-node figures without a
    node count would be guesses."""
    from bom.fit import derive_nodes
    nodes = derive_nodes(config)
    if nodes.get("node_count") is None:
        return None
    counts = {}  # type: Dict[str, int]
    for d in nodes.get("drives") or []:
        counts[d["kind"]] = counts.get(d["kind"], 0) + int(d.get("qty_per_node") or 0)
    return counts


def single_disk_finding(config: BOMConfig, platforms: List[HclPlatform]) -> Optional[Finding]:
    """Warn when a node that has bays for more disks is quoted with just one.

    Redundancy, not cost (owner decision 2026-09-17): with a single disk a disk
    failure takes the whole node down, which a multi-bay chassis never has to
    accept. Only raised when the platform's bay count is known and the BOM's
    node count could be established. Never on a NUC: a second disk is possible
    there but not required (owner, 2026-09-29).
    """
    bays = platform_bays(platforms)
    if not bays or bays <= 1 or is_nuc(platforms):
        return None
    counts = _drives_per_node(config)
    if counts is None or sum(counts.values()) != 1:
        return None
    return Finding(
        severity="warning",
        component="Storage",
        issue="Single disk per node in a chassis with %d drive bays" % bays,
        remediation="With one disk, a disk failure takes the whole node down. Quote at least "
                    "two disks per node (three or more preferred); the chassis has the bays.",
        code=CODE_SINGLE_DISK_MULTI_BAY,
    )


def hybrid_ratio_finding(config: BOMConfig) -> Optional[Finding]:
    """Warn when a hybrid node has fewer than the required slow (HDD) disks
    per fast (SSD or faster) disk — 2 by default (owner, 2026-09-29), the same
    tunable the engine and the manual calculator enforce."""
    from tunables import T
    counts = _drives_per_node(config)
    if counts is None:
        return None
    slow = counts.get("hdd", 0)
    fast = counts.get("ssd", 0) + counts.get("nvme", 0)
    need = T.hybrid_min_hdd_per_flash
    if not slow or not fast or slow >= need * fast:
        return None
    return Finding(
        severity="warning",
        component="Storage",
        issue="Hybrid layout has %d HDD(s) for %d SSD/NVMe disk(s) per node" % (slow, fast),
        remediation="A hybrid node needs at least %d HDDs per SSD/NVMe disk, so the slow tier "
                    "can absorb data tiered down from flash (HEAT). Add HDDs or quote fewer "
                    "flash disks." % need,
        code=CODE_HYBRID_RATIO,
    )


def apply_owner_rules(config: BOMConfig, findings: List[Finding],
                      platforms: List[HclPlatform]) -> List[Finding]:
    """Owner decisions layered over the ported rules, which stay a faithful
    1:1 port so the 26-BOM replay holds:

    * 2 drives per node is supported in a multi-node cluster but not on a
      Single Node System (2026-10-08) — the port's ``two_drives`` warning is
      dropped only when the BOM is clearly a cluster of 2 or more nodes;
    * on a NUC every NIC is onboard (LOM) and one disk is fine, so the NIC and
      single-drive findings do not apply there;
    * a node with more bays than disks must not run on one disk;
    * a hybrid node needs 2 HDDs per SSD/NVMe disk.
    """
    drop = set(_RESOLVED_CODES)
    from bom.fit import derive_nodes
    if (derive_nodes(config).get("node_count") or 0) >= 2:
        drop.add("two_drives")
    nuc = is_nuc(platforms)
    if nuc:
        drop |= _NUC_EXEMPT_CODES
    out = [f for f in findings if f.code not in drop]
    for extra in (single_disk_finding(config, platforms), hybrid_ratio_finding(config)):
        if extra is not None:
            out.append(extra)
    if not nuc:
        out = nic_topology_findings(config, out)
    return out


# ── NIC speeds and port count (owner rule 2026-09-30) ────────────────────────
# Owner rule: all ACTIVE ports on a node run at the same speed; 1-3 ports is
# backplane over VLAN, 4 or more is dedicated failover for LAN and backplane.
# A slower port next to a
# faster adapter — typically the 1 GbE Broadcom 5720 LOM on the motherboard
# next to a 10/25 GbE card, now that the 5720 is an accepted part — must be
# disabled, and then only the fast adapter's ports count. Fewer than four of
# those per node means the backplane shares the LAN ports over a VLAN, which
# the owner wants flagged with the fix: two more ports (a 4-port adapter
# instead, or a second 2-port adapter).

_ONBOARD_RE = re.compile(r"\bLOM\b|\brNDC\b|on-?board|onboard|integrated|integriert", re.I)


def _is_onboard(desc: str) -> bool:
    from bom.rules import _LOM_KEYWORDS, matches_any
    return bool(_ONBOARD_RE.search(desc or "")) or matches_any(desc or "", _LOM_KEYWORDS)


def _nic_lines(config: BOMConfig):
    """[(component, speed GbE, ports, onboard)] for every network port on the
    BOM: NIC lines, and onboard ports named on another line (a motherboard
    'with Broadcom 5720, 2 ports, 1 Gbit, integrated LOM'). Absence lines
    ('LOM Blank') are not ports."""
    from bom.fit import _parse_nic_ports
    from bom.rules import is_absence_indicator
    out = []
    for c in config.components:
        if is_absence_indicator(c):
            continue
        onboard = _is_onboard(c.description)
        if c.category != "nic" and not onboard:
            continue
        speeds = _nic_speeds(c.description)
        if not speeds:
            continue
        out.append((c, speeds, _parse_nic_ports(c.description), onboard))
    return out


def _nic_speeds(desc: str) -> set:
    """Every speed a port can run at: '10/25GbE' -> {10, 25}, '1GbE' -> {1}."""
    from bom.fit import _NIC_SPEED_RE
    out = set()
    for m in _NIC_SPEED_RE.finditer(desc or ""):
        for part in m.group(1).split("/"):
            try:
                out.add(float(part.strip()))
            except ValueError:
                continue
    return out


def _gbe(value: float) -> str:
    return ("%g" % value)


def nic_topology_findings(config: BOMConfig, findings: List[Finding]) -> List[Finding]:
    from bom.fit import _infer_node_count
    from bom.rules import extract_nic_keywords
    lines = _nic_lines(config)
    if not lines:
        return findings
    # The fastest adapter sets the speeds on offer; ports that share one of
    # them can run together (a 10 GbE X710 next to a 10/25 GbE E810 runs at
    # 10), ports that share none must be disabled.
    top = max(lines, key=lambda l: max(l[1]))[1]
    slow = [(c, max(speeds), onboard) for c, speeds, _p, onboard in lines if not speeds & top]
    kept = [(c, speeds, ports) for c, speeds, ports, _o in lines if speeds & top]
    common = set(top)
    for _c, speeds, _p in kept:
        common &= speeds
    fast = max(common) if common else max(top)
    quick = [(c, ports) for c, _s, ports in kept]
    out = list(findings)

    slow_desc = {c.description for c, _s, _o in slow}
    if slow:
        # The ported "LOM limits you to backplane over VLAN" note is wrong once
        # a faster adapter carries the traffic; the disable finding replaces it.
        out = [f for f in out if not (f.code == "nic_lom" and f.component in slow_desc)]
        for c, speed, onboard in slow:
            if onboard:
                fix = ("Disable these onboard ports in the BIOS before deployment: HyperCore "
                       "should not mix port speeds on a node, and the %s GbE adapter carries "
                       "the traffic." % _gbe(fast))
            else:
                fix = ("Remove this adapter, or disable it in the BIOS before deployment: "
                       "HyperCore should not mix port speeds on a node, and the %s GbE "
                       "adapter carries the traffic." % _gbe(fast))
            out.append(Finding(severity="warning", component=c.description,
                               issue="%s GbE ports next to a %s GbE adapter — must be disabled"
                                     % (_gbe(speed), _gbe(fast)),
                               remediation=fix, code=CODE_NIC_SPEED_MISMATCH))
        # "Several NIC families — select one" counted the slow part too.
        families = {tuple(extract_nic_keywords(c.description) or [c.description]) for c, _p in quick}
        if len(families) <= 1:
            out = [f for f in out if f.code != "nic_multiple_families"]

    if any(ports is None for _c, ports in quick):
        return out                            # cannot count what the BOM does not state
    total = sum(ports * max(int(c.quantity or 1), 1) for c, ports in quick)
    nodes = _infer_node_count(config, [c for c in config.components
                                       if c.category in ("chassis", "cpu", "memory", "storage", "nic")], [])
    if nodes:
        per_node = total / float(nodes)
    elif total < MIN_NODE_PORTS:
        per_node = total                      # below the minimum even on one node
    else:
        return out
    if per_node >= MIN_NODE_PORTS:
        return out
    shown = int(per_node) if float(per_node).is_integer() else round(per_node, 1)
    why = (" once the slower ports are disabled" if slow else "")
    out.append(Finding(
        severity="warning", component=quick[0][0].description,
        issue="Only %s %s GbE port(s) per node — Backplane over VLAN" % (shown, _gbe(fast)),
        remediation=("Each node has %s usable %s GbE network port(s)%s. With 1 to 3 ports the "
                     "backplane shares the LAN ports over a VLAN; 4 or more give LAN and backplane "
                     "their own failover pairs. Add two more %s GbE ports per node: swap to a "
                     "4-port adapter, or add a second 2-port adapter of the same speed."
                     % (shown, _gbe(fast), why, _gbe(fast))),
        code=CODE_NIC_PORTS_VLAN))
    return out


def _platform_parts(platforms: List[HclPlatform], kind: str) -> List[Dict]:
    """Active components of ``kind`` validated for any of the platforms, with
    the per-platform TCE flag folded to 'any'."""
    seen = {}  # type: Dict[int, Dict]
    for p in platforms:
        for link in p.links:
            comp = link.component
            if link.status != STATUS_ACTIVE or comp.status != STATUS_ACTIVE or comp.kind != kind:
                continue
            entry = seen.setdefault(comp.id, {
                "part_number": comp.part_number,
                "description": comp.description,
                "tce": False,
                "attrs": comp.attrs or {},
            })
            entry["tce"] = entry["tce"] or bool(link.tce)
    return list(seen.values())


def suggestions_for(config: BOMConfig, result: ConfigResult,
                    platforms: List[HclPlatform], limit: int = 4) -> List[Dict]:
    """Swap candidates for each suggestable finding of a config."""
    if not platforms:
        return []
    out = []  # type: List[Dict]
    # A part that is off the HCL AND leaves the node short of ports gets one
    # suggestion card, the HCL swap, with 4-port adapters first.
    short_ports = {f.component for f in result.findings if f.code == CODE_NIC_PORTS_VLAN}
    not_listed = {f.component for f in result.findings if f.code == "nic_not_in_hcl"}
    for f in result.findings:
        kind = SUGGESTABLE.get(f.code or "")
        if not kind:
            continue
        if f.code == CODE_NIC_PORTS_VLAN and f.component in not_listed:
            continue
        offending = next((c for c in config.components
                          if c.description == f.component), None)
        off_attrs = component_attrs(kind, offending.description) if offending else {}
        pool = _platform_parts(platforms, kind)
        below_speed = False
        if kind == "nic":
            need = off_attrs.get("speed_gbe")
            ff = off_attrs.get("form_factor")
            ports = off_attrs.get("ports")
            if f.code == CODE_NIC_PORTS_VLAN:
                # The fix is MORE ports at the same speed: 4-port adapters only.
                pool = [p for p in pool if (p["attrs"].get("ports") or 0) >= MIN_NODE_PORTS]
                ports = MIN_NODE_PORTS
            elif f.component in short_ports:
                ports = MIN_NODE_PORTS            # prefer, don't require, 4 ports
            if need:
                fast_enough = [p for p in pool
                               if (p["attrs"].get("speed_gbe") or 0) >= need]
                if fast_enough:
                    pool = fast_enough
                else:
                    # No validated NIC on this platform reaches the offending
                    # part's speed. Keep the pool as candidates, but flagged
                    # and fastest-first — the old silent fallback presented a
                    # 1 GbE adapter as the top swap for a 100 GbE NIC.
                    below_speed = True
            # Nearest-equivalent first: same slot type, lowest speed that still
            # covers the offending part, closest port count (a 2-port asked
            # for gets the 2-port offered before the 4-port). When nothing
            # covers the speed, the FASTEST available part leads instead.
            pool.sort(key=lambda p: (
                0 if ff and p["attrs"].get("form_factor") == ff else 1,
                (-(p["attrs"].get("speed_gbe") or 0) if below_speed
                 else (p["attrs"].get("speed_gbe") or 0)),
                abs((p["attrs"].get("ports") or 0) - ports) if ports else 0,
                -(p["attrs"].get("ports") or 0),
                p["part_number"],
            ))
        else:
            pool.sort(key=lambda p: (p["description"], p["part_number"]))
        if not pool:
            continue
        candidates = [{
            "part_number": p["part_number"],
            "description": p["description"],
            "tce": p["tce"],
            "speed_gbe": p["attrs"].get("speed_gbe"),
            "ports": p["attrs"].get("ports"),
            "form_factor": p["attrs"].get("form_factor"),
        } for p in pool[:limit]]
        if below_speed:
            for cand in candidates:
                cand["below_required_speed"] = True
        out.append({
            "config_name": config.name,
            "component": f.component,
            "code": f.code,
            "kind": kind,
            "candidates": candidates,
        })
    return out


def flag_reasons(verdict: str, config_results: List[ConfigResult]) -> List[str]:
    reasons = []  # type: List[str]
    if verdict == "INCONCLUSIVE":
        reasons.append("inconclusive")
    for r in config_results:
        for f in r.findings:
            if f.code in REVIEW_CODES and f.code not in reasons:
                reasons.append(f.code)
    return reasons


def load_delisted() -> List[HclComponent]:
    """Parts the HCL dropped — and only those.

    A delisted PRE-PUBLICATION part was never on the HCL: it was withdrawn by
    an admin, or superseded by the part the HCL finally published. Telling a
    partner it "was removed from the HCL on <date>" would be a plain untruth,
    so those are left out and the check simply reports the part as unknown
    again, which is the honest state.
    """
    return (HclComponent.query
            .filter(HclComponent.status == STATUS_DELISTED,
                    HclComponent.origin != ORIGIN_PREVIEW).all())
