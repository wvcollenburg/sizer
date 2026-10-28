"""Value formatting and rasterising shared by the deck (export_pptx) and the
document (export_docx) renderers, so neither reaches into the other's privates."""

import os
import subprocess
import tempfile


def fmt_ram(gb):
    if gb >= 1024:
        return f"{gb / 1024:.1f} TB"
    return f"{gb} GB"


def fmt_num(n):
    if isinstance(n, float):
        return f"{n:,.1f}"
    return f"{n:,}"


def spare_vcpus(r, projected_vcpus):
    """vCPUs the proposed cluster can still take at the TARGET ratio, beyond
    the projected demand: spare cores (CPU basis minus required cores) times
    the target ratio. r["vcpu_ratio"] is the ratio the cluster ACHIEVES, so
    basis_cores x vcpu_ratio is the projected demand itself and their
    difference was always 0. Required cores (ceil(vcpus / target), plus any
    replication reserve) ride on utilization.cpu.abs; the target ratio is
    recovered from them. Returns None when a stored result predates them."""
    full = r.get("sized_full_cluster", False)
    basis = r["totals"]["cores"] if full else r["n_minus_1"]["cores"]
    required = (((r.get("utilization") or {}).get("cpu") or {}).get("abs") or {}).get("total")
    if not required:
        return None
    return int((basis - required) * projected_vcpus / required)


def svg_to_png(svg, width=2000):
    """Rasterise an SVG string to PNG bytes. Production uses cairosvg; on the dev
    mac (no cairo) it falls back to qlmanage so the export can be tested locally.
    Returns None if neither is available (the caller then skips the diagram)."""
    try:
        import cairosvg
        return cairosvg.svg2png(bytestring=svg.encode("utf-8"), output_width=width)
    except Exception:
        pass
    try:  # macOS dev fallback
        with tempfile.TemporaryDirectory() as d:
            sp = os.path.join(d, "d.svg")
            with open(sp, "w") as f:
                f.write(svg)
            subprocess.run(["qlmanage", "-t", "-s", str(width), "-o", d, sp],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
            png = sp + ".png"
            return open(png, "rb").read() if os.path.exists(png) else None
    except Exception:
        return None
