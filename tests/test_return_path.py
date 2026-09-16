"""Return-path safety and snapshot helpers — no audio hardware."""

from __future__ import annotations

import unittest

from src.sync.return_safety import ReturnSafety, classify_return_health


def _dbfs_to_rms(db: float) -> float:
    return 10.0 ** (db / 20.0)


class ReturnSafetyTests(unittest.TestCase):
    def test_quiet_is_ok(self) -> None:
        s = ReturnSafety(hold_s=0.05, cooldown_s=0.2)
        allow, st = s.observe(_dbfs_to_rms(-40), _dbfs_to_rms(-40), 0.0)
        self.assertTrue(allow)
        self.assertEqual(st, "ok")
        self.assertEqual(classify_return_health(enabled=True, safety_status=st), "ok")

    def test_both_hot_trips_after_hold(self) -> None:
        s = ReturnSafety(trip_dbfs=-6.0, hold_s=0.10, cooldown_s=1.0)
        hot = _dbfs_to_rms(-3.0)
        allow, st = s.observe(hot, hot, 0.0)
        self.assertTrue(allow)
        self.assertEqual(st, "gated")
        allow, st = s.observe(hot, hot, 0.12)
        self.assertFalse(allow)
        self.assertEqual(st, "tripped")
        self.assertEqual(classify_return_health(enabled=True, safety_status=st), "fail")

    def test_disabled_never_gates(self) -> None:
        s = ReturnSafety(enabled=False)
        allow, st = s.observe(1.0, 1.0, 0.0)
        self.assertTrue(allow)
        self.assertEqual(st, "ok")
        self.assertEqual(classify_return_health(enabled=False, safety_status="off"), "off")

    def test_recovers_after_cooldown(self) -> None:
        s = ReturnSafety(trip_dbfs=-6.0, warn_dbfs=-12.0, hold_s=0.05, cooldown_s=0.2)
        hot = _dbfs_to_rms(-3.0)
        quiet = _dbfs_to_rms(-30.0)
        s.observe(hot, hot, 0.0)
        s.observe(hot, hot, 0.06)
        self.assertEqual(s.status, "tripped")
        allow, st = s.observe(quiet, quiet, 0.30)
        self.assertTrue(allow)
        self.assertEqual(st, "ok")


if __name__ == "__main__":
    unittest.main()
