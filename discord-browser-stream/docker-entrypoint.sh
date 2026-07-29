#!/bin/sh
set -eu

Xvfb "${DISPLAY}" -screen 0 \
  "${BROWSER_STREAM_WIDTH:-1280}x${BROWSER_STREAM_HEIGHT:-720}x24" \
  -nolisten tcp &
xvfb_pid=$!

cleanup() {
  kill "$xvfb_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

exec node src/index.js
