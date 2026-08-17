#!/usr/bin/env python3
"""Re-download the self-hosted Raleway webfont into app/static/fonts/.

The UI ships Raleway locally rather than linking Google Fonts, because we
deploy onto self-hosted infrastructure that can't be assumed to reach a CDN.
This script fetches the current version and prints @font-face rules with the
URLs already rewritten to our local paths — paste those over the block at the
top of app/static/css/style.css.

Only latin and latin-ext are kept: Raleway has no CJK glyphs, so Japanese
falls through to the system stack declared in `body`. Shipping the cyrillic /
greek / vietnamese subsets would add weight no supported locale can use.

    python3 tools/fetch_fonts.py
"""

import os
import re
import urllib.request

# Weight range must cover every weight the stylesheet asks for. Raleway is a
# variable font, so 400..700 is one file per subset rather than one per weight.
CSS_URL = (
    "https://fonts.googleapis.com/css2"
    "?family=Raleway:ital,wght@0,400..700;1,400..700&display=swap"
)
# Google serves woff2 only to browser user-agents; a default urllib UA gets
# the ttf fallback stylesheet instead.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
KEEP_SUBSETS = {"latin", "latin-ext"}
OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "app", "static", "fonts",
)


def _download(url, attempts=4):
    """Fetch a URL whole, retrying short reads.

    gstatic occasionally closes the connection early; a truncated woff2 is
    accepted by the filesystem and rejected by the browser, so verify the body
    against Content-Length rather than trusting a clean-looking write.
    """
    last = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req) as resp:
                expected = resp.headers.get("Content-Length")
                data = resp.read()
            if expected is not None and len(data) != int(expected):
                raise IOError(f"short read: {len(data)} of {expected} bytes")
            return data
        except (IOError, OSError) as exc:
            last = exc
            print(f"  retry {attempt}/{attempts} ({exc})")
    raise SystemExit(f"could not download {url}: {last}")


def main():
    os.makedirs(OUT_DIR, exist_ok=True)

    req = urllib.request.Request(CSS_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req) as resp:
        css = resp.read().decode("utf-8")

    # Google emits each @font-face preceded by a `/* subset */` comment, which
    # is the only place the subset name appears.
    blocks = re.findall(r"/\* (\S+) \*/\s*(@font-face \{.*?\})", css, re.S)
    if not blocks:
        raise SystemExit("no @font-face blocks found — did the CSS format change?")

    rules = []
    for subset, block in blocks:
        if subset not in KEEP_SUBSETS:
            continue
        url = re.search(r"url\((https://[^)]+)\)", block).group(1)
        style = re.search(r"font-style: (\w+)", block).group(1)
        name = f"raleway-{subset}-{style}.woff2"
        dest = os.path.join(OUT_DIR, name)
        data = _download(url)
        # Only touch the existing file once the whole body is in hand — a
        # short read part-way through would otherwise leave a truncated woff2
        # on disk, which browsers reject silently and the UI loses its font.
        with open(dest, "wb") as fh:
            fh.write(data)
        print(f"  saved {name} ({len(data):,} bytes)")
        rules.append(f"/* {subset} */\n" + block.replace(url, f"../fonts/{name}"))

    print("\n--- paste into app/static/css/style.css ---\n")
    print("\n".join(rules))


if __name__ == "__main__":
    main()
