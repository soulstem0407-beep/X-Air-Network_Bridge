"""Feedback latch for the stereo DAW → mixer return. Pure; no audio I/O."""
from __future__ import annotations

import math
from typing import Optional, Tuple


def _rms_to_dbfs(rms: float) -> float:
    return 20.0 * math.log10(max(float(rms), 1e-12))


def classify_return_health(
    *,
    enabled: bool,
    safety_status: str,
    decode_errors: int = 0,
) -> str:
    """``off`` / ``ok`` / ``warn`` / ``fail`` for the dashboard."""
    if not enabled:
        return "off"
    if safety_status == "tripped":
        return "fail"
    if safety_status == "gated" or int(decode_errors) > 0:
        return "warn"
    return "ok"


class ReturnSafety:
    """Mute the return if capture and the forward mix are both hot (loop risk).

    Observe from the return send thread, never from a JACK/PipeWire callback.
    Once tripped, stays muted until RMS falls and ``cooldown_s`` elapses.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        trip_dbfs: float = -6.0,
        warn_dbfs: float = -12.0,
        hold_s: float = 0.12,
        cooldown_s: float = 2.0,
    ) -> None:
        self.enabled = bool(enabled)
        self.trip_dbfs = float(trip_dbfs)
        self.warn_dbfs = float(warn_dbfs)
        self.hold_s = float(hold_s)
        self.cooldown_s = float(cooldown_s)
        self._hot_since: Optional[float] = None
        self._tripped = False
        self._trip_at: Optional[float] = None
        self.status = "off" if not self.enabled else "ok"

    def reset(self) -> None:
        self._hot_since = None
        self._tripped = False
        self._trip_at = None
        self.status = "off" if not self.enabled else "ok"

    def observe(
        self,
        return_rms: float,
        forward_rms: Optional[float],
        now: float,
    ) -> Tuple[bool, str]:
        """Return ``(allow_audio, safety_status)``. False → send silence."""
        if not self.enabled:
            self.status = "ok"
            return True, self.status
        ret_db = _rms_to_dbfs(return_rms)
        fwd = None if forward_rms is None else _rms_to_dbfs(forward_rms)
        both_hot = fwd is not None and ret_db >= self.trip_dbfs and fwd >= self.trip_dbfs
        if self._tripped:
            quiet = ret_db < self.warn_dbfs and (fwd is None or fwd < self.warn_dbfs)
            if quiet and self._trip_at is not None and (now - self._trip_at) >= self.cooldown_s:
                self._tripped = False
                self._trip_at = None
                self._hot_since = None
                self.status = "ok"
                return True, self.status
            self.status = "tripped"
            return False, self.status
        if both_hot:
            if self._hot_since is None:
                self._hot_since = now
            elif (now - self._hot_since) >= self.hold_s:
                self._tripped = True
                self._trip_at = now
                self.status = "tripped"
                return False, self.status
            self.status = "gated"
            return True, self.status
        self._hot_since = None
        if ret_db >= self.warn_dbfs:
            self.status = "gated"
            return True, self.status
        self.status = "ok"
        return True, self.status
