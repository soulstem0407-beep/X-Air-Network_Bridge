"""DAW OSC write-back onto the mixer. Control thread only — never audio."""
from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_message import OscMessage
from pythonosc.osc_server import ThreadingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient

from ..osc.x18_reaper_sync import (
    EchoGuard,
    MixerParam,
    osc_flag_to_bool,
    parse_bus_index,
    parse_send_index,
    parse_track_index,
    parse_xair_subscription,
    reaper_mute_to_mix_on,
    mix_on_to_reaper_mute,
)
from ..xair_control.osc_bridge import XAirOSCError

log = logging.getLogger(__name__)

DEFAULT_ECHO_SUPPRESS_SEC = 0.25


@dataclass
class WriteJob:
    origin: str
    kind: str
    key: str
    value: Any
    args: Tuple[Any, ...]


class MixWriteBack:
    """Queue DAW OSC → mixer SET on a dedicated control thread.

    Mixer ``/xremote`` updates SyncManager immediately (no SET in that callback).
    """

    def __init__(
        self,
        sync: Any,
        *,
        channels: int = 18,
        sends: int = 4,
        buses: int = 4,
        listen_host: str = "0.0.0.0",
        listen_port: int = 9002,
        daw_host: str = "127.0.0.1",
        daw_port: int = 9001,
        echo_suppress_sec: float = DEFAULT_ECHO_SUPPRESS_SEC,
    ) -> None:
        self._sync = sync
        self._channels = int(channels)
        self._sends = int(sends)
        self._buses = int(buses)
        self._listen_host = listen_host
        self._listen_port = int(listen_port)
        self._daw = SimpleUDPClient(daw_host, int(daw_port))
        self._guard = EchoGuard(echo_suppress_sec)
        self._jobs: "queue.Queue[Optional[WriteJob]]" = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._ctrl: Optional[threading.Thread] = None
        self._server: Optional[ThreadingOSCUDPServer] = None
        self._listener: Optional[threading.Thread] = None
        self.last_writeback_ns: Optional[int] = None
        self.writes_ok = 0
        self.writes_dropped = 0

    def start(self) -> None:
        bridge = self._sync.osc_bridge
        bridge.start_xremote(self._on_mixer_message)
        self._ctrl = threading.Thread(target=self._control_loop, name="xair-writeback", daemon=True)
        self._ctrl.start()
        disp = Dispatcher()
        disp.map("/track/*/volume", self._on_daw_fader)
        disp.map("/track/*/mute", self._on_daw_mute)
        disp.map("/track/*/pan", self._on_daw_pan)
        disp.map("/track/*/send/*", self._on_daw_send)
        disp.map("/bus/*/volume", self._on_daw_bus)
        self._server = ThreadingOSCUDPServer((self._listen_host, self._listen_port), disp)
        self._listener = threading.Thread(target=self._server.serve_forever, name="xair-return-osc", daemon=True)
        self._listener.start()
        log.info(
            "OSC write-back DAW listen %s:%s → mesa (control thread, echo_suppress)",
            self._listen_host,
            self._listen_port,
        )

    def stop(self) -> None:
        self._stop.set()
        try:
            self._jobs.put_nowait(None)
        except queue.Full:
            pass
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                log.debug("writeback OSC shutdown", exc_info=True)
            self._server = None
        if self._ctrl is not None:
            self._ctrl.join(timeout=1.5)
            self._ctrl = None
        try:
            self._sync.osc_bridge.stop_xremote()
        except Exception:
            log.debug("stop_xremote", exc_info=True)

    def snapshot(self) -> dict:
        return {
            "osc_enabled": True,
            "last_writeback_ns": self.last_writeback_ns,
            "writes_ok": self.writes_ok,
            "writes_dropped": self.writes_dropped,
        }

    def _enqueue(self, job: WriteJob) -> None:
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            self.writes_dropped += 1

    def _control_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            if job is None:
                break
            if job.origin == "daw":
                self._apply_daw(job)
            elif job.origin == "mixer_emit":
                self._emit_daw(job)

    def _apply_daw(self, job: WriteJob) -> None:
        if not self._guard.allow(job.key, job.value, "daw", job.kind):
            return
        bridge = self._sync.osc_bridge
        try:
            with self._sync.osc_io_lock:
                kind = job.key.split(":")
                if kind[0] == "ch" and "fader" in job.key:
                    bridge.set_fader(int(kind[1]), float(job.value))
                elif kind[0] == "ch" and "mute" in job.key:
                    bridge.set_mute(int(kind[1]), reaper_mute_to_mix_on(bool(job.value)))
                elif kind[0] == "ch" and "pan" in job.key:
                    bridge.set_pan(int(kind[1]), float(job.value))
                elif kind[0] == "ch" and "send" in job.key:
                    bridge.set_send(int(kind[1]), int(kind[3]), float(job.value))
                elif kind[0] == "bus":
                    bridge.set_bus_fader(int(kind[1]), float(job.value))
                else:
                    return
            self._guard.remember(job.key, job.value, "daw", job.kind)
            self.last_writeback_ns = time.time_ns()
            self.writes_ok += 1
            self._sync.note_writeback(self.last_writeback_ns)
            self._sync.apply_local_mix_from_key(job.key, job.value)
        except XAirOSCError as exc:
            log.warning("write-back %s: %s", job.key, exc)

    def _emit_daw(self, job: WriteJob) -> None:
        if not self._guard.allow(job.key, job.value, "xair", job.kind):
            return
        try:
            addr = job.args[0]
            val = job.args[1]
            self._daw.send_message(addr, val)
            self._guard.remember(job.key, job.value, "xair", job.kind)
        except Exception:
            log.debug("DAW emit %s", job.key, exc_info=True)

    def _on_mixer_message(self, msg: OscMessage) -> None:
        param = parse_xair_subscription(msg.address)
        if param is None or not msg.params:
            return
        raw = msg.params[0]
        self._sync.apply_mixer_param(param, raw)
        job = self._mixer_to_daw_job(param, raw)
        if job is not None:
            self._enqueue(job)

    def _mixer_to_daw_job(self, param: MixerParam, raw: Any) -> Optional[WriteJob]:
        if param.kind == "fader" and param.ch:
            val = float(raw)
            return WriteJob("mixer_emit", "float", f"ch:{param.ch}:fader", val, (f"/track/{param.ch}/volume", val))
        if param.kind == "mute" and param.ch:
            mix_on = bool(round(float(raw)))
            muted = mix_on_to_reaper_mute(mix_on)
            return WriteJob("mixer_emit", "bool", f"ch:{param.ch}:mute", muted, (f"/track/{param.ch}/mute", muted))
        if param.kind == "pan" and param.ch:
            val = float(raw)
            return WriteJob("mixer_emit", "float", f"ch:{param.ch}:pan", val, (f"/track/{param.ch}/pan", val))
        if param.kind == "send" and param.ch and param.send_idx:
            val = float(raw)
            return WriteJob(
                "mixer_emit",
                "float",
                f"ch:{param.ch}:send:{param.send_idx}",
                val,
                (f"/track/{param.ch}/send/{param.send_idx}", val),
            )
        if param.kind == "bus" and param.bus:
            val = float(raw)
            return WriteJob("mixer_emit", "float", f"bus:{param.bus}:fader", val, (f"/bus/{param.bus}/volume", val))
        return None

    def _in_ch(self, n: Optional[int]) -> bool:
        return n is not None and 1 <= n <= self._channels

    def _on_daw_fader(self, address: str, *args: Any) -> None:
        track = parse_track_index(address)
        if not self._in_ch(track) or not args:
            return
        try:
            val = float(args[0])
        except (TypeError, ValueError):
            return
        self._enqueue(WriteJob("daw", "float", f"ch:{track}:fader", val, ()))

    def _on_daw_mute(self, address: str, *args: Any) -> None:
        track = parse_track_index(address)
        if not self._in_ch(track) or not args:
            return
        muted = osc_flag_to_bool(args[0])
        self._enqueue(WriteJob("daw", "bool", f"ch:{track}:mute", muted, ()))

    def _on_daw_pan(self, address: str, *args: Any) -> None:
        track = parse_track_index(address)
        if not self._in_ch(track) or not args:
            return
        try:
            val = float(args[0])
        except (TypeError, ValueError):
            return
        self._enqueue(WriteJob("daw", "float", f"ch:{track}:pan", val, ()))

    def _on_daw_send(self, address: str, *args: Any) -> None:
        parsed = parse_send_index(address)
        if parsed is None or not args:
            return
        track, send_idx = parsed
        if not self._in_ch(track) or send_idx < 1 or send_idx > self._sends:
            return
        try:
            val = float(args[0])
        except (TypeError, ValueError):
            return
        self._enqueue(WriteJob("daw", "float", f"ch:{track}:send:{send_idx}", val, ()))

    def _on_daw_bus(self, address: str, *args: Any) -> None:
        bus = parse_bus_index(address)
        if bus is None or bus < 1 or bus > self._buses or not args:
            return
        try:
            val = float(args[0])
        except (TypeError, ValueError):
            return
        self._enqueue(WriteJob("daw", "float", f"bus:{bus}:fader", val, ()))
