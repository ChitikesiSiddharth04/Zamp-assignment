#!/usr/bin/env bash
# Start the AP process. Open http://127.0.0.1:8080
set -e
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
[ -f data/master/vendors.csv ] || .venv/bin/python scripts/make_samples.py
exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080 "$@"
