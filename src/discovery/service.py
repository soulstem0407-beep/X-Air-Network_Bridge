"""Control-thread mDNS / SSDP / /xinfo / presence discovery.

Audio UDP stays unicast. This module never runs in a JACK/PipeWire callback.
"""
from __future__ import annotations

import json
import logging
import os
import select
import socket
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .packets import (
    DEFAULT_DISCOVERY_GROUP,
    DEFAULT_DISCOVERY_PORT,
    DEFAULT_PROBE_S,
    DEFAULT_TTL_S,
    MDNS_GROUP,
    MDNS_PORT,
    SSDP_GROUP,
    SSDP_PORT,
    XINFO_PORT,
    DeviceRegistry,
    DiscoveredDevice,
    build_mdns_query,
    build_ssdp_msearch,
    build_xinfo_query,
    empty_snapshot,
    local_ipv4,
    pack_beacon,
    parse_beacon,
    parse_mdns_devices,
    parse_ssdp,
    parse_xinfo_dgram,
    wants_auto_host,
)

log = logging.getLogger(__name__)

PeerCallback = Callable[[DiscoveredDevice], None]


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _env_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return default if v is None or not str(v).strip() else str(v).strip()


def _join_group(sock: socket.socket, group: str) -> None:
    mreq = socket.inet_aton(group) + socket.inet_aton("0.0.0.0")
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)


def _bind_udp(port: int, *, reuse: bool = True) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if reuse:
        reuseport = getattr(socket, "SO_REUSEPORT", None)
        if reuseport is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, reuseport, 1)
            except OSError:
                pass
    sock.bind(("0.0.0.0", int(port)))
    sock.setblocking(False)
    return sock


class DiscoveryService:
    """Multicast listener + beacon on a dedicated control thread."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        role: str = "client",
        name: str = "xair-network-bridge",
        audio_port: int = 50000,
        group: str = DEFAULT_DISCOVERY_GROUP,
        port: int = DEFAULT_DISCOVERY_PORT,
        probe_s: float = DEFAULT_PROBE_S,
        ttl_s: float = DEFAULT_TTL_S,
        state_path: Optional[Path] = None,
        ignore_self: bool = True,
    ) -> None:
        self.enabled = bool(enabled)
        self.role = str(role).strip().lower() or "client"
        self.name = str(name)
        self.audio_port = int(audio_port)
        self.group = group
        self.port = int(port)
        self.probe_s = float(probe_s)
        self.registry = DeviceRegistry(ttl_s=ttl_s)
        self._state_path = Path(state_path) if state_path is not None else None
        self._ignore_self = bool(ignore_self)
        self._local_ip = local_ipv4()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._peer_cb: Optional[PeerCallback] = None
        self._lock = threading.Lock()
        self._socks: List[socket.socket] = []
        self._beacon_sock: Optional[socket.socket] = None
        self._probe_sock: Optional[socket.socket] = None
        self._last_state_key: Optional[tuple] = None

    @classmethod
    def from_env(
        cls,
        *,
        role: str,
        audio_port: int,
        state_path: Optional[Path] = None,
        enabled: Optional[bool] = None,
    ) -> DiscoveryService:
        on = _env_bool("XAIR_DISCOVERY", False) if enabled is None else bool(enabled)
        group = _env_str("XAIR_DISCOVERY_GROUP", DEFAULT_DISCOVERY_GROUP)
        port = _env_int("XAIR_DISCOVERY_PORT", DEFAULT_DISCOVERY_PORT)
        name = _env_str("XAIR_DISCOVERY_NAME", "xair-network-bridge")
        return cls(
            enabled=on,
            role=role,
            name=name,
            audio_port=audio_port,
            group=group,
            port=port,
            state_path=state_path,
        )

    def set_peer_callback(self, cb: Optional[PeerCallback]) -> None:
        self._peer_cb = cb

    def start(self) -> None:
        if not self.enabled:
            self._write_state()
            return
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._open_sockets()
        self._thread = threading.Thread(target=self._loop, name="xair-discovery", daemon=True)
        self._thread.start()
        log.info(
            "Discovery on (role=%s group=%s:%s audio_unicast=%s beacons+mDNS+SSDP+/xinfo)",
            self.role,
            self.group,
            self.port,
            self.audio_port,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        for sock in self._socks:
            try:
                sock.close()
            except OSError:
                pass
        self._socks.clear()
        self._beacon_sock = None
        self._probe_sock = None
        self._write_state()

    def snapshot(self) -> dict:
        with self._lock:
            snap = self.registry.snapshot(enabled=self.enabled, now_mono=time.monotonic())
        return snap

    def wait_mixer(self, timeout_s: float) -> Optional[DiscoveredDevice]:
        def getter() -> List[DiscoveredDevice]:
            with self._lock:
                return list(self.registry.mixers())

        return self._wait(getter, timeout_s)

    def wait_peer(self, role: str, timeout_s: float) -> Optional[DiscoveredDevice]:
        want = str(role).lower()

        def getter() -> List[DiscoveredDevice]:
            with self._lock:
                return list(self.registry.peers(want))

        return self._wait(getter, timeout_s)

    def _wait(
        self, getter: Callable[[], List[DiscoveredDevice]], timeout_s: float
    ) -> Optional[DiscoveredDevice]:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while time.monotonic() < deadline and not self._stop.is_set():
            items = getter()
            if items:
                return items[0]
            time.sleep(0.05)
        items = getter()
        return items[0] if items else None

    def _open_sockets(self) -> None:
        try:
            beacon = _bind_udp(self.port)
            _join_group(beacon, self.group)
            beacon.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            beacon.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            self._beacon_sock = beacon
            self._socks.append(beacon)
        except OSError:
            log.warning("discovery: no se pudo unir a %s:%s", self.group, self.port, exc_info=True)
            self.registry.listen_errors += 1

        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("0.0.0.0", 0))
            probe.setblocking(False)
            probe.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
            probe.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
            self._probe_sock = probe
            self._socks.append(probe)
        except OSError:
            log.debug("discovery probe socket", exc_info=True)
            self.registry.listen_errors += 1

        for port, group, label in (
            (MDNS_PORT, MDNS_GROUP, "mDNS"),
            (SSDP_PORT, SSDP_GROUP, "SSDP"),
        ):
            try:
                sock = _bind_udp(port)
                _join_group(sock, group)
                self._socks.append(sock)
            except OSError:
                log.info("discovery: %s listen skipped (port %s busy or no perm)", label, port)

    def _loop(self) -> None:
        last_probe = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now - last_probe >= self.probe_s:
                self._emit_probes()
                last_probe = now
            if self._socks:
                try:
                    readable, _, _ = select.select(self._socks, [], [], 0.2)
                except (OSError, ValueError):
                    time.sleep(0.2)
                    continue
            else:
                readable = []
                time.sleep(0.2)
            for sock in readable:
                self._drain(sock)
            with self._lock:
                self.registry.expire(time.monotonic())
            self._write_state()

    def _drain(self, sock: socket.socket) -> None:
        for _ in range(32):
            try:
                data, addr = sock.recvfrom(65535)
            except BlockingIOError:
                return
            except OSError:
                return
            src_ip = addr[0] if addr else ""
            self._ingest(data, src_ip)

    def _ingest(self, data: bytes, src_ip: str) -> None:
        now_mono = time.monotonic()
        now_ns = time.time_ns()
        found: List[DiscoveredDevice] = []
        beacon = parse_beacon(data, src_ip)
        if beacon is not None:
            found.append(beacon)
        else:
            xinfo = parse_xinfo_dgram(data, src_ip)
            if xinfo is not None:
                found.append(xinfo)
            ssdp = parse_ssdp(data, src_ip)
            if ssdp is not None:
                found.append(ssdp)
            found.extend(parse_mdns_devices(data, src_ip))
        if not found:
            return
        new_peers: List[DiscoveredDevice] = []
        with self._lock:
            for dev in found:
                if self._ignore_self and self._is_self(dev):
                    continue
                if not dev.ip:
                    continue
                if not dev.last_seen_ts:
                    dev.last_seen_ts = now_ns
                dev.last_seen_mono = now_mono
                noted = self.registry.note(dev)
                if noted.kind == "peer" and noted.role and noted.role != self.role:
                    new_peers.append(noted)
        cb = self._peer_cb
        if cb is not None:
            for peer in new_peers:
                try:
                    cb(peer)
                except Exception:
                    log.debug("discovery peer callback", exc_info=True)

    def _is_self(self, dev: DiscoveredDevice) -> bool:
        if dev.kind != "peer":
            return False
        if dev.role != self.role:
            return False
        return dev.role == self.role and dev.ip in (self._local_ip, "127.0.0.1")

    def _emit_probes(self) -> None:
        self._local_ip = local_ipv4()
        payload = pack_beacon(
            role=self.role,
            name=self.name,
            ip=self._local_ip,
            audio_port=self.audio_port,
            ts_ns=time.time_ns(),
        )
        dests: List[Tuple[socket.socket, Tuple[str, int], bytes]] = []
        if self._beacon_sock is not None:
            dests.append((self._beacon_sock, (self.group, self.port), payload))
        probe = self._probe_sock
        if probe is not None:
            try:
                xinfo = build_xinfo_query()
            except Exception:
                xinfo = b"/xinfo\x00\x00,\x00\x00\x00"
            dests.append((probe, ("255.255.255.255", XINFO_PORT), xinfo))
            dests.append((probe, (MDNS_GROUP, MDNS_PORT), build_mdns_query()))
            dests.append((probe, (SSDP_GROUP, SSDP_PORT), build_ssdp_msearch()))
            dests.append(
                (probe, (SSDP_GROUP, SSDP_PORT), build_ssdp_msearch("urn:behringer:device:X-AIR:1"))
            )
        for sock, dest, blob in dests:
            try:
                sock.sendto(blob, dest)
            except OSError:
                log.debug("discovery send %s", dest, exc_info=True)

    def _write_state(self) -> None:
        path = self._state_path
        if path is None:
            return
        snap = self.snapshot()
        key = (
            snap.get("last_discovery_ts"),
            len(snap.get("discovered_devices") or []),
            snap.get("discovery_health"),
        )
        if key == self._last_state_key:
            return
        self._last_state_key = key
        payload = {"schema": 1, **snap, "exported_time_ns": time.time_ns()}
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            log.debug("discovery state write", exc_info=True)


def default_discovery_path(project_root: Path) -> Path:
    raw = os.getenv("XAIR_DISCOVERY_STATE_PATH")
    if raw and str(raw).strip():
        return Path(str(raw).strip()).expanduser()
    return project_root / ".xair_discovery.json"
