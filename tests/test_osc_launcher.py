"""OSC launcher helpers. No mixer, no REAPER, no xair_network_bridge_client import."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import List
from unittest.mock import patch

from src.osc.launcher import (
    MSG_OK,
    MSG_REAPER,
    MSG_SYNC,
    MSG_X18,
    OscLauncherError,
    launch_osc,
    parse_proc_net_udp,
    resolve_osc_host,
    resolve_osc_port,
    resolve_bridge_listen_port,
    resolve_reaper_listen_port,
    upsert_env_file,
)


class EnvHelpersTests(unittest.TestCase):
    def test_upsert_sets_enabled_and_keeps_other_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("XAIR_UDP_PORT=50000\n# comment\nXAIR_OSC_HOST=1.2.3.4\n", encoding="utf-8")
            upsert_env_file(path, {"XAIR_OSC_ENABLED": "true", "XAIR_OSC_HOST": "10.0.0.8"})
            text = path.read_text(encoding="utf-8")
            self.assertIn("XAIR_UDP_PORT=50000", text)
            self.assertIn("# comment", text)
            self.assertIn("XAIR_OSC_ENABLED=true", text)
            self.assertIn("XAIR_OSC_HOST=10.0.0.8", text)
            self.assertEqual(text.count("XAIR_OSC_HOST="), 1)

    def test_resolve_ip_alias_and_port(self) -> None:
        self.assertEqual(resolve_osc_host({"XAIR_OSC_IP": "10.1.2.3", "XAIR_OSC_HOST": "9.9.9.9"}), "10.1.2.3")
        self.assertEqual(resolve_osc_host({"XAIR_OSC_HOST": "10.1.2.4"}), "10.1.2.4")
        self.assertEqual(resolve_osc_host({"XAIR_OSC_HOST": "auto"}), "")
        self.assertEqual(resolve_osc_port({"XAIR_OSC_PORT": "10024"}), 10024)
        with self.assertRaises(OscLauncherError):
            resolve_osc_port({"XAIR_OSC_PORT": "nope"})

    def test_proc_net_udp_and_reaper_port(self) -> None:
        sample = (
            "  sl  local_address rem_address\n"
            "   8: 0100007F:1F40 00000000:0000 07 00000000:00000000\n"
        )
        self.assertTrue(parse_proc_net_udp(sample, 8000))
        self.assertFalse(parse_proc_net_udp(sample, 9001))
        port, ok = resolve_reaper_listen_port(9001, bound_fn=lambda p: p == 8000)
        self.assertEqual((port, ok), (8000, True))
        port, ok = resolve_reaper_listen_port(8000, bound_fn=lambda _p: False)
        self.assertEqual((port, ok), (8000, False))
        self.assertEqual(
            resolve_bridge_listen_port(8000, 8000, bound_fn=lambda p: p == 8000),
            9001,
        )
        self.assertEqual(
            resolve_bridge_listen_port(8000, None, bound_fn=lambda p: p == 8000),
            9001,
        )


class LaunchFlowTests(unittest.TestCase):
    def test_success_prints_required_lines(self) -> None:
        lines: List[str] = []
        started: List[str] = []
        pids: List[int] = []

        def _start(_r: Path) -> None:
            started.append("client")
            pids.append(1)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.dict(
                os.environ,
                {"XAIR_OSC_ENABLED": "false", "XAIR_REAPER_LISTEN_PORT": "8000"},
                clear=False,
            ):
                status = launch_osc(
                    project_root=root,
                    host="10.0.0.5",
                    port=10024,
                    reaper_host="127.0.0.1",
                    reaper_port=8000,
                    non_interactive=True,
                    ensure_client=True,
                    start_sync=True,
                    echo=lines.append,
                    ping_fn=lambda _h: True,
                    handshake_fn=lambda _h, _p: ("active", ("active",)),
                    discover_fn=lambda **_k: None,
                    bound_fn=lambda p: p == 8000,
                    client_pids_fn=lambda: list(pids),
                    start_client_fn=_start,
                    sync_fn=lambda: started.append("sync"),
                )
            self.assertTrue(status["x18"] and status["reaper"] and status["sync"])
            self.assertEqual(status["listen_port"], 9001)
            self.assertEqual(status["reaper_port"], 8000)
            joined = "\n".join(lines)
            self.assertIn(MSG_X18, joined)
            self.assertIn(MSG_REAPER, joined)
            self.assertIn(MSG_SYNC, joined)
            self.assertIn(MSG_OK, joined)
            self.assertIn("X18 OK", joined)
            self.assertIn("REAPER OK", joined)
            self.assertIn("9001", joined)
            self.assertEqual(started, ["client", "sync"])
            env = (root / ".env").read_text(encoding="utf-8")
            self.assertIn("XAIR_OSC_ENABLED=true", env)
            self.assertIn("XAIR_OSC_IP=10.0.0.5", env)
            self.assertIn("XAIR_REAPER_PORT=8000", env)
            self.assertIn("XAIR_REAPER_LISTEN_PORT=9001", env)

    def test_x18_failure_message(self) -> None:
        def _boom(_h: str, _p: int):
            raise ConnectionError("timeout")

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(OscLauncherError) as ctx:
                launch_osc(
                    project_root=Path(tmp),
                    host="10.0.0.9",
                    port=10024,
                    non_interactive=True,
                    start_sync=False,
                    echo=lambda _s: None,
                    ping_fn=lambda _h: False,
                    handshake_fn=_boom,
                    discover_fn=lambda **_k: None,
                    bound_fn=lambda _p: True,
                    client_pids_fn=lambda: [1],
                    start_client_fn=lambda _r: None,
                )
            self.assertIn("X18 no responde", str(ctx.exception))

    def test_reaper_failure_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(OscLauncherError) as ctx:
                launch_osc(
                    project_root=Path(tmp),
                    host="10.0.0.5",
                    port=10024,
                    reaper_host="127.0.0.1",
                    reaper_port=8000,
                    non_interactive=True,
                    start_sync=False,
                    echo=lambda _s: None,
                    ping_fn=lambda _h: True,
                    handshake_fn=lambda _h, _p: ("active", ("active",)),
                    discover_fn=lambda **_k: None,
                    bound_fn=lambda _p: False,
                    client_pids_fn=lambda: [1],
                    start_client_fn=lambda _r: None,
                )
            self.assertIn("REAPER no responde", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
