"""Release artifacts. Does not import receiver or change transport/control."""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from ..package.builder import (
    build_deb,
    build_tarball,
    default_dist_dir,
    installed_unit_text,
)
from ..package.meta import (
    DEB_NAME,
    INSTALL_LIBDIR,
    PACKAGE_NAME,
    read_version,
)
from .meta import ensure_source_date_epoch, now_ts, persist_release_snapshot

DEFAULT_CFLAGS = "-O2 -g0 -DNDEBUG -fno-ident"
DEFAULT_LDFLAGS = "-s -Wl,--build-id=none -Wl,--as-needed"


def _split_flags(raw: str) -> List[str]:
    return [p for p in str(raw).split() if p]


def release_cflags() -> str:
    raw = os.getenv("CFLAGS")
    if raw and str(raw).strip():
        return str(raw).strip()
    return DEFAULT_CFLAGS


def release_ldflags() -> str:
    raw = os.getenv("LDFLAGS")
    if raw and str(raw).strip():
        return str(raw).strip()
    return DEFAULT_LDFLAGS


def apply_repro_env() -> int:
    """In-process locale/hash env. SOURCE_DATE_EPOCH is what stamps artifacts."""
    epoch = ensure_source_date_epoch()
    os.environ.setdefault("PYTHONHASHSEED", "0")
    os.environ.setdefault("TZ", "UTC")
    os.environ.setdefault("LC_ALL", "C")
    os.environ.setdefault("LANG", "C")
    os.environ.setdefault("CFLAGS", release_cflags())
    os.environ.setdefault("LDFLAGS", release_ldflags())
    return epoch


def format_release_stamp(
    *,
    version: str,
    build_ts: float,
    cflags: str,
    ldflags: str,
    stripped: bool,
) -> str:
    epoch = int(build_ts)
    return (
        f"release_version={version}\n"
        f"release_build_ts={build_ts}\n"
        f"source_date_epoch={epoch}\n"
        f"cflags={cflags}\n"
        f"ldflags={ldflags}\n"
        f"stripped_binaries={'1' if stripped else '0'}\n"
        f"reproducible=1\n"
    )


def compile_stripped_launcher(
    dest: Path,
    *,
    version: str,
    install_root: str = INSTALL_LIBDIR,
) -> Optional[Path]:
    """Compile + strip the exec wrapper. Returns None if no C compiler."""
    src = Path(__file__).with_name("launcher.c")
    if not src.is_file():
        return None
    cc = os.getenv("CC") or shutil.which("cc") or shutil.which("gcc")
    if not cc:
        return None
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    cmd = (
        [str(cc)]
        + _split_flags(release_cflags())
        + [
            f"-DXAIR_ROOT=\"{install_root}\"",
            f"-DXAIR_VERSION=\"{version}\"",
            str(src),
            "-o",
            str(dest),
        ]
        + _split_flags(release_ldflags())
    )
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.CalledProcessError):
        return None
    strip = shutil.which("strip")
    if strip:
        try:
            subprocess.run(
                [strip, "--strip-unneeded", str(dest)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            pass
    try:
        dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
    return dest if dest.is_file() else None


def write_systemd_units(dest_dir: Path, *, project_root: Path) -> List[Path]:
    out_dir = Path(dest_dir) / "systemd"
    out_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for role in ("server", "client"):
        text = installed_unit_text(role, project_root=project_root)
        path = out_dir / f"xair-network-bridge-{role}.service"
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


def write_sha256sums(paths: List[Path], dest: Path) -> Path:
    dest = Path(dest)
    lines: List[str] = []
    for path in sorted({p.resolve() for p in paths}, key=lambda p: p.name):
        if not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.name}")
    dest.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="ascii")
    return dest


def build_release(
    project_root: Path,
    *,
    dest_dir: Optional[Path] = None,
    sync_state_path: Optional[Path] = None,
) -> Dict[str, object]:
    """deb + tarball + systemd units + optional stripped launcher. No mixer."""
    root = Path(project_root).resolve()
    apply_repro_env()
    version = read_version(root)
    ts = now_ts()
    dest = Path(dest_dir) if dest_dir is not None else default_dist_dir(root)
    dest.mkdir(parents=True, exist_ok=True)

    cflags = release_cflags()
    ldflags = release_ldflags()
    launcher_path = compile_stripped_launcher(
        dest / "bin" / "xair-network-bridge",
        version=version,
        install_root=INSTALL_LIBDIR,
    )
    stripped = launcher_path is not None
    stamp_text = format_release_stamp(
        version=version,
        build_ts=float(ts),
        cflags=cflags,
        ldflags=ldflags,
        stripped=stripped,
    )
    stamp_bytes = stamp_text.encode("utf-8")
    stamp_file = dest / "RELEASE"
    stamp_file.write_text(stamp_text, encoding="utf-8")

    prefix = f"{PACKAGE_NAME}-{version}/"
    tar_extra: List[Tuple[str, bytes, int]] = [
        (f"{prefix}RELEASE", stamp_bytes, 0o100644),
    ]
    deb_extra: List[Tuple[str, bytes, int]] = [
        (f"{INSTALL_LIBDIR.lstrip('/')}/RELEASE", stamp_bytes, 0o100644),
        (f"usr/share/doc/{DEB_NAME}/RELEASE", stamp_bytes, 0o100644),
    ]

    tarball = build_tarball(
        root,
        dest_dir=dest,
        sync_state_path=sync_state_path,
        extra_members=tar_extra,
    )
    deb = build_deb(
        root,
        dest_dir=dest,
        sync_state_path=sync_state_path,
        extra_members=deb_extra,
    )
    units = write_systemd_units(dest, project_root=root)
    hashed = [deb, tarball, stamp_file, *units]
    if launcher_path is not None:
        hashed.append(launcher_path)
    sums = write_sha256sums(hashed, dest / "SHA256SUMS")

    snap = {
        "release_version": version,
        "release_build_ts": float(ts),
        "cflags": cflags,
        "ldflags": ldflags,
        "stripped": stripped,
        "reproducible": True,
        "artifacts": {
            "deb": str(deb),
            "tarball": str(tarball),
            "systemd": [str(p) for p in units],
            "launcher": str(launcher_path) if launcher_path is not None else None,
            "stamp": str(stamp_file),
            "sha256sums": str(sums),
        },
    }
    persist_release_snapshot(snap, project_root=root, sync_state_path=sync_state_path)
    return snap
