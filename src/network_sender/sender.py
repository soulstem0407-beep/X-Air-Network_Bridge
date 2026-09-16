"""
Servidor de red: fragmenta audio capturado, codifica y envía UDP.
"""
from __future__ import annotations

import io
import logging
import queue
import socket
import threading
from typing import Optional

import numpy as np

from .fec import FecEncoder
from .jitter_send import SendJitterBuffer
from .opus_codec import (
    DEFAULT_OPUS_BITRATE,
    DEFAULT_OPUS_FRAME_MS,
    OpusBundle,
    clamp_opus_bitrate,
    clamp_opus_frame_ms,
)
from .protocol import FLAG_OPUS, HEADER_SIZE, Codec, build_header, inspect_packet_mtu

log = logging.getLogger(__name__)

try:
    import soundfile as sf
except ImportError:
    sf = None


def _encode_flac(pcm16: np.ndarray, sample_rate: int) -> bytes:
    if sf is None:
        raise RuntimeError("FLAC requiere soundfile (libsndfile). pip install soundfile")
    buf = io.BytesIO()
    sf.write(buf, pcm16, sample_rate, format="FLAC", subtype="PCM_16")
    return buf.getvalue()


class NetworkSender:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        sample_rate: int,
        channels: int,
        codec: str,
        samples_per_packet: int,
        send_jitter_depth: int = 1,
        fec_enabled: bool = False,
        fec_group: int = 3,
        opus_enabled: bool = False,
        opus_bitrate: int = DEFAULT_OPUS_BITRATE,
        opus_frame_ms: float = DEFAULT_OPUS_FRAME_MS,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.sample_rate = sample_rate
        self.channels = channels
        self.samples_per_packet = int(samples_per_packet)
        c = codec.strip().upper()
        if c == "PCM16":
            self._codec = Codec.PCM16
        elif c == "PCM24":
            self._codec = Codec.PCM24
        elif c == "FLAC":
            self._codec = Codec.FLAC
        else:
            raise ValueError("XAIR_CODEC debe ser PCM16, PCM24 o FLAC")
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._seq = 0
        self._lock = threading.Lock()
        self._pending = np.empty((0, channels), dtype=np.float32)
        self._jbuf = SendJitterBuffer(max_packets=max(1, send_jitter_depth))
        self.fec_enabled = bool(fec_enabled)
        self._fec: Optional[FecEncoder] = FecEncoder(fec_group) if self.fec_enabled else None
        if self._fec is not None:
            log.info("FEC XOR activo (grupo=%s, +1 datagrama de paridad por grupo)", self._fec.group)

        self.opus_enabled = bool(opus_enabled)
        self.opus_bitrate = clamp_opus_bitrate(opus_bitrate)
        self.opus_frame_ms = clamp_opus_frame_ms(opus_frame_ms)
        self._opus: Optional[OpusBundle] = None
        self._send_q: Optional["queue.Queue[Optional[np.ndarray]]"] = None
        self._send_stop: Optional[threading.Event] = None
        self._send_thread: Optional[threading.Thread] = None
        if self.opus_enabled:
            self._opus = OpusBundle(
                sample_rate=self.sample_rate,
                channels=self.channels,
                bitrate=self.opus_bitrate,
                frame_ms=self.opus_frame_ms,
                decode=False,
            )
            self.samples_per_packet = self._opus.frame_samples
            self._send_q = queue.Queue(maxsize=8)
            self._send_stop = threading.Event()
            self._send_thread = threading.Thread(
                target=self._send_loop, name="xair-udp-send", daemon=True
            )
            self._send_thread.start()
            log.info(
                "Opus activo (bitrate=%s total, frame=%.1f ms / %s muestras, hilo UDP send)",
                self.opus_bitrate,
                self.opus_frame_ms,
                self.samples_per_packet,
            )
        else:
            mtu_hint = HEADER_SIZE + max(
                inspect_packet_mtu(self.samples_per_packet, channels, Codec.PCM24),
                256,
            )
            if mtu_hint > 1400 and self._codec != Codec.FLAC:
                log.warning(
                    "Paquete ≈ %s bytes; si hay fragmentación IP, baja XAIR_SAMPLES_PER_PACKET.",
                    mtu_hint,
                )

    def close(self) -> None:
        if self._send_stop is not None:
            self._send_stop.set()
        if self._send_q is not None:
            try:
                self._send_q.put_nowait(None)
            except queue.Full:
                pass
        if self._send_thread is not None and self._send_thread.is_alive():
            self._send_thread.join(timeout=1.5)
        if self._opus is not None:
            self._opus.close()
            self._opus = None
        self._sock.close()

    def set_peer_host(self, host: str) -> None:
        """Unicast audio destination. Control thread only; not the capture callback."""
        name = str(host or "").strip()
        if not name:
            return
        self.host = name

    def _encode_payload(self, float_block: np.ndarray) -> bytes:
        from ..usb_capture.pcm import (
            float_to_pcm16,
            float_to_pcm24,
            pack_pcm24_le,
            pcm16_packed,
        )

        if self._codec == Codec.PCM16:
            return pcm16_packed(float_to_pcm16(float_block))
        if self._codec == Codec.PCM24:
            return pack_pcm24_le(float_to_pcm24(float_block))
        return _encode_flac(float_to_pcm16(float_block), self.sample_rate)

    def _emit_media(self, nframes: int, payload: bytes, *, flags: int = 0) -> None:
        extra = self._fec.position_for_next() if self._fec is not None else 0
        seq = self._seq
        hdr = build_header(
            codec=self._codec,
            sample_rate=self.sample_rate,
            channels=self.channels,
            nframes=nframes,
            seq=seq,
            flags=int(flags),
            extra=extra,
        )
        self._seq = (self._seq + 1) % (1 << 32)
        self._jbuf.push(hdr + payload)
        if self._fec is not None:
            fec_pkt = self._fec.add_media(
                codec=self._codec,
                sample_rate=self.sample_rate,
                channels=self.channels,
                nframes=nframes,
                seq=seq,
                payload=payload,
            )
            if fec_pkt is not None:
                self._jbuf.push(fec_pkt)

        for pkt in self._jbuf.pop_all():
            self._sock.sendto(pkt, (self.host, self.port))

    def _flush_one(self, block: np.ndarray) -> None:
        nframes = int(block.shape[0])
        payload = self._encode_payload(block)
        self._emit_media(nframes, payload, flags=0)

    def _flush_opus(self, block: np.ndarray) -> None:
        """Encode off the capture callback; only seq/FEC/socket take ``_lock``."""
        assert self._opus is not None
        nframes = int(block.shape[0])
        payload = self._opus.encode(block)
        with self._lock:
            self._emit_media(nframes, payload, flags=FLAG_OPUS)

    def _send_loop(self) -> None:
        """Opus encode + UDP send. Never the capture or JACK callback."""
        q = self._send_q
        stop = self._send_stop
        if q is None or stop is None:
            return
        while not stop.is_set():
            try:
                block = q.get(timeout=0.1)
            except queue.Empty:
                continue
            if block is None:
                break
            try:
                self._flush_opus(block)
            except Exception:
                log.debug("Opus encode/send", exc_info=True)

    def feed(self, chunk: np.ndarray) -> None:
        """Desde callback de captura: solo copia y encola; Opus vive en el hilo UDP."""
        x = np.ascontiguousarray(chunk, dtype=np.float32)
        if x.ndim != 2 or x.shape[1] != self.channels:
            raise ValueError("chunk debe tener forma (frames, channels)")
        ready: list[np.ndarray] = []
        with self._lock:
            self._pending = np.vstack([self._pending, x])
            spp = self.samples_per_packet
            while self._pending.shape[0] >= spp:
                block = self._pending[:spp].copy()
                self._pending = self._pending[spp:]
                if self._send_q is None:
                    self._flush_one(block)
                else:
                    ready.append(block)
        q = self._send_q
        if q is None:
            return
        for block in ready:
            try:
                q.put_nowait(block)
            except queue.Full:
                try:
                    _ = q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(block)
                except queue.Full:
                    pass
