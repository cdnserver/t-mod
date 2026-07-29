#!/bin/sh
set -u

display="${DISPLAY:-:99}"
display_number="${display#:}"
display_number="${display_number%%.*}"
socket="/tmp/.X11-unix/X${display_number}"

Xvfb "$display" -screen 0 \
  "${BROWSER_STREAM_WIDTH:-1280}x${BROWSER_STREAM_HEIGHT:-720}x24" \
  -nolisten tcp -noreset &
xvfb_pid=$!
node_pid=

cleanup() {
  if [ -n "$node_pid" ]; then
    kill "$node_pid" 2>/dev/null || true
  fi
  kill "$xvfb_pid" 2>/dev/null || true
  if [ -n "$node_pid" ]; then
    wait "$node_pid" 2>/dev/null || true
  fi
  wait "$xvfb_pid" 2>/dev/null || true
}
trap 'cleanup; exit 0' INT TERM

attempt=0
while [ ! -S "$socket" ]; do
  if ! kill -0 "$xvfb_pid" 2>/dev/null; then
    echo "[FATAL] Xvfb exited before display $display became ready" >&2
    wait "$xvfb_pid" 2>/dev/null || true
    exit 1
  fi
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 100 ]; then
    echo "[FATAL] Timed out waiting for Xvfb display $display" >&2
    cleanup
    exit 1
  fi
  sleep 0.05
done

node src/index.js &
node_pid=$!

while kill -0 "$node_pid" 2>/dev/null && kill -0 "$xvfb_pid" 2>/dev/null; do
  sleep 1
done

if ! kill -0 "$xvfb_pid" 2>/dev/null; then
  echo "[FATAL] Xvfb stopped; terminating the service for Docker restart" >&2
  kill "$node_pid" 2>/dev/null || true
  wait "$node_pid" 2>/dev/null || true
  wait "$xvfb_pid" 2>/dev/null || true
  exit 1
fi

wait "$node_pid"
status=$?
kill "$xvfb_pid" 2>/dev/null || true
wait "$xvfb_pid" 2>/dev/null || true
exit "$status"
