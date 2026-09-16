"""Control OSC de consolas Behringer X AIR (sin flujo audio)."""

from .dsp import (
    DspParam,
    classify_dsp_health,
    parse_dsp_address,
)
from .dsp_control import DspController, DspJob
from .osc_bridge import (
    XAirOSCBridge,
    XAirOSCError,
    XAirOscTimeoutError,
)
from .scene import (
    SceneScope,
    apply_scene,
    dump_scene,
    load_scene_file,
    save_scene_file,
)

__all__ = [
    "XAirOSCBridge",
    "XAirOSCError",
    "XAirOscTimeoutError",
    "DspController",
    "DspJob",
    "DspParam",
    "classify_dsp_health",
    "parse_dsp_address",
    "SceneScope",
    "apply_scene",
    "dump_scene",
    "load_scene_file",
    "save_scene_file",
]
