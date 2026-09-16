"""HTTP dashboard over the SyncManager snapshot (stdlib only)."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Type
from urllib.parse import urlparse

from .reader import load_view, view_as_dict
from ..recorder.sidecar import default_record_cmd_path, write_record_command

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>X-AIR bridge meters</title>
<style>
  :root { color-scheme: dark; }
  body { font: 13px/1.4 ui-monospace, monospace; margin: 16px; background: #111; color: #ddd; }
  h1 { font-size: 16px; font-weight: 600; margin: 0 0 8px; }
  .meta, .rx, .fec, .opus, .ret, .disc, .dsp, .rec, .tests, .svc, .pkg, .rel { color: #999; margin-bottom: 6px; }
  .live { color: #6c6; } .stale, .inactive { color: #cc6; } .missing { color: #c66; }
  .ok { color: #6c6; } .warn { color: #cc6; } .fail, .ko { color: #c66; } .off { color: #888; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: 2px 8px 2px 0; white-space: nowrap; }
  th { color: #888; font-weight: 500; border-bottom: 1px solid #333; }
  .bar { display: inline-block; width: 88px; height: 8px; background: #222; vertical-align: middle; }
  .bar > i { display: block; height: 100%; background: #4a8; }
  button { font: inherit; margin-right: 8px; padding: 2px 10px; background: #222; color: #ddd; border: 1px solid #444; cursor: pointer; }
  button:hover { border-color: #888; }
</style>
</head>
<body>
<h1>X-AIR Network Bridge · meters / health</h1>
<div id="meta" class="meta">loading…</div>
<div id="rx" class="rx"></div>
<div id="fec" class="fec"></div>
<div id="opus" class="opus"></div>
<div id="ret" class="ret"></div>
<div id="disc" class="disc"></div>
<div id="dsp" class="dsp"></div>
<div id="rec" class="rec"></div>
<div id="tests" class="tests"></div>
<div id="svc" class="svc"></div>
<div id="pkg" class="pkg"></div>
<div id="rel" class="rel"></div>
<div>
  <button type="button" id="recStart">Record start</button>
  <button type="button" id="recStop">Record stop</button>
</div>
<table>
  <thead>
    <tr><th>ch</th><th>name</th><th>fader</th><th>rms dBFS</th><th></th><th>osc dB</th><th></th><th>ΔdB</th><th>ok</th></tr>
  </thead>
  <tbody id="rows"></tbody>
</table>
<script>
function frac(db) {
  if (db === null || db === undefined) return 0;
  return Math.max(0, Math.min(1, (db + 60) / 60));
}
function fmt(v, n) {
  if (v === null || v === undefined) return "—";
  return Number(v).toFixed(n);
}
async function tick() {
  const r = await fetch("/api/snapshot");
  const d = await r.json();
  const el = document.getElementById("meta");
  el.className = "meta " + d.status;
  const age = d.age_sec == null ? "—" : d.age_sec.toFixed(1) + "s";
  el.textContent = "snapshot " + d.status + "  age=" + age +
    "  drift_ns=" + (d.global_drift_ns == null ? "—" : d.global_drift_ns) +
    "  integration=" + ((d.integration || {}).integration_health || "off") +
    "  last_integration_ts=" + ((d.integration || {}).last_integration_ts == null ? "—" : (d.integration || {}).last_integration_ts) +
    "  levels OK=" + d.n_ok + " KO=" + d.n_ko + " n/a=" + d.n_na +
    "  Δ≤" + (d.tolerance_db == null ? "—" : d.tolerance_db) + " dB";
  const m = d.metrics || {};
  const health = m.rx_health || "—";
  document.getElementById("rx").textContent =
    "RX health=" + health + "/" + (m.rx_health_reason || "—") +
    "  target=" + (m.jitter_packets_target == null ? "—" : m.jitter_packets_target) +
    " (" + (m.jitter_packets_min == null ? "—" : m.jitter_packets_min) +
    "–" + (m.jitter_packets_max == null ? "—" : m.jitter_packets_max) + ")" +
    "  q=" + (m.queue_packets == null ? "—" : m.queue_packets) +
    "/" + (m.queue_capacity == null ? "—" : m.queue_capacity) +
    "  jitter_ema=" + (m.jitter_ms_ema == null ? "—" : m.jitter_ms_ema) + " ms" +
    "  peak=" + (m.jitter_ms_peak == null ? "—" : m.jitter_ms_peak) +
    "  lost=" + (m.packets_lost == null ? "—" : m.packets_lost) +
    "  underrun=" + (m.underruns == null ? "—" : m.underruns) +
    "  fill=" + (m.buffer_fill_percent_ema == null ? "—" : m.buffer_fill_percent_ema) + "%" +
    "  loss_ppm=" + (m.loss_window_ppm == null ? "—" : m.loss_window_ppm);
  document.getElementById("rx").className = "rx " + health;
  const fecOn = !!m.fec_enabled;
  const fecHealth = m.fec_health || (fecOn ? "ok" : "off");
  document.getElementById("fec").textContent =
    "FEC " + (fecOn ? "on" : "off") + "/N=" + (m.fec_group == null ? "—" : m.fec_group) +
    "  health=" + fecHealth +
    "  recovered=" + (m.fec_recovered == null ? "—" : m.fec_recovered) +
    "  unrecoverable=" + (m.fec_unrecoverable == null ? "—" : m.fec_unrecoverable) +
    "  rec_ppm=" + (m.fec_recovered_ppm == null ? "—" : m.fec_recovered_ppm) +
    "  unrec_ppm=" + (m.fec_unrecoverable_ppm == null ? "—" : m.fec_unrecoverable_ppm);
  document.getElementById("fec").className = "fec " + fecHealth;
  const opusOn = !!m.opus_enabled;
  const opusHealth = m.opus_health || (opusOn ? "ok" : "off");
  document.getElementById("opus").textContent =
    "Opus " + (opusOn ? "on" : "off") +
    "  bitrate=" + (m.opus_bitrate == null ? "—" : m.opus_bitrate) +
    "  frame=" + (m.opus_frame == null ? "—" : m.opus_frame) + "ms" +
    "  health=" + opusHealth +
    "  decode_errors=" + (m.opus_decode_errors == null ? "—" : m.opus_decode_errors);
  document.getElementById("opus").className = "opus " + opusHealth;
  const rp = d.return_path || {};
  const retHealth = rp.return_health || (rp.enabled ? "ok" : "off");
  document.getElementById("ret").textContent =
    "Return " + (rp.enabled ? "on" : "off") +
    "  osc=" + (rp.osc_enabled ? "on" : "off") +
    "  audio=" + (rp.audio_enabled ? "on" : "off") +
    "  health=" + retHealth +
    "  safety=" + (rp.return_safety == null ? "—" : rp.return_safety) +
    "  last_writeback_ns=" + (rp.last_writeback_ns == null ? "—" : rp.last_writeback_ns);
  document.getElementById("ret").className = "ret " + retHealth;
  const disc = d.discovery || {};
  const discOn = !!disc.discovery_enabled;
  const discHealth = disc.discovery_health || (discOn ? "ok" : "off");
  const devices = disc.discovered_devices || [];
  const mixers = devices.filter(x => x && x.kind !== "peer");
  let mixSummary = "none";
  if (mixers.length) {
    const m0 = mixers[0];
    mixSummary = (m0.name || "—") + " " + (m0.model || "") + " " + (m0.ip || "—") +
      " fw=" + (m0.firmware || "—");
    if (mixers.length > 1) mixSummary += " +" + (mixers.length - 1);
  }
  document.getElementById("disc").textContent =
    "Discovery " + (discOn ? "on" : "off") +
    "  health=" + discHealth +
    "  devices=" + devices.length +
    "  " + mixSummary +
    "  last_ts=" + (disc.last_discovery_ts == null ? "—" : disc.last_discovery_ts);
  document.getElementById("disc").className = "disc " + discHealth;
  const dsp = d.dsp || {};
  const eqOn = !!dsp.eq_enabled;
  const dynOn = !!dsp.dyn_enabled;
  const fxOn = !!dsp.fx_enabled;
  const eqH = dsp.eq_health || (eqOn ? "ok" : "off");
  const dynH = dsp.dyn_health || (dynOn ? "ok" : "off");
  const fxH = dsp.fx_health || (fxOn ? "ok" : "off");
  const rank = {fail: 3, warn: 2, ok: 1, off: 0};
  let worst = "off";
  [eqH, dynH, fxH].forEach(h => { if ((rank[h] || 0) > (rank[worst] || 0)) worst = h; });
  const chg = dsp.last_change || {};
  let chgS = "—";
  if (chg && (chg.path || chg.section)) {
    chgS = (chg.section || "—") + " " + (chg.path || "—") + "=" +
      (chg.value === undefined || chg.value === null ? "—" : chg.value) +
      " @" + (chg.ts_ns == null ? "—" : chg.ts_ns);
  }
  document.getElementById("dsp").textContent =
    "EQ " + (eqOn ? "on" : "off") + "/" + eqH +
    "  DYN " + (dynOn ? "on" : "off") + "/" + dynH +
    "  FX " + (fxOn ? "on" : "off") + "/" + fxH +
    "  last=" + chgS;
  document.getElementById("dsp").className = "dsp " + worst;
  const rec = d.record || {};
  const recOn = !!rec.recording;
  const recHealth = rec.record_health || (recOn ? "ok" : "off");
  const recFiles = rec.record_files || [];
  document.getElementById("rec").textContent =
    "Record " + (recOn ? "on" : "off") +
    "  enabled=" + (rec.record_enabled ? "on" : "off") +
    "  health=" + recHealth +
    "  files=" + recFiles.length +
    "  path=" + (rec.record_path || "—") +
    "  last_ts=" + (rec.last_record_ts == null ? "—" : rec.last_record_ts);
  document.getElementById("rec").className = "rec " + recHealth;
  const tests = d.tests || {};
  const testHealth = tests.test_health || "off";
  document.getElementById("tests").textContent =
    "Tests " + testHealth +
    "  run=" + (tests.tests_run == null ? "—" : tests.tests_run) +
    "  fail=" + (tests.tests_failed == null ? "—" : tests.tests_failed) +
    "  err=" + (tests.tests_errors == null ? "—" : tests.tests_errors) +
    "  skip=" + (tests.tests_skipped == null ? "—" : tests.tests_skipped) +
    "  last_test_run=" + (tests.last_test_run == null ? "—" : tests.last_test_run);
  document.getElementById("tests").className = "tests " + testHealth;
  const svc = d.service || {};
  const svcOn = !!svc.service_enabled;
  document.getElementById("svc").textContent =
    "Service " + (svcOn ? "on" : "off") +
    "  last_service_action=" + (svc.last_service_action == null ? "—" : svc.last_service_action) +
    "  scope=" + (svc.service_scope || "—") +
    "  role=" + (svc.service_role || "—");
  document.getElementById("svc").className = "svc " + (svcOn ? "ok" : "off");
  const pkg = d.package || {};
  document.getElementById("pkg").textContent =
    "Package " + (pkg.package_version || "—") +
    "  build_ts=" + (pkg.package_build_ts == null ? "—" : pkg.package_build_ts);
  const rel = d.release || {};
  document.getElementById("rel").textContent =
    "Release " + (rel.release_version || "—") +
    "  build_ts=" + (rel.release_build_ts == null ? "—" : rel.release_build_ts);
  const tb = document.getElementById("rows");
  tb.innerHTML = "";
  (d.channels || []).forEach(c => {
    const tr = document.createElement("tr");
    const ok = c.level_ok === true ? "ok" : (c.level_ok === false ? "no" : "—");
    const cls = c.level_ok === true ? "ok" : (c.level_ok === false ? "ko" : "");
    tr.innerHTML =
      "<td>" + c.ch + "</td><td>" + (c.name || "") + "</td>" +
      "<td>" + fmt(c.fader, 2) + "</td>" +
      "<td>" + fmt(c.rms_dbfs, 1) + "</td>" +
      "<td><span class=bar><i style=width:" + (frac(c.rms_dbfs)*100) + "%></i></span></td>" +
      "<td>" + fmt(c.meter_db, 1) + "</td>" +
      "<td><span class='bar osc'><i style=width:" + (frac(c.meter_db)*100) + "%></i></span></td>" +
      "<td>" + fmt(c.delta_db, 2) + "</td>" +
      "<td class='" + cls + "'>" + ok + "</td>";
    tb.appendChild(tr);
  });
}
tick();
setInterval(tick, 400);
document.getElementById("recStart").onclick = async () => { await fetch("/api/record/start", {method: "POST"}); };
document.getElementById("recStop").onclick = async () => { await fetch("/api/record/stop", {method: "POST"}); };
</script>
</body>
</html>
"""


def _handler_class(state_path: Path, cmd_path: Path, html: str) -> Type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            # Avoid access-log spam on the dashboard poll.
            return

        def do_GET(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
            if route in ("/", "/index.html"):
                body = html.encode("utf-8")
                self._send(200, "text/html; charset=utf-8", body)
                return
            if route == "/api/snapshot":
                payload = view_as_dict(load_view(state_path))
                body = json.dumps(payload).encode("utf-8")
                self._send(200, "application/json; charset=utf-8", body)
                return
            self._send(404, "text/plain; charset=utf-8", b"not found\n")

        def do_POST(self) -> None:  # noqa: N802
            route = urlparse(self.path).path
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                _ = self.rfile.read(min(length, 65536))
            if route == "/api/record/start":
                write_record_command(cmd_path, "start")
                body = b'{"ok":true,"action":"start"}\n'
                self._send(200, "application/json; charset=utf-8", body)
                return
            if route == "/api/record/stop":
                write_record_command(cmd_path, "stop")
                body = b'{"ok":true,"action":"stop"}\n'
                self._send(200, "application/json; charset=utf-8", body)
                return
            self._send(404, "text/plain; charset=utf-8", b"not found\n")

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


def run_http(
    path: Path,
    *,
    bind: str = "127.0.0.1",
    port: int = 8765,
    interval_s: float = 0.25,
) -> None:
    """Serve the dashboard until KeyboardInterrupt.

    Default bind is localhost so mixer telemetry is not advertised on the LAN.
    POST /api/record/start|stop writes the sidecar command file (no audio I/O).
    Poll interval matches the TUI / snapshot cadence (default 0.25 s).
    """
    cmd_path = default_record_cmd_path(path)
    ms = max(100, int(round(float(interval_s) * 1000.0)))
    html = _HTML.replace("setInterval(tick, 400);", f"setInterval(tick, {ms});")
    httpd = ThreadingHTTPServer((bind, int(port)), _handler_class(path, cmd_path, html))
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
