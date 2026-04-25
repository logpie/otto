# Web Mission Control Production Readiness - 2026-04-24

Worktree: `.worktrees/codex-provider-i2p`

## Goal

Make the web Mission Control portal usable as the default local single-user task manager for Otto: queue work, monitor live jobs, review proof, land completed work, recover blocked work, and audit what happened without needing TUI knowledge or hidden CLI state.

## Product Plan Executed

- Ported the frontend to a typed React/TypeScript task-board surface.
- Replaced the dense table-first view with an action-first workflow:
  - mission focus banner
  - task board grouped by Needs Action, Queued / Running, Ready To Land, Landed
  - selected Review Packet
  - Diagnostics view for runtime, command backlog, landing, live runs, events, and history
- Treated Review Packet integrity as the source of truth for landing actions.
- Added browser E2E scenarios driven by `agent-browser`.
- Used multiple read-only agents as real users:
  - first-time greenfield builder
  - release manager reviewing/landing/blocked states
  - support/operator debugging multi-state and command backlog states
- Ran multi-round code-health audits and fixed important findings before final verification.

## Critical Fixes

- Fresh queued jobs no longer show fatal git diff errors, missing queue manifests, missing worktrees, or fake evidence before the watcher starts.
- Done tasks are landable only when their branch diff can be inspected cleanly.
- Missing source branches now classify as blocked, not ready, and the merge action is disabled.
- Already-landed tasks skip source-branch diff inspection, so branch cleanup after landing cannot create false Review Packet failures.
- Already-merged merge attempts report `Already merged into <target>` before dirty-repo blockers.
- Merge history packets use the persisted merge target, with fallback to merge state for older history rows.
- Dirty ready tasks show `blocked` review state and `Repository cleanup required before landing`, not a contradictory `done`/ready packet.
- Command backlog is visible in Diagnostics with command id, kind, target, state, and age.
- Pending command backlog takes over the mission focus with `Start watcher` instead of telling users to queue a first job.
- Mixed needs-action plus ready work prioritizes reviewing problems; landing remains explicit from ready review packets.
- Sidebar counters now show product task state rather than raw `done` queue state.
- Review Packet wording for queued work now says `Waiting to start` instead of `Run finished`.
- Landed cards say `no unlanded diff`, not `no diff yet`.
- Raw git errors are translated in primary UI copy.
- Modal dialogs now have dialog roles, labels, and Escape handling.
- E2E artifacts default to `/tmp`, and the browser lock records owner/PID and cleans stale locks.
- E2E runs force `PYTHONPATH` to this worktree to avoid false passes against installed Otto code.

## E2E Matrix

Script: `scripts/e2e_web_mission_control.py`

Scenarios:

- `fresh-queue`: queue first build from the web UI before watcher starts.
- `ready-land`: review and land a clean completed task; assert the expected file content is present on `main` and the packet becomes audit-only.
- `dirty-blocked`: dirty tracked file blocks landing with clean recovery guidance.
- `multi-state`: queued, failed, ready, landed work in one board; inspect each packet and Diagnostics.
- `command-backlog`: pending command backlog is visible, Start watcher is enabled, the watcher drains the command, and the UI returns to no pending commands.

Final browser run:

```text
uv run --extra dev python scripts/e2e_web_mission_control.py --scenario all --artifacts /tmp/otto-web-e2e-final-4
PASS fresh-queue
PASS ready-land
PASS dirty-blocked
PASS multi-state
PASS command-backlog
```

## Verification

```text
npm run web:typecheck
PASS

npm run web:build
PASS

uv run pytest tests/test_web_mission_control.py tests/test_mission_control_model.py -q
55 passed in 11.67s

uv run pytest -q --maxfail=10
951 passed, 18 deselected in 112.23s
```

Static bundle check before commit:

- `otto/web/static/index.html` references `index-CWbcW1V1.js` and `index-BhnA45UB.css`.
- These assets must be staged with the static HTML and old hashed assets removed.

## Remaining Limits

- This is production-ready for local single-user use, not hosted multi-user deployment.
- Command backlog is inspectable and recoverable, but there is not yet a UI action to discard a pending command.
- Diagnostics is clearer than the MVP, but still dense for very large projects; future work should add expandable sections and better audit packet summaries.
- Modal focus trapping/restoration is still partial; roles, labels, and Escape handling are present.
- No 24x7 soak/chaos run was completed in this pass.
