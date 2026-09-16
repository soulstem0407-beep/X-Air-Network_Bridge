"""Package version and snapshot. No audio I/O."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

PACKAGE_NAME = "xair-network-bridge"
DEB_NAME = "xair-network-bridge"
INSTALL_LIBDIR = "/usr/lib/xair-network-bridge"
INSTALL_BIN = "/usr/bin/xair-network-bridge"
INSTALL_USER_UNITDIR = "/usr/lib/systemd/user"


def project_root_from_here() -> Path:
    return Path(__file__).resolve().parents[2]


def read_version(root: Optional[Path] = None) -> str:
    raw = os.getenv("XAIR_PACKAGE_VERSION")
    if raw and str(raw).strip():
        return str(raw).strip()
    base = Path(root) if root is not None else project_root_from_here()
    if base.is_file():
        base = base.parent
    path = base / "VERSION"
    if path.is_file():
        line = path.read_text(encoding="utf-8").strip().splitlines()
        if line and line[0].strip():
            return line[0].strip()
    return "0.0.0"


def empty_package_snapshot(root: Optional[Path] = None) -> dict:
    return {
        "package_version": read_version(root),
        "package_build_ts": None,
    }


def default_package_state_path(anchor: Optional[Path] = None) -> Path:
    raw = os.getenv("XAIR_PACKAGE_STATE_PATH")
    if raw and str(raw).strip():
        return Path(str(raw).strip()).expanduser()
    base = Path(anchor) if anchor is not None else project_root_from_here()
    if base.is_file():
        base = base.parent
    return base / ".xair_package.json"


def now_ts() -> float:
    env = os.getenv("SOURCE_DATE_EPOCH")
    if env and str(env).strip():
        try:
            return float(int(str(env).strip()))
        except ValueError:
            pass
    return time.time()


def _atomic_write_json(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise


def merge_package_into_sync_snapshot(sync_path: Path, pkg_snap: dict) -> None:
    if not sync_path.is_file():
        return
    try:
        with open(sync_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not data.get("active"):
            return
        report = data.get("report")
        if not isinstance(report, dict):
            report = {}
            data["report"] = report
        report["package"] = dict(pkg_snap)
        _atomic_write_json(sync_path, data)
    except Exception:
        return


def persist_package_snapshot(
    snap: dict,
    *,
    project_root: Path,
    sync_state_path: Optional[Path] = None,
) -> Path:
    path = default_package_state_path(project_root)
    _atomic_write_json(path, snap)
    if sync_state_path is not None:
        merge_package_into_sync_snapshot(sync_state_path, snap)
    return path


def load_package_sidecar(snapshot_path: Path) -> dict:
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


def current_package_snapshot(anchor: Optional[Path] = None) -> dict:
    snap = empty_package_snapshot(anchor)
    if anchor is None:
        side_from = project_root_from_here() / ".xair_sync_state.json"
    else:
        p = Path(anchor)
        side_from = p if p.suffix else p / ".xair_sync_state.json"
        if p.is_dir():
            side_from = p / ".xair_sync_state.json"
    side = load_package_sidecar(side_from)
    if side:
        snap.update(side)
        if not snap.get("package_version"):
            snap["package_version"] = read_version(anchor)
    return snap
