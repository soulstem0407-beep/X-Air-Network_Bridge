"""Stereo DAW → mixer USB return over a second XBRI UDP port.

Independent of the 18-ch forward path (no jitter/FEC/Opus changes).
Capture/play callbacks only copy PCM; encode/decode stay on UDP threads.
"""
from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from typing import Callable, Optional

import numpy as np

from ..network_sender.protocol import HEADER_SIZE, Codec, build_header, parse_header
from ..usb_capture.pcm import pack_pcm24_le, float_to_pcm24, pcm24_from_bytes
from .return_safety import ReturnSafety, classify_return_health

log = logging.getLogger(__name__)

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover
    sd = None


class ReturnAudioSender:
    """Client: capture DAW stereo → UDP. Encode on a send thread."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        sample_rate: int = 48000,
        frame_samples: int = 480,
        input_device_query: Optional[str] = None,
        safety: Optional[ReturnSafety] = None,
        forward_rms_provider: Optional[Callable[[], Optional[float]]] = None,
    ) -> None:
        if sd is None:
            raise RuntimeError("sounddevice no disponible para return audio")
        self.host = host
        self.port = int(port)
        self.sample_rate = int(sample_rate)
        self.frame_samples = int(max(64, frame_samples))
        self.input_device_query = input_device_query
        self.safety = safety if safety is not None else ReturnSafety()
        self._forward_rms = forward_rms_provider
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._seq = 0
        self._q: "queue.Queue[Optional[np.ndarray]]" = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stream = None
        self.packets_sent = 0
        self.gated_packets = 0
        self._pending = np.empty((0, 2), dtype=np.float32)

    def _resolve_in_dev(self) -> Optional[int]:
        q = self.input_device_query
        if not q or not str(q).strip():
            return None
        ql = str(q).lower().strip()
        for i, d in enumerate(sd.query_devices()):
            if ql in str(d["name"]).lower() and int(d["max_input_channels"]) >= 2:
                log.info("Return captura: [%s] %s", i, d["name"])
                return int(i)
        log.warning("Return input %r no encontrado; usando default", q)
        return None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._send_loop, name="xair-return-send", daemon=True)
        self._thread.start()

        def _cb(indata, frames, _time, status) -> None:  # type: ignore[no-untyped-def]
            if status:
                log.debug("return capture status: %s", status)
            try:
                self._q.put_nowait(np.ascontiguousarray(indata, dtype=np.float32).copy())
            except queue.Full:
                try:
                    _ = self._q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._q.put_nowait(np.ascontiguousarray(indata, dtype=np.float32).copy())
                except queue.Full:
                    pass

        self._stream = sd.InputStream(
            device=self._resolve_in_dev(),
            samplerate=self.sample_rate,
            channels=2,
            dtype="float32",
            callback=_cb,
            blocksize=self.frame_samples,
        )
        self._stream.start()
        log.info("Return audio TX → %s:%s stereo PCM24", self.host, self.port)

    def _send_loop(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            if chunk is None:
                break
            if chunk.ndim == 1:
                chunk = chunk.reshape(-1, 1)
            if chunk.shape[1] == 1:
                chunk = np.repeat(chunk, 2, axis=1)
            elif chunk.shape[1] > 2:
                chunk = chunk[:, :2]
            self._pending = np.vstack([self._pending, chunk])
            spp = self.frame_samples
            while self._pending.shape[0] >= spp:
                block = self._pending[:spp]
                self._pending = self._pending[spp:]
                self._emit(block)

    def _emit(self, block: np.ndarray) -> None:
        rms = float(np.sqrt(np.mean(np.square(block.astype(np.float64)))))
        fwd = None
        if self._forward_rms is not None:
            try:
                fwd = self._forward_rms()
            except Exception:
                fwd = None
        allow, _st = self.safety.observe(rms, fwd, time.monotonic())
        if not allow:
            block = np.zeros_like(block)
            self.gated_packets += 1
        payload = pack_pcm24_le(float_to_pcm24(block))
        hdr = build_header(
            codec=Codec.PCM24,
            sample_rate=self.sample_rate,
            channels=2,
            nframes=int(block.shape[0]),
            seq=self._seq,
        )
        self._seq = (self._seq + 1) % (1 << 32)
        self._sock.sendto(hdr + payload, (self.host, self.port))
        self.packets_sent += 1

    def snapshot(self) -> dict:
        st = self.safety.status
        enabled = True
        return {
            "audio_enabled": enabled,
            "return_safety": st,
            "return_health": classify_return_health(enabled=enabled, safety_status=st),
            "packets_sent": self.packets_sent,
            "gated_packets": self.gated_packets,
        }

    def stop(self) -> None:
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                log.debug("return capture stop", exc_info=True)
            self._stream = None
        try:
            self._sock.close()
        except OSError:
            pass


class ReturnAudioReceiver:
    """Server: UDP stereo → X18 USB playback. Play callback pulls PCM only."""

    def __init__(
        self,
        *,
        bind_host: str,
        port: int,
        sample_rate: int = 48000,
        output_device_query: Optional[str] = None,
        frame_samples: int = 480,
    ) -> None:
        if sd is None:
            raise RuntimeError("sounddevice no disponible para return audio")
        self.bind_host = bind_host
        self.port = int(port)
        self.sample_rate = int(sample_rate)
        self.output_device_query = output_device_query
        self.frame_samples = int(max(64, frame_samples))
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._stop = threading.Event()
        self._q: "queue.Queue[np.ndarray]" = queue.Queue(maxsize=32)
        self._thread: Optional[threading.Thread] = None
        self._stream = None
        self.packets_received = 0
        self.decode_errors = 0
        self.underruns = 0

    def _resolve_out_dev(self) -> int:
        """X18 USB playback only. Empty query used to mean default = HDMI."""
        from ..virtual_device.x18_device import require_x18_portaudio_device

        picked = require_x18_portaudio_device(sd, min_output_channels=2)
        log.info("Return USB out: [%s] %s", picked.index, picked.name)
        return int(picked.index)

    def start(self) -> None:
        self._sock.bind((self.bind_host, self.port))
        self._thread = threading.Thread(target=self._recv_loop, name="xair-return-recv", daemon=True)
        self._thread.start()

        def _cb(outdata, frames, _time, status) -> None:  # type: ignore[no-untyped-def]
            need = int(frames)
            acc = np.zeros((0, 2), dtype=np.float32)
            while acc.shape[0] < need:
                try:
                    blk = self._q.get_nowait()
                except queue.Empty:
                    break
                acc = np.vstack([acc, blk])
            out = np.zeros((need, 2), dtype=np.float32)
            if acc.shape[0] >= need:
                out[:] = acc[:need]
                if acc.shape[0] > need:
                    try:
                        self._q.put_nowait(acc[need:])
                    except queue.Full:
                        pass
            else:
                if acc.shape[0]:
                    out[: acc.shape[0]] = acc
                self.underruns += 1
            outdata[:] = out

        self._stream = sd.OutputStream(
            device=self._resolve_out_dev(),
            samplerate=self.sample_rate,
            channels=2,
            dtype="float32",
            callback=_cb,
            blocksize=self.frame_samples,
        )
        self._stream.start()
        log.info("Return audio RX %s:%s → USB stereo", self.bind_host, self.port)

    def _recv_loop(self) -> None:
        self._sock.settimeout(0.25)
        while not self._stop.is_set():
            try:
                data, _addr = self._sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < HEADER_SIZE:
                continue
            try:
                hdr, payload = parse_header(data)
                audio = pcm24_from_bytes(payload, 2)
            except Exception:
                self.decode_errors += 1
                continue
            if audio.ndim != 2:
                continue
            if audio.shape[1] == 1:
                audio = np.repeat(audio, 2, axis=1)
            elif audio.shape[1] > 2:
                audio = audio[:, :2]
            self.packets_received += 1
            try:
                self._q.put_nowait(np.ascontiguousarray(audio, dtype=np.float32))
            except queue.Full:
                try:
                    _ = self._q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._q.put_nowait(np.ascontiguousarray(audio, dtype=np.float32))
                except queue.Full:
                    pass

    def snapshot(self) -> dict:
        health = classify_return_health(
            enabled=True,
            safety_status="ok" if self.decode_errors == 0 else "gated",
            decode_errors=self.decode_errors,
        )
        return {
            "audio_enabled": True,
            "return_health": health,
            "return_safety": "ok" if self.decode_errors == 0 else "gated",
            "packets_received": self.packets_received,
            "decode_errors": self.decode_errors,
            "underruns": self.underruns,
        }

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                log.debug("return play stop", exc_info=True)
            self._stream = None
        try:
            self._sock.close()
        except OSError:
            pass
