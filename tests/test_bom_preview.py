"""Pre-publication accepts (bom/preview.py) end to end.

The real case this mirrors: a BOM arrives on a ThinkCentre M70q Tiny Gen 6
(HE155) quoting a NIC and a 'Core Ultra' CPU the HCL team has verified but
the public HCL does not list yet. The super admin accepts the parts so the
very next opportunity passes; a later scrape must neither delist them nor
duplicate them, and the HCL team pulls the accepted set over a tokened feed
with no user account.

Run: .venv/bin/python -m pytest tests/test_bom_preview.py -q
"""
import copy
import io
import json
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.pop("SESSION_COOKIE_SECURE", None)
os.environ.pop("ANTHROPIC_API_KEY", None)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402
import app as appmod  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from auth_models import AdminAuditLog, ROLE_SUPER_ADMIN, User  # noqa: E402
import hcl_models as hm
from bom import preview  # noqa: E402
from bom import hcl_scrape as hs  # noqa: E402
from bom import hcl_sync as sync  # noqa: E402
from bom.hcl_scrape import component_attrs  # noqa: E402
from bom.normalize import BOMComponent, BOMConfig, NormalizedBOM  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "hcl")

PASSWORD = "Abcdef1!xy"
PARTNER = "pm@partnerco.example"
ADMIN = "admin@scalecomputing.com"

NIC_PART = "MCX623106AN"
NIC_DESC = "Mellanox ConnectX-6 Dx 25GbE SFP28 2-Port OCP Ethernet Adapter"
NIC_KEY = "nic/" + NIC_PART
CPU_DESC = "Intel Core Ultra 7 265T Processor"
CPU_KEY = "cpu/no-part:core-ultra-7-265t"
FEED_TOKEN = "hcl-team-pull-token-1"


# ── fixture snapshot, cut down to the HE155 platform ─────────────────────────

def _read(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return fh.read()


def fixture_fetch():
    manifest = json.loads(_read("manifest.json"))
    by_url = {p["url"]: p["file"] for p in manifest["pages"]}

    def fetch(path):
        if path not in by_url:
            raise hs.ScrapeError("GET %s failed: HTTP 404" % path)
        return _read(by_url[path])
    return fetch


@pytest.fixture(scope="module")
def mini_snapshot():
    """The real scraped snapshot reduced to the one platform the story needs
    (keeps per-test seeding fast; the full snapshot is test_hcl_sync's job).
    Still 'complete': the delist/flip rules only run on complete snapshots."""
    full = hs.scrape_all(fetch=fixture_fetch(), sleep=lambda s: None)
    assert full["complete"] is True
    he155 = [copy.deepcopy(p) for p in full["platforms"]
             if p["brand"] == "lenovo" and p["sc_model"] == "HE155"]
    assert len(he155) == 1
    return {"platforms": he155, "devices": [], "complete": True,
            "pages_total": 2, "pages_done": 2, "errors": [],
            "scraped_at": full.get("scraped_at")}


@pytest.fixture()
def app(mini_snapshot):
    application = appmod.app
    application.config["TESTING"] = True
    application.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    with application.app_context():
        db.drop_all()
        db.create_all()
        run = sync.build_run(copy.deepcopy(mini_snapshot), user=None, source="import")
        assert run.status == hm.RUN_SUCCEEDED
        sync.bulk(None, "approve", None, all_pending=True)
        assert hm.HclPlatform.query.filter_by(sc_model="HE155").count() == 1
    return application


def client_for(app, email):
    c = app.test_client()
    c.post("/api/auth/signup", json={"email": email, "password": PASSWORD, "accept_privacy": True})
    c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    return c


def promote(app, email):
    with app.app_context():
        u = User.query.filter_by(email=email).first()
        u.role = ROLE_SUPER_ADMIN
        db.session.commit()


def admin_for(app):
    c = client_for(app, ADMIN)
    promote(app, ADMIN)
    return c


def make_project(c, name="Edge"):
    r = c.post("/api/projects/", json={"name": name})
    assert r.status_code == 201, r.get_data(as_text=True)
    return r.get_json()


def _bom():
    def comp(cat, desc, part, qty):
        return BOMComponent(part_number=part, description=desc, quantity=qty, category=cat)
    return NormalizedBOM(vendor="Lenovo", configs=[BOMConfig(
        name="Edge PROD", server_model="ThinkCentre M70q Tiny Gen 6", node_count=3,
        components=[
            comp("chassis", "ThinkCentre M70q Tiny Chassis", "BLK1", 3),
            comp("cpu", CPU_DESC, None, 3),
            comp("memory", "ThinkCentre 32GB DDR5 5600MHz SODIMM", "MEM1", 6),
            comp("storage", 'ThinkCentre 2.5" 3.84TB Read Intensive NVMe PCIe 4.0 SSD',
                 "SSD1", 6),
            comp("nic", NIC_DESC, NIC_PART, 3),
        ])])


def upload_check(c, project_id):
    from bom.parsers.template import build_template_bytes
    data = {"file": (io.BytesIO(build_template_bytes(bom=_bom())), "edge-bom.xlsx")}
    r = c.post(f"/api/projects/{project_id}/bom-checks", data=data,
               content_type="multipart/form-data")
    assert r.status_code == 201, r.get_data(as_text=True)
    return r.get_json()


def make_failed_check(app):
    """Partner uploads the BOM with the two unknown parts; returns
    (partner_client, admin_client, check dict)."""
    partner = client_for(app, PARTNER)
    project = make_project(partner)
    check = upload_check(partner, project["id"])
    admin = admin_for(app)
    return partner, admin, check


def accept(admin, check_id, keys=(NIC_KEY, CPU_KEY), note=None):
    body = {"keys": list(keys)}
    if note is not None:
        body["note"] = note
    return admin.post(f"/admin/api/bom-reviews/{check_id}/accept-parts", json=body)


# ── the failing upload ───────────────────────────────────────────────────────

def test_unknown_parts_fail_against_the_real_platform(app):
    partner, admin, check = make_failed_check(app)
    assert check["technical_verdict"] == "FAIL"
    assert set(check["flag_reasons"]) >= {"nic_not_in_hcl", "cpu_unknown"}
    assert check["review_status"] == "open"
    cr = check["result"]["technical"]["config_results"][0]
    assert cr["platform"]["sc_models"] == ["HE155"]
    assert cr["platform"]["server"] == "ThinkCentre M70q Tiny Gen6"


# ── acceptable candidates ────────────────────────────────────────────────────

def test_acceptable_lists_the_nic_and_cpu_with_the_platform(app):
    partner, admin, check = make_failed_check(app)
    r = admin.get(f"/admin/api/bom-reviews/{check['id']}/acceptable")
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    by_key = {c["key"]: c for c in d["candidates"]}
    assert set(by_key) == {NIC_KEY, CPU_KEY}
    nic = by_key[NIC_KEY]
    assert nic["kind"] == "nic" and nic["part_number"] == NIC_PART
    assert nic["description"] == NIC_DESC
    assert nic["from_code"] == "nic_not_in_hcl"
    assert nic["already_in_catalog"] is False
    assert nic["attrs"].get("speed_gbe") == 25
    cpu = by_key[CPU_KEY]
    assert cpu["kind"] == "cpu" and cpu["part_number"].startswith("no-part:")
    assert cpu["from_code"] == "cpu_unknown"
    assert [p["sc_model"] for p in d["platforms"]] == ["HE155"]
    assert d["platforms"][0]["brand"] == "lenovo"

    assert admin.get("/admin/api/bom-reviews/99999/acceptable").status_code == 404


# ── accepting ────────────────────────────────────────────────────────────────

def test_accept_creates_preview_components_linked_to_the_platform(app):
    partner, admin, check = make_failed_check(app)
    stamp_before = check["catalog_stamp"]
    r = accept(admin, check["id"], note="confirmed with HCL team")
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert sorted(d["created"]) == sorted([NIC_KEY, CPU_KEY])
    assert d["relisted"] == [] and d["skipped"] == []
    assert sorted(d["linked"]) == sorted(
        ["lenovo/HE155 -> %s" % NIC_KEY, "lenovo/HE155 -> %s" % CPU_KEY])

    with app.app_context():
        run = db.session.get(hm.HclScrapeRun, d["run_id"])
        assert run.source == "preview" and run.status == hm.RUN_SUCCEEDED
        assert run.complete is False
        assert run.summary["check_id"] == check["id"]
        rows = hm.HclPendingChange.query.filter_by(run_id=run.id).all()
        assert rows and all(r.status == hm.APPROVED for r in rows)
        note = "pre-publication accept from BOM check #%s" % check["id"]
        assert all(note in (r.note or "") for r in rows)
        # 2 component adds + 2 link additions, every mutation a queue row
        kinds = sorted((r.entity_type, r.change_kind, r.field or "") for r in rows)
        assert kinds == [("component", "add", ""), ("component", "add", ""),
                         ("platform", "update", "link"), ("platform", "update", "link")]
        nic = hm.HclComponent.query.filter_by(kind="nic", part_number=NIC_PART).one()
        assert nic.status == hm.STATUS_ACTIVE and nic.origin == hm.ORIGIN_PREVIEW
        assert nic.tce is False
        cpu = hm.HclComponent.query.filter_by(kind="cpu").one()
        assert cpu.origin == hm.ORIGIN_PREVIEW
        platform = hm.HclPlatform.query.filter_by(brand="lenovo", sc_model="HE155").one()
        links = {l.component.key: l for l in platform.links}
        assert set(links) == {NIC_KEY, CPU_KEY}
        assert all(l.status == hm.STATUS_ACTIVE and l.origin == hm.ORIGIN_PREVIEW
                   for l in links.values())
        assert sync.catalog_stamp() != stamp_before
        assert "hcl_preview_accept" in [a.action for a in AdminAuditLog.query.all()]


def test_accepting_twice_skips_and_duplicates_nothing(app):
    partner, admin, check = make_failed_check(app)
    assert accept(admin, check["id"]).status_code == 200
    r = accept(admin, check["id"])
    assert r.status_code == 200
    d = r.get_json()
    assert sorted(d["skipped"]) == sorted([NIC_KEY, CPU_KEY])
    assert d["created"] == [] and d["linked"] == [] and d["relisted"] == []
    with app.app_context():
        assert hm.HclComponent.query.filter_by(kind="nic", part_number=NIC_PART).count() == 1
        adds = hm.HclPendingChange.query.filter_by(
            entity_type="component", entity_key=NIC_KEY, change_kind="add").count()
        assert adds == 1
        platform = hm.HclPlatform.query.filter_by(sc_model="HE155").one()
        assert len(platform.links) == 2


def test_accept_validates_keys_and_check(app):
    partner, admin, check = make_failed_check(app)
    assert accept(admin, 99999).status_code == 404
    assert admin.post(f"/admin/api/bom-reviews/{check['id']}/accept-parts",
                      json={"keys": []}).status_code == 400
    assert admin.post(f"/admin/api/bom-reviews/{check['id']}/accept-parts",
                      json={}).status_code == 400
    r = accept(admin, check["id"], keys=["nic/NOT-A-CANDIDATE"])
    assert r.status_code == 400
    assert "unknown candidate" in r.get_json()["error"]
    with app.app_context():  # nothing was created by the failed attempts
        assert hm.HclComponent.query.filter_by(part_number=NIC_PART).count() == 0


# ── recheck after accepting ──────────────────────────────────────────────────

def test_recheck_passes_once_the_parts_are_accepted(app):
    partner, admin, check = make_failed_check(app)
    assert accept(admin, check["id"]).status_code == 200
    r = partner.post(f"/api/bom-checks/{check['id']}/recheck", json={})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d["technical_verdict"] == "PASS"
    assert d["flag_reasons"] == []
    assert d["review_status"] == "none"
    codes = [f["code"] for f in d["result"]["technical"]["config_results"][0]["findings"]]
    assert "nic_not_in_hcl" not in codes and "cpu_unknown" not in codes
    # the catalog moved under the check and the re-check shows the new stamp
    assert d["catalog_stamp"] != check["catalog_stamp"]


# ── scrape interplay ─────────────────────────────────────────────────────────

def test_scrape_without_the_parts_never_delists_them(app, mini_snapshot):
    partner, admin, check = make_failed_check(app)
    assert accept(admin, check["id"]).status_code == 200
    with app.app_context():
        run = sync.build_run(copy.deepcopy(mini_snapshot), user=None, source="import")
        assert run.summary["changes"] == {"add": 0, "update": 0, "delist": 0, "relist": 0, "merge": 0}
        assert hm.HclPendingChange.query.filter_by(status=hm.PENDING).count() == 0
        nic = hm.HclComponent.query.filter_by(kind="nic", part_number=NIC_PART).one()
        assert nic.status == hm.STATUS_ACTIVE and nic.origin == hm.ORIGIN_PREVIEW
        platform = hm.HclPlatform.query.filter_by(sc_model="HE155").one()
        assert all(l.status == hm.STATUS_ACTIVE for l in platform.links)


def test_publication_flips_origin_silently(app, mini_snapshot):
    partner, admin, check = make_failed_check(app)
    assert accept(admin, check["id"]).status_code == 200
    published = copy.deepcopy(mini_snapshot)
    published["platforms"][0].setdefault("components", []).append({
        "kind": "nic", "part_number": NIC_PART, "description": NIC_DESC,
        "tce": False, "attrs": component_attrs("nic", NIC_DESC),
    })
    with app.app_context():
        run = sync.build_run(published, user=None, source="import")
        # published = ordinary: no add, no update, no delist queued
        assert run.summary["changes"] == {"add": 0, "update": 0, "delist": 0, "relist": 0, "merge": 0}
        assert hm.HclPendingChange.query.filter_by(status=hm.PENDING).count() == 0
        assert hm.HclComponent.query.filter_by(kind="nic", part_number=NIC_PART).count() == 1
        nic = hm.HclComponent.query.filter_by(kind="nic", part_number=NIC_PART).one()
        assert nic.origin == hm.ORIGIN_SCRAPE
        platform = hm.HclPlatform.query.filter_by(sc_model="HE155").one()
        links = {l.component.key: l for l in platform.links}
        assert len(links) == 2  # no duplicate link row
        assert links[NIC_KEY].origin == hm.ORIGIN_SCRAPE
        # the CPU is still unpublished: stays preview, stays immune
        assert links[CPU_KEY].origin == hm.ORIGIN_PREVIEW
        cpu = hm.HclComponent.query.filter_by(kind="cpu").one()
        assert cpu.origin == hm.ORIGIN_PREVIEW and cpu.status == hm.STATUS_ACTIVE


# ── admin preview list + pull feed ───────────────────────────────────────────

def test_admin_preview_list_shows_accepted_parts(app):
    partner, admin, check = make_failed_check(app)
    assert admin.get("/admin/api/hcl/preview").get_json() == []
    assert accept(admin, check["id"]).status_code == 200
    rows = admin.get("/admin/api/hcl/preview").get_json()
    assert sorted(r["key"] for r in rows) == sorted([NIC_KEY, CPU_KEY])
    nic = next(r for r in rows if r["key"] == NIC_KEY)
    assert nic["origin"] == "preview"
    assert nic["platforms"][0]["sc_model"] == "HE155"
    assert nic["platforms"][0]["link_origin"] == "preview"


def test_pull_feed_is_tokened_and_needs_no_login(app):
    partner, admin, check = make_failed_check(app)
    assert accept(admin, check["id"]).status_code == 200
    anon = app.test_client()  # never signs up, never logs in

    # feature off: the endpoint does not reveal itself
    assert anon.get("/api/hcl/preview-feed").status_code == 404
    assert anon.get("/api/hcl/preview-feed",
                    headers={"Authorization": "Bearer " + FEED_TOKEN}).status_code == 404

    # token too short / not a string are refused; then a real one is set
    assert admin.put("/admin/api/hcl/settings",
                     json={"preview_feed_token": "short"}).status_code == 400
    assert admin.put("/admin/api/hcl/settings",
                     json={"preview_feed_token": 42}).status_code == 400
    r = admin.put("/admin/api/hcl/settings", json={"preview_feed_token": " %s " % FEED_TOKEN})
    assert r.status_code == 200 and r.get_json()["preview_feed_token"] == FEED_TOKEN
    assert admin.get("/admin/api/hcl/settings").get_json()["preview_feed_token"] == FEED_TOKEN

    assert anon.get("/api/hcl/preview-feed").status_code == 401
    assert anon.get("/api/hcl/preview-feed",
                    headers={"Authorization": "Bearer wrong-token-here!"}).status_code == 401

    r = anon.get("/api/hcl/preview-feed", headers={"Authorization": "Bearer " + FEED_TOKEN})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d["generated_at"]
    parts = {c["part_number"]: c for c in d["components"]}
    assert NIC_PART in parts
    nic = parts[NIC_PART]
    assert nic["kind"] == "nic" and nic["description"] == NIC_DESC
    assert nic["accepted_at"]
    assert nic["platforms"] == [{"brand": "lenovo", "sc_model": "HE155",
                                 "server": "ThinkCentre M70q Tiny Gen6"}]

    # curl-friendly query-string token works too
    assert anon.get("/api/hcl/preview-feed?token=" + FEED_TOKEN).status_code == 200

    # clearing the token turns the feed off again
    assert admin.put("/admin/api/hcl/settings",
                     json={"preview_feed_token": ""}).status_code == 200
    assert anon.get("/api/hcl/preview-feed",
                    headers={"Authorization": "Bearer " + FEED_TOKEN}).status_code == 404


# ── access control ───────────────────────────────────────────────────────────

def test_partner_cannot_touch_the_accept_surface(app):
    partner, admin, check = make_failed_check(app)
    assert partner.get(f"/admin/api/bom-reviews/{check['id']}/acceptable").status_code == 403
    assert partner.post(f"/admin/api/bom-reviews/{check['id']}/accept-parts",
                        json={"keys": [NIC_KEY]}).status_code == 403
    assert partner.get("/admin/api/hcl/preview").status_code == 403
    assert partner.put("/admin/api/hcl/settings",
                       json={"preview_feed_token": FEED_TOKEN}).status_code == 403


# ── pre-publication platform creation + description scoping ──────────────────
# The motivating case: a BOM known to be a Tiny the HCL does not list yet.
# Without a platform, accepted parts land unlinked — and a generic line like
# 'Integrated Graphics' would then validate on EVERY platform's BOMs. So the
# admin creates the platform during acceptance, and description-based matches
# of preview parts stay scoped to the platforms they were linked to.

GPU_DESC = "Integrated Graphics"
NEW_SERVER = "ThinkCentre M75q Gen 5"
NEW_PLATFORM = {"brand": "lenovo", "sc_model": "HE160", "server": NEW_SERVER}


def _tiny_bom(server=NEW_SERVER):
    """A BOM on a server the catalog does NOT list: no platform identified.
    Everything but the GPU line is clean (scalable CPU, NVMe-only storage)."""
    def comp(cat, desc, part, qty):
        return BOMComponent(part_number=part, description=desc, quantity=qty, category=cat)
    return NormalizedBOM(vendor="Lenovo", configs=[BOMConfig(
        name="Tiny PROD", server_model=server, node_count=3,
        components=[
            comp("chassis", "ThinkCentre M75q Chassis", "BLK9", 3),
            comp("cpu", "Intel Xeon Gold 6526Y Processor", "CPU9", 3),
            comp("memory", "ThinkCentre 32GB DDR5 5600MHz SODIMM", "MEM9", 6),
            comp("storage", 'ThinkCentre 2.5" 1.92TB NVMe PCIe 4.0 SSD', "SSD9", 6),
            comp("gpu", GPU_DESC, None, 3),
        ])])


def upload_bom(c, project_id, bom, filename="tiny-bom.xlsx"):
    from bom.parsers.template import build_template_bytes
    data = {"file": (io.BytesIO(build_template_bytes(bom=bom)), filename)}
    r = c.post(f"/api/projects/{project_id}/bom-checks", data=data,
               content_type="multipart/form-data")
    assert r.status_code == 201, r.get_data(as_text=True)
    return r.get_json()


def make_unidentified_check(app):
    partner = client_for(app, PARTNER)
    project = make_project(partner, name="Tiny")
    check = upload_bom(partner, project["id"], _tiny_bom())
    admin = admin_for(app)
    return partner, admin, check


def gpu_candidate_key(admin, check_id):
    d = admin.get(f"/admin/api/bom-reviews/{check_id}/acceptable").get_json()
    return next(c["key"] for c in d["candidates"] if c["kind"] == "gpu")


def accept_platform(admin, check_id, keys, platform):
    body = {"keys": list(keys)}
    if platform is not None:
        body["platform"] = platform
    return admin.post(f"/admin/api/bom-reviews/{check_id}/accept-parts", json=body)


def test_accept_with_platform_spec_creates_and_links_the_platform(app):
    partner, admin, check = make_unidentified_check(app)
    assert "gpu_not_in_hcl" in check["flag_reasons"]

    r = admin.get(f"/admin/api/bom-reviews/{check['id']}/acceptable")
    d = r.get_json()
    assert d["platforms"] == []
    # sc_model is suggested from the server line: these are software-only
    # (validated) builds, so the platform is named after the manufacturer
    # model, never an SC appliance number.
    assert d["platform_suggestion"] == {
        "brand": "lenovo",
        "sc_model": preview._model_name_from_server(NEW_SERVER),
        "server": NEW_SERVER, "form_factor": None}
    gpu_key = next(c["key"] for c in d["candidates"] if c["kind"] == "gpu")

    r = accept_platform(admin, check["id"], [gpu_key], dict(NEW_PLATFORM))
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d["platform_created"] == "lenovo/HE160"
    assert d["created"] == [gpu_key]
    assert d["linked"] == ["lenovo/HE160 -> %s" % gpu_key]
    assert "unlinked" not in d

    with app.app_context():
        plat = hm.HclPlatform.query.filter_by(brand="lenovo", sc_model="HE160").one()
        assert plat.status == hm.STATUS_ACTIVE and plat.origin == hm.ORIGIN_PREVIEW
        assert plat.server == NEW_SERVER
        # created via an approved pending 'add' row on the same preview run,
        # payload origin preview and no components list
        row = hm.HclPendingChange.query.filter_by(
            entity_type="platform", entity_key="lenovo/HE160", change_kind="add").one()
        assert row.status == hm.APPROVED and row.run_id == d["run_id"]
        assert row.payload["origin"] == "preview"
        assert "components" not in row.payload
        assert len(plat.links) == 1
        link = plat.links[0]
        assert link.component.key == gpu_key and link.origin == hm.ORIGIN_PREVIEW
        # the pull feed lists the platform
        from bom import preview as pv
        feed = pv.preview_feed()
        assert len(feed["platforms"]) == 1
        fp = feed["platforms"][0]
        assert fp["brand"] == "lenovo" and fp["sc_model"] == "HE160"
        assert fp["server"] == NEW_SERVER and fp["form_factor"] is None
        assert fp["accepted_at"]

    # a second accept reuses the ACTIVE preview platform: no duplicate rows
    r = accept_platform(admin, check["id"], [gpu_key], dict(NEW_PLATFORM))
    assert r.status_code == 200
    d2 = r.get_json()
    assert d2["skipped"] == [gpu_key] and d2["platform_created"] == "lenovo/HE160"
    with app.app_context():
        assert hm.HclPlatform.query.filter_by(sc_model="HE160").count() == 1
        assert hm.HclPendingChange.query.filter_by(
            entity_type="platform", entity_key="lenovo/HE160",
            change_kind="add").count() == 1

    # re-check: the created platform is identified by its server string and
    # the linked GPU now validates — the check PASSes
    r = partner.post(f"/api/bom-checks/{check['id']}/recheck", json={})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d["technical_verdict"] == "PASS"
    assert d["flag_reasons"] == []
    cr = d["result"]["technical"]["config_results"][0]
    assert cr["platform"]["sc_models"] == ["HE160"]
    assert "gpu_not_in_hcl" not in [f["code"] for f in cr["findings"]]


def test_linked_generic_description_stays_scoped_to_its_platform(app):
    """'Integrated Graphics' accepted for lenovo/HE160 must not validate on a
    BOM identified as another platform, nor on a platform-less BOM."""
    from bom.check import run_check
    partner, admin, check = make_unidentified_check(app)
    gpu_key = gpu_candidate_key(admin, check["id"])
    assert accept_platform(admin, check["id"], [gpu_key],
                           dict(NEW_PLATFORM)).status_code == 200

    with app.app_context():
        # same line on a BOM identified as HE155: still gpu_not_in_hcl
        bom = _bom()
        bom.configs[0].components.append(BOMComponent(
            part_number=None, description=GPU_DESC, quantity=1, category="gpu"))
        result = run_check(bom)
        cr = result["technical"]["config_results"][0]
        assert cr["platform"]["sc_models"] == ["HE155"]
        assert "gpu_not_in_hcl" in [f["code"] for f in cr["findings"]]

        # same line on a BOM with NO identified platform: the part is LINKED,
        # so it does not match there either
        result = run_check(_tiny_bom(server="ThinkCentre M75q Gen 9"))
        cr = result["technical"]["config_results"][0]
        assert cr["platform"] is None
        assert "gpu_not_in_hcl" in [f["code"] for f in cr["findings"]]


def test_unlinked_preview_part_matches_only_platformless_boms(app):
    """Accepted WITHOUT a platform (empty scope): the description matches a
    BOM whose platform is unidentified, and nothing else."""
    from bom.check import run_check
    partner, admin, check = make_unidentified_check(app)
    gpu_key = gpu_candidate_key(admin, check["id"])
    r = accept_platform(admin, check["id"], [gpu_key], None)
    assert r.status_code == 200
    d = r.get_json()
    assert d["unlinked"] is True and d["platform_created"] is None

    with app.app_context():
        comp = hm.HclComponent.query.filter_by(kind="gpu").one()
        assert comp.origin == hm.ORIGIN_PREVIEW and not comp.links

        # platform-less BOM: the unlinked part matches
        result = run_check(_tiny_bom(server="ThinkCentre M75q Gen 9"))
        cr = result["technical"]["config_results"][0]
        assert cr["platform"] is None
        assert "gpu_not_in_hcl" not in [f["code"] for f in cr["findings"]]

        # identified BOM (HE155): the unlinked part does NOT match
        bom = _bom()
        bom.configs[0].components.append(BOMComponent(
            part_number=None, description=GPU_DESC, quantity=1, category="gpu"))
        result = run_check(bom)
        cr = result["technical"]["config_results"][0]
        assert cr["platform"]["sc_models"] == ["HE155"]
        assert "gpu_not_in_hcl" in [f["code"] for f in cr["findings"]]


def test_preview_platform_is_delist_immune_then_flips_on_publication(app, mini_snapshot):
    partner, admin, check = make_unidentified_check(app)
    gpu_key = gpu_candidate_key(admin, check["id"])
    assert accept_platform(admin, check["id"], [gpu_key],
                           dict(NEW_PLATFORM)).status_code == 200

    with app.app_context():
        # complete snapshot NOT listing the platform: immune, nothing queued
        run = sync.build_run(copy.deepcopy(mini_snapshot), user=None, source="import")
        assert run.summary["changes"] == {"add": 0, "update": 0, "delist": 0, "relist": 0, "merge": 0}
        plat = hm.HclPlatform.query.filter_by(sc_model="HE160").one()
        assert plat.status == hm.STATUS_ACTIVE and plat.origin == hm.ORIGIN_PREVIEW

        # publication: a complete snapshot now lists (lenovo, HE160), with
        # the site's own field values
        published = copy.deepcopy(mini_snapshot)
        published["platforms"].append({
            "brand": "lenovo", "sc_model": "HE160",
            "server": "ThinkCentre M75q Gen5",   # scraped truth != admin entry
            "components": [],
        })
        run2 = sync.build_run(published, user=None, source="import")
        plat = hm.HclPlatform.query.filter_by(sc_model="HE160").one()
        # origin flipped silently; NO duplicate add/relist/conflict — the
        # scraped field values arrive as ordinary update rows for approval
        assert plat.origin == hm.ORIGIN_SCRAPE
        assert run2.summary["changes"]["add"] == 0
        assert run2.summary["changes"]["relist"] == 0
        assert run2.summary["changes"]["delist"] == 0
        assert run2.summary["changes"]["update"] >= 1
        pending = hm.HclPendingChange.query.filter_by(status=hm.PENDING).all()
        fields = {(p.entity_key, p.field) for p in pending if p.entity_type == "platform"}
        assert ("lenovo/HE160", "server") in fields
        srv = next(p for p in pending if p.entity_key == "lenovo/HE160"
                   and p.field == "server")
        assert srv.old_value == NEW_SERVER
        assert srv.new_value == "ThinkCentre M75q Gen5"


def test_platform_spec_validation_errors(app):
    partner, admin, check = make_failed_check(app)          # identifies HE155
    project = make_project(partner, name="Tiny2")
    check2 = upload_bom(partner, project["id"], _tiny_bom())  # no platform
    gpu_key = gpu_candidate_key(admin, check2["id"])

    # a platform WAS identified: the spec is refused, acceptance links to it
    r = accept_platform(admin, check["id"], [NIC_KEY], dict(NEW_PLATFORM))
    assert r.status_code == 400
    assert "identified" in r.get_json()["error"]

    # a scrape-origin ACTIVE platform with that key already exists
    r = accept_platform(admin, check2["id"], [gpu_key],
                        {"brand": "lenovo", "sc_model": "HE155", "server": None})
    assert r.status_code == 400
    assert "already exists" in r.get_json()["error"]

    # missing sc_model / missing brand
    r = accept_platform(admin, check2["id"], [gpu_key],
                        {"brand": "lenovo", "sc_model": ""})
    assert r.status_code == 400 and "sc_model" in r.get_json()["error"]
    r = accept_platform(admin, check2["id"], [gpu_key], {"sc_model": "HE160"})
    assert r.status_code == 400 and "brand" in r.get_json()["error"]

    with app.app_context():   # the refused attempts created nothing
        assert hm.HclPlatform.query.filter_by(sc_model="HE160").count() == 0
        assert hm.HclComponent.query.filter_by(kind="gpu").count() == 0
        assert hm.HclScrapeRun.query.filter_by(source="preview").count() == 0


# ── platform naming for software-only builds (owner, 2026-09-09) ─────────────
# These configurations are validated, not certified: there is no SC appliance
# number to quote, so the platform is named after the manufacturer's model and
# the form suggests it from the BOM's server line.

def test_platform_name_is_suggested_from_the_manufacturer_model():
    f = preview._model_name_from_server
    assert f("Lenovo ThinkEdge SE160 Gen 1") == "ThinkEdge SE160 Gen 1"
    assert f("Dell PowerEdge R760XD2") == "PowerEdge R760XD2"
    assert f("Supermicro SYS-511R-M") == "SYS-511R-M"
    assert f("HPE ProLiant DL320 Gen11") == "ProLiant DL320 Gen11"
    assert f("ThinkSystem SR650 V4") == "ThinkSystem SR650 V4"   # already vendor-less
    assert f(None) is None and f("   ") is None
    assert len(f("Lenovo " + "X" * 80)) == 40                    # fits sc_model


def test_a_manufacturer_model_name_is_a_valid_platform_name(app):
    """Free text with spaces must be accepted: the old hint steered towards
    HE/HC numbers, which do not exist for software-only builds."""
    partner, admin, check = make_unidentified_check(app)
    gpu_key = gpu_candidate_key(admin, check["id"])
    name = "ThinkEdge SE160 Gen 1"
    r = accept_platform(admin, check["id"], [gpu_key],
                        {"brand": "lenovo", "sc_model": name, "server": NEW_SERVER})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["platform_created"] == "lenovo/" + name
    with app.app_context():
        plat = hm.HclPlatform.query.filter_by(sc_model=name).one()
        assert plat.origin == hm.ORIGIN_PREVIEW and plat.brand == "lenovo"
        assert [l.component.kind for l in plat.links] == ["gpu"]


# ── near-match warning + link-to-existing (owner, 2026-09-09) ────────────────
# The platform name is free text by design, so two admins can spell the same
# box differently. The form warns while typing and offers to link to the
# existing platform instead of creating a twin — a warning, never a veto.

def test_near_matches_folds_spelling_differences(app):
    with app.app_context():
        from bom.preview import near_matches
        exact = near_matches("lenovo", "ThinkCentre M70q Tiny Gen6")
        assert exact and exact[0]["sc_model"] == "HE155"
        assert exact[0]["identical"] is True and exact[0]["similarity"] == 1.0
        # spacing and the vendor word must not hide the duplicate
        for spelling in ("ThinkCentre M70q Tiny Gen 6", "Lenovo ThinkCentre M70q TinyGen6"):
            assert [m["sc_model"] for m in near_matches("lenovo", spelling)] == ["HE155"]
        # a genuinely different box is not flagged
        assert near_matches("lenovo", "ThinkAgile VX7531 Gen 9") == []
        # a partial name still finds the fuller one
        partial = near_matches("lenovo", "M70q Tiny")
        assert "HE155" in [m["sc_model"] for m in partial]
        # …but a bare family token is too weak to sweep the catalog
        assert near_matches("lenovo", "M70q") == []


def test_near_matches_reports_other_brands_last(app):
    with app.app_context():
        from bom.preview import near_matches
        m = near_matches("dell", "ThinkCentre M70q Tiny Gen6")
        assert m and m[0]["sc_model"] == "HE155"
        assert m[0]["same_brand"] is False       # flagged, not hidden


def test_platform_matches_route(app):
    partner, admin, check = make_failed_check(app)
    r = admin.get("/admin/api/hcl/platform-matches?brand=lenovo&name=ThinkCentre%20M70q%20Tiny%20Gen%206")
    assert r.status_code == 200
    assert [m["sc_model"] for m in r.get_json()["matches"]] == ["HE155"]
    # nothing typed yet, and a partner cannot look at the catalog
    assert admin.get("/admin/api/hcl/platform-matches").get_json() == {"matches": []}
    assert partner.get("/admin/api/hcl/platform-matches?name=x").status_code == 403


def test_creating_a_near_duplicate_warns_but_proceeds(app):
    """Free-text naming is deliberate: the twin is created, with the near
    match reported so the admin (and the HCL team) can reconcile later."""
    partner, admin, check = make_unidentified_check(app)
    gpu_key = gpu_candidate_key(admin, check["id"])
    # a different server string, so the first platform does not then identify
    # the second check — only the NAME is nearly the same
    first = accept_platform(admin, check["id"], [gpu_key],
                            dict(NEW_PLATFORM, server="Lenovo Legacy Tiny Box"))
    assert first.status_code == 200, first.get_data(as_text=True)

    partner2, admin2, check2 = make_unidentified_check(app)
    gpu2 = gpu_candidate_key(admin2, check2["id"])
    twin = dict(NEW_PLATFORM, sc_model="HE 160")     # same box, spelled loosely
    r = accept_platform(admin2, check2["id"], [gpu2], twin)
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d["platform_created"] == "lenovo/" + twin["sc_model"]
    assert [w["sc_model"] for w in d["platform_warnings"]] == [NEW_PLATFORM["sc_model"]]


def test_linking_to_an_existing_platform_creates_nothing(app):
    partner, admin, check = make_unidentified_check(app)
    gpu_key = gpu_candidate_key(admin, check["id"])
    with app.app_context():
        target = hm.HclPlatform.query.filter_by(sc_model="HE155").one()
        target_id, target_key = target.id, target.key
        before = hm.HclPlatform.query.count()

    r = accept_platform(admin, check["id"], [gpu_key], {"existing_id": target_id})
    assert r.status_code == 200, r.get_data(as_text=True)
    d = r.get_json()
    assert d["platform_created"] is None
    assert d["platform_linked"] == target_key
    assert d["linked"] == ["%s -> %s" % (target_key, gpu_key)]
    with app.app_context():
        assert hm.HclPlatform.query.count() == before       # no twin
        comp = hm.HclComponent.query.filter_by(kind="gpu").one()
        assert [l.platform.sc_model for l in comp.links] == ["HE155"]
        assert comp.origin == hm.ORIGIN_PREVIEW


def test_link_to_existing_rejects_a_bad_id(app):
    partner, admin, check = make_unidentified_check(app)
    gpu_key = gpu_candidate_key(admin, check["id"])
    for bad in ({"existing_id": 999999}, {"existing_id": "abc"}):
        r = accept_platform(admin, check["id"], [gpu_key], bad)
        assert r.status_code == 400, r.get_data(as_text=True)
    with app.app_context():
        assert hm.HclComponent.query.filter_by(kind="gpu").count() == 0


# ── the HCL publishes a placeholder under its own name (owner, 2026-09-09) ────
# The publication flip keys on (brand, sc_model) and cannot see a rename, so
# without this the placeholder would live on beside the published platform and
# the feed would keep asking the HCL team to publish a box they already have.

MERGE_SERVER = "ThinkEdge SE160 Gen 1"


def _placeholder_with_parts(app):
    """A pre-publication platform holding two parts: one the HCL will publish
    an equivalent of, one it will not."""
    with app.app_context():
        p = hm.HclPlatform(brand="lenovo", sc_model=MERGE_SERVER,
                           server="Lenovo " + MERGE_SERVER, origin=hm.ORIGIN_PREVIEW)
        gpu = hm.HclComponent(kind="gpu", part_number="no-part:integrated-graphics",
                              description="Integrated Graphics",
                              origin=hm.ORIGIN_PREVIEW, attrs={})
        nic = hm.HclComponent(kind="nic", part_number="no-part:realtek-2-5g",
                              description="Realtek RTL8125BGS 2.5G Ethernet",
                              origin=hm.ORIGIN_PREVIEW, attrs={})
        db.session.add_all([p, gpu, nic])
        db.session.flush()
        db.session.add_all([
            hm.HclPlatformComponent(platform=p, component=gpu, origin=hm.ORIGIN_PREVIEW),
            hm.HclPlatformComponent(platform=p, component=nic, origin=hm.ORIGIN_PREVIEW)])
        db.session.commit()


def _published_snapshot(sc_model="HE160", server=MERGE_SERVER):
    return {"scraped_at": "2026-09-16T00:00:00+00:00", "complete": True, "errors": [],
            "pages_total": 2, "pages_done": 2, "devices": [],
            "platforms": [{"brand": "lenovo", "sc_model": sc_model, "server": server,
                           "form_factor": "DT", "sockets": 1, "components": [
                               {"kind": "gpu", "part_number": "LEN-GPU-1",
                                "description": "Integrated Graphics",
                                "tce": False, "attrs": {}}]}]}


def test_publication_under_another_name_queues_a_merge(app):
    _placeholder_with_parts(app)
    with app.app_context():
        changes = sync.diff_snapshot(_published_snapshot())
        merges = [c for c in changes if c["change_kind"] == "merge"]
        assert [(c["entity_key"], c["payload"]["into"]) for c in merges] == [
            ("lenovo/%s" % MERGE_SERVER, "lenovo/HE160")]
        # the placeholder is merged, never delisted outright (other platforms
        # absent from this deliberately minimal snapshot still delist normally)
        assert not [c for c in changes if c["change_kind"] == "delist"
                    and c["entity_key"] == "lenovo/%s" % MERGE_SERVER]


def test_approving_the_merge_retires_the_placeholder_into_the_published_platform(app):
    _placeholder_with_parts(app)
    with app.app_context():
        sync.build_run(_published_snapshot(), user=None, source="import")
        sync.bulk([], "approve", None, all_pending=True)

        placeholder = hm.HclPlatform.query.filter_by(sc_model=MERGE_SERVER).one()
        published = hm.HclPlatform.query.filter_by(sc_model="HE160").one()
        assert placeholder.status == hm.STATUS_DELISTED and placeholder.delisted_at
        assert published.status == hm.STATUS_ACTIVE and published.origin == "scrape"

        # the part the HCL published supersedes ours; the other one moves over
        gpu = hm.HclComponent.query.filter_by(part_number="no-part:integrated-graphics").one()
        nic = hm.HclComponent.query.filter_by(part_number="no-part:realtek-2-5g").one()
        assert gpu.status == hm.STATUS_DELISTED and gpu.delisted_at
        assert nic.status == hm.STATUS_ACTIVE and nic.origin == hm.ORIGIN_PREVIEW
        live = {l.component.key for l in published.links if l.status == hm.STATUS_ACTIVE}
        assert live == {"gpu/LEN-GPU-1", "nic/no-part:realtek-2-5g"}

        # and the HCL team is no longer asked to publish what they published
        feed = preview.preview_feed()
        assert feed["platforms"] == []
        assert [c["part_number"] for c in feed["components"]] == ["no-part:realtek-2-5g"]


def test_a_boms_check_survives_the_merge(app):
    """The placeholder disappearing must not turn a passing BOM red: the
    published part matches the same description, the still-unpublished one
    moved to the published platform."""
    _placeholder_with_parts(app)
    with app.app_context():
        sync.build_run(_published_snapshot(), user=None, source="import")
        sync.bulk([], "approve", None, all_pending=True)

        from bom.check import run_check
        def part(cat, desc, qty=3):
            return BOMComponent(part_number=None, description=desc, quantity=qty, category=cat)
        bom = NormalizedBOM(vendor="Lenovo", configs=[BOMConfig(
            name="Edge", server_model="Lenovo " + MERGE_SERVER, node_count=3, components=[
                part("chassis", "ThinkEdge SE160 chassis"),
                part("memory", "16 GB DDR5-5600MT/s SODIMM"),
                part("storage", "512 GB SSD M.2 2280 PCIe Gen4 TLC Opal"),
                part("gpu", "Integrated Graphics"),
                part("nic", "Realtek RTL8125BGS 2.5G Ethernet")])])
        result = run_check(bom, None)
        cr = result["technical"]["config_results"][0]
        codes = [f["code"] for f in cr["findings"]]
        assert "gpu_not_in_hcl" not in codes and "nic_not_in_hcl" not in codes
        # one platform now, not two
        assert cr["platform"]["sc_models"] == ["HE160"]


def test_a_merge_waits_for_the_published_platform_instead_of_being_consumed(app):
    """Approving the merge first must not mark it done and leave the catalog
    half-merged — it stays pending until the platform's own add is approved."""
    _placeholder_with_parts(app)
    with app.app_context():
        run = sync.build_run(_published_snapshot(), user=None, source="import")
        merge = next(c for c in run.changes if c.change_kind == "merge")
        res = sync.approve(merge, None)
        assert res["applied"] is False and "not in the catalog yet" in res["reason"]
        assert merge.status == hm.PENDING and "not in the catalog yet" in merge.note
        assert hm.HclPlatform.query.filter_by(sc_model=MERGE_SERVER).one().status == hm.STATUS_ACTIVE

        # a full pass then applies it, whatever order the rows come in
        sync.bulk([], "approve", None, all_pending=True)
        assert hm.HclPlatform.query.filter_by(sc_model=MERGE_SERVER).one().status == hm.STATUS_DELISTED


def test_no_merge_when_the_server_differs(app):
    """A genuinely different box must not swallow the placeholder."""
    _placeholder_with_parts(app)
    with app.app_context():
        snap = _published_snapshot(server="ThinkEdge SE450 Gen 2")
        changes = sync.diff_snapshot(snap)
        assert not [c for c in changes if c["change_kind"] == "merge"]
