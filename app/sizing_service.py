"""The layer between the HTTP routes and the sizing engine.

Holds what every caller of ``recommend.generate_recommendations`` should get
regardless of how the inputs arrived (a typed-in manual sizing, an import, and
later the MCP server, docs/mcp/00-findings.md §9), so the routes do not each
grow their own copy.

Deliberately NOT in ``fingerprint.ENGINE_MODULES``: nothing here changes a
computed number, only what is said about the inputs, so editing this module
must not mark every stored sizing stale.
"""


def _positive(value):
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def input_quality_warnings(summary):
    """Plain-language caveats about the inputs themselves, appended to the
    engine's own warnings.

    The engine reads a missing or zero largest VM as "no constraint" and skips
    the largest-VM fit check (``recommend._fit_model``): a sizing typed in
    without it could recommend nodes the biggest VM cannot run on, with nothing
    on screen saying so. Non-fatal on purpose; the sizing is still useful, it
    just has to be checked by hand."""
    missing = []
    if not _positive(summary.get("max_vm_cores")):
        missing.append("vCPUs")
    if not _positive(summary.get("max_vm_ram_gb")):
        missing.append("RAM")
    if not missing:
        return []
    return [
        f"Largest VM {' and '.join(missing)} unknown, so the check that the "
        "largest VM fits on a single node was skipped and this sizing may be "
        "wrong. Verify that no VM needs more vCPUs than a node's logical "
        "processors or more RAM than a node's usable RAM, or enter the largest "
        "VM to have it checked."
    ]
