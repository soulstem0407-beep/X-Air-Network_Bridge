"""Force the Behringer X18 / X AIR as the PortAudio physical device.

Why HDMI was selected
---------------------
PortAudio (sounddevice) treats ``device=None`` as the host-API default.
On Linux that is almost always the first ALSA/PipeWire playback node —
``hw:HDMI``, ``HDA Intel PCH HDMI``, NVIDIA HDMI, etc. — because cards
are enumerated in driver order, not by USB identity. ``XAIR_INPUT_DEVICE`` /
``XAIR_OUTPUT_DEVICE`` being empty used that default. Scoring “first device
with enough channels” could still land on HDMI if it advertised many
channels.

How the X18 is forced
---------------------
Every physical PortAudio open (capture or playback) enumerates
``query_devices()``, rejects HDMI/DisplayPort, and picks the best match
by name, ALSA hw id, and USB vendor/product. This is hard-coded — not
gated on ``.env``, ``XAIR_VIRTUAL_PORT_NAMES``, or device order.

Match patterns (case-insensitive)
---------------------------------
* Product: ``X18``, ``XR18``, ``X AIR`` / ``X-AIR`` / ``XAIR``, ``Behringer X18``
* ALSA: ``hw:X18``, ``hw:XR18``, ``hw:USB``, ``USB Audio``
* USB: vendor ``0x1397`` (Music Group / Behringer) when readable from
  ``/proc/asound/card*/usbid`` or sysfs; optional X-AIR product ids.

If the X18 is not connected, ``require_x18_portaudio_device`` raises
``RuntimeError`` with the full PortAudio list. HDMI is never used.

This helper is for ``xair_network_bridge_server`` USB capture (and USB return playback).
``xair_network_bridge_client`` must not call it: the mixer is often on another machine
and this host only has JACK/PipeWire + REAPER.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

# Exact line xair_network_bridge_client must print (tests and operators grep this).
FORCED_SELECTION_LOG = "Selected physical device: X18 (forced)"

# Music Group / Behringer. X-AIR family product ids seen in the wild.
BEHRINGER_USB_VID = 0x1397
XAIR_USB_PIDS = frozenset({0x00AA, 0x00AB, 0x00AC, 0x00AD, 0x00AE, 0x00AF})

_HDMI_RE = re.compile(
    r"hdmi|displayport|display\s*port|"
    r"hw:\s*hdmi|"
    r"hda\s*nvidia|"
    r"nvidia:\s*hdmi|ati\s*hdmi",
    re.IGNORECASE,
)
_ANALOG_RE = re.compile(
    r"analog|alc1220|alc\d+|starship|matisse|speaker|headphone|\bline\b",
    re.IGNORECASE,
)

_X18_PRODUCT_RE = re.compile(
    r"(xr[\s\-_]?18|"
    r"x[\s\-_]?18|"
    r"x[\s\-_]?air|"
    r"xair|"
    r"behringer.*(?:x[\s\-_]?18|xr[\s\-_]?18|x[\s\-_]?air))",
    re.IGNORECASE,
)

_ALSA_X18_RE = re.compile(r"hw:\s*x18|hw:\s*xr18", re.IGNORECASE)
_ALSA_USB_RE = re.compile(r"hw:\s*usb\b|usb\s*audio", re.IGNORECASE)


@dataclass(frozen=True)
class X18Device:
    index: int
    name: str
    max_input_channels: int
    max_output_channels: int
    score: int
    reason: str


def is_hdmi_device(name: str) -> bool:
    """True for HDMI / DisplayPort nodes that must never be opened."""
    return bool(_HDMI_RE.search(str(name or "")))


def is_analog_device(name: str) -> bool:
    """Onboard analog (Realtek ALC* / similar), never HDMI or S/PDIF-only."""
    s = str(name or "")
    if is_hdmi_device(s):
        return False
    if re.search(r"iec958|spdif|digital", s, re.I) and "analog" not in s.lower():
        return False
    return bool(_ANALOG_RE.search(s))


_CARD_LINE_RE = re.compile(r"^\s*(\d+)\s+\[([^\]]+)\]\s*:\s*(.*)$")
_QJACK_CONF = Path.home() / ".config/rncbc.org/QjackCtl.conf"


def iter_alsa_cards(raw: str) -> List[Tuple[int, str, str]]:
    """``(index, id, description)`` from ``/proc/asound/cards`` text."""
    lines = raw.splitlines()
    cards: List[Tuple[int, str, str]] = []
    i = 0
    while i < len(lines):
        m = _CARD_LINE_RE.match(lines[i])
        if m:
            extra = ""
            if i + 1 < len(lines) and not _CARD_LINE_RE.match(lines[i + 1]):
                extra = lines[i + 1].strip()
            desc = f"{m.group(3).strip()} {extra}".strip()
            cards.append((int(m.group(1)), m.group(2).strip(), desc))
        i += 1
    return cards


def pick_analog_alsa_hw(raw: str) -> Optional[str]:
    """Prefer ``hw:ALC…`` analog; never ``hw:HDMI``."""
    fallback: Optional[str] = None
    for idx, cid, desc in iter_alsa_cards(raw):
        blob = f"hw:{cid} hw:{idx} {desc}"
        if is_hdmi_device(blob) or is_hdmi_device(cid):
            continue
        hw = f"hw:{cid}"
        if is_analog_device(blob) or is_analog_device(cid):
            return hw
        if fallback is None:
            fallback = hw
    return fallback


def rewrite_qjackctl_devices(text: str, hw: str) -> str:
    """Replace HDMI / empty QjackCtl Interface with analog ``hw``."""

    def repl(m: re.Match[str]) -> str:
        key, val = m.group(1), m.group(2)
        stripped = val.strip()
        if is_hdmi_device(stripped) or stripped in ("", "(default)", "default"):
            return f"{key}={hw}"
        return m.group(0)

    return re.sub(
        r"(?m)^(Interface|InDevice|OutDevice)=(.*)$",
        repl,
        text,
    )


def _pactl(*args: str) -> Optional[str]:
    try:
        r = subprocess.run(
            ["pactl", *args],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    return r.stdout or ""


def pick_pactl_endpoint(listing: str) -> Optional[str]:
    analog: Optional[str] = None
    fallback: Optional[str] = None
    for line in listing.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        name = parts[1].strip()
        if not name or name == "auto_null" or is_hdmi_device(name):
            continue
        if name.endswith(".monitor"):
            continue
        if is_analog_device(name):
            analog = analog or name
        elif fallback is None:
            fallback = name
    return analog or fallback


def _write_qjackctl_analog(hw: str) -> None:
    path = _QJACK_CONF
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    updated = rewrite_qjackctl_devices(text, hw)
    if updated == text:
        return
    try:
        path.write_text(updated, encoding="utf-8")
        log.info("QjackCtl Interface HDMI → %s", hw)
    except OSError:
        log.debug("no se pudo escribir %s", path, exc_info=True)


def force_analog_session_defaults() -> Optional[str]:
    """PipeWire default sink/source and QjackCtl Interface: analog, never HDMI."""
    sinks = _pactl("list", "short", "sinks") or ""
    sources = _pactl("list", "short", "sources") or ""
    sink = pick_pactl_endpoint(sinks)
    source = pick_pactl_endpoint(sources)
    if sink:
        _pactl("set-default-sink", sink)
        log.info("PipeWire default sink (no HDMI): %s", sink)
        try:
            subprocess.run(
                [
                    "pw-metadata",
                    "-n",
                    "default",
                    "0",
                    "default.configured.audio.sink",
                    '{"name":"%s"}' % sink,
                ],
                capture_output=True,
                timeout=0.5,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            pass
    if source:
        _pactl("set-default-source", source)
        log.info("PipeWire default source (no HDMI): %s", source)
    cards = _read_text(Path("/proc/asound/cards")) or ""
    hw = pick_analog_alsa_hw(cards)
    if hw:
        _write_qjackctl_analog(hw)
        log.info("ALSA analog (no HDMI): %s", hw)
    return sink or hw


def _read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _parse_usbid(raw: str) -> Optional[Tuple[int, int]]:
    s = (raw or "").strip().lower().replace("0x", "")
    m = re.match(r"([0-9a-f]{1,4})[:/]([0-9a-f]{1,4})$", s)
    if not m:
        return None
    return int(m.group(1), 16), int(m.group(2), 16)


def _sysfs_usb_ids(card_index: int) -> Optional[Tuple[int, int]]:
    base = Path(f"/sys/class/sound/card{card_index}")
    if not base.exists():
        return None
    node: Optional[Path] = base / "device"
    for _ in range(8):
        if node is None or not node.exists():
            break
        vid_s = _read_text(node / "idVendor")
        pid_s = _read_text(node / "idProduct")
        if vid_s and pid_s:
            try:
                return int(vid_s, 16), int(pid_s, 16)
            except ValueError:
                return None
        node = (node / "..").resolve() if (node / "..").exists() else None
    return None


def read_alsa_usb_ids() -> List[Tuple[int, str, int, int]]:
    """``(card_index, card_id, vid, pid)`` for USB sound cards, if the kernel exposes them."""
    out: List[Tuple[int, str, int, int]] = []
    cards = Path("/proc/asound/cards")
    raw = _read_text(cards)
    id_by_index: dict[int, str] = {}
    if raw:
        for ln in raw.splitlines():
            m = re.match(r"\s*(\d+)\s+\[([^\]]+)\]", ln)
            if m:
                id_by_index[int(m.group(1))] = m.group(2).strip()
    proc = Path("/proc/asound")
    if not proc.is_dir():
        return out
    for child in sorted(proc.iterdir()):
        if not child.name.startswith("card") or not child.name[4:].isdigit():
            continue
        idx = int(child.name[4:])
        parsed = _parse_usbid(_read_text(child / "usbid") or "")
        if parsed is None:
            parsed = _sysfs_usb_ids(idx)
        if parsed is None:
            continue
        vid, pid = parsed
        card_id = id_by_index.get(idx, "")
        out.append((idx, card_id, vid, pid))
    return out


def _usb_bonus(name: str, usb_ids: Sequence[Tuple[int, str, int, int]]) -> Tuple[int, str]:
    n = name.lower()
    best = 0
    reason = ""
    for idx, card_id, vid, pid in usb_ids:
        cid = (card_id or "").strip().lower()
        belongs = False
        if cid and cid in n:
            belongs = True
        if f"hw:{idx}" in n.replace(" ", "") or f"hw:{idx}," in n:
            belongs = True
        if not belongs:
            continue
        if vid == BEHRINGER_USB_VID:
            extra = 120 if pid in XAIR_USB_PIDS else 100
            if extra > best:
                best = extra
                reason = f"usb {vid:04x}:{pid:04x} card={card_id or idx}"
    return best, reason


def score_x18_candidate(
    name: str,
    *,
    max_input_channels: int = 0,
    max_output_channels: int = 0,
    usb_ids: Sequence[Tuple[int, str, int, int]] = (),
) -> Tuple[int, str]:
    """Score a PortAudio device. 0 = reject (HDMI or unrelated)."""
    if is_hdmi_device(name):
        return 0, "hdmi-rejected"
    n = str(name or "")
    score = 0
    reasons: List[str] = []
    if _X18_PRODUCT_RE.search(n):
        score += 100
        reasons.append("product-name")
    if _ALSA_X18_RE.search(n):
        score += 90
        reasons.append("alsa-hw:X18")
    usb_pts, usb_why = _usb_bonus(n, usb_ids)
    if usb_pts:
        score += usb_pts
        reasons.append(usb_why)
    if _ALSA_USB_RE.search(n):
        # Generic USB only if the node looks like a mixer (18-ch class-compliant).
        if max(max_input_channels, max_output_channels) >= 18:
            score += 15
            reasons.append("alsa-usb-18ch")
    if score <= 0:
        return 0, "no-match"
    return score, ",".join(reasons) if reasons else "match"


def _device_rows(sd_module: Any) -> List[Tuple[int, dict]]:
    devices = sd_module.query_devices()
    rows: List[Tuple[int, dict]] = []
    for i, d in enumerate(devices):
        if not isinstance(d, dict):
            try:
                d = dict(d)
            except Exception:
                continue
        rows.append((i, d))
    return rows


def iter_x18_candidates(
    sd_module: Any,
    *,
    usb_ids: Optional[Sequence[Tuple[int, str, int, int]]] = None,
) -> List[X18Device]:
    ids = list(usb_ids) if usb_ids is not None else read_alsa_usb_ids()
    found: List[X18Device] = []
    for i, d in _device_rows(sd_module):
        name = str(d.get("name", ""))
        try:
            chin = int(d.get("max_input_channels") or 0)
        except (TypeError, ValueError):
            chin = 0
        try:
            chout = int(d.get("max_output_channels") or 0)
        except (TypeError, ValueError):
            chout = 0
        score, reason = score_x18_candidate(
            name,
            max_input_channels=chin,
            max_output_channels=chout,
            usb_ids=ids,
        )
        if score <= 0:
            continue
        found.append(
            X18Device(
                index=i,
                name=name,
                max_input_channels=chin,
                max_output_channels=chout,
                score=score,
                reason=reason,
            )
        )
    found.sort(key=lambda x: (-x.score, -x.max_input_channels, -x.max_output_channels, x.index))
    return found


def _format_device_list(sd_module: Any) -> str:
    lines = []
    for i, d in _device_rows(sd_module):
        name = d.get("name", "")
        tag = " [HDMI-ignored]" if is_hdmi_device(str(name)) else ""
        lines.append(
            f"  [{i}] {name} in={d.get('max_input_channels')} out={d.get('max_output_channels')}{tag}"
        )
    return "\n".join(lines) if lines else "  (ningún dispositivo PortAudio)"


def require_x18_portaudio_device(
    sd_module: Any,
    *,
    min_input_channels: int = 0,
    min_output_channels: int = 0,
    usb_ids: Optional[Sequence[Tuple[int, str, int, int]]] = None,
) -> X18Device:
    """Return the forced X18 PortAudio device or raise.

    Never returns HDMI. Never returns the host default. Independent of
    ``.env`` and of ALSA/PipeWire enumeration order.
    """
    try:
        cands = iter_x18_candidates(sd_module, usb_ids=usb_ids)
    except Exception as exc:
        raise RuntimeError(
            "No se pudo enumerar dispositivos PortAudio para forzar la X18 "
            f"({exc}). Conecta la mesa por USB y comprueba sounddevice."
        ) from exc

    for dev in cands:
        if min_input_channels and dev.max_input_channels < min_input_channels:
            continue
        if min_output_channels and dev.max_output_channels < min_output_channels:
            continue
        log.info(FORCED_SELECTION_LOG)
        log.info(
            "X18 PortAudio [%s] %s (in=%s out=%s score=%s %s)",
            dev.index,
            dev.name,
            dev.max_input_channels,
            dev.max_output_channels,
            dev.score,
            dev.reason,
        )
        return dev

    listing = _format_device_list(sd_module)
    need = []
    if min_input_channels:
        need.append(f">={min_input_channels} entradas")
    if min_output_channels:
        need.append(f">={min_output_channels} salidas")
    extra = (" con " + " y ".join(need)) if need else ""
    raise RuntimeError(
        "X18 no encontrada en PortAudio" + extra + ". "
        "HDMI y el dispositivo por defecto están prohibidos; no hay fallback. "
        "Conecta la Behringer X18/XR18 / X AIR por USB (no uses la salida HDMI). "
        "Dispositivos enumerados:\n" + listing
    )


def log_forced_x18(dev: X18Device) -> None:
    """Idempotent operator-facing line (safe if already logged)."""
    log.info(FORCED_SELECTION_LOG)
    log.info("physical=%s index=%s", dev.name, dev.index)
