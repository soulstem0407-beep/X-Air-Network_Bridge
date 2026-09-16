"""
Captura multicanal vía PortAudio (sounddevice).

La X18 expone perfil Audio USB estándar: PortAudio es la API estable documentada.
libusb directo sobre UAC para 18 canales no es una ruta habitual para apps de audio.

Convierte buffers float32 (interno típico de sounddevice) a PCM según configuración de red.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np

log = logging.getLogger(__name__)

try:
    import sounddevice as sd
except ImportError as e:  # pragma: no cover
    sd = None
    _IMPORT_ERROR = e
else:
    _IMPORT_ERROR = None


@dataclass
class CaptureConfig:
    sample_rate: int = 48_000
    channels: int = 18
    device_query: Optional[str] = None
    blocksize: int = 256

    def __post_init__(self) -> None:
        if self.channels < 1:
            raise ValueError("channels debe ser >= 1")


class USBCapture:
    """Streams de entrada X18/USB multicanal."""

    def __init__(self, config: CaptureConfig) -> None:
        if sd is None:
            raise RuntimeError(
                "Falta sounddevice/PortAudio. Instala dependencias: pip install sounddevice"
            ) from _IMPORT_ERROR
        self.config = config
        self._stream: Optional[sd.InputStream] = None

    def resolve_device_index(self) -> int:
        """Always the X18. Never HDMI, never PortAudio default (``device=None``).

        ``device_query`` / ``XAIR_INPUT_DEVICE`` are ignored: empty query used
        to mean “host default”, which on Linux is often ``hw:HDMI``.
        """
        from ..virtual_device.x18_device import require_x18_portaudio_device

        picked = require_x18_portaudio_device(
            sd,
            min_input_channels=self.config.channels,
        )
        log.info("Entrada seleccionada: [%s] %s", picked.index, picked.name)
        return int(picked.index)

    def start(self, callback: Callable[[np.ndarray], None]) -> None:
        dev = self.resolve_device_index()

        def wrapped_cb(indata, frames, _time, status) -> None:  # type: ignore[no-untyped-def]
            if status:
                log.debug("PortAudio status: %s", status)
            block = np.ascontiguousarray(indata, dtype=np.float32)
            callback(block)

        sr = float(self.config.sample_rate)
        bs = int(self.config.blocksize)
        latency_s = min(0.05, max(bs * 2.0 / sr, 8.0 / sr))
        base_kw = dict(
            device=dev,
            samplerate=self.config.sample_rate,
            channels=self.config.channels,
            dtype="float32",
            callback=wrapped_cb,
            blocksize=bs,
            latency=latency_s,
        )
        try:
            self._stream = sd.InputStream(
                **dict(base_kw, prime_output_buffers_using_stream_callback=False)
            )
        except TypeError:
            self._stream = sd.InputStream(**base_kw)
        self._stream.start()
        log.info(
            "Captura iniciada: %s Hz, %s canales, block=%s latency_s≥%.5f",
            self.config.sample_rate,
            self.config.channels,
            self.config.blocksize,
            latency_s,
        )

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            log.info("Captura detenida.")
