#!/usr/bin/env bash
# Official Bitwig setup launcher (Linux). macOS: xair_network_bridge_bitwig_setup.command
# Windows: xair_network_bridge_bitwig_setup.ps1
#
# Writes project-local template + OSC controller script. Does NOT change
# quantum, PipeWire, JACK, sample rate, buffers, HDMI, Bitwig user dir, or .env.
# GET-only OSC to the X18. No jack_connect / jack_disconnect.
#
# Usage: bash launchers/xair_network_bridge/xair_network_bridge_bitwig_setup.sh

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CLIENT_NAME="xair_net_bridge"
CHANNELS=18
OSC_PORT_DEFAULT=10024
DAW_LISTEN=8000
BRIDGE_LISTEN=9001

if [[ -t 1 ]]; then
  C_OK=$'\033[1;32m'; C_WN=$'\033[1;33m'; C_ER=$'\033[1;31m'
  C_HD=$'\033[1;36m'; C_DM=$'\033[2m'; C_RS=$'\033[0m'
else
  C_OK=""; C_WN=""; C_ER=""; C_HD=""; C_DM=""; C_RS=""
fi

have() { command -v "$1" >/dev/null 2>&1; }

run_to() {
  local sec="${1:-2}"; shift
  if have timeout; then timeout -k 1 "$sec" "$@"
  elif have gtimeout; then gtimeout -k 1 "$sec" "$@"
  else "$@"; fi
}

say() { printf '%s\n' "$*"; }

die() { printf '%sERROR%s  %s\n' "$C_ER" "$C_RS" "$*" >&2; exit 2; }

env_unquote() {
  local v="$1"
  v="${v%"${v##*[![:space:]]}"}"; v="${v#"${v%%[![:space:]]*}"}"
  if [[ "$v" == \"*\" ]]; then v="${v#\"}"; v="${v%\"}"
  elif [[ "$v" == \'*\' ]]; then v="${v#\'}"; v="${v%\'}"
  else v="${v%%#*}"; v="${v%"${v##*[![:space:]]}"}"
  fi
  printf '%s' "$v"
}

load_dotenv() {
  local f="$1" line k v
  [[ -f "$f" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    [[ "$line" =~ ^[[:space:]]*$ || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    k="${BASH_REMATCH[1]}"; v="$(env_unquote "${BASH_REMATCH[2]}")"
    if [[ -z "${!k+x}" ]]; then printf -v "$k" '%s' "$v"; fi
  done < "$f"
}

cfg() { local k="$1" d="${2:-}"; local v="${!k-}"; printf '%s' "${v:-$d}"; }

sanitize() {
  local s="$1"
  s="$(printf '%s' "$s" | tr -cd 'A-Za-z0-9_-' | cut -c1-40)"
  [[ -n "$s" ]] || s="$2"
  printf '%s' "$s"
}

xml_esc() {
  local s="$1"
  s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"; s="${s//\"/&quot;}"
  printf '%s' "$s"
}

osc_pad_str() {
  local s="$1"
  local total=$(( ${#s} + 1 ))
  local pad=$(( (4 - total % 4) % 4 ))
  printf '%s\0' "$s"
  if (( pad > 0 )); then dd if=/dev/zero bs=1 count="$pad" 2>/dev/null; fi
}

udp_send_recv() {
  local host="$1" port="$2" infile="$3" outfile="$4" wait_s="${5:-2}"
  : > "$outfile"
  if have timeout && have socat; then
    timeout "$wait_s" socat -T"$wait_s" - UDP:"${host}:${port}" < "$infile" > "$outfile" 2>/dev/null && [[ -s "$outfile" ]] && return 0
  fi
  if have timeout && have nc; then
    timeout "$wait_s" nc -u -w "$wait_s" "$host" "$port" < "$infile" > "$outfile" 2>/dev/null && [[ -s "$outfile" ]] && return 0
  fi
  if have timeout; then
    timeout "$wait_s" bash -c '
      exec 3<>/dev/udp/'"$host"'/'"$port"' || exit 1
      cat "'"$infile"'" >&3
      dd bs=65535 count=1 of="'"$outfile"'" <&3 2>/dev/null
    ' 2>/dev/null && [[ -s "$outfile" ]] && return 0
  fi
  return 1
}

osc_query() {
  local host="$1" port="$2" addr="$3" out="$4"
  { osc_pad_str "$addr"; osc_pad_str ","; } > "$TMP/osc_q.bin"
  udp_send_recv "$host" "$port" "$TMP/osc_q.bin" "$out" 2
}

jack_list() {
  if have jack_lsp; then run_to 2 jack_lsp 2>/dev/null && return 0; fi
  if have pw-jack; then run_to 2 pw-jack jack_lsp 2>/dev/null && return 0; fi
  if have pw-link; then run_to 2 pw-link 2>/dev/null && return 0; fi
  return 1
}

find_bitwig() {
  local c
  for c in bitwig-studio bitwig BitwigStudio; do
    if have "$c"; then command -v "$c"; return 0; fi
  done
  for c in \
    /opt/bitwig-studio/bitwig-studio \
    /usr/bin/bitwig-studio \
    /usr/local/bin/bitwig-studio \
    "/Applications/Bitwig Studio.app/Contents/MacOS/BitwigStudio"
  do
    [[ -x "$c" ]] && { printf '%s' "$c"; return 0; }
  done
  return 1
}

pack_zip() {
  local zipfile="$1" srcdir="$2"
  ( cd "$srcdir" && zip -q -r "$zipfile" . ) && return 0
  if have python3; then
    python3 - "$srcdir" "$zipfile" <<'PY'
import os, sys, zipfile
root, dest = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
    for dirpath, _, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            z.write(p, os.path.relpath(p, root))
PY
    return 0
  fi
  if have python; then
    python - "$srcdir" "$zipfile" <<'PY'
import os, sys, zipfile
root, dest = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
    for dirpath, _, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            z.write(p, os.path.relpath(p, root))
PY
    return 0
  fi
  return 1
}

# --- start ------------------------------------------------------------------

load_dotenv "$ROOT/.env"
CLIENT_NAME="$(cfg XAIR_JACK_CLIENT_NAME "$CLIENT_NAME")"
OSC_HOST="$(cfg XAIR_OSC_HOST "")"
[[ -z "$OSC_HOST" ]] && OSC_HOST="$(cfg XAIR_OSC_IP "")"
OSC_PORT="$(cfg XAIR_OSC_PORT "$OSC_PORT_DEFAULT")"
DAW_LISTEN="$(cfg XAIR_REAPER_PORT "$DAW_LISTEN")"
BRIDGE_LISTEN="$(cfg XAIR_REAPER_LISTEN_PORT "$BRIDGE_LISTEN")"
SR="$(cfg XAIR_SAMPLE_RATE 48000)"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/xair-bitwig-setup.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

OS_NAME="$(uname -s 2>/dev/null || echo unknown)"
AUDIO_BACKEND="unknown"
case "$OS_NAME" in
  Linux) AUDIO_BACKEND="JACK/PipeWire" ;;
  Darwin) AUDIO_BACKEND="CoreAudio" ;;
  MINGW*|MSYS*|CYGWIN*) AUDIO_BACKEND="WASAPI/ASIO" ;;
esac

say "${C_HD}X-Air Network Bridge — Bitwig setup (project files only)${C_RS}"
say "${C_DM}OS: ${OS_NAME}  audio backend: ${AUDIO_BACKEND}${C_RS}"
say "${C_DM}Does not change JACK / PipeWire / quantum / HDMI / ~/Bitwig Studio${C_RS}"

BITWIG_BIN=""
BITWIG_BIN="$(find_bitwig)" || BITWIG_BIN=""
if [[ -z "$BITWIG_BIN" ]]; then
  die "Bitwig is not installed (PATH, /opt, or /Applications/Bitwig Studio.app)"
fi
say "  Bitwig: $BITWIG_BIN"

ports_ok=0
case "$OS_NAME" in
  Linux)
    pw_ok=0; jack_ok=0
    pgrep -x pipewire >/dev/null 2>&1 && pw_ok=1
    pgrep -x jackdbus >/dev/null 2>&1 && jack_ok=1
    pgrep -x jackd >/dev/null 2>&1 && jack_ok=1
    if have pactl && run_to 2 pactl info 2>/dev/null | grep -qi pipewire; then pw_ok=1; fi
    if (( pw_ok || jack_ok )); then
      say "  Linux audio: JACK/PipeWire active"
    else
      printf '  %sWARN%s  JACK/PipeWire not detected (template still written)\n' "$C_WN" "$C_RS"
    fi
    JACK_TXT="$TMP/jack.txt"
    n_in=0; n_out=0
    if jack_list > "$JACK_TXT"; then
      n_in="$(grep -c -E "^${CLIENT_NAME}:.*_in$" "$JACK_TXT" 2>/dev/null || true)"
      n_out="$(grep -c -E "^${CLIENT_NAME}:.*_out$" "$JACK_TXT" 2>/dev/null || true)"
      n_in="${n_in:-0}"; n_out="${n_out:-0}"
    fi
    if (( n_in == CHANNELS && n_out == CHANNELS )); then
      ports_ok=1
      say "  Bridge ports: ${CLIENT_NAME} ${n_in} in + ${n_out} out"
    else
      printf '  %sWARN%s  expected %s: 18× *_in + 18× *_out (have in=%s out=%s). Is xair_network_bridge_client running?\n' \
        "$C_WN" "$C_RS" "$CLIENT_NAME" "$n_in" "$n_out"
    fi
    ;;
  Darwin)
    if pgrep -x coreaudiod >/dev/null 2>&1; then
      say "  macOS audio: CoreAudio (coreaudiod running)"
    else
      printf '  %sWARN%s  coreaudiod not seen\n' "$C_WN" "$C_RS"
    fi
    if ls /Library/Audio/Plug-Ins/HAL/*BlackHole* >/dev/null 2>&1 \
      || ls "$HOME/Library/Audio/Plug-Ins/HAL/"*BlackHole* >/dev/null 2>&1; then
      ports_ok=1
      say "  Bridge ports: BlackHole CoreAudio driver present"
    else
      printf '  %sWARN%s  BlackHole not found. Bitwig device = BlackHole. Not installing from here.\n' \
        "$C_WN" "$C_RS"
    fi
    ;;
  *)
    printf '  %sWARN%s  use xair_network_bridge_bitwig_setup.ps1 on Windows for WASAPI/ASIO\n' "$C_WN" "$C_RS"
    ;;
esac

NAMES=()
i=1
while (( i <= CHANNELS )); do
  NAMES[i]="CH$(printf '%02d' "$i")"
  i=$((i + 1))
done
osc_ok=0
if [[ -n "$OSC_HOST" && "$OSC_HOST" != "auto" ]]; then
  if osc_query "$OSC_HOST" "$OSC_PORT" "/status" "$TMP/st.bin" && grep -a -q '/status' "$TMP/st.bin"; then
    osc_ok=1
    say "  OSC handshake: ${OSC_HOST}:${OSC_PORT} /status OK"
    i=1
    while (( i <= CHANNELS )); do
      ii="$(printf '%02d' "$i")"
      if osc_query "$OSC_HOST" "$OSC_PORT" "/ch/${ii}/config/name" "$TMP/nm.bin" \
        && grep -a -q "/ch/${ii}/config/name" "$TMP/nm.bin"; then
        raw="$(tr -d '\0' < "$TMP/nm.bin")"
        raw="${raw#*",s"}"
        raw="$(sanitize "$raw" "CH${ii}")"
        NAMES[i]="$raw"
      fi
      i=$((i + 1))
    done
    i=1
    while (( i <= CHANNELS )); do
      base="${NAMES[$i]}"
      name="$base"
      n=2
      j=1
      while (( j < i )); do
        if [[ "${NAMES[$j]}" == "$name" ]]; then
          name="${base}_${n}"
          n=$((n + 1))
          j=1
          continue
        fi
        j=$((j + 1))
      done
      NAMES[i]="$name"
      i=$((i + 1))
    done
  else
    printf '  %sWARN%s  no /status from %s:%s — using CH01…CH18 names\n' \
      "$C_WN" "$C_RS" "${OSC_HOST:-unset}" "$OSC_PORT"
  fi
else
  printf '  %sWARN%s  XAIR_OSC_HOST not set — using CH01…CH18 names\n' "$C_WN" "$C_RS"
fi

mkdir -p "$ROOT/templates" "$ROOT/bitwig-osc" "$TMP/bw"

# --- Bitwig OSC controller (repo only; not ~/Bitwig Studio) -----------------
JS="$ROOT/bitwig-osc/xair_network_bridge.control.js"
cat > "$JS" <<EOF
// X-Air Network Bridge — Bitwig controller script
// Same OSC surface as REAPER so start-reaper-sync / osc-launcher work.
// Load: Bitwig Settings → Controllers → Add → Hardware → Script
//       point at this file. Do not copy into ~/Bitwig Studio from this launcher
//       (user config is left untouched).
//
// JACK: ${CLIENT_NAME}:NN_*_out = Bitwig inputs (dry from X18)
//       ${CLIENT_NAME}:NN_*_in  = Bitwig outputs (duplex graph)
// Plugins: always on the OUTPUT chain / post-fader send to USB Return.

loadAPI(17);

host.defineController(
  "X-Air Network Bridge",
  "X-Air Network Bridge OSC",
  "1.0.0",
  "c0ffee00-18a1-4b17-9e00-000000000018",
  "X-Air"
);
host.defineMidiPorts(0, 0);

var TRACKS = ${CHANNELS};
var OSC_OUT_HOST = "127.0.0.1";
var OSC_OUT_PORT = ${BRIDGE_LISTEN};
var OSC_IN_PORT = ${DAW_LISTEN};

function init() {
  println("X-Air Network Bridge OSC init  in=" + OSC_IN_PORT + "  out=" + OSC_OUT_HOST + ":" + OSC_OUT_PORT);

  var osc = host.getOscModule();
  var space = osc.createAddressSpace();
  space.setShouldConsumeEvents(false);

  var bank = host.createMainTrackBank(TRACKS, 1, 0);
  var i;
  for (i = 0; i < TRACKS; i++) {
    (function (idx) {
      var t = bank.getItemAt(idx);
      t.name().markInterested();
      t.volume().markInterested();
      t.pan().markInterested();
      t.mute().markInterested();
    })(i);
  }

  space.registerMethod("/track/{n}/volume", "*", function (source, path, types, args) {
    var n = parseInt(path.split("/")[2], 10);
    if (n >= 1 && n <= TRACKS && args.length) {
      bank.getItemAt(n - 1).volume().set(args[0], 1);
    }
  });
  space.registerMethod("/track/{n}/pan", "*", function (source, path, types, args) {
    var n = parseInt(path.split("/")[2], 10);
    if (n >= 1 && n <= TRACKS && args.length) {
      bank.getItemAt(n - 1).pan().set(args[0], 1);
    }
  });
  space.registerMethod("/track/{n}/mute", "*", function (source, path, types, args) {
    var n = parseInt(path.split("/")[2], 10);
    if (n >= 1 && n <= TRACKS && args.length) {
      bank.getItemAt(n - 1).mute().set(!!args[0]);
    }
  });
  space.registerMethod("/track/{n}/name", "*", function (source, path, types, args) {
    var n = parseInt(path.split("/")[2], 10);
    if (n >= 1 && n <= TRACKS && args.length) {
      bank.getItemAt(n - 1).name().set(String(args[0]));
    }
  });

  try {
    osc.createUdpServer(OSC_IN_PORT, space);
    println("X-Air Network Bridge OSC listening UDP " + OSC_IN_PORT);
  } catch (e) {
    println("X-Air Network Bridge: could not bind OSC " + OSC_IN_PORT + " (" + e + ")");
  }

  try {
    osc.connectToUdpServer(OSC_OUT_HOST, OSC_OUT_PORT, space);
    println("X-Air Network Bridge OSC TX " + OSC_OUT_HOST + ":" + OSC_OUT_PORT);
  } catch (e) {
    println("X-Air Network Bridge: OSC TX failed (" + e + ")");
  }

  println("USB Return: post-fader send from stems to a stereo FX track; plugins on OUTPUT only.");
}

function flush() {}
function exit() {
  println("X-Air Network Bridge OSC exit");
}
EOF
say "  wrote $JS"

# --- Bitwig project container (18 tracks + USB return) ----------------------
# Native .bwproject layout is version-private. We ship a DAWproject XML +
# routing map inside the .bwproject zip so Bitwig 5+ can Import, without
# writing anything under ~/Bitwig Studio.

{
  printf '%s\n' '{'
  printf '%s\n' "  \"name\": \"X-Air Network Bridge\","
  printf '%s\n' "  \"jack_client\": \"${CLIENT_NAME}\","
  printf '%s\n' "  \"sample_rate\": ${SR},"
  printf '%s\n' "  \"tracks\": ["
  i=1
  while (( i <= CHANNELS )); do
    ii="$(printf '%02d' "$i")"
    stem="${ii}_${NAMES[$i]}"
    comma=","; (( i == CHANNELS )) && comma=""
    printf '    {"index": %s, "name": "%s", "input": "%s:%s_out", "output": "%s:%s_in", "send": "USB_Return", "send_mode": "post-fader"}%s\n' \
      "$i" "$stem" "$CLIENT_NAME" "$stem" "$CLIENT_NAME" "$stem" "$comma"
    i=$((i + 1))
  done
  printf '%s\n' "  ],"
  printf '%s\n' "  \"usb_return\": {"
  printf '%s\n' "    \"name\": \"USB_Return\","
  printf '%s\n' "    \"channels\": 2,"
  printf '%s\n' "    \"plugins\": \"output chain only\","
  printf '%s\n' "    \"note\": \"Capture this stereo bus with XAIR_RETURN_INPUT_DEVICE\""
  printf '%s\n' "  }"
  printf '%s\n' '}'
} > "$TMP/bw/routing.json"

{
  printf '%s\n' '<?xml version="1.0" encoding="UTF-8"?>'
  printf '%s\n' '<Project version="1.0">'
  printf '%s\n' '  <Application name="X-Air Network Bridge" version="1.0"/>'
  printf '%s\n' "  <Transport><Tempo value=\"120\" unit=\"bpm\"/></Transport>"
  printf '%s\n' '  <Structure>'
  i=1
  while (( i <= CHANNELS )); do
    ii="$(printf '%02d' "$i")"
    stem="$(xml_esc "${ii}_${NAMES[$i]}")"
    printf '    <Track name="%s" contentType="audio" loaded="true">\n' "$stem"
    printf '      <Channel audioChannels="1" role="regular"/>\n'
    printf '      <Sends><Send destination="USB_Return" type="post" volume="0"/></Sends>\n'
    printf '    </Track>\n'
    i=$((i + 1))
  done
  printf '%s\n' '    <Track name="USB_Return" contentType="audio" loaded="true">'
  printf '%s\n' '      <Channel audioChannels="2" role="effect"/>'
  printf '%s\n' '    </Track>'
  printf '%s\n' '  </Structure>'
  printf '%s\n' '</Project>'
} > "$TMP/bw/project.xml"

cat > "$TMP/bw/XAIR_NETWORK_BRIDGE.txt" <<EOF
X-Air Network Bridge — Bitwig template
JACK client: ${CLIENT_NAME}
Inputs  (dry from X18): ${CLIENT_NAME}:NN_*_out
Outputs (duplex):       ${CLIENT_NAME}:NN_*_in
USB Return: stereo FX track, POST-fader sends, plugins on OUTPUT only.
OSC script: bitwig-osc/xair_network_bridge.control.js
Listen ${DAW_LISTEN} → dest 127.0.0.1:${BRIDGE_LISTEN}
Audio device in Bitwig: ${AUDIO_BACKEND} (Linux JACK / Windows WASAPI-ASIO / macOS CoreAudio). This launcher does not change that.
EOF

BW="$ROOT/templates/xair_network_bridge.bwproject"
rm -f "$BW"
if pack_zip "$BW" "$TMP/bw"; then
  say "  wrote $BW  (18 stems + USB Return routing map)"
else
  mkdir -p "$ROOT/templates/xair_network_bridge.bwproject.d"
  cp -a "$TMP/bw/." "$ROOT/templates/xair_network_bridge.bwproject.d/"
  printf '  %sWARN%s  zip not available; wrote templates/xair_network_bridge.bwproject.d/\n' "$C_WN" "$C_RS"
fi

if [[ -n "${DISPLAY-}${WAYLAND_DISPLAY-}" ]] && ! pgrep -i bitwig >/dev/null 2>&1; then
  if [[ -f "$BW" ]]; then
    nohup "$BITWIG_BIN" "$BW" >/dev/null 2>&1 &
    say "  launched Bitwig with the template (existing session was not touched)"
  fi
elif [[ "$OS_NAME" == "Darwin" ]] && ! pgrep -i bitwig >/dev/null 2>&1; then
  open -a "Bitwig Studio" "$BW" >/dev/null 2>&1 && say "  launched Bitwig with the template" || true
elif pgrep -i bitwig >/dev/null 2>&1; then
  say "  Bitwig already running — File → Open ${BW} (session not restarted)"
fi

say "  Bitwig audio: Settings → Audio → ${AUDIO_BACKEND}. Not written by this script."
say "  Controller: Settings → Controllers → add ${JS}"

say ""
if (( ports_ok && osc_ok )); then
  printf '%sBITWIG READY — X-Air Network Bridge configurado correctamente.%s\n' "$C_OK" "$C_RS"
  exit 0
fi
printf '%sBitwig template written, but not fully READY%s (bridge ports=%s OSC=%s backend=%s).\n' \
  "$C_WN" "$C_RS" "$ports_ok" "$osc_ok" "$AUDIO_BACKEND"
say "Start xair_network_bridge_client (and the virtual cable on Windows/macOS) and set XAIR_OSC_HOST, then re-run."
exit 1
