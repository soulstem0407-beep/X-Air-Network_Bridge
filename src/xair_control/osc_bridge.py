"""
Cliente OSC para X AIR (ej. X18) vía UDP — sólo control, sin audio.

Protocolo MUSIC Group descrito en *X AIR Mixer Series Remote Control Protocol* (OSC sobre UDP).
Puerto por defecto de la consola: 10024.

Rutas implementadas (Parameters / documentación serie X AIR):
    /ch/XX/mix/fader
    /headamp/XX/gain
    /ch/XX/config/name
    Suscripción de medidores: /meters + (string "/meters/1", entero de suscripción).
    /xremote — keep-alive ~10 s; la mesa reenvía cambios de parámetros al puerto origen.

``start_xremote`` arranca un hilo de recepción en el mismo socket (la mesa
responde al puerto desde el que se envió ``/xremote``). GET/SET en ese modo
esperan en una cola de waiters; el callback sólo ve tráfico no reclamado.
El CLI one-shot no arranca el hilo y sigue con recv bloqueante.
"""
from __future__ import annotations

import logging
import os
import socket
import struct
import threading
import time
import numbers
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple

from pythonosc.osc_message_builder import OscMessageBuilder
from pythonosc.osc_packet import OscPacket, ParseError
from pythonosc.osc_message import OscMessage

log = logging.getLogger(__name__)

DEFAULT_OSC_PORT = 10024
DEFAULT_METER_SUB_INT = 9
# La mesa caduca /xremote a ~10 s; refrescar por debajo de eso.
DEFAULT_XREMOTE_INTERVAL_SEC = 8.0
XREMOTE_INTERVAL_MIN = 1.0
XREMOTE_INTERVAL_MAX = 9.0


class XAirOSCError(Exception):
    pass


class XAirOscTimeoutError(XAirOSCError):
    pass


def _is_osc_scalar_float(x: object) -> bool:
    return isinstance(x, numbers.Real) and not isinstance(x, bool)


def _ch_token(ch: int) -> str:
    if not (1 <= ch <= 18):
        raise ValueError("ch debe estar entre 1 y 18")
    return f"{ch:02d}"


def _bus_token(bus: int) -> str:
    if not (1 <= bus <= 16):
        raise ValueError("bus debe estar entre 1 y 16")
    return f"{bus:02d}"


def _build_msg(address: str, args: Optional[List[Tuple[Any, Optional[str]]]] = None) -> bytes:
    b = OscMessageBuilder(address)
    if args:
        for val, typ in args:
            b.add_arg(val, typ)
    return b.build().dgram


def clamp_xremote_interval_sec(value: float) -> float:
    """Keep-alive must stay under the mixer's ~10 s /xremote window."""
    return max(XREMOTE_INTERVAL_MIN, min(XREMOTE_INTERVAL_MAX, float(value)))


def parse_meter_blob(blob: bytes) -> List[int]:
    """Decode an X AIR ``/meters/1`` blob to int16 levels (dB × 256).

    Firmware has used both endiannesses for the leading count; pick the
    interpretation whose count fits the remaining bytes.
    """
    if not blob or len(blob) < 4:
        return []

    def _try(count_endian: str, sample_endian: str) -> Optional[List[int]]:
        n = struct.unpack(f"{count_endian}i", blob[:4])[0]
        if n < 0 or n > 256:
            return None
        need = 4 + n * 2
        if len(blob) < need:
            return None
        return list(struct.unpack(f"{sample_endian}{n}h", blob[4:need]))

    for count_e, sample_e in ((">", ">"), ("<", "<"), (">", "<"), ("<", ">")):
        got = _try(count_e, sample_e)
        if got is not None:
            return got
    nfit = (len(blob) - 4) // 2
    if nfit <= 0:
        return []
    return list(struct.unpack(f">{nfit}h", blob[4 : 4 + nfit * 2]))


@dataclass
class _OscWaiter:
    """In-flight GET/SET waiting for a matching datagram on the recv thread."""

    predicate: Callable[[OscMessage], bool]
    event: threading.Event = field(default_factory=threading.Event)
    message: Optional[OscMessage] = None


class XAirOSCBridge:
    def __init__(
        self,
        host: str,
        port: int = DEFAULT_OSC_PORT,
        *,
        timeout_s: float = 2.0,
        meter_sub_int: Optional[int] = None,
        send_xremote: bool = False,
    ) -> None:
        self.host = host.strip()
        self.port = int(port)
        self.timeout_s = float(timeout_s)
        self.meter_sub_int = (
            int(meter_sub_int)
            if meter_sub_int is not None
            else int(os.getenv("XAIR_OSC_METER_SUB_ARG", str(DEFAULT_METER_SUB_INT)))
        )
        self.send_xremote = send_xremote

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", 0))
        self._dest = (self.host, self.port)

        interval_raw = os.getenv("XAIR_OSC_XREMOTE_INTERVAL_SEC", str(DEFAULT_XREMOTE_INTERVAL_SEC))
        try:
            interval = float(interval_raw)
        except ValueError:
            interval = DEFAULT_XREMOTE_INTERVAL_SEC
        self._xremote_interval_s = clamp_xremote_interval_sec(interval)
        self._on_unsolicited: Optional[Callable[[OscMessage], None]] = None
        self._recv_thread: Optional[threading.Thread] = None
        self._xremote_stop = threading.Event()
        self._waiters: List[_OscWaiter] = []
        self._waiters_lock = threading.Lock()
        # One /meters subscribe fills all channels; cache so refresh_osc_state
        # does not send 18 identical subscriptions per cycle.
        self._meter_cache: Optional[Tuple[float, List[int]]] = None
        self._meter_cache_ttl_s = 0.20

    @property
    def xremote_active(self) -> bool:
        t = self._recv_thread
        return t is not None and t.is_alive()

    def close(self) -> None:
        self.stop_xremote()
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> XAirOSCBridge:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _emit_xremote_keepalive(self) -> None:
        """Fire-and-forget ``/xremote`` (no reply from the mixer)."""
        self._sock.sendto(_build_msg("/xremote"), self._dest)

    def _optional_xremote(self) -> None:
        # Keep-alive thread owns /xremote while a subscription session is live.
        if self.xremote_active:
            return
        if self.send_xremote:
            self._emit_xremote_keepalive()

    def _recv_thread_running(self) -> bool:
        return self.xremote_active

    def _dispatch_incoming(self, msg: OscMessage) -> None:
        """Deliver to the first matching GET/SET waiter, else the xremote callback."""
        with self._waiters_lock:
            for i, waiter in enumerate(self._waiters):
                try:
                    matched = waiter.predicate(msg)
                except Exception:
                    continue
                if matched:
                    waiter.message = msg
                    del self._waiters[i]
                    waiter.event.set()
                    return
        cb = self._on_unsolicited
        if cb is None:
            return
        try:
            cb(msg)
        except Exception:
            log.debug("callback /xremote", exc_info=True)

    def _xremote_loop(self) -> None:
        """Single thread: /xremote keep-alive + recv. Never used on the audio path."""
        last_keep = 0.0
        interval = self._xremote_interval_s
        while not self._xremote_stop.is_set():
            now = time.monotonic()
            if now - last_keep >= interval:
                try:
                    self._emit_xremote_keepalive()
                except OSError:
                    if self._xremote_stop.is_set():
                        break
                    log.debug("envio /xremote", exc_info=True)
                last_keep = now
            self._sock.settimeout(0.25)
            try:
                data, _addr = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            except OSError:
                if self._xremote_stop.is_set():
                    break
                log.debug("recv /xremote", exc_info=True)
                break
            try:
                packet = OscPacket(data)
            except ParseError:
                continue
            for tm in packet.messages:
                self._dispatch_incoming(tm.message)

    def start_xremote(
        self,
        on_message: Callable[[OscMessage], None],
        *,
        interval_s: Optional[float] = None,
    ) -> None:
        """Subscribe to mixer parameter changes on this socket.

        The X AIR remote protocol forwards edits to the UDP source port of
        ``/xremote`` for ~10 s. This starts a recv thread so GET/SET and
        unsolicited updates can share that port. ``on_message`` must stay
        non-blocking (no nested GET on this bridge).
        """
        if self._recv_thread_running():
            raise RuntimeError("sesión /xremote ya activa")
        if interval_s is not None:
            self._xremote_interval_s = clamp_xremote_interval_sec(interval_s)
        self._on_unsolicited = on_message
        self._xremote_stop.clear()
        self._recv_thread = threading.Thread(
            target=self._xremote_loop,
            name="xair-xremote-rx",
            daemon=True,
        )
        self._recv_thread.start()
        try:
            self._emit_xremote_keepalive()
        except OSError as exc:
            self.stop_xremote()
            raise XAirOSCError(f"no se pudo enviar /xremote: {exc}") from exc
        log.info(
            "/xremote activo hacia %s:%s (keep-alive %.1fs)",
            self.host,
            self.port,
            self._xremote_interval_s,
        )

    def stop_xremote(self) -> None:
        """Stop keep-alive/recv; in-flight GET/SET waiters time out."""
        was_running = self._recv_thread_running()
        self._xremote_stop.set()
        self._on_unsolicited = None
        thread = self._recv_thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._recv_thread = None
        with self._waiters_lock:
            pending = list(self._waiters)
            self._waiters.clear()
        for waiter in pending:
            waiter.event.set()
        if was_running:
            log.info("/xremote detenido (%s:%s)", self.host, self.port)

    def add_xremote_listener(self, on_message: Callable[[OscMessage], None]) -> None:
        """Fan-out another control-plane callback. No SET/GET inside the callback."""
        prev = self._on_unsolicited
        if prev is None:
            self._on_unsolicited = on_message
            return

        def _fanout(msg: OscMessage, first=prev, second=on_message) -> None:
            try:
                first(msg)
            except Exception:
                log.debug("xremote listener", exc_info=True)
            second(msg)

        self._on_unsolicited = _fanout

    def _receive_matching(
        self,
        predicate: Callable[[OscMessage], bool],
        *,
        timeout_s: Optional[float] = None,
    ) -> OscMessage:
        limit = timeout_s if timeout_s is not None else self.timeout_s
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            self._sock.settimeout(max(0.001, deadline - time.monotonic()))
            try:
                data, _addr = self._sock.recvfrom(65536)
            except socket.timeout:
                continue
            try:
                packet = OscPacket(data)
            except ParseError:
                continue
            for tm in packet.messages:
                try:
                    if predicate(tm.message):
                        return tm.message
                except Exception:
                    continue
        raise XAirOscTimeoutError(f"sin respuesta OSC antes de {limit}s")

    def _drain_transact(
        self,
        dgram: bytes,
        predicate: Callable[[OscMessage], bool],
        *,
        timeout_s: Optional[float] = None,
    ) -> OscMessage:
        limit = timeout_s if timeout_s is not None else self.timeout_s
        self._optional_xremote()
        if self._recv_thread_running():
            # Register before send so the SET/GET echo cannot miss the waiter
            # and leak into the subscription callback (echo storm risk).
            waiter = _OscWaiter(predicate=predicate)
            with self._waiters_lock:
                self._waiters.append(waiter)
            try:
                self._sock.sendto(dgram, self._dest)
                if not waiter.event.wait(limit):
                    raise XAirOscTimeoutError(f"sin respuesta OSC antes de {limit}s")
                if waiter.message is None:
                    raise XAirOscTimeoutError(f"sin respuesta OSC antes de {limit}s")
                return waiter.message
            finally:
                with self._waiters_lock:
                    try:
                        self._waiters.remove(waiter)
                    except ValueError:
                        pass
        self._sock.sendto(dgram, self._dest)
        return self._receive_matching(predicate, timeout_s=timeout_s)

    @staticmethod
    def from_env(
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
    ) -> XAirOSCBridge:
        oh = host if host is not None else os.getenv("XAIR_OSC_HOST", "192.168.1.100").strip()
        op = int(port) if port is not None else int(os.getenv("XAIR_OSC_PORT", str(DEFAULT_OSC_PORT)))
        timeout_s = float(os.getenv("XAIR_OSC_TIMEOUT", "2.0"))
        xremote = os.getenv("XAIR_OSC_XREMOTE", "").strip().lower() in ("1", "true", "yes", "on")
        return XAirOSCBridge(oh, op, timeout_s=timeout_s, send_xremote=xremote)

    # -------------------------
    # MÉTODOS ORIGINALES
    # -------------------------

    def ping(self) -> Tuple[str, Tuple[Any, ...]]:
        dgram = _build_msg("/status")
        msg = self._drain_transact(
            dgram,
            lambda m: m.address == "/status"
            and bool(m.params)
            and isinstance(m.params[0], (str, bytes)),
        )
        st_raw = msg.params[0]
        status_str = st_raw.decode() if isinstance(st_raw, bytes) else str(st_raw)
        return status_str, tuple(msg.params)

    def get_fader(self, ch: int) -> float:
        path = f"/ch/{_ch_token(ch)}/mix/fader"
        dgram = _build_msg(path)
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1 and _is_osc_scalar_float(m.params[0]),
        )
        return float(msg.params[0])

    def set_fader(self, ch: int, value: float) -> float:
        path = f"/ch/{_ch_token(ch)}/mix/fader"
        dgram = _build_msg(path, [(float(value), OscMessageBuilder.ARG_TYPE_FLOAT)])
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1 and _is_osc_scalar_float(m.params[0]),
        )
        return float(msg.params[0])

    def get_gain(self, ch: int) -> float:
        path = f"/headamp/{_ch_token(ch)}/gain"
        dgram = _build_msg(path)
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1 and _is_osc_scalar_float(m.params[0]),
        )
        return float(msg.params[0])

    def set_gain(self, ch: int, value: float) -> float:
        path = f"/headamp/{_ch_token(ch)}/gain"
        dgram = _build_msg(path, [(float(value), OscMessageBuilder.ARG_TYPE_FLOAT)])
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1 and _is_osc_scalar_float(m.params[0]),
        )
        return float(msg.params[0])

    def get_channel_name(self, ch: int) -> str:
        path = f"/ch/{_ch_token(ch)}/config/name"
        dgram = _build_msg(path)
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p
            and len(m.params) >= 1
            and isinstance(m.params[0], (str, bytes)),
        )
        nm = msg.params[0]
        return nm.decode() if isinstance(nm, bytes) else str(nm)

    def set_channel_name(self, ch: int, name: str) -> str:
        path = f"/ch/{_ch_token(ch)}/config/name"
        dgram = _build_msg(path, [(str(name), OscMessageBuilder.ARG_TYPE_STRING)])
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p
            and len(m.params) >= 1
            and isinstance(m.params[0], (str, bytes)),
        )
        nm = msg.params[0]
        return nm.decode() if isinstance(nm, bytes) else str(nm)

    # -------------------------
    # MÉTODOS NUEVOS COMPLETOS
    # -------------------------

    def get_mute(self, ch: int) -> bool:
        path = f"/ch/{_ch_token(ch)}/mix/on"
        dgram = _build_msg(path)
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1 and _is_osc_scalar_float(m.params[0]),
        )
        return bool(round(float(msg.params[0])))

    def set_mute(self, ch: int, state: bool) -> bool:
        path = f"/ch/{_ch_token(ch)}/mix/on"
        val = 1.0 if state else 0.0
        dgram = _build_msg(path, [(val, OscMessageBuilder.ARG_TYPE_FLOAT)])
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1 and _is_osc_scalar_float(m.params[0]),
        )
        return bool(round(float(msg.params[0])))

    def get_pan(self, ch: int) -> float:
        path = f"/ch/{_ch_token(ch)}/mix/pan"
        dgram = _build_msg(path)
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1 and _is_osc_scalar_float(m.params[0]),
        )
        return float(msg.params[0])

    def set_pan(self, ch: int, value: float) -> float:
        path = f"/ch/{_ch_token(ch)}/mix/pan"
        dgram = _build_msg(path, [(float(value), OscMessageBuilder.ARG_TYPE_FLOAT)])
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1 and _is_osc_scalar_float(m.params[0]),
        )
        return float(msg.params[0])

    def get_send(self, ch: int, send_idx: int) -> float:
        path = f"/ch/{_ch_token(ch)}/mix/{send_idx:02d}"
        dgram = _build_msg(path)
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1 and _is_osc_scalar_float(m.params[0]),
        )
        return float(msg.params[0])

    def set_send(self, ch: int, send_idx: int, value: float) -> float:
        path = f"/ch/{_ch_token(ch)}/mix/{send_idx:02d}"
        dgram = _build_msg(path, [(float(value), OscMessageBuilder.ARG_TYPE_FLOAT)])
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1 and _is_osc_scalar_float(m.params[0]),
        )
        return float(msg.params[0])

    def get_lr_fader(self) -> float:
        path = "/lr/mix/fader"
        dgram = _build_msg(path)
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1 and _is_osc_scalar_float(m.params[0]),
        )
        return float(msg.params[0])

    def set_lr_fader(self, value: float) -> float:
        path = "/lr/mix/fader"
        dgram = _build_msg(path, [(float(value), OscMessageBuilder.ARG_TYPE_FLOAT)])
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1 and _is_osc_scalar_float(m.params[0]),
        )
        return float(msg.params[0])

    def get_bus_fader(self, bus: int) -> float:
        path = f"/bus/{_bus_token(bus)}/mix/fader"
        dgram = _build_msg(path)
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1 and _is_osc_scalar_float(m.params[0]),
        )
        return float(msg.params[0])

    def set_bus_fader(self, bus: int, value: float) -> float:
        path = f"/bus/{_bus_token(bus)}/mix/fader"
        dgram = _build_msg(path, [(float(value), OscMessageBuilder.ARG_TYPE_FLOAT)])
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1 and _is_osc_scalar_float(m.params[0]),
        )
        return float(msg.params[0])

    def get_float_param(self, path: str) -> float:
        """GET a float OSC parameter. Control thread / CLI only."""
        dgram = _build_msg(path)
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1 and _is_osc_scalar_float(m.params[0]),
        )
        return float(msg.params[0])

    def set_float_param(self, path: str, value: float) -> float:
        """SET a float OSC parameter. Control thread / CLI only — never audio."""
        dgram = _build_msg(path, [(float(value), OscMessageBuilder.ARG_TYPE_FLOAT)])
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1 and _is_osc_scalar_float(m.params[0]),
        )
        return float(msg.params[0])

    def get_int_param(self, path: str) -> int:
        dgram = _build_msg(path)
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1,
        )
        raw = msg.params[0]
        if isinstance(raw, bool):
            return int(raw)
        if _is_osc_scalar_float(raw):
            return int(round(float(raw)))
        if isinstance(raw, int):
            return int(raw)
        raise XAirOSCError(f"{path}: entero esperado")

    def set_int_param(self, path: str, value: int) -> int:
        dgram = _build_msg(path, [(int(value), OscMessageBuilder.ARG_TYPE_INT)])
        msg = self._drain_transact(
            dgram,
            lambda m, p=path: m.address == p and len(m.params) >= 1,
        )
        raw = msg.params[0]
        if _is_osc_scalar_float(raw) or isinstance(raw, int):
            return int(round(float(raw)))
        raise XAirOSCError(f"{path}: entero esperado")

    def get_eq_on(self, ch: int) -> bool:
        from .dsp import eq_on_path

        return bool(round(self.get_float_param(eq_on_path(ch))))

    def set_eq_on(self, ch: int, on: bool) -> bool:
        from .dsp import eq_on_path

        return bool(round(self.set_float_param(eq_on_path(ch), 1.0 if on else 0.0)))

    def get_eq_band(self, ch: int, band: int, leaf: str) -> float:
        from .dsp import eq_band_path

        return self.get_float_param(eq_band_path(ch, band, leaf))

    def set_eq_band(self, ch: int, band: int, leaf: str, value: float) -> float:
        from .dsp import eq_band_path

        return self.set_float_param(eq_band_path(ch, band, leaf), float(value))

    def get_hpf(self, ch: int) -> float:
        from .dsp import hpf_path

        return self.get_float_param(hpf_path(ch))

    def set_hpf(self, ch: int, value: float) -> float:
        from .dsp import hpf_path

        return self.set_float_param(hpf_path(ch), float(value))

    def get_gate(self, ch: int, leaf: str) -> float:
        from .dsp import gate_path

        return self.get_float_param(gate_path(ch, leaf))

    def set_gate(self, ch: int, leaf: str, value: float) -> float:
        from .dsp import gate_path

        return self.set_float_param(gate_path(ch, leaf), float(value))

    def get_dyn(self, ch: int, leaf: str) -> float:
        from .dsp import dyn_path

        return self.get_float_param(dyn_path(ch, leaf))

    def set_dyn(self, ch: int, leaf: str, value: float) -> float:
        from .dsp import dyn_path

        return self.set_float_param(dyn_path(ch, leaf), float(value))

    def get_fx_send(self, ch: int, fx: int) -> float:
        from .dsp import fx_send_path

        return self.get_float_param(fx_send_path(ch, fx))

    def set_fx_send(self, ch: int, fx: int, value: float) -> float:
        from .dsp import fx_send_path

        return self.set_float_param(fx_send_path(ch, fx), float(value))

    def get_fx_return(self, fx: int, leaf: str = "fader") -> float:
        from .dsp import fx_return_path

        return self.get_float_param(fx_return_path(fx, leaf))

    def set_fx_return(self, fx: int, leaf: str, value: float) -> float:
        from .dsp import fx_return_path

        return self.set_float_param(fx_return_path(fx, leaf), float(value))

    def get_fx_type(self, fx: int) -> int:
        from .dsp import fx_type_path

        return self.get_int_param(fx_type_path(fx))

    def set_fx_type(self, fx: int, value: int) -> int:
        from .dsp import fx_type_path

        return self.set_int_param(fx_type_path(fx), int(value))

    def get_fx_par(self, fx: int, par: int) -> float:
        from .dsp import fx_par_path

        return self.get_float_param(fx_par_path(fx, par))

    def set_fx_par(self, fx: int, par: int, value: float) -> float:
        from .dsp import fx_par_path

        return self.set_float_param(fx_par_path(fx, par), float(value))

    def fetch_meter_levels(self) -> List[int]:
        """Subscribe ``/meters/1`` once and return the int16 vector (cached briefly)."""
        now = time.monotonic()
        cached = self._meter_cache
        if cached is not None and (now - cached[0]) < self._meter_cache_ttl_s:
            return cached[1]
        dgram = _build_msg(
            "/meters",
            [
                ("/meters/1", OscMessageBuilder.ARG_TYPE_STRING),
                (int(self.meter_sub_int), OscMessageBuilder.ARG_TYPE_INT),
            ],
        )
        msg = self._drain_transact(
            dgram,
            lambda m: m.address == "/meters/1" and len(m.params) >= 1,
        )
        raw = msg.params[0]
        if isinstance(raw, (bytes, bytearray, memoryview)):
            blob = bytes(raw)
        else:
            raise XAirOSCError("respuesta /meters/1 sin blob")
        levels = parse_meter_blob(blob)
        self._meter_cache = (now, levels)
        return levels

    def get_meter(self, ch: int) -> int:
        """Level for channel ``ch`` (1-based) from the last ``/meters/1`` blob."""
        _ch_token(ch)
        levels = self.fetch_meter_levels()
        idx = ch - 1
        if idx >= len(levels):
            raise XAirOSCError(
                f"meter CH {ch} fuera del blob (/meters/1 tiene {len(levels)} niveles)"
            )
        return int(levels[idx])
