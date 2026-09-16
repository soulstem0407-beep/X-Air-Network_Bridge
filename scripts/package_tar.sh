#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"
PY="$ROOT/.venv/bin/python3"
if [[ ! -x "$PY" ]]; then
  PY="${XAIR_PYTHON:-python3}"
fi
exec "$PY" -m src.cli package-tar "$@"
