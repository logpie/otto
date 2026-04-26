# Token And Runtime Centralization Audit

Date: 2026-04-26
Branch: `codex-i2p`

## Scope

This pass audited provider token accounting, Mission Control runtime cleanup, and nearby duplicated accounting or process logic that could drift across CLI, queue, web, and benchmark surfaces.

## Fixed

- Centralized token addition, normalization, and totals through `otto.token_usage`.
- Corrected Codex/OpenAI `cached_input_tokens` handling so cached subset tokens are not counted as additional cache-read tokens.
- Updated logstream phase aggregation to use shared token helpers and to ignore cost resets when deciding whether token counters are cumulative.
- Updated certifier, pipeline, agent, queue summaries, Mission Control model, and web UI to preserve all token fields: input, cache creation, cache read, cached subset, output, reasoning, and total.
- Made stale phase-message usage overwrite stale summary breakdown token fields.
- Made Mission Control watcher start honor `queue.concurrent` from `otto.yaml` when the UI does not explicitly override it.
- Hardened watcher stop/shutdown so it terminates recorded in-flight queue children in their own process groups, waits, and escalates to SIGKILL when needed.
- Validated `queue.worktree_dir` as a relative in-project path, rejecting empty, absolute, and `..` traversal values.
- Removed duplicate benchmark merge-cost parsing from `scripts/bench_runner.py` and `scripts/bench_synthetic_skip.py`; both now use `scripts/bench_costs.py`.

## Process Cleanup

Cleaned stale orphaned processes left from older Otto and demo runs, including old queue runners, certifier child processes, and uvicorn/http demo servers. Kept the active live Mission Control portal and watcher running:

- Web portal: PID `28496`
- Watcher: PID `43143`

Also kept the current product preview process for the active expense-portal worktree.

## Remaining Centralization Candidates

- Process identity checks are still spread across queue runner, run registry, Mission Control runtime, service cleanup, and path lock handling. A shared `ProcessIdentity`/`ProcessProbe` helper should own pid, pgid, start time, cwd, zombie handling, and permission-denied semantics.
- Mission Control run config is still reconstructed from current project config in some views. Durable run records should snapshot effective provider/model/reasoning/certification options at run start.
- Cost display formatting still differs between CLI, logs, and Mission Control. Token totals are now centralized; cost display should get the same treatment.
- Stale queue cleanup is improved by process cleanup, but UI-level cleanup semantics should explicitly distinguish remove, retry, resume, mark interrupted, and preserve artifacts.

## Verification

- `uv run pytest -q tests/test_token_usage_phase_logs.py tests/test_logstream.py tests/test_config.py tests/test_web_mission_control.py::test_web_start_watcher_uses_configured_default_concurrency tests/test_web_mission_control.py::test_web_can_stop_stale_but_live_watcher_process tests/test_web_mission_control.py::test_web_allows_stop_for_supervised_live_watcher_pid tests/test_web_mission_control.py::test_web_paused_spec_review_exposes_approve_and_regenerate_actions tests/test_watcher_orphan_cleanup.py --maxfail=1`
- `uv run ruff check ...`
- `npm run web:typecheck`
- `npm run web:build`
- `git diff --check`
