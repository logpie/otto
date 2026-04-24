# Production Bug Hunter

Scope: Codex adapter subprocess handling, default branch detection, and Mission
Control landing target detection.

## Findings

1. fixed - Codex subprocess stdout could crash on large JSONL events.
   - Severity: high.
   - Location: `otto/agent.py`.
   - Evidence: real Mission Control Codex task crashed on a broad `rg` output;
     asyncio `readline()` fails when a single provider JSONL line exceeds the
     default 64 KiB stream limit.
   - Fix: pass `limit=CODEX_STDIO_LIMIT_BYTES` to
     `asyncio.create_subprocess_exec`.

2. fixed - Default branch detection truncated branch paths.
   - Severity: high for worktree/feature-branch default clones.
   - Location: `otto/config.py`.
   - Evidence: `refs/remotes/origin/fix/codex-provider-i2p` was detected as
     `codex-provider-i2p`, causing web merge to fail its target-branch
     precondition.
   - Fix: strip the `refs/remotes/origin/` prefix and preserve the remaining
     branch path.

3. fixed - Mission Control landing target and merge CLI could disagree.
   - Severity: important.
   - Location: `otto/mission_control/service.py`.
   - Evidence: without `otto.yaml`, the web landing model used `main` while the
     merge CLI used auto-detected config.
   - Fix: `_merge_target()` now calls `load_config()` even when `otto.yaml` is
     absent.

## Rejected Candidates

- The remaining `except Exception` blocks in `MissionControlService` are
  pre-existing defensive adapters around optional queue/run state and merge
  preflight. They are not introduced by this change and are covered by existing
  recovery behavior.
- A larger stream limit could increase memory exposure if the provider emits
  very large single lines. The chosen 16 MiB limit is bounded and comfortably
  above the observed 579 KiB crash case.
