## Observations

- Reproduced during a real Mission Control web run on a fresh complex Otto repo
  clone: `/private/tmp/otto-complex-web-20260424-093052`.
- Task: `complex-filter-requeue`, queued from the web UI with provider `codex`
  and effort `medium`.
- The task failed in 11 seconds during build before any code edits.
- Exact error: `Agent crashed: Separator is not found, and chunk exceed the limit`.
- Crash traceback points to `otto/agent.py` awaiting
  `raw_line = await stdout.readline()` inside `_query_codex`.
- Provider stderr shows Codex emitted JSONL events and was in the middle of an
  `item.started` event for a broad `rg` command when Otto crashed.
- The same `rg` command in the task worktree produced `579322` bytes of output.
- A minimal asyncio subprocess reproduction that prints one JSON line larger
  than the default stream limit raises `ValueError ... chunk is longer than limit`.

## Hypotheses

### H1: Codex JSONL stdout lines can exceed asyncio's default stream limit (ROOT HYPOTHESIS)
- Supports: traceback is `asyncio.exceptions.LimitOverrunError` wrapped as
  `ValueError` by `StreamReader.readline`; broad `rg` output was about 579 KB;
  Codex encodes command output inside one JSONL `aggregated_output` event.
- Conflicts: none.
- Test: pass a larger `limit` to `asyncio.create_subprocess_exec` for Codex and
  verify the subprocess is created with that limit.

### H2: The provider emitted malformed JSON without a newline
- Supports: error mentions separator not found.
- Conflicts: Python's `readline()` reports the same error when a line is valid
  but longer than the stream limit; provider stderr before the crash is valid
  JSONL.
- Test: inspect the provider stderr prefix and reproduce a long valid JSON line.

### H3: Otto's Codex command quoting made `rg` search too broad
- Supports: the model chose a broad `rg` command over the whole repo.
- Conflicts: broad searches are normal agent behavior in large repos; Otto's
  reader should not crash when provider output is large.
- Test: measure the command output size and compare it with the default stream
  limit.

## Experiments

- Minimal subprocess experiment: a child printed one JSON line with a 70 KB
  string. Reading it with default `asyncio.create_subprocess_exec(...,
  stdout=PIPE)` raised `ValueError Separator is found, but chunk is longer than
  limit`, confirming the limit-boundary behavior.
- Output-size experiment: the real `rg` command produced `579322` bytes, far
  above asyncio's default 64 KiB reader limit.

## Root Cause

Otto's Codex provider used asyncio's default subprocess stream limit, but Codex
can emit one JSONL event containing hundreds of kilobytes of command output.

## Fix

- Increase the Codex subprocess stream reader limit to 16 MiB.
- Add a regression test asserting Codex subprocess creation uses the enlarged
  limit.

## Follow-up Observation

The retry run exposed a second real web-merge bug. The temporary clone's
`origin/HEAD` pointed at `origin/fix/codex-provider-i2p`, but
`detect_default_branch()` truncated it to `codex-provider-i2p`. Web-triggered
merge then failed because the current branch was `fix/codex-provider-i2p`.

Fixes:

- Preserve branch paths when parsing `refs/remotes/origin/HEAD`.
- Make Mission Control's landing target call `load_config()` even when
  `otto.yaml` is absent, so the UI and merge CLI use the same detected target.
- Add regressions for branch-path detection and landing target display.

Live verification after the fix:

- Requeued task `complex-filter-requeue-2` completed with Codex and certified
  5/5 stories.
- Web `Merge 1 ready` merged
  `build/complex-filter-requeue-2-2026-04-24` into
  `fix/codex-provider-i2p`.
- Restarted Mission Control showed the landing target as
  `fix/codex-provider-i2p`, and the merge detail recorded the same target.
