"""
Coordinación de estado OSC (consola) con métricas del flujo audio UDP + receptor.

No modifica el protocolo de audio; el receptor llama hooks opcionales.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..package.meta import default_package_state_path, empty_package_snapshot
from ..recorder.sidecar import empty_record_snapshot
from ..release.meta import default_release_state_path, empty_release_snapshot
from ..service.systemd import default_service_state_path, empty_service_snapshot
from ..testing.suite import default_test_state_path, empty_test_snapshot
from ..xair_control.osc_bridge import XAirOSCBridge
from .integration import integration_snapshot

log = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        return default
    return float(v)


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        return default
    return int(float(v))


@dataclass
class SyncChannelState:
    ch: int
    name: Optional[str] = None
    fader: Optional[float] = None
    gain: Optional[float] = None
    mute: Optional[bool] = None
    pan: Optional[float] = None
    meter_osc: Optional[int] = None
    rms_audio: Optional[float] = None
    last_osc_ns: Optional[int] = None
    last_audio_ns: Optional[int] = None
    last_eq_write_ns: Optional[int] = None
    last_dyn_write_ns: Optional[int] = None
    last_fx_write_ns: Optional[int] = None


@dataclass
class SyncState:
    channels: Dict[int, SyncChannelState] = field(default_factory=dict)
    last_report_ns: Optional[int] = None


def _rms_per_channel(float_block, num_ch: int) -> Dict[int, float]:
    """float_block: (nframes, ch) numpy-like; devuelve RMS lineal por canal 1..num_ch."""
    import numpy as np

    out: Dict[int, float] = {}
    for c in range(int(num_ch)):
        col = float_block[:, c].astype(np.float64, copy=False)
        out[c + 1] = float(math.sqrt(float(np.mean(np.square(col)))))
    return out


def _rms_linear_to_dbfs(rms_linear: float) -> float:
    return 20.0 * math.log10(max(float(rms_linear), 1e-12))


def _meter_int_to_db(meter_int: int) -> float:
    """Documentación X AIR: resolución 1/256 dB en entero del meter."""
    return float(meter_int) / 256.0


class SyncManager:
    """
    Mantiene SyncState bajo lock; refresca OSC vía XAirOSCBridge;
    niveles RMS y métricas de receptor vía callbacks opcionales.
    """

    def __init__(
        self,
        *,
        osc_bridge: XAirOSCBridge,
        channels: int = 18,
        level_tolerance_db: Optional[float] = None,
        report_interval_s: Optional[float] = None,
        cli_state_path: Optional[Path] = None,
    ) -> None:
        self._bridge = osc_bridge
        nch = int(channels)
        if not (1 <= nch <= 32):
            raise ValueError("channels debe estar entre 1 y 32")
        self._n_channels = nch
        self._level_tolerance_db = (
            float(level_tolerance_db)
            if level_tolerance_db is not None
            else _env_float("XAIR_SYNC_LEVEL_TOLERANCE_DB", 6.0)
        )
        self._report_interval_s = (
            float(report_interval_s)
            if report_interval_s is not None
            else _env_float("XAIR_SYNC_REPORT_INTERVAL_SEC", 5.0)
        )
        self._lock = threading.Lock()
        self.state = SyncState(
            channels={i: SyncChannelState(ch=i) for i in range(1, self._n_channels + 1)},
            last_report_ns=None,
        )
        self._last_receiver_metrics: Optional[Dict[str, Any]] = None
        self._cli_state_path = Path(cli_state_path) if cli_state_path is not None else None
        self._last_cli_snapshot_mono = 0.0
        self._cli_snapshot_min_interval_s = 0.25
        self._metrics_provider: Optional[Callable[[], Optional[Dict[str, Any]]]] = None
        self._return_provider: Optional[Callable[[], Optional[Dict[str, Any]]]] = None
        self._osc_io_lock = threading.Lock()
        self._writeback = None
        self._return_path: Dict[str, Any] = {
            "enabled": False,
            "osc_enabled": False,
            "audio_enabled": False,
            "last_writeback_ns": None,
            "return_health": "off",
            "return_safety": "off",
        }
        self._discovery_provider: Optional[Callable[[], Optional[Dict[str, Any]]]] = None
        self._discovery: Dict[str, Any] = {
            "discovery_enabled": False,
            "discovered_devices": [],
            "last_discovery_ts": None,
            "discovery_health": "off",
        }
        self._dsp_provider: Optional[Callable[[], Optional[Dict[str, Any]]]] = None
        self._dsp: Dict[str, Any] = {
            "eq_enabled": False,
            "dyn_enabled": False,
            "fx_enabled": False,
            "eq_health": "off",
            "dyn_health": "off",
            "fx_health": "off",
            "last_eq_write_ns": None,
            "last_dyn_write_ns": None,
            "last_fx_write_ns": None,
            "last_change": None,
            "writes_ok": 0,
            "writes_err": 0,
        }
        self._dsp_ctrl = None
        self._record_provider: Optional[Callable[[], Optional[Dict[str, Any]]]] = None
        self._record: Dict[str, Any] = empty_record_snapshot()
        self._recorder = None
        self._test: Dict[str, Any] = empty_test_snapshot()
        self._service: Dict[str, Any] = empty_service_snapshot()
        self._package: Dict[str, Any] = empty_package_snapshot(self._cli_state_path)
        self._release: Dict[str, Any] = empty_release_snapshot(self._cli_state_path)

        self._periodic_refresh_stop: Optional[threading.Event] = None
        self._periodic_thread: Optional[threading.Thread] = None

    @classmethod
    def from_env(
        cls,
        *,
        level_tolerance_db: Optional[float] = None,
        report_interval_s: Optional[float] = None,
        channels: Optional[int] = None,
        cli_state_path: Optional[Path] = None,
        osc_host: Optional[str] = None,
        osc_port: Optional[int] = None,
    ) -> SyncManager:
        tol = (
            float(level_tolerance_db)
            if level_tolerance_db is not None
            else _env_float("XAIR_SYNC_LEVEL_TOLERANCE_DB", 6.0)
        )
        rep = (
            float(report_interval_s)
            if report_interval_s is not None
            else _env_float("XAIR_SYNC_REPORT_INTERVAL_SEC", 5.0)
        )
        nch = int(channels) if channels is not None else _env_int("XAIR_CHANNELS", 18)
        return cls(
            osc_bridge=XAirOSCBridge.from_env(host=osc_host, port=osc_port),
            channels=nch,
            level_tolerance_db=tol,
            report_interval_s=rep,
            cli_state_path=cli_state_path,
        )

    def ingest_receiver_metrics(self, snapshot: Dict[str, Any]) -> None:
        """Copia perezosa de ReceiverMetrics.snapshot() u otro dict (jitter, buffer, etc.)."""
        with self._lock:
            self._last_receiver_metrics = dict(snapshot)

    def set_metrics_provider(
        self, provider: Optional[Callable[[], Optional[Dict[str, Any]]]]
    ) -> None:
        """Optional callback used when writing the CLI JSON snapshot.

        Invoked on the snapshot cadence (~0.25 s), never per UDP packet, so it
        must stay cheap (e.g. ``ReceiverMetrics.snapshot()``).
        """
        self._metrics_provider = provider

    def set_discovery_provider(
        self, provider: Optional[Callable[[], Optional[Dict[str, Any]]]]
    ) -> None:
        """Optional discovery snapshot. Control/snapshot cadence only."""
        self._discovery_provider = provider

    def set_dsp_provider(
        self, provider: Optional[Callable[[], Optional[Dict[str, Any]]]]
    ) -> None:
        self._dsp_provider = provider

    def attach_dsp(self, ctrl: Any) -> None:
        self._dsp_ctrl = ctrl

    def set_record_provider(
        self, provider: Optional[Callable[[], Optional[Dict[str, Any]]]]
    ) -> None:
        self._record_provider = provider

    def attach_recorder(self, rec: Any) -> None:
        self._recorder = rec

    def note_dsp_write(self, section: str, ts_ns: int, *, ch: Optional[int] = None) -> None:
        """Control-thread DSP write or /xremote observe. No OSC SET here."""
        mark = int(ts_ns)
        with self._lock:
            if section == "eq":
                self._dsp["last_eq_write_ns"] = mark
                self._dsp["eq_enabled"] = True
            elif section == "dyn":
                self._dsp["last_dyn_write_ns"] = mark
                self._dsp["dyn_enabled"] = True
            elif section == "fx":
                self._dsp["last_fx_write_ns"] = mark
                self._dsp["fx_enabled"] = True
            if ch is not None:
                st = self.state.channels.get(int(ch))
                if st is not None:
                    if section == "eq":
                        st.last_eq_write_ns = mark
                    elif section == "dyn":
                        st.last_dyn_write_ns = mark
                    elif section == "fx":
                        st.last_fx_write_ns = mark
                    st.last_osc_ns = mark
            self.state.last_report_ns = mark

    @property
    def osc_bridge(self) -> XAirOSCBridge:
        return self._bridge

    @property
    def osc_io_lock(self) -> threading.Lock:
        return self._osc_io_lock

    def set_return_provider(
        self, provider: Optional[Callable[[], Optional[Dict[str, Any]]]]
    ) -> None:
        self._return_provider = provider

    def attach_writeback(self, writeback: Any) -> None:
        self._writeback = writeback

    def note_writeback(self, ts_ns: int) -> None:
        with self._lock:
            self._return_path["last_writeback_ns"] = int(ts_ns)
            self._return_path["osc_enabled"] = True
            self._return_path["enabled"] = True

    def apply_local_mix_from_key(self, key: str, value: Any) -> None:
        """Update cached mix after a successful DAW write-back (control thread)."""
        parts = str(key).split(":")
        mark = time.time_ns()
        with self._lock:
            if parts[0] == "ch" and len(parts) >= 3:
                ch = int(parts[1])
                st = self.state.channels.get(ch)
                if st is None:
                    return
                leaf = parts[2]
                if leaf == "fader":
                    st.fader = float(value)
                elif leaf == "mute":
                    st.mute = bool(value)
                elif leaf == "pan":
                    st.pan = float(value)
                st.last_osc_ns = mark
            self.state.last_report_ns = mark

    def apply_mixer_param(self, param: Any, raw: Any) -> None:
        """Immediate mixer → state from /xremote. No OSC SET here."""
        mark = time.time_ns()
        kind = getattr(param, "kind", None)
        with self._lock:
            if kind == "fader" and getattr(param, "ch", None):
                st = self.state.channels.get(int(param.ch))
                if st is not None:
                    st.fader = float(raw)
                    st.last_osc_ns = mark
            elif kind == "mute" and getattr(param, "ch", None):
                st = self.state.channels.get(int(param.ch))
                if st is not None:
                    mix_on = bool(round(float(raw)))
                    st.mute = not mix_on
                    st.last_osc_ns = mark
            elif kind == "pan" and getattr(param, "ch", None):
                st = self.state.channels.get(int(param.ch))
                if st is not None:
                    st.pan = float(raw)
                    st.last_osc_ns = mark
            self.state.last_report_ns = mark

    def mean_forward_rms(self) -> Optional[float]:
        with self._lock:
            vals = [
                float(cs.rms_audio)
                for cs in self.state.channels.values()
                if cs.rms_audio is not None
            ]
        if not vals:
            return None
        return float(sum(vals) / len(vals))

    def refresh_osc_state(self) -> None:
        """Lee nombre/fader/gain/meter por canal desde la mesa; marca last_osc_ns si hay dato nuevo."""
        mark = time.time_ns()
        bulk: Dict[int, Dict[str, Any]] = {}
        br = self._bridge
        with self._osc_io_lock:
            for ch in range(1, self._n_channels + 1):
                vals: Dict[str, Any] = {}
                try:
                    vals["name"] = br.get_channel_name(ch)
                except Exception:
                    pass
                try:
                    vals["fader"] = float(br.get_fader(ch))
                except Exception:
                    pass
                try:
                    vals["gain"] = float(br.get_gain(ch))
                except Exception:
                    pass
                try:
                    vals["meter_osc"] = int(br.get_meter(ch))
                except Exception:
                    pass

                if vals:
                    bulk[ch] = vals

        lr = time.time_ns()
        with self._lock:
            for ch, vals in bulk.items():
                st = self.state.channels[ch]
                if "name" in vals:
                    st.name = vals["name"]
                if "fader" in vals:
                    st.fader = vals["fader"]
                if "gain" in vals:
                    st.gain = vals["gain"]
                if "meter_osc" in vals:
                    st.meter_osc = vals["meter_osc"]
                st.last_osc_ns = mark
            self.state.last_report_ns = lr

    def periodic_refresh(self, interval_sec: float) -> None:
        """
        Hilo daemon que llama `refresh_osc_state()` cada `interval_sec`.
        Las escrituras al estado OSC se aplican en bloque bajo `_lock`
        dentro de `refresh_osc_state` (sin mantener candado durante I/O OSC).
        """
        if self._periodic_thread is not None and self._periodic_thread.is_alive():
            log.debug("periodic_refresh ya en ejecución")
            return
        iv = float(max(0.1, interval_sec))

        stop = threading.Event()
        self._periodic_refresh_stop = stop

        def _run() -> None:
            while not stop.is_set():
                try:
                    self.refresh_osc_state()
                    self._write_cli_snapshot()
                except Exception:
                    log.exception("periodic_refresh: error en ciclo OSC")
                if stop.wait(timeout=iv):
                    break

        th = threading.Thread(target=_run, name="xair-sync-periodic", daemon=True)
        self._periodic_thread = th
        th.start()

    def update_audio_levels(self, ch_rms: Dict[int, float], ts_ns: int) -> None:
        with self._lock:
            ts_i = int(ts_ns)
            for ch, val in ch_rms.items():
                ci = int(ch)
                if 1 <= ci <= self._n_channels:
                    cs = self.state.channels[ci]
                    cs.rms_audio = float(val)
                    cs.last_audio_ns = ts_i
        self._maybe_cli_snapshot_after_audio()

    def _maybe_cli_snapshot_after_audio(self) -> None:
        if self._cli_state_path is None:
            return
        now = time.monotonic()
        if now - self._last_cli_snapshot_mono < self._cli_snapshot_min_interval_s:
            return
        self._last_cli_snapshot_mono = now
        self._write_cli_snapshot()

    def _write_cli_snapshot(self) -> None:
        if self._cli_state_path is None:
            return
        try:
            provider = self._metrics_provider
            if provider is not None:
                try:
                    snap = provider()
                except Exception:
                    log.debug("metrics_provider falló", exc_info=True)
                    snap = None
                if snap:
                    self.ingest_receiver_metrics(snap)
            rprov = self._return_provider
            extra_ret: Optional[Dict[str, Any]] = None
            if rprov is not None:
                try:
                    extra_ret = rprov()
                except Exception:
                    log.debug("return_provider falló", exc_info=True)
                    extra_ret = None
            if extra_ret:
                with self._lock:
                    self._return_path.update(extra_ret)
                    self._return_path["enabled"] = bool(
                        self._return_path.get("osc_enabled") or self._return_path.get("audio_enabled")
                    )
            dprov = self._discovery_provider
            extra_disc: Optional[Dict[str, Any]] = None
            if dprov is not None:
                try:
                    extra_disc = dprov()
                except Exception:
                    log.debug("discovery_provider falló", exc_info=True)
                    extra_disc = None
            if extra_disc:
                with self._lock:
                    self._discovery.update(extra_disc)
            dspp = self._dsp_provider
            extra_dsp: Optional[Dict[str, Any]] = None
            if dspp is not None:
                try:
                    extra_dsp = dspp()
                except Exception:
                    log.debug("dsp_provider falló", exc_info=True)
                    extra_dsp = None
            if extra_dsp:
                with self._lock:
                    self._dsp.update(extra_dsp)
            recp = self._record_provider
            extra_rec: Optional[Dict[str, Any]] = None
            if recp is not None:
                try:
                    extra_rec = recp()
                except Exception:
                    log.debug("record_provider falló", exc_info=True)
                    extra_rec = None
            if extra_rec:
                with self._lock:
                    self._record.update(extra_rec)
            try:
                tpath = default_test_state_path(self._cli_state_path)
                if tpath.is_file():
                    with open(tpath, encoding="utf-8") as f:
                        extra_test = json.load(f)
                    if isinstance(extra_test, dict):
                        with self._lock:
                            self._test.update(extra_test)
            except Exception:
                log.debug("test sidecar falló", exc_info=True)
            try:
                spath = default_service_state_path(self._cli_state_path)
                if spath.is_file():
                    with open(spath, encoding="utf-8") as f:
                        extra_svc = json.load(f)
                    if isinstance(extra_svc, dict):
                        with self._lock:
                            self._service.update(extra_svc)
            except Exception:
                log.debug("service sidecar falló", exc_info=True)
            try:
                ppath = default_package_state_path(self._cli_state_path)
                extra_pkg = empty_package_snapshot(self._cli_state_path)
                if ppath.is_file():
                    with open(ppath, encoding="utf-8") as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict):
                        extra_pkg.update(loaded)
                with self._lock:
                    self._package.update(extra_pkg)
            except Exception:
                log.debug("package sidecar falló", exc_info=True)
            try:
                rpath = default_release_state_path(self._cli_state_path)
                extra_rel = empty_release_snapshot(self._cli_state_path)
                if rpath.is_file():
                    with open(rpath, encoding="utf-8") as f:
                        loaded_rel = json.load(f)
                    if isinstance(loaded_rel, dict):
                        extra_rel.update(loaded_rel)
                with self._lock:
                    self._release.update(extra_rel)
            except Exception:
                log.debug("release sidecar falló", exc_info=True)
            wb = self._writeback
            if wb is not None:
                try:
                    wbs = wb.snapshot()
                except Exception:
                    wbs = None
                if wbs:
                    with self._lock:
                        self._return_path.update(wbs)
                        self._return_path["enabled"] = bool(
                            self._return_path.get("osc_enabled") or self._return_path.get("audio_enabled")
                        )
            rep = self.build_sync_report()
            payload = {
                "schema": 1,
                "active": True,
                "channels": self._n_channels,
                "report_interval_sec": self._report_interval_s,
                "report": rep,
                "exported_time_ns": time.time_ns(),
            }
            self._atomic_write_json(self._cli_state_path, payload)
        except Exception:
            log.exception("no se pudo escribir XAIR_SYNC_STATE_PATH")

    def _write_cli_snapshot_inactive(self) -> None:
        if self._cli_state_path is None:
            return
        try:
            rep = self.build_sync_report(active=False)
            payload = {
                "schema": 1,
                "active": False,
                "channels": self._n_channels,
                "report_interval_sec": self._report_interval_s,
                "report": rep,
                "exported_time_ns": time.time_ns(),
            }
            self._atomic_write_json(self._cli_state_path, payload)
        except Exception:
            log.debug("no se pudo marcar snapshot como inactivo", exc_info=True)

    def _atomic_write_json(self, path: Path, data: dict) -> None:
        tmp_path = path.with_suffix(path.suffix + ".tmp")

        try:
            # 1. Escribimos el JSON en un archivo temporal normal
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f)

            # 2. Reemplazo atómico y seguro
            os.replace(tmp_path, path)

        except Exception:
            # Limpieza si quedó basura
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            raise



    def compute_drift_ns(self, ch: int) -> Optional[int]:
        with self._lock:
            cs = self.state.channels.get(int(ch))
            if cs is None:
                return None
            if cs.last_audio_ns is None or cs.last_osc_ns is None:
                return None
            return int(cs.last_audio_ns - cs.last_osc_ns)

    def compute_global_drift_ns(self) -> Optional[int]:
        with self._lock:
            drifts: List[int] = []
            for ch_i in range(1, self._n_channels + 1):
                cs = self.state.channels[ch_i]
                if cs.last_audio_ns is not None and cs.last_osc_ns is not None:
                    drifts.append(int(cs.last_audio_ns - cs.last_osc_ns))
            if not drifts:
                return None
            return int(round(sum(drifts) / len(drifts)))

    def compare_levels(self, ch: int) -> Dict[str, Any]:
        with self._lock:
            cs = self.state.channels.get(int(ch))
            if cs is None:
                return {
                    "ch": int(ch),
                    "rms_audio": None,
                    "meter_osc": None,
                    "delta": None,
                    "ok": False,
                }
            raw_rms = cs.rms_audio
            osc_m = cs.meter_osc
            if raw_rms is None or osc_m is None:
                return {
                    "ch": int(ch),
                    "rms_audio": raw_rms,
                    "meter_osc": osc_m,
                    "delta": None,
                    "ok": False,
                }
            db_rms = _rms_linear_to_dbfs(raw_rms)
            db_m = _meter_int_to_db(int(osc_m))
            delta = abs(db_rms - db_m)
            ok = delta <= self._level_tolerance_db
            return {
                "ch": int(ch),
                "rms_audio": raw_rms,
                "meter_osc": int(osc_m),
                "delta": float(delta),
                "ok": bool(ok),
            }

    def build_sync_report(self, *, active: bool = True) -> Dict[str, Any]:
        with self._lock:
            gdrift = None
            acc: List[int] = []
            for ch_i in range(1, self._n_channels + 1):
                cs = self.state.channels[ch_i]
                if cs.last_audio_ns is not None and cs.last_osc_ns is not None:
                    acc.append(int(cs.last_audio_ns - cs.last_osc_ns))
            if acc:
                gdrift = int(round(sum(acc) / len(acc)))

            rows: List[Dict[str, Any]] = []
            for ch_i in range(1, self._n_channels + 1):
                cs = self.state.channels[ch_i]
                dlev = None
                lok = None
                d_ns = (
                    int(cs.last_audio_ns - cs.last_osc_ns)
                    if cs.last_audio_ns is not None and cs.last_osc_ns is not None
                    else None
                )
                raw_rms = cs.rms_audio
                osc_m = cs.meter_osc
                if raw_rms is not None and osc_m is not None:
                    dlev = float(abs(_rms_linear_to_dbfs(raw_rms) - _meter_int_to_db(int(osc_m))))
                    lok = bool(dlev <= self._level_tolerance_db)
                rows.append(
                    {
                        "ch": ch_i,
                        "name": cs.name,
                        "fader": cs.fader,
                        "gain": cs.gain,
                        "mute": cs.mute,
                        "pan": cs.pan,
                        "rms_audio": raw_rms,
                        "meter_osc": osc_m,
                        "delta_level": dlev,
                        "level_ok": lok,
                        "drift_ns": d_ns,
                        "last_eq_write_ns": cs.last_eq_write_ns,
                        "last_dyn_write_ns": cs.last_dyn_write_ns,
                        "last_fx_write_ns": cs.last_fx_write_ns,
                    }
                )

            metrics = (
                dict(self._last_receiver_metrics) if self._last_receiver_metrics is not None else None
            )
            integ = integration_snapshot(
                active=bool(active),
                last_ts=time.time(),
                metrics=metrics or {},
                return_path=self._return_path,
                discovery=self._discovery,
                dsp=self._dsp,
                record=self._record,
            )
            rep: Dict[str, Any] = {
                "global_drift_ns": gdrift,
                "channels": rows,
                "receiver_metrics": metrics,
                "level_tolerance_db": self._level_tolerance_db,
                "last_report_ns": self.state.last_report_ns,
                "return_path": dict(self._return_path),
                "discovery": dict(self._discovery),
                "dsp": dict(self._dsp),
                "record": dict(self._record),
                "test": dict(self._test),
                "service": dict(self._service),
                "package": dict(self._package),
                "release": dict(self._release),
                "integration": integ,
                "integration_health": integ["integration_health"],
                "last_integration_ts": integ["last_integration_ts"],
            }
            return rep

    def close(self) -> None:
        try:
            if self._periodic_refresh_stop is not None:
                self._periodic_refresh_stop.set()
            if self._periodic_thread is not None and self._periodic_thread.is_alive():
                self._periodic_thread.join(timeout=5.0)
        except Exception:
            log.debug("cierre periodic_refresh", exc_info=True)
        self._periodic_thread = None
        self._periodic_refresh_stop = None
        rec = self._recorder
        self._recorder = None
        if rec is not None:
            try:
                rec.stop()
            except Exception:
                log.debug("cierre recorder", exc_info=True)
        dsp = self._dsp_ctrl
        self._dsp_ctrl = None
        if dsp is not None:
            try:
                dsp.stop()
            except Exception:
                log.debug("cierre dsp", exc_info=True)
        wb = self._writeback
        self._writeback = None
        if wb is not None:
            try:
                wb.stop()
            except Exception:
                log.debug("cierre writeback", exc_info=True)
        self._write_cli_snapshot_inactive()
        try:
            self._bridge.close()
        except Exception:
            pass

    def __enter__(self) -> SyncManager:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def hook_update_audio_levels_from_block(
    sync: SyncManager,
    float_block,
    ts_ns: int,
) -> None:
    """Helper: bloque (frames, ch) float32 y ts del paquete UDP."""
    nch = int(float_block.shape[1])
    ch_rms = _rms_per_channel(float_block, nch)
    sync.update_audio_levels(ch_rms, int(ts_ns))
