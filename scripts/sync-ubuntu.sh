#!/bin/bash
# Sync otto to ubuntu and run certifier benchmarks.
# Usage: ./scripts/sync-ubuntu.sh [smoke|parallel|browser|all]
set -e

REMOTE="yuxuan-ubuntu"
REMOTE_DIR="~/work/otto"
LOCAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MODE="${1:-smoke}"

echo "=== Syncing code to $REMOTE ==="
ssh "$REMOTE" "mkdir -p $REMOTE_DIR" 2>/dev/null
rsync -az --delete \
  --exclude '.venv' --exclude 'node_modules' --exclude '.git' \
  --exclude '.otto-workers' --exclude '__pycache__' \
  "$LOCAL_DIR/" "$REMOTE:$REMOTE_DIR/"

echo "=== Setting up on $REMOTE ==="
ssh "$REMOTE" "cd $REMOTE_DIR && \
  (test -d .venv || uv venv .venv) && \
  uv pip install -e '.[dev]' --python .venv/bin/python -q && \
  (cd bench/certifier-stress-test/task-manager && test -d node_modules || npm install --no-audit --no-fund -s)"

echo "=== Running certifier ($MODE) ==="
ssh "$REMOTE" "cd $REMOTE_DIR && PYTHONUNBUFFERED=1 .venv/bin/python scripts/bench-certifier.py $MODE"

echo "=== Pulling results back ==="
rsync -az "$REMOTE:$REMOTE_DIR/bench/certifier-stress-test/task-manager/otto_logs/" \
  "$LOCAL_DIR/bench/certifier-stress-test/task-manager/otto_logs-ubuntu/"

echo "=== Done. Results in bench/certifier-stress-test/task-manager/otto_logs-ubuntu/ ==="
