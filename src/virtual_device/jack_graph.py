"""Permanent JACK/PipeWire patchbay: System in → X-Air Network Bridge → REAPER → System out.

QjackCtl otherwise lets WirePlumber (or the user) wire the bridge straight to
the speakers, or REAPER straight to ``system:capture``, skipping the bridge.
This module always restores one chain, in this order:

1. System input  (physical capture, never HDMI) → ``xair_net_bridge:*_in``
2. ``xair_net_bridge:*_out`` → REAPER inputs
3. REAPER outputs → System output (physical playback, never HDMI)

Bypass edges (system→REAPER, bridge→system, system→system, REAPER→bridge)
are disconnected. Not gated on ``.env``. Does not touch UDP/jitter/FEC/Opus.
Connecting is done off the JACK process callback (thread + port-registration
flag). REAPER may appear later; the chain is retried until it exists.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Iterable, List, Optional, Sequence, Tuple

from .x18_device import is_analog_device, is_hdmi_device

log = logging.getLogger(__name__)

ConnectPair = Tuple[str, str]

_NAT = re.compile(r"(\d+)")
_REAPER_CLIENT = re.compile(r"reaper", re.IGNORECASE)
_X18_HINT = re.compile(r"x[\s\-_]?18|xr[\s\-_]?18|x[\s\-_]?air|xair|behringer", re.IGNORECASE)
_PLAYBACK_HINT = re.compile(r"playback|playback_|_out$|speaker|analog", re.IGNORECASE)


def _full_name(port: Any) -> str:
    if isinstance(port, str):
        return port
    name = getattr(port, "name", None)
    if name:
        return str(name)
    return str(port)


def client_of(port: str) -> str:
    s = _full_name(port)
    return s.split(":", 1)[0] if ":" in s else s


def short_of(port: str) -> str:
    s = _full_name(port)
    return s.split(":", 1)[-1]


def natkey(port: str) -> List[Any]:
    s = _full_name(port)
    return [int(p) if p.isdigit() else p.lower() for p in _NAT.split(s)]


def _sorted_ports(ports: Iterable[Any]) -> List[str]:
    names = [_full_name(p) for p in ports]
    names = [n for n in names if n]
    names.sort(key=natkey)
    return names


def _port_aliases(port: Any) -> List[str]:
    aliases = getattr(port, "aliases", None)
    if callable(aliases):
        try:
            aliases = aliases()
        except Exception:
            return []
    if not aliases:
        return []
    return [str(a) for a in aliases]


def is_hdmi_port(port: Any) -> bool:
    """True if the JACK name *or* ALSA alias is HDMI (system:playback can hide that)."""
    if is_hdmi_device(_full_name(port)):
        return True
    return any(is_hdmi_device(a) for a in _port_aliases(port))


def is_reaper_port(port: str) -> bool:
    return bool(_REAPER_CLIENT.search(client_of(port)))


def is_system_client(port: str) -> bool:
    return client_of(port).strip().lower() == "system"


def is_x18ish(port: str) -> bool:
    return bool(_X18_HINT.search(_full_name(port)))


def pick_system_inputs(physical_capture: Sequence[Any]) -> List[str]:
    """Hardware capture → bridge ins. Onboard analog over HDMI-backed ``system``."""
    caps = [p for p in physical_capture if not is_hdmi_port(p)]
    analog = [p for p in caps if is_analog_device(_full_name(p))]
    if analog:
        return _sorted_ports(analog)
    system = [p for p in caps if is_system_client(_full_name(p))]
    if system:
        return _sorted_ports(system)
    x18 = [p for p in caps if is_x18ish(_full_name(p))]
    if x18:
        return _sorted_ports(x18)
    return _sorted_ports(caps)


def pick_system_outputs(physical_playback: Sequence[Any]) -> List[str]:
    """Hardware playback ← REAPER. Onboard analog; never HDMI (name or alias)."""
    pb = [p for p in physical_playback if not is_hdmi_port(p)]
    analog = [p for p in pb if is_analog_device(_full_name(p))]
    if analog:
        return _sorted_ports(analog)
    system = [p for p in pb if is_system_client(_full_name(p))]
    if system:
        return _sorted_ports(system)
    hinted = [p for p in pb if _PLAYBACK_HINT.search(_full_name(p))]
    if hinted:
        return _sorted_ports(hinted)
    return _sorted_ports(pb)


def pick_reaper_inputs(all_inputs: Sequence[str], *, bridge_client: str) -> List[str]:
    ours = client_of(bridge_client + ":x")
    return [
        p
        for p in _sorted_ports(all_inputs)
        if is_reaper_port(p) and client_of(p) != ours
    ]


def pick_reaper_outputs(all_outputs: Sequence[str], *, bridge_client: str) -> List[str]:
    ours = client_of(bridge_client + ":x")
    return [
        p
        for p in _sorted_ports(all_outputs)
        if is_reaper_port(p) and client_of(p) != ours
    ]


def zip_connect(sources: Sequence[str], dests: Sequence[str]) -> List[ConnectPair]:
    n = min(len(sources), len(dests))
    return [(sources[i], dests[i]) for i in range(n)]


def plan_forced_graph(
    *,
    bridge_ins: Sequence[str],
    bridge_outs: Sequence[str],
    physical_capture: Sequence[str],
    physical_playback: Sequence[str],
    all_inputs: Sequence[str],
    all_outputs: Sequence[str],
    bridge_client: str,
) -> Tuple[List[ConnectPair], List[str]]:
    """Return (src, dst) pairs in chain order, plus human-readable notes."""
    notes: List[str] = []
    sys_in = pick_system_inputs(physical_capture)
    sys_out = pick_system_outputs(physical_playback)
    r_in = pick_reaper_inputs(all_inputs, bridge_client=bridge_client)
    r_out = pick_reaper_outputs(all_outputs, bridge_client=bridge_client)
    ins = _sorted_ports(bridge_ins)
    outs = _sorted_ports(bridge_outs)

    wanted: List[ConnectPair] = []
    if sys_in and ins:
        wanted.extend(zip_connect(sys_in, ins))
    else:
        notes.append("esperando System input (capture físico, no HDMI)")

    if r_in and outs:
        wanted.extend(zip_connect(outs, r_in))
    else:
        notes.append("esperando REAPER inputs")

    if r_out and sys_out:
        wanted.extend(zip_connect(r_out, sys_out))
    else:
        notes.append("esperando REAPER outputs y/o System output (no HDMI)")

    return wanted, notes


def is_bypass(
    src: str,
    dst: str,
    *,
    bridge_ins: Sequence[str],
    bridge_outs: Sequence[str],
    sys_in: Sequence[str],
    sys_out: Sequence[str],
    reaper_ins: Sequence[str],
    reaper_outs: Sequence[str],
) -> bool:
    """True if this edge skips the required System→bridge→REAPER→System order."""
    b_in = set(bridge_ins)
    b_out = set(bridge_outs)
    s_in = set(sys_in)
    s_out = set(sys_out)
    r_in = set(reaper_ins)
    r_out = set(reaper_outs)
    # System → REAPER (skips bridge)
    if src in s_in and dst in r_in:
        return True
    # System → System
    if src in s_in and dst in s_out:
        return True
    # Bridge → System (skips REAPER)
    if src in b_out and dst in s_out:
        return True
    # REAPER → bridge (feedback)
    if src in r_out and dst in b_in:
        return True
    # System → not our bridge ins (steal capture from the chain) when dest is REAPER or system
    return False


class JackGraphRouter:
    """Watches the JACK graph and keeps the forced chain connected."""

    def __init__(self, client: Any, *, bridge_client: str) -> None:
        self._client = client
        self._bridge_client = str(bridge_client)
        self._stop = threading.Event()
        self._dirty = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._last_sig: Optional[Tuple[Tuple[ConnectPair, ...], Tuple[str, ...]]] = None
        self._quiet_until = 0.0

    def quiet(self, seconds: float) -> None:
        """Ignore graph callbacks for a bit (buffer-size changes must not rewire)."""
        until = time.monotonic() + max(0.0, float(seconds))
        if until > self._quiet_until:
            self._quiet_until = until

    def start(self) -> None:
        client = self._client
        try:
            client.set_port_registration_callback(self._on_port_reg)
        except Exception:
            log.debug("JACK port-registration callback no disponible", exc_info=True)
        try:
            client.set_client_registration_callback(self._on_client_reg)
        except Exception:
            log.debug("JACK client-registration callback no disponible", exc_info=True)
        self._dirty.set()
        self._thread = threading.Thread(target=self._loop, name="xair-jack-graph", daemon=True)
        self._thread.start()
        self.apply()

    def stop(self) -> None:
        self._stop.set()
        self._dirty.set()
        th = self._thread
        self._thread = None
        if th is not None and th.is_alive():
            th.join(timeout=1.0)

    def _on_port_reg(self, *args: Any) -> None:  # noqa: ANN401
        if time.monotonic() < self._quiet_until:
            return
        self._dirty.set()

    def _on_client_reg(self, *args: Any) -> None:  # noqa: ANN401
        if time.monotonic() < self._quiet_until:
            return
        self._dirty.set()

    def _loop(self) -> None:
        # Only rewire when ports/clients appear. Polling every 2s rebuilt the
        # graph and PipeWire reset Frames/Period to the default 1024.
        while not self._stop.is_set():
            self._dirty.wait(timeout=1.0)
            if self._stop.is_set():
                break
            if not self._dirty.is_set():
                continue
            # QjackCtl buffer-size apply emits a graph change ~1ms before the
            # Frames/Period callback. Wait so quiet() can suppress rewiring,
            # which otherwise resets PipeWire to 1024.
            time.sleep(0.35)
            if self._stop.is_set():
                break
            if time.monotonic() < self._quiet_until:
                self._dirty.clear()
                continue
            if not self._dirty.is_set():
                continue
            self._dirty.clear()
            try:
                self.apply()
            except Exception:
                log.debug("JACK graph apply", exc_info=True)

    def _names(self, ports: Iterable[Any]) -> List[str]:
        return _sorted_ports(ports)

    def apply(self) -> None:
        """Inspect ports, drop bypasses, connect System→bridge→REAPER→System."""
        client = self._client
        if client is None:
            return
        with self._lock:
            self._apply_unlocked(client)

    def _apply_unlocked(self, client: Any) -> None:
        def gp(**kwargs: Any) -> List[Any]:
            try:
                return list(client.get_ports(**kwargs) or [])
            except TypeError:
                return list(client.get_ports() or [])
            except Exception:
                log.debug("get_ports failed", exc_info=True)
                return []

        def names_of(ports: Sequence[Any]) -> List[str]:
            return [_full_name(p) for p in ports if _full_name(p)]

        physical_cap = gp(is_audio=True, is_output=True, is_physical=True)
        physical_pb = gp(is_audio=True, is_input=True, is_physical=True)
        all_in = names_of(gp(is_audio=True, is_input=True))
        all_out = names_of(gp(is_audio=True, is_output=True))

        try:
            bridge_ins = self._names(client.inports)
            bridge_outs = self._names(client.outports)
        except Exception:
            prefix = self._bridge_client + ":"
            bridge_ins = [p for p in all_in if p.startswith(prefix)]
            bridge_outs = [p for p in all_out if p.startswith(prefix)]

        # JACK-Client register() stores the short name; graph needs client:port.
        bc = self._bridge_client
        bridge_ins = [p if ":" in p else f"{bc}:{p}" for p in bridge_ins]
        bridge_outs = [p if ":" in p else f"{bc}:{p}" for p in bridge_outs]

        wanted, notes = plan_forced_graph(
            bridge_ins=bridge_ins,
            bridge_outs=bridge_outs,
            physical_capture=physical_cap,
            physical_playback=physical_pb,
            all_inputs=all_in,
            all_outputs=all_out,
            bridge_client=bc,
        )
        sys_in = pick_system_inputs(physical_cap)
        sys_out = pick_system_outputs(physical_pb)
        r_in = pick_reaper_inputs(all_in, bridge_client=bc)
        r_out = pick_reaper_outputs(all_out, bridge_client=bc)

        sig = (tuple(wanted), tuple(notes))
        if sig == self._last_sig:
            return
        if time.monotonic() < self._quiet_until:
            return

        wanted_set = set(wanted)
        self._drop_bypasses(
            client,
            bridge_ins=bridge_ins,
            bridge_outs=bridge_outs,
            sys_in=sys_in,
            sys_out=sys_out,
            reaper_ins=r_in,
            reaper_outs=r_out,
            keep=wanted_set,
        )
        for src, dst in wanted:
            self._connect(client, src, dst)
        self._last_sig = sig
        if wanted:
            log.info(
                "JACK graph: System input → %s → REAPER → System output (%s enlaces)",
                bc,
                len(wanted),
            )
            for src, dst in wanted:
                log.info("  %s → %s", src, dst)
            for note in notes:
                log.info("JACK graph: %s", note)
        else:
            for note in notes:
                log.info("JACK graph: %s", note)

    def _drop_bypasses(
        self,
        client: Any,
        *,
        bridge_ins: Sequence[str],
        bridge_outs: Sequence[str],
        sys_in: Sequence[str],
        sys_out: Sequence[str],
        reaper_ins: Sequence[str],
        reaper_outs: Sequence[str],
        keep: set,
    ) -> None:
        watched = list(bridge_ins) + list(bridge_outs) + list(sys_in) + list(sys_out) + list(reaper_ins) + list(reaper_outs)
        seen = set()
        for port in watched:
            if port in seen:
                continue
            seen.add(port)
            try:
                conns = client.get_all_connections(port) or []
            except Exception:
                continue
            for other in conns:
                other_n = _full_name(other)
                # get_all_connections on an output lists destinations; on an input, sources.
                # Try both orientations.
                for src, dst in ((port, other_n), (other_n, port)):
                    pair = (src, dst)
                    if pair in keep:
                        continue
                    if is_bypass(
                        src,
                        dst,
                        bridge_ins=bridge_ins,
                        bridge_outs=bridge_outs,
                        sys_in=sys_in,
                        sys_out=sys_out,
                        reaper_ins=reaper_ins,
                        reaper_outs=reaper_outs,
                    ):
                        self._disconnect(client, src, dst)

    def _connect(self, client: Any, src: str, dst: str) -> None:
        try:
            already = False
            try:
                conns = [_full_name(p) for p in (client.get_all_connections(src) or [])]
                already = dst in conns
            except Exception:
                already = False
            if already:
                return
            client.connect(src, dst)
        except Exception as exc:
            msg = str(exc).lower()
            if "already connected" in msg:
                return
            log.debug("JACK connect %s → %s: %s", src, dst, exc)

    def _disconnect(self, client: Any, src: str, dst: str) -> None:
        try:
            client.disconnect(src, dst)
            log.info("JACK graph: quitado bypass %s → %s", src, dst)
        except Exception:
            log.debug("JACK disconnect %s → %s", src, dst, exc_info=True)
