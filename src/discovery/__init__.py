"""LAN discovery for X AIR mixers and bridge peers (control plane only)."""

from .packets import (
    DiscoveredDevice,
    classify_discovery_health,
    empty_snapshot,
    wants_auto_host,
)
from .service import DiscoveryService, default_discovery_path

__all__ = [
    "DiscoveredDevice",
    "DiscoveryService",
    "classify_discovery_health",
    "default_discovery_path",
    "empty_snapshot",
    "wants_auto_host",
]
