# Queued Review Packet Diff Error

## Observations

- User screenshot shows the first queued build immediately displaying a `fatal: ambiguous argument 'main...build/...'` changed-files error.
- The task is still `queued`; the watcher is stopped and the future task branch has not been created yet.
- `landing_status()` already suppresses branch diffs for queued/starting/running/terminating tasks.
- `_review_packet()` still calls `_branch_diff()` unconditionally, so selecting the queued row reproduces the false diff error in the detail panel.
- The landing plan also counts queued work as `Needs action`, which makes paused queued work look like a failure.

## Hypotheses

### H1: Detail review packet diffs future branches too early (ROOT HYPOTHESIS)
- Supports: screenshot error is exactly `git diff main...future-branch`; `_review_packet()` calls `_branch_diff()` before checking status.
- Conflicts: landing table itself does not show the same diff error because it already has an in-flight guard.
- Test: enqueue a build, fetch the queued run detail, and assert `review_packet.changes.diff_error is None`.

### H2: Queue task branch naming is malformed
- Supports: branch name contains a generated suffix and date.
- Conflicts: the same branch name is expected for future worktree creation; the failure is "unknown revision", not invalid branch syntax.
- Test: assert the branch string is present and syntactically normal while still absent from git refs.

### H3: Frontend renders a stale packet from a prior failed run
- Supports: detail panel was selected and refreshed asynchronously.
- Conflicts: API state in the screenshot includes the same queued run id and the error string comes from backend review data.
- Test: reproduce through `TestClient` without browser state.

## Experiments

- Confirmed H1 by code inspection against the queued path: landing suppresses diffs for in-flight statuses, detail does not.
- Added a regression test that enqueues a future-branch task, opens the queued run detail, and verifies `diff_error` and `diff_command` are both `null`.
- Replayed the user path in `agent-browser`: fresh repo, queue the expense portal build from the web modal, leave watcher stopped, select the queued live run, and inspect the review packet.

## Root Cause

The detail review packet attempted to inspect changed files for queued/in-flight tasks before their worktree branch existed.

## Fix

- Suppress detail changed-file diffs for queued/starting/running/terminating tasks.
- Mark certification, changed files, and evidence as pending for in-flight tasks instead of warning/failing.
- Split landing-plan "waiting" work from real "needs action" work in the frontend.
- Do not suggest `remove` as the primary queued-task action; point the user to Start watcher instead.
- Add a regression test for queued future-branch review packets.
