"""Multichannel Opus for XBRI: encode/decode off the JACK/PipeWire callback.

Each stereo pair (plus a leftover mono channel) is an independent libopus
stream packed into one datagram. The capture/play callbacks never call this.
"""
from __future__ import annotations

import ctypes
import ctypes.util
from typing import List, Sequence

import numpy as np

OPUS_APPLICATION_AUDIO = 2049
OPUS_SET_BITRATE_REQUEST = 4002
OPUS_OK = 0

DEFAULT_OPUS_BITRATE = 128_000
DEFAULT_OPUS_FRAME_MS = 20.0
OPUS_FRAME_MS_ALLOWED = (2.5, 5.0, 10.0, 20.0, 40.0, 60.0)
MIN_OPUS_BITRATE = 6_000
MAX_OPUS_BITRATE = 512_000
MAX_PACKET = 4000


def clamp_opus_bitrate(bps: int) -> int:
    try:
        v = int(bps)
    except (TypeError, ValueError):
        return DEFAULT_OPUS_BITRATE
    return max(MIN_OPUS_BITRATE, min(MAX_OPUS_BITRATE, v))


def clamp_opus_frame_ms(ms: float) -> float:
    try:
        v = float(ms)
    except (TypeError, ValueError):
        return DEFAULT_OPUS_FRAME_MS
    best = min(OPUS_FRAME_MS_ALLOWED, key=lambda a: abs(a - v))
    return float(best)


def opus_frame_samples(sample_rate: int, frame_ms: float) -> int:
    ms = clamp_opus_frame_ms(frame_ms)
    n = int(round(float(sample_rate) * ms / 1000.0))
    return max(120, n)


def pack_opus_streams(chunks: Sequence[bytes]) -> bytes:
    n = len(chunks)
    if n > 255:
        raise ValueError("demasiados streams Opus")
    parts = [bytes([n & 0xFF])]
    for c in chunks:
        raw = bytes(c)
        if len(raw) > 0xFFFF:
            raise ValueError("stream Opus demasiado largo")
        parts.append(len(raw).to_bytes(2, "big"))
        parts.append(raw)
    return b"".join(parts)


def unpack_opus_streams(buf: bytes) -> List[bytes]:
    if not buf:
        raise ValueError("payload Opus vacío")
    n = buf[0]
    off = 1
    out: List[bytes] = []
    for _ in range(n):
        if off + 2 > len(buf):
            raise ValueError("payload Opus truncado")
        ln = int.from_bytes(buf[off : off + 2], "big")
        off += 2
        if off + ln > len(buf):
            raise ValueError("payload Opus truncado")
        out.append(buf[off : off + ln])
        off += ln
    return out


def _load_libopus() -> ctypes.CDLL:
    name = ctypes.util.find_library("opus") or "libopus.so.0"
    return ctypes.CDLL(name)


def libopus_available() -> bool:
    try:
        _load_libopus()
        return True
    except OSError:
        return False


def _stream_layout(channels: int) -> List[int]:
    """Channel counts per encoder (2, 2, …, optional 1)."""
    ch = int(channels)
    layout: List[int] = []
    while ch >= 2:
        layout.append(2)
        ch -= 2
    if ch == 1:
        layout.append(1)
    if not layout:
        raise ValueError("Opus requiere al menos 1 canal")
    return layout


class OpusBundle:
    """One Opus encoder/decoder set for interleaved float32 (frames, channels)."""

    def __init__(
        self,
        *,
        sample_rate: int,
        channels: int,
        bitrate: int,
        frame_ms: float,
        decode: bool = False,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.bitrate = clamp_opus_bitrate(bitrate)
        self.frame_ms = clamp_opus_frame_ms(frame_ms)
        self.frame_samples = opus_frame_samples(self.sample_rate, self.frame_ms)
        self._layout = _stream_layout(self.channels)
        self._lib = _load_libopus()
        self._encoders: List[ctypes.c_void_p] = []
        self._decoders: List[ctypes.c_void_p] = []
        err = ctypes.c_int()
        create_enc = self._lib.opus_encoder_create
        create_enc.restype = ctypes.c_void_p
        create_enc.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        create_dec = self._lib.opus_decoder_create
        create_dec.restype = ctypes.c_void_p
        create_dec.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        encode_fn = self._lib.opus_encode_float
        encode_fn.restype = ctypes.c_int
        encode_fn.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int,
        ]
        decode_fn = self._lib.opus_decode_float
        decode_fn.restype = ctypes.c_int
        decode_fn.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.c_int,
        ]
        destroy_e = self._lib.opus_encoder_destroy
        destroy_e.argtypes = [ctypes.c_void_p]
        destroy_d = self._lib.opus_decoder_destroy
        destroy_d.argtypes = [ctypes.c_void_p]
        ctl = self._lib.opus_encoder_ctl
        ctl.restype = ctypes.c_int
        n_streams = len(self._layout)
        per = max(MIN_OPUS_BITRATE, self.bitrate // n_streams)
        if decode:
            for nch in self._layout:
                st = create_dec(self.sample_rate, nch, ctypes.byref(err))
                if not st or err.value != OPUS_OK:
                    self.close()
                    raise RuntimeError(f"opus_decoder_create: {err.value}")
                self._decoders.append(ctypes.c_void_p(st))
        else:
            for nch in self._layout:
                st = create_enc(
                    self.sample_rate, nch, OPUS_APPLICATION_AUDIO, ctypes.byref(err)
                )
                if not st or err.value != OPUS_OK:
                    self.close()
                    raise RuntimeError(f"opus_encoder_create: {err.value}")
                ptr = ctypes.c_void_p(st)
                rc = int(ctl(ptr, ctypes.c_int(OPUS_SET_BITRATE_REQUEST), ctypes.c_int(per)))
                if rc != OPUS_OK:
                    self.close()
                    raise RuntimeError(f"OPUS_SET_BITRATE: {rc}")
                self._encoders.append(ptr)

    def encode(self, pcm: np.ndarray) -> bytes:
        x = np.ascontiguousarray(pcm, dtype=np.float32)
        if x.ndim != 2 or x.shape[1] != self.channels:
            raise ValueError("PCM Opus debe ser (frames, channels)")
        n = int(x.shape[0])
        if n != self.frame_samples:
            raise ValueError(
                f"frame Opus {n} ≠ {self.frame_samples} (XAIR_OPUS_FRAME={self.frame_ms} ms)"
            )
        encode_fn = self._lib.opus_encode_float
        chunks: List[bytes] = []
        col = 0
        buf = (ctypes.c_ubyte * MAX_PACKET)()
        for enc, nch in zip(self._encoders, self._layout):
            sl = np.ascontiguousarray(x[:, col : col + nch])
            col += nch
            pcm_ptr = sl.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            nbytes = int(
                encode_fn(enc, pcm_ptr, n, ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)), MAX_PACKET)
            )
            if nbytes < 0:
                raise RuntimeError(f"opus_encode_float: {nbytes}")
            chunks.append(bytes(buf[:nbytes]))
        return pack_opus_streams(chunks)

    def decode(self, payload: bytes, nframes: int) -> np.ndarray:
        streams = unpack_opus_streams(payload)
        if len(streams) != len(self._decoders):
            raise ValueError(
                f"streams Opus {len(streams)} ≠ {len(self._decoders)} (canales={self.channels})"
            )
        decode_fn = self._lib.opus_decode_float
        cols: List[np.ndarray] = []
        want = int(nframes) if nframes > 0 else self.frame_samples
        pcm_buf = (ctypes.c_float * (want * 2))()
        for dec, nch, chunk in zip(self._decoders, self._layout, streams):
            data = (ctypes.c_ubyte * len(chunk)).from_buffer_copy(chunk)
            got = int(
                decode_fn(
                    dec,
                    ctypes.cast(data, ctypes.POINTER(ctypes.c_ubyte)),
                    len(chunk),
                    ctypes.cast(pcm_buf, ctypes.POINTER(ctypes.c_float)),
                    want,
                    0,
                )
            )
            if got < 0:
                raise RuntimeError(f"opus_decode_float: {got}")
            arr = np.ctypeslib.as_array(pcm_buf)[: got * nch].copy()
            cols.append(arr.reshape(got, nch))
        rows = min(c.shape[0] for c in cols)
        out = np.concatenate([c[:rows] for c in cols], axis=1)
        if out.shape[1] != self.channels:
            raise ValueError("decode Opus: canales inesperados")
        return np.clip(np.ascontiguousarray(out, dtype=np.float32), -1.0, 1.0)

    def close(self) -> None:
        destroy_e = getattr(self._lib, "opus_encoder_destroy", None) if hasattr(self, "_lib") else None
        destroy_d = getattr(self._lib, "opus_decoder_destroy", None) if hasattr(self, "_lib") else None
        for st in self._encoders:
            if destroy_e and st:
                destroy_e(st)
        for st in self._decoders:
            if destroy_d and st:
                destroy_d(st)
        self._encoders.clear()
        self._decoders.clear()

    def __enter__(self) -> "OpusBundle":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
