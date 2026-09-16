"""
Salida virtual multicanal en Linux: PulseAudio/PipeWire (module-null-sink) y refuerzo ALSA loopback.

Detecta el servidor con `pactl info`, crea el sink `xair_net_bridge`, guarda ids de módulos para unload.

Los puertos JACK/PipeWire *nombrados* (``01_Kick_in`` / ``01_Kick_out``) no salen de este sink: ver
``jack_named_output.py``. Este módulo es el fallback PortAudio cuando
``XAIR_VIRTUAL_PORT_NAMES=false`` o JACK no está disponible.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from typing import Iterable, List, Optional, Sequence, Tuple

from .x18_device import is_analog_device, is_hdmi_device

log = logging.getLogger(__name__)

XAIR_NULL_SINK_NAME = "xair_net_bridge"

# Puntuación por subcadena (nombre del dispositivo PortAudio, comparación en minúsculas).
_KEYWORDS_SCORE: Tuple[str, ...] = (
    "loopback",
    "monitor",
    "pcm",
    "pipewire",
    "pulse",
    "jack",
    "system",
    "xair",
    "x-air",
    "xair_net_bridge",
)


def _truthy_env_auto_virtual() -> bool:
    v = os.getenv("XAIR_AUTO_VIRTUAL_OUTPUT")
    if v is None or not str(v).strip():
        return True
    return str(v).strip().lower() != "false"


def should_try_linux_auto_virtual(output_device_query: Optional[str]) -> bool:
    """Linux, sin XAIR_OUTPUT_DEVICE y XAIR_AUTO_VIRTUAL_OUTPUT distinto de 'false'."""
    if sys.platform != "linux":
        return False
    if output_device_query and str(output_device_query).strip():
        return False
    return _truthy_env_auto_virtual()


def should_try_named_jack(output_device_query: Optional[str]) -> bool:
    """Named JACK/PipeWire ports: Linux, no XAIR_OUTPUT_DEVICE, not explicitly off.

    Default on. Set ``XAIR_VIRTUAL_PORT_NAMES=false`` to keep the Pulse null-sink
    + PortAudio path. When JACK is used, capture + playback ports are always
    created (not an extra env flag). OSC names are read at setup, not in RT.
    """
    if sys.platform != "linux":
        return False
    if output_device_query and str(output_device_query).strip():
        return False
    v = os.getenv("XAIR_VIRTUAL_PORT_NAMES")
    if v is not None and str(v).strip():
        return str(v).strip().lower() not in ("0", "false", "no", "off")
    return True


def unload_null_sink_named(sink_name: str) -> None:
    """Unload a leftover module-null-sink so a JACK client can reuse the name."""
    pactl = _which("pactl")
    if not pactl:
        return
    r = _run([pactl, "list", "short", "modules"], timeout=8.0)
    if r.returncode != 0:
        return
    needle = f"sink_name={sink_name}"
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        blob = "\t".join(parts[1:])
        if needle not in blob:
            continue
        mid = parts[0]
        try:
            int(mid)
        except ValueError:
            continue
        log.info(
            "Descargando sink Pulse/PipeWire previo %r (module %s) para el cliente JACK.",
            sink_name,
            mid,
        )
        ur = _run([pactl, "unload-module", str(mid)], timeout=8.0)
        if ur.returncode != 0:
            log.debug("unload-module %s: %s", mid, (ur.stderr or ur.stdout or "").strip())


def _which(exe: str) -> Optional[str]:
    return shutil.which(exe)


def _run(argv: Sequence[str], *, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def pulse_server_ok() -> bool:
    """Detectar PulseAudio/PipeWire: `pactl info` debe tener éxito."""
    pactl = _which("pactl")
    if not pactl:
        return False
    r = _run([pactl, "info"], timeout=8.0)
    return r.returncode == 0


def _pulse_sink_name_present(sink_name: str) -> bool:
    pactl = _which("pactl")
    if not pactl:
        return False
    r = _run([pactl, "list", "short", "sinks"], timeout=8.0)
    if r.returncode != 0:
        return False
    needle = sink_name.lower()
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and needle in parts[1].lower():
            return True
    return False


def load_pulse_null_sink_multichannel(*, channels: int, sample_rate: int) -> Tuple[bool, Optional[int]]:
    """
    pactl load-module module-null-sink sink_name=xair_net_bridge channels=<N> rate=<SR>
    sink_properties=device.description="X-AIR_Network_Bridge_<N>ch"
    Reintento tras 0.45 s si falla.
    """
    pactl = _which("pactl")
    if not pactl:
        return False, None

    if _pulse_sink_name_present(XAIR_NULL_SINK_NAME):
        log.info("Sink Pulse/PipeWire '%s' ya existe; reutilizando.", XAIR_NULL_SINK_NAME)
        return True, None

    n = int(channels)
    sr = int(sample_rate)
    desc = f"X-AIR_Network_Bridge_{n}ch"
    sink_props = f'sink_properties=device.description="{desc}"'

    cmd = [
        pactl,
        "load-module",
        "module-null-sink",
        f"sink_name={XAIR_NULL_SINK_NAME}",
        f"channels={n}",
        f"rate={sr}",
        sink_props,
    ]

    r = _run(cmd, timeout=15.0)
    if r.returncode != 0:
        log.warning(
            "module-null-sink falló (%s): %s — reintento en 0.45s…",
            r.returncode,
            (r.stderr or r.stdout or "").strip(),
        )
        time.sleep(0.45)
        r = _run(cmd, timeout=15.0)

    if r.returncode != 0:
        log.warning(
            "module-null-sink falló de forma definitiva (%s): %s",
            r.returncode,
            (r.stderr or r.stdout or "").strip(),
        )
        return False, None

    raw = (r.stdout or "").strip()
    try:
        mid = int(raw.split()[0])
    except (ValueError, IndexError):
        mid = None
    log.info(
        "Pulse/PipeWire: module-null-sink cargado (module id=%s, %s ch @ %s Hz, description=%s).",
        mid,
        n,
        sr,
        desc,
    )
    return True, mid


def load_pulse_module_loopback_latency() -> Optional[int]:
    """Fallback: pactl load-module module-loopback latency_msec=1"""
    pactl = _which("pactl")
    if not pactl:
        return None
    r = _run([pactl, "load-module", "module-loopback", "latency_msec=1"], timeout=10.0)
    if r.returncode != 0:
        log.debug("module-loopback: %s", (r.stderr or r.stdout or "").strip())
        return None
    raw = (r.stdout or "").strip()
    try:
        mid = int(raw.split()[0])
    except (ValueError, IndexError):
        mid = None
    if mid is not None:
        log.info("Pulse/PipeWire: module-loopback cargado (latency_msec=1, module id=%s).", mid)
    return mid


def score_output_device_name(name: str) -> int:
    if is_hdmi_device(name):
        return -1000
    n = name.lower()
    score = 0
    for kw in _KEYWORDS_SCORE:
        if kw.lower() in n:
            score += 10
    if is_analog_device(name):
        score += 20
    return score


def pick_best_output_device_index(
    sd_module,
    *,
    channels: int,
    sample_rate: int,
) -> Tuple[Optional[int], Optional[str]]:
    """sounddevice.query_devices(): mejor candidato con max_output_channels >= N."""
    try:
        devices = sd_module.query_devices()
    except Exception as exc:
        log.error("sounddevice.query_devices() falló: %s", exc)
        return None, None

    best_idx: Optional[int] = None
    best_score = -1
    best_name: Optional[str] = None
    best_sr_delta = 10**9

    for i, d in enumerate(devices):
        try:
            max_out = int(d["max_output_channels"])
        except (KeyError, TypeError):
            continue
        if max_out < channels:
            continue
        name = str(d.get("name", ""))
        if is_hdmi_device(name):
            continue
        low = name.lower().strip()
        if low in ("default", "hdmi") or "reaper" in low:
            continue
        score = score_output_device_name(name)
        sr_def = d.get("default_samplerate")
        try:
            sr_delta = abs(float(sr_def) - float(sample_rate)) if sr_def else 999999.0
        except (TypeError, ValueError):
            sr_delta = 999999.0

        better = (
            best_idx is None
            or score > best_score
            or (score == best_score and sr_delta < best_sr_delta)
        )
        if better:
            best_idx = i
            best_score = score
            best_name = name
            best_sr_delta = sr_delta

    return best_idx, best_name


def wait_for_output_device(
    sd_module,
    *,
    channels: int,
    sample_rate: int,
    attempts: int = 10,
    delay_s: float = 0.35,
) -> Tuple[Optional[int], Optional[str]]:
    """
    Hasta `attempts` intentos con pausa `delay_s`, hasta que PortAudio exponga
    un dispositivo de salida con >= N canales (priorizando el sink virtual por puntuación).
    """
    idx: Optional[int] = None
    name: Optional[str] = None
    for attempt in range(attempts):
        idx, name = pick_best_output_device_index(sd_module, channels=channels, sample_rate=sample_rate)
        if idx is None:
            time.sleep(delay_s)
            continue
        # Preferir dispositivo que coincida con las subcadenas del sink virtual / Pulse.
        if score_output_device_name(name or "") > 0 or attempt >= attempts - 1:
            return idx, name
        time.sleep(delay_s)
    return idx, name


def prepare_linux_virtual_audio_environment(
    *,
    channels: int,
    sample_rate: int,
    pulse_modules_loaded: List[int],
) -> None:
    """null-sink → fallback loopback → ALSA loopback si hace falta."""
    if pulse_server_ok():
        ok, mid = load_pulse_null_sink_multichannel(channels=channels, sample_rate=sample_rate)
        if ok and mid is not None:
            pulse_modules_loaded.append(mid)
        if not ok:
            lb = load_pulse_module_loopback_latency()
            if lb is not None:
                pulse_modules_loaded.append(lb)
    else:
        log.info("pactl info no disponible o servidor no responde; probando ALSA loopback.")

    try:
        from .bridge_device import linux_loopback_loaded, linux_enable_loopback

        if not linux_loopback_loaded():
            log.info("ALSA loopback: intentando linux_enable_loopback()…")
            linux_enable_loopback()
    except Exception:
        log.debug("ALSA loopback omitido", exc_info=True)


def unload_pulse_modules(module_ids: Iterable[int]) -> None:
    """Descarga solo los módulos cargados por esta sesión (module-id guardados)."""
    pactl = _which("pactl")
    if not pactl:
        return
    for mid in reversed(list(module_ids)):
        try:
            r = _run([pactl, "unload-module", str(int(mid))], timeout=8.0)
            if r.returncode == 0:
                log.info("Pulse/PipeWire: unload-module %s", mid)
            else:
                log.debug("unload-module %s: %s", mid, (r.stderr or r.stdout).strip())
        except Exception:
            log.debug("unload-module %s falló", mid, exc_info=True)
