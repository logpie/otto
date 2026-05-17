# Proposal B — Explicit Tiered Architecture

**Stance.** Otto's pipeline is one tier (multi-group decomposition) masquerading as universal. The finance-dashboard failure proves it: `spec_compile.py` emitted `foundation.shared_contracts[browser-quality-contract].paths = ["tests/browser/**", ...]` while `transactions-ledger.owned_paths` claimed `tests/browser/test_transactions.*`, `budgets` claimed `tests/browser/test_budgets.*`, etc. — five groups writing inside foundation's contract umbrella. `detect_scope_violations` (otto/spec_compile.py:548) correctly rejected them; 36 attempts burned because the *spec was internally inconsistent before the first agent ran*. Not a prompt bug — a category error: T2 machinery on a T1 project. Fix: expose the choice.

---

## 1. Tier definitions

Four tiers. Each names exactly which Otto machinery runs.

### T0 — Solo
- **Mechanism:** one ClaudeAgent invocation. No spec compile, no groups, no merge queue, no scope check. Audit + render after.
- **Cost:** 1 agent + audit. ~$0.50–$3.
- **Use:** scripts, single-page tools, ≤3-feature MVPs, copy/UI tweaks.
- **Otto code:** `otto/agent.py`, `otto/audit.py`, `otto/render.py`. Not `spec_compile`/`build`/`merge_queue`.

### T1 — Supervised
- **Mechanism:** one **lead** agent owns the build. Otto compiles a *flat* spec — features + behavior journeys + cross-product invariants, **no `groups`, no `owned_paths`, no `shared_contracts`**. Lead may spawn subagents via the SDK Agent/Task tool when it decides it helps; Otto exposes an MCP tool (`otto.spawn_subagent(branch, intent)`) for on-demand worktree mechanics, doesn't pre-fabricate. Lead is the architect; integration is its job.
- **Cost:** 3–8× T0.
- **Use:** microblogs, finance dashboards, ~10-feature greenfield single-surface apps.
- **Otto code:** `spec_compile.py` (groups synthesis disabled), new `otto/build_solo.py`, `audit.py`, `runner.run_pipeline(mode="t1")`.

### T2 — Modular
- **Mechanism:** two-phase. **Phase 1** — *discovery agent* (no edits) explores intent, writes `ARCHITECTURE.md` + a contract manifest (modules, owned globs, shared interfaces, integration points). **Phase 2** — Otto runs build agents per module against that manifest with current `merge_queue` + scope check + shared-contract enforcement.
- **Critical change vs. today:** the manifest is *agent-authored after thinking*, not LLM-emitted from a single 5000-line `compile-spec.md` prompt that asks one model to invent decomposition from intent alone.
- **Cost:** 10–30× T0.
- **Use:** multi-surface products (web + CLI + API), browser-scale, IDE-scale, ≥4 distinct subsystems.
- **Otto code:** new `otto/discovery.py`, current `spec_compile.py` (manifest-merge mode), full `build.py` + `merge_queue.py` + `audit.py`.

### T3 — Brownfield
- **Mechanism:** repo IS the decomposition. `spec_compile.py` runs `brownfield=True` (already exists, `cli_run.py:1271`). Agent reads codebase, edits in place. No Otto-imposed structure. Audit verifies behavior journeys.
- **Use:** any modification to a non-trivial existing repo.
- **Otto code:** existing brownfield path (`_brownfield_compile_locked`, `_drive_brownfield_pipeline`). T3 is existing code, renamed/promoted.

**Mapping today:** current pipeline = T2. The `groups`/`owned_paths`/`shared_contracts` synthesis in `spec_compile.py` is T2-only and has been incorrectly applied universally.

---

## 2. Mode selection

### Explicit override (always wins)
```
otto run "<intent>" --tier t0|t1|t2|t3|auto|ask
otto run "<intent>" --no-decompose      # alias: --tier t1 + lead subagent-spawn off
```
MC: tier dropdown beside the model picker. `RunPayload` (otto/web/client/src/api.ts:144) gains `tier`; `pushPhaseArgs` appends `--tier`.

### Autopilot (`--tier auto`, default)
A **rule-based, reproducible** dispatcher (new `otto/tier_select.py`) runs *before* `spec_compile`:
1. Project root has ≥1 of {package.json, pyproject.toml, Cargo.toml, go.mod} + ≥10 source files? → **T3**.
2. Intent < 200 chars, no list markers? → **T0**.
3. Intent enumerates ≥4 distinct surfaces (regex on "web/CLI/API/daemon/SDK/extension")? → **T2**.
4. Otherwise → **T1**.

Writes choice + reasoning to `otto_logs/sessions/<id>/tier-decision.json`. Prints `tier: t1 (greenfield single-surface heuristic)`. **Does not ask.** User can `--cancel` and re-run. LLM-based dispatch is rejected: it adds non-determinism to a decision that must be reproducible.

### Conservative (`--tier ask`)
Computes choice, prompts: `Otto suggests T1. [Enter] / t0 / t2 / t3:`. CLI only. MC's "Auto (suggest)" surfaces the suggestion and waits.

### Force solo / no-decompose
`--tier t0` literal. `--tier t1 --no-decompose` synonym (T1 already has no Otto-imposed decomposition; flag just disables lead subagent spawning).

---

## 3. (Tier × project) matrix

| | small (1-page, script) | medium (~10 features, single-surface) | large (multi-surface, 30+ features) | brownfield |
|---|---|---|---|---|
| **T0** | best | works, slow context | **inadvisable** — context OOM | inadvisable — no boundary discipline |
| **T1** | overkill, allowed | **best** — finance dashboard, microblog | risky — lead overflow likely | not designed; audit vs blank spec |
| **T2** | absurd — 30 min for 1 page | **inadvisable** — what bit us today | best | inadvisable — manifest fights existing boundaries |
| **T3** | inadvisable | inadvisable for greenfield | inadvisable for greenfield | best |

Off-diagonal "slow but works" is tolerated; **inadvisable** is blocked or warned by the dispatcher.

---

## 4. Tier interaction

**Up-escalation T1 → T2.** Allowed mid-run, *only* on lead-agent request. Lead emits `ESCALATE_T2` marker with a draft contract manifest. Otto checkpoints (`otto/checkpoint.py`), stashes the lead's working tree under `otto_logs/sessions/<id>/escalation/`, runs discovery on top of the existing branch, resumes as T2. **One escalation per run**; second triggers human-confirm.

**Down-fallback T2 module → T1.** When a T2 module fails audit twice and `merge_queue` records `FAILED_SCOPE` (otto/spec_compile.py:108) for >50% of attempts in that group, Otto marks the group "contract unsound" and re-runs *just that group's slice* under T1 semantics: drop its `owned_paths`/`shared_contracts`, give one agent the union of the group's features + dependencies' contracts read-only, write anywhere, merge with conflict-detection only. This is the recovery the finance-dashboard run needed.

**Rollback.** Every tier writes `tier-checkpoint.json` after each phase. `otto run --resume` reads tier from checkpoint. T2 → T1 fallback recorded as new checkpoint with `from_tier`/`to_tier`/`reason`. No silent demotion.

**Disallowed.** T0 → T1 mid-run (just re-run from CLI). T3 → anything (brownfield is terminal).

---

## 5. Surgical plan: dies / stays / changes

**Dies in T0/T1; stays for T2/T3:**
- `otto/spec_compile.py:1280–1418` (`groups_out` synthesis loop) — gated `if mode == "t2"`.
- `otto/spec_compile.py:548–700` (`detect_scope_violations`, `detect_dependency_scope_extensions`) — T2-only.
- `otto/merge_queue.py` — entire file, T2-only.
- `otto/build.py` group orchestration — T1 uses lighter `otto/build_solo.py`.
- References in `otto/prompts/build-agent-static-policy.md` to "your slice"/"owned_paths"/"shared_contracts" — T1 gets new `build-solo.md`.

**Stays everywhere:** `otto/audit.py`, `audit_loop.py`, `render.py`, `checkpoint.py`, `journal.py`, `observability.py`, `branching.py`, `worktree.py`, `budget.py`, `spec_state.py`. Behavior journeys (audit consumes them). Repair loop (works against any diff).

**New:**
- `otto/tier_select.py` (~100 LOC): rule dispatcher.
- `otto/discovery.py` (~300 LOC): T2 phase-1 agent.
- `otto/build_solo.py` (~200 LOC): T1 lead-agent runner with optional subagent-spawn MCP.
- `otto/prompts/{compile-spec-flat,compile-spec-modular,discovery,build-solo}.md`.

**Edit pattern in `otto/runner.py`:** `run_pipeline` gains `tier: Literal["t0","t1","t2","t3"]`. Current body becomes T2 branch. T0 = `agent.run_once + audit`. T1 = `compile_flat + build_solo + audit + repair`. T3 = existing brownfield branch.

---

## 6. Verification per tier

| Tier | Pre-build | Build-time | Post-build |
|---|---|---|---|
| T0 | none | none | full audit + render |
| T1 | spec validator (features non-empty, journeys reference real features) | budget cap, marker grep | full audit + repair |
| T2 | spec validator + **manifest consistency check** | scope check, merge-queue, marker grep | full audit + repair |
| T3 | brownfield compile + git-clean precondition | budget cap | full audit vs pre-change baseline |

**Manifest consistency check** (the fix): 50-line static analysis on `spec.json`. Pseudocode: `for path in any_group.owned_paths: assert no shared_contract whose paths-glob matches path is owned by another group`. Catches the finance-dashboard failure in <1s, pre-flight. T2-only — other tiers lack the data shape that produces the contradiction.

---

## 7. CLI / MC

**CLI.**
```
otto run "<intent>" [--tier auto|t0|t1|t2|t3|ask]
otto run "<intent>" --no-decompose
otto run --resume <session>            # tier from checkpoint
otto improve <target>                   # always T3; --tier rejected
```
Default: `--tier auto`. Otto prints chosen tier before compile.

**Mission Control.** `tier` in `RunPayload` (api.ts:144), default `"auto"`. Form dropdown `[Auto (recommended) | Solo | Supervised | Modular | Brownfield]` with one-line description per option. Dashboard: tier badge next to session id; `from_tier → to_tier` arrow on escalation/fallback. Hover reveals dispatcher reasoning.

---

## 8. Failure modes

| Failure | T0 | T1 | T2 | T3 |
|---|---|---|---|---|
| Lead context overflow | rerun smaller | escalate T2 | n/a (per-module) | narrow change spec |
| Subagent failure | n/a | lead retries / absorbs | merge-queue retries; 2 failed → T1 fallback | n/a |
| Contract drift | n/a | n/a (no contracts) | manifest check pre-flight; fallback mid-run | n/a |
| Cost overrun | hard-cap, partial kept | hard-cap, lead checkpoint kept | per-module hard-cap; completed retained | hard-cap |
| Partial product | render what exists | render what exists | merged-so-far rendered, blocked groups marked | git stash revert; audit reports incomplete |
| Crash recovery | rerun (idempotent) | resume from lead checkpoint | resume from merge_queue + group checkpoints | resume from brownfield checkpoint |

---

## 9. Not confident about

- **Autopilot heuristics.** Four if-statements will misclassify. Escape valve is `--tier <explicit>`, but users unaware of tiers won't override. Telemetry (tier-decision.json + post-run verdict) needed for 4–6 weeks before tuning.
- **T1 lead context budget.** Microblog fine; finance dashboard with 28 features + 4 journeys may hit 200K tokens. If lead overflows consistently, T1 stops being the normal tier — we end up with two extremes plus a brittle middle. Mitigation: lead spawns subagents proactively.
- **T2 → T1 fallback granularity.** Rebuilding one group under T1 needs read-only dep views — easy if merged, hard if concurrent. May force serialization, eroding T2 parallelism in failure cases.
- **Discovery-agent quality.** T2 rests on Phase-1 producing a coherent manifest. If discovery is just `compile-spec.md` with extra steps, we re-implemented today's bug. Manifest-check is a backstop; coherence still requires discovery to think well. Needs its own eval harness before T2 ships.
- **Migration.** Existing `otto.yaml` and sessions assume today's T2 shape. `runner.py` carries dual codepaths for one release.
- **Naming.** T0–T3 is precise but cold. Solo/Supervised/Modular/Brownfield reads better. Ship the words, keep T-numbers in code.
