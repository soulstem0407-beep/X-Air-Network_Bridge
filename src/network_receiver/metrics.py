"""RX counters, EMA jitter, and health snapshot.

Kept separate from ``receiver.py`` so tests do not import PortAudio/JACK.
The audio callback only bumps counters under a short lock.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from .adaptive_jitter import DEFAULT_WINDOW_S, classify_rx_health


def classify_fec_health(
    *,
    enabled: bool,
    unrecoverable_window: int,
    unrecoverable_ppm: float,
) -> str:
    """Dashboard color key: ``off`` / ``ok`` / ``warn`` / ``fail``. Does not change jitter policy."""
    if not enabled:
        return "off"
    if int(unrecoverable_window) <= 0:
        return "ok"
    if float(unrecoverable_ppm) >= 10_000.0:
        return "fail"
    return "warn"


def classify_opus_health(
    *,
    enabled: bool,
    decode_errors_window: int,
    decode_error_ppm: float,
) -> str:
    """Dashboard color key: ``off`` / ``ok`` / ``warn`` / ``fail``. Does not change jitter/FEC."""
    if not enabled:
        return "off"
    if int(decode_errors_window) <= 0:
        return "ok"
    if float(decode_error_ppm) >= 10_000.0:
        return "fail"
    return "warn"


class ReceiverMetrics:
    """Contadores y EMA. El callback de audio sólo incrementa contadores bajo lock corto."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.packets_received = 0
        self.packets_lost = 0
        self.packets_late_drop = 0
        self.jitter_ms_ema = 0.0
        self.jitter_ms_peak = 0.0
        self.buffer_fill_ema = 0.0
        self.underruns = 0
        self.adapt_holds = 0
        self.adapt_drops = 0
        self.adapt_steps = 0
        self.queue_packets = 0
        self.queue_capacity = 0
        self.jitter_packets_target = 0
        self.jitter_packets_min = 0
        self.jitter_packets_max = 0
        self.jitter_adaptive = False
        self._last_arrival_mono: Optional[float] = None
        self._win_t0 = time.monotonic()
        self._win_rx0 = 0
        self._win_lost0 = 0
        self._win_underrun0 = 0
        self.rx_packets_per_sec = 0.0
        self.loss_window_ppm = 0.0
        self.underruns_window = 0
        self.rx_health = "ok"
        self.rx_health_reason = "ok"
        self.fec_enabled = False
        self.fec_group = 0
        self.fec_recovered = 0
        self.fec_unrecoverable = 0
        self.fec_groups_closed = 0
        self.fec_recovered_ppm = 0.0
        self.fec_unrecoverable_ppm = 0.0
        self.fec_health = "off"
        self._win_fec_rec0 = 0
        self._win_fec_unrec0 = 0
        self._win_fec_grp0 = 0
        self.fec_unrecoverable_window = 0
        self.opus_enabled = False
        self.opus_bitrate = 0
        self.opus_frame_ms = 0.0
        self.opus_decode_errors = 0
        self.opus_decode_errors_window = 0
        self.opus_decode_error_ppm = 0.0
        self.opus_health = "off"
        self._win_opus_err0 = 0

    def on_arrival_nominal_timing(self, nframes: int, sample_rate: int, mono_time: float) -> None:
        exp = float(nframes) / float(sample_rate)
        with self._lock:
            self.packets_received += 1
            if self._last_arrival_mono is not None:
                inter = mono_time - self._last_arrival_mono
                delta_ms = abs(inter - exp) * 1000.0
                self.jitter_ms_ema = self.jitter_ms_ema * 0.94 + delta_ms * 0.06
                self.jitter_ms_peak = max(self.jitter_ms_peak * 0.99, delta_ms)
            self._last_arrival_mono = mono_time

    def add_lost(self, nseq: int) -> None:
        with self._lock:
            self.packets_lost += int(nseq)

    def late_drop(self) -> None:
        with self._lock:
            self.packets_late_drop += 1

    def note_underrun(self) -> None:
        with self._lock:
            self.underruns += 1

    def note_adapt_hold(self) -> None:
        with self._lock:
            self.adapt_holds += 1

    def note_adapt_drop(self) -> None:
        with self._lock:
            self.adapt_drops += 1

    def note_adapt_step(self) -> None:
        with self._lock:
            self.adapt_steps += 1

    def set_opus_control(self, enabled: bool, bitrate: int, frame_ms: float) -> None:
        with self._lock:
            self.opus_enabled = bool(enabled)
            self.opus_bitrate = int(bitrate)
            self.opus_frame_ms = float(frame_ms)

    def note_opus_decode_error(self) -> None:
        with self._lock:
            self.opus_decode_errors += 1

    def set_fec_control(self, enabled: bool, group: int) -> None:
        with self._lock:
            self.fec_enabled = bool(enabled)
            self.fec_group = int(group)

    def set_fec_counts(self, recovered: int, unrecoverable: int, groups_closed: int) -> None:
        with self._lock:
            self.fec_recovered = int(recovered)
            self.fec_unrecoverable = int(unrecoverable)
            self.fec_groups_closed = int(groups_closed)

    def set_jitter_control(
        self, target: int, floor: int, ceiling: int, adaptive: bool
    ) -> None:
        with self._lock:
            self.jitter_packets_target = int(target)
            self.jitter_packets_min = int(floor)
            self.jitter_packets_max = int(ceiling)
            self.jitter_adaptive = bool(adaptive)

    def update_buffer_fill(self, qsize: int, qmax: int) -> None:
        """Legacy fill vs queue capacity. Prefer ``note_queue`` (fill vs target)."""
        self.note_queue(qsize, qmax, qmax)

    def note_queue(self, qsize: int, capacity: int, target: int) -> None:
        denom = max(1, int(target))
        ratio = float(qsize) / float(denom)
        with self._lock:
            self.queue_packets = int(qsize)
            self.queue_capacity = int(capacity)
            self.jitter_packets_target = int(target)
            self.buffer_fill_ema = self.buffer_fill_ema * 0.9 + ratio * 100.0 * 0.1

    def refresh_window(self, now: Optional[float] = None) -> None:
        t = time.monotonic() if now is None else float(now)
        with self._lock:
            elapsed = t - self._win_t0
            drx = self.packets_received - self._win_rx0
            dlost = self.packets_lost - self._win_lost0
            dund = self.underruns - self._win_underrun0
            drec = self.fec_recovered - self._win_fec_rec0
            dunrec = self.fec_unrecoverable - self._win_fec_unrec0
            dgrp = self.fec_groups_closed - self._win_fec_grp0
            dopus = self.opus_decode_errors - self._win_opus_err0
            use_elapsed = max(elapsed, 1e-3)
            self.rx_packets_per_sec = float(drx) / use_elapsed
            tot = drx + dlost
            self.loss_window_ppm = (float(dlost) / float(tot) * 1e6) if tot else 0.0
            self.underruns_window = int(dund)
            self.fec_unrecoverable_window = int(dunrec)
            self.fec_recovered_ppm = (float(drec) / float(drx) * 1e6) if drx else 0.0
            self.fec_unrecoverable_ppm = (float(dunrec) / float(dgrp) * 1e6) if dgrp else 0.0
            self.opus_decode_errors_window = int(dopus)
            self.opus_decode_error_ppm = (float(dopus) / float(drx) * 1e6) if drx else 0.0
            if elapsed >= DEFAULT_WINDOW_S:
                self._win_t0 = t
                self._win_rx0 = self.packets_received
                self._win_lost0 = self.packets_lost
                self._win_underrun0 = self.underruns
                self._win_fec_rec0 = self.fec_recovered
                self._win_fec_unrec0 = self.fec_unrecoverable
                self._win_fec_grp0 = self.fec_groups_closed
                self._win_opus_err0 = self.opus_decode_errors

    def snapshot(self) -> dict:
        with self._lock:
            tot = self.packets_received + self.packets_lost
            loss_ppm = (self.packets_lost / tot * 1e6) if tot else 0.0
            if self.packets_received == 0:
                health, reason = "ok", "ok"
            else:
                health, reason = classify_rx_health(
                    loss_window_ppm=self.loss_window_ppm,
                    jitter_ms=self.jitter_ms_ema,
                    underruns_window=self.underruns_window,
                    fill_percent=self.buffer_fill_ema,
                    stream_alive=self.rx_packets_per_sec > 0.5,
                )
            self.rx_health = health
            self.rx_health_reason = reason
            self.fec_health = classify_fec_health(
                enabled=self.fec_enabled,
                unrecoverable_window=self.fec_unrecoverable_window,
                unrecoverable_ppm=self.fec_unrecoverable_ppm,
            )
            self.opus_health = classify_opus_health(
                enabled=self.opus_enabled,
                decode_errors_window=self.opus_decode_errors_window,
                decode_error_ppm=self.opus_decode_error_ppm,
            )
            return {
                "packets_received": self.packets_received,
                "packets_lost": self.packets_lost,
                "packets_late_drop": self.packets_late_drop,
                "jitter_ms_ema": round(self.jitter_ms_ema, 3),
                "jitter_ms_peak": round(self.jitter_ms_peak, 3),
                "buffer_fill_percent_ema": round(self.buffer_fill_ema, 2),
                "underruns": self.underruns,
                "underruns_window": self.underruns_window,
                "adapt_holds": self.adapt_holds,
                "adapt_drops": self.adapt_drops,
                "adapt_steps": self.adapt_steps,
                "queue_packets": self.queue_packets,
                "queue_capacity": self.queue_capacity,
                "jitter_packets_target": self.jitter_packets_target,
                "jitter_packets_min": self.jitter_packets_min,
                "jitter_packets_max": self.jitter_packets_max,
                "jitter_adaptive": self.jitter_adaptive,
                "rx_packets_per_sec": round(self.rx_packets_per_sec, 1),
                "loss_ppm": round(loss_ppm, 1),
                "loss_window_ppm": round(self.loss_window_ppm, 1),
                "rx_health": self.rx_health,
                "rx_health_reason": self.rx_health_reason,
                "fec_enabled": self.fec_enabled,
                "fec_group": self.fec_group,
                "fec_recovered": self.fec_recovered,
                "fec_unrecoverable": self.fec_unrecoverable,
                "fec_recovered_ppm": round(self.fec_recovered_ppm, 1),
                "fec_unrecoverable_ppm": round(self.fec_unrecoverable_ppm, 1),
                "fec_health": self.fec_health,
                "opus_enabled": self.opus_enabled,
                "opus_bitrate": self.opus_bitrate,
                "opus_frame": self.opus_frame_ms,
                "opus_decode_errors": self.opus_decode_errors,
                "opus_health": self.opus_health,
            }
