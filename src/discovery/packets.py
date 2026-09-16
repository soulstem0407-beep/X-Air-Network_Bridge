"""Wire formats for X-AIR / peer discovery. No audio I/O.

Presence beacons are multicast on a dedicated group/port. Audio stays unicast UDP.
Mixer detection uses OSC ``/xinfo`` (broadcast), mDNS, and SSDP on the control thread.
"""
from __future__ import annotations

import json
import socket
import struct
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

BEACON_MAGIC = b"XBRP"
BEACON_VERSION = 1
DEFAULT_DISCOVERY_GROUP = "239.255.77.77"
DEFAULT_DISCOVERY_PORT = 50010
MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353
SSDP_GROUP = "239.255.255.250"
SSDP_PORT = 1900
XINFO_PORT = 10024
DEFAULT_TTL_S = 8.0
DEFAULT_PROBE_S = 2.0

MDNS_PTR_NAMES = (
    "_osc._udp.local",
    "_xair-osc._udp.local",
    "_xair._tcp.local",
)

_MIXER_MARKERS = ("x18", "xr18", "xr16", "xr12", "xair", "x-air", "x air")
_AUTO_HOSTS = frozenset(("", "auto", "discover", "*"))

DNS_TYPE_A = 1
DNS_TYPE_PTR = 12
DNS_TYPE_TXT = 16
DNS_TYPE_SRV = 33
DNS_CLASS_IN = 1


def wants_auto_host(host: Optional[str]) -> bool:
    """True when the configured IP should be filled from discovery."""
    return str(host or "").strip().lower() in _AUTO_HOSTS


def looks_like_mixer_text(*parts: Optional[str]) -> bool:
    blob = " ".join(p or "" for p in parts).lower()
    return any(m in blob for m in _MIXER_MARKERS)


def classify_discovery_health(
    *,
    enabled: bool,
    mixer_count: int,
    peer_count: int,
    last_discovery_age_s: Optional[float],
    listen_errors: int = 0,
    stale_s: float = 10.0,
) -> str:
    """Dashboard color key: ``off`` / ``ok`` / ``warn`` / ``fail``."""
    if not enabled:
        return "off"
    if listen_errors and mixer_count <= 0 and peer_count <= 0:
        return "fail"
    if mixer_count > 0:
        if last_discovery_age_s is not None and last_discovery_age_s > stale_s:
            return "warn"
        return "ok"
    if peer_count > 0:
        return "warn"
    if last_discovery_age_s is None:
        return "warn"
    if last_discovery_age_s >= stale_s * 2:
        return "fail"
    return "warn"


def local_ipv4() -> str:
    """Best-effort LAN address for presence beacons (control thread)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        return str(ip) if ip else "127.0.0.1"
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


@dataclass
class DiscoveredDevice:
    kind: str
    ip: str
    name: str = ""
    model: str = ""
    firmware: str = ""
    port: int = 0
    source: str = ""
    role: str = ""
    last_seen_ts: int = 0
    last_seen_mono: float = 0.0

    def identity(self) -> str:
        role = self.role or ""
        return f"{self.kind}|{self.ip}|{role}"

    def as_dict(self) -> Dict[str, object]:
        return {
            "name": self.name or None,
            "model": self.model or None,
            "ip": self.ip,
            "firmware": self.firmware or None,
            "kind": self.kind,
            "source": self.source or None,
            "role": self.role or None,
            "port": self.port or None,
        }


def merge_device(dst: DiscoveredDevice, src: DiscoveredDevice) -> DiscoveredDevice:
    """Fill empty fields from ``src``; keep the newest timestamps."""
    if src.name and (not dst.name or dst.name.startswith("_")):
        dst.name = src.name
    elif src.name and not dst.name:
        dst.name = src.name
    if src.model:
        dst.model = src.model
    if src.firmware:
        dst.firmware = src.firmware
    if src.port:
        dst.port = src.port
    if src.source and src.source not in (dst.source or ""):
        if dst.source:
            parts = {p.strip() for p in dst.source.split(",") if p.strip()}
            parts.add(src.source)
            dst.source = ",".join(sorted(parts))
        else:
            dst.source = src.source
    if src.role:
        dst.role = src.role
    if src.last_seen_ts >= dst.last_seen_ts:
        dst.last_seen_ts = src.last_seen_ts
        dst.last_seen_mono = src.last_seen_mono
    return dst


class DeviceRegistry:
    """In-memory table of mixers and bridge peers. Control thread only."""

    def __init__(self, ttl_s: float = DEFAULT_TTL_S) -> None:
        self.ttl_s = float(ttl_s)
        self._items: Dict[str, DiscoveredDevice] = {}
        self.last_discovery_ts: Optional[int] = None
        self.listen_errors = 0

    def note(self, device: DiscoveredDevice) -> DiscoveredDevice:
        key = device.identity()
        existing = self._items.get(key)
        if existing is None:
            self._items[key] = device
            self.last_discovery_ts = device.last_seen_ts or self.last_discovery_ts
            return device
        merge_device(existing, device)
        self.last_discovery_ts = existing.last_seen_ts or self.last_discovery_ts
        return existing

    def expire(self, now_mono: float) -> None:
        dead = [
            key
            for key, dev in self._items.items()
            if dev.last_seen_mono and (now_mono - dev.last_seen_mono) > self.ttl_s
        ]
        for key in dead:
            self._items.pop(key, None)

    def devices(self) -> List[DiscoveredDevice]:
        return sorted(self._items.values(), key=lambda d: (d.kind, d.ip, d.role))

    def mixers(self) -> List[DiscoveredDevice]:
        return [d for d in self.devices() if d.kind == "mixer"]

    def peers(self, role: Optional[str] = None) -> List[DiscoveredDevice]:
        out = [d for d in self.devices() if d.kind == "peer"]
        if role:
            want = str(role).lower()
            out = [d for d in out if d.role == want]
        return out

    def snapshot(self, *, enabled: bool, now_mono: Optional[float] = None) -> Dict[str, object]:
        mixers = self.mixers()
        peers = self.peers()
        age: Optional[float] = None
        if now_mono is not None:
            seen = [d.last_seen_mono for d in self.devices() if d.last_seen_mono]
            if seen:
                age = float(now_mono - max(seen))
        health = classify_discovery_health(
            enabled=enabled,
            mixer_count=len(mixers),
            peer_count=len(peers),
            last_discovery_age_s=age,
            listen_errors=self.listen_errors,
        )
        return {
            "discovery_enabled": bool(enabled),
            "discovered_devices": [d.as_dict() for d in self.devices()],
            "last_discovery_ts": self.last_discovery_ts,
            "discovery_health": health,
        }


def pack_beacon(
    *,
    role: str,
    name: str,
    ip: str,
    audio_port: int,
    ts_ns: int,
) -> bytes:
    body = {
        "v": BEACON_VERSION,
        "role": str(role),
        "name": str(name),
        "ip": str(ip),
        "audio_port": int(audio_port),
        "ts_ns": int(ts_ns),
    }
    raw = json.dumps(body, separators=(",", ":")).encode("utf-8")
    return BEACON_MAGIC + raw


def parse_beacon(data: bytes, src_ip: str = "") -> Optional[DiscoveredDevice]:
    if not data.startswith(BEACON_MAGIC):
        return None
    try:
        body = json.loads(data[len(BEACON_MAGIC) :].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    if int(body.get("v") or 0) != BEACON_VERSION:
        return None
    role = str(body.get("role") or "").strip().lower()
    if role not in ("server", "client"):
        return None
    ip = str(body.get("ip") or src_ip or "").strip()
    if not ip:
        return None
    port = int(body.get("audio_port") or 0)
    name = str(body.get("name") or "xair-network-bridge")
    ts = int(body.get("ts_ns") or 0)
    return DiscoveredDevice(
        kind="peer",
        ip=ip,
        name=name,
        model="bridge",
        firmware="",
        port=port,
        source="beacon",
        role=role,
        last_seen_ts=ts,
    )


def encode_dns_name(name: str) -> bytes:
    out = bytearray()
    trimmed = name.rstrip(".")
    if trimmed:
        for lab in trimmed.split("."):
            raw = lab.encode("utf-8")
            if len(raw) > 63:
                raise ValueError("DNS label too long")
            out.append(len(raw))
            out.extend(raw)
    out.append(0)
    return bytes(out)


def parse_dns_name(buf: bytes, offset: int) -> Tuple[str, int]:
    labels: List[str] = []
    jumped = False
    pos = offset
    end = offset
    hops = 0
    while True:
        if pos >= len(buf):
            raise ValueError("DNS name truncated")
        length = buf[pos]
        if length == 0:
            pos += 1
            if not jumped:
                end = pos
            break
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(buf):
                raise ValueError("DNS pointer truncated")
            ptr = ((length & 0x3F) << 8) | buf[pos + 1]
            if not jumped:
                end = pos + 2
            pos = ptr
            jumped = True
            hops += 1
            if hops > 16:
                raise ValueError("DNS pointer loop")
            continue
        if length & 0xC0:
            raise ValueError("unsupported DNS label")
        pos += 1
        if pos + length > len(buf):
            raise ValueError("DNS label truncated")
        labels.append(buf[pos : pos + length].decode("utf-8", "replace"))
        pos += length
        if not jumped:
            end = pos
    return ".".join(labels), end


def build_mdns_query(names: Sequence[str] = MDNS_PTR_NAMES) -> bytes:
    header = struct.pack("!HHHHHH", 0, 0, len(names), 0, 0, 0)
    parts = [header]
    for name in names:
        parts.append(encode_dns_name(name))
        parts.append(struct.pack("!HH", DNS_TYPE_PTR, DNS_CLASS_IN))
    return b"".join(parts)


def parse_mdns_devices(buf: bytes, src_ip: str = "") -> List[DiscoveredDevice]:
    """Extract mixer-like A/PTR/SRV records from an mDNS datagram."""
    if len(buf) < 12:
        return []
    try:
        _id, _flags, qd, an, ns, ar = struct.unpack_from("!HHHHHH", buf, 0)
    except struct.error:
        return []
    pos = 12
    try:
        for _ in range(qd):
            _name, pos = parse_dns_name(buf, pos)
            pos += 4
    except ValueError:
        return []

    ptr_targets: List[str] = []
    a_by_name: Dict[str, str] = {}
    srv_by_name: Dict[str, Tuple[str, int]] = {}
    txt_by_name: Dict[str, str] = {}
    total = an + ns + ar
    for _ in range(total):
        try:
            owner, pos = parse_dns_name(buf, pos)
            if pos + 10 > len(buf):
                break
            rtype, _rclass, _ttl, rdlen = struct.unpack_from("!HHIH", buf, pos)
            pos += 10
            rdata = buf[pos : pos + rdlen]
            pos += rdlen
        except (ValueError, struct.error):
            break
        owner_l = owner.rstrip(".").lower()
        if rtype == DNS_TYPE_A and len(rdata) == 4:
            a_by_name[owner_l] = socket.inet_ntoa(rdata)
        elif rtype == DNS_TYPE_PTR:
            try:
                target, _ = parse_dns_name(buf, pos - rdlen)
            except ValueError:
                continue
            ptr_targets.append(target)
        elif rtype == DNS_TYPE_SRV and len(rdata) >= 6:
            _prio, _w, port = struct.unpack_from("!HHH", rdata, 0)
            try:
                target, _ = parse_dns_name(buf, pos - rdlen + 6)
            except ValueError:
                target = ""
            srv_by_name[owner_l] = (target.rstrip(".").lower(), int(port))
        elif rtype == DNS_TYPE_TXT:
            txt_by_name[owner_l] = _decode_txt(rdata)

    out: List[DiscoveredDevice] = []
    seen_ip: set[str] = set()

    def _add(ip: str, name: str, model: str, port: int, extra: str) -> None:
        if not ip or ip in seen_ip:
            return
        blob = " ".join((name, model, extra))
        if not looks_like_mixer_text(blob, name):
            return
        seen_ip.add(ip)
        model_s = model or _model_from_name(name)
        out.append(
            DiscoveredDevice(
                kind="mixer",
                ip=ip,
                name=name or model_s,
                model=model_s,
                firmware=_firmware_from_txt(extra),
                port=port or XINFO_PORT,
                source="mdns",
            )
        )

    for inst in ptr_targets:
        inst_l = inst.rstrip(".").lower()
        short = inst.split(".")[0] if inst else ""
        ip = a_by_name.get(_host_from_instance(inst_l), "")
        port = XINFO_PORT
        if inst_l in srv_by_name:
            target, port = srv_by_name[inst_l]
            ip = ip or a_by_name.get(target, "")
        ip = ip or src_ip
        _add(ip, short, _model_from_name(short), port, txt_by_name.get(inst_l, ""))

    for owner, ip in a_by_name.items():
        short = owner.split(".")[0]
        _add(ip, short, _model_from_name(short), XINFO_PORT, txt_by_name.get(owner, ""))

    return out


def _decode_txt(rdata: bytes) -> str:
    parts: List[str] = []
    pos = 0
    while pos < len(rdata):
        n = rdata[pos]
        pos += 1
        parts.append(rdata[pos : pos + n].decode("utf-8", "replace"))
        pos += n
    return " ".join(parts)


def _host_from_instance(instance: str) -> str:
    """X18-ABCD._osc._udp.local → x18-abcd.local (common A-record owner)."""
    labels = instance.rstrip(".").split(".")
    if not labels:
        return instance
    return labels[0] + ".local"


def _model_from_name(name: str) -> str:
    n = (name or "").upper()
    for token in ("XR18", "XR16", "XR12", "X18"):
        if token in n:
            return token
    if "XAIR" in n or "X-AIR" in n:
        return "XAIR"
    return ""


def _firmware_from_txt(txt: str) -> str:
    for part in (txt or "").split():
        if "=" in part:
            k, _, v = part.partition("=")
            if k.lower() in ("fw", "firmware", "version", "ver"):
                return v
    return ""


def build_ssdp_msearch(st: str = "ssdp:all", mx: int = 2) -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_GROUP}:{SSDP_PORT}\r\n"
        'MAN: "ssdp:discover"\r\n'
        f"MX: {int(mx)}\r\n"
        f"ST: {st}\r\n"
        "\r\n"
    ).encode("utf-8")


def parse_ssdp(data: bytes, src_ip: str = "") -> Optional[DiscoveredDevice]:
    try:
        text = data.decode("utf-8", "replace")
    except UnicodeDecodeError:
        return None
    head = text.split("\r\n\r\n", 1)[0]
    if not head:
        return None
    first = head.split("\r\n", 1)[0].upper()
    if not (
        first.startswith("HTTP/")
        or first.startswith("NOTIFY")
        or first.startswith("M-SEARCH")
    ):
        return None
    headers: Dict[str, str] = {}
    for line in head.split("\r\n")[1:]:
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    blob = " ".join(
        (
            headers.get("server", ""),
            headers.get("usn", ""),
            headers.get("st", ""),
            headers.get("nt", ""),
            headers.get("location", ""),
        )
    )
    if not looks_like_mixer_text(blob):
        return None
    ip = src_ip
    loc = headers.get("location", "")
    if loc:
        try:
            host = urlparse(loc).hostname
            if host:
                ip = host
        except ValueError:
            pass
    if not ip:
        return None
    name = headers.get("usn", "") or headers.get("server", "") or "XAIR"
    name = name.split("::")[0].split(":")[-1] or name
    model = _model_from_name(blob) or _model_from_name(name)
    fw = ""
    server = headers.get("server", "")
    if "/" in server:
        fw = server.rsplit("/", 1)[-1].strip()
    return DiscoveredDevice(
        kind="mixer",
        ip=ip,
        name=name.strip()[:64],
        model=model,
        firmware=fw,
        port=XINFO_PORT,
        source="ssdp",
    )


def parse_xinfo_dgram(data: bytes, src_ip: str = "") -> Optional[DiscoveredDevice]:
    """Parse an OSC ``/xinfo`` reply: ip, name, model, firmware."""
    try:
        from pythonosc.osc_packet import OscPacket
    except ImportError:
        return _parse_xinfo_raw(data, src_ip)
    try:
        pkt = OscPacket(data)
    except Exception:
        return _parse_xinfo_raw(data, src_ip)
    messages = getattr(pkt, "messages", None) or []
    for timed in messages:
        msg = getattr(timed, "message", timed)
        if getattr(msg, "address", "") != "/xinfo":
            continue
        got = _xinfo_from_params(list(getattr(msg, "params", ()) or ()), src_ip)
        if got is not None:
            return got
    return _parse_xinfo_raw(data, src_ip)


def build_xinfo_query() -> bytes:
    from pythonosc.osc_message_builder import OscMessageBuilder

    return OscMessageBuilder("/xinfo").build().dgram


def _xinfo_from_params(params: Iterable[object], src_ip: str) -> Optional[DiscoveredDevice]:
    strs: List[str] = []
    for p in params:
        if isinstance(p, bytes):
            strs.append(p.decode("utf-8", "replace"))
        else:
            strs.append(str(p))
    if not strs:
        return None
    ip = strs[0] if _looks_ip(strs[0]) else src_ip
    name = strs[1] if len(strs) > 1 else (strs[0] if not _looks_ip(strs[0]) else "XAIR")
    model = strs[2] if len(strs) > 2 else _model_from_name(name)
    firmware = strs[3] if len(strs) > 3 else ""
    if not ip:
        return None
    if not looks_like_mixer_text(name, model) and model not in ("X18", "XR18", "XR16", "XR12", "XAIR"):
        # /xinfo from a real desk is authoritative even if the name is custom.
        if len(strs) < 3:
            return None
    return DiscoveredDevice(
        kind="mixer",
        ip=ip,
        name=name,
        model=model,
        firmware=firmware,
        port=XINFO_PORT,
        source="xinfo",
    )


def _looks_ip(s: str) -> bool:
    try:
        socket.inet_aton(s)
        return True
    except OSError:
        return False


def _parse_xinfo_raw(data: bytes, src_ip: str) -> Optional[DiscoveredDevice]:
    """Minimal OSC string bundle parser for ``/xinfo`` without relying on pythonosc internals."""
    if b"/xinfo" not in data:
        return None
    # OSC strings are NUL-padded to 4 bytes. Skip address + type tag, collect 's' args.
    pos = 0
    if data.startswith(b"#bundle"):
        if len(data) < 16:
            return None
        pos = 16
        while pos + 4 <= len(data):
            size = struct.unpack_from(">I", data, pos)[0]
            pos += 4
            chunk = data[pos : pos + size]
            pos += size
            got = _parse_xinfo_raw(chunk, src_ip)
            if got is not None:
                return got
        return None
    addr, pos = _osc_padded_str(data, 0)
    if addr != "/xinfo":
        return None
    tags, pos = _osc_padded_str(data, pos)
    if not tags.startswith(","):
        return None
    params: List[str] = []
    for tag in tags[1:]:
        if tag != "s":
            break
        s, pos = _osc_padded_str(data, pos)
        params.append(s)
    return _xinfo_from_params(params, src_ip)


def _osc_padded_str(buf: bytes, offset: int) -> Tuple[str, int]:
    end = buf.find(b"\x00", offset)
    if end < 0:
        raise ValueError("OSC string truncated")
    s = buf[offset:end].decode("utf-8", "replace")
    size = end - offset + 1
    pad = (4 - (size % 4)) % 4
    return s, end + 1 + pad


def empty_snapshot(*, enabled: bool) -> Dict[str, object]:
    return {
        "discovery_enabled": bool(enabled),
        "discovered_devices": [],
        "last_discovery_ts": None,
        "discovery_health": "off" if not enabled else "warn",
    }
