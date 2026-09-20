"""
Construcción del `NetworkReceiver` y, opcionalmente, `SyncManager` + refresco OSC periódico.

Con `XAIR_SYNC_ON_CLIENT=true`, el proceso cliente expone el estado por disco para
`sync-status` / `sync-detail` / `dashboard` en otro proceso (véase docs/SYNC.md).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..network_receiver.receiver import NetworkReceiver
from ..sync.sync_manager import SyncManager

log = logging.getLogger(__name__)

_RUNNING_SM: Optional[SyncManager] = None
_RUNNING_RETURN_TX = None
_RUNNING_DISCOVERY = None
_RUNNING_RECORDER = None


def get_running_sync_manager() -> Optional[SyncManager]:
    """Instancia `SyncManager` del cliente en este proceso, o `None` si no está activo."""
    return _RUNNING_SM


def clear_running_sync_manager() -> None:
    global _RUNNING_SM, _RUNNING_RETURN_TX, _RUNNING_DISCOVERY, _RUNNING_RECORDER
    _RUNNING_SM = None
    tx = _RUNNING_RETURN_TX
    _RUNNING_RETURN_TX = None
    if tx is not None:
        try:
            tx.stop()
        except Exception:
            log.debug("return audio stop", exc_info=True)
    disc = _RUNNING_DISCOVERY
    _RUNNING_DISCOVERY = None
    if disc is not None:
        try:
            disc.stop()
        except Exception:
            log.debug("discovery stop", exc_info=True)
    rec = _RUNNING_RECORDER
    _RUNNING_RECORDER = None
    if rec is not None:
        try:
            rec.stop()
        except Exception:
            log.debug("recorder stop", exc_info=True)


def _sync_on_client_enabled() -> bool:
    v = os.getenv("XAIR_SYNC_ON_CLIENT")
    if v is None or not str(v).strip():
        return False
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _default_state_path(project_root: Path) -> Path:
    raw = os.getenv("XAIR_SYNC_STATE_PATH")
    if raw and str(raw).strip():
        return Path(str(raw).strip()).expanduser()
    return project_root / ".xair_sync_state.json"


def build_network_receiver(
    cfg: Dict[str, Any],
    *,
    project_root: Path,
) -> Tuple[NetworkReceiver, Optional[SyncManager]]:
    """
    Crea el receptor UDP. Si `XAIR_SYNC_ON_CLIENT=true`, también `SyncManager`, hook RMS
    y hilo daemon `periodic_refresh` según `XAIR_SYNC_REPORT_INTERVAL_SEC`.
    """
    global _RUNNING_SM, _RUNNING_RETURN_TX, _RUNNING_DISCOVERY, _RUNNING_RECORDER

    channels = int(cfg["channels"])
    return_osc = bool(cfg.get("return_osc"))
    return_audio = bool(cfg.get("return_audio"))
    discovery_on = bool(cfg.get("discovery_enabled"))
    eq_on = bool(cfg.get("eq_enabled"))
    dyn_on = bool(cfg.get("dyn_enabled"))
    fx_on = bool(cfg.get("fx_enabled"))
    record_on = bool(cfg.get("record_enabled"))
    want_sync = (
        _sync_on_client_enabled()
        or return_osc
        or return_audio
        or discovery_on
        or eq_on
        or dyn_on
        or fx_on
        or record_on
    )

    hook = None
    sm: Optional[SyncManager] = None
    osc_host = cfg.get("osc_host")
    from ..discovery.packets import wants_auto_host

    if discovery_on:
        from ..discovery.service import DiscoveryService, default_discovery_path

        disc = DiscoveryService.from_env(
            role="client",
            audio_port=int(cfg["udp_port"]),
            state_path=default_discovery_path(project_root),
            enabled=True,
        )
        disc.start()
        _RUNNING_DISCOVERY = disc
        wait_s = float(cfg.get("discovery_wait_s") or 2.5)
        if wants_auto_host(osc_host if osc_host is not None else os.getenv("XAIR_OSC_HOST")):
            mixer = disc.wait_mixer(wait_s)
            if mixer is not None:
                osc_host = mixer.ip
                log.info(
                    "OSC host via discovery: %s (%s %s fw=%s)",
                    mixer.ip,
                    mixer.name,
                    mixer.model,
                    mixer.firmware or "—",
                )
            else:
                log.warning("discovery: no mixer yet; set XAIR_OSC_HOST or wait for /xinfo")
    else:
        _RUNNING_DISCOVERY = None

    if want_sync:
        state_path = _default_state_path(project_root)
        iv = float(os.getenv("XAIR_SYNC_REPORT_INTERVAL_SEC") or "5.0")
        sm = SyncManager.from_env(
            channels=channels,
            cli_state_path=state_path,
            osc_host=None if wants_auto_host(osc_host) else (str(osc_host).strip() if osc_host else None),
        )
        _RUNNING_SM = sm
        sm.periodic_refresh(iv)
        hook = sm.update_audio_levels
        log.info(
            "SyncManager activo: canales=%s refresco OSC=%ss snapshot=%s",
            channels,
            iv,
            state_path,
        )
        disc_running = _RUNNING_DISCOVERY
        if disc_running is not None:
            sm.set_discovery_provider(disc_running.snapshot)
            sm._discovery.update(disc_running.snapshot())
    else:
        clear_running_sync_manager()

    from ..recorder.sidecar import (
        WavRecorder,
        default_record_cmd_path,
        default_record_path,
        default_record_state_path,
    )
    from ..virtual_device.port_names import fallback_label

    def _record_labels():
        labels = []
        if sm is not None:
            for i in range(1, channels + 1):
                cs = sm.state.channels.get(i)
                labels.append((cs.name if cs is not None else None) or fallback_label(i))
        else:
            labels = [fallback_label(i) for i in range(1, channels + 1)]
        return labels

    rec_dir = cfg.get("record_path")
    rec_anchor = _default_state_path(project_root)
    recorder = WavRecorder(
        channels=channels,
        sample_rate=int(cfg["sample_rate"]),
        output_dir=Path(rec_dir) if rec_dir else default_record_path(project_root),
        cmd_path=default_record_cmd_path(rec_anchor),
        state_path=default_record_state_path(rec_anchor),
        labels_provider=_record_labels,
        auto_start=record_on,
    )
    recorder.start()
    _RUNNING_RECORDER = recorder

    recv = NetworkReceiver(
        bind_host=str(cfg["bind_host"]),
        port=int(cfg["udp_port"]),
        sample_rate=int(cfg["sample_rate"]),
        channels=channels,
        samples_per_packet=int(cfg["samples_per_packet"]),
        jitter_packets=int(cfg["jitter_packets"]),
        output_device_query=cfg["output_device_query"],
        blocksize=max(64, int(cfg.get("audio_blocksize", 256))),
        metrics_log_interval_s=float(cfg["metrics_interval_s"]),
        wasapi_exclusive=bool(cfg["wasapi_exclusive"]),
        audio_levels_hook=hook,
        pcm_tap=recorder.offer,
        jitter_config=cfg.get("jitter_config"),
        fec_enabled=bool(cfg.get("fec_enabled", False)),
        fec_group=int(cfg.get("fec_group", 3)),
        opus_enabled=bool(cfg.get("opus_enabled", False)),
        opus_bitrate=int(cfg.get("opus_bitrate", 128000)),
        opus_frame_ms=float(cfg.get("opus_frame_ms", 20.0)),
        expected_peer_host=str(cfg.get("peer_host") or ""),
    )
    if sm is not None:
        def _metrics_provider() -> Optional[Dict[str, Any]]:
            return recv.health_snapshot()

        sm.set_metrics_provider(_metrics_provider)
        sm._return_path["osc_enabled"] = return_osc
        sm._return_path["audio_enabled"] = return_audio
        sm._return_path["enabled"] = return_osc or return_audio

        if return_osc:
            from ..sync.writeback import MixWriteBack

            wb = MixWriteBack(
                sm,
                channels=channels,
                sends=int(cfg.get("return_sends", 4)),
                buses=int(cfg.get("return_buses", 4)),
                listen_host=str(cfg.get("return_listen_host") or "0.0.0.0"),
                listen_port=int(cfg.get("return_listen_port") or 9002),
                daw_host=str(cfg.get("return_daw_host") or "127.0.0.1"),
                daw_port=int(cfg.get("return_daw_port") or 9001),
            )
            sm.attach_writeback(wb)
            wb.start()

        if eq_on or dyn_on or fx_on:
            from ..xair_control.dsp_control import DspController

            dsp = DspController(
                sm, eq_enabled=eq_on, dyn_enabled=dyn_on, fx_enabled=fx_on
            )
            sm.attach_dsp(dsp)
            sm.set_dsp_provider(dsp.snapshot)
            sm._dsp.update(dsp.snapshot())
            dsp.start()

        sm.attach_recorder(recorder)
        sm.set_record_provider(recorder.snapshot)
        sm._record.update(recorder.snapshot())

    if return_audio:
        from ..sync.return_audio import ReturnAudioSender
        from ..sync.return_safety import ReturnSafety

        fwd = (sm.mean_forward_rms if sm is not None else None)
        tx = ReturnAudioSender(
            host=str(cfg.get("return_peer_host") or cfg["peer_host"]),
            port=int(cfg.get("return_udp_port") or 50001),
            sample_rate=int(cfg["sample_rate"]),
            frame_samples=max(64, int(cfg.get("return_frame_samples") or 480)),
            input_device_query=cfg.get("return_input_device"),
            safety=ReturnSafety(enabled=bool(cfg.get("return_safety", True))),
            forward_rms_provider=fwd,
        )
        tx.start()
        _RUNNING_RETURN_TX = tx
        if sm is not None:
            sm.set_return_provider(tx.snapshot)

    return recv, sm
