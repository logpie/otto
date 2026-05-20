# v6.5 Simplification Plan

_written_at: 2026-05-14T22:20:00Z_

## Design Principle

Trust the agent. Minimize classification. Reject patches when an LLM can decide.

The goal is not to make Otto less ambitious. The goal is to stop spending agent minutes on proving a model followed our bookkeeping rules, and spend them on building and testing the product.

## Cost Breakdown

Recent iTracker runs show the pattern:

| Run | Graph nodes | Completed session cost | Summed session duration | Failure signal |
| --- | ---: | ---: | ---: | --- |
| `v5-itracker-v6b-012759` | 15 | `$31.32` | `8,706s` | over-decomposition into frontend grandchildren |
| `v5-itracker-v6c-113615` | 9 | `$22.83` | `6,182s` | one issue-view child spent `$4.58` / `1,550s` and ended `unverified` due verdict schema handling |
| `v5-itracker-v6d-142011` | 16 | `$13.05` before completion | `3,984s` completed so far | dirty `decisions.md` merge escalated; two frontend parents had already decomposed into 9 grandchildren |

Phase signals:
- Spec compile was `335-389s` and `84K-99K` tokens for the three runs. The output is only about `28-31KB`, so the issue is reasoning load from cross-coverage, not file size.
- Root planning was `185-377s` and cheap, but emits `4-6` top-level nodes.
- Architect was `515-865s` and `$2.44-$3.72`, mostly CHARTER prose and scaffold/toolchain validation.
- Leaf sessions were usually `5-15min` each. At 8-16 sessions, setup + prompt + test overhead dominates.
- Recursive decomposition is the multiplier: v6b and v6d both had frontend parents that decomposed again, turning moderate UI scopes into many sessions.

Target shape for iTracker-class work:
- 1 compile under 3 minutes.
- 1 root planner under 4 minutes.
- 1 architect/scaffold under 8 minutes, with CHARTER under 250 prose lines.
- 3-5 build leaves, no grandchildren by default.
- 1 integration session with full matrix and live-stack smoke.
- Expected wall: `30-40min` at normal parallelism. Expected spend: `$7-$10`.

## Remove Or Relax

### Spec Compile

Remove hard cross-coverage obligations from the compile prompt and validator.

Current pressure points:
- `otto/spec_compile_flat.py:321-435` validates product overview, journey coverage, intent claim coverage, and cold-start journey shape.
- `otto/spec_compile_flat.py:444-571` asks for PM PRD, engineering layers, cross-coverage, and hard rules.
- `otto/spec_compile_flat.py:965-1000` uses `validate_structured_spec(..., strict=True)` and can reject new compiles.

Simplification:
- Keep JSON shape, max 5 journeys, and obvious minimum fields.
- Treat product overview/cross-coverage findings as warnings.
- Stop retrying or failing because every primary action maps to a journey or every claim maps to a field/action.
- Keep cache and metrics.

Verify: existing spec cache hardening tests still pass, with updated expectations that strict validation is advisory for coverage rather than a compile blocker.

### Lead Prompt / Decomposition

Cut the planning prompt down to agent-native guidance.

Current pressure points:
- `otto/prompts/lead.md:47-75` pushes decomposition with `3-7` subtasks.
- `otto/prompts/lead.md:111-308` makes architect-first a large ceremony and embeds a full IA JSON schema.
- `otto/prompts/lead.md:314-358` adds DAG and child-sizing heuristics that bias toward splitting.
- `otto/prompts/lead.md:398-457` over-specifies leaf testing strategy.
- `otto/prompts/lead.md:470-503` and `535-545` duplicate verdict and decisions schema.

Simplification:
- Default to inline for focused/moderate scopes.
- Decompose only when the scope has truly independent subsystems or would not fit in context.
- When decomposing a moderate webapp, prefer architect + 3-5 build leaves. Do not decompose a child again unless the child is clearly too large to fit.
- Architect writes concise CHARTER: IA JSON plus operational facts only; no prose restatement.
- Keep honest verdict, tests, subsystem boundaries, and no leaf cross-stack Playwright.

Verify: prompt render tests if present, smoke matrix, and manual grep that old `3-7`, `5-10 minutes`, and long IA schema blocks are gone from the prompt.

### Integration Prompt

Keep integration as the place where breadth belongs.

Current pressure points:
- `otto/prompts/lead-integration.md:117-180` correctly makes the integration agent the external verifier, but the prompt is long.
- `otto/prompts/lead-integration.md:204-233` repeats broad intent accounting.
- `otto/prompts/lead-integration.md:245-288` repeats the verdict schema.

Simplification:
- Keep arbitration, merge-blocked recovery, preflight repair, real live-stack E2E, honest intent coverage.
- Remove repetitive schema prose and detailed examples.

Verify: integration prompt still contains `merge_blocked`, `smoke_clean_deploy`, `start.sh`, no-mock live-stack verification, and verdict write requirements.

### Repair Loop

Rip out the hardcoded failure taxonomy. Keep only deterministic auto-fixes.

Current pressure points:
- `otto/v5_preflight_repair.py:83-202` classifies before repair.
- `otto/v5_preflight_repair.py:413-455` only agents `script_valid_failed`, TypeScript, and malformed verdict; everything else escalates.
- `otto/v5_runner.py:1530-1532` skips repair when `is_repairable_preflight_issue` says no.
- `otto/v5_runner.py:1588-1663` creates a scoped repair prompt from classifier fields.

Simplification:
- Auto-fix only `port_busy` and `filename_too_long`.
- Everything else goes to a repair agent by default with raw issue JSON, git status, and bounded attempts/cost.
- Escalate only repeated fingerprint, total attempt cap, cost cap, or missing repair callback.
- This would have allowed v6d's `merge_worktree_dirty`/dirty `decisions.md` to be handled agentically instead of escalating.

Verify: smoke tests cover port and filename auto-fix, unknown failure goes to agent, repeated fingerprint escalates.

### Coverage Matrix

Make full structured matrix integration-only by default.

Current pressure points:
- `otto/v5_verification_plan.py:94-151` already supports `matrix_scope="integration_only"`.
- `otto/lead.py:456-470` defaults to `"leaf"`, preserving full matrix at every node.
- `otto/v5_verification_plan.py:321-503` runs page/route/endpoint/action/feedback/empty-state checks that are often outside a leaf's local scope.
- `otto/v5_verification_plan.py:900-913` requires all journeys to be passed by the node under full matrix.

Simplification:
- Change default matrix scope to `integration_only`.
- Leaves get local evidence, no stub text, verdict consistency, deprecation warnings, and decisions broadcast checks.
- Integration gets full matrix and journey pass checks.

Verify: existing tests for integration-only scope pass; update default test expectations where they encode old leaf matrix default.

### Verdict Schema

Keep forgiving parser. Do not add validators.

Current state:
- `otto/lead.py:470-860` already canonicalizes non-canonical success/partial verdicts, finds misplaced verdicts, rescues inline JSON, and has a one-shot rewrite fallback.

Simplification:
- Preserve and slightly broaden canonicalization only if tests reveal a real gap.
- Remove prompt hostility that makes non-canonical shapes fatal in the agent's mind.

Verify: smoke verdict canonicalization tests pass.

### Context Slicing

Do not make slicer the main fix.

Current state:
- `otto/v5_context_slicer.py` is off by default and adds another deterministic transformation layer.
- `otto/v5_runner.py:1134-1170` safely supports opt-in.

Simplification:
- Leave opt-in available.
- Shrink the source artifacts so full context is cheaper.

Verify: no behavior change required.

## What Stays

- Deterministic auto-fixes for ports and filenames.
- Clean-deploy smoke checks.
- Scaffold preflight that catches broken shells/toolchains.
- Smoke matrix in `tests/smoke/`.
- Nested subtree propagation and the silent self-merge guard in `_propagate_subtree_integration`.
- Forgiving verdict rescue.
- Provider routing.

## Implementation Steps

1. Relax compile validation and prompt.
   - Verify: focused spec tests and smoke matrix.

2. Simplify repair controller and runner hook.
   - Verify: smoke preflight repair tests.

3. Default verification matrix to integration-only.
   - Verify: verification-plan tests and smoke matrix.

4. Shorten Lead and integration prompts.
   - Verify: grep removed heuristics; smoke matrix.

5. Re-run smoke tier.
   - Verify: `uv run pytest -q tests/smoke` and `uv run python scripts/test_tiers.py smoke`.

## Plan Gate Result

Proceed. The simplification has a narrow code surface, deletes or relaxes code instead of adding classifiers, and leaves the smoke matrix as the protection layer. The main risk is under-verification at leaves; the mitigation is explicit integration-only full matrix plus local evidence/deprecation/stub checks at leaves.

