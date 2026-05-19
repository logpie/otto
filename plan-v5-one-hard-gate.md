# v5: Collapse the fail-closed tower into ONE hard gate — Implementation Plan (rev 4)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use `- [ ]`.

**Goal:** "Merge resolves to a coherent product (no conflict markers anywhere), and the clean build+boot result is recorded honestly" is the *only* hard outcome. Journeys/feature/ownership/contract, **budget, missing toolchain, deadline** → land built work + annotate. The product **always commits a coherent tree**; **boots** is *measured & reported* (`true|false|unmeasured`), never inferred from git cleanliness, never used to refuse. The single non-landing outcome is `INFRA_CORRUPT` = "clean git tree/index impossible after one bounded recovery" (rare, auditable).

**Architecture:** A control-flow-aware central chokepoint that returns a **per-caller continuation** (the caller must prove the artifact-to-land exists before continuing). A **landing transaction** makes child-commit → parent-merge → ancestry → metadata atomic (or `landing_pending` + forced resume) so a timeout never orphans work. A **universal pre-commit conflict-marker scan** runs before *every* runner-owned commit. Conflict resolution: bounded semantic repair (re-scoped to builds+boots) → capped boot-maximizing deterministic fallback → measurement-only clean-oracle pass. Foundation fail → degrade to P0 scaffold under the parent-integration lock. Architect cascade → bounded local amend / degrade with a real scheduler restart.

**Source of truth:** `research-linkboard-overconstraint.md`. Behavioral reference (not a git revert): `29243bc3b` (2026-05-14).

> **Implementer contract:** re-read and confirm every cited `file:line`/shape before editing. "confirm shape" = read the real symbol, use it verbatim. Guard tests enforce completeness.

---

## Terminal cause taxonomy + per-caller continuation

`resolve_terminal_outcome(*, cause: TerminalCause, caller: CallerCtx) -> Continuation` — **no default `cause`**.

| `TerminalCause` | Meaning |
|---|---|
| `PRODUCT` | feature incomplete / behavior gap |
| `VERIFICATION` | journey/contract/ownership finding |
| `CONFLICT_RESIDUAL` | repaired/boot-max-resolved merge residual |
| `BUDGET_EXHAUSTED` | wall/turn/deadline budget hit |
| `ENV_UNMEASURED` | missing toolchain — boot cannot be measured |
| `INFRA_CORRUPT` | clean git tree/index impossible after `_bounded_git_recovery` |

`Continuation` (Codex R3 #1 — generic "fall through" is unsafe; the caller must know *how* to land):

| `Continuation` | Caller does | Used when |
|---|---|---|
| `RECOVER_THEN_COMMIT` | run `_bounded_git_recovery`, assert artifact exists, then commit+continue | conflict residual / merge-delta feedback where a tree exists |
| `COMMIT_ANYWAY_ANNOTATED` | commit current coherent tree with annotation, continue | product/verification where the child tree is valid |
| `RECREATE_WORKTREE_THEN_CONTINUE` | rebuild the missing worktree from parent, then continue | `INTEGRATION_WORKTREE_MISSING` only (no valid worktree) |
| `LAND_STOP_ALREADY_LANDED` | nothing to commit; stop child; siblings unaffected | child already committed; budget/env stop; `INTEGRATION_SETUP_SMOKE_BLOCK` (+append boot measurement) |
| `HONEST_TERMINAL` | legacy terminal; `# ALLOWED-TERMINAL:` | `INFRA_CORRUPT` only |

`resolve_terminal_outcome` maps `(cause, caller)` → `Continuation`. **Every caller must assert the artifact it is about to land actually exists** (Codex R3 #1) before acting on a non-terminal continuation.

**KEEP untouched:** `merge_source_additive_union` + driver registry, port hermeticity, `13af1ef39`, oracle-decides-repair, checkpoint/resume, the bounded semantic conflict-repair agent (re-scoped).

---

## Phase 1 — Control-flow-aware chokepoint + landing transaction + universal marker scan

### Task 1.1: Chokepoint returns a per-caller `Continuation`

**Files:** Modify `otto/v5_runner.py` (`_record_task_merge_blocked_reason`, `_record_structured_merge_failed`, all callers). Create `tests/test_terminal_chokepoint.py`.

- [ ] **Step 1 — Read & confirm.** Inventory both helpers + every caller; for each caller note *what artifact would be landed and whether it exists at that point* (Codex R3 #1 named: `7462-7474` contract feedback pre-child-commit, `7475-7497` child `commit_worktree` failure, `7500-7519` merge-delta feedback, `9063-9198` integration-setup → no worktree). Inventory terminal literals: `merge_blocked`/`catastrophic` (`~4539`), `LeadResult(verdict=...)` (`~8804`), child catastrophic (`~6435`), `cli_v5.py` `sys.exit` (`~241`). Write to `review.md`.

- [ ] **Step 2 — Failing AST guard + behavior test**

```python
# tests/test_terminal_chokepoint.py
import ast, pathlib, inspect

RUNNER = pathlib.Path("otto/v5_runner.py")
MARKERS = {"merge_blocked", "catastrophic"}


def test_no_terminal_literal_outside_chokepoint():
    src = RUNNER.read_text(); tree = ast.parse(src); lines = src.splitlines()
    bad = []
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            if isinstance(c, ast.Constant) and c.value in MARKERS:
                ln = getattr(n, "lineno", c.lineno)
                ctx = "\n".join(lines[max(0, ln - 4):ln])
                if "resolve_terminal_outcome" in ctx or "ALLOWED-TERMINAL:" in ctx:
                    continue
                bad.append(ln)
    assert not bad, f"un-chokepointed terminal literal at {sorted(set(bad))}"


def test_no_default_cause():
    from otto.v5_runner import resolve_terminal_outcome
    p = inspect.signature(resolve_terminal_outcome).parameters
    assert p["cause"].default is inspect.Parameter.empty


def test_continuation_mapping():
    from otto.v5_runner import (resolve_terminal_outcome as r,
                                TerminalCause as C, Continuation as K, CallerCtx)
    assert r(cause=C.INFRA_CORRUPT, caller=CallerCtx.CHILD_MERGE) is K.HONEST_TERMINAL
    assert r(cause=C.BUDGET_EXHAUSTED, caller=CallerCtx.CHILD_MERGE) is K.LAND_STOP_ALREADY_LANDED
    assert r(cause=C.VERIFICATION, caller=CallerCtx.CHILD_MERGE) is K.COMMIT_ANYWAY_ANNOTATED
    assert r(cause=C.CONFLICT_RESIDUAL, caller=CallerCtx.CHILD_MERGE) is K.RECOVER_THEN_COMMIT
    assert r(cause=C.PRODUCT, caller=CallerCtx.INTEGRATION_WORKTREE_MISSING) is K.RECREATE_WORKTREE_THEN_CONTINUE
    assert r(cause=C.PRODUCT, caller=CallerCtx.INTEGRATION_SETUP_SMOKE_BLOCK) is K.LAND_STOP_ALREADY_LANDED
```

> Codex R4 #1: `otto/v5_runner.py:~9063` conflates three cases. Split `CallerCtx`: `INTEGRATION_WORKTREE_MISSING` → `RECREATE_WORKTREE_THEN_CONTINUE`; `INTEGRATION_SETUP_SMOKE_BLOCK` → `LAND_STOP_ALREADY_LANDED` **and append a boot measurement** (the build/start failure is real and must be recorded `boots:false`, not hidden by a recreate loop); escalated setup-repair → classify by the concrete issue, never a blanket recreate.

- [ ] **Step 3 — Run, fail.**

- [ ] **Step 4 — Implement** `TerminalCause`, `Continuation`, `CallerCtx` enums and `resolve_terminal_outcome(*, cause, caller)` with the `(cause, caller) -> Continuation` table above (INFRA_CORRUPT→HONEST_TERMINAL; BUDGET/ENV→LAND_STOP_ALREADY_LANDED; CONFLICT_RESIDUAL→RECOVER_THEN_COMMIT; INTEGRATION_SETUP caller→RECREATE_WORKTREE_THEN_CONTINUE; else COMMIT_ANYWAY_ANNOTATED).

- [ ] **Step 5 — Route helpers (return Continuation, no default cause).**

- [ ] **Step 6 — Per-caller control-flow refactor (Codex R2 #1 + R3 #1).** For each caller: `k = _record_*(..., cause=<c>, caller=<ctx>)`; **assert the artifact-to-land exists**; then `match k`: `RECOVER_THEN_COMMIT`→`_bounded_git_recovery` then commit+continue; `COMMIT_ANYWAY_ANNOTATED`→commit coherent tree+continue; `RECREATE_WORKTREE_THEN_CONTINUE`→rebuild worktree from parent then continue; `LAND_STOP_ALREADY_LANDED`→stop child only; `HONEST_TERMINAL`→legacy + `# ALLOWED-TERMINAL:`. Document each in `review.md`. Callers where continuing is unsafe (no artifact) MUST use `RECREATE_*` or `LAND_STOP_*`, never blind fall-through.

- [ ] **Step 7 — Run + suite. Step 8 — Commit.**

**Verify:** AST guard green; injected-conflict run shows the merge commit actually in `git log` (work landed, not just "no merge_blocked").

### Task 1.2: Landing transaction (atomic; Codex R3 #4)

**Files:** Modify `otto/v5_runner.py` child-merge/propagation path + the run-deadline wrapper (`~4630`). Create `tests/test_landing_transaction.py`.

- [ ] **Step 1 — Failing test:** simulate a budget interrupt between child commit and parent merge → assert NOT an orphaned child: either the transaction completed under the reserved slice, or `landing_pending` was persisted and a forced resume re-merged it before the final verdict.

- [ ] **Step 2 — Run, fail.**

- [ ] **Step 3 — Implement** `_landing_txn(child_task_id)`: a context manager wrapping {child commit → parent integration merge commit → `source ancestor-of target` ancestry assert → metadata write}. On entry, reserve a non-interruptible budget slice (a small fixed wall-clock that the deadline checker must not preempt). If the slice is insufficient, persist `landing_pending=<child,parent,sha>` to spec-state and emit `landing_pending`; the resume path (existing checkpoint/resume) must drain all `landing_pending` and re-merge before computing the final verdict. Budget interrupts are only honored *between* transactions, never inside one.

- [ ] **Step 4 — Run, pass. Step 5 — Commit.**

**Verify:** forced mid-merge timeout → `git log` shows either the completed parent merge or a `landing_pending` that the resume drains; never a committed child branch absent from the parent.

### Task 1.3: Universal pre-commit conflict-marker scan (Codex R3 #5)

**Files:** Add `_assert_no_conflict_markers(project_dir)` to `otto/v5_branching.py`; call it before EVERY runner-owned commit (merge, semantic-repair output, scaffold degrade, integration-agent commit, child `commit_worktree`). Create `tests/test_no_marker_commit.py`.

- [ ] **Step 1 — Failing test:** stage (incl. a lockfile, e.g. `package-lock.json`) a tracked text file containing `<<<<<<<`/`|||||||`/`=======`/`>>>>>>>`; assert every runner-owned commit entrypoint refuses to commit it and routes to **`CONFLICT_RESIDUAL`** resolution (semantic/deterministic fallback or keep the clean side + attach the dropped-side patch), NOT to `INFRA_CORRUPT`.

- [ ] **Step 2 — Run, fail.**

- [ ] **Step 3 — Implement** `_assert_no_conflict_markers`: scan the **staged commit snapshot** (`git grep --cached -I -nE '^(<{7}|\|{7}|={7}|>{7})( |$)'`) *after* staging — not the worktree (Codex R4 #2). **Do NOT exclude lockfiles** — `package-lock.json`/`pnpm-lock.yaml`/`uv.lock`/`yarn.lock` are product paths (`otto/v5_branching.py:~997`) and markers there break builds; exclude only true binary (via `-I`) and vendor (`node_modules`). Invariant = "no markers in committed product paths". Wire one call into a `_runner_commit(project_dir, message)` wrapper that ALL runner-owned commits route through; on markers → route to **`cause=CONFLICT_RESIDUAL`** (semantic/deterministic fallback per Phase 3, or keep the clean side + attach the dropped patch). `cause=INFRA_CORRUPT` ONLY if, after one `_bounded_git_recovery`, git still cannot produce a marker-free clean tree/index (genuine index corruption) — markers are content residue, not index corruption (Codex R4 #3: routing them to INFRA_CORRUPT would reintroduce a hidden refusal).

- [ ] **Step 4 — Run, pass. Step 5 — Commit.**

**Verify:** grep proves every `git commit`/`commit_worktree` in `otto/v5_runner.py`+`otto/v5_branching.py` goes through `_runner_commit`; injected-marker file never reaches a commit.

---

## Phase 2 — Journeys/contract advisory; build/start/port stay HARD

(Unchanged from rev 3 — Codex R1 #5/#6 resolved.)

### Task 2.1: Verdict classifier on the CONFIRMED shape
- [ ] Confirm journey→`partial` location; payload under `verify_result["runner_checks"]` (`otto/lead.py:~352-353`); contract rows `repair_domain="spec_contract"` (`otto/v5_verification_plan.py:~129-135`). Failing test → delete journey/contract downgrade (keep only `failed_required`) → add `_verdict_is_advisory_only` reading `runner_checks`+`repair_domain` → run → commit.

### Task 2.2: Only `ui_journey_failed` → advisory `warn`
- [ ] Failing test both directions → edit `otto/v5_preflight.py:~291-315` so ONLY `ui_journey_failed`→`warn`; build/start/port/smoke keep `block` → run → commit.

**Verify:** boot failure → still preflight-`block` → `boots:false` but lands; journey-only → lands, `boots:true`, annotated.

---

## Phase 3 — Conflict: semantic repair → CAPPED boot-max fallback → measurement-only record

### Task 3.1: Re-scope bounded semantic repair to builds+boots
- [ ] Repair never refuses; terminal → chokepoint `cause=CONFLICT_RESIDUAL`. Keep the whole-side-checkout prohibition (`~8414-8421`) for the *semantic* repair. Run/commit.

### Task 3.2: Capped boot-maximizing deterministic fallback (Codex R3 #2/#3)
- [ ] Conflict-matrix tests (add/add, modify/delete both dirs, rename, binary, submodule, both-modified): coherent land, `git ls-files -u` empty, no markers, merge commit, ancestry.
- [ ] Implement typed resolver with **hard caps**: (1) syntactic parse check first (`ast.parse`/`tsc`-less), cheapest; (2) ≤`MAX_CANDIDATES_PER_FILE` candidates; (3) exactly **one** project-level build AFTER all per-file selections (never per-candidate repo-wide build); (4) strict per-merge wall-clock cap — on cap hit stop trying, keep parent-side, record `boots:false`/`unmeasured`, attach dropped side as a recovery patch artifact; never spend landing budget. `_bounded_git_recovery` runs in a **disposable temp worktree** OR snapshots `git diff` + `git diff --cached` and reapplies (Codex R3 #2 — never `merge --abort`/`reset` over already-correct staged resolutions); destructive `N = 1`.
- [ ] Run/commit.

### Task 3.3: Measurement-only oracle → append to `integration_packet.json` (Codex R2 #6/#8 + R3 #6)
- [ ] Failing test: post-fallback uses RAW `verify_from_clean_oracle`/`_run_integration_smoke_preflight` only (never `*_with_repair`/`_run_preflight_payload_repair_session`, `~8724`/`v5_preflight_repair.py:~1284`); no repair events after; result **appended** (not overwritten) to `integration_packet.json` (`~9202`).
- [ ] Implement `_append_boot_measurement(project_dir, *, boots, status, artifacts)` with `_written_at`, attempt id, `boots`, `boot_measurement_status`, recovery-artifact refs; assert the final packet retains BOTH the original integration context AND the boot truth. Budget-capped; no budget → `boots:unmeasured`.
- [ ] Separate explicit task: render a human-visible proof surface for the v5 path (extend `otto v5 run` output, `otto/cli_v5.py:~220`, or a proof renderer reading `integration_packet.json`). Run/commit.

**Verify:** injected cross-feature shared-symbol conflict → product commits; `jq '.boot_measurement_status,.boots' integration_packet.json` truthful + original context intact; dropped-side patch artifact present; no extra repair events after fallback.

---

## Phase 4 — Foundation degrade-to-scaffold, concurrency-safe (Codex R1 #7/#8)

- [ ] `_foundation_failure_action(probe_blocks)` pure; degrade deferred if any in-flight descendant worktree branched from the pre-degrade parent.
- [ ] In the foundation-gate branch (confirm lines; draft 6160-6220): acquire parent-integration lock; descendants in-flight → requeue/rebase from post-degrade parent via the existing requeue path (never overwrite live worktrees); inline confirmed `_head` logic (nested `_head()` in `_seed_scaffold_profile` ~3910-3927); import real `commit_worktree`; `materialize_seed(...)`; route the commit through `_runner_commit` (Task 1.3) and **check its return** — failure → chokepoint `cause=INFRA_CORRUPT`; success → emit `foundation_clean_boot_degraded_to_scaffold`, proceed (no block, no architect re-entry).

**Verify:** broken foundation + in-flight child → child requeued/rebased (clean, post-degrade parent), features land, boots on scaffold.

---

## Phase 5 — Architect cascade → local amend / degrade with real scheduler restart (Codex R1 #9/#10 + R2 #7)

- [ ] Replace `_reenter_or_block_architect_contract` with `_architect_contract_action(...) -> ArchitectAction{LOCAL_AMEND, DEGRADE, ANNOTATE_LAND}`. Each site sets **`restart_scheduler_loop = True`**, `break`s the architect scan; the outer `while` (repurpose `if retry_architect: continue` at `~5994`) recomputes graph/ready **before** feature dispatch.
  - `LOCAL_AMEND`: scoped `_run_scaffold_repair_packet` (~5689-5727) and/or `_schedule_foundation_contract_amendment` with required context (~1230-1243; gather at callsite or `DEGRADE`).
  - `DEGRADE`: Phase-4 degrade + annotate.
  - `ANNOTATE_LAND`: chokepoint `cause=VERIFICATION`.
  Bounded counter → `DEGRADE` on exhaustion. Preflight-invalid decomp (DAG cycle/dup id, `v5_preflight.py:~56-179`) leaving no ready work → `DEGRADE`+annotate (never loop/fresh-lead/silent).
- [ ] Guard test: no un-annotated `retry_architect = True`; enum only; `restart_scheduler_loop` drives the outer loop.

**Verify:** contract-gate failure → NO second decomposition in `messages.jsonl`; children preserved; invalid CHARTER → degrade+annotate, lands bootable scaffold; no infinite loop/empty result.

---

## Cross-cutting Verify

1. **All non-landing exits classified** (AST guard green; manual `grep -nE 'merge_blocked|catastrophic|sys\.exit|retry_architect = True|merge --abort'` across `v5_runner.py v5_branching.py cli_v5.py` — each chokepointed or `# ALLOWED-TERMINAL:`).
2. **INFRA_CORRUPT narrow:** every `HONEST_TERMINAL` preceded by one `_bounded_git_recovery`; budget/deadline/missing-toolchain = LAND_STOP (forced-timeout + missing-`tsc` tests prove land).
3. **Control flow:** `LAND_*` continuations actually commit/merge/recreate (merge commit in `git log`), with artifact-existence asserted per caller.
4. **Atomic landing:** mid-merge timeout never orphans a child (`landing_pending` drained on resume).
5. **No markers anywhere:** every runner-owned commit goes through `_runner_commit`+`_assert_no_conflict_markers`; injected 4-type markers never commit.
6. **Conflict matrix + boot-max:** booting candidate chosen when one exists within caps; else parent-side + `boots:false` + dropped-side patch artifact; recovery is temp-worktree/snapshot, N=1.
7. **Measurement-only:** post-fallback oracle schedules no repair; budget-capped; **appends** to `integration_packet.json` preserving original context.
8. **Channels:** build/start/port → preflight-`block` → `boots:false` but lands; only `ui_journey_failed` advisory.
9. **Concurrency:** foundation degrade requeues/rebases stale child worktrees; `commit_worktree` return checked.
10. **No cascade:** contract/scaffold/invalid-decomp → bounded local amend/degrade + real scheduler restart; children never discarded; exhaustion does not loop.
11. **E2E:** `otto v5 run` on `/tmp/fastrepro_linkboard_intent.txt`, logs OUTSIDE worktree, budget ≥ 2400s: both feature merges in `git log`; verdict never `merge_blocked` for journey/conflict/contract/budget cause; `boots` honest in `integration_packet.json`; no mid-flight re-decomposition.
12. **Regression floor:** `uv run pytest -q` green; `ruff check otto/` clean.

---

## Self-Review

R1 #1/#2→P1.1; #3→P3.2; #4→P3.1/3.3; #5→P2.2; #6→P2.1; #7/#8→P4; #9/#10→P5; #11→Cross-cutting.
R2 #1→P1.1 S6; #2→P1.1 AST; #3→no-default+sig test; #4→taxonomy+`_bounded_git_recovery`; #5→P3.2; #6→P3.3 raw oracle; #7→P5 `restart_scheduler_loop`; #8→P3.3 `integration_packet.json`.
R3 #1→`Continuation`/`CallerCtx` per-caller + artifact-exists assert (P1.1); #2→temp-worktree/snapshot recovery, N=1 (P3.2); #3→hard caps, one project build (P3.2); #4→landing transaction (P1.2); #5→universal `_runner_commit`+marker scan (P1.3); #6→`_append_boot_measurement` append+preserve (P3.3).

## Plan Review

### Round 1 — Codex (REVISE, 11) → addressed (rev 2; superseded by rev 4 architecture).
### Round 2 — Codex (REVISE, 8) → addressed (rev 3; superseded by rev 4).
### Round 3 — Codex (REVISE, 6 new)
- [#1] generic LAND_CONTINUE unsafe — fixed: `Continuation`/`CallerCtx`, per-caller continuation + artifact-exists assert (P1.1 S6).
- [#2] recovery erases good staged resolutions — fixed: temp-worktree/snapshot reapply, N=1 (P3.2).
- [#3] candidate build-check unbounded — fixed: syntactic-first, max candidates, one project build, wall-clock cap (P3.2).
- [#4] LAND_STOP not atomic — fixed: `_landing_txn` + `landing_pending` resume drain (P1.2).
- [#5] markers can commit outside resolver — fixed: universal `_runner_commit`+`_assert_no_conflict_markers` (P1.3).
- [#6] integration_packet persistence underspecified — fixed: `_append_boot_measurement` append + preserve original (P3.3).

### Round 4 — Codex (REVISE, 3 new) — 4-round cap reached
- [#1] `INTEGRATION_SETUP` conflates 3 cases — fixed: split `CallerCtx` → `INTEGRATION_WORKTREE_MISSING` (RECREATE) vs `INTEGRATION_SETUP_SMOKE_BLOCK` (LAND_STOP + append boot measurement); escalated setup-repair classified by concrete issue.
- [#2] lockfile markers excluded + wrong scan surface — fixed: scan staged snapshot `git grep --cached -I`, do NOT exclude lockfiles, invariant = committed product paths.
- [#3] marker failure → INFRA_CORRUPT is a hidden refusal — fixed: marker residue routes to `CONFLICT_RESIDUAL` (fallback / keep clean side + patch); INFRA_CORRUPT only if git cannot restore a marker-free clean tree after one bounded recovery.

**Gate status (rev 5):** convergent trajectory R1→R4 = 11→8→6→3, zero re-raises, zero architecture objections in R4; all 3 final issues agreed (no disagreements) and folded in. Formal `APPROVED` not returned within the 4-round cap. Per codex-gate + CLAUDE.md (implementation requires APPROVED): user decision required on closeout (one confirmation round on the 3 fixes vs. accept rev 5).
