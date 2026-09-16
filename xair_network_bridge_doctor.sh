#!/usr/bin/env bash
# xair_network_bridge_doctor.sh — read-only health check for X-Air Network Bridge.
#
# Does NOT change quantum, PipeWire, JACK, sample rate, buffers, HDMI, .env,
# mixer parameters, firewall, packages, or any user configuration.
# GET-only OSC. No jack_connect / jack_disconnect. No pip install.
#
# Usage: ./xair_network_bridge_doctor.sh
#
# Exit: 0 READY | 1 WARNING | 2 ERROR

set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT_NAME="${XAIR_JACK_CLIENT_NAME:-xair_net_bridge}"
CHANNELS=18
OSC_PORT_DEFAULT=10024
UDP_PORT_DEFAULT=50000
RETURN_UDP_DEFAULT=50001
CODEC_PCM24=2
HEADER_SIZE=28

TMP=""
N_PASS=0
N_WARN=0
N_FAIL=0
RESULTS=""

if [[ -t 1 ]]; then
  C_OK=$'\033[1;32m'
  C_WN=$'\033[1;33m'
  C_ER=$'\033[1;31m'
  C_HD=$'\033[1;36m'
  C_DM=$'\033[2m'
  C_RS=$'\033[0m'
else
  C_OK=""; C_WN=""; C_ER=""; C_HD=""; C_DM=""; C_RS=""
fi

cleanup() {
  if [[ -n "$TMP" && -d "$TMP" ]]; then
    rm -rf "$TMP"
  fi
}
trap cleanup EXIT

have() { command -v "$1" >/dev/null 2>&1; }

run_to() {
  local sec="${1:-2}"
  shift
  if have timeout; then
    timeout -k 1 "$sec" "$@"
  else
    "$@"
  fi
}

say() { printf '%s\n' "$*"; }

section() {
  printf '\n%s== %s ==%s\n' "$C_HD" "$1" "$C_RS"
}

record() {
  local kind="$1" item="$2" msg="$3"
  RESULTS+="${kind}|${item}|${msg}"$'\n'
  case "$kind" in
    PASS) N_PASS=$((N_PASS + 1)); printf '  %sPASS%s  %s — %s\n' "$C_OK" "$C_RS" "$item" "$msg" ;;
    WARN) N_WARN=$((N_WARN + 1)); printf '  %sWARN%s  %s — %s\n' "$C_WN" "$C_RS" "$item" "$msg" ;;
    FAIL) N_FAIL=$((N_FAIL + 1)); printf '  %sFAIL%s  %s — %s\n' "$C_ER" "$C_RS" "$item" "$msg" ;;
  esac
}

# --- env (read-only; never writes .env) -------------------------------------

env_unquote() {
  local v="$1"
  v="${v%"${v##*[![:space:]]}"}"
  v="${v#"${v%%[![:space:]]*}"}"
  if [[ "$v" == \"*\" ]]; then
    v="${v#\"}"; v="${v%\"}"
  elif [[ "$v" == \'*\' ]]; then
    v="${v#\'}"; v="${v%\'}"
  else
    v="${v%%#*}"
    v="${v%"${v##*[![:space:]]}"}"
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
    k="${BASH_REMATCH[1]}"
    v="$(env_unquote "${BASH_REMATCH[2]}")"
    if [[ -z "${!k+x}" ]]; then
      printf -v "$k" '%s' "$v"
    fi
  done < "$f"
}

cfg() {
  local key="$1" def="${2:-}"
  local val="${!key-}"
  if [[ -n "$val" ]]; then
    printf '%s' "$val"
  else
    printf '%s' "$def"
  fi
}

# --- binary helpers ----------------------------------------------------------

# Print raw bytes. Never capture these with $() — bash strips NULs.
le16() {
  local n=$(($1 & 0xFFFF))
  printf '%b' "$(printf '\\x%02x\\x%02x' $((n & 255)) $(((n >> 8) & 255)))"
}

le32() {
  local n=$(($1 & 0xFFFFFFFF))
  printf '%b' "$(printf '\\x%02x\\x%02x\\x%02x\\x%02x' \
    $((n & 255)) $(((n >> 8) & 255)) $(((n >> 16) & 255)) $(((n >> 24) & 255)))"
}

be16() {
  local n=$(($1 & 0xFFFF))
  printf '%b' "$(printf '\\x%02x\\x%02x' $(((n >> 8) & 255)) $((n & 255)))"
}

be32() {
  local n=$(($1 & 0xFFFFFFFF))
  printf '%b' "$(printf '\\x%02x\\x%02x\\x%02x\\x%02x' \
    $(((n >> 24) & 255)) $(((n >> 16) & 255)) $(((n >> 8) & 255)) $((n & 255)))"
}

be64() {
  local n="$1" i hex=""
  for i in 56 48 40 32 24 16 8 0; do
    hex+="$(printf '\\x%02x' $(( (n >> i) & 255 )))"
  done
  printf '%b' "$hex"
}

osc_pad_str() {
  local s="$1"
  local total=$(( ${#s} + 1 ))
  local pad=$(( (4 - total % 4) % 4 ))
  printf '%s\0' "$s"
  if (( pad > 0 )); then
    dd if=/dev/zero bs=1 count="$pad" status=none 2>/dev/null || dd if=/dev/zero bs=1 count="$pad" 2>/dev/null
  fi
}

osc_query_dgram() {
  osc_pad_str "$1"
  osc_pad_str ","
}

ip_to_int() {
  local a b c d
  IFS=. read -r a b c d <<<"$1"
  printf '%s' $(( (a << 24) + (b << 16) + (c << 8) + d ))
}

cidr_contains() {
  local net="$1" host="$2"
  local ip mask bits
  ip="${net%%/*}"
  bits="${net##*/}"
  [[ "$bits" == "$net" ]] && bits=32
  mask=$(( 0xFFFFFFFF << (32 - bits) & 0xFFFFFFFF ))
  local hi hi2
  hi=$(( $(ip_to_int "$ip") & mask ))
  hi2=$(( $(ip_to_int "$host") & mask ))
  [[ "$hi" -eq "$hi2" ]]
}

# --- UDP / OSC I/O (no mixer SETs) ------------------------------------------

udp_send_recv() {
  local host="$1" port="$2" infile="$3" outfile="$4" wait_s="${5:-2}"
  : > "$outfile"
  if have timeout && have socat; then
    timeout "$wait_s" socat -T"$wait_s" - UDP:"${host}:${port}" < "$infile" > "$outfile" 2>/dev/null && [[ -s "$outfile" ]] && return 0
  fi
  if have timeout && have nc; then
    timeout "$wait_s" nc -u -w "$wait_s" "$host" "$port" < "$infile" > "$outfile" 2>/dev/null && [[ -s "$outfile" ]] && return 0
    timeout "$wait_s" nc -u -n -w "$wait_s" "$host" "$port" < "$infile" > "$outfile" 2>/dev/null && [[ -s "$outfile" ]] && return 0
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

udp_listen() {
  local port="$1" outfile="$2" wait_s="${3:-2}"
  : > "$outfile"
  if have timeout && have nc; then
    timeout "$wait_s" nc -u -l -p "$port" > "$outfile" 2>/dev/null && [[ -s "$outfile" ]] && return 0
    timeout "$wait_s" nc -u -l "$port" > "$outfile" 2>/dev/null && [[ -s "$outfile" ]] && return 0
  fi
  return 1
}

udp_port_bound() {
  local port="$1"
  if have ss; then
    run_to 2 ss -uln 2>/dev/null | grep -E -q ":${port}[[:space:]]"
    return $?
  fi
  if have netstat; then
    run_to 2 netstat -uln 2>/dev/null | grep -E -q ":${port}[[:space:]]"
    return $?
  fi
  return 1
}

# --- JACK listing (read-only) -----------------------------------------------

jack_list() {
  if have jack_lsp; then
    run_to 2 jack_lsp 2>/dev/null && return 0
  fi
  if have pw-jack; then
    run_to 2 pw-jack jack_lsp 2>/dev/null && return 0
  fi
  if have pw-link; then
    run_to 2 pw-link 2>/dev/null && return 0
  fi
  return 1
}

# --- WAV PCM24 (temp only; never touches recordings/) -----------------------

write_pcm24_wav() {
  local path="$1"
  local sr=48000 ch=1 nframes=48
  local data_bytes=$((nframes * ch * 3))
  local fmt_size=16
  local riff_size=$((4 + (8 + fmt_size) + (8 + data_bytes)))
  {
    printf 'RIFF'
    le32 "$riff_size"
    printf 'WAVEfmt '
    le32 "$fmt_size"
    le16 1
    le16 "$ch"
    le32 "$sr"
    le32 $((sr * ch * 3))
    le16 $((ch * 3))
    le16 24
    printf 'data'
    le32 "$data_bytes"
    dd if=/dev/zero bs=1 count="$data_bytes" status=none 2>/dev/null || dd if=/dev/zero bs=1 count="$data_bytes" 2>/dev/null
  } > "$path"
}

verify_pcm24_wav() {
  local path="$1"
  [[ -f "$path" && -s "$path" ]] || return 1
  local magic data_tag bits
  magic="$(dd if="$path" bs=1 count=4 status=none 2>/dev/null || dd if="$path" bs=1 count=4 2>/dev/null)"
  [[ "$magic" == "RIFF" ]] || return 1
  dd if="$path" bs=1 skip=8 count=4 status=none 2>/dev/null | grep -q WAVE || return 1
  # PCM bits at offset 34 (uint16 LE) in canonical 44-byte header
  bits="$(od -An -t u2 -N 2 -j 34 "$path" 2>/dev/null | awk '{print $1}')"
  [[ "$bits" == "24" ]] || return 1
  local sz hdr
  sz="$(wc -c < "$path" | tr -d ' ')"
  hdr=44
  [[ "$sz" -ge "$hdr" ]] || return 1
  [[ $(( (sz - hdr) % 3 )) -eq 0 ]] || return 1
  return 0
}

# --- XBRI loopback (never sent to a live client) ----------------------------

write_xbri() {
  local path="$1"
  local sr=48000 ch=2 nf=8 seq=1
  local payload=$((nf * ch * 3))
  local ts=0
  if [[ -n "${EPOCHREALTIME-}" ]]; then
    ts="$(printf '%.0f' "$(echo "${EPOCHREALTIME} * 1000000000" | awk '{printf "%.0f", $1}')")"
  else
    ts=$(( $(date +%s) * 1000000000 ))
  fi
  {
    printf 'XBRI'
    printf '%b' "$(printf '\\x01\\x%02x\\x00\\x00' "$CODEC_PCM24")"
    be32 "$sr"
    be16 "$ch"
    be16 "$nf"
    be32 "$seq"
    be64 "$ts"
    dd if=/dev/zero bs=1 count="$payload" status=none 2>/dev/null || dd if=/dev/zero bs=1 count="$payload" 2>/dev/null
  } > "$path"
}

parse_xbri_magic() {
  local path="$1"
  [[ -s "$path" ]] || return 1
  local mag
  mag="$(dd if="$path" bs=1 count=4 status=none 2>/dev/null || dd if="$path" bs=1 count=4 2>/dev/null)"
  [[ "$mag" == "XBRI" ]] || return 1
  local sz
  sz="$(wc -c < "$path" | tr -d ' ')"
  [[ "$sz" -ge "$HEADER_SIZE" ]] || return 1
  return 0
}

# =============================================================================

TMP="$(mktemp -d /tmp/xair-doctor.XXXXXX)"
load_dotenv "$ROOT/.env"

OSC_HOST="$(cfg XAIR_OSC_HOST "")"
[[ -z "$OSC_HOST" ]] && OSC_HOST="$(cfg XAIR_OSC_IP "")"
OSC_PORT="$(cfg XAIR_OSC_PORT "$OSC_PORT_DEFAULT")"
PEER_HOST="$(cfg XAIR_PEER_HOST "127.0.0.1")"
UDP_PORT="$(cfg XAIR_UDP_PORT "$UDP_PORT_DEFAULT")"
RETURN_UDP="$(cfg XAIR_RETURN_UDP_PORT "$RETURN_UDP_DEFAULT")"
RETURN_AUDIO="$(cfg XAIR_RETURN_AUDIO "false")"
CHANNELS="$(cfg XAIR_CHANNELS "$CHANNELS")"
CLIENT_NAME="$(cfg XAIR_JACK_CLIENT_NAME "$CLIENT_NAME")"

PYTHON=""
if [[ -x "$ROOT/.venv/bin/python3" ]]; then
  PYTHON="$ROOT/.venv/bin/python3"
elif have python3; then
  PYTHON="$(command -v python3)"
fi

say "${C_HD}X-Air Network Bridge — doctor (read-only)${C_RS}"
say "${C_DM}Project: $ROOT${C_RS}"
say "${C_DM}No changes to JACK/PipeWire/quantum/HDMI/.env/mixer.${C_RS}"

# -----------------------------------------------------------------------------
section "1. JACK / PipeWire ports"

JACK_OUT="$TMP/jack_lsp.txt"
if jack_list > "$JACK_OUT"; then
  n_in="$(grep -c -E "^${CLIENT_NAME}:.*_in$" "$JACK_OUT" 2>/dev/null || true)"
  n_out="$(grep -c -E "^${CLIENT_NAME}:.*_out$" "$JACK_OUT" 2>/dev/null || true)"
  n_in="${n_in:-0}"
  n_out="${n_out:-0}"
  missing_ch=0
  i=1
  while (( i <= CHANNELS )); do
    ii="$(printf '%02d' "$i")"
    grep -qE "^${CLIENT_NAME}:${ii}_CH${ii}_in$" "$JACK_OUT" || missing_ch=$((missing_ch + 1))
    grep -qE "^${CLIENT_NAME}:${ii}_CH${ii}_out$" "$JACK_OUT" || missing_ch=$((missing_ch + 1))
    i=$((i + 1))
  done
  if (( n_in == CHANNELS && n_out == CHANNELS )); then
    if (( missing_ch == 0 )); then
      record PASS "JACK ports" "${CLIENT_NAME}: 18× _in + 18× _out (01_CH01 … 18_CH18)"
    else
      record PASS "JACK ports" "${CLIENT_NAME}: 18× _in + 18× _out (OSC stem names, not CH## fallback)"
    fi
  elif grep -qE "^${CLIENT_NAME}:" "$JACK_OUT"; then
    record FAIL "JACK ports" "${CLIENT_NAME} present but count in=${n_in} out=${n_out} (need ${CHANNELS}+${CHANNELS})"
  else
    record WARN "JACK ports" "client ${CLIENT_NAME} not in the graph (is xair_network_bridge_client running?)"
  fi
else
  record WARN "JACK ports" "jack_lsp / pw-jack / pw-link not available or JACK not running"
fi

# -----------------------------------------------------------------------------
section "2. OSC (X18) — GET only"

if [[ -z "$OSC_HOST" || "$OSC_HOST" == "auto" ]]; then
  record WARN "OSC ping" "XAIR_OSC_HOST / XAIR_OSC_IP not set (no mixer target)"
else
  osc_query_dgram "/status" > "$TMP/osc_status.bin"
  if udp_send_recv "$OSC_HOST" "$OSC_PORT" "$TMP/osc_status.bin" "$TMP/osc_status_rx.bin" 2; then
    if grep -a -q '/status' "$TMP/osc_status_rx.bin"; then
      record PASS "OSC ping" "${OSC_HOST}:${OSC_PORT} replied to /status"
    else
      record FAIL "OSC ping" "${OSC_HOST}:${OSC_PORT} UDP reply without /status"
    fi
  else
    record FAIL "OSC ping" "no /status reply from ${OSC_HOST}:${OSC_PORT}"
  fi

  osc_ok=0
  for path in \
    "/ch/01/config/name" \
    "/ch/01/mix/fader" \
    "/ch/01/mix/on" \
    "/ch/01/mix/pan"
  do
    osc_query_dgram "$path" > "$TMP/osc_q.bin"
    if udp_send_recv "$OSC_HOST" "$OSC_PORT" "$TMP/osc_q.bin" "$TMP/osc_r.bin" 2 \
      && grep -a -q -- "$path" "$TMP/osc_r.bin"; then
      osc_ok=$((osc_ok + 1))
    fi
  done
  if (( osc_ok == 4 )); then
    record PASS "OSC params" "ch1 name, fader, mute (/mix/on), pan all answered"
  elif (( osc_ok > 0 )); then
    record WARN "OSC params" "partial replies (${osc_ok}/4: name/fader/mute/pan)"
  else
    record FAIL "OSC params" "name / fader / mute / pan did not answer on ch1"
  fi
fi

# -----------------------------------------------------------------------------
section "3. UDP transport (XBRI)"

write_xbri "$TMP/xbri_tx.bin"
LOOP_PORT=$((54000 + (RANDOM % 500)))
if have timeout && have nc; then
  # Loopback: listener in background, send to self. Never aimed at a live client.
  timeout 2 nc -u -l -p "$LOOP_PORT" > "$TMP/xbri_rx.bin" 2>/dev/null &
  lp=$!
  sleep 0.15
  timeout 1 nc -u -w 1 127.0.0.1 "$LOOP_PORT" < "$TMP/xbri_tx.bin" >/dev/null 2>&1 || true
  wait "$lp" 2>/dev/null || true
  if parse_xbri_magic "$TMP/xbri_rx.bin"; then
    record PASS "UDP loopback" "XBRI PCM24 packet sent and received on 127.0.0.1:${LOOP_PORT}"
  else
    # second nc dialect
    timeout 2 nc -u -l "$LOOP_PORT" > "$TMP/xbri_rx.bin" 2>/dev/null &
    lp=$!
    sleep 0.15
    timeout 1 nc -u -w 1 127.0.0.1 "$LOOP_PORT" < "$TMP/xbri_tx.bin" >/dev/null 2>&1 || true
    wait "$lp" 2>/dev/null || true
    if parse_xbri_magic "$TMP/xbri_rx.bin"; then
      record PASS "UDP loopback" "XBRI PCM24 packet sent and received on 127.0.0.1:${LOOP_PORT}"
    else
      record WARN "UDP loopback" "could not complete local XBRI echo (nc UDP listen dialect)"
    fi
  fi
else
  record WARN "UDP loopback" "timeout/nc missing; skipped packet echo"
fi

SNAP="$ROOT/.xair_sync_state.json"
if [[ -f "$SNAP" ]]; then
  jitter="$(grep -oE '"jitter_ms_ema":[[:space:]]*[0-9.]+' "$SNAP" | head -n1 | grep -oE '[0-9.]+$' || true)"
  loss="$(grep -oE '"loss_window_ppm":[[:space:]]*[0-9.]+' "$SNAP" | head -n1 | grep -oE '[0-9.]+$' || true)"
  drift="$(grep -oE '"global_drift_ns":[[:space:]]*-?[0-9.]+' "$SNAP" | head -n1 | grep -oE -- '-?[0-9.]+$' || true)"
  jnum="${jitter:-0}"
  lnum="${loss:-0}"
  extra="jitter_ema=${jitter:-?}ms loss_ppm=${loss:-?} drift_ns=${drift:-?}"
  awk_ok="$(awk -v j="$jnum" -v l="$lnum" 'BEGIN { if (j+0 > 20 || l+0 > 1000) print "bad"; else print "ok" }')"
  if [[ "$awk_ok" == "bad" ]]; then
    record WARN "UDP live" "sync snapshot shows high jitter/loss ($extra)"
  else
    record PASS "UDP live" "sync snapshot present ($extra) — no audio injected"
  fi
elif udp_port_bound "$UDP_PORT"; then
  record PASS "UDP live" "port ${UDP_PORT} is bound (receiver likely running); not injecting XBRI into the live path"
else
  if udp_listen "$UDP_PORT" "$TMP/xbri_live.bin" 2 && parse_xbri_magic "$TMP/xbri_live.bin"; then
    record PASS "UDP live" "passive listen on ${UDP_PORT} caught an XBRI packet"
  else
    record WARN "UDP live" "no snapshot and no XBRI heard on ${UDP_PORT} (xair_network_bridge_server not sending to this host?)"
  fi
fi

# -----------------------------------------------------------------------------
section "4. Recorder (temp PCM24 WAV)"

WAV_DIR="$TMP/rec"
mkdir -p "$WAV_DIR"
WAV="$WAV_DIR/doctor_ch01.wav"
if write_pcm24_wav "$WAV" && verify_pcm24_wav "$WAV"; then
  record PASS "Recorder" "wrote and verified PCM24 WAV (${WAV##*/}, 48 kHz / 24-bit / mono)"
else
  record FAIL "Recorder" "failed to write or verify a PCM24 WAV in a temp folder"
fi

# -----------------------------------------------------------------------------
section "5. USB return"

ra_lc="$(printf '%s' "$RETURN_AUDIO" | tr 'A-Z' 'a-z')"
usb_hints=0
if have lsusb && run_to 2 lsusb 2>/dev/null | grep -qiE '1397|Behringer|X18|XR18'; then
  usb_hints=$((usb_hints + 1))
fi
if grep -qiE 'X18|XR18|X-AIR|X AIR' /proc/asound/cards 2>/dev/null; then
  usb_hints=$((usb_hints + 1))
fi

usb_route=0
if [[ -n "$OSC_HOST" && "$OSC_HOST" != "auto" ]]; then
  for path in "/routing/USB/01" "/routing/USB/02"; do
    osc_query_dgram "$path" > "$TMP/osc_usb.bin"
    if udp_send_recv "$OSC_HOST" "$OSC_PORT" "$TMP/osc_usb.bin" "$TMP/osc_usb_rx.bin" 2 \
      && grep -a -q -- "$path" "$TMP/osc_usb_rx.bin"; then
      usb_route=$((usb_route + 1))
    fi
  done
fi

if [[ "$ra_lc" == "true" || "$ra_lc" == "1" || "$ra_lc" == "yes" || "$ra_lc" == "on" ]]; then
  if (( usb_route >= 1 )); then
    record PASS "USB return" "XAIR_RETURN_AUDIO=true and mixer answered USB routing (ch 1–2)"
  elif (( usb_hints > 0 )); then
    record WARN "USB return" "return enabled and X18 USB seen locally, but OSC USB routing did not confirm assignment"
  else
    record WARN "USB return" "XAIR_RETURN_AUDIO=true but USB routing / X18 USB not confirmed on this host"
  fi
  if udp_port_bound "$RETURN_UDP"; then
    record PASS "USB return UDP" "return port ${RETURN_UDP} is bound"
  else
    record WARN "USB return UDP" "port ${RETURN_UDP} not bound (server return RX not listening here)"
  fi
else
  if (( usb_route >= 1 )); then
    record WARN "USB return" "mixer USB 1–2 routing answers, but XAIR_RETURN_AUDIO is not true in .env"
  else
    record WARN "USB return" "disabled (XAIR_RETURN_AUDIO≠true). Hybrid FX return is off — not an error"
  fi
fi

# -----------------------------------------------------------------------------
section "6. DAWs (REAPER / Bitwig)"

reaper_bin=0
bitwig_bin=0
have reaper && reaper_bin=1
have Reaper && reaper_bin=1
have bitwig && bitwig_bin=1
have bitwig-studio && bitwig_bin=1

reaper_run=0
bitwig_run=0
pgrep -i -x reaper >/dev/null 2>&1 && reaper_run=1
pgrep -i reaper >/dev/null 2>&1 && reaper_run=1
pgrep -i bitwig >/dev/null 2>&1 && bitwig_run=1

daw_jack=0
if [[ -s "$JACK_OUT" ]]; then
  grep -qiE 'reaper|bitwig' "$JACK_OUT" && daw_jack=1
fi

if (( reaper_run || bitwig_run )); then
  names=""
  (( reaper_run )) && names+="REAPER "
  (( bitwig_run )) && names+="Bitwig "
  if (( daw_jack )); then
    record PASS "DAW" "${names}running and visible on JACK/PipeWire"
  else
    record WARN "DAW" "${names}running but no matching JACK client (audio device may not be JACK)"
  fi
elif (( reaper_bin || bitwig_bin )); then
  record WARN "DAW" "REAPER/Bitwig installed but not running"
else
  record WARN "DAW" "REAPER/Bitwig not detected (install or start a JACK DAW to use stems)"
fi

# -----------------------------------------------------------------------------
section "7. Dependencies (check only, no install)"

if [[ -n "$PYTHON" ]]; then
  record PASS "python3" "$PYTHON"
else
  record FAIL "python3" "python3 not found"
fi

if have pip3 || have pip; then
  record PASS "pip" "$(command -v pip3 2>/dev/null || command -v pip)"
elif [[ -n "$PYTHON" ]] && "$PYTHON" -m pip --version >/dev/null 2>&1; then
  record PASS "pip" "$PYTHON -m pip"
else
  record WARN "pip" "pip not found (ok if the venv is already populated)"
fi

if [[ -n "$PYTHON" ]]; then
  "$PYTHON" -c "import numpy" >/dev/null 2>&1 \
    && record PASS "numpy" "import ok" \
    || record FAIL "numpy" "not importable (not installing)"
  "$PYTHON" -c "import soundfile" >/dev/null 2>&1 \
    && record PASS "soundfile" "import ok" \
    || record FAIL "soundfile" "not importable (not installing)"
  if "$PYTHON" -c "import pythonosc" >/dev/null 2>&1; then
    record PASS "pythonosc" "python-osc import ok (project dependency)"
  elif "$PYTHON" -c "import osc" >/dev/null 2>&1; then
    record WARN "pyosc" "legacy pyosc (osc) found; this project uses python-osc (pythonosc)"
  else
    record FAIL "pythonosc" "pythonosc/pyosc not importable (not installing)"
  fi
fi

# -----------------------------------------------------------------------------
section "8. Network (subnet, UDP, OSC)"

if [[ -z "$OSC_HOST" || "$OSC_HOST" == "auto" ]]; then
  record WARN "Subnet" "no mixer IP to compare"
else
  same=0
  if [[ "$OSC_HOST" == "127.0.0.1" || "$OSC_HOST" == "localhost" ]]; then
    same=1
  else
    if have ip; then
      while read -r cidr; do
        [[ -z "$cidr" ]] && continue
        if cidr_contains "$cidr" "$OSC_HOST"; then
          same=1
          break
        fi
      done < <(ip -4 -o addr show 2>/dev/null | awk '{print $4}')
    fi
  fi
  if (( same )); then
    record PASS "Subnet" "${OSC_HOST} is on a local IPv4 subnet (or loopback)"
  else
    record WARN "Subnet" "${OSC_HOST} is not on a detected local IPv4 subnet (VPN/routing?)"
  fi

  if have ping; then
    if run_to 2 ping -c 1 -W 1 "$OSC_HOST" >/dev/null 2>&1; then
      record PASS "ICMP" "ping ${OSC_HOST} ok"
    else
      record WARN "ICMP" "ping ${OSC_HOST} failed (ICMP may be filtered; OSC still possible)"
    fi
  fi
fi

if [[ "$PEER_HOST" == "127.0.0.1" || "$PEER_HOST" == "localhost" ]]; then
  record PASS "Audio peer" "XAIR_PEER_HOST=${PEER_HOST} (same machine)"
else
  record PASS "Audio peer" "XAIR_PEER_HOST=${PEER_HOST} UDP ${UDP_PORT} (unicast; not probed with live audio)"
fi

# -----------------------------------------------------------------------------
section "9. Report"

if (( N_FAIL > 0 )); then
  VERDICT="ERROR"
  COL="$C_ER"
elif (( N_WARN > 0 )); then
  VERDICT="WARNING"
  COL="$C_WN"
else
  VERDICT="READY"
  COL="$C_OK"
fi

printf '\n%s======== %s ========%s\n' "$COL" "$VERDICT" "$C_RS"
printf '  pass=%s  warn=%s  fail=%s\n' "$N_PASS" "$N_WARN" "$N_FAIL"
say ""
say "Recommendations (nothing was changed on this system):"
if (( N_FAIL == 0 && N_WARN == 0 )); then
  say "  • All checks passed. Safe to run xair_network_bridge_server / xair_network_bridge_client / osc-launcher."
else
  grep -E '^(FAIL|WARN)\|' <<<"$RESULTS" | while IFS='|' read -r k item msg; do
    printf '  • [%s] %s: %s\n' "$k" "$item" "$msg"
  done
  say "  • JACK: xair_network_bridge_client on the DAW machine; do not expect ports if the client is down."
  say "  • OSC: XAIR_OSC_HOST must be the X18 IP, port 10024; Enable OSC is mixer-side, not rewritten here."
  say "  • UDP: live XBRI is observed only; this doctor never injects audio into a running client."
  say "  • USB return: enable XAIR_RETURN_AUDIO and assign USB 1–2 on the X18 Routing page."
  say "  • DAW: Audio device = JACK (PipeWire); block size must match the engine you already chose."
  say "  • Deps: use scripts/install.sh / the venv — this script will not pip-install."
fi
say ""
say "${C_DM}This doctor does not touch quantum, PipeWire, JACK buffers, HDMI, or user configs.${C_RS}"

case "$VERDICT" in
  READY) exit 0 ;;
  WARNING) exit 1 ;;
  *) exit 2 ;;
esac
