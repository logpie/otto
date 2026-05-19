#!/usr/bin/env bash
# Otto-seeded scaffold (profile: webapp.react-vite-fastapi.py312).
# AUTHORITATIVE — do not rewrite this file; build product code AROUND it.
#
# Port contract (read by the clean verifier via the scaffold contract):
#   PORT / FRONTEND_PORT  -> the browser-facing Vite dev server (default 5173)
#   API_PORT              -> the FastAPI backend (default 8000)
# Vite runs with --strictPort (no silent 5173->5174 drift) and proxies
# /api -> http://127.0.0.1:$API_PORT.
#
# Toolchain: uv with a pinned Python 3.12, falling back to python3.12.
# It NEVER invokes the unversioned host interpreter (which resolves to the
# machine default — e.g. 3.14 with no pydantic-core wheels — and breaks
# clean-boot).
set -euo pipefail

FRONTEND_PORT="${FRONTEND_PORT:-5173}"
PORT="${PORT:-$FRONTEND_PORT}"
API_PORT="${API_PORT:-8000}"
HOST="127.0.0.1"
export PORT FRONTEND_PORT API_PORT

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- backend -------------------------------------------------------------
cd "$ROOT/backend"
if command -v uv >/dev/null 2>&1; then
  uv sync --python 3.12
  uv run --python 3.12 uvicorn app.main:app --host "$HOST" --port "$API_PORT" &
elif command -v python3.12 >/dev/null 2>&1; then
  python3.12 -m venv .venv
  ./.venv/bin/python -m pip install --quiet --upgrade pip
  ./.venv/bin/python -m pip install --quiet -e .
  ./.venv/bin/uvicorn app.main:app --host "$HOST" --port "$API_PORT" &
else
  echo "otto-scaffold: no usable uv or python3.12 toolchain (missing_toolchain)" >&2
  exit 86
fi
BACKEND_PID=$!

# --- frontend ------------------------------------------------------------
cd "$ROOT/frontend"
npm install
npm run dev -- --host "$HOST" --port "$PORT" --strictPort &
FRONTEND_PID=$!

# Bash-3 compatible shutdown: two positional waits, no GNU-only wait flags
# (macOS /bin/bash is 3.2).
trap 'kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true' INT TERM EXIT
wait "$BACKEND_PID"
wait "$FRONTEND_PID"
