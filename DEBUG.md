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
