"""Load and shape the SyncManager snapshot for the dashboard (pure, no I/O besides JSON)."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


def rms_linear_to_dbfs(rms_linear: Optional[float]) -> Optional[float]:
    if rms_linear is None:
        return None
    return 20.0 * math.log10(max(float(rms_linear), 1e-12))


def meter_int_to_db(meter_int: Optional[int]) -> Optional[float]:
    if meter_int is None:
        return None
    return float(meter_int) / 256.0


def bar(fraction: float, width: int) -> str:
    """ASCII meter. ``fraction`` is 0..1; values outside are clipped."""
    if width <= 0:
        return ""
    f = max(0.0, min(1.0, float(fraction)))
    filled = int(round(f * width))
    filled = min(width, max(0, filled))
    return "#" * filled + "-" * (width - filled)


def dbfs_to_fraction(dbfs: Optional[float], floor_db: float = -60.0) -> float:
    """Map dBFS in [floor, 0] to 0..1 for a bar."""
    if dbfs is None:
        return 0.0
    span = max(1.0, -float(floor_db))
    return max(0.0, min(1.0, (float(dbfs) - float(floor_db)) / span))


@dataclass
class ChannelView:
    ch: int
    name: str
    fader: Optional[float]
    rms_dbfs: Optional[float]
    meter_db: Optional[float]
    delta_db: Optional[float]
    level_ok: Optional[bool]
    drift_ns: Optional[int]
    last_eq_write_ns: Optional[int] = None
    last_dyn_write_ns: Optional[int] = None
    last_fx_write_ns: Optional[int] = None


@dataclass
class DashboardView:
    path: Path
    status: str
    age_sec: Optional[float]
    global_drift_ns: Optional[int]
    tolerance_db: Optional[float]
    metrics: Dict[str, Any] = field(default_factory=dict)
    channels: List[ChannelView] = field(default_factory=list)
    n_ok: int = 0
    n_ko: int = 0
    n_na: int = 0
    report_interval_sec: float = 5.0
    return_path: Dict[str, Any] = field(default_factory=dict)
    discovery: Dict[str, Any] = field(default_factory=dict)
    dsp: Dict[str, Any] = field(default_factory=dict)
    record: Dict[str, Any] = field(default_factory=dict)
    tests: Dict[str, Any] = field(default_factory=dict)
    service: Dict[str, Any] = field(default_factory=dict)
    package: Dict[str, Any] = field(default_factory=dict)
    release: Dict[str, Any] = field(default_factory=dict)
    integration: Dict[str, Any] = field(default_factory=dict)


def default_state_path(project_root: Path) -> Path:
    import os

    raw = os.getenv("XAIR_SYNC_STATE_PATH")
    if raw and str(raw).strip():
        return Path(str(raw).strip()).expanduser()
    return project_root / ".xair_sync_state.json"


def load_discovery_sidecar(snapshot_path: Path) -> Dict[str, Any]:
    path = snapshot_path.parent / ".xair_discovery.json"
    envp = os.getenv("XAIR_DISCOVERY_STATE_PATH")
    if envp and str(envp).strip():
        path = Path(str(envp).strip()).expanduser()
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_snapshot_file(path: Path) -> Optional[dict]:
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def snapshot_age_sec(data: dict, path: Path, *, now: Optional[float] = None) -> Optional[float]:
    """Age from ``exported_time_ns`` when present, else file mtime. Seconds."""
    t = now if now is not None else time.time()
    exported = data.get("exported_time_ns")
    if isinstance(exported, (int, float)) and float(exported) > 0:
        return t - (float(exported) / 1_000_000_000.0)
    try:
        return t - path.stat().st_mtime
    except OSError:
        return None


def snapshot_fresh(data: dict, path: Path, *, now: Optional[float] = None) -> bool:
    if not data.get("active"):
        return False
    interval = float(data.get("report_interval_sec") or 5.0)
    max_age = max(45.0, interval * 3.0 + 5.0)
    age = snapshot_age_sec(data, path, now=now)
    if age is None:
        return False
    return age <= max_age


def _discovery_block(path: Path, rep: Optional[dict] = None) -> Dict[str, Any]:
    sidecar = load_discovery_sidecar(path)
    disc = {}
    if isinstance(rep, dict) and isinstance(rep.get("discovery"), dict):
        disc = dict(rep["discovery"])
    if disc.get("discovered_devices") or disc.get("discovery_enabled"):
        return disc
    if sidecar:
        return {
            "discovery_enabled": bool(sidecar.get("discovery_enabled")),
            "discovered_devices": sidecar.get("discovered_devices") or [],
            "last_discovery_ts": sidecar.get("last_discovery_ts"),
            "discovery_health": sidecar.get("discovery_health") or "off",
        }
    return disc


def load_record_sidecar(snapshot_path: Path) -> Dict[str, Any]:
    path = snapshot_path.parent / ".xair_record.json"
    envp = os.getenv("XAIR_RECORD_STATE_PATH")
    if envp and str(envp).strip():
        path = Path(str(envp).strip()).expanduser()
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _record_block(path: Path, rep: Optional[dict] = None) -> Dict[str, Any]:
    rec: Dict[str, Any] = {}
    if isinstance(rep, dict) and isinstance(rep.get("record"), dict):
        rec = dict(rep["record"])
    sidecar = load_record_sidecar(path)
    if sidecar:
        rec.update(sidecar)
    return rec


def load_test_sidecar(snapshot_path: Path) -> Dict[str, Any]:
    path = snapshot_path.parent / ".xair_test.json"
    envp = os.getenv("XAIR_TEST_STATE_PATH")
    if envp and str(envp).strip():
        path = Path(str(envp).strip()).expanduser()
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _test_block(path: Path, rep: Optional[dict] = None) -> Dict[str, Any]:
    tests: Dict[str, Any] = {}
    if isinstance(rep, dict) and isinstance(rep.get("test"), dict):
        tests = dict(rep["test"])
    sidecar = load_test_sidecar(path)
    if sidecar:
        tests.update(sidecar)
    return tests


def load_service_sidecar(snapshot_path: Path) -> Dict[str, Any]:
    path = snapshot_path.parent / ".xair_service.json"
    envp = os.getenv("XAIR_SERVICE_STATE_PATH")
    if envp and str(envp).strip():
        path = Path(str(envp).strip()).expanduser()
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _service_block(path: Path, rep: Optional[dict] = None) -> Dict[str, Any]:
    svc: Dict[str, Any] = {}
    if isinstance(rep, dict) and isinstance(rep.get("service"), dict):
        svc = dict(rep["service"])
    sidecar = load_service_sidecar(path)
    if sidecar:
        svc.update(sidecar)
    return svc


def load_package_sidecar(snapshot_path: Path) -> Dict[str, Any]:
    path = snapshot_path.parent / ".xair_package.json"
    envp = os.getenv("XAIR_PACKAGE_STATE_PATH")
    if envp and str(envp).strip():
        path = Path(str(envp).strip()).expanduser()
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _package_block(path: Path, rep: Optional[dict] = None) -> Dict[str, Any]:
    from ..package.meta import empty_package_snapshot

    pkg = empty_package_snapshot(path)
    if isinstance(rep, dict) and isinstance(rep.get("package"), dict):
        pkg.update(rep["package"])
    sidecar = load_package_sidecar(path)
    if sidecar:
        pkg.update(sidecar)
    return pkg


def load_release_sidecar(snapshot_path: Path) -> Dict[str, Any]:
    path = snapshot_path.parent / ".xair_release.json"
    envp = os.getenv("XAIR_RELEASE_STATE_PATH")
    if envp and str(envp).strip():
        path = Path(str(envp).strip()).expanduser()
    if not path.is_file():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _release_block(path: Path, rep: Optional[dict] = None) -> Dict[str, Any]:
    from ..release.meta import empty_release_snapshot

    rel = empty_release_snapshot(path)
    if isinstance(rep, dict) and isinstance(rep.get("release"), dict):
        rel.update(rep["release"])
    sidecar = load_release_sidecar(path)
    if sidecar:
        rel.update(sidecar)
    return rel


def _rep_dict(rep: Optional[dict], key: str) -> Dict[str, Any]:
    if isinstance(rep, dict) and isinstance(rep.get(key), dict):
        return dict(rep[key])
    return {}


def _integration_block(
    path: Path,
    data: Optional[dict],
    rep: Optional[dict],
    *,
    metrics: Dict[str, Any],
    return_path: Dict[str, Any],
    discovery: Dict[str, Any],
    dsp: Dict[str, Any],
    record: Dict[str, Any],
) -> Dict[str, Any]:
    from ..sync.integration import empty_integration_snapshot, integration_snapshot

    integ = empty_integration_snapshot()
    if isinstance(rep, dict) and isinstance(rep.get("integration"), dict):
        integ.update(rep["integration"])
    active = bool(data and data.get("active"))
    last_ts = integ.get("last_integration_ts")
    computed = integration_snapshot(
        active=active,
        last_ts=float(last_ts) if isinstance(last_ts, (int, float)) else None,
        metrics=metrics,
        return_path=return_path,
        discovery=discovery,
        dsp=dsp,
        record=record,
    )
    integ.update(computed)
    return integ


def _channels_from_report(rep: Optional[dict]) -> tuple[List[ChannelView], int, int, int]:
    channels: List[ChannelView] = []
    n_ok = n_ko = n_na = 0
    rows_in = rep.get("channels") if isinstance(rep, dict) and isinstance(rep.get("channels"), list) else []
    for row in rows_in:
        if not isinstance(row, dict):
            continue
        rms = row.get("rms_audio")
        meter = row.get("meter_osc")
        ok = row.get("level_ok")
        if rms is None or meter is None:
            n_na += 1
        elif ok is True:
            n_ok += 1
        else:
            n_ko += 1
        name = str(row.get("name") or "")
        channels.append(
            ChannelView(
                ch=int(row.get("ch") or 0),
                name=name,
                fader=row.get("fader") if isinstance(row.get("fader"), (int, float)) else None,
                rms_dbfs=rms_linear_to_dbfs(float(rms) if isinstance(rms, (int, float)) else None),
                meter_db=meter_int_to_db(int(meter) if isinstance(meter, (int, float)) else None),
                delta_db=float(row["delta_level"]) if isinstance(row.get("delta_level"), (int, float)) else None,
                level_ok=ok if isinstance(ok, bool) else None,
                drift_ns=int(row["drift_ns"]) if isinstance(row.get("drift_ns"), (int, float)) else None,
                last_eq_write_ns=int(row["last_eq_write_ns"])
                if isinstance(row.get("last_eq_write_ns"), (int, float))
                else None,
                last_dyn_write_ns=int(row["last_dyn_write_ns"])
                if isinstance(row.get("last_dyn_write_ns"), (int, float))
                else None,
                last_fx_write_ns=int(row["last_fx_write_ns"])
                if isinstance(row.get("last_fx_write_ns"), (int, float))
                else None,
            )
        )
    return channels, n_ok, n_ko, n_na


def build_view(path: Path, data: Optional[dict], *, now: Optional[float] = None) -> DashboardView:
    """Turn a snapshot dict (or missing file) into a render-ready view."""
    t = now if now is not None else time.time()
    if data is None:
        rec = _record_block(path, None)
        disc = _discovery_block(path, None)
        integ = _integration_block(
            path, None, None, metrics={}, return_path={}, discovery=disc, dsp={}, record=rec
        )
        return DashboardView(
            path=path,
            status="missing",
            age_sec=None,
            global_drift_ns=None,
            tolerance_db=None,
            discovery=disc,
            record=rec,
            tests=_test_block(path, None),
            service=_service_block(path, None),
            package=_package_block(path, None),
            release=_release_block(path, None),
            integration=integ,
        )
    age = snapshot_age_sec(data, path, now=t)
    rep = data.get("report") if isinstance(data.get("report"), dict) else {}
    metrics = _rep_dict(rep, "receiver_metrics")
    ret = _rep_dict(rep, "return_path")
    dsp = _rep_dict(rep, "dsp")
    disc = _discovery_block(path, rep)
    rec = _record_block(path, rep)
    integ = _integration_block(
        path, data, rep, metrics=metrics, return_path=ret, discovery=disc, dsp=dsp, record=rec
    )
    gdrift = rep.get("global_drift_ns") if isinstance(rep, dict) else None
    channels, n_ok, n_ko, n_na = _channels_from_report(rep)
    common = dict(
        path=path,
        age_sec=age,
        global_drift_ns=int(gdrift) if isinstance(gdrift, (int, float)) else None,
        tolerance_db=float(rep["level_tolerance_db"])
        if isinstance(rep.get("level_tolerance_db"), (int, float))
        else None,
        metrics=metrics,
        channels=channels,
        n_ok=n_ok,
        n_ko=n_ko,
        n_na=n_na,
        report_interval_sec=float(data.get("report_interval_sec") or 5.0),
        return_path=ret,
        discovery=disc,
        dsp=dsp,
        record=rec,
        tests=_test_block(path, rep),
        service=_service_block(path, rep),
        package=_package_block(path, rep),
        release=_release_block(path, rep),
        integration=integ,
    )
    if not data.get("active"):
        return DashboardView(status="inactive", **common)
    status = "live" if snapshot_fresh(data, path, now=t) else "stale"
    return DashboardView(status=status, **common)


def load_view(path: Path) -> DashboardView:
    return build_view(path, load_snapshot_file(path))


def view_as_dict(view: DashboardView) -> Dict[str, Any]:
    """JSON-friendly payload for the HTTP dashboard."""
    return {
        "path": str(view.path),
        "status": view.status,
        "age_sec": view.age_sec,
        "global_drift_ns": view.global_drift_ns,
        "tolerance_db": view.tolerance_db,
        "metrics": view.metrics,
        "n_ok": view.n_ok,
        "n_ko": view.n_ko,
        "n_na": view.n_na,
        "report_interval_sec": view.report_interval_sec,
        "return_path": view.return_path,
        "discovery": view.discovery,
        "dsp": view.dsp,
        "record": view.record,
        "tests": view.tests,
        "service": view.service,
        "package": view.package,
        "release": view.release,
        "integration": view.integration,
        "channels": [
            {
                "ch": c.ch,
                "name": c.name,
                "fader": c.fader,
                "rms_dbfs": c.rms_dbfs,
                "meter_db": c.meter_db,
                "delta_db": c.delta_db,
                "level_ok": c.level_ok,
                "drift_ns": c.drift_ns,
                "last_eq_write_ns": c.last_eq_write_ns,
                "last_dyn_write_ns": c.last_dyn_write_ns,
                "last_fx_write_ns": c.last_fx_write_ns,
            }
            for c in view.channels
        ],
    }
