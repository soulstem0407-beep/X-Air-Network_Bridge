"""Adaptive jitter policy and RX health — no audio hardware."""

from __future__ import annotations

import unittest

from src.network_receiver.adaptive_jitter import (
    AdaptiveJitterConfig,
    classify_rx_health,
    desired_packets,
    packet_duration_ms,
    step_target,
)
from src.network_receiver.metrics import ReceiverMetrics


class AdaptivePolicyTests(unittest.TestCase):
    def test_packet_duration_21_at_48k(self) -> None:
        self.assertAlmostEqual(packet_duration_ms(21, 48000), 1000.0 * 21 / 48000)

    def test_desired_covers_jitter_and_respects_floor(self) -> None:
        pkt = packet_duration_ms(21, 48000)
        # ~0.1 ms jitter → still the floor
        self.assertEqual(desired_packets(0.1, pkt, floor=5, ceiling=16), 5)
        # 5 ms jitter × 3 / 0.4375 ≈ 35 → clip to ceiling
        self.assertEqual(desired_packets(5.0, pkt, floor=5, ceiling=16), 16)

    def test_step_grows_one_shrinks_after_quiet(self) -> None:
        self.assertEqual(
            step_target(5, 8, underrun=False, stream_alive=True, quiet_streak=0, floor=5, ceiling=16),
            6,
        )
        self.assertEqual(
            step_target(8, 5, underrun=False, stream_alive=True, quiet_streak=1, shrink_after=4, floor=5, ceiling=16),
            8,
        )
        self.assertEqual(
            step_target(8, 5, underrun=False, stream_alive=True, quiet_streak=4, shrink_after=4, floor=5, ceiling=16),
            7,
        )

    def test_underrun_grows_even_if_desired_equals_current(self) -> None:
        self.assertEqual(
            step_target(5, 5, underrun=True, stream_alive=True, quiet_streak=0, floor=5, ceiling=16),
            6,
        )

    def test_dead_stream_does_not_move(self) -> None:
        self.assertEqual(
            step_target(9, 5, underrun=True, stream_alive=False, quiet_streak=9, floor=5, ceiling=16),
            9,
        )

    def test_health_ok_warn_fail(self) -> None:
        self.assertEqual(
            classify_rx_health(
                loss_window_ppm=0.0, jitter_ms=0.4, underruns_window=0, fill_percent=100.0, stream_alive=True
            ),
            ("ok", "ok"),
        )
        self.assertEqual(
            classify_rx_health(
                loss_window_ppm=0.0, jitter_ms=4.0, underruns_window=0, fill_percent=90.0, stream_alive=True
            )[0],
            "warn",
        )
        self.assertEqual(
            classify_rx_health(
                loss_window_ppm=0.0, jitter_ms=0.4, underruns_window=2, fill_percent=40.0, stream_alive=True
            ),
            ("fail", "underrun"),
        )
        self.assertEqual(
            classify_rx_health(
                loss_window_ppm=0.0, jitter_ms=0.4, underruns_window=0, fill_percent=80.0, stream_alive=False
            ),
            ("warn", "idle"),
        )

    def test_config_adaptive_false_locks_range(self) -> None:
        import os
        from unittest.mock import patch

        env = {"XAIR_JITTER_ADAPTIVE": "false", "XAIR_JITTER_MAX": "24"}
        with patch.dict(os.environ, env, clear=False):
            cfg = AdaptiveJitterConfig.from_env(5)
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.floor, 5)
        self.assertEqual(cfg.ceiling, 5)


class ReceiverMetricsHealthTests(unittest.TestCase):
    def test_snapshot_includes_health_fields(self) -> None:
        m = ReceiverMetrics()
        m.set_jitter_control(5, 5, 16, True)
        m.on_arrival_nominal_timing(21, 48000, 1.0)
        m.on_arrival_nominal_timing(21, 48000, 1.0 + 21 / 48000)
        m.note_queue(5, 64, 5)
        m.refresh_window()
        snap = m.snapshot()
        self.assertIn("rx_health", snap)
        self.assertIn("jitter_ms_peak", snap)
        self.assertEqual(snap["jitter_packets_target"], 5)
        self.assertTrue(snap["jitter_adaptive"])
        self.assertEqual(snap["queue_packets"], 5)
        # Two on-time packets: health should not be fail.
        self.assertNotEqual(snap["rx_health"], "fail")

    def test_underrun_counter(self) -> None:
        m = ReceiverMetrics()
        m.note_underrun()
        m.note_underrun()
        self.assertEqual(m.snapshot()["underruns"], 2)


if __name__ == "__main__":
    unittest.main()
