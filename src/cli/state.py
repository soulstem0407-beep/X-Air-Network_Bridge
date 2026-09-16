"""Persistencia liviana de overrides (puerto, jitter) además de `.env`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

STATE_NAME = ".xair_runtime.json"


def runtime_path(project_root: Path) -> Path:
    return project_root / STATE_NAME


def load_runtime(project_root: Path) -> Dict[str, Any]:
    p = runtime_path(project_root)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_runtime(project_root: Path, data: Dict[str, Any]) -> None:
    p = runtime_path(project_root)
    p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
