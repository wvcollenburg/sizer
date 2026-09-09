"""BOM rules engine tests (docs/bom-checker-plan.md phase 2).

Two layers:

  - Acceptance: replay SC//Design's 26 archived fixtures (NormalizedBOM in,
    ValidationResult out, against their HCL snapshot) and demand the same
    verdicts and the same findings in the same order. The archive is
    gitignored, so these skip — never silently pass — when it is absent.
  - Unit: the helper semantics that the fixtures happen not to exercise
    (DWPD text formatting, SED, GPU rules, single-drive tiers, diskless, the
    INCONCLUSIVE routes, blocked vendors) plus our two additive extensions
    (form-factor override, finding codes) and the lenient from_dict.

Run: .venv/bin/python -m pytest tests/test_bom_rules.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402

from bom.normalize import (  # noqa: E402
    BOMComponent,
    BOMConfig,
    Finding,
    NormalizedBOM,
    ValidationResult,
    normalize_part,
)
from bom.rules import (  # noqa: E402
    HclData,
    HclGpu,
    HclHba,
    HclNic,
    determine_verdict,
    dwpd_threshold,
    extract_gpu_keywords,
    extract_hba_keywords,
    extract_nic_keywords,
    find_cpu_in_hcl,
    find_gpu_in_hcl,
    find_hba_in_hcl,
    find_nic_in_hcl,
    get_drive_count,
    is_absence_indicator,
    is_boss_card,
    is_clearly_old_cpu,
    is_hardware_config,
    is_hdd,
    is_scalable_cpu,
    is_sed_drive,
    is_ssd,
    parse_dwpd,
    validate_bom,
    validate_config,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FIXTURES_DIR = os.path.join(REPO_ROOT, "_archive", "SC-Sizing-main", "tests", "fixtures", "bom")
NORMALIZED_DIR = os.path.join(FIXTURES_DIR, "normalized")
EXPECTED_DIR = os.path.join(FIXTURES_DIR, "expected")
HCL_SNAPSHOT = os.path.join(FIXTURES_DIR, "hcl-snapshot.json")

ARCHIVE_MISSING = "SC//Design archive fixtures not present (_archive is gitignored)"


def _fixture_names():
    if not (os.path.isdir(NORMALIZED_DIR) and os.path.isfile(HCL_SNAPSHOT)):
        return []
    return sorted(f for f in os.listdir(NORMALIZED_DIR) if f.endswith(".json"))


# Parametrize at collection time; with no archive we still emit one skipped
# case so the suite reports the gap instead of quietly collecting nothing.
FIXTURE_PARAMS = _fixture_names() or [
    pytest.param(None, marks=pytest.mark.skip(reason=ARCHIVE_MISSING))
]


def _load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def snapshot_hcl():
    if not os.path.isfile(HCL_SNAPSHOT):
        pytest.skip(ARCHIVE_MISSING)
    return HclData.from_dict(_load(HCL_SNAPSHOT))


def _findings_tuples(config_result_dict):
    return [
        (f["severity"], f["component"], f["issue"], f["remediation"])
        for f in config_result_dict["findings"]
    ]


# ─── acceptance: archived fixtures ───────────────────────────────────────────


@pytest.mark.parametrize("name", FIXTURE_PARAMS)
def test_normalized_fixture_round_trips(name):
    raw = _load(os.path.join(NORMALIZED_DIR, name))
    bom = NormalizedBOM.from_dict(raw)
    assert bom.to_dict() == raw


@pytest.mark.parametrize("name", FIXTURE_PARAMS)
def test_fixture_matches_expected_validation(name, snapshot_hcl):
    expected_path = os.path.join(EXPECTED_DIR, name)
    if not os.path.isfile(expected_path):
        pytest.skip("no expected/%s in archive" % name)
    bom = NormalizedBOM.from_dict(_load(os.path.join(NORMALIZED_DIR, name)))
    expected = _load(expected_path)

    got = validate_bom(bom, snapshot_hcl).to_dict()

    assert got["verdict"] == expected["verdict"]
    assert len(got["configResults"]) == len(expected["configResults"])
    for got_cfg, exp_cfg in zip(got["configResults"], expected["configResults"]):
        assert got_cfg["configName"] == exp_cfg["configName"]
        assert got_cfg["verdict"] == exp_cfg["verdict"]
        assert _findings_tuples(got_cfg) == _findings_tuples(exp_cfg)


def test_snapshot_hcl_shape(snapshot_hcl):
    # The snapshot uses camelCase formFactor / blockedVendors; both must land.
    assert snapshot_hcl.nics and snapshot_hcl.nics[0].form_factor
    assert snapshot_hcl.blocked_vendors == ["hpe"]
    assert snapshot_hcl.hbas and snapshot_hcl.cpus


# ─── unit: builders ──────────────────────────────────────────────────────────


def comp(category, description, part=None, qty=1):
    return BOMComponent(part_number=part, description=description, quantity=qty, category=category)


def make_bom(components, vendor="Dell", server_model=None):
    return NormalizedBOM(
        vendor=vendor,
        configs=[BOMConfig(name="Config 1", server_model=server_model, components=components)],
        raw_text="",
    )


EMPTY_HCL = HclData(blocked_vendors=["hpe"])

VALID_NIC = comp("nic", "Intel X710 10GbE", "X710-DA2")
VALID_CPU = comp("cpu", "Intel Xeon Gold 6338")
VALID_SSD = comp("storage", "Samsung PM9A3 NVMe SSD 1.92TB 1DWPD", qty=3)  # 3 drives: no single/2-drive warnings


def small_hcl():
    return HclData(
        hbas=[
            HclHba(part="405-AAXX", type="SAS", description="HBA355i Front", eol=False, supported=True),
            HclHba(part="7Y37A01088", type="SAS", description="ThinkSystem 430-8i SAS/SATA 12Gb HBA"),
            HclHba(part="4Y37A78834", type="SAS", description="ThinkSystem 440-16i SAS/SATA PCIe Gen4 12Gb HBA"),
            HclHba(part="405-OLD", type="SAS", description="HBA330 Adapter", eol=True, supported=True),
            HclHba(part="405-NOPE", type="RAID", description="PERC H965i Front", eol=False, supported=False),
        ],
        nics=[
            HclNic(part="X710-DA2", type="PCIe", speed="10Gb", description="Intel X710 Dual Port 10GbE SFP+", form_factor="PCIe"),
            HclNic(part="540-BCXW", type="OCP", speed="25Gb", description="Intel E810-XXV Dual Port 25GbE OCP", form_factor="OCP"),
            HclNic(part="540-LOM", type="LOM", speed="1Gb", description="Broadcom 5720 Quad Port 1GbE", form_factor="LOM"),
        ],
        cpus=[HclCpu(model="Xeon Gold 6338", description="Intel Xeon Gold 6338", socket="LGA4189"),
              HclCpu(model="AMD EPYC 9354", description="AMD EPYC 9354 32-Core", socket="SP5")],
        gpus=[
            HclGpu(part="490-BJRV", model="L4", description="NVIDIA L4 24GB", vram=24, eol=False, supported=True),
            HclGpu(part="490-OLD", model="T4", description="NVIDIA T4 16GB", vram=16, eol=True, supported=True),
            HclGpu(part="490-BAD", model="A2", description="NVIDIA A2 16GB", vram=16, eol=False, supported=False),
        ],
        blocked_vendors=["hpe"],
    )


from bom.rules import HclCpu  # noqa: E402  (after builders for readability)


def findings_of(result, idx=0):
    return result.config_results[idx].findings


def codes_of(result, idx=0):
    return [f.code for f in findings_of(result, idx)]


# ─── unit: helper semantics ──────────────────────────────────────────────────


def test_parse_dwpd_variants():
    assert parse_dwpd("<2DWPD") == {"value": 2.0, "lessThan": True}
    assert parse_dwpd("1DWPD") == {"value": 1.0, "lessThan": False}
    assert parse_dwpd("0.3 DWPD") == {"value": 0.3, "lessThan": False}
    assert parse_dwpd("3.84TB Read Intensive NVMe") is None
    assert parse_dwpd("rated 1 dwpd") == {"value": 1.0, "lessThan": False}


def test_absence_indicators():
    assert is_absence_indicator(comp("boss", "No BOSS"))
    assert is_absence_indicator(comp("boss", "BOSS Blank"))
    assert is_absence_indicator(comp("other", "Riser Blank"))
    assert is_absence_indicator(comp("other", "Blank filler"))
    assert not is_absence_indicator(comp("boss", "BOSS-N1 controller card"))
    # "Nominal" starts with "no" but not "no " — must not count.
    assert not is_absence_indicator(comp("other", "Nominal riser"))
    assert not is_boss_card(comp("boss", "No BOSS card"))
    assert is_boss_card(comp("other", "Boot Optimized Server Storage S2"))


def test_dwpd_threshold_heuristic_and_override():
    two_u = BOMConfig(name="c", server_model="PowerEdge R760", components=[])
    one_u = BOMConfig(name="c", server_model="ThinkSystem SR630 V3 1U", components=[])
    sff = BOMConfig(name="c", server_model="Edge SFF box", components=[])
    none = BOMConfig(name="c", server_model=None, components=[])
    assert dwpd_threshold(two_u) == 0.3
    assert dwpd_threshold(one_u) == 0.2
    assert dwpd_threshold(sff) == 0.2
    assert dwpd_threshold(none) == 0.3
    # "1U" glued into a token ("R1U") is not a word — same as the JS \b.
    assert dwpd_threshold(BOMConfig(name="c", server_model="R1UX", components=[])) == 0.3
    # Explicit form factor beats the model-string guess in both directions.
    assert dwpd_threshold(two_u, form_factor="1U") == 0.2
    assert dwpd_threshold(two_u, form_factor="dt") == 0.2
    assert dwpd_threshold(one_u, form_factor="2U") == 0.3
    assert dwpd_threshold(one_u, form_factor="") == 0.2


def test_is_scalable_cpu_variants():
    assert is_scalable_cpu("Intel Xeon 6 6767P 64C")
    assert is_scalable_cpu("Intel Xeon 6 Performance 6325P")
    assert is_scalable_cpu("Xeon 6960P")
    assert is_scalable_cpu("Intel Xeon Gold 6526Y 16C 195W 2.8GHz Processor")
    assert is_scalable_cpu("Intel Xeon Silver 4410Y")
    assert is_scalable_cpu("Xeon E-2434")
    assert is_scalable_cpu("Intel Raptor Lake-E E-2434")
    assert is_scalable_cpu("E-2434 4C")
    assert is_scalable_cpu("Intel 8462Y+ SPR 32C")
    assert is_scalable_cpu("Sapphire Rapids 8462Y+")
    # The SPR/EMR/GNR and bare E-2xxx checks are case-sensitive in the source.
    assert not is_scalable_cpu("spr 8462Y+")
    assert not is_scalable_cpu("e-2434 4C")
    assert not is_scalable_cpu("AMD EPYC 9354")
    assert not is_scalable_cpu("Intel Xeon E5-2697A v4")


def test_is_clearly_old_cpu():
    assert is_clearly_old_cpu("Intel Xeon E5-2697A v4 2.6GHz")
    assert is_clearly_old_cpu("E7-8890 v4")
    assert is_clearly_old_cpu("Xeon E3-1230")
    assert not is_clearly_old_cpu("Intel Xeon E-2434")
    assert not is_clearly_old_cpu("Intel Xeon Gold 6338")


def test_find_cpu_in_hcl_substring_semantics():
    hcl = small_hcl()
    assert find_cpu_in_hcl(comp("cpu", "Intel Xeon Gold 6338 32C"), hcl)
    assert find_cpu_in_hcl(comp("cpu", "AMD EPYC 9354 32-core"), hcl)
    assert not find_cpu_in_hcl(comp("cpu", "AMD EPYC 9554"), hcl)
    assert not find_cpu_in_hcl(comp("cpu", "anything"), HclData())


def test_find_hba_in_hcl_fallbacks():
    hcl = small_hcl()
    # exact part
    assert find_hba_in_hcl(comp("controller", "some card", "405-AAXX"), hcl).part == "405-AAXX"
    # '-P' suffix stripped
    assert find_hba_in_hcl(comp("controller", "some card", "405-AAXX-P"), hcl).part == "405-AAXX"
    # keyword from description (HBA355i) when the part is unknown
    assert find_hba_in_hcl(comp("controller", "Dell HBA355i Adapter", "405-ZZZZ"), hcl).part == "405-AAXX"
    # Lenovo model number style
    assert find_hba_in_hcl(comp("controller", "ThinkSystem 440-16i SAS/SATA PCIe Gen4 12Gb HBA", "BM50"), hcl).part == "4Y37A78834"
    # exact description fallback when neither part nor keyword hits
    assert find_hba_in_hcl(comp("controller", "ThinkSystem 430-8i SAS/SATA 12Gb HBA", "FEATURECODE"), hcl).part == "7Y37A01088"
    assert find_hba_in_hcl(comp("controller", "Mystery RAID card", "XYZ"), hcl) is None
    # empty part number is dropped from the search terms (filter(Boolean))
    assert find_hba_in_hcl(comp("controller", "HBA355i Front", ""), hcl).part == "405-AAXX"


def test_extract_keywords():
    # "H755N" is not matched by the upstream regex (the trailing N breaks the
    # word boundary) - that is faithful to validator.ts, so test the plain form.
    assert extract_hba_keywords("perc  h755n front") is None
    assert extract_hba_keywords("perc  h755 front") == ["perc h755"]
    assert extract_hba_keywords("h750 adapter") == ["h750"]
    assert extract_hba_keywords("thinksystem 4350-16i hba") == ["4350-16i"]
    assert extract_hba_keywords("nothing here") is None
    assert extract_nic_keywords("Intel E810-XXV") == ["e810"]
    assert extract_nic_keywords("Broadcom 57504 quad") == ["57504"]
    assert extract_nic_keywords("Mellanox CX6 Dx") == ["cx6"]
    assert extract_nic_keywords("Realtek 8111") is None
    assert extract_gpu_keywords("NVIDIA RTX A1000 8GB") == ["a1000"]
    assert extract_gpu_keywords("NVIDIA L4 24GB") == ["l4"]
    assert extract_gpu_keywords("no gpu id") is None


def test_find_nic_and_gpu_in_hcl():
    hcl = small_hcl()
    assert find_nic_in_hcl(comp("nic", "anything", "x710-da2-p"), hcl).part == "X710-DA2"
    assert find_nic_in_hcl(comp("nic", "Intel E810-XXV4 25GbE", "unknown"), hcl).part == "540-BCXW"
    assert find_nic_in_hcl(comp("nic", "Realtek 8111", None), hcl) is None
    assert find_gpu_in_hcl(comp("gpu", "NVIDIA L4 24GB GPU"), hcl).part == "490-BJRV"
    assert find_gpu_in_hcl(comp("gpu", "GPU", "490-old"), hcl).part == "490-OLD"
    assert find_gpu_in_hcl(comp("gpu", "NVIDIA H100 80GB"), hcl) is None


def test_drive_classification_and_counts():
    hdd = comp("storage", "12TB 7.2K NL-SAS 3.5in HDD", qty=9)
    nvme = comp("storage", "3.84TB U.2 NVMe Read Intensive SSD", qty=3)
    sata_ssd = comp("storage", "960GB SATA SSD Mixed Use", qty=0)  # qty 0 counts as 1
    assert is_hdd(hdd) and not is_ssd(hdd)
    assert is_ssd(nvme) and is_ssd(sata_ssd)
    assert get_drive_count([hdd, nvme, sata_ssd, comp("other", "HDD Filler")]) == {
        "nvme": 3, "ssd": 1, "hdd": 9, "total": 13}
    assert is_sed_drive(comp("storage", "1.92TB SAS SED SSD"))
    assert not is_sed_drive(comp("storage", "USED drive"))


def test_normalize_part():
    assert normalize_part("405-AAXX-P") == "405-AAXX"
    assert normalize_part("405-aaxx-p") == "405-aaxx"
    assert normalize_part("405-P-AAXX") == "405-P-AAXX"


# ─── unit: rule outcomes ─────────────────────────────────────────────────────


def test_boss_card_fails_and_carries_code():
    result = validate_bom(make_bom([comp("boss", "Dell BOSS-S2 Controller Card", "403-BBST"),
                                    VALID_NIC, VALID_CPU, VALID_SSD]), small_hcl())
    assert result.verdict == "FAIL"
    assert "boss_card" in codes_of(result)
    # Absence indicators never trigger it.
    result = validate_bom(make_bom([comp("boss", "No BOSS Card"), VALID_NIC, VALID_CPU, VALID_SSD]), small_hcl())
    assert "boss_card" not in codes_of(result)


def test_nvme_only_with_controller_fails():
    result = validate_bom(make_bom([comp("controller", "PERC H755N Front", "405-AAZZ"),
                                    VALID_NIC, VALID_CPU, VALID_SSD]), small_hcl())
    assert codes_of(result)[0] == "nvme_only_controller"
    assert result.verdict == "FAIL"


def test_missing_controller_warns_with_vendor_suggestions():
    result = validate_bom(make_bom([comp("storage", "2.4TB 10K SAS HDD", qty=4), VALID_NIC, VALID_CPU]),
                          small_hcl())
    f = findings_of(result)[0]
    assert f.code == "controller_missing" and f.severity == "warning"
    # Dell suggestions: only supported 405- parts, EOL still counts as supported.
    assert f.remediation.endswith("Supported options for Dell: 405-AAXX (HBA355i Front), 405-OLD (HBA330 Adapter).")
    # Unknown vendor: no suggestion tail.
    result = validate_bom(make_bom([comp("storage", "2.4TB 10K SAS HDD", qty=4), VALID_NIC, VALID_CPU],
                                   vendor="Unknown"), small_hcl())
    assert findings_of(result)[0].remediation.endswith("final build.")


def test_controller_states():
    hdd = comp("storage", "2.4TB 10K SAS HDD", qty=4)
    hcl = small_hcl()
    # Supported + description says HBAxxx → "HBA mode active by default"
    result = validate_bom(make_bom([comp("controller", "HBA355i Front", "405-AAXX"), hdd, VALID_NIC, VALID_CPU]), hcl)
    assert codes_of(result) == ["controller_hba_default"] and result.verdict == "PASS"
    # Supported card whose description lacks an "HBA<digits>" token (the Lenovo
    # naming) → generic passthrough info, exactly as the upstream regex decides
    result = validate_bom(make_bom([comp("controller", "ThinkSystem 440-16i SAS/SATA PCIe Gen4 12Gb HBA", "BM50"),
                                    hdd, VALID_NIC, VALID_CPU], vendor="Lenovo"), hcl)
    assert codes_of(result) == ["controller_passthrough"]
    result = validate_bom(make_bom([comp("controller", "Some 430-8i card", "7Y37A01088"), hdd, VALID_NIC, VALID_CPU]), hcl)
    assert codes_of(result) == ["controller_passthrough"]
    # EOL adds a warning after the info
    result = validate_bom(make_bom([comp("controller", "HBA330 Adapter", "405-OLD"), hdd, VALID_NIC, VALID_CPU]), hcl)
    assert codes_of(result) == ["controller_hba_default", "controller_eol"] and result.verdict == "PASS"
    # In HCL but unsupported → error
    result = validate_bom(make_bom([comp("controller", "PERC H965i Front", "405-NOPE"), hdd, VALID_NIC, VALID_CPU]), hcl)
    assert codes_of(result) == ["controller_unsupported"] and result.verdict == "FAIL"
    # Not in HCL → error with suggestions
    result = validate_bom(make_bom([comp("controller", "Mystery card", "999"), hdd, VALID_NIC, VALID_CPU]), hcl)
    assert codes_of(result) == ["controller_not_in_hcl"]
    assert "Supported options for Dell" in findings_of(result)[0].remediation
    # Two controllers → multiple + one finding each
    result = validate_bom(make_bom([comp("controller", "HBA355i Front", "405-AAXX"),
                                    comp("controller", "HBA330 Adapter", "405-OLD"), hdd, VALID_NIC, VALID_CPU]), hcl)
    assert codes_of(result)[0] == "controller_multiple"
    assert findings_of(result)[0].component == "HBA355i Front, HBA330 Adapter"


def test_cpu_rules_and_inconclusive():
    hcl = small_hcl()
    result = validate_bom(make_bom([comp("cpu", "Intel Xeon E5-2697A v4"), VALID_NIC, VALID_SSD]), hcl)
    assert codes_of(result) == ["cpu_too_old"] and result.verdict == "FAIL"
    result = validate_bom(make_bom([comp("cpu", "AMD EPYC 9554"), VALID_NIC, VALID_SSD]), hcl)
    assert codes_of(result) == ["cpu_unknown"] and result.verdict == "INCONCLUSIVE"
    # HCL-listed non-Xeon family is fine
    result = validate_bom(make_bom([comp("cpu", "AMD EPYC 9354 32C"), VALID_NIC, VALID_SSD]), hcl)
    assert codes_of(result) == [] and result.verdict == "PASS"


def test_storage_topology_rules():
    hcl = small_hcl()
    result = validate_bom(make_bom([VALID_CPU, VALID_NIC]), hcl)
    assert codes_of(result) == ["diskless"] and result.verdict == "FAIL"

    one_ssd = comp("storage", "Samsung PM9A3 NVMe SSD 1.92TB 1DWPD")
    result = validate_bom(make_bom([VALID_CPU, VALID_NIC, one_ssd]), hcl)
    assert codes_of(result) == ["single_flash_drive"] and result.verdict == "PASS"

    result = validate_bom(make_bom([VALID_CPU, VALID_NIC, comp("controller", "HBA355i Front", "405-AAXX"),
                                    comp("storage", "8TB 7.2K SAS HDD")]), hcl)
    assert codes_of(result) == ["controller_hba_default", "single_hdd"]

    result = validate_bom(make_bom([VALID_CPU, VALID_NIC, comp("storage", "3.84TB NVMe SSD 1DWPD", qty=2)]), hcl)
    assert codes_of(result) == ["two_drives"]

    # 5 flash + 3 HDD = 62.5% → Math.round half-up → 63
    result = validate_bom(make_bom([VALID_CPU, VALID_NIC, comp("controller", "HBA355i Front", "405-AAXX"),
                                    comp("storage", "3.84TB NVMe SSD 1DWPD", qty=5),
                                    comp("storage", "8TB 7.2K SATA HDD", qty=3)]), hcl)
    ratio = [f for f in findings_of(result) if f.code == "high_flash_ratio"][0]
    assert ratio.issue == "High flash ratio (63% flash) — storage is lopsided"
    assert "sata_hdd" in codes_of(result)

    result = validate_bom(make_bom([VALID_CPU, VALID_NIC, comp("storage", "1.92TB NVMe SAS SSD", qty=3)]), hcl)
    assert codes_of(result) == ["storage_protocol_contradiction"] and result.verdict == "INCONCLUSIVE"


def test_dwpd_and_sed_findings_text():
    hcl = small_hcl()
    # 2U (default 0.3): "<0.3" is at the ceiling → flagged; text prints JS-style numbers.
    result = validate_bom(make_bom([VALID_CPU, VALID_NIC, comp("storage", "1.92TB NVMe <0.3DWPD SSD", qty=3)],
                                   server_model="PowerEdge R760"), hcl)
    f = findings_of(result)[0]
    assert f.code == "dwpd_low"
    assert f.issue == "Drive endurance (<0.3 DWPD) is below the recommended minimum of 0.3 DWPD"
    assert f.remediation.startswith("Replace with a drive rated at 0.3 DWPD or higher.")
    # explicit 0.3 on 2U is fine; 0.2 on a 1U is fine; 0.2 on 2U is flagged
    assert codes_of(validate_bom(make_bom([VALID_CPU, VALID_NIC, comp("storage", "NVMe 0.3 DWPD SSD", qty=3)],
                                          server_model="R760"), hcl)) == []
    assert codes_of(validate_bom(make_bom([VALID_CPU, VALID_NIC, comp("storage", "NVMe 0.2 DWPD SSD", qty=3)],
                                          server_model="SR630 V3 1U"), hcl)) == []
    result = validate_bom(make_bom([VALID_CPU, VALID_NIC, comp("storage", "NVMe 0.2 DWPD SSD", qty=3)],
                                   server_model="R760"), hcl)
    assert findings_of(result)[0].issue == "Drive endurance (0.2 DWPD) is below the recommended minimum of 0.3 DWPD"
    # "<2 DWPD" prints as 2, not 2.0, and is not flagged (2 > 0.3)
    assert parse_dwpd("<2DWPD")["value"] == 2.0
    assert codes_of(validate_bom(make_bom([VALID_CPU, VALID_NIC, comp("storage", "NVMe <2DWPD SSD", qty=3)]), hcl)) == []
    # SED is info only; HDDs are never DWPD-checked
    result = validate_bom(make_bom([VALID_CPU, VALID_NIC, comp("storage", "1.92TB NVMe SED SSD", qty=3)]), hcl)
    assert codes_of(result) == ["sed"] and result.verdict == "PASS"


def test_form_factor_override_changes_threshold():
    hcl = small_hcl()
    bom = make_bom([VALID_CPU, VALID_NIC, comp("storage", "NVMe 0.2 DWPD SSD", qty=3)], server_model="R760")
    assert codes_of(validate_bom(bom, hcl)) == ["dwpd_low"]
    assert codes_of(validate_bom(bom, hcl, form_factor="1U")) == []
    assert codes_of(validate_bom(bom, hcl, form_factor="DT")) == []
    assert codes_of(validate_bom(bom, hcl, form_factor="2U")) == ["dwpd_low"]
    cfg = bom.configs[0]
    assert validate_config(cfg, "Dell", hcl, form_factor="1U").findings == []


def test_memory_rank_warning():
    result = validate_bom(make_bom([VALID_CPU, VALID_NIC, VALID_SSD,
                                    comp("memory", "16GB TruDDR5 5600MHz (1Rx8) RDIMM", qty=8)]), small_hcl())
    assert "memory_1rx8" in codes_of(result) and result.verdict == "PASS"
    result = validate_bom(make_bom([VALID_CPU, VALID_NIC, VALID_SSD,
                                    comp("memory", "32GB TruDDR5 (2Rx8) RDIMM", qty=8)]), small_hcl())
    assert "memory_1rx8" not in codes_of(result)


def test_nic_rules():
    hcl = small_hcl()
    result = validate_bom(make_bom([VALID_CPU, VALID_SSD]), hcl)
    assert "nic_missing" in codes_of(result)
    # LOM by description short-circuits the HCL lookup
    result = validate_bom(make_bom([VALID_CPU, VALID_SSD, comp("nic", "Broadcom 5720 On-Board LOM", "nope")]), hcl)
    assert "nic_lom" in codes_of(result) and "nic_not_in_hcl" not in codes_of(result)
    # LOM by HCL form factor
    result = validate_bom(make_bom([VALID_CPU, VALID_SSD, comp("nic", "Quad 1GbE", "540-LOM")]), hcl)
    assert "nic_lom" in codes_of(result)
    result = validate_bom(make_bom([VALID_CPU, VALID_SSD, comp("nic", "Realtek 8111")]), hcl)
    assert "nic_not_in_hcl" in codes_of(result) and result.verdict == "FAIL"
    # Two discrete families → info listing them in BOM order, uppercased
    result = validate_bom(make_bom([VALID_CPU, VALID_SSD, comp("nic", "Intel E810-XXV 25GbE", "540-BCXW"), VALID_NIC]), hcl)
    fam = [f for f in findings_of(result) if f.code == "nic_multiple_families"][0]
    assert "This BOM contains E810, X710." in fam.remediation
    assert result.verdict == "PASS"
    # Same family twice → no finding
    result = validate_bom(make_bom([VALID_CPU, VALID_SSD, VALID_NIC, VALID_NIC]), hcl)
    assert "nic_multiple_families" not in codes_of(result)


def test_gpu_rules():
    hcl = small_hcl()
    base = [VALID_CPU, VALID_NIC, VALID_SSD]
    assert "gpu_eol" in codes_of(validate_bom(make_bom(base + [comp("gpu", "NVIDIA T4 16GB")]), hcl))
    result = validate_bom(make_bom(base + [comp("gpu", "NVIDIA A2 16GB")]), hcl)
    assert "gpu_unsupported" in codes_of(result) and result.verdict == "FAIL"
    result = validate_bom(make_bom(base + [comp("gpu", "NVIDIA H100 80GB")]), hcl)
    assert "gpu_not_in_hcl" in codes_of(result) and result.verdict == "PASS"
    assert not [c for c in codes_of(validate_bom(make_bom(base + [comp("gpu", "NVIDIA L4 24GB")]), hcl)) if c.startswith("gpu")]
    assert not [c for c in codes_of(validate_bom(make_bom(base + [comp("gpu", "No GPU")]), hcl)) if c.startswith("gpu")]


def test_blocked_vendor():
    bom = make_bom([VALID_CPU, VALID_NIC, VALID_SSD], vendor="HPE")
    result = validate_bom(bom, EMPTY_HCL)
    assert result.verdict == "FAIL"
    f = findings_of(result)[-1]
    assert f.code == "vendor_blocked"
    assert f.issue == "HPE hardware is not approved at this time"
    assert f.remediation == "We are not currently certifying HPE hardware. Please use a supported vendor."
    assert "vendor_blocked" not in codes_of(validate_bom(make_bom([VALID_CPU, VALID_NIC, VALID_SSD]), EMPTY_HCL))
    # The snapshot shape ("hpe") and an env-style string both work.
    assert HclData.from_dict({"blockedVendors": "HPE, Cisco"}).blocked_vendors == ["hpe", "cisco"]
    assert HclData.from_dict({"blocked_vendors": ["Dell"]}).blocked_vendors == ["dell"]


def test_no_hardware_configs_is_inconclusive():
    bom = NormalizedBOM(vendor="Dell", configs=[
        BOMConfig(name="Services", server_model=None, components=[comp("other", "ProSupport 3yr", qty=3)]),
    ])
    assert not is_hardware_config(bom.configs[0])
    result = validate_bom(bom, EMPTY_HCL)
    assert result.verdict == "INCONCLUSIVE" and result.config_results == []
    assert validate_bom(NormalizedBOM(vendor="Dell"), EMPTY_HCL).verdict == "INCONCLUSIVE"


def test_determine_verdict_precedence():
    assert determine_verdict([]) == "PASS"
    assert determine_verdict([Finding("warning", "x", "CPU generation could not be determined", "r")]) == "INCONCLUSIVE"
    assert determine_verdict([Finding("warning", "x", "CPU generation could not be determined", "r"),
                              Finding("error", "x", "anything", "r")]) == "FAIL"
    assert determine_verdict([Finding("warning", "x", "2-drive configuration detected", "r")]) == "PASS"


def test_every_finding_has_a_code():
    hcl = small_hcl()
    bom = make_bom([
        comp("boss", "BOSS-N1"), comp("controller", "Mystery", "999"), comp("controller", "HBA330 Adapter", "405-OLD"),
        comp("cpu", "AMD EPYC 9554"), comp("storage", "NVMe SAS <0.3DWPD SED SSD", qty=3),
        comp("storage", "8TB SATA HDD", qty=2), comp("memory", "16GB 1Rx8"), comp("nic", "Realtek 8111"),
        comp("nic", "Integrated NIC 1GbE"), comp("gpu", "NVIDIA A2"),
    ], vendor="HPE")
    result = validate_bom(bom, hcl)
    assert result.verdict == "FAIL"
    assert all(f.code for f in findings_of(result))
    assert all(f.to_dict()["code"] == f.code for f in findings_of(result))


# ─── unit: lenient from_dict ─────────────────────────────────────────────────


def test_from_dict_is_lenient():
    bom = NormalizedBOM.from_dict({
        "vendor": "lenovo",
        "raw_text": "x",
        "configs": [{
            "name": "c1",
            "server_model": "SR650 V3",
            "components": [
                {"description": "CPU", "quantity": "2", "category": "CPU"},
                {"part_number": 12345, "description": "thing", "quantity": 3.0, "category": "widget"},
                {"partNumber": None, "description": "n/a", "quantity": None, "category": None},
                {"description": "junk qty", "quantity": "lots", "category": "nic"},
            ],
        }],
    })
    assert bom.vendor == "Lenovo" and bom.raw_text == "x"
    cfg = bom.configs[0]
    assert cfg.server_model == "SR650 V3"
    c0, c1, c2, c3 = cfg.components
    assert (c0.part_number, c0.quantity, c0.category) == (None, 2, "cpu")
    assert (c1.part_number, c1.quantity, c1.category) == ("12345", 3, "other")
    assert (c2.quantity, c2.category) == (1, "other")
    assert (c3.quantity, c3.category) == (1, "nic")
    assert NormalizedBOM.from_dict({"vendor": "Cisco"}).vendor == "Unknown"
    assert NormalizedBOM.from_dict({}).to_dict() == {"vendor": "Unknown", "configs": [], "rawText": ""}


def test_validation_result_round_trip_tolerates_missing_code():
    raw = {"verdict": "PASS", "configResults": [{"configName": "c", "verdict": "PASS", "findings": [
        {"severity": "info", "component": "x", "issue": "i", "remediation": "r"}]}]}
    vr = ValidationResult.from_dict(raw)
    assert vr.config_results[0].findings[0].code is None
    out = vr.to_dict()
    assert out["configResults"][0]["findings"][0]["code"] is None
    # snake_case input is accepted too
    assert ValidationResult.from_dict({"verdict": "FAIL", "config_results": [
        {"config_name": "z", "verdict": "FAIL", "findings": []}]}).config_results[0].config_name == "z"
