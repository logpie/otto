# Otto Architecture v5 — Unified Plan

Synthesized from proposal-A (dynamic), proposal-B (tiered), and the red-team critique. Honors the user's three constraints:

1. Works across small → complex projects.
2. Doesn't lazily default small projects to a single agent.
3. Users can pick: manual decomposition, autopilot decomposition, or no decomposition.

Anchored to Otto's mission: autonomous intent → product, reliably.

---

## 0. What's actually broken

Two bug *classes*, both real, neither addressed by today's i2p:

**Class 1 — Pre-frozen ownership lies.** `spec_compile.py` invents groups + owned_paths + shared_contracts from intent text alone, before any code exists. The finance-true-web run shows the failure mode: `tests/browser/**` claimed by foundation as a shared contract while every downstream group held `tests/browser/test_<feature>.*` in its owned_paths. 36 doomed agent attempts. No validator should have to catch this — the synthesis should not produce it.

**Class 2 — Self-authored brittle artifacts.** finance-dash-claude failed at foundation itself: same agent wrote `<a>Finance Dashboard</a>` brand link AND `<a>Dashboard</a>` nav link AND a Playwright journey calling `getByRole('link', { name: 'Dashboard' })` non-strict. Strict-mode collision, blocked at attempt 3 / $2.41. The agent grades its own homework with an instrument it just calibrated against itself.

Class 1 is a decomposition problem. Class 2 is a verification-actor problem. Both are about Otto's posture — when does Otto trust the agent vs. when does Otto interpose mechanics that prevent the agent from fooling itself.

---

## 1. Architecture (unified)

Otto = **mechanics + tools + verifier + workflow shells**. The workflow shell is selected by user or autopilot from four tiers. The shells share invariants; the tier choice changes how decomposition and ownership are *sourced*, not whether they exist.

### Tiers

| Tier | Decomposition source | Ownership source | When |
|---|---|---|---|
| **T0 — Solo** | None | None | True one-shots: scripts, single-file changes, ≤2 features, intent < 200 chars. Used only when explicitly requested or when autopilot is highly confident. **Not the default for "small" projects.** |
| **T1 — Lead** | Lead agent decides at runtime; may spawn subagents via Agent/TeamCreate. Otto provides per-subagent worktree mechanics on demand. Spec is *flat* (features + behavior journeys), no groups/owned_paths/shared_contracts. | Lead authors each subagent's contract as it dispatches; Otto enforces via write-time locks (§4). | Default for greenfield single-surface apps: microblogs, finance dashboards, dashboards, internal tools. |
| **T2 — Modular** | Two-phase. Phase 1: a *discovery agent* (read-only) explores intent, writes `ARCHITECTURE.md` + module manifest. Phase 2: parallel subagents per module against that manifest. The manifest is agent-authored *after thinking*, not LLM-emitted from intent text. | Manifest declares per-module ownership. Otto enforces via write-time locks + manifest-consistency pre-flight. | Multi-surface products (web + CLI + API), browser-scale, IDE-scale, ≥4 distinct subsystems. |
| **T3 — Brownfield** | Existing code IS the decomposition. | Existing module boundaries. | Any modification to an existing repo with non-trivial code. |

### Cross-tier invariants

These hold in every tier:

1. **Build agent ≠ test agent.** Two separate Claude sessions with different system prompts. Build agent writes app code, NEVER writes tests/journeys. Test agent (renamed from "certifier") writes tests + journeys, NEVER touches app code. Closes the finance-dash-claude failure class. The test agent's selectors are written without bias toward what the build agent emitted, because it sees the *running product*, not the source.

2. **Verifier owns the PASS gate.** Audit is derived from `behavior_journeys` that came from the intent (preserved across tiers). Builders cannot self-attest. Same audit mechanic as today; we don't lose proof-of-work.

3. **Write-time lock-based prevention, not post-hoc audit.** When a subagent is dispatched in T1/T2, Otto's `lock_paths(agent_id, globs)` registers the agent's writable surface. The Write/Edit/Bash tools are wrapped to fail-fast on violations — preventing 6 minutes of doomed work, instead of catching it post-hoc. T0 has no lock; T3 uses the existing-code structure as an implicit lock.

4. **Manifest consistency check pre-flight.** When a manifest exists (T2 always; T1 if lead hands one in), validate before any agent runs: no path appears in both `module[A].owned_paths` and `module[B != A].shared_contract.paths` unless explicitly listed in `allowed_extension_paths`. Hard error, not warning. Catches Class 1 in <1s.

5. **Best-effort acceptance.** Already landed (commit 808ba7ec6). Hard caps on retries; render whatever's there with honest verdict (`pass | partial | blocked`).

6. **Resume works on session_id checkpoint regardless of tier.** Today's mechanism preserved. Subagent sessions in T1/T2 are tracked in the lead's checkpoint manifest.

7. **Provider fallback.** Each phase's provider is configurable; on credit-exhaust or transient terminal errors, Otto can swap to the configured fallback (e.g. codex → claude). Cross-provider cost is summed in summary.json.

### What Otto stops doing

- **No more "spec compiler invents groups."** T1 produces a flat spec. T2's manifest comes from the discovery agent, post-thinking.
- **No more post-hoc scope policing as the primary mechanism.** Locks prevent writes; manifest pre-flight catches contradictions. Post-hoc detection becomes a backstop, not a primary defense.
- **No silent multi-tier dispatch.** Tier choice is logged, written to `tier-decision.json`, surfaced in MC, reproducible from intent + flags.

---

## 2. User controls

Two orthogonal axes, both user-controllable:

### Axis A — Decomposition tier (`--tier`)

```
otto run "<intent>" [--tier auto|t0|t1|t2|t3|ask]
```

- `--tier auto` (default): rule-based dispatcher in `otto/tier_select.py`, deterministic, writes reasoning to `tier-decision.json`.
- `--tier <explicit>`: user picks. Always wins.
- `--tier ask`: dispatcher proposes, prompts for confirm/override.
- `--no-decompose`: alias for `--tier t0` if that's truly what's wanted, OR `--tier t1` with subagent-spawn disabled (configurable via `--no-subagents`).

**Autopilot heuristics (rule-based, reproducible):**

```
if has_existing_code(project_dir): tier = T3
elif intent_complexity(intent) >= "large" (>=4 surfaces detected, or intent > 1500 chars with structure): tier = T2
elif intent_complexity(intent) == "trivial" (single sentence, no list, no UI, < 200 chars): tier = T0  # rare
else: tier = T1                                                                                       # default
```

`tier_select.py` is plain Python, no LLM. Misclassifications go to telemetry; the dispatcher is tuned in code, not by another model.

**Important:** the default is **not** "small → T0." Default is **T1**. T0 fires only when the heuristic is highly confident the work is trivially one-shot. This honors the user's "don't lazily default small to single agent."

### Axis B — Operator mode (`--mode`)

```
otto run "<intent>" [--mode autopilot|supervised|solo]
```

- `--mode autopilot` (default): no pauses; Otto and the lead make decisions independently; review only at the end.
- `--mode supervised`: lead must `otto.checkpoint(reason)` before spawning subagents, before merging, before finalizing. MC surfaces a plan diff and waits for approval.
- `--mode solo`: Agent/TeamCreate stripped from the build agent's tool list. Forces single-agent execution at the build phase. Compatible with any tier (T1+T2+T3 still apply for the orchestration logic; the build agent itself has no subagents).

`--no-decompose` is `--mode solo --tier t1` in canonical form (single lead, no fan-out).

### Other knobs

- `--max-parallel N` (default 4): hard cap on concurrent subagents at any depth.
- `--decomp-depth N` (default 2): subagent recursion cap.
- `--budget-usd N` / `--budget-min N`: budget; `BUDGET_EXCEEDED` injected on lead's next turn at threshold.
- `--review-gate spec|plan|merge|none`: when MC opens review threads. Default `plan` in supervised, `none` in autopilot.

### MC surface

`RunPayload.tier`, `RunPayload.mode`, `RunPayload.max_parallel`, `RunPayload.decomp_depth`, `RunPayload.budget_usd` join `provider`. Form has two dropdowns (Tier, Mode) and an Advanced disclosure with the rest. Tier badge on every run card, with hover showing the dispatcher's reasoning if auto.

---

## 3. What dies / stays / changes

**Dies (deleted, not gated):**
- `otto/spec_compile.py` group synthesis (lines 1280–1418). T1 doesn't need it. T2 uses discovery agent. T3 uses brownfield path. Net deletion: ~400 lines.
- `otto/spec_compile.py` shared_contracts auto-synthesis from features. The discovery agent emits these in T2; never invented from intent in T1.
- `otto/build.py::FAILED_SCOPE` post-hoc detection as primary gate. Becomes backstop for unexpected violations only.

**Gated to T2/T3 only:**
- `otto/spec_compile.py:548–700` (`detect_scope_violations`, `detect_dependency_scope_extensions`).
- `otto/merge_queue.py` (entire file). T1 doesn't need merge queue — lead agent merges as it goes. T2 still uses it. T3 not applicable.
- `otto/build.py` group orchestration loop. T1 uses new `otto/build_lead.py`.

**New code:**
- `otto/tier_select.py` (~150 LOC): rule-based dispatcher.
- `otto/build_lead.py` (~250 LOC): T1 lead-agent runner.
- `otto/discovery.py` (~300 LOC): T2 phase-1 agent + manifest emission.
- `otto/locks.py` (~200 LOC): write-time path locking, sqlite-backed per worktree.
- `otto/build_test_separator.py` (~150 LOC): the build-agent ↔ test-agent split. Build agent prompt explicitly forbids touching `tests/`; test agent prompt explicitly forbids touching everything else.
- `otto/manifest_check.py` (~100 LOC): pre-flight consistency check (correctly-implemented glob containment, not naive string compare).
- New prompts: `compile-spec-flat.md`, `compile-spec-modular.md`, `discovery.md`, `build-lead.md`, `test-agent.md`. Existing `build.md`, `build-agent-static-policy.md` rewritten.

**Stays unchanged:**
- `otto/audit.py` (the verifier, behavior_journey-derived).
- `otto/checkpoint.py`, `otto/resume.py`, `otto/budget.py`, `otto/observability.py`.
- `otto/branching.py`, `otto/worktree.py`, `otto/queue/`.
- `otto/web/` (modulo new RunPayload fields).
- `otto/render.py` (proof packet rendering).
- `otto/cli_*.py` (modulo new flags).

---

## 4. Lock semantics (closes Class 1 cleanly)

`otto/locks.py`:

```python
def acquire(agent_id: str, globs: list[str]) -> LockHandle: ...
def release(handle: LockHandle): ...
def check_write(agent_id: str, path: str) -> Result[None, LockViolation]: ...
```

Per-worktree sqlite. The Write/Edit tools are wrapped via the SDK's `can_use_tool` hook; a lock violation returns an error that the agent reads and acts on (re-route, request upstream change, or escalate). No commits attempted with violations — they fail at tool-call boundary.

**Critical:** locks are sourced from the *manifest* in T2 and from the *lead's runtime declaration* in T1. They are NOT pre-fabricated by Otto from intent text. The lock IS the agent's contract, made by the agent.

---

## 5. Build-agent ↔ test-agent separation (closes Class 2)

In T0/T1/T2:

- **Build agent** receives the build prompt + product code surface. System prompt explicitly: "You write app code. You never write `tests/` or `*.test.*` or `*.spec.*` or browser journeys."
- **Test agent** runs after the build agent commits. Receives behavior_journeys + access to the *running product* (not its source — read-only browser/HTTP). System prompt: "You write tests. You never modify app code." Selectors are written from the running DOM, not from the source — automatically resilient to brand/nav collisions.
- **Repair flow** dispatches whichever side caused the failure (build-side bug → build agent gets feedback; test-side flake → test agent re-writes the journey).

This is the architectural fix the critic was right to call out. Today, Otto's certifier is a separate session but it generates *check commands*; the actual journey files are written by the build agent. Splitting authorship at the file level closes the self-collision.

---

## 6. Open questions explicitly noted

The critic flagged things neither proposal handled. Listing them so we don't ship without addressing:

1. **Mid-run intent amendment** (multi-week builds). Out of scope for v5; track separately.
2. **Cross-module repair** (Feature A's repair needs change in B's owned paths). T1: lead re-plans, releases sub-locks, re-dispatches. T2: discovery agent re-runs on the affected interface; manifest is regenerated; merge_queue replays. Need to design — not block v5 ship.
3. **`intent.md` drift mid-run.** Snapshot at session start, hash compared on resume. If drifted, prompt user to re-confirm.
4. **Cost ceiling across nested subagents.** Otto wraps the Agent tool to slice budgets; if SDK doesn't support per-call budget natively, Otto checks the running total before each Agent dispatch and refuses if remaining budget can't fund a typical sub-call.
5. **Determinism.** Tier dispatcher is deterministic. Lead-agent decisions are not (model is non-deterministic). Sessions can be replayed from message-stream JSONL; "reproduce" means resume from checkpoint + same model version, not bit-for-bit.
6. **Malware-reminder injection on Read for non-Opus-4.6 models.** Document the hazard. Opus-4.6 escape: per-phase model override. Not blocking for v5.
7. **Two MC users, same project.** Today's queue lock applies. T1's lead is per-task; locks are per-worktree-per-task. Cross-task arbitration is unchanged.

---

## 7. Ship sequence

Strict ordering. Each milestone independently shippable, each closes a real failure observed in the wild.

### M1 — Manifest pre-flight + tier flag plumbing (this week)

- `otto/manifest_check.py` — correctly-implemented glob containment. Hard error on contradictions like finance-true-web's. Single PR; per memory rule, dispatch the implementation to Codex (it found the bug class via review).
- `--tier` flag wired through `otto/cli_run.py` and `otto/web/client/src/api.ts`. Choices `auto|t0|t1|t2|t3|ask`. Default `auto`.
- `otto/tier_select.py` rule dispatcher. `auto` defaults map to T2 today (preserves current behavior); only `t0` and `t1` need new code paths to be implemented in M2.
- `tier-decision.json` written per session. MC tier badge.

**Outcome:** finance-true-web class can no longer reach the build phase. Users can opt into T0/T1 once those exist. Today's behavior is preserved as `--tier t2`.

### M2 — T0 single-agent path + T1 lead-agent flat spec (next 1–2 weeks)

- `otto/build_lead.py` — flat-spec lead runner. No groups, no owned_paths.
- `otto/locks.py` minimum viable: per-worktree sqlite, registered against the lead's runtime declarations. Tool wrapper for Write/Edit.
- New prompts `compile-spec-flat.md`, `build-lead.md`.
- `tier_select.py` updated: trivial intents → T0; default greenfield → T1.

**Outcome:** finance dashboard runs through T1 in ~10–15 min (target). The hour-long codex burn becomes impossible — no contradictory contracts to thrash on, locks prevent doomed work, lead orchestrates serially or fans out per its own judgment.

### M3 — Build-agent ↔ test-agent separation (parallel with M2)

- `otto/build_test_separator.py` + `test-agent.md` prompt.
- T0/T1 configured to use separate sessions.
- Audit/render unchanged.

**Outcome:** finance-dash-claude class closed. Self-locator-collision class closed.

### M4 — T2 discovery agent + modular pipeline (after M2 ships and benches)

- `otto/discovery.py` + `discovery.md`.
- T2 wires discovery → manifest → existing build/merge_queue with manifest-driven owned_paths + shared_contracts.
- The current `spec_compile.py` group-synthesis path is removed; T2 *only* runs through discovery.

**Outcome:** browser-scale and multi-surface projects have a coherent architecture. T2 is no longer "T1 with extra groups" — it has its own thinking phase.

### M5 — Provider fallback, intent drift, polish (ongoing)

- Cross-provider cost accounting.
- `intent.md` drift detection on resume.
- Subagent budget slicing wrapper.
- MC supervised-mode plan-diff visualizations.

---

## 8. Bench plan

Before ripping out today's `spec_compile.py` group synthesis, benchmark T1 against today's pipeline + M1 manifest check on:

- finance-dashboard (the bug class M1 catches)
- microblog (current i2p evaluation)
- ops-dashboard (medium complexity)
- one brownfield refactor (verify T3 still works)

Compare wall time, cost, feature coverage. T1 must be at least competitive on all three before T2 is built — otherwise we're investing in a tier that wasn't validated.

---

## 9. Honest weak points of this synthesis

- **T1's lead-context budget.** Finance dashboard with 28 features may push 200K tokens. Mitigation: lead spawns subagents proactively. If consistently overflows, T1 stops being the normal tier.
- **The build-agent ↔ test-agent split adds tokens.** Two sessions per slice, separate context. Worth the safety, but cost goes up. Acceptance: lower attempt cost beats wasted-attempt cost.
- **Lock semantics under crash.** Stale locks need a TTL or process-liveness probe. Otto's existing watcher can release locks for dead processes; design pending.
- **Migration of running sessions.** Existing checkpoints assume T2-shape. Resume code needs `tier` field detection; sessions without it default to T2.
- **Tier dispatcher rules are heuristic.** They will misclassify. Telemetry + the `ask` mode are the safety nets. Tuning happens in code, with bench data, not by another LLM.
