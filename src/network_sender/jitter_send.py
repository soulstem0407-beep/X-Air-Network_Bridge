"""Cola corta opcional en el emisor (alisado de ráfagas cortas)."""

from collections import deque
from typing import Deque, Iterator


class SendJitterBuffer:
    def __init__(self, max_packets: int = 2) -> None:
        self.max_packets = max(1, max_packets)
        self._q: Deque[bytes] = deque()

    def push(self, pkt: bytes) -> None:
        self._q.append(pkt)
        while len(self._q) > self.max_packets:
            self._q.popleft()

    def pop_all(self) -> Iterator[bytes]:
        while self._q:
            yield self._q.popleft()
