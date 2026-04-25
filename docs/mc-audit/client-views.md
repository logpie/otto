# Mission Control Client — Product-State Catalog (Phase 1B)

Source: `otto/web/client/src/App.tsx` (3198 lines, single-file component tree).
Supporting types: `otto/web/client/src/types.ts`.
HTTP layer: `otto/web/client/src/api.ts`.
Visual surfaces: `otto/web/client/src/styles.css`.

This catalog enumerates every distinguishable **product state** rendered by the
single-page app — including conditional sub-states inside a single React
component. Branches were walked from the App-level returns at App.tsx:574
(launcher gate) and App.tsx:601 (main shell) down through every nested
conditional render.

URL routing model (App.tsx:88-111, `readRouteState` / `writeRouteState`):
- One pathname `/` (the SPA shell). All state lives in the query string.
- `?view=tasks|diagnostics` — selects `viewMode`. Default: `tasks`.
- `?run=<run_id>` — selects the focused run for the right-hand panel and
  inspector. Cleared on project switch / launcher mount / 404.
- `pushState` is used for explicit user navigation (view change, run select);
  `replaceState` is used for derived defaults (auto-pick first run on refresh,
  clear-after-404).
- A `popstate` listener (App.tsx:148-162) reads back the URL and force-closes
  the inspector, job dialog, and confirm modal.

Polling cadence (App.tsx:288-292, 332-336):
- Global `/api/state` polled every `refreshIntervalMs(data)` =
  `clamp(data.live.refresh_interval_s * 1000, 700, 5000)`.
- When the inspector is on the **Logs** tab, an additional 1200 ms `loadLogs`
  poll runs against `/api/runs/<id>/logs?offset=...`, appending text by byte
  offset (App.tsx:217-232).

Modal-open accessibility (App.tsx:503, 603, 621):
- `modalOpen = jobOpen || Boolean(confirm)`.
- When `modalOpen`, both `<aside class="sidebar">` and `<main class="workspace">`
  receive `aria-hidden=true`. Toast remains visible with `role="status"`.

Focus management for modals/inspector (`useDialogFocus` App.tsx:2696-2751):
- On mount: stores `document.activeElement`, then schedules a microtask to
  focus the first focusable element inside the dialog (or the dialog root).
- `Escape` calls `onCancel()` unless the `disabled` ref says the dialog is in a
  pending state (e.g. `confirmPending`, `submitting` for JobDialog).
- `Tab` / `Shift+Tab` are wrapped: first → last and last → first.
- On unmount: restores focus to the previously focused element if still
  connected.
- Used by `RunInspector` (App.tsx:1529, `disabled=false`), `JobDialog`
  (App.tsx:2017, `disabled=submitting`), `ConfirmDialog` (App.tsx:2268,
  `disabled=pending`).

There are **no global keyboard shortcuts**. The only keyboard interaction
beyond `Tab`/`Escape` is `selectOnKeyboard` (App.tsx:2690-2694) — Enter or
Space activates a row in the Live Runs / History tables (`role=button`,
`tabIndex=0`). Filter selects, textareas, and other native controls use the
default browser keyboard handling. There are **no transitions or animations**
authored in `styles.css` aside from CSS pseudo `:hover`/`:focus` color shifts;
state changes are instantaneous.

Data sources by API endpoint (referenced throughout):
- `GET /api/projects` — `loadProjects` (App.tsx:241-245).
- `GET /api/state?type&outcome&query&active_only` — `refresh` (App.tsx:265).
- `GET /api/runs/<id>?<state-query>` — `refreshDetail` (App.tsx:236).
- `GET /api/runs/<id>/logs?offset=<n>` — `loadLogs` (App.tsx:221).
- `GET /api/runs/<id>/artifacts/<index>/content` — `loadArtifact` /
  `loadProofArtifact` (App.tsx:425, 440).
- `GET /api/runs/<id>/diff` — `loadDiff` (App.tsx:454).
- `POST /api/runs/<id>/actions/<action>` — `runActionForRun` (App.tsx:351).
- `POST /api/actions/merge-all` — `mergeReadyTasks` (App.tsx:381).
- `POST /api/watcher/start|stop` — `runWatcherAction` (App.tsx:394).
- `POST /api/queue/<command>` — `JobDialog.submit` (App.tsx:2055).
- `POST /api/projects/{create,select,clear}` — managed-project mutations
  (App.tsx:506, 526, 546).

---

## 1. Project / Launcher

### 1.1 Launcher gate condition (App.tsx:574)
The whole shell is replaced by the launcher when **both** are true:
- `projectsState.launcher_enabled === true` (server told the client this is
  a launcher-mode deployment).
- `data === null` (no `/api/state` payload yet — either pre-first-fetch or
  cleared after the user pressed "Switch project" or selected nothing).

When `launcher_enabled === false`, the launcher never renders even if `data`
is null — the regular shell renders with `data?.…` lookups returning
undefined.

### 1.2 Launcher empty state — pre-fetch (App.tsx:574-598, ProjectLauncher
App.tsx:792-906)
- **URL**: any (route state ignored; project-launcher view always reachable
  via missing `data`).
- **Data**: only `projectsState`. Render shows a brand block in the sidebar,
  no `ProjectMeta` (because `data` is null).
- **Layout**: two-column launcher grid — left "Create project" form, right
  "Managed root" card showing the absolute path (`projects_root`).
- **Project list panel**: header pill displays `projects.length`; if zero,
  renders `<div class="launcher-empty">No managed projects yet.</div>`.

### 1.3 Launcher populated — projects listed
- Each row is a `<button class="project-row">` with name, full path (in
  `<code>`), branch, dirty/clean state, short head SHA (7 chars) or `-`.
- `disabled` while another project is opening (`pending`).
- Click → `openProject` → `selectManagedProject` → `POST /api/projects/select`
  → on success, replaces `projectsState`, resets `viewMode='tasks'`,
  `selectedRunId=null`, calls `refresh(true)`. Toast: `Opened <name>`.

### 1.4 Launcher submitting — create form
- States via local `pending` and `status`:
  - **idle**: empty `status`, button reads "Create project".
  - **validation error**: trimmed name empty → `status="Project name is
    required."` (no API call).
  - **submitting**: `pending=true`, status `"Creating project"`, submit button
    disabled and reads "Working".
  - **failure**: `status` = `errorMessage(error)`; remains visible in
    `<p class="launcher-status" aria-live="polite">`.
  - **success**: clears `name` and `status`; `createManagedProject` toasts
    `Created <name>`, refreshes state, transitions to main shell.

### 1.5 Launcher refresh affordance
- Top-right "Refresh" button (App.tsx:847-849) — calls `onRefresh` which is
  `refresh(true)`.
- Inline `<span class="muted">` shows `refreshLabel(refreshStatus)` —
  `"refreshing"` while a fetch is in flight, `"refresh failed"` after error.

### 1.6 Sidebar in main shell — Project meta (`ProjectMeta` App.tsx:767-786)
- Always visible when shell mounts. Six `MetaItem` rows:
  - Project name (`-` if undefined).
  - Branch (`-` if undefined).
  - State: `unknown` | `dirty` | `clean`.
  - Watcher: from `watcherSummary` — `running pid <n>`, `stale pid <n>`, or
    `stopped`.
  - Heartbeat: `<n>s ago` rounded; `-` if null/undefined.
  - In flight: count from `activeCount` (running + initializing + starting +
    terminating).
  - Tasks: `queued <n> / ready <n> / landed <n>` formatted from watcher
    counts and landing counts.

### 1.7 Sidebar — "Switch project" button (App.tsx:612-614)
- Renders only when `projectsState?.launcher_enabled` is true.
- Click → `switchProject` → `POST /api/projects/clear`. Resets all detail/log/
  inspector state, clears `data`, returns to launcher. Toast: "Choose a
  project".

---

## 2. View Mode Switcher (Toolbar) — `viewMode === "tasks" | "diagnostics"`

### 2.1 Toolbar header (App.tsx:908-984)
Always rendered above either layout. Three regions:
- **View tabs** (App.tsx:918-937): "Tasks" / "Diagnostics" buttons,
  `aria-pressed` reflects active mode, `data-testid="tasks-tab"` /
  `"diagnostics-tab"`. Click calls `navigateView(next)` which pushes a new
  history entry and closes the inspector.
- **Filters** (App.tsx:938-977): five controls (see §10).
- **Toolbar actions** (App.tsx:978-981): refresh status pill + "Refresh"
  button. Same `refreshLabel` semantics as launcher.

### 2.2 Tasks layout (App.tsx:631-685)
- Single CSS grid `<section class="mission-layout">`:
  - Left/main stack: `MissionFocus` → `TaskBoard` → `RecentActivity`.
  - Right rail: `RunDetailPanel`.
  - Overlay: `RunInspector` when `inspectorOpen && detail`.

### 2.3 Diagnostics layout (App.tsx:687-737)
- `<section class="diagnostics-layout">`:
  - Top: `OperationalOverview` strip (six metric cards + banners).
  - Below: `<div class="diagnostics-workspace">` with a 2x2 grid
    (`DiagnosticsSummary`, `LiveRuns`, `EventTimeline`, `History`) and
    `RunDetailPanel` rail. Same `RunInspector` overlay.

### 2.4 View transitions
- `navigateView(next)` (App.tsx:164-170): no-op if same view; else
  closes inspector, calls `setViewMode(next)`, `pushState`. Job dialog and
  confirm modal are NOT closed by view change (only by `popstate`).

---

## 3. Mission Focus Banner (Tasks view only) — `MissionFocus` App.tsx:1122-1178

The banner picks one of nine product states via `missionFocus(data)`
(App.tsx:2297-2422). Each provides `kicker`, `title`, `body`, `tone` (drives
`focus-<tone>` class), and `primary` (drives the primary button).

### 3.1 Loading state — `data === null`
- kicker "Loading", title "Reading project state", tone `info`, primary `new`.

### 3.2 Commands waiting + watcher not running
- Trigger: `commandBacklog > 0 && watcher.health.state !== "running"`.
- kicker "Commands", title `"<n> command(s) waiting"`, tone `warning`,
  primary "Start watcher" (disabled if `!canStartWatcher(data)`).

### 3.3 Repository cleanup required
- Trigger: `landing.merge_blocked && rawReady > 0`.
- kicker "Repository", title "Cleanup required before landing".
- body lists up to 3 dirty files; tone `danger`, primary "Review cleanup"
  → opens diagnostics view.

### 3.4 Tasks need action
- Trigger: `needsAction > 0`.
- title `"<n> task(s) need action"`, tone `warning`, primary "Review cleanup".

### 3.5 Tasks ready to land
- Trigger: `ready > 0`. tone `success`, primary "Land all ready" (disabled
  if `!canMerge(data?.landing)`).

### 3.6 Queue waiting + watcher idle
- Trigger: `queued > 0 && watcher.state !== "running"`. tone `info`, primary
  "Start watcher".

### 3.7 Working
- Trigger: `working > 0` (after the previous gates are negative). tone
  `info`, primary `new`.

### 3.8 Idle (history exists)
- Trigger: `landing.counts.total || history.total_rows`. kicker "Idle",
  title "No task needs action", tone `neutral`, primary `new`.

### 3.9 First run
- Fallback: kicker "Start", title "Queue the first job", tone `neutral`.

### 3.10 Banner extras (always rendered if data present)
- Three `FocusMetric`s: Queued/running, Needs action, Ready.
- `lastError` banner (App.tsx:1161-1167) shown if `lastError !== null`,
  with Dismiss button → `setLastError(null)`.
- `resultBanner` banner (App.tsx:1168-1174) — `severity` `error|warning|
  information`. Dismiss → `setResultBanner(null)`.
- `RuntimeWarnings` (App.tsx:1175) renders only if `runtime.issues.length`.

### 3.11 Secondary "New job" button
- Always rendered alongside the primary (App.tsx:1154). When
  `focus.primary === "new"`, the **primary** button is "New job" and no
  secondary "New job" appears.

---

## 4. Task Board (Tasks view) — `TaskBoard` App.tsx:1189-1228

### 4.1 Board structure
- Four columns, in fixed render order: **Needs Action**, **Queued / Running**,
  **Ready To Land**, **Landed** (App.tsx:2430-2435). Column header shows the
  count.
- `taskBoardSubtitle` reports `"<n> visible task(s) for <target>"` or
  `"No work queued."` or `"Loading tasks."`.

### 4.2 Column empty states (literal copy)
- attention: `"No blocked work."`
- working: `"No queued or running tasks."`
- ready: `"Nothing ready yet."`
- landed: `"Nothing landed yet."`

### 4.3 Card states — `TaskCard` App.tsx:1230-1278
- **Selected**: `task.runId && task.runId === selectedRunId` → adds
  `selected` class, `aria-pressed=true`. CTA chip reads "Review" for
  `stage==="ready"`, otherwise "Details".
- **Disabled**: when `!task.runId` (queue task with no run yet) — main button
  disabled.
- **Card source labels**: derived from `boardTaskFromLanding` and
  `boardTaskFromLive`:
  - landing-derived: status from `boardStatusLabel` — `ready|landed|blocked`
    or queue_status string.
  - live-derived: status = `display_status`; reason = overlay reason / last
    event / elapsed display / display status.
- **Meta line** built from `taskChangeLine(task)` and `task.proof`:
  - `taskChangeLine`: `"diff pending"` (no count yet), `"not built yet"`
    (working+queued), `"diff pending"` (working otherwise), `"no unlanded
    diff"` (landed), or `"<n> file(s)"`.
  - `proof`: `"<passed>/<tested> stories"`, queue_status, or `-`.

### 4.4 "More / Less" drawer per card
- Toggle button `aria-expanded`, `aria-controls=<test-id>-drawer`. State is
  local (`useState`).
- Drawer body (when expanded): truncated summary (220 chars,
  `shortText`), `<dl>` with Branch (or "no branch") and Reason.

### 4.5 Filtering interactions (recompute board on every render)
- `boardTaskMatchesFilters` enforces:
  - `query` — substring match across id/title/summary/status/branch/reason/
    proof, lowercased.
  - `activeOnly` — only `working` stage cards survive.
  - `outcome !== "all"` — sliced by `boardTaskMatchesOutcome` heuristic
    (status string contains "ready"/"failed"/etc.).

### 4.6 Stage assignment (`boardStageForLanding` App.tsx:2523)
- merged → `landed`.
- ready + `mergeAllowed` → `ready`; ready + `!mergeAllowed` → `attention`.
- waiting (queued/starting/initializing/running/terminating) → `working`.
- everything else → `attention`.

### 4.7 Click-through
- `selectTask` → `onSelect(task.runId)` → `selectRun` (App.tsx:187-202):
  closes inspector, clears detail/log/artifact/proof/diff state if changing
  selection, sets `inspectorMode='proof'`, pushes URL with `?run=<id>`.

---

## 5. Recent Activity (Tasks view) — `RecentActivity` App.tsx:1280-1321

- Top 4 events + top 4 history items, interleaved (events first).
- Counter pill = `events.total_count + history.length`.
- Event rows: severity span, message, formatted time. Class
  `event-<severity>`.
- History rows: clickable buttons → `onSelect(item.run_id)`. Class
  `history-activity selected` when `item.run_id === selectedRunId`.
- Empty state literal: `"No activity yet."`

---

## 6. Run Detail Panel — `RunDetailPanel` App.tsx:1456-1509

Rendered in **both** the Tasks layout (right rail) and the Diagnostics layout
(below the diagnostics grid). Heading text changes by state.

### 6.1 No selection
- `detail === null` → heading "Run Detail", pill `-`, body `<div>Select a
  run.</div>`.

### 6.2 Detail loading
- `selectedRunId !== null && detail === null` while `refreshDetail` is in
  flight. Same render as 6.1 (no skeleton). Effect at App.tsx:294-330 also
  fires on selection change to clear all secondary state.

### 6.3 Detail loaded
- Heading "Review Packet", pill = `detailStatusLabel(detail)` —
  `blocked|merged|<display_status>|-`.
- Body has scrollable region (`detail-scroll`) → `ReviewPacket`, then
  `<details>` "Run metadata" (collapsed by default), then `ActionBar`.
- Inspector-action row (always visible at the bottom):
  - **Open proof** (primary, always enabled) — opens inspector at proof tab.
  - **Diff** — disabled if `!canShowDiff(detail)` (no branch, diff_error, or
    `readiness.state==='in_progress'`).
  - **Logs** — always enabled.
  - **Artifacts** — always enabled.

### 6.4 ReviewPacket (App.tsx:1771-1883)
- Variants by `packet.readiness.tone` → CSS class `review-<tone>`
  (`success|warning|danger|info`).
- Variants by `packet.readiness.state`:
  - `in_progress` — wider review-grid, hides "View all evidence" button.
  - `merged` — wider review-grid, includes Artifacts metric.
  - `ready` — `diff_command` shown inside Changed-files drawer.
- Next-action button: rendered only when `packet.next_action.action_key`
  exists. Disabled if `!action.enabled`. Tooltip = `action.reason`. Click →
  `runActionForRun` confirm flow.
- Failure summary block (`FailureSummary` App.tsx:1906) renders only when
  `packet.failure !== null`. Provides reason; the proof tab also shows the
  excerpt as `<pre>`.
- Blockers list rendered only if `!hasFailure && blockers.length > 0`.
- Three `ReviewMetric`s always: Stories, Changes, Evidence.
- `ReviewDrawer` "Checks" — rendered if `drawerChecks.length > 0`. Default
  open if there is a failure or any non-pass/info checks.
- `review-note danger` literal — diff_error message (formatTechnicalIssue
  rewrites "unknown revision" / "unstaged changes" into operator-friendly
  copy).
- `recovery-note` — when `isRepositoryBlockedPacket`, prints "Run git status
  --short, then commit, stash, or revert local project changes before
  landing."
- `ReviewDrawer` "Changed files" — only if `packet.changes.files.length > 0`;
  `<li>more files not shown</li>` appended if `truncated`. The
  `diff_command` `<code>` only renders for `readiness.state==='ready'`.
- `ReviewDrawer` "Evidence" — first 4 readable artifacts, button per
  artifact. Disabled + `missing` class when `!isReadableArtifact` (missing
  or directory).
- `review-inline-action` "View all evidence" — only when
  `packet.evidence.length > readable.length` AND `!inProgress`.

### 6.5 Run metadata expander (App.tsx:1476-1494)
- `<details>` collapsed by default. Body has `<dl>` with Run / Type / Branch
  / Worktree / Provider / Artifacts / Overlay / summary lines.
- `DetailLine` (App.tsx:1921-1939) drops lines starting with "compat:" and
  rewrites "legacy queue mode" → "queue compatibility mode".

### 6.6 ActionBar (advanced run actions) — App.tsx:1941-1961
- Filters out keys o/e/m/M (visible-only set differs from primary
  `next_action`).
- If no visible actions → renders empty placeholder `div.advanced-actions
  empty` with `aria-hidden=true`.
- `<details>` summary "Advanced run actions". Each button `disabled` if
  `!action.enabled` or (`action.key==='m' && mergeBlocked`). Title text:
  merge-blocked tooltip vs `action.reason`/`action.preview`.

---

## 7. Run Inspector (overlay) — `RunInspector` App.tsx:1511-1572

Rendered above the workspace when `inspectorOpen && detail` (in either
layout). `role=dialog`, `aria-modal=true`, focus-trapped via
`useDialogFocus`. Always closeable with Escape; "Close inspector" button
(App.tsx:1551).

### 7.1 Inspector heading
- Title = `detail.title || detail.run_id`.
- Subhead "<status> evidence packet" via `detailStatusLabel`.
- Tab strip: Proof / Diff / Logs / Artifacts. Diff disabled when
  `!canShowDiff`. `aria-selected` follows `mode`.

### 7.2 Tab transitions / data loading
- `showProof` (App.tsx:485-488) — sets mode `proof`. Effect at App.tsx:490-496
  fires `loadProofArtifact` for the preferred artifact (`preferredProofArtifact`
  prefers labels: summary, queue manifest, manifest, intent, primary log).
- `showDiff` — sets mode `diff`, calls `loadDiff` (clears existing diff
  state first).
- `showLogs` — sets mode `logs`, clears artifact state, calls `loadLogs(runId,
  reset=true)`. Polling interval starts (App.tsx:332-336).
- `showArtifacts` — sets mode `artifacts`, clears `selectedArtifactIndex`
  and `artifactContent` so the user re-enters the artifact list.

### 7.3 Proof tab — `ProofPane` App.tsx:1588-1725
States within Proof:

- **Header summary** always rendered: readiness label, "Proof of work"
  heading, packet headline. Three metrics: Stories, Changes, Evidence.
- **Next action section** (App.tsx:1618-1628):
  - HTML report present → `<a target="_blank">Open HTML proof report</a>`
    (test-id `proof-report-link`). `proofReport.html_url`.
  - Otherwise → `<span>No HTML proof report is linked for this run.</span>`.
- **Failure section** (only when `packet.failure`): `FailureSummary` with
  excerpt as `<pre class="log-content">`.
- **Certification checks**:
  - `proofChecks` filters out `run` and `landing` keys when failure exists.
  - List renders if `proofChecks.length > 0`; else
    `<p>No additional checks were recorded before the task failed.</p>`.
- **Stories tested**:
  - Story list with status badge (`pass|warn|fail|skipped|unknown`),
    methodology+surface footer.
  - Empty literal: `"No per-story certification details were recorded. Open
    the HTML report or summary artifact if available."`
- **Changed files**:
  - First 10 files; `<li>more files not shown</li>` if `truncated`.
  - Empty: `"No changed files reported yet."`
- **Code diff section**:
  - `diff_error` present → `formatTechnicalIssue(error)` (rewrites unknown
    revision / unstaged changes).
  - Else `canShowDiff(detail)` → button `data-testid="proof-open-diff-button"`
    that switches the inspector to Diff tab.
  - Else literal: `"No code diff is available for this run yet."`
- **Evidence artifacts** grid:
  - Empty: `"No readable evidence artifacts are attached."`
  - Each button `selected` if `proofArtifactIndex === artifact.index`.
- **Evidence content** pane:
  - Subtitle = `proofContent?.artifact.label || "Loading selected evidence
    artifact"`.
  - "truncated" badge if `proofContent?.truncated || compact.truncated`.
  - Body: pre block with `compact.text` (cap 20 000 chars; tail-aligned).
  - Loading literal: `"Loading evidence content..."`.

### 7.4 Diff tab — `DiffPane` App.tsx:1727-1769
- **Loading**: `diff === null` → toolbar `loading` plus `<pre>Loading
  diff...</pre>`.
- **Loaded**: toolbar reads `<branch> → <target>` (with `· truncated`
  suffix when `diff.truncated`). Diff `error` → `formatTechnicalIssue`
  banner.
- **Sections**: `splitDiffIntoFiles` parses `diff --git a/.. b/..` lines.
  - Sections present → file list nav (`data-testid="diff-file-list"`),
    selected file shows `<n> file(s)` and `pre` body. Default selection
    resets on `diff.run_id`/`diff.text` change (App.tsx:1730-1732).
  - No sections, no text → `<pre>No diff content.</pre>`, heading
    "No changed file selected".
  - Plain-text fallback when no `diff --git` headers: single section using
    `files[0] || "diff"`.
- **Coloring**: `diffLineClass` adds `diff-add|diff-del|diff-hunk|diff-meta|
  diff-context`.

### 7.5 Logs tab — `LogPane` App.tsx:1574-1586
- Toolbar shows line count or `"waiting for output"` when text empty;
  appends `· showing latest output` when `compactLongText` truncates (cap
  14 000 chars).
- `<pre data-testid="run-log-pane">` renders `renderLogText`
  (line-classified by `logLineClass`: error/warn/success/info/muted) and
  `renderAnsiText` (handles SGR codes 0/1/22/30-37/39/90-97).
- Polled every 1.2 s while open. `loadLogs` is also called once on tab
  open with `reset=true`.

### 7.6 Artifacts tab — `ArtifactPane` App.tsx:1963-1997
- **Empty list**: `selectedArtifactIndex === null && !artifacts.length` →
  `<div>No artifacts.</div>`.
- **List view**: `selectedArtifactIndex === null && artifacts.length` →
  vertical button list. Each button disabled if `!isReadableArtifact`. Sub-
  label = `artifactKindLabel` ("<kind> (missing)" if not exists, "directory
  - use Diff for code review" for directories).
- **Detail view**: `selectedArtifactIndex !== null`:
  - "Back to artifacts" button → `onBack` clears index/content (returns to
    list).
  - Header line: label + `(truncated)` when `artifactContent?.truncated`
    or `compactLongText` truncated.
  - Body: pre with log rendering when `isLogArtifact`, else
    `formatArtifactContent` (auto-pretty JSON when content starts with
    `{`/`[` and parses).

---

## 8. Diagnostics View

### 8.1 Operational Overview — `OperationalOverview` App.tsx:986-1021
- Six metric cards: Active, Needs attention, Ready, Repository, Watcher,
  Runtime. Each carries a `tone-<tone>` class set by `workflowHealth`.
- Banners (same as MissionFocus): `lastError`, `resultBanner`, runtime
  warnings (top three issues plus backlog suffix).
- `RuntimeWarnings` (App.tsx:1032-1050): banner reads
  `"<label>: <next_action> | …"` for top three issues. Tooltip lists
  `label: detail` joined by newlines. Suffix `<n> pending / <n> processing /
  <n> malformed` from `command_backlog`, fallback to `runtime.status`.

### 8.2 Diagnostics Summary — `DiagnosticsSummary` App.tsx:1052-1120
Three sub-sections:

#### Command Backlog
- For each `command_backlog.items[]`: a `<details>` with state badge,
  `kind || "queued action"`, and `commandBacklogLine` ("<id> · <target> · <n>s
  old").
- Empty: `"No pending commands."`.

#### Runtime Issues
- Up to 4 issues. Severity-error rows render `open` by default.
- Empty: `"No runtime issues."`.

#### Review And Landing
- Up to 8 items, ordered ready → blocked → merged.
- Each row is a button → `onSelect(run_id)`; disabled when no `run_id`.
- Status text from `landingStateText`; subtitle `landingDiagnosticAction`
  (queued → "Start the watcher…", failed → "Open review packet and
  requeue or remove.", stale → "…remove stale work.", etc.).
- Empty: `"No queued work."`.

### 8.3 Live Runs table — `LiveRuns` App.tsx:1323-1375
- Table columns: Status, Run, Branch / Task, Elapsed, Usage, Event.
- Row classes: `status-<display_status>`; `selected` when
  `run_id===selectedRunId`. `aria-selected`, `role=button`, `tabIndex=0`.
- Keyboard: Enter/Space activates `onSelect`.
- Status cell tooltip = `overlay.reason` or `display_status`.
- Event cell text from `runEventText`:
  - `"Ready for review"` when landingByTask says ready.
  - `"Landed"` when merged.
  - `"Queued"` / `"In progress"` when waiting.
  - Lower-cased `"legacy queue mode"` is rewritten to `"Queue task"`.
  - Fallback: `last_event` or `-`.
- Empty state: full-row cell `"No live runs."`.

### 8.4 Event Timeline — `EventTimeline` App.tsx:1423-1454
- Pill = `events?.total_count || 0`. Subtitle `timelineSubtitle`:
  - `"Queue, watcher, merge, and recovery actions appear here."` when no
    events.
  - `"Recent <visible> of <total or 'scanned recent log'> [/ <m>
    malformed]."` when events exist.
- `malformed_count > 0` → `<div class="timeline-warning">Ignored <n>
  malformed event row(s).</div>` rendered above the list.
- Each row: severity badge, message, target line (`<kind> - task <task_id>
  / run <run_id>`), `<time>`.
- Empty: `"No operator events yet."`.

### 8.5 History — `History` App.tsx:1377-1421
- Columns: Outcome, Run, Summary, Duration, Usage. Pill = `totalRows`.
- Row class `status-<terminal_outcome|status>` lowercased.
- Same Enter/Space keyboard activation as Live Runs.
- Empty cell: `"No matching history."`.
- The history component is **only** rendered in the diagnostics view; the
  tasks view exposes recent history through `RecentActivity` (top 4 only).
- There is **no pagination UI** — `page`, `page_size`, `total_pages` from
  the API are unused; only `total_rows` displays.

---

## 9. Job Dialog Matrix — `JobDialog` App.tsx:1999-2179

Rendered when `jobOpen=true`. Closing path: header "Close" button, footer
state, Escape (focus trap). Backdrop is a `presentation` div (no
click-to-dismiss).

### 9.1 Command picker
- Three options: build / improve / certify (`data-testid=
  "job-command-select"`).
- Improve mode picker (App.tsx:2109-2117) appears **only** when
  `command==='improve'`. Three options: bugs / feature / target.

### 9.2 Target guard (App.tsx:2089-2108)
- Always rendered. Class `target-dirty` when `project?.dirty`.
- Reads `project.path / branch / state`.
- "I understand…" checkbox (`data-testid="target-project-confirm"`)
  rendered only when `targetNeedsConfirmation = project?.dirty === true`.
- Confirmation resets when `project.path` changes (App.tsx:2021-2023).
- Submit disabled if `targetNeedsConfirmation && !targetConfirmed`.

### 9.3 Intent / focus textarea
- 5 rows, placeholder "Describe the requested outcome".
- For `command==='build'`: required (button disabled while empty;
  validation message `"Build intent is required."` from `submit`).
- For `command==='improve'`: maps to `payload.focus`.
- For `command==='certify'`: maps to `payload.intent`.

### 9.4 Advanced options `<details>` (App.tsx:2121-2171)
Closed by default. Contents:
- **Task id** — free-text; placeholder "auto-generated".
- **After** — comma-separated dependency list.
- **Provider** — `""` (inherit) | `codex` | `claude`. Inherit label dynamic:
  `Inherit: <Provider> (otto.yaml | built-in default)`; `"Inherit from
  otto.yaml"` when no defaults yet.
- **Reasoning effort** — `""` | `low|medium|high|max`. Inherit label =
  `Inherit: <effort> (<source>)` or "Provider default" when no default.
- **Model** — free-text; placeholder either `provider default` or
  `project default: <model>`.
- **Certification** — split rendering:
  - Dynamic select: when `certificationOptions(...)` returns options
    (build, certify, improve+bugs).
    - Inherit option label changes for improve+bugs:
      `"Inherit: thorough bug certification (improve default)"`. Otherwise
      `certificationDefaultLabel` (e.g. `"Inherit: skip certification
      (otto.yaml)"`).
    - Always: fast / standard / thorough.
    - build only adds: `"Skip certification (--no-qa)"`.
    - Help text from `certificationHelp` (config_error overrides everything
      with `"Using built-in defaults because otto.yaml could not be read:
      <err>"`).
  - Static label (`data-testid="job-certification-static"`): rendered for
    `improve+feature` ("Feature improvement uses hillclimb evaluation") and
    `improve+target` ("Target improvement uses target evaluation").
- Effect at App.tsx:2025-2029 forces `certification=""` when policy not
  allowed by `(command, subcommand)` pair.

### 9.5 Footer states
- Live status string (`aria-live="polite"`):
  - Validation: `"Build intent is required."`, `"Confirm the dirty target
    project before queueing."`.
  - In flight: `"queueing"`. Submit button reads `"Queueing"`.
  - On API error: error message.
- Submit button disabled when `submitting || (build && !intent.trim()) ||
  (targetNeedsConfirmation && !targetConfirmed)`.
- On success: parent `onQueued(message)` closes the dialog, toasts the
  result, and refreshes state.

---

## 10. Filters / Search — `Toolbar` App.tsx:938-977

- **Type select** — values: all | build | improve | certify | merge | queue.
- **Outcome select** — values: all | success | failed | interrupted |
  cancelled | removed | other.
- **Search input** (type=search) — placeholder "run, task, branch".
  Matched client-side against TaskBoard cards (lowercased substring) and
  passed server-side via `stateQueryParams` for state requests.
- **Active checkbox** — `filters.activeOnly`. Server filter; locally also
  hides non-working board cards (App.tsx:2473).
- **Clear filters** button — resets to `defaultFilters`.
- Filters are **not** persisted in the URL — refreshing the page resets
  them.
- Filter changes refire `refresh` via the dependency on `filters` in
  `refresh`'s `useCallback` deps (App.tsx:286).

---

## 11. Watcher Controls (sidebar) — App.tsx:615-618

- **Start watcher** button: disabled unless `canStartWatcher(data)` —
  requires `runtime.supervisor.can_start && (queued > 0 || backlog > 0)`.
  Tooltip = `start_blocked_reason` or `next_action`.
- **Stop watcher** button: disabled unless `canStopWatcher(data) =
  supervisor.can_stop`. Tooltip = `next_action`.
- Stop triggers a confirm dialog ("Stop watcher" / "Stop the queue
  watcher? Running tasks will be interrupted." / danger tone).
- Start does **not** confirm. Both call `POST /api/watcher/<action>`;
  start payload is `{concurrent: 2}`, stop is empty.
- `watcher-action-hint` (`<p id>`): contextual copy from
  `watcherControlHint` — "Start watcher to process N queued and M
  command(s).", "Watcher is running; stop it only when…", "Start
  unavailable: <reason>", "Queue a job before starting the watcher.", or
  watcher.health.next_action.

---

## 12. Confirm Dialog Matrix — `ConfirmDialog` App.tsx:2261-2295

One component, parameterized by the `confirm` state object set by various
call sites. All variants share: focus trap, Escape cancels (unless
`pending`), header Close button, footer Cancel + primary/danger button. The
primary button reads `pending ? "Working" : confirm.confirmLabel`.

### 12.1 Cancel run
- Trigger: `runActionForRun(runId, "cancel", body, label)`.
- Title: `"Cancel run"`; tone `danger`; body: `"Cancel this run?"`.
- Confirm label: `"Cancel"`.

### 12.2 Cleanup run
- Trigger: action `"cleanup"`.
- Title: `"Cleanup run"`; tone `danger`; body: `"Clean up this run?"`.

### 12.3 Merge / Land single task
- Title: `"Land task"`; tone `primary`; body: `"Land this task into the
  target branch?"`.
- Confirm label: `"Land task"`.
- Pre-check: if `data.landing.merge_blocked`, the confirm is **never
  opened** — `runActionForRun` toasts `mergeBlockedText(landing)` and
  returns.

### 12.4 Merge-all (Land all ready)
- Trigger: `mergeReadyTasks`.
- Title: `"Land ready tasks"`; tone `primary`; body =
  `landingBulkConfirmation` ("Land N ready tasks into <target>: id1, id2,…
  +K more. This will land C changed file(s) across the ready work.").
- Confirm label: `"Land 1 task"` or `"Land N tasks"`.
- Same merge_blocked pre-check (toast then return).
- Same "no ready" pre-check (`"No land-ready tasks"` warning toast).

### 12.5 Stop watcher (see §11)

### 12.6 Other run actions (resume / retry / requeue / remove)
- Generic body comes from `actionConfirmationBody` — `"Resume this run?"`,
  `"Requeue this task?"`, `"Remove this queue task?"`, fallback
  `"<Action> this run?"`.
- Tone is `danger` only for `cancel`/`cleanup`.

### 12.7 In-flight state
- `confirmPending=true` while `onConfirm` runs — Cancel/Close buttons
  disabled, primary reads "Working", Escape ignored.
- On success: `setConfirm(null)` (dialog closes). On exception:
  `showToast(errorMessage)` and dialog stays open. The action's
  `handleActionResult` may set `resultBanner` (modal_title/modal_message)
  for follow-up display in the focus banner.

---

## 13. Toasts and Banners (cross-cutting)

### 13.1 Toast
- Singleton element rendered at `<div id="toast">` in both shells.
- Auto-dismisses after 3200 ms (App.tsx:172-176).
- `aria-live="polite"`, `role="status"`. Class `toast-<severity>`.
- `severity==='error'` also writes to `lastError` so it persists in the
  banner area until dismissed.

### 13.2 Result banner
- `resultBanner` set by `handleActionResult` when an `ActionResult` carries
  a `modal_title` or `modal_message`.
- Rendered inside MissionFocus (tasks) or OperationalOverview (diagnostics)
  — visible in both views.
- Severity drives banner class (`error` vs `warning`).
- `result.clear_banner === true` clears it; `result.ok && !modal_*` also
  clears.

### 13.3 Last error
- Sticky red banner; survives across refreshes until the user presses
  Dismiss or another action toasts an error (which overwrites it).

---

## 14. Loading / Empty / Truncated Sub-states (cheat sheet)

Captured here for quick audit reference — exact literals used by the UI.

- **Detail not loaded** → "Select a run." (App.tsx:1505)
- **Logs empty** → "No logs yet." displayed via `compactLongText` fallback;
  toolbar reads "waiting for output" (App.tsx:1581).
- **Logs truncated** → toolbar appends "· showing latest output"; pre
  prepends "[showing latest <N> complete lines]" (App.tsx:3175).
- **Diff loading** → "loading" toolbar + "Loading diff..." pre.
- **Diff empty content** → "No diff content." (App.tsx:1764).
- **Diff truncated** → toolbar suffix "· truncated".
- **Diff branch missing** → "Changed files could not be inspected because
  the source branch is missing or not reachable. Refresh after the task
  creates its branch, or remove and requeue the task."
- **Working tree dirty** (formatTechnicalIssue) → "Repository has local
  changes. Commit, stash, or revert them before landing."
- **Proof report missing** → "No HTML proof report is linked for this
  run."
- **Proof checks empty** → "No additional checks were recorded before the
  task failed."
- **Proof stories empty** → "No per-story certification details were
  recorded. Open the HTML report or summary artifact if available."
- **Proof changed-files empty** → "No changed files reported yet."
- **Proof diff unavailable** → "No code diff is available for this run
  yet."
- **Proof evidence empty** → "No readable evidence artifacts are
  attached."
- **Proof content loading** → "Loading evidence content..."
- **Artifact list empty** → "No artifacts."
- **Artifact missing** → label appended with " missing"; button disabled.
- **Diagnostics – commands empty** → "No pending commands."
- **Diagnostics – issues empty** → "No runtime issues."
- **Diagnostics – landing empty** → "No queued work."
- **Live runs empty** → "No live runs."
- **History empty** → "No matching history."
- **Timeline empty** → "No operator events yet."
- **Timeline malformed** → "Ignored N malformed event row(s)."
- **Activity empty** → "No activity yet."
- **TaskBoard subtitle** → "Loading tasks." | "No work queued." | "<n>
  visible task(s) for <target>."
- **Switch project pending** → toast "Choose a project".
- **Project create pending** → "Working" / "Creating project".

---

## 15. URL / Route State Reference

| Param | Values | Effect | Persistence |
|-------|--------|--------|-------------|
| `view` | `tasks`/`diagnostics` | Selects layout; default `tasks` | pushState on user click; replaceState on initial mount |
| `run` | run id string | Drives `selectedRunId`, fills `RunDetailPanel`, can power inspector | pushState on click; replaceState on auto-pick / 404 / project switch |

Behaviors:
- `popstate` listener resets local UI: closes inspector, job dialog,
  confirm dialog; pulls `viewMode` and `selectedRunId` from the new URL.
- Project mutation flows (`switchProject`, `selectManagedProject`,
  `createManagedProject`) hard-set `view=tasks&run=` (replaceState).
- 404 on `refreshDetail` clears the `run` param and selection.
- The first `useEffect` (App.tsx:148) writes the route on initial mount via
  `replaceState` to ensure the URL always carries explicit params even when
  the user landed on `/`.

---

## Catalog totals

Sections cover **30 distinct surfaces** rendered out of the App.tsx tree.
Sub-state counts (each is a separately rendered branch in App.tsx):

- Project / launcher: **7** (gate, empty, populated, submitting/error/
  success, switch-project sidebar, projectMeta).
- View mode switcher: **3** (tabs, tasks layout, diagnostics layout).
- Mission Focus: **9** product states from `missionFocus()` × banner sub-
  states (lastError, resultBanner, runtime warnings).
- Task board columns: **4** columns × 2 (populated/empty) + selected/
  drawer card states = **12** card sub-states.
- Recent Activity: **3** (events, history, empty).
- Run Detail Panel: **4** (no selection, loading, loaded packet, action
  bar empty/full).
- ReviewPacket variants: **8** (in_progress, merged, ready, failure,
  blockers, drawer-checks default-open, recovery-note, more-evidence).
- Run Inspector: **4 tabs** × multiple internal sub-states (proof has
  **9**, diff has **5**, logs has **3**, artifacts has **3**) = **20**
  inspector sub-states.
- Job dialog: **6 base states** + dirty-target preflight + advanced
  options × 5 fields + cert dynamic vs static = **~12** sub-states.
- Confirm dialog: **7 variants** (cancel, cleanup, merge-single, merge-
  all, stop-watcher, resume/retry/requeue, generic).
- Diagnostics view: **6 panels** + sub-states (overview metrics x 6,
  commands/issues/landing empty/populated, live runs, history, timeline
  malformed banner) = **~15** sub-states.
- Filters / search: **5 controls** + clear button = **6**.
- Watcher controls: **3** (can-start, can-stop, blocked).
- URL/route: **2 params**, 4 transition flows.
- Toasts / banners / sticky-error: **3 channels**.

**Aggregate: ~120 distinguishable product states / sub-states catalogued
across the App.tsx render tree.**
