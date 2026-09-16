"""Roundtrip tests for OSC scene dump/recall. No hardware, no UDP audio."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple
from unittest.mock import patch

from src.xair_control.osc_bridge import XAirOSCError, XAirOscTimeoutError
from src.xair_control.scene import (
    SCENE_KIND,
    SCENE_SCHEMA,
    SceneScope,
    apply_scene,
    dump_scene,
    load_scene_file,
    save_scene_file,
    validate_scene,
)


class FakeBridge:
    """In-memory stand-in for ``XAirOSCBridge`` GET/SET used by scenes."""

    def __init__(self) -> None:
        self.host = "192.0.2.10"
        self.port = 10024
        self.names = {i: f"Ch{i}" for i in range(1, 19)}
        self.faders = {i: 0.1 * (i % 10) for i in range(1, 19)}
        self.gains = {i: 0.25 for i in range(1, 19)}
        self.pans = {i: 0.5 for i in range(1, 19)}
        self.mix_on = {i: i != 3 for i in range(1, 19)}
        self.sends: Dict[Tuple[int, int], float] = {
            (ch, s): 0.05 * s for ch in range(1, 19) for s in range(1, 17)
        }
        self.buses = {i: 0.4 for i in range(1, 17)}
        self.lr = 0.81
        self.fail_gets: Set[str] = set()
        self.set_log: List[str] = []

    def _fail_get(self, label: str) -> None:
        if label in self.fail_gets:
            raise XAirOscTimeoutError(f"timeout {label}")

    def get_channel_name(self, ch: int) -> str:
        self._fail_get(f"ch{ch}.name")
        return self.names[ch]

    def set_channel_name(self, ch: int, name: str) -> str:
        self.set_log.append(f"ch{ch}.name")
        self.names[ch] = str(name)
        return self.names[ch]

    def get_fader(self, ch: int) -> float:
        self._fail_get(f"ch{ch}.fader")
        return self.faders[ch]

    def set_fader(self, ch: int, value: float) -> float:
        self.set_log.append(f"ch{ch}.fader")
        self.faders[ch] = float(value)
        return self.faders[ch]

    def get_gain(self, ch: int) -> float:
        self._fail_get(f"ch{ch}.gain")
        return self.gains[ch]

    def set_gain(self, ch: int, value: float) -> float:
        self.set_log.append(f"ch{ch}.gain")
        self.gains[ch] = float(value)
        return self.gains[ch]

    def get_pan(self, ch: int) -> float:
        self._fail_get(f"ch{ch}.pan")
        return self.pans[ch]

    def set_pan(self, ch: int, value: float) -> float:
        self.set_log.append(f"ch{ch}.pan")
        self.pans[ch] = float(value)
        return self.pans[ch]

    def get_mute(self, ch: int) -> bool:
        self._fail_get(f"ch{ch}.mix_on")
        return self.mix_on[ch]

    def set_mute(self, ch: int, state: bool) -> bool:
        self.set_log.append(f"ch{ch}.mix_on")
        self.mix_on[ch] = bool(state)
        return self.mix_on[ch]

    def get_send(self, ch: int, send_idx: int) -> float:
        self._fail_get(f"ch{ch}.send{send_idx}")
        return self.sends[(ch, send_idx)]

    def set_send(self, ch: int, send_idx: int, value: float) -> float:
        self.set_log.append(f"ch{ch}.send{send_idx}")
        self.sends[(ch, send_idx)] = float(value)
        return self.sends[(ch, send_idx)]

    def get_bus_fader(self, bus: int) -> float:
        self._fail_get(f"bus{bus}.fader")
        return self.buses[bus]

    def set_bus_fader(self, bus: int, value: float) -> float:
        self.set_log.append(f"bus{bus}.fader")
        self.buses[bus] = float(value)
        return self.buses[bus]

    def get_lr_fader(self) -> float:
        self._fail_get("lr.fader")
        return self.lr

    def set_lr_fader(self, value: float) -> float:
        self.set_log.append("lr.fader")
        self.lr = float(value)
        return self.lr

    def get_meter(self, ch: int) -> int:  # pragma: no cover - must not be called
        raise AssertionError("scene dump must not read meters")


def _channel(scene: Dict[str, Any], ch: int) -> Dict[str, Any]:
    for row in scene["channel"]:
        if row["ch"] == ch:
            return row
    raise KeyError(ch)


class SceneDumpRecallTests(unittest.TestCase):
    def test_dump_roundtrip_apply_restores_state(self) -> None:
        src = FakeBridge()
        src.names[1] = "Kick"
        src.faders[1] = 0.77
        src.gains[1] = 0.33
        src.pans[1] = 0.2
        src.mix_on[1] = False
        src.sends[(1, 1)] = 0.42
        src.buses[1] = 0.55
        src.lr = 0.9
        dst = FakeBridge()
        dst.names[1] = "x"
        dst.faders[1] = 0.0
        dst.gains[1] = 0.0
        dst.pans[1] = 0.5
        dst.mix_on[1] = True
        dst.sends[(1, 1)] = 0.0
        dst.buses[1] = 0.0
        dst.lr = 0.0

        scope = SceneScope(channels=2, sends=2, buses=1)
        scene = dump_scene(src, scope)  # type: ignore[arg-type]
        self.assertEqual(scene["kind"], SCENE_KIND)
        self.assertEqual(scene["schema"], SCENE_SCHEMA)
        self.assertEqual(scene["channels"], 2)
        self.assertNotIn("meter", _channel(scene, 1))
        self.assertEqual(_channel(scene, 1)["name"], "Kick")
        self.assertFalse(_channel(scene, 1)["mix_on"])
        self.assertTrue(_channel(scene, 1)["muted"])
        self.assertFalse(scene["errors"])

        report = apply_scene(dst, scene)  # type: ignore[arg-type]
        self.assertEqual(report.skipped, 0)
        self.assertGreater(report.applied, 0)
        self.assertEqual(dst.names[1], "Kick")
        self.assertAlmostEqual(dst.faders[1], 0.77)
        self.assertAlmostEqual(dst.gains[1], 0.33)
        self.assertAlmostEqual(dst.pans[1], 0.2)
        self.assertFalse(dst.mix_on[1])
        self.assertAlmostEqual(dst.sends[(1, 1)], 0.42)
        self.assertAlmostEqual(dst.buses[1], 0.55)
        self.assertAlmostEqual(dst.lr, 0.9)
        ch1 = [e for e in dst.set_log if e.startswith("ch1.")]
        self.assertEqual(ch1[0], "ch1.mix_on")
        self.assertLess(ch1.index("ch1.mix_on"), ch1.index("ch1.fader"))

    def test_dump_continues_on_timeout(self) -> None:
        br = FakeBridge()
        br.fail_gets.add("ch1.fader")
        scene = dump_scene(br, SceneScope(channels=1, sends=0, buses=0))  # type: ignore[arg-type]
        self.assertIn("ch1.fader", " ".join(scene["errors"]))
        self.assertNotIn("fader", _channel(scene, 1))
        self.assertIn("name", _channel(scene, 1))

    def test_recall_prefers_mix_on_and_falls_back_to_muted(self) -> None:
        dst = FakeBridge()
        dst.mix_on[1] = True
        dst.mix_on[2] = True
        scene = {
            "schema": 1,
            "kind": SCENE_KIND,
            "channel": [
                {"ch": 1, "mix_on": False, "muted": False},
                {"ch": 2, "muted": True},
            ],
        }
        apply_scene(dst, scene)  # type: ignore[arg-type]
        self.assertFalse(dst.mix_on[1])
        self.assertFalse(dst.mix_on[2])

    def test_file_roundtrip_and_dry_run(self) -> None:
        src = FakeBridge()
        scene = dump_scene(src, SceneScope(channels=1, sends=1, buses=1))  # type: ignore[arg-type]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sala.json"
            save_scene_file(path, scene)
            loaded = load_scene_file(path)
            self.assertEqual(loaded["channel"][0]["ch"], 1)
            text = path.read_text(encoding="utf-8")
            json.loads(text)
        dry = apply_scene(None, loaded, dry_run=True)
        self.assertTrue(dry.dry_run)
        self.assertGreater(dry.applied, 0)

    def test_validate_rejects_wrong_kind_and_schema(self) -> None:
        with self.assertRaises(ValueError):
            validate_scene({"kind": "other", "schema": 1, "channel": []})
        with self.assertRaises(ValueError):
            validate_scene({"kind": SCENE_KIND, "schema": 99, "channel": []})
        with self.assertRaises(ValueError):
            validate_scene({"kind": SCENE_KIND, "schema": 1, "channel": {}})

    def test_strict_recall_raises_on_first_error(self) -> None:
        class Boom(FakeBridge):
            def set_fader(self, ch: int, value: float) -> float:
                raise XAirOSCError("nope")

        scene = {
            "schema": 1,
            "kind": SCENE_KIND,
            "channel": [{"ch": 1, "fader": 0.5}],
        }
        with self.assertRaises(XAirOSCError):
            apply_scene(Boom(), scene, continue_on_error=False)  # type: ignore[arg-type]

    def test_scope_from_env(self) -> None:
        env = {
            "XAIR_SCENE_CHANNELS": "8",
            "XAIR_SCENE_SENDS": "2",
            "XAIR_SCENE_BUSES": "3",
        }
        with patch.dict(os.environ, env, clear=False):
            sc = SceneScope.from_env()
        self.assertEqual((sc.channels, sc.sends, sc.buses), (8, 2, 3))


if __name__ == "__main__":
    unittest.main()
