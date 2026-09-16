#!/bin/bash
# Optional helper: start server + client from the repo root, then open REAPER.
# Uses this clone only. Does not change JACK, PipeWire, quantum, or .env.

set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "=== X-Air Network Bridge autostart ==="

if [[ -f "$ROOT/.venv/bin/activate" ]]; then
  echo "[1/5] Activating virtualenv..."
  # shellcheck disable=SC1091
  source "$ROOT/.venv/bin/activate"
else
  echo "[1/5] No .venv found; using python3 on PATH"
fi

echo "[2/5] Starting xair_network_bridge_server..."
PYTHONPATH="$ROOT" python3 -m src.cli xair_network_bridge_server &
PID_SERVER=$!
sleep 1

echo "[3/5] Starting xair_network_bridge_client..."
PYTHONPATH="$ROOT" python3 -m src.cli xair_network_bridge_client &
PID_CLIENT=$!
sleep 2

echo "[4/5] Waiting for JACK/PipeWire client xair_net_bridge..."
for _ in {1..10}; do
  if command -v pactl >/dev/null 2>&1 && pactl list short sources 2>/dev/null | grep -q xair_net_bridge; then
    echo "xair_net_bridge is visible."
    break
  fi
  sleep 1
done

echo "[5/5] Opening REAPER if installed..."
if command -v reaper >/dev/null 2>&1; then
  reaper &
else
  echo "REAPER not on PATH; open it yourself and load templates/xair_network_bridge.RPP"
fi

echo "=== Started ==="
echo "Server PID: $PID_SERVER"
echo "Client PID: $PID_CLIENT"
