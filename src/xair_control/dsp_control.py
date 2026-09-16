"""EQ / dyn / FX write-back on a dedicated control thread.

Never called from JACK/PipeWire/PortAudio callbacks. SET goes through
``SyncManager.osc_io_lock``. ``/xremote`` updates memory only (no SET).
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from pythonosc.osc_message import OscMessage

from .dsp import classify_dsp_health, empty_dsp_snapshot, parse_dsp_address
from .osc_bridge import XAirOSCError

log = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class DspJob:
    path: str
    value: float
    as_int: bool
    section: str
    ch: Optional[int]


class DspController:
    """Queue mixer DSP writes; expose snapshot health for the dashboard."""

    def __init__(
        self,
        sync: Any,
        *,
        eq_enabled: bool = False,
        dyn_enabled: bool = False,
        fx_enabled: bool = False,
    ) -> None:
        self._sync = sync
        self.eq_enabled = bool(eq_enabled)
        self.dyn_enabled = bool(dyn_enabled)
        self.fx_enabled = bool(fx_enabled)
        self._jobs: "queue.Queue[Optional[DspJob]]" = queue.Queue(maxsize=256)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._owns_xremote = False
        self._lock = threading.Lock()
        self.writes_ok = 0
        self.writes_err = 0
        self.last_eq_write_ns: Optional[int] = None
        self.last_dyn_write_ns: Optional[int] = None
        self.last_fx_write_ns: Optional[int] = None
        self.last_change: Optional[dict] = None
        self._win_ok0 = 0
        self._win_err0 = 0
        self._win_t0 = time.monotonic()

    @classmethod
    def from_env(cls, sync: Any) -> DspController:
        return cls(
            sync,
            eq_enabled=_env_bool("XAIR_EQ", False),
            dyn_enabled=_env_bool("XAIR_DYN", False),
            fx_enabled=_env_bool("XAIR_FX", False),
        )

    @property
    def enabled(self) -> bool:
        return self.eq_enabled or self.dyn_enabled or self.fx_enabled

    def start(self) -> None:
        if not self.enabled:
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="xair-dsp", daemon=True)
        self._thread.start()
        bridge = self._sync.osc_bridge
        if bridge.xremote_active:
            bridge.add_xremote_listener(self._on_mixer_message)
        else:
            bridge.start_xremote(self._on_mixer_message)
            self._owns_xremote = True
        log.info(
            "DSP OSC control-thread eq=%s dyn=%s fx=%s",
            "on" if self.eq_enabled else "off",
            "on" if self.dyn_enabled else "off",
            "on" if self.fx_enabled else "off",
        )

    def stop(self) -> None:
        self._stop.set()
        try:
            self._jobs.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        if self._owns_xremote:
            try:
                self._sync.osc_bridge.stop_xremote()
            except Exception:
                log.debug("dsp stop_xremote", exc_info=True)
            self._owns_xremote = False

    def enqueue(self, job: DspJob) -> None:
        if not self._section_on(job.section):
            return
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            with self._lock:
                self.writes_err += 1

    def write_eq_on(self, ch: int, on: bool) -> None:
        from .dsp import eq_on_path

        self.enqueue(DspJob(eq_on_path(ch), 1.0 if on else 0.0, False, "eq", int(ch)))

    def write_eq_band(self, ch: int, band: int, leaf: str, value: float) -> None:
        from .dsp import eq_band_path

        self.enqueue(DspJob(eq_band_path(ch, band, leaf), float(value), False, "eq", int(ch)))

    def write_hpf(self, ch: int, value: float) -> None:
        from .dsp import hpf_path

        self.enqueue(DspJob(hpf_path(ch), float(value), False, "eq", int(ch)))

    def write_gate(self, ch: int, leaf: str, value: float) -> None:
        from .dsp import gate_path

        self.enqueue(DspJob(gate_path(ch, leaf), float(value), False, "dyn", int(ch)))

    def write_dyn(self, ch: int, leaf: str, value: float) -> None:
        from .dsp import dyn_path

        self.enqueue(DspJob(dyn_path(ch, leaf), float(value), False, "dyn", int(ch)))

    def write_fx_send(self, ch: int, fx: int, value: float) -> None:
        from .dsp import fx_send_path

        self.enqueue(DspJob(fx_send_path(ch, fx), float(value), False, "fx", int(ch)))

    def write_fx_return(self, fx: int, leaf: str, value: float) -> None:
        from .dsp import fx_return_path

        self.enqueue(DspJob(fx_return_path(fx, leaf), float(value), False, "fx", None))

    def write_fx_type(self, fx: int, value: int) -> None:
        from .dsp import fx_type_path

        self.enqueue(DspJob(fx_type_path(fx), float(value), True, "fx", None))

    def write_fx_par(self, fx: int, par: int, value: float) -> None:
        from .dsp import fx_par_path

        self.enqueue(DspJob(fx_par_path(fx, par), float(value), False, "fx", None))

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            elapsed = max(now - self._win_t0, 1e-3)
            dok = self.writes_ok - self._win_ok0
            derr = self.writes_err - self._win_err0
            tot = dok + derr
            ppm = (float(derr) / float(tot) * 1e6) if tot else 0.0
            if elapsed >= 5.0:
                self._win_t0 = now
                self._win_ok0 = self.writes_ok
                self._win_err0 = self.writes_err
            ages = {
                "eq": _age_s(self.last_eq_write_ns),
                "dyn": _age_s(self.last_dyn_write_ns),
                "fx": _age_s(self.last_fx_write_ns),
            }
            snap = empty_dsp_snapshot(
                eq=self.eq_enabled, dyn=self.dyn_enabled, fx=self.fx_enabled
            )
            snap["eq_health"] = classify_dsp_health(
                enabled=self.eq_enabled,
                errors_window=derr if self.eq_enabled else 0,
                error_ppm=ppm if self.eq_enabled else 0.0,
                last_write_age_s=ages["eq"],
            )
            snap["dyn_health"] = classify_dsp_health(
                enabled=self.dyn_enabled,
                errors_window=derr if self.dyn_enabled else 0,
                error_ppm=ppm if self.dyn_enabled else 0.0,
                last_write_age_s=ages["dyn"],
            )
            snap["fx_health"] = classify_dsp_health(
                enabled=self.fx_enabled,
                errors_window=derr if self.fx_enabled else 0,
                error_ppm=ppm if self.fx_enabled else 0.0,
                last_write_age_s=ages["fx"],
            )
            snap["last_eq_write_ns"] = self.last_eq_write_ns
            snap["last_dyn_write_ns"] = self.last_dyn_write_ns
            snap["last_fx_write_ns"] = self.last_fx_write_ns
            snap["last_change"] = dict(self.last_change) if self.last_change else None
            snap["writes_ok"] = self.writes_ok
            snap["writes_err"] = self.writes_err
            return snap

    def _section_on(self, section: str) -> bool:
        if section == "eq":
            return self.eq_enabled
        if section == "dyn":
            return self.dyn_enabled
        if section == "fx":
            return self.fx_enabled
        return False

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._jobs.get(timeout=0.1)
            except queue.Empty:
                continue
            if job is None:
                break
            self._apply(job)

    def _apply(self, job: DspJob) -> None:
        bridge = self._sync.osc_bridge
        try:
            with self._sync.osc_io_lock:
                if job.as_int:
                    echoed = bridge.set_int_param(job.path, int(round(job.value)))
                    value: Any = int(echoed)
                else:
                    value = bridge.set_float_param(job.path, float(job.value))
            ts = time.time_ns()
            with self._lock:
                self.writes_ok += 1
                self._stamp(job.section, ts)
                self.last_change = {
                    "section": job.section,
                    "ch": job.ch,
                    "path": job.path,
                    "value": value,
                    "ts_ns": ts,
                    "origin": "control",
                }
            self._sync.note_dsp_write(job.section, ts, ch=job.ch)
        except XAirOSCError as exc:
            with self._lock:
                self.writes_err += 1
            log.warning("DSP SET %s: %s", job.path, exc)

    def _stamp(self, section: str, ts: int) -> None:
        if section == "eq":
            self.last_eq_write_ns = ts
        elif section == "dyn":
            self.last_dyn_write_ns = ts
        elif section == "fx":
            self.last_fx_write_ns = ts

    def _on_mixer_message(self, msg: "OscMessage") -> None:
        """Mixer → state. Must not SET."""
        param = parse_dsp_address(msg.address)
        if param is None or not msg.params or not self._section_on(param.section):
            return
        raw = msg.params[0]
        try:
            if isinstance(raw, bool):
                value: Any = int(raw)
            else:
                value = float(raw)
        except (TypeError, ValueError):
            return
        ts = time.time_ns()
        with self._lock:
            self._stamp(param.section, ts)
            self.last_change = {
                "section": param.section,
                "ch": param.ch,
                "path": param.path or msg.address,
                "value": value,
                "ts_ns": ts,
                "origin": "mixer",
            }
        self._sync.note_dsp_write(param.section, ts, ch=param.ch)


def _age_s(ts_ns: Optional[int]) -> Optional[float]:
    if ts_ns is None:
        return None
    return max(0.0, (time.time_ns() - int(ts_ns)) / 1e9)
