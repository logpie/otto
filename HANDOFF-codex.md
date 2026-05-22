# Handoff to Codex — Otto v5 robustness pass

**Date:** 2026-05-22
**Branch:** `cc-i2p-simply` (pushed to origin)
**Latest tagged milestone:** `milestone/integration-merge-authority` → commit `4c81aec85`
**Context window producing this doc:** Claude Code session in cc-i2p-simply worktree.

---

## TL;DR

We just shipped two major v5 architectural improvements (Phase 2 = integration as single merge authority, Phase 3 = sibling owned-path overlap check). During Phase 3 live validation, a pre-existing **brittleness pattern** surfaced: the orchestrator strictly parses an LLM-produced artifact (`CHARTER.md` Foundation Contracts JSON) and gates execution on the parse result. **Two consecutive runs hit 100% failure** because the architect agent wrote `"owner"` instead of `"owner_task_id"` for the same field. Field-name drift cascades into a scheduler print-loop (62k+ emissions) and prevents feature dispatch.

User reaction: **"this is brittle. prompt fixes will come back. how can we be more robust; Otto should automatically reliably handle different decomp and contracts (even sometimes broken to some extent)."**

We Codex-gated a 5-layer robustness plan (`plan-robustness-pass.md`) through 4 rounds → APPROVED. After approval, user pushed back on the FRAME: alias canonicalization is a band-aid, not a fundamental fix. **You're picking this up to design and implement the fundamental fix.**

---

## What is committed and pushed

```
9f59167c4 docs(v5): robustness-pass plan — APPROVED after 4 rounds of Codex-gate
425df8007 feat(v5): Phase 3b — sibling-overlap retry budget + graceful degrade
9ec5f5c8b feat(v5): Phase 3a — sibling-overlap pure helpers + validator
7376fa5c2 docs(v5): Phase 3 plan — APPROVED after 3 rounds of Codex-gate
799d1fe1f cleanup(v5): T1-2 + T1-3 — delete dead helper + duplicate locals
03d1fc251 fix(v5): remove two more "work that gets redone" cycles
4c81aec85 fix(v5): foundation must merge eagerly; only features defer to integration  [TAG]
57a759186 feat(v5): Phase 2 complete — integration Lead is the only merge authority
```

Plus tag `milestone/integration-merge-authority`.

---

## Bug symptoms (the immediate trigger)

### Reproduction

`otto run --model claude-sonnet-4-6 --tier modular "<multi-domain intent>"` with the multi-domain intent at `/tmp/intent-multidomain.txt` (bookmarks + tasks + notes, 3 independent backend subsystems). **100% reproduction across 2 consecutive runs**:

| Run | Path | Outcome |
|---|---|---|
| v1 | `/tmp/linkboard-validate-phase3-092553` | killed at 21 min, 16951 chokepoint emissions |
| v2 | `/tmp/linkboard-validate-phase3-v2-094430` | killed at 27 min, 62159 chokepoint emissions |

### Mechanical chain

1. **Architect agent writes `CHARTER.md`** with a `## Information Architecture Contract` JSON block. The `foundation_contracts` array contains entries with field `"owner": "v5-..."` (the architect picked `owner` as a more natural-feeling field name, consistent with the surrounding `shared_registry_files` entries).
2. **`persist_foundation_contracts_from_charter`** (`otto/v5_capability_inventory.py:898`) parses CHARTER → emits 6+ findings of `kind=foundation_contracts_contract_invalid, detail="foundation contract entries must include owner_task_id"`. Returns `(parsed=[], findings=[6+ rejections])`.
3. **`persist_feature_owned_paths_from_charter`** (same file:1075) similarly returns rejections.
4. **`dispatch.py:393`** checks `if partition_findings:` (the all-findings gate). Sees findings non-empty → dispatches `_reenter_or_block_architect_contract` → eventually lands architect partial+annotation via the Phase 5 chokepoint.
5. **Architect verdict becomes `partial`.** Foundation_scheduler queries the task graph: foundation passed → mergeable_foundations is non-empty AND contracts is empty (because no contracts were persisted at step 2). Scheduler emits `foundation_contracts_missing_after_pass` feedback.
6. **`_reenter_or_block_architect_contract` is called again** with the new feedback kind. It tries the Phase 5 partial+annotation path again. Re-records architect as partial. **Returns control to dispatch loop.**
7. **Dispatch loop iterates.** Scheduler queries graph again. Foundation still has no contracts. Scheduler emits the same finding. GOTO 5.
8. **Each iteration emits the `terminal chokepoint: unmapped origin='foundation_scheduler' phase=None → PRODUCT` warning** via `_cause_from_origin` (`otto/v5/merge.py:1023`).

This is 2 separate bugs compounding:
- **(A) Parser strictness about field name:** `owner_task_id` vs `owner` — pure prompt-drift.
- **(B) Scheduler doesn't bail on repeated identical findings:** loops forever even though nothing's changing between iterations.

### Why this matters beyond #89

This is an instance of a broader pattern, documented in `plan-robustness-pass.md`:

> The orchestrator strictly parses an LLM-produced artifact and uses it to gate critical execution paths. Every "forgiving parser" fix preserves that dependency.

**Field-name drift is one of infinite possible deviations.** Next model version / next intent / next architect roll could:
- Write `taskId`, `ownedBy`, `owner_task`, `owner_id`
- Move the JSON block to a different section
- Substitute prose for JSON
- Add unrequested fields
- Omit required fields entirely

Aliases require us to predict and patch each. **Pure whack-a-mole.**

---

## The plan (approved by Codex round 4 — but pushed back on by user)

`plan-robustness-pass.md` proposes 5 layers of defense:

1. **Layer 1**: K=1 scheduler bail-out, K=2 retry-loop, bounded probe budgets, no-budget natural waits, content-only stable signatures.
2. **Layer 2a**: alias canonicalization (silent w.r.t. findings, observable via new telemetry channel) with alias-conflict rule.
3. **Layer 2b**: severity split in `CoherenceFinding` (`blocking|advisory`) across all 3 gate sites (foundation contracts persist + feature owned paths persist + dispatch.py:393).
4. **Layer 3 (next session)**: behavioral fallback restricted to allowlist (only `feature_owned_paths_overlap` currently).
5. **Layer 4**: concrete JSON example in `lead-architect.md`.

Plus narrow audit of touched scheduler emissions.

**User pushback after approval:**

> "even it's an easy fix (naming convention) — 1) is it general enough for other applications/product we want to build? 2) we can't always guarantee it will work, right? as it's prompt engineering. second, what is layer 2a? why are we adding more rules, is it fundamental?"

**Claude's response** (which produced this handoff):

> No, Layer 2a is a symptom fix. The disease is the orchestrator's brittle dependency on LLM-produced structured artifacts. Three genuinely fundamental approaches:
> - **A.** Schema-driven LLM output via Anthropic tool-use (force architect to register contracts via API-validated tool calls; CHARTER becomes a rendering of those calls, not source of truth)
> - **B.** Integration as contract authority (same pattern as Phase 2 — integration's Step 1 derives effective contracts from real code)
> - **C.** Runtime ownership (don't ask architect to declare; watch what features actually write)

The user agreed Layer 2a is a band-aid. The robustness-pass plan was solving the wrong-shaped problem.

---

## What you should do, Codex

Your charter: **design and implement the fundamental fix.** Throw out the symptom-fix bias I had. Lean toward Approach A unless you find a better cut.

### Specific deliverables

**1. Design doc:** `plan-fundamental-robustness.md`

Decide: Approach A (schema-driven tool calls), B (integration as contract authority), C (runtime ownership), or a synthesis. Justify the choice. Cover:

- Exact tool-call shape if A (use Anthropic's structured-output / tool-use API; see how `mcp__otto__submit_subtask` is currently shaped for reference)
- Migration path: what existing code needs to change, what becomes dead, what tests need updating
- How CHARTER.md transitions (does it become rendered? deprecated? optional human-readable summary?)
- How bug #89 dies along the way
- Sequencing: what ships in increments, what's a one-shot rewrite
- Verify: how do we know it works? Unit + integration + live runs across at least 4 diverse intents (CLAUDE.md mandates ≥4 diverse projects for prompt changes)

**2. Codex-gate the plan** (this is a non-trivial plan per CLAUDE.md rule):

You can self-review or dispatch a subagent. Aim for 2-3 rounds. The pushback should be on the FRAME, not just the implementation. Questions to stress-test:
- Does this actually eliminate the brittleness class, or just push it elsewhere?
- What new failure modes does the schema introduce?
- Does it work for non-webapp projects (CLI, library, backend-only API)?
- Cost: how many extra LLM calls per architect session?
- What if the model doesn't support tool-use well (fallback path)?

**3. Implementation:**

Workspace-write mode if dispatching subagents. **Co-Author with Codex** in commits if you adopt agent-written code.

**4. Validate:**

- Unit tests + structural tests
- Live multi-domain run (`/tmp/intent-multidomain.txt`) that previously hit bug #89 must now pass cleanly
- Phase 3 unblocks: live validation of multi-feature partition (currently blocked at task #88)
- Cost/duration should not regress meaningfully vs Phase 2c baseline ($3.45 / 22.9 min on the same intent)

### Files you need to read first

| Path | Why |
|---|---|
| `plan-robustness-pass.md` | The (approved-then-rejected-by-user) symptom-fix plan. Throw it out conceptually but read it for context — Codex's 4 rounds of review there caught real correctness issues you don't want to repeat. |
| `plan-phase-3-sibling-ownership.md` | Phase 3 design — shows the pattern of pure-helper + structural backstop |
| `otto/v5_capability_inventory.py` lines 821-1100 | The parsers being criticized: `parse_foundation_contracts`, `persist_foundation_contracts_from_charter`, `persist_feature_owned_paths_from_charter`. THIS is what becomes optional/deprecated/rendered if you go with Approach A. |
| `otto/v5_runner.py:2738-2865` | `_foundation_scheduler_feedback` and `_foundation_isolation_feedback` — the scheduler that loops. |
| `otto/v5/dispatch.py:381-490` | The dispatch architect-gate that gates on `partition_findings` non-empty. |
| `otto/v5/repair.py:1403-1510` | `_reenter_or_block_architect_contract` — the existing retry/escalation flow that's part of the loop. |
| `otto/prompts/lead-architect.md` | The architect prompt. Mentions `owner_task_id` exactly once in passing. No JSON example. This is the prompt-drift surface. |
| `otto/mcp_tools.py` | Existing tool-call shapes — your model for new schema-driven tools |
| `CLAUDE.md` | Project conventions (Hard Rules, NEVER list, Codex collaboration rules) |
| `docs/v5-v6-punch-list.md` | Bigger architecture changes already filed |

### Things NOT to do

- Don't ship alias canonicalization as a stopgap. The user explicitly pushed back. If you need to unblock bug #89 quickly, do it as part of the fundamental fix, not as a separate band-aid.
- Don't extend `parse_foundation_contracts` to be more forgiving. Same reason.
- Don't `git push --force` or rewrite history on `cc-i2p-simply`. Pushed commits are sticky.
- Don't change `_normalize_contract_path` semantics (Codex round-3 review flagged it has many permissive callers).

### Things you have license to do

- Throw out `plan-robustness-pass.md` if Approach A makes it obsolete
- Mark tasks #88 and #89 done (or refactored) when the fundamental fix lands
- Update `audit-synthesis-and-todos.md` to reflect what the fundamental approach makes obsolete
- Dispatch subagents for the implementation if it's faster (you have `workspace-write` available)
- Push a follow-up tag once the fundamental fix is live-validated

### Constraints you must honor

- **Honest test verdicts.** The session before this hand-off had 50 pass / 2 pre-existing failures on the affected test suite. Don't regress that.
- **No fresh-architect cascade.** Tests like `tests/test_no_architect_cascade.py` enforce the Phase 5 anti-cascade invariant. Your fix must preserve it.
- **Phase 2 architecture preserved.** Integration is the single merge authority for features. Foundation merges eagerly. Don't undo that.
- **CLAUDE.md Hard Rules** apply: real artifacts before claiming anything, grep whole project after edits, behavioral verification, ask before destructive operations.

---

## State of in-flight tasks (`TaskList`)

- `#88` Phase 3: blocked on live validation pending bug #89 unblock
- `#89` foundation_scheduler print-loop on malformed CHARTER: in_progress, 100% reproducible
- `#63` Phase 1 cleanup: completed
- `#84-#87` Phase 2 + multi-feature validation: completed

---

## Open questions you might need to answer

1. **Does Anthropic's tool-use API support the level of schema enforcement we need?** Test with a small example before designing around it. (JSON schema validation IS enforced at the API boundary in Claude tool calls, but verify nested structures behave as you expect.)

2. **What happens to old CHARTERs in resumed runs?** A resumed run on a checkpointed-with-old-CHARTER session should still work. Need a migration story.

3. **Does the fundamental fix need to apply to ALL load-bearing LLM artifacts, or just `foundation_contracts`?** Candidates: feature `owned_paths`, `journeys[]` per child, `intent_coverage` per child, `decisions.md` entries. Some of these have less brittleness because integration tolerates more variation.

4. **Is there a hybrid approach worth considering?** E.g. keep CHARTER.md as the human-readable description, but require contracts be ALSO registered via tool-call (single source of truth at API boundary; CHARTER is documentation that doesn't gate execution).

---

## Background you may need

- This branch is a worktree at `/Users/yuxuan/work/cc-autonomous/.worktrees/cc-i2p-simply`. Main repo is at `/Users/yuxuan/work/cc-autonomous`.
- The user has been running long sessions; multiple stale linkboard validation directories exist in `/tmp/linkboard-validate-*`. None are load-bearing.
- The CHARTER.md from the failing run is at `/tmp/linkboard-validate-phase3-v2-094430/CHARTER.md` — preserved for analysis.
- Tests run via `.venv/bin/python3 -m pytest`. Always use `--no-header -q` for tighter output.
- Live-run launch pattern: `nohup bash -lc "cd $PROD && .venv/bin/otto run --model claude-sonnet-4-6 --tier modular '<intent>'" > $LOG 2>&1 &` plus the `run_in_background` pattern documented in `~/.claude/projects/-Users-yuxuan-work-cc-autonomous/memory/feedback_otto_run_launch_pattern.md`.

---

## Pickup checklist (use this as your first message to yourself when you start)

- [ ] Read `plan-robustness-pass.md` for context on what was tried + Codex's 4-round review
- [ ] Read this handoff doc fully
- [ ] Read CLAUDE.md + the touch-point files listed above
- [ ] Draft `plan-fundamental-robustness.md` proposing Approach A (or your choice)
- [ ] Codex-gate the plan (2-3 rounds)
- [ ] Implement
- [ ] Validate with `/tmp/intent-multidomain.txt` live run
- [ ] Mark tasks #88 and #89 appropriately
- [ ] Push + tag if milestone-worthy

Good luck.
