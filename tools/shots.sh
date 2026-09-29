#!/bin/sh
# Screenshot the running sizer into docs/shots/ for UI review.
#
# Claude can read the PNGs once they exist, so this gives it a quick visual
# check of the main pages.
#
#   tools/shots.sh [base-url]
#
# Default base-url is the local dev server on :5101. Uses Playwright's
# chrome-headless-shell (~/.cache/ms-playwright) when installed; otherwise the
# binary for this platform is fetched on first run into .tools/ (gitignored,
# ~200MB).

set -e
BASE="${1:-http://127.0.0.1:5101}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/docs/shots"

case "$(uname -s)-$(uname -m)" in
    Darwin-arm64) PLAT=mac-arm64 ;;
    Darwin-*)     PLAT=mac-x64 ;;
    *)            PLAT=linux64 ;;
esac

CHS=""
for c in "$HOME"/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-"$PLAT"/chrome-headless-shell; do
    [ -x "$c" ] && CHS="$c"
done

if [ -z "$CHS" ]; then
    CHS="$ROOT/.tools/chrome-headless-shell-$PLAT/chrome-headless-shell"
fi

if [ ! -x "$CHS" ]; then
    echo "Fetching chrome-headless-shell ($PLAT)..."
    mkdir -p "$ROOT/.tools"
    VER=$(curl -s "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions.json" \
          | python3 -c "import json,sys; print(json.load(sys.stdin)['channels']['Stable']['version'])")
    curl -sL -o "$ROOT/.tools/chs.zip" \
        "https://storage.googleapis.com/chrome-for-testing-public/$VER/$PLAT/chrome-headless-shell-$PLAT.zip"
    (cd "$ROOT/.tools" && unzip -q -o chs.zip && rm chs.zip)
    chmod +x "$CHS"
fi

mkdir -p "$OUT"

shot() {
    name="$1"; path="$2"; height="${3:-1000}"
    "$CHS" --headless --disable-gpu --no-sandbox --hide-scrollbars \
        --force-device-scale-factor=1 \
        --window-size="1440,$height" \
        --virtual-time-budget=6000 \
        --screenshot="$OUT/$name.png" \
        "$BASE$path" >/dev/null 2>&1 || true
    if [ -f "$OUT/$name.png" ]; then
        echo "  $name.png"
    else
        echo "  $name.png FAILED"
    fi
}

echo "Shooting $BASE -> docs/shots/"
shot home / 1000
shot privacy /privacy 1400

echo
echo "Done. Point Claude at docs/shots/ and it can read them."
