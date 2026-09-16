#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"
export PYTHONHASHSEED="${PYTHONHASHSEED:-0}"
export TZ="${TZ:-UTC}"
export LC_ALL="${LC_ALL:-C}"
export CFLAGS="${CFLAGS:--O2 -g0 -DNDEBUG -fno-ident}"
export LDFLAGS="${LDFLAGS:--s -Wl,--build-id=none -Wl,--as-needed}"
if [[ -z "${SOURCE_DATE_EPOCH:-}" ]]; then
  SOURCE_DATE_EPOCH="$(date +%s)"
  export SOURCE_DATE_EPOCH
fi
PY="$ROOT/.venv/bin/python3"
if [[ ! -x "$PY" ]]; then
  PY="${XAIR_PYTHON:-python3}"
fi
exec "$PY" -m src.cli build-release "$@"
