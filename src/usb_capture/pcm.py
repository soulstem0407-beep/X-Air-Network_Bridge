"""PCM pack/unpack used by UDP transport. No PortAudio/JACK."""
from __future__ import annotations

import numpy as np


def float_to_pcm16(samples: np.ndarray) -> np.ndarray:
    clipped = np.clip(samples, -1.0, 1.0)
    return np.rint(clipped * 32767.0).astype(np.int16)


def float_to_pcm24(samples: np.ndarray) -> np.ndarray:
    clipped = np.clip(samples, -1.0, 1.0)
    return np.rint(clipped * 8388607.0).astype(np.int32)


def pack_pcm24_le(pcm24: np.ndarray) -> bytes:
    if pcm24.dtype != np.int32:
        pcm24 = pcm24.astype(np.int32)
    pcm24_u = pcm24.astype(np.uint32) & 0xFFFFFF
    b0 = (pcm24_u & 0xFF).astype(np.uint8)
    b1 = ((pcm24_u >> 8) & 0xFF).astype(np.uint8)
    b2 = ((pcm24_u >> 16) & 0xFF).astype(np.uint8)
    stack = np.stack([b0, b1, b2], axis=-1)
    return stack.reshape(-1).tobytes()


def pcm16_packed(pcm16: np.ndarray) -> bytes:
    return np.ascontiguousarray(pcm16, dtype="<i2").tobytes()


def pcm24_from_bytes(blob: bytes, channels: int) -> np.ndarray:
    raw = np.frombuffer(blob, dtype=np.uint8)
    n_frames = len(blob) // (channels * 3)
    v = raw[: n_frames * channels * 3].reshape(-1, 3)
    s = (
        v[:, 0].astype(np.int32)
        | (v[:, 1].astype(np.int32) << 8)
        | (v[:, 2].astype(np.int32) << 16)
    )
    mask = (s & 0x800000).astype(np.int32)
    s_signed = np.where(mask != 0, s - (1 << 24), s).astype(np.float32)
    return np.clip(s_signed / 8388607.0, -1.0, 1.0).reshape(n_frames, channels)


def pcm16_from_bytes(blob: bytes, channels: int) -> np.ndarray:
    arr = np.frombuffer(blob, dtype="<i2").reshape(-1, channels).astype(np.float32)
    return np.clip(arr / 32767.0, -1.0, 1.0)
