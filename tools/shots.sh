#!/bin/sh
# Screenshot the running sizer into docs/shots/ for UI review.
#
# Exists because Claude Code's sandbox denies Chrome the Mach-port bootstrap it
# needs to start, so it can't take these itself — but it CAN read the PNGs once
# they exist. Run this yourself and the screenshots become reviewable.
#
#   tools/shots.sh [base-url]
#
# Default base-url is the local dev server on :5101. The browser binary is
# fetched on first run into .tools/ (gitignored, ~200MB).

set -e
BASE="${1:-http://127.0.0.1:5101}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/docs/shots"
CHS="$ROOT/.tools/chrome-headless-shell-mac-arm64/chrome-headless-shell"

if [ ! -x "$CHS" ]; then
    echo "Fetching chrome-headless-shell..."
    mkdir -p "$ROOT/.tools"
    VER=$(curl -s "https://googlechromelabs.github.io/chrome-for-testing/last-known-good-versions.json" \
          | python3 -c "import json,sys; print(json.load(sys.stdin)['channels']['Stable']['version'])")
    curl -sL -o "$ROOT/.tools/chs.zip" \
        "https://storage.googleapis.com/chrome-for-testing-public/$VER/mac-arm64/chrome-headless-shell-mac-arm64.zip"
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
