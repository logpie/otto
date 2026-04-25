# Mission Control Queue Failure Debug

Date: 2026-04-25

## Observations

- User queued a first follow-up task from the web portal and it failed within seconds.
- The run had no primary session log, no normal proof packet, and the UI only showed a generic failure.
- The project watcher log contained:
  - `Fatal Python error: init_sys_streams: can't initialize sys standard streams`
  - `OSError: [Errno 9] Bad file descriptor`
  - `reaped add-simple-authentication-and-role-cc47fe: failed (exit_code=1)`
- The failure happened after a web-server restart while an older watcher process was still alive.
- Normal completed runs still had proof, diff, artifacts, and logs.

## Hypotheses

### H1: Queue children inherited a bad stdio fd from a long-lived watcher (root)

- Supports: Python died during interpreter startup before Otto session files existed; the watcher outlived the terminal/web process that launched it; the error is `Bad file descriptor` in standard stream initialization.
- Conflicts: none found.
- Test: assert queue subprocesses are spawned with a stable `stdin` rather than inheriting watcher fd 0, and verify failed pre-artifact tasks expose watcher logs.

### H2: The child command was malformed

- Supports: the failure happened immediately after dispatch.
- Conflicts: the error is a Python runtime stdio initialization failure, not an Otto argument parse error; no session log or manifest was created.
- Test: inspect watcher log and run record command fields for malformed argv.

### H3: Mission Control hid an available failure source

- Supports: the watcher log contained the root cause but `/api/runs/{id}/logs` returned no text because the primary session log did not exist.
- Conflicts: normal artifact-backed runs display logs correctly.
- Test: seed a failed queue task with no primary log and a watcher log excerpt, then assert Proof, Artifacts, and Logs expose it.

## Root Cause

A watcher process can survive the terminal or web server that launched it and then spawn task children with an inherited broken fd 0. Python can fail during interpreter startup before Otto writes session logs, leaving Mission Control with only a generic failed queue state.

## Fix

- Queue runner now spawns task children with `stdin=subprocess.DEVNULL`.
- Queue run artifacts include watcher-log fallback paths when a terminal queue task has no primary session log.
- Mission Control derives a concise failure summary from watcher log excerpts and exposes it in Proof, Logs, Artifacts, and API details.
- Regression coverage now seeds this exact pre-artifact failure mode and checks that the UI/API shows the real root cause.

## UI Notes

Comparable build/task UIs put the actionable failure first, then let users drill into logs:

- GitHub Actions expands failed steps and supports line-level log links/search.
- Vercel shows a deployment error summary when logs are unavailable, then points users to build logs when they exist.
- Buildkite uses annotations for concise job-scoped summaries alongside logs and artifacts.

For Otto, this means the Proof packet should lead with: root cause, next action, evidence links, then logs/artifacts as drill-downs. It should not duplicate generic "failed" text in several panels.

# Mission Control Landed Diff Debug

Date: 2026-04-25

## Observations

- Live landed queue run `2026-04-25-051721-18f4a2` returned an empty diff from `/api/runs/2026-04-25-051721-18f4a2/diff`.
- Its review packet reported `file_count: 0`, `files: []`, and `diff_command: null`.
- The merge state for `merge-1777107550-44374-37be72a2` contained `target_head_before=e3f2600...` and branch outcome `merge_commit=8e2656d...`.
- `git diff --name-only e3f2600... 8e2656d...` in the project returned 10 changed files.
- The previous backend intentionally suppressed branch diff lookup for merged queue tasks to avoid errors after the source branch is deleted.

## Hypotheses

### H1: Landed queue tasks suppress all diff data after merge (root)

- Supports: `_review_packet` and `landing_status` replaced diff data with empty files when merge info existed.
- Conflicts: none found.
- Test: compute diff from merge state's `target_head_before` or merge commit first parent to `merge_commit`.

### H2: UI hides valid diff data for landed tasks

- Supports: user saw an empty diff panel.
- Conflicts: API itself returned empty `text` and `files`, so the UI was rendering backend truth.
- Test: inspect `/api/runs/<run>/diff` response.

### H3: Source branch was deleted or unreachable

- Supports: prior tests intentionally avoid diffing deleted merged branches.
- Conflicts: live branch still existed, but `main...branch` was empty after merge because the merge base was the branch tip.
- Test: compute merge-state diff independent of source branch reachability.

## Root Cause

Landed queue tasks used source-branch diff logic even after merge. Once a branch is merged, `main...branch` can be empty, and if the source branch is deleted it may be unreachable. The persisted merge state already has the durable commit range needed for historical review.

## Fix

- Landed queue review packets and `/diff` now compute changed files from persisted merge state.
- Merge state indexing stores `target_head_before`, `merge_commit`, and a first-parent `diff_base`.
- Cleaned failed queue history no longer advertises cleanup as an enabled next action when the queue item has already been removed.
- Regression tests cover landed diff after source branch deletion and cleaned failed queue history.

# Mission Control Navigation Debug

Date: 2026-04-25

## Observations

- User reports that clicking `Tasks` from Diagnostics can behave like a no-op.
- Browser reproduction could switch Diagnostics -> Tasks through the semantic locator, so the click handler itself is not completely dead.
- The URL stays `http://127.0.0.1:9000/` after switching to Diagnostics.
- Browser Back after opening Diagnostics navigates to `about:blank`, leaving the app instead of returning to Tasks.
- Reloading while Diagnostics is visible rehydrates the app on Tasks, so the current view is not refresh-safe.

## Hypotheses

### H1: Mission Control view state is local React state only (root)

- Supports: URL does not change when switching views; reload resets to default `tasks`; Back exits the app because no in-app history entry exists.
- Conflicts: none found.
- Test: add URL-backed view state, then verify Diagnostics refresh persists and Back returns to Tasks.

### H2: The Tasks tab is covered by an overlay or layout layer

- Supports: user sees a click no-op; layout has dense diagnostics and inspector panels.
- Conflicts: agent-browser can click the tab and switch views in the live layout.
- Test: inspect hit targets and add E2E click coverage for Diagnostics -> Tasks.

### H3: A stale static bundle is served in the browser

- Supports: user may have had an older bundle loaded after server rebuilds.
- Conflicts: Back/reload behavior is also incorrect in the current bundle.
- Test: verify route behavior after rebuilding and restarting the live server.

## Experiments

- Reproduced Diagnostics -> Back on the live server: after opening Diagnostics, the URL stayed `/`; browser Back left the app and navigated to `about:blank`.
- Reproduced Diagnostics -> Reload on the live server: reload returned to the default Tasks view instead of preserving Diagnostics.
- After URL-backed routing, `control-tour` E2E verifies Diagnostics reload stays on Diagnostics and browser Back returns to Tasks.

## Root Cause

Mission Control treated the active view and selected run as private React state, so the browser had no in-app history entry and no URL state to restore after reload.

## Fix

- View and selected run are now stored in query parameters (`view` and `run`).
- App startup reads the URL, replaces missing route state with `view=tasks`, and listens for `popstate`.
- View changes and run selections push in-app history entries; automatic refresh selection uses replace.
- E2E coverage now exercises Diagnostics refresh, Tasks tab switching, and browser Back returning to Tasks.
