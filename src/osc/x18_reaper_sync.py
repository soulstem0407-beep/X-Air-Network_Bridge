"""Bidirectional OSC sync between an X AIR mixer and REAPER.

This module does **not** touch USB capture or the XBRI UDP audio path. It is
an optional control-plane process: start it from the CLI (`start-reaper-sync`)
or as ``python -m src.osc.x18_reaper_sync``.

Design notes
------------
* **Injection:** ``ReaperXAirSync`` takes an ``XAirOSCBridge`` and a REAPER
  UDP client. The CLI constructs both; tests can pass fakes.
* **Thread safety:** ``XAirOSCBridge`` uses one UDP socket. The REAPER listener
  and GET/SET share that socket under ``_xair_lock``. Incoming mixer traffic
  is owned by the bridge recv thread when ``/xremote`` is active (waiters vs
  subscription callback) so two threads never ``recvfrom`` the same socket.
* **Loop safety:** ``EchoGuard`` drops echoes (same value, or a reverse-origin
  write inside the suppress window). Mixer SET echoes are claimed by the GET/SET
  waiter and do not reach the subscription callback.
* **X18 → REAPER:** default path is ``/xremote`` keep-alive + unsolicited
  parameter pushes after a one-shot snapshot. GET-poll remains as
  ``XAIR_REAPER_USE_XREMOTE=false``.
* **Mute mapping:** ``XAirOSCBridge.get_mute`` / ``set_mute`` operate on
  ``/ch/XX/mix/on`` (1 = unmuted). REAPER ``/track/n/mute`` is 1 = muted.
  Conversion happens only at this boundary.
* **Logging:** INFO for start/stop and the channel list. Per-parameter traffic
  is DEBUG and only when a value is actually applied.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_message import OscMessage
from pythonosc.osc_server import ThreadingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient

from ..xair_control.osc_bridge import (
    DEFAULT_XREMOTE_INTERVAL_SEC,
    XAirOSCBridge,
    XAirOSCError,
    clamp_xremote_interval_sec,
)

log = logging.getLogger(__name__)

DEFAULT_REAPER_PORT = 9001
DEFAULT_POLL_INTERVAL_SEC = 0.20
DEFAULT_ECHO_SUPPRESS_SEC = 0.25
DEFAULT_SENDS = 4
DEFAULT_BUSES = 4
FLOAT_EPS = 1e-4


def mix_on_to_reaper_mute(mix_on: bool) -> int:
    """Map X AIR mix/on to REAPER mute (1 = muted)."""
    return 0 if mix_on else 1


def reaper_mute_to_mix_on(muted: bool) -> bool:
    """Map REAPER mute to the bool expected by ``XAirOSCBridge.set_mute`` (mix/on)."""
    return not muted


def osc_flag_to_bool(value: Any) -> bool:
    """Interpret OSC 0/1 or 0.0/1.0 as bool without treating 0.0 as True."""
    if isinstance(value, bool):
        return value
    try:
        return float(value) >= 0.5
    except (TypeError, ValueError):
        return bool(value)


def parse_track_index(address: str) -> Optional[int]:
    """``/track/{n}/...`` → n, or None if the path is not a track address."""
    parts = address.split("/")
    if len(parts) < 3 or parts[1] != "track":
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


def parse_send_index(address: str) -> Optional[Tuple[int, int]]:
    """``/track/{ch}/send/{n}`` → (ch, n)."""
    parts = address.split("/")
    if len(parts) < 5 or parts[1] != "track" or parts[3] != "send":
        return None
    try:
        return int(parts[2]), int(parts[4])
    except ValueError:
        return None


def parse_bus_index(address: str) -> Optional[int]:
    """``/bus/{n}/volume`` → n."""
    parts = address.split("/")
    if len(parts) < 3 or parts[1] != "bus":
        return None
    try:
        return int(parts[2])
    except ValueError:
        return None


@dataclass
class MixerParam:
    """Subset of X AIR paths that this sync forwards to REAPER."""

    kind: str
    ch: Optional[int] = None
    send_idx: Optional[int] = None
    bus: Optional[int] = None


def _osc_index(token: str, lo: int, hi: int) -> Optional[int]:
    try:
        n = int(token)
    except ValueError:
        return None
    if lo <= n <= hi:
        return n
    return None


def parse_xair_subscription(address: str) -> Optional[MixerParam]:
    """Map an unsolicited mixer path to a REAPER-facing parameter.

    Ignores EQ/dyn/FX and other ``/xremote`` traffic. Send levels accept both
    ``/ch/XX/mix/YY`` (this project's GET path) and ``/ch/XX/mix/YY/level``.
    """
    parts = address.split("/")
    if len(parts) < 2:
        return None
    head = parts[1]
    if head == "lr":
        if len(parts) >= 4 and parts[2] == "mix" and parts[3] == "fader":
            return MixerParam("lr")
        return None
    if head == "bus":
        if len(parts) >= 5 and parts[3] == "mix" and parts[4] == "fader":
            bus = _osc_index(parts[2], 1, 16)
            if bus is None:
                return None
            return MixerParam("bus", bus=bus)
        return None
    if head != "ch" or len(parts) < 5:
        return None
    ch = _osc_index(parts[2], 1, 18)
    if ch is None:
        return None
    if parts[3] == "config" and parts[4] == "name":
        return MixerParam("name", ch=ch)
    if parts[3] != "mix":
        return None
    leaf = parts[4]
    if leaf == "fader":
        return MixerParam("fader", ch=ch)
    if leaf == "on":
        return MixerParam("mute", ch=ch)
    if leaf == "pan":
        return MixerParam("pan", ch=ch)
    send_idx = _osc_index(leaf, 1, 16)
    if send_idx is None:
        return None
    # /ch/XX/mix/YY  or  /ch/XX/mix/YY/level — skip /grpon, /pan, etc.
    if len(parts) == 5 or (len(parts) >= 6 and parts[5] == "level"):
        return MixerParam("send", ch=ch, send_idx=send_idx)
    return None


def values_close(a: Any, b: Any, *, kind: str) -> bool:
    """Equality used by the echo guard (float epsilon, exact otherwise)."""
    if kind == "float":
        try:
            return abs(float(a) - float(b)) < FLOAT_EPS
        except (TypeError, ValueError):
            return False
    return a == b


@dataclass
class ReaperSyncConfig:
    """Destination REAPER OSC port plus local listen / subscription knobs."""

    reaper_host: str
    reaper_port: int = DEFAULT_REAPER_PORT
    listen_host: str = "0.0.0.0"
    listen_port: int = DEFAULT_REAPER_PORT
    poll_interval_sec: float = DEFAULT_POLL_INTERVAL_SEC
    echo_suppress_sec: float = DEFAULT_ECHO_SUPPRESS_SEC
    channels: int = 18
    sends: int = DEFAULT_SENDS
    buses: int = DEFAULT_BUSES
    use_xremote: bool = True
    xremote_interval_sec: float = DEFAULT_XREMOTE_INTERVAL_SEC

    @classmethod
    def from_env(
        cls,
        *,
        reaper_host: Optional[str] = None,
        reaper_port: Optional[int] = None,
        listen_port: Optional[int] = None,
    ) -> ReaperSyncConfig:
        host = (
            reaper_host
            if reaper_host is not None
            else os.getenv("XAIR_REAPER_HOST", "127.0.0.1")
        )
        host = (host or "").strip()
        if not host:
            raise ValueError("XAIR_REAPER_HOST vacío: indica la IP de REAPER")

        def _int(name: str, default: int) -> int:
            raw = os.getenv(name)
            if raw is None or not str(raw).strip():
                return default
            return int(raw)

        def _float(name: str, default: float) -> float:
            raw = os.getenv(name)
            if raw is None or not str(raw).strip():
                return default
            return float(raw)

        def _bool(name: str, default: bool) -> bool:
            raw = os.getenv(name)
            if raw is None or not str(raw).strip():
                return default
            return str(raw).strip().lower() in ("1", "true", "yes", "on")

        dest_port = int(reaper_port) if reaper_port is not None else _int(
            "XAIR_REAPER_PORT", DEFAULT_REAPER_PORT
        )
        bind_port = int(listen_port) if listen_port is not None else _int(
            "XAIR_REAPER_LISTEN_PORT", dest_port
        )
        ch = _int("XAIR_REAPER_CHANNELS", _int("XAIR_CHANNELS", 18))
        return cls(
            reaper_host=host,
            reaper_port=dest_port,
            listen_host=os.getenv("XAIR_REAPER_LISTEN_HOST", "0.0.0.0").strip()
            or "0.0.0.0",
            listen_port=bind_port,
            poll_interval_sec=max(0.05, _float("XAIR_REAPER_POLL_INTERVAL_SEC", DEFAULT_POLL_INTERVAL_SEC)),
            echo_suppress_sec=max(0.0, _float("XAIR_REAPER_ECHO_SUPPRESS_SEC", DEFAULT_ECHO_SUPPRESS_SEC)),
            channels=max(1, min(18, ch)),
            sends=max(0, min(16, _int("XAIR_REAPER_SENDS", DEFAULT_SENDS))),
            buses=max(0, min(16, _int("XAIR_REAPER_BUSES", DEFAULT_BUSES))),
            use_xremote=_bool("XAIR_REAPER_USE_XREMOTE", True),
            xremote_interval_sec=clamp_xremote_interval_sec(
                _float("XAIR_OSC_XREMOTE_INTERVAL_SEC", DEFAULT_XREMOTE_INTERVAL_SEC)
            ),
        )


class EchoGuard:
    """Drop reverse-path echoes so REAPER ↔ X18 cannot oscillate.

    A write is skipped when:
    * the stored value is already equal, or
    * the last writer was the *other* origin and we are still inside the
      suppress window (the far side echoing our own set).

    ``allow`` is a pure check; ``remember`` runs only after a successful I/O
    so a failed OSC set does not poison the slot.
    """

    def __init__(self, suppress_s: float) -> None:
        self._suppress_s = float(suppress_s)
        self._slots: Dict[str, Tuple[Any, str, float, str]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, value: Any, origin: str, kind: str) -> bool:
        now = time.monotonic()
        with self._lock:
            prev = self._slots.get(key)
            if prev is None:
                return True
            last_val, last_origin, last_t, last_kind = prev
            if values_close(last_val, value, kind=last_kind if last_kind else kind):
                return False
            if last_origin != origin and (now - last_t) < self._suppress_s:
                return False
            return True

    def remember(self, key: str, value: Any, origin: str, kind: str) -> None:
        with self._lock:
            self._slots[key] = (value, origin, time.monotonic(), kind)


class ReaperXAirSync:
    """Runs a REAPER OSC listener plus X18 ``/xremote`` (or optional poll)."""

    def __init__(
        self,
        xair: XAirOSCBridge,
        reaper: SimpleUDPClient,
        config: ReaperSyncConfig,
        *,
        owns_xair: bool = False,
    ) -> None:
        self._xair = xair
        self._reaper = reaper
        self._config = config
        self._owns_xair = owns_xair
        self._xair_lock = threading.Lock()
        self._guard = EchoGuard(config.echo_suppress_sec)
        self._stop = threading.Event()
        self._started = False
        self._channels: List[int] = []
        self._server: Optional[ThreadingOSCUDPServer] = None
        self._listener: Optional[threading.Thread] = None
        self._poller: Optional[threading.Thread] = None

    @classmethod
    def from_env(
        cls,
        *,
        config: Optional[ReaperSyncConfig] = None,
        xair: Optional[XAirOSCBridge] = None,
        osc_host: Optional[str] = None,
        osc_port: Optional[int] = None,
    ) -> ReaperXAirSync:
        """Build mixer + REAPER clients from environment (CLI entry)."""
        cfg = config if config is not None else ReaperSyncConfig.from_env()
        owns = xair is None
        bridge = xair if xair is not None else XAirOSCBridge.from_env(
            host=osc_host, port=osc_port
        )
        client = SimpleUDPClient(cfg.reaper_host, cfg.reaper_port)
        return cls(bridge, client, cfg, owns_xair=owns)

    @property
    def config(self) -> ReaperSyncConfig:
        return self._config

    @property
    def active_channels(self) -> Sequence[int]:
        return tuple(self._channels)

    def start(self) -> None:
        """Ping, snapshot once, then subscribe (or poll) until ``stop()``."""
        if self._started:
            raise RuntimeError("ReaperXAirSync ya está en marcha")

        with self._xair_lock:
            status, _args = self._xair.ping()
        log.info(
            "X18 %s:%s /status=%s → REAPER %s:%s (listen %s:%s)",
            self._xair.host,
            self._xair.port,
            status,
            self._config.reaper_host,
            self._config.reaper_port,
            self._config.listen_host,
            self._config.listen_port,
        )

        self._channels = self._detect_channels()
        if not self._channels:
            raise XAirOSCError(
                "ningún canal X18 respondió (revisa XAIR_OSC_HOST / red OSC)"
            )
        log.info("Canales activos: %s", self._channels)
        self._stop.clear()
        self._wake_channels()
        # /xremote only forwards *changes*; push current mix once via GET.
        self._snapshot_to_reaper()

        dispatcher = Dispatcher()
        dispatcher.map("/track/*/volume", self._on_reaper_fader)
        dispatcher.map("/track/*/mute", self._on_reaper_mute)
        dispatcher.map("/track/*/pan", self._on_reaper_pan)
        dispatcher.map("/track/*/name", self._on_reaper_name)
        dispatcher.map("/track/*/send/*", self._on_reaper_send)
        dispatcher.map("/bus/*/volume", self._on_reaper_bus)
        dispatcher.map("/master/volume", self._on_reaper_master)

        self._server = ThreadingOSCUDPServer(
            (self._config.listen_host, self._config.listen_port),
            dispatcher,
        )
        self._listener = threading.Thread(
            target=self._server.serve_forever,
            name="reaper-osc-rx",
            daemon=True,
        )
        self._listener.start()
        try:
            if self._config.use_xremote:
                # Recv thread starts after the snapshot so GET still uses blocking recv.
                self._xair.start_xremote(
                    self._on_mixer_message,
                    interval_s=self._config.xremote_interval_sec,
                )
                log.info(
                    "REAPER sync activo (/xremote keep-alive=%.1fs, echo_suppress=%.2fs)",
                    self._config.xremote_interval_sec,
                    self._config.echo_suppress_sec,
                )
            else:
                self._poller = threading.Thread(
                    target=self._poll_loop,
                    name="xair-osc-poll",
                    daemon=True,
                )
                self._poller.start()
                log.info(
                    "REAPER sync activo (poll=%.2fs, echo_suppress=%.2fs) "
                    "[XAIR_REAPER_USE_XREMOTE=false]",
                    self._config.poll_interval_sec,
                    self._config.echo_suppress_sec,
                )
        except Exception:
            self.stop()
            raise
        self._started = True

    def stop(self) -> None:
        """Stop subscriptions, poller, and OSC server; close mixer if we own it."""
        self._stop.set()
        try:
            self._xair.stop_xremote()
        except Exception:
            log.debug("stop_xremote", exc_info=True)
        server = self._server
        if server is not None:
            try:
                server.shutdown()
            except Exception:
                log.debug("OSC server shutdown", exc_info=True)
            try:
                server.server_close()
            except Exception:
                log.debug("OSC server close", exc_info=True)
            self._server = None
        if self._poller is not None:
            self._poller.join(timeout=3.0)
            self._poller = None
        if self._listener is not None:
            self._listener.join(timeout=2.0)
            self._listener = None
        if self._owns_xair:
            try:
                self._xair.close()
            except Exception:
                log.debug("cierre XAirOSCBridge", exc_info=True)
        self._started = False
        log.info("REAPER sync detenido")

    def _emit_to_reaper(
        self,
        key: str,
        value: Any,
        kind: str,
        address: str,
        payload: Any,
    ) -> None:
        if not self._guard.allow(key, value, "xair", kind):
            return
        self._reaper.send_message(address, payload)
        self._guard.remember(key, value, "xair", kind)
        log.debug("X18 → REAPER %s = %r", address, payload)

    def _apply_to_xair(
        self,
        key: str,
        value: Any,
        kind: str,
        fn: Any,
        *args: Any,
    ) -> None:
        if not self._guard.allow(key, value, "reaper", kind):
            return
        try:
            with self._xair_lock:
                fn(*args)
            self._guard.remember(key, value, "reaper", kind)
            log.debug("REAPER → X18 %s %s", getattr(fn, "__name__", fn), args)
        except XAirOSCError as exc:
            log.warning("%s %s: %s", getattr(fn, "__name__", fn), args, exc)

    def _detect_channels(self) -> List[int]:
        activos: List[int] = []
        for ch in range(1, self._config.channels + 1):
            try:
                with self._xair_lock:
                    name = self._xair.get_channel_name(ch)
            except XAirOSCError as exc:
                log.debug("Canal %s no responde al nombre: %s", ch, exc)
                continue
            activos.append(ch)
            self._emit_to_reaper(
                f"ch:{ch}:name", name, "str", f"/track/{ch}/name", name
            )
        return activos

    def _wake_channels(self) -> None:
        """Rewrite each fader to itself so the desk treats us as a remote.

        X AIR often ignores the first few sets until a parameter is touched;
        this is the same warm-up the standalone script used.
        """
        for ch in self._channels:
            try:
                with self._xair_lock:
                    val = self._xair.get_fader(ch)
                    self._xair.set_fader(ch, val)
                self._guard.remember(f"ch:{ch}:fader", val, "xair", "float")
            except XAirOSCError as exc:
                log.debug("wake CH %s: %s", ch, exc)

    def _in_channels(self, track: Optional[int]) -> bool:
        return track is not None and track in self._channels

    def _on_reaper_fader(self, address: str, *args: Any) -> None:
        track = parse_track_index(address)
        if not self._in_channels(track) or not args:
            return
        try:
            val = float(args[0])
        except (TypeError, ValueError):
            return
        self._apply_to_xair(
            f"ch:{track}:fader", val, "float", self._xair.set_fader, int(track), val
        )

    def _on_reaper_mute(self, address: str, *args: Any) -> None:
        track = parse_track_index(address)
        if not self._in_channels(track) or not args:
            return
        muted = osc_flag_to_bool(args[0])
        mix_on = reaper_mute_to_mix_on(muted)
        self._apply_to_xair(
            f"ch:{track}:mute",
            muted,
            "bool",
            self._xair.set_mute,
            int(track),
            mix_on,
        )

    def _on_reaper_pan(self, address: str, *args: Any) -> None:
        track = parse_track_index(address)
        if not self._in_channels(track) or not args:
            return
        try:
            val = float(args[0])
        except (TypeError, ValueError):
            return
        self._apply_to_xair(
            f"ch:{track}:pan", val, "float", self._xair.set_pan, int(track), val
        )

    def _on_reaper_name(self, address: str, *args: Any) -> None:
        track = parse_track_index(address)
        if not self._in_channels(track) or not args:
            return
        name = str(args[0])
        self._apply_to_xair(
            f"ch:{track}:name",
            name,
            "str",
            self._xair.set_channel_name,
            int(track),
            name,
        )

    def _on_reaper_send(self, address: str, *args: Any) -> None:
        parsed = parse_send_index(address)
        if parsed is None or not args:
            return
        track, send_idx = parsed
        if not self._in_channels(track):
            return
        if send_idx < 1 or send_idx > self._config.sends:
            return
        try:
            val = float(args[0])
        except (TypeError, ValueError):
            return
        self._apply_to_xair(
            f"ch:{track}:send:{send_idx}",
            val,
            "float",
            self._xair.set_send,
            int(track),
            send_idx,
            val,
        )

    def _on_reaper_bus(self, address: str, *args: Any) -> None:
        bus = parse_bus_index(address)
        if bus is None or not args:
            return
        if bus < 1 or bus > self._config.buses:
            return
        try:
            val = float(args[0])
        except (TypeError, ValueError):
            return
        self._apply_to_xair(
            f"bus:{bus}:fader", val, "float", self._xair.set_bus_fader, bus, val
        )

    def _on_reaper_master(self, address: str, *args: Any) -> None:
        del address
        if not args:
            return
        try:
            val = float(args[0])
        except (TypeError, ValueError):
            return
        self._apply_to_xair("lr:fader", val, "float", self._xair.set_lr_fader, val)

    def _on_mixer_message(self, msg: OscMessage) -> None:
        """Forward unsolicited X AIR updates to REAPER. Must not GET/SET here."""
        parsed = parse_xair_subscription(msg.address)
        if parsed is None or not msg.params:
            return
        param = msg.params[0]
        kind = parsed.kind
        if kind == "fader":
            if not self._in_channels(parsed.ch):
                return
            try:
                val = float(param)
            except (TypeError, ValueError):
                return
            self._emit_to_reaper(
                f"ch:{parsed.ch}:fader",
                val,
                "float",
                f"/track/{parsed.ch}/volume",
                val,
            )
            return
        if kind == "mute":
            if not self._in_channels(parsed.ch):
                return
            mix_on = osc_flag_to_bool(param)
            muted = mix_on_to_reaper_mute(mix_on)
            self._emit_to_reaper(
                f"ch:{parsed.ch}:mute",
                bool(muted),
                "bool",
                f"/track/{parsed.ch}/mute",
                muted,
            )
            return
        if kind == "pan":
            if not self._in_channels(parsed.ch):
                return
            try:
                val = float(param)
            except (TypeError, ValueError):
                return
            self._emit_to_reaper(
                f"ch:{parsed.ch}:pan",
                val,
                "float",
                f"/track/{parsed.ch}/pan",
                val,
            )
            return
        if kind == "name":
            if not self._in_channels(parsed.ch):
                return
            name = param.decode() if isinstance(param, bytes) else str(param)
            self._emit_to_reaper(
                f"ch:{parsed.ch}:name",
                name,
                "str",
                f"/track/{parsed.ch}/name",
                name,
            )
            return
        if kind == "send":
            if not self._in_channels(parsed.ch):
                return
            send_idx = parsed.send_idx
            if send_idx is None or send_idx < 1 or send_idx > self._config.sends:
                return
            try:
                val = float(param)
            except (TypeError, ValueError):
                return
            self._emit_to_reaper(
                f"ch:{parsed.ch}:send:{send_idx}",
                val,
                "float",
                f"/track/{parsed.ch}/send/{send_idx}",
                val,
            )
            return
        if kind == "bus":
            bus = parsed.bus
            if bus is None or bus < 1 or bus > self._config.buses:
                return
            try:
                val = float(param)
            except (TypeError, ValueError):
                return
            self._emit_to_reaper(
                f"bus:{bus}:fader",
                val,
                "float",
                f"/bus/{bus}/volume",
                val,
            )
            return
        if kind == "lr":
            try:
                val = float(param)
            except (TypeError, ValueError):
                return
            self._emit_to_reaper("lr:fader", val, "float", "/master/volume", val)

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            self._poll_once()
            if self._stop.wait(self._config.poll_interval_sec):
                break

    def _snapshot_to_reaper(self) -> None:
        """One GET pass so REAPER matches the desk before /xremote (changes only)."""
        self._poll_once()

    def _poll_once(self) -> None:
        for ch in list(self._channels):
            if self._stop.is_set():
                return
            self._push_fader(ch)
            self._push_mute(ch)
            self._push_pan(ch)
            for send_idx in range(1, self._config.sends + 1):
                self._push_send(ch, send_idx)
        for bus in range(1, self._config.buses + 1):
            if self._stop.is_set():
                return
            self._push_bus(bus)
        self._push_lr()

    def _xair_get(self, fn: Any, *args: Any) -> Optional[Any]:
        try:
            with self._xair_lock:
                return fn(*args)
        except XAirOSCError as exc:
            log.debug("OSC get %s: %s", getattr(fn, "__name__", fn), exc)
            return None

    def _push_fader(self, ch: int) -> None:
        val = self._xair_get(self._xair.get_fader, ch)
        if val is None:
            return
        self._emit_to_reaper(
            f"ch:{ch}:fader", val, "float", f"/track/{ch}/volume", float(val)
        )

    def _push_mute(self, ch: int) -> None:
        mix_on = self._xair_get(self._xair.get_mute, ch)
        if mix_on is None:
            return
        muted = mix_on_to_reaper_mute(bool(mix_on))
        self._emit_to_reaper(
            f"ch:{ch}:mute", bool(muted), "bool", f"/track/{ch}/mute", muted
        )

    def _push_pan(self, ch: int) -> None:
        val = self._xair_get(self._xair.get_pan, ch)
        if val is None:
            return
        self._emit_to_reaper(
            f"ch:{ch}:pan", val, "float", f"/track/{ch}/pan", float(val)
        )

    def _push_send(self, ch: int, send_idx: int) -> None:
        val = self._xair_get(self._xair.get_send, ch, send_idx)
        if val is None:
            return
        self._emit_to_reaper(
            f"ch:{ch}:send:{send_idx}",
            val,
            "float",
            f"/track/{ch}/send/{send_idx}",
            float(val),
        )

    def _push_bus(self, bus: int) -> None:
        val = self._xair_get(self._xair.get_bus_fader, bus)
        if val is None:
            return
        self._emit_to_reaper(
            f"bus:{bus}:fader", val, "float", f"/bus/{bus}/volume", float(val)
        )

    def _push_lr(self) -> None:
        val = self._xair_get(self._xair.get_lr_fader)
        if val is None:
            return
        self._emit_to_reaper("lr:fader", val, "float", "/master/volume", float(val))


def run_forever(
    *,
    config: Optional[ReaperSyncConfig] = None,
    osc_host: Optional[str] = None,
    osc_port: Optional[int] = None,
) -> None:
    """Blocking helper for the CLI and ``python -m`` entry."""
    import signal

    sync = ReaperXAirSync.from_env(
        config=config, osc_host=osc_host, osc_port=osc_port
    )
    try:
        sync.start()
        ev = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: ev.set())
        signal.signal(signal.SIGTERM, lambda *_: ev.set())
        ev.wait()
    except KeyboardInterrupt:
        pass
    finally:
        sync.stop()


def main() -> None:
    from pathlib import Path

    from dotenv import load_dotenv

    root = Path(__file__).resolve().parents[2]
    env_path = root / ".env"
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_forever()


if __name__ == "__main__":
    main()
