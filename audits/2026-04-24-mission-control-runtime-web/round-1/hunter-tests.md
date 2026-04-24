# Test Hunter

## Fixed / Added Coverage

- Added web API regression coverage for:
  - Review packet and changed-file display for ready queue tasks.
  - Failed queue tasks remaining inspectable and requeueable.
  - Healthy runtime status on a clean local repo.
  - Malformed queue file and unfinished command drain surfacing as runtime recovery issues.
  - Stale held queue lock reporting as a stale runtime.
  - Unheld queue lock files being ignored.
  - Stale watcher PID without a held lock not being kill-targeted.

## Live E2E

- Launched web Mission Control against `/tmp/otto-mc-live-e2e`.
- Used `agent-browser` session `otto-prod-e2e` to verify:
  - Runtime overview showed command-drain and failed-task recovery issues.
  - Failed task detail showed review packet, failure reason, evidence count, and requeue action.
  - Ready task detail showed `2/2` stories, one changed file, `git diff main...build/add-report`, and merge confirmation.
  - Web New Job dialog queued a real `audit-trail` task with provider `codex`, effort `high`, and `--fast` inherited into `.otto-queue.yml`.

## Residual Test Risk

- No long-running real LLM build/certify cycle was launched in this pass; the focus was Mission Control runtime/UX correctness and recovery behavior.
