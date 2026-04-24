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
- Product UX pass: in progress.
- Real E2E: pending.
- Code-health release pass: pending.
