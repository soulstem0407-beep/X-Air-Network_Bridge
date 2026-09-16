"""
Validación de entorno PortAudio orientada a Ubuntu Studio / Linux y X18 (Behringer).

No graba audio: sólo enumera dispositivos y abre un InputStream de prueba (~200 ms).

Requisitos: sounddevice (sin dependencias extra).
"""
from __future__ import annotations

import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

try:
    import sounddevice as sd
except ImportError:  # pragma: no cover
    sd = None  # type: ignore[misc, assignment]


EXPECTED_SR = 48_000
EXPECTED_CH = 18
TEST_DURATION_S = 0.2
# Bloques ~10 ms a 48 kHz → varias invocaciones del callback en ~200 ms
TEST_BLOCKSIZE = 480

# Nombre comercial Behringer X18 / familia X AIR en listados ALSA/PipeWire habituales
from ..virtual_device.x18_device import iter_x18_candidates


def _is_linux() -> bool:
    return platform.system().lower() == "linux"


def _pactl_server_name() -> Optional[str]:
    try:
        r = subprocess.run(
            ["pactl", "info"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    for ln in r.stdout.splitlines():
        if ln.strip().lower().startswith("server name:"):
            return ln.split(":", 1)[1].strip()
    return None


def _jack_lsp_available() -> bool:
    try:
        r = subprocess.run(
            ["jack_lsp"],
            capture_output=True,
            timeout=1,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False
    return r.returncode == 0


def _portaudio_hostapi_by_index(idx: int) -> str:
    apis = sd.query_hostapis()
    d = sd.query_devices(idx)
    h = int(d["hostapi"])
    return str(apis[h]["name"])


def _classify_backend_stack(
    *,
    device_index: int,
) -> Tuple[str, Dict[str, Optional[str]]]:
    """
    Inf stack: API PortAudio del dispositivo + heurística de sesión (pactl/JACK).
    """
    pa_name = _portaudio_hostapi_by_index(device_index).upper()
    details: Dict[str, Optional[str]] = {
        "portaudio_host_api": pa_name,
        "pactl_server_name": None,
        "notes": None,
    }

    if "JACK" in pa_name:
        details["notes"] = "PortAudio expone el host API JACK."
        return "JACK", details

    pactl_srv = _pactl_server_name()
    details["pactl_server_name"] = pactl_srv

    if pactl_srv:
        ps = pactl_srv.lower()
        if "pipewire" in ps:
            details["notes"] = (
                "pactl muestra PipeWire (típico en Ubuntu Studio / PW-pulse)."
                " El dispositivo PortAudio suele aparecer igualmente como ALSA;"
                " PulseAudio/PipeWire está debajo como capa de sesión."
            )
            return "PipeWire", details
        if "pulseaudio" in ps:
            details["notes"] = "PulseAudio gestor de sesión (sin indicio de PipeWire en el nombre pactl)."
            return "PulseAudio", details

    if "ALSA" in pa_name:
        extra = ""
        if _jack_lsp_available():
            extra = " (jack_lsp responde: posible servidor JACK en paralelo)."
        details["notes"] = (
            "Dispositivo vía ALSA en PortAudio. Sin pactl,"
            + " suele tratarse de acceso relativamente directo a ALSA o de un cliente no detectado aquí."
            + extra
        )
        return "ALSA", details

    details["notes"] = "Host API PortAudio sin clasificación ALSA/JACK estándar."
    return pa_name or "desconocido", details


def _match_x18_candidates() -> List[Tuple[int, dict]]:
    out: List[Tuple[int, dict]] = []
    for c in iter_x18_candidates(sd):
        out.append(
            (
                c.index,
                {
                    "name": c.name,
                    "max_input_channels": c.max_input_channels,
                    "max_output_channels": c.max_output_channels,
                },
            )
        )
    return out


def _merge_callback_flags(acc: Any, status: Any) -> None:
    """OR en sitio usando API pública CallbackFlags."""
    for attr in ("input_underflow", "input_overflow", "output_underflow", "output_overflow"):
        if getattr(status, attr, False):
            setattr(acc, attr, True)


def _new_callback_flags() -> Any:
    if sd is None:
        raise RuntimeError("sounddevice no disponible")
    return sd.CallbackFlags()


@dataclass
class StreamTrialResult:
    dtype_used: str
    int24_available: bool
    callback_invocations: int = 0
    flags_aggregate: Any = field(default_factory=_new_callback_flags)


def _open_input_trial(device_index: int, *, dtype: str) -> StreamTrialResult:
    acc_flags = sd.CallbackFlags()
    invocations = {"n": 0}

    def _cb(_indata, _frames, _t, status) -> None:  # type: ignore[no-untyped-def]
        invocations["n"] += 1
        if status:
            _merge_callback_flags(acc_flags, status)

    sr = float(EXPECTED_SR)
    latency_s = min(0.05, max(TEST_BLOCKSIZE * 2.0 / sr, 4.0 / sr))
    base_kw = dict(
        device=device_index,
        channels=EXPECTED_CH,
        samplerate=EXPECTED_SR,
        dtype=dtype,
        blocksize=TEST_BLOCKSIZE,
        latency=latency_s,
        callback=_cb,
    )
    try:
        stream = sd.InputStream(**dict(base_kw, prime_output_buffers_using_stream_callback=False))
    except TypeError:
        stream = sd.InputStream(**base_kw)

    with stream:
        stream.start()
        time.sleep(TEST_DURATION_S)
        stream.stop()

    return StreamTrialResult(
        dtype_used=dtype,
        int24_available=dtype == "int24",
        callback_invocations=invocations["n"],
        flags_aggregate=acc_flags,
    )


def _try_stream_formats(device_index: int) -> Tuple[StreamTrialResult, Optional[str]]:
    """
    Intenta int24; si PortAudio/Python rechaza formato, usa float32.
    Devuelve (resultado, advertencia).
    """
    warn: Optional[str] = None
    try:
        return _open_input_trial(device_index, dtype="int24"), warn
    except Exception as exc:  # pragma: no cover - hardware dependiente
        warn = (
            "No se abrió el stream en int24; PortAudio/driver puede no exponer ese dtype en esta ruta. "
            f"Detalle: {exc}. Se usará float32 como en gran parte del ecosistema ALSA/USB."
        )
    res = _open_input_trial(device_index, dtype="float32")
    return res, warn


def validate_x18_ubuntustudio() -> int:
    """
    Ejecuta comprobaciones. Código salida 0 si la X18 se detectó y cumple canales; 1 errores/graves; 2 no Linux.

    Imprime informe en español por stdout.
    """
    if sd is None:
        print("ERROR: instala sounddevice en el venv del proyecto.", file=sys.stderr)
        return 1

    print("=== Validación X18 / PortAudio (Linux / Ubuntu Studio) ===\n")

    if not _is_linux():
        print("Esta utilidad está pensada para Linux; el sistema actual no es Linux.")
        return 2

    print("--- Dispositivos PortAudio (todos) ---")
    print(sd.query_devices())
    print()

    apis = sd.query_hostapis()
    print("--- Host APIs PortAudio ---")
    for i, h in enumerate(apis):
        print(f"  [{i}] {h['name']!r} dispositivos default in={h.get('default_input_device')} out={h.get('default_output_device')}")
    print()

    cands = _match_x18_candidates()
    if not cands:
        print(
            "ADVERTENCIA: no se encontró ningún dispositivo cuyo nombre coincida "
            "(heurística Behringer X18 / X AIR / XR18)."
        )
        print(" Conecta la mesa por USB y comprueba cómo aparece en la lista anterior.")
        return 1

    print("--- Candidatos X18 por nombre ---")
    for idx, d in cands:
        print(f"  índice {idx}: {d['name']} (in={d['max_input_channels']}, out={d['max_output_channels']})")
    print()

    # Elige el primero con ≥18 entradas; si ninguno, reporta pero prueba igual el mejor candidato informativamente
    pick: Optional[Tuple[int, dict]] = None
    for idx, d in cands:
        if int(d["max_input_channels"]) >= EXPECTED_CH:
            pick = (idx, d)
            break
    warn_channels = False
    if pick is None:
        pick = (cands[0][0], cands[0][1])
        warn_channels = True

    dev_index, info = pick
    print(f"--- Dispositivo analizado en detalle ---\n Índice: {dev_index}\n Nombre: {info['name']}")

    chin = int(info["max_input_channels"])
    ok_ch = chin >= EXPECTED_CH
    if not ok_ch:
        print(f" PROBLEMA: sólo hay {chin} entrada(s); se requieren ≥{EXPECTED_CH} para modo multichannel previsto.")
    elif warn_channels:
        print(
            " NOTA: ningún candidato por nombre llega a ≥18 entradas; se usará el primero sólo como referencia,"
            " no se ejecutará stream de prueba a 18 canales."
        )
    else:
        print(f" Canales de entrada: {chin} (≥ {EXPECTED_CH}) OK")

    def_sr = float(info.get("default_samplerate", 0) or 0)
    print(f" Frecuencia por defecto reportada por PortAudio: {def_sr:.0f} Hz", end="")
    if abs(def_sr - EXPECTED_SR) < 1.0:
        print(" (coincide con 48 kHz)")
    else:
        print(f" (distinto de {EXPECTED_SR} Hz; el stream de prueba se pedirá igualmente a {EXPECTED_SR} Hz)")

    stack, stack_det = _classify_backend_stack(device_index=dev_index)
    print("\n--- Backend (heurística) ---")
    print(f" Clasificación: {stack}")
    print(f" Host API PortAudio (dispositivo): {stack_det.get('portaudio_host_api')}")
    if stack_det.get("pactl_server_name"):
        print(f" pactl «Server Name»: {stack_det['pactl_server_name']}")
    if stack_det.get("notes"):
        print(f" Nota: {stack_det['notes']}")

    print("\n--- Prueba de apertura de stream (~200 ms, callback no lee el buffer de captura) ---")
    trial: Optional[StreamTrialResult] = None
    fmt_warn: Optional[str] = None

    if not ok_ch:
        print(
            " OMITIDA: hace falta soporte ≥18 entradas para abrir este stream de prueba con seguridad;"
            " arregla el perfil USB / ALSA antes de repetir."
        )
    else:
        try:
            trial, fmt_warn = _try_stream_formats(dev_index)
        except Exception as exc:  # pragma: no cover
            print(f" ERROR al abrir stream: {exc}")
            trial = None

    if trial is not None:
        if fmt_warn:
            print(f" AVISO FORMATO: {fmt_warn}")
        print(f" dtype usado: {trial.dtype_used}")
        print(f" int24 usable en stream: {'sí' if trial.int24_available else 'no (se usó float32 o falló int24)'}")
        if stack == "ALSA" and trial.dtype_used == "float32":
            print(
                " AVISO ALSA: muchas rutas USB + PortAudio entregan float32 internamente aunque el hardware sea 24-bit;"
                " no implica fallo de la mesa, sólo el formato del stream de la API."
            )

        print(
            f" Invocaciones del callback (≈ {TEST_DURATION_S:.2f} s, blocksize={TEST_BLOCKSIZE}): "
            f"{trial.callback_invocations}"
        )

        f = trial.flags_aggregate
        print(
            " Indicadores PortAudio en callbacks (captura: input_overflow = datos descartados por saturar buffer;"
            " input_underflow = inserción de ceros en ciertos modos; salida_* no aplica en stream sólo entrada salvo flags residuales):"
        )
        print(f"   input_underflow:  {f.input_underflow}")
        print(f"   input_overflow:   {f.input_overflow}")
        print(f"   output_underflow: {f.output_underflow}")
        print(f"   output_overflow:  {f.output_overflow}")
        if not f:
            print("   (ningún flag en status durante la prueba)")
        else:
            print("   Resumen textual:", str(f) or "(sin detalle)")

    print("\n=== Fin del informe ===")

    if not ok_ch:
        return 1
    if trial is None:
        return 1
    f = trial.flags_aggregate
    if f.input_overflow or f.input_underflow:
        return 1
    return 0


def main() -> int:
    return validate_x18_ubuntustudio()


if __name__ == "__main__":
    sys.exit(main())
