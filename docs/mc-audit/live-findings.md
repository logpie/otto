# Mission Control — Live Web-As-User Findings (Phase 5)

Real-LLM dogfood run of W1 (first-time user) and W11 (operator-day). Each
finding is keyed by its source (W1/W11 step number, screenshot, console
log, /api/state response, etc.) so it can be reproduced.

Severity legend:
- **CRITICAL** — user-blocking or wrong-answer; ship-stopper.
- **IMPORTANT** — clear UX/correctness defect; worth fixing this week.
- **NOTE** — minor papercut, cosmetic, or speculative.
- **INFRA** — environment/test-rig issue, not Otto's fault.

## Run metadata

| Field | Value |
|------|-------|
| Branch | worktree-i2p |
| Provider | claude (Sonnet via SDK) |
| Real-LLM cost guard | `OTTO_ALLOW_REAL_COST=1` |
| Bundle freshness | `OTTO_WEB_SKIP_FRESHNESS=1` (pre-existing untracked App.tsx + styles.css edits) |
| Run id W1 | `2026-04-26-011318-fd13c6` (`bench-results/web-as-user/2026-04-26-011318-fd13c6/W1/`) |
| Run id W11 | `2026-04-26-01XXXX` (running) |
| W1 wall time | 222s (≈3min 42s end-to-end Playwright + LLM build) |
| W1 verdict | **FAIL** (4 soft-assert failures collected) |

---

## W1 findings

### W1-IMPORTANT-1: `/api/state` schema differs from Python `read_live_records` types — no one canonical record shape

- **Severity:** IMPORTANT
- **Symptom:** Initial harness used `body["live_runs"]["items"][i]["record"]["status"]`
  (mirroring the TUI's typed records), but `/api/state` actually returns
  `body["live"]["items"][i]["status"]` — flat keys, no `record` wrapper.
  Same divergence for `history` (no `row` wrapper).
- **Reproduction:** `curl http://127.0.0.1:<port>/api/state | jq '.live.items[0]'`
  vs. `from otto.runs.registry import read_live_records`. The browser
  client (`otto/web/client/src/api.ts`) reshapes records before they reach
  the SPA, but external clients (`scripts/`, future MCP, third-party tools)
  must guess the public shape.
- **Hypothesis:** `otto/web/serializers.py` flattens for transport; the
  internal `runs/schema.py` stays nested. There's no documented schema and
  no shared TypeScript-style codegen.
- **Suggested fix:** publish a typed `/api/state` schema (pydantic on the
  server side surfaces JSON Schema for free) and link it from
  `docs/mc-audit/server-endpoints.md`. Or expose a sibling
  `/api/runs/<id>` raw view that mirrors `read_live_records`.

### W1-NOTE-1: "Otto is committing runtime bookkeeping files" stderr fires twice on first run

- **Severity:** NOTE
- **Symptom:** When the first queue job is enqueued and the watcher starts,
  the message
  > `Otto is committing runtime bookkeeping files so queue/merge can run with a clean tree: .gitignore, .gitattributes`
  appears twice in the in-process backend's stderr.
- **Reproduction:** W1 step 6→7 (single submit, single watcher start).
- **Hypothesis:** Both `enqueue` and `start_watcher` independently invoke
  the bookkeeping commit, and the second invocation no-ops but still logs.
  See `otto/mission_control/service.py` (search for "bookkeeping").
- **Suggested fix:** make the second call a silent no-op (already
  idempotent, just don't log if there's nothing to commit).

### W1-IMPORTANT-2: JobDialog "Submit is disabled." validation hint persists during in-flight submit

- **Severity:** IMPORTANT
- **Symptom:** Screenshot
  `bench-results/web-as-user/<run-id>/W1/05-submitted.png` shows the
  dialog with the intent textarea fully filled, `aria-busy` set, but the
  validation hint reads "Submit is disabled." Confusing because the
  submit DID succeed (live row appeared in /api/state immediately
  afterward).
- **Reproduction:** W1 step 6 — fill intent via Playwright `fill()` and
  click submit immediately. The hint shows for the first React render
  cycle after the click.
- **Hypothesis:** `submitDisabled` is computed from local state that
  doesn't update synchronously when `setSubmitting(true)` runs. The
  validation hint's `submitDisabled && !submitting` condition fires before
  React commits the `submitting=true` flip.
- **Suggested fix:** in `otto/web/client/src/App.tsx` JobDialog form,
  hide the validation hint while `submitting` is true (combine the guards):
  `{submitDisabled && !submitting && !pendingSubmit && (…)}`. Or move the
  intent-required check into a layout-effect so the validation message
  computation can happen post-render.

### W1-NOTE-2: First-run "Start first build" is a `mission-new-job-button` testid wearing a different label

- **Severity:** NOTE
- **Symptom:** The big primary "Start first build" CTA in the mission
  focus on first run AND the smaller "New job" CTA on subsequent runs
  share `data-testid="mission-new-job-button"` — only the visible text
  changes (`focus.firstRun ? "Start first build" : "New job"`).
- **Reproduction:** Inspect `app.tsx:1829`.
- **Hypothesis:** Intentional shared testid. Documenting because external
  test harnesses may want to scope by visible text and this is invisible
  to a casual reader.
- **Suggested fix:** Either keep this and document it in
  `docs/mc-audit/user-flows.md`, or split into
  `mission-start-first-build-button` for the first-run case so dashboards
  can A/B them separately.

### W1-CRITICAL-1: Inspector tab buttons (Diff / Proof / Artifacts) are click-blocked by the log `<pre>` pane

- **Severity:** CRITICAL — UI claims to expose "Code changes / Logs / Result / Artifacts" tabs in the run inspector, but only the first-shown tab (Logs) is reachable. Clicking another tab times out: Playwright reports
  > `<pre tabindex="0" data-testid="run-log-pane" aria-label="Run log output" class="log-pane log-content">…</pre> from <section role="dialog" tabindex="-1" aria-modal="true" class="run-inspector"…> subtree intercepts pointer events`
- **Reproduction:** W1 step 10. After the kanban build completes, click on the
  task card to open the inspector (Logs is shown by default), then attempt to
  click the "Code changes" / "Result" / "Artifacts" tab buttons in the
  toolbar. Each click hits the log `<pre>` element and is swallowed.
  Screenshot:
  `bench-results/web-as-user/2026-04-26-011318-fd13c6/W1/09-tab-logs.png`.
- **Hypothesis:** The `<pre data-testid="run-log-pane">` has a stacking
  context (z-index or `position:`) that puts it above the tab toolbar inside
  the `.run-inspector` modal. Likely a CSS issue in the recent inspector-
  layout edit (`otto/web/client/src/styles.css` modified, untracked).
- **Suggested fix:** scope the log-pane's stacking to its own panel — give
  the toolbar `z-index: 1` and the `.log-pane` `z-index: 0`, or move the
  log-pane out of the toolbar's flow. Add a Playwright regression test that
  clicks all four inspector tabs in sequence with a short timeout.

### W1-IMPORTANT-3: `artifact_mine_pass` reports queue-spawned live runs as missing their session dir — but the session dir lives inside the worktree, not the project root

- **Severity:** IMPORTANT
- **Symptom:** Failure
  > `live run '2026-04-26-011325-2615a8' has no session dir at /var/folders/.../otto-mc-web-px5_0doh/otto_logs/sessions/2026-04-26-011325-2615a8`
  yet the actual session is at
  `/var/folders/.../otto-mc-web-px5_0doh/.worktrees/build-a-small-kanban-board-web-app-with-4a6d9e/otto_logs/sessions/2026-04-26-011325-2615a8/`
  (inside the queue worktree).
- **Reproduction:** W1 finally-block runs `artifact_mine_pass(project_dir, failures)` (`scripts/web_as_user.py:126`) which uses `paths.live_runs_dir(project_dir)` and `paths.session_dir(project_dir, run_id)`. For queue runs, the session lives in the worktree, not the project root.
- **Hypothesis:** Either:
  - **Bug in `paths.session_dir`**: it should consider the run's recorded
    `cwd` (the worktree) when resolving sessions for queue domain. Right
    now it joins the project_dir directly.
  - **Bug in the live-record schema**: the live-record's `cwd` already says
    the worktree — `read_live_records` returns it and downstream tooling
    must use it instead of `project_dir`.
- **Suggested fix:** in `artifact_mine_pass`, pull `cwd` off the live record
  and call `paths.session_dir(Path(record_cwd), run_id)` if present. Better,
  surface a helper `paths.session_dir_for_run_record(record)` so every
  consumer agrees.

### W1-IMPORTANT-4: Queue + supervisor lockfiles leak into project root (untracked, no `.gitignore` coverage on a fresh project)

- **Severity:** IMPORTANT
- **Symptom:** After a single first-time-user build, the project root contains:
  ```
  .otto-queue-commands.jsonl.lock
  .otto-queue-state.json
  .otto-queue.lock
  .otto-queue.yml
  .otto-queue.yml.lock
  ```
  (see `bench-results/web-as-user/2026-04-26-011318-fd13c6/W1/project-files.txt`)
  None of these are listed in the throwaway project's `.gitignore` (which
  Otto auto-commits as `otto_logs/\n.worktrees/\n.otto-queue*.lock\n`). The
  `.lock` files are covered, but `.otto-queue-state.json`,
  `.otto-queue-commands.jsonl.lock` (yes, the lock IS covered, ok),
  `.otto-queue.yml` are not, so they show up in `git status`.
- **Reproduction:** Same project_files.txt listing as above. Run `git status`
  in the throwaway project after the build — `.otto-queue-state.json` and
  `.otto-queue.yml` will appear as untracked.
- **Hypothesis:** The auto-`.gitignore` in `_create_managed_project`
  (`otto/web/app.py:281`) only includes `.otto-queue*.lock`. Should be
  `.otto-queue*` (wildcard) to also cover `.otto-queue-state.json`, the
  `.otto-queue.yml`, and `.otto-queue-commands.jsonl`.
- **Suggested fix:** in `otto/web/app.py:281` change
  `.otto-queue*.lock` → `.otto-queue*` (and consider also adding
  `.otto-queue-commands.jsonl`).

### W1-NOTE-3: `cost_display` stays `"…"` for the entire build (no streaming cost surface in the UI)

- **Severity:** NOTE
- **Symptom:** Polling `/api/state` for the running queue task shows
  `cost_display: "…"` and `cost_usd: null` from start (00:00) through
  3:11 wall time when the build went terminal. Cost only appears
  *after* terminal — there's no incremental cost telemetry to look at
  while the run is in flight.
- **Reproduction:** Curl `/api/state` repeatedly during a long build; watch
  `live.items[0].cost_display`.
- **Hypothesis:** Cost is computed from session metadata that only updates
  on terminal events (or large checkpoints). Not a bug per se, but the
  UI showing `"…"` indefinitely is uninformative — an "in-flight" hint
  ("computing…", or last checkpoint cost + `+`) would help.
- **Suggested fix:** when `cost_usd` is null but the run has been in flight
  > 30s, render "—" or "(running)" in the UI rather than the placeholder
  ellipsis, which reads as "loading" forever.

(W11 findings appended live as the run progresses)

---

## W11 findings

Run id: `2026-04-26-011751-e472c7` (`bench-results/web-as-user/2026-04-26-011751-e472c7/W11/`).
Wall time: ~3 minutes (terminated early because the standalone CLI build finished
and there were no queue items because the JobDialog enqueues failed silently —
see W11-CRITICAL-1 below). Verdict: **FAIL** (7 soft-assert failures collected).

### W11-CRITICAL-1: JobDialog requires a "dirty target" confirmation checkbox after Otto auto-dirties the project — but neither the CLI doc nor the UI says so

- **Severity:** CRITICAL — silently breaks the documented operator-day workflow
  on a freshly initialized project. The user clicks "New job", fills intent,
  and clicks "Queue job" — the button does nothing, no error toast, no
  status text. The form caption reads "Confirm the dirty target project
  above to enable queueing." but on a fresh project the user has done
  nothing dirty — Otto did, by auto-committing bookkeeping files and
  checking out the build branch on the project root.
- **Reproduction:** W11 step 5. Sequence:
  1. Spawn `otto build` standalone in a fresh project — Otto auto-checks
     out a build branch in the project root.
  2. Open Mission Control web, click "New job", fill intent, click "Queue
     job".
  3. Submit is silently disabled because `target_dirty=True` and
     `target_confirmed=False`.
  Screenshot:
  `bench-results/web-as-user/2026-04-26-011751-e472c7/W11/06-watcher.png`.
- **Hypothesis:** The dirtiness check is correct — Otto can lose the user's
  uncommitted edits if the queue task swaps branches mid-air. But two issues:
  1. The CTA-text ("Queue job") gives no hint — should change to "Confirm
     dirty target above". Or the checkbox label should be the primary CTA.
  2. The project becomes dirty *because Otto auto-committed bookkeeping
     files and checked out a build branch*. From a user's POV the project
     was clean 5 seconds ago. Either suppress the dirty-target warning when
     the only "dirtiness" is Otto's own bookkeeping commit/branch checkout,
     or surface that fact in the warning ("Project is on branch
     `build/...` because of an in-flight build").
- **Suggested fix:** in `otto/web/client/src/App.tsx` JobDialog, when
  `target_dirty` is true but the dirty source is internal Otto state
  (build branch checkout, bookkeeping commits), down-grade the warning to
  an info-level note and pre-check the confirmation. Or, on the server
  side, treat `branch starts with "build/"` as "in-flight build, expected"
  and don't bubble it up as a dirty-target.

### W11-IMPORTANT-1: Sidebar TASKS counter says "queued 0 / ready 0 / landed 0" while Task Board shows a running task

- **Severity:** IMPORTANT
- **Symptom:** The standalone CLI build was running and visible on the
  Task Board (column "QUEUED / RUNNING — 1") but the sidebar TASKS
  counter showed "queued 0 / ready 0 / landed 0" and the IN FLIGHT
  counter said "0".
  Screenshot: `bench-results/web-as-user/2026-04-26-011751-e472c7/W11/04-standalone-live.png`.
- **Reproduction:** W11 step 4. Standalone `otto build` invocation in
  project root, then load Mission Control.
- **Hypothesis:** TASKS counter pulls from `landing.counts` which only
  knows about queue-domain runs. The standalone CLI build is
  `domain: "atomic"` and is excluded from the landing counts.
- **Suggested fix:** include atomic-domain in-flight runs in the IN FLIGHT
  counter at minimum. The "1 task in flight" banner does include it (good),
  but the sidebar contradicts — pick one source of truth. Likely add
  `landing.counts.in_flight_atomic` and have the sidebar display
  `queue + atomic` for IN FLIGHT.

### W11-IMPORTANT-2: Standalone `otto build` registers as `domain: "atomic"` — public API rename or document it

- **Severity:** IMPORTANT
- **Symptom:** `otto build` (standalone CLI) creates a live record with
  `domain: "atomic"`. Mission Control's wording (sidebar, dialogs, focus
  banner) calls it "build" or "task in flight". The disjoint naming
  trips up any external integration that filters by domain (the
  web-as-user harness itself fell into this — searched `domain == "build"`
  for 60s, never found the run).
- **Reproduction:**
  ```
  curl http://127.0.0.1:<port>/api/state | jq '.live.items[].domain'
  # → "atomic"  (when standalone otto build is the only run)
  ```
- **Hypothesis:** Internal naming evolved. `atomic` makes sense vs.
  `merge` and `queue` (it's a one-shot run), but `build` is the user-
  facing name.
- **Suggested fix:** either rename the domain to `build` (breaking change
  but better DX), or document the mapping in
  `docs/mc-audit/server-endpoints.md`: `cli otto build → domain "atomic"`.

### W11-IMPORTANT-3: `start-watcher-button` stays disabled even after a queue task arrives via CLI watcher subprocess (was disabled b/c queue empty after Step 5 enqueue failures)

- **Severity:** IMPORTANT
- **Symptom:** Failure
  > `start watcher click failed: Locator.click: Timeout 5000ms exceeded.
  >  …<button disabled type="button" aria-busy="false" data-testid="start-watcher-button"
  >    aria-describedby="watcher-action-hint" title="Start watcher when queued tasks should run.">Start watcher</button>`
  The button stays `disabled` for the full 30s wait window. In W11 this is
  a *consequence* of W11-CRITICAL-1 (no queue items → no watcher needed).
- **Reproduction:** W11 step 6.
- **Hypothesis:** `canStartWatcher(data)` returns false when `landing.counts.queued == 0`.
- **Suggested fix:** the disabled-state's `title` already says "Start
  watcher when queued tasks should run." — that's good. But after the user
  has tried to enqueue (W11-CRITICAL-1) and silently failed, this is the
  *second* dead-end with no error feedback. Surface a toast or banner
  when the prior queue submit was rejected.

### W11-CRITICAL-2: `<div class="modal-backdrop">` lingers after JobDialog dismiss and intercepts every subsequent click on the page

- **Severity:** CRITICAL
- **Symptom:** Failure
  > `enqueue post failed: Locator.click: Timeout 5000ms exceeded.
  >  …<div role="presentation" class="modal-backdrop">…</div> intercepts pointer events`
  Triggered when trying to open a *second* JobDialog after the first
  closed (because submit was rejected silently from W11-CRITICAL-1, but
  the user perceives the form as closed).
- **Reproduction:** W11 step 5, second iteration. Click "New job" → fill
  intent → click "Queue job" → modal appears closed but backdrop layer
  remains, blocking every subsequent click anywhere on the page until a
  hard reload.
- **Hypothesis:** When `submitDisabled` blocks the submit, React doesn't
  reset the `dialogOpen` / backdrop state correctly. Or the backdrop
  cleanup hook depends on a successful submit path.
- **Suggested fix:** in `otto/web/client/src/App.tsx`, ensure
  `setDialogOpen(false)` (or equivalent) fires from a `useEffect`
  cleanup or a try/finally around the submit. Add a Playwright
  regression: open dialog, dismiss without submit, ensure pointer events
  are restored.

### W11-IMPORTANT-4: orphan watcher subprocesses survive `backend.stop()` cleanup

- **Severity:** IMPORTANT
- **Symptom:** After W1 finished (and the `_throwaway_project` cleanup
  removed the temp dir), `ps aux | grep "otto queue run"` showed two
  orphan watchers from earlier runs:
  ```
  21101 ... /Users/.../otto queue run --no-dashboard --concurrent 2
                cwd: /private/var/folders/.../otto-mc-web-px5_0doh
  1783  ... (same)
                cwd: /private/var/folders/.../otto-mc-web-jsaxjnz_
  ```
  Both `cwd` directories had been `rmtree`-d.
- **Reproduction:** Run W1, let it finish, `ps aux | grep "otto queue run"`.
- **Hypothesis:** `MCBackend.stop()` joins the uvicorn server thread but
  does not signal the watcher subprocess that the service spawned via
  `subprocess.Popen`. The watcher detects no queue work, but it doesn't
  exit because no SIGTERM is sent.
- **Suggested fix:** add a `service.stop_watcher_blocking()` in
  `MCBackend.stop()` that calls
  `os.killpg(watcher.pid, SIGTERM)` and waits up to 5s. Also have the
  test conftest's `_assert_no_orphan_watcher` hook loud-fail when an
  orphan is detected (it currently no-ops if `stop_requested_at` is set,
  but the bug is that stop is *never* requested in this code path).

### W11-NOTE-1: Standalone `otto build` against a fresh project root checks out the build branch on the project HEAD, leaving the project on `build/...` after the run

- **Severity:** NOTE
- **Symptom:** After W11's standalone `otto build` finished, the project's
  git worktree was on `build/add-a-get-tasks-endpoint-that-returns-…`,
  not `main`:
  ```
  $ git worktree list
  /private/var/folders/.../otto-mc-web-6a775g1t  9341157 [build/add-a-get-tasks-...]
  ```
- **Reproduction:** W11 final state (`worktrees.txt`).
- **Hypothesis:** Standalone `otto build` (no `--allow-dirty` quirk
  around branches?) operates on the project's working copy, not in a
  worktree, so the project's HEAD lands on the build branch. Compared to
  the queue path which uses `.worktrees/<task>/` and leaves the project
  HEAD untouched.
- **Suggested fix:** at the very least, surface this in the post-run
  summary toast: "Build branch `build/…` is checked out on your project;
  run `git checkout main` to return." Or, opt-in to the worktree path
  for standalone builds too.

---

## W2 findings

Run id: `2026-04-26-020826-9de329` (`bench-results/web-as-user/2026-04-26-020826-9de329/W2/`).
Wall time: 165s (2 of 3 builds drained successfully ~$0.40 each, third was cancelled).
Verdict: **FAIL** (6 soft-assert failures collected).

### W2-CRITICAL-1: Cancelled queue task vanishes — no live row, no history row, but its manifest entry leaks in queue state

- **Severity:** CRITICAL — silently breaks operator audit. The user cancels a queued job; the UI's POST returns 200; the cancelled task then disappears from /api/state entirely. No history row, no live row, but it lingers in the queue-state JSON with a missing manifest.
- **Symptom:** After enqueueing 3 builds (W2_INTENT_A/B/C) and cancelling the 3rd via `/api/runs/<run_id>/actions/cancel`, the final state shows only 2 history rows (both `success`) and no rows at all for the cancelled task. Yet `artifact_mine_pass` reports
  > `queue task 'add-a-date-utility-module-that-exports-221a3c' listed in state but has no manifest at /var/folders/.../otto_logs/queue/add-a-date-utility-module-that-exports-221a3c/manifest.json`
  Confirmed by `final-state.json` history queue rows = 2 (only the two non-cancelled builds), and zero rows with `terminal_outcome=cancelled` or `status=cancelled`.
- **Reproduction:** W2 step 6→7. Compare against W12a where atomic-domain cancellation DOES surface in history (`atomic | cancelled | cancelled`), so the bug is specifically queue-domain.
- **Hypothesis:** `MissionControlService.execute(..., "cancel")` for queue domain unsubscribes the task from the live registry but skips writing a terminal history row. Likely in `otto/mission_control/service.py` near the queue cancel branch (~line 1678). Atomic-domain has a separate adapter that DOES write the history.
- **Suggested fix:** in the queue-domain cancel handler, write a terminal history row with `terminal_outcome="cancelled"` and `status="cancelled"` before unsubscribing from live. Also clean up the queue manifest entry so artifact-mine-pass stops complaining about missing manifests.

### W2-IMPORTANT-1: Browser auto-fetches `GET /api/runs/queue-compat:<task-id>?...` and gets 404

- **Severity:** IMPORTANT
- **Symptom:** Console error during W2:
  > `Failed to load resource: the server responded with a status of 404 (Not Found)
  >  http://127.0.0.1:53320/api/runs/queue-compat%3Acreate-a-tiny-calculator-html-page-with-c53170?type=all&outcome=all&query=&active_only=false&history_page_size=25`
  The SPA tries to look up a queue-compat:* run via the per-run detail endpoint with `/api/state` query params appended (looks like a URL routing mistake — the query params suggest the SPA was building a /api/state URL but routed to /api/runs/<id>).
- **Reproduction:** W2 step 5–7. Triggered when the user clicks/inspects the cancelled queue row before it disappears. See `bench-results/web-as-user/2026-04-26-020826-9de329/W2/console.json` and `network-errors.json`.
- **Hypothesis:** `otto/web/client/src/api.ts` (or a hook in App.tsx) constructs a per-run URL but appends `/api/state` query params instead of using bare `/api/runs/<id>`. Possibly the run-id selector swaps to a queue-compat alias that the backend doesn't accept on the `/api/runs/{run_id}` route.
- **Suggested fix:** strip `?type/outcome/...` from the `/api/runs/<id>` request, or accept queue-compat run-IDs at that endpoint and resolve to the underlying run.

### W2-IMPORTANT-2: 30-second `wait_until="networkidle"` page.goto often does not settle

- **Severity:** IMPORTANT — affects external automation as well as Playwright tests
- **Symptom:** In W12b and W13, `page.goto(url, wait_until="networkidle", timeout=30_000)` raised `Page.goto: Timeout 30000ms exceeded.` even when the page eventually loaded (the test's downstream assertions found the React shell up).
- **Reproduction:** Re-run any scenario that does `page.goto(networkidle)` against an MC backend with an in-flight queue. See `/tmp/w12b.log` line `shell load failed: Page.goto: Timeout 30000ms exceeded`.
- **Hypothesis:** Mission Control opens persistent SSE / long-poll connections (event stream, etc.) that prevent the network ever reaching "idle." A page that never has 0 in-flight requests for 500ms can't satisfy networkidle.
- **Suggested fix:** the SPA should not hold a connection open before user interaction. Either use Server-Sent Events with `Connection: close` semantics for the initial page load, or document `wait_until="domcontentloaded"` as the canonical strategy for browser automation. Update the recorded-fixture browser tests + this harness accordingly.

### W2-IMPORTANT-3: Watcher concurrency runs cancelled-and-other-jobs in parallel — Step 5 cancellation racing with the watcher's pickup

- **Severity:** IMPORTANT (or NOTE)
- **Symptom:** When 3 jobs were enqueued and watcher started, all 3 (potentially) entered "running" within seconds. The harness saw `running, running` immediately after `cancel POST status=200`, indicating the cancel was applied to a queued task but the watcher still ran the other two. Total wall to drain ~2:18 — both ran sequentially-ish.
- **Reproduction:** W2 step 5 — `non_terminal=2 statuses=['running', 'running']` for ~2 minutes.
- **Hypothesis:** The mission-control watcher started with default `--concurrent 2` from the Web `Start watcher` button (which we never specified). For 3 jobs that's: cancel #3, run #1 + #2 concurrently. Behaviour is correct; the surfacing on the UI may be confusing because the user expected sequential drain.
- **Suggested fix:** make the default concurrency value visible in the JobDialog or the start-watcher dropdown so the user knows "Start watcher (concurrent=2)" before clicking.

### W2-IMPORTANT-4: artifact-mine reports session-dir mismatch (W1-IMPORTANT-3 reproduces; queue + cancelled-task surfaces)

- **Severity:** IMPORTANT
- **Symptom:** Same root cause as W1-IMPORTANT-3 — every queue-domain run lives under `<project>/.worktrees/<task>/otto_logs/sessions/<run_id>/` but the harness invariant scan looks under `<project>/otto_logs/sessions/<run_id>/`.
- **Confirmed in:** W2 (2 entries) + W12b (2 entries: queue-run + merge-run) + W13 (1 entry).
- **Suggested fix:** see W1-IMPORTANT-3 — `paths.session_dir(...)` must consider the live record's `cwd` for queue/merge domains.

### W2-NOTE-1: "Otto is committing runtime bookkeeping files…" message fires twice on first build (W1-NOTE-1 reproduces, also from CLI path)

- **Severity:** NOTE
- **Symptom:** `Otto is committing runtime bookkeeping files…` printed twice during W2 step 2 (enqueue). Also surfaces from the CLI in W12b (`cli-queue.log`).
- **Suggested fix:** see W1-NOTE-1 — silent no-op when nothing to commit.

---

## W12a findings

Run id: `2026-04-26-020429-ea7b5e` (`bench-results/web-as-user/2026-04-26-020429-ea7b5e/W12a/`).
Wall time: 18s — fast because we cancelled the CLI build immediately.
Verdict: **PASS** (no soft-assert failures collected).

### Confirmations (no new bugs, but corroborates earlier findings)

- **Atomic-domain cancellation works correctly** — the CLI subprocess died within ~6s of the UI POST cancel (cli exit code=0), and the history row appeared with `terminal_outcome=cancelled`, `status=cancelled`. This is the **counter-example** that proves W2-CRITICAL-1 is queue-domain-specific.
- **W11-IMPORTANT-2 (atomic vs build naming)** confirmed: standalone `otto build` consistently exposes `domain="atomic"` in `/api/state`. Public-API rename or doc still pending. Harness now defensively accepts both `"atomic"` and `"build"` to avoid the W11 trap.
- **No console errors, no network errors, no page errors** during W12a.

### W12a-NOTE-1: Cancel response payload's `severity:"information"` may mislead the user

- **Severity:** NOTE
- **Symptom:** `POST /api/runs/<id>/actions/cancel` returns
  ```
  {"ok":true,"message":null,"severity":"information","modal_title":null,"modal_message":null,"refresh":true,"clear_banner":true}
  ```
  The `severity:"information"` and `message:null` give no positive confirmation that the cancel completed (vs. only "the cancel was *requested*").
- **Reproduction:** W12a step 5.
- **Hypothesis:** The cancel handler returns the same envelope shape regardless of action, with `message=null` for "successful, no banner needed." Result: the SPA shows nothing after a successful cancel — the user clicks Cancel, sees nothing, then realizes the row disappeared.
- **Suggested fix:** include a `message:"Run X cancelled"` for `severity:"information"` so the SPA can flash a toast confirming the action.

---

## W12b findings

Run id: `2026-04-26-020508-3bc491` (`bench-results/web-as-user/2026-04-26-020508-3bc491/W12b/`).
Wall time: 156s (queue-CLI enqueue → web watcher start → build+cert ~2:09 → merge from UI 200 → branch landed).
Verdict: **FAIL** (3 soft-assert failures collected; the merge actually succeeded — see git-log.txt).

### W12b-IMPORTANT-1: `page.goto wait_until="networkidle"` times out (cross-link to W2-IMPORTANT-2)

- **Severity:** IMPORTANT
- **Symptom:** First failure: `shell load failed: Page.goto: Timeout 30000ms exceeded.` Yet downstream steps succeeded — the page DID load, networkidle just never fired. See W2-IMPORTANT-2 for root-cause hypothesis.
- **Reproduction:** W12b step 2. Same backend, same SPA — the symptom is intermittent depending on backend chattiness.

### W12b-IMPORTANT-2: artifact-mine reports session-dir mismatch — including for **merge** runs (extends W1-IMPORTANT-3)

- **Severity:** IMPORTANT
- **Symptom:**
  > `live run 'merge-1777169263-43042-04899947' has no session dir at /var/folders/.../otto-mc-web-rd10h2yl/otto_logs/sessions/merge-1777169263-43042-04899947`
  Merge actions create a synthetic live record (run_id starts with `merge-`) but no real session_dir under `<project>/otto_logs/sessions/`. The W1-IMPORTANT-3 fix needs to handle merge-domain too: either (a) merge runs should not get session-dir invariants applied, or (b) merge runs should write to a sessions dir for completeness (so audits can trace them).
- **Reproduction:** W12b step 6 (merge POST → live record appears).
- **Suggested fix:** in `paths.session_dir_for_run_record(record)`, return None for merge-domain (no session) and have `artifact_mine_pass` skip the assertion when None. Or write a stub `summary.json` in `otto_logs/sessions/merge-*/` for merges.

### W12b confirmations

- **Queue-CLI → web → merge round-trip works end-to-end.** `otto queue build ... --as w12b-task` enqueues; web SPA shows the row immediately; watcher kicks off the build (which committed `c7072c3 feat: add /version endpoint`); merge POST returns 200; merge history row appears within ≤3s; git log shows `Merge branch 'build/w12b-task-2026-04-25'` landed on main.
- **Cost:** ~$0.40 (build $0.07 + certify $0.33 — from `otto pow`-style summary in narrative).
- **No console errors, no page errors, no unexpected 4xx/5xx during W12b.**
- **`otto queue build` returned rc=0 in <1s** — fast and quiet. Worker-not-running banner is correct.

---

## W13 findings

Run id: `2026-04-26-021154-ccc09b` (`bench-results/web-as-user/2026-04-26-021154-ccc09b/W13/`).
Wall time: 165s (started a 4-test TODO build, "killed" backend at +13s, restarted on new port, run survived to terminal=success).
Verdict: **FAIL** (4 soft-assert failures collected).

### W13-INFRA-1: ScenarioContext does not expose the in-process backend handle — true SIGTERM-style outage cannot be issued

- **Severity:** INFRA (harness limitation, not Otto bug)
- **Symptom:** The W13 implementation cannot reach `backend.stop()` because the harness creates the backend in `run_one_scenario` and only forwards `web_url`/`web_port` to the scenario context. Workaround: spin up a SECOND backend (`backend_b`) on a different port against the same `project_dir`, "outage" simulated by switching the browser between them. The build's queue/registry state (in `<project_dir>/otto_logs/`) is shared via filesystem, so survivability is verified — but we don't actually exercise the uvicorn shutdown path.
- **Suggested fix:** add `backend` (or at least `backend.stop`) to `ScenarioContext`. Then add a true sub-test where we call `backend.stop()`, verify uvicorn terminates cleanly + sockets release, then start a fresh `start_backend(...)` against the same project dir.

### W13-CRITICAL-1: Inspector tab buttons (Diff / Proof / Artifacts) are click-blocked — W1-CRITICAL-1 reproduces post-restart, also blocked by `app-shell` overlay

- **Severity:** CRITICAL — same fundamental bug as W1-CRITICAL-1, now reproduced on a *fresh* page-load (post-restart) and with a NEW intercepting element: `<div class="app-shell">…</div> intercepts pointer events`.
- **Symptom:** Walking Logs/Diff/Proof drawers post-restart: Logs click works (screenshot `05-drawer-logs.png` exists), Diff and Proof clicks fail with the same log-pane intercept error from W1-CRITICAL-1, then degrade further to `<div class="app-shell">…</div> intercepts pointer events` (a parent-level overlay also catching clicks). See `/tmp/w13.log` lines 8-26.
- **Reproduction:** W13 step 6 — after `backend_b` restart, click Logs (works) → click Diff (fails on log-pane) → click Proof (fails on log-pane → degrades to app-shell intercept).
- **Hypothesis:** Same as W1-CRITICAL-1 (log-pane stacking). Additional regression: an `.app-shell` overlay (likely the modal-backdrop sibling W11-CRITICAL-2 mentioned) also catches subsequent clicks.
- **Suggested fix:** see W1-CRITICAL-1; additionally inspect `.app-shell` z-index/pointer-events when an inspector dialog is open.

### W13-IMPORTANT-1: Console 404 on `/api/runs/queue-compat:<task>?...` reproduces (cross-link to W2-IMPORTANT-1)

- **Severity:** IMPORTANT
- **Symptom:** Same as W2-IMPORTANT-1 — the SPA fetches `/api/runs/queue-compat:build-a-small-todo-list-app-with-html-bb07f8?type=all&outcome=all&query=&active_only=false&history_page_size=25` and gets 404. Triggered when the user inspects the (long-running) queue-compat row.
- **Reproduction:** W13 step 4–6.

### W13-IMPORTANT-2: Mission Control event stream sparse / does not record per-build progress events

- **Severity:** IMPORTANT
- **Symptom:** After backend restart, `/api/events?limit=200` returned only 2 entries, both `kind: "watcher.started"`. The build that completed during the outage produced no events visible to the post-restart UI. Pre-outage events file: not directly inspected (cleaned up by tempdir). Post-restart `events.jsonl` had 2 items total covering ~130s of activity including a successful build-+-certify run.
- **Reproduction:** W13 step 8 — `(artifact_dir / "post-restart-events.json")` shows `items: [{"kind": "watcher.started", ...}, {"kind": "watcher.started", ...}]`, both at `created_at: "2026-04-26T02:12:03.166362Z"` (before the restart). Nothing for the run that completed.
- **Hypothesis:** Mission Control's event log only records lifecycle events (watcher start/stop, queue add/remove) — not run progress events (build started, certify started, terminal). For an "outage recovery" UX where the user reopens after a crash, that means the timeline is empty post-restart even though work has happened.
- **Suggested fix:** emit per-run lifecycle events (`run.started`, `run.terminal`, `run.merged`, `run.cancelled`) into the same `events.jsonl`. Alternatively, document that the event log is *not* the source of truth for run history, and the UI's Live/History tabs are. Even then, post-outage UX would benefit from at least one event saying "run X completed during outage."

### W13-IMPORTANT-3: artifact-mine session-dir mismatch (W1-IMPORTANT-3 reproduces — for atomic-from-web build, run_id matches a worktree session dir)

- **Severity:** IMPORTANT
- **Symptom:** Same as W1-IMPORTANT-3 — even for builds submitted via the web JobDialog (which become queue-compat:* runs), the session_dir lives in the worktree.

### W13 confirmations

- **Run survived backend restart**: the queue task `2026-04-26-021203-262134` started on backend A (port 54366) at 02:12:03, transitioned to "running" before the simulated outage at 02:12:08, and was visible in `live` on backend B (port 54444) immediately on reopen. Final outcome `success` was recorded in history. The `.worktrees/build-a-small-todo-list-app-with-html-bb07f8/` build artifacts are intact.
- **Cost:** ~$0.40-$0.50 (TODO app build + 1 certify round, all 4 stories PASS).
- **JobDialog submit + watcher start + drawer open all worked** post-restart.

### W13-NOTE-1: Harness bug (NOT Otto): event-stream check used `body["events"]` but `/api/events` returns `body["items"]`

- **Severity:** NOTE (harness fix, not Otto bug)
- **Symptom:** My W13 soft-assert read `events_count = len(ev_body.get("events") or [])` but the API actually returns `{"items": [...], "total_count": N, ...}`. So the "0 events" message is a false negative. Real count was 2.
- **Suggested fix:** in `scripts/web_as_user.py` `_run_w13`, read `len(ev_body.get("items") or [])`.

---

## W3 findings

Two runs collected for W3 (improve loop):

- **First run**: id `2026-04-26-041802-0548fe`, wall time 208s, FAIL with 4 soft-asserts. The harness raced the SPA boot — the `wait_for_function("#root.children.length>0")` returned as soon as the "Loading Mission Control…" skeleton populated the root, *before* the workspace state was ready and the `New job` button was rendered. The whole rest of the run was a no-op because nothing was ever enqueued. Cost: **$0.00** (no LLM calls). The very first thing this exposed (W3-IMPORTANT-1 below) is a real Otto/UI bug worth flagging on its own.

- **Second run**: id `2026-04-26-042240-16857d`, wall time 305s, FAIL with 3 soft-asserts but the build+improve actually completed end-to-end. After hardening the harness wait (now waits for `mission-new-job-button|new-job-button|launcher-subhead` to appear) the scenario reached Step 7. Cost: **$0.83** ($0.36 build + $0.47 improve, both Claude Sonnet via SDK).

Findings below are from the second run unless noted.

### W3-CRITICAL-1: Improve via JobDialog forks from `main`, not from the prior build's branch — collides on the same files

- **Severity:** CRITICAL — silently breaks the documented "iterate on an existing run" promise the JobDialog literally advertises.
- **Symptom:** After Step 4 (enqueue Improve(bugs) referring to a just-completed build that wrote `greet.py` + `test_greet.py` to branch `build/add-a-small-python-module-greet-py-that-49f59d-2026-04-25`), the improve worker:
  1. Created a *new* worktree `.worktrees/web-as-user/` rooted at `main` (HEAD = the bookkeeping commit `b74f01b`, no `greet.py`),
  2. Logged in its own narrative: `"This is a bare project with just a README. I need to create the greet() function and tests."`,
  3. Wrote brand-new `greet.py` + `test_greet.py` from scratch on branch `improve/web-as-user-2026-04-25`, and
  4. Marked itself ready to land *alongside* the original build branch.
  Final-state.json:
  ```json
  "landing": {
    "items": [
      {"branch": "build/add-a-small-python-module-greet-py-that-49f59d-2026-04-25"},
      {"branch": "improve/web-as-user-2026-04-25"}
    ],
    "collisions": [{"left": "add-a-small-python-module-greet-py-that-49f59d",
                    "right": "web-as-user", "files": ["greet.py", "test_greet.py"], "file_count": 2}]
  }
  ```
- **Reproduction:** `OTTO_WEB_SKIP_FRESHNESS=1 OTTO_ALLOW_REAL_COST=1 .venv/bin/python scripts/web_as_user.py --scenario W3 --provider claude`. See `bench-results/web-as-user/2026-04-26-042240-16857d/W3/final-state.json` and screenshot `07-improve-terminal.png` (READY 2 with both branches).
- **Hypothesis:** The web JobDialog enqueues `improve bugs <focus>` exactly the same way it would from CLI, with no concept of a "prior run" reference. `MissionControlService.execute(... command="improve" ...)` (`otto/mission_control/service.py:814–836`) takes only `subcommand` and `focus`/`goal`, then calls `enqueue_task(...)` which spawns a fresh worktree from `main`. There is no plumbing to attach `--reference-run <id>` or to check out the prior build branch as the base. The CLI's standalone `otto improve` works because it operates on the project's *current* working copy (which the user manually leaves on the build branch) — but the queue path always rebases to a fresh worktree, breaking the model.
- **Suggested fix:** Either:
  - **Server-side**: the JobDialog should accept a `prior_run_id` (or auto-pick the latest successful build on `main`), have the queue create the improve worktree from `<prior_branch>` instead of `main`, and name the branch `improve/<prior_branch_basename>-<date>` so it logically extends.
  - **UI**: surface a select like "Refine which run?" populated from history (last N successful builds + improves on this project). Hide it for greenfield projects.
  - **Stopgap (Mission Control)**: detect that an improve job's "after" landing collides with the still-pending base branch, and surface a banner: "This improve and its base build both touch greet.py. Land base first, then re-queue improve." Or auto-set `after: <prior_task_id>` so the watcher serializes them.

### W3-CRITICAL-2: Loading skeleton races SPA boot — first interaction can fire against a non-actionable shell

- **Severity:** CRITICAL — first run of W3 was a $0 no-op because of this.
- **Symptom:** The MC SPA renders a `<div>Loading Mission Control…</div>` card into `#root` *before* it reads project state and renders the actionable shell (sidebar, task board, "New job" button). The whole render fits in `#root`, so the standard hydration probe `document.querySelector('#root')?.children.length > 0` returns true after ~50–200 ms while the workspace is still booting (~5–8 s on a fresh tempdir). Any external automation that uses that probe (Playwright, MCP tooling, third-party scripts, smoke tests) will then fire `getByTestId("mission-new-job-button")` and miss because the button doesn't exist yet. Screenshot: `bench-results/web-as-user/2026-04-26-041802-0548fe/W3/01-shell.png` (only "Loading Mission Control…" visible).
- **Reproduction:** Open `http://127.0.0.1:<port>` in a brand-new browser context against a brand-new project, race a `getByTestId("mission-new-job-button")` against the `#root` populate. The harness fix (now landed in this branch) was to also wait for `[data-testid="mission-new-job-button"], [data-testid="new-job-button"], [data-testid="launcher-subhead"]` — but the *underlying SPA bug* is that the loading card and the actionable shell share `#root` with no marker to differentiate them.
- **Hypothesis:** `otto/web/client/src/App.tsx` renders the LoadingMissionControl component into the same root before the initial `/api/state` fetch resolves. There is no `data-testid="mission-loading"` or `data-state="loading|ready"` attribute on the shell, and no separate `data-testid="mission-shell-ready"` once it's interactive.
- **Suggested fix:** Add `data-state="loading|ready"` (or a specific `data-testid="mission-loading"` / `data-testid="mission-shell"`) to the top-level `<div id="root">` child or its immediate body, so external automation has a single deterministic probe. Document the recommended probe in `docs/mc-audit/server-endpoints.md` ("wait for `[data-testid=mission-shell]` before any interaction").

### W3-IMPORTANT-1: Improve task id and resolved_intent default to project name, ignoring user focus

- **Severity:** IMPORTANT — confuses operators reviewing the task board and pollutes the agent's context.
- **Symptom:** After enqueueing improve via JobDialog with focus `Make greet() handle empty/None name…`, the queue manifest in `.otto-queue.yml` shows:
  ```
  - id: web-as-user            # ← project tempdir name, not focus
    command_argv: [improve, bugs, "Make greet() handle empty/None name..."]
    resolved_intent: '# web-as-user'   # ← Markdown heading from project name
    focus: Make greet() handle empty/None name by returning 'Hello, world!'...
    branch: improve/web-as-user-2026-04-25
  ```
  The Task Board displays the card title as **`web-as-user`** (see screenshot `06-watcher-improve.png`); the Recent Activity feed says `web-as-user: improve bugs started`; and the agent's `intent.txt` reads `# web-as-user\n\n## Improvement Focus\nMake greet()...` — a noisy heading prepended to the actual focus.
- **Reproduction:** Enqueue any improve job from the JobDialog on a project without an `intent.md`. See `.otto-queue.yml`, `06-watcher-improve.png`, and `intent.txt` in the session dir.
- **Hypothesis:** `enqueue_task(...)` derives the task id by slugifying `intent` when provided, falling back to project basename. For improve, `intent` is empty (only `focus` is set) so it falls back to project name. `resolve_intent_for_enqueue(...)` reads `intent.md` if present and otherwise constructs `# <project_name>` as the snapshot intent.
- **Suggested fix:** For improve tasks, slugify the `focus` (`make-greet-handle-empty-or-none-name-...`) for the task id and set `intent` to the focus directly (no `# project_name` heading). Or skip the snapshot-intent prepending entirely when `focus` is set — just hand the agent the focus.

### W3-IMPORTANT-2: improve loop never writes `build-journal.md` when fix-loop is single-round

- **Severity:** IMPORTANT — breaks the documented improve UX ("round-by-round index in build-journal.md") and removes the only handoff artifact for partial-progress debugging.
- **Symptom:** After improve completed (PASS, 1 round), no `build-journal.md` exists anywhere under the session's `improve/` dir. `find <session_dir> -name build-journal.md` returns nothing. The harness's `(artifact_dir / "build-journal-versions.txt")` reads `(none)`. Yet `improvement-report.md` *was* written (so the improve session ran end-to-end), and the project CLAUDE.md documents `improve/build-journal.md` as the round-by-round index.
- **Reproduction:** Submit an improve job whose first fix-loop pass already passes the certifier (`PASS (3/5)` in this run). Inspect `<session>/improve/`.
- **Hypothesis:** `pipeline.py` calls `append_journal(...)` only inside the certify→fix loop body (lines 1897, 1930, 2104, 2194). When the loop bails early on first-round PASS, the journal init line (`certify round 1`) IS appended but only on round transition. Looking at the narrative, the agent did `Now dispatching the certifier.` and got `CERTIFY ROUND 1 → PASS (3/5)` — but no journal write fired. Possibly the `append_journal` for the first round is inside the *fix* branch only, not the certify branch.
- **Suggested fix:** Always seed `build-journal.md` at the start of `improve` (even before round 1), with a header line `## improve bugs — <focus>`. Append a row for round 1 regardless of outcome. This way the journal exists for every improve, even the trivial-pass case, and matches the docs.

### W3-IMPORTANT-3: improvement-report.md misrepresents WARN observations as PASS checkmarks

- **Severity:** IMPORTANT — operator reviewing the report sees five green ✓ rows, but three of them are WARN-level observations the certifier explicitly flagged as out-of-scope edge cases.
- **Symptom:** `improvement-report.md` (artifact copy in `bench-results/.../W3/improvement-report.md`):
  ```
  ## Results
  - ✓ Empty string, None, and missing argument all correctly return the safe default
  - ✓ All 4 tests pass including the required empty-string test
  - ✓ Whitespace-only strings are not stripped or treated as empty, producing ugly output
  - ✓ Using `if not name` catches all falsy values (0, False, []) not just None/empty-string
  - ✓ No type validation; arbitrary types accepted via f-string coercion
  ```
  The last three rows are clearly *negative* findings (whitespace produces ugly output, falsy-catch is overly broad, no type validation) but rendered with a green check. Compare to the certifier's own narrative diagnosis:
  ```
  Three WARN-level observations exist around whitespace handling and overly broad falsy-value matching, but these are edge cases beyond the stated improvement scope.
  ```
- **Reproduction:** Run any improve where the certifier returns PASS with WARN-level observations.
- **Hypothesis:** The report renderer iterates over story results and prints `✓` for everything in the PASS bucket regardless of severity tag. WARN observations should render as `⚠`/`!` and be in their own subsection.
- **Suggested fix:** Group results by `severity` or `result_type` (PASS / WARN / FAIL) before rendering. Use distinct glyphs. Or split into "Met" / "Observations" sections.

### W3-IMPORTANT-4: improvement-report.md `Stories: 5/5` contradicts certifier narrative `(3/5)`

- **Severity:** IMPORTANT — count is wrong or the categorization is wrong; either way the report doesn't match the narrative log.
- **Symptom:** `improvement-report.md` says `Stories: 5/5`. The certifier's narrative says `CERTIFY ROUND 1 → PASS (3/5)` — i.e. 3 of 5 stories actually passed; the other 2 were WARN observations. Either the report should say `3/5` or the WARNs shouldn't count toward the denominator.
- **Reproduction:** Same as W3-IMPORTANT-3.
- **Suggested fix:** Pick one definition of "passed" and stick with it. If WARN counts as pass, the narrative should also say `5/5`. If WARN does not, the report should say `3/5` and surface WARN count separately.

### W3-IMPORTANT-5: improve mode shows phase=`build` and writes to `build/narrative.log` instead of `improve/`

- **Severity:** IMPORTANT — disagrees with the documented per-session layout (CLAUDE.md says `improve/build-journal.md`, etc.) and breaks downstream tools that filter by `phase`.
- **Symptom:** The improve session's `checkpoint.json` shows `"phase": "build"` and the agent stream is at `<session>/build/narrative.log`, not `<session>/improve/narrative.log`. Yet the documented per-session layout (CLAUDE.md) lists `improve/improvement-report.md`, `improve/session-report.md`, `improve/build-journal.md`, `improve/current-state.md`, `improve/rounds/<round-id>/` as the canonical improve subdir. So the agent stream lives under `build/` while the *outputs* live under `improve/` — split-brained.
- **Reproduction:** Enqueue any improve job. `cat <session>/checkpoint.json | grep phase` → `"build"`. `ls <session>/build/` shows the live narrative.
- **Hypothesis:** `pipeline.py` reuses the build-phase plumbing (single `build/` log dir) for all command modes, with `phase` derived from the inner agent's runtime config. There's no separate `phase: improve` enum.
- **Suggested fix:** Either (a) rename the agent stream dir to `agent/` (command-agnostic) so `build/` and `improve/` only contain phase-specific outputs, or (b) duplicate-write the agent stream into `improve/agent.log` for improve runs so the whole session is contained in `improve/`.

### W3-IMPORTANT-6: Watcher button stays disabled and labeled "Start watcher" while title says "Stop watcher to pause queue dispatch"

- **Severity:** IMPORTANT — this killed Step 6 of the harness (`start watcher click failed (W3-improve)` was the headline failure) but it's also a real UX bug.
- **Symptom:** When the watcher is already running, the JobDialog page renders TWO buttons in the sidebar: a disabled `<button data-testid="start-watcher-button">Start watcher</button>` with `title="Stop watcher to pause queue dispatch."` *and* an enabled `<button>Stop watcher</button>` next to it. The disabled "Start watcher" button is contradictory: its label says start, its title says stop. External automation (and humans) reasonably interpret "Start watcher" as the button to click to start work — but it's disabled because the watcher *is* already started, with a tooltip that describes the action of the *other* button. See screenshots `06-watcher-improve.png` and `07-improve-terminal.png`.
- **Reproduction:** Start the watcher (click "Start watcher" once). Then look at the sidebar.
- **Hypothesis:** `App.tsx` renders both buttons unconditionally and disables `start-watcher-button` based on `watcher.alive`, but reuses the *Stop* tooltip when disabled — instead of either hiding the button or updating the tooltip to say "Watcher is running" / "Already running".
- **Suggested fix:** When `watcher.alive`, hide the "Start watcher" button (or render it visually muted with title "Watcher is running"). Don't keep stale tooltip text from the opposite-state action. Only one of `start`/`stop` should be visible at a time.

### W3-IMPORTANT-7: Recent Activity logs `web-as-user: legacy queue mode started` — what is "legacy queue mode"?

- **Severity:** IMPORTANT (or NOTE if intended)
- **Symptom:** `06-watcher-improve.png` Recent Activity feed shows the line `info  web-as-user: legacy queue mode started  09:24:00 PM`. There is no UI affordance for picking between modes; "legacy" suggests something deprecated leaking into the user-facing event stream.
- **Reproduction:** Enqueue any queue task; observe Recent Activity.
- **Hypothesis:** Internal mode flag (`legacy` vs new modular dispatcher?) being event-logged by name. Should either be removed from user-facing feed or renamed if it's meaningful UX.
- **Suggested fix:** rename to a descriptive label (`queue mode: serial`) or filter out internal-only events from Recent Activity.

### W3-NOTE-1: Narrative log spinner shows stale tool name long after agent has moved on

- **Severity:** NOTE
- **Symptom:** During the improve phase the narrative log lines for ~2 minutes were:
  ```
  [+0:31] • committed to improve/web-as-user-2026-04-25: 823ab65 ...
  [+0:33] ▸ Now dispatching the certifier.
  [+0:52] ⋯ building… (52s) · running git add greet.py test_greet.py · 8 tool calls
  [+1:12] ⋯ building… (1m 12s) · running git add greet.py test_greet.py · 8 tool calls
  [+1:32] ⋯ building… (1m 32s) · running git add greet.py test_greet.py · 8 tool calls
  ```
  The "running git add greet.py test_greet.py" status was the LAST tool call BEFORE the commit; the agent dispatched the certifier 19 s before the spinner started repeating. The spinner is frozen on a stale tool snapshot rather than reflecting the in-flight subagent (certifier) doing real work.
- **Reproduction:** Tail any otto build/improve narrative log during a long subagent dispatch.
- **Hypothesis:** The narrative formatter samples `last_tool_name` from the parent agent's checkpoint and keeps printing it during sub-agent windows where no new top-level tool calls happen. There's no plumbing for "subagent active: certifier" status messages.
- **Suggested fix:** When a subagent is dispatched, emit a `subagent: certifier — running` line and either (a) hide the parent spinner during that window, or (b) say "subagent in progress · n s elapsed" instead of repeating the last unrelated tool name.

### W3-NOTE-2: Console 404 on `/api/runs/queue-compat:<task-id>?...` reproduces (cross-link to W2-IMPORTANT-1, W13-IMPORTANT-1)

- **Severity:** NOTE (already filed)
- **Symptom:** `network-errors.json` records one `404 http://127.0.0.1:49523/api/runs/queue-compat%3Aadd-a-small-python-module-greet-py-that-49f59d?history_page_size=25`. Same root cause as W2-IMPORTANT-1.

### W3-INFRA-1: pytest result for product verification not captured in artifacts

- **Severity:** INFRA (harness hole, not Otto bug)
- **Symptom:** Step 7 logs `running pytest in /var/folders/.../web-as-user/` and never writes `pytest.log` to the artifact dir nor logs the rc. The script then jumps straight to the FAIL summary. We don't know if the post-improve `test_greet.py` actually passed because the harness never captured the result.
- **Reproduction:** Re-run W3.
- **Hypothesis:** The pytest `subprocess.run(...)` may import-fail or hang silently before the `(artifact_dir / "pytest.log").write_text(...)` line. Could also be that the worktree was concurrently being torn down.
- **Suggested fix:** Wrap the entire Step 7 try/except so a stale write *always* happens (write `rc=?` even on exception). Pre-write `pytest.log` with `"running..."` immediately so we at least see the harness reached the call.

### W3 confirmations (no new bugs)

- **W1-NOTE-1** (`Otto is committing runtime bookkeeping files…` printed twice on first build) reproduces in W3 — see lines `[04:22:42]` in debug.log.
- **W2-IMPORTANT-1** (`/api/runs/queue-compat:...` 404) reproduces in W3 — see W3-NOTE-2.
- **build → certify → success → ready-to-land** path works end-to-end for both queue jobs in this scenario; the issue is purely on the *improve baseline* (W3-CRITICAL-1) and the *artifact/UX* layer.
- **JobDialog improve mode IS reachable** — testid `job-improve-mode-select` works, command select works, focus textarea works, submit works. So the *form* is fine; the problem is what happens after submit (CRITICAL-1).

---

## W4 findings

W4 = Merge happy path: enqueue tiny `hello.py` build via UI → wait
success → click Merge action → confirm branch landed in `main`.

Run id: `2026-04-26-053237-7087ed/W4`. Verdict: **FAIL** in 736s. Cost
spent on real LLM: **$0.00** (the build never reached the queue, so the
agent never ran). Of the 7 soft-asserts that fired, all 7 cascade from a
single root cause.

### W4-CRITICAL-1: Step 1 race — `wait_for_function('#root.children > 0')` returns against the loading skeleton, then every subsequent interaction misses

- **Severity:** CRITICAL — full reproduction of W3-CRITICAL-2 in W4 (and W5). $0 no-op runs are a tax on every CI cycle that uses this harness.
- **Symptom:** After `page.goto(...)` the harness's only readiness probe is `document.querySelector('#root')?.children.length > 0`. The MC SPA renders a `<div>Loading Mission Control…</div>` card into `#root` long before the actionable shell mounts, so the probe returns true within ~50ms. `_enqueue_via_dialog_full` then immediately calls `page.locator('[data-testid="mission-new-job-button"]').first` — the button doesn't exist yet, `failures.fail("new-job button missing (enqueue w4-build)")` fires, and the rest of the scenario cascades:
  - `could not enqueue W4 build` (consequence)
  - `start-watcher click failed (W4): element is not enabled` (Start watcher is correctly disabled while the queue is empty)
  - `build outcome=None (need success to merge)` (no build was ever queued)
  - `no merge history row appeared in 90s` (consequence)
  - `main commit count did not grow: pre=1 post=1` (consequence)
  - `hello.py not present on main after merge (rc=128)` (consequence)
  - Screenshot evidence: `bench-results/web-as-user/2026-04-26-053237-7087ed/W4/01-shell.png` shows ONLY the "Loading Mission Control…" card; the very next screenshot, `03-watcher.png`, taken ~95s later, shows the fully-rendered shell with the `New job` button.
  - `final-state.json` confirms no work ever reached the backend: `runtime.files.queue.exists=False`, `history.items=[]`, `live.items=[]`, `watcher.health.state="stopped"`.
- **Reproduction:** `OTTO_ALLOW_REAL_COST=1 OTTO_WEB_SKIP_FRESHNESS=1 .venv/bin/python scripts/web_as_user.py --scenario W4 --provider claude` against any clean tempdir.
- **Hypothesis:** `scripts/web_as_user.py` lines 2772-2776 (W4) and 2992-2996 (W5) use only the bare `#root` probe — they never landed the workaround that already exists in W3 (lines 2459-2474), W1 (line 503), W11 (line 810), W2 (line 1320), W12a/b (lines 1602/1826), W13 (line 2058), W6 (line 3217), W7 (line 3466), W8 (line 3899): a follow-up `wait_for_selector('[data-testid="mission-new-job-button"], [data-testid="new-job-button"], [data-testid="launcher-subhead"]', timeout=20_000)`. W4/W5 are the only two scenarios that omit it.
- **Suggested fix (harness, trivial):** Add the same 8-line follow-up wait after `wait_for_function` in `_run_w4` and `_run_w5`. Example for W4:
  ```python
  page.wait_for_function(
      "document.querySelector('#root')?.children.length > 0",
      timeout=15_000,
  )
  page.wait_for_selector(
      '[data-testid="mission-new-job-button"], '
      '[data-testid="new-job-button"], '
      '[data-testid="launcher-subhead"]',
      timeout=20_000,
  )
  ```
- **Suggested fix (SPA, durable):** As recommended in W3-CRITICAL-2, add `data-state="loading|ready"` (or a distinct `data-testid="mission-shell"`) to the rendered shell so any external automation has one deterministic probe and doesn't need scenario-by-scenario fixes. `otto/web/client/src/App.tsx`.
- **Why CRITICAL even though it's a harness regression:** The same pattern keeps re-occurring across scenarios because the SPA exposes no first-class readiness signal. Every new automation (browser tests, MCP tooling, smoke tests, Phase 5 scenarios) pays the same tax. This is the second re-occurrence after W3-CRITICAL-2; harness-level fixes won't stop the third.

### W4-IMPORTANT-1: `_enqueue_via_dialog_full` swallows the failure — caller adds a duplicate `soft_assert` that will never fire correctly

- **Severity:** IMPORTANT — the noise makes the failure harder to triage.
- **Symptom:** `_enqueue_via_dialog_full` calls `failures.fail("new-job button missing (enqueue w4-build)")` *internally* and returns `False`. The caller then calls `failures.soft_assert(ok, "could not enqueue W4 build")` (line 2793), which adds a *second* failure with a different message describing the same root cause. The summary now lists two failures for one bug, plus a third (`start watcher click failed`) that's also a downstream effect — total of 7 soft-asserts for what's really 1 bug. Compare W4 step-by-step: 7 listed failures, 1 actual bug.
- **Reproduction:** Any failed enqueue in W4/W5/W6/W7/W8.
- **Hypothesis:** `_enqueue_via_dialog_full` was refactored to record its own failure inside the helper, but the callsites still wrap it in `soft_assert(ok, ...)` from before the refactor.
- **Suggested fix:** Pick one. Either remove the `failures.fail(...)` from inside the helper (let the caller decide), OR remove the `soft_assert(ok, ...)` at every callsite. The callers I see (W4 line 2793, W5 line 3011, W6, W7, W8) all duplicate the message.

---

## W5 findings

W5 = Merge blocked: enqueue tiny `ping.py` build → success → seed
`DIRTY_FILE.txt` in target → click Merge → expect 409 with reason → no
merge happens.

Run id: `2026-04-26-054529-06158f/W5`. Verdict: **FAIL** in 653s. Cost
spent on real LLM: **$0.00** (same root cause as W4 — never enqueued).

### W5-CRITICAL-1: Same Step-1 race as W4 (and W3) — never reached the merge-blocking logic

- **Severity:** CRITICAL — same root cause, same cascade. Counted once, but worth noting that the *interesting* scenario (does the backend block merge with a sensible reason?) is never even exercised.
- **Symptom:** Identical to W4-CRITICAL-1. `01-shell.png` shows the loading skeleton only. The 6 cascading soft-asserts:
  - `new-job button missing (enqueue w5-build)`
  - `could not enqueue W5 build`
  - `start watcher click failed (W5): element is not enabled`
  - `W5 build outcome=None (need success to attempt merge)`
  - `expected 409/400 (merge blocked); got 0`  ← because `build_run_id` was `None`, the merge POST was *skipped* (`if build_run_id:` guard at line 3057). `merge_status` defaulted to `0` and the assertion fired against the placeholder.
  - `merge-blocked reason did not mention dirty/blocked/merge/repo: ` ← consequence of the same skipped POST.
- **Reproduction:** `OTTO_ALLOW_REAL_COST=1 OTTO_WEB_SKIP_FRESHNESS=1 .venv/bin/python scripts/web_as_user.py --scenario W5 --provider claude`.
- **Suggested fix:** Same as W4-CRITICAL-1. After the harness fix, this scenario will actually exercise the merge-block path; the underlying merge/dirt-detection logic in the backend was *not* tested in this run.

### W5-IMPORTANT-1: When `build_run_id` is `None`, the harness silently skips the merge POST and reports a misleading `expected 409/400; got 0`

- **Severity:** IMPORTANT — masks "we never asked the server" as "the server returned 0", which sends triage in the wrong direction.
- **Symptom:** Lines 3055-3069: `merge_status = 0`, `if build_run_id: merge_status, merge_body = _post_action(...)`, then unconditionally `failures.soft_assert(merge_status in (409, 400), f"expected 409/400; got {merge_status}")`. When `build_run_id` is `None` (e.g. because the build never ran), the assertion fires with `got 0` even though no HTTP call was made. Compare W4 (line 2826-2845): `if build_run_id:` guards the POST AND the legal-actions check, but the failure messages still imply the merge was attempted.
- **Reproduction:** Any W5 run where the build doesn't reach a terminal outcome.
- **Suggested fix:** Add an explicit early-out / different failure message when `build_run_id is None`: `failures.fail("could not attempt merge — build never produced a run_id")`. Don't conflate "no attempt" with "attempt returned 0".

### W5-NOTE-1: Recovering from a $0 cascade requires reading the harness source

- **Severity:** NOTE.
- **Symptom:** When the cascade fires, the human reading the FAIL summary sees 6 soft-asserts but no clear "this is the same Step-1 race you saw last time". The fix is in scripts/web_as_user.py at a specific line; without that knowledge, every scenario added looks like a new bug.
- **Suggested fix:** Add a top-of-scenario readiness helper `_wait_for_mc_shell_ready(page, failures)` that all scenarios call. One implementation, one bug fix when the SPA gains a real readiness marker.

---

## Harness migration (post-audit)

The W3/W4/W5-CRITICAL-1 race against the loading skeleton was eliminated
at the harness layer by switching every `wait_for_function('#root.children > 0')`
probe to a single shared helper:

```python
def _wait_for_mc_ready(page: Any, *, timeout_ms: int = 20_000) -> None:
    page.wait_for_selector(
        '[data-mc-shell="ready"], [data-testid="launcher-subhead"]',
        timeout=timeout_ms,
    )
```

The `data-mc-shell="ready"` attribute was already added to the SPA as the
W3-CRITICAL-2 fix (cluster G). All 13 readiness sites in
`scripts/web_as_user.py` now use the helper (W1, W11, W2, W12a, W12b,
W13×2, W3, W4, W5, W6, W7, W8). Bare `#root.children > 0` no longer
appears in the harness.

Also: `_enqueue_via_dialog_full` was changed to record diagnostic
`failures.note(...)` instead of `failures.fail(...)` on internal
failures, so the caller's single `failures.soft_assert(ok, "could not
enqueue …")` is the canonical failure entry — fixes W4-IMPORTANT-1
double-soft-assert.

## W4 findings (re-run)

Re-ran with the harness migration in place.

Run id: `2026-04-26-060444-2326ae/W4`. Verdict: **PASS** in 78s. Cost
spent on real LLM: **~$0.23** (one tiny `hello.py` build, ~54s, $0.23
per `final-state.json` `cost_usd`).

The Step-1 race is gone. The merge happy path now actually exercises
the backend:

- `pre-merge-git-log.txt` → 2 commits on `main`
- `post-merge-git-log.txt` → 4 commits on `main`, with
  `Merge branch 'build/add-a-tiny-python-module-hello-py-…'` and
  `Add hello.py module exporting hello() returning 'world'`
- `merge-response.json` → `{"ok":true,"message":"merge … finished",…}`
- `hello-py-in-main.txt` → file present
- No console errors, no page errors, no network 4xx.

No new bugs found in W4 — the merge-happy path is correct.

## W5 findings (re-run)

Re-ran with the harness migration in place. Now exercises the actual
merge-block path (the original $0 race never reached this logic).

Run id: `2026-04-26-060606-9cd966/W5`. Verdict: **FAIL** in 72s. Cost
spent on real LLM: **~$0.23** (one tiny `ping.py` build, ~54s).

### W5-CRITICAL-1 (post-rerun, replaces the harness-only finding): Merge of a successful build branch ignores untracked files in the project root and lands a "blocked" merge as success

- **Severity:** CRITICAL — silent merge of work that the operator
  intentionally guarded against. Untracked files in the working tree
  (a real stop-the-world signal in any merge UX) are completely ignored
  by Otto's merge preflight.
- **Symptom:**
  1. The harness builds `ping.py` to a worktree (success, run_id
     `2026-04-26-060618-469e1f`).
  2. Writes `DIRTY_FILE.txt` (untracked) to the project root before
     attempting the merge.
  3. Captures `pre-merge-git-status.txt` confirming the dirt:
     ```
     ?? DIRTY_FILE.txt
     ```
  4. POSTs `/api/runs/<id>/actions/merge`. Server returns **HTTP 200**:
     ```json
     {"ok":true,"message":"merge add-a-tiny-python-module-ping-py-e93a94 finished",
      "severity":"information","modal_title":null,"modal_message":null,
      "refresh":true,"clear_banner":false}
     ```
  5. `final-state.json` confirms a `merge` live row with
     `terminal_outcome: "success"` and `last_event: "all clean merges,
     cert skipped per --fast"`.
  6. `git show main:ping.py` succeeds (rc=0) — `ping.py` is now on
     `main` despite the dirty tree.
- **Reproduction:** `OTTO_ALLOW_REAL_COST=1 OTTO_WEB_SKIP_FRESHNESS=1
  .venv/bin/python scripts/web_as_user.py --scenario W5 --provider claude`.
- **Hypothesis:** Otto's merge preflight (the `all clean merges` path)
  considers a project root "clean" when there are no *modified* tracked
  files, but does not detect *untracked* files in the project root or
  the worktree. Either the preflight invokes `git diff --quiet` (which
  ignores untracked) instead of `git status --porcelain` (which would
  list `??`), or the preflight only looks at the inside of the build's
  `.worktrees/<task>/` and never inspects the project root.
- **Suggested fix:** Add `git status --porcelain` (or `--ignored=no
  --untracked-files=normal`) to the merge preflight. If any untracked
  files exist in the project root that are not in `.gitignore`, return
  HTTP 409 with body `{"reason": "project root has untracked files",
  "files": [...]}` and surface as a banner in MC. Same check should run
  for modified-but-unstaged tracked files. If untracked-in-root is
  *intentionally* ignored, document why (and fix W5 to test a
  modified-tracked file instead).
- **Why this is the real bug, not the harness:** With the readiness
  probe fixed, the harness now reaches the actual merge logic and
  produces a clean 4-step trace: build → seed dirt → POST merge →
  merge succeeds anyway. This is the merge-block invariant the
  scenario was designed to test, and Otto fails it.

### W5-IMPORTANT-1 (post-rerun): merge response shape doesn't expose blocked-merge reason

- **Severity:** IMPORTANT — even if the preflight were fixed, the
  current response envelope (`{ok, message, severity, modal_title,
  modal_message, refresh, clear_banner}`) doesn't have a structured
  field for "merge was blocked because X". The harness has to scrape
  `message`/`modal_message` text for substrings like `"dirty"`,
  `"blocked"`, `"merge"`, `"repo"` to know what happened. Suggest
  adding `blocked: bool` and `block_reason: str | null` to the
  response so MC and external automation can render the banner from
  structured data.
- **Reproduction:** Inspect any `/api/runs/<id>/actions/merge` response.

### W5-NOTE-1 (post-rerun): repeated 404 on `/api/runs/queue-compat:<task-id>?...`

- **Severity:** NOTE — already filed as **W2-IMPORTANT-1**. Reproduces
  here. Console error in `console.json`. Cross-link only; not a new
  finding.

---

## Summary

| Run | Verdict | Wall time | Bugs (C/I/N) | Cost (live) |
|-----|---------|-----------|---------------|-------------|
| W1   | FAIL    | 222s      | 1 / 4 / 3     | LLM: 1 kanban build (Sonnet, ~$0.5–1.5 est.) |
| W11  | FAIL    | ~180s     | 2 / 4 / 1     | LLM: 1 GET /tasks endpoint (Sonnet, ~$0.3–1 est.) |
| W2   | FAIL    | 165s      | 1 / 4 / 1     | LLM: 2 of 3 builds completed (~$0.40 each) + 1 cancelled |
| W12a | PASS    | 18s       | 0 / 0 / 1     | LLM: 1 build cancelled within 11s of start (~$0.05) |
| W12b | FAIL    | 156s      | 0 / 2 / 0     | LLM: 1 build + cert + merge (~$0.40) |
| W13  | FAIL    | 165s      | 1 / 3 / 1     | LLM: 1 TODO build + cert (~$0.40-$0.50) |
| W3   | FAIL    | 305s (run 2; run 1 = 208s no-op)    | 2 / 7 / 2     | LLM: 1 greet build + 1 improve (~$0.83 = $0.36 build + $0.47 improve) |
| W4   | PASS (rerun; orig FAIL/736s harness-race) | 78s | 0 / 0 / 0 | LLM: 1 hello build ($0.23) |
| W5   | FAIL (rerun; orig FAIL/653s harness-race) | 72s | 1 / 1 / 1 | LLM: 1 ping build ($0.23) — merge-block invariant violated |

Total findings (across all 9 runs): **9 CRITICAL, 26 IMPORTANT, 9 NOTE**
(unchanged in count — the W4/W5 reruns *replace* the prior harness-only
findings; the W4 race is now resolved at source, the W5 race resolved
revealed one new merge-preflight CRITICAL + one IMPORTANT, see
"W5 findings (re-run)" above).

(Some findings reproduce across scenarios — e.g. W1-CRITICAL-1 also surfaces in W13;
W1-IMPORTANT-3 surfaces in W2/W12b/W13. The reproduction breadth is itself a
data point: regressions like the log-pane stacking issue affect every flow that
opens an inspector. Counted once each at the **first** observation; reproductions
flagged inline.)

Cost actually spent: **~$3.39** total
(W2 ≈ $0.85 for 2 builds; W12a ≈ $0.05 quick cancel; W12b ≈ $0.40 build+merge;
W13 ≈ $0.50 TODO build + cert + outage; W3 ≈ $0.83 for 1 greet build + 1 improve;
W4 rerun ≈ $0.23 for hello build; W5 rerun ≈ $0.23 for ping build.
W3 first attempt was $0.00 — never enqueued anything, see W3-CRITICAL-2.
W4/W5 first attempts were also $0.00 — harness Step-1 race; see harness-migration section.).

### INFRA-class issues observed

- **Bundle freshness check trips on uncommitted client edits** (pre-existing
  on this branch — not a new bug, but blocks any harness that doesn't set
  `OTTO_WEB_SKIP_FRESHNESS=1`). The harness defaults are correct here.
- **W13-INFRA-1**: ScenarioContext does not expose the backend handle; W13
  cannot test true uvicorn shutdown. Workaround: parallel backend on a new
  port. Plumbing fix needed.
- **`page.goto wait_until="networkidle"` 30s timeout** — affects external
  automation; reclassified as W2-IMPORTANT-2.
- No rate-limit or 429 observed across any of the four runs.
- 4xx network errors observed: 1 in W2 (`/api/runs/queue-compat:...?...` 404
  — see W2-IMPORTANT-1). Same in W13.
- Console errors observed: same 404s as above (only on W2 + W13).
- No page errors anywhere.
- **Orphan `otto queue run --no-dashboard --concurrent N` watchers persist across temp-dir cleanup** (W11-IMPORTANT-4 reproduces in W2). After W2 finished, `ps aux | grep "otto queue run"` showed two new orphans pointing at the now-deleted W2 tempdir. This is the same backend.stop() doesn't-signal-watcher bug.
- **W3-INFRA-1**: pytest result for product verification not captured in artifacts (Step 7 logs the call but never writes pytest.log).
- **Loading-Mission-Control race (W3-CRITICAL-2)** also affects external automation generally — the SPA needs a `data-state="ready"` marker on the shell so any tool can probe deterministically. The harness now waits for `mission-new-job-button|new-job-button|launcher-subhead` as a workaround; this should be folded into the recommended probe.

### Reproduction one-liner (now covers W1+W11+W2+W3+W12a+W12b+W13)

```
OTTO_WEB_SKIP_FRESHNESS=1 OTTO_ALLOW_REAL_COST=1 OTTO_BROWSER_SKIP_BUILD=1 \
  .venv/bin/python scripts/web_as_user.py --scenario W1,W2,W3,W11,W12a,W12b,W13 --provider claude
```

Or one at a time (faster to triage failures):

```
OTTO_WEB_SKIP_FRESHNESS=1 OTTO_ALLOW_REAL_COST=1 OTTO_BROWSER_SKIP_BUILD=1 \
  .venv/bin/python scripts/web_as_user.py --scenario W2 --provider claude
```

Artifacts under `bench-results/web-as-user/<utc-id>/<scenario>/`.
