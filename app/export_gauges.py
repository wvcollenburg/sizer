"""Utilization charts for the exportables (PPTX / DOCX / PDF).

Renders the same CPU/RAM/Storage "now vs sized" stacked bars the web sizer shows,
as a single PNG block that drops identically into every export format
(python-pptx and python-docx both take PNGs; the PDF is the LibreOffice render
of the PPTX). The full bar = 100% of the full cluster:

  now            solid, coloured by current load (green / orange / red)
  growth+snap    45deg light hatch (#8ca3c6 / #b9c7de), fills up to the sized %
  free           track gap in the middle
  HA reserve    -45deg dark hatch (#5f7aa6 / #93a8cb), anchored at the right edge
"""
import io
import os

from PIL import Image, ImageDraw, ImageFont

from i18n import translator, is_cjk

# ── Sizer palette (kept in sync with style.css .util-* rules) ────────────────
NOW_LOW = "#2e7d32"      # < 70% load
NOW_MID = "#e67e22"      # 70-90%
NOW_HIGH = "#c0392b"     # > 90%
RESERVE_HATCH = ("#8ca3c6", "#b9c7de")   # growth + snapshot, +45deg
HA_HATCH = ("#5f7aa6", "#93a8cb")        # HA failover reserve, -45deg
TRACK = "#cfe0f4"        # free / unused — light blue, enough contrast to read
HAIRLINE = "#9fb2cf"     # thin outline around each bar pill
TEXT = "#2c3e50"
MUTED = "#6b7a90"
ORANGE = "#e67e22"

_SS = 4                  # supersample factor for crisp edges + hatching
_FONT_DIR = os.path.join(os.path.dirname(__file__), "..", "resources", "fonts")


# CJK-capable fallbacks (installed via the Dockerfile's fonts-noto-cjk). Try both
# the opentype and truetype install paths — distros differ.
_NOTO_CJK = ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
             "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc")


def _font(name, px, cjk=False):
    # For CJK languages, prefer Noto so labels render as glyphs instead of tofu;
    # Martel Sans / DejaVu / Arial can't render CJK.
    cands = (_NOTO_CJK if cjk else ()) + (
        os.path.join(_FONT_DIR, name),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ) + (() if cjk else _NOTO_CJK)
    for cand in cands:
        try:
            return ImageFont.truetype(cand, px)
        except OSError:
            continue
    return ImageFont.load_default()


def now_color(now_pct):
    return NOW_HIGH if now_pct > 90 else (NOW_MID if now_pct >= 70 else NOW_LOW)


def util_rows(utilization):
    """Build render_util_bars() rows + an any_ha flag from a recommendation's
    utilization dict: {cpu/ram/storage: {current, total, ha_reserve}}."""
    rows, any_ha = [], False
    for label, key in (("CPU", "cpu"), ("RAM", "ram"), ("Storage", "storage")):
        v = (utilization or {}).get(key)
        if not v:
            continue
        now = max(0, round(v.get("current", 0)))
        tot = max(now, round(v.get("total", 0)))
        ha = max(0, round(v.get("ha_reserve", 0)))
        any_ha = any_ha or ha > 0
        rows.append({"label": label, "now": now, "sized": tot, "ha": ha})
    return rows, any_ha


def compute_floor_sentence(r, lang="en"):
    """One-line plain-language summary of a recommendation's active compute-floor
    coverage (perf-based sizing), or None when the floor is off / absent. Shared
    by the PPTX and DOCX exporters so both read identically. Coverage >= 100%
    means the cluster meets the source's utilized, grown compute demand; the
    clock/benchmark split shows which signal carried it."""
    cf = r.get("compute_floor")
    if not cf or cf.get("coverage_pct") is None:
        return None
    t = translator(lang)
    parts = []
    if cf.get("ghz_pct") is not None:
        parts.append(t("export.gauge.compute_floor_clock", pct=f"{cf['ghz_pct']:.0f}"))
    if cf.get("perf_pct") is not None:
        parts.append(t("export.gauge.compute_floor_benchmark", pct=f"{cf['perf_pct']:.0f}"))
    detail = " ({})".format(", ".join(parts)) if parts else ""
    return t("export.gauge.compute_floor_sentence",
             coverage=f"{cf['coverage_pct']:.0f}", detail=detail,
             util=f"{cf['source_cpu_util_pct']:.0f}")


def _hatch(size, sign, c1, c2, period, lw):
    """A diagonal-stripe tile (c1 lines on a c2 ground). sign +1 = '/', -1 = '\\'."""
    w, h = size
    if w <= 0 or h <= 0:
        return Image.new("RGB", (max(w, 1), max(h, 1)), c2)
    layer = Image.new("RGB", size, c2)
    d = ImageDraw.Draw(layer)
    if sign >= 0:
        for off in range(-h, w + h, period):
            d.line([(off, h), (off + h, 0)], fill=c1, width=lw)
    else:
        for off in range(0, w + 2 * h, period):
            d.line([(off, 0), (off - h, h)], fill=c1, width=lw)
    return layer


def _bar(img, x, y, w, h, now, sized, ha, color):
    """Paint one stacked bar (rounded) onto img at (x, y)."""
    def px(p):
        return int(round(max(0, min(100, p)) / 100 * w))

    layer = Image.new("RGB", (w, h), TRACK)
    d = ImageDraw.Draw(layer)
    if now > 0:
        d.rectangle([0, 0, px(now), h], fill=color)                 # now (solid)
    if sized > now:
        x0, x1 = px(now), px(sized)
        layer.paste(_hatch((x1 - x0, h), +1, *RESERVE_HATCH, 7 * _SS, 3 * _SS), (x0, 0))
    if ha > 0:
        x0 = px(100 - ha)
        layer.paste(_hatch((w - x0, h), -1, *HA_HATCH, 7 * _SS, 3 * _SS), (x0, 0))

    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=h // 2, fill=255)
    img.paste(layer, (x, y), mask)
    # hairline around the whole pill
    ImageDraw.Draw(img).rounded_rectangle(
        [x, y, x + w - 1, y + h - 1], radius=h // 2, outline=HAIRLINE, width=max(1, round(1.2 * _SS)))


def _text(d, xy, s, font, fill, anchor="la"):
    d.text(xy, s, font=font, fill=fill, anchor=anchor)


def _swatch(img, d, x, y, kind, label, font):
    sz = 16 * _SS
    if kind == "now":
        d.rounded_rectangle([x, y, x + sz, y + sz], radius=3 * _SS, fill=NOW_LOW)
    else:
        sign, cols = (+1, RESERVE_HATCH) if kind == "growth" else (-1, HA_HATCH)
        tile = _hatch((sz, sz), sign, *cols, 7 * _SS, 3 * _SS)
        m = Image.new("L", (sz, sz), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, sz, sz], radius=3 * _SS, fill=255)
        img.paste(tile, (x, y), m)
    _text(d, (x + sz + 7 * _SS, y + sz // 2), label, font, MUTED, anchor="lm")
    l, t, r, b = d.textbbox((0, 0), label, font=font)
    return x + sz + 7 * _SS + (r - l) + 22 * _SS    # next swatch x


def render_util_bars(rows, limiting_key="", any_ha=True, lang="en"):
    """rows: list of dicts {label, now, sized, ha}. Returns PNG bytes for the
    whole 'Utilization vs full cluster' block (title, legend, bars)."""
    t = translator(lang)
    cjk = is_cjk(lang)

    # Display label for a resource: CPU/RAM are technical terms (verbatim);
    # Storage is prose (translated). Comparison to limiting_key uses the raw label.
    def res_label(key):
        return t("export.common.storage") if key == "Storage" else key

    W = 940 * _SS
    pad = 14 * _SS
    title_h = 30 * _SS
    row_h = 22 * _SS
    row_gap = 20 * _SS
    top = title_h + 14 * _SS
    H = top + len(rows) * (row_h + row_gap) + pad

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))   # transparent — sits on the slide gradient
    d = ImageDraw.Draw(img)

    f_title = _font("MartelSans-SemiBold.ttf", 17 * _SS, cjk)
    f_label = _font("MartelSans-SemiBold.ttf", 17 * _SS, cjk)
    f_pct = _font("MartelSans-Bold.ttf", 17 * _SS, cjk)
    f_pct2 = _font("MartelSans-SemiBold.ttf", 15 * _SS, cjk)
    f_leg = _font("MartelSans-Regular.ttf", 13 * _SS, cjk)
    f_badge = _font("MartelSans-Bold.ttf", 11 * _SS, cjk)

    # title (left) + legend (right)
    _text(d, (pad, title_h // 2), t("export.gauge.title"),
          f_title, TEXT, anchor="lm")
    lx = W - pad
    legend = [("now", t("export.gauge.legend_now"))]
    legend.append(("growth", t("export.gauge.legend_growth")))
    if any_ha:
        legend.append(("ha", t("export.gauge.legend_ha")))
    # measure from the right: lay out left-to-right but start far enough left
    total_w = 0
    for kind, lab in legend:
        l, _tb, r, b = d.textbbox((0, 0), lab, font=f_leg)
        total_w += 16 * _SS + 7 * _SS + (r - l) + 22 * _SS
    x = W - pad - total_w
    sy = (title_h - 16 * _SS) // 2
    for kind, lab in legend:
        x = _swatch(img, d, x, sy, kind, lab, f_leg)

    d = ImageDraw.Draw(img)  # refresh after pastes

    label_w = 150 * _SS
    pct_w = 150 * _SS
    bar_x = pad + label_w
    bar_w = W - pad - label_w - pct_w
    bar_h = row_h

    y = top
    for r in rows:
        now, sized, ha = r["now"], r["sized"], r.get("ha", 0)
        # label + optional LIMITING badge
        disp = res_label(r["label"])
        _text(d, (pad, y + bar_h // 2), disp, f_label, TEXT, anchor="lm")
        if r["label"] == limiting_key:
            l, tt, rr, b = d.textbbox((0, 0), disp, font=f_label)
            bx = pad + (rr - l) + 10 * _SS
            _badge(img, d, bx, y + bar_h // 2, t("export.gauge.limiting"), f_badge)
        # bar
        _bar(img, bar_x, y, bar_w, bar_h, now, sized, ha, now_color(now))
        d = ImageDraw.Draw(img)
        # pct: "35% / 57%"
        px = bar_x + bar_w + 16 * _SS
        _text(d, (px, y + bar_h // 2), f"{now}%", f_pct, TEXT, anchor="lm")
        l, _tb, rr, b = d.textbbox((0, 0), f"{now}%", font=f_pct)
        _text(d, (px + (rr - l) + 4 * _SS, y + bar_h // 2), f"/ {sized}%",
              f_pct2, MUTED, anchor="lm")
        y += row_h + row_gap

    img = img.resize((W // _SS, H // _SS), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _badge(img, d, x, cy, text, font):
    l, t, r, b = d.textbbox((0, 0), text, font=font)
    tw, th = r - l, b - t
    padx, pady = 6 * _SS, 4 * _SS
    d.rounded_rectangle([x, cy - th / 2 - pady, x + tw + 2 * padx, cy + th / 2 + pady],
                        radius=4 * _SS, outline=ORANGE, width=2 * _SS)
    _text(d, (x + padx, cy), text, font, ORANGE, anchor="lm")


if __name__ == "__main__":  # quick visual check
    rows = [{"label": "CPU", "now": 35, "sized": 57, "ha": 43},
            {"label": "RAM", "now": 38, "sized": 61, "ha": 39},
            {"label": "Storage", "now": 29, "sized": 63, "ha": 0}]
    png = render_util_bars(rows, limiting_key="CPU", any_ha=True)
    g = Image.open(io.BytesIO(png))
    bg = Image.new("RGB", g.size, (224, 230, 238))   # mock the slide's light gradient
    bg.paste(g, (0, 0), g)                            # composite using alpha
    bg.save("/tmp/gauge_sample.png")
    print("wrote /tmp/gauge_sample.png (transparent block on mock gradient)")
