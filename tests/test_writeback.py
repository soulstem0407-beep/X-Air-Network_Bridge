"""OSC DAW write-back on the control thread. No mixer hardware."""

from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from src.osc.x18_reaper_sync import EchoGuard
from src.sync.writeback import MixWriteBack, WriteJob
from src.xair_control.osc_bridge import XAirOSCError


class FakeBridge:
    xremote_active = True

    def __init__(self) -> None:
        self.faders: Dict[int, float] = {}
        self.set_log: List[Tuple[str, Any, str]] = []
        self.listener = None

    def start_xremote(self, cb: Any) -> None:
        self.listener = cb

    def stop_xremote(self) -> None:
        self.listener = None

    def set_fader(self, ch: int, value: float) -> float:
        self.set_log.append(("fader", (ch, float(value)), threading.current_thread().name))
        self.faders[int(ch)] = float(value)
        return float(value)

    def set_mute(self, ch: int, state: bool) -> bool:
        self.set_log.append(("mute", (ch, bool(state)), threading.current_thread().name))
        return bool(state)

    def set_pan(self, ch: int, value: float) -> float:
        self.set_log.append(("pan", (ch, float(value)), threading.current_thread().name))
        return float(value)

    def set_send(self, ch: int, send_idx: int, value: float) -> float:
        self.set_log.append(("send", (ch, send_idx, float(value)), threading.current_thread().name))
        return float(value)

    def set_bus_fader(self, bus: int, value: float) -> float:
        self.set_log.append(("bus", (bus, float(value)), threading.current_thread().name))
        return float(value)


class FakeSync:
    def __init__(self) -> None:
        self.osc_bridge = FakeBridge()
        self.osc_io_lock = threading.Lock()
        self.notes: List[int] = []
        self.keys: List[Tuple[str, Any]] = []
        self.mixer_params: List[Any] = []

    def note_writeback(self, ts_ns: int) -> None:
        self.notes.append(int(ts_ns))

    def apply_local_mix_from_key(self, key: str, value: Any) -> None:
        self.keys.append((key, value))

    def apply_mixer_param(self, param: Any, raw: Any) -> None:
        self.mixer_params.append((param, raw))


class EchoGuardTests(unittest.TestCase):
    def test_equal_value_blocked(self) -> None:
        g = EchoGuard(0.25)
        self.assertTrue(g.allow("ch:1:fader", 0.5, "daw", "float"))
        g.remember("ch:1:fader", 0.5, "daw", "float")
        self.assertFalse(g.allow("ch:1:fader", 0.5, "daw", "float"))

    def test_other_origin_suppressed(self) -> None:
        g = EchoGuard(1.0)
        g.remember("ch:1:fader", 0.2, "daw", "float")
        self.assertFalse(g.allow("ch:1:fader", 0.9, "xair", "float"))


class WriteBackThreadTests(unittest.TestCase):
    def _wait_sets(self, br: FakeBridge, n: int = 1, timeout_s: float = 2.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if len(br.set_log) >= n:
                return
            time.sleep(0.01)
        self.fail(f"write-back SET not seen ({len(br.set_log)} < {n})")

    def test_daw_set_runs_on_control_thread_xremote_does_not_set(self) -> None:
        sync = FakeSync()
        wb = MixWriteBack(sync, channels=4, listen_host="127.0.0.1", listen_port=0)
        wb._stop.clear()
        t = threading.Thread(target=wb._control_loop, name="xair-writeback", daemon=True)
        t.start()
        try:
            wb._on_daw_fader("/track/1/volume", 0.61)
            self._wait_sets(sync.osc_bridge, 1)
            path, args, tname = sync.osc_bridge.set_log[0]
            self.assertEqual(path, "fader")
            self.assertEqual(args[0], 1)
            self.assertAlmostEqual(args[1], 0.61)
            self.assertEqual(tname, "xair-writeback")
            self.assertTrue(sync.notes)
            self.assertEqual(sync.keys[-1][0], "ch:1:fader")

            before = list(sync.osc_bridge.set_log)
            wb._on_mixer_message(
                SimpleNamespace(address="/ch/01/mix/fader", params=[0.2])
            )
            time.sleep(0.05)
            self.assertEqual(sync.osc_bridge.set_log, before)
            self.assertTrue(sync.mixer_params)
        finally:
            wb._stop.set()
            wb._jobs.put_nowait(None)
            t.join(timeout=1.5)

    def test_apply_error_does_not_count_ok(self) -> None:
        sync = FakeSync()

        def boom(ch: int, value: float) -> float:
            raise XAirOSCError("offline")

        sync.osc_bridge.set_fader = boom  # type: ignore[method-assign]
        wb = MixWriteBack(sync, channels=2)
        job = WriteJob("daw", "float", "ch:1:fader", 0.4, ())
        wb._apply_daw(job)
        self.assertEqual(wb.writes_ok, 0)


if __name__ == "__main__":
    unittest.main()
