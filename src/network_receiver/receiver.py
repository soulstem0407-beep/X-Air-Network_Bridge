"""
Cliente: UDP, buffer de jitter + recuperación ante pérdidas, salida PortAudio low-latency.
"""
from __future__ import annotations

import io
import logging
import queue
import socket
import sys
import threading
import time
from typing import Callable, Dict, Iterator, List, Optional, Tuple

import numpy as np

from .adaptive_jitter import (
    AdaptiveJitterConfig,
    desired_packets,
    packet_duration_ms,
    step_target,
)
from .metrics import ReceiverMetrics
from .reorder import RecvReorderLost
from ..network_sender.fec import FecAssembler, clamp_fec_group
from ..network_sender.opus_codec import (
    DEFAULT_OPUS_BITRATE,
    DEFAULT_OPUS_FRAME_MS,
    OpusBundle,
    clamp_opus_bitrate,
    clamp_opus_frame_ms,
)
from ..network_sender.protocol import (
    HEADER_SIZE,
    Codec,
    ParsedHeader,
    is_fec_packet,
    is_opus_packet,
    parse_header,
    validate_payload,
)
from ..virtual_device import linux_virtual_output

try:
    import sounddevice as sd
except ImportError as e:  # pragma: no cover
    sd = None
    _IMPORT_ERR = e
else:
    _IMPORT_ERR = None

try:
    import soundfile as sf
except ImportError:
    sf = None

from ..usb_capture.pcm import pcm16_from_bytes, pcm24_from_bytes

log = logging.getLogger(__name__)


def _decode_payload(ph: ParsedHeader, payload: bytes) -> np.ndarray:
    if ph.codec == Codec.PCM16:
        return pcm16_from_bytes(payload, ph.channels)
    if ph.codec == Codec.PCM24:
        return pcm24_from_bytes(payload, ph.channels)
    if ph.codec == Codec.FLAC:
        if sf is None:
            raise RuntimeError("FLAC en recepción requiere soundfile")
        data, _sr = sf.read(io.BytesIO(payload), dtype="int16", always_2d=True)
        fl = data.astype(np.float32) / 32767.0
        return np.clip(fl, -1.0, 1.0)
    raise ValueError(f"codec no soportado: {ph.codec}")


class NetworkReceiver:
    def __init__(
        self,
        *,
        bind_host: str,
        port: int,
        sample_rate: int,
        channels: int,
        samples_per_packet: int,
        jitter_packets: int,
        output_device_query: Optional[str],
        blocksize: Optional[int] = None,
        metrics_log_interval_s: float = 5.0,
        wasapi_exclusive: bool = False,
        audio_levels_hook: Optional[Callable[[Dict[int, float], int], None]] = None,
        pcm_tap: Optional[Callable[[np.ndarray], None]] = None,
        jitter_config: Optional[AdaptiveJitterConfig] = None,
        fec_enabled: bool = False,
        fec_group: int = 3,
        opus_enabled: bool = False,
        opus_bitrate: int = DEFAULT_OPUS_BITRATE,
        opus_frame_ms: float = DEFAULT_OPUS_FRAME_MS,
        expected_peer_host: Optional[str] = None,
    ) -> None:
        if sd is None:
            raise RuntimeError("sounddevice no disponible") from _IMPORT_ERR
        self.bind_host = bind_host
        self.port = int(port)
        self.expected_peer_host = str(expected_peer_host or "").strip()
        self._expected_peer_ips: set[str] = set()
        if self.expected_peer_host and self.expected_peer_host.lower() != "auto":
            try:
                self._expected_peer_ips = {
                    str(item[4][0])
                    for item in socket.getaddrinfo(
                        self.expected_peer_host, None, socket.AF_INET, socket.SOCK_DGRAM
                    )
                }
            except socket.gaierror:
                log.warning("No se pudo resolver peer UDP confiable %r", self.expected_peer_host)
        self.sample_rate = sample_rate
        self.channels = channels
        self.samples_per_packet = samples_per_packet
        self._jitter_cfg = jitter_config or AdaptiveJitterConfig.from_env(int(jitter_packets))
        self.jitter_packets = max(self._jitter_cfg.floor, int(jitter_packets))
        if not self._jitter_cfg.enabled:
            self.jitter_packets = self._jitter_cfg.floor
        self.output_device_query = output_device_query
        bs = blocksize if blocksize else samples_per_packet
        self.blocksize = int(max(8, bs))
        self.metrics_log_interval_s = float(max(0.0, metrics_log_interval_s))
        self.wasapi_exclusive = bool(wasapi_exclusive)

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        except OSError:
            log.debug("SO_RCVBUF no ajustable")

        self._stop = threading.Event()

        qmax = max(32, self._jitter_cfg.ceiling * 4)
        self._decoded_queue: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=qmax)

        self.metrics = ReceiverMetrics()
        self.metrics.set_jitter_control(
            self.jitter_packets,
            self._jitter_cfg.floor,
            self._jitter_cfg.ceiling,
            self._jitter_cfg.enabled,
        )
        self.fec_enabled = bool(fec_enabled)
        self.fec_group = clamp_fec_group(fec_group)
        self._fec: Optional[FecAssembler] = FecAssembler(self.fec_group) if self.fec_enabled else None
        self.metrics.set_fec_control(self.fec_enabled, self.fec_group)
        self.opus_enabled = bool(opus_enabled)
        self.opus_bitrate = clamp_opus_bitrate(opus_bitrate)
        self.opus_frame_ms = clamp_opus_frame_ms(opus_frame_ms)
        self._opus: Optional[OpusBundle] = None
        if self.opus_enabled:
            self._opus = OpusBundle(
                sample_rate=self.sample_rate,
                channels=self.channels,
                bitrate=self.opus_bitrate,
                frame_ms=self.opus_frame_ms,
                decode=True,
            )
            self.samples_per_packet = self._opus.frame_samples
        self.metrics.set_opus_control(self.opus_enabled, self.opus_bitrate, self.opus_frame_ms)
        max_pending = max(64, self._jitter_cfg.ceiling * 32)
        self._reorder = RecvReorderLost(
            channels=self.channels,
            nominal_samples_per_datagram=self.samples_per_packet,
            max_packets_pending=max_pending,
        )
        self._reorder.set_metrics(self.metrics)

        self._recv_thread: Optional[threading.Thread] = None
        self._play_stream: Optional[sd.OutputStream] = None
        self._jack_output = None
        self._last_metrics_log = 0.0
        self._audio_levels_hook = audio_levels_hook
        self._pcm_tap = pcm_tap
        self._pulse_modules_loaded: List[int] = []
        self._last_adapt_mono = 0.0
        self._hold_frames_left = 0
        self._quiet_streak = 0
        self._adapt_underruns_mark = 0
        self._adapt_rx_mark = 0

    def _resolve_out_dev(self) -> Optional[int]:
        """PortAudio fallback only. JACK named ports do not use this.

        ``device=None`` is the host default (almost always ``hw:HDMI`` here).
        The X18 is captured by ``xair_network_bridge_server``, not by this process: this
        machine enumerated only HDMI, analog, PipeWire and REAPER — no USB
        mixer — so requiring X18 on xair_network_bridge_client aborted before JACK started.
        HDMI remains forbidden; empty query uses the virtual/JACK path, never
        the PortAudio default.
        """
        from ..virtual_device.x18_device import is_hdmi_device

        q = self.output_device_query
        if q and str(q).strip():
            if is_hdmi_device(q):
                raise RuntimeError(
                    "XAIR_OUTPUT_DEVICE apunta a HDMI; el cliente no usa esa salida."
                )
            devices = sd.query_devices()
            ql = str(q).lower().strip()
            for i, d in enumerate(devices):
                name = str(d["name"])
                if is_hdmi_device(name):
                    continue
                if ql in name.lower() and int(d["max_output_channels"]) >= self.channels:
                    log.info("Salida seleccionada: [%s] %s", i, name)
                    return int(i)
            log.warning(
                "Salida no encontrada para %r con >= %s canales; no se usará HDMI.",
                q,
                self.channels,
            )

        if linux_virtual_output.should_try_linux_auto_virtual(q):
            linux_virtual_output.prepare_linux_virtual_audio_environment(
                channels=self.channels,
                sample_rate=self.sample_rate,
                pulse_modules_loaded=self._pulse_modules_loaded,
            )
            idx, name = linux_virtual_output.wait_for_output_device(
                sd,
                channels=self.channels,
                sample_rate=self.sample_rate,
                attempts=10,
                delay_s=0.35,
            )
            if idx is None:
                try:
                    devices = sd.query_devices()
                    log.error(
                        "No hay dispositivo PortAudio de salida con ≥%s canales (HDMI ignorado):",
                        self.channels,
                    )
                    for i, d in enumerate(devices):
                        log.error("  [%s] %s", i, d)
                except Exception:
                    log.debug("listado de dispositivos falló", exc_info=True)
                raise RuntimeError(
                    "No se encontró dispositivo de salida con suficientes canales "
                    "(JACK/PipeWire virtual). HDMI está prohibido como fallback. "
                    "`python3 -c \"import sounddevice as sd; print(sd.query_devices())\"`."
                )
            label = name or str(idx)
            log.info("Loopback virtual detectado: %s", label)
            log.info("Canales disponibles: %s", self.channels)
            log.info("Enviando audio decodificado al dispositivo virtual ...")
            return int(idx)

        raise RuntimeError(
            "Sin JACK y sin dispositivo virtual: no se abre el default de PortAudio "
            "(suele ser HDMI). Arranca PipeWire/JACK o define un sink 18ch."
        )

    def _maybe_log_metrics(self, force: bool = False) -> None:
        if self.metrics_log_interval_s <= 0 and not force:
            return
        now = time.monotonic()
        if not force and (now - self._last_metrics_log) < self.metrics_log_interval_s:
            return
        self._last_metrics_log = now
        snap = self.health_snapshot()
        log.info(
            "métricas RX: health=%s/%s perdidos=%s tarde=%s underrun=%s "
            "jitter_ema_ms=%s peak=%s target=%s cola=%s/%s fill%%=%s pps=%s loss_ppm=%s "
            "fec=%s/%s rec=%s unrec=%s rec_ppm=%s unrec_ppm=%s "
            "opus=%s/%s/%s err=%s",
            snap.get("rx_health"),
            snap.get("rx_health_reason"),
            snap["packets_lost"],
            snap["packets_late_drop"],
            snap.get("underruns"),
            snap["jitter_ms_ema"],
            snap.get("jitter_ms_peak"),
            snap.get("jitter_packets_target"),
            snap.get("queue_packets"),
            snap.get("queue_capacity"),
            snap["buffer_fill_percent_ema"],
            snap.get("rx_packets_per_sec"),
            snap.get("loss_window_ppm"),
            "on" if snap.get("fec_enabled") else "off",
            snap.get("fec_group"),
            snap.get("fec_recovered"),
            snap.get("fec_unrecoverable"),
            snap.get("fec_recovered_ppm"),
            snap.get("fec_unrecoverable_ppm"),
            "on" if snap.get("opus_enabled") else "off",
            snap.get("opus_bitrate"),
            snap.get("opus_frame"),
            snap.get("opus_decode_errors"),
        )

    def health_snapshot(self) -> dict:
        """Queue depth + ReceiverMetrics (for dashboard / logs). Not for the audio callback."""
        self.metrics.note_queue(
            self._decoded_queue.qsize(),
            self._decoded_queue.maxsize,
            self.jitter_packets,
        )
        self.metrics.refresh_window()
        return self.metrics.snapshot()

    def _maybe_adapt(self) -> None:
        """Grow/shrink playout depth. Receive thread only — never the audio callback."""
        cfg = self._jitter_cfg
        now = time.monotonic()
        if now - self._last_adapt_mono < cfg.interval_s:
            return
        self._last_adapt_mono = now
        self.metrics.refresh_window(now)
        self.metrics.note_queue(
            self._decoded_queue.qsize(),
            self._decoded_queue.maxsize,
            self.jitter_packets,
        )
        if not cfg.enabled:
            return
        if self.metrics.packets_received < 40:
            return
        und_delta = self.metrics.underruns - self._adapt_underruns_mark
        self._adapt_underruns_mark = self.metrics.underruns
        rx_delta = self.metrics.packets_received - self._adapt_rx_mark
        self._adapt_rx_mark = self.metrics.packets_received
        alive = rx_delta > 0
        pkt_ms = packet_duration_ms(self.samples_per_packet, self.sample_rate)
        desired = desired_packets(
            self.metrics.jitter_ms_ema,
            pkt_ms,
            cover_factor=cfg.cover_factor,
            floor=cfg.floor,
            ceiling=cfg.ceiling,
        )
        if und_delta > 0:
            self._quiet_streak = 0
        elif alive:
            self._quiet_streak += 1
        new = step_target(
            self.jitter_packets,
            desired,
            underrun=und_delta > 0,
            stream_alive=alive,
            quiet_streak=self._quiet_streak,
            shrink_after=cfg.shrink_idle_periods,
            floor=cfg.floor,
            ceiling=cfg.ceiling,
        )
        if new == self.jitter_packets:
            self.metrics.set_jitter_control(new, cfg.floor, cfg.ceiling, True)
            return
        old = self.jitter_packets
        self.jitter_packets = new
        if new > old:
            self._hold_frames_left += (new - old) * int(self.samples_per_packet)
        self.metrics.note_adapt_step()
        self.metrics.set_jitter_control(new, cfg.floor, cfg.ceiling, True)
        log.info(
            "jitter adapt %s → %s (desired=%s ema=%.2fms underrunΔ=%s hold_frames=%s)",
            old,
            new,
            desired,
            self.metrics.jitter_ms_ema,
            und_delta,
            self._hold_frames_left,
        )

    def _sync_fec_stats(self) -> None:
        fec = self._fec
        if fec is None:
            return
        self.metrics.set_fec_counts(fec.recovered, fec.unrecoverable, fec.groups_closed)

    def _enqueue_play_blocks(self, blocks: List[np.ndarray]) -> None:
        tap = self._pcm_tap
        for blk in blocks:
            if tap is not None:
                try:
                    tap(blk)
                except Exception:
                    log.debug("pcm_tap falló", exc_info=True)
            try:
                self._decoded_queue.put_nowait(blk)
            except queue.Full:
                try:
                    _ = self._decoded_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._decoded_queue.put_nowait(blk)
                except queue.Full:
                    log.warning("cola saturada; descartando bloque reproducido")

    def _decode_and_reorder(self, hdr: ParsedHeader, payload: bytes) -> None:
        """Decode one media datagram and push into reorder. UDP thread only."""
        try:
            if self._opus is not None:
                audio = self._opus.decode(payload, int(hdr.nframes))
            else:
                audio = _decode_payload(hdr, payload)
        except Exception as exc:
            if self._opus is not None:
                self.metrics.note_opus_decode_error()
            log.debug("paquete descartado: %s", exc)
            return
        if audio.shape[1] != hdr.channels and audio.shape[1] != self.channels:
            return
        if audio.shape[0] <= 0:
            return

        if self._audio_levels_hook is not None and audio.shape[1] == self.channels:
            try:
                ch_rms: Dict[int, float] = {}
                for c in range(self.channels):
                    col = audio[:, c].astype(np.float64, copy=False)
                    ch_rms[c + 1] = float(np.sqrt(np.mean(np.square(col))))
                self._audio_levels_hook(ch_rms, int(hdr.ts_ns))
            except Exception:
                log.debug("audio_levels_hook falló", exc_info=True)

        blocks = self._reorder.push(hdr.seq, int(audio.shape[0]), audio)
        self._enqueue_play_blocks(blocks)

    def _deliver_ready(self, ready: List[Tuple[ParsedHeader, bytes]]) -> None:
        self._sync_fec_stats()
        for hdr, payload in ready:
            self._decode_and_reorder(hdr, payload)

    def _receiver_loop(self) -> None:
        self._sock.settimeout(0.25)
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(65535)
            except socket.timeout:
                if self._fec is not None:
                    self._deliver_ready(self._fec.on_idle())
                self._maybe_adapt()
                self._maybe_log_metrics(force=False)
                continue
            except OSError:
                break
            if self._expected_peer_ips and str(addr[0]) not in self._expected_peer_ips:
                log.warning("datagrama XBRI ignorado desde peer no autorizado %s", addr[0])
                continue
            if len(data) < HEADER_SIZE:
                continue
            try:
                hdr, payload = parse_header(data)
                validate_payload(hdr, payload)
            except Exception as exc:
                log.debug("paquete descartado: %s", exc)
                continue

            if is_opus_packet(hdr) and self._opus is None:
                continue
            if is_fec_packet(hdr):
                if self._fec is None:
                    continue
                self._deliver_ready(self._fec.ingest_fec(hdr, payload))
                self._maybe_adapt()
                self._maybe_log_metrics(force=False)
                continue

            now = time.monotonic()
            self.metrics.on_arrival_nominal_timing(hdr.nframes, hdr.sample_rate, now)

            if self._fec is not None:
                ready = self._fec.ingest_media(hdr, payload)
            else:
                ready = [(hdr, payload)]
            self._deliver_ready(ready)

            self._maybe_adapt()
            self._maybe_log_metrics(force=False)

    def _prime_queue(self, timeout_s: float = 3.0) -> None:
        t0 = time.monotonic()
        while not self._stop.is_set():
            if self._decoded_queue.qsize() >= self.jitter_packets:
                return
            if time.monotonic() - t0 > timeout_s:
                log.warning(
                    "primer llenado del buffer tardó más de %ss; arranca igual (cola menor)",
                    timeout_s,
                )
                return
            time.sleep(0.002)

    def _latency_seconds(self) -> float:
        """Ventana solicitada explícita a PortAudio (~2× bloques)."""
        return max(float(self.blocksize) * 2.0 / float(self.sample_rate), 0.004)

    def _pull_play_frames(self, frames: int) -> np.ndarray:
        """Fill ``frames`` interleaved samples from the decoded queue (realtime-safe)."""
        need = int(frames)
        target = self.jitter_packets
        qsize = self._decoded_queue.qsize()
        cap = self._decoded_queue.maxsize

        hold = self._hold_frames_left
        if hold > 0 and qsize < target:
            self._hold_frames_left = max(0, hold - need)
            self.metrics.note_adapt_hold()
            self.metrics.note_queue(qsize, cap, target)
            return np.zeros((need, self.channels), dtype=np.float32)

        if qsize > target + 2:
            try:
                self._decoded_queue.get_nowait()
                self.metrics.note_adapt_drop()
            except queue.Empty:
                pass

        self.metrics.note_queue(self._decoded_queue.qsize(), cap, target)

        acc = np.zeros((0, self.channels), dtype=np.float32)
        while acc.shape[0] < need:
            try:
                blk = self._decoded_queue.get_nowait()
            except queue.Empty:
                break
            acc = np.vstack([acc, blk])

        out = np.zeros((need, self.channels), dtype=np.float32)
        if acc.shape[0] >= need:
            out[:] = acc[:need]
            if acc.shape[0] > need:
                rem = acc[need:]
                try:
                    self._decoded_queue.put_nowait(rem)
                except queue.Full:
                    log.debug("sin espacio para remanente")
        else:
            out[: acc.shape[0]] = acc
            if acc.shape[0] < need:
                self.metrics.note_underrun()

        self.metrics.note_queue(self._decoded_queue.qsize(), cap, target)
        return out

    def _try_start_named_jack(self) -> bool:
        """JACK/PipeWire named duplex ports. False → caller uses PortAudio.

        Bug: the JACK client used to register only playback (``outports``) and
        advertised PipeWire ``Audio/Source``, so QjackCtl/REAPER saw
        ``CH##_out`` / ``01_Kick`` and no capture terminals. Fix is permanent
        in ``jack_named_output``: same-order ``_in`` + ``_out`` (18+18).
        ``XAIR_VIRTUAL_PORT_NAMES`` only chooses JACK vs PortAudio; it cannot
        disable capture ports on the JACK path. Transport (UDP/jitter/FEC/Opus)
        is unchanged.
        """
        from ..virtual_device import jack_named_output, port_names

        if not linux_virtual_output.should_try_named_jack(self.output_device_query):
            return False
        if not jack_named_output.jack_module_available():
            log.info(
                "JACK-Client no instalado; salida PortAudio "
                "(pip: JACK-Client, o XAIR_VIRTUAL_PORT_NAMES=false)."
            )
            return False
        names, errors = port_names.resolve_playback_port_names(self.channels)
        # Capture + playback short names; channel order is the OSC 1..N order.
        in_names, out_names = port_names.duplex_io_names(names)
        if errors:
            log.info(
                "Nombres OSC incompletos (%s); puertos con fallback donde falte.",
                len(errors),
            )
        linux_virtual_output.unload_null_sink_named(port_names.jack_client_name())
        try:
            self._jack_output = jack_named_output.start_named_jack_output(
                client_name=port_names.jack_client_name(),
                port_names=names,
                sample_rate=self.sample_rate,
                pull_frames=self._pull_play_frames,
                in_port_names=in_names,
                out_port_names=out_names,
            )
        except Exception as exc:
            log.warning(
                "JACK/PipeWire con puertos nombrados no disponible (%s); "
                "usando PortAudio / sink virtual.",
                exc,
            )
            self._jack_output = None
            return False
        return True

    def _open_play_stream(self, dev: Optional[int]) -> sd.OutputStream:
        latency_s = min(0.05, max(self._latency_seconds(), 8.0 / float(self.sample_rate)))
        base_kw: dict = {
            "device": dev,
            "samplerate": self.sample_rate,
            "channels": self.channels,
            "dtype": "float32",
            "callback": self._play_callback,
            "blocksize": self.blocksize,
            "latency": latency_s,
        }
        if sys.platform.startswith("win") and getattr(sd, "WasapiSettings", None) is not None:
            try:
                extra = sd.WasapiSettings(exclusive=self.wasapi_exclusive)
                base_kw["extra_settings"] = extra
            except (TypeError, RuntimeError):
                log.debug("WasapiSettings no aplicable")

        try:
            kw = dict(base_kw)
            kw["prime_output_buffers_using_stream_callback"] = False
            return sd.OutputStream(**kw)
        except TypeError:
            return sd.OutputStream(**base_kw)

    def _play_callback(self, outdata, frames, _time, status) -> None:  # type: ignore[no-untyped-def]
        if status:
            log.debug("PortAudio salida status: %s", status)
        outdata[:] = self._pull_play_frames(int(frames))

    def start(self) -> None:
        self._sock.bind((self.bind_host, self.port))
        log.info(
            "Escuchando UDP %s:%s (jitter target=%s adaptive=%s floor=%s ceil=%s, "
            "fec=%s group=%s, opus=%s bitrate=%s frame=%.1fms, block=%s Hz=%s)",
            self.bind_host,
            self.port,
            self.jitter_packets,
            self._jitter_cfg.enabled,
            self._jitter_cfg.floor,
            self._jitter_cfg.ceiling,
            "on" if self.fec_enabled else "off",
            self.fec_group,
            "on" if self.opus_enabled else "off",
            self.opus_bitrate,
            self.opus_frame_ms,
            self.blocksize,
            self.sample_rate,
        )

        self._recv_thread = threading.Thread(target=self._receiver_loop, name="xair-udp-recv", daemon=True)
        self._recv_thread.start()

        self._prime_queue()

        # X18 USB belongs to xair_network_bridge_server. This host has no mixer in PortAudio
        # (only HDMI / analog / PipeWire / REAPER); do not require it here or
        # JACK never starts. HDMI is still never opened as PortAudio default.
        if self._try_start_named_jack():
            log.info(
                "JACK duplex activo (%s in + %s out; UDP block=%s Hz=%s).",
                self.channels,
                self.channels,
                self.blocksize,
                self.sample_rate,
            )
        else:
            dev = self._resolve_out_dev()
            self._play_stream = self._open_play_stream(dev)
            self._play_stream.start()

            latency_attr = getattr(self._play_stream, "latency", None)
            log.info(
                "Salida iniciada blocksize=%s latency_solicitado≈%.4f repr=%s Wasapi_exclusive=%s",
                self.blocksize,
                min(0.05, max(self._latency_seconds(), 8.0 / float(self.sample_rate))),
                latency_attr,
                self.wasapi_exclusive,
            )

    def stop(self) -> None:
        self._stop.set()
        if self._recv_thread is not None and self._recv_thread.is_alive():
            self._recv_thread.join(timeout=1.0)

        if self._jack_output is not None:
            try:
                self._jack_output.stop()
            except Exception:
                log.debug("JACK stop", exc_info=True)
            self._jack_output = None

        if self._play_stream is not None:
            self._play_stream.stop()
            self._play_stream.close()
            self._play_stream = None

        try:
            self._sock.close()
        except OSError:
            pass

        if self._pulse_modules_loaded:
            linux_virtual_output.unload_pulse_modules(self._pulse_modules_loaded)
            self._pulse_modules_loaded.clear()

        if self._opus is not None:
            try:
                self._opus.close()
            except Exception:
                log.debug("Opus close", exc_info=True)
            self._opus = None

        self._maybe_log_metrics(force=True)
        log.info("Cliente detenido. Resumen métricas: %s", self.metrics.snapshot())
