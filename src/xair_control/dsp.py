"""X AIR channel EQ / dynamics / FX OSC paths. Control plane only.

Does not touch USB capture, XBRI UDP, jitter, FEC, Opus, discovery, or the
DAW return-path modules. Write-back belongs on a control thread.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

EQ_BANDS = (1, 2, 3, 4)
EQ_BAND_LEAVES = ("type", "g", "f", "q")
GATE_LEAVES = ("on", "thr", "range", "attack", "hold", "release")
DYN_LEAVES = ("on", "thr", "ratio", "knee", "mgain", "attack", "hold", "release", "mix")
FX_SLOTS = (1, 2, 3, 4)


def _ch(ch: int) -> str:
    if not (1 <= int(ch) <= 18):
        raise ValueError("ch debe estar entre 1 y 18")
    return f"{int(ch):02d}"


def _band(band: int) -> str:
    if int(band) not in EQ_BANDS:
        raise ValueError("banda EQ debe ser 1..4")
    return str(int(band))


def _fx(slot: int) -> str:
    if int(slot) not in FX_SLOTS:
        raise ValueError("FX debe ser 1..4")
    return f"{int(slot):02d}"


def _par(n: int) -> str:
    if not (1 <= int(n) <= 64):
        raise ValueError("parámetro FX 1..64")
    return f"{int(n):02d}"


def eq_on_path(ch: int) -> str:
    return f"/ch/{_ch(ch)}/eq/on"


def eq_band_path(ch: int, band: int, leaf: str) -> str:
    leaf = str(leaf).strip().lower()
    if leaf not in EQ_BAND_LEAVES:
        raise ValueError(f"hoja EQ inválida: {leaf}")
    return f"/ch/{_ch(ch)}/eq/{_band(band)}/{leaf}"


def hpf_path(ch: int) -> str:
    return f"/ch/{_ch(ch)}/preamp/hpf"


def gate_path(ch: int, leaf: str) -> str:
    leaf = str(leaf).strip().lower()
    if leaf not in GATE_LEAVES:
        raise ValueError(f"hoja gate inválida: {leaf}")
    return f"/ch/{_ch(ch)}/gate/{leaf}"


def dyn_path(ch: int, leaf: str) -> str:
    leaf = str(leaf).strip().lower()
    if leaf not in DYN_LEAVES:
        raise ValueError(f"hoja dyn inválida: {leaf}")
    return f"/ch/{_ch(ch)}/dyn/{leaf}"


def fx_send_path(ch: int, fx: int) -> str:
    """Channel send into FX slot N via mix bus N (X AIR default 01..04)."""
    return f"/ch/{_ch(ch)}/mix/{_fx(fx)}"


def fx_return_path(fx: int, leaf: str = "fader") -> str:
    leaf = str(leaf).strip().lower()
    if leaf not in ("fader", "on", "pan"):
        raise ValueError("FX return: fader|on|pan")
    return f"/rtn/{_fx(fx)}/mix/{leaf}"


def fx_type_path(fx: int) -> str:
    return f"/fx/{int(_fx(fx))}"


def fx_par_path(fx: int, par: int) -> str:
    return f"/fx/{int(_fx(fx))}/par/{_par(par)}"


@dataclass(frozen=True)
class DspParam:
    """Parsed mixer DSP path. ``section`` is eq / dyn / fx."""

    section: str
    ch: Optional[int] = None
    band: Optional[int] = None
    fx: Optional[int] = None
    leaf: str = ""
    path: str = ""


def _osc_index(token: str, lo: int, hi: int) -> Optional[int]:
    try:
        n = int(token)
    except ValueError:
        return None
    if lo <= n <= hi:
        return n
    return None


def parse_dsp_address(address: str) -> Optional[DspParam]:
    """Map an X AIR OSC path to EQ / gate / compressor / FX. Else None."""
    parts = address.split("/")
    if len(parts) < 3:
        return None
    head = parts[1]
    if head == "ch" and len(parts) >= 5:
        ch = _osc_index(parts[2], 1, 18)
        if ch is None:
            return None
        family = parts[3]
        if family == "eq":
            if parts[4] == "on":
                return DspParam("eq", ch=ch, leaf="on", path=address)
            band = _osc_index(parts[4], 1, 4)
            if band is None or len(parts) < 6:
                return None
            leaf = parts[5]
            if leaf not in EQ_BAND_LEAVES:
                return None
            return DspParam("eq", ch=ch, band=band, leaf=leaf, path=address)
        if family == "preamp" and parts[4] == "hpf":
            return DspParam("eq", ch=ch, leaf="hpf", path=address)
        if family == "gate":
            leaf = parts[4]
            if leaf not in GATE_LEAVES:
                return None
            return DspParam("dyn", ch=ch, leaf=f"gate_{leaf}", path=address)
        if family == "dyn":
            leaf = parts[4]
            if leaf not in DYN_LEAVES:
                return None
            return DspParam("dyn", ch=ch, leaf=leaf, path=address)
        if family == "mix":
            fx = _osc_index(parts[4], 1, 4)
            if fx is None:
                return None
            if len(parts) == 5 or (len(parts) >= 6 and parts[5] == "level"):
                return DspParam("fx", ch=ch, fx=fx, leaf="send", path=address)
            return None
        return None
    if head == "rtn" and len(parts) >= 5:
        fx = _osc_index(parts[2], 1, 4)
        if fx is None or parts[3] != "mix":
            return None
        leaf = parts[4]
        if leaf not in ("fader", "on", "pan"):
            return None
        return DspParam("fx", fx=fx, leaf=f"return_{leaf}", path=address)
    if head == "fx":
        fx = _osc_index(parts[2], 1, 4)
        if fx is None:
            return None
        if len(parts) == 3:
            return DspParam("fx", fx=fx, leaf="type", path=address)
        if len(parts) >= 5 and parts[3] == "par":
            par = _osc_index(parts[4], 1, 64)
            if par is None:
                return None
            return DspParam("fx", fx=fx, leaf=f"par_{par}", path=address)
        return None
    return None


def classify_dsp_health(
    *,
    enabled: bool,
    errors_window: int,
    error_ppm: float,
    last_write_age_s: Optional[float] = None,
    stale_s: float = 30.0,
) -> str:
    """Dashboard color: ``off`` / ``ok`` / ``warn`` / ``fail``."""
    if not enabled:
        return "off"
    if int(errors_window) <= 0:
        if last_write_age_s is not None and last_write_age_s > stale_s:
            return "warn"
        return "ok"
    if float(error_ppm) >= 10_000.0:
        return "fail"
    return "warn"


def empty_dsp_snapshot(*, eq: bool, dyn: bool, fx: bool) -> dict:
    return {
        "eq_enabled": bool(eq),
        "dyn_enabled": bool(dyn),
        "fx_enabled": bool(fx),
        "eq_health": "off" if not eq else "warn",
        "dyn_health": "off" if not dyn else "warn",
        "fx_health": "off" if not fx else "warn",
        "last_eq_write_ns": None,
        "last_dyn_write_ns": None,
        "last_fx_write_ns": None,
        "last_change": None,
        "writes_ok": 0,
        "writes_err": 0,
    }
