"""Release version snapshot. No audio I/O or transport imports."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

from ..package.meta import read_version


def project_root_from_here() -> Path:
    return Path(__file__).resolve().parents[2]


def empty_release_snapshot(root: Optional[Path] = None) -> dict:
    return {
        "release_version": read_version(root),
        "release_build_ts": None,
    }


def default_release_state_path(anchor: Optional[Path] = None) -> Path:
    raw = os.getenv("XAIR_RELEASE_STATE_PATH")
    if raw and str(raw).strip():
        return Path(str(raw).strip()).expanduser()
    base = Path(anchor) if anchor is not None else project_root_from_here()
    if base.is_file():
        base = base.parent
    return base / ".xair_release.json"


def now_ts() -> float:
    env = os.getenv("SOURCE_DATE_EPOCH")
    if env and str(env).strip():
        try:
            return float(int(str(env).strip()))
        except ValueError:
            pass
    return time.time()


def ensure_source_date_epoch() -> int:
    raw = os.getenv("SOURCE_DATE_EPOCH")
    if raw and str(raw).strip():
        try:
            return int(str(raw).strip())
        except ValueError:
            pass
    epoch = int(time.time())
    os.environ["SOURCE_DATE_EPOCH"] = str(epoch)
    return epoch


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


def merge_release_into_sync_snapshot(sync_path: Path, rel_snap: dict) -> None:
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
        report["release"] = dict(rel_snap)
        _atomic_write_json(sync_path, data)
    except Exception:
        return


def persist_release_snapshot(
    snap: dict,
    *,
    project_root: Path,
    sync_state_path: Optional[Path] = None,
) -> Path:
    path = default_release_state_path(project_root)
    _atomic_write_json(path, snap)
    if sync_state_path is not None:
        merge_release_into_sync_snapshot(sync_state_path, snap)
    return path


def load_release_sidecar(snapshot_path: Path) -> dict:
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
