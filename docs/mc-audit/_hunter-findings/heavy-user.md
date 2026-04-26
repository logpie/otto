# Heavy-User / Power-User Paper Cuts — Mission Control

Hunter premise: experienced operator running 10–20 jobs, sweeping history,
doing comparative analysis. Got past first-time friction; now hunt what
slows them down.

Schema for each finding:

> `severity, effort, theme, location` — `problem` → `concrete fix`.

---

## 1. No URL persistence for filters / search / view-tab outcome

- **severity:** IMPORTANT
- **effort:** S
- **theme:** url-state / context-memory
- **location:** `App.tsx:115` (`useState<Filters>(defaultFilters)`),
  `App.tsx:88-111` (`readRouteState`/`writeRouteState`),
  `App.tsx:938-977` (Toolbar filters); `client-views.md` §10
  ("Filters are **not** persisted in the URL — refreshing the page resets them").
- **problem:** Filters (type, outcome, query, activeOnly) live in React state
  only. A power user who sets `type=improve`, `outcome=failed`, `query=auth`
  to triage a sweep loses the entire filter on refresh, on Back-from-detail
  through `popstate` (App.tsx:148-162 only restores `view` and `run`), and on
  every project switch (`switchProject` resets data). Cannot share a filtered
  URL with a teammate. Cannot bookmark "all failed improve runs."
- **concrete fix:** Extend `RouteState` (App.tsx:78–111) to encode
  `type`/`outcome`/`query`/`active` as query params. Initialize `filters`
  from `readRouteState()` on mount. On `setFilters`, call
  `writeRouteState(..., "replace")` (replace, not push, to avoid spamming
  history while typing in the search box). Update `popstate` handler at
  App.tsx:148-162 to also reset `filters`.

## 2. No pagination UI — power user with 200+ runs is stuck

- **severity:** CRITICAL
- **effort:** M
- **theme:** pagination
- **location:** `App.tsx:700` (`<History items={data?.history.items || []} totalRows={data?.history.total_rows || 0} ...>`),
  `App.tsx:1377-1421` (`History` component); `client-views.md` §8.5 ("There
  is **no pagination UI** — `page`, `page_size`, `total_pages` from the API
  are unused; only `total_rows` displays").
- **problem:** Server returns `page`, `page_size`, `total_pages`. UI ignores
  them and renders only the first page (typically 50 rows) while the pill
  shows `total_rows` (e.g. 247). The user sees "247" and a 50-row list —
  cannot reach rows 51–247 at all. Flow 16's "200+ rows renders in <1s" only
  covers the first page; everything older is invisible.
- **concrete fix:** Add a Prev/Next + page-size selector under the History
  table (`App.tsx:1418`). Plumb `page` into `RouteState` (so deep links work)
  and into `stateQueryParams` so `/api/state` requests get the right slice.
  Stretch: show "showing 51–100 of 247" subtitle in the panel heading
  (`App.tsx:1380`).

## 3. No sort controls on History or Live Runs

- **severity:** IMPORTANT
- **effort:** M
- **theme:** sort
- **location:** `App.tsx:1377-1421` (History rows render in API order),
  `App.tsx:1323-1375` (LiveRuns), `App.tsx:1386-1393` (table headers are
  plain `<th>`, not interactive); `compareBoardTasks` at `App.tsx:2557`
  sorts the *task board* but not history.
- **problem:** Power user sweeping history wants to sort by **cost desc**
  ("which runs are blowing the budget"), **duration desc** ("which were
  slow"), **age** ("recent failures only"). Today the columns Outcome / Run
  / Summary / Duration / Usage are **non-clickable headers** — no sort
  affordance at all. They get whatever order the server returns.
- **concrete fix:** Make `<th>` cells `role="button"` with `aria-sort`,
  toggle local sort state on click, render an arrow indicator. Either
  client-sort the visible page or pass `sort=cost.desc` to `/api/state` via
  `stateQueryParams` (App.tsx:235).

## 4. No keyboard shortcuts for navigation / actions

- **severity:** IMPORTANT
- **effort:** M
- **theme:** hotkey
- **location:** `App.tsx` global; `client-views.md` lines 49–55 ("There are
  **no global keyboard shortcuts**. The only keyboard interaction beyond
  Tab/Escape is `selectOnKeyboard`"); `selectOnKeyboard` at
  `App.tsx:2690-2694`.
- **problem:** A daily user navigating 200-row history with Tab takes
  hundreds of keystrokes to traverse. There is no `j`/`k` row navigation,
  no `n` to open New Job (despite the button having `data-testid="new-job-button"`
  at App.tsx:615), no `/` to focus search, no `R` to refresh, no `1`/`2` to
  switch Tasks/Diagnostics, no `Esc`-back-from-detail. Mouse-only for
  everything except Tab/Enter.
- **concrete fix:** Add a global `keydown` listener in `App.tsx:113–202`
  that ignores typing in `<input>`/`<textarea>`/`contentEditable` (and
  modal-open state), then maps:
  - `n` → `openJobDialog`
  - `/` → focus the search input (give it a ref / id)
  - `R` → `refresh(true)`
  - `1`/`2` → `navigateView('tasks'|'diagnostics')`
  - `j`/`k` → move `selectedRunId` to next/prev visible run id
    (concat `live.items`, `landing.items`, `history.items` per
    `visibleRunIds` at App.tsx:2576)
  - `Esc` (when no modal) → close inspector
  - `?` → toggle a shortcut cheat-sheet overlay.
  Also surface the shortcuts in a `kbd`-styled help panel — discoverability
  matters as much as the keys.

## 5. No bulk operations beyond Land-all

- **severity:** IMPORTANT
- **effort:** L
- **theme:** bulk-ops
- **location:** `App.tsx:381` (`mergeReadyTasks` is the *only* bulk path),
  Mission Focus "Land all ready" CTA (App.tsx:1135-1178); user-flows.md
  Flow 15 explicitly notes the bulk path is merge-only.
- **problem:** A power user with 12 failed runs across a flaky weekend
  cannot "select all failed → cleanup." Cannot "select 5 stale queue tasks
  → remove." Cannot multi-cancel. The only bulk action in the entire app is
  `merge-all`. Each cleanup/cancel/remove is a click → confirm → wait → next
  row, repeated N times.
- **concrete fix:** Add row checkboxes to TaskBoard cards (App.tsx:1230)
  and History rows (App.tsx:1396). Track a `Set<string>` of selected run
  ids in App state. When non-empty, render a sticky "bulk action bar"
  (mirroring MissionFocus's `Land all`) with available actions derived from
  the *intersection* of `legal_actions` for the selected rows — server can
  add `POST /api/runs/actions/bulk` taking `{run_ids, action}`.

## 6. No log search inside the streaming Logs pane

- **severity:** IMPORTANT
- **effort:** S
- **theme:** search
- **location:** `App.tsx:1574-1586` (`LogPane`), `App.tsx:1583`
  (`<pre data-testid="run-log-pane">` renders `renderLogText(compact.text)`).
- **problem:** A long run's log can hit the 14 000-char `compactLongText`
  cap (App.tsx:1581). User trying to find "ERROR" or "DIAGNOSIS" in a 20k+
  line log scrolls forever. Browser Ctrl-F finds matches, but only inside
  the *truncated* tail — the prefix is gone and there is no "next match"
  jump within the rendered region.
- **concrete fix:** Add a search input to the LogPane toolbar that
  highlights matches in the rendered `<pre>` (wrap matches in `<mark>`),
  shows match count, and `n`/`N` jumps. Better yet: when search is non-empty
  and the truncation cap was hit, fetch the full log via a new
  `/api/runs/{id}/logs?grep=...` endpoint that does server-side filter so
  the 14 000-char cap doesn't hide matches.

## 7. Inspector tab resets to Proof on every run-switch

- **severity:** IMPORTANT
- **effort:** S
- **theme:** context-memory
- **location:** `App.tsx:197` (`setInspectorMode("proof")` inside `selectRun`),
  `App.tsx:305` (`setInspectorMode("proof")` in selection-change effect),
  `App.tsx:485-488` (`showProof` resets); user-flows.md Flow 18 edge case 3
  explicitly flags this as "Document mismatch as finding if expected was sticky."
- **problem:** A user comparing logs across 10 failed runs (the canonical
  power-user flow) opens run A → Logs tab → next run B → **forced back to
  Proof tab** → has to click Logs again. For 10 runs that's 10 extra clicks
  per sweep. Same issue for users who live in Diff or Artifacts.
- **concrete fix:** Persist `inspectorMode` across run changes — drop the
  reset on App.tsx:197 and App.tsx:305. Re-run `loadLogs(reset=true)`,
  `loadDiff()`, etc. according to the *current* `inspectorMode` when a new
  run is selected. Add `?tab=logs|diff|proof|artifacts` to RouteState so a
  deep-link to a specific tab survives refresh.

## 8. No way to compare two runs side-by-side

- **severity:** NOTE
- **effort:** L
- **theme:** comparison
- **location:** `App.tsx:1456-1509` (`RunDetailPanel` shows one run at a
  time), `App.tsx:1511-1572` (`RunInspector` overlay also single-run); no
  "compare" affordance anywhere.
- **problem:** Heavy-user A/B testing two providers ("did codex fix what
  claude couldn't?") needs to see both diffs / both logs. Today: open A,
  copy values, open B, eyeball-diff. No structural support.
- **concrete fix:** Add a "Pin for compare" button on RunDetailPanel that
  stores a second run id. When set, the inspector renders a 2-pane layout
  for the active tab (logs side-by-side, diff side-by-side, stories
  side-by-side). Lower priority but a real win for iterative debugging.

## 9. No re-run / "queue this intent again" affordance

- **severity:** IMPORTANT
- **effort:** S
- **theme:** bulk-ops / context-memory
- **location:** `App.tsx:1456-1509` (RunDetailPanel) — only `legal_actions`
  retry is exposed via ActionBar at App.tsx:1941; no "rerun-with-different-
  provider" path.
- **problem:** Power-user pattern: a run failed on `claude`. They want to
  re-queue the *same intent* but with `provider=codex`, or `effort=high`,
  or `certification=thorough`. Today: copy intent text from the detail
  panel, click "New job", paste, manually adjust dropdowns. ~5 manual steps
  for a one-line action. user-flows.md Flow 13 covers retry but only as
  "preserves provider / certification policy from the prior run" —
  re-queue with *modified* params is not in the UI.
- **concrete fix:** Add a "Re-run…" button next to "Open proof" at
  App.tsx:1497-1502 that opens the JobDialog with `command`, `intent`,
  `provider`, `effort`, `certification` **pre-filled** from `detail`.
  User tweaks one field and submits. One click instead of five.

## 10. No CSV / JSON export for history

- **severity:** NOTE
- **effort:** S
- **theme:** export
- **location:** `App.tsx:1377-1421` (History) — no Download / Export button
  in the panel heading; `cross-sessions/history.jsonl` exists on disk but
  the UI exposes no link.
- **problem:** Heavy user wants weekly cost totals, per-provider averages,
  flake-rate trends. The data is in `history.jsonl` but reaching it
  requires shell access to the server. No "Download as CSV" anywhere in
  the app.
- **concrete fix:** Add a small "Export" button in the History panel
  heading (App.tsx:1380-1383) that hits a new `/api/history/export?format=
  csv|json` endpoint and triggers a file download. Respect the active
  filters so "export all failed improves last week" works.

## 11. No cost / usage dashboard

- **severity:** NOTE
- **effort:** L
- **theme:** export / comparison
- **location:** `cost_display` per row at App.tsx:1364, 1411 — no aggregate
  anywhere. `OperationalOverview` (App.tsx:986-1021) shows six metric
  cards (Active / Needs attention / Ready / Repository / Watcher / Runtime)
  — none about money.
- **problem:** A daily user spending $50–$100/day has no in-app visibility
  into weekly burn, per-provider breakdown, or cost-per-run trends. They
  must `cat history.jsonl | jq` to find out.
- **concrete fix:** Add a 7th metric card (or a new "Cost" diagnostics
  section) that sums `cost_usd` over a configurable window (today / 7d /
  30d), broken down by provider. Also a sparkline chart per provider.
  Reuses data already in `history.jsonl`.

## 12. No notifications when a long-running run finishes

- **severity:** IMPORTANT
- **effort:** S
- **theme:** notification
- **location:** `App.tsx` polling at App.tsx:288-292 / 332-336 — no
  `Notification.requestPermission` call anywhere in the codebase; no
  favicon badge, no document-title update.
- **problem:** user-flows.md Flow 28 ("Tab backgrounded → return") only
  asserts that on return the state reflects reality. Power user kicks off
  a 12-minute build, switches to another tab, has *no signal* that it
  finished — must come back and check. No browser notification, no audio
  beep, no `(3 done!)` prefix in `document.title`, no favicon dot.
- **concrete fix:** When the polling loop detects a previously-running run
  transitioned to a terminal state and the tab is hidden
  (`document.hidden`), call `new Notification("Run X complete", {body: ...})`
  after a one-time `Notification.requestPermission` (gated by a user-pref
  toggle in the sidebar). Also update `document.title` to
  `"(N) Mission Control"` while N runs are unread-completed; clear on
  visibility change.

## 13. No saved templates / recent intents for JobDialog

- **severity:** NOTE
- **effort:** M
- **theme:** context-memory
- **location:** `App.tsx:1999-2179` (`JobDialog`) — `intent`, `taskId`,
  `after`, `provider`, `effort`, `model` all default to empty.
- **problem:** Heavy user types nearly the same intent every day ("build a
  kanban app with X features") with minor variations. No "recent intents"
  history, no saved templates, no autosuggest from the last N intents.
  Also: dialog state is *not* persisted — close + reopen and you start
  from scratch.
- **concrete fix:** Persist last 10 submitted intents to `localStorage`
  (`mc.recentIntents`); render a "Recent" `<datalist>` below the textarea.
  Add a "Save as template" button that names a payload (`name → {command,
  subcommand, intent, provider, effort, model, certification}`) and shows
  templates as quick-fill chips at the top of the dialog.

## 14. No "pin" / "favorite" affordance for frequently-checked runs

- **severity:** NOTE
- **effort:** S
- **theme:** context-memory
- **location:** `App.tsx:1456-1509` RunDetailPanel — no pin button. The
  selection memory is purely the URL `?run=`.
- **problem:** Operator monitoring a baseline run for a week (e.g.
  `golden-kanban-prod`) has to find it in history every time. No "pinned
  runs" panel, no recently-viewed list (RecentActivity at App.tsx:1280-1321
  is *event*-based, not user-history-based).
- **concrete fix:** Add a "Pin run" button on RunDetailPanel that stores
  the run_id in `localStorage` per-project. Render a "Pinned" panel on the
  Tasks layout (above RecentActivity) with the pinned ids resolved against
  current state. Bonus: a "Recently viewed" list (last 10 distinct run ids
  the user opened, also localStorage).

## 15. Project switch tears down all state — no instant cross-project switch

- **severity:** IMPORTANT
- **effort:** L
- **theme:** context-memory
- **location:** `App.tsx:546` (`switchProject` calls
  `POST /api/projects/clear`, resets `data` to null), `App.tsx:556-571`;
  `client-views.md` §1.7 ("Resets all detail/log/inspector state, clears
  `data`, returns to launcher.").
- **problem:** Power user toggling between two projects (otto-dev ↔
  bench-suite) hits the launcher every time, picks the row, waits for
  state to load. No quick-switcher, no recent-projects dropdown in the
  sidebar. Each switch is ~3 clicks + a wait.
- **concrete fix:** Add a project-picker `<select>` in the sidebar (or a
  Cmd-K palette) that lists `projectsState.projects` and on change calls
  `selectManagedProject` directly without going through
  `switchProject`/`clear`. Keep the launcher as the primary "first time"
  experience but offer the dropdown for switching.

## 16. Watcher status only visible in the sidebar (left rail)

- **severity:** NOTE
- **effort:** S
- **theme:** context-memory
- **location:** `App.tsx:777-783` (`ProjectMeta` in sidebar) — `Watcher:
  running pid <n>` row; `OperationalOverview` (App.tsx:986-1021) repeats
  it in Diagnostics only.
- **problem:** When the right-rail RunDetailPanel is expanded or the
  RunInspector overlay is open, the user is focused on the workspace
  center and can easily miss that the watcher died. There is no
  always-visible at-a-glance indicator (e.g. a colored dot in the toolbar
  or favicon).
- **concrete fix:** Add a small watcher-state pill to the toolbar at
  App.tsx:908-984 (next to the Refresh button), color-coded
  green/yellow/red, click → focuses the sidebar control. Also reflect
  state in the favicon (green dot when running, red when stale, gray
  when stopped).

## 17. Dirty-target confirm checkbox doesn't remember consent within a session

- **severity:** NOTE
- **effort:** S
- **theme:** context-memory
- **location:** `App.tsx:2021-2023` (`targetConfirmed` resets on
  `project.path` change), `App.tsx:2089-2108` (target-guard render); user-
  flows.md Flow 8 edge case 1 ("Switching projects mid-dialog resets
  `targetConfirmed` to false").
- **problem:** Heavy user submitting 6 jobs in a row against a known-dirty
  worktree must tick the "I understand the worktree is dirty" checkbox 6
  separate times. The dirty state hasn't changed; the user already
  acknowledged it; the friction is pure repetition.
- **concrete fix:** Persist `targetConfirmed=true` for the current
  `project.path` in `sessionStorage` for the lifetime of the tab; reset
  only when `project.path` changes OR `project.dirty` flips clean→dirty
  again. Skip the checkbox when sessionStorage already records consent
  for this path+dirty-fingerprint.

## 18. Diff viewer is unified-only — no side-by-side option

- **severity:** NOTE
- **effort:** L
- **theme:** comparison
- **location:** `App.tsx:1727-1769` `DiffPane` — single `<pre>` with
  per-line `diff-add|diff-del|diff-hunk|diff-meta|diff-context` classes;
  no toggle between unified and split view.
- **problem:** Reviewing a non-trivial multi-file diff in unified format
  is painful for power users used to GitHub/IntelliJ split view —
  context lines repeat, hunks are dense, eye has to track `-` vs `+`
  alternation.
- **concrete fix:** Add a "Unified | Split" toggle in the diff toolbar.
  Parse hunks and render two `<pre>` columns per file (left = old, right =
  new) with line-number gutters. Persist the choice in localStorage.

## 19. No way to queue multiple related intents at once

- **severity:** NOTE
- **effort:** M
- **theme:** bulk-ops
- **location:** `App.tsx:1999-2179` `JobDialog` submits exactly one
  command per close.
- **problem:** Power user wants to queue "build kanban", "build blog",
  "build cli" with the same provider/effort. Today: open dialog, fill,
  submit, dialog closes, open again, refill (most fields reset), submit,
  repeat. No "Queue another" button that keeps the dialog open with the
  shared fields prefilled.
- **concrete fix:** Add a secondary submit button "Queue & add another"
  that calls `JobDialog.submit` with `keepOpen=true` — clears only `intent`
  and `taskId` while preserving command, subcommand, provider, effort,
  model, certification. Heavy user can rip through 5 jobs with the same
  config in seconds.

## 20. RunDetailPanel + RunInspector both use the same data — no
"docked" preview while browsing

- **severity:** NOTE
- **effort:** L
- **theme:** comparison
- **location:** `App.tsx:1456-1509` (RunDetailPanel right rail),
  `App.tsx:1511-1572` (RunInspector overlay). The right rail shows
  ReviewPacket; the overlay shows full Proof/Diff/Logs/Artifacts.
- **problem:** Power user sweeping history wants to *quickly preview* the
  proof report for each run without committing to opening the full
  inspector overlay (which `aria-hidden`s the whole workspace at
  App.tsx:603, 621). Today the right rail shows ReviewPacket which is a
  good summary, but the proof body / log tail is one click + overlay
  away. There's no "expand right rail to half-width" option.
- **concrete fix:** Add a "wide rail" toggle that doubles RunDetailPanel
  width and embeds a compact LogPane / ProofPane preview *inline* (no
  overlay), so j/k traversal of history rows updates a live preview pane
  to the right. Power user can sweep 50 rows in seconds without the
  overlay-open-close dance.

## 21. Refresh polling pauses are invisible — no "last updated" timestamp

- **severity:** NOTE
- **effort:** S
- **theme:** context-memory
- **location:** `App.tsx:288-292` polling, `App.tsx:978-981` toolbar
  refresh status pill — only shows transient "refreshing" / "refresh
  failed" labels via `refreshLabel`.
- **problem:** Power user returns to a tab after lunch — was the data
  updated 3 seconds ago or 30 minutes ago (e.g. polling broke silently)?
  Need to compare timestamps across runs and don't trust the freshness.
- **concrete fix:** Track `lastSuccessfulRefreshAt` and render `Updated
  3s ago` next to the Refresh button. Tick once per second from a single
  global timer; flip to red when > 2× the polling interval has elapsed
  without success.

## 22. Search query is substring-only with no field qualifiers

- **severity:** NOTE
- **effort:** S
- **theme:** search / filter
- **location:** `App.tsx:962` (search input bound to `filters.query`),
  `App.tsx:2466` (`boardTaskMatchesFilters` does
  `query.toLowerCase()` substring match across
  `id/title/summary/status/branch/reason/proof`).
- **problem:** Power user wants `branch:feature/auth`, `cost:>5`,
  `provider:codex`. Today the only operator is implicit substring across
  every field — searching for "auth" matches branches, summaries,
  reasons, statuses indiscriminately. No way to scope.
- **concrete fix:** Parse `field:value` tokens in `boardTaskMatchesFilters`:
  `branch:foo`, `provider:codex`, `cost:>5`, `outcome:failed`. Fall back
  to substring for unqualified terms. Document the syntax in a tooltip on
  the search input.

## 23. Filter `outcome=all` and `type=all` are not reflected in the URL or "active filters" summary

- **severity:** NOTE
- **effort:** S
- **theme:** filter / context-memory
- **location:** `App.tsx:938-977` Toolbar — Clear button always present
  even when filters are at defaults (App.tsx:976); no visible "active
  filters" pill count.
- **problem:** With four filter controls at non-default values it's hard
  to tell at a glance "what am I currently filtering on?" — especially
  after coming back from a 30-second tab switch. The Clear button is
  always shown so it doesn't signal "filters are active."
- **concrete fix:** Render a small "3 filters active" badge next to the
  search input when any filter differs from defaults. Make the Clear
  button visually muted when no filters are active (or hide it entirely).

## 24. No multi-select on intent text for the JobDialog (paste-multiline → one job each?)

- **severity:** NOTE
- **effort:** L
- **theme:** bulk-ops
- **location:** `App.tsx:2019-2055` JobDialog submit logic — `intent` is
  a single string sent in a single POST.
- **problem:** Power user has a list of 5 features in a markdown bullet
  list. Wants to queue 5 separate `improve feature` runs. Today: copy
  bullet 1, open dialog, paste, submit, repeat 5x.
- **concrete fix:** Add a "split mode" toggle in advanced options:
  when on, treat blank-line-separated paragraphs in the intent textarea
  as N separate jobs and queue them sequentially with the same
  command/provider/effort. Confirm dialog summarizes "Queue 5 improve
  feature jobs?".

## 25. RecentActivity is not personalized — shows server events, not user history

- **severity:** NOTE
- **effort:** M
- **theme:** context-memory
- **location:** `App.tsx:1280-1321` `RecentActivity` — top-4 events
  (operator-action log) interleaved with top-4 history rows. No record
  of "runs the *current user* opened recently."
- **problem:** Power user opens run X, switches projects, comes back —
  no "you were just looking at X" link. The launcher doesn't remember the
  last-active project either.
- **concrete fix:** Persist `lastViewedRunIds: Record<projectPath,
  string[]>` and `lastActiveProject: string` to `localStorage`. On
  project mount, scroll RecentActivity to highlight last-viewed runs;
  on launcher mount (App.tsx:574), pre-select the last-active project
  row.

## 26. No deep-link to a specific inspector tab or proof artifact

- **severity:** NOTE
- **effort:** S
- **theme:** url-state
- **location:** `client-views.md` §15 ("URL / Route State Reference"):
  only `view` and `run` are URL params. App.tsx:486 (`showProof`) /
  ApIp.tsx:464 (`showLogs`) / etc. set local state only.
- **problem:** Sharing a teammate "look at run X's diff for this file" is
  *just* a `?run=X` URL — they land on the proof tab, have to click Diff,
  scroll to find the file. No `?run=X&tab=diff&file=src/foo.ts` deep link.
- **concrete fix:** Extend `RouteState` with `tab` and `artifact|diffFile`
  params. On mount, hydrate `inspectorMode` and the artifact/diff
  selection from URL. On tab/file change, `replaceState` so refresh
  preserves the deep link.

---

## Summary

The Mission Control web app is comfortable for first-time users (clear
flows, good empty states, accessible focus management) but is built around
the assumption that users open one or two runs at a time and don't sweep
history. For an operator running 10–20 jobs/day, doing comparative
analysis, and triaging failures across providers, the app surfaces almost
no power-user features: no URL-persisted filters, no pagination UI, no
sortable columns, no keyboard shortcuts beyond Tab/Enter, no bulk
operations beyond Land-all, no log search, no notifications when long
runs finish in a backgrounded tab, no cost dashboard, no re-run-with-
modified-params affordance, no compare-two-runs, no saved templates, no
pinned runs, no instant cross-project switch, no inspector-tab persistence
across run-switches, no export. Each of these is a discrete paper cut;
together they bottleneck heavy use to the throughput of the slowest
mouse-only manual flow.

Total findings: 1 CRITICAL, 13 IMPORTANT, 12 NOTE
