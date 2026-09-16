#!/bin/bash
# macOS double-click launcher for Bitwig + X-Air Network Bridge.
# Detects CoreAudio. Does not change JACK/PipeWire/quantum/HDMI or user configs.
# Keep this window open so you can read the result.

cd "$(dirname "$0")" || exit 1
ROOT="$(cd ../.. && pwd)"
cd "$ROOT" || exit 1

echo "macOS / CoreAudio — X-Air Network Bridge Bitwig setup"
echo "Project: $ROOT"
echo

bash "$ROOT/launchers/xair_network_bridge/xair_network_bridge_bitwig_setup.sh"
status=$?

echo
echo "Press Return to close this window."
read -r _
exit "$status"
