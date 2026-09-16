"""Unit tests for JACK/PipeWire port-name sanitizing. No audio, no mixer."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from src.virtual_device.linux_virtual_output import should_try_named_jack
from src.virtual_device.port_names import (
    duplex_io_names,
    fallback_label,
    jack_client_name,
    labels_from_getter,
    sanitize_label,
    unique_port_names,
)


class PortNameTests(unittest.TestCase):
    def test_sanitize_spaces_colon_and_unicode(self) -> None:
        self.assertEqual(sanitize_label("Kick Drum", 1), "Kick_Drum")
        self.assertEqual(sanitize_label("L:R", 2), "L_R")
        self.assertEqual(sanitize_label("Batería", 3), "Bateria")
        self.assertEqual(sanitize_label("   ", 4), "CH04")
        self.assertEqual(sanitize_label("", 1), "CH01")
        self.assertEqual(sanitize_label(None, 9), "CH09")

    def test_unique_prefix_and_collisions(self) -> None:
        names = unique_port_names(["Kick", "Kick", "Hat"])
        self.assertEqual(names, ["01_Kick", "02_Kick", "03_Hat"])

    def test_duplex_io_names_keep_order_and_suffix(self) -> None:
        stems = unique_port_names(["Kick", "Snare"] + ["CH%02d" % i for i in range(3, 19)])
        self.assertEqual(len(stems), 18)
        ins, outs = duplex_io_names(stems)
        self.assertEqual(len(ins), 18)
        self.assertEqual(len(outs), 18)
        self.assertEqual(ins[0], "01_Kick_in")
        self.assertEqual(outs[0], "01_Kick_out")
        self.assertEqual(ins[1], "02_Snare_in")
        self.assertEqual(outs[1], "02_Snare_out")
        self.assertEqual(ins[-1], "18_CH18_in")
        self.assertEqual(outs[-1], "18_CH18_out")
        self.assertEqual([n[:-3] for n in ins], [n[:-4] for n in outs])
        ins2, outs2 = duplex_io_names(outs)
        self.assertEqual(ins2, ins)
        self.assertEqual(outs2, outs)

    def test_labels_from_getter_continues_on_error(self) -> None:
        def getter(ch: int) -> str:
            if ch == 2:
                raise TimeoutError("osc")
            return "Snare" if ch == 1 else f"Ch{ch}"

        labels, errors = labels_from_getter(3, getter)
        self.assertEqual(labels[0], "Snare")
        self.assertEqual(labels[1], "CH02")
        self.assertEqual(len(errors), 1)
        self.assertIn("ch2.name", errors[0])

    def test_fallback_and_client_name_env(self) -> None:
        self.assertEqual(fallback_label(18), "CH18")
        with patch.dict(os.environ, {"XAIR_JACK_CLIENT_NAME": "my_bridge"}, clear=False):
            self.assertEqual(jack_client_name(), "my_bridge")

    def test_should_try_named_jack_respects_env(self) -> None:
        with patch.dict(os.environ, {"XAIR_VIRTUAL_PORT_NAMES": "false"}, clear=False):
            self.assertFalse(should_try_named_jack(None))
        with patch.dict(os.environ, {"XAIR_VIRTUAL_PORT_NAMES": "true"}, clear=False):
            self.assertFalse(should_try_named_jack("Loopback"))
            if os.name == "posix":
                # Function is Linux-only; this host is Linux in CI/dev.
                self.assertTrue(should_try_named_jack(None))


if __name__ == "__main__":
    unittest.main()
