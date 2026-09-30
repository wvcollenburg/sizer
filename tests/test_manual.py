"""The online manual (app/manual_routes.py, templates/manual/).

Two editions; the staff one must never reach a partner:
  - /manual/scale and its pictures answer 404 to partners (users and tenant
    admins alike) and redirect when signed out, like every page
  - the partner edition renders none of the staff chapters or asides, and
    points at no staff picture
  - every picture a chapter asks for exists, so a renamed shot is caught here
    rather than as a broken image in front of a reader

Run: .venv/bin/python -m pytest tests/test_manual.py -q
"""
import glob
import os
import re
import sys

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("ENABLE_SCHEDULER", "0")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.pop("SESSION_COOKIE_SECURE", None)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

import pytest  # noqa: E402
import app as appmod  # noqa: E402
from database import db  # noqa: E402
from extensions import limiter  # noqa: E402
from auth_models import User, ROLE_SUPER_ADMIN  # noqa: E402

APP = os.path.join(os.path.dirname(__file__), "..", "app")
IMG = os.path.join(APP, "manual", "img")
PASSWORD = "Abcdef1!xy"

PARTNER = "pm@partnerco.example"
COLLEAGUE = "colleague@partnerco.example"   # second sign-up: a plain user
STAFF = "sa@scalecomputing.com"
SUPER = "root@superco.example"               # super admin outside the scale tenant

SHOT = re.compile(r"""shot\(\s*"([^"]+)"\s*,\s*"[^"]*"(?:\s*,\s*"(user|scale)")?""")


@pytest.fixture()
def app():
    application = appmod.app
    application.config["TESTING"] = True
    application.config["RATELIMIT_ENABLED"] = False
    limiter.enabled = False
    with application.app_context():
        db.drop_all()
        db.create_all()
    return application


def client_for(app, email, super_admin=False):
    c = app.test_client()
    c.post("/api/auth/signup",
           json={"email": email, "password": PASSWORD, "accept_privacy": True})
    if super_admin:
        with app.app_context():
            u = User.query.filter_by(email=email).first()
            u.role = ROLE_SUPER_ADMIN
            db.session.commit()
    c.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    return c


def _any_image(edition):
    names = sorted(os.listdir(os.path.join(IMG, edition)))
    assert names, "no %s pictures — run tools/manual_shots.py" % edition
    return names[0]


# ── who may read what ────────────────────────────────────────────────────────

def test_signed_out_is_sent_to_the_login(app):
    c = app.test_client()
    for path in ("/manual/", "/manual/scale", "/manual/img/user/" + _any_image("user")):
        assert c.get(path).status_code == 302, path


@pytest.mark.parametrize("email", [PARTNER, COLLEAGUE])
def test_partners_read_only_the_partner_edition(app, email):
    if email == COLLEAGUE:
        client_for(app, PARTNER)          # makes the colleague a plain user
    c = client_for(app, email)
    assert c.get("/manual/").status_code == 200
    assert c.get("/manual/scale").status_code == 404
    assert c.get("/manual/img/user/" + _any_image("user")).status_code == 200
    assert c.get("/manual/img/scale/" + _any_image("scale")).status_code == 404
    # no way round the edition check through the path
    assert c.get("/manual/img/user/..%2Fscale%2F" + _any_image("scale")).status_code == 404
    assert c.get("/manual/img/other/x.png").status_code == 404


@pytest.mark.parametrize("email,is_super", [(STAFF, False), (SUPER, True)])
def test_staff_and_super_admins_read_both(app, email, is_super):
    c = client_for(app, email, super_admin=is_super)
    assert c.get("/manual/").status_code == 200
    assert c.get("/manual/scale").status_code == 200
    assert c.get("/manual/img/scale/" + _any_image("scale")).status_code == 200


# ── what each edition contains ──────────────────────────────────────────────

def test_partner_edition_shows_nothing_staff_only(app):
    c = client_for(app, PARTNER)
    html = c.get("/manual/").get_data(as_text=True)
    assert "/manual/img/scale/" not in html
    assert "manual-staff-tag" not in html
    assert 'id="staff"' not in html and 'id="admin"' not in html
    assert "/manual/scale" not in html, "partners must not even see the link"
    assert "Salesforce" not in html


def test_staff_edition_has_the_staff_chapters(app):
    c = client_for(app, STAFF)
    html = c.get("/manual/scale").get_data(as_text=True)
    for anchor in ('id="staff"', 'id="admin"', 'id="staff-salesforce"'):
        assert anchor in html
    assert 'href="/manual/"' in html      # the switch to the partner edition


def test_every_referenced_picture_exists():
    missing = []
    for path in glob.glob(os.path.join(APP, "templates", "manual", "**", "*.html"),
                          recursive=True):
        for name, edition in SHOT.findall(open(path, encoding="utf-8").read()):
            if not os.path.isfile(os.path.join(IMG, edition or "user", name + ".png")):
                missing.append("%s: %s/%s" % (os.path.basename(path), edition or "user", name))
    assert not missing, "\n".join(missing)


def test_staff_pictures_only_in_staff_chapters():
    """A scale/ picture in a shared chapter would render a broken image in the
    partner edition (and name a staff screen). Shared chapters may only use
    them inside a staff() aside, which renders nothing for partners."""
    staff_only = {"staff.html", "admin.html"}
    bad = []
    for path in glob.glob(os.path.join(APP, "templates", "manual", "chapters", "*.html")):
        if os.path.basename(path) in staff_only:
            continue
        text = open(path, encoding="utf-8").read()
        outside = re.sub(r"{%\s*call m\.staff\(\)\s*%}.*?{%\s*endcall\s*%}", "", text, flags=re.S)
        for name, edition in SHOT.findall(outside):
            if edition == "scale":
                bad.append("%s: %s" % (os.path.basename(path), name))
    assert not bad, "\n".join(bad)
