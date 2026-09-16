"""systemd unit render/install — no mixer, no JACK, no systemctl required."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.dashboard.reader import build_view
from src.service.systemd import (
    UNIT_FILES,
    expand_roles,
    install_service,
    persist_service_snapshot,
    remove_service,
    render_unit,
)


class RoleTests(unittest.TestCase):
    def test_expand(self) -> None:
        self.assertEqual(expand_roles("server"), ["server"])
        self.assertEqual(expand_roles("both"), ["server", "client"])
        with self.assertRaises(ValueError):
            expand_roles("audio")


class RenderUnitTests(unittest.TestCase):
    def test_client_user_has_reload_and_term(self) -> None:
        text = render_unit(
            "client",
            project_root=Path("/tmp/xair-root"),
            python=Path("/tmp/xair-root/.venv/bin/python3"),
            scope="user",
        )
        self.assertIn("ExecStart=/tmp/xair-root/.venv/bin/python3 -m src.cli xair_network_bridge_client", text)
        self.assertIn("ExecReload=/bin/kill -HUP $MAINPID", text)
        self.assertIn("KillSignal=SIGTERM", text)
        self.assertIn("WantedBy=default.target", text)
        self.assertIn("PYTHONPATH=/tmp/xair-root", text)
        self.assertNotIn("__ROOT__", text)
        self.assertNotIn("start-server", text)

    def test_server_system_wanted_by_multi_user(self) -> None:
        text = render_unit(
            "server",
            project_root=Path("/opt/bridge"),
            python=Path("/opt/bridge/.venv/bin/python3"),
            scope="system",
            extra_user="studio",
        )
        self.assertIn("xair_network_bridge_server", text)
        self.assertIn("WantedBy=multi-user.target", text)
        self.assertIn("User=studio", text)
        self.assertIn("KillSignal=SIGTERM", text)


class InstallRemoveTests(unittest.TestCase):
    def test_install_writes_units_and_snapshot(self) -> None:
        calls: list[list[str]] = []

        def runner(cmd):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(list(cmd), 0, "", "")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dest = root / "units"
            with mock.patch.dict(os.environ, {"XAIR_SERVICE_STATE_PATH": ""}, clear=False):
                snap = install_service(
                    project_root=root,
                    role="client",
                    scope="user",
                    python=Path("/usr/bin/python3"),
                    unit_dir=dest,
                    apply=True,
                    start=True,
                    runner=runner,
                )
            unit = dest / UNIT_FILES["client"]
            self.assertTrue(unit.is_file())
            body = unit.read_text(encoding="utf-8")
            self.assertIn("xair_network_bridge_client", body)
            self.assertIn("ExecReload", body)
            self.assertEqual(snap["last_service_action"], "install")
            self.assertTrue(snap["service_enabled"])
            state = json.loads((root / ".xair_service.json").read_text(encoding="utf-8"))
            self.assertEqual(state["last_service_action"], "install")
            self.assertTrue(any("daemon-reload" in c for c in calls))
            self.assertTrue(any("enable" in c for c in calls))
            self.assertTrue(any("start" in c for c in calls))

            snap2 = remove_service(
                project_root=root,
                role="client",
                scope="user",
                unit_dir=dest,
                apply=True,
                runner=runner,
            )
            self.assertFalse(unit.is_file())
            self.assertEqual(snap2["last_service_action"], "remove")
            self.assertFalse(snap2["service_enabled"])

    def test_persist_merges_active_sync(self) -> None:
        snap = {
            "service_enabled": True,
            "last_service_action": "install",
            "last_service_ts": 1.0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sync = root / ".xair_sync_state.json"
            sync.write_text(
                json.dumps({"schema": 1, "active": True, "report": {"channels": []}}),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"XAIR_SERVICE_STATE_PATH": ""}, clear=False):
                persist_service_snapshot(snap, project_root=root, sync_state_path=sync)
            merged = json.loads(sync.read_text(encoding="utf-8"))
            self.assertTrue(merged["report"]["service"]["service_enabled"])
            self.assertEqual(merged["report"]["service"]["last_service_action"], "install")


class DashboardServiceTests(unittest.TestCase):
    def test_view_merges_service_block(self) -> None:
        payload = {
            "schema": 1,
            "active": True,
            "report_interval_sec": 5.0,
            "report": {
                "level_tolerance_db": 6.0,
                "service": {
                    "service_enabled": True,
                    "last_service_action": "install",
                },
                "channels": [],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xair_sync_state.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            view = build_view(path, payload, now=path.stat().st_mtime)
            self.assertTrue(view.service["service_enabled"])
            self.assertEqual(view.service["last_service_action"], "install")


if __name__ == "__main__":
    unittest.main()
