#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"
exec "$ROOT/.venv/bin/python" -m src.cli xair_network_bridge_client "$@"
