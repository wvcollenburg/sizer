"""Shared helpers for reading the openpyxl workbooks we ingest.

These three were duplicated verbatim across rvtools.py, liveoptics.py and
admin_routes.py (the LiveOptics/RVTools/catalog parsers). Centralised here so a
fix to header handling or numeric coercion lands everywhere at once.
"""


def sheet_rows(wb, name):
    """Return a sheet's data rows as dicts keyed by the header row.

    Missing sheet or header-only sheet -> []. Blank header cells become
    ``col_<i>`` so positional access still works. All-empty rows are dropped
    (trailing blank rows in an export must not become phantom hosts/VMs).
    """
    if name not in wb.sheetnames:
        return []
    ws = wb[name]
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        return []
    headers = [str(h).strip() if h else f"col_{i}" for i, h in enumerate(rows[0])]
    return [dict(zip(headers, row)) for row in rows[1:] if any(v is not None for v in row)]


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
