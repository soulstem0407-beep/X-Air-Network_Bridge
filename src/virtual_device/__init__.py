"""Dispositivo virtual del sistema (guías y soporte ALSA loopback)."""

from .bridge_device import (
    OSFamily,
    VirtualDeviceHint,
    detect_os,
    hints_for_this_os,
    linux_enable_loopback,
    linux_loopback_loaded,
    suggest_output_query,
)

__all__ = [
    "OSFamily",
    "VirtualDeviceHint",
    "detect_os",
    "hints_for_this_os",
    "linux_enable_loopback",
    "linux_loopback_loaded",
    "suggest_output_query",
]
