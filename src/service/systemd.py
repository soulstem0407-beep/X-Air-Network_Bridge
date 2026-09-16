"""systemd user/system units for the bridge. Does not touch UDP/JACK transport."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Sequence

UNIT_FILES = {
    "server": "xair-network-bridge-server.service",
    "client": "xair-network-bridge-client.service",
}

SystemctlRunner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def packaging_unit_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "packaging" / "systemd"


def empty_service_snapshot() -> dict:
    return {
        "service_enabled": False,
        "last_service_action": None,
        "last_service_ts": None,
        "service_scope": None,
        "service_role": None,
        "units": [],
    }


def default_service_state_path(anchor: Optional[Path] = None) -> Path:
    raw = os.getenv("XAIR_SERVICE_STATE_PATH")
    if raw and str(raw).strip():
        return Path(str(raw).strip()).expanduser()
    base = Path(anchor) if anchor is not None else Path(".")
    if base.is_file():
        base = base.parent
    return base / ".xair_service.json"


def default_python(project_root: Path) -> Path:
    venv = Path(project_root) / ".venv" / "bin" / "python3"
    if venv.is_file():
        return venv
    return Path(sys.executable)


def expand_roles(role: str) -> List[str]:
    r = str(role or "").strip().lower()
    if r == "both":
        return ["server", "client"]
    if r in UNIT_FILES:
        return [r]
    raise ValueError("role must be server, client, or both")


def unit_dir_for_scope(scope: str, *, override: Optional[Path] = None) -> Path:
    if override is not None:
        return Path(override)
    s = str(scope or "user").strip().lower()
    if s == "system":
        return Path("/etc/systemd/system")
    xdg = os.getenv("XDG_CONFIG_HOME")
    if xdg and str(xdg).strip():
        return Path(str(xdg).strip()).expanduser() / "systemd" / "user"
    return Path.home() / ".config" / "systemd" / "user"


def render_unit(
    role: str,
    *,
    project_root: Path,
    python: Optional[Path] = None,
    scope: str = "user",
    extra_user: Optional[str] = None,
    exec_start: Optional[str] = None,
) -> str:
    """Fill the packaged unit template. Transport ExecStart is unchanged CLI."""
    name = UNIT_FILES[role]
    raw = (packaging_unit_dir() / name).read_text(encoding="utf-8")
    root = str(Path(project_root).resolve())
    py = str(Path(python) if python is not None else default_python(project_root))
    sc = str(scope or "user").strip().lower()
    if sc == "system":
        after = "network-online.target sound.target"
        wants = "network-online.target"
        wanted_by = "multi-user.target"
        extra_lines = []
        if extra_user:
            extra_lines.append(f"User={extra_user}")
        extra = ("\n".join(extra_lines) + "\n") if extra_lines else ""
    else:
        after = "pipewire.service pipewire-pulse.service"
        wants = "pipewire.service"
        wanted_by = "default.target"
        extra = ""
    text = (
        raw.replace("__ROOT__", root)
        .replace("__PYTHON__", py)
        .replace("__AFTER__", after)
        .replace("__WANTS__", wants)
        .replace("__WANTED_BY__", wanted_by)
        .replace("__EXTRA__\n", extra)
        .replace("__EXTRA__", extra.rstrip("\n"))
    )
    if exec_start:
        lines = []
        for line in text.splitlines(True):
            if line.startswith("ExecStart="):
                nl = "\n" if line.endswith("\n") else ""
                line = f"ExecStart={exec_start}{nl}"
            lines.append(line)
        text = "".join(lines)
    return text


def _atomic_write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise


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


def merge_service_into_sync_snapshot(sync_path: Path, service_snap: dict) -> None:
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
        report["service"] = dict(service_snap)
        _atomic_write_json(sync_path, data)
    except Exception:
        return


def persist_service_snapshot(
    snap: dict,
    *,
    project_root: Path,
    sync_state_path: Optional[Path] = None,
) -> Path:
    path = default_service_state_path(project_root)
    _atomic_write_json(path, snap)
    if sync_state_path is not None:
        merge_service_into_sync_snapshot(sync_state_path, snap)
    return path


def run_systemctl(
    scope: str,
    args: Sequence[str],
    *,
    runner: Optional[SystemctlRunner] = None,
) -> subprocess.CompletedProcess[str]:
    cmd: List[str] = ["systemctl"]
    if str(scope).strip().lower() == "user":
        cmd.append("--user")
    cmd.extend(str(a) for a in args)
    if runner is not None:
        return runner(cmd)
    return subprocess.run(cmd, capture_output=True, text=True)


def _unit_enabled(scope: str, unit: str, *, runner: Optional[SystemctlRunner]) -> bool:
    r = run_systemctl(scope, ["is-enabled", "--quiet", unit], runner=runner)
    return r.returncode == 0


def install_service(
    *,
    project_root: Path,
    role: str = "client",
    scope: str = "user",
    python: Optional[Path] = None,
    unit_dir: Optional[Path] = None,
    apply: bool = True,
    start: bool = True,
    linger: bool = False,
    extra_user: Optional[str] = None,
    runner: Optional[SystemctlRunner] = None,
    sync_state_path: Optional[Path] = None,
) -> dict:
    """Write unit files, optionally enable/start via systemctl. No audio I/O."""
    roles = expand_roles(role)
    dest = unit_dir_for_scope(scope, override=unit_dir)
    dest.mkdir(parents=True, exist_ok=True)
    written: List[str] = []
    py = Path(python) if python is not None else default_python(project_root)
    for r in roles:
        text = render_unit(
            r,
            project_root=project_root,
            python=py,
            scope=scope,
            extra_user=extra_user,
        )
        unit = UNIT_FILES[r]
        _atomic_write(dest / unit, text)
        written.append(unit)

    enabled = False
    notes: List[str] = []
    if apply:
        reload = run_systemctl(scope, ["daemon-reload"], runner=runner)
        if reload.returncode != 0:
            notes.append((reload.stderr or reload.stdout or "daemon-reload failed").strip())
        else:
            for unit in written:
                en = run_systemctl(scope, ["enable", unit], runner=runner)
                if en.returncode != 0:
                    notes.append((en.stderr or en.stdout or f"enable {unit} failed").strip())
                if start:
                    st = run_systemctl(scope, ["start", unit], runner=runner)
                    if st.returncode != 0:
                        notes.append((st.stderr or st.stdout or f"start {unit} failed").strip())
            enabled = any(_unit_enabled(scope, u, runner=runner) for u in written)
        if linger and str(scope).strip().lower() == "user":
            linger_cmd = ["loginctl", "enable-linger", os.environ.get("USER") or os.getenv("LOGNAME") or ""]
            if runner is not None:
                lr = runner(linger_cmd)
            else:
                lr = subprocess.run(linger_cmd, capture_output=True, text=True)
            if lr.returncode != 0:
                notes.append((lr.stderr or lr.stdout or "enable-linger failed").strip())
    snap = {
        "service_enabled": bool(enabled),
        "last_service_action": "install",
        "last_service_ts": time.time(),
        "service_scope": str(scope),
        "service_role": ",".join(roles),
        "units": written,
        "unit_dir": str(dest),
        "notes": notes,
    }
    persist_service_snapshot(snap, project_root=project_root, sync_state_path=sync_state_path)
    return snap


def remove_service(
    *,
    project_root: Path,
    role: str = "client",
    scope: str = "user",
    unit_dir: Optional[Path] = None,
    apply: bool = True,
    runner: Optional[SystemctlRunner] = None,
    sync_state_path: Optional[Path] = None,
) -> dict:
    roles = expand_roles(role)
    dest = unit_dir_for_scope(scope, override=unit_dir)
    units = [UNIT_FILES[r] for r in roles]
    notes: List[str] = []
    if apply:
        for unit in units:
            run_systemctl(scope, ["stop", unit], runner=runner)
            dis = run_systemctl(scope, ["disable", unit], runner=runner)
            if dis.returncode != 0:
                notes.append((dis.stderr or dis.stdout or f"disable {unit} failed").strip())
        run_systemctl(scope, ["daemon-reload"], runner=runner)
    for unit in units:
        path = dest / unit
        try:
            if path.is_file():
                path.unlink()
        except OSError as exc:
            notes.append(str(exc))
    snap = {
        "service_enabled": False,
        "last_service_action": "remove",
        "last_service_ts": time.time(),
        "service_scope": str(scope),
        "service_role": ",".join(roles),
        "units": units,
        "unit_dir": str(dest),
        "notes": notes,
    }
    persist_service_snapshot(snap, project_root=project_root, sync_state_path=sync_state_path)
    return snap
