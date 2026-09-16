"""Opus transport flags and bundle — no PortAudio/JACK."""

from __future__ import annotations

import unittest

import numpy as np

from src.network_receiver.metrics import ReceiverMetrics, classify_opus_health
from src.network_sender.opus_codec import (
    clamp_opus_bitrate,
    clamp_opus_frame_ms,
    libopus_available,
    opus_frame_samples,
    pack_opus_streams,
    unpack_opus_streams,
    _stream_layout,
)
from src.network_sender.protocol import (
    FLAG_FEC,
    FLAG_OPUS,
    Codec,
    build_header,
    is_opus_packet,
    parse_header,
)


class OpusHeaderTests(unittest.TestCase):
    def test_flag_roundtrip_and_not_fec(self) -> None:
        raw = build_header(
            codec=Codec.PCM24,
            sample_rate=48000,
            channels=18,
            nframes=960,
            seq=3,
            flags=FLAG_OPUS,
            extra=1,
        )
        hdr, payload = parse_header(raw + b"\x01\x02")
        self.assertTrue(is_opus_packet(hdr))
        self.assertFalse(bool(hdr.flags & FLAG_FEC))
        self.assertEqual(hdr.seq, 3)
        self.assertEqual(hdr.extra, 1)
        self.assertEqual(payload, b"\x01\x02")

    def test_pcm_header_has_no_opus_flag(self) -> None:
        raw = build_header(
            codec=Codec.PCM24, sample_rate=48000, channels=2, nframes=21, seq=0
        )
        hdr, _ = parse_header(raw)
        self.assertFalse(is_opus_packet(hdr))


class OpusPackTests(unittest.TestCase):
    def test_pack_unpack_streams(self) -> None:
        chunks = [b"aa", b"bbb", b""]
        blob = pack_opus_streams(chunks)
        self.assertEqual(unpack_opus_streams(blob), chunks)

    def test_clamp_frame_and_bitrate(self) -> None:
        self.assertEqual(clamp_opus_frame_ms(12), 10.0)
        self.assertEqual(clamp_opus_frame_ms(18), 20.0)
        self.assertEqual(opus_frame_samples(48000, 20), 960)
        self.assertEqual(clamp_opus_bitrate(100), 6000)
        self.assertEqual(clamp_opus_bitrate(128000), 128000)

    def test_stream_layout_pairs_plus_mono(self) -> None:
        self.assertEqual(_stream_layout(18), [2] * 9)
        self.assertEqual(_stream_layout(1), [1])
        self.assertEqual(_stream_layout(3), [2, 1])


class OpusMetricsTests(unittest.TestCase):
    def test_snapshot_fields(self) -> None:
        m = ReceiverMetrics()
        m.set_opus_control(True, 128000, 20.0)
        m.note_opus_decode_error()
        snap = m.snapshot()
        self.assertTrue(snap["opus_enabled"])
        self.assertEqual(snap["opus_bitrate"], 128000)
        self.assertEqual(snap["opus_frame"], 20.0)
        self.assertEqual(snap["opus_decode_errors"], 1)
        self.assertEqual(snap["opus_health"], "ok")  # window still 0

    def test_classify_colors(self) -> None:
        self.assertEqual(
            classify_opus_health(enabled=False, decode_errors_window=0, decode_error_ppm=0.0),
            "off",
        )
        self.assertEqual(
            classify_opus_health(enabled=True, decode_errors_window=0, decode_error_ppm=0.0),
            "ok",
        )
        self.assertEqual(
            classify_opus_health(enabled=True, decode_errors_window=1, decode_error_ppm=100.0),
            "warn",
        )
        self.assertEqual(
            classify_opus_health(enabled=True, decode_errors_window=2, decode_error_ppm=10_000.0),
            "fail",
        )


@unittest.skipUnless(libopus_available(), "libopus not installed")
class OpusRoundtripTests(unittest.TestCase):
    def test_stereo_roundtrip(self) -> None:
        from src.network_sender.opus_codec import OpusBundle

        sr, ch, ms = 48000, 2, 20.0
        n = opus_frame_samples(sr, ms)
        t = np.arange(n, dtype=np.float32) / float(sr)
        pcm = np.stack([0.2 * np.sin(2 * np.pi * 440 * t), 0.2 * np.sin(2 * np.pi * 660 * t)], axis=1)
        enc = OpusBundle(sample_rate=sr, channels=ch, bitrate=128000, frame_ms=ms, decode=False)
        dec = OpusBundle(sample_rate=sr, channels=ch, bitrate=128000, frame_ms=ms, decode=True)
        try:
            payload = enc.encode(pcm)
            out = dec.decode(payload, n)
        finally:
            enc.close()
            dec.close()
        self.assertEqual(out.shape, pcm.shape)
        self.assertGreater(len(payload), 8)
        self.assertGreater(float(np.sqrt(np.mean(np.square(out)))), 0.02)

    def test_eighteen_channel_roundtrip(self) -> None:
        from src.network_sender.opus_codec import OpusBundle

        sr, ch, ms = 48000, 18, 20.0
        n = opus_frame_samples(sr, ms)
        t = np.arange(n, dtype=np.float32) / float(sr)
        cols = [0.15 * np.sin(2 * np.pi * (220 + 40 * i) * t) for i in range(ch)]
        pcm = np.stack(cols, axis=1)
        enc = OpusBundle(sample_rate=sr, channels=ch, bitrate=128000, frame_ms=ms, decode=False)
        dec = OpusBundle(sample_rate=sr, channels=ch, bitrate=128000, frame_ms=ms, decode=True)
        try:
            payload = enc.encode(pcm)
            out = dec.decode(payload, n)
        finally:
            enc.close()
            dec.close()
        self.assertEqual(out.shape, pcm.shape)
        streams = unpack_opus_streams(payload)
        self.assertEqual(len(streams), 9)
        self.assertGreater(float(np.sqrt(np.mean(np.square(out)))), 0.02)


if __name__ == "__main__":
    unittest.main()
