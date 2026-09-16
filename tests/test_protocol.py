"""XBRI header sequencing — no audio hardware."""

from __future__ import annotations

import unittest

from src.network_sender.jitter_send import SendJitterBuffer
from src.network_sender.protocol import (
    FLAG_FEC,
    FLAG_OPUS,
    HEADER_SIZE,
    MAGIC,
    Codec,
    build_header,
    inspect_packet_mtu,
    is_fec_packet,
    is_opus_packet,
    parse_header,
)


class HeaderSeqTests(unittest.TestCase):
    def test_build_parse_roundtrip_seq(self) -> None:
        raw = build_header(
            codec=Codec.PCM24,
            sample_rate=48000,
            channels=18,
            nframes=21,
            seq=42,
        )
        hdr, payload = parse_header(raw + b"xyz")
        self.assertEqual(hdr.seq, 42)
        self.assertEqual(hdr.codec, Codec.PCM24)
        self.assertEqual(hdr.channels, 18)
        self.assertEqual(hdr.nframes, 21)
        self.assertEqual(payload, b"xyz")
        self.assertEqual(len(raw), HEADER_SIZE)
        self.assertTrue(raw.startswith(MAGIC))

    def test_seq_wraps_to_32bit(self) -> None:
        raw = build_header(
            codec=Codec.PCM16, sample_rate=48000, channels=1, nframes=8, seq=1 << 32
        )
        hdr, _ = parse_header(raw)
        self.assertEqual(hdr.seq, 0)
        raw2 = build_header(
            codec=Codec.PCM16, sample_rate=48000, channels=1, nframes=8, seq=(1 << 32) + 7
        )
        hdr2, _ = parse_header(raw2)
        self.assertEqual(hdr2.seq, 7)

    def test_rejects_truncated_and_bad_magic(self) -> None:
        with self.assertRaises(ValueError):
            parse_header(b"short")
        raw = build_header(
            codec=Codec.PCM16, sample_rate=48000, channels=1, nframes=8, seq=1
        )
        bad = b"NOPE" + raw[4:]
        with self.assertRaises(ValueError):
            parse_header(bad)

    def test_flags_are_independent(self) -> None:
        raw = build_header(
            codec=Codec.PCM24,
            sample_rate=48000,
            channels=2,
            nframes=21,
            seq=9,
            flags=FLAG_FEC | FLAG_OPUS,
            extra=3,
        )
        hdr, _ = parse_header(raw)
        self.assertTrue(is_fec_packet(hdr))
        self.assertTrue(is_opus_packet(hdr))
        self.assertEqual(hdr.extra, 3)

    def test_mtu_pcm24_18ch_21(self) -> None:
        self.assertEqual(inspect_packet_mtu(21, 18, Codec.PCM24), 21 * 18 * 3)
        self.assertEqual(inspect_packet_mtu(21, 18, Codec.PCM16), 21 * 18 * 2)


class SendJitterBufferTests(unittest.TestCase):
    def test_depth_one_is_passthrough(self) -> None:
        buf = SendJitterBuffer(max_packets=1)
        buf.push(b"a")
        self.assertEqual(list(buf.pop_all()), [b"a"])
        self.assertEqual(list(buf.pop_all()), [])

    def test_overflow_drops_oldest(self) -> None:
        buf = SendJitterBuffer(max_packets=2)
        buf.push(b"1")
        buf.push(b"2")
        buf.push(b"3")
        self.assertEqual(list(buf.pop_all()), [b"2", b"3"])


if __name__ == "__main__":
    unittest.main()
