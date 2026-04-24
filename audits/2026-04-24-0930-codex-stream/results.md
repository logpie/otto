# Codex Stream and Complex Web E2E Results

Worktree: `/Users/yuxuan/work/cc-autonomous/.worktrees/codex-provider-i2p`

## Real E2E Scenario

Project: `/private/tmp/otto-complex-web-20260424-093052`

Mission Control queued a real Codex task against a fresh Otto repo clone:

```text
Add a focused regression test for Mission Control web queue recovery: when a
legacy failed queue task is selected, /api/runs/{id} must remain inspectable
under unrelated list filters and expose an enabled requeue action.
```

The first task failed in 11 seconds before edits with:

```text
Agent crashed: Separator is not found, and chunk exceed the limit
```

Root cause: Codex emitted one JSONL event containing large command output from a
broad `rg`; Otto read stdout with asyncio's default 64 KiB stream limit.

## Fixes Landed

1. Codex stream handling
   - Added a 16 MiB stdout reader limit for the Codex subprocess.
   - Added a regression assertion that `_query_codex()` passes the enlarged
     limit into `asyncio.create_subprocess_exec`.

2. Web detail under unrelated filters
   - Ported the Codex-built regression so `/api/runs/{id}` remains inspectable
     under unrelated list filters and exposes enabled requeue.

3. Default branch detection for slash branch names
   - Fixed `detect_default_branch()` so `origin/fix/codex-provider-i2p` returns
     `fix/codex-provider-i2p`, not `codex-provider-i2p`.
   - Mission Control now uses the same config loader as the merge CLI for its
     landing target, even without `otto.yaml`.

## Retry Outcome

After the stream fix, the failed task was requeued from the web UI as
`complex-filter-requeue-2`.

Result:

- Build completed and committed `4c6fdb7eb test web queue detail under filters`.
- Certifier passed 5/5 stories.
- Full project test suite in the task worktree passed: `922 passed, 18 deselected`.
- Runtime: `build=4:48, certify=1:46, total=6:35`.
- Usage shown by Mission Control: `1.3M in / 5.2K out`.

## Merge Outcome

The first merge attempt exposed the slash-branch default detection bug. After
fixing it:

- Web `Merge 1 ready` landed the ready task.
- Merge id: `merge-1777049241-95715-5fe70e1b`.
- Merge target: `fix/codex-provider-i2p`.
- Merge commit in the temp project:
  `bf5fabff6 Merge branch 'build/complex-filter-requeue-2-2026-04-24' into fix/codex-provider-i2p`.
- Restarted Mission Control displayed:
  `1 already merged / 1 not ready into fix/codex-provider-i2p`.

## Verification So Far

Focused regression command:

```text
uv run pytest tests/test_agent.py::test_codex_query_normalizes_json_events \
  tests/test_config.py::TestDetectDefaultBranch::test_preserves_origin_head_branch_path \
  tests/test_web_mission_control.py::test_web_keeps_failed_queue_tasks_inspectable_for_requeue \
  tests/test_web_mission_control.py::test_web_landing_target_preserves_detected_branch_path -q
```

Result: `4 passed`.

Final gates:

- `npm run web:typecheck` passed.
- `npm run web:build` passed.
- `uv run pytest -q --maxfail=10` passed:
  `924 passed, 18 deselected in 105.58s`.
- `git diff --check` passed.
