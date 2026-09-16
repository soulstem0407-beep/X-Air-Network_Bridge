#!/usr/bin/env bash
# Official OSC launcher: X18 ↔ X-Air Network Bridge ↔ REAPER.
# No toca UDP/jitter/FEC/Opus ni xair_network_bridge_server.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  exec "$ROOT/.venv/bin/python" -m src.cli osc-launcher "$@"
fi
exec python3 -m src.cli osc-launcher "$@"
