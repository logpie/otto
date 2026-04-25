#!/usr/bin/env bash
# fake-otto.sh — drop-in replacement for `otto` used by the E2E harness.
#
# Mimics the bits of otto that the queue runner cares about:
#   - argv parsing for `build|improve|certify`
#   - writes a manifest at OTTO_QUEUE_PROJECT_DIR/otto_logs/queue/$ID/
#   - makes a real git commit (so merge tests have history)
#   - writes intent.md / appends to it (so .gitattributes union driver runs)
#   - exits with the requested status
#
# Behavior knobs (env vars, set by harness):
#   FAKE_OTTO_FAILS=1       → write manifest with exit_status=failure, exit 1
#   FAKE_OTTO_SLEEP=N       → sleep N seconds before doing anything
#   FAKE_OTTO_NO_MANIFEST=1 → skip manifest write
#   FAKE_OTTO_TOUCH=path    → create/modify this file (relative to cwd)
#   FAKE_OTTO_TOUCH_TEXT=s  → contents to put in the touched file
#   FAKE_OTTO_NO_COMMIT=1   → don't make a git commit
#   FAKE_OTTO_COST=0.42     → cost_usd to record
#   FAKE_OTTO_DURATION=1.0  → duration_s to record
#   FAKE_OTTO_PRINT=msg     → echo msg to stdout (visible in agent.log analog)

set -euo pipefail

CMD="${1:-}"
shift || true

# Trap SIGTERM so cancel tests see clean exits not SIGKILL kills (closer to real otto)
trap 'echo "fake-otto: caught SIGTERM" >&2; exit 143' TERM

if [ -n "${FAKE_OTTO_SLEEP:-}" ]; then
  sleep "${FAKE_OTTO_SLEEP}"
fi

if [ -n "${FAKE_OTTO_PRINT:-}" ]; then
  echo "${FAKE_OTTO_PRINT}"
fi

# Make a real change + commit (so merge has something to merge)
if [ -z "${FAKE_OTTO_NO_COMMIT:-}" ] && { [ -d .git ] || git rev-parse --git-dir >/dev/null 2>&1; }; then
  TASK_TOKEN="${OTTO_QUEUE_TASK_ID:-unknown}"
  TOUCH_PATH="${FAKE_OTTO_TOUCH:-fake-otto-output.txt}"
  TOUCH_PATH="${TOUCH_PATH//\{task_id\}/$TASK_TOKEN}"
  # Default TEXT is task-id-suffixed so two parallel runs DO produce
  # distinct content → real merge conflicts (not coincidental no-ops).
  TOUCH_TEXT="${FAKE_OTTO_TOUCH_TEXT:-from-${TASK_TOKEN}-$(date +%s%N)}"
  TOUCH_TEXT="${TOUCH_TEXT//\{task_id\}/$TASK_TOKEN}"
  mkdir -p "$(dirname "$TOUCH_PATH")"
  echo "$TOUCH_TEXT" >> "$TOUCH_PATH"
  # Append to intent.md too so the union merge driver gets exercised
  if [ "$CMD" = "build" ] || [ "$CMD" = "improve" ]; then
    INTENT_TEXT="${1:-(no intent)}"
    {
      echo ""
      echo "## $(date '+%Y-%m-%d %H:%M:%S') — $CMD"
      echo "$INTENT_TEXT"
    } >> intent.md
  fi
  # Configure git if not configured (CI / fresh-tmp safety)
  if ! git config user.email >/dev/null; then
    git config user.email "fake-otto@example.com"
    git config user.name "Fake Otto"
  fi
  git add -A 2>/dev/null || true
  git commit -q -m "fake-otto: ${CMD} ${1:-}" 2>/dev/null || true
fi

# Write manifest unless explicitly disabled
if [ -z "${FAKE_OTTO_NO_MANIFEST:-}" ]; then
  TASK_ID="${OTTO_QUEUE_TASK_ID:-}"
  if [ -n "$TASK_ID" ] && [ -n "${OTTO_QUEUE_PROJECT_DIR:-}" ]; then
    MANIFEST_DIR="${OTTO_QUEUE_PROJECT_DIR}/otto_logs/queue/${TASK_ID}"
    mkdir -p "$MANIFEST_DIR"
    EXIT_STATUS="success"
    if [ -n "${FAKE_OTTO_FAILS:-}" ]; then
      EXIT_STATUS="failure"
    fi
    BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
    HEAD_SHA=$(git rev-parse HEAD 2>/dev/null || echo "")
    NOW=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    FAKE_OTTO_CMD="$CMD" \
    FAKE_OTTO_TASK_ID="$TASK_ID" \
    FAKE_OTTO_BRANCH="$BRANCH" \
    FAKE_OTTO_NOW="$NOW" \
    FAKE_OTTO_HEAD_SHA="$HEAD_SHA" \
    FAKE_OTTO_RESOLVED_INTENT="${1:-}" \
    FAKE_OTTO_EXIT_STATUS="$EXIT_STATUS" \
    python3 - "$MANIFEST_DIR/manifest.json" <<'PY'
import json
import os
import sys

path = sys.argv[1]
cmd = os.environ.get("FAKE_OTTO_CMD", "")
data = {
    "command": cmd,
    "argv": [cmd],
    "queue_task_id": os.environ.get("FAKE_OTTO_TASK_ID", ""),
    "run_id": f"fake-{os.environ.get('FAKE_OTTO_TASK_ID', '')}",
    "branch": os.environ.get("FAKE_OTTO_BRANCH", ""),
    "checkpoint_path": None,
    "proof_of_work_path": None,
    "cost_usd": float(os.environ.get("FAKE_OTTO_COST", "0.42")),
    "duration_s": float(os.environ.get("FAKE_OTTO_DURATION", "1.0")),
    "started_at": os.environ.get("FAKE_OTTO_NOW", ""),
    "finished_at": os.environ.get("FAKE_OTTO_NOW", ""),
    "head_sha": os.environ.get("FAKE_OTTO_HEAD_SHA", ""),
    "resolved_intent": os.environ.get("FAKE_OTTO_RESOLVED_INTENT", ""),
    "focus": None,
    "target": None,
    "exit_status": os.environ.get("FAKE_OTTO_EXIT_STATUS", "success"),
    "schema_version": 1,
    "extra": {},
}
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, indent=2)
    fh.write("\n")
PY
  fi
fi

if [ -n "${FAKE_OTTO_FAILS:-}" ]; then
  exit 1
fi
exit 0
