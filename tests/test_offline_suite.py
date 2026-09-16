"""Offline suite snapshot helpers — no mixer, no JACK."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.dashboard.reader import build_view
from src.testing.suite import (
    OFFLINE_MODULES,
    classify_test_health,
    format_summary,
    persist_test_snapshot,
)


class ClassifyHealthTests(unittest.TestCase):
    def test_off_ok_warn_fail(self) -> None:
        self.assertEqual(classify_test_health(run=0, failed=0, errors=0, skipped=0), "off")
        self.assertEqual(classify_test_health(run=4, failed=0, errors=0, skipped=0), "ok")
        self.assertEqual(classify_test_health(run=4, failed=0, errors=0, skipped=1), "warn")
        self.assertEqual(classify_test_health(run=4, failed=1, errors=0, skipped=0), "fail")
        self.assertEqual(classify_test_health(run=4, failed=0, errors=1, skipped=0), "fail")


class PersistSnapshotTests(unittest.TestCase):
    def test_persist_sidecar_and_merge_active_sync(self) -> None:
        snap = {
            "last_test_run": 123.5,
            "test_health": "ok",
            "tests_run": 4,
            "tests_failed": 0,
            "tests_errors": 0,
            "tests_skipped": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(os.environ, {"XAIR_TEST_STATE_PATH": ""}, clear=False):
                path = persist_test_snapshot(snap, project_root=root)
                data = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(data["last_test_run"], 123.5)
                self.assertEqual(data["test_health"], "ok")
                self.assertIn("last_test_run", format_summary(data))
                self.assertIn("health=ok", format_summary(data))

                inactive = root / ".xair_sync_state.json"
                inactive.write_text(json.dumps({"schema": 1, "active": False}), encoding="utf-8")
                persist_test_snapshot(snap, project_root=root, sync_state_path=inactive)
                self.assertNotIn("test", json.loads(inactive.read_text(encoding="utf-8")))

                active = root / ".xair_sync_state.json"
                active.write_text(
                    json.dumps({"schema": 1, "active": True, "report": {"channels": []}}),
                    encoding="utf-8",
                )
                persist_test_snapshot(snap, project_root=root, sync_state_path=active)
                merged = json.loads(active.read_text(encoding="utf-8"))
                self.assertEqual(merged["report"]["test"]["test_health"], "ok")
                self.assertEqual(merged["report"]["test"]["last_test_run"], 123.5)


class DashboardTestBlockTests(unittest.TestCase):
    def test_view_merges_test_block_and_sidecar(self) -> None:
        payload = {
            "schema": 1,
            "active": True,
            "report_interval_sec": 5.0,
            "report": {
                "level_tolerance_db": 6.0,
                "test": {
                    "last_test_run": 1.0,
                    "test_health": "warn",
                    "tests_run": 2,
                    "tests_failed": 0,
                    "tests_errors": 0,
                    "tests_skipped": 1,
                },
                "channels": [],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xair_sync_state.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            view = build_view(path, payload, now=path.stat().st_mtime)
            self.assertEqual(view.tests["test_health"], "warn")
            self.assertEqual(view.tests["last_test_run"], 1.0)

            side = Path(tmp) / ".xair_test.json"
            side.write_text(
                json.dumps(
                    {
                        "last_test_run": 9.0,
                        "test_health": "ok",
                        "tests_run": 10,
                        "tests_failed": 0,
                        "tests_errors": 0,
                        "tests_skipped": 0,
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"XAIR_TEST_STATE_PATH": ""}, clear=False):
                view2 = build_view(path, payload, now=path.stat().st_mtime)
            self.assertEqual(view2.tests["test_health"], "ok")
            self.assertEqual(view2.tests["last_test_run"], 9.0)


class SuiteIsolationTests(unittest.TestCase):
    def test_modules_do_not_include_receiver(self) -> None:
        self.assertNotIn("tests.test_receiver", OFFLINE_MODULES)
        self.assertTrue(all("receiver.py" not in n for n in OFFLINE_MODULES))
        self.assertIn("tests.test_port_names", OFFLINE_MODULES)
        self.assertIn("tests.test_jack_named_output", OFFLINE_MODULES)
        self.assertIn("tests.test_jack_graph", OFFLINE_MODULES)
        self.assertIn("tests.test_reorder", OFFLINE_MODULES)
        self.assertIn("tests.test_writeback", OFFLINE_MODULES)
        self.assertIn("tests.test_osc_launcher", OFFLINE_MODULES)
        self.assertIn("tests.test_transport_loop", OFFLINE_MODULES)
        self.assertIn("tests.test_package", OFFLINE_MODULES)
        self.assertIn("tests.test_release", OFFLINE_MODULES)
        self.assertIn("tests.test_integration", OFFLINE_MODULES)

    def test_transport_loop_does_not_load_portaudio(self) -> None:
        import importlib

        sys.modules.pop("src.network_receiver.receiver", None)
        sys.modules.pop("src.usb_capture.capture", None)
        sys.modules.pop("sounddevice", None)
        name = "tests.test_transport_loop"
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
        self.assertNotIn("src.network_receiver.receiver", sys.modules)
        self.assertNotIn("src.usb_capture.capture", sys.modules)
        self.assertNotIn("sounddevice", sys.modules)

    def test_reorder_tests_do_not_load_receiver_py(self) -> None:
        import importlib

        sys.modules.pop("src.network_receiver.receiver", None)
        name = "tests.test_reorder"
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            importlib.import_module(name)
        self.assertNotIn("src.network_receiver.receiver", sys.modules)


if __name__ == "__main__":
    unittest.main()
