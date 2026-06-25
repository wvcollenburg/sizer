"""Regenerate app/data/specrate_lookup.json from a SPEC CPU 2017 results export.

Input: an .mhtml (or .html) saved copy of "All Published SPEC CPU 2017 Integer
Rate Results" (spec.org/cpu2017/results/rint2017.html). Output: per-socket
average SPECrate2017_int_base for each distinct CPU, keyed by the same
normalize_cpu() the runtime lookup uses.

Usage:
    PYTHONPATH=app python tools/build_specrate_lookup.py "<path-to.mhtml>"

Re-run whenever you grab a fresh SPEC export to refresh the source-CPU scores.
"""
import email
import json
import os
import re
import statistics
import sys
from email import policy

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))
from cpu_benchmarks import normalize_cpu  # noqa: E402  (shared key logic)

OUT = os.path.join(os.path.dirname(__file__), "..", "app", "data",
                   "specrate_lookup.json")


def _html(path):
    raw = open(path, "rb").read()
    if path.lower().endswith(".mhtml"):
        msg = email.message_from_bytes(raw, policy=policy.default)
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                return part.get_content()
        raise SystemExit("no text/html part in mhtml")
    return raw.decode("utf-8", "replace")


def _num(cell):
    t = re.sub(r"<.*?>", "", cell).replace("&nbsp;", "").strip()
    try:
        return float(t)
    except ValueError:
        return None


def _processor(sysname_cell):
    """The System Name cell embeds '{system} {processor},{ghz}GHz' before the
    doc-link <span>. The processor is the trailing run starting at the last
    Intel/AMD token."""
    head = re.split(r"<br", sysname_cell, 1)[0]
    head = re.sub(r"\s+", " ", re.sub(r"<.*?>", " ", head).replace("&nbsp;", " ")).strip()
    head = re.sub(r",\s*[\d.]+\s*GHz.*$", "", head, flags=re.I).strip()
    hits = list(re.finditer(r"\b(Intel|AMD)\b", head))
    return head[hits[-1].start():].strip() if hits else None


def main(path):
    html = _html(path)
    agg = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.I | re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.I | re.S)
        if len(cells) != 10:
            continue
        base, chips = _num(cells[6]), _num(cells[4])   # Base result, # Chips
        proc = _processor(cells[1])
        if base is None or not chips or not proc:
            continue
        key = normalize_cpu(proc)
        if not key:
            continue
        e = agg.setdefault(key, {"display": proc.strip(") "), "vals": []})
        e["vals"].append(base / chips)               # per-socket
    out = {k: {"model": e["display"],
               "specrate_int": round(statistics.mean(e["vals"]), 1),
               "samples": len(e["vals"])}
           for k, e in agg.items()}
    json.dump(out, open(OUT, "w"), indent=0, sort_keys=True)
    print(f"wrote {len(out)} CPUs ({sum(len(e['vals']) for e in agg.values())} "
          f"results) -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1
         else "_archive/All Published SPEC CPU 2017 Integer Rate Results.mhtml")
