#!/usr/bin/env bash
# Dev server: applies pending migrations, then serves with auto-reload.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8010}"
.venv/bin/alembic upgrade head
exec .venv/bin/uvicorn app.main:app --reload --reload-dir app --host "${HOST:-127.0.0.1}" --port "$PORT"
