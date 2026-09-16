"""Offline unittest runner. Does not import receiver.py or talk to a mixer."""
from __future__ import annotations

import io
import json
import os
import time
import unittest
from pathlib import Path
from typing import Optional, Sequence, TextIO

# Explicit modules: never discover receiver.py / start_client (PortAudio).
OFFLINE_MODULES: Sequence[str] = (
    "tests.test_protocol",
    "tests.test_reorder",
    "tests.test_adaptive_jitter",
    "tests.test_fec",
    "tests.test_opus",
    "tests.test_writeback",
    "tests.test_osc_launcher",
    "tests.test_transport_loop",
    "tests.test_return_path",
    "tests.test_dsp",
    "tests.test_discovery",
    "tests.test_scene",
    "tests.test_port_names",
    "tests.test_jack_named_output",
    "tests.test_jack_graph",
    "tests.test_recorder",
    "tests.test_offline_suite",
    "tests.test_service",
    "tests.test_package",
    "tests.test_release",
    "tests.test_integration",
)


def classify_test_health(
    *,
    run: int,
    failed: int,
    errors: int,
    skipped: int,
) -> str:
    """Dashboard color: ``off`` / ``ok`` / ``warn`` / ``fail``."""
    if int(run) <= 0 and int(failed) <= 0 and int(errors) <= 0:
        return "off"
    if int(failed) > 0 or int(errors) > 0:
        return "fail"
    if int(skipped) > 0:
        return "warn"
    return "ok"


def empty_test_snapshot() -> dict:
    return {
        "last_test_run": None,
        "test_health": "off",
        "tests_run": 0,
        "tests_failed": 0,
        "tests_errors": 0,
        "tests_skipped": 0,
    }


def default_test_state_path(anchor: Optional[Path] = None) -> Path:
    raw = os.getenv("XAIR_TEST_STATE_PATH")
    if raw and str(raw).strip():
        return Path(str(raw).strip()).expanduser()
    base = Path(anchor) if anchor is not None else Path(".")
    if base.is_file():
        base = base.parent
    return base / ".xair_test.json"


def _atomic_write_json(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise


def snapshot_from_result(result: unittest.TestResult, *, started_ts: float) -> dict:
    run = int(result.testsRun)
    failed = len(result.failures)
    errors = len(result.errors)
    skipped = len(getattr(result, "skipped", []) or [])
    return {
        "last_test_run": float(started_ts),
        "test_health": classify_test_health(
            run=run, failed=failed, errors=errors, skipped=skipped
        ),
        "tests_run": run,
        "tests_failed": failed,
        "tests_errors": errors,
        "tests_skipped": skipped,
    }


def merge_test_into_sync_snapshot(sync_path: Path, test_snap: dict) -> None:
    """Patch report.test in an existing CLI snapshot. No-op if missing/inactive."""
    if not sync_path.is_file():
        return
    try:
        with open(sync_path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not data.get("active"):
            return
        report = data.get("report")
        if not isinstance(report, dict):
            report = {}
            data["report"] = report
        report["test"] = dict(test_snap)
        _atomic_write_json(sync_path, data)
    except Exception:
        return


def run_offline_suite(
    *,
    stream: Optional[TextIO] = None,
    verbosity: int = 1,
    modules: Optional[Sequence[str]] = None,
) -> tuple[unittest.TestResult, dict]:
    names = list(modules) if modules is not None else list(OFFLINE_MODULES)
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for name in names:
        suite.addTests(loader.loadTestsFromName(name))
    buf = stream if stream is not None else io.StringIO()
    runner = unittest.TextTestRunner(stream=buf, verbosity=int(verbosity))
    started = time.time()
    result = runner.run(suite)
    snap = snapshot_from_result(result, started_ts=started)
    return result, snap


def format_summary(snap: dict) -> str:
    return (
        f"tests_run={snap.get('tests_run', 0)}  "
        f"failed={snap.get('tests_failed', 0)}  "
        f"errors={snap.get('tests_errors', 0)}  "
        f"skipped={snap.get('tests_skipped', 0)}  "
        f"health={snap.get('test_health', '—')}  "
        f"last_test_run={snap.get('last_test_run', '—')}"
    )


def persist_test_snapshot(
    snap: dict,
    *,
    project_root: Path,
    sync_state_path: Optional[Path] = None,
) -> Path:
    path = default_test_state_path(project_root)
    _atomic_write_json(path, snap)
    if sync_state_path is not None:
        merge_test_into_sync_snapshot(sync_state_path, snap)
    return path
