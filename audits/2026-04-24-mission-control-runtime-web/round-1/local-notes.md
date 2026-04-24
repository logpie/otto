# Local Notes

## Implemented

- Runtime ownership:
  - Added watcher health with `running`, `stale`, and `stopped` states.
  - Verified `.otto-queue.lock` with `flock` before treating its PID as blocking.
  - Stop action can stop a stale held queue lock but will not kill a stale state PID that does not own the runtime.

- Runtime recovery diagnostics:
  - Added `/api/runtime`.
  - Added `runtime` to `/api/state`.
  - Surfaces queue/state parse errors, command backlog, malformed command logs, paused queued work, stale runtime, task attention, and merge blockers.

- Merge/evidence UX:
  - Landing Queue now shows changed-file counts and proof counts.
  - Run detail now includes a review packet with headline, next action, certification summary, changed files, diff command, evidence count, and failure reason.
  - Ready queue tasks with only queue-compatible records remain selectable from Landing Queue.

- Web self-serve:
  - Overview now includes a Runtime metric and recovery banner.
  - Sidebar watcher controls use actual runtime health rather than only old `alive` boolean.

## Verification Commands

- `uv run pytest tests/test_web_mission_control.py tests/test_mission_control_model.py tests/test_mission_control_integration.py -q`
- `npm run web:typecheck`
- `npm run web:build`
- `uv run python -m compileall -q otto/mission_control otto/web`
- `uv run pytest -q --maxfail=10`
- `agent-browser --session otto-prod-e2e ...` live UI flow against `/tmp/otto-mc-live-e2e`
