"""Forced JACK patchbay: System in → bridge → REAPER → System out. No JACK server."""

from __future__ import annotations

import unittest

from src.virtual_device.jack_graph import (
    is_bypass,
    pick_system_inputs,
    pick_system_outputs,
    plan_forced_graph,
    zip_connect,
)


class GraphPlanTests(unittest.TestCase):
    def test_chain_order_and_hdmi_skipped(self) -> None:
        bridge_ins = [f"xair_net_bridge:{i:02d}_CH{i:02d}_in" for i in range(1, 19)]
        bridge_outs = [f"xair_net_bridge:{i:02d}_CH{i:02d}_out" for i in range(1, 19)]
        capture = ["HDA:hdmi-capture"] + [f"system:capture_{i}" for i in range(1, 19)]
        playback = ["HDA:hdmi-stereo"] + [f"system:playback_{i}" for i in range(1, 19)]
        r_in = [f"REAPER:in{i}" for i in range(1, 19)]
        r_out = [f"REAPER:out{i}" for i in range(1, 19)]
        wanted, notes = plan_forced_graph(
            bridge_ins=bridge_ins,
            bridge_outs=bridge_outs,
            physical_capture=capture,
            physical_playback=playback,
            all_inputs=bridge_ins + r_in + playback,
            all_outputs=bridge_outs + r_out + capture,
            bridge_client="xair_net_bridge",
        )
        self.assertEqual(wanted[0], ("system:capture_1", "xair_net_bridge:01_CH01_in"))
        self.assertEqual(wanted[17], ("system:capture_18", "xair_net_bridge:18_CH18_in"))
        self.assertEqual(wanted[18], ("xair_net_bridge:01_CH01_out", "REAPER:in1"))
        self.assertEqual(wanted[35], ("xair_net_bridge:18_CH18_out", "REAPER:in18"))
        self.assertEqual(wanted[36], ("REAPER:out1", "system:playback_1"))
        self.assertEqual(wanted[-1], ("REAPER:out18", "system:playback_18"))
        self.assertEqual(len(wanted), 54)
        self.assertFalse(any("hdmi" in a.lower() or "hdmi" in b.lower() for a, b in wanted))
        self.assertEqual(notes, [])

    def test_prefers_x18_when_no_system_client(self) -> None:
        caps = ["GenericUSB:capture_1", "X18:capture_1", "X18:capture_2"]
        picked = pick_system_inputs(caps)
        self.assertEqual(picked, ["X18:capture_1", "X18:capture_2"])

    def test_hdmi_never_chosen_as_system_output(self) -> None:
        picked = pick_system_outputs(["hw:HDMI,0:playback_1", "system:playback_1"])
        self.assertEqual(picked, ["system:playback_1"])
        self.assertEqual(pick_system_outputs(["NVIDIA HDMI:playback_FL"]), [])

    def test_analog_preferred_over_system(self) -> None:
        picked = pick_system_outputs(
            [
                "system:playback_1",
                "ALC1220 Analog:playback_FL",
                "ALC1220 Analog:playback_FR",
            ]
        )
        self.assertEqual(
            picked,
            ["ALC1220 Analog:playback_FL", "ALC1220 Analog:playback_FR"],
        )

    def test_hdmi_alias_on_system_port_is_skipped(self) -> None:
        class _Port:
            def __init__(self, name: str, aliases: list) -> None:
                self.name = name
                self.aliases = aliases

        hdmi_system = _Port("system:playback_1", ["hdmi:playback_FL", "hw:HDMI,0"])
        analog = _Port("ALC1220 Analog:playback_FL", ["alc1220:playback_FL"])
        self.assertEqual(
            pick_system_outputs([hdmi_system, analog]),
            ["ALC1220 Analog:playback_FL"],
        )

    def test_waits_for_reaper(self) -> None:
        wanted, notes = plan_forced_graph(
            bridge_ins=["xair_net_bridge:01_CH01_in"],
            bridge_outs=["xair_net_bridge:01_CH01_out"],
            physical_capture=["system:capture_1"],
            physical_playback=["system:playback_1"],
            all_inputs=["xair_net_bridge:01_CH01_in", "system:playback_1"],
            all_outputs=["xair_net_bridge:01_CH01_out", "system:capture_1"],
            bridge_client="xair_net_bridge",
        )
        self.assertEqual(wanted, [("system:capture_1", "xair_net_bridge:01_CH01_in")])
        self.assertTrue(any("REAPER" in n for n in notes))

    def test_bypass_detection(self) -> None:
        kw = dict(
            bridge_ins=["xair_net_bridge:01_CH01_in"],
            bridge_outs=["xair_net_bridge:01_CH01_out"],
            sys_in=["system:capture_1"],
            sys_out=["system:playback_1"],
            reaper_ins=["REAPER:in1"],
            reaper_outs=["REAPER:out1"],
        )
        self.assertTrue(is_bypass("system:capture_1", "REAPER:in1", **kw))
        self.assertTrue(is_bypass("xair_net_bridge:01_CH01_out", "system:playback_1", **kw))
        self.assertTrue(is_bypass("system:capture_1", "system:playback_1", **kw))
        self.assertTrue(is_bypass("REAPER:out1", "xair_net_bridge:01_CH01_in", **kw))
        self.assertFalse(is_bypass("system:capture_1", "xair_net_bridge:01_CH01_in", **kw))
        self.assertFalse(is_bypass("xair_net_bridge:01_CH01_out", "REAPER:in1", **kw))
        self.assertFalse(is_bypass("REAPER:out1", "system:playback_1", **kw))

    def test_zip_connect_keeps_channel_order(self) -> None:
        pairs = zip_connect(["system:capture_2", "system:capture_1"], ["a", "b"])
        # callers sort before zip; this only pairs by index
        self.assertEqual(pairs, [("system:capture_2", "a"), ("system:capture_1", "b")])


class AnalogNeverHdmiTests(unittest.TestCase):
    def test_hdmi_and_analog_names(self) -> None:
        from src.virtual_device.x18_device import is_analog_device, is_hdmi_device

        self.assertTrue(is_hdmi_device("hw:HDMI,0"))
        self.assertTrue(is_hdmi_device("HDA NVidia"))
        self.assertTrue(is_hdmi_device("alsa_output.pci-0000_0a_00.1.hdmi-stereo"))
        self.assertFalse(is_hdmi_device("system:playback_1"))
        self.assertFalse(is_hdmi_device("ALC1220 Analog"))
        self.assertTrue(is_analog_device("ALC1220 Analog:playback_FL"))
        self.assertTrue(is_analog_device("alsa_output.pci-0000_0c_00.4.analog-stereo"))
        self.assertFalse(is_analog_device("hw:HDMI"))

    def test_pick_analog_alsa_hw_skips_hdmi_cards(self) -> None:
        from src.virtual_device.x18_device import pick_analog_alsa_hw

        raw = (
            " 0 [HDMI           ]: HDA-Intel - HDA ATI HDMI\n"
            "                      HDA ATI HDMI at 0xfce60000 irq 80\n"
            " 2 [NVidia         ]: HDA-Intel - HDA NVidia\n"
            "                      HDA NVidia at 0xfc080000 irq 81\n"
            " 3 [Generic        ]: HDA-Intel - HD-Audio Generic\n"
            "                      Realtek ALC1220 Analog at 0xfcd00000 irq 83\n"
        )
        self.assertEqual(pick_analog_alsa_hw(raw), "hw:Generic")

    def test_qjackctl_interface_hdmi_rewritten(self) -> None:
        from src.virtual_device.x18_device import rewrite_qjackctl_devices

        text = "[Settings]\nInterface=hw:HDMI\nInDevice=hw:HDMI,0\nOutDevice=\nOther=keep\n"
        out = rewrite_qjackctl_devices(text, "hw:Generic")
        self.assertIn("Interface=hw:Generic", out)
        self.assertIn("InDevice=hw:Generic", out)
        self.assertIn("OutDevice=hw:Generic", out)
        self.assertIn("Other=keep", out)

    def test_pactl_picks_analog_not_hdmi(self) -> None:
        from src.virtual_device.x18_device import pick_pactl_endpoint

        listing = (
            "0\talsa_output.pci-0000_0a_00.1.hdmi-stereo\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
            "1\talsa_output.pci-0000_0c_00.4.analog-stereo\tPipeWire\ts32le 2ch 48000Hz\tSUSPENDED\n"
        )
        self.assertEqual(
            pick_pactl_endpoint(listing),
            "alsa_output.pci-0000_0c_00.4.analog-stereo",
        )


if __name__ == "__main__":
    unittest.main()
