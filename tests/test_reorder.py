"""RecvReorderLost sequencing — no receiver.py / PortAudio."""

from __future__ import annotations

import unittest

import numpy as np

from src.network_receiver.metrics import ReceiverMetrics
from src.network_receiver.reorder import (
    SEQ_MASK,
    RecvReorderLost,
    linear_interpolate_gap,
    seq_forward,
    seq_is_behind,
)


def _blk(seq_tag: float, n: int = 4, ch: int = 2) -> np.ndarray:
    out = np.full((n, ch), float(seq_tag), dtype=np.float32)
    return out


class SeqMathTests(unittest.TestCase):
    def test_forward_and_behind(self) -> None:
        self.assertEqual(seq_forward(10, 12), 2)
        self.assertEqual(seq_forward(SEQ_MASK, 0), 1)
        self.assertTrue(seq_is_behind(8, 10))
        self.assertFalse(seq_is_behind(10, 10))
        self.assertFalse(seq_is_behind(11, 10))

    def test_interpolate_rows(self) -> None:
        tail = np.zeros(2, dtype=np.float32)
        head = np.ones(2, dtype=np.float32)
        gap = linear_interpolate_gap(tail, head, 3)
        self.assertEqual(gap.shape, (3, 2))
        self.assertGreater(float(gap[0, 0]), 0.0)
        self.assertLess(float(gap[-1, 0]), 1.0)


class ReorderTests(unittest.TestCase):
    def _ro(self) -> RecvReorderLost:
        return RecvReorderLost(
            channels=2, nominal_samples_per_datagram=4, max_packets_pending=32
        )

    def test_in_order_seq(self) -> None:
        ro = self._ro()
        a = ro.push(0, 4, _blk(0.1))
        b = ro.push(1, 4, _blk(0.2))
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)
        self.assertAlmostEqual(float(a[0][0, 0]), 0.1, places=5)
        self.assertAlmostEqual(float(b[0][0, 0]), 0.2, places=5)
        self.assertEqual(ro.next_seq, 2)

    def test_gap_interpolates_lost_packet(self) -> None:
        ro = self._ro()
        m = ReceiverMetrics()
        ro.set_metrics(m)
        ro.push(0, 4, _blk(0.0))
        out = ro.push(2, 4, _blk(1.0))
        # synth for missing seq=1 (4 samples) then real seq=2
        self.assertGreaterEqual(len(out), 2)
        self.assertEqual(out[0].shape[0], 4)
        self.assertEqual(m.snapshot()["packets_lost"], 1)
        self.assertEqual(ro.next_seq, 3)

    def test_late_packet_dropped(self) -> None:
        ro = self._ro()
        m = ReceiverMetrics()
        ro.set_metrics(m)
        ro.push(0, 4, _blk(0.0))
        ro.push(1, 4, _blk(0.2))
        late = ro.push(0, 4, _blk(0.9))
        self.assertEqual(late, [])
        self.assertEqual(m.snapshot()["packets_late_drop"], 1)

    def test_seq_wrap_next_after_max(self) -> None:
        ro = self._ro()
        ro.next_seq = SEQ_MASK
        ro.tail_primed = True
        out = ro.push(SEQ_MASK, 4, _blk(0.3))
        self.assertEqual(len(out), 1)
        self.assertEqual(ro.next_seq, 0)
        out2 = ro.push(0, 4, _blk(0.4))
        self.assertEqual(len(out2), 1)
        self.assertEqual(ro.next_seq, 1)


if __name__ == "__main__":
    unittest.main()
