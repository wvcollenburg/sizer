"""Shared helpers for reading the openpyxl workbooks we ingest.

These three were duplicated verbatim across rvtools.py, liveoptics.py and
admin_routes.py (the LiveOptics/RVTools/catalog parsers). Centralised here so a
fix to header handling or numeric coercion lands everywhere at once.
"""

# Hard caps on how much of an uploaded sheet we will materialize. Uploads are
# untrusted: an .xlsx is a ZIP, and a sheet declaring an enormous used-range of
# highly-compressible cells expands to gigabytes ("decompression bomb"), which
# `list(ws.iter_rows())` would happily allocate until the worker OOMs — the 32 MB
# MAX_CONTENT_LENGTH only bounds the COMPRESSED upload. These limits sit far
# above any real RVTools/LiveOptics/catalog export (tens of thousands of rows at
# most) but keep a bomb bounded to a few seconds / tens of MB.
MAX_SHEET_ROWS = 100_000
MAX_SHEET_COLS = 256


class SheetTooLargeError(ValueError):
    """A worksheet exceeds MAX_SHEET_ROWS — likely a decompression bomb."""


def sheet_rows(wb, name):
    """Return a sheet's data rows as dicts keyed by the header row.

    Missing sheet or header-only sheet -> []. Blank header cells become
    ``col_<i>`` so positional access still works. All-empty rows are dropped
    (trailing blank rows in an export must not become phantom hosts/VMs).

    Untrusted-input safety: each row is bounded to MAX_SHEET_COLS (so one
    absurdly-wide row can't blow up memory), and we abort past MAX_SHEET_ROWS
    instead of materializing an unbounded sheet.
    """
    if name not in wb.sheetnames:
        return []
    ws = wb[name]
    headers = None
    width = 0
    out = []
    # max_col caps how many cells we read per row (defeats a column-bomb without
    # materializing 16k-wide rows); we then trim to the real header width so a
    # normal narrow sheet parses exactly as before (no phantom None columns). We
    # count rows ourselves and raise past MAX_SHEET_ROWS rather than OOM-ing.
    for i, row in enumerate(ws.iter_rows(values_only=True, max_col=MAX_SHEET_COLS)):
        if i == 0:
            for j, h in enumerate(row):
                if h is not None:
                    width = j + 1              # real columns = up to last named header
            width = width or len(row)
            row = row[:width]
            headers = [str(h).strip() if h else f"col_{j}" for j, h in enumerate(row)]
            continue
        if i > MAX_SHEET_ROWS:
            raise SheetTooLargeError(
                f"Sheet '{name}' exceeds the maximum of {MAX_SHEET_ROWS} rows."
            )
        row = row[:width]
        if any(v is not None for v in row):
            out.append(dict(zip(headers, row)))
    if headers is None:
        return []
    return out


def to_float(v):
    """Coerce a cell value to float; blanks/None/garbage -> 0.0."""
    try:
        return float(v) if v else 0.0
    except (ValueError, TypeError):
        return 0.0


def to_int(v):
    """Coerce a cell value to int; blanks/None/garbage -> 0."""
    try:
        return int(v) if v else 0
    except (ValueError, TypeError):
        return 0


def source_cpus(hosts):
    """Aggregate the distinct source CPU models across the imported hosts, with
    total socket and host counts. Lets the UI show what the customer runs today
    and look up / let them enter a benchmark score per distinct CPU. Sorted by
    socket count (the dominant CPU first). Hosts with no CPU model are skipped.
    """
    agg = {}
    for h in hosts:
        desc = (h.get("cpu_desc") or "").strip()
        if not desc:
            continue
        entry = agg.setdefault(desc, {"model": desc, "sockets": 0, "hosts": 0})
        entry["sockets"] += h.get("cpu_sockets") or 0
        entry["hosts"] += 1
    return sorted(agg.values(), key=lambda x: x["sockets"], reverse=True)
