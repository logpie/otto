# v6.6 Consolidation Plan

## Plan Gate

- Objective: close the runner-side P1/CRITICAL v6.6 debt without product UX changes or live runs.
- Worktree: `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-2`, branch `cc-i2p-2`.
- Owned files: `otto/v5_runner.py`, `otto/v5_preflight_repair.py`, `otto/lead.py`, `otto/v5_verification_plan.py`, `otto/prompts/lead.md`, `otto/prompts/lead-integration.md`, focused tests, and this plan/research artifact.
- Main risks: accidentally adding new runner policy, breaking v5 prompt rendering, or masking dirty worktree failures instead of handing them to repair.
- Verification after each major change:
  - `uv run --extra dev pytest tests/smoke/ -q`
  - `uv run --extra dev pytest tests/ -q -k "v5" --ignore=tests/integration`
- Codex Gate checklist result: approved for implementation as a self-review gate. External Codex MCP reviewer is not available in this tool session, so this uses the local `codex-gate` checklist only.

## P0/P1/CRITICAL Decisions

1. `_checkout_v5_branch_clean` hard exception: **fix**. Convert dirty/checkout failures to `worktree_dirty_at_phase`/checkout preflight payloads and route through the repair controller.
2. Repair classifier too narrow: **simplify**. Keep deterministic port, filename, chmod fixes; default everything else to the agent. Remove USD repair cap.
3. Verdict schema brittleness: **fix**. Expand canonicalization and distinguish missing vs malformed verdict files in failure reasons.
4. Decomp reasoning via operational inputs: **fix**. Add `decomp_runtime_context` JSON with `max_parallel`, `run_budget_seconds`, elapsed time, cost model in seconds, queue state, spec profile, and runtime policy.
5. Integration packet: **fix**. Write `integration_packet.json` before integration-agent dispatch and point the prompt at it as first read.
6. Decisions.md broadcast runner heuristic: **delete**. Keep prompt context, remove path-based downgrade.
7. DAG breadth explosion: **leave for v6.7 as runner policy**. The v6.6-safe fix is prompt/runtime context bias only; no total-node guard or hard threshold in this pass.
8. FE/BE shape mismatch and missing Create Issue button: **defer**. User explicitly scoped these to the next live agent run, not runner-time fixes.

## Implementation Order

1. Branch dirty-check repair entry.
   Verify: smoke matrix above.
2. Repair controller simplification and chmod auto-fix.
   Verify: smoke matrix above.
3. Verdict parser/error-message expansion.
   Verify: smoke matrix above.
4. Decomp runtime context and prompt bias.
   Verify: smoke matrix above.
5. Integration packet artifact and prompt read-first wiring.
   Verify: smoke matrix above.
6. Delete decisions.md runner-side path heuristic.
   Verify: smoke matrix above.

## Review Trail

- Plan Gate 2026-05-14: approved by local codex-gate checklist. Risk controls are focused tests plus the requested v5 smoke matrix after each major change.
- Implementation Gate 2026-05-14: local diff review completed. Deleted the decisions.md path heuristic, preflight USD repair cap, unused smoke-preflight repair wrapper, unused auditor scaffold, and unused MCP verify detector. No provider routing or i2p monolithic paths changed.

## Results

- Fixed: dirty checkout now enters repair instead of crashing; repair defaults to agent except port/filename/chmod; verdict parser is more forgiving and reports malformed existing files honestly; lead prompt gets operational decomp context; integration sessions write/read `integration_packet.json`; decisions.md is prompt context only.
- Deferred: DAG total-node guard to v6.7 because it would add a hard threshold; iTracker product UX defects to the next live run per scope.
- Verification: six post-change smoke-matrix passes. Final pass: `tests/smoke/` 14 passed; `tests/ -k v5 --ignore=tests/integration` 302 passed. Ruff on touched Python/test files passed.
- Commit status: blocked by sandbox because git admin files live outside writable roots (`.git/worktrees/cc-i2p-2/index.lock` could not be created).
