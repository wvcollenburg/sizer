"""Small helpers for reading an HTTP request the same way in every route.

``json_body()`` replaces the bare ``request.json`` (which raises a 415/400
HTML error for a missing or non-JSON body) and the ``... or {}`` variants
scattered across the blueprints: the body is always a dict, empty when the
client sent nothing usable. Routes that must reject an empty body still do,
from the dict.

``spooled_xlsx()`` is the upload preamble every .xlsx importer repeated:
field present, ``.xlsx`` name, ZIP magic, spool to a temp file, unlink.

``sha256_file()`` is the digest every importer keeps as provenance.
"""
import contextlib
import hashlib
import os
import tempfile

from flask import request

XLSX_MAGIC = b"PK\x03\x04"


def json_body():
    """The request's JSON body as a dict; {} when absent, unparseable or not
    an object."""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def sha256_file(path):
    """Hex SHA-256 of a file, read in 1 MiB chunks. Only the digest is ever
    stored — never the file — so a re-import can be spotted without holding
    customer data (docs/projects-plan.md §8)."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class UploadError(ValueError):
    """An upload the route must refuse; ``status`` is the HTTP code."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@contextlib.contextmanager
def spooled_xlsx(req, field="file", bad_type_msg="File must be .xlsx"):
    """Validate the ``field`` upload of ``req`` as an .xlsx and yield the path
    of a temp copy, removed on exit. Raises UploadError (400) when the field
    is missing, the name is not .xlsx or the bytes are not a ZIP (an .xlsx is
    one; anything else must not reach openpyxl)."""
    if field not in req.files:
        raise UploadError("No file uploaded")
    f = req.files[field]
    if not f.filename or not f.filename.endswith(".xlsx"):
        raise UploadError(bad_type_msg)
    head = f.stream.read(len(XLSX_MAGIC))
    f.stream.seek(0)
    if head != XLSX_MAGIC:
        raise UploadError(bad_type_msg)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    try:
        f.save(tmp.name)
        tmp.close()
        yield tmp.name
    finally:
        os.unlink(tmp.name)
