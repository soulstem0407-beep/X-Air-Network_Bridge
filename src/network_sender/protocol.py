"""
Fixed UDP binary header for X-Air Network Bridge.

Datagramas cortos para MTU típico con 18 canales.
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Tuple

MAGIC = b"XBRI"


class Codec(IntEnum):
    PCM16 = 1
    PCM24 = 2
    FLAC = 3


# magic, ver, codec, flags, extra, sr, ch, nframes, seq uint32; ts_ns uint64 marca build_header (captura lado emisor)
# flags: FLAG_FEC = paridad (no ocupa seq). FLAG_OPUS = payload Opus (sí usa seq de media).
# extra: posición 0..N-1 (media) o N (FEC).
HEADER_STRUCT = struct.Struct("!4sBBBBIHHIQ")
HEADER_SIZE = HEADER_STRUCT.size
FLAG_FEC = 0x01
FLAG_OPUS = 0x02


def build_header(
    *,
    codec: Codec,
    sample_rate: int,
    channels: int,
    nframes: int,
    seq: int,
    flags: int = 0,
    extra: int = 0,
) -> bytes:
    if channels > 0xFFFF or nframes > 0xFFFF:
        raise ValueError("canal/frame fuera de rango u16")
    ts_ns = time.time_ns()
    return HEADER_STRUCT.pack(
        MAGIC,
        1,
        int(codec),
        int(flags) & 0xFF,
        int(extra) & 0xFF,
        int(sample_rate),
        int(channels),
        int(nframes),
        int(seq % (1 << 32)),
        ts_ns,
    )


@dataclass
class ParsedHeader:
    version: int
    codec: Codec
    sample_rate: int
    channels: int
    nframes: int
    seq: int
    ts_ns: int
    flags: int = 0
    extra: int = 0
    recovered: bool = False


def is_fec_packet(hdr: ParsedHeader) -> bool:
    return bool(int(hdr.flags) & FLAG_FEC)


def is_opus_packet(hdr: ParsedHeader) -> bool:
    return bool(int(hdr.flags) & FLAG_OPUS)


def parse_header(buf: bytes) -> Tuple[ParsedHeader, bytes]:
    if len(buf) < HEADER_SIZE:
        raise ValueError("cabecera truncada")
    (
        mg,
        ver,
        codec_b,
        flags_b,
        extra_b,
        sr,
        ch,
        nf,
        seq,
        ts_ns,
    ) = HEADER_STRUCT.unpack_from(buf, 0)
    if mg != MAGIC:
        raise ValueError("magic inválido")
    payload = buf[HEADER_SIZE:]
    ph = ParsedHeader(
        version=int(ver),
        codec=Codec(codec_b),
        sample_rate=int(sr),
        channels=int(ch),
        nframes=int(nf),
        seq=int(seq),
        ts_ns=int(ts_ns),
        flags=int(flags_b),
        extra=int(extra_b),
    )
    return ph, payload


def inspect_packet_mtu(nframes: int, channels: int, codec: Codec) -> int:
    if codec == Codec.PCM16:
        return nframes * channels * 2
    if codec == Codec.PCM24:
        return nframes * channels * 3
    return -1
