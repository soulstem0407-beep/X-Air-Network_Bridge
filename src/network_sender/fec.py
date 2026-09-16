"""XOR FEC for XBRI UDP: one parity datagram per group of N media packets.

Reconstruction belongs on the UDP receive thread, never in a JACK/PipeWire
or PortAudio playback callback. FEC packets do not consume media ``seq``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Dict, List, Optional, Sequence, Tuple

from .protocol import FLAG_FEC, Codec, ParsedHeader, build_header

SEQ_MASK = (1 << 32) - 1
MIN_FEC_GROUP = 2
MAX_FEC_GROUP = 16
DEFAULT_FEC_GROUP = 3
# Hold at most this many groups so a late FEC can still recover one hole.
MAX_HELD_GROUPS = 2


def clamp_fec_group(n: int) -> int:
    try:
        v = int(n)
    except (TypeError, ValueError):
        return DEFAULT_FEC_GROUP
    return max(MIN_FEC_GROUP, min(MAX_FEC_GROUP, v))


def xor_bytes(chunks: Sequence[bytes]) -> bytes:
    """XOR of payloads, zero-padded to the longest chunk."""
    if not chunks:
        return b""
    n = max(len(c) for c in chunks)
    acc = bytearray(n)
    for c in chunks:
        for i, b in enumerate(c):
            acc[i] ^= b
    return bytes(acc)


class FecEncoder:
    """Accumulate N media payloads and emit one XOR parity datagram."""

    def __init__(self, group: int = DEFAULT_FEC_GROUP) -> None:
        self.group = clamp_fec_group(group)
        self._payloads: List[bytes] = []
        self._start_seq = 0
        self._codec = Codec.PCM24
        self._sample_rate = 48000
        self._channels = 18
        self._nframes = 0

    def position_for_next(self) -> int:
        return len(self._payloads)

    def add_media(
        self,
        *,
        codec: Codec,
        sample_rate: int,
        channels: int,
        nframes: int,
        seq: int,
        payload: bytes,
    ) -> Optional[bytes]:
        """Store a media payload. Returns a FEC datagram after every ``group`` packets."""
        if not self._payloads:
            self._start_seq = int(seq) & SEQ_MASK
        self._payloads.append(payload)
        self._codec = codec
        self._sample_rate = int(sample_rate)
        self._channels = int(channels)
        self._nframes = int(nframes)
        if len(self._payloads) < self.group:
            return None
        xor_payload = xor_bytes(self._payloads)
        fec = (
            build_header(
                codec=self._codec,
                sample_rate=self._sample_rate,
                channels=self._channels,
                nframes=self._nframes,
                seq=self._start_seq,
                flags=FLAG_FEC,
                extra=self.group,
            )
            + xor_payload
        )
        self._payloads.clear()
        return fec


@dataclass
class _Group:
    start_seq: int
    n: int
    media: Dict[int, Tuple[ParsedHeader, bytes]] = field(default_factory=dict)
    fec_payload: Optional[bytes] = None
    closed: bool = False


class FecAssembler:
    """Hold open FEC groups, recover exactly one missing media packet via XOR.

    Releases packets in seq order once a group is closed (complete, recovered,
    or unrecoverable). Call only from the UDP receive thread.
    """

    def __init__(self, group: int = DEFAULT_FEC_GROUP) -> None:
        self.group = clamp_fec_group(group)
        self._groups: Dict[int, _Group] = {}
        self._next_start: Optional[int] = None
        self.recovered = 0
        self.unrecoverable = 0
        self.groups_closed = 0

    def ingest_media(self, hdr: ParsedHeader, payload: bytes) -> List[Tuple[ParsedHeader, bytes]]:
        pos = int(hdr.extra)
        if pos < 0 or pos >= self.group:
            pos = 0
        start = (int(hdr.seq) - pos) & SEQ_MASK
        if self._already_released(start):
            return []
        g = self._ensure(start)
        g.media[pos] = (replace(hdr, flags=0, extra=pos, recovered=False), payload)
        self._maybe_close(g)
        self._evict(start)
        return self._collect()

    def ingest_fec(self, hdr: ParsedHeader, payload: bytes) -> List[Tuple[ParsedHeader, bytes]]:
        start = int(hdr.seq) & SEQ_MASK
        if self._already_released(start):
            return []
        n = int(hdr.extra) if int(hdr.extra) >= MIN_FEC_GROUP else self.group
        g = self._ensure(start, n=clamp_fec_group(n))
        g.fec_payload = payload
        self._maybe_close(g)
        self._evict(start)
        return self._collect()

    def on_idle(self) -> List[Tuple[ParsedHeader, bytes]]:
        """Force-close held groups when the socket times out (end of burst)."""
        for start in sorted(self._groups, key=lambda s: (s - (self._next_start or 0)) & SEQ_MASK):
            self._force_close(self._groups[start])
        return self._collect()

    def _ensure(self, start: int, n: Optional[int] = None) -> _Group:
        g = self._groups.get(start)
        if g is None:
            g = _Group(start_seq=start, n=int(n) if n is not None else self.group)
            self._groups[start] = g
            if self._next_start is None:
                self._next_start = start
        return g

    def _already_released(self, start: int) -> bool:
        if self._next_start is None:
            return False
        d = (self._next_start - start) & SEQ_MASK
        return 0 < d < 0x8000_0000

    def _maybe_close(self, g: _Group) -> None:
        if g.closed:
            return
        missing = [i for i in range(g.n) if i not in g.media]
        if not missing:
            g.closed = True
            self.groups_closed += 1
            return
        if len(missing) == 1 and g.fec_payload is not None:
            rec_hdr, rec_pl = _reconstruct(g, missing[0])
            g.media[missing[0]] = (rec_hdr, rec_pl)
            g.closed = True
            self.recovered += 1
            self.groups_closed += 1
            return

    def _force_close(self, g: _Group) -> None:
        if g.closed:
            return
        self._maybe_close(g)
        if g.closed:
            return
        missing = [i for i in range(g.n) if i not in g.media]
        if len(missing) >= 2 or (len(missing) == 1 and g.fec_payload is None) or not g.media:
            self.unrecoverable += 1
        g.closed = True
        self.groups_closed += 1

    def _evict(self, newest_start: int) -> None:
        if self._next_start is None:
            return
        # Skip groups that never appeared once we are two groups ahead.
        while True:
            gap = (newest_start - self._next_start) & SEQ_MASK
            if gap < self.group * MAX_HELD_GROUPS:
                break
            if self._next_start in self._groups:
                self._force_close(self._groups[self._next_start])
                break
            self.unrecoverable += 1
            self.groups_closed += 1
            self._next_start = (self._next_start + self.group) & SEQ_MASK

        held = [s for s, g in self._groups.items() if not g.closed]
        if len(held) <= MAX_HELD_GROUPS:
            return
        held.sort(key=lambda s: (s - (self._next_start or 0)) & SEQ_MASK)
        for start in held[: len(held) - MAX_HELD_GROUPS]:
            self._force_close(self._groups[start])

    def _collect(self) -> List[Tuple[ParsedHeader, bytes]]:
        out: List[Tuple[ParsedHeader, bytes]] = []
        if self._next_start is None:
            return out
        while self._next_start in self._groups:
            g = self._groups[self._next_start]
            if not g.closed:
                break
            for pos in range(g.n):
                pkt = g.media.get(pos)
                if pkt is not None:
                    out.append(pkt)
            del self._groups[self._next_start]
            self._next_start = (self._next_start + g.n) & SEQ_MASK
        return out


def _reconstruct(g: _Group, missing_pos: int) -> Tuple[ParsedHeader, bytes]:
    sib_hdr, sib_pl = next(iter(g.media.values()))
    chunks = [pl for pos, (_h, pl) in g.media.items() if pos != missing_pos]
    chunks.append(g.fec_payload or b"")
    payload = xor_bytes(chunks)
    if len(payload) > len(sib_pl):
        payload = payload[: len(sib_pl)]
    elif len(payload) < len(sib_pl):
        payload = payload + bytes(len(sib_pl) - len(payload))
    hdr = replace(
        sib_hdr,
        seq=(g.start_seq + missing_pos) & SEQ_MASK,
        flags=0,
        extra=missing_pos,
        recovered=True,
    )
    return hdr, payload
