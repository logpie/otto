# Audit synthesis + todos (post-self-debate, 2026-05-21)

Triages findings from four audit docs through a corrected lens. To be
worked on AFTER the current validation run succeeds.

## Source audits
- `audit-orchestrator-brittleness.md` — original 8-finding sweep
- `audit-pre-flight-duplication.md` — pre/post-flight that duplicates agent work (REVISED after self-debate)
- `audit-snapshot-staleness.md` — capture→decide→act races
- `audit-verdict-overrides.md` — orchestrator second-guessing the agent
- `audit-dead-code.md` — vestigial code after the merge refactor

## The corrected lens (what we resolved in the self-debate)

The original "drop orchestrator pre-flight that duplicates agent work"
oversimplified. Real rule (4 cases, different answers):

| Condition | Action |
|---|---|
| Structural check is CHEAP + catches a SPECIFIC bug class prompts can't reliably prevent | **Keep** (e.g. file-system partition check Task #72 — even though the prompt says the same thing) |
| Structural check duplicates a NEW prompt that hasn't been validated end-to-end yet | **Keep for now**; revisit after ≥4 diverse runs confirm the prompt holds (e.g. foundation_gate clean-boot probe — platform-verify prompt is freshly added) |
| Structural check duplicates a VALIDATED prompt + cost of duplication is real | **Drop or refactor** (e.g. integration `pre_agent` smoke preflight — integration's start-stack is Phase-1-validated) |
| Mechanical / environmental issue dressed up as agent work | **Replace** with mechanical retry-with-backoff; don't dispatch LLM agent |

The Task #72 ("foundation_seeded_feature_path" structural check) and the
audit's "drop foundation_gate clean-boot probe" recommendation are NOT
contradictory — they're at different points in the matrix:
- Task #72: file-existence partition check (cheap + bug-class-specific + new) → keep
- Clean-boot probe: behavioral re-verification of a fresh prompt → keep for now, revisit

## Triaged todos

Ordered by impact-per-risk, ready-to-work after current validation run.

### Tier 1 — Low risk, validated, ship next

| # | What | Source | Effort | Rationale |
|---|---|---|---|---|
| **T1-1** | Drop `pre_agent` integration smoke preflight + repair-agent dispatch | pre-flight #1 | small | Integration Lead's start-stack is Phase-1-validated across ≥4 diverse runs. Same fix shape as the merge refactor just shipped. Saves $0.20+ per integration. |
| **T1-2** | Phase 3 of merge refactor: delete dead code | merge refactor + dead-code | small-medium | `_run_child_verify_repair_packet`, `_repair_child_upward_merge_after_failure`, `_refresh_child_result_from_verdict_file` are now unreachable. ~150-300 LOC delete. |
| **T1-3** | Consolidate duplicate `_branch_is_ancestor` and `_verify_child_branches_reached_parent` | dead-code #3, #4 | small | Identical duplicates between v5_runner.py and v5/merge.py. ~150 LOC. |
| **T1-4** | Replace mechanical-issue preflight repair dispatches with retry-with-backoff | pre-flight #3, snapshot-staleness #3 | small-medium | Port-cleanup, git-lock, checkout-clean — these are mechanical, not agent-shaped. Retry without LLM. |

### Tier 2 — Medium risk, design discussion needed

| # | What | Source | Effort | Rationale |
|---|---|---|---|---|
| **T2-1** | Foundation_gate clean-boot probe: keep probe, drop repair-agent dispatch (middle ground) | pre-flight #2 (REVISED) | medium | Probe is cheap insurance for a freshly-added prompt; the repair dispatch is the no-op-cost. On probe-fail, re-dispatch foundation Lead with specific failure as feedback (same shape as architect-contract retry). |
| **T2-2** | Replace `default_oracle_invocations=3` hardcoded cap with run-budget-only cap | brittleness B1 | small-medium | Hardcoded 3-invocation ceiling penalizes products that need 4-5. Run-budget is the real safety boundary. |
| **T2-3** | Stale-snapshot fix at pre-merge contract delta check (parallel-child case) | snapshot-staleness #4 | medium | Highest-severity snapshot-staleness finding. Re-snapshot the parent branch ref at merge time, not before agent dispatch. |
| **T2-4** | Stop emitting per-claim/per-action coverage-gap lint that nobody acts on | brittleness C1 | small | Already partly done (action-coverage removed); finish by demoting intent_claim coverage to internal-only signal OR removing entirely. |
| **T2-5** | Architect retry cap (`MAX_ARCHITECT_RETRIES=2`) → agent-owned adaptive retry | brittleness D1 | medium | Hardcoded retry is dumb; agent should decide based on error class (fixable vs architectural). |

### Tier 3 — Architectural, high-risk, careful

| # | What | Source | Effort | Rationale |
|---|---|---|---|---|
| **T3-1** | Contract-amendment dispatch: let violating Lead self-detect partition violation pre-yield | pre-flight #6, verdict-overrides #6 | high | Today's flow: violator commits → orchestrator detects → dispatches second agent to fix/amend. If violator detected own violation pre-yield, no second agent needed. Restructure non-trivial; touches the chokepoint and amendment flow. |
| **T3-2** | Verdict aggregation: refresh child verdicts before parent aggregation | verdict-overrides #6 | medium-high | When integration Lead resolves a child's merge_blocked, parent aggregation may use the stale verdict. Same shape as the merge-snapshot case. |
| **T3-3** | Demote remaining `validate_lead_verdict` advisory checks to off-by-default lint | brittleness C1, pre-flight #4 | medium | CHECK_KINDS gates are valid (post-bd89feb96); the advisory layer beneath produces 7-11 warnings/run that drive no action. Move to lint mode behind a `--strict-lint` flag. |
| **T3-4** | Contract rules from hardcoded → CHARTER-declared DSL | brittleness F1 | high | Path ownership rules currently hardcoded in v5_runner.py; CHARTER should declare them. Adds a DSL; payoff is composition flexibility. |
| **T3-5** | Proof packet: separate documentation from gating | brittleness G1 | medium | Today's packet conflates "proof for human" and "proof for automation"; quality_score implies gating that doesn't fire. Split. |

### Tier 4 — Verified-correct, no change needed

These showed up in audits but inspection confirms they're working as designed:

- `journey_verdict_sink` credibility check (40-char detail + evidence) — fail-closed accounting, NOT duplication
- `lead.py:331-339` no-verify auto-downgrade to unverified — intentional hard gate
- Evidence-gate downgrade — fail-closed correctness, agent knows the contract
- Spec compile schema enforcement — structural, not agent-replicable

## What got fixed in this session (for context, not action)

- Merge refactor Phases 1+2: `_ensure_child_merge_ready` no-op; per-child upward merge deferred to integration Lead (validation in flight at `linkboard-validate-bigref-170033`)
- Foundation conftest must be populated with working fixtures (lead-architect.md sharpening)
- Spec-compile coverage prompt: minimal journeys, not coverage maps
- Per-spec-compile coverage lint removed (action-coverage warning that pressured journey inflation)
- Foundation owns shared CONTRACTS (interfaces, DI surfaces) not implementations
- Feature tests test the feature; cross-feature is integration's truth
- Lead.md Hard Rule 7 added: no copying sibling code, use DI overrides
- Repair-prompt: exit on oracle-passing entry

## Sequencing for next session

1. **Wait for current validation run to complete + report** (in flight at `bigref-170033`).
2. **Ship T1-1** through **T1-4** as a single dead-code-cleanup pass — they're all consequences of the merge refactor.
3. **Live-validate again** to confirm Tier 1 holds and nothing breaks.
4. Then move to Tier 2 individually (each one a separate decision + commit).
5. Tier 3 and 4 are bigger discussions; tackle one per session at most.

## Process lessons (for future audits)

1. **The "drop duplicate orchestrator work" rule has 4 cases, not 1.** A flat rule misses the distinction between cheap structural backstops (keep) and expensive prompt-duplications (drop). The corrected lens at the top is the discipline.
2. **Trust is earned.** A new prompt-side responsibility doesn't earn the right to remove its orchestrator backstop until it's validated across multiple diverse runs.
3. **Self-debate before publishing an audit.** I shipped the original "drop foundation_gate clean-boot probe" recommendation without checking it against the Task #72 reasoning I'd just argued for. User caught the contradiction. Going forward: write the audit, then explicitly check each recommendation against ALL the prior shipped reasoning in this codebase. If they conflict, debate it before publishing.
