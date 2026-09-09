"""HCL site scraper tests (docs/bom-checker-plan.md, phase 1).

Runs every parser over a real snapshot of hcl.scalecomputing.com
(tests/fixtures/hcl, 62 HC-Ready platforms + device table) so a template
change on their side shows up as a red test here, not as a corrupted
pending-change queue. The distinct part counts are pinned on purpose: they
are what the first approved scrape seeds the catalog with.

Run: .venv/bin/python -m pytest tests/test_hcl_scrape.py -q
"""
import glob
import http.client
import io
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402
from bom import hcl_scrape as hs  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "hcl")

EXPECTED_PLATFORMS = 62
EXPECTED_DEVICES = 64
# 67 CPUs with a vendor part number + the 4 Xeon E-24xx listed without one
EXPECTED_DISTINCT_PARTS = {"cpu": 71, "nic": 32, "hba": 12, "hdd": 30, "ssd": 52}
EXPECTED_DEVICE_TYPES = {"network": 32, "storage": 26, "gpu": 3, "wifi": 2, "fabric": 1}


def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def manifest():
    return json.loads(_read("manifest.json"))


@pytest.fixture(scope="module")
def listing():
    return hs.parse_listing(_read("hcready_filter.html"))


@pytest.fixture(scope="module")
def details():
    """{(brand, sc_model): parsed detail} for every detail fixture."""
    out = {}
    for path in sorted(glob.glob(os.path.join(FIXTURES, "detail", "*.html"))):
        with open(path, encoding="utf-8") as fh:
            d = hs.parse_detail(fh.read())
        out[(d["brand"], d["sc_model"])] = d
    return out


@pytest.fixture(scope="module")
def devices():
    return hs.parse_devinfo(_read("hcl_devinfo.html"))


def _component(detail, kind, part_number):
    for c in detail["components"]:
        if c["kind"] == kind and c["part_number"] == part_number:
            return c
    raise AssertionError("%s %s not on %s" % (kind, part_number, detail["sc_model"]))


# ---------------------------------------------------------------- listing

def test_listing_has_every_platform_with_a_detail_fixture(listing):
    assert len(listing) == EXPECTED_PLATFORMS
    keys = {(c["brand"], c["sc_model"]) for c in listing}
    assert len(keys) == EXPECTED_PLATFORMS
    for brand, model in keys:
        assert os.path.exists(os.path.join(FIXTURES, "detail", "%s__%s.html" % (brand, model)))


def test_listing_card_fields(listing):
    card = next(c for c in listing if c["brand"] == "lenovo" and c["sc_model"] == "HC1350")
    assert card["form_factor"] == "1U"
    assert card["socket"] == "FCLGA4189"
    assert card["cpu_count"] == 2
    assert card["memory_type"] == "DDR4 RDIMM"
    assert card["max_ram_gb"] == 8192


def test_listing_parses_lines_by_label_not_position():
    page = (
        '<div class="modelBox" data-ru="2U" data-model="HCX" data-brand="acme">'
        "<p><strong>Max RAM:</strong> 512GB</p>"
        "<p><strong>Socket:</strong> SP5</p>"
        "<p><strong>CPU Count:</strong> 1</p>"
        "</div>"
    )
    (card,) = hs.parse_listing(page)
    assert card == {
        "sc_model": "HCX", "brand": "acme", "form_factor": "2U", "socket": "SP5",
        "cpu_count": 1, "memory_type": None, "max_ram_gb": 512,
    }


def test_listing_on_garbage_is_empty():
    assert hs.parse_listing("") == []
    assert hs.parse_listing("<html><body>nope</body></html>") == []


# ---------------------------------------------------------------- detail

def test_every_detail_fixture_parses(details):
    assert len(details) == EXPECTED_PLATFORMS
    for (brand, model), d in details.items():
        assert d["brand"] == brand and d["sc_model"] == model
        assert d["server"], "server missing on %s" % model
        for c in d["components"]:
            assert c["kind"] in hs.COMPONENT_KINDS
            assert "[" not in c["description"] and "]" not in c["description"]
            assert isinstance(c["tce"], bool)
            assert isinstance(c["attrs"], dict)


def test_distinct_part_counts_pinned(details):
    parts = {k: set() for k in hs.COMPONENT_KINDS}
    for d in details.values():
        for c in d["components"]:
            # the site lists four Xeon E-24xx options with an empty value; they
            # are distinct parts and get the synthetic key the catalog stores
            parts[c["kind"]].add(c["part_number"] or hs.synthetic_part_number(
                c["kind"], c["description"], c.get("attrs")))
    assert {k: len(v) for k, v in parts.items()} == EXPECTED_DISTINCT_PARTS


def test_lenovo_hc1350(details):
    d = details[("lenovo", "HC1350")]
    assert d["server"] == "ThinkSystem SR630V2"
    assert d["sockets"] == 2 and d["socket"] == "FCLGA4189" and d["max_cores"] == 80
    assert d["ram_slots"] == 32 and d["ram_type"] == "DDR4 RDIMM" and d["ram_ecc"] is True
    assert d["cooling"] is None and d["risers"] is None  # 'Unspecified' -> None
    assert d["tpm"] == "TPM 2.0 [B0MK]"
    assert d["oob_license"] == "XCC2 Platinum"
    assert [c["part_number"] for c in d["components"] if c["kind"] == "hba"] == [
        "7Y37A01088", "4Y37A78601"]
    assert d["hdd_max"] == 3 and d["ssd_max"] == 1
    assert d["nic_listed"] is True

    tce_nic = _component(d, "nic", "4XC7A80566")
    assert tce_nic["tce"] is True
    assert tce_nic["description"] == (
        "ThinkSystem Broadcom 57504 10/25GbE SFP28 4-port PCIe Ethernet Adapter")

    nic = _component(d, "nic", "4XC7A08294")
    assert nic["tce"] is False
    assert nic["description"] == (
        "ThinkSystem Intel E810-DA2 10/25GbE SFP28 2-Port OCP Ethernet Adapter")
    assert nic["attrs"] == {
        "speed_gbe": 25, "ports": 2, "form_factor": "OCP", "family": "e810", "media": "SFP28"}

    hdd = _component(d, "hdd", "4XB7A93788")
    assert hdd["description"] == 'ThinkSystem 3.5" 12TB 7.2K SAS 12Gb Hot Swap 512e HDD v2'
    assert hdd["attrs"]["capacity_tb"] == 12.0


def test_lenovo_hc1450d_cpu_select(details):
    d = details[("lenovo", "HC1450D")]
    assert d["sockets"] == 2 and d["socket"] == "FCLGA4677"
    assert d["max_cores"] is None  # '(up to 2x FCLGA4677)' states no core cap
    cpu = _component(d, "cpu", "PK8072205511700")
    assert cpu["description"] == "Intel® Xeon® Platinum 8593Q Processor"
    assert cpu["attrs"] == {
        "cores": 64, "threads": 128, "ghz": 3.0, "vendor": "Intel",
        "model": "Xeon Platinum 8593Q", "tier": "Platinum"}
    blank_clock = _component(d, "cpu", "PK8071305120002")
    assert blank_clock["attrs"]["ghz"] is None
    assert blank_clock["attrs"]["model"] == "Xeon Silver 4410Y"
    assert blank_clock["attrs"]["cores"] == 12


def test_lenovo_he155_has_no_components(details):
    d = details[("lenovo", "HE155")]
    assert d["server"] == "ThinkCentre M70q Tiny Gen6"
    assert d["nic_listed"] is False
    assert d["components"] == []
    assert d["hdd_max"] is None and d["ssd_max"] is None
    assert d["ram_type"] == "DDR5 SODIMM" and d["ram_slots"] == 2


def test_supermicro_hc1200(details):
    d = details[("supermicro", "HC1200")]
    hbas = [c for c in d["components"] if c["kind"] == "hba"]
    assert len(hbas) == 1
    assert hbas[0]["part_number"] == "AOC-S3008L-L8i"
    assert hbas[0]["attrs"] == {"model": "AOC-S3008L-L8i", "ports": 8}
    assert d["nic_listed"] is False


def test_hpe_hc1400_nic_family_normalized(details):
    # HPE writes 'BCM57416' (with U+2011 non-breaking hyphens in '2‑port');
    # the family token drops the BCM prefix so it matches Lenovo's '57416'.
    d = details[("hpe", "HC1400")]
    nic = _component(d, "nic", "P26253-B21")
    assert nic["attrs"]["family"] == "57416"
    assert nic["attrs"]["speed_gbe"] == 10
    assert nic["attrs"]["ports"] == 2
    assert nic["attrs"]["media"] == "BASE-T"
    assert d["sockets"] == 1


def test_detail_without_title_raises():
    with pytest.raises(hs.ScrapeParseError):
        hs.parse_detail("<html><body><h1>Internal Server Error</h1></body></html>")
    with pytest.raises(hs.ScrapeParseError):
        hs.parse_detail("")


def test_detail_missing_sections_degrade_to_none():
    d = hs.parse_detail('<h2 id="detailPlatformTitle"> SC//HyperCore-ready [ HCZ ]</h2>')
    assert d["sc_model"] == "HCZ" and d["brand"] is None
    assert d["server"] is None and d["sockets"] is None and d["ram_slots"] is None
    assert d["ram_ecc"] is False and d["nic_listed"] is False
    assert d["components"] == []


# ---------------------------------------------------------------- component attrs

def test_drive_attrs():
    hdd = hs.component_attrs(
        "hdd", 'ThinkSystem 3.5" 12TB 7.2K SAS 12Gb Hot Swap 512e HDD v2')
    assert hdd == {
        "capacity_tb": 12.0, "interface": "SAS", "form_factor": "3.5",
        "rpm": 7200, "endurance": None, "dwpd": None}
    ssd = hs.component_attrs(
        "ssd", 'ThinkSystem 2.5" U.2 VA 3.84TB Read Intensive NVMe PCIe 4.0 x4 HS SSD')
    assert ssd["capacity_tb"] == 3.84
    assert ssd["interface"] == "NVMe"
    assert ssd["form_factor"] == "2.5"
    assert ssd["endurance"] == "Read Intensive"
    assert ssd["rpm"] is None
    dell = hs.component_attrs(
        "ssd", "960GB SSD SATA Read Intensive 6Gbps 512 2.5in Hot-plug AG Drive,"
        "3.5in HYB CARR, 1 DWPD")
    assert dell["capacity_tb"] == 0.96
    assert dell["form_factor"] == "2.5"  # the drive, not its hybrid carrier
    assert dell["dwpd"] == 1.0
    assert hs.component_attrs("hdd", "12TB 7.2K RPM NLSAS 12Gbps 512e 3.5in Hard Drive")[
        "interface"] == "SAS"
    u2 = hs.component_attrs("ssd", "1.92TB Data Center NVMe Read Intensive AG Drive U2 Gen4")
    assert u2["form_factor"] == "2.5"


def test_nic_attrs():
    a = hs.component_attrs("nic", "ThinkSystem Intel X710-T4L 10GBase-T 4-Port PCIe Ethernet Adapter")
    assert a == {"speed_gbe": 10, "ports": 4, "form_factor": "PCIe", "family": "x710", "media": "BASE-T"}
    a = hs.component_attrs("nic", "Intel E810-XXV Dual Port 10/25GbE SFP28, OCP NIC 3.0")
    assert a == {"speed_gbe": 25, "ports": 2, "form_factor": "OCP", "family": "e810", "media": "SFP28"}
    a = hs.component_attrs("nic", "ThinkSystem 10Gb 4-port SFP+ LOM")
    assert a == {"speed_gbe": 10, "ports": 4, "form_factor": "LOM", "family": None, "media": "SFP+"}
    a = hs.component_attrs("nic", "ThinkSystem Broadcom 5719 1GbE RJ45 4-port OCP Ethernet Adapter")
    assert a["speed_gbe"] == 1 and a["family"] == "5719" and a["media"] == "RJ45"
    a = hs.component_attrs("nic", "Intel X710-DA4 4x10Gb SFP+ Adapter")
    assert a["ports"] == 4 and a["speed_gbe"] == 10 and a["form_factor"] is None
    # nothing stated -> nothing guessed
    a = hs.component_attrs("nic", "Supermicro AOC-S25GC-i4S Quad Port E810-CAM1")
    assert a["speed_gbe"] is None and a["ports"] == 4 and a["family"] == "e810"


def test_hba_attrs():
    assert hs.component_attrs("hba", "ThinkSystem 440-16i SAS/SATA PCIe Gen4 12Gb HBA") == {
        "model": "440-16i", "ports": 16}
    assert hs.component_attrs("hba", "Front HBA355i Front Load") == {"model": "HBA355i", "ports": None}
    assert hs.component_attrs("hba", "PERC H755 Adapter") == {"model": "H755", "ports": None}
    assert hs.component_attrs("hba", "HPE MR216i-o Gen11 x16 Lanes")["model"] == "MR216i-o"
    assert hs.component_attrs("hba", "Supermicro 12Gb/s Eight-Port SAS PCIe 4.0 Internal") == {
        "model": None, "ports": 8}


def test_cpu_attrs_amd_and_unknown():
    a = hs.component_attrs("cpu", "AMD EPYC 9354P Processor", "32C/64T @ 3.25GHz")
    assert a == {"cores": 32, "threads": 64, "ghz": 3.25, "vendor": "AMD",
                 "model": "EPYC 9354P", "tier": None}
    a = hs.component_attrs("cpu", "Intel® Xeon® 6767P Processor", "64C/128T @ 3.6GHz")
    assert a["model"] == "Xeon 6767P" and a["tier"] is None and a["vendor"] == "Intel"
    a = hs.component_attrs("cpu", "", None)
    assert a == {"cores": None, "threads": None, "ghz": None, "vendor": None,
                 "model": None, "tier": None}
    assert hs.component_attrs("gpu", "whatever") == {}


# ---------------------------------------------------------------- devinfo

def test_devinfo(devices):
    assert len(devices) == EXPECTED_DEVICES
    counts = {}
    for d in devices:
        counts[d["type"]] = counts.get(d["type"], 0) + 1
    assert counts == EXPECTED_DEVICE_TYPES
    first = devices[0]
    assert first == {
        "dev_id": "a352", "ven_id": "8086", "description": "Cannon Lake PCH SATA AHCI Controller",
        "driver": "ahci", "type": "storage", "supported": True, "since": None}
    with_since = [d for d in devices if d["since"]]
    assert with_since and with_since[0]["since"] == "9.6.17"
    for d in devices:
        assert d["dev_id"] == d["dev_id"].lower() and d["ven_id"] == d["ven_id"].lower()
        assert isinstance(d["supported"], bool)


def test_devinfo_on_garbage_is_empty():
    assert hs.parse_devinfo("") == []
    assert hs.parse_devinfo("<table><tr><td>x</td></tr></table>") == []


# ---------------------------------------------------------------- scrape_all

def _fixture_fetch(manifest, failing=()):
    """fetch(path) serving the snapshot; ``failing`` paths raise ScrapeError."""
    by_url = {p["url"]: p["file"] for p in manifest["pages"]}

    def fetch(path):
        if path in failing:
            raise hs.ScrapeError("GET %s failed after 4 attempts: IncompleteRead" % path)
        if path not in by_url:
            raise hs.ScrapeError("GET %s failed: HTTP 404" % path)
        return _read(by_url[path])

    return fetch


def test_scrape_all_complete(manifest):
    sleeps = []
    progress = []
    snap = hs.scrape_all(
        fetch=_fixture_fetch(manifest),
        progress=lambda done, total, label: progress.append((done, total, label)),
        sleep=sleeps.append, delay=0.25)
    assert snap["complete"] is True
    assert snap["errors"] == []
    assert len(snap["platforms"]) == EXPECTED_PLATFORMS
    assert len(snap["devices"]) == EXPECTED_DEVICES
    assert snap["pages_total"] == EXPECTED_PLATFORMS + 2 == 64
    assert snap["pages_done"] == 64
    assert snap["scraped_at"].endswith("+00:00")
    assert len(progress) == 64 and progress[0][2] == hs.LISTING_PATH
    assert progress[-1] == (64, 64, hs.DEVINFO_PATH)
    assert len(sleeps) == 63 and set(sleeps) == {0.25}
    # merged card + detail
    hc1350 = next(p for p in snap["platforms"] if p["sc_model"] == "HC1350" and p["brand"] == "lenovo")
    assert hc1350["max_ram_gb"] == 8192 and hc1350["server"] == "ThinkSystem SR630V2"
    assert hc1350["form_factor"] == "1U" and hc1350["cpu_count"] == 2
    assert any(c["kind"] == "cpu" for p in snap["platforms"] for c in p["components"])


def test_scrape_all_records_failure_and_continues(manifest):
    bad = "/hcready/detail?model=HC1350&brand=lenovo"
    snap = hs.scrape_all(fetch=_fixture_fetch(manifest, failing={bad}), sleep=lambda s: None)
    assert snap["complete"] is False
    assert len(snap["platforms"]) == EXPECTED_PLATFORMS - 1
    assert len(snap["errors"]) == 1
    assert snap["errors"][0]["path"] == bad
    assert "IncompleteRead" in snap["errors"][0]["error"]
    assert len(snap["devices"]) == EXPECTED_DEVICES
    assert snap["pages_done"] == snap["pages_total"] == 64


def test_scrape_all_listing_failure_is_not_fatal():
    def fetch(path):
        raise hs.ScrapeError("GET %s failed" % path)
    snap = hs.scrape_all(fetch=fetch, sleep=lambda s: None)
    assert snap["complete"] is False
    assert snap["platforms"] == [] and snap["devices"] == []
    assert [e["path"] for e in snap["errors"]] == [hs.LISTING_PATH, hs.DEVINFO_PATH]


def test_scrape_all_parse_error_is_recorded(manifest):
    good = _fixture_fetch(manifest)

    def fetch(path):
        if path.startswith("/hcready/detail?model=HE155"):
            return "<html>Internal Server Error</html>"
        return good(path)

    snap = hs.scrape_all(fetch=fetch, sleep=lambda s: None)
    assert snap["complete"] is False
    assert len(snap["errors"]) == 1 and "ScrapeParseError" in snap["errors"][0]["error"]


def test_detail_path_carries_mandatory_brand():
    assert hs.detail_path("HC5250D-V", "dell") == "/hcready/detail?model=HC5250D-V&brand=dell"


def test_snapshot_component_index(manifest):
    snap = hs.scrape_all(fetch=_fixture_fetch(manifest), sleep=lambda s: None)
    index = hs.snapshot_component_index(snap)
    counts = {}
    for kind, _ in index:
        counts[kind] = counts.get(kind, 0) + 1
    assert counts == EXPECTED_DISTINCT_PARTS
    cpu = index[("cpu", "PK8072205511700")]
    assert cpu["description"] == "Intel® Xeon® Platinum 8593Q Processor"
    assert cpu["attrs"]["cores"] == 64
    assert len(cpu["platforms"]) > 1  # the same CPU is validated on many platforms
    assert ("lenovo", "HC1450D", False) in cpu["platforms"]
    nic = index[("nic", "4XC7A80566")]
    assert nic["tce"] is True
    assert ("lenovo", "HC1350", True) in nic["platforms"]
    assert hs.snapshot_component_index({}) == {}


# ---------------------------------------------------------------- fetch_page

class _FakeResponse(object):
    def __init__(self, body):
        self._body = body
        self.closed = False

    def read(self):
        return self._body

    def close(self):
        self.closed = True


def test_fetch_page_retries_incomplete_read(monkeypatch):
    calls = []
    outcomes = [
        http.client.IncompleteRead(b"partial"),
        http.client.IncompleteRead(b"partial"),
        _FakeResponse("<h2>ok</h2>".encode("utf-8")),
    ]

    def fake_urlopen(req, timeout=None):
        calls.append((req.full_url, dict(req.header_items()), timeout))
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sleeps = []
    text = hs.fetch_page("/hcready/filter", timeout=7, sleep=sleeps.append)
    assert text == "<h2>ok</h2>"
    assert len(calls) == 3
    assert sleeps == [0.5, 1.0]
    url, headers, timeout = calls[0]
    assert url == "https://hcl.scalecomputing.com/hcready/filter" and timeout == 7
    lowered = {k.lower(): v for k, v in headers.items()}
    assert lowered["user-agent"] == "sc-sizer-bom-checker/1.0"
    assert lowered["accept-encoding"] == "identity"
    assert lowered["connection"] == "close"


def test_fetch_page_gives_up_after_attempts(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection reset")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    sleeps = []
    with pytest.raises(hs.ScrapeError) as exc:
        hs.fetch_page("/hcl/devinfo", attempts=4, sleep=sleeps.append)
    assert "/hcl/devinfo" in str(exc.value)
    assert sleeps == [0.5, 1.0, 2.0]


def test_fetch_page_retries_5xx_but_not_4xx(monkeypatch):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        code = 503 if len(calls) == 1 else 200
        if code != 200:
            raise urllib.error.HTTPError(req.full_url, code, "boom", {}, io.BytesIO(b""))
        return _FakeResponse(b"fine")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert hs.fetch_page("/x", sleep=lambda s: None) == "fine"
    assert len(calls) == 2

    def not_found(req, timeout=None):
        calls.append("nf")
        raise urllib.error.HTTPError(req.full_url, 500 - 96, "nope", {}, io.BytesIO(b""))

    monkeypatch.setattr(urllib.request, "urlopen", not_found)
    calls[:] = []
    with pytest.raises(hs.ScrapeError) as exc:
        hs.fetch_page("/missing", sleep=lambda s: None)
    assert "404" in str(exc.value) and "/missing" in str(exc.value)
    assert calls == ["nf"]  # no retry on a client error
