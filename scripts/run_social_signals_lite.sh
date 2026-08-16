#!/usr/bin/env bash
# Start the Fieldnotes-compatible social-signals lite service on :8899.
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${SOCIAL_SIGNALS_PORT:-8899}"
HOST="${SOCIAL_SIGNALS_HOST:-127.0.0.1}"
KEY="${SOCIAL_SIGNALS_API_KEY:-demo-key}"
LOG="${SOCIAL_SIGNALS_LOG:-/tmp/social-signals-lite.log}"
PIDFILE="${SOCIAL_SIGNALS_PID:-/tmp/social-signals-lite.pid}"

# Prefer an explicit interpreter, then field-note .venv (fastapi + playwright).
if [ -n "${SOCIAL_SIGNALS_PYTHON:-}" ] && [ -x "$SOCIAL_SIGNALS_PYTHON" ]; then
  PY="$SOCIAL_SIGNALS_PYTHON"
elif [ -x "$HERE/.venv/bin/python" ]; then
  PY="$HERE/.venv/bin/python"
else
  PY="$(command -v python3)"
fi

export SOCIAL_SIGNALS_API_KEY="$KEY"
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
# Faster Reddit pacing for demos (override freely).
export PAGE_PAUSE="${PAGE_PAUSE:-4}"
export SCROLL_PAUSE="${SCROLL_PAUSE:-2}"

if curl -sf "http://${HOST}:${PORT}/health" >/dev/null 2>&1; then
  echo "social-signals already healthy on ${HOST}:${PORT}"
  curl -s "http://${HOST}:${PORT}/health"
  echo
  exit 0
fi

"$PY" -c 'import fastapi, uvicorn, httpx' >/dev/null
"$PY" -c 'from playwright.sync_api import sync_playwright' >/dev/null

echo "Starting social-signals-lite on ${HOST}:${PORT} (py=$PY)"
nohup "$PY" -m uvicorn social_signals_lite.app:app --host "$HOST" --port "$PORT" \
  >"$LOG" 2>&1 &
echo $! >"$PIDFILE"

for _ in $(seq 1 40); do
  if curl -sf "http://${HOST}:${PORT}/health" >/dev/null 2>&1; then
    curl -s "http://${HOST}:${PORT}/health"
    echo
    echo "log: $LOG  pid: $(cat "$PIDFILE")"
    exit 0
  fi
  sleep 0.25
done

echo "failed to start — last log lines:" >&2
tail -30 "$LOG" >&2 || true
exit 1
