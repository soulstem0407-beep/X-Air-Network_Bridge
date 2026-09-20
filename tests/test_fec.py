"""XOR FEC groups and header flags — no audio hardware / receiver.py."""

from __future__ import annotations

import unittest

from src.network_receiver.metrics import ReceiverMetrics, classify_fec_health
from src.network_sender.fec import (
    DEFAULT_FEC_GROUP,
    FecAssembler,
    FecEncoder,
    clamp_fec_group,
    xor_bytes,
)
from src.network_sender.protocol import (
    FLAG_FEC,
    Codec,
    build_header,
    is_fec_packet,
    parse_header,
)


def _hdr(*, seq: int, extra: int = 0, flags: int = 0, nframes: int = 2):
    raw = build_header(
        codec=Codec.PCM16,
        sample_rate=48000,
        channels=1,
        nframes=nframes,
        seq=seq,
        flags=flags,
        extra=extra,
    )
    hdr, _ = parse_header(raw)
    return hdr


def _encode_media(payloads: list[bytes], *, group: int = 3, seq0: int = 0):
    enc = FecEncoder(group)
    media = []
    fec_datagram = None
    for i, payload in enumerate(payloads):
        seq = seq0 + i
        extra = enc.position_for_next()
        hdr = _hdr(seq=seq, extra=extra)
        media.append((hdr, payload))
        fec_datagram = enc.add_media(
            codec=Codec.PCM16,
            sample_rate=48000,
            channels=1,
            nframes=2,
            seq=seq,
            payload=payload,
        )
    return media, fec_datagram, enc


class XorAndHeaderTests(unittest.TestCase):
    def test_xor_roundtrip_one_missing(self) -> None:
        a, b, c = b"\x01\x02\x03", b"\x10\x20\x30", b"\xff\x00\xaa"
        parity = xor_bytes([a, b, c])
        recovered = xor_bytes([a, c, parity])
        self.assertEqual(recovered, b)

    def test_header_flags_roundtrip(self) -> None:
        raw = build_header(
            codec=Codec.PCM24,
            sample_rate=48000,
            channels=18,
            nframes=21,
            seq=7,
            flags=FLAG_FEC,
            extra=3,
        )
        hdr, payload = parse_header(raw + b"\x00\x01")
        self.assertTrue(is_fec_packet(hdr))
        self.assertEqual(hdr.extra, 3)
        self.assertEqual(hdr.seq, 7)
        self.assertEqual(payload, b"\x00\x01")
        media = build_header(
            codec=Codec.PCM24,
            sample_rate=48000,
            channels=18,
            nframes=21,
            seq=4,
            extra=1,
        )
        mh, _ = parse_header(media)
        self.assertFalse(is_fec_packet(mh))
        self.assertEqual(mh.extra, 1)
        self.assertEqual(mh.flags, 0)

    def test_fec_off_header_still_zero_pads(self) -> None:
        raw = build_header(
            codec=Codec.PCM16, sample_rate=48000, channels=2, nframes=8, seq=0
        )
        hdr, _ = parse_header(raw)
        self.assertEqual(hdr.flags, 0)
        self.assertEqual(hdr.extra, 0)

    def test_clamp_group(self) -> None:
        self.assertEqual(clamp_fec_group(3), 3)
        self.assertEqual(clamp_fec_group(1), 2)
        self.assertEqual(clamp_fec_group(99), 16)
        self.assertEqual(clamp_fec_group(DEFAULT_FEC_GROUP), 3)


class EncoderTests(unittest.TestCase):
    def test_emits_parity_after_group_without_consuming_seq(self) -> None:
        payloads = [b"aa", b"bb", b"cc"]
        media, fec, enc = _encode_media(payloads)
        self.assertEqual(len(media), 3)
        self.assertIsNotNone(fec)
        fec_hdr, fec_pl = parse_header(fec)
        self.assertTrue(is_fec_packet(fec_hdr))
        self.assertEqual(fec_hdr.seq, 0)
        self.assertEqual(fec_hdr.extra, enc.group)
        self.assertTrue(fec_pl.startswith(b"XF2\x00"))
        self.assertEqual([h.seq for h, _ in media], [0, 1, 2])

    def test_disabled_encoder_not_used_when_none(self) -> None:
        # Sender with fec_enabled=false never tags; pads stay 0.
        hdr = _hdr(seq=0, extra=0, flags=0)
        self.assertFalse(is_fec_packet(hdr))
        self.assertEqual(hdr.extra, 0)

    def test_opus_flag_and_variable_length_survive_recovery(self) -> None:
        payloads = [b"a", b"variable", b"xyz"]
        enc = FecEncoder(3)
        media = []
        fec = None
        for i, payload in enumerate(payloads):
            extra = enc.position_for_next()
            hdr = _hdr(seq=i, extra=extra, flags=2, nframes=960)
            media.append((hdr, payload))
            fec = enc.add_media(
                codec=Codec.PCM24,
                sample_rate=48000,
                channels=2,
                nframes=960,
                seq=i,
                payload=payload,
                flags=2,
            )
        assert fec is not None
        fh, fp = parse_header(fec)
        asm = FecAssembler(3)
        out = asm.ingest_media(*media[0]) + asm.ingest_media(*media[2])
        out += asm.ingest_fec(fh, fp)
        recovered = next((h, p) for h, p in out if h.seq == 1)
        self.assertEqual(recovered[1], payloads[1])
        self.assertEqual(recovered[0].flags, 2)


class AssemblerTests(unittest.TestCase):
    def test_recover_exactly_one_missing(self) -> None:
        payloads = [b"red!", b"grn!", b"blu!"]
        media, fec, _enc = _encode_media(payloads)
        asm = FecAssembler(3)
        out = []
        out += asm.ingest_media(*media[0])
        out += asm.ingest_media(*media[2])
        self.assertEqual(out, [])
        fec_hdr, fec_pl = parse_header(fec)
        out += asm.ingest_fec(fec_hdr, fec_pl)
        self.assertEqual([h.seq for h, _p in out], [0, 1, 2])
        self.assertEqual([p for _h, p in out], payloads)
        self.assertTrue(out[1][0].recovered)
        self.assertFalse(out[0][0].recovered)
        self.assertEqual(asm.recovered, 1)
        self.assertEqual(asm.unrecoverable, 0)

    def test_two_missing_unrecoverable(self) -> None:
        payloads = [b"red!", b"grn!", b"blu!"]
        media, fec, _enc = _encode_media(payloads)
        asm = FecAssembler(3)
        out = asm.ingest_media(*media[0])
        self.assertEqual(out, [])
        fec_hdr, fec_pl = parse_header(fec)
        out += asm.ingest_fec(fec_hdr, fec_pl)
        self.assertEqual(out, [])
        out += asm.on_idle()
        self.assertEqual([h.seq for h, _p in out], [0])
        self.assertEqual(asm.recovered, 0)
        self.assertEqual(asm.unrecoverable, 1)

    def test_complete_group_without_fec(self) -> None:
        payloads = [b"aa", b"bb", b"cc"]
        media, fec, _enc = _encode_media(payloads)
        asm = FecAssembler(3)
        out = []
        out += asm.ingest_media(*media[0])
        out += asm.ingest_media(*media[1])
        out += asm.ingest_media(*media[2])
        self.assertEqual([p for _h, p in out], payloads)
        self.assertEqual(asm.recovered, 0)
        fec_hdr, fec_pl = parse_header(fec)
        late = asm.ingest_fec(fec_hdr, fec_pl)
        self.assertEqual(late, [])

    def test_hold_order_recovers_middle(self) -> None:
        payloads = [b"0xxx", b"1yyy", b"2zzz"]
        media, fec, _enc = _encode_media(payloads)
        asm = FecAssembler(3)
        out = []
        out += asm.ingest_media(*media[2])
        out += asm.ingest_media(*media[0])
        fec_hdr, fec_pl = parse_header(fec)
        out += asm.ingest_fec(fec_hdr, fec_pl)
        self.assertEqual([h.seq for h, _p in out], [0, 1, 2])
        self.assertEqual(out[1][1], payloads[1])

    def test_later_group_marks_incomplete_unrecoverable(self) -> None:
        g0, _fec0, _ = _encode_media([b"a0xx", b"a1xx", b"a2xx"], seq0=0)
        g1, _fec1, _ = _encode_media([b"b0xx", b"b1xx", b"b2xx"], seq0=3)
        g2, _fec2, _ = _encode_media([b"c0xx", b"c1xx", b"c2xx"], seq0=6)
        asm = FecAssembler(3)
        out: list = []
        out += asm.ingest_media(*g0[0])
        for pkt in g1:
            out += asm.ingest_media(*pkt)
        self.assertEqual(out, [])
        for pkt in g2:
            out += asm.ingest_media(*pkt)
        seqs = [h.seq for h, _p in out]
        self.assertIn(0, seqs)
        self.assertEqual(seqs[-3:], [6, 7, 8])
        self.assertEqual(asm.unrecoverable, 1)
        self.assertEqual(asm.recovered, 0)


class MetricsFecTests(unittest.TestCase):
    def test_snapshot_fec_fields(self) -> None:
        m = ReceiverMetrics()
        m.set_fec_control(True, 3)
        m.set_fec_counts(4, 1, 10)
        snap = m.snapshot()
        self.assertTrue(snap["fec_enabled"])
        self.assertEqual(snap["fec_group"], 3)
        self.assertEqual(snap["fec_recovered"], 4)
        self.assertEqual(snap["fec_unrecoverable"], 1)
        self.assertIn("fec_recovered_ppm", snap)
        self.assertIn("fec_unrecoverable_ppm", snap)
        self.assertEqual(snap["fec_health"], "ok")  # window still 0 until refresh

    def test_classify_fec_health_colors(self) -> None:
        self.assertEqual(
            classify_fec_health(enabled=False, unrecoverable_window=0, unrecoverable_ppm=0.0),
            "off",
        )
        self.assertEqual(
            classify_fec_health(enabled=True, unrecoverable_window=0, unrecoverable_ppm=0.0),
            "ok",
        )
        self.assertEqual(
            classify_fec_health(enabled=True, unrecoverable_window=1, unrecoverable_ppm=100.0),
            "warn",
        )
        self.assertEqual(
            classify_fec_health(enabled=True, unrecoverable_window=2, unrecoverable_ppm=10_000.0),
            "fail",
        )

    def test_window_ppm(self) -> None:
        m = ReceiverMetrics()
        m.set_fec_control(True, 3)
        m.on_arrival_nominal_timing(21, 48000, 1.0)
        m.on_arrival_nominal_timing(21, 48000, 1.01)
        m.set_fec_counts(1, 1, 200)
        m.refresh_window()
        snap = m.snapshot()
        self.assertGreater(snap["fec_recovered_ppm"], 0.0)
        self.assertGreater(snap["fec_unrecoverable_ppm"], 0.0)
        self.assertLess(snap["fec_unrecoverable_ppm"], 10_000.0)
        self.assertEqual(snap["fec_health"], "warn")


if __name__ == "__main__":
    unittest.main()
