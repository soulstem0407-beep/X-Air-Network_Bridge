#!/usr/bin/env bash
# Arregla quantum=1024 e Interface=hw:HDMI en el cliente XAIR.
# No borra el programa, no usa sudo, no toca UDP/jitter/FEC/Opus/server.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
QUANTUM="${XAIR_JACK_QUANTUM:-}"
RESTART_CLIENT="auto"
DO_PERSIST=0

usage() {
  cat <<'EOF'
Uso: bash tools/fix_jack_pipewire.sh [--quantum N] [--restart|--no-restart] [--persist]

  --quantum N     Frames/Period a fijar (64–2048). Defecto: 128 si el reloj es 1024,
                  o el force-quantum actual si ya es 64/128/256/512.
  --restart       Reinicia xair_network_bridge_client si está en marcha (nunca xair_network_bridge_server).
  --no-restart    No toca el proceso del cliente.
  --persist       Escribe ~/.config/pipewire/pipewire.conf.d/99-xair-quantum.conf
                  (opcional; el metadata de sesión ya basta hasta el próximo reboot).

Seguro y no destructivo: no apaga HDMI, no mata PipeWire, no borra .venv ni .env.
EOF
}

have() { command -v "$1" >/dev/null 2>&1; }

log() { printf '%s\n' "$*"; }
warn() { printf 'aviso: %s\n' "$*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --quantum)
      QUANTUM="${2:-}"
      shift 2
      ;;
    --quantum=*)
      QUANTUM="${1#--quantum=}"
      shift
      ;;
    --restart)
      RESTART_CLIENT="yes"
      shift
      ;;
    --no-restart)
      RESTART_CLIENT="no"
      shift
      ;;
    --persist)
      DO_PERSIST=1
      shift
      ;;
    *)
      warn "opción desconocida: $1"
      usage >&2
      exit 2
      ;;
  esac
done

is_hdmi_name() {
  printf '%s' "$1" | grep -qiE 'hdmi|displayport|display[[:space:]]*port|hda[[:space:]]*nvidia|nvidia:[[:space:]]*hdmi'
}

is_analog_name() {
  if is_hdmi_name "$1"; then
    return 1
  fi
  printf '%s' "$1" | grep -qiE 'analog|alc1220|alc[0-9]+|starship|matisse|speaker|headphone'
}

pw_meta_get() {
  local key="$1"
  local blob="$2"
  printf '%s\n' "$blob" | sed -n "s/.*key:'${key}' value:'\\([^']*\\)'.*/\\1/p" | tail -n1
}

pick_analog_alsa_hw() {
  printf '%s\n' "${1:-}" | awk '
    function consider() {
      if (cid == "") return
      blob = "hw:" cid " hw:" idx " " desc
      hdmi = (blob ~ /[Hh][Dd][Mm][Ii]|[Dd]isplay[Pp]ort|[Hh][Dd][Aa][ \t]*[Nn][Vv]idia/ || cid ~ /[Hh][Dd][Mm][Ii]/)
      analog = (!hdmi && blob ~ /[Aa]nalog|[Aa][Ll][Cc][0-9]+|[Ss]tarship|[Mm]atisse|[Ss]peaker|[Hh]eadphone/)
      if (hdmi) { cid = ""; return }
      if (analog && !got) { print "hw:" cid; got = 1 }
      if (fallback == "") fallback = "hw:" cid
      cid = ""
    }
    /^[[:space:]]*[0-9]+[[:space:]]+\[/ {
      consider()
      line = $0
      sub(/^[[:space:]]*/, "", line)
      idx = line
      sub(/[[:space:]].*/, "", idx)
      cid = line
      sub(/^[0-9]+[[:space:]]+\[/, "", cid)
      sub(/\].*/, "", cid)
      sub(/[[:space:]]+$/, "", cid)
      desc = line
      sub(/^[^]]+\][[:space:]]*:[[:space:]]*/, "", desc)
      next
    }
    { if (cid != "") desc = desc " " $0 }
    END {
      consider()
      if (!got && fallback != "") print fallback
    }
  '
}

pick_pactl_endpoint() {
  local listing="$1"
  local analog="" fallback="" name
  while IFS=$'\t' read -r _id name _rest; do
    [[ -z "${name:-}" ]] && continue
    [[ "$name" == "auto_null" ]] && continue
    [[ "$name" == *.monitor ]] && continue
    if is_hdmi_name "$name"; then
      continue
    fi
    if is_analog_name "$name"; then
      analog="$name"
      break
    fi
    if [[ -z "$fallback" ]]; then
      fallback="$name"
    fi
  done <<< "$listing"
  if [[ -n "$analog" ]]; then
    printf '%s\n' "$analog"
    return 0
  fi
  if [[ -n "$fallback" ]]; then
    printf '%s\n' "$fallback"
    return 0
  fi
  return 1
}

rewrite_qjackctl() {
  local file="$1"
  local hw="$2"
  local tmp
  [[ -f "$file" ]] || return 1
  tmp="$(mktemp)"
  awk -v hw="$hw" '
    BEGIN { IGNORECASE=1 }
    $0 ~ /^(Interface|InDevice|OutDevice)=/ {
      split($0, a, "=")
      key=a[1]
      val=substr($0, length(key)+2)
      if (val ~ /hdmi|displayport|hda[ \t]*nvidia/ || val == "" || val == "(default)" || val == "default") {
        print key "=" hw
        changed=1
        next
      }
    }
    { print }
  ' "$file" > "$tmp"
  if ! cmp -s "$file" "$tmp"; then
    if [[ ! -f "${file}.bak" ]]; then
      cp -p "$file" "${file}.bak"
    fi
    mv "$tmp" "$file"
    return 0
  fi
  rm -f "$tmp"
  return 1
}

client_pids() {
  pgrep -f 'src\.cli xair_network_bridge_client' 2>/dev/null || true
}

systemd_client_active() {
  have systemctl || return 1
  systemctl --user is-active --quiet xair-network-bridge-client.service 2>/dev/null
}

# --- PipeWire presente? -------------------------------------------------------
PW_OK=0
if pgrep -x pipewire >/dev/null 2>&1 || pgrep -x pipewire-pulse >/dev/null 2>&1; then
  PW_OK=1
elif have pactl && pactl info 2>/dev/null | grep -qi pipewire; then
  PW_OK=1
fi
if [[ "$PW_OK" -ne 1 ]]; then
  warn "PipeWire no detectado; se aplica solo QjackCtl/ALSA si hay ficheros."
fi

if have qjackctl && pgrep -x qjackctl >/dev/null 2>&1; then
  warn "QjackCtl está abierto. Ciérralo (Stop) para que relea Interface; el script no lo mata."
fi

# --- Quantum ------------------------------------------------------------------
META=""
CUR_FORCE=""
CUR_Q=""
if have pw-metadata; then
  META="$(pw-metadata -n settings 0 2>/dev/null || true)"
  CUR_FORCE="$(pw_meta_get clock.force-quantum "$META")"
  CUR_Q="$(pw_meta_get clock.quantum "$META")"
fi

NEED_Q_FIX=0
if [[ -z "$QUANTUM" ]]; then
  if [[ "$CUR_FORCE" =~ ^(64|128|256|512)$ ]]; then
    QUANTUM="$CUR_FORCE"
  elif [[ "$CUR_Q" =~ ^(64|128|256|512)$ && "$CUR_FORCE" != "1024" ]]; then
    QUANTUM="$CUR_Q"
  else
    QUANTUM=128
  fi
fi

if ! [[ "$QUANTUM" =~ ^[0-9]+$ ]] || [[ "$QUANTUM" -lt 16 || "$QUANTUM" -gt 8192 ]]; then
  warn "quantum inválido: $QUANTUM (usa 64–2048)"
  exit 2
fi

if [[ "${CUR_FORCE:-0}" == "1024" || "${CUR_Q:-}" == "1024" || -z "${CUR_FORCE:-}" || "${CUR_FORCE:-}" == "0" ]]; then
  if [[ "$QUANTUM" != "1024" ]]; then
    NEED_Q_FIX=1
  fi
fi
if [[ "${CUR_FORCE:-}" != "$QUANTUM" || "${CUR_Q:-}" != "$QUANTUM" ]]; then
  NEED_Q_FIX=1
fi

if have pw-metadata; then
  pw-metadata -n settings 0 clock.force-quantum "$QUANTUM" >/dev/null
  pw-metadata -n settings 0 clock.quantum "$QUANTUM" >/dev/null
  pw-metadata -n settings 0 clock.min-quantum 32 >/dev/null
  pw-metadata -n settings 0 clock.max-quantum 8192 >/dev/null
  log "quantum: clock.force-quantum=$QUANTUM  clock.quantum=$QUANTUM  (antes force=${CUR_FORCE:-?} quantum=${CUR_Q:-?})"
else
  warn "pw-metadata no está en PATH; no se pudo fijar el quantum."
fi

if [[ "$DO_PERSIST" -eq 1 ]]; then
  confdir="${XDG_CONFIG_HOME:-$HOME/.config}/pipewire/pipewire.conf.d"
  mkdir -p "$confdir"
  conf="$confdir/99-xair-quantum.conf"
  cat > "$conf" <<EOF
# X-Air Network Bridge — written by tools/fix_jack_pipewire.sh --persist
context.properties = {
    default.clock.quantum     = $QUANTUM
    default.clock.min-quantum = 32
    default.clock.max-quantum = 8192
}
EOF
  log "persist: $conf (hace falta reiniciar PipeWire para cargarlo; el metadata ya está activo)"
fi

# --- Analog / no HDMI ---------------------------------------------------------
CARDS=""
if [[ -r /proc/asound/cards ]]; then
  CARDS="$(cat /proc/asound/cards)"
fi
ANALOG_HW=""
if [[ -n "$CARDS" ]]; then
  ANALOG_HW="$(pick_analog_alsa_hw "$CARDS" || true)"
fi

HDMI_FIXED=0
QJACK="${XDG_CONFIG_HOME:-$HOME/.config}/rncbc.org/QjackCtl.conf"
if [[ ! -f "$QJACK" && -f "${XDG_CONFIG_HOME:-$HOME/.config}/QjackCtl/QjackCtl.conf" ]]; then
  QJACK="${XDG_CONFIG_HOME:-$HOME/.config}/QjackCtl/QjackCtl.conf"
fi

if [[ -n "$ANALOG_HW" && -f "$QJACK" ]]; then
  if rewrite_qjackctl "$QJACK" "$ANALOG_HW"; then
    HDMI_FIXED=1
    log "QjackCtl: Interface HDMI/vacío → $ANALOG_HW  ($QJACK; backup .bak si no existía)"
  else
    log "QjackCtl: Interface ya no es HDMI ($QJACK)"
  fi
elif [[ -n "$ANALOG_HW" ]]; then
  log "ALSA analog: $ANALOG_HW (sin QjackCtl.conf; ábrelo después del cliente)"
fi

SINK=""
SOURCE=""
if have pactl; then
  SINK="$(pick_pactl_endpoint "$(pactl list short sinks 2>/dev/null || true)" || true)"
  SOURCE="$(pick_pactl_endpoint "$(pactl list short sources 2>/dev/null || true)" || true)"
  if [[ -n "$SINK" ]]; then
    pactl set-default-sink "$SINK" >/dev/null 2>&1 || true
    log "PipeWire default sink (no HDMI): $SINK"
  fi
  if [[ -n "$SOURCE" ]]; then
    pactl set-default-source "$SOURCE" >/dev/null 2>&1 || true
    log "PipeWire default source (no HDMI): $SOURCE"
  fi
  if have pw-metadata && [[ -n "$SINK" ]]; then
    pw-metadata -n default 0 default.configured.audio.sink "{\"name\":\"${SINK}\"}" >/dev/null 2>&1 || true
  fi
fi

# --- Reinicio del cliente si hace falta ---------------------------------------
PIDS="$(client_pids | tr '\n' ' ')"
NEED_RESTART=0
if [[ "$RESTART_CLIENT" == "yes" ]]; then
  NEED_RESTART=1
elif [[ "$RESTART_CLIENT" == "auto" && ( "$NEED_Q_FIX" -eq 1 || "$HDMI_FIXED" -eq 1 ) ]]; then
  if [[ -n "${PIDS// /}" ]] || systemd_client_active; then
    NEED_RESTART=1
  fi
fi

if [[ "$NEED_RESTART" -eq 1 ]]; then
  if systemd_client_active; then
    log "reiniciando unidad xair-network-bridge-client.service …"
    systemctl --user restart xair-network-bridge-client.service
  elif [[ -n "${PIDS// /}" ]]; then
    log "parando xair_network_bridge_client (pids ${PIDS}) — no se toca xair_network_bridge_server"
    # SIGTERM only
    # shellcheck disable=SC2086
    kill -TERM $PIDS 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      sleep 0.3
      leftover="$(client_pids | tr '\n' ' ')"
      [[ -z "${leftover// /}" ]] && break
    done
    leftover="$(client_pids | tr '\n' ' ')"
    if [[ -n "${leftover// /}" ]]; then
      warn "el cliente no salió a tiempo (pids ${leftover}); no se envía SIGKILL"
    elif [[ -x "$ROOT/scripts/xair_network_bridge_client.sh" && -x "$ROOT/.venv/bin/python" ]]; then
      log "relanzando bash scripts/xair_network_bridge_client.sh"
      nohup "$ROOT/scripts/xair_network_bridge_client.sh" >/tmp/xair-start-client.log 2>&1 &
      sleep 0.4
    else
      warn "relanza a mano: bash scripts/xair_network_bridge_client.sh"
    fi
  else
    log "xair_network_bridge_client was not running; not started automatically. Use: bash scripts/xair_network_bridge_client.sh"
  fi
else
  log "cliente: no se reinicia (--no-restart o no hacía falta)"
fi

# --- Estado final -------------------------------------------------------------
META_AFTER=""
if have pw-metadata; then
  META_AFTER="$(pw-metadata -n settings 0 2>/dev/null || true)"
fi
FORCE_AFTER="$(pw_meta_get clock.force-quantum "$META_AFTER")"
Q_AFTER="$(pw_meta_get clock.quantum "$META_AFTER")"
IFACE_NOW=""
if [[ -f "$QJACK" ]]; then
  IFACE_NOW="$(sed -n 's/^Interface=//p' "$QJACK" | head -n1)"
fi
DEF_SINK=""
DEF_SRC=""
if have pactl; then
  DEF_SINK="$(pactl info 2>/dev/null | sed -n 's/^Default Sink: //p' || true)"
  DEF_SRC="$(pactl info 2>/dev/null | sed -n 's/^Default Source: //p' || true)"
fi

NODES=""
if have jack_lsp; then
  NODES="$(jack_lsp 2>/dev/null | sed -n 's/:.*//p' | sort -u | tr '\n' ' ')"
elif have pw-cli; then
  NODES="$(pw-cli ls Node 2>/dev/null | sed -n 's/.*node.name = "\([^"]*\)".*/\1/p' | sort -u | tr '\n' ' ')"
fi

BRIDGE="no"
if printf '%s' "$NODES" | grep -q 'xair_net_bridge'; then
  BRIDGE="yes"
fi
CLIENT_NOW="$(client_pids | tr '\n' ' ')"
[[ -z "${CLIENT_NOW// /}" ]] && CLIENT_NOW="(no)"

HDMI_IFACE="no"
if is_hdmi_name "${IFACE_NOW:-}" || is_hdmi_name "${DEF_SINK:-}"; then
  HDMI_IFACE="YES"
fi

cat <<EOF

=== estado JACK/PipeWire (XAIR) ===
pipewire:          $([[ "$PW_OK" -eq 1 ]] && echo yes || echo no)
clock.force-quantum: ${FORCE_AFTER:-?}
clock.quantum:       ${Q_AFTER:-?}
alsa analog:         ${ANALOG_HW:-?}
QjackCtl Interface:  ${IFACE_NOW:-?}
default sink:        ${DEF_SINK:-?}
default source:      ${DEF_SRC:-?}
hdmi residual:       ${HDMI_IFACE}
xair_net_bridge:     ${BRIDGE}
xair_network_bridge_client pids:   ${CLIENT_NOW}
nodos:               ${NODES:-?}

Siguiente: cierra QjackCtl si sigue abierto, abre QjackCtl, Frames/Period=${QUANTUM},
Interface analog. Verifica:
  PYTHONPATH=. python3 -m src.cli name-ports
  PYTHONPATH=. python3 -m src.cli sync-status
Guía: docs/README_JACK_PIPEWIRE.md
EOF

if [[ "$HDMI_IFACE" == "YES" ]]; then
  exit 1
fi
if [[ "${FORCE_AFTER:-}" == "1024" || "${Q_AFTER:-}" == "1024" ]]; then
  exit 1
fi
exit 0
