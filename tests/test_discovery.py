"""Discovery packets, health, and registry — no PortAudio/JACK."""

from __future__ import annotations

import socket
import struct
import unittest

from pythonosc.osc_message_builder import OscMessageBuilder

from src.discovery.packets import (
    DNS_CLASS_IN,
    DNS_TYPE_A,
    DNS_TYPE_PTR,
    DeviceRegistry,
    DiscoveredDevice,
    build_mdns_query,
    build_ssdp_msearch,
    classify_discovery_health,
    encode_dns_name,
    pack_beacon,
    parse_beacon,
    parse_mdns_devices,
    parse_ssdp,
    parse_xinfo_dgram,
    wants_auto_host,
)


def _xinfo_dgram(ip: str, name: str, model: str, fw: str) -> bytes:
    b = OscMessageBuilder("/xinfo")
    b.add_arg(ip)
    b.add_arg(name)
    b.add_arg(model)
    b.add_arg(fw)
    return b.build().dgram


def _mdns_a(name: str, ip: str) -> bytes:
    header = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0)
    body = encode_dns_name(name)
    rdata = socket.inet_aton(ip)
    body += struct.pack("!HHIH", DNS_TYPE_A, DNS_CLASS_IN, 120, 4) + rdata
    return header + body


def _mdns_ptr(service: str, instance: str) -> bytes:
    header = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0)
    body = encode_dns_name(service)
    target = encode_dns_name(instance)
    body += struct.pack("!HHIH", DNS_TYPE_PTR, DNS_CLASS_IN, 120, len(target)) + target
    return header + body


class AutoHostTests(unittest.TestCase):
    def test_auto_tokens(self) -> None:
        self.assertTrue(wants_auto_host("auto"))
        self.assertTrue(wants_auto_host(""))
        self.assertTrue(wants_auto_host("discover"))
        self.assertFalse(wants_auto_host("192.168.0.1"))
        self.assertFalse(wants_auto_host("127.0.0.1"))


class HealthTests(unittest.TestCase):
    def test_colors(self) -> None:
        self.assertEqual(
            classify_discovery_health(
                enabled=False, mixer_count=0, peer_count=0, last_discovery_age_s=None
            ),
            "off",
        )
        self.assertEqual(
            classify_discovery_health(
                enabled=True, mixer_count=1, peer_count=0, last_discovery_age_s=0.2
            ),
            "ok",
        )
        self.assertEqual(
            classify_discovery_health(
                enabled=True, mixer_count=0, peer_count=1, last_discovery_age_s=0.2
            ),
            "warn",
        )
        self.assertEqual(
            classify_discovery_health(
                enabled=True, mixer_count=0, peer_count=0, last_discovery_age_s=30.0
            ),
            "fail",
        )


class BeaconTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        raw = pack_beacon(
            role="client",
            name="daw-pc",
            ip="192.168.0.20",
            audio_port=50000,
            ts_ns=123,
        )
        dev = parse_beacon(raw, "10.0.0.1")
        assert dev is not None
        self.assertEqual(dev.kind, "peer")
        self.assertEqual(dev.role, "client")
        self.assertEqual(dev.ip, "192.168.0.20")
        self.assertEqual(dev.port, 50000)
        self.assertEqual(dev.source, "beacon")

    def test_rejects_junk(self) -> None:
        self.assertIsNone(parse_beacon(b"XBRI\x00not-a-beacon"))
        self.assertIsNone(parse_beacon(b"XBRP{bad"))


class XinfoTests(unittest.TestCase):
    def test_pythonosc_and_raw(self) -> None:
        raw = _xinfo_dgram("192.168.0.5", "X18-DEMO", "X18", "1.22")
        dev = parse_xinfo_dgram(raw, "10.0.0.9")
        assert dev is not None
        self.assertEqual(dev.kind, "mixer")
        self.assertEqual(dev.ip, "192.168.0.5")
        self.assertEqual(dev.name, "X18-DEMO")
        self.assertEqual(dev.model, "X18")
        self.assertEqual(dev.firmware, "1.22")
        self.assertEqual(dev.source, "xinfo")


class SsdpTests(unittest.TestCase):
    def test_behringer_notify(self) -> None:
        blob = (
            "NOTIFY * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\n"
            "NT: urn:behringer:device:X-AIR:1\r\n"
            "USN: uuid:x18-abcd::urn:behringer:device:X-AIR:1\r\n"
            "SERVER: X18/1.22\r\n"
            "LOCATION: http://192.168.0.8:80/desc.xml\r\n"
            "\r\n"
        ).encode("utf-8")
        dev = parse_ssdp(blob, "192.168.0.8")
        assert dev is not None
        self.assertEqual(dev.kind, "mixer")
        self.assertEqual(dev.ip, "192.168.0.8")
        self.assertEqual(dev.model, "X18")
        self.assertEqual(dev.firmware, "1.22")
        self.assertEqual(dev.source, "ssdp")

    def test_ignores_unrelated(self) -> None:
        blob = (
            "HTTP/1.1 200 OK\r\n"
            "ST: upnp:rootdevice\r\n"
            "SERVER: Linux/1.0 UPnP/1.0 printer/1\r\n"
            "\r\n"
        ).encode("utf-8")
        self.assertIsNone(parse_ssdp(blob, "192.168.0.9"))


class MdnsTests(unittest.TestCase):
    def test_a_record_x18(self) -> None:
        pkt = _mdns_a("X18-ABCD.local", "192.168.0.7")
        got = parse_mdns_devices(pkt, "192.168.0.7")
        self.assertTrue(got)
        self.assertEqual(got[0].ip, "192.168.0.7")
        self.assertEqual(got[0].model, "X18")
        self.assertEqual(got[0].source, "mdns")

    def test_ptr_instance(self) -> None:
        pkt = _mdns_ptr("_osc._udp.local", "X18-FOH._osc._udp.local")
        got = parse_mdns_devices(pkt, "192.168.0.11")
        self.assertTrue(got)
        self.assertEqual(got[0].ip, "192.168.0.11")
        self.assertIn("X18", got[0].name.upper() + got[0].model)

    def test_query_has_ptr_questions(self) -> None:
        q = build_mdns_query()
        self.assertTrue(q.startswith(b"\x00\x00"))
        self.assertIn(b"_osc", q)

    def test_ssdp_msearch_bytes(self) -> None:
        raw = build_ssdp_msearch()
        self.assertTrue(raw.startswith(b"M-SEARCH"))
        self.assertIn(b"ssdp:all", raw)


class RegistryTests(unittest.TestCase):
    def test_merge_expire_snapshot(self) -> None:
        reg = DeviceRegistry(ttl_s=1.0)
        a = DiscoveredDevice(
            kind="mixer",
            ip="192.168.0.5",
            name="X18-DEMO",
            model="X18",
            firmware="",
            source="mdns",
            last_seen_ts=1,
            last_seen_mono=10.0,
        )
        b = DiscoveredDevice(
            kind="mixer",
            ip="192.168.0.5",
            name="X18-DEMO",
            model="X18",
            firmware="1.22",
            source="xinfo",
            last_seen_ts=2,
            last_seen_mono=10.5,
        )
        reg.note(a)
        reg.note(b)
        mix = reg.mixers()
        self.assertEqual(len(mix), 1)
        self.assertEqual(mix[0].firmware, "1.22")
        self.assertIn("mdns", mix[0].source)
        self.assertIn("xinfo", mix[0].source)
        snap = reg.snapshot(enabled=True, now_mono=10.5)
        self.assertTrue(snap["discovery_enabled"])
        self.assertEqual(len(snap["discovered_devices"]), 1)
        self.assertEqual(snap["discovery_health"], "ok")
        self.assertEqual(snap["last_discovery_ts"], 2)
        reg.expire(12.0)
        self.assertEqual(reg.mixers(), [])


if __name__ == "__main__":
    unittest.main()
