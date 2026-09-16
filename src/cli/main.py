"""CLI for X-Air Network Bridge."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import click
from dotenv import load_dotenv

from ..network_sender.protocol import HEADER_SIZE
from ..network_sender.fec import DEFAULT_FEC_GROUP, clamp_fec_group
from ..network_sender.opus_codec import (
    DEFAULT_OPUS_BITRATE,
    DEFAULT_OPUS_FRAME_MS,
    clamp_opus_bitrate,
    clamp_opus_frame_ms,
    opus_frame_samples,
)
from ..network_receiver.adaptive_jitter import AdaptiveJitterConfig
from ..virtual_device import bridge_device
from . import state


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _ensure_env_loaded(*, override: bool = False) -> None:
    env_path = _project_root() / ".env"
    if env_path.is_file():
        load_dotenv(dotenv_path=env_path, override=override)
    load_dotenv(override=override)


def _int_env(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        return default
    try:
        return int(v)
    except ValueError:
        raise click.ClickException(f"{name} inválido: {v!r}")


def _str_env(name: str, default: str) -> str:
    v = os.getenv(name)
    return default if v is None else str(v)


def _float_env(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        return default
    try:
        return float(v)
    except ValueError:
        raise click.ClickException(f"{name} inválido: {v!r}")


def _bool_env(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None or not str(v).strip():
        return default
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _sync_state_path(root: Path) -> Path:
    raw = os.getenv("XAIR_SYNC_STATE_PATH")
    if raw and str(raw).strip():
        return Path(str(raw).strip()).expanduser()
    return root / ".xair_sync_state.json"


def _read_sync_snapshot(root: Path) -> tuple[Optional[dict], Path]:
    path = _sync_state_path(root)
    if not path.is_file():
        return None, path
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return None, path
        return data, path
    except Exception:
        return None, path


def _sync_snapshot_valid(data: dict, path: Path) -> bool:
    from ..dashboard.reader import snapshot_fresh

    return snapshot_fresh(data, path)


def _merged_config() -> dict:
    _ensure_env_loaded()
    rt = state.load_runtime(_project_root())
    port = rt.get("udp_port")
    jitter = rt.get("jitter_packets")
    peer = rt.get("peer_host")
    jitter_n = int(jitter) if jitter is not None else _int_env("XAIR_JITTER_PACKETS", 5)
    jitter_cfg = AdaptiveJitterConfig.from_env(jitter_n)
    return {
        "input_device_query": (_str_env("XAIR_INPUT_DEVICE", "").strip() or None),
        "output_device_query": (_str_env("XAIR_OUTPUT_DEVICE", "").strip() or None),
        "udp_port": int(port) if port is not None else _int_env("XAIR_UDP_PORT", 50000),
        "bind_host": _str_env("XAIR_BIND_HOST", "0.0.0.0"),
        "peer_host": str(peer).strip()
        if peer
        else _str_env("XAIR_PEER_HOST", "127.0.0.1"),
        "jitter_packets": jitter_n,
        "jitter_config": jitter_cfg,
        "samples_per_packet": max(8, _int_env("XAIR_SAMPLES_PER_PACKET", 21)),
        "codec": _str_env("XAIR_CODEC", "PCM24"),
        "sample_rate": _int_env("XAIR_SAMPLE_RATE", 48000),
        "channels": _int_env("XAIR_CHANNELS", 18),
        "send_jitter_depth": max(1, _int_env("XAIR_SEND_JITTER_DEPTH", 1)),
        "metrics_interval_s": _float_env("XAIR_METRICS_INTERVAL_SEC", 5.0),
        "wasapi_exclusive": _bool_env("XAIR_WASAPI_EXCLUSIVE", False),
        "fec_enabled": _bool_env("XAIR_FEC", False),
        "fec_group": clamp_fec_group(_int_env("XAIR_FEC_GROUP", DEFAULT_FEC_GROUP)),
        "opus_enabled": _bool_env("XAIR_OPUS", False),
        "opus_bitrate": clamp_opus_bitrate(_int_env("XAIR_OPUS_BITRATE", DEFAULT_OPUS_BITRATE)),
        "opus_frame_ms": clamp_opus_frame_ms(_float_env("XAIR_OPUS_FRAME", DEFAULT_OPUS_FRAME_MS)),
        "return_osc": _bool_env("XAIR_RETURN_OSC", False),
        "return_audio": _bool_env("XAIR_RETURN_AUDIO", False),
        "return_safety": _bool_env("XAIR_RETURN_SAFETY", True),
        "return_udp_port": _int_env("XAIR_RETURN_UDP_PORT", 50001),
        "return_peer_host": _str_env("XAIR_RETURN_PEER_HOST", "") or _str_env("XAIR_PEER_HOST", "127.0.0.1"),
        "return_input_device": (_str_env("XAIR_RETURN_INPUT_DEVICE", "").strip() or None),
        "return_output_device": (_str_env("XAIR_RETURN_OUTPUT_DEVICE", "").strip() or None),
        "return_listen_host": _str_env("XAIR_RETURN_LISTEN_HOST", "0.0.0.0"),
        "return_listen_port": _int_env("XAIR_RETURN_LISTEN_PORT", 9002),
        "return_daw_host": _str_env("XAIR_RETURN_DAW_HOST", "127.0.0.1"),
        "return_daw_port": _int_env("XAIR_RETURN_DAW_PORT", 9001),
        "return_sends": _int_env("XAIR_RETURN_SENDS", 4),
        "return_buses": _int_env("XAIR_RETURN_BUSES", 4),
        "return_frame_samples": opus_frame_samples(
            _int_env("XAIR_SAMPLE_RATE", 48000),
            10.0,
        ),
        "discovery_enabled": _bool_env("XAIR_DISCOVERY", False),
        "discovery_wait_s": _float_env("XAIR_DISCOVERY_WAIT_SEC", 2.5),
        "discovery_group": _str_env("XAIR_DISCOVERY_GROUP", "239.255.77.77"),
        "discovery_port": _int_env("XAIR_DISCOVERY_PORT", 50010),
        "discovery_name": _str_env("XAIR_DISCOVERY_NAME", "xair-network-bridge"),
        "osc_host": _str_env("XAIR_OSC_HOST", "192.168.1.100"),
        "eq_enabled": _bool_env("XAIR_EQ", False),
        "dyn_enabled": _bool_env("XAIR_DYN", False),
        "fx_enabled": _bool_env("XAIR_FX", False),
        "record_enabled": _bool_env("XAIR_RECORD", False),
        "record_path": _str_env("XAIR_RECORD_PATH", ""),
    }


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _serve_until_signal(*, on_reload=None) -> None:
    """Block until SIGINT/SIGTERM. SIGHUP runs ``on_reload`` (no transport restart)."""
    stop = threading.Event()
    hup = threading.Event()

    def _stop(*_a: object) -> None:
        stop.set()

    def _got_hup(*_a: object) -> None:
        hup.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, _got_hup)
    log = logging.getLogger("xair.cli")
    while not stop.is_set():
        if hup.is_set():
            hup.clear()
            if on_reload is not None:
                try:
                    on_reload()
                except Exception:
                    log.exception("reload (SIGHUP) failed")
        stop.wait(0.5)


@click.group()
def cli() -> None:
    """X-Air Network Bridge: X18 USB → UDP → virtual device (PortAudio)."""


@cli.command("xair_network_bridge_server")
def start_server_cmd() -> None:
    """Inicia captura USB y envío UDP."""
    from ..network_sender.sender import NetworkSender
    from ..usb_capture.capture import CaptureConfig, USBCapture

    _setup_logging()
    cfg = _merged_config()
    log = logging.getLogger("xair.cli")
    disc = None
    peer_host = str(cfg["peer_host"])
    if cfg.get("discovery_enabled"):
        from ..discovery.packets import wants_auto_host
        from ..discovery.service import DiscoveryService, default_discovery_path

        disc = DiscoveryService.from_env(
            role="server",
            audio_port=int(cfg["udp_port"]),
            state_path=default_discovery_path(_project_root()),
            enabled=True,
        )
        disc.start()
        if wants_auto_host(peer_host):
            found = disc.wait_peer("client", float(cfg.get("discovery_wait_s") or 2.5))
            if found is not None:
                peer_host = found.ip
                log.info("audio peer via discovery: %s (%s)", found.ip, found.name)
            else:
                log.warning(
                    "discovery: no client beacon yet; audio stays unicast when a peer appears"
                )
    sender = NetworkSender(
        host=peer_host,
        port=cfg["udp_port"],
        sample_rate=cfg["sample_rate"],
        channels=cfg["channels"],
        codec=cfg["codec"],
        samples_per_packet=cfg["samples_per_packet"],
        send_jitter_depth=cfg["send_jitter_depth"],
        fec_enabled=bool(cfg["fec_enabled"]),
        fec_group=int(cfg["fec_group"]),
        opus_enabled=bool(cfg["opus_enabled"]),
        opus_bitrate=int(cfg["opus_bitrate"]),
        opus_frame_ms=float(cfg["opus_frame_ms"]),
    )

    capture_cfg = CaptureConfig(
        sample_rate=cfg["sample_rate"],
        channels=cfg["channels"],
        device_query=cfg["input_device_query"],
        blocksize=cfg["samples_per_packet"],
    )
    capture = USBCapture(capture_cfg)
    return_rx = None
    if cfg.get("return_audio"):
        from ..sync.return_audio import ReturnAudioReceiver

        return_rx = ReturnAudioReceiver(
            bind_host=str(cfg["bind_host"]),
            port=int(cfg["return_udp_port"]),
            sample_rate=int(cfg["sample_rate"]),
            output_device_query=cfg.get("return_output_device"),
            frame_samples=int(cfg.get("return_frame_samples") or 480),
        )
        return_rx.start()

    if disc is not None:
        def _on_peer(dev):  # type: ignore[no-untyped-def]
            from ..discovery.packets import wants_auto_host

            if wants_auto_host(cfg["peer_host"]) and getattr(dev, "role", "") == "client":
                sender.set_peer_host(dev.ip)
                log.info("audio peer via discovery: %s (%s)", dev.ip, dev.name)

        disc.set_peer_callback(_on_peer)

    stop = {"v": False}

    def cb(block):  # type: ignore[no-untyped-def]
        if stop["v"]:
            return
        try:
            sender.feed(block)
        except Exception:
            log.exception("Error enviando bloque")

    try:
        capture.start(cb)
        log.info(
            "Servidor activo → %s:%s codec=%s samples/pkt=%s fec=%s group=%s opus=%s bitrate=%s frame=%.1fms discovery=%s",
            sender.host,
            cfg["udp_port"],
            cfg["codec"],
            cfg["samples_per_packet"],
            "on" if cfg["fec_enabled"] else "off",
            cfg["fec_group"],
            "on" if cfg["opus_enabled"] else "off",
            cfg["opus_bitrate"],
            cfg["opus_frame_ms"],
            "on" if cfg.get("discovery_enabled") else "off",
        )

        def _reload_server() -> None:
            _ensure_env_loaded(override=True)
            new = _merged_config()
            sender.set_peer_host(str(new["peer_host"]))
            log.info(
                "reload: peer_host=%s (codec/FEC/Opus/jitter/UDP bind unchanged until restart)",
                sender.host,
            )

        try:
            _serve_until_signal(on_reload=_reload_server)
        except KeyboardInterrupt:
            pass
    finally:
        stop["v"] = True
        capture.stop()
        sender.close()
        if disc is not None:
            disc.stop()
        if return_rx is not None:
            return_rx.stop()


@cli.command("xair_network_bridge_client")
def start_client_cmd() -> None:
    """Recibe UDP y reproduce al dispositivo virtual."""
    from ..client.start_client import build_network_receiver, clear_running_sync_manager

    _setup_logging()
    cfg = _merged_config()
    log = logging.getLogger("xair.cli")
    root = _project_root()

    recv, sync_mgr = build_network_receiver(cfg, project_root=root)

    try:
        recv.start()
        log.info("Cliente escuchando en puerto UDP %s", cfg["udp_port"])

        def _reload_client() -> None:
            _ensure_env_loaded(override=True)
            _merged_config()
            log.info(
                "reload: .env re-read; UDP bind/jitter/FEC/Opus unchanged until restart"
            )

        try:
            _serve_until_signal(on_reload=_reload_client)
        except KeyboardInterrupt:
            pass
    finally:
        recv.stop()
        if sync_mgr is not None:
            sync_mgr.close()
        clear_running_sync_manager()


@cli.command("set-port")
@click.argument("port", type=int)
def set_port_cmd(port: int) -> None:
    """Persiste UDP en `.xair_runtime.json` (anula .env hasta borrar archivo)."""
    if not (1 <= port <= 65535):
        raise click.ClickException("puerto fuera de rango")
    root = _project_root()
    data = state.load_runtime(root)
    data["udp_port"] = port
    state.save_runtime(root, data)
    click.echo(f"udp_port={port} guardado en {state.runtime_path(root)}")


@cli.command("set-buffer")
@click.argument("packets", type=int)
def set_buffer_cmd(packets: int) -> None:
    """Buffer de jitter receptor (suelo si adaptive está on; 4–6 en LAN estable)."""
    if packets < 2 or packets > 256:
        raise click.ClickException("usa un valor entre 2 y 256")
    root = _project_root()
    data = state.load_runtime(root)
    data["jitter_packets"] = packets
    state.save_runtime(root, data)
    click.echo(f"jitter_packets={packets} guardado en {state.runtime_path(root)}")


@cli.command("status")
def status_cmd() -> None:
    """Muestra entorno combinado (.env + runtime)."""
    cfg = _merged_config()
    fam = bridge_device.detect_os()
    lb = ""
    if fam == bridge_device.OSFamily.LINUX:
        lb = "; loopback cargado=%s" % bridge_device.linux_loopback_loaded()

    click.echo(
        (
            "OS={os}{lb}\n"
            "peer={peer}:{port} bind={bind}\n"
            "SR={sr} ch={ch} codec={codec} samples/pkt={spp}\n"
            "jitter_packets={jit} adaptive={ad} ({jmin}–{jmax}) input={inp!r} output={out!r}\n"
            "udp_payload_approx_bytes={pay} metrics_log_s={mtr}"
            " WASAPI_exclusive={wx}\n"
            "fec={fec} group={fgrp}\n"
            "opus={opus} bitrate={obr} frame_ms={ofm}\n"
            "return_osc={rosc} return_audio={raud} safety={rsf} return_udp={rudp}\n"
            "discovery={disc} wait_s={dwait} group={dgrp}:{dport}\n"
            "eq={eq} dyn={dyn} fx={fx}\n"
            "record={rec} path={rpath}\n"
        ).format(
            os=fam.value,
            lb=lb,
            peer=cfg["peer_host"],
            port=cfg["udp_port"],
            bind=cfg["bind_host"],
            sr=cfg["sample_rate"],
            ch=cfg["channels"],
            codec=cfg["codec"],
            spp=cfg["samples_per_packet"],
            jit=cfg["jitter_packets"],
            ad="on" if cfg["jitter_config"].enabled else "off",
            jmin=cfg["jitter_config"].floor,
            jmax=cfg["jitter_config"].ceiling,
            inp=cfg["input_device_query"],
            out=cfg["output_device_query"],
            pay=cfg["samples_per_packet"] * cfg["channels"] * 3 + HEADER_SIZE,
            mtr=cfg["metrics_interval_s"],
            wx=cfg["wasapi_exclusive"],
            fec="on" if cfg["fec_enabled"] else "off",
            fgrp=cfg["fec_group"],
            opus="on" if cfg["opus_enabled"] else "off",
            obr=cfg["opus_bitrate"],
            ofm=cfg["opus_frame_ms"],
            rosc="on" if cfg["return_osc"] else "off",
            raud="on" if cfg["return_audio"] else "off",
            rsf="on" if cfg["return_safety"] else "off",
            rudp=cfg["return_udp_port"],
            disc="on" if cfg["discovery_enabled"] else "off",
            dwait=cfg["discovery_wait_s"],
            dgrp=cfg["discovery_group"],
            dport=cfg["discovery_port"],
            eq="on" if cfg["eq_enabled"] else "off",
            dyn="on" if cfg["dyn_enabled"] else "off",
            fx="on" if cfg["fx_enabled"] else "off",
            rec="on" if cfg["record_enabled"] else "off",
            rpath=cfg["record_path"] or "recordings/",
        )
    )
    click.echo(
        "named_jack={nj} jack_client={jc} jack_ports=duplex _in+_out "
        "(Linux, sin XAIR_OUTPUT_DEVICE; "
        "XAIR_VIRTUAL_PORT_NAMES=false → PortAudio)".format(
            nj="on" if _bool_env("XAIR_VIRTUAL_PORT_NAMES", True) else "off",
            jc=_str_env("XAIR_JACK_CLIENT_NAME", "xair_net_bridge"),
        )
    )
    click.echo("Sugerencias dispositivo virtual:")
    for h in bridge_device.hints_for_this_os():
        click.echo(f"  - {h.name_substring!r}: {h.notes}")
    click.echo(
        "reaper={rh}:{rp} listen={lh}:{lp} mode={mode} keep_alive={ka}s "
        "poll={poll}s echo_suppress={es}s ch={rch}".format(
            rh=_str_env("XAIR_REAPER_HOST", "127.0.0.1"),
            rp=_int_env("XAIR_REAPER_PORT", 9001),
            lh=_str_env("XAIR_REAPER_LISTEN_HOST", "0.0.0.0"),
            lp=_int_env("XAIR_REAPER_LISTEN_PORT", _int_env("XAIR_REAPER_PORT", 9001)),
            mode="xremote" if _bool_env("XAIR_REAPER_USE_XREMOTE", True) else "poll",
            ka=_float_env("XAIR_OSC_XREMOTE_INTERVAL_SEC", 8.0),
            poll=_float_env("XAIR_REAPER_POLL_INTERVAL_SEC", 0.20),
            es=_float_env("XAIR_REAPER_ECHO_SUPPRESS_SEC", 0.25),
            rch=_int_env("XAIR_REAPER_CHANNELS", cfg["channels"]),
        )
    )


@cli.command("discover")
@click.option("--wait", "wait_s", default=None, type=float, help="Seconds to listen (default XAIR_DISCOVERY_WAIT_SEC)")
def discover_cmd(wait_s: Optional[float]) -> None:
    """Probe the LAN for X AIR mixers (mDNS/SSDP//xinfo) and bridge peers (beacons)."""
    from ..discovery.service import DiscoveryService, default_discovery_path

    _setup_logging()
    cfg = _merged_config()
    root = _project_root()
    timeout = float(wait_s) if wait_s is not None else float(cfg.get("discovery_wait_s") or 2.5)
    disc = DiscoveryService.from_env(
        role="client",
        audio_port=int(cfg["udp_port"]),
        state_path=default_discovery_path(root),
        enabled=True,
    )
    disc.start()
    try:
        time.sleep(max(0.2, timeout))
        snap = disc.snapshot()
    finally:
        disc.stop()
    click.echo(
        "discovery={on} health={h} last_discovery_ts={ts}".format(
            on="on" if snap.get("discovery_enabled") else "off",
            h=snap.get("discovery_health") or "—",
            ts=snap.get("last_discovery_ts") if snap.get("last_discovery_ts") is not None else "—",
        )
    )
    devices = snap.get("discovered_devices") or []
    if not devices:
        click.echo("no devices (set XAIR_OSC_HOST / XAIR_PEER_HOST manually, or check LAN multicast)")
        return
    click.echo(f"{'kind':<7} {'name':<16} {'model':<8} {'ip':<16} firmware")
    for d in devices:
        if not isinstance(d, dict):
            continue
        click.echo(
            f"{str(d.get('kind') or '—'):<7} {str(d.get('name') or '—')[:16]:<16} "
            f"{str(d.get('model') or '—')[:8]:<8} {str(d.get('ip') or '—'):<16} "
            f"{d.get('firmware') or '—'}"
        )


def _xair_env_bridge(host: Optional[str], port: Optional[int]):
    _ensure_env_loaded()
    from ..xair_control.osc_bridge import XAirOSCBridge, XAirOSCError

    br = XAirOSCBridge.from_env(host=host, port=port)
    return br, XAirOSCError


def _sync_client_inactive_message(path: Optional[Path] = None) -> None:
    click.echo(
        "SyncManager no está activo. Activa XAIR_SYNC_ON_CLIENT=true en .env.",
        err=False,
    )
    if path is not None:
        click.echo(f"(Sin snapshot válido reciente en {path})", err=False)


@cli.command("sync-status")
@click.option("--host", default=None, metavar="ADDR", hidden=True)
@click.option("--port", default=None, type=int, hidden=True)
def sync_status_cmd(host: Optional[str], port: Optional[int]) -> None:
    """Resumen usando el cliente en ejecución (snapshot en disco si XAIR_SYNC_ON_CLIENT está activo)."""
    del host, port
    _ensure_env_loaded()
    root = _project_root()
    snap, spath = _read_sync_snapshot(root)
    if snap is None or not _sync_snapshot_valid(snap, spath):
        _sync_client_inactive_message(spath)
        return

    rep = snap.get("report")
    if not isinstance(rep, dict):
        _sync_client_inactive_message(spath)
        return

    rep_iv = snap.get("report_interval_sec") or _float_env("XAIR_SYNC_REPORT_INTERVAL_SEC", 5.0)
    nch = int(snap.get("channels") or len(rep.get("channels", [])) or 18)
    tol = rep.get("level_tolerance_db")

    rows = rep["channels"]
    gdr = rep.get("global_drift_ns")
    n_drift = sum(1 for r in rows if r.get("drift_ns") is not None)
    n_ok = sum(1 for r in rows if r.get("level_ok") is True)
    n_ko = sum(
        1 for r in rows if r.get("delta_level") is not None and not r.get("level_ok")
    )
    n_lvl_na = sum(
        1 for r in rows if r.get("rms_audio") is None or r.get("meter_osc") is None
    )

    drift_s = "—" if gdr is None else str(gdr)
    rx = rep.get("receiver_metrics") if isinstance(rep.get("receiver_metrics"), dict) else {}
    rx_line = ""
    if rx:
        rx_line = (
            f"RX health={rx.get('rx_health', '—')}/{rx.get('rx_health_reason', '—')}  "
            f"jitter_ema={rx.get('jitter_ms_ema', '—')}ms peak={rx.get('jitter_ms_peak', '—')}ms  "
            f"target={rx.get('jitter_packets_target', '—')}  "
            f"underrun={rx.get('underruns', '—')}  "
            f"loss_ppm={rx.get('loss_window_ppm', '—')}  "
            f"q={rx.get('queue_packets', '—')}/{rx.get('queue_capacity', '—')}\n"
        )
        rx_line += (
            f"FEC {('on' if rx.get('fec_enabled') else 'off')}"
            f"/N={rx.get('fec_group', '—')}  "
            f"health={rx.get('fec_health', '—')}  "
            f"recovered={rx.get('fec_recovered', '—')}  "
            f"unrecoverable={rx.get('fec_unrecoverable', '—')}  "
            f"rec_ppm={rx.get('fec_recovered_ppm', '—')}  "
            f"unrec_ppm={rx.get('fec_unrecoverable_ppm', '—')}\n"
        )
        rx_line += (
            f"Opus {('on' if rx.get('opus_enabled') else 'off')}"
            f" bitrate={rx.get('opus_bitrate', '—')}  "
            f"frame={rx.get('opus_frame', '—')}ms  "
            f"health={rx.get('opus_health', '—')}  "
            f"decode_errors={rx.get('opus_decode_errors', '—')}\n"
        )
    ret = rep.get("return_path") if isinstance(rep.get("return_path"), dict) else {}
    if ret:
        rx_line = rx_line + (
            f"Return {('on' if ret.get('enabled') else 'off')}  "
            f"osc={('on' if ret.get('osc_enabled') else 'off')}  "
            f"audio={('on' if ret.get('audio_enabled') else 'off')}  "
            f"health={ret.get('return_health', '—')}  "
            f"safety={ret.get('return_safety', '—')}  "
            f"last_writeback_ns={ret.get('last_writeback_ns', '—')}\n"
        )
    disc = rep.get("discovery") if isinstance(rep.get("discovery"), dict) else {}
    if disc:
        devices = disc.get("discovered_devices") or []
        rx_line = rx_line + (
            f"Discovery {('on' if disc.get('discovery_enabled') else 'off')}  "
            f"health={disc.get('discovery_health', '—')}  "
            f"devices={len(devices)}  "
            f"last_discovery_ts={disc.get('last_discovery_ts', '—')}\n"
        )
        for d in devices:
            if not isinstance(d, dict):
                continue
            rx_line += (
                f"  {d.get('kind') or 'device'}  {d.get('name') or '—'}  "
                f"model={d.get('model') or '—'}  ip={d.get('ip') or '—'}  "
                f"fw={d.get('firmware') or '—'}\n"
            )
    dsp = rep.get("dsp") if isinstance(rep.get("dsp"), dict) else {}
    if dsp:
        chg = dsp.get("last_change") if isinstance(dsp.get("last_change"), dict) else {}
        chg_s = "—"
        if chg:
            chg_s = (
                f"{chg.get('section') or '—'} {chg.get('path') or '—'} "
                f"={chg.get('value')} @{chg.get('ts_ns') or '—'}"
            )
        rx_line = rx_line + (
            f"EQ {('on' if dsp.get('eq_enabled') else 'off')} health={dsp.get('eq_health', '—')}  "
            f"last_eq_write_ns={dsp.get('last_eq_write_ns', '—')}\n"
            f"DYN {('on' if dsp.get('dyn_enabled') else 'off')} health={dsp.get('dyn_health', '—')}  "
            f"last_dyn_write_ns={dsp.get('last_dyn_write_ns', '—')}\n"
            f"FX {('on' if dsp.get('fx_enabled') else 'off')} health={dsp.get('fx_health', '—')}  "
            f"last_fx_write_ns={dsp.get('last_fx_write_ns', '—')}\n"
            f"DSP last={chg_s}\n"
        )
    recb = rep.get("record") if isinstance(rep.get("record"), dict) else {}
    from ..dashboard.reader import (
        load_package_sidecar,
        load_record_sidecar,
        load_release_sidecar,
        load_service_sidecar,
        load_test_sidecar,
    )

    side_rec = load_record_sidecar(spath)
    if side_rec:
        recb = dict(recb)
        recb.update(side_rec)
    if recb:
        files = recb.get("record_files") if isinstance(recb.get("record_files"), list) else []
        rx_line = rx_line + (
            f"Record {('on' if recb.get('recording') else 'off')}  "
            f"enabled={('on' if recb.get('record_enabled') else 'off')}  "
            f"health={recb.get('record_health', '—')}  "
            f"files={len(files)}  path={recb.get('record_path') or '—'}  "
            f"last_ts={recb.get('last_record_ts', '—')}\n"
        )
    testb = rep.get("test") if isinstance(rep.get("test"), dict) else {}
    servb = rep.get("service") if isinstance(rep.get("service"), dict) else {}
    pkgb = rep.get("package") if isinstance(rep.get("package"), dict) else {}
    relb = rep.get("release") if isinstance(rep.get("release"), dict) else {}

    side_test = load_test_sidecar(spath)
    if side_test:
        testb = dict(testb)
        testb.update(side_test)
    if testb:
        rx_line = rx_line + (
            f"Tests health={testb.get('test_health', '—')}  "
            f"run={testb.get('tests_run', '—')}  "
            f"fail={testb.get('tests_failed', '—')}  "
            f"err={testb.get('tests_errors', '—')}  "
            f"skip={testb.get('tests_skipped', '—')}  "
            f"last_test_run={testb.get('last_test_run', '—')}\n"
        )
    side_svc = load_service_sidecar(spath)
    if side_svc:
        servb = dict(servb)
        servb.update(side_svc)
    if servb:
        rx_line = rx_line + (
            f"Service enabled={('yes' if servb.get('service_enabled') else 'no')}  "
            f"last_service_action={servb.get('last_service_action', '—')}  "
            f"scope={servb.get('service_scope', '—')}  "
            f"role={servb.get('service_role', '—')}\n"
        )
    side_pkg = load_package_sidecar(spath)
    if side_pkg:
        pkgb = dict(pkgb)
        pkgb.update(side_pkg)
    if pkgb:
        rx_line = rx_line + (
            f"Package version={pkgb.get('package_version', '—')}  "
            f"package_build_ts={pkgb.get('package_build_ts', '—')}\n"
        )
    side_rel = load_release_sidecar(spath)
    if side_rel:
        relb = dict(relb)
        relb.update(side_rel)
    if relb:
        rx_line = rx_line + (
            f"Release version={relb.get('release_version', '—')}  "
            f"release_build_ts={relb.get('release_build_ts', '—')}\n"
        )
    from ..sync.integration import integration_snapshot

    intb = rep.get("integration") if isinstance(rep.get("integration"), dict) else {}
    last_its = intb.get("last_integration_ts")
    if last_its is None:
        last_its = rep.get("last_integration_ts")
    computed = integration_snapshot(
        active=True,
        last_ts=float(last_its) if isinstance(last_its, (int, float)) else None,
        metrics=rx,
        return_path=ret,
        discovery=disc,
        dsp=dsp,
        record=recb,
    )
    rx_line = rx_line + (
        f"Integration health={computed.get('integration_health', '—')}  "
        f"last_integration_ts={computed.get('last_integration_ts', '—')}\n"
    )
    click.echo(
        f"(fuente: snapshot del cliente • {spath})\n"
        f"{rx_line}"
        f"drift global medio (ns): {drift_s}\n"
        f"canales con drift audio↔OSC: {n_drift}/{nch}\n"
        f"niveles: OK={n_ok}  fuera tolerancia={n_ko}  sin datos RMS/meter={n_lvl_na}  (Δ≤{tol} dB)\n"
        f"canales en snapshot: {nch} • XAIR_SYNC_REPORT_INTERVAL_SEC(ref)={rep_iv}\n"
        "Tabla por canal: PYTHONPATH=. python3 -m src.cli sync-detail\n"
    )


@cli.command("sync-detail")
@click.option("--host", default=None, metavar="ADDR", hidden=True)
@click.option("--port", default=None, type=int, hidden=True)
def sync_detail_cmd(host: Optional[str], port: Optional[int]) -> None:
    """Tabla por nivel/drift usando snapshot del proceso xair_network_bridge_client."""
    del host, port
    _ensure_env_loaded()
    root = _project_root()
    snap, spath = _read_sync_snapshot(root)
    if snap is None or not _sync_snapshot_valid(snap, spath):
        _sync_client_inactive_message(spath)
        return

    rep = snap.get("report")
    if not isinstance(rep, dict):
        _sync_client_inactive_message(spath)
        return

    rows = rep["channels"]
    tol = rep.get("level_tolerance_db")
    nch = len(rows)
    click.echo(f"(snapshot: {spath})\n")

    hdr = (
        f"{'ch':>3}  {'nombre':<16} {'fader':>8} {'gain':>8} {'rms_lin':>10} {'mOsc':>7} "
        f"{'ddB':>7} {'drift_ns':>12} {'eq_ns':>12} {'dyn_ns':>12} {'fx_ns':>12} {'ok':>3}"
    )
    click.echo(hdr)
    click.echo("-" * min(len(hdr), 120) + f"  tol_ddB={tol}  ch={nch}")
    for r in rows:
        nm = (r.get("name") or "")[:16]
        fd = r.get("fader")
        gn = r.get("gain")
        rms = r.get("rms_audio")
        mtr = r.get("meter_osc")
        dd = r.get("delta_level")
        dns = r.get("drift_ns")
        ok = r.get("level_ok")
        fd_s = "{:>8}".format("—" if fd is None else f"{fd:.3f}")
        gn_s = "{:>8}".format("—" if gn is None else f"{gn:.2f}")
        rms_s = "{:>10}".format("—" if rms is None else f"{rms:.5f}")
        mtr_s = "{:>7}".format("—" if mtr is None else str(mtr))
        dd_s = "{:>7}".format("—" if dd is None else f"{dd:.2f}")
        dns_s = "           —" if dns is None else f"{dns:12d}"
        eq_s = "           —" if r.get("last_eq_write_ns") is None else f"{int(r['last_eq_write_ns']):12d}"
        dyn_s = "           —" if r.get("last_dyn_write_ns") is None else f"{int(r['last_dyn_write_ns']):12d}"
        fx_s = "           —" if r.get("last_fx_write_ns") is None else f"{int(r['last_fx_write_ns']):12d}"
        ok_s = "—" if ok is None else ("si" if ok else "no")
        click.echo(
            f"{r['ch']:3d}  {nm:<16} {fd_s} {gn_s} {rms_s} {mtr_s} {dd_s} {dns_s} {eq_s} {dyn_s} {fx_s} {ok_s:>3}"
        )


@cli.command("dashboard")
@click.option(
    "--http",
    "use_http",
    is_flag=True,
    help="Servidor HTTP local en vez de TUI (solo lectura).",
)
@click.option("--bind", default="127.0.0.1", show_default=True, help="Bind HTTP (solo --http).")
@click.option("--port", default=8765, type=int, show_default=True, help="Puerto HTTP (solo --http).")
@click.option(
    "--interval",
    default=None,
    type=float,
    help="Refresco TUI/HTTP en segundos (defecto 0.25 o XAIR_DASHBOARD_INTERVAL_SEC).",
)
def dashboard_cmd(use_http: bool, bind: str, port: int, interval: Optional[float]) -> None:
    """Medidores RMS vs OSC y salud UDP, desde el snapshot del cliente.

    Requiere xair_network_bridge_client con XAIR_SYNC_ON_CLIENT=true. No entra en el audio UDP.
    HTTP: botones Record start/stop. TUI: tecla r.
    """
    _ensure_env_loaded()
    root = _project_root()
    path = _sync_state_path(root)
    iv = float(interval) if interval is not None else _float_env("XAIR_DASHBOARD_INTERVAL_SEC", 0.25)
    if use_http:
        from ..dashboard.http import run_http

        click.echo(f"Dashboard HTTP http://{bind}:{port}/  (Ctrl+C para salir)")
        click.echo(f"snapshot: {path}")
        try:
            run_http(path, bind=bind, port=int(port), interval_s=iv)
        except KeyboardInterrupt:
            pass
        except OSError as exc:
            raise click.ClickException(str(exc)) from exc
        return
    from ..dashboard.tui import run_tui

    try:
        run_tui(path, interval_s=iv)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command("record-start")
@click.option("--path", "record_path", default=None, type=click.Path(path_type=Path))
def record_start_cmd(record_path: Optional[Path]) -> None:
    """Pide al cliente que empiece a grabar WAV por canal (sidecar)."""
    _ensure_env_loaded()
    from ..recorder.sidecar import default_record_cmd_path, write_record_command

    root = _project_root()
    dest = str(record_path.expanduser()) if record_path is not None else None
    cmd = default_record_cmd_path(_sync_state_path(root))
    write_record_command(cmd, "start", record_path=dest)
    click.echo(f"record start → {cmd}" + (f"  dir={dest}" if dest else ""))


@cli.command("record-stop")
def record_stop_cmd() -> None:
    """Pide al cliente que cierre los WAV de la sesión actual."""
    _ensure_env_loaded()
    from ..recorder.sidecar import default_record_cmd_path, write_record_command

    write_record_command(default_record_cmd_path(_sync_state_path(_project_root())), "stop")
    click.echo("record stop")


@cli.command("record-status")
def record_status_cmd() -> None:
    """Muestra el estado del sidecar WAV (snapshot o .xair_record.json)."""
    _ensure_env_loaded()
    from ..dashboard.reader import load_record_sidecar
    from ..recorder.sidecar import default_record_state_path

    root = _project_root()
    snap, spath = _read_sync_snapshot(root)
    rec = {}
    if snap and isinstance(snap.get("report"), dict) and isinstance(snap["report"].get("record"), dict):
        rec = dict(snap["report"]["record"])
    side = load_record_sidecar(spath)
    if not side:
        p = default_record_state_path(root)
        if p.is_file():
            try:
                side = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                side = {}
    if side:
        rec.update(side)
    if not rec:
        click.echo("Recorder inactivo. Arranca xair_network_bridge_client (sidecar WAV).")
        return
    files = rec.get("record_files") if isinstance(rec.get("record_files"), list) else []
    click.echo(
        f"enabled={('on' if rec.get('record_enabled') else 'off')}  "
        f"recording={('on' if rec.get('recording') else 'off')}  "
        f"health={rec.get('record_health', '—')}  "
        f"path={rec.get('record_path') or '—'}  "
        f"files={len(files)}  last_ts={rec.get('last_record_ts', '—')}"
    )
    for f in files[:18]:
        click.echo(f"  {f}")


@cli.command("test")
@click.option("-q", "--quiet", is_flag=True, help="Minimal unittest output.")
@click.option("-v", "--verbose", is_flag=True, help="Verbose unittest output.")
def test_cmd(quiet: bool, verbose: bool) -> None:
    """Run the offline transport/jitter/FEC/Opus/control suite (no mixer)."""
    _ensure_env_loaded()
    root = _project_root()
    os.chdir(root)
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    verbosity = 0 if quiet else (2 if verbose else 1)
    from ..testing.suite import format_summary, persist_test_snapshot, run_offline_suite

    _result, snap = run_offline_suite(stream=sys.stdout, verbosity=verbosity)
    persist_test_snapshot(snap, project_root=root, sync_state_path=_sync_state_path(root))
    click.echo(format_summary(snap))
    if int(snap.get("tests_failed") or 0) or int(snap.get("tests_errors") or 0):
        raise SystemExit(1)


def _service_role_default() -> str:
    raw = _str_env("XAIR_SERVICE_ROLE", "client").strip().lower()
    return raw if raw in ("server", "client", "both") else "client"


@cli.command("install-service")
@click.option(
    "--role",
    type=click.Choice(["server", "client", "both"]),
    default=None,
    help="Which unit(s). Default XAIR_SERVICE_ROLE or client.",
)
@click.option("--system", "as_system", is_flag=True, help="Install system units (/etc/systemd/system). Needs root.")
@click.option("--no-start", is_flag=True, help="Enable for boot but do not start now.")
@click.option("--linger", is_flag=True, help="User scope: loginctl enable-linger so units start at boot.")
@click.option("--dry-run", is_flag=True, help="Print unit files; do not write or call systemctl.")
def install_service_cmd(
    role: Optional[str],
    as_system: bool,
    no_start: bool,
    linger: bool,
    dry_run: bool,
) -> None:
    """Install systemd unit(s) for xair_network_bridge_server / xair_network_bridge_client (Linux)."""
    import getpass

    from ..service.systemd import expand_roles, install_service, render_unit

    _ensure_env_loaded()
    root = _project_root()
    chosen = role or _service_role_default()
    scope = "system" if as_system else "user"
    if dry_run:
        for r in expand_roles(chosen):
            click.echo(render_unit(r, project_root=root, scope=scope, extra_user=getpass.getuser() if as_system else None))
        return
    extra_user = getpass.getuser() if as_system else None
    snap = install_service(
        project_root=root,
        role=chosen,
        scope=scope,
        apply=True,
        start=not no_start,
        linger=linger,
        extra_user=extra_user,
        sync_state_path=_sync_state_path(root),
    )
    click.echo(
        "service_enabled={en}  last_service_action={act}  scope={sc}  role={ro}  dir={d}".format(
            en="yes" if snap.get("service_enabled") else "no",
            act=snap.get("last_service_action") or "—",
            sc=snap.get("service_scope") or "—",
            ro=snap.get("service_role") or "—",
            d=snap.get("unit_dir") or "—",
        )
    )
    for n in snap.get("notes") or []:
        if n:
            click.echo(str(n), err=True)
    if scope == "user" and not linger:
        click.echo("Boot without login: python -m src.cli install-service --linger  (or loginctl enable-linger)")


@cli.command("remove-service")
@click.option(
    "--role",
    type=click.Choice(["server", "client", "both"]),
    default=None,
    help="Which unit(s). Default XAIR_SERVICE_ROLE or client.",
)
@click.option("--system", "as_system", is_flag=True, help="Remove system units.")
@click.option("--dry-run", is_flag=True, help="Show what would be removed.")
def remove_service_cmd(role: Optional[str], as_system: bool, dry_run: bool) -> None:
    """Disable and remove systemd unit(s) installed by install-service."""
    from ..service.systemd import UNIT_FILES, expand_roles, remove_service, unit_dir_for_scope

    _ensure_env_loaded()
    root = _project_root()
    chosen = role or _service_role_default()
    scope = "system" if as_system else "user"
    dest = unit_dir_for_scope(scope)
    units = [UNIT_FILES[r] for r in expand_roles(chosen)]
    if dry_run:
        click.echo(f"would stop/disable/remove {', '.join(units)} from {dest}")
        return
    snap = remove_service(
        project_root=root,
        role=chosen,
        scope=scope,
        apply=True,
        sync_state_path=_sync_state_path(root),
    )
    click.echo(
        "service_enabled={en}  last_service_action={act}  units={u}".format(
            en="yes" if snap.get("service_enabled") else "no",
            act=snap.get("last_service_action") or "—",
            u=",".join(snap.get("units") or []),
        )
    )
    for n in snap.get("notes") or []:
        if n:
            click.echo(str(n), err=True)


@cli.command("package-deb")
@click.option("--out", "out_dir", default=None, type=click.Path(path_type=Path), help="Output directory (default dist/).")
def package_deb_cmd(out_dir: Optional[Path]) -> None:
    """Build a Debian/Ubuntu .deb (units, Depends, /usr/bin/xair-network-bridge). No mixer."""
    from ..package.builder import build_deb

    _ensure_env_loaded()
    root = _project_root()
    dest = out_dir.expanduser() if out_dir is not None else root / "dist"
    path = build_deb(root, dest_dir=dest, sync_state_path=_sync_state_path(root))
    click.echo(str(path))


@cli.command("package-tar")
@click.option("--out", "out_dir", default=None, type=click.Path(path_type=Path), help="Output directory (default dist/).")
def package_tar_cmd(out_dir: Optional[Path]) -> None:
    """Build a generic Linux source tarball with CLI wrapper and systemd templates."""
    from ..package.builder import build_tarball

    _ensure_env_loaded()
    root = _project_root()
    dest = out_dir.expanduser() if out_dir is not None else root / "dist"
    path = build_tarball(root, dest_dir=dest, sync_state_path=_sync_state_path(root))
    click.echo(str(path))


@cli.command("build-release")
@click.option("--out", "out_dir", default=None, type=click.Path(path_type=Path), help="Output directory (default dist/).")
def build_release_cmd(out_dir: Optional[Path]) -> None:
    """Optimized, reproducible release: .deb, tarball, systemd units. No mixer."""
    from ..release.pipeline import build_release

    _ensure_env_loaded()
    root = _project_root()
    dest = out_dir.expanduser() if out_dir is not None else root / "dist"
    snap = build_release(root, dest_dir=dest, sync_state_path=_sync_state_path(root))
    arts = snap.get("artifacts") if isinstance(snap.get("artifacts"), dict) else {}
    click.echo(f"release_version={snap.get('release_version')}")
    click.echo(f"release_build_ts={snap.get('release_build_ts')}")
    for key in ("deb", "tarball", "stamp", "sha256sums", "launcher"):
        val = arts.get(key)
        if val:
            click.echo(str(val))
    for unit in arts.get("systemd") or []:
        click.echo(str(unit))


@cli.command("start-reaper-sync")
@click.option("--reaper-host", default=None, metavar="ADDR", help="Anula XAIR_REAPER_HOST")
@click.option("--reaper-port", default=None, type=int, help="Anula XAIR_REAPER_PORT")
@click.option("--listen-port", default=None, type=int, help="Anula XAIR_REAPER_LISTEN_PORT")
@click.option("--host", default=None, metavar="ADDR", help="Anula XAIR_OSC_HOST")
@click.option("--port", default=None, type=int, help="Anula XAIR_OSC_PORT")
def start_reaper_sync_cmd(
    reaper_host: Optional[str],
    reaper_port: Optional[int],
    listen_port: Optional[int],
    host: Optional[str],
    port: Optional[int],
) -> None:
    """Sincroniza faders/mute/pan/nombres/sends/buses/LR entre X18 y REAPER (OSC).

    Proceso aparte de xair_network_bridge_server / xair_network_bridge_client: no entra en el camino UDP de audio.
    """
    from ..osc.x18_reaper_sync import ReaperSyncConfig, run_forever
    from ..xair_control.osc_bridge import XAirOSCError

    _setup_logging()
    _ensure_env_loaded()
    try:
        cfg = ReaperSyncConfig.from_env(
            reaper_host=reaper_host,
            reaper_port=reaper_port,
            listen_port=listen_port,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    try:
        run_forever(config=cfg, osc_host=host, osc_port=port)
    except XAirOSCError as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command("osc-launcher")
@click.option("--host", default=None, metavar="ADDR", help="IP de la X18 (XAIR_OSC_IP / XAIR_OSC_HOST)")
@click.option("--port", default=None, type=int, help="Puerto OSC de la X18 (defecto 10024)")
@click.option("--reaper-host", default=None, metavar="ADDR", help="Anula XAIR_REAPER_HOST")
@click.option("--reaper-port", default=None, type=int, help="Puerto OSC de REAPER (8000 o el configurado)")
@click.option("--non-interactive", is_flag=True, help="No pide IP; falla si no hay host")
@click.option("--no-start-client", is_flag=True, help="No arranca xair_network_bridge_client si falta")
@click.option("--check", is_flag=True, help="Solo verifica; no entra en start-reaper-sync")
def osc_launcher_cmd(
    host: Optional[str],
    port: Optional[int],
    reaper_host: Optional[str],
    reaper_port: Optional[int],
    non_interactive: bool,
    no_start_client: bool,
    check: bool,
) -> None:
    """Activa OSC X18 ↔ X-Air Network Bridge ↔ REAPER (un comando). No toca el audio UDP."""
    from ..osc.launcher import OscLauncherError, launch_osc
    from ..xair_control.osc_bridge import XAirOSCError

    _setup_logging()
    _ensure_env_loaded()
    root = _project_root()

    def _prompt(msg: str) -> str:
        return str(click.prompt(msg))

    try:
        launch_osc(
            project_root=root,
            host=host,
            port=port,
            reaper_host=reaper_host,
            reaper_port=reaper_port,
            non_interactive=non_interactive or not sys.stdin.isatty(),
            ensure_client=not no_start_client,
            start_sync=not check,
            echo=click.echo,
            prompt_fn=None if (non_interactive or not sys.stdin.isatty()) else _prompt,
        )
    except OscLauncherError as exc:
        raise click.ClickException(str(exc)) from exc
    except XAirOSCError as exc:
        raise click.ClickException(str(exc)) from exc
    except KeyboardInterrupt:
        click.echo("OSC launcher detenido.", err=True)


@cli.command("xair-network-status")
@click.option("--host", default=None, metavar="ADDR", help="Anula XAIR_OSC_HOST")
@click.option("--port", default=None, type=int, help="Anula XAIR_OSC_PORT")
def xair_status(host: Optional[str], port: Optional[int]) -> None:
    """Solicitud /status a la mesa OSC (¿active/standby?)."""
    bridge, OSCEx = _xair_env_bridge(host, port)
    try:
        with bridge as br:
            first, tup = br.ping()
            click.echo(f"{br.host}:{br.port}  /status  → {first!r}")
            click.echo(f"argumentos completos: {tup}")
    except OSCEx as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command("xair-network-get-fader")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_get_fader(channel: int, host: Optional[str], port: Optional[int]) -> None:
    bridge, OSCEx = _xair_env_bridge(host, port)
    try:
        with bridge as br:
            click.echo(br.get_fader(channel))
    except OSCEx as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command("xair-network-set-fader")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.option("--value", required=True, type=float, help="Normalizado típico 0.0 … 1.0")
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_set_fader(channel: int, value: float, host: Optional[str], port: Optional[int]) -> None:
    bridge, OSCEx = _xair_env_bridge(host, port)
    try:
        with bridge as br:
            echoed = br.set_fader(channel, float(value))
            click.echo(echoed)
    except OSCEx as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command("xair-network-get-gain")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_get_gain(channel: int, host: Optional[str], port: Optional[int]) -> None:
    bridge, OSCEx = _xair_env_bridge(host, port)
    try:
        with bridge as br:
            click.echo(br.get_gain(channel))
    except OSCEx as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command("xair-network-set-gain")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.option("--value", required=True, type=float)
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_set_gain(channel: int, value: float, host: Optional[str], port: Optional[int]) -> None:
    bridge, OSCEx = _xair_env_bridge(host, port)
    try:
        with bridge as br:
            click.echo(br.set_gain(channel, float(value)))
    except OSCEx as exc:
        raise click.ClickException(str(exc)) from exc


def _xair_call(host: Optional[str], port: Optional[int], fn) -> None:
    """One-shot mixer OSC: open, run ``fn(bridge)``, print the result.

    Control plane only — does not touch USB/UDP audio.
    """
    bridge, OSCEx = _xair_env_bridge(host, port)
    try:
        with bridge as br:
            result = fn(br)
            if result is not None:
                if isinstance(result, (dict, list)):
                    click.echo(json.dumps(result, ensure_ascii=False))
                else:
                    click.echo(result)
    except OSCEx as exc:
        raise click.ClickException(str(exc)) from exc


def _muted_flag_from_mix_on(mix_on: bool) -> int:
    """CLI mute flag: 1 = muted. ``XAirOSCBridge.get_mute`` is ``/mix/on`` (1 = open)."""
    return 0 if mix_on else 1


def _mix_on_from_muted_flag(muted: int) -> bool:
    """Invert CLI mute flag into the bool expected by ``set_mute`` (mix/on)."""
    return muted == 0


@cli.command("xair-get-mute")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_get_mute(channel: int, host: Optional[str], port: Optional[int]) -> None:
    """Lee mute del canal: 1 = muteado, 0 = abierto (invierte /ch/XX/mix/on)."""

    def _run(br) -> int:
        return _muted_flag_from_mix_on(br.get_mute(channel))

    _xair_call(host, port, _run)


@cli.command("xair-set-mute")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.option(
    "--value",
    required=True,
    type=click.IntRange(0, 1),
    help="1 = muteado, 0 = abierto (se traduce a /mix/on invertido)",
)
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_set_mute(
    channel: int, value: int, host: Optional[str], port: Optional[int]
) -> None:
    """Escribe mute del canal. 1 = muteado, 0 = abierto."""

    def _run(br) -> int:
        echoed_on = br.set_mute(channel, _mix_on_from_muted_flag(int(value)))
        return _muted_flag_from_mix_on(echoed_on)

    _xair_call(host, port, _run)


@cli.command("xair-get-pan")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_get_pan(channel: int, host: Optional[str], port: Optional[int]) -> None:
    """Lee pan del canal (/ch/XX/mix/pan, típico 0.0 … 1.0)."""
    _xair_call(host, port, lambda br: br.get_pan(channel))


@cli.command("xair-set-pan")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.option("--value", required=True, type=float, help="Normalizado típico 0.0 … 1.0")
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_set_pan(channel: int, value: float, host: Optional[str], port: Optional[int]) -> None:
    """Escribe pan del canal."""
    _xair_call(host, port, lambda br: br.set_pan(channel, float(value)))


@cli.command("xair-get-send")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.argument("send", type=click.IntRange(1, 16), metavar="SEND")
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_get_send(channel: int, send: int, host: Optional[str], port: Optional[int]) -> None:
    """Lee nivel de send SEND del canal CH (/ch/XX/mix/YY)."""
    _xair_call(host, port, lambda br: br.get_send(channel, send))


@cli.command("xair-set-send")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.argument("send", type=click.IntRange(1, 16), metavar="SEND")
@click.option("--value", required=True, type=float, help="Normalizado típico 0.0 … 1.0")
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_set_send(
    channel: int, send: int, value: float, host: Optional[str], port: Optional[int]
) -> None:
    """Escribe nivel de send SEND del canal CH."""
    _xair_call(host, port, lambda br: br.set_send(channel, send, float(value)))


@cli.command("xair-get-bus")
@click.argument("bus", type=click.IntRange(1, 16), metavar="BUS")
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_get_bus(bus: int, host: Optional[str], port: Optional[int]) -> None:
    """Lee fader del bus (/bus/XX/mix/fader)."""
    _xair_call(host, port, lambda br: br.get_bus_fader(bus))


@cli.command("xair-set-bus")
@click.argument("bus", type=click.IntRange(1, 16), metavar="BUS")
@click.option("--value", required=True, type=float, help="Normalizado típico 0.0 … 1.0")
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_set_bus(bus: int, value: float, host: Optional[str], port: Optional[int]) -> None:
    """Escribe fader del bus."""
    _xair_call(host, port, lambda br: br.set_bus_fader(bus, float(value)))


@cli.command("xair-get-lr")
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_get_lr(host: Optional[str], port: Optional[int]) -> None:
    """Lee fader del master LR (/lr/mix/fader)."""
    _xair_call(host, port, lambda br: br.get_lr_fader())


@cli.command("xair-set-lr")
@click.option("--value", required=True, type=float, help="Normalizado típico 0.0 … 1.0")
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_set_lr(value: float, host: Optional[str], port: Optional[int]) -> None:
    """Escribe fader del master LR."""
    _xair_call(host, port, lambda br: br.set_lr_fader(float(value)))


@cli.command("xair-get-eq")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.option("--band", type=click.IntRange(1, 4), default=None)
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_get_eq(
    channel: int, band: Optional[int], host: Optional[str], port: Optional[int]
) -> None:
    """Lee EQ de canal (on, HPF, bandas type/g/f/q)."""

    def _fn(br):
        if band is None:
            out = {
                "on": int(br.get_eq_on(channel)),
                "hpf": br.get_hpf(channel),
                "bands": {},
            }
            for b in range(1, 5):
                out["bands"][str(b)] = {
                    leaf: br.get_eq_band(channel, b, leaf)
                    for leaf in ("type", "g", "f", "q")
                }
            return out
        return {
            leaf: br.get_eq_band(channel, band, leaf) for leaf in ("type", "g", "f", "q")
        }

    _xair_call(host, port, _fn)


@cli.command("xair-set-eq")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.option("--on", "eq_on", type=click.IntRange(0, 1), default=None)
@click.option("--hpf", type=float, default=None)
@click.option("--band", type=click.IntRange(1, 4), default=None)
@click.option("--leaf", type=click.Choice(["type", "g", "f", "q"]), default=None)
@click.option("--value", type=float, default=None)
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_set_eq(
    channel: int,
    eq_on: Optional[int],
    hpf: Optional[float],
    band: Optional[int],
    leaf: Optional[str],
    value: Optional[float],
    host: Optional[str],
    port: Optional[int],
) -> None:
    """Escribe EQ (CLI one-shot; el cliente vivo usa el hilo DSP)."""

    def _fn(br):
        wrote = False
        last = None
        if eq_on is not None:
            last = int(br.set_eq_on(channel, bool(eq_on)))
            wrote = True
        if hpf is not None:
            last = br.set_hpf(channel, float(hpf))
            wrote = True
        if band is not None:
            if leaf is None or value is None:
                raise click.ClickException("xair-set-eq --band requiere --leaf y --value")
            last = br.set_eq_band(channel, band, leaf, float(value))
            wrote = True
        elif leaf is not None or value is not None:
            raise click.ClickException("xair-set-eq --leaf/--value requieren --band")
        if not wrote:
            raise click.ClickException("indica --on, --hpf o --band/--leaf/--value")
        return last

    _xair_call(host, port, _fn)


@cli.command("xair-get-gate")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.option(
    "--leaf",
    type=click.Choice(["on", "thr", "range", "attack", "hold", "release"]),
    default=None,
)
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_get_gate(
    channel: int, leaf: Optional[str], host: Optional[str], port: Optional[int]
) -> None:
    """Lee gate de canal."""

    def _fn(br):
        leaves = ("on", "thr", "range", "attack", "hold", "release")
        if leaf is None:
            return {k: br.get_gate(channel, k) for k in leaves}
        return br.get_gate(channel, leaf)

    _xair_call(host, port, _fn)


@cli.command("xair-set-gate")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.option(
    "--leaf",
    required=True,
    type=click.Choice(["on", "thr", "range", "attack", "hold", "release"]),
)
@click.option("--value", required=True, type=float)
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_set_gate(
    channel: int, leaf: str, value: float, host: Optional[str], port: Optional[int]
) -> None:
    """Escribe un parámetro de gate."""
    _xair_call(host, port, lambda br: br.set_gate(channel, leaf, float(value)))


@cli.command("xair-get-dyn")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.option(
    "--leaf",
    type=click.Choice(
        ["on", "thr", "ratio", "knee", "mgain", "attack", "hold", "release", "mix"]
    ),
    default=None,
)
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_get_dyn(
    channel: int, leaf: Optional[str], host: Optional[str], port: Optional[int]
) -> None:
    """Lee compresor de canal (/ch/XX/dyn/...)."""

    def _fn(br):
        leaves = ("on", "thr", "ratio", "knee", "mgain", "attack", "hold", "release", "mix")
        if leaf is None:
            return {k: br.get_dyn(channel, k) for k in leaves}
        return br.get_dyn(channel, leaf)

    _xair_call(host, port, _fn)


@cli.command("xair-set-dyn")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.option(
    "--leaf",
    required=True,
    type=click.Choice(
        ["on", "thr", "ratio", "knee", "mgain", "attack", "hold", "release", "mix"]
    ),
)
@click.option("--value", required=True, type=float)
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_set_dyn(
    channel: int, leaf: str, value: float, host: Optional[str], port: Optional[int]
) -> None:
    """Escribe un parámetro de compresor."""
    _xair_call(host, port, lambda br: br.set_dyn(channel, leaf, float(value)))


@cli.command("xair-get-fx-send")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.argument("fx", type=click.IntRange(1, 4), metavar="FX")
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_get_fx_send(
    channel: int, fx: int, host: Optional[str], port: Optional[int]
) -> None:
    """Lee send de canal a FX 1–4 (/ch/XX/mix/0N)."""
    _xair_call(host, port, lambda br: br.get_fx_send(channel, fx))


@cli.command("xair-set-fx-send")
@click.argument("channel", type=click.IntRange(1, 18), metavar="CH")
@click.argument("fx", type=click.IntRange(1, 4), metavar="FX")
@click.option("--value", required=True, type=float)
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_set_fx_send(
    channel: int, fx: int, value: float, host: Optional[str], port: Optional[int]
) -> None:
    """Escribe send de canal a FX 1–4."""
    _xair_call(host, port, lambda br: br.set_fx_send(channel, fx, float(value)))


@cli.command("xair-get-fx-return")
@click.argument("fx", type=click.IntRange(1, 4), metavar="FX")
@click.option("--leaf", type=click.Choice(["fader", "on", "pan"]), default="fader")
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_get_fx_return(
    fx: int, leaf: str, host: Optional[str], port: Optional[int]
) -> None:
    """Lee FX return (/rtn/0N/mix/...)."""
    _xair_call(host, port, lambda br: br.get_fx_return(fx, leaf))


@cli.command("xair-set-fx-return")
@click.argument("fx", type=click.IntRange(1, 4), metavar="FX")
@click.option("--leaf", type=click.Choice(["fader", "on", "pan"]), default="fader")
@click.option("--value", required=True, type=float)
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_set_fx_return(
    fx: int, leaf: str, value: float, host: Optional[str], port: Optional[int]
) -> None:
    """Escribe FX return."""
    _xair_call(host, port, lambda br: br.set_fx_return(fx, leaf, float(value)))


@cli.command("xair-get-fx")
@click.argument("fx", type=click.IntRange(1, 4), metavar="FX")
@click.option("--par", type=click.IntRange(1, 64), default=None)
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_get_fx(fx: int, par: Optional[int], host: Optional[str], port: Optional[int]) -> None:
    """Lee tipo FX o un parámetro /fx/N/par/NN."""

    def _fn(br):
        if par is None:
            return br.get_fx_type(fx)
        return br.get_fx_par(fx, par)

    _xair_call(host, port, _fn)


@cli.command("xair-set-fx")
@click.argument("fx", type=click.IntRange(1, 4), metavar="FX")
@click.option("--type", "fx_type", type=int, default=None)
@click.option("--par", type=click.IntRange(1, 64), default=None)
@click.option("--value", type=float, default=None)
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
def xair_set_fx(
    fx: int,
    fx_type: Optional[int],
    par: Optional[int],
    value: Optional[float],
    host: Optional[str],
    port: Optional[int],
) -> None:
    """Escribe tipo FX o un parámetro."""

    def _fn(br):
        if fx_type is not None:
            return br.set_fx_type(fx, int(fx_type))
        if par is None or value is None:
            raise click.ClickException("indica --type o --par y --value")
        return br.set_fx_par(fx, par, float(value))

    _xair_call(host, port, _fn)


@cli.command("scene-dump")
@click.argument("path", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
@click.option("--channels", default=None, type=click.IntRange(1, 18))
@click.option("--sends", default=None, type=click.IntRange(0, 16))
@click.option("--buses", default=None, type=click.IntRange(0, 16))
def scene_dump_cmd(
    path: Path,
    host: Optional[str],
    port: Optional[int],
    channels: Optional[int],
    sends: Optional[int],
    buses: Optional[int],
) -> None:
    """Guarda nombres/faders/gain/pan/mute/sends/buses/LR de la mesa en JSON."""
    from ..xair_control.osc_bridge import XAirOSCError
    from ..xair_control.scene import SceneScope, dump_scene, save_scene_file

    _ensure_env_loaded()
    scope = SceneScope.from_env()
    if channels is not None:
        scope.channels = channels
    if sends is not None:
        scope.sends = sends
    if buses is not None:
        scope.buses = buses
    bridge, OSCEx = _xair_env_bridge(host, port)
    try:
        with bridge as br:
            scene = dump_scene(br, scope)
    except OSCEx as exc:
        raise click.ClickException(str(exc)) from exc
    except XAirOSCError as exc:
        raise click.ClickException(str(exc)) from exc
    save_scene_file(path, scene)
    nerr = len(scene.get("errors") or [])
    click.echo(f"scene → {path}  ch={scope.channels} sends={scope.sends} buses={scope.buses}  skips={nerr}")
    if nerr:
        for line in scene["errors"][:12]:
            click.echo(f"  skip: {line}", err=True)
        if nerr > 12:
            click.echo(f"  … {nerr - 12} más", err=True)


@cli.command("scene-recall")
@click.argument("path", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
@click.option("--dry-run", is_flag=True, help="No escribe en la mesa; cuenta qué se aplicaría.")
@click.option(
    "--strict",
    is_flag=True,
    help="Abortar en el primer error OSC (por defecto continúa).",
)
def scene_recall_cmd(
    path: Path,
    host: Optional[str],
    port: Optional[int],
    dry_run: bool,
    strict: bool,
) -> None:
    """Restaura un JSON de scene-dump sobre la mesa (solo control OSC)."""
    from ..xair_control.osc_bridge import XAirOSCError
    from ..xair_control.scene import apply_scene, load_scene_file

    _ensure_env_loaded()
    try:
        scene = load_scene_file(path)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if dry_run:
        report = apply_scene(
            None,
            scene,
            dry_run=True,
            continue_on_error=not strict,
        )
        click.echo(f"dry-run {path}: aplicarían {report.applied} escrituras")
        return
    bridge, OSCEx = _xair_env_bridge(host, port)
    try:
        with bridge as br:
            report = apply_scene(
                br, scene, dry_run=False, continue_on_error=not strict
            )
    except OSCEx as exc:
        raise click.ClickException(str(exc)) from exc
    except XAirOSCError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(
        f"scene ← {path}  applied={report.applied} skipped={report.skipped} errors={len(report.errors)}"
    )
    for line in report.errors[:12]:
        click.echo(f"  skip: {line}", err=True)
    if len(report.errors) > 12:
        click.echo(f"  … {len(report.errors) - 12} más", err=True)


@cli.command("name-ports")
@click.option("--host", default=None, metavar="ADDR")
@click.option("--port", default=None, type=int)
@click.option("--channels", default=None, type=click.IntRange(1, 18))
def name_ports_cmd(host: Optional[str], port: Optional[int], channels: Optional[int]) -> None:
    """Lee nombres OSC y muestra los puertos JACK que usaría xair_network_bridge_client (sin audio)."""
    from ..virtual_device import port_names

    _ensure_env_loaded()
    n = channels if channels is not None else _int_env("XAIR_CHANNELS", 18)
    labels, errors = port_names.fetch_osc_channel_labels(n, host=host, port=port)
    names = port_names.unique_port_names(labels)
    ins, outs = port_names.duplex_io_names(names)
    client = port_names.jack_client_name()
    click.echo(f"cliente JACK/PipeWire: {client}  ({n} ch, duplex {n} in + {n} out)")
    for i, (lab, pin, pout) in enumerate(zip(labels, ins, outs), start=1):
        click.echo(f"  {i:02d}  {lab:20} → {client}:{pin}")
        click.echo(f"      {'':20} → {client}:{pout}")
    if errors:
        click.echo(f"skips OSC: {len(errors)}", err=True)
        for line in errors[:8]:
            click.echo(f"  skip: {line}", err=True)


def main() -> None:
    try:
        cli()
    except click.ClickException as exc:
        click.echo(str(exc), err=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
