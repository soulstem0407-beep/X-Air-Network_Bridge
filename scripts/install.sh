#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
fi
./.venv/bin/pip install -U pip setuptools wheel >/dev/null
./.venv/bin/pip install -r requirements.txt
if [[ ! -f ".env" ]]; then
  cp .env.example .env
  echo "Creado .env desde .env.example — edítalo antes de producir."
fi
echo "Listo. Activa el entorno con: source \"$ROOT/.venv/bin/activate\""
