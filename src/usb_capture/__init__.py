"""Captura USB/PortAudio para X‑AIR."""

from .pcm import (
    float_to_pcm16,
    float_to_pcm24,
    pack_pcm24_le,
    pcm16_from_bytes,
    pcm16_packed,
    pcm24_from_bytes,
)

__all__ = [
    "CaptureConfig",
    "USBCapture",
    "float_to_pcm16",
    "float_to_pcm24",
    "pack_pcm24_le",
    "pcm16_from_bytes",
    "pcm16_packed",
    "pcm24_from_bytes",
]


def __getattr__(name: str):
    if name in ("CaptureConfig", "USBCapture"):
        from .capture import CaptureConfig, USBCapture

        return CaptureConfig if name == "CaptureConfig" else USBCapture
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
