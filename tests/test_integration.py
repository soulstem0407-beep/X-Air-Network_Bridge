"""Integration snapshot / dashboard consistency — no mixer, no JACK, no receiver.py."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.dashboard.reader import build_view, snapshot_age_sec, snapshot_fresh, view_as_dict
from src.sync.integration import classify_integration_health, integration_snapshot


class ClassifyIntegrationTests(unittest.TestCase):
    def test_inactive_is_off(self) -> None:
        self.assertEqual(
            classify_integration_health(
                active=False,
                metrics={"rx_health": "ok"},
            ),
            "off",
        )

    def test_live_unused_modules_ok(self) -> None:
        self.assertEqual(classify_integration_health(active=True), "ok")

    def test_fail_beats_warn(self) -> None:
        self.assertEqual(
            classify_integration_health(
                active=True,
                metrics={"rx_health": "ok", "fec_enabled": True, "fec_health": "warn"},
                dsp={"eq_enabled": True, "eq_health": "fail"},
            ),
            "fail",
        )

    def test_disabled_fec_ignored(self) -> None:
        self.assertEqual(
            classify_integration_health(
                active=True,
                metrics={"rx_health": "ok", "fec_enabled": False, "fec_health": "fail"},
            ),
            "ok",
        )

    def test_enabled_subsystems(self) -> None:
        self.assertEqual(
            classify_integration_health(
                active=True,
                metrics={"rx_health": "ok", "opus_enabled": True, "opus_health": "ok"},
                return_path={"enabled": True, "return_health": "ok"},
                discovery={"discovery_enabled": True, "discovery_health": "ok"},
                dsp={
                    "eq_enabled": True,
                    "eq_health": "ok",
                    "dyn_enabled": True,
                    "dyn_health": "ok",
                    "fx_enabled": True,
                    "fx_health": "ok",
                },
                record={"record_enabled": True, "record_health": "ok"},
            ),
            "ok",
        )


class SnapshotTimingTests(unittest.TestCase):
    def test_age_prefers_exported_time_ns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xair_sync_state.json"
            path.write_text("{}", encoding="utf-8")
            now = 1_700_000_100.0
            data = {
                "active": True,
                "report_interval_sec": 5.0,
                "exported_time_ns": int(1_700_000_090 * 1_000_000_000),
            }
            self.assertAlmostEqual(snapshot_age_sec(data, path, now=now), 10.0, places=3)
            self.assertTrue(snapshot_fresh(data, path, now=now))
            stale = dict(data)
            stale["exported_time_ns"] = int(1_700_000_000 * 1_000_000_000)
            self.assertFalse(snapshot_fresh(stale, path, now=now))


class DashboardIntegrationTests(unittest.TestCase):
    def _full_report(self, **overrides: object) -> dict:
        rep = {
            "level_tolerance_db": 6.0,
            "global_drift_ns": 12,
            "receiver_metrics": {
                "rx_health": "ok",
                "fec_enabled": True,
                "fec_health": "ok",
                "opus_enabled": True,
                "opus_health": "ok",
                "jitter_ms_ema": 1.0,
            },
            "return_path": {"enabled": True, "return_health": "ok", "osc_enabled": True},
            "discovery": {
                "discovery_enabled": True,
                "discovery_health": "ok",
                "discovered_devices": [],
                "last_discovery_ts": 3,
            },
            "dsp": {
                "eq_enabled": True,
                "eq_health": "ok",
                "dyn_enabled": True,
                "dyn_health": "ok",
                "fx_enabled": True,
                "fx_health": "ok",
            },
            "record": {"record_enabled": True, "recording": False, "record_health": "ok"},
            "test": {"test_health": "ok", "last_test_run": 1.0},
            "service": {"service_enabled": True},
            "package": {"package_version": "0.1.0"},
            "release": {"release_version": "0.1.0"},
            "integration": {"integration_health": "ok", "last_integration_ts": 9.0},
            "integration_health": "ok",
            "last_integration_ts": 9.0,
            "channels": [
                {
                    "ch": 1,
                    "name": "Kick",
                    "fader": 0.5,
                    "rms_audio": 0.1,
                    "meter_osc": 256,
                    "delta_level": 0.2,
                    "level_ok": True,
                    "drift_ns": 12,
                }
            ],
        }
        rep.update(overrides)
        return rep

    def test_view_aggregates_all_modules_and_fails_on_opus(self) -> None:
        payload = {
            "schema": 1,
            "active": True,
            "report_interval_sec": 5.0,
            "exported_time_ns": 1_700_000_000_000_000_000,
            "report": self._full_report(
                receiver_metrics={
                    "rx_health": "ok",
                    "fec_enabled": True,
                    "fec_health": "ok",
                    "opus_enabled": True,
                    "opus_health": "fail",
                }
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xair_sync_state.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            view = build_view(path, payload, now=1_700_000_000.2)
            self.assertEqual(view.status, "live")
            self.assertEqual(view.metrics["opus_health"], "fail")
            self.assertEqual(view.return_path["return_health"], "ok")
            self.assertEqual(view.discovery["discovery_health"], "ok")
            self.assertEqual(view.dsp["eq_health"], "ok")
            self.assertEqual(view.record["record_health"], "ok")
            self.assertEqual(view.integration["integration_health"], "fail")
            self.assertEqual(view.integration["last_integration_ts"], 9.0)
            dumped = view_as_dict(view)
            self.assertEqual(dumped["integration"]["integration_health"], "fail")
            self.assertEqual(len(dumped["channels"]), 1)

    def test_inactive_keeps_last_report_and_forces_health_off(self) -> None:
        payload = {
            "schema": 1,
            "active": False,
            "report_interval_sec": 5.0,
            "exported_time_ns": 1_700_000_000_000_000_000,
            "report": self._full_report(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xair_sync_state.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            view = build_view(path, payload, now=1_700_000_000.2)
            self.assertEqual(view.status, "inactive")
            self.assertEqual(view.return_path["enabled"], True)
            self.assertEqual(view.metrics["rx_health"], "ok")
            self.assertEqual(view.n_ok, 1)
            self.assertEqual(view.integration["integration_health"], "off")

    def test_record_sidecar_updates_integration(self) -> None:
        payload = {
            "schema": 1,
            "active": True,
            "report_interval_sec": 5.0,
            "exported_time_ns": 1_700_000_000_000_000_000,
            "report": self._full_report(),
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xair_sync_state.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            (Path(tmp) / ".xair_record.json").write_text(
                json.dumps({"record_enabled": True, "record_health": "fail", "recording": True}),
                encoding="utf-8",
            )
            view = build_view(path, payload, now=1_700_000_000.2)
            self.assertEqual(view.record["record_health"], "fail")
            self.assertEqual(view.integration["integration_health"], "fail")

    def test_snapshot_helper_roundtrip(self) -> None:
        snap = integration_snapshot(
            active=True,
            last_ts=4.5,
            metrics={"rx_health": "warn"},
        )
        self.assertEqual(snap["integration_health"], "warn")
        self.assertEqual(snap["last_integration_ts"], 4.5)


if __name__ == "__main__":
    unittest.main()
