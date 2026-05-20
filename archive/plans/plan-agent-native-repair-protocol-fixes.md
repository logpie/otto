# Agent-Native Repair Protocol Fix Plan

_written_at: 2026-05-15T00:00:00Z

## Gate Status

`/codex-gate` / `mcp__codex__codex` is not available in this tool context, so the mandatory external Plan Gate cannot be invoked from this session. I will compensate with red-first regressions, focused implementation, and full local verification.

## Steps

1. Add five focused regressions for the five substantive findings.
   - Verify: run those five tests and record that they fail before production edits.

2. Sanitize clean-verify oracle serialization.
   - Add an env allowlist helper in `otto/v5_clean_verify.py`.
   - Keep runtime execution merging sanitized packet overrides onto live ambient env in `_default_oracle_runner`.
   - Verify: secret key names/values are absent from command env, packet JSON, and `CleanOracleResult` JSON.

3. Centralize packet construction.
   - Add one shared repair-packet builder in `otto/v5_runner.py`.
   - Route integration preflight, child verify, scaffold, and merge-conflict repair through it.
   - Builder owns oracle command creation, attempt-history merge, current-state head/pre-repair-head capture, and scope-baseline capture.
   - Verify: existing packet-shape tests for child/scaffold/merge continue to pass.

4. Harden composite gate ordering and path detection.
   - Evaluate a pre-commit composite gate on dirty/staged repair changes before any commit hook.
   - After commit hook, evaluate a final gate requiring clean state and deriving committed changed paths from stored `pre_repair_head..HEAD`.
   - Verify: a commit hook that would commit conflict markers plus out-of-scope changes is blocked before commit.

5. Make repair-loop budget state durable.
   - Under the repair-unit lock, replay prior `agent_turn`, `closeout_agent_turn`, `agent_error`, and `oracle_run` events before loop decisions.
   - Enforce agent-turn, oracle, cost, wall, idle, and closeout reserve budgets between invocations.
   - Use closeout reserve only for a structured escalation turn; if no reserve remains, write escalation from packet.
   - Verify: replayed prior cost/turn state blocks repair and spends only the closeout reserve.

6. Convert provider failures into structured escalations.
   - Catch `AgentCallError` around the selected runner.
   - Preserve session id, partial cost, and error evidence in packet events.
   - Verify: provider crash returns `merge_blocked` and does not raise out of the protocol.

7. Honor agent-run oracle results before controller budget escalation.
   - Reload the packet after each agent turn.
   - If the agent has appended a passing oracle result, evaluate it through the composite gate before any controller oracle or budget escalation.
   - Verify: with controller oracle budget exhausted, an agent-appended passing oracle still yields pass.

8. Run verification.
   - Verify: focused new tests, required no-regress batch, `uv run python scripts/test_tiers.py smoke`, ruff on touched Python files, and basedpyright at level error on touched files.
