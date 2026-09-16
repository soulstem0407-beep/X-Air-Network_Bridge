"""
Detección y guía para dispositivos virtuales (user-space / drivers de terceros).

No se puede crear un dispositivo ASIO/WASAPI renombrado a 'X-AIR Network Bridge'
desde Python: el DAW verá el nombre real de VB-Cable, BlackHole o ALSA loopback.
"""
from __future__ import annotations

import logging
import platform
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


class OSFamily(str, Enum):
    LINUX = "linux"
    DARWIN = "darwin"
    WINDOWS = "windows"
    OTHER = "other"


@dataclass
class VirtualDeviceHint:
    name_substring: str
    notes: str


def detect_os() -> OSFamily:
    s = platform.system().lower()
    if s == "linux":
        return OSFamily.LINUX
    if s == "darwin":
        return OSFamily.DARWIN
    if s == "windows":
        return OSFamily.WINDOWS
    return OSFamily.OTHER


def linux_loopback_loaded() -> bool:
    cards = Path("/proc/asound/cards")
    if not cards.is_file():
        return False
    try:
        text = cards.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return False
    return "loopback" in text


def linux_enable_loopback() -> bool:
    """
    Intenta `modprobe snd-aloop`; requiere permisos administrativos.
    Devuelve True si parece tener éxito.
    """
    try:
        r = subprocess.run(
            ["modprobe", "snd-aloop", "pcm_substreams=16"],
            check=False,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            log.error("modprobe snd-aloop falló (¿root?): %s", (r.stderr or r.stdout).strip())
            return False
        log.info("Módulo snd-aloop cargado.")
        return True
    except FileNotFoundError:
        log.error("modprobe no encontrado.")
        return False


def hints_for_this_os() -> List[VirtualDeviceHint]:
    fam = detect_os()
    if fam == OSFamily.LINUX:
        return [
            VirtualDeviceHint(
                "xair_net_bridge",
                "xair_network_bridge_client (sin XAIR_OUTPUT_DEVICE) publica 18 in + 18 out JACK/PipeWire "
                "con los nombres OSC de la mesa (_in/_out). XAIR_VIRTUAL_PORT_NAMES=false vuelve al sink Pulse.",
            ),
            VirtualDeviceHint(
                "loopback",
                "Ej.: salida jack en hw:Loopback,0, subdevice 1; el DAW escucha otro lado del par.",
            ),
            VirtualDeviceHint("[Loopback PCM]", "Nombre típico visto por PortAudio bajo Pulse/PipeWire."),
        ]
    if fam == OSFamily.WINDOWS:
        return [
            VirtualDeviceHint("cable input", "VB-Audio Cable Input (entrada virtual)."),
            VirtualDeviceHint("vb-audio", "Instala VB-Cable; elige ese dispositivo de salida en .env."),
        ]
    if fam == OSFamily.DARWIN:
        return [
            VirtualDeviceHint("blackhole", "BlackHole 16ch o multi para 18 rutas combinadas cuando aplique."),
        ]
    return [
        VirtualDeviceHint(
            "",
            "Instala cable virtual del fabricante compatible multicanal; ajusta XAIR_OUTPUT_DEVICE.",
        ),
    ]


def suggest_output_query() -> Optional[str]:
    for h in hints_for_this_os():
        if h.name_substring:
            return h.name_substring
    return None


def summarize_for_readme() -> str:
    """Texto corto reusable en documentación."""
    lines = []
    for h in hints_for_this_os():
        lines.append(f"- `{h.name_substring}`: {h.notes}")
    return "\n".join(lines)

