# Mission Control Production Readiness Results

Worktree: `/Users/yuxuan/work/cc-autonomous/.worktrees/codex-provider-i2p`
Branch: `fix/codex-provider-i2p`

## Fixed Bugs

1. Failed queue tasks could not be requeued from Mission Control.
   - Root cause: retry reconstruction reused the permanent original queue id.
   - Fix: retry ids are deduplicated, e.g. `failed-feature-2`, before launching `otto queue build`.

2. Legacy queue tasks stuck in `running` could look active forever after the watcher died.
   - Root cause: legacy queue rows suppressed stale overlays and lost child process identity.
   - Fix: legacy in-flight rows preserve writer identity, accept stale overlays, show `STALE`, and expose removal.

3. Stale run detail could disagree with `/api/state`.
   - Root cause: stale detection depended on a monotonic grace tracker that reset on first poll.
   - Fix: wall-clock heartbeat age plus dead writer identity now returns a stale overlay consistently.

4. Old failed queue tasks aged out of detail view while landing still linked to them.
   - Root cause: terminal live retention dropped legacy failed/cancelled/interrupted queue rows after five minutes.
   - Fix: failed/cancelled/interrupted legacy queue rows remain inspectable while queue state references them.

5. The overview `Needs attention` metric undercounted queue failures.
   - Root cause: it only counted failed history and stale live rows, not failed/stale landing rows.
   - Fix: attention keys are de-duplicated across landing, live, and history.

6. Detail refreshes could surface stale 404 errors after a selected row was cleaned up.
   - Root cause: selected run detail/log polling kept targeting removed rows.
   - Fix: 404 detail/log responses clear or ignore stale selection instead of leaving a false error banner.

7. Detail lookup was hidden by current list filters.
   - Root cause: `/api/runs/{id}` reused list filters before resolving the selected run.
   - Fix: detail lookup resolves against the complete model so selected rows stay inspectable.

8. Action confirmations used internal action names instead of user labels.
   - Root cause: the TS client mapped action key `x` to `cleanup` even when the rendered label was `remove`.
   - Fix: confirmation titles/buttons now use the server-provided action label.

9. Codex provider crashed on large JSONL stdout lines.
   - Root cause: Codex can emit a single JSONL event containing hundreds of kilobytes of command output, while Otto used asyncio's default subprocess stream limit.
   - Fix: Codex subprocess creation now uses a 16 MiB stream reader limit and has regression coverage.

10. Web merge target could truncate default branch names containing slashes.
   - Root cause: `detect_default_branch()` split `refs/remotes/origin/fix/codex-provider-i2p` on `/` and kept only the last segment.
   - Fix: branch paths under `refs/remotes/origin/` are preserved, and Mission Control uses `load_config()` for the same target detection path as the merge CLI.

## Browser E2E Coverage

### Greenfield API

Project: `/private/tmp/otto-greenfield-api-0821`

- Queued `health-endpoint` from the web portal with provider `codex`.
- Started the watcher from the web portal.
- Codex built `GET /api/health`, added pytest coverage, passed certification, and merged through the web portal.
- Queued `ping-endpoint` from the web portal with provider `claude`.
- Claude built `GET /api/ping`, added pytest coverage, passed certification, and merged through the web portal.
- Final project verification: `uv run pytest -q` returned `3 passed`.

Provider notes:

- Codex worked end-to-end but certification was slow for a simple API change: about 5:53 in certify time, with duplicate certifier-agent activity observed in logs.
- Codex usage displayed token counts (`482.8K in / 6.9K out`) while USD cost remained `$0.00`, which appears to be provider accounting rather than zero execution cost.
- Claude completed the comparable simple API task faster, around 2:38 total with cost reported around `$0.45`.

### Failure Lab

Project: `/private/tmp/otto-failure-lab-0821`

Browser checks with `agent-browser` on `http://localhost:8778`:

- Dirty repository state showed `blocked` and disabled global/row merge buttons.
- Dirty path list included `README.md`.
- Collision warning showed `collision-a vs collision-b: shared.txt`.
- Failed task `failed-feature` appeared as `Needs attention`; overview counted one attention item.
- Failed task detail exposed enabled `requeue` and `cleanup` actions.
- Requeue confirmation used the user-facing label `Requeue`.
- Previously requeued `failed-feature-2` appeared as a queued landing/live row.

### Existing Otto Repo Copy

Project: `/private/tmp/otto-e2e-otto-copy-0821`

Browser checks with `agent-browser` on `http://localhost:8777`:

- Opened the web portal on a real Otto repo copy.
- Submitted an empty build job and verified the inline validation message `Build intent is required.`
- Queued `otto-copy-audit` with provider `codex`, reasoning effort `high`, and fast mode from the web form.
- Verified the detail panel displayed `Provider: codex / high`, branch, worktree, queue task id, and queue artifacts.
- Verified type/search filters kept the queued row visible and selected.
- Removed the queued task from the web detail panel through the in-app `Remove` confirmation.
- Verified CLI parity with `otto queue ls --all`, which reported an empty queue after removal.
- Started the watcher from the web portal with an empty queue, then stopped it through the in-app confirmation and verified the watcher returned to `stopped`.

## Residual Risks

- Codex provider is usable from Mission Control, but simple certification can be much slower than Claude because the Codex run duplicated certifier/sub-agent activity. That should become a provider efficiency follow-up, not a web blocker.
- Mission Control still uses polling. It is acceptable for the local MVP, but high-volume task lists may need pagination controls or row virtualization beyond the current history pagination.
- Remove/requeue actions are launched asynchronously. The row can remain visible until the next refresh after the subprocess completes; the UI now recovers correctly, but a future polish pass could show a per-row pending state.
- Failed runs that crash before Codex reports token usage can still show `$0.00`; successful Codex runs display token counts, while USD cost remains provider-unreported.

## Final Verification

- `npm run web:typecheck` passed.
- `npm run web:build` passed and produced `otto/web/static/assets/index-DsuiWvCp.js`.
- `uv run pytest -x -q` passed: `922 passed, 18 deselected in 102.74s`.
- `uv run pytest -q --maxfail=10` passed: `922 passed, 18 deselected in 103.19s`.
- Additional Codex stream/branch-target fix verification passed:
  `uv run pytest -q --maxfail=10` returned
  `924 passed, 18 deselected in 105.58s`.
- `git diff --check` passed.
