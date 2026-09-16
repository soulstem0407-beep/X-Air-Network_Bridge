"""Aggregate module health for the integration snapshot. No audio I/O."""
from __future__ import annotations

from typing import Any, Mapping, Optional

HEALTH_RANK = {"off": 0, "ok": 1, "warn": 2, "fail": 3}


def empty_integration_snapshot() -> dict:
    return {
        "integration_health": "off",
        "last_integration_ts": None,
    }


def _norm(value: Any) -> str:
    s = str(value or "off").strip().lower()
    return s if s in HEALTH_RANK else "off"


def classify_integration_health(
    *,
    active: bool,
    metrics: Optional[Mapping[str, Any]] = None,
    return_path: Optional[Mapping[str, Any]] = None,
    discovery: Optional[Mapping[str, Any]] = None,
    dsp: Optional[Mapping[str, Any]] = None,
    record: Optional[Mapping[str, Any]] = None,
) -> str:
    """Worst of enabled subsystem healths. Unused modules (off) do not fail the bus."""
    if not active:
        return "off"
    scores: list[str] = []
    m = metrics or {}
    rx = _norm(m.get("rx_health"))
    if rx != "off":
        scores.append(rx)
    if m.get("fec_enabled"):
        scores.append(_norm(m.get("fec_health")))
    if m.get("opus_enabled"):
        scores.append(_norm(m.get("opus_health")))
    rp = return_path or {}
    if rp.get("enabled"):
        scores.append(_norm(rp.get("return_health")))
    disc = discovery or {}
    if disc.get("discovery_enabled"):
        scores.append(_norm(disc.get("discovery_health")))
    d = dsp or {}
    if d.get("eq_enabled"):
        scores.append(_norm(d.get("eq_health")))
    if d.get("dyn_enabled"):
        scores.append(_norm(d.get("dyn_health")))
    if d.get("fx_enabled"):
        scores.append(_norm(d.get("fx_health")))
    rec = record or {}
    if rec.get("recording") or rec.get("record_enabled"):
        scores.append(_norm(rec.get("record_health")))
    if not scores:
        return "ok"
    return max(scores, key=lambda s: HEALTH_RANK.get(s, 0))


def integration_snapshot(
    *,
    active: bool,
    last_ts: Optional[float],
    metrics: Optional[Mapping[str, Any]] = None,
    return_path: Optional[Mapping[str, Any]] = None,
    discovery: Optional[Mapping[str, Any]] = None,
    dsp: Optional[Mapping[str, Any]] = None,
    record: Optional[Mapping[str, Any]] = None,
) -> dict:
    return {
        "integration_health": classify_integration_health(
            active=active,
            metrics=metrics,
            return_path=return_path,
            discovery=discovery,
            dsp=dsp,
            record=record,
        ),
        "last_integration_ts": last_ts,
    }
