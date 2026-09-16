"""Multichannel WAV sidecar. Control/disk thread only — never the audio callback.

UDP/play threads may call ``offer``: copy + ``put_nowait``, drop on full.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time
import wave
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

import numpy as np

from ..virtual_device.port_names import fallback_label, sanitize_label, unique_port_names

log = logging.getLogger(__name__)

DEFAULT_QUEUE = 256


def default_record_path(project_root: Optional[Path] = None) -> Path:
    raw = os.getenv("XAIR_RECORD_PATH")
    if raw and str(raw).strip():
        return Path(str(raw).strip()).expanduser()
    base = Path(project_root) if project_root is not None else Path(".")
    return (base / "recordings").resolve()


def default_record_cmd_path(anchor: Optional[Path] = None) -> Path:
    raw = os.getenv("XAIR_RECORD_CMD_PATH")
    if raw and str(raw).strip():
        return Path(str(raw).strip()).expanduser()
    base = Path(anchor) if anchor is not None else Path(".")
    if base.is_file():
        base = base.parent
    return base / ".xair_record_cmd"


def default_record_state_path(anchor: Optional[Path] = None) -> Path:
    raw = os.getenv("XAIR_RECORD_STATE_PATH")
    if raw and str(raw).strip():
        return Path(str(raw).strip()).expanduser()
    base = Path(anchor) if anchor is not None else Path(".")
    if base.is_file():
        base = base.parent
    return base / ".xair_record.json"


def classify_record_health(
    *,
    enabled: bool,
    recording: bool,
    errors: int,
    dropped: int,
    frames: int,
    error_ppm: float = 0.0,
) -> str:
    """Dashboard color: ``off`` / ``ok`` / ``warn`` / ``fail``."""
    if not enabled or not recording:
        return "off"
    if int(errors) > 0 and float(error_ppm) >= 10_000.0:
        return "fail"
    if int(errors) > 0 or int(dropped) > 0:
        return "warn"
    if int(frames) <= 0:
        return "warn"
    return "ok"


def empty_record_snapshot(*, enabled: bool = False, path: Optional[str] = None) -> dict:
    return {
        "record_enabled": bool(enabled),
        "recording": False,
        "record_path": path,
        "record_files": [],
        "last_record_ts": None,
        "record_health": "off",
        "dropped": 0,
        "writes_err": 0,
        "frames": 0,
    }


def write_record_command(
    path: Path,
    action: str,
    *,
    record_path: Optional[str] = None,
) -> None:
    """IPC for CLI / dashboard → sidecar. Atomic replace; not on the audio path."""
    act = str(action).strip().lower()
    if act not in ("start", "stop"):
        raise ValueError("action must be start or stop")
    payload = {
        "action": act,
        "seq": time.time_ns(),
        "path": record_path,
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


def float_to_pcm24(samples: np.ndarray) -> bytes:
    """Clip float -1..1 to packed little-endian 24-bit PCM."""
    x = np.clip(np.asarray(samples, dtype=np.float64), -1.0, 1.0)
    i = np.rint(x * 8388607.0).astype(np.int32)
    np.clip(i, -8388608, 8388607, out=i)
    u = i.astype(np.uint32) & np.uint32(0xFFFFFF)
    out = np.empty(i.size * 3, dtype=np.uint8)
    out[0::3] = (u & np.uint32(0xFF)).astype(np.uint8)
    out[1::3] = ((u >> np.uint32(8)) & np.uint32(0xFF)).astype(np.uint8)
    out[2::3] = ((u >> np.uint32(16)) & np.uint32(0xFF)).astype(np.uint8)
    return out.tobytes()


def _atomic_write_json(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise


class WavRecorder:
    """Per-channel WAV writer on a dedicated thread."""

    def __init__(
        self,
        *,
        channels: int = 18,
        sample_rate: int = 48000,
        output_dir: Optional[Path] = None,
        cmd_path: Optional[Path] = None,
        state_path: Optional[Path] = None,
        labels_provider: Optional[Callable[[], Sequence[str]]] = None,
        queue_size: int = DEFAULT_QUEUE,
        auto_start: bool = False,
    ) -> None:
        nch = int(channels)
        if not (1 <= nch <= 32):
            raise ValueError("channels debe estar entre 1 y 32")
        self.channels = nch
        self.sample_rate = int(sample_rate)
        self.output_dir = Path(output_dir) if output_dir is not None else default_record_path()
        self.cmd_path = Path(cmd_path) if cmd_path is not None else None
        self.state_path = Path(state_path) if state_path is not None else None
        self._labels_provider = labels_provider
        self._q: "queue.Queue[Optional[np.ndarray]]" = queue.Queue(maxsize=max(1, int(queue_size)))
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._recording = False
        self._auto_start = bool(auto_start)
        self._writers: List[wave.Wave_write] = []
        self._files: List[str] = []
        self._session_dir: Optional[str] = None
        self.dropped = 0
        self.writes_err = 0
        self.frames = 0
        self.last_record_ts: Optional[float] = None
        self._cmd_seq = 0
        self._last_state_mono = 0.0

    @property
    def recording(self) -> bool:
        return self._recording

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="xair-record", daemon=True)
        self._thread.start()
        if self._auto_start:
            self.begin()
        log.info(
            "recorder sidecar dir=%s auto_start=%s",
            self.output_dir,
            "on" if self._auto_start else "off",
        )

    def stop(self) -> None:
        self.end()
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._flush_state()

    def offer(self, frames: np.ndarray) -> None:
        """UDP/play thread: never block. Drop if the sidecar is behind."""
        if not self._recording:
            return
        try:
            self._q.put_nowait(np.array(frames, dtype=np.float32, copy=True))
        except queue.Full:
            with self._lock:
                self.dropped += 1

    def begin(self, output_dir: Optional[Path] = None) -> None:
        """Open a new session. Recorder thread (or start()) only."""
        if output_dir is not None:
            self.output_dir = Path(output_dir)
        if self._recording:
            self.end()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        session = self.output_dir / stamp
        n = 1
        while session.exists():
            session = self.output_dir / f"{stamp}_{n}"
            n += 1
        session.mkdir(parents=True, exist_ok=True)
        labels = self._resolve_labels()
        writers: List[wave.Wave_write] = []
        files: List[str] = []
        try:
            for name in labels:
                path = session / f"{name}.wav"
                wf = wave.open(str(path), "wb")
                wf.setnchannels(1)
                wf.setsampwidth(3)
                wf.setframerate(self.sample_rate)
                writers.append(wf)
                files.append(str(path))
        except Exception:
            for wf in writers:
                try:
                    wf.close()
                except Exception:
                    pass
            raise
        with self._lock:
            self._writers = writers
            self._files = files
            self._session_dir = str(session)
            self.frames = 0
            self._recording = True
        self._flush_state()
        log.info("record start %s files=%s", session, len(files))

    def end(self) -> None:
        with self._lock:
            writers = self._writers
            self._writers = []
            self._recording = False
        for wf in writers:
            try:
                wf.close()
            except Exception:
                log.debug("wav close", exc_info=True)
        self._flush_state()
        if writers:
            log.info("record stop files=%s", len(self._files))

    def snapshot(self) -> dict:
        with self._lock:
            dropped = self.dropped
            err = self.writes_err
            frames = self.frames
            recording = self._recording
            files = list(self._files)
            last = self.last_record_ts
            path = self._session_dir or str(self.output_dir)
        tot = frames + dropped + err
        ppm = (float(err) / float(tot) * 1e6) if tot else 0.0
        return {
            "record_enabled": True,
            "recording": recording,
            "record_path": path,
            "record_files": files,
            "last_record_ts": last,
            "record_health": classify_record_health(
                enabled=True,
                recording=recording,
                errors=err,
                dropped=dropped,
                frames=frames,
                error_ppm=ppm,
            ),
            "dropped": dropped,
            "writes_err": err,
            "frames": frames,
        }

    def _resolve_labels(self) -> List[str]:
        raw: List[str] = []
        if self._labels_provider is not None:
            try:
                got = list(self._labels_provider() or [])
                raw = [sanitize_label(x, i) for i, x in enumerate(got, start=1)]
            except Exception:
                log.debug("record labels_provider", exc_info=True)
                raw = []
        if len(raw) < self.channels:
            raw.extend(fallback_label(i) for i in range(len(raw) + 1, self.channels + 1))
        return unique_port_names(raw[: self.channels])

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._poll_command()
            now = time.monotonic()
            if now - self._last_state_mono >= 1.0:
                self._last_state_mono = now
                self._flush_state()
            try:
                item = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                break
            self._write_block(item)

    def _write_block(self, frames: np.ndarray) -> None:
        if frames.ndim != 2 or frames.shape[1] < 1:
            return
        try:
            with self._lock:
                writers = self._writers
                nch = min(int(frames.shape[1]), self.channels, len(writers))
                if nch <= 0 or not self._recording:
                    return
                for c in range(nch):
                    writers[c].writeframes(float_to_pcm24(frames[:, c]))
                self.frames += int(frames.shape[0])
                self.last_record_ts = time.time()
        except Exception:
            with self._lock:
                self.writes_err += 1
            log.warning("record write failed", exc_info=True)

    def _poll_command(self) -> None:
        path = self.cmd_path
        if path is None or not path.is_file():
            return
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        seq = data.get("seq")
        try:
            seq_i = int(seq)
        except (TypeError, ValueError):
            return
        if seq_i == self._cmd_seq:
            return
        self._cmd_seq = seq_i
        action = str(data.get("action") or "").strip().lower()
        extra = data.get("path")
        try:
            if action == "start":
                dest = Path(str(extra)).expanduser() if extra else None
                self.begin(dest)
            elif action == "stop":
                self.end()
        except Exception:
            log.warning("record command %s failed", action, exc_info=True)

    def _flush_state(self) -> None:
        if self.state_path is None:
            return
        try:
            _atomic_write_json(self.state_path, self.snapshot())
        except Exception:
            log.debug("record state write", exc_info=True)
