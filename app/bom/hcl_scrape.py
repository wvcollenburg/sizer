"""hcl.scalecomputing.com page parsers + fetcher (docs/bom-checker-plan.md, phase 1).

The HCL site is Express/EJS, fully server-rendered and has no JSON API, so the
only way to get part-number-level validated components is to read its HTML.
This module is the *pure* half of that: turn the three page shapes into plain
dicts, and pull a whole site snapshot through an injectable fetcher. It never
touches the ORM - the sync layer (hcl_sync) diffs a snapshot against the
catalog and writes to the pending-change queue; nothing here lands in the live
catalog directly.

Design choices worth knowing about:

- Regex over the raw markup rather than a DOM tree. The EJS templates are
  small and uniform (``<ul><b>LABEL:</b> value</ul>`` lines, ``<select
  name=kind>`` option lists, a 7-column table), and a handful of anchored
  patterns is easier to keep defensive than a tree walk. Every parser degrades
  to None/[] for a missing section; only a page that is not a detail page at
  all (no ``detailPlatformTitle``) raises, because that means we fetched the
  wrong thing and must not record it as "platform with no components".

- The option VALUE is the vendor part number and is authoritative; the same
  part number also appears as a ``[PART]`` suffix in the option text, which we
  strip so descriptions stay clean for matching against BOM lines.

- Component attributes (``component_attrs``) are derived from the description
  text on a best-effort basis, because the site exposes nothing structured
  beyond the CPU ``[64C/128T @ 3GHz]`` prefix. Anything not stated in the text
  is None rather than guessed (e.g. a NIC listed without a speed keeps
  speed_gbe None even when the family implies 25G) - the rules layer can decide
  how to treat unknowns; the scraper should not invent facts.

- NIC family tokens are normalized to the bare silicon name in lower case with
  any ``BCM`` prefix removed, so HPE's ``BCM57416`` and Lenovo's ``57416`` are
  the same family ``57416``.

- ``fetch_page`` retries: the site drops connections mid-body on a noticeable
  share of detail pages (``http.client.IncompleteRead`` at ~6.6KB), and a retry
  fixes it every time. Retries cover transport errors and 5xx only; a 4xx is a
  wrong URL and retrying it would just hammer the site.

- ``scrape_all`` never raises. A failed page is recorded in ``errors`` and the
  snapshot is marked incomplete; the delist diff downstream must only run on a
  complete snapshot, otherwise a transient network failure would look like a
  mass delisting.
"""
import html
import http.client
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

DEFAULT_BASE_URL = "https://hcl.scalecomputing.com"
LISTING_PATH = "/hcready/filter"
DEVINFO_PATH = "/hcl/devinfo"
USER_AGENT = "sc-sizer-bom-checker/1.0"

COMPONENT_KINDS = ("cpu", "nic", "hba", "hdd", "ssd")


class ScrapeError(Exception):
    """Any failure talking to or reading the HCL site."""


class ScrapeParseError(ScrapeError):
    """The page fetched is not the page shape we expected."""


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
# The site uses U+2011 (non-breaking hyphen) in some HPE descriptions
# ("2‑port BASE‑T"); fold every dash-like codepoint to ASCII so the attribute
# regexes below only have to know about "-".
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—−"), "-")


def _clean_text(fragment: str) -> str:
    """Strip tags, unescape entities, fold dashes and collapse whitespace."""
    text = html.unescape(_TAG_RE.sub(" ", fragment or ""))
    text = text.translate(_DASHES)
    return _WS_RE.sub(" ", text).strip()


def _to_int(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    m = re.search(r"\d+", text)
    return int(m.group(0)) if m else None


def _to_float(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    m = re.search(r"\d+(?:\.\d+)?", text)
    return float(m.group(0)) if m else None


def _unspecified(value: Optional[str]) -> Optional[str]:
    """'Unspecified' is the site's null; collapse it (and blanks) to None."""
    if value is None:
        return None
    value = value.strip()
    if not value or value.lower() == "unspecified":
        return None
    return value


# --------------------------------------------------------------------------
# listing page  (/hcready/filter)
# --------------------------------------------------------------------------

_MODELBOX_RE = re.compile(
    r'<div\s+class="modelBox"([^>]*)>(.*?)</div>', re.S | re.I
)
_DATA_ATTR_RE = re.compile(r'data-([\w-]+)="([^"]*)"')
_CARD_LINE_RE = re.compile(
    r"<p>\s*<strong>([^<]*?):?\s*</strong>(.*?)</p>", re.S | re.I
)


def parse_listing(page_html: str) -> List[dict]:
    """All platform cards on the HC-Ready listing, one dict per card.

    Card body lines are matched by label, not position, because the template
    is free to reorder them and a positional parse would silently misfile.
    """
    cards = []
    for attrs_html, body in _MODELBOX_RE.findall(page_html or ""):
        attrs = {k.lower(): html.unescape(v) for k, v in _DATA_ATTR_RE.findall(attrs_html)}
        lines = {}
        for label, value in _CARD_LINE_RE.findall(body):
            lines[_clean_text(label).lower().rstrip(":")] = _clean_text(value)
        sc_model = attrs.get("model") or None
        brand = attrs.get("brand") or None
        if not sc_model or not brand:
            # A card without its data attributes can't be fetched in detail
            # (brand is mandatory on the detail URL) - skip rather than guess.
            continue
        cards.append({
            "sc_model": sc_model,
            "brand": brand.lower(),
            "form_factor": lines.get("form factor") or attrs.get("ru") or None,
            "socket": lines.get("socket") or None,
            "cpu_count": _to_int(lines.get("cpu count")),
            "memory_type": lines.get("memory") or None,
            "max_ram_gb": _to_int(lines.get("max ram")),
        })
    return cards


# --------------------------------------------------------------------------
# detail fragment  (/hcready/detail?model=X&brand=Y)
# --------------------------------------------------------------------------

_TITLE_RE = re.compile(
    r'<h2[^>]*id="detailPlatformTitle"[^>]*>(.*?)</h2>', re.S | re.I
)
_CHASSIS_RE = re.compile(r"<h3>\s*CHASSIS\s*\[\s*([^\]]*?)\s*\]\s*</h3>", re.I)
_UL_LINE_RE = re.compile(r"<ul>\s*<b>(.*?)</b>(.*?)</ul>", re.S | re.I)
_SELECT_RE = re.compile(
    r'<select\s+name="(\w+)"[^>]*>(.*?)</select>', re.S | re.I
)
_OPTION_RE = re.compile(
    r'<option\s+value="([^"]*)"[^>]*>(.*?)</option>', re.S | re.I
)
_TRAILING_PART_RE = re.compile(r"\s*\[[^\]]*\]\s*$")
_LEADING_SPEC_RE = re.compile(r"^\s*\[([^\]]*)\]\s*")
# "2x FCLGA4189 (up to 80 cores)"  and  "(up to 2x FCLGA4677)"
_CPU_LINE_RE = re.compile(r"(\d+)\s*x\s*([A-Za-z]+\d+[A-Za-z0-9-]*)", re.I)
_MAX_CORES_RE = re.compile(r"up to\s+(\d+)\s*cores", re.I)
# "(up to) 32x  ECC DDR4 RDIMM"
_RAM_LINE_RE = re.compile(r"(\d+)\s*x\s*(.*)$", re.I)
_UP_TO_N_RE = re.compile(r"up to\s+(\d+)", re.I)


def _detail_lines(fragment: str) -> List[Tuple[str, str]]:
    """Every ``<ul><b>LABEL</b> value</ul>`` as (label, value), both cleaned.

    Labels keep their trailing colon stripped but otherwise verbatim, because
    the storage lines carry their count inside the label ("HDD: (up to 3)").
    """
    out = []
    for label, value in _UL_LINE_RE.findall(fragment or ""):
        out.append((_clean_text(label), _clean_text(value)))
    return out


def _find_line(lines: List[Tuple[str, str]], prefix: str) -> Optional[Tuple[str, str]]:
    prefix = prefix.lower()
    for label, value in lines:
        if label.lower().startswith(prefix):
            return label, value
    return None


def _parse_option(kind: str, value: str, inner_html: str) -> dict:
    """One <option> into a component dict; VALUE is the part number."""
    tce = "tce-badge" in inner_html
    text = _clean_text(inner_html)
    # The badge text survives tag stripping; drop it before the bracket work.
    if tce:
        text = re.sub(r"\s*\bTCE\b\s*$", "", text)
    text = _TRAILING_PART_RE.sub("", text)
    spec_prefix = None
    if kind == "cpu":
        m = _LEADING_SPEC_RE.match(text)
        if m:
            spec_prefix = m.group(1).strip()
            text = text[m.end():]
    description = text.strip()
    part_number = html.unescape(value or "").strip()
    attrs = component_attrs(kind, description, spec_prefix)
    # Supermicro HBAs are sometimes described generically ("12Gb/s Eight-Port
    # SAS PCIe 4.0 Internal") while the part number *is* the model name.
    if kind == "hba" and not attrs.get("model") and part_number.upper().startswith("AOC-"):
        attrs["model"] = part_number
    return {
        "kind": kind,
        "part_number": part_number,
        "description": description,
        "tce": tce,
        "attrs": attrs,
    }


def parse_detail(fragment: str) -> dict:
    """One HC-Ready detail fragment into a platform dict with components."""
    m = _TITLE_RE.search(fragment or "")
    if not m:
        raise ScrapeParseError("not a platform detail page (no detailPlatformTitle)")
    title = _clean_text(m.group(1))
    tm = re.search(r"\[\s*([^\]]*?)\s*\]", title)
    sc_model = tm.group(1) if tm else title
    if not sc_model:
        raise ScrapeParseError("detail page title carries no model")

    bm = _CHASSIS_RE.search(fragment)
    brand = bm.group(1).strip().lower() if bm and bm.group(1).strip() else None

    lines = _detail_lines(fragment)

    def line_value(prefix: str) -> Optional[str]:
        hit = _find_line(lines, prefix)
        return _unspecified(hit[1]) if hit else None

    # -- processor -------------------------------------------------------
    sockets = socket_name = max_cores = None
    cpu_line = _find_line(lines, "CPU")
    if cpu_line:
        cm = _CPU_LINE_RE.search(cpu_line[1])
        if cm:
            sockets = int(cm.group(1))
            socket_name = cm.group(2)
        mm = _MAX_CORES_RE.search(cpu_line[1])
        if mm:
            max_cores = int(mm.group(1))

    # -- memory ----------------------------------------------------------
    ram_slots = ram_type = None
    ram_ecc = False
    ram_line = _find_line(lines, "RAM")
    if ram_line:
        rm = _RAM_LINE_RE.search(ram_line[1])
        if rm:
            ram_slots = int(rm.group(1))
            rest = rm.group(2).strip()
            ram_ecc = bool(re.search(r"\bECC\b", rest))
            rest = re.sub(r"\bECC\b", "", rest).strip()
            ram_type = _WS_RE.sub(" ", rest) or None

    # -- network / storage headings --------------------------------------
    nic_listed = _find_line(lines, "NIC") is not None
    hdd_line = _find_line(lines, "HDD")
    ssd_line = _find_line(lines, "SSD")
    hdd_max = _first_up_to(hdd_line)
    ssd_max = _first_up_to(ssd_line)

    # -- component selects -----------------------------------------------
    components = []
    for name, body in _SELECT_RE.findall(fragment):
        kind = name.lower()
        if kind not in COMPONENT_KINDS:
            continue
        for value, inner in _OPTION_RE.findall(body):
            components.append(_parse_option(kind, value, inner))
    if any(c["kind"] == "nic" for c in components):
        nic_listed = True

    return {
        "sc_model": sc_model,
        "brand": brand,
        "server": line_value("SERVER"),
        "power_supply": line_value("POWER SUPPLY"),
        "cooling": line_value("COOLING"),
        "tpm": line_value("TPM"),
        "risers": line_value("RISER"),
        "oob_license": line_value("OOB LICENSE"),
        "sockets": sockets,
        "socket": socket_name,
        "max_cores": max_cores,
        "ram_slots": ram_slots,
        "ram_type": ram_type,
        "ram_ecc": ram_ecc,
        "hdd_max": hdd_max,
        "ssd_max": ssd_max,
        "nic_listed": nic_listed,
        "components": components,
    }


def _first_up_to(line: Optional[Tuple[str, str]]) -> Optional[int]:
    if not line:
        return None
    m = _UP_TO_N_RE.search(line[0]) or _UP_TO_N_RE.search(line[1])
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------
# component attributes
# --------------------------------------------------------------------------

_CPU_SPEC_RE = re.compile(
    r"(?P<cores>\d+)\s*C\s*/\s*(?P<threads>\d+)\s*T(?:\s*@\s*(?P<ghz>\d+(?:\.\d+)?)?\s*GHz)?",
    re.I,
)
_CPU_TIERS = ("Platinum", "Gold", "Silver", "Bronze")

_NIC_SPEED_RE = re.compile(r"(\d+(?:/\d+)*)\s*G(?:b|B)", re.I)
_NIC_FAMILY_RE = re.compile(
    r"\b(?:BCM)?(E810|E823|E825|E610|XL710|X710|X722|X550|X557|X540|I350|I210|I226|"
    r"57504|57454|57416|57414|57412|5719|5720|CX\d|MCX\d+)\b",
    re.I,
)
_PORTS_WORDS = {"single": 1, "dual": 2, "quad": 4, "eight": 8, "octal": 8}
_PORTS_NUM_RE = re.compile(r"\b(\d+)\s*-?\s*ports?\b", re.I)
_PORTS_WORD_RE = re.compile(r"\b(single|dual|quad|eight|octal)\s*-?\s*port\b", re.I)
_PORTS_NX_RE = re.compile(r"\b(\d+)\s*x\s*\d+\s*G", re.I)

_HBA_MODEL_RE = re.compile(
    r"\b(AOC-[A-Z0-9-]+|\d{3,4}-\d{1,2}i|HBA\s?\d{3}i?|H\d{3}|MR\d{3}i-[a-z]|SR\d{3}i-[a-z]|"
    r"\d{4}-\d{1,2}e)\b",
    re.I,
)
_HBA_PORTS_RE = re.compile(r"(?:\b|-|L)(\d{1,2})[ie]T?\b", re.I)

_CAPACITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(TB|GB)\b")
_RPM_RE = re.compile(r"\b(\d+(?:\.\d+)?)K(?:\s*RPM)?\b")
_DWPD_RE = re.compile(r"(\d+(?:\.\d+)?)\s*DWPD\b", re.I)
_ENDURANCE = (
    (re.compile(r"\bRead Intensive\b|\bVRO\b|\bRI\b", re.I), "Read Intensive"),
    (re.compile(r"\bMixed Use\b|\bMU\b", re.I), "Mixed Use"),
    (re.compile(r"\bWrite Intensive\b|\bWI\b", re.I), "Write Intensive"),
)


def component_attrs(kind: str, description: str, spec_prefix: Optional[str] = None) -> dict:
    """Best-effort structured attributes from a component description.

    Everything that is not literally stated in the text stays None; the site
    is the source of truth and the rules layer decides what to do with gaps.
    """
    text = _clean_text(description or "")
    if kind == "cpu":
        return _cpu_attrs(text, spec_prefix)
    if kind == "nic":
        return _nic_attrs(text)
    if kind == "hba":
        return _hba_attrs(text)
    if kind in ("hdd", "ssd"):
        return _drive_attrs(text)
    return {}


def _cpu_attrs(text: str, spec_prefix: Optional[str]) -> dict:
    cores = threads = ghz = None
    if spec_prefix:
        m = _CPU_SPEC_RE.search(spec_prefix)
        if m:
            cores = int(m.group("cores"))
            threads = int(m.group("threads"))
            # "[12C/24T @ GHz]" happens: the site left the clock blank.
            ghz = float(m.group("ghz")) if m.group("ghz") else None
    low = text.lower()
    if "amd" in low or "epyc" in low:
        vendor = "AMD"
    elif "intel" in low or "xeon" in low:
        vendor = "Intel"
    else:
        vendor = None
    model = re.sub(r"[®™]", "", text)  # ® ™
    model = re.sub(r"\bIntel\b|\bAMD\b|\bProcessor\b|\bCPU\b", "", model, flags=re.I)
    model = _WS_RE.sub(" ", model).strip() or None
    tier = None
    for candidate in _CPU_TIERS:
        if re.search(r"\b%s\b" % candidate, text, re.I):
            tier = candidate
            break
    return {
        "cores": cores,
        "threads": threads,
        "ghz": ghz,
        "vendor": vendor,
        "model": model,
        "tier": tier,
    }


def _ports_from(text: str) -> Optional[int]:
    m = _PORTS_NUM_RE.search(text)
    if m:
        return int(m.group(1))
    m = _PORTS_WORD_RE.search(text)
    if m:
        return _PORTS_WORDS[m.group(1).lower()]
    m = _PORTS_NX_RE.search(text)
    if m:
        return int(m.group(1))
    return None


def _nic_attrs(text: str) -> dict:
    speeds = []
    for group in _NIC_SPEED_RE.findall(text):
        speeds.extend(int(s) for s in group.split("/") if s)
    speed = max(speeds) if speeds else None

    upper = text.upper()
    if "OCP" in upper:
        form_factor = "OCP"
    elif re.search(r"\bLOM\b", upper):
        form_factor = "LOM"
    elif "PCIE" in upper:
        form_factor = "PCIe"
    else:
        form_factor = None

    fm = _NIC_FAMILY_RE.search(text)
    family = fm.group(1).lower() if fm else None

    if "SFP28" in upper:
        media = "SFP28"
    elif "SFP+" in upper:
        media = "SFP+"
    elif "RJ45" in upper or "RJ-45" in upper:
        media = "RJ45"
    elif "BASE-T" in upper or "BASET" in upper:
        media = "BASE-T"
    else:
        media = None

    return {
        "speed_gbe": speed,
        "ports": _ports_from(text),
        "form_factor": form_factor,
        "family": family,
        "media": media,
    }


def _hba_attrs(text: str) -> dict:
    mm = _HBA_MODEL_RE.search(text)
    model = mm.group(1) if mm else None
    ports = None
    if model:
        pm = _HBA_PORTS_RE.search(model)
        if pm:
            ports = int(pm.group(1))
    if ports is None:
        ports = _ports_from(text)
    return {"model": model, "ports": ports}


def _drive_attrs(text: str) -> dict:
    capacity = None
    cm = _CAPACITY_RE.search(text)
    if cm:
        capacity = float(cm.group(1))
        if cm.group(2) == "GB":
            capacity = round(capacity / 1000.0, 3)

    upper = text.upper()
    if "NVME" in upper:
        interface = "NVMe"
    elif re.search(r"\bN?L?SAS\b", upper):
        interface = "SAS"
    elif "SATA" in upper:
        interface = "SATA"
    else:
        interface = None

    # First stated size wins: Dell lists "2.5in ... 3.5in HYB CARR" for a
    # small drive in a hybrid carrier, and the drive is what matters.
    form_factor = None
    hits = []
    for pattern, ff in (
        (r'2\.5\s*(?:"|in\b|inch\b)', "2.5"),
        (r'3\.5\s*(?:"|in\b|inch\b)', "3.5"),
        (r"\bSFF\b", "2.5"),
        (r"\bLFF\b", "3.5"),
        (r"\bM\.2\b", "M.2"),
    ):
        m = re.search(pattern, text, re.I)
        if m:
            hits.append((m.start(), ff))
    if hits:
        form_factor = min(hits)[1]
    elif re.search(r"\bU\.?2\b", text, re.I):
        # U.2 (SFF-8639) is a 2.5" connector by definition; only used when
        # the text states no size of its own (Lenovo says 3.5" U.2 for the
        # tray, and the stated size wins there).
        form_factor = "2.5"

    rpm = None
    rm = _RPM_RE.search(text)
    if rm:
        rpm = int(round(float(rm.group(1)) * 1000))

    endurance = None
    for pattern, label in _ENDURANCE:
        if pattern.search(text):
            endurance = label
            break

    dm = _DWPD_RE.search(text)
    dwpd = float(dm.group(1)) if dm else None

    return {
        "capacity_tb": capacity,
        "interface": interface,
        "form_factor": form_factor,
        "rpm": rpm,
        "endurance": endurance,
        "dwpd": dwpd,
    }


# --------------------------------------------------------------------------
# device table  (/hcl/devinfo)
# --------------------------------------------------------------------------

_TABLE_RE = re.compile(r'<table[^>]*id="tableOutput"[^>]*>(.*?)</table>', re.S | re.I)
_TBODY_RE = re.compile(r"<tbody[^>]*>(.*?)</tbody>", re.S | re.I)
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S | re.I)


def parse_devinfo(page_html: str) -> List[dict]:
    """PCI device rows: (dev_id, ven_id) is the robust match key for a BOM."""
    tm = _TABLE_RE.search(page_html or "")
    if not tm:
        return []
    body = tm.group(1)
    bm = _TBODY_RE.search(body)
    if bm:
        body = bm.group(1)
    devices = []
    for row in _TR_RE.findall(body):
        cells = [_clean_text(c) for c in _TD_RE.findall(row)]
        if len(cells) < 7:
            continue  # header or malformed row
        dev_id, description, driver, dev_type, ven_id, supported, since = cells[:7]
        devices.append({
            "dev_id": dev_id.lower(),
            "ven_id": ven_id.lower(),
            "description": description,
            "driver": driver or None,
            "type": dev_type.lower() or None,
            "supported": supported.strip().lower() == "true",
            "since": since or None,
        })
    return devices


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

_RETRY_BACKOFF = (0.5, 1.0, 2.0)


def fetch_page(
    path: str,
    base_url: str = DEFAULT_BASE_URL,
    timeout: int = 30,
    attempts: int = 4,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """GET one site path as text, retrying transport failures and 5xx.

    ``Accept-Encoding: identity`` + ``Connection: close`` keep the response a
    plain, single-use body: the IncompleteRead failures we observed are worse
    with keep-alive, and there is nothing to gain from compression at this
    page size.
    """
    url = base_url.rstrip("/") + path
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    last_error = None  # type: Optional[BaseException]
    for attempt in range(max(1, attempts)):
        if attempt:
            backoff = _RETRY_BACKOFF[min(attempt - 1, len(_RETRY_BACKOFF) - 1)]
            sleep(backoff)
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            try:
                data = resp.read()
            finally:
                close = getattr(resp, "close", None)
                if close:
                    close()
            return data.decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code < 500:
                # Wrong URL or blocked: retrying can't change the answer.
                raise ScrapeError("GET %s failed: HTTP %s" % (path, exc.code)) from exc
        except (http.client.IncompleteRead, http.client.HTTPException,
                urllib.error.URLError, socket.timeout, OSError) as exc:
            last_error = exc
    raise ScrapeError(
        "GET %s failed after %d attempts: %r" % (path, max(1, attempts), last_error)
    )


def detail_path(sc_model: str, brand: str) -> str:
    """Detail URL for a card. ``brand`` is mandatory - the server 500s without it."""
    return "/hcready/detail?" + urllib.parse.urlencode({"model": sc_model, "brand": brand})


def scrape_all(
    fetch: Callable[[str], str] = fetch_page,
    progress: Optional[Callable[[int, int, str], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    delay: float = 0.25,
) -> dict:
    """Whole-site snapshot: listing, every detail page, the device table.

    Never raises. ``complete`` is True only when every page fetched and
    parsed; the sync layer must not run its delist diff otherwise. ``delay``
    is a politeness pause between requests - the site is a small Express app.
    """
    started = datetime.now(timezone.utc)
    errors = []  # type: List[dict]
    platforms = []  # type: List[dict]
    devices = []  # type: List[dict]
    cards = []  # type: List[dict]
    listing_ok = False

    # listing first: it decides how many pages there are in total
    pages_done = 0
    try:
        cards = parse_listing(fetch(LISTING_PATH))
        listing_ok = True
    except Exception as exc:  # noqa: BLE001 - every failure must be recorded, not raised
        errors.append({"path": LISTING_PATH, "error": _describe(exc)})
    pages_total = 1 + len(cards) + 1
    pages_done += 1
    if progress:
        progress(pages_done, pages_total, LISTING_PATH)

    for card in cards:
        path = detail_path(card["sc_model"], card["brand"])
        if delay:
            sleep(delay)
        try:
            detail = parse_detail(fetch(path))
        except Exception as exc:  # noqa: BLE001
            errors.append({"path": path, "error": _describe(exc)})
        else:
            merged = dict(card)
            merged.update({k: v for k, v in detail.items() if k not in ("sc_model", "brand")})
            # keep the listing identity; the detail page can only agree with it
            merged["sc_model"] = card["sc_model"]
            merged["brand"] = card["brand"]
            platforms.append(merged)
        pages_done += 1
        if progress:
            progress(pages_done, pages_total, path)

    if delay:
        sleep(delay)
    try:
        devices = parse_devinfo(fetch(DEVINFO_PATH))
    except Exception as exc:  # noqa: BLE001
        errors.append({"path": DEVINFO_PATH, "error": _describe(exc)})
    pages_done += 1
    if progress:
        progress(pages_done, pages_total, DEVINFO_PATH)

    return {
        "scraped_at": started.isoformat(),
        "complete": listing_ok and not errors,
        "errors": errors,
        "platforms": platforms,
        "devices": devices,
        "pages_total": pages_total,
        "pages_done": pages_done,
    }


def _describe(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    return "%s: %s" % (exc.__class__.__name__, text) if text != exc.__class__.__name__ else text


# --------------------------------------------------------------------------
# snapshot helpers
# --------------------------------------------------------------------------

def synthetic_part_number(kind: str, description: str, attrs: Optional[dict] = None) -> Optional[str]:
    """Key for a part the site lists without a vendor part number (it happens:
    the Xeon E-24xx options on the Dell HE5xx pages have an empty value). The
    prefix makes it obvious in the queue and impossible to mistake for a real
    order code; the rules layer matches CPUs by model anyway, so the entry
    still does its job. Shared with hcl_sync so index and catalog agree."""
    base = (attrs or {}).get("model") or description or ""
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    if not slug:
        return None
    return ("no-part:" + slug)[:80]


def snapshot_component_index(snapshot: dict) -> Dict[Tuple[str, str], dict]:
    """Dedupe components across platforms, keyed by (kind, part_number).

    The same CPU part is validated on many platforms; the catalog stores it
    once and links platforms to it. ``tce`` on the indexed entry is True if
    any platform flags it, while the per-platform flag is kept in
    ``platforms`` so the sync layer can store the exact relation.
    """
    index = {}  # type: Dict[Tuple[str, str], dict]
    for platform in snapshot.get("platforms") or []:
        brand = platform.get("brand")
        sc_model = platform.get("sc_model")
        for comp in platform.get("components") or []:
            part = comp.get("part_number") or synthetic_part_number(
                comp.get("kind"), comp.get("description") or "", comp.get("attrs"))
            if not part:
                continue
            key = (comp.get("kind"), part)
            entry = index.get(key)
            if entry is None:
                entry = {
                    "kind": comp.get("kind"),
                    "part_number": part,
                    "description": comp.get("description"),
                    "tce": bool(comp.get("tce")),
                    "attrs": dict(comp.get("attrs") or {}),
                    "platforms": [],
                }
                index[key] = entry
            elif comp.get("tce"):
                entry["tce"] = True
            entry["platforms"].append((brand, sc_model, bool(comp.get("tce"))))
    return index
