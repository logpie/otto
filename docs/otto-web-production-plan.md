# Otto Web Production Plan

## Product Target

Otto Web should feel like a local mission-control product, not a thin dashboard.
The user should be able to express intent, launch work, monitor progress, inspect
evidence, understand blockers, and land completed branches without knowing the
internal queue/run/merge model.

## Implementation Plan

1. TypeScript foundation
   - Replace the static imperative client with a typed Vite + React client.
   - Keep FastAPI as the backend and keep committed static build output so
     `otto web` requires no Node runtime.
   - Maintain typed API contracts for project, watcher, queue, landing, live,
     history, detail, logs, artifacts, and actions.

2. User workflow improvements
   - Add an operational overview that surfaces active work, failed/stale work,
     merge-ready work, repository blockers, and watcher status.
   - Make merge blockers visible before action: dirty paths, collision preview,
     and disabled merge buttons.
   - Make job intake self-serve with provider/model/effort inheritance, optional
     dependency ordering, validation, and clear submission errors.
   - Make run detail useful for audit: provider/model/effort, branch/worktree,
     status overlays, logs, artifacts, and artifact content.
   - Preserve actions for cancel, resume, retry, cleanup, and merge while
     disabling unsafe actions when preconditions fail.

3. Correctness and resilience
   - Keep API guards server-side for stale browser tabs and direct API calls.
   - Keep dirty-tree merge safety conservative; do not auto-revert user data.
   - Keep browser state synced through polling and direct refresh.
   - Ensure empty states, disabled states, and error messages are explicit.

4. Verification and evaluation
   - Unit/API tests for Mission Control service and web endpoints.
   - Typecheck and production build for the client.
   - Browser E2E with `agent-browser` against a live real project.
   - Real project scenarios:
     - Empty repo first experience.
     - Queue jobs from the web portal.
     - Start/stop watcher from the portal.
     - Inspect live logs and history.
     - Inspect artifacts/evidence.
     - Hit blocked merge path with tracked local changes.
     - Hit merge-ready path after resolving local changes.
     - Exercise failed/stale/retry/cleanup paths where practical.
   - Final code-health pass after implementation, not before.

## Current Status

- TypeScript foundation: done in `6bcf89c3d`.
- Product UX pass: implemented.
- Real E2E: completed against `/tmp/otto-web-e2e-kanban`.
- Code-health release pass: pending.

## Implementation Notes

- Ported the static web client to a typed Vite + React app while keeping
  committed static assets for `otto web`.
- Added an operational overview strip for active work, attention, ready work,
  repository state, and watcher state.
- Added a self-serve job dialog with provider/model/effort inheritance,
  dependency input, validation, and disabled submit state.
- Added in-app confirmation dialogs for merge, cleanup, and watcher stop.
  Native browser confirms were removed because they were invisible in product
  screenshots and brittle in browser automation.
- Added artifact count, artifact browsing, overlay reason, and improved run
  detail display.
- Kept server-side guards for dirty-tree merges and direct API calls from stale
  browser tabs.

## Bugs Found By Real E2E

1. Overview active count used live rows instead of watcher state.
   - Symptom: a queued/done row could make the overview show active work even
     when the watcher had no active children.
   - Fix: compute active count from watcher task states.

2. Merge actions used native `window.confirm`.
   - Symptom: agent-browser clicks appeared to succeed but merge did not launch
     because the native confirm path was not inspectable or reliable.
   - Fix: replace native confirms with an accessible in-app confirmation modal.

3. `otto merge --fast` still ran post-merge certification.
   - Symptom: web merge showed as long-running and spawned Claude even though
     the CLI prints `--fast (pure git, no LLM)`.
   - Fix: make fast merge imply certification skip in the orchestrator and pass
     `--no-certify` from Mission Control merge actions.

4. Mission Control action-launched child processes inherited the web server
   process group.
   - Symptom: a merge live record could report the web server PGID, making
     fallback cancellation unsafe.
   - Fix: launch action subprocesses with `start_new_session=True`.

5. Stale pre-fix merge rows needed a clear user recovery path.
   - Symptom: a killed pre-fix merge remained visible as stale.
   - Fix validated: stale merge row exposed cleanup; cleanup from the web UI
     cleared it and restored `Needs attention` to 0.

6. Merge-ready actions could re-include already merged queue tasks.
   - Symptom: when one task was already merged and another became ready,
     `merge ready` could launch `otto merge --all`, making the merge run mention
     previously landed branches.
   - Fix: `--all` resolution now skips branches already recorded as merged by
     prior merge states.

7. Merge run ids could collide inside one Python process.
   - Symptom: tight merge regression tests could create multiple merge runs in
     the same second with the same `merge-<timestamp>-<pid>` id, overwriting
     `state.json` evidence for the earlier run.
   - Fix: merge ids now include a short random suffix and have a uniqueness
     regression test.

## Real E2E Evidence

- Project: `/tmp/otto-web-e2e-kanban`, a real FastAPI mini-kanban app generated
  by Otto, with git branches and queue state.
- Codex provider path:
  - Queued from web: add card priority support.
  - Watcher launched from web.
  - Build/certify completed: 5/5 stories passed.
  - Merged from web after in-app confirmation.
  - Resulting project tests: `7 passed in 0.20s`.
- Claude provider path:
  - Queued from web: add optional due dates and overdue UI.
  - Build/certify completed: 5/5 stories passed.
  - Merged from web with patched fast merge path.
  - Resulting project tests: `14 passed in 0.23s`.
- Mission Control user paths exercised with agent-browser:
  - New job validation and queue submission.
  - Provider selection for Codex and Claude.
  - Start watcher.
  - Stop watcher with in-app confirmation.
  - Merge-ready view and merge confirmation.
  - Stale merge cleanup.
  - Artifact/log detail browsing.
  - Dirty-project merge-blocking path on `/tmp/otto-greenfield-kanban`.

## Remaining Product Gaps

- Claude due-date certification passed but used source review for the overdue UI
  callout rather than a live browser rendering of an actually overdue card.
- Cleanup buttons are available after successful merge; the UI could better
  explain whether cleanup removes only live bookkeeping or queue worktrees.
- Long-running provider work still needs better budget and time affordances in
  the overview.
