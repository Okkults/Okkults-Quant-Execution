#!/usr/bin/env bash
# Okkults Quant Terminal — one-command launcher for macOS/Linux.
set -euo pipefail
cd "$(dirname "$0")"

PY=python3
if command -v python3.11 >/dev/null 2>&1; then
  PY=python3.11
fi

if [ ! -x ".venv/bin/python" ] && [ ! -x ".venv/Scripts/python.exe" ]; then
  echo "Creating .venv ($PY)..."
  "$PY" -m venv .venv
fi

if [ -x ".venv/bin/python" ]; then
  PYPATH=".venv/bin/python"
else
  PYPATH=".venv/Scripts/python.exe"
fi

echo "Installing dependencies (first run only, quiet updates later)..."
"$PYPATH" -m pip install -q -r requirements.txt

echo "Launching Okkults Quant Terminal..."
exec "$PYPATH" main.py "$@"
