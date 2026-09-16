"""One-command OSC launcher: X18 ↔ X-Air Network Bridge ↔ REAPER.

Does not touch USB/UDP audio, jitter, FEC, Opus, recorder, or JACK ports.
Starts existing ``xair_network_bridge_client`` / ``start-reaper-sync`` processes only.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

DEFAULT_OSC_PORT = 10024

Echo = Callable[[str], None]

MSG_X18 = "Conectando OSC con X18…"
MSG_REAPER = "Conectando OSC con REAPER…"
MSG_SYNC = "Iniciando sincronizador OSC…"
MSG_OK = "OSC conectado correctamente."

DEFAULT_REAPER_PROBE_PORTS: Tuple[int, ...] = (8000, 9001)
# Where THIS process binds. Must not be REAPER's local listen port (usually 8000).
DEFAULT_BRIDGE_LISTEN_PORT = 9001
BRIDGE_LISTEN_FALLBACKS: Tuple[int, ...] = (9001, 9003, 9010)
PLACEHOLDER_HOSTS = frozenset({"", "auto", "0.0.0.0"})


class OscLauncherError(Exception):
    """User-facing launcher failure (X18 / REAPER / ports / client)."""


def _truthy(raw: Optional[str]) -> bool:
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on")


def upsert_env_file(path: Path, updates: Dict[str, str]) -> None:
    """Set keys in a ``.env`` file without dropping other lines or comments."""
    path = Path(path)
    if path.is_file():
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
    else:
        lines = []
    pending = dict(updates)
    out: List[str] = []
    key_re = re.compile(r"^(\s*#\s*)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
    for line in lines:
        m = key_re.match(line)
        if m and m.group(2) in pending:
            key = m.group(2)
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(line)
    if pending and out and out[-1].strip():
        out.append("")
    for key, val in pending.items():
        out.append(f"{key}={val}")
    body = "\n".join(out)
    if body and not body.endswith("\n"):
        body += "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def resolve_osc_host(env: Optional[Dict[str, str]] = None) -> str:
    """``XAIR_OSC_IP`` wins, then ``XAIR_OSC_HOST``."""
    src = env if env is not None else os.environ
    for key in ("XAIR_OSC_IP", "XAIR_OSC_HOST"):
        raw = str(src.get(key) or "").strip()
        if raw and raw.lower() not in PLACEHOLDER_HOSTS:
            return raw
    return ""


def resolve_osc_port(env: Optional[Dict[str, str]] = None) -> int:
    src = env if env is not None else os.environ
    raw = str(src.get("XAIR_OSC_PORT") or "").strip()
    if not raw:
        return int(DEFAULT_OSC_PORT)
    try:
        port = int(raw)
    except ValueError as exc:
        raise OscLauncherError(
            f"Puerto OSC de la X18 incorrecto: XAIR_OSC_PORT={raw!r} "
            f"(usa 10024)."
        ) from exc
    if not (1 <= port <= 65535):
        raise OscLauncherError(
            f"Puerto OSC de la X18 fuera de rango: {port} (usa 1–65535, típico 10024)."
        )
    return port


def parse_proc_net_udp(text: str, port: int) -> bool:
    """True if ``/proc/net/udp`` (or udp6) lists a bind on ``port``."""
    want = f"{int(port):04X}".upper()
    for line in text.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        loc = parts[1]
        if ":" not in loc:
            continue
        if loc.rsplit(":", 1)[-1].upper() == want:
            return True
    return False


def udp_port_bound(port: int) -> bool:
    for name in ("/proc/net/udp", "/proc/net/udp6"):
        try:
            raw = Path(name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if parse_proc_net_udp(raw, port):
            return True
    return False


def icmp_ping(host: str, timeout_s: float = 2.0) -> bool:
    if sys.platform == "win32":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout_s * 1000)), host]
    else:
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout_s))), host]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout_s + 1.5,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def osc_handshake(host: str, port: int) -> Tuple[str, Tuple[Any, ...]]:
    from ..xair_control.osc_bridge import XAirOSCBridge

    with XAirOSCBridge.from_env(host=host, port=port) as br:
        return br.ping()


def discover_mixer_ip(*, timeout_s: float = 2.0, osc_port: int = DEFAULT_OSC_PORT) -> Optional[str]:
    """Broadcast ``/xinfo``; return the first mixer IPv4 or None."""
    from ..discovery.packets import build_xinfo_query, parse_xinfo_dgram

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(max(0.3, float(timeout_s)))
        sock.sendto(build_xinfo_query(), ("255.255.255.255", int(osc_port)))
        deadline = time.monotonic() + max(0.3, float(timeout_s))
        while time.monotonic() < deadline:
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                break
            except OSError:
                break
            src = addr[0] if addr else ""
            dev = parse_xinfo_dgram(data, src)
            ip = (getattr(dev, "ip", None) or src or "").strip()
            if ip and ip not in PLACEHOLDER_HOSTS:
                return ip
    finally:
        sock.close()
    return None


def client_pids() -> List[int]:
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", r"src\.cli xair_network_bridge_client"],
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError, OSError):
        return []
    pids: List[int] = []
    for line in out.split():
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids


def start_bridge_client(root: Path) -> None:
    """Background ``xair_network_bridge_client`` via the official script. Does not import it."""
    root = Path(root)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root)
    script = root / "scripts" / "xair_network_bridge_client.sh"
    log = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "xair-osc-launcher-client.log"
    if script.is_file():
        argv: Sequence[str] = ["bash", str(script)]
    else:
        py = root / ".venv" / "bin" / "python"
        exe = str(py) if py.is_file() else sys.executable
        argv = [exe, "-m", "src.cli", "xair_network_bridge_client"]
    with open(log, "ab") as stdout:
        subprocess.Popen(
            list(argv),
            cwd=str(root),
            env=env,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def resolve_reaper_listen_port(
    configured: Optional[int],
    *,
    bound_fn: Callable[[int], bool] = udp_port_bound,
) -> Tuple[int, bool]:
    """Return (port, listening). Prefer configured, then 8000, then 9001."""
    seen: List[int] = []
    if configured is not None:
        seen.append(int(configured))
    for p in DEFAULT_REAPER_PROBE_PORTS:
        if p not in seen:
            seen.append(p)
    for p in seen:
        if bound_fn(p):
            return p, True
    return seen[0], False


def resolve_bridge_listen_port(
    reaper_port: int,
    configured: Optional[int],
    *,
    bound_fn: Callable[[int], bool] = udp_port_bound,
) -> int:
    """UDP bind for the bridge. Never the same port REAPER already owns."""
    reaper_port = int(reaper_port)
    candidates: List[int] = []
    if configured is not None:
        cfg = int(configured)
        if cfg != reaper_port:
            candidates.append(cfg)
    for p in BRIDGE_LISTEN_FALLBACKS:
        if p != reaper_port and p not in candidates:
            candidates.append(p)
    for p in candidates:
        if not bound_fn(p):
            return p
    tried = ", ".join(str(p) for p in candidates) or "(ninguno)"
    raise OscLauncherError(
        "Puerto del puente OSC en uso. REAPER ya escucha en "
        f"{reaper_port}; el puente necesita otro puerto (probado {tried}). "
        "En REAPER: Local listen port = "
        f"{reaper_port}, Destination port = {DEFAULT_BRIDGE_LISTEN_PORT} "
        "(no uses el mismo). Cierra otro osc-launcher / start-reaper-sync."
    )


def _looks_local(host: str) -> bool:
    h = (host or "").strip().lower()
    return h in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "")


def launch_osc(
    *,
    project_root: Path,
    host: Optional[str] = None,
    port: Optional[int] = None,
    reaper_host: Optional[str] = None,
    reaper_port: Optional[int] = None,
    non_interactive: bool = False,
    ensure_client: bool = True,
    start_sync: bool = True,
    echo: Echo = print,
    ping_fn: Callable[[str], bool] = icmp_ping,
    handshake_fn: Callable[[str, int], Tuple[str, Tuple[Any, ...]]] = osc_handshake,
    discover_fn: Callable[..., Optional[str]] = discover_mixer_ip,
    bound_fn: Callable[[int], bool] = udp_port_bound,
    client_pids_fn: Callable[[], List[int]] = client_pids,
    start_client_fn: Callable[[Path], None] = start_bridge_client,
    prompt_fn: Optional[Callable[[str], str]] = None,
    sync_fn: Optional[Callable[..., None]] = None,
) -> Dict[str, Any]:
    """Configure OSC, start the client if needed, then ``start-reaper-sync``."""
    root = Path(project_root)
    env_path = root / ".env"

    echo(MSG_X18)

    updates: Dict[str, str] = {}
    if not _truthy(os.getenv("XAIR_OSC_ENABLED")):
        updates["XAIR_OSC_ENABLED"] = "true"
        os.environ["XAIR_OSC_ENABLED"] = "true"

    osc_port = int(port) if port is not None else resolve_osc_port()
    osc_host = (host or "").strip() or resolve_osc_host()

    if not osc_host:
        found = discover_fn(timeout_s=2.0, osc_port=osc_port)
        if found:
            osc_host = found
            echo(f"X18 detectada en {osc_host}")
        elif not non_interactive and prompt_fn is not None:
            osc_host = str(prompt_fn("IP de la X18 (OSC)")).strip()
        else:
            raise OscLauncherError(
                "X18 no responde: falta XAIR_OSC_IP / XAIR_OSC_HOST. "
                "Pon la IP de la mesa en .env o pasa --host. "
                "La mesa debe estar en la LAN (OSC UDP 10024)."
            )

    if not ping_fn(osc_host):
        echo(f"aviso: ping ICMP a {osc_host} falló; se prueba handshake OSC /status")

    try:
        from ..xair_control.osc_bridge import XAirOSCError as _OscErr
    except Exception:
        _OscErr = type("_OscErr", (Exception,), {})

    status_txt = ""
    try:
        status_txt, _args = handshake_fn(osc_host, osc_port)
    except (_OscErr, TimeoutError, ConnectionError, OSError) as exc:
        found = discover_fn(timeout_s=2.0, osc_port=osc_port)
        if found and found != osc_host:
            echo(f"reintento con X18 descubierta en {found}")
            osc_host = found
            try:
                status_txt, _args = handshake_fn(osc_host, osc_port)
            except (_OscErr, TimeoutError, ConnectionError, OSError) as exc2:
                raise OscLauncherError(
                    f"X18 no responde en {osc_host}:{osc_port} "
                    f"(handshake OSC /status: {exc2}). "
                    "Comprueba IP, que la mesa esté encendida y que nadie bloquee UDP 10024."
                ) from exc2
        else:
            raise OscLauncherError(
                f"X18 no responde en {osc_host}:{osc_port} "
                f"(handshake OSC /status: {exc}). "
                "Comprueba XAIR_OSC_IP / XAIR_OSC_PORT. Puerto típico: 10024."
            ) from exc

    os.environ["XAIR_OSC_HOST"] = osc_host
    os.environ["XAIR_OSC_IP"] = osc_host
    os.environ["XAIR_OSC_PORT"] = str(osc_port)
    os.environ["XAIR_OSC_ENABLED"] = "true"
    updates["XAIR_OSC_ENABLED"] = "true"
    updates["XAIR_OSC_HOST"] = osc_host
    updates["XAIR_OSC_IP"] = osc_host
    updates["XAIR_OSC_PORT"] = str(osc_port)

    echo(MSG_REAPER)

    r_host = (reaper_host or os.getenv("XAIR_REAPER_HOST") or "127.0.0.1").strip()
    cfg_port: Optional[int] = None
    if reaper_port is not None:
        cfg_port = int(reaper_port)
    else:
        raw_rp = str(os.getenv("XAIR_REAPER_PORT") or "").strip()
        if raw_rp:
            try:
                cfg_port = int(raw_rp)
            except ValueError as exc:
                raise OscLauncherError(
                    f"Puerto OSC de REAPER incorrecto: XAIR_REAPER_PORT={raw_rp!r}. "
                    "Usa 8000 (defecto REAPER) o el puerto de Control/OSC/Web."
                ) from exc
            if not (1 <= cfg_port <= 65535):
                raise OscLauncherError(
                    f"Puerto OSC de REAPER fuera de rango: {cfg_port}."
                )

    r_port, listening = resolve_reaper_listen_port(cfg_port, bound_fn=bound_fn)
    if _looks_local(r_host) and not listening:
        tried = []
        if cfg_port is not None:
            tried.append(str(cfg_port))
        tried.extend(str(p) for p in DEFAULT_REAPER_PROBE_PORTS if str(p) not in tried)
        raise OscLauncherError(
            "REAPER no responde: no hay nadie escuchando OSC en "
            f"{r_host} (puertos {', '.join(tried)}). "
            "En REAPER: Preferences → Control/OSC/Web → Enable OSC. "
            f"Local listen port = {cfg_port or 8000}. "
            "Destination IP = 127.0.0.1, Destination port = "
            f"{DEFAULT_BRIDGE_LISTEN_PORT} (distinto del listen de REAPER)."
        )
    if not _looks_local(r_host) and not listening:
        echo(
            f"aviso: no se puede comprobar el bind UDP remoto; se usará {r_host}:{r_port}"
        )

    raw_lp = str(os.getenv("XAIR_REAPER_LISTEN_PORT") or "").strip()
    cfg_listen: Optional[int] = None
    if raw_lp:
        try:
            cfg_listen = int(raw_lp)
        except ValueError as exc:
            raise OscLauncherError(
                f"Puerto OSC del puente incorrecto: XAIR_REAPER_LISTEN_PORT={raw_lp!r}."
            ) from exc
    listen_port = resolve_bridge_listen_port(r_port, cfg_listen, bound_fn=bound_fn)

    os.environ["XAIR_REAPER_HOST"] = r_host
    os.environ["XAIR_REAPER_PORT"] = str(r_port)
    os.environ["XAIR_REAPER_LISTEN_PORT"] = str(listen_port)
    updates["XAIR_REAPER_HOST"] = r_host
    updates["XAIR_REAPER_PORT"] = str(r_port)
    updates["XAIR_REAPER_LISTEN_PORT"] = str(listen_port)

    if updates:
        upsert_env_file(env_path, updates)

    echo(MSG_SYNC)

    client_ok = bool(client_pids_fn())
    if not client_ok and ensure_client:
        echo("cliente del bridge no está corriendo; arrancando xair_network_bridge_client…")
        start_client_fn(root)
        for _ in range(20):
            time.sleep(0.25)
            if client_pids_fn():
                client_ok = True
                break
        if not client_ok:
            echo(
                "aviso: xair_network_bridge_client se lanzó pero aún no aparece el proceso; "
                "el audio puede tardar (JACK). OSC de REAPER sigue."
            )
            client_ok = True
    elif not client_ok:
        raise OscLauncherError(
            "Cliente no corriendo: lanza `bash scripts/xair_network_bridge_client.sh` "
            "o quita --no-start-client."
        )

    echo(MSG_OK)
    echo("")
    echo("Estado final:")
    echo(f"  X18 OK     {osc_host}:{osc_port}  /status={status_txt or 'ok'}")
    echo(f"  OSC OK     XAIR_OSC_ENABLED=true")
    echo(f"  REAPER OK  {r_host}:{r_port}  (REAPER listen; el puente envía aquí)")
    echo(f"  puente OK  0.0.0.0:{listen_port}  (REAPER destination port)")
    echo(f"  sync OK    start-reaper-sync listo")
    echo(f"  cliente    {'running' if client_pids_fn() else 'started'}")
    echo(
        f"  REAPER: Local listen={r_port}  →  Destination {r_host} port={listen_port}"
    )

    status = {
        "x18": True,
        "osc": True,
        "reaper": True,
        "sync": bool(start_sync),
        "host": osc_host,
        "port": osc_port,
        "reaper_host": r_host,
        "reaper_port": r_port,
        "listen_port": listen_port,
        "status": status_txt,
        "client": client_ok,
    }

    if start_sync:
        runner = sync_fn
        if runner is None:
            from .x18_reaper_sync import ReaperSyncConfig, run_forever

            def runner(**_k: Any) -> None:
                cfg = ReaperSyncConfig.from_env(
                    reaper_host=r_host,
                    reaper_port=r_port,
                    listen_port=listen_port,
                )
                try:
                    run_forever(config=cfg, osc_host=osc_host, osc_port=osc_port)
                except OSError as exc:
                    err = str(exc).lower()
                    if getattr(exc, "errno", None) == 98 or "address already in use" in err:
                        raise OscLauncherError(
                            f"Puerto del puente OSC {listen_port} en uso. "
                            f"REAPER debe escuchar en {r_port} y enviar a {listen_port} "
                            "(Preferences → Control/OSC/Web: Local listen ≠ Destination port). "
                            "Cierra otro osc-launcher."
                        ) from exc
                    raise OscLauncherError(
                        f"No se pudo abrir OSC hacia REAPER ({r_host}:{r_port} / "
                        f"listen {listen_port}): {exc}"
                    ) from exc

        runner()
    return status
