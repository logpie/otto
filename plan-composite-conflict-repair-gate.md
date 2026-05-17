# Composite Conflict Repair Gate Plan

Written: 2026-05-16T03:46:57Z

## Goal

Make the composite landing gate part of the conflict-repair agent's durable acceptance loop. Clean-deploy passing while composite scope/markers/dirty/graph checks fail must become the next bounded repair signal, not a silent terminal override.

## Plan

1. Add the deterministic repro first.
   - Build a tiny git fixture with parent and child branches that 3-way-conflict on a shared product file.
   - Drive `_merge_child_branch`, which calls the real `merge_child_into_integration` and `_repair_child_merge_conflict_once`.
   - Monkeypatch only `otto.agent.run_agent_with_timeout` to make the agent deterministic: first turn resolves markers and leaves an unrelated file, second turn removes it after receiving the composite reason.
   - Verify: run the new test before production edits and confirm it fails with `merge_blocked` / one agent turn.

2. Make composite-gate blocks structured.
   - `_evaluate_composite_gate()` always returns `passed: bool`, `reasons: list[dict]`, and a short `summary`.
   - Include dirty paths, scope violations, conflict-marker path candidates, and unmerged state in reasons.
   - Verify: focused tests assert blocked gates never have empty reasons.

3. Feed composite failures back into the durable loop.
   - On pre-commit or post-commit composite failure, append a `composite_gate` event to the same repair packet.
   - Replace `latest_oracle_result` with a synthetic blocking oracle result whose issue message contains the structured composite reasons.
   - Continue the existing loop; if the repair budget is exhausted, use `block_with_escalation()` so terminal `merge_blocked` carries the structured reason.
   - Verify: the repro shows a second agent turn with composite feedback and successful landing; an exhausted variant blocks with escalation and reasons.

4. Add the conflict-path scope carve-in.
   - Treat `repair_unit.conflicted_paths` / conflict packet `unmerged_paths` as in-scope for merge-conflict repair, independently from owned-path scope.
   - Keep unrelated file changes blocked by the same scope gate.
   - Verify: a focused variant allows a shared conflicted file and blocks an unrelated file.

## Gate Review Note

The project instruction requires `/codex-gate`, but no Codex MCP gate tool is available in this session. This plan proceeds with local deterministic red/green proof and no-regress verification.

