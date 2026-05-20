# Plan — make Otto v5 BUILD iTracker in <45 min (compile + recursive-decomp)

Goal: stop being a safe non-builder. Acceptance = `otto v5 run`
iTracker (Linear-lite) produces a working product, in **compile mode
AND `--tier modular` recursive-decomp mode**, in **< 45 min**, without
regressing the 89 green safety/seam tests.

## Integrated root cause (4 parallel scans, file:line)

The 58-min failed run produced **nothing** (only the foundation child
ran; 5 feature siblings never dispatched). Four compounding causes:

1. **CHARTER.md self-dirty → capstone death.** `inject_into_charter`
   (`otto/v5_capability_inventory.py:1645`) writes `project_dir/
   CHARTER.md` AFTER the architect's commit; **no git commit follows**
   (`otto/v5_runner.py:4725`, nothing through ~4790). The uncommitted
   `M CHARTER.md` makes the merge target dirty; `assert_clean_before
   _checkout` (`otto/v5_branching.py:190-199`) raises
   `MergeWorktreeDirtyError`; it slips past the `_looks_like_merge
   _conflict` guard (`v5_runner.py:6422`, matches only "conflict on:"/
   "merge conflict") into `_repair_child_upward_merge_gate_once`
   (`:5653`/`:6424`) → a 1800s LLM repair in a child worktree that
   can't even see `project_dir`'s dirt → guaranteed 1799s timeout →
   foundation merge_blocked → run dead.
2. **No mechanical/env-blocker pre-filter → 30 min (67% of budget)
   wasted.** Port-busy (non-otto zombies) and dirty-from-otto-output
   are routed straight to a 1800s LLM repair (`RepairBudget.wall
   _clock_s=1800.0` `otto/v5_preflight_repair.py:32`/`:58`,
   `v5_runner.py:2149`). Sites: `v5_runner.py:6422` (pre
   `_repair_child_upward_merge_gate_once`) and `v5_runner.py:433`
   (`_run_startup_port_cleanup_with_repair`). No classifier says "this
   is mechanical/environment → handle deterministically or fail fast".
3. **Contract-gate collisions are STRUCTURAL + recovery is blind
   rebuild.** Feature `owned_paths` are predicted by the **Lead from
   intent text before any code exists** (`lead.md:60-64`,
   `mcp_tools.py:342-389`, `queue/subtask.py:98`); `foundation
   _contracts` are authored **later by a different agent (architect)
   from the scaffold it built** (`lead.md:82-86`, parsed
   `v5_runner.py:4566-4573`). Two agents, two times, zero shared ground
   truth → `_foundation_isolation_feedback` (`v5_runner.py:4167-4179`)
   rejects the inevitable overlap and **blind-re-dispatches the whole
   architect** (`_reenter_or_block_architect_contract` `:3955-4012`,
   `MAX_ARCHITECT_RETRIES=2` `:114`) = 3 serial full rebuilds, ~17 min.
   (Merge/smoke/child-verify repair is ALREADY informed-repair via
   `_build_repair_packet`/`run_oracle_repair_agent` — the pattern to
   copy, not rebuild.)
4. **Recursive-decomp is structurally >45 min; no depth cap; no hard
   wall-clock kill.** `_process_children` recurses unconditionally
   (`v5_runner.py:4940-4952`); each level stacks a serial
   foundation→features→integration chain (~36 min each); depth-2
   recursive iTracker ≈ 70 min. Only `tree_budget_usd=25`
   (`cli_v5.py:69`, `v5_runner.py:4354`) actually aborts (→ `partial`);
   `run_budget_seconds` is advisory planner context, not enforced.

Time accounting of the 58-min run: 30 (env-blocker LLM repair) + 17
(architect rebuild loop) + 11 (spec+decomp) ≈ all of it. jv2 "good"
path ≈ 39 min (and that's with zero merge failure).

## The fixes (ordered by leverage; surgical)

### P1 — Commit CHARTER.md so the foundation can merge its own work
After `inject_into_charter(project_dir, rendered)` returns True
(`v5_runner.py:4725`): immediately `git add CHARTER.md && git commit`
on the integration branch in `project_dir` (mirror the belt-and-braces
pattern `commit_integration_worktree` already uses), OR run the
inventory inject **inside the architect worktree before its
build-commit** so the block travels with the architect's own commit.
~5–15 lines. **This alone removes the literal capstone killer.**
**Verify:** a foundation/scaffold task with a CHARTER Detected-Infra +
Foundation Contracts block merges upward with NO `MergeWorktreeDirty`;
`git status` clean post-inject; unit test on the inject→commit→merge
path; capstone foundation reaches `pass` and contracts persist.

### P2 — Derive the WHOLE ownership partition from the built scaffold (eliminates the collision class)
Stop predicting feature `owned_paths` at decomposition. The **architect
authors the full partition AFTER it builds the scaffold** (it just
created the real tree): every `foundation_contracts` path AND each
feature child's exact `owned_paths`. Feature children own only NEW
files under feature-extension globs (`registration_isolation.leaf
_extension_globs`, already in the IA contract `lead.md:76-81`) and may
NEVER own a contract/shared file. One agent, one ground truth, one
self-consistent partition → `_foundation_isolation_feedback` becomes a
no-op instead of a retry engine.
- Lead decomposition: emit feature children WITHOUT predicted
  `owned_paths` (or with provisional intent only), `task_role=feature`,
  `depends_on=[foundation]` (`lead.md:60-64`, the `submit_subtask`
  path).
- Architect/scaffold prompt + post-pass wiring (`v5_runner.py:4566-4573`
  region, `v5_capability_inventory.py` contract authoring): the
  architect emits the authoritative partition (contracts + per-feature
  owned_paths derived from the files it created); persist that onto the
  children before they dispatch.
- Keep S1's isolation gate as the cheap invariant check (it should now
  pass first try); on the rare residual ambiguity use **P5**.
**Verify:** capstone architect produces a partition where NO feature
owned_path overlaps a foundation contract on the FIRST gate check
(0 `feature_overlaps_foundation_contract` findings); architect runs
ONCE (no contract-gate re-dispatch); a fixture decomposition test
asserting derived-not-predicted ownership.

### P3 — Mechanical/env-blocker classifier; never burn 1800s on the unfixable
Add one classifier consulted at the two routing sites
(`v5_runner.py:6422` before `_repair_child_upward_merge_gate_once`;
`v5_runner.py:433` before `_run_preflight_payload_repair_session`):
- **dirty-from-otto-own-output** (dirt confined to otto-owned files,
  e.g. `CHARTER.md`): auto-commit + retry merge deterministically —
  never an LLM repair. (Backstop for P1.)
- **port-busy / zombie not otto-owned, missing-toolchain, other
  non-agent-fixable env**: fail-fast structured terminal in **seconds**
  with the exact PID/cmdline/binary — not a 1800s LLM repair. Also
  pre-kill otto's own zombie declared ports at run start.
- Slash `RepairBudget.wall_clock_s` 1800 → **~400s** (an LLM merge/build
  repair that hasn't converged in ~7 min won't in 30; 45-min budget
  cannot afford even one 30-min repair).
**Verify:** a simulated dirty-CHARTER blocker resolves deterministically
in <5 s (no LLM); a simulated foreign port-busy fails-fast structured
in <10 s; no repair-agent invocation can exceed ~400 s; existing
repair/seam tests still green.

### P4 — Bound recursion + enforce a real wall-clock deadline
- **Depth-2 cap**: only the root Lead decomposes; non-root children
  build inline (no-op the recursion at `v5_runner.py:4940` when
  ancestor-count ≥ 1, or reject `submit_subtask` from non-root Leads).
  Removes the stacked serial foundation chain (the dominant >45-min
  term). Recursive-decomp mode then = the same single
  foundation→features→integration chain as compile mode.
- **Hard wall-clock deadline** keyed to `run_budget_seconds`: actually
  enforced (graceful degrade/honest terminal at the ceiling), not
  advisory. Bound spec+decomp combined to **<8 min**.
**Verify:** `--tier modular` iTracker takes the SAME single-chain path
as compile mode (no nested foundation chains); a recursion-depth unit
test (non-root decompose is rejected/inlined); the run hard-stops at
the wall-clock ceiling instead of drifting to 58 min.

### P5 — Deterministic re-scope as the contract-collision backstop (not full rebuild)
For any residual contract collision after P2: in
`_reenter_or_block_architect_contract` (`v5_runner.py:3955-4012`),
when the violation is unambiguous (a feature owns a path that is a
declared foundation contract) **deterministically remove that path
from the feature's `owned_paths`** (the path belongs to the foundation
by contract) and re-validate in-process — ~0 cost, no agent. Only for
genuinely ambiguous feature↔feature overlap, dispatch a focused
**plan-amendment** repair (reuse `_build_repair_packet`/
`run_oracle_repair_agent`, new `otto/prompts/plan-amendment.md`,
`allowed_paths`=CHARTER+task-graph only) — NOT a full architect
re-dispatch. `MAX_ARCHITECT_RETRIES` → backstop only.
**Verify:** an injected unambiguous collision is resolved
deterministically with NO architect re-dispatch; an ambiguous one uses
the scoped plan-amendment (≤1 turn), not a rebuild; capstone shows ≤0
full architect rebuilds.

## Time budget after fixes (target <45 min)

Single chain (compile = recursive, P4): spec+decomp ≤8 (P4) +
foundation build ≈8 (runs once, P2) + features parallel ≈12 +
subtree/root integration ≈6 ≈ **~34 min**, with NO 30-min env-repair
sink (P3), NO 17-min architect rebuild loop (P2/P5), NO CHARTER death
(P1). Headroom under 45.

## Verification philosophy (deliberate — per the "safe non-builder" critique)

Acceptance is **the product builds**, not 20 gate rounds:
1. The 89 ownership/seam/repair tests stay green (don't regress safety).
2. Targeted unit `Verify:` per P1–P5 (above) — fast, deterministic.
3. **Real acceptance: one `otto v5 run` iTracker in compile mode AND
   one in `--tier modular`, each < 45 min, producing a runnable product
   (start.sh clean, root verdict ≥ partial-with-working-core).**
4. ONE adversarial Plan-Gate pass on THIS plan before implementing
   (cheap; catches design holes before a 35-min capstone). Implementation
   itself: Codex implements P1–P5, light review, then the capstone IS
   the gate — no multi-round Implementation-Gate grind (that grind is
   what produced the safe non-builder).

## Risks / rejected

- **Rejected:** more isolation gates / more bounded-retry. That is the
  architecture that produced the safe non-builder. P2 removes the need
  for the retry by removing the collision; P3/P5 make recovery
  deterministic/informed, not blind.
- **Risk:** P2 architect must author a correct full partition in one
  pass. Mitigation: it has the real tree in front of it (derive, don't
  predict); P5 is the cheap deterministic backstop; S1 gate stays as
  the invariant check.
- **Risk:** depth-2 cap reduces decomposition depth for genuinely huge
  products. Acceptable: iTracker (the bar) fits a single chain; deeper
  recursion is structurally incompatible with 45 min anyway and can be
  a separate future budget tier.
- **Risk:** 400s repair cap too tight for a legitimately big repair.
  Acceptable: a >7-min unconverged LLM repair almost never converges by
  30 min (empirically: 1799s timeouts), and the budget can't afford it;
  honest-terminal + the now-rarer collisions (P2) make it moot.

## Plan Review
(Plan-Gate trail appended below.)
