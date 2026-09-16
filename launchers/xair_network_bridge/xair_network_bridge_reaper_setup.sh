#!/usr/bin/env bash
# Official REAPER setup launcher (Linux). macOS: xair_network_bridge_reaper_setup.command
# Windows: xair_network_bridge_reaper_setup.ps1
#
# Writes project-local template + OSC surface. Does NOT change quantum,
# PipeWire, JACK, sample rate, buffers, HDMI, REAPER.ini, or .env.
# GET-only OSC to the X18. No jack_connect / jack_disconnect.
#
# Usage: bash launchers/xair_network_bridge/xair_network_bridge_reaper_setup.sh

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CLIENT_NAME="xair_net_bridge"
CHANNELS=18
OSC_PORT_DEFAULT=10024
REAPER_LISTEN=8000
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

new_guid() {
  local u=""
  if [[ -r /proc/sys/kernel/random/uuid ]]; then u="$(tr 'A-Z' 'a-z' < /proc/sys/kernel/random/uuid)"
  elif have uuidgen; then u="$(uuidgen | tr 'A-Z' 'a-z')"
  else u="$(printf '%08x-%04x-4%03x-a%03x-%012x' $RANDOM $RANDOM $RANDOM $RANDOM $RANDOM)"
  fi
  printf '{%s}' "$u"
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

find_reaper() {
  local c
  for c in reaper Reaper REAPER; do
    if have "$c"; then command -v "$c"; return 0; fi
  done
  for c in \
    /opt/REAPER/reaper \
    "$HOME/opt/REAPER/reaper" \
    /usr/bin/reaper \
    /usr/local/bin/reaper \
    /Applications/REAPER.app/Contents/MacOS/REAPER
  do
    [[ -x "$c" ]] && { printf '%s' "$c"; return 0; }
  done
  return 1
}

udp_port_listening() {
  local port="$1"
  if have ss && run_to 2 ss -uln 2>/dev/null | grep -E -q ":${port}[[:space:]]"; then return 0; fi
  if have lsof && run_to 2 lsof -nP -iUDP:"$port" >/dev/null 2>&1; then return 0; fi
  if have netstat && run_to 2 netstat -an 2>/dev/null | grep -E -q "[\.:]${port}[^0-9].*udp"; then return 0; fi
  return 1
}

# --- start ------------------------------------------------------------------

load_dotenv "$ROOT/.env"
CLIENT_NAME="$(cfg XAIR_JACK_CLIENT_NAME "$CLIENT_NAME")"
OSC_HOST="$(cfg XAIR_OSC_HOST "")"
[[ -z "$OSC_HOST" ]] && OSC_HOST="$(cfg XAIR_OSC_IP "")"
OSC_PORT="$(cfg XAIR_OSC_PORT "$OSC_PORT_DEFAULT")"
REAPER_LISTEN="$(cfg XAIR_REAPER_PORT "$REAPER_LISTEN")"
BRIDGE_LISTEN="$(cfg XAIR_REAPER_LISTEN_PORT "$BRIDGE_LISTEN")"
SR="$(cfg XAIR_SAMPLE_RATE 48000)"

TMP="$(mktemp -d "${TMPDIR:-/tmp}/xair-reaper-setup.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT

OS_NAME="$(uname -s 2>/dev/null || echo unknown)"
AUDIO_BACKEND="unknown"
case "$OS_NAME" in
  Linux) AUDIO_BACKEND="JACK/PipeWire" ;;
  Darwin) AUDIO_BACKEND="CoreAudio" ;;
  MINGW*|MSYS*|CYGWIN*) AUDIO_BACKEND="WASAPI/ASIO" ;;
esac

say "${C_HD}X-Air Network Bridge — REAPER setup (project files only)${C_RS}"
say "${C_DM}OS: ${OS_NAME}  audio backend: ${AUDIO_BACKEND}${C_RS}"
say "${C_DM}Does not change JACK / PipeWire / quantum / HDMI / user REAPER.ini${C_RS}"

REAPER_BIN=""
REAPER_BIN="$(find_reaper)" || REAPER_BIN=""
if [[ -z "$REAPER_BIN" ]]; then
  die "REAPER is not installed (PATH, /opt/REAPER, or /Applications/REAPER.app)"
fi
say "  REAPER: $REAPER_BIN"

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
      say "  Bridge ports: BlackHole CoreAudio driver present (DAW sees it as a device, not JACK names)"
    else
      printf '  %sWARN%s  BlackHole not found. Install BlackHole 16ch/64ch; DAW device = BlackHole. Not installing from here.\n' \
        "$C_WN" "$C_RS"
    fi
    ;;
  *)
    printf '  %sWARN%s  use xair_network_bridge_reaper_setup.ps1 on Windows for WASAPI/ASIO\n' "$C_WN" "$C_RS"
    ;;
esac

# Track names (GET only). Fallback CH01…CH18.
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

mkdir -p "$ROOT/templates" "$ROOT/reaper-osc"

# --- ReaperOSC surface (repo only; not ~/.config/REAPER) --------------------
OSC_FILE="$ROOT/reaper-osc/xair_network_bridge.ReaperOSC"
cat > "$OSC_FILE" <<EOF
# X-Air Network Bridge — REAPER OSC surface
# Load in REAPER: Preferences → Control/OSC/Web → Add → Load this file.
# This launcher never writes ~/.config/REAPER or reaper.ini.
# Local listen = ${REAPER_LISTEN} (REAPER receives). Destination = 127.0.0.1:${BRIDGE_LISTEN}.

DEVICE_NAME "X-Air Network Bridge"
DEVICE_IN 0.0.0.0
DEVICE_PORT_IN ${REAPER_LISTEN}
DEVICE_OUT 127.0.0.1
DEVICE_PORT_OUT ${BRIDGE_LISTEN}

TRACK_COUNT ${CHANNELS}
SEND_COUNT 4
RECEIVE_COUNT 4
FX_COUNT 8
FX_PARAM_COUNT 16
MARKER_COUNT 0
REGION_COUNT 0

REAPER_TRACK_FOLLOWS REAPER
DEVICE_TRACK_FOLLOWS DEVICE
DEVICE_TRACK_BANK_FOLLOWS DEVICE
DEVICE_FX_FOLLOWS DEVICE

TRACK_NAME s/track/@/name
TRACK_MUTE t/track/@/mute
TRACK_SOLO t/track/@/solo
TRACK_REC_ARM t/track/@/recarm
TRACK_VOLUME n/track/@/volume
TRACK_PAN n/track/@/pan
TRACK_SEND_VOLUME n/track/@/send/@/volume
TRACK_SEND_PAN n/track/@/send/@/pan

MASTER_VOLUME n/master/volume
MASTER_PAN n/master/pan
MASTER_MUTE t/master/mute

DEVICE_TRACK_COUNT n/device/track/count
REWIND t/rewind
PLAY t/play
STOP t/stop
RECORD t/record
EOF
say "  wrote $OSC_FILE"

# --- RPP template -----------------------------------------------------------
RPP="$ROOT/templates/xair_network_bridge.RPP"
PGUID="$(new_guid)"
{
  printf '%s\n' "<REAPER_PROJECT 0.1 \"7.0\" 0"
  printf '%s\n' "  RIPPLE 0"
  printf '%s\n' "  GROUPOVERRIDE 0 0 0"
  printf '%s\n' "  AUTOXFADE 1"
  printf '%s\n' "  ENVATTACH 1"
  printf '%s\n' "  MIXERUIFLAGS 11 48"
  printf '%s\n' "  CURSOR 0"
  printf '%s\n' "  ZOOM 100 0 0"
  printf '%s\n' "  VZOOMEX 6 0"
  printf '%s\n' "  RECMODE 1"
  printf '%s\n' "  LOOP 0"
  printf '%s\n' "  RECORD_PATH \"Media\" \"\""
  printf '%s\n' "  SAMPLERATE ${SR} 0 0"
  printf '%s\n' "  TEMPO 120 4 4"
  printf '%s\n' "  PLAYRATE 1 0 0.25 4"
  printf '%s\n' "  MASTERAUTOMODE 0"
  printf '%s\n' "  MASTERHWOUT 0 0 1 0 0 0 0 0"
  printf '%s\n' "  MASTER_NCH 2"
  printf '%s\n' "  MASTER_VOLUME 1 0 -1 -1 1"
  printf '%s\n' "  MASTER_FX 1"
  printf '%s\n' "  <NOTES 0 2"
  printf '%s\n' "    |X-Air Network Bridge — REAPER template"
  printf '%s\n' "    |Host OS: ${OS_NAME}  audio: ${AUDIO_BACKEND}"
  printf '%s\n' "    |Linux JACK: ${CLIENT_NAME}:NN_*_out = REAPER inputs; *_in = outputs"
  printf '%s\n' "    |Windows: WASAPI/ASIO virtual cable (VB-Cable). macOS: CoreAudio BlackHole."
  printf '%s\n' "    |USB Return: last track, post-fader sends from 1–18, HWOUT 1–2 (stereo)"
  printf '%s\n' "    |Plugins: always on track/bus OUTPUT feeding USB Return. Never input FX."
  printf '%s\n' "    |OSC: load ./reaper-osc/xair_network_bridge.ReaperOSC  listen ${REAPER_LISTEN} → dest 127.0.0.1:${BRIDGE_LISTEN}"
  printf '%s\n' "  >"

  i=1
  while (( i <= CHANNELS )); do
    ii="$(printf '%02d' "$i")"
    tguid="$(new_guid)"
    stem="${ii}_${NAMES[$i]}"
    recin=$((i - 1))
    printf '  <TRACK %s\n' "$tguid"
    printf '    NAME "%s"\n' "$stem"
    printf '    PEAKCOL 16576\n'
    printf '    BEAT -1\n'
    printf '    AUTOMODE 0\n'
    printf '    VOLPAN 1 0 -1 -1 1\n'
    printf '    MUTESOLO 0 0 0\n'
    printf '    IPHASE 0\n'
    printf '    PLAYOFFS 0 1\n'
    printf '    ISBUS 0 0\n'
    printf '    BUSCOMP 0 0 0 0 0\n'
    printf '    NCHAN 2\n'
    printf '    FX 1\n'
    printf '    TRACKHEIGHT 0 0 0 0 0 0\n'
    printf '    INQ 0 0 0 0.5 100 0 0 100\n'
    printf '    NRECARM 0\n'
    printf '    REC 0 %s 1 1 0 0 0 0\n' "$recin"
    printf '    VU 2\n'
    printf '    TRACKID %s\n' "$tguid"
    printf '    PERF 0\n'
    printf '    MIDIOUT -1\n'
    printf '    MAINSEND 1 0\n'
    printf '    AUXSEND %s 0 0 0 0 0 0 0 0 -1:U 0 -1 '\'''\''\n' "$CHANNELS"
    printf '  >\n'
    i=$((i + 1))
  done

  rguid="$(new_guid)"
  printf '  <TRACK %s\n' "$rguid"
  printf '    NAME "USB_Return"\n'
  printf '    PEAKCOL 255\n'
  printf '    BEAT -1\n'
  printf '    AUTOMODE 0\n'
  printf '    VOLPAN 1 0 -1 -1 1\n'
  printf '    MUTESOLO 0 0 0\n'
  printf '    IPHASE 0\n'
  printf '    PLAYOFFS 0 1\n'
  printf '    ISBUS 0 0\n'
  printf '    BUSCOMP 0 0 0 0 0\n'
  printf '    NCHAN 2\n'
  printf '    FX 1\n'
  printf '    TRACKHEIGHT 0 0 0 0 0 0\n'
  printf '    INQ 0 0 0 0.5 100 0 0 100\n'
  printf '    NRECARM 0\n'
  printf '    REC 0 -1 0 0 0 0 0 0\n'
  printf '    VU 2\n'
  printf '    TRACKID %s\n' "$rguid"
  printf '    PERF 0\n'
  printf '    MIDIOUT -1\n'
  printf '    MAINSEND 0 0\n'
  printf '    HWOUT 0 0 1 0 0 0 0 0\n'
  printf '  >\n'
  printf '>\n'
} > "$RPP"
say "  wrote $RPP  (18 stems + USB Return, post-fader sends)"

# Do not rewrite the user's reaper.ini. If OSC is already listening, note it.
if udp_port_listening "$REAPER_LISTEN"; then
  say "  OSC listen :${REAPER_LISTEN} already bound (REAPER Control/OSC likely enabled)"
else
  printf '  %sNOTE%s  Enable OSC in REAPER Preferences (this script does not edit reaper.ini):\n' "$C_WN" "$C_RS"
  say "         Load ${OSC_FILE}"
  say "         Local listen ${REAPER_LISTEN}  →  Destination 127.0.0.1:${BRIDGE_LISTEN}"
fi

reaper_running=0
pgrep -i reaper >/dev/null 2>&1 && reaper_running=1
if (( reaper_running )); then
  say "  REAPER already running — open ${RPP} from File → Open (session not restarted)"
elif [[ "$OS_NAME" == "Darwin" ]]; then
  open -a REAPER "$RPP" >/dev/null 2>&1 && say "  launched REAPER with the template" || true
elif [[ -n "${DISPLAY-}${WAYLAND_DISPLAY-}" ]]; then
  nohup "$REAPER_BIN" "$RPP" >/dev/null 2>&1 &
  say "  launched REAPER with the template (existing session was not touched)"
fi

say ""
if (( ports_ok && osc_ok )); then
  printf '%sREAPER READY — X-Air Network Bridge configurado correctamente.%s\n' "$C_OK" "$C_RS"
  exit 0
fi
printf '%sREAPER template written, but not fully READY%s (bridge ports=%s OSC=%s backend=%s).\n' \
  "$C_WN" "$C_RS" "$ports_ok" "$osc_ok" "$AUDIO_BACKEND"
say "Start xair_network_bridge_client (and the virtual cable on Windows/macOS) and set XAIR_OSC_HOST, then re-run."
exit 1
