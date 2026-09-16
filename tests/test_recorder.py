"""WAV sidecar: PCM24, non-blocking offer, start/stop commands. No audio hardware."""

from __future__ import annotations

import json
import tempfile
import time
import unittest
import wave
from pathlib import Path

import numpy as np

from src.dashboard.reader import build_view
from src.recorder.sidecar import (
    WavRecorder,
    classify_record_health,
    float_to_pcm24,
    write_record_command,
)


def _pcm24_to_float(blob: bytes) -> np.ndarray:
    n = len(blob) // 3
    out = np.empty(n, dtype=np.int32)
    b = np.frombuffer(blob, dtype=np.uint8)
    out[:] = b[0::3].astype(np.int32) | (b[1::3].astype(np.int32) << 8) | (b[2::3].astype(np.int32) << 16)
    neg = out >= 0x800000
    out = out - (neg.astype(np.int32) * 0x1000000)
    return out.astype(np.float64) / 8388607.0


class Pcm24Tests(unittest.TestCase):
    def test_roundtrip_levels(self) -> None:
        src = np.array([0.0, 0.5, -0.5, 1.0, -1.0], dtype=np.float32)
        blob = float_to_pcm24(src)
        self.assertEqual(len(blob), 5 * 3)
        back = _pcm24_to_float(blob)
        np.testing.assert_allclose(back, src, atol=2e-7)


class HealthTests(unittest.TestCase):
    def test_colors(self) -> None:
        self.assertEqual(
            classify_record_health(
                enabled=False, recording=False, errors=0, dropped=0, frames=0
            ),
            "off",
        )
        self.assertEqual(
            classify_record_health(
                enabled=True, recording=False, errors=0, dropped=0, frames=0
            ),
            "off",
        )
        self.assertEqual(
            classify_record_health(
                enabled=True, recording=True, errors=0, dropped=0, frames=100
            ),
            "ok",
        )
        self.assertEqual(
            classify_record_health(
                enabled=True, recording=True, errors=0, dropped=3, frames=100
            ),
            "warn",
        )
        self.assertEqual(
            classify_record_health(
                enabled=True,
                recording=True,
                errors=5,
                dropped=0,
                frames=1,
                error_ppm=10_000.0,
            ),
            "fail",
        )


class SidecarTests(unittest.TestCase):
    def test_offer_does_not_block_when_full(self) -> None:
        rec = WavRecorder(channels=2, sample_rate=48000, queue_size=2, auto_start=False)
        rec._recording = True
        blk = np.zeros((8, 2), dtype=np.float32)
        rec.offer(blk)
        rec.offer(blk)
        t0 = time.monotonic()
        rec.offer(blk)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 0.05)
        self.assertGreaterEqual(rec.dropped, 1)
        rec._recording = False

    def test_wav_roundtrip_and_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rec = WavRecorder(
                channels=2,
                sample_rate=48000,
                output_dir=root / "takes",
                cmd_path=root / ".xair_record_cmd",
                state_path=root / ".xair_record.json",
                labels_provider=lambda: ["Kick", "Snare"],
                queue_size=32,
                auto_start=False,
            )
            rec.start()
            try:
                rec.begin()
                tone = np.column_stack(
                    [
                        np.full(64, 0.25, dtype=np.float32),
                        np.full(64, -0.25, dtype=np.float32),
                    ]
                )
                rec.offer(tone)
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if rec.frames >= 64:
                        break
                    time.sleep(0.01)
                self.assertGreaterEqual(rec.frames, 64)
                snap = rec.snapshot()
                self.assertTrue(snap["record_enabled"])
                self.assertTrue(snap["recording"])
                self.assertEqual(len(snap["record_files"]), 2)
                self.assertIsNotNone(snap["last_record_ts"])
                rec.end()
                files = snap["record_files"]
                with wave.open(files[0], "rb") as wf:
                    self.assertEqual(wf.getnchannels(), 1)
                    self.assertEqual(wf.getsampwidth(), 3)
                    self.assertEqual(wf.getframerate(), 48000)
                    raw = wf.readframes(wf.getnframes())
                back = _pcm24_to_float(raw)
                np.testing.assert_allclose(back[:64], 0.25, atol=2e-5)

                write_record_command(root / ".xair_record_cmd", "start")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if rec.recording:
                        break
                    time.sleep(0.02)
                self.assertTrue(rec.recording)
                write_record_command(root / ".xair_record_cmd", "stop")
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if not rec.recording:
                        break
                    time.sleep(0.02)
                self.assertFalse(rec.recording)
                state = json.loads((root / ".xair_record.json").read_text(encoding="utf-8"))
                self.assertIn("record_files", state)
            finally:
                rec.stop()


class DashboardRecordTests(unittest.TestCase):
    def test_view_merges_record_block(self) -> None:
        payload = {
            "schema": 1,
            "active": True,
            "report_interval_sec": 5.0,
            "report": {
                "level_tolerance_db": 6.0,
                "record": {
                    "record_enabled": True,
                    "recording": True,
                    "record_path": "/tmp/rec",
                    "record_files": ["/tmp/rec/01_Kick.wav"],
                    "last_record_ts": 1.5,
                    "record_health": "ok",
                },
                "channels": [],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xair_sync_state.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            view = build_view(path, payload, now=path.stat().st_mtime)
            self.assertTrue(view.record["record_enabled"])
            self.assertTrue(view.record["recording"])
            self.assertEqual(view.record["record_files"][0], "/tmp/rec/01_Kick.wav")
            self.assertEqual(view.record["last_record_ts"], 1.5)


if __name__ == "__main__":
    unittest.main()
