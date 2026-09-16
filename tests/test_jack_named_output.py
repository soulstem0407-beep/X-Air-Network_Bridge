"""JACK duplex registration tests. Mocks ``jack`` — no JACK server, no receiver.py."""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest.mock import patch

import numpy as np

from src.virtual_device.jack_named_output import (
    JackNamedOutput,
    _ensure_pipewire_duplex,
    pin_pipewire_clock_quantum,
    should_reject_auto_1024,
    start_named_jack_output,
)
from src.virtual_device.port_names import duplex_io_names, unique_port_names


class _FakePort:
    def __init__(self, name: str) -> None:
        self.name = name
        self.buf = np.zeros(32, dtype=np.float32)

    def get_array(self) -> np.ndarray:
        return self.buf


class _PortList:
    def __init__(self) -> None:
        self.ports: list[_FakePort] = []

    def register(self, name: str) -> _FakePort:
        port = _FakePort(name)
        self.ports.append(port)
        return port

    def __len__(self) -> int:
        return len(self.ports)

    def __iter__(self):
        return iter(self.ports)

    def __getitem__(self, i: int) -> _FakePort:
        return self.ports[i]


class _FakeClient:
    last: "_FakeClient | None" = None

    def __init__(self, name: str, no_start_server: bool = False) -> None:
        self.name = name
        self.samplerate = 48000
        self.inports = _PortList()
        self.outports = _PortList()
        self.process_cb = None
        self.activated = False
        self.closed = False
        self._blocksize = 1024
        type(self).last = self

    def set_process_callback(self, cb) -> None:  # type: ignore[no-untyped-def]
        self.process_cb = cb

    def set_blocksize_callback(self, cb) -> None:  # type: ignore[no-untyped-def]
        self.blocksize_cb = cb

    @property
    def blocksize(self) -> int:
        return int(self._blocksize)

    @blocksize.setter
    def blocksize(self, value: int) -> None:
        self._blocksize = int(value)

    def activate(self) -> None:
        self.activated = True

    def deactivate(self) -> None:
        self.activated = False

    def close(self) -> None:
        self.closed = True

    def set_port_registration_callback(self, cb) -> None:  # type: ignore[no-untyped-def]
        self.port_reg_cb = cb

    def set_client_registration_callback(self, cb) -> None:  # type: ignore[no-untyped-def]
        self.client_reg_cb = cb

    def get_ports(self, **kwargs):  # type: ignore[no-untyped-def]
        return []

    def connect(self, src: str, dst: str) -> None:
        return None

    def disconnect(self, src: str, dst: str) -> None:
        return None

    def get_all_connections(self, port: str):  # type: ignore[no-untyped-def]
        return []


class PipewireDuplexPropsTests(unittest.TestCase):
    def test_default_and_replace_source_sink(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            _ensure_pipewire_duplex()
            self.assertIn("Audio/Duplex", os.environ["PIPEWIRE_PROPS"])
            self.assertIn("node.autoconnect = false", os.environ["PIPEWIRE_PROPS"])
            self.assertNotIn("lock-quantum", os.environ["PIPEWIRE_PROPS"])

        with patch.dict(
            os.environ,
            {
                "PIPEWIRE_PROPS": (
                    "{ node.force-quantum = 2048 node.latency = 2048/48000 "
                    "node.lock-quantum = false media.class = Audio/Duplex }"
                ),
            },
            clear=False,
        ):
            _ensure_pipewire_duplex()
            props = os.environ["PIPEWIRE_PROPS"]
            self.assertNotIn("force-quantum", props)
            self.assertNotIn("node.latency", props)
            self.assertNotIn("lock-quantum", props)

        with patch.dict(
            os.environ,
            {"PIPEWIRE_PROPS": "{ node.autoconnect = false media.class = Audio/Source }"},
            clear=False,
        ):
            _ensure_pipewire_duplex()
            self.assertIn("Audio/Duplex", os.environ["PIPEWIRE_PROPS"])
            self.assertNotIn("Audio/Source", os.environ["PIPEWIRE_PROPS"])

        with patch.dict(
            os.environ,
            {"PIPEWIRE_PROPS": "{ media.class = Audio/Sink }"},
            clear=False,
        ):
            _ensure_pipewire_duplex()
            self.assertIn("Audio/Duplex", os.environ["PIPEWIRE_PROPS"])
            self.assertNotIn("Audio/Sink", os.environ["PIPEWIRE_PROPS"])

        with patch.dict(
            os.environ,
            {"PIPEWIRE_PROPS": "{ node.autoconnect = false }"},
            clear=False,
        ):
            _ensure_pipewire_duplex()
            self.assertIn("Audio/Duplex", os.environ["PIPEWIRE_PROPS"])
            self.assertIn("node.autoconnect = false", os.environ["PIPEWIRE_PROPS"])
            self.assertNotIn("lock-quantum", os.environ["PIPEWIRE_PROPS"])


class PeriodSnapbackTests(unittest.TestCase):
    def test_rejects_pipewire_default_1024_after_user_period(self) -> None:
        self.assertTrue(should_reject_auto_1024(64, 1024, 1.0))
        self.assertTrue(should_reject_auto_1024(128, 1024, 30.0))
        self.assertFalse(should_reject_auto_1024(64, 1024, 90.0))
        self.assertFalse(should_reject_auto_1024(1024, 1024, 1.0))
        self.assertFalse(should_reject_auto_1024(None, 1024, 1.0))
        self.assertFalse(should_reject_auto_1024(64, 256, 1.0))

    def test_pin_command_targets_clock_force_quantum(self) -> None:
        with patch("src.virtual_device.jack_named_output.subprocess.run") as run:
            run.return_value.returncode = 0
            self.assertTrue(pin_pipewire_clock_quantum(128))
            cmds = [tuple(c[0][0]) for c in run.call_args_list]
            self.assertTrue(cmds)
            self.assertTrue(all(c[:3] == ("pw-metadata", "-n", "settings") for c in cmds))
            keys = {c[4]: c[5] for c in cmds}
            self.assertEqual(keys.get("clock.force-quantum"), "128")
            self.assertEqual(keys.get("clock.quantum"), "128")
            self.assertIn("clock.min-quantum", keys)
            self.assertIn("clock.max-quantum", keys)


class JackDuplexRegisterTests(unittest.TestCase):
    def test_registers_18_in_and_18_out_same_order(self) -> None:
        stems = unique_port_names(["Kick", "Snare"] + ["CH%02d" % i for i in range(3, 19)])
        ins, outs = duplex_io_names(stems)
        self.assertEqual(len(ins), 18)
        self.assertEqual(len(outs), 18)
        fake_jack = types.SimpleNamespace(Client=_FakeClient)
        _FakeClient.last = None

        def pull(n: int) -> np.ndarray:
            return np.zeros((n, 18), dtype=np.float32)

        with patch(
            "src.virtual_device.jack_named_output.force_analog_session_defaults"
        ):
            with patch.dict(sys.modules, {"jack": fake_jack}):
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("PIPEWIRE_PROPS", None)
                    handle = start_named_jack_output(
                        client_name="xair_net_bridge",
                        port_names=stems,
                        sample_rate=48000,
                        pull_frames=pull,
                        in_port_names=ins,
                        out_port_names=outs,
                    )
        try:
            client = _FakeClient.last
            assert client is not None
            in_names = [p.name for p in client.inports]
            out_names = [p.name for p in client.outports]
            self.assertEqual(in_names, ins)
            self.assertEqual(out_names, outs)
            self.assertEqual(in_names[0], "01_Kick_in")
            self.assertEqual(out_names[0], "01_Kick_out")
            self.assertEqual(in_names[-1], "18_CH18_in")
            self.assertEqual(out_names[-1], "18_CH18_out")
            self.assertTrue(client.activated)
            self.assertIsNotNone(client.process_cb)
            client.process_cb(32)
            self.assertEqual(handle.actual_client_name, "xair_net_bridge")
        finally:
            handle.stop()
        self.assertTrue(client.closed)

    def test_derives_in_out_from_stems_when_lists_omitted(self) -> None:
        stems = unique_port_names(["CH%02d" % i for i in range(1, 19)])
        expected_in, expected_out = duplex_io_names(stems)
        obj = JackNamedOutput(
            client_name="xair_net_bridge",
            port_names=stems,
            sample_rate=48000,
            pull_frames=lambda n: np.zeros((n, 18), dtype=np.float32),
        )
        self.assertEqual(obj.in_port_names, expected_in)
        self.assertEqual(obj.out_port_names, expected_out)
        self.assertEqual(obj.in_port_names[0], "01_CH01_in")
        self.assertEqual(obj.out_port_names[0], "01_CH01_out")


if __name__ == "__main__":
    unittest.main()
