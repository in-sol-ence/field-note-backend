#!/usr/bin/env bash
# Stable field-note API for console scrapes.
# No --reload: live x-scraper writes (and long Playwright runs) used to kill
# in-flight /scrape/x when WatchFiles restarted the worker mid-response.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck disable=SC1091
source .venv/bin/activate
exec uvicorn main:app --host 127.0.0.1 --port 8000
