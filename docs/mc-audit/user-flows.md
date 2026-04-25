# Mission Control — User Flow Catalog (Phase 1C)

**Source:** `plan-mc-audit.md` § Phase 1 / 1C. 40 flows, expanded with concrete pre-state, steps, expected outcome, and edge cases. Each flow is one Playwright test scope (see Phase 3C `t01–t40`).

**Conventions:**
- "Pre-state" = on-disk fixture state + UI state at flow start (after refresh / settle).
- "Steps" = literal user actions; selectors below reference real `data-testid`s found in `otto/web/client/src/App.tsx`.
- "Expected outcome" = ONE sentence the test asserts on. Sub-assertions live as edge cases.
- "Edge cases" = 1–3 named sub-cases worth a parametrize / sibling test.
- Flows that overlap with `scripts/e2e_web_mission_control.py` reference the existing scenario name in parentheses.

---

## Project / launcher

### Flow 1 — Cold start, no project

**Pre-state:** Fixture-isolated managed-projects root (no entries on disk); `projectsState.launcher_enabled = true`; no `project` selected; `data` is null so the launcher shell renders (App.tsx:574).
**Steps:**
1. Navigate to `http://127.0.0.1:<port>/`.
2. Wait for the launcher heading "Project Launcher" to be visible.
3. Observe the empty project list panel and the "Create project" form.
**Expected outcome:** Launcher shell renders with the "Create project" form auto-focused, the managed-root path visible, and the project list shows the `launcher-empty` "No managed projects yet." message — no task board, no toolbar, no sidebar `New job` button. (Existing harness: `project-launcher`.)
**Edge cases worth covering in tests:**
- Managed-root path string is the fixture-isolated path (no leak of `~/otto-projects`).
- Submit with empty name → status text "Project name is required." appears, no network call.
- Refresh button click triggers `/api/projects` and re-renders without remount.

### Flow 2 — Launch invalid path

**Pre-state:** Launcher visible. Server is configured so that `POST /api/projects/select` rejects non-existent / non-directory paths with a 4xx + error JSON.
**Steps:**
1. From the launcher, drive `selectManagedProject` with a path that does not exist on disk (via direct API call mirroring what an attacker / stale link would do, or by stubbing the project-row to point at a missing path).
2. Wait for the response.
**Expected outcome:** The launcher remains mounted, the project list still shows the prior set, and `launcher-status` (or toast) surfaces the server error message verbatim — no partial mount of the task board, no console error.
**Edge cases:**
- Path under the projects root but no `.git/`.
- Path is a regular file, not a directory.
- Path is empty string / whitespace.

### Flow 3 — Launch duplicate project

**Pre-state:** Launcher visible; one managed project already selectable. The user double-clicks (or two parallel clicks fire) on the same row.
**Steps:**
1. Click `project-row` for project P.
2. Before the first promise resolves, click the same row again.
**Expected outcome:** Only one `/api/projects/select` request is in flight at a time (subsequent clicks are blocked by the `pending` guard at App.tsx:825), and the final mounted state is a single task board for P with no doubled events / runs.
**Edge cases:**
- Two different rows clicked back-to-back — last write wins, no orphan request.
- Click row, then refresh — no double-mount.

### Flow 4 — Launch outside repo root / non-git

**Pre-state:** Managed-projects root contains a folder that is NOT a git repo (no `.git/`). Server-side validation rejects with a clear error.
**Steps:**
1. Click the non-git project row in the launcher.
2. Wait for the response.
**Expected outcome:** Launcher stays visible, the row's `launcher-status` (or toast) shows a human-readable "not a git repository" error, and no task board renders.
**Edge cases:**
- Folder has a `.git` file (gitlink) instead of `.git/` dir.
- Folder is a bare repo.
- Folder lives outside the managed root entirely.

### Flow 5 — Switch project

**Pre-state:** Task board is mounted on project A (data loaded, one selected run in URL `?run=...`).
**Steps:**
1. Click sidebar `switch-project-button` (App.tsx:613).
2. From the relaunched launcher, click the project-row for B.
3. Wait for the new task board to mount.
**Expected outcome:** The URL drops `?run=` and `view`, `selectedRunId` is null, the task board renders B's runs only (no leak of A's runs/events/inspector), and the toast `"Choose a project"` then `"Opened <B>"` (or equivalent) fires once. (Existing harness: indirectly via `project-launcher`.)
**Edge cases:**
- Switch while inspector is open — inspector closes and `selectedRunId` resets (App.tsx:556–571).
- Switch while a confirm dialog is open — dialog cleans up.
- Switch while `jobOpen` is true — dialog closes.

---

## Build/run lifecycle

### Flow 6 — Submit build job (happy path)

**Pre-state:** Clean project mounted; mission focus shows "Queue the first job" or "Idle"; sidebar `new-job-button` enabled. Watcher state is irrelevant for submission.
**Steps:**
1. Click `new-job-button`.
2. JobDialog opens with command=`build` selected.
3. Type intent text into the textarea.
4. Click `Queue job`.
**Expected outcome:** `POST /api/queue/build` fires once with the intent payload, the dialog closes, a success toast appears (e.g. `"queued <task-id>"`), and the next `/api/state` poll shows the task in the queued column of the task board. (Existing harness: `fresh-queue`.)
**Edge cases:**
- Submit button disabled until intent is non-empty (`submitDisabled`, App.tsx:2019).
- Server returns 4xx → toast severity=error, dialog stays open with status text.
- Submit with provider override (Codex) — payload includes provider field.

### Flow 7 — Submit improve / certify job

**Pre-state:** Same as Flow 6; a prior run exists so `improve` / `certify` are meaningful.
**Steps:**
1. Open JobDialog.
2. `job-command-select` → `improve`. Confirm `job-improve-mode-select` becomes visible with default `bugs`.
3. Switch to `feature` then `target`; confirm certification options collapse to the static label per `staticCertificationLabel` (App.tsx:2247).
4. Type intent, click `Queue job`.
5. Repeat with command=`certify`.
**Expected outcome:** `POST /api/queue/improve` (then `/certify`) each fire exactly once with the correct subcommand and certification policy in payload, dialog closes between submissions, and both tasks appear queued. (Existing harness: `job-submit-matrix`.)
**Edge cases:**
- Improve `bugs` default certification text reads "Inherit: thorough bug certification (improve default)".
- Certify command excludes the `skip` certification option (App.tsx:2196).
- Submitting `improve target` shows the static evaluation label, not the certification select.

### Flow 8 — Submit on dirty target

**Pre-state:** Project mounted with `project.dirty = true` (uncommitted changes in the worktree); JobDialog's target-guard renders the `target-dirty` class and the confirm checkbox.
**Steps:**
1. Open JobDialog.
2. Type intent.
3. Try to click `Queue job` without ticking the confirm checkbox.
4. Tick `target-project-confirm`.
5. Click `Queue job`.
**Expected outcome:** Submit is blocked while `targetConfirmed` is false (button disabled and `Confirm the dirty target project before queueing.` status appears on attempted submit), then once confirmed the POST fires and the task queues. (Existing harness: `dirty-blocked` covers the read side.)
**Edge cases:**
- Switching projects mid-dialog resets `targetConfirmed` to false (App.tsx:2021).
- Clean project never shows the checkbox.
- Confirm-then-uncheck disables submit again.

### Flow 9 — Submit with invalid input

**Pre-state:** JobDialog open, command=`build`.
**Steps:**
1. Try submit with empty intent → blocked locally.
2. Try submit with whitespace-only intent → also blocked (`intent.trim()` check, App.tsx:2033).
3. Paste a 50,000-character intent and submit.
4. Submit with intent containing emoji + RTL chars + null-byte-encoded text.
**Expected outcome:** Local validation rejects empty/whitespace before any network call; the server accepts oversized and unicode intents (or returns a 4xx surfaced as a toast — recorded per server behavior).
**Edge cases:**
- Task ID with shell metacharacters (`;`, `&`, `$(...)`).
- After-dependency referencing a non-existent task ID — server should reject; toast surfaces.
- Provider value not in the allow-list — server rejects.

### Flow 10 — Watch run live

**Pre-state:** A run is in-flight (queued task that the watcher has picked up); `data.live.items` includes it; events JSONL is being appended to.
**Steps:**
1. From the task board, click the in-flight task card.
2. RunDetail panel renders; click `open-logs-button` (or the `Logs` tab).
3. Wait through ≥2 polling intervals.
**Expected outcome:** The Logs pane line count increases monotonically across polls, the EventTimeline shows new entries, and no error toast appears — proving the polling loop populates the inspector live.
**Edge cases:**
- Switching tabs (Logs → Proof → Logs) does not reset the streamed text.
- Polling continues if the inspector is closed and reopened.
- Run completes mid-watch → status pill flips to terminal label without page reload.

### Flow 11 — Resume paused run

**Pre-state:** A run is in `paused` state with a checkpoint on disk; `legal_actions` includes the resume key (per `ActionBar`, App.tsx:1941).
**Steps:**
1. Select the paused run.
2. Open the "Advanced run actions" `details` element (or invoke via `review-next-action-button` if resume is the next action).
3. Click the resume action.
4. Confirm in the ConfirmDialog if one is shown.
**Expected outcome:** `POST /api/runs/{id}/actions/resume` fires once, the run's status transitions out of `paused`, and the action button disappears or transitions to the next-step affordance on the next poll.
**Edge cases:**
- Resume action disabled when `merge_blocked` or run already terminal.
- Resume on a run whose worktree was deleted → server returns 4xx; error toast surfaces.
- Repeated clicks during pending state are blocked (`pending` guard).

### Flow 12 — Cancel running run

**Pre-state:** A run is in `running` state; `legal_actions` exposes `cancel`.
**Steps:**
1. Select the running run.
2. Click the cancel action (advanced actions or review next-action).
3. ConfirmDialog appears; first click `Cancel` (the dismiss button) and verify dialog closes without action.
4. Reopen, click the danger confirm button.
**Expected outcome:** The first click dismisses without firing the API; the second click POSTs `/api/runs/{id}/actions/cancel`, the dialog closes, the run transitions to `cancelled` on the next poll, and a result-banner / toast confirms the action.
**Edge cases:**
- Escape key dismisses the confirm dialog (focus-trap behavior).
- Confirm button shows `"Working"` while pending (App.tsx:2289).
- Cancel on an already-terminal run is not offered as a legal action.

### Flow 13 — Retry failed run

**Pre-state:** A run is in `failed` state; `legal_actions` includes a retry / re-queue key.
**Steps:**
1. Select the failed run.
2. Click retry from the advanced action bar.
3. Confirm if prompted.
**Expected outcome:** A new task is queued whose intent matches the original run (verified via the queue task list), the new task has a distinct `queue_task_id` and `run_id`, and the failed run remains in history unchanged.
**Edge cases:**
- Retry on a run whose intent file is missing → server 4xx, surfaced inline.
- Retry preserves provider / certification policy from the prior run.
- Two retry clicks in quick succession queue exactly one new task.

### Flow 14 — Cleanup completed run

**Pre-state:** A completed (merged or cancelled) run is selected; `legal_actions` includes a `cleanup` key (worktree / branch removal).
**Steps:**
1. Open the advanced actions on the selected run.
2. Click cleanup.
3. ConfirmDialog appears with danger tone (`confirm.tone === "danger"`, App.tsx:2267).
4. Click the danger confirm button.
**Expected outcome:** `POST /api/runs/{id}/actions/cleanup` fires exactly once, the dialog uses the danger styling on the confirm button, on success the run's worktree-related fields disappear from `RunDetail`, and a result-banner reports cleanup succeeded. (Existing harness: `control-tour` exercises the cancel path.)
**Edge cases:**
- Cancel-from-confirm is non-destructive.
- Cleanup on a run that already has no worktree → no-op success.
- Pending state disables both the confirm and cancel buttons (App.tsx:2283).

### Flow 15 — Merge run (single + bulk)

**Pre-state:** At least one run is in `ready` state with `merge_blocked = false`; for the bulk path, `landing.counts.ready >= 2`.
**Steps:**
1. Single: click the run, hit `review-next-action-button` (label "Land"/"Merge") → confirm.
2. Bulk: from MissionFocus, click `Land all ready` → confirm.
3. For both, observe the post-merge state.
**Expected outcome:** Single merge POSTs `/api/runs/{id}/actions/merge` and bulk merge POSTs `/api/actions/merge-all`; in both cases the affected runs transition to `merged` on the next poll, the success banner shows the per-row outcome list, and the readiness counters decrement. (Existing harness: `ready-land`, `bulk-land`.)
**Edge cases:**
- Bulk-merge with one row failing → result banner lists per-row success/failure, no rollback.
- Merge button disabled when `landing.merge_blocked` (e.g., dirty target) — title attribute explains why (App.tsx:1951).
- Cancel from the confirm dialog leaves all runs in `ready`. (Existing harness: `control-tour`.)

---

## Read paths

### Flow 16 — Browse run history

**Pre-state:** Project mounted with ≥30 historical runs in `cross-sessions/history.jsonl`; diagnostics view's `History` panel renders the rows.
**Steps:**
1. Switch to Diagnostics tab.
2. Scroll the History panel.
3. Click any history row to select it.
**Expected outcome:** The full set of `data.history.items` renders in document order without virtualization regressions, clicking a row sets `selectedRunId` and routes to that run's detail without changing view mode.
**Edge cases:**
- 200+ rows (R14 fixture) renders in <1s and remains scrollable.
- Sorting / row order matches `history.jsonl` ordering.
- A run that no longer has session-dir data still renders the history row (gracefully empty detail).

### Flow 17 — Filter / search / no-match empty state

**Pre-state:** Task board has runs spanning multiple types and outcomes.
**Steps:**
1. From the toolbar, set `filter-type-select` to a value that excludes all current rows.
2. Set `filter-outcome-select` similarly.
3. Confirm the empty-state panel renders.
4. Click the toolbar's "Clear filters" button.
**Expected outcome:** Filtered state shows the no-match empty copy (instead of an empty board with no explanation), and clearing returns the full set without remount. (Existing harness: `control-tour` covers the filter+clear loop.)
**Edge cases:**
- Filter combination that matches zero history rows → diagnostics History panel also shows empty copy.
- Filter persists across `Refresh` clicks but resets on project switch.
- "Clear filters" returns to `defaultFilters` exactly (no stale type/outcome).

### Flow 18 — Open run detail (inspector tab routing)

**Pre-state:** A run with proof report, diff, logs, and ≥2 artifacts.
**Steps:**
1. Select the run.
2. Click `open-proof-button` → inspector opens on Proof tab.
3. Click the `Logs` tab → switches to Logs.
4. Click `Diff` → switches to Diff.
5. Click `Artifacts` → switches to Artifacts.
6. Press Escape.
**Expected outcome:** Each tab click swaps the body content (Proof/Logs/Diff/Artifacts), the active tab gets `aria-selected=true`, and Escape closes the inspector via the dialog focus hook (`useDialogFocus`). (Existing harness: `long-log-layout` covers proof/logs/artifacts.)
**Edge cases:**
- Diff tab disabled when `canShowDiff(detail)` is false (App.tsx:1547).
- Switching to Artifacts when none exist shows "No artifacts."
- Reopening inspector remembers the last tab? (Verify against current behavior — App.tsx:486 resets to `proof`. Document mismatch as finding if expected was sticky.)

### Flow 19 — Diff viewer

**Pre-state:** A run with a multi-file diff (≥3 files) and one large file (>20k lines for truncation testing).
**Steps:**
1. Select the run, open Diff tab.
2. Click each entry in `diff-file-list`.
3. Inspect the truncation banner on the large file.
**Expected outcome:** Selecting a file in `diff-file-list` updates `diff-selected-file` heading and the `diff-pane` body text in <100ms with no network call (sections are computed client-side from the cached diff response).
**Edge cases:**
- Empty diff (no changes) → "empty diff" indicator, file list hidden (App.tsx:1745).
- `diff.error` set → `diff-error` block surfaces the error text.
- `diff.truncated = true` → toolbar shows "truncated" suffix.

### Flow 20 — Proof drawers

**Pre-state:** A completed run with `review_packet.checks`, `changes.files`, and `evidence` all populated.
**Steps:**
1. Select the run.
2. In the RunDetail review packet, expand the `Checks` drawer, then collapse.
3. Expand `Changed files`, then collapse.
4. Expand `Evidence`, click an artifact button.
**Expected outcome:** Each drawer (`<details>` element) toggles independently, the Evidence artifact click loads `/api/runs/{id}/artifacts/{i}/content` exactly once and routes to the inspector Artifacts tab via `onLoadArtifact`. (Existing harness: `long-log-layout`.)
**Edge cases:**
- `attentionChecks > 0` → Checks drawer is `defaultOpen` (App.tsx:1787).
- Evidence artifact with `exists = false` → button disabled and labeled "missing".
- Drawer state persists across re-renders triggered by polling.

### Flow 21 — Artifact viewer

**Pre-state:** A run with one text artifact, one binary artifact, one >20k-char artifact, and one missing artifact reference.
**Steps:**
1. Select the run, open the Artifacts tab.
2. Click each artifact button.
3. Click "Back to artifacts".
**Expected outcome:** Text artifacts render in the `<pre>` block within the inspector, binary artifacts surface a non-readable affordance (or server-side rejection), large content shows the "(truncated)" indicator (App.tsx:1978), and the missing artifact button is disabled.
**Edge cases:**
- Text artifact with log-like extension renders via `renderLogText` (line decoration).
- 10MB log → server caps at the configured byte limit; UI shows truncated marker.
- Missing artifact (`exists = false`) → button disabled, no network call on click.

---

## Diagnostics / watcher

### Flow 22 — Diagnostics view

**Pre-state:** Project mounted with non-empty `runtime`, `command_backlog`, and at least one malformed history row recorded.
**Steps:**
1. Click the `diagnostics-tab`.
2. Inspect each `DiagnosticsSummary` section (command backlog, runtime issues, landing states).
**Expected outcome:** Diagnostics view renders the diagnostic count pill matching `attentionChecks + backlog + landing-blocked-rows`, all three sub-sections display non-empty content, and `?view=tasks` is replaced with `?view=diagnostics` in the URL. (Existing harness: `multi-state` covers `diagnostics.open`.)
**Edge cases:**
- Zero issues → diagnostic pill shows `0`, sub-sections show "No issues" copy.
- Malformed-row count surfaces honestly (not silently dropped).
- Clicking a backlog row's run target selects that run without leaving Diagnostics.

### Flow 23 — Watcher start

**Pre-state:** Watcher is `stopped`; queued tasks exist; `canStartWatcher(data)` is true; `subprocess.Popen` is monkeypatched (per Phase 3B) to a fake script.
**Steps:**
1. Click sidebar `start-watcher-button` (or MissionFocus "Start watcher" CTA when primary=`start`).
2. Wait through one poll cycle.
**Expected outcome:** `POST /api/watcher/start` fires once, the next `/api/watcher` poll returns `health.state="running"` with a non-null PID, the sidebar `ProjectMeta`'s Watcher row updates, and the Start button becomes disabled while Stop becomes enabled. (Existing harness: `command-backlog` covers `watcher.start`.)
**Edge cases:**
- Start when already running → button disabled, no duplicate POST.
- Start in a non-managed project — guarded by `canStartWatcher`.
- Start while a `start_blocked_reason` is set → button disabled with title=reason (App.tsx:616).

### Flow 24 — Watcher stop

**Pre-state:** Watcher is `running` (via fake subprocess); `canStopWatcher(data)` is true.
**Steps:**
1. Click sidebar `stop-watcher-button`.
2. ConfirmDialog appears (per `watcher-stop-ui` harness scenario).
3. Click Cancel — verify nothing happens.
4. Reopen, click confirm.
**Expected outcome:** Cancel leaves watcher running; confirm POSTs `/api/watcher/stop`, the next poll shows `health.state="stopped"` with a null PID, and the sidebar Watcher row reflects stopped. (Existing harness: `watcher-stop-ui` covers both `watcher.stop.cancel` and `watcher.stop.confirm`.)
**Edge cases:**
- Stop while subprocess does not respond — UI shows pending then surfaces a graceful timeout error.
- Stop when already stopped → button disabled, no POST.
- Confirm dialog Escape key dismisses without action.

### Flow 25 — Stale / unverified watcher PID

**Pre-state:** `web/watcher-supervisor.json` records a PID, but no such process exists (R11 fixture). Server-side verification flags `pid_verified=false` or marks state as `stale`.
**Steps:**
1. Mount project; observe sidebar Watcher row.
2. Hover the Watcher row / Start button to inspect tooltip.
**Expected outcome:** Sidebar `ProjectMeta` does NOT claim "running" — instead it shows `stale`, `unverified`, or equivalent honest text per `watcherSummary`, and the Start button is enabled (or the Stop button surfaces a "force-clear" affordance) so the user can recover.
**Edge cases:**
- PID belongs to a different process (PID reuse) — UI marks unverified.
- Heartbeat age > threshold but PID exists — UI shows heartbeat age and stale warning.
- Supervisor file is malformed → UI degrades gracefully without crashing the dashboard.

### Flow 26 — Watcher start failure

**Pre-state:** Watcher is stopped; the fake `Popen` is configured to fail (raise `OSError`, exit 1, or return without writing the supervisor file).
**Steps:**
1. Click `start-watcher-button`.
2. Wait for the response.
**Expected outcome:** `POST /api/watcher/start` returns 4xx/5xx, an error toast appears with the server-provided reason, the sidebar Watcher row remains `stopped`, and no orphan PID file is left behind.
**Edge cases:**
- Subprocess fails to write the supervisor file within the timeout — server returns a deterministic error.
- Provider auth error path bubbles up (not classified as INFRA in the UI).
- Repeated start failures do not spam duplicate toasts (last-write-wins).

---

## Resilience / state

### Flow 27 — Server restart mid-session

**Pre-state:** Project mounted, polling active, at least one in-flight run.
**Steps:**
1. From the test harness, kill the FastAPI process.
2. Wait through ≥2 polling intervals (UI sees fetch errors).
3. Restart the FastAPI process on the same port.
4. Wait through ≥2 more poll cycles.
**Expected outcome:** During outage the UI surfaces a non-fatal banner / toast and stops trying to refresh-flicker; on recovery, the next poll repopulates `data` and the inspector / selected-run state is preserved (URL-driven).
**Edge cases:**
- Outage spans >10 polls → backoff is observable in DevTools network log (no thundering herd).
- Restart with different `data` (run completed during outage) → UI reconciles to new state.
- Selected run still exists post-restart → inspector body re-fetches and reopens.

### Flow 28 — Tab backgrounded → return

**Pre-state:** Project mounted; in-flight run; tab visible.
**Steps:**
1. Switch to a different browser tab for ≥2 minutes.
2. Return to the Mission Control tab.
**Expected outcome:** Within one poll interval after returning, `data` reflects the latest server state (run status updated, events list extended), without showing a stale `selectedRunId` for a deleted run.
**Edge cases:**
- Polling pauses while hidden (Page Visibility API) — verify it resumes without delay on visibility change.
- Inspector remains open on the same run, content refreshes from cache + new fetch.
- A run completed while backgrounded → terminal status visible immediately.

### Flow 29 — Two tabs open

**Pre-state:** Two browser tabs against the same `<port>/` and the same project. Tab A and Tab B both polling.
**Steps:**
1. In Tab A, submit a build via JobDialog.
2. Wait one poll interval in Tab B.
**Expected outcome:** Tab B's task board shows the new queued task without requiring a manual refresh, and selecting it in Tab B does not affect Tab A's `selectedRunId`.
**Edge cases:**
- Tab A cancels a run → Tab B sees status flip within one poll.
- Tab A switches project → Tab B is unaffected (separate URL state).
- Tab A and Tab B click the same merge button concurrently → exactly one server-side merge runs; other tab sees idempotent failure or success.

### Flow 30 — Long-running run + slow network

**Pre-state:** Mid-build run; network throttled to slow 3G via Playwright `page.route` slowdown.
**Steps:**
1. Open the in-flight run.
2. Switch tabs Logs ↔ Proof.
3. Wait through several polls.
**Expected outcome:** UI does not show a permanent spinner — each tab transition either uses cached data or shows a tab-local loading indicator; no fatal errors and no duplicate fetches stacking up.
**Edge cases:**
- Fetch race: tab switch fires while previous tab fetch in flight — final tab wins.
- Polling continues at the configured interval (network slowness does not trigger spammy retries).
- A `/api/events?limit=...` call exceeding the slow-link budget shows partial events without freezing the UI.

### Flow 31 — Action error (4xx/5xx)

**Pre-state:** A run selected; the server is configured to return 500 on `POST /api/runs/{id}/actions/cancel`.
**Steps:**
1. Click cancel → confirm.
2. Observe error surfacing.
**Expected outcome:** The error toast / inline message appears next to the action that failed (not as a global banner), the run's status does not change, the confirm dialog has closed, and `RunDetail` re-enables the action button (so the user can retry).
**Edge cases:**
- 4xx with structured error JSON → toast shows server message verbatim.
- 5xx with HTML body → toast shows generic "Server error" without leaking HTML into DOM.
- Error from `merge-all` reports per-row outcomes (already covered) — single-action errors stay attached to the single row.

---

## Navigation / URL

### Flow 32 — Tasks ↔ Diagnostics + URL push/replace

**Pre-state:** Mounted on Tasks view, no run selected.
**Steps:**
1. Click `diagnostics-tab` → URL gets `?view=diagnostics` (push).
2. Click `tasks-tab` → URL gets `?view=tasks` (or removes the param).
3. Use browser Back, then Forward.
**Expected outcome:** URL toggles via `writeRouteState({...}, "push")` (App.tsx:154–168), and Back/Forward cycle through the same view-mode states without remount or stale data.
**Edge cases:**
- Switching while a run is selected preserves `?run=...`.
- Back from Diagnostics returns to Tasks with the same `selectedRunId`.
- Forward after Back replays the same view.

### Flow 33 — Deep link

**Pre-state:** Cold load to URL `?run=<run-id>&view=tasks` for an existing run.
**Steps:**
1. Navigate directly to the URL.
2. Wait for the app to mount.
**Expected outcome:** App lands on Tasks view with `selectedRunId=<run-id>` set, RunDetail panel shows that run's review packet on first paint after data loads, and no extra navigations or replaces fire (verified via push-state spy).
**Edge cases:**
- `?view=diagnostics&run=X` lands on Diagnostics with the run selected and detail panel populated.
- Unknown query params are ignored without console warnings.
- Trailing whitespace / casing in `view` value is normalized.

### Flow 34 — Invalid deep link

**Pre-state:** Cold load to `?run=does-not-exist`.
**Steps:**
1. Navigate to the URL.
2. Wait for the app to mount and `/api/runs/does-not-exist` to return 404.
**Expected outcome:** App falls back to "Select a run." empty detail (App.tsx:1505), `selectedRunId` is reset to null, the URL is replaced (not pushed) to drop the bad `run=`, and a non-fatal toast informs the user the run was not found.
**Edge cases:**
- Malformed run id (special chars) — server 4xx, same fallback.
- `?run=` empty string — falls back without an API call.
- `?view=garbage` — view defaults to `tasks`.

### Flow 35 — Selected run deleted mid-session

**Pre-state:** A run is selected (URL `?run=X`); during the session, the run's session dir is deleted on disk (e.g., cleanup action or external `rm`).
**Steps:**
1. Wait for the next `/api/state` poll (or trigger refresh).
2. Server's state response no longer includes that run.
**Expected outcome:** App detects the missing run (App.tsx:253–284 effect), unsets `selectedRunId`, replaces the URL to drop `?run=`, closes the inspector, and shows the "Select a run." empty state — no stale detail kept around.
**Edge cases:**
- Run deleted while inspector is open → inspector closes.
- Run deleted while a confirm dialog is open for that run's action → dialog auto-cancels and shows a toast.
- Run deleted but a copy still exists in `history.items` → detail is still navigable.

---

## Keyboard / accessibility

### Flow 36 — Tab through whole UI

**Pre-state:** Tasks view mounted with one selected run, inspector closed.
**Steps:**
1. Press Tab repeatedly from `document.body`.
2. Record the focused element at each step until focus returns to the start.
**Expected outcome:** Tab order visits the sidebar (switch-project, new-job, start/stop watcher), then the toolbar (view tabs, filters, refresh), then mission focus CTAs, then task cards, then RunDetail evidence buttons, with no element trapping focus and every interactive control reachable.
**Edge cases:**
- Modal open (JobDialog or ConfirmDialog) traps focus inside via `useDialogFocus`; Tab cycles within the modal only.
- `aria-hidden` set on workspace when modal open prevents background tabbing (App.tsx:603, 621).
- Skip-links (if added) appear on first Tab.

### Flow 37 — Operate critical flows keyboard-only

**Pre-state:** Tasks view, no project selected → drive Flows 1, 6, 10, 12, 15 entirely with the keyboard.
**Steps:**
1. Tab to project-row, press Enter.
2. Tab to `new-job-button`, Enter; complete the JobDialog with Tab + Type + Enter on submit.
3. Tab to a queued task card, Enter; Tab to `open-proof-button`, Enter.
4. Tab to advanced action (cancel), Enter; Tab to confirm button, Enter.
5. Tab to `Land all ready`, Enter; confirm with Enter.
**Expected outcome:** Every step completes without using the mouse — submit, navigation, action confirmation, and merge all reachable and triggerable via Tab + Enter (or Space for buttons).
**Edge cases:**
- Escape cancels JobDialog and ConfirmDialog (focus restored to the trigger).
- Enter on a `<details>` summary toggles the drawer.
- Arrow keys do NOT silently submit forms.

### Flow 38 — Screen-reader landmarks

**Pre-state:** Any view mounted.
**Steps:**
1. Inspect the rendered DOM for landmarks.
**Expected outcome:** Exactly one `<main>` (workspace), one navigation landmark for the toolbar (`aria-label="Mission Control views"`), and named `<section>`s for mission-layout, task-board, run-detail, run-inspector, project-launcher, and diagnostics-layout — verifiable via `getByRole("main")`, `getByRole("dialog")`, etc.
**Edge cases:**
- Modal dialog has `aria-modal="true"` and `aria-labelledby` (JobDialog: App.tsx:2073, ConfirmDialog: App.tsx:2277).
- Toast region has `role="status"` and `aria-live="polite"` (App.tsx:596, 762).
- Each `<section>` either has an `aria-label` or `aria-labelledby` heading.

### Flow 39 — Reduced motion

**Pre-state:** Browser context emulates `prefers-reduced-motion: reduce` (per Phase 3B fixture).
**Steps:**
1. Mount the app.
2. Trigger transitions: open JobDialog, expand a `<details>` drawer, switch tabs.
**Expected outcome:** All CSS transitions / animations declared in `styles.css` are disabled (zero animation duration), so motion-sensitive users see instant state changes without parallax/fade.
**Edge cases:**
- Custom JS animations (if any) honor the media query.
- Toasts still appear/disappear (visibility), but without slide-in animation.
- Spinner glyphs still rotate (or alternative non-motion progress text shown).

---

## Visual / layout

### Flow 40 — Resize between mini / MBA / iPhone + long-string overflow

**Pre-state:** Task board with one selected run; intent text "A".repeat(5000) and a 200-char run-id; all three viewports configured (Mac mini ~1920×1080, MBA ~1440×900, iPhone 14 webkit).
**Steps:**
1. Mount at Mac mini viewport.
2. Resize to MBA viewport.
3. Resize to iPhone 14 viewport.
4. At each step, scroll the task board and run detail panels.
**Expected outcome:** No element overflows its container at any of the three viewports — long intent / URL / error strings wrap or truncate with ellipsis, sidebar stacks below workspace at iPhone width, and no horizontal scrollbar appears on the body. (This is the single visual + layout flow; tv01–tv08 in Phase 3C cover the snapshot variants.)
**Edge cases:**
- Long intent in MissionFocus headline truncates with title attribute fallback.
- Long error message in toast wraps within the toast container; does not push other UI off-screen.
- 200-char run-id in `RunDetail` heading uses `title=full` and visually truncates.

---

## Cross-flow assertions (apply to every flow)

These are pre-conditions every Playwright test should enforce, hoisted out of individual flows to avoid repetition:

- No unexpected console errors (warnings allowed only on the explicit allow-list).
- No unhandled 4xx/5xx network responses except those the test deliberately triggers.
- No leftover orphan watcher subprocess on teardown.
- Screenshot + Playwright trace + API state dump captured on failure.
- `aria-hidden` discipline preserved when modals open/close.
