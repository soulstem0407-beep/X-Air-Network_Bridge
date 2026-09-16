"""WAV sidecar for decoded PCM. Disk thread; never the UDP or JACK callback."""

from .sidecar import (
    WavRecorder,
    classify_record_health,
    default_record_cmd_path,
    default_record_path,
    default_record_state_path,
    empty_record_snapshot,
    float_to_pcm24,
    write_record_command,
)

__all__ = [
    "WavRecorder",
    "classify_record_health",
    "default_record_cmd_path",
    "default_record_path",
    "default_record_state_path",
    "empty_record_snapshot",
    "float_to_pcm24",
    "write_record_command",
]
