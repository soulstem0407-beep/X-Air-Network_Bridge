"""Build .deb and source tarball. Does not import receiver or change transport."""
from __future__ import annotations

import gzip
import hashlib
import io
import os
import stat
import sys
import tarfile
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from ..service.systemd import render_unit
from .meta import (
    DEB_NAME,
    INSTALL_BIN,
    INSTALL_LIBDIR,
    INSTALL_USER_UNITDIR,
    PACKAGE_NAME,
    now_ts,
    persist_package_snapshot,
    read_version,
)

SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    "recordings",
    "dist",
    ".mypy_cache",
    ".pytest_cache",
    "node_modules",
}
SKIP_FILE_NAMES = {
    ".env",
    ".xair_runtime.json",
    ".xair_sync_state.json",
    ".xair_discovery.json",
    ".xair_record.json",
    ".xair_record_cmd",
    ".xair_test.json",
    ".xair_service.json",
    ".xair_package.json",
    ".xair_release.json",
}
INCLUDE_TOP = (
    "src",
    "tests",
    "packaging",
    "scripts",
    "launchers",
    "docs",
    "requirements.txt",
    "VERSION",
    ".env.example",
    "pyproject.toml",
    "README.md",
    "LICENSE",
    "INSTRUCCIONES_PARA_DUMMIES.md",
    "templates",
    "reaper-osc",
    "bitwig-osc",
    "xair_network_bridge_doctor.sh",
)

# Apt names that exist on Ubuntu 24.04. python-osc and sounddevice are not
# packaged there; they are copied from the build environment onto PYTHONPATH.
DEB_DEPENDS = (
    "python3 (>= 3.10)",
    "python3-numpy",
    "python3-click",
    "python3-dotenv",
    "python3-soundfile",
    "python3-jack-client",
    "libportaudio2",
    "libsndfile1",
)
DEB_RECOMMENDS = ("libopus0",)
BUNDLE_PY_PACKAGES = ("pythonosc", "sounddevice")


def default_dist_dir(project_root: Path) -> Path:
    return Path(project_root) / "dist"


def _mode_file(path: Path) -> int:
    if path.suffix in {".sh"} or path.name in {"xair-network-bridge", "postinst", "prerm"}:
        return 0o100755
    try:
        st = path.stat().st_mode
        if st & stat.S_IXUSR:
            return 0o100755
    except OSError:
        pass
    return 0o100644


def iter_source_files(project_root: Path) -> Iterable[Tuple[str, Path]]:
    root = Path(project_root).resolve()
    for name in INCLUDE_TOP:
        src = root / name
        if not src.exists():
            continue
        if src.is_file():
            yield name, src
            continue
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
            base = Path(dirpath)
            for fn in sorted(filenames):
                if fn in SKIP_FILE_NAMES or fn.endswith(".pyc"):
                    continue
                full = base / fn
                rel = full.relative_to(root).as_posix()
                yield rel, full


def find_dist_package(name: str) -> Optional[Path]:
    """Locate an installed distro/venv package without importing it."""
    fallback: Optional[Path] = None
    for entry in sys.path:
        if not entry:
            continue
        cand = Path(entry) / name
        if not cand.is_dir() or not (cand / "__init__.py").is_file():
            continue
        posix = Path(entry).as_posix()
        if "site-packages" in posix or "dist-packages" in posix:
            return cand
        if fallback is None:
            fallback = cand
    return fallback


def find_dist_module(name: str) -> Optional[Path]:
    """Locate a top-level ``name.py`` (e.g. sounddevice) without importing it."""
    fallback: Optional[Path] = None
    for entry in sys.path:
        if not entry:
            continue
        cand = Path(entry) / f"{name}.py"
        if not cand.is_file():
            continue
        posix = Path(entry).as_posix()
        if "site-packages" in posix or "dist-packages" in posix:
            return cand
        if fallback is None:
            fallback = cand
    return fallback


def iter_bundle_files() -> Iterable[Tuple[str, Path]]:
    for name in BUNDLE_PY_PACKAGES:
        root = find_dist_package(name)
        if root is not None:
            parent = root.parent
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
                for fn in sorted(filenames):
                    if fn.endswith(".pyc"):
                        continue
                    full = Path(dirpath) / fn
                    yield full.relative_to(parent).as_posix(), full
            continue
        py = find_dist_module(name)
        if py is None:
            continue
        yield py.name, py
        sibling = py.parent / f"_{name}.py"
        if sibling.is_file():
            yield sibling.name, sibling


def source_tree_wrapper() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"
cd "$ROOT"
if [[ -x "$ROOT/.venv/bin/python3" && -z "${XAIR_PYTHON:-}" ]]; then
  PY="$ROOT/.venv/bin/python3"
else
  PY="${XAIR_PYTHON:-python3}"
fi
exec "$PY" -m src.cli "$@"
"""


def installed_wrapper() -> str:
    return f"""#!/bin/sh
set -e
ROOT="{INSTALL_LIBDIR}"
export PYTHONPATH="$ROOT${{PYTHONPATH:+:$PYTHONPATH}}"
cd "$ROOT"
exec python3 -m src.cli "$@"
"""


def debian_control(version: str) -> str:
    depends = ", ".join(DEB_DEPENDS)
    recommends = ", ".join(DEB_RECOMMENDS)
    return (
        f"Package: {DEB_NAME}\n"
        f"Version: {version}\n"
        "Section: sound\n"
        "Priority: optional\n"
        "Architecture: all\n"
        "Maintainer: X-Air Network Bridge <noreply@localhost>\n"
        f"Depends: {depends}\n"
        f"Recommends: {recommends}\n"
        "Description: X-Air Network Bridge (X18 USB to UDP)\n"
        " Multichannel LAN bridge: capture from a Behringer X AIR mixer over USB,\n"
        " send XBRI UDP, play back on a virtual JACK/PipeWire device. Optional OSC\n"
        " control, FEC, and Opus. Runtime behaviour matches the source tree CLI.\n"
        " python-osc and sounddevice are bundled next to src/ (no Ubuntu packages).\n"
    )


def debian_postinst() -> str:
    return f"""#!/bin/sh
set -e
ROOT="{INSTALL_LIBDIR}"
if [ ! -f "$ROOT/.env" ] && [ -f "$ROOT/.env.example" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
fi
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  systemctl daemon-reload >/dev/null 2>&1 || true
fi
exit 0
"""


def debian_prerm() -> str:
    return """#!/bin/sh
set -e
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user stop xair-network-bridge-client.service xair-network-bridge-server.service >/dev/null 2>&1 || true
  systemctl stop xair-network-bridge-client.service xair-network-bridge-server.service >/dev/null 2>&1 || true
fi
exit 0
"""


def debian_copyright() -> str:
    return (
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        f"Upstream-Name: {PACKAGE_NAME}\n"
        "\n"
        "Files: *\n"
        "Copyright: X-Air Network Bridge contributors\n"
        "License: GPL-3.0-or-later\n"
        "\n"
        "License: GPL-3.0-or-later\n"
        " This program is free software: you can redistribute it and/or modify\n"
        " it under the terms of the GNU General Public License as published by\n"
        " the Free Software Foundation, either version 3 of the License, or\n"
        " (at your option) any later version.\n"
    )


def _mtime(ts: float) -> int:
    return int(ts)


def _add_tar_bytes(
    tar: tarfile.TarFile,
    name: str,
    data: bytes,
    *,
    mtime: int,
    mode: int = 0o100644,
) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.mtime = mtime
    info.mode = mode & 0o7777
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "root"
    tar.addfile(info, io.BytesIO(data))


def _tar_gz(members: List[Tuple[str, bytes, int]], *, mtime: int) -> bytes:
    raw = io.BytesIO()
    ordered = sorted(members, key=lambda t: t[0])
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name, data, mode in ordered:
            _add_tar_bytes(tar, name, data, mtime=mtime, mode=mode)
    gz = io.BytesIO()
    with gzip.GzipFile(fileobj=gz, mode="wb", mtime=mtime) as gzf:
        gzf.write(raw.getvalue())
    return gz.getvalue()


def _ar_header(name: str, data: bytes, mtime: int, mode: int = 0o100644) -> bytes:
    n = name if name.endswith("/") or len(name) < 16 else name[:15]
    hdr = (
        f"{n:<16}{mtime:<12}{0:<6}{0:<6}{mode:<8o}{len(data):<10}`\n"
    ).encode("ascii")
    blob = hdr + data
    if len(data) % 2 == 1:
        blob += b"\n"
    return blob


def write_deb_ar(path: Path, *, debian_binary: bytes, control: bytes, data: bytes, mtime: int) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [
        _ar_header("debian-binary", debian_binary, mtime),
        _ar_header("control.tar.gz", control, mtime),
        _ar_header("data.tar.gz", data, mtime),
    ]
    path.write_bytes(b"!<arch>\n" + b"".join(parts))


def _payload_members(
    project_root: Path,
    *,
    version: str,
    build_ts: float,
    prefix: str,
    wrapper_rel: Optional[str],
    wrapper_text: Optional[str],
    extra_members: Optional[List[Tuple[str, bytes, int]]] = None,
) -> List[Tuple[str, bytes, int]]:
    mtime = _mtime(build_ts)
    out: List[Tuple[str, bytes, int]] = []
    stamp = (
        '{"package_version": "%s", "package_build_ts": %s, "package_format": "%s"}\n'
        % (version, repr(float(build_ts)), "deb" if prefix.startswith("usr/") else "tar")
    ).encode("ascii")
    for rel, full in iter_source_files(project_root):
        data = full.read_bytes()
        mode = _mode_file(full)
        name = f"{prefix}{rel}" if prefix else rel
        out.append((name, data, mode))
    have = {n for n, _d, _m in out}
    for rel, full in iter_bundle_files():
        name = f"{prefix}{rel}" if prefix else rel
        if name in have:
            continue
        out.append((name, full.read_bytes(), _mode_file(full)))
        have.add(name)
    stamp_name = f"{prefix}.xair_package.json" if prefix else ".xair_package.json"
    out.append((stamp_name, stamp, 0o100644))
    have.add(stamp_name)
    if wrapper_rel and wrapper_text is not None:
        out.append((wrapper_rel, wrapper_text.encode("utf-8"), 0o100755))
        have.add(wrapper_rel)
    if extra_members:
        for name, data, mode in extra_members:
            if name in have:
                continue
            out.append((name, data, mode))
            have.add(name)
    out.sort(key=lambda t: t[0])
    return out


def installed_unit_text(role: str, *, project_root: Path) -> str:
    return render_unit(
        role,
        project_root=Path(INSTALL_LIBDIR),
        python=Path("/usr/bin/python3"),
        scope="user",
        exec_start=f"{INSTALL_BIN} xair_network_bridge_{role}",
    )


def build_tarball(
    project_root: Path,
    *,
    dest_dir: Optional[Path] = None,
    sync_state_path: Optional[Path] = None,
    extra_members: Optional[List[Tuple[str, bytes, int]]] = None,
) -> Path:
    root = Path(project_root).resolve()
    version = read_version(root)
    ts = now_ts()
    dest = Path(dest_dir) if dest_dir is not None else default_dist_dir(root)
    dest.mkdir(parents=True, exist_ok=True)
    base = f"{PACKAGE_NAME}-{version}"
    prefix = f"{base}/"
    members = _payload_members(
        root,
        version=version,
        build_ts=ts,
        prefix=prefix,
        wrapper_rel=None,
        wrapper_text=None,
        extra_members=extra_members,
    )
    if not any(name.endswith("scripts/xair-network-bridge") for name, _d, _m in members):
        members.append(
            (f"{prefix}scripts/xair-network-bridge", source_tree_wrapper().encode("utf-8"), 0o100755)
        )
    install_txt = (
        f"{PACKAGE_NAME} {version}\n\n"
        "Generic Linux tarball (non-Debian).\n\n"
        f"  tar xf {base}.tar.gz && cd {base}\n"
        "  bash scripts/install.sh\n"
        "  cp .env.example .env   # edit mixer / peer addresses\n"
        "  ./scripts/xair-network-bridge status\n"
        "  ./scripts/xair-network-bridge install-service --role client\n"
    ).encode("utf-8")
    members.append((f"{prefix}INSTALL.txt", install_txt, 0o100644))
    mtime = _mtime(ts)
    blob = _tar_gz(members, mtime=mtime)
    out = dest / f"{base}.tar.gz"
    out.write_bytes(blob)
    snap = {
        "package_version": version,
        "package_build_ts": float(ts),
        "package_format": "tar",
        "artifact": str(out),
    }
    persist_package_snapshot(snap, project_root=root, sync_state_path=sync_state_path)
    return out


def build_deb(
    project_root: Path,
    *,
    dest_dir: Optional[Path] = None,
    sync_state_path: Optional[Path] = None,
    extra_members: Optional[List[Tuple[str, bytes, int]]] = None,
) -> Path:
    root = Path(project_root).resolve()
    version = read_version(root)
    ts = now_ts()
    mtime = _mtime(ts)
    dest = Path(dest_dir) if dest_dir is not None else default_dist_dir(root)
    dest.mkdir(parents=True, exist_ok=True)

    lib_prefix = INSTALL_LIBDIR.lstrip("/") + "/"
    data_members = _payload_members(
        root,
        version=version,
        build_ts=ts,
        prefix=lib_prefix,
        wrapper_rel=INSTALL_BIN.lstrip("/"),
        wrapper_text=installed_wrapper(),
        extra_members=extra_members,
    )
    for role in ("server", "client"):
        unit_name = f"xair-network-bridge-{role}.service"
        text = installed_unit_text(role, project_root=root)
        data_members.append(
            (
                f"{INSTALL_USER_UNITDIR.lstrip('/')}/{unit_name}",
                text.encode("utf-8"),
                0o100644,
            )
        )
    readme = root / "docs" / "README.md"
    if readme.is_file():
        data_members.append(
            (
                f"usr/share/doc/{DEB_NAME}/README.md",
                readme.read_bytes(),
                0o100644,
            )
        )
    data_members.append(
        (
            f"usr/share/doc/{DEB_NAME}/copyright",
            debian_copyright().encode("utf-8"),
            0o100644,
        )
    )

    md5_lines: List[str] = []
    for name, blob, _mode in data_members:
        md5_lines.append(f"{hashlib.md5(blob).hexdigest()}  {name}")
    md5_lines.sort()
    control_members = [
        ("./control", debian_control(version).encode("utf-8"), 0o100644),
        ("./md5sums", ("\n".join(md5_lines) + "\n").encode("ascii"), 0o100644),
        ("./postinst", debian_postinst().encode("utf-8"), 0o100755),
        ("./prerm", debian_prerm().encode("utf-8"), 0o100755),
    ]
    control_gz = _tar_gz(control_members, mtime=mtime)
    data_gz = _tar_gz(data_members, mtime=mtime)
    out = dest / f"{DEB_NAME}_{version}_all.deb"
    write_deb_ar(
        out,
        debian_binary=b"2.0\n",
        control=control_gz,
        data=data_gz,
        mtime=mtime,
    )
    snap = {
        "package_version": version,
        "package_build_ts": float(ts),
        "package_format": "deb",
        "artifact": str(out),
    }
    persist_package_snapshot(snap, project_root=root, sync_state_path=sync_state_path)
    return out
