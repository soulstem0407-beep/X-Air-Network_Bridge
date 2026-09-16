"""Adaptive jitter-buffer policy and RX health classification.

Pure functions plus a small config object. The audio/JACK callback must not
call these (they allocate / branch on env). The UDP receive thread runs the
policy on a ~0.5 s cadence.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from typing import Tuple

DEFAULT_FLOOR = 5
DEFAULT_CEILING = 16
DEFAULT_INTERVAL_S = 0.5
DEFAULT_COVER_FACTOR = 3.0
DEFAULT_SHRINK_PERIODS = 4  # ~2 s at 0.5 s
DEFAULT_WINDOW_S = 2.0
MIN_PACKETS = 2
MAX_PACKETS = 64


def _clamp_packets(n: int) -> int:
    return max(MIN_PACKETS, min(MAX_PACKETS, int(n)))


@dataclass(frozen=True)
class AdaptiveJitterConfig:
    """Receiver playout depth. ``floor`` is XAIR_JITTER_PACKETS (manual override)."""

    enabled: bool = True
    floor: int = DEFAULT_FLOOR
    ceiling: int = DEFAULT_CEILING
    interval_s: float = DEFAULT_INTERVAL_S
    cover_factor: float = DEFAULT_COVER_FACTOR
    shrink_idle_periods: int = DEFAULT_SHRINK_PERIODS

    @classmethod
    def from_env(cls, jitter_packets: int) -> AdaptiveJitterConfig:
        floor = _clamp_packets(jitter_packets)
        enabled = _env_bool("XAIR_JITTER_ADAPTIVE", True)
        raw_min = os.getenv("XAIR_JITTER_MIN")
        if raw_min is not None and str(raw_min).strip():
            floor = _clamp_packets(int(raw_min))
            # Manual set-buffer / XAIR_JITTER_PACKETS still raises the floor.
            floor = max(floor, _clamp_packets(jitter_packets))
        raw_max = os.getenv("XAIR_JITTER_MAX")
        ceiling = _clamp_packets(int(raw_max)) if raw_max and str(raw_max).strip() else DEFAULT_CEILING
        if ceiling < floor:
            ceiling = floor
        if not enabled:
            ceiling = floor
        factor = _env_float("XAIR_JITTER_COVER", DEFAULT_COVER_FACTOR)
        interval = _env_float("XAIR_JITTER_ADAPT_SEC", DEFAULT_INTERVAL_S)
        return cls(
            enabled=enabled,
            floor=floor,
            ceiling=max(floor, ceiling),
            interval_s=max(0.15, min(5.0, interval)),
            cover_factor=max(1.0, min(8.0, factor)),
            shrink_idle_periods=DEFAULT_SHRINK_PERIODS,
        )


def packet_duration_ms(samples_per_packet: int, sample_rate: int) -> float:
    sr = max(1, int(sample_rate))
    return 1000.0 * float(max(1, int(samples_per_packet))) / float(sr)


def desired_packets(
    jitter_ms: float,
    packet_ms: float,
    *,
    cover_factor: float = DEFAULT_COVER_FACTOR,
    floor: int = DEFAULT_FLOOR,
    ceiling: int = DEFAULT_CEILING,
) -> int:
    """How many packets cover ``cover_factor × jitter_ema`` of delay."""
    floor_i = _clamp_packets(floor)
    ceiling_i = max(floor_i, _clamp_packets(ceiling))
    if packet_ms <= 1e-9:
        return floor_i
    need = int(math.ceil(max(0.0, float(jitter_ms)) * float(cover_factor) / packet_ms))
    return max(floor_i, min(ceiling_i, need))


def step_target(
    current: int,
    desired: int,
    *,
    underrun: bool,
    stream_alive: bool,
    quiet_streak: int,
    shrink_after: int = DEFAULT_SHRINK_PERIODS,
    floor: int = DEFAULT_FLOOR,
    ceiling: int = DEFAULT_CEILING,
) -> int:
    """Move at most one packet per decision. Grow on underrun; shrink only after quiet periods."""
    floor_i = _clamp_packets(floor)
    ceiling_i = max(floor_i, _clamp_packets(ceiling))
    cur = max(floor_i, min(ceiling_i, int(current)))
    des = max(floor_i, min(ceiling_i, int(desired)))
    if not stream_alive:
        return cur
    if underrun:
        return min(ceiling_i, max(cur + 1, des))
    if des > cur:
        return cur + 1
    if des < cur and quiet_streak >= max(1, int(shrink_after)):
        return cur - 1
    return cur


def classify_rx_health(
    *,
    loss_window_ppm: float,
    jitter_ms: float,
    underruns_window: int,
    fill_percent: float,
    stream_alive: bool,
) -> Tuple[str, str]:
    """Return ``(ok|warn|fail, reason)``. Thresholds are for LAN PCM, not WAN."""
    if not stream_alive:
        return "warn", "idle"
    if int(underruns_window) > 0:
        return "fail", "underrun"
    if float(loss_window_ppm) >= 10_000.0:
        return "fail", "loss"
    if float(fill_percent) < 25.0:
        return "warn", "starved"
    if float(loss_window_ppm) >= 1_000.0:
        return "warn", "loss"
    if float(jitter_ms) >= 8.0:
        return "warn", "jitter"
    if float(jitter_ms) >= 3.0:
        return "warn", "jitter"
    return "ok", "ok"


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        return default
    try:
        return float(v)
    except ValueError:
        return default
