"""Dump and recall a subset of X AIR mix state as JSON.

Control plane only: names, faders, HA gain, pan, mix/on, sends, buses, LR.
Does not touch USB capture or the XBRI UDP path. EQ/dyn/FX are out of scope
(later roadmap). Meters are live levels, not mix state, so they are omitted.

Mute storage uses the mixer-native ``mix_on`` (``/ch/XX/mix/on``, 1 = open)
plus a derived ``muted`` flag for humans. Recall prefers ``mix_on``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .osc_bridge import XAirOSCBridge, XAirOSCError

log = logging.getLogger(__name__)

SCENE_SCHEMA = 1
SCENE_KIND = "xair-scene"
DEFAULT_SENDS = 4
DEFAULT_BUSES = 4


def _round_f(value: float) -> float:
    return round(float(value), 6)


def _is_num(value: Any) -> bool:
    """JSON number (bool is a subclass of int — exclude it)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _try(errors: List[str], label: str, fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except Exception as exc:
        errors.append(f"{label}: {exc}")
        log.debug("scene skip %s", label, exc_info=True)
        return None


@dataclass
class SceneScope:
    """How much of the desk to read or write."""

    channels: int = 18
    sends: int = DEFAULT_SENDS
    buses: int = DEFAULT_BUSES

    @classmethod
    def from_env(cls) -> SceneScope:
        def _int(name: str, default: int) -> int:
            raw = os.getenv(name)
            if raw is None or not str(raw).strip():
                return default
            return int(raw)

        ch = max(1, min(18, _int("XAIR_SCENE_CHANNELS", _int("XAIR_CHANNELS", 18))))
        return cls(
            channels=ch,
            sends=max(0, min(16, _int("XAIR_SCENE_SENDS", DEFAULT_SENDS))),
            buses=max(0, min(16, _int("XAIR_SCENE_BUSES", DEFAULT_BUSES))),
        )


@dataclass
class SceneApplyReport:
    applied: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    dry_run: bool = False


def dump_scene(
    bridge: XAirOSCBridge,
    scope: Optional[SceneScope] = None,
) -> Dict[str, Any]:
    """GET mix parameters into a JSON-serializable dict. Continues on timeouts."""
    sc = scope if scope is not None else SceneScope.from_env()
    errors: List[str] = []
    channels: List[Dict[str, Any]] = []
    for ch in range(1, sc.channels + 1):
        row: Dict[str, Any] = {"ch": ch}
        name = _try(errors, f"ch{ch}.name", lambda c=ch: bridge.get_channel_name(c))
        if name is not None:
            row["name"] = str(name)
        fader = _try(errors, f"ch{ch}.fader", lambda c=ch: bridge.get_fader(c))
        if fader is not None:
            row["fader"] = _round_f(float(fader))
        gain = _try(errors, f"ch{ch}.gain", lambda c=ch: bridge.get_gain(c))
        if gain is not None:
            row["gain"] = _round_f(float(gain))
        pan = _try(errors, f"ch{ch}.pan", lambda c=ch: bridge.get_pan(c))
        if pan is not None:
            row["pan"] = _round_f(float(pan))
        mix_on = _try(errors, f"ch{ch}.mix_on", lambda c=ch: bridge.get_mute(c))
        if mix_on is not None:
            on = bool(mix_on)
            row["mix_on"] = on
            row["muted"] = not on
        sends: List[Optional[float]] = []
        for si in range(1, sc.sends + 1):
            sv = _try(
                errors,
                f"ch{ch}.send{si}",
                lambda c=ch, s=si: bridge.get_send(c, s),
            )
            sends.append(_round_f(float(sv)) if sv is not None else None)
        if sc.sends:
            row["sends"] = sends
        channels.append(row)

    buses: List[Dict[str, Any]] = []
    for bus in range(1, sc.buses + 1):
        fv = _try(errors, f"bus{bus}.fader", lambda b=bus: bridge.get_bus_fader(b))
        entry: Dict[str, Any] = {"bus": bus}
        if fv is not None:
            entry["fader"] = _round_f(float(fv))
        buses.append(entry)

    lr_f = _try(errors, "lr.fader", bridge.get_lr_fader)
    lr: Dict[str, Any] = {}
    if lr_f is not None:
        lr["fader"] = _round_f(float(lr_f))

    return {
        "schema": SCENE_SCHEMA,
        "kind": SCENE_KIND,
        "exported_time_ns": time.time_ns(),
        "osc_host": bridge.host,
        "osc_port": bridge.port,
        "channels": sc.channels,
        "sends": sc.sends,
        "buses": sc.buses,
        "lr": lr,
        "channel": channels,
        "bus": buses,
        "errors": errors,
    }


def validate_scene(data: Any) -> Dict[str, Any]:
    """Raise ValueError if the document is not a usable scene."""
    if not isinstance(data, dict):
        raise ValueError("scene JSON debe ser un objeto")
    kind = data.get("kind")
    if kind not in (SCENE_KIND, None):
        raise ValueError(f"kind inválido: {kind!r} (esperado {SCENE_KIND!r})")
    schema = data.get("schema", SCENE_SCHEMA)
    try:
        schema_i = int(schema)
    except (TypeError, ValueError) as exc:
        raise ValueError("schema inválido") from exc
    if schema_i != SCENE_SCHEMA:
        raise ValueError(f"schema {schema_i} no soportado (este código lee {SCENE_SCHEMA})")
    chans = data.get("channel")
    if chans is None:
        chans = []
    if not isinstance(chans, list):
        raise ValueError("channel debe ser una lista")
    return data


def _channel_rows(scene: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = scene.get("channel")
    if isinstance(raw, list):
        return [r for r in raw if isinstance(r, dict)]
    return []


def _mix_on_from_row(row: Dict[str, Any]) -> Optional[bool]:
    if "mix_on" in row:
        v = row["mix_on"]
        if isinstance(v, bool):
            return v
        try:
            return bool(round(float(v)))
        except (TypeError, ValueError):
            return None
    if "muted" in row:
        muted = row["muted"]
        if isinstance(muted, bool):
            return not muted
        try:
            return not bool(round(float(muted)))
        except (TypeError, ValueError):
            return None
    return None


def _apply_one(
    report: SceneApplyReport,
    label: str,
    fn: Callable[[], Any],
    dry_run: bool,
) -> None:
    if dry_run:
        report.applied += 1
        log.debug("dry-run %s", label)
        return
    try:
        fn()
        report.applied += 1
    except Exception as exc:
        report.skipped += 1
        report.errors.append(f"{label}: {exc}")
        log.debug("recall skip %s", label, exc_info=True)


def apply_scene(
    bridge: Optional[XAirOSCBridge],
    scene: Dict[str, Any],
    *,
    dry_run: bool = False,
    continue_on_error: bool = True,
) -> SceneApplyReport:
    """Write scene fields to the mixer.

    Order: mix/on first (keep muted channels quiet), then names/pan/sends,
    faders, gains, buses, LR. Continues on OSC errors unless
    ``continue_on_error`` is false.
    """
    if not dry_run and bridge is None:
        raise ValueError("bridge es obligatorio salvo --dry-run")
    validate_scene(scene)
    report = SceneApplyReport(dry_run=dry_run)

    def _maybe_raise() -> None:
        if not continue_on_error and report.errors:
            raise XAirOSCError(report.errors[-1])

    for row in _channel_rows(scene):
        try:
            ch = int(row.get("ch") or 0)
        except (TypeError, ValueError):
            report.errors.append(f"fila canal sin ch: {row!r}")
            _maybe_raise()
            continue
        if not (1 <= ch <= 18):
            report.errors.append(f"ch fuera de rango: {ch}")
            _maybe_raise()
            continue
        mix_on = _mix_on_from_row(row)
        if mix_on is not None:
            _apply_one(
                report,
                f"ch{ch}.mix_on",
                lambda c=ch, v=mix_on: bridge.set_mute(c, v),
                dry_run,
            )
            _maybe_raise()
        if "name" in row and row["name"] is not None:
            _apply_one(
                report,
                f"ch{ch}.name",
                lambda c=ch, v=str(row["name"]): bridge.set_channel_name(c, v),
                dry_run,
            )
            _maybe_raise()
        if _is_num(row.get("pan")):
            _apply_one(
                report,
                f"ch{ch}.pan",
                lambda c=ch, v=float(row["pan"]): bridge.set_pan(c, v),
                dry_run,
            )
            _maybe_raise()
        sends = row.get("sends")
        if isinstance(sends, list):
            for i, sv in enumerate(sends, start=1):
                if not _is_num(sv):
                    continue
                _apply_one(
                    report,
                    f"ch{ch}.send{i}",
                    lambda c=ch, s=i, v=float(sv): bridge.set_send(c, s, v),
                    dry_run,
                )
                _maybe_raise()
        if _is_num(row.get("fader")):
            _apply_one(
                report,
                f"ch{ch}.fader",
                lambda c=ch, v=float(row["fader"]): bridge.set_fader(c, v),
                dry_run,
            )
            _maybe_raise()
        if _is_num(row.get("gain")):
            _apply_one(
                report,
                f"ch{ch}.gain",
                lambda c=ch, v=float(row["gain"]): bridge.set_gain(c, v),
                dry_run,
            )
            _maybe_raise()

    for brow in scene.get("bus") or []:
        if not isinstance(brow, dict):
            continue
        try:
            bus = int(brow.get("bus") or 0)
        except (TypeError, ValueError):
            continue
        if not (1 <= bus <= 16):
            continue
        if _is_num(brow.get("fader")):
            _apply_one(
                report,
                f"bus{bus}.fader",
                lambda b=bus, v=float(brow["fader"]): bridge.set_bus_fader(b, v),
                dry_run,
            )
            _maybe_raise()

    lr = scene.get("lr")
    if isinstance(lr, dict) and _is_num(lr.get("fader")):
        _apply_one(
            report,
            "lr.fader",
            lambda v=float(lr["fader"]): bridge.set_lr_fader(v),
            dry_run,
        )
        _maybe_raise()

    return report


def save_scene_file(path: Path, scene: Dict[str, Any]) -> None:
    """Atomic JSON write (tmp + replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(json.dumps(scene, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise


def load_scene_file(path: Path) -> Dict[str, Any]:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"no se pudo leer {path}: {exc}") from exc
    return validate_scene(data)
