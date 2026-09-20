"""Deb/tarball packaging — no mixer, no JACK, no dpkg required."""

from __future__ import annotations

import gzip
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.dashboard.reader import build_view
from src.package.builder import DEB_DEPENDS, build_deb, build_tarball, debian_control
from src.package.meta import persist_package_snapshot, read_version


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _parse_ar(blob: bytes) -> dict[str, bytes]:
    if not blob.startswith(b"!<arch>\n"):
        raise AssertionError("not a GNU ar archive")
    off = 8
    out: dict[str, bytes] = {}
    while off + 60 <= len(blob):
        hdr = blob[off : off + 60]
        off += 60
        name = hdr[0:16].decode("ascii").strip()
        size = int(hdr[48:58].decode("ascii").strip() or "0")
        data = blob[off : off + size]
        off += size
        if size % 2 == 1:
            off += 1
        out[name] = data
    return out


class VersionTests(unittest.TestCase):
    def test_read_version_file(self) -> None:
        self.assertEqual(read_version(_repo_root()), "0.1.1")


class ControlTests(unittest.TestCase):
    def test_depends_include_python_and_portaudio(self) -> None:
        text = debian_control("0.1.1")
        self.assertIn("python3 (>= 3.10)", text)
        self.assertIn("libportaudio2", text)
        self.assertIn("python3-jack-client", text)
        self.assertNotIn("python3-osc", text)
        self.assertNotIn("python3-sounddevice", text)
        for dep in DEB_DEPENDS:
            self.assertIn(dep.split()[0], text)


class TarballTests(unittest.TestCase):
    def test_tarball_contains_cli_unit_and_stamp(self) -> None:
        root = _repo_root()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            snap_path = dest / "pkg.json"
            with mock.patch.dict(
                os.environ,
                {
                    "XAIR_PACKAGE_STATE_PATH": str(snap_path),
                    "SOURCE_DATE_EPOCH": "1700000000",
                    "XAIR_PACKAGE_VERSION": "",
                },
                clear=False,
            ):
                tgz = build_tarball(root, dest_dir=dest)
            self.assertTrue(tgz.is_file())
            self.assertNotIn(".venv", tgz.name)
            names: list[str] = []
            with tarfile.open(tgz, "r:gz") as tar:
                names = [m.name for m in tar.getmembers()]
                stamp = tar.extractfile(
                    [n for n in names if n.endswith(".xair_package.json")][0]
                )
                assert stamp is not None
                meta = json.loads(stamp.read().decode("utf-8"))
            self.assertTrue(any(n.endswith("src/cli/main.py") for n in names))
            self.assertTrue(any(n.endswith("scripts/xair-network-bridge") for n in names))
            self.assertTrue(any(n.endswith("packaging/systemd/xair-network-bridge-client.service") for n in names))
            self.assertTrue(any(n.endswith("INSTALL.txt") for n in names))
            self.assertTrue(any(n.endswith("pythonosc/__init__.py") for n in names))
            self.assertTrue(any(n.endswith("sounddevice.py") for n in names))
            self.assertTrue(any(n.endswith("_sounddevice.py") for n in names))
            self.assertFalse(any(".venv" in n for n in names))
            self.assertEqual(meta["package_version"], "0.1.1")
            self.assertEqual(meta["package_build_ts"], 1700000000.0)
            side = json.loads(snap_path.read_text(encoding="utf-8"))
            self.assertEqual(side["package_version"], "0.1.1")
            self.assertIsNotNone(side["package_build_ts"])


class DebTests(unittest.TestCase):
    def test_deb_has_entrypoint_units_and_depends(self) -> None:
        root = _repo_root()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            snap_path = dest / "pkg.json"
            with mock.patch.dict(
                os.environ,
                {
                    "XAIR_PACKAGE_STATE_PATH": str(snap_path),
                    "SOURCE_DATE_EPOCH": "1700000000",
                },
                clear=False,
            ):
                deb = build_deb(root, dest_dir=dest)
            self.assertTrue(deb.name.endswith("_0.1.1_all.deb"))
            members = _parse_ar(deb.read_bytes())
            self.assertIn("debian-binary", members)
            self.assertEqual(members["debian-binary"].startswith(b"2.0"), True)
            ctl = gzip.decompress(members["control.tar.gz"])
            data = gzip.decompress(members["data.tar.gz"])
            with tarfile.open(fileobj=io.BytesIO(ctl), mode="r:") as tar:
                control = tar.extractfile("./control")
                postinst = tar.extractfile("./postinst")
                assert control is not None and postinst is not None
                ctext = control.read().decode("utf-8")
                self.assertIn("Depends:", ctext)
                self.assertIn("python3-numpy", ctext)
                self.assertIn("#!/bin/sh", postinst.read().decode("utf-8"))
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
                names = [m.name for m in tar.getmembers()]
            self.assertIn("usr/bin/xair-network-bridge", names)
            self.assertTrue(any(n.endswith("src/cli/main.py") for n in names))
            self.assertTrue(any(n.endswith("pythonosc/__init__.py") for n in names))
            self.assertTrue(any(n.endswith("sounddevice.py") for n in names))
            self.assertTrue(any(n.endswith("_sounddevice.py") for n in names))
            self.assertIn("usr/lib/systemd/user/xair-network-bridge-client.service", names)
            self.assertIn("usr/lib/systemd/user/xair-network-bridge-server.service", names)
            unit = None
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
                f = tar.extractfile("usr/lib/systemd/user/xair-network-bridge-client.service")
                assert f is not None
                unit = f.read().decode("utf-8")
            self.assertIn("ExecStart=/usr/bin/xair-network-bridge xair_network_bridge_client", unit)
            self.assertIn("ExecReload=/bin/kill -HUP $MAINPID", unit)
            self.assertIn("KillSignal=SIGTERM", unit)
            self.assertNotIn(".venv", "\n".join(names))
            side = json.loads(snap_path.read_text(encoding="utf-8"))
            self.assertEqual(side["package_format"], "deb")


class SnapshotTests(unittest.TestCase):
    def test_dashboard_and_sync_merge(self) -> None:
        snap = {"package_version": "0.1.1", "package_build_ts": 9.0}
        payload = {
            "schema": 1,
            "active": True,
            "report_interval_sec": 5.0,
            "report": {"level_tolerance_db": 6.0, "package": snap, "channels": []},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".xair_sync_state.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            view = build_view(path, payload, now=path.stat().st_mtime)
            self.assertEqual(view.package["package_version"], "0.1.1")
            self.assertEqual(view.package["package_build_ts"], 9.0)
            with mock.patch.dict(os.environ, {"XAIR_PACKAGE_STATE_PATH": ""}, clear=False):
                persist_package_snapshot(snap, project_root=root, sync_state_path=path)
            merged = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(merged["report"]["package"]["package_version"], "0.1.1")


if __name__ == "__main__":
    unittest.main()
