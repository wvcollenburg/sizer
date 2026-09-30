"""The online user manual, in two editions.

  /manual        partners and customers: users and tenant admins
  /manual/scale  Scale Computing staff (scale tenant) and super admins — the
                 partner edition plus the staff-only features and the admin
                 panel

Both sit behind the global login gate (auth.require_login), like the rest of
the app. The staff edition answers 404 to anyone else rather than 403, so its
existence is not advertised.

Screenshots are NOT under /static: static files skip the login gate, and the
staff edition's pictures show staff-only screens. They live in
app/manual/img/<edition>/ and are served here with the same check as the page
that shows them. Regenerate them with tools/manual_shots.py.
"""
import os

from flask import Blueprint, abort, render_template, send_from_directory

from auth import current_user

manual_bp = Blueprint("manual", __name__, url_prefix="/manual")

IMG_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manual", "img")
EDITIONS = ("user", "scale")


def can_read_scale_edition(user):
    return user is not None and (user.is_scale or user.is_super_admin)


def _render(edition):
    return render_template("manual/manual.html", edition=edition,
                           scale=(edition == "scale"),
                           can_read_scale=can_read_scale_edition(current_user()))


@manual_bp.route("/")
def user_manual():
    return _render("user")


@manual_bp.route("/scale")
def scale_manual():
    if not can_read_scale_edition(current_user()):
        abort(404)
    return _render("scale")


@manual_bp.route("/img/<edition>/<path:name>")
def manual_image(edition, name):
    if edition not in EDITIONS:
        abort(404)
    if edition == "scale" and not can_read_scale_edition(current_user()):
        abort(404)
    resp = send_from_directory(os.path.join(IMG_ROOT, edition), name)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    return resp
