# Mission Control Runtime/Web Readiness Results

## Outcome

This pass improves single-user web Mission Control readiness in the areas the previous gap list called out: runtime ownership, guided recovery, merge UX, evidence review, state/command diagnostics, and observability. Auth and multi-user behavior were intentionally left out.

## Key Fixes

- Added verified watcher runtime health and stale-lock handling.
- Prevented Stop watcher from targeting stale state PIDs unless a fresh heartbeat or held queue lock proves runtime ownership.
- Added runtime diagnostics to `/api/state` and `/api/runtime`.
- Added a web Runtime metric and recovery banner.
- Added review packets for run detail: next action, proof counts, changed files, diff command, evidence count, and failure reason.
- Added changed-file and proof columns to Landing Queue.
- Made ready queue tasks selectable from Landing Queue even when they only exist as queue-compatible live records.

## Live E2E Summary

- Project: `/tmp/otto-mc-live-e2e`
- Server: `otto web --no-open --port 8876`
- Browser: `agent-browser --session otto-prod-e2e`
- Scenarios:
  - Failed task review and requeue affordance.
  - Ready task review packet and merge confirmation.
  - Runtime warning banner for unfinished command drain and failed task.
  - Web New Job dialog queueing a Codex task with high effort.

## Final Verification

- `uv run pytest -q --maxfail=10`: 930 passed, 18 deselected in 119.34s.
- `uv run pytest tests/test_web_mission_control.py tests/test_mission_control_model.py tests/test_mission_control_integration.py -q`: 35 passed.
- `npm run web:typecheck`: passed.
- `npm run web:build`: passed.
- `uv run python -m compileall -q otto/mission_control otto/web`: passed.
- `git diff --check`: passed.

## Remaining Gaps

- State durability is still JSON-based. The new diagnostics make local failures visible, but they do not replace a transactional store.
- Provider reliability and long LLM build/certify quality were not re-benchmarked here.
- Review packet has enough structure for this pass, but a typed module would be better if evidence review expands.
