"""UDP seq reorder + loss interpolation. No PortAudio/JACK.

Extracted so tests can exercise sequencing without importing receiver.py.
Logic matches the previous in-receiver implementation.
"""
from __future__ import annotations

import logging
from typing import Dict, Iterator, List, Optional, Tuple

import numpy as np

from .metrics import ReceiverMetrics

log = logging.getLogger(__name__)

SEQ_MASK = (1 << 32) - 1

# Salto sospechoso en seq (probable rollover o reinicio de emisor — re-sinc sin synth enorme)
SEQ_JUMP_RESET = 1 << 24


def seq_forward(origin: int, dest: int) -> int:
    """Puestos modulo 32 bit, muestras de secuencia hacia destino después de origin (sin llegar)."""
    return (dest - origin) & SEQ_MASK


def seq_is_behind(seq: int, watermark: int) -> bool:
    """True si seq es “viejo” respecto watermark (dentro del semicírculo)."""
    d = (watermark - seq) & SEQ_MASK
    return 0 < d < 0x8000_0000


def linear_interpolate_gap(
    tail_row: np.ndarray,
    head_row: np.ndarray,
    rows: int,
) -> np.ndarray:
    """Relleno lineal canal a canal entre la última muestra conocida y la primera del siguiente bloque."""
    if rows <= 0:
        return np.zeros((0, tail_row.shape[0]), dtype=np.float32)
    ch = int(tail_row.shape[0])
    alpha = (np.arange(1, rows + 1, dtype=np.float64)[:, None]) / float(rows + 1)
    t = tail_row.astype(np.float64)[None, :]
    h = head_row.astype(np.float64)[None, :]
    out = (t * (1.0 - alpha) + h * alpha).astype(np.float32)
    return np.clip(np.ascontiguousarray(out.reshape(rows, ch)), -1.0, 1.0)


class RecvReorderLost:
    """
    Buffer de llegada desordenadas + pérdidas: ordena por `seq`,
    ante huecos genera muestras con interpolación lineal entre bloques válidos.

    Cabecera UDP ya incluye `seq` y `ts_ns` (captura lado emisor; el jitter RTP clásico
    aquí usa además llegadas locales monotónicas).
    """

    def __init__(
        self,
        *,
        channels: int,
        nominal_samples_per_datagram: int,
        max_packets_pending: int,
    ) -> None:
        self.channels = channels
        self.nominal_samples_per_datagram = int(nominal_samples_per_datagram)
        self.max_packets_pending = max(16, max_packets_pending)
        self.pending: Dict[int, Tuple[np.ndarray, int]] = {}
        self.next_seq: Optional[int] = None
        self.tail_row = np.zeros((channels,), dtype=np.float32)
        self.tail_primed = False
        self.metrics: Optional[ReceiverMetrics] = None

    def set_metrics(self, m: ReceiverMetrics) -> None:
        self.metrics = m

    def push(self, seq: int, nframes_pkt: int, block: np.ndarray) -> List[np.ndarray]:
        """Inserta paquete; devuelve 0..N bloques listos (incl. sintéticos entre seq)."""
        seq = int(seq) & SEQ_MASK

        # Duplicados: conservar último valor
        if seq in self.pending:
            log.debug("seq duplicada %s, sobrescrita", seq)

        if self.next_seq is not None and seq_is_behind(seq, self.next_seq):
            if self.metrics:
                self.metrics.late_drop()
            log.debug("paquete tarde descartado seq=%s next=%s", seq, self.next_seq)
            return []

        self.pending[seq] = (np.ascontiguousarray(block, dtype=np.float32), int(nframes_pkt))

        while len(self.pending) > self.max_packets_pending:
            ks = sorted(self.pending.keys())
            stale = ks[0]
            if self.next_seq is not None:
                fwd = seq_forward(self.next_seq, stale)
                if fwd == 0 or fwd > SEQ_JUMP_RESET:
                    log.warning(
                        "backlog extremo seq=%s (next_seq=%s), descartando",
                        stale,
                        self.next_seq,
                    )
                    self.pending.pop(stale, None)
                    continue
                break
            self.pending.pop(stale, None)
            log.warning("backlog inicial descartando seq=%s", stale)

        return list(self._drain())

    def _drain(self) -> Iterator[np.ndarray]:
        """Entrega ordenado por `seq`; ante huecos de `fwd` paquetes, interpola muestras lineales."""
        while self.pending:
            if self.next_seq is None:
                self.next_seq = min(self.pending.keys())

            if self.next_seq in self.pending:
                blk, _nf = self.pending.pop(self.next_seq)
                self._after_real_block(blk)
                yield blk
                self.next_seq = (self.next_seq + 1) & SEQ_MASK
                continue

            earliest = min(self.pending.keys())
            assert self.next_seq is not None

            # Punto llegado muy tarde (seq < siguiente esperado)
            late_dist = (self.next_seq - earliest) & SEQ_MASK
            if 0 < late_dist < 0x8000_0000:
                self.pending.pop(earliest)
                if self.metrics:
                    self.metrics.late_drop()
                log.debug("descartando bloque obsoleto seq=%s (next=%s)", earliest, self.next_seq)
                continue

            fwd = seq_forward(self.next_seq, earliest)

            if fwd == 0 or fwd >= SEQ_JUMP_RESET:
                log.warning(
                    "re-sinc seq %s → %s (saltar sin relleno; reinicio/perdida masiva)",
                    self.next_seq,
                    earliest,
                )
                self.next_seq = earliest
                continue

            # Falta(next_seq … earliest): `fwd` = número de secuencias a cubrir antes de reproducir earliest
            lost_pkts = int(fwd)
            blk_start, _nf_start = self.pending[earliest]
            head_row = blk_start[0].copy()
            if not self.tail_primed:
                self.tail_row.fill(0.0)
            lost_rows = lost_pkts * self.nominal_samples_per_datagram
            synth = linear_interpolate_gap(self.tail_row, head_row, lost_rows)
            if self.metrics:
                self.metrics.add_lost(lost_pkts)
            if synth.shape[0] > 0:
                self.tail_row = synth[-1].copy()
                self.tail_primed = True
                yield synth
            self.next_seq = earliest
            continue

    def _after_real_block(self, blk: np.ndarray) -> None:
        self.tail_row = blk[-1].copy()
        self.tail_primed = True
