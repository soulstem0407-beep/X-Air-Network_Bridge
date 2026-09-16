"""Release pipeline — no mixer, no JACK, no transport imports."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.dashboard.reader import build_view
from src.package.meta import PACKAGE_NAME
from src.release.meta import persist_release_snapshot
from src.release.pipeline import build_release, format_release_stamp


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


class StampTests(unittest.TestCase):
    def test_stamp_has_version_and_flags(self) -> None:
        text = format_release_stamp(
            version="0.1.0",
            build_ts=1700000000.0,
            cflags="-O2 -g0",
            ldflags="-s",
            stripped=True,
        )
        self.assertIn("release_version=0.1.0", text)
        self.assertIn("release_build_ts=1700000000.0", text)
        self.assertIn("cflags=-O2 -g0", text)
        self.assertIn("stripped_binaries=1", text)
        self.assertIn("reproducible=1", text)


class PipelineTests(unittest.TestCase):
    def test_release_emits_deb_tar_units_and_snapshot(self) -> None:
        root = _repo_root()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "out"
            snap_path = Path(tmp) / "rel.json"
            pkg_path = Path(tmp) / "pkg.json"
            with mock.patch.dict(
                os.environ,
                {
                    "XAIR_RELEASE_STATE_PATH": str(snap_path),
                    "XAIR_PACKAGE_STATE_PATH": str(pkg_path),
                    "SOURCE_DATE_EPOCH": "1700000000",
                    "PYTHONHASHSEED": "0",
                    "TZ": "UTC",
                    "LC_ALL": "C",
                },
                clear=False,
            ):
                snap = build_release(root, dest_dir=dest)
            self.assertEqual(snap["release_version"], "0.1.0")
            self.assertEqual(snap["release_build_ts"], 1700000000.0)
            self.assertTrue(snap["reproducible"])
            self.assertIn("-O2", str(snap["cflags"]))
            arts = snap["artifacts"]
            deb = Path(arts["deb"])
            tgz = Path(arts["tarball"])
            stamp = Path(arts["stamp"])
            self.assertTrue(deb.is_file())
            self.assertTrue(tgz.is_file())
            self.assertTrue(stamp.is_file())
            self.assertIn("release_version=0.1.0", stamp.read_text(encoding="utf-8"))
            units = [Path(p) for p in arts["systemd"]]
            self.assertEqual(len(units), 2)
            for u in units:
                self.assertTrue(u.is_file())
                text = u.read_text(encoding="utf-8")
                self.assertIn("ExecStart=/usr/bin/xair-network-bridge xair_network_bridge_", text)
                self.assertIn("KillSignal=SIGTERM", text)
            with tarfile.open(tgz, "r:gz") as tar:
                names = [m.name for m in tar.getmembers()]
                relf = tar.extractfile(
                    [n for n in names if n.endswith("/RELEASE")][0]
                )
                assert relf is not None
                body = relf.read().decode("utf-8")
            self.assertIn(f"{PACKAGE_NAME}-0.1.0/RELEASE", names)
            self.assertIn("release_version=0.1.0", body)
            members = _parse_ar(deb.read_bytes())
            data = gzip.decompress(members["data.tar.gz"])
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as tar:
                dnames = [m.name for m in tar.getmembers()]
            self.assertTrue(any(n.endswith("/RELEASE") for n in dnames))
            side = json.loads(snap_path.read_text(encoding="utf-8"))
            self.assertEqual(side["release_version"], "0.1.0")
            self.assertEqual(side["release_build_ts"], 1700000000.0)
            launcher = arts.get("launcher")
            if launcher:
                lp = Path(launcher)
                self.assertTrue(lp.is_file())
                blob = lp.read_bytes()
                self.assertTrue(blob.startswith(b"\x7fELF"))
                self.assertIn(b"XAIR_RELEASE_VERSION=0.1.0", blob)

    def test_same_epoch_is_bit_reproducible(self) -> None:
        root = _repo_root()
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a"
            b = Path(tmp) / "b"
            env = {
                "XAIR_RELEASE_STATE_PATH": str(Path(tmp) / "rel.json"),
                "XAIR_PACKAGE_STATE_PATH": str(Path(tmp) / "pkg.json"),
                "SOURCE_DATE_EPOCH": "1700000000",
                "PYTHONHASHSEED": "0",
                "TZ": "UTC",
                "LC_ALL": "C",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                sa = build_release(root, dest_dir=a)
            env["XAIR_RELEASE_STATE_PATH"] = str(Path(tmp) / "rel2.json")
            env["XAIR_PACKAGE_STATE_PATH"] = str(Path(tmp) / "pkg2.json")
            with mock.patch.dict(os.environ, env, clear=False):
                sb = build_release(root, dest_dir=b)
            da = Path(sa["artifacts"]["deb"]).read_bytes()
            db = Path(sb["artifacts"]["deb"]).read_bytes()
            ta = Path(sa["artifacts"]["tarball"]).read_bytes()
            tb = Path(sb["artifacts"]["tarball"]).read_bytes()
            self.assertEqual(hashlib.sha256(da).digest(), hashlib.sha256(db).digest())
            self.assertEqual(hashlib.sha256(ta).digest(), hashlib.sha256(tb).digest())
            ua = Path(sa["artifacts"]["systemd"][0]).read_text(encoding="utf-8")
            ub = Path(sb["artifacts"]["systemd"][0]).read_text(encoding="utf-8")
            self.assertEqual(ua, ub)


class SnapshotTests(unittest.TestCase):
    def test_dashboard_and_sync_merge(self) -> None:
        snap = {"release_version": "0.1.0", "release_build_ts": 11.0}
        payload = {
            "schema": 1,
            "active": True,
            "report_interval_sec": 5.0,
            "report": {"level_tolerance_db": 6.0, "release": snap, "channels": []},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / ".xair_sync_state.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            view = build_view(path, payload, now=path.stat().st_mtime)
            self.assertEqual(view.release["release_version"], "0.1.0")
            self.assertEqual(view.release["release_build_ts"], 11.0)
            with mock.patch.dict(os.environ, {"XAIR_RELEASE_STATE_PATH": ""}, clear=False):
                persist_release_snapshot(snap, project_root=root, sync_state_path=path)
            merged = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(merged["report"]["release"]["release_version"], "0.1.0")


if __name__ == "__main__":
    unittest.main()
