"""Recepción UDP y reproducción multicanal."""

from .adaptive_jitter import AdaptiveJitterConfig
from .metrics import ReceiverMetrics

# NetworkReceiver stays in receiver.py (PortAudio/JACK). Import it from
# ``src.network_receiver.receiver`` so tests of metrics/policy do not load drivers.

__all__ = ["AdaptiveJitterConfig", "NetworkReceiver", "ReceiverMetrics"]


def __getattr__(name: str):
    if name == "NetworkReceiver":
        from .receiver import NetworkReceiver

        return NetworkReceiver
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
