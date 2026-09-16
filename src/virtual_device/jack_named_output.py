"""JACK/PipeWire duplex client: named capture (_in) and playback (_out) ports.

This is the Linux DAW-facing path: PortAudio is not used when the client
starts. The process callback only copies from the existing decoded queue
(same contract as the PortAudio callback). OSC is never called from here.

Bug (full-duplex missing in QjackCtl / REAPER)
----------------------------------------------
The client used to register only ``client.outports`` and set PipeWire
``media.class = Audio/Source``. That does two things:

1. No JACK input (capture) ports are created, so QjackCtl shows only
   ``CH##_out`` / ``01_Kick`` playback terminals.
2. PipeWire treats the node as capture-only, so even if inports were
   registered they would not show up as a duplex device in REAPER.

Fix: always register the same number of ``inports`` and ``outports``
(channel order unchanged), and advertise ``media.class = Audio/Duplex``.
This is mandatory for the JACK path — not gated on ``.env``.
Input buffers are read in the RT callback (consumed) so the JACK cycle
is complete; they are not injected into UDP/jitter/FEC/Opus.

After activate, ``JackGraphRouter`` keeps this patchbay (never HDMI):
System input → this client ``*_in`` → this client ``*_out`` → REAPER → System output.
WirePlumber autoconnect stays off so playback cannot skip REAPER.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from typing import Any, Callable, Optional, Sequence

import numpy as np

from .port_names import duplex_io_names
from .jack_graph import JackGraphRouter
from .x18_device import force_analog_session_defaults

log = logging.getLogger(__name__)

# WirePlumber otherwise auto-links playback to the default speakers.
# Do NOT set node.lock-quantum=false: PipeWire then snaps Frames/Period
# back to default.clock.quantum (1024) after every QjackCtl change.
_DEFAULT_PW_PROPS = "{ node.autoconnect = false media.class = Audio/Duplex }"

_FORCE_QUANTUM_RE = re.compile(r"\bnode\.force-quantum\s*=\s*\S+", re.I)
_NODE_LATENCY_RE = re.compile(r"\bnode\.latency\s*=\s*\S+", re.I)
_LOCK_QUANTUM_RE = re.compile(r"\bnode\.lock-quantum\s*=\s*\S+", re.I)

# PipeWire session default; QjackCtl changes must not be overwritten with this.
_PIPEWIRE_DEFAULT_QUANTUM = 1024


def jack_module_available() -> bool:
    try:
        import jack  # noqa: F401
    except Exception:
        return False
    return True


def _strip_forced_quantum(props: str) -> str:
    """Drop per-node quantum pins. Do not inject lock-quantum=false (that snaps to 1024)."""
    s = _FORCE_QUANTUM_RE.sub("", props)
    s = _NODE_LATENCY_RE.sub("", s)
    s = _LOCK_QUANTUM_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def _unlock_qjackctl_period() -> None:
    """Do not pin or auto-adapt Frames/Period.

    ``node.lock-quantum = false`` lets PipeWire rewrite the period to the
    session default (1024) after every QjackCtl change. Leave lock-quantum
    unset. Do not set client.blocksize except to undo that 1024 snap-back.
    """
    raw = os.environ.get("PIPEWIRE_PROPS") or ""
    s = _strip_forced_quantum(raw)
    os.environ["PIPEWIRE_PROPS"] = s if s else _DEFAULT_PW_PROPS


def pin_pipewire_clock_quantum(frames: int) -> bool:
    """Pin the PipeWire session quantum so it cannot snap back to 1024.

    QjackCtl's Frames/Period is ignored by PipeWire's clock unless
    ``clock.force-quantum`` (and ``clock.quantum``) are set. Runs off the
    JACK callback thread. min/max stay a wide range so 64–2048 still work.
    """
    n = int(frames)
    if n < 16 or n > 8192:
        return False
    keys = (
        ("clock.force-quantum", str(n)),
        ("clock.quantum", str(n)),
        ("clock.min-quantum", "32"),
        ("clock.max-quantum", "8192"),
    )
    ok_any = False
    for key, val in keys:
        try:
            r = subprocess.run(
                ["pw-metadata", "-n", "settings", "0", key, val],
                capture_output=True,
                timeout=0.5,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return False
        if r.returncode == 0:
            ok_any = True
    return ok_any


def should_reject_auto_1024(held: Optional[int], incoming: int, held_age_s: float) -> bool:
    """True when PipeWire/JACK bounced the period back to the 1024 default."""
    if held is None or incoming != _PIPEWIRE_DEFAULT_QUANTUM:
        return False
    if int(held) == _PIPEWIRE_DEFAULT_QUANTUM:
        return False
    return held_age_s < 60.0


def _ensure_pipewire_duplex() -> None:
    """Force a duplex PipeWire node so inports are visible in QjackCtl/REAPER.

    If ``PIPEWIRE_PROPS`` already contains Audio/Duplex, leave the class.
    Otherwise replace Audio/Source or Audio/Sink (both hide one side)
    or append ``media.class = Audio/Duplex``. Then unlock JACK period size.
    """
    raw = os.environ.get("PIPEWIRE_PROPS")
    if raw is not None and str(raw).strip():
        s = str(raw)
        if "Audio/Duplex" not in s:
            if "Audio/Source" in s:
                os.environ["PIPEWIRE_PROPS"] = s.replace("Audio/Source", "Audio/Duplex")
            elif "Audio/Sink" in s:
                os.environ["PIPEWIRE_PROPS"] = s.replace("Audio/Sink", "Audio/Duplex")
            else:
                stripped = s.strip()
                if stripped.startswith("{") and stripped.endswith("}"):
                    inner = stripped[1:-1].strip()
                    os.environ["PIPEWIRE_PROPS"] = (
                        "{ %s media.class = Audio/Duplex }" % inner
                    )
                else:
                    os.environ["PIPEWIRE_PROPS"] = _DEFAULT_PW_PROPS
    else:
        os.environ["PIPEWIRE_PROPS"] = _DEFAULT_PW_PROPS
    _unlock_qjackctl_period()


class JackNamedOutput:
    """Owns a JACK client with N capture (_in) + N playback (_out) ports.

    ``pull_frames(n)`` must be realtime-safe (no OSC, no I/O). Input ports
    are consumed in the same callback and are not sent on the UDP path.
    """

    def __init__(
        self,
        *,
        client_name: str,
        port_names: Sequence[str],
        sample_rate: int,
        pull_frames: Callable[[int], np.ndarray],
        in_port_names: Optional[Sequence[str]] = None,
        out_port_names: Optional[Sequence[str]] = None,
    ) -> None:
        self.client_name = client_name
        stems = [str(p) for p in port_names]
        if in_port_names is not None and out_port_names is not None:
            self.in_port_names = [str(p) for p in in_port_names]
            self.out_port_names = [str(p) for p in out_port_names]
        else:
            self.in_port_names, self.out_port_names = duplex_io_names(stems)
        # Legacy alias: playback short names (same order as ``port_names``).
        self.port_names = list(self.out_port_names)
        self.sample_rate = int(sample_rate)
        self._pull_frames = pull_frames
        self._client = None
        self._actual_name = client_name
        self._graph: Optional[JackGraphRouter] = None
        self._held_period: Optional[int] = None
        self._held_at = 0.0
        self._pin_stop = threading.Event()
        self._pin_n: Optional[int] = None
        self._pin_wake = threading.Event()
        self._pin_thread: Optional[threading.Thread] = None

    def _schedule_pin(self, frames: int) -> None:
        n = int(frames)
        if n == _PIPEWIRE_DEFAULT_QUANTUM:
            return
        self._pin_n = n
        self._pin_wake.set()

    def _apply_pin(self, n: int, *, log_it: bool = True) -> None:
        if n == _PIPEWIRE_DEFAULT_QUANTUM:
            return
        graph = self._graph
        if graph is not None:
            graph.quiet(2.5)
        ok = pin_pipewire_clock_quantum(n)
        client = self._client
        if client is not None:
            try:
                if int(client.blocksize) != int(n):
                    client.blocksize = int(n)
            except Exception:
                log.debug("JACK set blocksize %s", n, exc_info=True)
        if log_it:
            log.info(
                "JACK Frames/Period fijado en %s (pw-metadata=%s)",
                n,
                "ok" if ok else "sin pw-metadata",
            )

    def _pin_loop(self) -> None:
        last_meta = 0.0
        while not self._pin_stop.is_set():
            self._pin_wake.wait(timeout=0.2)
            if self._pin_stop.is_set():
                break
            if self._pin_wake.is_set():
                self._pin_wake.clear()
                n = self._pin_n
                if n is not None and int(n) != _PIPEWIRE_DEFAULT_QUANTUM:
                    self._apply_pin(int(n))
                    last_meta = time.monotonic()
            held = self._held_period
            if held is None or int(held) == _PIPEWIRE_DEFAULT_QUANTUM:
                continue
            held_n = int(held)
            client = self._client
            snapped = False
            if client is not None:
                try:
                    snapped = int(client.blocksize) == _PIPEWIRE_DEFAULT_QUANTUM
                except Exception:
                    snapped = False
            now = time.monotonic()
            if snapped:
                log.warning(
                    "watchdog: Frames/Period=1024, restaurando %s (QjackCtl)",
                    held_n,
                )
                self._apply_pin(held_n)
                last_meta = now
            elif now - last_meta >= 1.0:
                pin_pipewire_clock_quantum(held_n)
                last_meta = now

    def _on_blocksize_event(self, client: Any, incoming: int) -> None:
        """JACK blocksize callback: never call jack_set_buffer_size here."""
        incoming = int(incoming)
        graph = self._graph
        if graph is not None:
            graph.quiet(2.5)
        age = time.monotonic() - self._held_at
        if should_reject_auto_1024(self._held_period, incoming, age):
            held = int(self._held_period or incoming)
            log.warning(
                "JACK Frames/Period volvió solo a 1024; se mantiene %s (QjackCtl)",
                held,
            )
            self._schedule_pin(held)
            return
        self._held_period = incoming
        self._held_at = time.monotonic()
        log.info("JACK Frames/Period=%s (QjackCtl)", incoming)
        if incoming != _PIPEWIRE_DEFAULT_QUANTUM:
            self._schedule_pin(incoming)

    def start(self) -> None:
        import jack

        _ensure_pipewire_duplex()
        try:
            force_analog_session_defaults()
        except Exception:
            log.debug("force analog session defaults", exc_info=True)
        client = jack.Client(self.client_name, no_start_server=True)
        sr = int(client.samplerate)
        if sr != self.sample_rate:
            client.close()
            raise RuntimeError(
                f"JACK sample rate {sr} != XAIR_SAMPLE_RATE {self.sample_rate}"
            )
        if not self.out_port_names or len(self.in_port_names) != len(self.out_port_names):
            client.close()
            raise RuntimeError("JACK duplex requiere el mismo número de puertos _in y _out")
        # Capture first, then playback — same channel index on both sides.
        for name in self.in_port_names:
            client.inports.register(name)
        for name in self.out_port_names:
            client.outports.register(name)

        # Keep QjackCtl Frames/Period. Do not call jack_set_buffer_size from
        # this callback (JACK forbids it); pin PipeWire clock off-thread.
        def _on_blocksize(n: int) -> None:
            self._on_blocksize_event(client, int(n))

        try:
            client.set_blocksize_callback(_on_blocksize)
        except Exception:
            log.debug("JACK set_blocksize_callback no disponible", exc_info=True)
        self._held_period = int(client.blocksize)
        self._held_at = time.monotonic()
        log.info(
            "JACK Frames/Period=%s (QjackCtl; no se revierte a 1024 solo)",
            self._held_period,
        )

        def _process(frames: int) -> None:
            nframes = int(frames)
            # Consume capture ports (full-duplex cycle). Do not write them and
            # do not touch UDP/jitter/FEC/Opus from this RT callback.
            for pin in client.inports:
                pin.get_array()
            block = self._pull_frames(nframes)
            nch = min(block.shape[1], len(client.outports))
            for i in range(nch):
                buf = client.outports[i].get_array()
                n = min(nframes, buf.shape[0], block.shape[0])
                buf[:n] = block[:n, i]
                if n < buf.shape[0]:
                    buf[n:] = 0.0

        client.set_process_callback(_process)
        self._client = client
        self._actual_name = str(client.name)
        self._pin_stop.clear()
        self._pin_thread = threading.Thread(target=self._pin_loop, name="xair-jack-period", daemon=True)
        self._pin_thread.start()
        if self._held_period != _PIPEWIRE_DEFAULT_QUANTUM:
            self._schedule_pin(int(self._held_period))
        client.activate()
        # Wire System in → this client → REAPER → System out. Autoconnect stays
        # off so WirePlumber cannot dump playback on HDMI/speakers and skip REAPER.
        self._graph = JackGraphRouter(client, bridge_client=self._actual_name)
        self._graph.start()
        log.info(
            "JACK/PipeWire duplex: cliente %r, %s in + %s out @ %s Hz",
            self._actual_name,
            len(self.in_port_names),
            len(self.out_port_names),
            sr,
        )
        log.info(
            "  capture: %s",
            ", ".join(f"{self._actual_name}:{n}" for n in self.in_port_names),
        )
        log.info(
            "  playback: %s",
            ", ".join(f"{self._actual_name}:{n}" for n in self.out_port_names),
        )

    @property
    def actual_client_name(self) -> str:
        return self._actual_name

    def stop(self) -> None:
        self._pin_stop.set()
        self._pin_wake.set()
        thp = self._pin_thread
        self._pin_thread = None
        if thp is not None and thp.is_alive():
            thp.join(timeout=1.0)
        graph = self._graph
        self._graph = None
        if graph is not None:
            try:
                graph.stop()
            except Exception:
                log.debug("JACK graph stop", exc_info=True)
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            client.deactivate()
        except Exception:
            log.debug("JACK deactivate", exc_info=True)
        try:
            client.close()
        except Exception:
            log.debug("JACK close", exc_info=True)
        log.info("JACK/PipeWire: cliente %r cerrado.", self._actual_name)


def start_named_jack_output(
    *,
    client_name: str,
    port_names: Sequence[str],
    sample_rate: int,
    pull_frames: Callable[[int], np.ndarray],
    in_port_names: Optional[Sequence[str]] = None,
    out_port_names: Optional[Sequence[str]] = None,
) -> JackNamedOutput:
    out = JackNamedOutput(
        client_name=client_name,
        port_names=port_names,
        sample_rate=sample_rate,
        pull_frames=pull_frames,
        in_port_names=in_port_names,
        out_port_names=out_port_names,
    )
    out.start()
    return out
