"""Sender → UDP localhost → FEC/decode/reorder loop. No mixer, no JACK."""

from __future__ import annotations

import socket
import time
import unittest

import numpy as np

from src.network_receiver.reorder import RecvReorderLost
from src.network_sender.fec import FecAssembler
from src.network_sender.opus_codec import libopus_available, opus_frame_samples
from src.network_sender.protocol import (
    Codec,
    is_fec_packet,
    is_opus_packet,
    parse_header,
)
from src.network_sender.sender import NetworkSender
from src.usb_capture.pcm import pcm16_from_bytes, pcm24_from_bytes


def _recv_until(sock: socket.socket, n: int, timeout_s: float = 2.0) -> list[bytes]:
    sock.settimeout(0.2)
    out: list[bytes] = []
    deadline = time.monotonic() + timeout_s
    while len(out) < n and time.monotonic() < deadline:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        out.append(data)
    return out


class PcmLoopTests(unittest.TestCase):
    def test_pcm24_feed_roundtrip(self) -> None:
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        port = rx.getsockname()[1]
        sender = NetworkSender(
            host="127.0.0.1",
            port=port,
            sample_rate=48000,
            channels=2,
            codec="PCM24",
            samples_per_packet=8,
            send_jitter_depth=1,
        )
        try:
            blk = np.zeros((8, 2), dtype=np.float32)
            blk[:, 0] = 0.25
            blk[:, 1] = -0.25
            sender.feed(blk)
            pkts = _recv_until(rx, 1)
            self.assertEqual(len(pkts), 1)
            hdr, payload = parse_header(pkts[0])
            self.assertEqual(hdr.codec, Codec.PCM24)
            self.assertEqual(hdr.seq, 0)
            self.assertEqual(hdr.nframes, 8)
            audio = pcm24_from_bytes(payload, 2)
            self.assertEqual(audio.shape, (8, 2))
            np.testing.assert_allclose(audio[:, 0], 0.25, atol=2e-5)
            np.testing.assert_allclose(audio[:, 1], -0.25, atol=2e-5)
        finally:
            sender.close()
            rx.close()

    def test_fec_recovers_dropped_media_then_reorder(self) -> None:
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        port = rx.getsockname()[1]
        group = 3
        sender = NetworkSender(
            host="127.0.0.1",
            port=port,
            sample_rate=48000,
            channels=1,
            codec="PCM16",
            samples_per_packet=4,
            send_jitter_depth=2,
            fec_enabled=True,
            fec_group=group,
        )
        try:
            for i in range(group):
                blk = np.full((4, 1), 0.05 * (i + 1), dtype=np.float32)
                sender.feed(blk)
            pkts = _recv_until(rx, group + 1)
            self.assertGreaterEqual(len(pkts), group + 1)
            parsed = [parse_header(p) for p in pkts]
            media = [(h, pl) for h, pl in parsed if not is_fec_packet(h)]
            fec = [(h, pl) for h, pl in parsed if is_fec_packet(h)]
            self.assertEqual(len(media), group)
            self.assertEqual(len(fec), 1)
            dropped = media[1]
            keep = [media[0], media[2]]
            asm = FecAssembler(group)
            recovered: list = []
            recovered.extend(asm.ingest_media(*keep[0]))
            recovered.extend(asm.ingest_media(*keep[1]))
            recovered.extend(asm.ingest_fec(*fec[0]))
            seqs = sorted(h.seq for h, _ in recovered)
            self.assertEqual(seqs, [0, 1, 2])
            by_seq = {h.seq: (h, pl) for h, pl in recovered}
            self.assertTrue(by_seq[1][0].recovered)
            audio1 = pcm16_from_bytes(by_seq[1][1], 1)
            expect = pcm16_from_bytes(dropped[1], 1)
            np.testing.assert_allclose(audio1, expect, atol=1e-6)

            ro = RecvReorderLost(
                channels=1, nominal_samples_per_datagram=4, max_packets_pending=32
            )
            blocks = []
            for seq in range(3):
                h, pl = by_seq[seq]
                blocks.extend(ro.push(h.seq, h.nframes, pcm16_from_bytes(pl, 1)))
            self.assertEqual(len(blocks), 3)
        finally:
            sender.close()
            rx.close()


@unittest.skipUnless(libopus_available(), "libopus not installed")
class OpusLoopTests(unittest.TestCase):
    def test_opus_udp_roundtrip(self) -> None:
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        port = rx.getsockname()[1]
        sr = 48000
        sender = NetworkSender(
            host="127.0.0.1",
            port=port,
            sample_rate=sr,
            channels=2,
            codec="PCM24",
            samples_per_packet=21,
            send_jitter_depth=1,
            opus_enabled=True,
            opus_bitrate=64000,
            opus_frame_ms=20.0,
        )
        try:
            n = opus_frame_samples(sr, 20.0)
            t = np.arange(n, dtype=np.float32) / float(sr)
            blk = np.stack(
                [0.2 * np.sin(2 * np.pi * 440 * t), 0.2 * np.sin(2 * np.pi * 660 * t)],
                axis=1,
            )
            sender.feed(blk)
            pkts = _recv_until(rx, 1, timeout_s=3.0)
            self.assertEqual(len(pkts), 1)
            hdr, payload = parse_header(pkts[0])
            self.assertTrue(is_opus_packet(hdr))
            self.assertEqual(hdr.seq, 0)
            self.assertGreater(len(payload), 8)
            from src.network_sender.opus_codec import OpusBundle

            dec = OpusBundle(
                sample_rate=sr, channels=2, bitrate=64000, frame_ms=20.0, decode=True
            )
            try:
                out = dec.decode(payload, n)
            finally:
                dec.close()
            self.assertEqual(out.shape, blk.shape)
            self.assertGreater(float(np.sqrt(np.mean(np.square(out)))), 0.02)
        finally:
            sender.close()
            rx.close()


if __name__ == "__main__":
    unittest.main()
