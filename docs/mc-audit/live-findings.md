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

## Summary

| Run | Verdict | Wall time | Bugs (C/I/N) | Cost (live) |
|-----|---------|-----------|---------------|-------------|
| W1  | FAIL    | 222s      | 1 / 4 / 3     | LLM: 1 kanban build (Sonnet, ~$0.5–1.5 est.) |
| W11 | FAIL    | ~180s     | 2 / 4 / 1     | LLM: 1 GET /tasks endpoint (Sonnet, ~$0.3–1 est.) |

Total findings: **3 CRITICAL, 8 IMPORTANT, 4 NOTE** — see sections above.

Cost actually spent: not directly captured (no /api/state cost streaming
in either run — see W1-NOTE-3). Estimate ≤ $3 across both runs based on
how short both LLM builds were (W1 build+verify in 3:11, W11 build in
~2:00 wall time).

### INFRA-class issues observed

- **Bundle freshness check trips on uncommitted client edits** (pre-existing
  on this branch — not a new bug, but blocks any harness that doesn't set
  `OTTO_WEB_SKIP_FRESHNESS=1`). The harness defaults are correct here.
- No rate-limit or 429 observed in this smoke run.
- No 4xx/5xx in either run's network-errors.json.
- Console log clean in both (console.json empty).
- No page errors.

### Reproduction one-liner

```
OTTO_WEB_SKIP_FRESHNESS=1 OTTO_ALLOW_REAL_COST=1 OTTO_BROWSER_SKIP_BUILD=1 \
  .venv/bin/python scripts/web_as_user.py --scenario W1,W11 --provider claude
```

Artifacts under `bench-results/web-as-user/<utc-id>/<scenario>/`.
