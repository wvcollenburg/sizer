"""Validity fingerprint for a cached sizing result (docs/projects-plan.md §3).

A saved sizing stores its computed result so a project can be listed, compared
and exported without opening each sizing in a browser. That cache is only
trustworthy while the three things it was computed from are unchanged:

1. **the engine** — a content hash of the sizing modules, so a bug fix in the
   maths invalidates every stored result without anyone remembering to bump a
   version constant (decision 9);
2. **the tunables** — the admin-editable SizingSetting rows, which are global
   and genuinely alter every result;
3. **the catalog rows the sizing actually used** — re-resolved from the *live*
   catalog each time (decision 6).

Point 3 is the subtle one. The stored ``refs`` hold identity only ("model
HE153, this CPU option, these drive sizes"); the hash is taken over the values
that identity resolves to *now*. Hashing the stored values instead would be
self-referential — they never change, so nothing could ever go stale. Editing
the Edge node's spec therefore invalidates sizings that used an Edge node,
while adding a 626th CPU to the catalog invalidates nothing.

Validated (software-only) sizings resolve no catalog rows at all: their maths
runs on disk specs posted with the request, so their fingerprint depends on the
engine and tunables only. That is correct rather than a gap — a catalog edit
cannot change a number that was never read from the catalog.

Parser versions are tracked separately by design, see PARSER_VERSION below.
"""
import hashlib
import json
import os

# Every module whose contents can change a computed number. app.py is
# deliberately absent: the calculators were moved to calc.py precisely so an
# unrelated route edit would not invalidate every stored result in the system.
ENGINE_MODULES = (
    "calc.py",
    "recommend.py",
    "models.py",
    "cluster_split.py",
    "storage_only.py",
    "tunables.py",
    "cpu_benchmarks.py",
)

# Parsers are NOT part of the engine hash. A parser fix cannot be repaired by
# recalculating: refresh replays the summary stored in the payload and never
# re-reads the source file, which the app does not keep. A mismatch here means
# "re-import to pick up the correction" — a prompt to a human, not something
# auto-refresh can clear (§3.3). Commit 6dae54f, which fixed a misread Live
# Optics RAM label, is exactly this case.
PARSER_MODULES = ("liveoptics.py", "rvtools.py", "parser_common.py")

_APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _hash_files(names):
    """Content hash of the given app/ modules, in a fixed order."""
    digest = hashlib.sha256()
    for name in names:
        path = os.path.join(_APP_DIR, name)
        digest.update(name.encode("utf-8"))
        try:
            with open(path, "rb") as handle:
                digest.update(handle.read())
        except OSError:
            # A missing module is itself a meaningful change; record the absence
            # rather than silently hashing nothing.
            digest.update(b"<missing>")
    return digest.hexdigest()


# Computed once per process at import: cheap, and impossible to forget.
ENGINE_VERSION = _hash_files(ENGINE_MODULES)[:16]
PARSER_VERSION = _hash_files(PARSER_MODULES)[:16]


def _canonical(value):
    """Stable JSON for hashing — sorted keys, no incidental whitespace."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def tunables_digest():
    """Hash of every sizing tunable. Small table, read once per fingerprint
    batch by the callers that fingerprint many sizings at a time."""
    from orm_models import SizingSetting
    rows = SizingSetting.query.with_entities(
        SizingSetting.key, SizingSetting.value).all()
    return hashlib.sha256(
        _canonical(sorted((k, v) for k, v in rows)).encode("utf-8")).hexdigest()[:16]


def catalog_digest(refs):
    """Hash the catalog values ``refs`` resolves to *right now*.

    ``refs`` carries identity, not values: the model's name, the description of
    the chosen CPU option, the RAM size, the selected drive sizes. Everything
    hashed here is read fresh from the catalog, which is what makes an admin's
    edit visible as staleness.
    """
    if not isinstance(refs, dict):
        return "norefs"
    mode = refs.get("mode")
    if mode != "appliance":
        # Validated and manual sizings read nothing from the catalog.
        return "nocatalog:" + str(mode or "unknown")

    from orm_models import Model
    model_name = refs.get("model")
    model = Model.query.filter_by(name=model_name).first()
    if model is None:
        # The model was withdrawn from the catalog — permanently stale, and a
        # constant so it doesn't flap between refreshes.
        return "missing-model:" + str(model_name)

    spec = model.to_dict()
    chosen_cpu = None
    for option in spec.get("cpu_options", []):
        if option.get("desc") == refs.get("cpu_desc"):
            chosen_cpu = option
            break

    material = {
        # Only the fields that can move a computed number.
        "cpu": chosen_cpu,
        "storage": spec.get("storage"),
        "ram_options_gb": spec.get("ram_options_gb"),
        "min_nodes": spec.get("min_nodes"),
        "form_factor": spec.get("form_factor"),
        "storage_only_cpu": _matching_option(
            spec.get("storage_only_cpu_options"), refs.get("so_cpu_desc")),
        # The selection itself: two sizings on the same model with different
        # drive sizes must not share a digest.
        "selection": refs.get("selection"),
    }
    return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()[:16]


def _matching_option(options, desc):
    for option in options or []:
        if option.get("desc") == desc:
            return option
    return None


def fingerprint_for(refs, tunables=None):
    """The full validity fingerprint for a result computed from ``refs``."""
    parts = [
        "e:" + ENGINE_VERSION,
        "t:" + (tunables if tunables is not None else tunables_digest()),
        "c:" + catalog_digest(refs),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def refs_from_snapshot(snapshot):
    """Pull the identity refs out of a stored result snapshot.

    A snapshot holds one entry per cluster; they share a model in the common
    case but need not, so all of them are fingerprinted together.
    """
    if not isinstance(snapshot, dict):
        return []
    clusters = snapshot.get("clusters")
    if not isinstance(clusters, list):
        return []
    return [c.get("refs") for c in clusters if isinstance(c, dict) and c.get("refs")]


def fingerprint_snapshot(snapshot, tunables=None, replication=""):
    """Fingerprint for a whole snapshot: every cluster's refs, in order.

    ``replication`` is the inbound-partner digest (§8.5) — empty for a sizing
    nothing replicates into, which is the common case.
    """
    tunables = tunables if tunables is not None else tunables_digest()
    refs = refs_from_snapshot(snapshot)
    if not refs:
        # No refs at all — treat as permanently stale rather than silently
        # trusting a snapshot whose provenance can't be checked.
        return None
    combined = "|".join(fingerprint_for(r, tunables) for r in refs)
    if replication:
        combined += "|r:" + replication
    return hashlib.sha256(combined.encode("utf-8")).hexdigest()


def inbound_replication_digest(config_id):
    """Digest of everything replicating INTO this sizing (§8.5).

    A DR target's capacity is sized from its sources' demand, so a target is not
    fingerprint-independent: exclude a few VMs from site A and site B's inbound
    reserve is wrong with nothing on screen to say so. Folding the sources'
    payload digests and the link terms in here turns that into ordinary
    staleness.

    Reads the stored ``payload_digest`` column rather than re-hashing payloads,
    which run to megabytes of VM lists.
    """
    from auth_models import Configuration
    from project_models import ReplicationLink

    links = ReplicationLink.query.filter_by(
        target_configuration_id=config_id).order_by(
            ReplicationLink.source_configuration_id,
            ReplicationLink.source_cluster).all()
    if not links:
        return ""

    sources = {c.id: c for c in Configuration.query.filter(
        Configuration.id.in_([l.source_configuration_id for l in links])).all()}
    material = []
    for link in links:
        source = sources.get(link.source_configuration_id)
        material.append(link.digest_material(
            source.payload_digest if source else None))
    return hashlib.sha256(
        _canonical(material).encode("utf-8")).hexdigest()[:16]


def result_state(config, tunables=None):
    """Cache state for one sizing: ``fresh``, ``stale`` or ``none``, plus
    whether it predates the current parsers.

    Returns a dict ready to merge into an API row.
    """
    state = {
        "stale": True,
        "has_result": config.result_snapshot is not None,
        "needs_reimport": bool(config.parser_version
                               and config.parser_version != PARSER_VERSION),
    }
    if config.result_snapshot is None or not config.result_fingerprint:
        state["cache"] = "none"
        return state

    current = fingerprint_snapshot(config.result_snapshot, tunables,
                                   inbound_replication_digest(config.id))
    fresh = bool(current) and current == config.result_fingerprint
    state["stale"] = not fresh
    state["cache"] = "fresh" if fresh else "stale"
    return state
