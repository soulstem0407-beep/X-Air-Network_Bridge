"""EQ / dyn / FX OSC paths, health, and control-thread writes. No audio hardware."""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Tuple

from src.dashboard.reader import build_view
from src.xair_control.dsp import (
    classify_dsp_health,
    dyn_path,
    eq_band_path,
    eq_on_path,
    fx_par_path,
    fx_return_path,
    fx_send_path,
    fx_type_path,
    gate_path,
    hpf_path,
    parse_dsp_address,
)
from src.xair_control.dsp_control import DspController
from src.xair_control.osc_bridge import XAirOSCBridge


class PathAndParseTests(unittest.TestCase):
    def test_path_builders(self) -> None:
        self.assertEqual(eq_on_path(1), "/ch/01/eq/on")
        self.assertEqual(eq_band_path(2, 3, "g"), "/ch/02/eq/3/g")
        self.assertEqual(eq_band_path(2, 1, "f"), "/ch/02/eq/1/f")
        self.assertEqual(eq_band_path(2, 4, "q"), "/ch/02/eq/4/q")
        self.assertEqual(hpf_path(18), "/ch/18/preamp/hpf")
        self.assertEqual(gate_path(4, "thr"), "/ch/04/gate/thr")
        self.assertEqual(dyn_path(5, "ratio"), "/ch/05/dyn/ratio")
        self.assertEqual(fx_send_path(1, 2), "/ch/01/mix/02")
        self.assertEqual(fx_return_path(3, "fader"), "/rtn/03/mix/fader")
        self.assertEqual(fx_type_path(1), "/fx/1")
        self.assertEqual(fx_par_path(2, 7), "/fx/2/par/07")

    def test_parse_eq_hpf_gate_dyn_fx(self) -> None:
        eq = parse_dsp_address("/ch/01/eq/on")
        self.assertIsNotNone(eq)
        self.assertEqual(eq.section, "eq")
        self.assertEqual(eq.ch, 1)
        self.assertEqual(eq.leaf, "on")

        band = parse_dsp_address("/ch/03/eq/2/q")
        self.assertEqual(band.section, "eq")
        self.assertEqual(band.ch, 3)
        self.assertEqual(band.band, 2)
        self.assertEqual(band.leaf, "q")

        hpf = parse_dsp_address("/ch/08/preamp/hpf")
        self.assertEqual(hpf.section, "eq")
        self.assertEqual(hpf.leaf, "hpf")

        gate = parse_dsp_address("/ch/02/gate/attack")
        self.assertEqual(gate.section, "dyn")
        self.assertEqual(gate.leaf, "gate_attack")

        dyn = parse_dsp_address("/ch/02/dyn/mgain")
        self.assertEqual(dyn.section, "dyn")
        self.assertEqual(dyn.leaf, "mgain")

        send = parse_dsp_address("/ch/01/mix/03")
        self.assertEqual(send.section, "fx")
        self.assertEqual(send.fx, 3)
        self.assertEqual(send.leaf, "send")

        send_lvl = parse_dsp_address("/ch/01/mix/01/level")
        self.assertEqual(send_lvl.section, "fx")

        ret = parse_dsp_address("/rtn/04/mix/pan")
        self.assertEqual(ret.section, "fx")
        self.assertEqual(ret.fx, 4)
        self.assertEqual(ret.leaf, "return_pan")

        fxt = parse_dsp_address("/fx/2")
        self.assertEqual(fxt.section, "fx")
        self.assertEqual(fxt.leaf, "type")

        par = parse_dsp_address("/fx/1/par/12")
        self.assertEqual(par.leaf, "par_12")

    def test_parse_ignores_unrelated(self) -> None:
        self.assertIsNone(parse_dsp_address("/ch/01/mix/fader"))
        self.assertIsNone(parse_dsp_address("/ch/01/mix/on"))
        self.assertIsNone(parse_dsp_address("/lr/mix/fader"))
        self.assertIsNone(parse_dsp_address("/headamp/01/gain"))
        self.assertIsNone(parse_dsp_address("/ch/01/config/name"))


class HealthTests(unittest.TestCase):
    def test_classify_dsp_health(self) -> None:
        self.assertEqual(
            classify_dsp_health(enabled=False, errors_window=0, error_ppm=0.0),
            "off",
        )
        self.assertEqual(
            classify_dsp_health(enabled=True, errors_window=0, error_ppm=0.0),
            "ok",
        )
        self.assertEqual(
            classify_dsp_health(
                enabled=True,
                errors_window=0,
                error_ppm=0.0,
                last_write_age_s=45.0,
            ),
            "warn",
        )
        self.assertEqual(
            classify_dsp_health(enabled=True, errors_window=1, error_ppm=100.0),
            "warn",
        )
        self.assertEqual(
            classify_dsp_health(enabled=True, errors_window=5, error_ppm=10_000.0),
            "fail",
        )


class FakeOscBridge:
    """In-memory GET/SET used by XAirOSCBridge DSP helpers and DspController."""

    xremote_active = True
    get_eq_on = XAirOSCBridge.get_eq_on
    set_eq_on = XAirOSCBridge.set_eq_on
    get_eq_band = XAirOSCBridge.get_eq_band
    set_eq_band = XAirOSCBridge.set_eq_band
    get_hpf = XAirOSCBridge.get_hpf
    set_hpf = XAirOSCBridge.set_hpf
    get_gate = XAirOSCBridge.get_gate
    set_gate = XAirOSCBridge.set_gate
    get_dyn = XAirOSCBridge.get_dyn
    set_dyn = XAirOSCBridge.set_dyn
    get_fx_send = XAirOSCBridge.get_fx_send
    set_fx_send = XAirOSCBridge.set_fx_send
    get_fx_return = XAirOSCBridge.get_fx_return
    set_fx_return = XAirOSCBridge.set_fx_return
    get_fx_type = XAirOSCBridge.get_fx_type
    set_fx_type = XAirOSCBridge.set_fx_type
    get_fx_par = XAirOSCBridge.get_fx_par
    set_fx_par = XAirOSCBridge.set_fx_par

    def __init__(self) -> None:
        self.values: Dict[str, float] = {}
        self.set_log: List[Tuple[str, float, str]] = []
        self.listener = None

    def add_xremote_listener(self, on_message: Any) -> None:
        self.listener = on_message

    def start_xremote(self, on_message: Any) -> None:
        raise AssertionError("must not own /xremote when already active")

    def get_float_param(self, path: str) -> float:
        return float(self.values.get(path, 0.0))

    def set_float_param(self, path: str, value: float) -> float:
        self.set_log.append((path, float(value), threading.current_thread().name))
        self.values[path] = float(value)
        return float(value)

    def get_int_param(self, path: str) -> int:
        return int(round(float(self.values.get(path, 0))))

    def set_int_param(self, path: str, value: int) -> int:
        self.set_log.append((path, float(value), threading.current_thread().name))
        self.values[path] = float(value)
        return int(value)


class BridgeRoundtripTests(unittest.TestCase):
    def test_eq_dyn_fx_get_set(self) -> None:
        br = FakeOscBridge()
        self.assertTrue(br.set_eq_on(1, True))
        self.assertTrue(br.get_eq_on(1))
        self.assertAlmostEqual(br.set_hpf(1, 0.22), 0.22)
        self.assertAlmostEqual(br.get_hpf(1), 0.22)
        self.assertAlmostEqual(br.set_eq_band(1, 2, "g", 0.61), 0.61)
        self.assertAlmostEqual(br.get_eq_band(1, 2, "q"), 0.0)
        self.assertAlmostEqual(br.set_eq_band(1, 2, "q", 0.4), 0.4)
        self.assertAlmostEqual(br.set_gate(3, "thr", 0.3), 0.3)
        self.assertAlmostEqual(br.get_gate(3, "thr"), 0.3)
        self.assertAlmostEqual(br.set_dyn(3, "ratio", 0.55), 0.55)
        self.assertAlmostEqual(br.get_dyn(3, "ratio"), 0.55)
        self.assertAlmostEqual(br.set_fx_send(4, 1, 0.15), 0.15)
        self.assertAlmostEqual(br.get_fx_send(4, 1), 0.15)
        self.assertAlmostEqual(br.set_fx_return(2, "fader", 0.8), 0.8)
        self.assertEqual(br.set_fx_type(1, 7), 7)
        self.assertEqual(br.get_fx_type(1), 7)
        self.assertAlmostEqual(br.set_fx_par(1, 3, 0.9), 0.9)
        self.assertAlmostEqual(br.get_fx_par(1, 3), 0.9)


class FakeSync:
    def __init__(self, bridge: FakeOscBridge) -> None:
        self.osc_bridge = bridge
        self.osc_io_lock = threading.Lock()
        self.notes: List[Tuple[str, int, Optional[int], str]] = []
        self.channels: Dict[int, SimpleNamespace] = {
            i: SimpleNamespace(
                last_eq_write_ns=None,
                last_dyn_write_ns=None,
                last_fx_write_ns=None,
            )
            for i in range(1, 19)
        }

    def note_dsp_write(self, section: str, ts_ns: int, *, ch: Optional[int] = None) -> None:
        self.notes.append((section, int(ts_ns), ch, threading.current_thread().name))
        if ch is not None and ch in self.channels:
            st = self.channels[ch]
            if section == "eq":
                st.last_eq_write_ns = int(ts_ns)
            elif section == "dyn":
                st.last_dyn_write_ns = int(ts_ns)
            elif section == "fx":
                st.last_fx_write_ns = int(ts_ns)


class ControlThreadTests(unittest.TestCase):
    def _wait_ok(self, ctrl: DspController, n: int = 1, timeout_s: float = 2.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if ctrl.writes_ok >= n:
                return
            time.sleep(0.01)
        self.fail(f"control thread did not apply {n} job(s); ok={ctrl.writes_ok} err={ctrl.writes_err}")

    def test_set_runs_on_control_thread_not_xremote(self) -> None:
        br = FakeOscBridge()
        sync = FakeSync(br)
        ctrl = DspController(sync, eq_enabled=True, dyn_enabled=True, fx_enabled=True)
        ctrl.start()
        try:
            self.assertIsNotNone(br.listener)
            ctrl.write_eq_band(1, 1, "g", 0.5)
            self._wait_ok(ctrl, 1)
            self.assertEqual(len(br.set_log), 1)
            path, value, tname = br.set_log[0]
            self.assertEqual(path, "/ch/01/eq/1/g")
            self.assertAlmostEqual(value, 0.5)
            self.assertEqual(tname, "xair-dsp")
            self.assertEqual(sync.notes[-1][3], "xair-dsp")
            self.assertEqual(ctrl.snapshot()["last_change"]["origin"], "control")
            self.assertEqual(ctrl.snapshot()["eq_health"], "ok")

            before = list(br.set_log)
            br.listener(
                SimpleNamespace(address="/ch/01/eq/2/f", params=[0.33])
            )
            time.sleep(0.05)
            self.assertEqual(br.set_log, before)
            self.assertEqual(ctrl.last_change["origin"], "mixer")
            self.assertEqual(ctrl.last_change["path"], "/ch/01/eq/2/f")
            self.assertIsNotNone(sync.channels[1].last_eq_write_ns)
        finally:
            ctrl.stop()

    def test_disabled_section_does_not_enqueue(self) -> None:
        br = FakeOscBridge()
        ctrl = DspController(FakeSync(br), eq_enabled=False, dyn_enabled=True)
        ctrl.start()
        try:
            ctrl.write_eq_on(1, True)
            ctrl.write_gate(2, "on", 1.0)
            self._wait_ok(ctrl, 1)
            self.assertEqual(len(br.set_log), 1)
            self.assertEqual(br.set_log[0][0], "/ch/02/gate/on")
        finally:
            ctrl.stop()


class DashboardDspTests(unittest.TestCase):
    def test_build_view_includes_dsp_health_and_channel_timestamps(self) -> None:
        payload = {
            "schema": 1,
            "active": True,
            "report_interval_sec": 5.0,
            "report": {
                "level_tolerance_db": 6.0,
                "global_drift_ns": 0,
                "dsp": {
                    "eq_enabled": True,
                    "dyn_enabled": True,
                    "fx_enabled": False,
                    "eq_health": "ok",
                    "dyn_health": "warn",
                    "fx_health": "off",
                    "last_eq_write_ns": 100,
                    "last_dyn_write_ns": 200,
                    "last_fx_write_ns": None,
                    "last_change": {
                        "section": "eq",
                        "ch": 1,
                        "path": "/ch/01/eq/1/g",
                        "value": 0.5,
                        "ts_ns": 100,
                    },
                },
                "channels": [
                    {
                        "ch": 1,
                        "name": "Kick",
                        "fader": 0.7,
                        "rms_audio": 0.01,
                        "meter_osc": -10,
                        "delta_level": 1.0,
                        "level_ok": True,
                        "drift_ns": 12,
                        "last_eq_write_ns": 100,
                        "last_dyn_write_ns": 200,
                        "last_fx_write_ns": None,
                    }
                ],
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".xair_sync_state.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            view = build_view(path, payload, now=path.stat().st_mtime)
            self.assertEqual(view.dsp["eq_health"], "ok")
            self.assertEqual(view.dsp["dyn_health"], "warn")
            self.assertEqual(view.dsp["last_change"]["path"], "/ch/01/eq/1/g")
            self.assertEqual(view.channels[0].last_eq_write_ns, 100)
            self.assertEqual(view.channels[0].last_dyn_write_ns, 200)


if __name__ == "__main__":
    unittest.main()
