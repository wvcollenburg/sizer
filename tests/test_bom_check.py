"""Platform matching, enrichment and the check orchestrator
(docs/bom-checker-build.md §4-§5).

These are the pieces that sit *around* the faithful rules port: which
platform a BOM is built on, what the scraped catalog adds (delisted parts,
swap suggestions), and which results need a human. They run on a small
hand-built catalog in SQLite so the assertions are about behaviour, not about
the live HCL.

Run: .venv/bin/python -m pytest tests/test_bom_check.py -q
"""
import os
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

from database import db  # noqa: E402
import auth_models  # noqa: F401,E402
import project_models  # noqa: F401,E402
from hcl_models import (HclComponent, HclPlatform, HclPlatformComponent,  # noqa: E402
                        STATUS_DELISTED)
from bom.normalize import BOMComponent, BOMConfig, NormalizedBOM  # noqa: E402
from bom.rules import HclData, HclHba, HclNic, HclCpu  # noqa: E402
from bom import platform_match, enrich  # noqa: E402
from bom.check import run_check  # noqa: E402
from bom.hcl_scrape import component_attrs  # noqa: E402


@pytest.fixture()
def app():
    application = Flask(__name__)
    application.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///:memory:"
    application.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(application)
    with application.app_context():
        db.drop_all()
        db.create_all()
        _seed()
        yield application


def _comp(kind, part, desc, **attrs):
    a = component_attrs(kind, desc)
    a.update(attrs)
    c = HclComponent(kind=kind, part_number=part, description=desc, attrs=a)
    db.session.add(c)
    return c


def _seed():
    """Two Lenovo SC models on the same server (union of parts is the
    platform's validated list), one Dell platform, one delisted NIC."""
    sr630 = HclPlatform(brand="lenovo", sc_model="HC1450D", server="ThinkSystem SR630V3",
                        form_factor="1U", socket="FCLGA4677", sockets=2)
    sr630b = HclPlatform(brand="lenovo", sc_model="HC3450F", server="ThinkSystem SR630V3",
                         form_factor="1U", socket="FCLGA4677", sockets=2)
    r760 = HclPlatform(brand="dell", sc_model="HC5450D-V", server="PowerEdge R760XD2",
                       form_factor="2U", socket="FCLGA4677", sockets=2)
    db.session.add_all([sr630, sr630b, r760])
    e810_2 = _comp("nic", "4XC7A08294", "ThinkSystem Intel E810-DA2 10/25GbE SFP28 2-Port OCP Ethernet Adapter")
    e810_4 = _comp("nic", "4XC7A80269", "ThinkSystem Intel E810-DA4 10/25GbE SFP28 4-Port OCP Ethernet Adapter")
    x710 = _comp("nic", "7ZT7A00537", "ThinkSystem Intel X710-DA2 PCIe 10Gb 2-Port SFP+ Ethernet Adapter")
    i350 = _comp("nic", "7ZT7A00535", "ThinkSystem I350-T4 PCIe 1Gb 4-Port RJ45 Ethernet Adapter")
    hba = _comp("hba", "4Y37A78602", "ThinkSystem 440-16i SAS/SATA PCIe Gen4 12Gb HBA")
    cpu = _comp("cpu", "PK8071305120500", "Intel Xeon Gold 6526Y Processor", model="Xeon Gold 6526Y")
    old = _comp("nic", "7ZT7A00547", "ThinkSystem 10Gb 4-port SFP+ LOM")
    old.status = STATUS_DELISTED
    old.delisted_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    db.session.flush()
    for p in (sr630, sr630b):
        for c in (e810_2, e810_4, x710, i350, hba, cpu):
            db.session.add(HclPlatformComponent(platform=p, component=c, tce=(c is e810_4 and p is sr630)))
    db.session.commit()


def hcl_data():
    return HclData(
        hbas=[HclHba(part="4Y37A78602", type="SAS", description="ThinkSystem 440-16i SAS/SATA PCIe Gen4 12Gb HBA", eol=False, supported=True)],
        nics=[HclNic(part="4XC7A08294", type="e810", speed="25GbE", description="ThinkSystem Intel E810-DA2 10/25GbE SFP28 2-Port OCP Ethernet Adapter", form_factor="OCP"),
              HclNic(part="4XC7A80269", type="e810", speed="25GbE", description="ThinkSystem Intel E810-DA4 10/25GbE SFP28 4-Port OCP Ethernet Adapter", form_factor="OCP"),
              HclNic(part="7ZT7A00537", type="x710", speed="10GbE", description="ThinkSystem Intel X710-DA2 PCIe 10Gb 2-Port SFP+ Ethernet Adapter", form_factor="PCIe")],
        cpus=[HclCpu(model="Xeon Gold 6526Y", description="Intel Xeon Gold 6526Y Processor", socket="FCLGA4677")],
        gpus=[], blocked_vendors=[],
    )


def c(category, desc, part=None, qty=1):
    return BOMComponent(part_number=part, description=desc, quantity=qty, category=category)


def lenovo_config(nic_desc, name="PROD", server="ThinkSystem SR630 V3"):
    return BOMConfig(name=name, server_model=server, node_count=3, components=[
        c("chassis", 'ThinkSystem V3 1U 10x2.5" Chassis', "BLK4", 3),
        c("cpu", "Intel Xeon Gold 6526Y 16C 195W 2.8GHz Processor", "BYVX", 3),
        c("memory", "ThinkSystem 32GB TruDDR5 5600MHz (2Rx8) RDIMM", "BWJC", 24),
        c("storage", 'ThinkSystem 2.5" U.2 VA 3.84TB Read Intensive NVMe PCIe 4.0 x4 HS SSD', "C18M", 12),
        c("nic", nic_desc, "BXXX", 3),
        c("other", "ThinkSystem SR630 V3 MB", "BLK5", 3),
    ])


# ── platform matching ────────────────────────────────────────────────────────

def test_identify_by_server_model_returns_every_sc_model_on_that_server(app):
    cfg = lenovo_config("ThinkSystem Intel E810-DA4 10/25GbE SFP28 4-Port OCP Ethernet Adapter")
    found = platform_match.identify(cfg, "Lenovo")
    assert sorted(p.sc_model for p in found) == ["HC1450D", "HC3450F"]


def test_identify_falls_back_to_motherboard_line_when_server_model_missing(app):
    cfg = lenovo_config("x", server=None)
    assert {p.sc_model for p in platform_match.identify(cfg, "Lenovo")} == {"HC1450D", "HC3450F"}


def test_identify_is_exact_not_prefix(app):
    """A PowerEdge R760 must not match the R760XD2 platform: different
    chassis, different validated list."""
    cfg = BOMConfig(name="Config 1", server_model="PowerEdge R760", components=[
        c("chassis", "PowerEdge R760 Server", "210-BDZY", 1)])
    assert platform_match.identify(cfg, "Dell") == []
    cfg2 = BOMConfig(name="Config 1", server_model="PowerEdge R760XD2", components=[])
    assert [p.sc_model for p in platform_match.identify(cfg2, "Dell")] == ["HC5450D-V"]


def test_identify_respects_vendor_brand(app):
    cfg = lenovo_config("x")
    assert platform_match.identify(cfg, "Dell") == []
    assert platform_match.identify(cfg, None)          # unknown vendor searches all brands


def test_model_tokens():
    assert platform_match.model_tokens("Scale Computing Platform SR630 V3") == ["sr630v3"]
    assert platform_match.model_tokens("PowerEdge R660xs Server") == ["r660xs"]
    assert "dl320gen11" in platform_match.model_tokens("HPE ProLiant DL320 Gen11")
    assert platform_match.model_tokens("Riser cage") == []


# ── enrichment ───────────────────────────────────────────────────────────────

def test_delisted_part_draws_a_dated_warning(app):
    cfg = lenovo_config("ThinkSystem 10Gb 4-port SFP+ LOM")
    findings = enrich.delisted_findings(cfg, enrich.load_delisted())
    assert len(findings) == 1
    f = findings[0]
    assert f.code == "component_delisted" and f.severity == "warning"
    assert "2026-08-01" in f.issue


def test_delisted_matches_dell_part_with_p_suffix(app):
    old = HclComponent.query.filter_by(part_number="7ZT7A00547").one()
    bom_line = c("nic", "different words", "7zt7a00547-P")
    assert enrich._matches_component(bom_line, old)


def test_suggestions_come_from_the_identified_platform_only(app):
    cfg = lenovo_config("Mellanox ConnectX-6 Dx 25GbE 2-port OCP")
    platforms = platform_match.identify(cfg, "Lenovo")
    result = run_check(NormalizedBOM(vendor="Lenovo", configs=[cfg]), hcl=hcl_data(), platforms=platforms)
    sugg = result["suggestions"]
    assert len(sugg) == 1 and sugg[0]["kind"] == "nic" and sugg[0]["code"] == "nic_not_in_hcl"
    parts = [x["part_number"] for x in sugg[0]["candidates"]]
    # 25 GbE needed: the 10 GbE X710 and 1 GbE I350 are filtered out; OCP first.
    assert parts == ["4XC7A08294", "4XC7A80269"]
    assert any(x["tce"] for x in sugg[0]["candidates"])


def test_no_platform_means_no_suggestions_but_a_plain_red_flag(app):
    cfg = lenovo_config("Mellanox ConnectX-6 Dx 25GbE 2-port OCP", server="ThinkSystem SR999 V9")
    cfg.components = [x for x in cfg.components if x.category != "other"]
    result = run_check(NormalizedBOM(vendor="Lenovo", configs=[cfg]), hcl=hcl_data())
    assert result["technical"]["verdict"] == "FAIL"
    assert result["suggestions"] == []
    codes = [f["code"] for f in result["technical"]["config_results"][0]["findings"]]
    assert "platform_unknown" in codes and "nic_not_in_hcl" in codes


# ── the orchestrator ─────────────────────────────────────────────────────────

def test_clean_pass_needs_no_review(app):
    cfg = lenovo_config("ThinkSystem Intel E810-DA4 10/25GbE SFP28 4-Port OCP Ethernet Adapter")
    result = run_check(NormalizedBOM(vendor="Lenovo", configs=[cfg]), hcl=hcl_data())
    assert result["technical"]["verdict"] == "PASS"
    assert result["flag_reasons"] == []
    cr = result["technical"]["config_results"][0]
    assert cr["platform"]["sc_models"] == ["HC1450D", "HC3450F"]
    assert cr["platform"]["form_factor"] == "1U"
    assert cr["node_count"] == 3
    assert result["fit"] is None


def test_not_in_hcl_and_delisted_parts_open_a_review(app):
    cfg = lenovo_config("ThinkSystem 10Gb 4-port SFP+ LOM")
    result = run_check(NormalizedBOM(vendor="Lenovo", configs=[cfg]), hcl=hcl_data())
    assert "component_delisted" in result["flag_reasons"]
    assert "nic_not_in_hcl" in result["flag_reasons"]


def test_platform_form_factor_drives_the_dwpd_threshold(app):
    """1U platform → 0.2 DWPD floor: a 0.25 DWPD drive passes there but fails
    the 0.3 default used when no platform is identified."""
    drive = c("storage", 'ThinkSystem 2.5" 3.84TB 0.25DWPD Read Intensive NVMe SSD', "C18M", 12)
    cfg = lenovo_config("ThinkSystem Intel E810-DA4 10/25GbE SFP28 4-Port OCP Ethernet Adapter")
    cfg.components = [x if x.category != "storage" else drive for x in cfg.components]
    with_platform = run_check(NormalizedBOM(vendor="Lenovo", configs=[cfg]), hcl=hcl_data())
    codes = [f["code"] for f in with_platform["technical"]["config_results"][0]["findings"]]
    assert "dwpd_low" not in codes
    cfg.server_model = "ThinkSystem SR999 V1"
    cfg.components = [x for x in cfg.components if x.category != "other"]
    without = run_check(NormalizedBOM(vendor="Lenovo", configs=[cfg]), hcl=hcl_data())
    codes = [f["code"] for f in without["technical"]["config_results"][0]["findings"]]
    assert "dwpd_low" in codes


def test_services_only_bom_is_inconclusive(app):
    cfg = BOMConfig(name="Software", server_model=None, components=[c("other", "SC//HyperCore licence")])
    result = run_check(NormalizedBOM(vendor="Lenovo", configs=[cfg]), hcl=hcl_data())
    assert result["technical"]["verdict"] == "INCONCLUSIVE"
    assert result["technical"]["config_results"] == []
    assert "inconclusive" in result["flag_reasons"]
