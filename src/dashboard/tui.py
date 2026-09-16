"""Curses TUI for the SyncManager snapshot. No audio I/O; ``r`` writes the record command file."""

from __future__ import annotations

import curses
import sys
from pathlib import Path

from .reader import bar, dbfs_to_fraction, load_view
from ..recorder.sidecar import default_record_cmd_path, write_record_command


def run_tui(path: Path, interval_s: float = 0.25) -> None:
    """Block until q / ESC. Requires a real TTY. ``r`` toggles WAV record."""
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        raise RuntimeError(
            "dashboard TUI necesita un TTY. Usa: python -m src.cli dashboard --http"
        )
    interval_s = max(0.1, float(interval_s))
    curses.wrapper(lambda stdscr: _loop(stdscr, path, interval_s))


def _loop(stdscr: "curses._CursesWindow", path: Path, interval_s: float) -> None:
    curses.curs_set(0)
    stdscr.nodelay(False)
    stdscr.timeout(int(interval_s * 1000))
    color = False
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, curses.COLOR_CYAN, -1)
        color = True

    while True:
        view = load_view(path)
        _draw(stdscr, view, color)
        stdscr.refresh()
        key = stdscr.getch()
        if key in (ord("q"), ord("Q"), 27):
            return
        if key in (ord("r"), ord("R")):
            rec = view.record or {}
            action = "stop" if rec.get("recording") else "start"
            try:
                write_record_command(default_record_cmd_path(path), action)
            except Exception:
                pass


def _pair(color: bool, n: int) -> int:
    return curses.color_pair(n) if color else 0


def _draw(stdscr: "curses._CursesWindow", view, color: bool) -> None:
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    if h < 18 or w < 40:
        _add(stdscr, 0, 0, "terminal too small", _pair(color, 3))
        return

    status_pair = {
        "live": _pair(color, 1),
        "stale": _pair(color, 2),
        "inactive": _pair(color, 2),
        "missing": _pair(color, 3),
    }.get(view.status, 0)

    age = "—" if view.age_sec is None else f"{view.age_sec:.1f}s"
    drift = "—" if view.global_drift_ns is None else str(view.global_drift_ns)
    integ = view.integration or {}
    m = view.metrics or {}
    jitter = m.get("jitter_ms_ema")
    peak = m.get("jitter_ms_peak")
    lost = m.get("packets_lost")
    rx = m.get("packets_received")
    late = m.get("packets_late_drop")
    fill = m.get("buffer_fill_percent_ema")
    health = m.get("rx_health") or "—"
    reason = m.get("rx_health_reason") or ""
    target = m.get("jitter_packets_target")
    jmin = m.get("jitter_packets_min")
    jmax = m.get("jitter_packets_max")
    und = m.get("underruns")
    qpk = m.get("queue_packets")
    qcap = m.get("queue_capacity")
    pps = m.get("rx_packets_per_sec")
    lppm = m.get("loss_window_ppm")
    health_pair = {
        "ok": _pair(color, 1),
        "warn": _pair(color, 2),
        "fail": _pair(color, 3),
    }.get(str(health), 0)

    _add(stdscr, 0, 0, "X-AIR Network Bridge  ·  meter / health", _pair(color, 4) | curses.A_BOLD)
    _add(
        stdscr,
        1,
        0,
        f"snapshot {view.status:<8}  age={age}  drift_ns={drift}  "
            f"integ={integ.get('integration_health') or 'off'}  "
            f"last_ts={integ.get('last_integration_ts') if integ.get('last_integration_ts') is not None else '—'}  "
            f"file={view.path.name}",
        status_pair,
    )
    _add(
        stdscr,
        2,
        0,
        (
            f"RX health={health}/{reason or '—'}  "
            f"target={target if target is not None else '—'} "
            f"({jmin if jmin is not None else '—'}–{jmax if jmax is not None else '—'})  "
            f"adaptive={m.get('jitter_adaptive')}  "
            f"q={qpk if qpk is not None else '—'}/{qcap if qcap is not None else '—'}"
        ),
        health_pair,
    )
    _add(
        stdscr,
        3,
        0,
        (
            f"jitter_ema={jitter if jitter is not None else '—'} ms  "
            f"peak={peak if peak is not None else '—'}  "
            f"lost={lost if lost is not None else '—'}  "
            f"rx={rx if rx is not None else '—'}  "
            f"late={late if late is not None else '—'}  "
            f"underrun={und if und is not None else '—'}  "
            f"fill={fill if fill is not None else '—'}%  "
            f"pps={pps if pps is not None else '—'}  "
            f"loss_ppm={lppm if lppm is not None else '—'}"
        ),
    )
    fec_on = m.get("fec_enabled")
    fec_health = str(m.get("fec_health") or ("off" if not fec_on else "ok"))
    fec_pair = {
        "ok": _pair(color, 1),
        "warn": _pair(color, 2),
        "fail": _pair(color, 3),
        "off": 0,
    }.get(fec_health, 0)
    fec_state = "on" if fec_on else "off"
    _add(
        stdscr,
        4,
        0,
        (
            f"FEC {fec_state}/N={m.get('fec_group') if m.get('fec_group') is not None else '—'}  "
            f"health={fec_health}  "
            f"recovered={m.get('fec_recovered') if m.get('fec_recovered') is not None else '—'}  "
            f"unrecoverable={m.get('fec_unrecoverable') if m.get('fec_unrecoverable') is not None else '—'}  "
            f"rec_ppm={m.get('fec_recovered_ppm') if m.get('fec_recovered_ppm') is not None else '—'}  "
            f"unrec_ppm={m.get('fec_unrecoverable_ppm') if m.get('fec_unrecoverable_ppm') is not None else '—'}"
        ),
        fec_pair,
    )
    opus_on = m.get("opus_enabled")
    opus_health = str(m.get("opus_health") or ("off" if not opus_on else "ok"))
    opus_pair = {
        "ok": _pair(color, 1),
        "warn": _pair(color, 2),
        "fail": _pair(color, 3),
        "off": 0,
    }.get(opus_health, 0)
    _add(
        stdscr,
        5,
        0,
        (
            f"Opus {'on' if opus_on else 'off'}  "
            f"bitrate={m.get('opus_bitrate') if m.get('opus_bitrate') is not None else '—'}  "
            f"frame={m.get('opus_frame') if m.get('opus_frame') is not None else '—'}ms  "
            f"health={opus_health}  "
            f"decode_errors={m.get('opus_decode_errors') if m.get('opus_decode_errors') is not None else '—'}"
        ),
        opus_pair,
    )
    rp = view.return_path or {}
    ret_health = str(rp.get("return_health") or ("off" if not rp.get("enabled") else "ok"))
    ret_pair = {
        "ok": _pair(color, 1),
        "warn": _pair(color, 2),
        "fail": _pair(color, 3),
        "off": 0,
    }.get(ret_health, 0)
    _add(
        stdscr,
        6,
        0,
        (
            f"Return {'on' if rp.get('enabled') else 'off'}  "
            f"osc={'on' if rp.get('osc_enabled') else 'off'}  "
            f"audio={'on' if rp.get('audio_enabled') else 'off'}  "
            f"health={ret_health}  "
            f"safety={rp.get('return_safety') if rp.get('return_safety') is not None else '—'}  "
            f"last_writeback_ns={rp.get('last_writeback_ns') if rp.get('last_writeback_ns') is not None else '—'}"
        ),
        ret_pair,
    )
    disc = view.discovery or {}
    disc_on = disc.get("discovery_enabled")
    disc_health = str(disc.get("discovery_health") or ("off" if not disc_on else "ok"))
    disc_pair = {
        "ok": _pair(color, 1),
        "warn": _pair(color, 2),
        "fail": _pair(color, 3),
        "off": 0,
    }.get(disc_health, 0)
    devices = disc.get("discovered_devices") if isinstance(disc.get("discovered_devices"), list) else []
    mixers = [d for d in devices if isinstance(d, dict) and d.get("kind") != "peer"]
    summary = "none"
    if mixers:
        m0 = mixers[0]
        summary = (
            f"{m0.get('name') or '—'} {m0.get('model') or ''} "
            f"{m0.get('ip') or '—'} fw={m0.get('firmware') or '—'}"
        )
        if len(mixers) > 1:
            summary += f" +{len(mixers) - 1}"
    _add(
        stdscr,
        7,
        0,
        (
            f"Discovery {'on' if disc_on else 'off'}  "
            f"health={disc_health}  "
            f"devices={len(devices)}  "
            f"{summary}  "
            f"last_ts={disc.get('last_discovery_ts') if disc.get('last_discovery_ts') is not None else '—'}"
        ),
        disc_pair,
    )
    dsp = view.dsp or {}
    eq_h = str(dsp.get("eq_health") or ("off" if not dsp.get("eq_enabled") else "ok"))
    dyn_h = str(dsp.get("dyn_health") or ("off" if not dsp.get("dyn_enabled") else "ok"))
    fx_h = str(dsp.get("fx_health") or ("off" if not dsp.get("fx_enabled") else "ok"))
    rank = {"fail": 3, "warn": 2, "ok": 1, "off": 0}
    worst = max((eq_h, dyn_h, fx_h), key=lambda s: rank.get(s, 0))
    dsp_pair = {
        "ok": _pair(color, 1),
        "warn": _pair(color, 2),
        "fail": _pair(color, 3),
        "off": 0,
    }.get(worst, 0)
    chg = dsp.get("last_change") if isinstance(dsp.get("last_change"), dict) else {}
    chg_s = "—"
    if chg:
        chg_s = (
            f"{chg.get('section') or '—'} {chg.get('path') or '—'}="
            f"{chg.get('value')} @{chg.get('ts_ns') or '—'}"
        )
    _add(
        stdscr,
        8,
        0,
        (
            f"EQ {('on' if dsp.get('eq_enabled') else 'off')}/{eq_h}  "
            f"DYN {('on' if dsp.get('dyn_enabled') else 'off')}/{dyn_h}  "
            f"FX {('on' if dsp.get('fx_enabled') else 'off')}/{fx_h}  "
            f"last={chg_s}"
        ),
        dsp_pair,
    )
    rec = view.record or {}
    rec_health = str(rec.get("record_health") or ("off" if not rec.get("recording") else "ok"))
    rec_pair = {
        "ok": _pair(color, 1),
        "warn": _pair(color, 2),
        "fail": _pair(color, 3),
        "off": 0,
    }.get(rec_health, 0)
    files = rec.get("record_files") if isinstance(rec.get("record_files"), list) else []
    _add(
        stdscr,
        9,
        0,
        (
            f"Record {('on' if rec.get('recording') else 'off')}  "
            f"enabled={('on' if rec.get('record_enabled') else 'off')}  "
            f"health={rec_health}  "
            f"files={len(files)}  "
            f"path={rec.get('record_path') or '—'}  "
            f"last_ts={rec.get('last_record_ts') if rec.get('last_record_ts') is not None else '—'}"
        ),
        rec_pair,
    )
    tests = view.tests or {}
    test_health = str(tests.get("test_health") or "off")
    test_pair = {
        "ok": _pair(color, 1),
        "warn": _pair(color, 2),
        "fail": _pair(color, 3),
        "off": 0,
    }.get(test_health, 0)
    _add(
        stdscr,
        10,
        0,
        (
            f"Tests {test_health}  "
            f"run={tests.get('tests_run') if tests.get('tests_run') is not None else '—'}  "
            f"fail={tests.get('tests_failed') if tests.get('tests_failed') is not None else '—'}  "
            f"err={tests.get('tests_errors') if tests.get('tests_errors') is not None else '—'}  "
            f"skip={tests.get('tests_skipped') if tests.get('tests_skipped') is not None else '—'}  "
            f"last_test_run={tests.get('last_test_run') if tests.get('last_test_run') is not None else '—'}"
        ),
        test_pair,
    )
    svc = view.service or {}
    svc_on = bool(svc.get("service_enabled"))
    svc_pair = _pair(color, 1) if svc_on else 0
    _add(
        stdscr,
        11,
        0,
        (
            f"Service {'on' if svc_on else 'off'}  "
            f"action={svc.get('last_service_action') if svc.get('last_service_action') is not None else '—'}  "
            f"scope={svc.get('service_scope') or '—'}  "
            f"role={svc.get('service_role') or '—'}"
        ),
        svc_pair,
    )
    pkg = view.package or {}
    _add(
        stdscr,
        12,
        0,
        (
            f"Package {pkg.get('package_version') or '—'}  "
            f"build_ts={pkg.get('package_build_ts') if pkg.get('package_build_ts') is not None else '—'}"
        ),
    )
    rel = view.release or {}
    _add(
        stdscr,
        13,
        0,
        (
            f"Release {rel.get('release_version') or '—'}  "
            f"build_ts={rel.get('release_build_ts') if rel.get('release_build_ts') is not None else '—'}"
        ),
    )
    tol = "—" if view.tolerance_db is None else f"{view.tolerance_db:g}"
    _add(
        stdscr,
        14,
        0,
        f"levels OK={view.n_ok}  KO={view.n_ko}  n/a={view.n_na}  (Δ≤{tol} dB)   q/ESC quit  r record",
    )
    if view.status == "missing":
        _add(stdscr, 15, 0, "No snapshot. Start the client with XAIR_SYNC_ON_CLIENT=true.", _pair(color, 2))
        return
    if view.status == "inactive":
        _add(stdscr, 15, 0, "Client stopped (snapshot marked inactive).", _pair(color, 2))
        return

    hdr = (
        f"{'ch':>3}  {'name':<16} {'fd':>5}  {'rms':<22}  {'osc':<22}  "
        f"{'ΔdB':>6} {'ok':>3}"
    )
    _add(stdscr, 15, 0, hdr[: max(0, w - 1)], curses.A_UNDERLINE)

    bar_w = 12
    row0 = 16
    max_rows = max(0, h - row0 - 1)
    for i, ch in enumerate(view.channels[:max_rows]):
        fd = "—" if ch.fader is None else f"{ch.fader:5.2f}"
        rms_bar = bar(dbfs_to_fraction(ch.rms_dbfs), bar_w)
        osc_bar = bar(dbfs_to_fraction(ch.meter_db), bar_w)
        rms_s = "—" if ch.rms_dbfs is None else f"{ch.rms_dbfs:6.1f}"
        osc_s = "—" if ch.meter_db is None else f"{ch.meter_db:6.1f}"
        dd = "—" if ch.delta_db is None else f"{ch.delta_db:6.2f}"
        if ch.level_ok is True:
            ok_s, attr = "ok", _pair(color, 1)
        elif ch.level_ok is False:
            ok_s, attr = "no", _pair(color, 3)
        else:
            ok_s, attr = "—", 0
        line = (
            f"{ch.ch:3d}  {(ch.name or '')[:16]:<16} {fd}  "
            f"{rms_s} {rms_bar}  {osc_s} {osc_bar}  {dd} {ok_s:>3}"
        )
        _add(stdscr, row0 + i, 0, line[: max(0, w - 1)], attr)
    if len(view.channels) > max_rows:
        _add(stdscr, h - 1, 0, f"… {len(view.channels) - max_rows} more channels (enlarge terminal)", _pair(color, 2))


def _add(win: "curses._CursesWindow", y: int, x: int, text: str, attr: int = 0) -> None:
    h, w = win.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    chunk = (text or "")[: max(0, w - x - 1)]
    try:
        win.addstr(y, x, chunk, attr)
    except curses.error:
        pass
