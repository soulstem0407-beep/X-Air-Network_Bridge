"""Sanitize mixer channel names into JACK/PipeWire port labels.

Control-plane only: OSC GETs run at setup (or from the CLI), never from the
UDP receiver or the JACK/PortAudio callback.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from typing import List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

DEFAULT_JACK_CLIENT_NAME = "xair_net_bridge"
# Short-name budget after ``NN_`` prefix; full JACK names are ``client:port``.
_MAX_LABEL = 40


def jack_client_name() -> str:
    raw = os.getenv("XAIR_JACK_CLIENT_NAME")
    if raw and str(raw).strip():
        return str(raw).strip()[:jack_safe_client_len()]
    return DEFAULT_JACK_CLIENT_NAME


def jack_safe_client_len() -> int:
    return 32


def fallback_label(ch: int) -> str:
    return f"CH{int(ch):02d}"


def sanitize_label(raw: Optional[str], ch: int) -> str:
    """ASCII JACK-safe token (no colon). Empty mixer names become CH01…"""
    s = unicodedata.normalize("NFKD", str(raw or ""))
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.replace(":", "_")
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", s)
    s = s.strip("_")
    if not s:
        s = fallback_label(ch)
    return s[:_MAX_LABEL]


def unique_port_names(labels: Sequence[str]) -> List[str]:
    """``01_Kick``, ``02_Snare`` — index prefix keeps order if names collide."""
    seen: dict[str, int] = {}
    out: List[str] = []
    for i, lab in enumerate(labels, start=1):
        base = f"{i:02d}_{lab}" if lab else f"{i:02d}_{fallback_label(i)}"
        name = base
        n = 2
        key = name.lower()
        while key in seen:
            name = f"{base}_{n}"
            key = name.lower()
            n += 1
        seen[key] = 1
        out.append(name)
    return out


def _strip_io_suffix(name: str) -> str:
    s = str(name or "")
    low = s.lower()
    if low.endswith("_out") or low.endswith("_in"):
        return s[: s.rfind("_")]
    return s


def duplex_io_names(stems: Sequence[str]) -> Tuple[List[str], List[str]]:
    """Same channel order: capture ``{stem}_in``, playback ``{stem}_out``.

    JACK port names must be unique on the client, so in and out cannot share
    a short name. Stems come from ``unique_port_names`` (OSC labels); order
    is 1..N and is not reshuffled. Empty mixer names become
    ``01_CH01_in`` / ``01_CH01_out`` (the ``CH01_in`` pattern plus the
    existing ``NN_`` prefix — prefix and order are not changed).
    """
    ins: List[str] = []
    outs: List[str] = []
    for i, stem in enumerate(stems, start=1):
        base = _strip_io_suffix(stem) or f"{i:02d}_{fallback_label(i)}"
        ins.append(f"{base}_in")
        outs.append(f"{base}_out")
    return ins, outs


def labels_from_getter(channels: int, getter) -> Tuple[List[str], List[str]]:
    """Call ``getter(ch)`` for 1..N. Continues on errors (timeouts)."""
    labels: List[str] = []
    errors: List[str] = []
    n = max(1, min(18, int(channels)))
    for ch in range(1, n + 1):
        try:
            raw = getter(ch)
        except Exception as exc:
            errors.append(f"ch{ch}.name: {exc}")
            labels.append(fallback_label(ch))
            continue
        labels.append(sanitize_label(raw if raw is None else str(raw), ch))
    return labels, errors


def fetch_osc_channel_labels(
    channels: int,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> Tuple[List[str], List[str]]:
    """GET ``/ch/XX/config/name``. Ping first; on failure use CH01… without 18 timeouts."""
    n = max(1, min(18, int(channels)))
    fallback = [fallback_label(ch) for ch in range(1, n + 1)]
    try:
        from ..xair_control.osc_bridge import XAirOSCBridge, XAirOSCError
    except Exception as exc:
        log.info("OSC no disponible para nombres de puerto (%s).", exc)
        return fallback, [str(exc)]

    errors: List[str] = []
    try:
        bridge = XAirOSCBridge.from_env(host=host, port=port)
    except Exception as exc:
        return fallback, [str(exc)]

    try:
        with bridge as br:
            try:
                br.ping()
            except XAirOSCError as exc:
                log.info("Mesa OSC no responde; puertos JACK usarán CH01… (%s).", exc)
                return fallback, [str(exc)]
            # Per-channel GET; keep this off the realtime threads.
            br.timeout_s = min(float(br.timeout_s), 0.6)
            labels, errors = labels_from_getter(n, br.get_channel_name)
            return labels, errors
    except Exception as exc:
        log.info("No se pudieron leer nombres OSC (%s); fallback CH01…", exc)
        return fallback, [str(exc)]


def resolve_playback_port_names(
    channels: int,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> Tuple[List[str], List[str]]:
    """Labels from the mixer, then unique ``NN_Name`` JACK port short names."""
    labels, errors = fetch_osc_channel_labels(channels, host=host, port=port)
    return unique_port_names(labels), errors
