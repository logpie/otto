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

# Autopilot Retry State Debug

Date: 2026-04-29

## Observations

- Health showed `Active 1` and `Needs attention 1` for the same certification intent after the user approved Autopilot recovery.
- Autopilot displayed a requeue-style action for `certify-the-existing-app-loads-and-the-8b463c-3-2` while that retry was already `running`.
- `/api/state` showed the running retry with `queue_status=running`, `landing_state=blocked`, and an active Autopilot pending decision on the same run.

## Hypotheses

### H1: In-flight landing items are being treated as recovery incidents (root)

- Supports: Autopilot scanned landing items by `landing_state=blocked` and did not skip `queue_status=running`.
- Conflicts: none.
- Test: feed Autopilot a running blocked landing item and assert it creates no incidents or pending decisions.

### H2: Older interrupted attempts remain visible while a later retry is active (root)

- Supports: the old interrupted attempt still counted as attention until a later retry completed successfully.
- Conflicts: cleanup should not delete old attempts while a retry is still running.
- Test: seed an interrupted attempt plus a later running retry; assert the old attempt is marked superseded for display, but cleanup still only considers later resolved retries.

## Fix

- Autopilot now ignores queued/running/terminating landing items in its recovery scan.
- Mission Control now marks older failed/interrupted attempts as superseded when a later retry exists, including when the retry is still running.
- Cleanup remains conservative: it only removes old failed queue records after a later retry reaches a resolved state.
- The review packet for an old attempt now says whether the retry is complete or still current.

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

# Resume Checkpoint Debug

Date: 2026-04-25

## Observations

- Web `Resume from checkpoint` reached the backend and returned 200.
- The old queue watcher ignored the command because it was running pre-fix code.
- After watcher restart, the queue spawned a resumed child, but the child exited with `Checkpoint fingerprint does not match the current code/prompt state`.
- The failed run's checkpoint was still from run start: phase `build`, status `in_progress`, current round `0`, and git SHA before the task commits.
- The task worktree HEAD had advanced through build/fix commits, so fingerprint rejection was correct.

## Hypotheses

### H1: Non-command `KeyboardInterrupt` does not refresh the single-agent checkpoint

- Supports: the checkpoint timestamp and git SHA stayed at run start after timeout.
- Conflicts: split-mode interrupt handling already writes paused checkpoints.
- Test: simulate an agent interrupt after committing work and assert resume has no fingerprint mismatch.

### H2: Queue resume fails to attach `--resume`

- Supports: user saw no visible resume progress.
- Conflicts: after watcher restart, child command reached checkpoint validation, which only happens after `--resume`.
- Test: inspect queue child failure and runner spawn path.

### H3: Checkpoint fingerprint comparison is too strict

- Supports: resume rejected.
- Conflicts: rejection prevented resuming stale state against changed code, which is desirable.
- Test: write a fresh paused checkpoint after the same commit and assert `resolve_resume` accepts it.

## Experiments

- Confirmed H1 by inspecting the stale checkpoint and live child error.
- Rejected H2 for the current watcher after observing the resumed child reach checkpoint validation.
- Rejected H3 as the root cause because fresh checkpoints should match current HEAD.

## Root Cause

The single-agent build path wrote an initial checkpoint before the agent call, but ordinary SIGTERM/`KeyboardInterrupt` did not rewrite it as `paused` after partial work changed the git fingerprint.

## Fix

Refresh the single-agent checkpoint on non-command interrupts with status `paused`, current git fingerprint, prior SDK session id, duration, and any available round/activity metadata.

Follow-up: queue resume now validates checkpoint compatibility before requeueing. A stale checkpoint is shown as not resumable, with the concrete reason surfaced to CLI, watcher logs, and Mission Control.

# Mission Control Nightly Follow-Up Debug

Date: 2026-04-28

## Observations

- Claude's follow-up review is directionally useful, but several finding IDs in the handoff are mismatched against the actual report. In the report, F62/F66/F71 are X1 network failures, not S2 history/filter findings.
- F1 is a real modal accessibility issue: the New Job dialog had focus management fixed, but the background shell also needs to be removed from the accessibility tree while a top-level modal is open.
- S2 is still a real current-code issue: Tasks view renders no task rows when `live` and `landing` are empty even if `/api/state.history.items` has completed runs.
- X1 is a real current-code issue in reduced form: a sticky connection banner exists, but background polling still surfaces repeated failure feedback and does not back off to the banner's stated 5s reconnect cadence.
- R7 cancellation trust is still a current-code issue: the UI applied an optimistic `cancelling` overlay before `/api/state` confirmed a queue transition.

## Hypotheses

### H1: Current regressions are mostly state mapping/polling policy, not missing data (root)

- Supports: history data, connection streak state, and modal state all exist; UI interpretation is what fails.
- Conflicts: S1 launcher latency may still involve backend project scanning, not just frontend rendering.
- Test: add targeted browser checks for history fallback, modal aria hiding, cancel no-optimism, and reconnect backoff.

### H2: The previous fixes are absent from the static bundle only

- Supports: the user repeatedly hit stale-server/stale-bundle issues.
- Conflicts: source inspection shows actual source-level gaps for S2, X1, and R7.
- Test: run typecheck/build and browser tests against the rebuilt bundle.

### H3: Nightly harness selector ambiguity is the main remaining issue

- Supports: several high findings are strict locator failures (`Land`, `Diff`, `Tasks`) rather than observed wrong state.
- Conflicts: S2/X1/R7/F1 include concrete UI state findings independent of selector ambiguity.
- Test: skip pure locator-only findings unless they map to a real ambiguous UI affordance.

## Fix

- Modal inert handling now optionally sets `aria-hidden=true`; topbar/main are hidden only while a top-level modal is open, while inspector-only inert remains non-hidden.
- Tasks view now falls back to current history-page rows when there are no live or landable rows, so completed-run projects do not look empty.
- Connection-loss polling now backs off to 5s while the banner is visible and suppresses repeated background toasts.
- Cancel no longer mutates row state optimistically; the row stays server-derived until `/api/state` reflects cancellation.
- Launcher project rows now have stable test IDs and exact action labels.

# Failed Elapsed Time Debug

Date: 2026-04-25

## Observations

- The failed `add-ticket-activity-history-record-3cd946` task showed an elapsed timer that kept increasing after failure.
- Its live run record had `status: failed` with no `timing.finished_at`, so Mission Control recomputed elapsed from `started_at` to the current time.
- The queue state already had the actual failed-at timestamp for the most recent resume attempt: `started_at=2026-04-26T00:45:51Z`, `finished_at=2026-04-26T00:45:53Z`.
- After refreshing the queue records, the live JSON froze at `duration_s=2.0`, but the queue task state still had no `duration_s`, so the board could lose the stopped duration after live-record retention.

## Hypotheses

### H1: Terminal live run records are missing `finished_at` (ROOT HYPOTHESIS)

- Supports: reproduced directly in the live JSON before repair.
- Conflicts: queue state did have finish time, so the data existed upstream.
- Test: update a terminal live record twice and assert `finished_at` and `duration_s` do not change.

### H2: Queue refresh does not copy terminal timestamps into run records

- Supports: stale live record had terminal status but no finish timestamp.
- Conflicts: queue state had enough data to repair it.
- Test: refresh a failed queue task and assert the live record uses queue `finished_at`.

### H3: Mission Control cards depend only on retained live records for elapsed display

- Supports: landing items exposed `duration_s`, but state repair did not persist it for failed queue attempts.
- Conflicts: active tasks correctly use live record elapsed.
- Test: terminal queue state should retain `duration_s` after refresh so board cards can show stopped time without a live record.

## Experiments

- Confirmed H1 by inspecting the failed live record and API response before repair.
- Confirmed H2 by starting the watcher and seeing the live record rewritten with `finished_at` and a frozen `duration_s`.
- Confirmed H3 by inspecting the same task in `/api/state`: `landing.items[].duration_s` was still `null` until queue state repair wrote it.

## Root Cause

Terminal queue attempts could leave live run records and queue task state without a frozen duration, so Mission Control treated the failed run like an active timer.

## Fix

- Registry writes now defensively set `finished_at` for terminal records.
- Queue refresh repairs terminal `finished_at` and `duration_s` in both run records and queue state.
- Queue terminal transitions now store `duration_s`; resume clears stale duration.
- Task cards can show stopped duration from landing state after live records age out.

# Log And Artifact Readability Audit

Date: 2026-04-25

## Observations

- Live task `add-ticket-activity-history-redo` completed in about 36m: build 12m, certify 15m, fix 9m.
- The runtime was reasonable for a real build plus two thorough certification rounds, but Mission Control made it hard to see why it took that long.
- Split-mode certify logs showed repeated `CERTIFY starting` / `CERTIFY complete` banners instead of `CERTIFY ROUND 1` and `CERTIFY ROUND 2`.
- The split-mode proof report was overwritten on each certifier invocation, so the final report only showed the last passing round and hid the first-round failures that justified the fix.
- Mission Control artifact labels such as `extra 1` did not explain what the user would open.
- Queue-backed runs could have `certify/proof-of-work.*` on disk while the review packet exposed only intent, manifest, summary, and build logs.
- Evidence counters included directory artifacts such as `worktree`, producing confusing ratios like `12/13` even when every readable file artifact existed.
- The task-board batch landing action existed only in the mission focus banner, so it disappeared when any other page state took priority.

## Hypotheses

### H1: Split phases lack user-facing round identity

- Supports: each certifier invocation reused logical phase `CERTIFY`, which is good for summaries but poor for log scanning.
- Test: a formatter can display `CERTIFY ROUND 2` while retaining `certify` as the summary phase.

### H2: Run-level PoW is not aggregating split rounds

- Supports: final proof artifacts were written by the last standalone certifier call.
- Test: run a mocked fail-fix-pass split loop and assert `proof-of-work.json` contains both failed and passed round history.

### H3: Web artifact defaults prioritize metadata over review proof

- Supports: the proof pane preferred summary/manifest before readable proof markdown.
- Test: adapter output should label proof report, markdown, and JSON siblings explicitly.

## Fix

- Added display-only phase labels so split logs now say `CERTIFY ROUND N` and `FIX ROUND N` without corrupting logical timing summaries.
- Added a run-level split proof report writer that aggregates all certification rounds after the split loop completes.
- Preserved explicit per-round duration/cost in proof history instead of marking it estimated.
- Added Mission Control artifact labeling/expansion for proof HTML, markdown, JSON, messages, and primary logs.
- Added session artifact discovery so queue and atomic runs expose certifier proof reports and certifier logs even if older run records did not persist them.
- Evidence counts now exclude directories, so the UI counts readable files instead of mixing files and worktree folders.
- Made the proof pane prefer readable proof markdown before JSON or generic summaries.
- Highlighted phase banners and certification markers in the log viewer.
- Added an always-visible task-board landing button for ready work; it launches the existing server-side `otto merge --fast --no-certify --all` flow.
- Regenerated the live `add-ticket-activity-history-redo` proof report from its checkpoint so the current report shows both the failed first round and the passing second round.

## Verification

- `uv run pytest tests/test_logstream.py tests/test_mission_control_adapters.py tests/test_v3_pipeline.py -k "display_phase_label or atomic_adapter_orders or split_loop_writes_aggregate_pow_round_history" -q`
- `uv run pytest tests/test_logstream.py tests/test_mission_control_adapters.py tests/test_v3_pipeline.py tests/test_mission_control_actions.py tests/test_web_mission_control.py -q`
- `npm run web:typecheck`
- `npm run web:build`
- `uv run ruff check otto tests/test_logstream.py tests/test_mission_control_adapters.py tests/test_v3_pipeline.py tests/test_web_mission_control.py tests/test_mission_control_actions.py`
- `git diff --check`

# Web Land All Partial Merge Failure

Date: 2026-04-26

## Observations

- Clicking web `Land all ready tasks` launched `otto merge --fast --no-certify --all`.
- Merge run `merge-1777189084-69755-cc90eb0b` merged `build/add-saved-filters-for-the-ticket-list-3d5253-2026-04-25`, then conflicted on `build/add-csv-export-for-the-filtered-ticket-878c61-2026-04-25`.
- The target branch advanced from `8e2656dd6809` to `6727be8b4dbc` before the failure.
- The project was left with an in-progress merge: `UU expense_portal/app.py` plus staged modifications in `expense_portal/static/styles.css`, `expense_portal/templates/dashboard.html`, and `tests/test_app.py`.
- Mission Control had already detected a ready-task collision between `add-csv-export` and `add-ticket-activity-history-redo` on `expense_portal/app.py`, `styles.css`, `dashboard.html`, and `tests/test_app.py`.
- Current web state now blocks further landing because the repository has unmerged paths and a merge in progress.

## Hypotheses

### H1: Web batch landing uses unsafe non-transactional fast merge (ROOT HYPOTHESIS)

- Supports: the web action shells `otto merge --fast --no-certify --all`; merge state records a partial target advance before conflict.
- Conflicts: none.
- Test: inspect `execute_merge_all` argv and update tests to require `--transactional`.

### H2: Collision preflight exists but does not guard the action

- Supports: API state exposed ready branch collisions before the click; the button still launched a batch merge.
- Conflicts: collisions are warnings today, not blockers.
- Test: assert web merge-all can surface collisions and still relies on transactional safety.

### H3: The product branches genuinely conflict

- Supports: `git diff --merge -- expense_portal/app.py` shows overlapping edits across saved filters and CSV export.
- Conflicts: a non-fast agent merge might be able to resolve, but `--fast` intentionally refuses.
- Test: reproduce with transactional fast merge in tests and confirm target remains unchanged on conflict.

### H4: Recovery UX is incomplete after failed merge

- Supports: web reports a large failure and repository blockers, but no first-class “abort failed merge” action is presented.
- Conflicts: CLI text mentions `git merge --continue`, but that is not self-serve for web users.
- Test: inspect merge run legal actions and add a recovery path separately if needed.

## Experiments

- Confirmed H1 by reading `otto/mission_control/actions.py`: `execute_merge_all` uses `otto merge --fast --no-certify --all`.
- Confirmed H2 by reading `/api/state`: `landing.collisions` included ready task file overlaps.
- Confirmed H3 by reading git conflict output for `expense_portal/app.py`.
- H4 remains a product gap after the immediate safety fix.

## Root Cause

The web batch landing action used the incremental fast merge path. That path is acceptable for explicit CLI users who can resolve conflicts, but it is unsafe as a web “Land all” default because any later conflict can leave the target partially advanced and the repository in an in-progress merge.

## Fix

- Made web batch landing transactional by default: `otto merge --fast --transactional --no-certify --all`.
- Updated Mission Control action previews and confirmation copy to explain transactional fast merge and collision-safe failure.
- Verified transactional fast merge keeps target unchanged on conflict in focused tests.
- Remaining recovery gap: the current live project is already in an in-progress merge from the old unsafe action. It needs `git merge --abort` or a manual conflict resolution before another landing attempt.

# Web Landing Recovery Too Manual

Date: 2026-04-26

## Observations

- After the old unsafe batch merge, Mission Control showed repository blockers but still required the user to know whether to run `git merge --abort`, retry a fast merge, or start an agentic merge.
- `/api/state` already exposes the needed signals: `landing.merge_blocked`, `landing.merge_blockers`, `landing.dirty_files`, and ready or blocked landing items.
- The live project has an in-progress merge with unmerged `expense_portal/app.py`, so ordinary `Land all` correctly refuses to launch.
- The existing `otto merge --no-certify --all` path is the conflict-resolving merge path; `--fast --transactional` is safe but intentionally does not resolve conflicts.

## Hypotheses

### H1: Recovery is manual because web has no action for in-progress merges (ROOT HYPOTHESIS)

- Supports: service exposes only `merge_all`; UI opens health for merge blockers.
- Conflicts: none.
- Test: add web endpoints for abort and recover, then assert the correct commands run.

### H2: The right default recovery is not another transactional fast merge

- Supports: transactional fast merge protects the target but leaves conflicting ready branches unresolved.
- Conflicts: fast merge is cheaper and provider-independent.
- Test: recover action should launch non-fast `otto merge --no-certify --all` after aborting the interrupted merge.

### H3: Users still need an escape hatch

- Supports: an agentic merge may be impossible with current provider/config, but aborting the bad git state should remain useful.
- Conflicts: abort alone does not complete the product intent.
- Test: provide both `Recover landing` and `Abort merge`.

## Root Cause

Mission Control treated an interrupted landing like generic dirty repository state. That was technically correct but operationally wrong: the UI had enough state to know the repo was mid-merge, yet it did not expose an autonomous recovery action.

## Fix

- Added `POST /api/actions/merge-abort` to abort an in-progress git merge from the web app.
- Added `POST /api/actions/merge-recover` to abort the interrupted git merge and relaunch `otto merge --no-certify --all`, which uses the conflict-resolving merge path instead of another pure-git fast merge.
- Updated the task-page focus banner to show `Recover landing` and `Abort merge` whenever merge blockers include unmerged paths or an in-progress merge.
- Updated runtime issue text and ordering so interrupted landing recovery is the primary visible issue.
- Added regression coverage for the action helpers, web routes, and runtime issue priority.

## Verification

- `uv run pytest tests/test_mission_control_actions.py::test_merge_abort_requires_in_progress_merge tests/test_mission_control_actions.py::test_merge_abort_aborts_git_merge tests/test_mission_control_actions.py::test_merge_recover_aborts_then_launches_agentic_merge tests/test_web_mission_control.py::test_web_merge_recovery_routes_record_actions -q`
- `uv run pytest tests/test_web_mission_control.py::test_web_runtime_issue_prefers_recovery_for_interrupted_merge -q`
- `uv run pytest tests/test_mission_control_actions.py tests/test_web_mission_control.py -q`
- `uv run pytest tests/test_mission_control_actions.py tests/test_web_mission_control.py tests/test_merge_orchestrator.py -q`
- `npm run web:typecheck`
- `npm run web:build`
- `uv run ruff check otto/mission_control/actions.py otto/mission_control/service.py otto/mission_control/runtime.py otto/web/app.py tests/test_mission_control_actions.py tests/test_web_mission_control.py`
- `git diff --check`
- `agent-browser` verified the live task page shows `Landing needs recovery`, `Recover landing`, and `Abort merge`; clicking `Recover landing` opens a confirmation dialog without executing recovery until confirmed.
- Live recovery E2E on `/Users/yuxuan/otto-projects/acme-expense-portal`: `Recover landing` launched `merge-1777190623-48468-573304a3`, resolved the two remaining ready branches, removed all conflict markers, left the repo clean, and changed landing counts to `ready=0, merged=4, blocked=1`.
- Product repo verification after recovery: `.venv/bin/python -m pytest -q` → `28 passed`.
- Cleaned the superseded failed original activity-history queue card after the redo landed; the live board now shows `ready=0, merged=4, blocked=0` and `No task needs action`.

## Follow-Up Finding

The merge run completed correctly, but while it was running the live run card stayed on `last_event=starting` even though the conflict agent was actively reading files, writing resolutions, and running tests. Mission Control now tails the conflict-agent narrative as merge progress and exposes the conflict-agent log/messages in merge artifacts.

# Autonomous Release Issue Resolution

Date: 2026-04-26

## Observations

- Users should not need to choose between `Recover landing`, `Land all`, `Abort merge`, and `Clean run record` when they only want Otto to make the release state sane.
- The web app already has enough state to pick a safe next release action: merge blockers, ready counts, landing state, queue task status, and task summaries.
- Some cleanup is safe only when Otto can prove a failed card is superseded by landed work. Otherwise it should fail closed and ask the user to inspect the review packet.

## Fix

- Added `POST /api/actions/resolve-release`.
- Added a `Resolve release issues` primary action in Mission Focus when a release-safe action exists.
- Resolution order is conservative:
  1. interrupted merge -> abort and relaunch conflict-resolving landing;
  2. ready work with clean repo -> transactional land-all;
  3. failed/stale cards with matching landed summaries -> queue cleanup;
  4. unknown blocked work -> warning with no mutation.
- Added queue-cleanup action plumbing so superseded failed cards can be removed through the same web flow.

## Verification

- `uv run pytest tests/test_mission_control_actions.py::test_queue_cleanup_shells_out_for_superseded_tasks tests/test_web_mission_control.py::test_web_resolve_release_recovers_interrupted_merge tests/test_web_mission_control.py::test_web_resolve_release_cleans_superseded_failed_tasks -q`
- `uv run pytest tests/test_mission_control_actions.py tests/test_web_mission_control.py tests/test_mission_control_model.py -q`
- `uv run ruff check otto/mission_control/actions.py otto/mission_control/service.py otto/web/app.py tests/test_mission_control_actions.py tests/test_web_mission_control.py`
- `npm run web:typecheck`
- `npm run web:build`
- `git diff --check`
- Restarted the live web server and verified `/api/state` for `acme-expense-portal`: `ready=0`, `merged=4`, `blocked=0`, `merge_blocked=false`, no runtime issues, project dirty=false.

# Certifier Background Server Leak

Date: 2026-04-28

## Observations

- A real Otto certification run for `/Users/yuxuan/otto-projects/acme-expense-portal` produced Claude SDK task output files of 18 GB and 9.2 GB under `/private/tmp/claude-501/.../962b6740-6591-481a-ab8d-b205a6c0d513/tasks/`.
- The session id matched the Otto certifier run `2026-04-28-064529-400050`.
- The leaked process was an orphan Flask dev server on port 5199 with PPID 1:
  `/Users/yuxuan/otto-projects/acme-expense-portal/.venv/bin/python .venv/bin/flask --app expense_portal.app run --port 5199`.
- The huge `.output` files were no longer open by the time they were inspected.
- Killing that one Flask process and removing the two temp output files reduced the task directory from about 27 GB to 12 KB.
- A follow-up scan found no remaining Claude SDK `.output` files over 100 MB.

## Hypotheses

### H1: Certifier agents can leave background project dev servers running (ROOT HYPOTHESIS)

- Supports: the orphaned process was a Flask dev server launched from the certified project; it outlived the SDK session and wrote access logs into Claude SDK task output.
- Conflicts: none.
- Test: snapshot listening processes before certification, run cleanup after the agent returns, and assert new project-scoped dev servers are terminated.

### H2: The proof-video prompt encouraged excessive endpoint polling

- Supports: the leaked output was dominated by Flask access logs from PDF endpoint checks.
- Conflicts: endpoint checks are legitimate; the disk growth required a background process to survive and keep writing.
- Test: add explicit prompt requirements to stop any app/server process started for certification and redirect noisy logs outside the SDK transcript.

### H3: Otto cannot prevent SDK `.output` growth directly

- Supports: the files live in Claude SDK temp task storage outside Otto's report/log directories.
- Conflicts: Otto can still reduce the risk by preventing orphaned background servers and instructing agents not to stream access logs into SDK-managed output.
- Test: add certifier-side cleanup independent of SDK internals.

## Root Cause

The certifier can ask the provider to start a project dev server in the background, but Otto did not enforce cleanup after the agent call. If the provider leaves that shell alive, framework access logs can continue streaming into Claude SDK temp `.output` files outside Otto's own artifact retention controls.

## Fix

- Add certifier-side cleanup for new listening dev-server processes that belong to the certified project.
- Make certifier prompts explicitly require cleanup of any app/server/background process started during certification.
- Prefer bounded foreground commands, temp log redirection, and explicit server stop/port-closed verification in certification evidence.

# Mission Control Nightly Follow-Up Debug

Date: 2026-04-28

## Observations

- Claude's follow-up review was directionally useful but not exact: the S2 IDs it cited as pagination/filter bugs were actually X1 network-blackhole findings in the report. The S2 history/filter cluster is real, but the IDs are F25-F31 plus medium findings F207-F217.
- Several high findings were already covered by current source before this pass: initial JobDialog focus, bottom-left toasts, inspector tab test IDs, stale watcher surfacing, cross-tab refresh nudges, and connection-lost banner/backoff.
- The previous mistake was verification scope: passing typecheck plus obvious targeted checks was not enough. Each claimed root-cause cluster needs at least one targeted regression test or an explicit “not fixed / harness-only” note.

## Fixes This Pass

- Modal accessibility isolation: `InertEffect` now can apply `aria-hidden` as well as `inert`; top-level dialogs hide the topbar, main content, and inspector from the accessibility tree while open.
- Cancel trust: removed optimistic cancel state from the frontend. The row stays in the server-reported state until the server confirms a transition.
- Network degradation: repeated `/api/state` failures no longer emit toast spam, and the visible poller backs off to the reconnect cadence while the lost-connection banner is active.
- History fallback: when there are no live/landing tasks, the Tasks board can show history rows instead of pretending there is no prior work.
- Launcher S1 UX: added search for large project lists, compact relative project-row paths, stable row test IDs, explicit “Projects” switch affordance in the topbar, and visible refresh progress text.
- Launcher CLS mitigation: reserve boot/hero/explainer height and align the boot placeholder with the launcher's top layout.

## Verification

- `npm run web:typecheck`
- `npm run web:build`
- `OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 .venv/bin/pytest tests/browser/test_first_run_clarity.py::test_empty_task_board_with_history_mentions_history_not_first_run tests/browser/test_first_run_clarity.py::test_launcher_many_projects_are_searchable_and_compact tests/browser/test_first_run_clarity.py::test_project_switch_button_returns_to_launcher tests/browser/test_accessibility.py::test_job_dialog_hides_background_from_accessibility_tree tests/browser/test_lost_connection_banner.py::test_banner_appears_after_3_consecutive_failures tests/browser/test_optimistic_cancel.py::test_cancel_does_not_optimistically_flip_row_to_cancelling tests/browser/test_optimistic_cancel.py::test_cancel_failure_keeps_server_state -m browser -p playwright -q`
- `OTTO_BROWSER_SKIP_BUILD=1 OTTO_WEB_SKIP_FRESHNESS=1 .venv/bin/pytest tests/browser/test_filters_url_persistence.py tests/browser/test_history_pagination.py -m browser -p playwright -q`
- `git diff --check`

## Residual Risk

- The report's 3s `/api/projects` calls are backend/IO latency; this pass improves frontend feedback and navigation, but does not make the endpoint faster.
- CLS should be lower after reserved heights, but I did not rerun the full nightly CLS recorder. Treat this as mitigated, not proven eliminated.

# Metro Field Ops Dogfood Rerun

Date: 2026-04-29

## Observations

- Created a fresh real project at `/Users/yuxuan/otto-projects/metro-field-ops-rerun-20260429-154109` from the Metro Field Ops baseline, then queued five real LLM tasks: dispatch board, technician schedule, detail notes, CSV/CLI exports, and audit timeline.
- The queue runner completed all five tasks with real provider calls. The SDK subprocesses used Claude `sonnet` for build and `haiku` for certifier even though the CLI banner displayed the configured Claude runtime default.
- `otto queue ls` showed stale checkpoint warnings on running tasks, which was a display bug: resume diagnostics should only appear for resumable terminal/interrupted states.
- `otto merge --all --verify smart --cleanup-on-success` hit real multi-branch conflicts, invoked the consolidated merge agent, resolved `fieldops/app.py`, `fieldops/server.py`, `fieldops/store.py`, adjusted `tests/test_exports.py`, landed all five branches, and left the product tests passing (`88 tests OK`).
- The merge then failed post-merge certification solely because the standard web proof gate required browser demo evidence and the merge-specific certifier prompt did not require agent-browser video/screenshots. The certifier story results were otherwise 19/19 pass.
- Mission Control/Autopilot reported idle after this failed merge because the failure only appeared as a historical merge row and `AutopilotController._classify` scanned live runs, landing rows, runner state, and blockers, but not failed merge history.

## Hypotheses

### H1: Merge-specific certification prompt omitted required browser proof (confirmed)

- Supports: `proof-of-work.json` had all stories passing but `demo_evidence.demo_status=missing`; the standard certifier prompt requires agent-browser evidence, while the merge-specific prompt treated screenshots/video as optional support.
- Conflicts: none.
- Test: assert the merge-specific prompt includes explicit agent-browser recording and evidence-dir paths.

### H2: Product integration failed after merge (rejected)

- Supports: merge terminal outcome was failure.
- Conflicts: product tests passed and the merge certifier reported all 19 stories pass; the report failed only at the demo-proof gate.
- Test: rerun project tests and inspect proof JSON outcome split.

### H3: Autopilot could already recover the failed merge (rejected)

- Supports: the failed merge exists in Mission Control history.
- Conflicts: `/api/state` showed `autopilot.health=idle`, no incidents, and no pending decisions because history merge failures were not classified.
- Test: feed the real project state into `MissionControlService(...).state()` and inspect `autopilot.incidents`.

## Fix

- Display effective provider-safe defaults in CLI/web model summaries instead of the raw runtime default when an agent type has a safer implicit model.
- Suppress queue resume checkpoint diagnostics for non-resumable running/done states.
- Update the merge-specific certifier prompt to require agent-browser visual proof for standard/thorough web UI merge certification.
- Add `otto merge-verify <merge-id>` and Mission Control `rerun_merge_verification` plumbing so Otto can rerun post-merge certification without attempting another git merge.
- Teach Autopilot to classify a failed historical merge whose branches landed but post-merge certification failed, and propose/execute “Rerun merge verification” instead of reporting idle.

## Rerun Recovery Follow-Up

### Observations

- Approving Autopilot's real `rerun_merge_verification` action launched `otto merge-verify merge-1777503465-98520-7b05b1f7 --verify smart`.
- The new certifier session `2026-04-29-232210-dd4034` stopped after about 25s. It ran tests, started `python3 -m fieldops.server --host 127.0.0.1 --port 5107`, curled `/`, and emitted no certifier markers or proof report.
- No `otto merge-verify` process remained, but the merge state file still said `status=running`, `cert_passed=null`, and retained the old `cert_run_id`.
- Mission Control's live merge record still showed the older failed merge, so live state and merge state diverged.
- The project server PID 47646 was orphaned with PPID 1, cwd inside the dogfood project, and still listening on 127.0.0.1:5107. The certifier cleanup guard missed it because the command was a custom `python -m fieldops.server`, not one of the hard-coded framework command markers.

### Hypotheses

#### H1: Custom project server cleanup is too narrow

- Supports: PID 47646 was a new listening process with cwd under the certified project, but `_looks_like_project_dev_server()` did not match `python3 -m fieldops.server`.
- Conflicts: none.
- Experiment: verify `lsof -a -p 47646 -d cwd -Fn` points at the dogfood project and `lsof -iTCP:5107` shows a listening socket. Confirmed.

#### H2: `merge-verify` can leave merge state running if the certifier exits without structured output

- Supports: session messages contain a phase end but no proof report or summary, state stayed running, and the process exited.
- Conflicts: the current rerun wrapper catches `Exception` around `_run_post_merge_verification`, so an ordinary `MalformedCertifierOutputError` should mark failed.
- Experiment: rerun foreground after tightening cleanup to capture the exact CLI exit path and state transition.

#### H3: Mission Control needs to classify stale running merge verification as recoverable

- Supports: the live merge record and state file disagree, and there is no live writer/process. Autopilot should not go idle in this condition.
- Conflicts: if a real merge-verify process is active, Autopilot should not offer duplicate reruns.
- Experiment: create a stale running merge state with no live writer and assert Autopilot proposes a rerun/repair instead of idle.

## Final Rerun Result

### Observations

- The first real Autopilot recovery approval correctly detected the failed post-merge verification and launched `otto merge-verify merge-1777503465-98520-7b05b1f7 --verify smart`, but that retry exposed Otto bugs instead of completing: a custom project server leaked, merge state stayed `running`, and no proof report was emitted.
- A later certifier run `2026-04-29-233811-3f4bc3` produced browser screenshots and passed all 19 stories, but the proof gate still failed because screenshot filenames did not map cleanly to story ids and the gate treated partial story-specific visuals as fully missing.
- A subsequent run `2026-04-29-235113-1bc9b6` correctly failed because the certifier used HTTP/text evidence only for browser UI stories. This was a real quality failure, not a gate bug.
- After tightening cleanup, merge-state terminalization, stale merge detection, visual-proof matching, and merge-specific certifier instructions, a real `otto merge-verify merge-1777503465-98520-7b05b1f7 --verify smart` rerun produced session `2026-04-30-000125-011d12`.
- The final rerun passed with merge state `status=done`, `cert_passed=true`, and 19/19 certified stories. Evidence included one browser recording, six story screenshots, CLI evidence, HTTP evidence, file/export validation, and 88 passing product tests.
- No project dev server remained listening on the tested port after the final rerun. Mission Control reported Autopilot `idle` with no incidents or pending decisions for the project.

### Conclusion

- The complex multitask dogfood project was built, conflict-merged, tested, and post-merge certified successfully.
- Autopilot can detect and dispatch recovery for a failed post-merge verification on a real project. It should not be described as able to repair Otto's own internal bugs by itself; the first real recovery attempt found those bugs, and this pass fixed them in Otto.
- For future dogfood loops, treat "provider run succeeded" and "certification passed" as separate outcomes. A green provider completion banner can still hide a failed proof gate, so CLI/UI wording should avoid implying product success before the verifier outcome is known.

# Incident Command Center Dogfood Proof-Repair Failure

Date: 2026-04-30

## Observations

- Created a fresh real greenfield project at `/Users/yuxuan/otto-projects/incident-command-center-dogfood-20260429-221052` and queued real Otto tasks through the queue runner.
- The first scaffold task built and certified correctly, but the queue marked it failed because successful generated artifacts (`.coverage`, `incident_command_center.egg-info/`) were not ignored. This is a separate generated-artifact hygiene bug and already has a focused fix in progress.
- The second `operator-actions` task built successfully and committed branch `build/operator-actions-2026-04-29` with 76 product tests passing.
- Certification round 1 proved the product behavior with 23/23 stories passing, then correctly blocked the run on the required demo proof gate because visual/video evidence was incomplete.
- Certification round 2 followed the proof-repair focus and created browser media artifacts, including `certify/evidence/recording.webm` and story screenshots.
- Round 2 then returned no structured story results. The final `certify/proof-of-work.json` has `stories=[]`, `passed_count=0`, `failed_count=0`, and `outcome=failed`.
- The same final proof JSON reports `evidence_gate.blocks_pass=false`, while `round_history[0]` preserves the prior 23 passing stories. This means the final media repair no longer blocked the proof gate, but Otto discarded the prior product-story verdict.
- `otto.pipeline.run_certify_fix_loop` currently assigns `last_stories = report.story_results` before checking for empty stories. It immediately breaks with `FAIL (no stories)` when a proof-repair round returns no stories, so final report generation receives an empty story list.

## Hypotheses

### H1: Proof-repair rounds overwrite the previous passing story set (ROOT HYPOTHESIS)

- Supports: the final proof JSON has no stories, but `round_history[0]` has 23 passing stories and `evidence_gate.blocks_pass=false`; pipeline code sets `last_stories = stories` before the empty-story guard.
- Conflicts: none yet.
- Test: add a split-loop regression where round 1 has all stories passing plus `evidence_gate.blocks_pass=true`, and round 2 returns `outcome=PASSED`, no stories, and `evidence_gate.blocks_pass=false`; the run should pass and preserve the round-1 stories.

### H2: The certifier proof-repair prompt is under-specified because it does not require re-emitting story markers

- Supports: round 2 collected evidence but emitted no stories; the proof-repair focus asks for media and a final verdict, not explicitly to repeat the previous `STORY_RESULT` markers.
- Conflicts: even if the prompt is tightened, proof-only repair should still be robust to an agent that emits only proof artifacts and a pass verdict.
- Test: inspect `run_certify_fix_loop` behavior with an intentionally story-empty repaired report; if preserving prior stories fixes the run, prompt tightening is optional rather than root.

### H3: Proof report generation is the only broken layer

- Supports: final `evidence_gate.blocks_pass=false` indicates report construction can detect repaired evidence.
- Conflicts: the queue failed before final report semantics alone; pipeline broke early on `no stories returned` and `BuildResult.passed` became false.
- Test: assert `run_certify_fix_loop` result itself is false before report rendering in the story-empty repair scenario.

### H4: The proof gate still failed despite `blocks_pass=false`

- Supports: final diagnosis text still contains stale "Required demo proof gate failed" wording from round 1.
- Conflicts: JSON `evidence_gate.blocks_pass=false/status=not_applicable`, and the observed failure path logged `no stories returned`.
- Test: in the regression, make round 2 `evidence_gate.blocks_pass=false` and diagnosis neutral; if it still fails, the issue is story handling, not stale diagnosis text.

## Experiments

### E1: Story-empty proof-repair round after passing product stories

- Setup: ran a minimal in-memory split-loop reproduction with two fake certifier reports. Round 1 returned one passing story plus `evidence_gate.blocks_pass=true`. Round 2 returned `CertificationOutcome.PASSED`, `story_results=[]`, and `evidence_gate.blocks_pass=false`. The code-fix agent was patched to raise if called.
- Result: reproduced the failure without provider calls. Output: `Certify-fix loop round 2: no stories returned` and `{'passed': False, 'tasks_passed': 0, 'tasks_failed': 0, 'journeys': []}`.
- Conclusion: H1 is confirmed. The loop discards the prior passing story set before final pass/report generation. H2 may still be improved later, but the core bug is pipeline state handling.

## Root Cause

The split certify/fix loop treats every certifier round as a complete product verdict. A proof-repair round can be evidence-only, so when it returns no stories after a prior all-passing proof-gated round, the loop overwrites `last_stories` with `[]`, records `FAIL (no stories)`, and fails the task even if the repaired evidence gate no longer blocks the pass.

## Fix

- When a proof gate blocks an otherwise all-passing round, the next certifier call now receives the previous passing stories as explicit required stories.
- If that proof-repair round still returns no stories but reports a non-blocking evidence gate, the loop preserves the prior passing story set for final reporting and does not dispatch a code-fix agent.
- If an evidence-only proof repair returns no stories and no conclusive evidence gate, the loop stops as an inconclusive proof failure instead of launching a meaningless code-fix round with no failed stories.
- Split-loop resume now seeds `last_stories` from checkpoint rounds and treats a resumed `Proof Repair Focus` as a proof-repair round, so interrupted proof repair does not lose the already-proven story contract.
- Added a regression where round 1 passes behavior but fails proof, round 2 writes media and returns no stories, and the run must pass with the original story retained in `proof-of-work.json`.
- Added a resume-shaped regression where the previous passing story set comes only from `resume_rounds`.

## Verification

- `uv run pytest -q tests/test_hardening.py::TestHistoryWrites::test_certify_fix_loop_repairs_proof_gate_without_code_fix tests/test_hardening.py::TestHistoryWrites::test_certify_fix_loop_preserves_stories_when_proof_repair_is_evidence_only`
- `uv run pytest -q tests/test_hardening.py::TestHistoryWrites tests/test_v3_pipeline.py::test_split_loop_writes_aggregate_pow_round_history tests/test_setup_gitignore.py tests/test_dirty_target_no_otto_files.py`
- `uv run ruff check otto/pipeline.py otto/setup_gitignore.py otto/web/app.py tests/test_hardening.py tests/test_setup_gitignore.py tests/test_dirty_target_no_otto_files.py`

# Incident Command Center Analytics Dogfood Findings

Date: 2026-04-30

## Observations

- Queued a third real greenfield slice, `analytics-reporting`, against `/Users/yuxuan/otto-projects/incident-command-center-dogfood-20260429-221052`.
- The build committed `build/analytics-reporting-2026-04-29` and entered certify. Round 1 passed all stories, then correctly entered proof repair because browser/demo evidence was not strong enough.
- The queue runner was interrupted during proof repair. The task became `interrupted`, but `otto queue ls` reported `checkpoint is stale: worktree status changed`.
- The only worktree change was an untracked runtime SQLite file, `icc.db`, created by the app during browser certification. The certifier cleanup normally removes this file, but an immediate watcher shutdown can kill the child before `finally` cleanup executes.
- After adding a generated-artifact classifier, the same interrupted task reported `RESUME ready` without manually deleting `icc.db`, and `otto queue resume analytics-reporting` resumed from the proof-repair checkpoint.
- The resumed run completed 14/14 stories but still failed the proof gate. The evidence directory contained story-specific screenshots and command evidence, but the proof matcher left `story-013-drill-down-to-details` on generic walkthrough and demanded visual proof for `story-014-csv-content-disposition` despite concrete HTTP CSV/header validation.

## Root Causes

- Resume fingerprinting compared raw `git status --porcelain --untracked-files=all`, so common generated artifacts that were not yet in an older project `.gitignore` made a valid checkpoint look stale.
- Mission Control dirty preflight and merge preflight filtered Otto-owned paths but not common generated artifacts such as `.coverage`, `*.egg-info/`, Flask `instance/`, and local SQLite DBs.
- Visual proof matching treated connector words like `from` as meaningful filename tokens, so `incident-detail-from-analytics.png` did not map to an analytics drill-down story.
- Demo proof treated any story that mentioned browser download behavior as visual-only, even when the story was an HTTP/file validation story with concrete `Content-Type`, `Content-Disposition`, filename, and CSV evidence.

## Fix

- Added local database/runtime patterns (`*.db`, `*.sqlite`, `*.sqlite3`, `instance/`) to Otto-managed common `.gitignore` entries.
- Added `is_common_build_artifact_path()` and used it in Mission Control dirty checks, merge preflight, and resume checkpoint fingerprinting.
- Switched dirty/preflight untracked inspection to `--untracked-files=all` so nested generated files under otherwise untracked parent directories can be classified correctly.
- Tightened visual filename tokenization so descriptive screenshots like `incident-detail-from-analytics.png` can map to the intended story.
- Allowed concrete file/download validation to satisfy proof for HTTP/API export stories, while still requiring visual proof for explicitly browser/DOM/live-UI download stories.

## Verification

- Existing analytics evidence recomputed with the fixed proof gate as `outcome=passed`, `evidence_gate.blocks_pass=false`, and `demo_status=strong`.
- `otto queue resume analytics-reporting` resumed the interrupted task after the generated-artifact fix without deleting `icc.db`.
- `uv run pytest -q` in the analytics dogfood worktree passed with `151 passed`.
- `otto merge --allow-any-branch build/analytics-reporting-2026-04-29 --verify smart` merged the branch successfully.
- `uv run pytest -q` on dogfood `main` passed with `151 passed`.

# Mission Control Landed-Run Proof Artifact Regression

Date: 2026-04-30

## Observations

- In Mission Control for `incident-command-center-dogfood-20260429-221052`, queue rows such as `operator-actions` and `analytics-reporting` show `LANDED`, but the proof tab also shows a prominent `What failed` section.
- Live run detail for `2026-04-30-062552-6ba62d` has `status=failed` and `review_packet.status=merged`; `review_packet.failure` still contains the stale queue failure (`exit_code=1`) even though landing context shows the branch has been merged.
- Generic artifacts for that run point at deleted `.worktrees/analytics-reporting/...` paths after queue cleanup, while durable proof files exist under root `otto_logs/sessions/2026-04-30-062552-6ba62d/...`.
- The full HTML proof report rewrites `evidence/recording.webm` to `/api/runs/<id>/proof-assets/evidence%2Frecording.webm`, but that route returns 403 because asset-root validation trusts the stale worktree `session_dir` from the run record.

## Hypotheses

### H1: Merged queue records need a final-state failure mask (ROOT HYPOTHESIS)

- Supports: `_review_packet` computes `merged=True` but still passes `_failure_summary(...)` through to the client.
- Conflicts: the historical queue run did fail, so the information should remain available somewhere less prominent.
- Test: create a failed queue run with a landing item that marks the branch merged; detail packet should have merged readiness and no top-level failure.

### H2: Proof asset validation uses stale session_dir after cleanup

- Supports: proof HTML lives under root `otto_logs/sessions/<run>/certify`, but `_proof_report_asset_root` returns the deleted worktree `session_dir`.
- Conflicts: existing tests pass when `session_dir` and proof HTML live under the same tree.
- Test: create a record whose stale `session_dir` is deleted but whose root proof report and evidence files exist; `/proof-assets/evidence%2F...` should return bytes.

### H3: Queue artifact enumeration never falls back to durable root session artifacts

- Supports: `/api/runs/<id>` lists only queue manifest plus missing worktree artifacts, while root session proof/log files exist.
- Conflicts: live queue records before cleanup should still prefer worktree artifacts.
- Test: create a queue record with stale worktree artifact paths and root session artifacts; artifacts endpoint should include root proof report, certifier log, and media evidence.

## Experiments

- Manual curl confirmed H1: `review_packet.readiness.state` is `merged`, but `review_packet.failure.reason` is still `Process exited with exit_code=1...`.
- Manual curl confirmed H2: `/api/runs/2026-04-30-062552-6ba62d/proof-assets/evidence%2Frecording.webm` returned 403 with `proof-report asset path is outside the session`.
- Manual artifact listing confirmed H3: root `otto_logs/sessions/.../certify/evidence/*.png` and `recording.webm` exist, but the artifact payload only exposes deleted `.worktrees/...` paths plus queue manifests.

## Root Cause

Mission Control was treating a queue run's original failed execution record as the canonical detail source even after landing context proved the branch was merged, and artifact/proof asset lookup trusted stale worktree paths after cleanup instead of rehydrating durable root session artifacts.

## Fix

- Suppressed stale top-level failure summaries in review packets once merge state proves the queue branch landed.
- Added durable root-session fallback for queue artifacts and logs when the original queue worktree session directory has been cleaned up.
- Made proof-report asset routing derive its asset root from the durable proof report location when the recorded `session_dir` is stale, so embedded videos and screenshots keep working after cleanup.
- Projected merged queue history rows from merge state before rendering Mission Control history/task cards, so a run no longer appears as both `LANDED` and `FAILURE`.

## Verification

- Live `analytics-reporting` run now reports `failure=null`, readiness `merged`, 60 artifacts, durable build/certify logs, and `LANDED` in `/api/state`.
- Live proof assets return `200 video/webm` for `evidence/recording.webm` and `200 image/png` for story screenshots.
- Live full HTML proof report returns `200 text/html` and rewrites embedded asset links to `/api/runs/<id>/proof-assets/...`.
- `uv run pytest -q tests/test_mission_control_model.py::test_history_projects_landed_queue_attempts_from_merge_state tests/test_web_review_packet.py::test_web_merged_failed_queue_run_suppresses_stale_failure_and_uses_durable_artifacts`
- `uv run pytest -q tests/test_mission_control_model.py tests/test_web_review_packet.py tests/test_mission_control_adapters.py tests/test_web_mission_control.py`

# Microfeed Dogfood Certifier Process Kill

Date: 2026-04-30

## Observations

- A real queued `core-platform` dogfood run built a Microfeed web app, failed cert round 1 on missing social controls, ran fix round 1, and passed cert round 2 functionally.
- The proof gate requested a proof-repair certification round. During that round the certifier ran `killall python3` while trying to restart the app server.
- `killall python3` killed Mission Control on port 9000 and the queue runner. The queue directory contained `ready.json`, but that file is a queue-child/session-readiness marker, not proof that the task is land-ready or safe to mark complete.
- The certifier prompt already said not to use `kill`, `pkill`, or `killall` broadly, so prompt-only policy was insufficient.

## Hypotheses

### H1: Certifier shell safety is prompt-only and needs provider-enforced permissions (ROOT HYPOTHESIS)

- Supports: the agent violated explicit lifecycle instructions and executed `killall python3`.
- Conflicts: the first attempted SDK hook shape installed but then failed during a real long-running certifier call because the string-prompt `query()` path can close stdin while Claude still needs hook callbacks.
- Test: install a provider permission callback through the interactive SDK client and verify with a real Claude smoke that safe Bash is allowed while `killall` is denied without a stream error.

### H2: Queue runner should ignore SIGTERM after a ready marker exists (rejected)

- Supports: `ready.json` predated `SIGTERM` by about two minutes, but watcher history still recorded failure.
- Conflicts: `ready.json` means the child has initialized its session; it does not mean build/certify completed. The watcher was correct to mark the task interrupted when the child died mid-certification.
- Test: clarify UI/CLI wording around queue-child readiness so it cannot be confused with merge-ready task state.

### H3: Proof repair certifier does too much process orchestration

- Supports: proof repair should collect missing evidence, but it restarted servers, killed processes, and dispatched subagents.
- Conflicts: the current product prompt asks certifier to start the app when needed.
- Test: later constrain proof-repair focus to reuse/own only its run-scoped app process and fail safely if startup is ambiguous.

## Experiments

- Confirmed from `certify/narrative.log` that proof repair ran `killall python3`; immediately afterward Mission Control was unreachable and `watcher.log` recorded SIGTERM shutdown.
- Confirmed there were orphaned Microfeed app/provider processes after the queue runner exited, then stopped only dogfood-scoped processes.

## Root Cause

Otto relied on certifier prompt text to prevent dangerous process cleanup, but provider Bash tools were still allowed to run broad process-kill commands in bypass mode.

## Fix

- Added a default Claude SDK `can_use_tool` Bash permission callback for Otto agent sessions.
- Agent calls that need provider callbacks now use the interactive `ClaudeSDKClient` path so the control stream stays open for the whole response.
- The permission callback blocks `killall`, `pkill`, malformed `kill`, and `kill` commands that do not target explicit numeric PIDs.
- Kept the hook guard shape available for explicit future use, but it is no longer the default safety mechanism.
- Added focused regression tests for unsafe command detection, explicit PID allowance, default permission-callback installation, and SDK option propagation.

## Follow-up Proof Gate Finding

- After the process-kill guard, the resumed Microfeed run reached `14/14` passing stories but still exited nonzero because the proof gate treated two quality issues as hard failures:
  - CSV/file validation was present in `observed_steps` and `observed_result`, but the gate only trusted the optional `evidence` field.
  - `partial` proof quality, such as generic walkthrough coverage for some UI stories, blocked pass instead of surfacing as a warning.
- Fix: file/download validation now considers observed steps/results. The temporary downgrade of `partial` demo proof to a warning was reverted; audit-grade proof requires story-specific visual/video evidence for UI stories.
- Fix: descriptive screenshot names now match story identity using observed steps/results too, so artifacts like `crud-posts-created.png` and `engagement-likes-reposts.png` are assigned to the right story instead of staying unassigned.
- Verification: replaying the Microfeed proof packet under the patched gate correctly blocks incomplete proof packets instead of silently accepting generic walkthrough coverage.

# Microfeed Dogfood Merge Proof Gate Regression

Date: 2026-04-30

## Observations

- A rerun of the Microfeed core-platform queue task completed the core autonomous build/certify path: the build produced commit `a40522e`, project tests passed, and certification reported 22/22 passing stories.
- The certifier attempted process cleanup during proof repair; prompt-only safeguards were insufficient, and the first hook-based implementation proved unreliable in the real SDK stream.
- `otto merge --all --verify smart` advanced `main` and merged the build branch, but post-merge verification failed on the stricter demo proof gate.
- The terminal stream printed a provider-level `SUCCESS` line before the proof gate converted the certifier result to failed, which looked contradictory next to `Merge incomplete`.
- Re-running `otto merge --all --verify smart` after the partial merge had no branches left to merge, so the obvious command was not the correct recovery path.

## Root Cause

Post-merge verification did not share the build/certify loop's proof-repair behavior. A proof-only failure immediately ended the merge as incomplete even when all product stories passed. The live certifier stream also used provider process success as if it were the final certification verdict, even though Otto applies proof-gate policy after the provider returns. The first process-safety hook implementation also used the SDK string-query path, which produced repeated `Stream closed` hook callback failures during a long proof-repair run.

## Fix

- Restored audit-grade proof semantics: `partial` demo proof now blocks pass again instead of being downgraded to a warning.
- Added one post-merge proof-repair recertification pass when all stories pass but the proof gate blocks on missing story-specific media.
- Changed standalone certifier stream completion from `SUCCESS` to `AGENT COMPLETE`, leaving final pass/fail wording to the certifier or merge command after proof-gate evaluation.
- Added `otto merge-verify <merge-id> --verify <policy>` guidance when a merge is incomplete because post-merge certification failed.
- Replaced the default hook guard with an interactive SDK permission callback so proof repair cannot kill unrelated processes and does not trip the SDK hook stream bug.

## Verification

- Replayed the Microfeed queue proof packet: it now fails the proof gate with missing story-specific screenshots for `edit-post`, `reply-post`, `repost-post`, and `unfollow-user`, which is the intended stricter quality bar.
- Replayed the Microfeed post-merge proof packet: it now fails the proof gate with missing story-specific media for `home-timeline`, not broken asset links.
- Checked both generated HTML proof reports for local asset links: 28/28 and 24/24 refs exist.
- Real Claude SDK smoke allowed safe Bash: `echo otto-safe-smoke`.
- Real Claude SDK smoke denied `killall __otto_nonexistent_process_name_abcdef__` through Otto's permission callback.
- `otto merge-verify merge-1777544920-57163-25fc921f --verify smart` completed successfully after two cert rounds, including post-merge proof repair; final state `terminal_outcome=success`, `cert_passed=true`, proof `story_count=19`, no missing visual evidence.
- `uv run pytest -q tests/test_cli_merge.py tests/test_merge_orchestrator.py tests/test_logstream.py tests/test_certifier_stories.py tests/test_agent_safety.py`
- `uv run ruff check otto/agent.py otto/certifier/__init__.py otto/logstream.py otto/merge/orchestrator.py otto/cli_merge.py tests/test_agent_safety.py tests/test_certifier_stories.py tests/test_merge_orchestrator.py tests/test_cli_merge.py`

# OpsBoard Multi-Task Dogfood Proof Repair Exhaustion

Date: 2026-05-01

## Observations

- Controlled dogfood project: `/Users/yuxuan/otto-projects/opsboard-dogfood-20260430-multitask`.
- Task `incident-workflow-actions` ran through the normal queue path with `--rounds 4`.
- Round 1 correctly failed missing workflow actions, then fix commit `0531525` implemented browser/API comment, status, owner, persistence, audit, validation, and tests.
- Round 2 correctly failed missing README curl examples, then fix commit `4f774aa` added endpoint examples.
- Round 3 passed the product story but failed the required demo proof gate because no browser visual proof was recorded.
- Round 4 entered Proof Repair Focus, collected six story-specific PNG screenshots, emitted `VERDICT: PASS`, and regenerated proof manifests.
- Despite that final PASS, `proof-of-work.json` still has `outcome=failed`, `demo_status=partial`, and `evidence_gate.blocks_pass=true` because `generic_recordings=0` and `story_videos=0`.
- The queue result is therefore `FAILURE` with `exit_code=1`; the product code appears complete, but proof repair exhausted the configured rounds before collecting the required `.webm`.
- The proof-repair certifier ignored the explicit instruction to verify a `.webm` exists, used macOS `open`, `screencapture`, and AppleScript, then attempted a broad `pkill -f "python.*run.py"` that Otto denied.

## Hypotheses

### H1: Proof-repair rounds are counted against product-fix rounds, so a successful product fix can still fail when one proof repair attempt misses video (ROOT HYPOTHESIS)

- Supports: the run had two product-fix failures, one product-pass/proof-gate failure, then one proof-repair attempt; `max_rounds=4` left no extra proof-only retry after the certifier collected screenshots but not video.
- Conflicts: the proof gate correctly blocked incomplete audit-grade proof; this is not a false product failure.
- Test: add a focused pipeline regression where max product rounds are exhausted but all stories pass and proof repair keeps returning proof-gate failure; verify Otto allows a bounded extra proof-repair retry or records a proof-specific recovery state instead of treating it like product failure.

### H2: The certifier prompt is too easy to ignore for `.webm` proof collection

- Supports: the proof-repair focus explicitly asked to verify a `.webm`, but the certifier stopped after screenshots and still emitted `VERDICT: PASS`.
- Conflicts: the structured proof gate still caught the missing video, so the backend policy layer is doing its job.
- Test: inspect rendered proof-repair prompts and add stronger, tool-specific instructions and/or a backend-generated proof checklist that is impossible to satisfy without a `.webm`.

### H3: Otto needs an owned app-server/proof-recorder helper instead of letting certifiers improvise browser proof

- Supports: the certifier repeatedly hit macOS `localhost:5000` AirPlay/ControlCenter behavior, used local desktop commands, and tried process cleanup despite prior safety work.
- Conflicts: previous dashboard task did eventually record a `.webm`, so the current ad hoc approach can work sometimes.
- Test: add or prototype a single helper that starts the app on a safe `127.0.0.1` port, records browser video, writes story-specific artifacts, and kills only its own child process.

## Experiments

- Confirmed from the run checkpoint that round 4 had `stories_passed=1`, no failing story ids, but `result="FAIL proof gate (1/1 stories passed)"`.
- Confirmed from `proof-of-work.json` that six story screenshots were mapped to `incident-workflow-actions-2026-04-30`, but no `.webm` was present and the evidence gate blocked pass.
- Confirmed from the narrative log that the certifier believed proof repair succeeded even though it did not collect the required video.
- Added a regression test with `max_certify_rounds=1` and two additional proof-repair attempts. Before the fix, the run failed after the product round cap even when a later proof-repair certifier call would provide valid media.

## Root Cause

The product-fix round budget and proof-repair evidence budget were coupled. That made code churn bounded, which is good, but it also meant a completed product could fail permanently when the first proof-repair certifier attempt missed required video.

## Fix

- Split the budgets in `run_certify_fix_loop`: product fixes still stop at `max_certify_rounds`, while proof-gate-only failures get a bounded `max_proof_repair_rounds` extension, defaulting to 2 and capped at 5.
- Extra proof-repair rounds pass the prior passing stories back into the certifier and do not dispatch the code-fix agent.
- Regression test: `uv run pytest -q tests/test_hardening.py -k 'certify_fix_loop_allows_extra_proof_repair_after_product_round_cap or certify_fix_loop_repairs_proof_gate_without_code_fix or certify_fix_loop_preserves_stories_when_proof_repair_is_evidence_only'`.

# OpsBoard Rerun Feature Certification Inefficiency

Date: 2026-05-01

## Observations

- Controlled rerun project: `/Users/yuxuan/otto-projects/opsboard-dogfood-rerun-20260430-232120`.
- Same config as the prior 4-hour dogfood: Claude Sonnet low for build/improve/fix, Claude Haiku low for certifier, standard split mode, queue concurrency 2, max certify rounds 4.
- Foundation completed in 669s and merged with risk-based verification in 196s.
- The parallel feature wave started correctly with concurrency 2, but after roughly 24 minutes neither of the first two feature tasks had landed and the full campaign was already trending past the 60-minute target.
- `dashboard-search-filters` reached certify round 6 before the run was stopped. Round 4 had product behavior passing but failed the browser proof gate; proof repair then wrote markdown "visual proof" files rather than actual `.webm`/PNG evidence.
- `incident-workflow-actions` spent 11m53s in its first certification round, including port conflict recovery, failed Agent tool schema attempts, Selenium/install attempts, Safari/osascript usage, and unsafe process-kill attempts that Otto blocked.
- Focused `otto improve feature <focus>` tasks were still using `certifier_mode="hillclimb"` even though the proof gate expected standard/thorough browser proof.
- Checkpoints showed repeated appended `## Proof Repair Focus` sections, which bloated and confused later proof-repair prompts.

## Hypotheses

### H1: Focused feature work is certified with the wrong evaluator mode (ROOT HYPOTHESIS)

- Supports: `otto improve feature` hardcoded `certifier_mode="hillclimb"`; hillclimb prompt treats screenshots/clips as optional product-advisor evidence, while the proof gate blocks standard web UI passes without real browser media.
- Conflicts: foundation build used standard certification and completed in acceptable time with only one proof repair.
- Test: change focused feature mode resolution to `standard` when focus text is present, while preserving `hillclimb` for unfocused feature discovery.

### H2: Proof-repair prompts accumulate duplicate focus sections

- Supports: resumed checkpoints contained repeated `## Proof Repair Focus` sections.
- Conflicts: duplicate text alone does not explain every product miss, but it amplifies proof repair confusion.
- Test: strip an existing proof-repair section before appending a fresh one and assert the second proof-repair call has exactly one marker.

### H3: Proof repair default budget is too forgiving for the pressure-test target

- Supports: repeated proof-only rounds can consume minutes without changing product code, making a sub-hour campaign impossible when a certifier misses media.
- Conflicts: at least one proof-repair round is still needed for audit-grade evidence.
- Test: lower the default extra proof-repair budget to one bounded attempt while leaving `max_proof_repair_rounds` configurable.

## Experiments

- Added `_resolve_feature_certifier_mode`: focused feature requests resolve to `standard`; blank feature discovery remains `hillclimb`.
- Added `_base_focus_without_proof_repair` so proof-repair focus is replaced, not stacked.
- Reduced the default extra proof-repair budget from two to one.
- Strengthened certifier prompts to choose a high free test port and forbid broad kill/Safari/OS scripting for port recovery.

## Root Cause

Focused feature tasks were evaluated as open-ended hillclimb/product-advisor runs but then judged by standard proof gates. That mismatch caused proof-gate retries and long tool thrash instead of a tight contract verification of the user-requested feature.

## Fix

- `otto improve feature` now uses standard certification for explicit focus text and hillclimb only for unfocused feature discovery.
- Proof-repair focus is de-duplicated before each repair call.
- Default proof-repair retries are capped to one extra evidence-only round unless config opts into more.
- Certifier prompts now steer port recovery toward high free ports and away from unsafe process cleanup or desktop automation.

## Verification

- `uv run pytest -q tests/test_hardening.py::TestHistoryWrites::test_certify_fix_loop_allows_extra_proof_repair_after_product_round_cap tests/test_hardening.py::TestHistoryWrites::test_certify_fix_loop_defaults_to_one_proof_repair_round tests/test_hardening.py::TestImproveCLIHardening::test_focused_feature_improve_uses_standard_certifier`
- `uv run pytest -q tests/test_hardening.py::TestHistoryWrites tests/test_hardening.py::TestImproveCLIHardening tests/test_agent_safety.py`
- `uv run python scripts/test_tiers.py smoke`

## Follow-up Finding

The first clean rerun after these fixes showed `foundation-platform` entering
certify round 3 even though the default proof-repair budget was intended to
allow only one evidence-only retry. The bug was in the limiter: it compared the
absolute round number to `max_certify_rounds + max_proof_repair_rounds`, so a
task with `--rounds 4` could still take several proof-only retries.

Fix: count completed proof-repair attempts directly. A proof-gate failure on a
proof-repair round now stops when `proof_repair_attempts >=
max_proof_repair_rounds`, regardless of the product round budget.

Additional regression:

- `uv run pytest -q tests/test_hardening.py::TestHistoryWrites::test_certify_fix_loop_counts_default_proof_repair_attempts_not_total_rounds`

# OpsBoard Rerun Dirty Fix Commit Failure

Date: 2026-05-01

## Observations

- Controlled rerun project: `/Users/yuxuan/otto-projects/opsboard-dogfood-rerun3-20260501-004010`.
- Foundation completed in about 10m36s and merged with risk-based verification in about 3m31s.
- The first feature wave correctly ran focused feature tasks in `standard` certifier mode with queue concurrency 2.
- `incident-workflow-actions` completed after one product fix and one final certification round in about 14.7 minutes.
- `dashboard-search-filters` failed round 1, ran fix round 1, then reached product-pass/proof-repair-pass state, but the queue task failed when Otto attempted another fix with a dirty worktree.
- The dashboard feature branch HEAD stayed equal to main after "fix round 1"; the worktree contained modified product files and a new `opsboard/templates/dashboard.html`.
- The round-003 manifest recorded `"action": "fix round 1"`, `"passed": true`, and identical `commit_before` / `commit_after` SHAs. The generated summary said `(no changes)` despite real dirty product edits.

## Hypotheses

### H1: Successful fix agents are trusted to commit their own changes, but this is not enforced (ROOT HYPOTHESIS)

- Supports: the fix agent returned success, changed product files, and did not advance HEAD; the pipeline then recorded the fix as done with no commit.
- Conflicts: some agents do commit successfully, so this only appears when provider behavior deviates from the prompt.
- Test: add a regression where the mocked fix agent writes a file without committing; the split certify-fix loop should either commit the change before continuing or fail before certification can pass a dirty tree.

### H2: The certifier writes product changes during proof repair

- Supports: dirty files were present after later certification rounds.
- Conflicts: the files match dashboard product implementation and tests, not proof artifacts; the round-003 manifest is the first point where the branch should have advanced.
- Test: inspect branch HEAD and dirty paths before and after the mocked fix phase.

### H3: The dirty-state guard is too strict after proof repair

- Supports: it blocked the run.
- Conflicts: blocking was correct; the bug was that Otto allowed earlier phases to proceed after an uncommitted product fix.
- Test: preserve the guard and fix the earlier commit boundary.

## Experiments

- Confirmed the dashboard feature branch had dirty product/test files and no fix commit after the successful fix phase.
- Confirmed `run_certify_fix_loop` computes `fix_commit_sha` by comparing HEAD before/after `build_agentic_v3`, but does not enforce a commit if a successful fix leaves tracked/untracked product files dirty.

## Root Cause

The split certify-fix pipeline relied on the fix agent's prompt-level instruction to commit changes. When the agent returned success without committing, Otto certified a dirty tree and later failed recovery from the same dirty state.

## Fix

- Add a pipeline-level post-fix commit guard for successful fix phases: if the repo was clean before the fix and the agent left committable product changes, Otto stages and commits those changes before the next certification round.
- Keep runtime/output paths such as `otto_logs/`, `.otto-*`, `.venv/`, `output/`, and `test-results/` out of the automatic product commit.

## Verification

- `uv run pytest -q tests/test_hardening.py::TestHistoryWrites::test_certify_fix_loop_commits_successful_dirty_fix_before_recertifying tests/test_hardening.py::TestHistoryWrites::test_certify_fix_loop_counts_default_proof_repair_attempts_not_total_rounds tests/test_hardening.py::TestImproveCLIHardening::test_focused_feature_improve_uses_standard_certifier`
- `uv run pytest -q tests/test_hardening.py::TestHistoryWrites tests/test_hardening.py::TestImproveCLIHardening tests/test_agent_safety.py`
- `uv run python scripts/test_tiers.py smoke`

# OpsBoard Rerun Storyless Merge Certifier Failure

Date: 2026-05-01

## Observations

- Controlled rerun project: `/Users/yuxuan/otto-projects/opsboard-dogfood-rerun4-20260501-012742`.
- Foundation completed cleanly in 503.9s: 4:49 build and 3:34 certification, with no proof-repair round.
- `otto merge --fast --verify risk-based foundation-platform` advanced `main` from `119420f` to `001f485`, collected command evidence, browser screenshots, and `recording.webm`, then reported merge verification failed.
- The merge certifier narrative ended with `VERDICT: PASS` and a positive diagnosis, but emitted no `STORY_RESULT:` lines. `proof-of-work.json` therefore had `stories_tested=0`, `stories_passed=0`, `outcome=failed`, and a non-blocking evidence gate.
- The certifier also attempted `pkill -f "uvicorn opsboard.main"`; Otto blocked it and the cleanup guard killed the owned dev server afterward.

## Hypotheses

### H1: A storyless `VERDICT: PASS` is treated as a normal failed certification instead of malformed output (ROOT HYPOTHESIS)

- Supports: the certifier completed with valid-looking evidence and a PASS verdict, but zero structured stories. Merge verification had no retry path for this parser-contract failure.
- Conflicts: failing the merge is safer than accepting a storyless pass, but it is not the right recovery behavior because the agent did the verification work and only omitted machine-readable markers.
- Test: make `run_agentic_certifier` raise `MalformedCertifierOutputError` when a verdict is present without `STORY_RESULT`, and make post-merge verification retry once with an explicit output-contract repair focus.

### H2: The merge prompt does not list the required story IDs clearly enough

- Supports: the agent wrote `STORY_EVIDENCE_*` blocks for named stories but did not map them to `STORY_RESULT`.
- Conflicts: the prompt already contains a required story list and marker format; provider compliance can still drift.
- Test: on malformed retry, keep the same story list and add an explicit marker-only correction.

### H3: Merge proof-gate logic incorrectly blocks command/API-style evidence

- Supports: the merge failed despite evidence.
- Conflicts: the evidence gate was `not_applicable` and `blocks_pass=false`; the failure was zero structured story results.
- Test: inspect `evidence_gate` and `stories_tested` in the proof JSON.

## Experiments

- Confirmed the merge proof JSON had zero stories but `evidence_gate.blocks_pass=false`.
- Confirmed the narrative log had no `STORY_RESULT` markers but did have `VERDICT: PASS`.
- Added a merge-orchestrator regression where the first certifier call raises a malformed storyless-verdict error and the second structured response passes.

## Root Cause

Otto correctly refused to accept a certifier PASS without story results, but treated that parser-contract failure as a final merge failure instead of retrying the certifier once with a stricter output contract.

## Fix

- `run_agentic_certifier` now raises `MalformedCertifierOutputError` when a response emits `VERDICT` but no `STORY_RESULT` markers.
- Post-merge verification catches that malformed-output error and retries once with an `Output Contract Repair` focus that requires one structured `STORY_RESULT` per story.

## Verification

- `uv run pytest -q tests/test_hardening.py::TestSpecTimeoutTolerance::test_certifier_raises_on_verdict_without_story_results tests/test_merge_orchestrator.py::test_post_merge_verification_retries_storyless_verdict_once tests/test_merge_orchestrator.py::test_post_merge_verification_repairs_proof_gate_without_remerge`
- `uv run pytest -q tests/test_hardening.py::TestHistoryWrites tests/test_hardening.py::TestSpecTimeoutTolerance tests/test_hardening.py::TestImproveCLIHardening tests/test_agent_safety.py tests/test_merge_orchestrator.py::test_post_merge_verification_retries_storyless_verdict_once tests/test_merge_orchestrator.py::test_post_merge_verification_repairs_proof_gate_without_remerge tests/test_merge_orchestrator.py::test_post_merge_verification_full_verify_preserves_merge_context_flag tests/test_merge_orchestrator.py::test_post_merge_verification_blocks_human_flag_even_if_certifier_verdict_passes`

# OpsBoard Rerun Merge Proof Retry Loop

Date: 2026-05-01

## Observations

- Controlled rerun project: `/Users/yuxuan/otto-projects/opsboard-dogfood-rerun4-20260501-012742`.
- Final product merge landed successfully at `2657130`, but post-merge verification repeatedly failed after all stories passed.
- Failed proof packets showed `agent_outcome=passed`, 44/44 story passes, and product-ready diagnoses, but proof gate failures:
  - 0/44 story evidence extracted even though `narrative.log` contained `STORY_EVIDENCE_START` blocks.
  - Non-file audit stories were marked as needing file/download validation.
  - UI stories were forced into fresh story-specific screenshots during merge verification, causing repeated proof rounds.
- Certifier also attempted broad `pkill` cleanup and wrote a temporary `verify_merge.py` helper into the product repo; Otto blocked/cleaned both, but those attempts cost time.

## Hypotheses

### H1: Proof generation only parses final agent text, not the full narrative stream (ROOT HYPOTHESIS)

- Supports: `narrative.log` had 44 evidence blocks, but `proof-of-work.json` had `with_evidence=0`.
- Conflicts: normal final responses sometimes include the evidence blocks, so this only appears when the provider emits evidence before the final answer.
- Test: parse timestamp/glyph-prefixed narrative markers and confirm 44/44 evidence blocks attach.

### H2: File/export detection treats broad words as file artifacts

- Supports: `audit-events` was marked as file validation required because its observed steps mentioned an audit export, despite the story verifying audit rows/actions, not a downloadable file.
- Test: audit-event story with `examined audit export` must not require file validation unless file-like tokens such as CSV/PDF/download/content-disposition are present.

### H3: Merge verification is using the wrong proof standard

- Supports: merge verification repeated full certifier rounds to collect fresh product-demo visuals. Source feature tasks already own product proof; merge verification should prove integration and regressions.
- Test: a post-merge integration intent with DOM stories and structured evidence should not require a fresh browser video.

## Root Cause

Post-merge verification was conflating three duties: source-task product proof, merge integration verification, and proof repair. That made successful product behavior look failed when proof evidence was recorded in a different stream or when the merge certifier did not collect redundant visual artifacts.

## Fix

- Parse certifier narrative logs alongside final agent text when evidence markers are present.
- Normalize timestamp/glyph prefixes in marker and evidence parsing.
- Tighten file/download story detection and accept concrete CSV row-order evidence.
- Relax merge verification proof policy: post-merge checks rely on source proof packets plus structured integration evidence instead of demanding a fresh product-demo video for each UI story.

## Verification

- `uv run pytest -q tests/test_hardening.py::test_parser_extracts_prefixed_story_evidence_blocks tests/test_hardening.py::test_parser_extracts_timestamped_narrative_story_evidence_blocks tests/test_hardening.py::test_parser_preserves_fenced_code_inside_story_evidence`
- `uv run pytest -q tests/test_certifier_stories.py::test_pow_merge_verification_does_not_require_fresh_ui_demo_video tests/test_certifier_stories.py::test_pow_file_validation_accepts_csv_row_order_evidence tests/test_certifier_stories.py::test_pow_demo_evidence_accepts_walkthrough_with_story_text_evidence tests/test_certifier_stories.py::test_pow_generic_recording_does_not_cover_unvisualized_ui_story`
- Final live merge verification: `otto merge-verify merge-1777628934-92022-af8fd2c9 --verify risk-based` passed in one round, 44/44 stories, 44/44 evidence, proof gate pass.

# Micro Twitter True WebTest Cross-Group Check Merge Ordering

Date: 2026-05-07

## Observations

- True WebTest run seed `20260509` built Micro Twitter with `group_concurrent=3`.
- Real group execution overlapped: `composer-authoring`, `timeline-actions`, and `feed-search-filter` all emitted `group.execution.started` at `2026-05-07T10:43:21Z`.
- `quality-persistence-polish` owns `tests/browser/main-workflow.spec.*` and depends on composer, timeline, and search.
- During `timeline-actions` merge, Otto ran the cross-group browser journey command `npm run test:browser -- tests/browser/main-workflow.spec.ts` before `quality-persistence-polish` landed.
- The merge blocked on `Cannot find module .../tests/browser/main-workflow.spec.ts`, even though the missing test file is owned by a later group and was not expected to exist yet.

## Hypotheses

### H1: Merge queue runs every cross-group check after every group, regardless of whether the check's own runner files have landed (ROOT HYPOTHESIS)

- Supports: `otto/merge_queue.py` calls `run_checks(list(spec.cross_group_checks), ...)` inside every per-group merge verification.
- Supports: the missing `main-workflow.spec.ts` path matches the later `quality-persistence-polish` owned path `tests/browser/main-workflow.spec.*`.
- Conflicts: cross-group checks that do not depend on future-owned files should still run early.
- Test: create two git-backed groups where group 2 owns the cross-check runner file; group 1 should land without running that future-owned check, and group 2 should run it.

### H2: The spec compiler assigned the integrated browser journey to the wrong owner

- Supports: cross-group checks are global, not attached to a group.
- Conflicts: the spec explicitly puts `tests/browser/main-workflow.spec.*` in `quality-persistence-polish` owned paths, which is a reasonable integration group owner.
- Test: inspect the generated spec and compare command path references to group owned paths.

### H3: The generated browser script should tolerate missing requested spec files

- Supports: `npm run test:browser -- missing-file` could choose to skip missing files.
- Conflicts: silently skipping an explicitly requested browser journey would create false green runs and hide real missing test bugs once all groups should be landed.
- Test: keep missing runner files as a failure when no unlanded group owns the path.

## Experiments

- Confirmed the generated `spec.json` cross-group command references `tests/browser/main-workflow.spec.ts`.
- Confirmed `quality-persistence-polish` owns `tests/browser/main-workflow.spec.*`.
- Confirmed current merge code runs the full `spec.cross_group_checks` list inside every group merge verification.

## Root Cause

Per-group merge verification treated all cross-group checks as immediately runnable, even when a check's declared runner path belonged to a later group in the dependency graph.

## Fix

- Defer cross-group checks whose explicit path references match owned paths of unlanded groups.
- Still run global checks without future-owned path references at each merge.
- Run the deferred check when its owning group is included in the integrated post-merge state.

# Micro Twitter True WebTest Codex Post-Result Hang

Date: 2026-05-07

## Observations

- During the same Micro Twitter True WebTest, merge repair agents emitted terminal Codex `result` / `phase_end` records but their `codex exec` subprocesses stayed alive.
- Otto did not advance until the exact stale process groups were manually terminated.
- The provider transcript already contained `turn.completed` and a successful result before the process stalled.

## Hypotheses

### H1: Otto's Codex adapter waits for process/stdout EOF after terminal `turn.completed` (ROOT HYPOTHESIS)

- Supports: `_query_codex` yields `ResultMessage` on `turn.completed` but keeps reading stdout until EOF and then awaits `process.wait()`.
- Supports: manual termination of the already-terminal Codex process immediately allowed Otto to continue.
- Conflicts: short Codex runs often exit immediately after `turn.completed`, so the bug only appears when the CLI process lingers.
- Test: simulate a Codex process that emits `turn.completed` and never exits; `query()` should return the result and terminate the provider process.

### H2: Merge queue repair handling ignores successful provider output

- Supports: the merge queue marked one repair as agent error after a process interruption.
- Conflicts: the direct blocker was the awaited provider process; after manual termination, merge queue resumed with the parsed provider output.
- Test: fix the provider wait path first and rerun the live merge path.

### H3: The Codex CLI itself should always exit immediately after `turn.completed`

- Supports: many runs do exit cleanly.
- Conflicts: Otto must not let a provider lifecycle quirk wedge the product queue after terminal output.
- Test: adapter-level post-result timeout and cleanup.

## Root Cause

The Codex provider adapter treated process EOF as the terminal condition even though `turn.completed` is already the terminal JSON event for Otto's normalized stream.

## Fix

- Treat Codex `turn.completed` as terminal.
- Give the process a short grace period to exit, then let the existing provider cleanup terminate it.
- Add a regression where a fake Codex process emits `turn.completed` but never exits.

# True WebTest Long-Run False Progress Audit

Date: 2026-05-07

## Observations

- Several recent Mission Control queue runners stayed alive for 13-16 hours after their only queued task had already reached terminal failed/completed state.
- The actual child runs were much shorter: one Microfeed run failed after about 13 minutes, and a later polished Micro Twitter run failed after about 34 minutes.
- Older browser evidence shows the true-web harness could keep probing a run that was already terminal, because Mission Control sometimes kept terminal queue rows in `live.items`.
- Shared wait helpers checked only `history.items`, so terminal rows retained in `live.items` could force timeout-length waits.

## Hypotheses

### H1: Terminal-state detection is section-specific instead of user-visible-state-specific (ROOT HYPOTHESIS)

- Supports: `_wait_for_terminal` only scanned history rows, while Mission Control can retain terminal queue rows in the live section.
- Supports: W1 had a separate helper that already accepted terminal live rows, suggesting this bug existed in the shared path.
- Conflicts: some scenarios use fresh throwaway projects and terminal rows often move to history promptly.
- Test: a live queue row with `status=failed` and no history row should terminate `_wait_for_terminal` immediately.

### H2: Stale queue-runner processes are being mistaken for active E2E progress

- Supports: queue runner state heartbeats continued long after task `finished_at`.
- Conflicts: a long-lived Mission Control queue runner is valid for a real product server; the bug is harness interpretation/cleanup, not necessarily the product default.
- Test: true-web teardown should record and fail if project-referencing processes survive SIGTERM/SIGKILL.

### H3: Long waits have no visible-progress budget

- Supports: a real user would not accept hours of no visible progress, even if a provider process is still alive.
- Conflicts: some provider calls can be legitimately quiet for minutes.
- Test: W1 should treat prolonged absence of Mission Control-visible row changes as a user-visible stall, separate from total build timeout.

## Root Cause

The true-web harness mixed real build runtime with stale Mission Control/process liveness: shared waits did not treat terminal live rows as terminal, queued-work counting included stale live rows, and long waits had no independent visible-progress stall guard.

## Fix

- Read terminal outcome from all Mission Control rows, not just history rows.
- Infer terminal outcome from terminal status when `terminal_outcome` is absent.
- Exclude terminal live/landing rows from queued-work counts.
- Add a W1 visible-progress idle guard so true-web fails on user-visible stalls instead of passively waiting to the build timeout.
- Make teardown process leaks visible in `teardown.json` and fail if project processes survive cleanup.

# True WebTest Polished Micro Twitter Stall Audit

Date: 2026-05-07

## Observations

- The polished Micro Twitter run used real group concurrency: after the foundation group, composer, timeline, and search groups started together.
- The three sibling groups wrote real feature files and Playwright journeys into their isolated worktrees, and those browser journeys passed.
- Their unit checks failed with `No test files found` for commands such as `npm run test -- --run src/features/composer/*.test.tsx`.
- The files did exist, for example `src/features/composer/PostComposer.test.tsx`; the wildcard was passed as a literal structured subprocess argument, so Vitest did not expand it.
- Current `main` already contains the root fix in `otto/checks.py`: structured check commands expand path globs before running without a shell. Replaying the exact archived composer check against the failed worktree now passes.
- The audit agent also bulk-read generated assets and `node_modules` listings, creating huge provider events and a noisy `audit agent crashed` result even though deterministic evidence was already enough to block.

## Hypotheses

### H1: Structured check commands need path-glob expansion before subprocess execution (ROOT HYPOTHESIS)

- Supports: the failed command contained `*.test.tsx`; the matching file existed; replay with current glob expansion passes.
- Conflicts: shell-based manual runs would expand the wildcard, but Otto intentionally avoids shell execution for structured checks.
- Test: run `RepoTestCheck(command=("python", "-c", code, "src/features/search/*.test.tsx"))` and assert the subprocess receives concrete file paths.

### H2: No-progress detection hid the real check-runner issue

- Supports: after two identical failed check attempts, Otto reported `no progress` even though useful uncommitted feature work existed in the worktrees.
- Conflicts: the no-progress guard is still useful for genuine retry loops; the misleading part was the underlying false check failure.
- Test: fix command glob expansion first; the same group should pass and commit instead of reaching no-progress.

### H3: Audit prompt guardrails were too soft around generated assets

- Supports: the prompt warned against bulk session-log sweeps, but the agent still read generated bundle output and `node_modules` listings.
- Conflicts: some generated artifacts can be useful when specifically referenced by evidence.
- Test: prompt and evidence packet should explicitly forbid broad reads of `node_modules/**`, `dist/assets/**`, `coverage/**`, and `test-results/**` unless named evidence points there; Codex adapter should compact huge provider-error lines.

## Root Cause

Two independent issues amplified the long run: check execution used structured subprocesses without shell glob expansion in the failed run, causing false `No test files found` failures; then audit/provider logging preserved huge raw command output when the audit agent inspected generated assets, turning a clear deterministic block into noisy crash evidence.

## Fix

- Verified the existing current-head fix for `RepoTestCheck` path-glob expansion against the archived failed composer worktree.
- Tightened the audit evidence packet and prompt to avoid broad reads of `node_modules`, generated bundles, coverage, and test-results.
- Compacted Codex provider nonzero-exit error summaries and truncated huge command-output log blocks so one bad inspection cannot flood Mission Control artifacts or follow-on crash details.

---

# Modular Decomposition Field-Test Debug

Date: 2026-05-15T02:05:47Z

## Observations

- Round 2 04 and 05 both had root `decomposition=emit`, three children, and all child verdicts `pass`.
- 04 final failure was not a source merge conflict. It was clean-deploy preflight:
  - `clean_deploy_port_busy`: declared port 19301 already bound.
  - `clean_deploy_start_failed`: `start.sh exited 127`; output included `python: command not found`.
- 05 final failure was clean-deploy preflight:
  - `clean_deploy_start_failed`: `http.server` raised `OSError: [Errno 48] Address already in use`.
- In current code, `_run_integration_smoke_preflight()` records blocking smoke issues but does not invoke `PreflightRepairController`.
- Existing `PreflightRepairController` defaults unknown blocking preflight issues to an agent and auto-fixes `port_busy`.
- Existing preflight repair agent dispatch does not runner-commit successful repair edits.
- Archived 04/05 repositories show all `i2p/build/*` branch tips are ancestors of `main`; 04 FE branch has no unique FE commit because the agent found frontend already complete.

## Hypotheses

### H1: Clean-deploy smoke failures bypass repair (ROOT HYPOTHESIS)

- Supports: blocking `clean_deploy_start_failed` appears in logs; code only passes it to integration Lead context and downgrades after repeat smoke failure.
- Supports: the repair controller would classify `clean_deploy_start_failed` as `agent`, but it is not called.
- Conflicts: a normal integration Lead could theoretically fix it, but Round 2 shows it did not.
- Test: wrap smoke preflight in repair loop and simulate first smoke failing then repair agent changing `start.sh`; assert repair result is recorded and final integration continues.

### H2: Port cleanup is not available when ports become known

- Supports: pre-run cleanup happens before `CHARTER.md`/`start.sh` exist, so declared ports are unknown.
- Supports: both 04 and 05 encountered stale port symptoms.
- Conflicts: some address-in-use failures can be caused by `start.sh` starting duplicate servers, not external zombies.
- Test: port-busy smoke issue should run cleanup and rerun smoke; cleanup should not kill unrelated listeners.

### H3: Branch propagation lacks a graph-level invariant

- Supports: older v6e bug class; existing tests do not cover all graph children through `_process_children`.
- Supports: confusing "FE branch missing" report came from lack of explicit branch ancestry reporting.
- Conflicts: archived 04/05 child branch tips are ancestors of main.
- Test: run `_process_children()` with three pass children and assert every pass child branch tip is an ancestor of main.

## Experiments

- Verified `git merge-base --is-ancestor` for every archived 04 and 05 `i2p/build/*` branch against `main`; all returned yes.
- Inspected 04 FE branch `i2p/build/v5-6825f5f82ade`; its tip is a merge commit of the architect branch and no unique frontend commit.
- Inspected `_run_integration_smoke_preflight`, root integration, subtree integration, and `_run_preflight_repair_agent`; confirmed no smoke repair wrapper or repair commit path exists.

## Root Cause

The modular path had a repair loop, but the load-bearing clean-deploy smoke gate was outside it. Blocking `start.sh` and port-busy failures were serialized as context, then allowed to become terminal `merge_blocked` after a repeat smoke. Successful focused preflight repairs also lacked a runner-managed commit path, so even when repair edits were made they were not guaranteed to propagate through integration branches.

## Fix Plan

- Add an async smoke-preflight repair wrapper around `smoke_clean_deploy()` in v5 integration paths.
- Commit successful focused preflight repair edits with `commit_integration_worktree()`.
- Let `clean_deploy_start_failed` default to agent repair; keep only tiny deterministic shortcuts such as port cleanup.
- Tighten port cleanup to kill only project/Otto-owned listeners.
- Add graph-level branch ancestry checks/tests for all direct children after propagation.
