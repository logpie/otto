# Otto v5 — Implementation Plan

Draft 1. Synthesizes the philosophy decisions and architecture from prior conversation. Plan to be reviewed by three CC agents with different framings.

---

## 0. North star

Otto converts intent into a stream of well-described product states. The user reads the stream when convenient, intervenes when they want, and is never required to.

Three philosophy commitments that constrain every design decision:

1. **Autonomy is the default.** Supervised mode is a rare opt-in. The architecture has no human-in-the-loop blocking points outside explicitly-requested supervision.
2. **Best-effort everywhere; advance always.** Every loop (tool call, subagent, build, merge, audit) bounds retries, terminates in an honest state, and lets work continue. Hard blocks only on catastrophic infrastructure failure or explicit user opt-in.
3. **The agent decides decomposition; Otto provides the rails.** Otto is workspace mechanics + tools + verifier + queue + merge coordinator. Otto does not predict groups/owned_paths/contracts from intent text. Decomposition is the agent's runtime decision, sourced from real surfaces.

---

## 1. Architecture

### 1.1 Two persistent units, one transient unit

- **Project** (persistent). Lives in `~/otto-projects/<name>`. Has a `main` branch. Accumulates code, history, intent. Never destroyed by Otto.
- **Queue** (persistent, per-project). Tasks live here. Includes user-submitted and agent-emitted tasks. Otto's queue runtime already exists (`otto/queue/`); we extend it.
- **Task** (transient). One PR-sized change. Has a worktree, a Lead, a verdict, a proof packet. Merges to `main` and is gone.

### 1.2 The Lead — the only build primitive

A Lead is a Claude session with the following tools available:

- Standard Claude Code tools (Read, Write, Edit, Bash, Glob, Grep, TodoWrite).
- `Agent` / `TeamCreate` (in-session subagents — for tightly-coupled parallel work).
- `otto.lock_paths(agent_id, globs[])` — register write-time locks for self or in-session subagents.
- `otto.submit_subtask(intent, depends_on=[...], group_id=None)` — emit a new task to the project's queue.
- `otto.verify(claim_set)` — run the deterministic verifier against the running product.
- `otto.checkpoint(reason)` — persist state for resumability. Non-blocking in autopilot.

The Lead's job: read its task's intent, read the project's `main` (so it knows the existing code), decide how to do the task, do it, leave the worktree ready to merge.

The Lead's three decomposition choices, on a per-task basis:

| Mode | When | Cost |
|---|---|---|
| Inline | Small task; tightly-coupled work; Lead can hold it all in context. | 0 fan-out cost. |
| In-session subagents (`Agent` / `TeamCreate`) | Tightly-coupled parallel work where context-sharing matters (e.g., refactor two related files at once). | One-time, transient. Subagent results return to Lead. |
| Emit child tasks (`otto.submit_subtask`) | Genuinely independent work. Each child gets its own worktree, own Lead, own PR. | Persistent, observable, cancellable. Each child task is a normal task. |

The Lead picks. Otto doesn't.

### 1.3 Build agent ≠ test agent

Inside any task, Otto runs *two* sessions, not one:

- **Build agent.** Receives task intent + project main. System prompt explicitly forbids touching `tests/`, `*.test.*`, `*.spec.*`, browser journey files. Writes app code, runs unit tests, commits to worktree.
- **Test agent.** Runs after build agent commits. Receives the *running product* (read-only browser/HTTP/CLI access), the intent's behavior journeys, and the current test suite (read+write). System prompt forbids touching app code (`src/**`, etc., scoped by project layout). Writes tests + journeys based on what it actually observes in the running system.

The split closes the self-attestation class of failures (finance-dash-claude: same agent wrote brand link, nav link, AND the brittle locator that collided on both).

When repair is needed, Otto invokes whichever side caused the failure (build-side bug → build agent; test-side flake → test agent).

### 1.4 Verifier (audit) — the only PASS gate

`otto/audit.py` derives behavior journeys from the project's accumulated intent and runs them against the integrated product. Build/test agents cannot self-attest a PASS — they can only ship code; the verifier is the gate.

Audit runs:
- Per-task before merge (validates the task's specific contribution).
- Per-merge against `main` (validates project-level coherence).

### 1.5 Cross-task merge

`otto/merge_queue.py` (kept and rescoped from "groups in a session" to "tasks against project main"):

```
git rebase task-branch onto main
  ↓ clean
land. update main. trigger project audit.
  ↓ conflict
resume task's Lead with conflict + new main + original intent
  ↓ resolved
re-attempt rebase
  ↓ exhausted (3 attempts)
fresh integration Lead with full context
  ↓ exhausted
mark task `merge_blocked`. continue with other tasks.
```

When task audit on `main` fails:

```
git revert offending merge automatically (last successful merge)
file fix-task with audit failure context
  ↓ fix-task fails
file another fix-task with cumulative context (cap 3)
  ↓ all exhausted
mark regression `unfixable`. project state becomes `degraded`. continue.
```

### 1.6 Locks

`otto/locks.py` (new): per-worktree sqlite. The Lead registers locks for itself and any in-session subagents. Otto's tool wrapper (via SDK `can_use_tool` hook) returns an error on Write/Edit attempts to paths not in the agent's lock. The agent reads the error and either re-routes or escalates back to its parent.

Locks are write-time prevention, not post-hoc detection. They eliminate the 6-min-doomed-attempt class.

Locks have a TTL. A crashed agent's locks release automatically after the TTL. Otto's existing watcher (`otto/queue/runtime.py`) extends to perform liveness probes.

### 1.7 Discovery agent (used at the Lead's discretion)

When a task's Lead determines that the project benefits from up-front architecture thinking before implementation, it invokes a discovery agent (`otto/discovery.py`, new). The discovery agent is read-only (no Write/Edit/Bash-mutation). It produces:

- `ARCHITECTURE.md` (committed to the worktree).
- `manifest.json` (lists modules, owned paths, shared interfaces).

The manifest then goes through `manifest_check.py` (new, mechanical glob containment): if any module's `owned_paths` overlap another module's `shared_contract.paths` without explicit `allowed_extension_paths`, the manifest is rejected and the discovery agent re-runs with the failure as feedback.

Discovery is one option a Lead may choose, not a mandatory phase. Triggers:
- Lead's heuristic detection (intent enumerates ≥4 surfaces, large codebase, etc.).
- Explicit user request: `--think-first` / preset `--tier modular`.

### 1.8 Brownfield is just "Lead reads existing code first"

When a Lead's task targets an existing codebase (detected by Otto via repo state — package files present + ≥10 source files), the Lead's prompt is configured to mandate reading the codebase before any decomposition decision. The existing module boundaries become the lock structure. No spec-imposed structure.

### 1.9 Tier as preset, not separate codepath

There is one Lead implementation. "Tier" is a set of knob defaults:

| Preset | Knobs |
|---|---|
| `t0` / `solo` | `--no-decompose`, `--no-subagents`, `--no-think-first` |
| `t1` / `lead` | `--decompose=auto` |
| `t2` / `modular` | `--think-first=required`, `--decompose=encouraged` |
| `t3` / `brownfield` | `--read-existing-first` (auto-set when repo non-empty) |

The dispatcher (`otto/tier_select.py`, new, deterministic rule-based, no LLM) picks a preset based on:
1. Project repo state (existing code → brownfield).
2. Intent length and complexity markers (≥4 enumerated surfaces → modular).
3. Single-sentence trivial intent → solo.
4. Default → lead.

Defaults to lead, never to solo for normal-sized projects (per user's "don't lazy-default small projects to single agent").

User can override with `--tier <explicit>` or specific knobs. `--tier auto` is the default.

---

## 2. User-facing surface

### 2.1 CLI

```
otto run "<intent>" [--tier auto|t0|t1|t2|t3|ask]
                    [--mode autopilot|supervised|solo]
                    [--max-parallel N]                 # cap on concurrent subagents at any depth (default 4)
                    [--max-depth N]                    # subagent recursion cap (default 2)
                    [--budget-usd N | --budget-min N]
                    [--no-decompose]                   # alias for --tier t0
                    [--think-first]                    # force discovery agent
                    [--no-think-first]
                    [--no-subagents]                   # forbid spawning helpers
                    [--review-gate spec|plan|merge|none]   # supervised only

otto run --resume <session>
otto improve <target> [--tier t3]                # always brownfield
otto queue submit "<intent>"                     # add to queue without running now
```

`--mode autopilot` is the default. `--tier auto` is the default.

### 2.2 Mission Control

`RunPayload` extended with: `tier`, `mode`, `max_parallel`, `max_depth`, `budget_usd`, `think_first`. Form has Tier and Mode dropdowns and an Advanced disclosure for the rest.

Tier badge on every run card. Hover shows the dispatcher's reasoning if `auto`. Verdict pill (pass / partial / unverified / merge_blocked / regression_unfixable / degraded / catastrophic).

MC dashboard surfaces project state across all tasks: queue depth, in-flight count, recent verdicts, regression count, budget burn. No approval gates in autopilot — it's an information surface.

### 2.3 Configuration

`otto.yaml` extended with:

```yaml
defaults:
  mode: autopilot
  tier: auto
  max_parallel: 4
  max_depth: 2
  budget_usd: 5
  retry_budget:
    build_attempt: 3
    merge_resolution: 3
    fix_task: 3
    audit_resolution: 3
  preferred_provider: claude
  fallback_provider: codex-app-server
```

---

## 3. Verdict and state vocabulary

Every task and every project carries a verdict. Verdicts are richer than today's `pass | fail | blocked` because best-effort everywhere requires honest partial states.

### 3.1 Task-level verdicts

| Verdict | Meaning | Recovery? |
|---|---|---|
| `pass` | All checks green; merged to main; coherent. | None needed. |
| `partial` | Built and merged; some declared features missing within budget. Honest list in proof packet. | User may file follow-up tasks. |
| `unverified` | Built and committed; the verifier itself failed/timed-out. Code unverified. | Re-run audit; or accept. |
| `merge_blocked` | Built fine, can't integrate with current main after retry exhaust. Worktree preserved. | User may inspect; or Otto re-tries when main quiets. |
| `catastrophic` | Infrastructure failure (provider auth, disk, etc.). | Fix infra; resume. |

### 3.2 Project-level states

A project, integrated across all tasks, is in one of:

| State | Meaning |
|---|---|
| `green` | All recent merges audit-pass. |
| `degraded` | One or more `regression_unfixable` items present. New work continues; user is notified. |
| `quarantined` | Auto-rule (e.g., 5 consecutive audit failures): pause new dispatches, finish in-flight, surface for user. Rare. |

### 3.3 Proof packet

Every task produces a proof packet (`proof-packet.html` + `.json`). It must show:

- Task intent.
- Verdict (one of above).
- What worked: features, journeys, screenshots, video.
- What didn't: failed checks, missing features, blocked merge details, audit findings.
- Cost, duration, tokens.
- Provenance: which Lead, which subagents, which fix-tasks, what tier, what knobs.

Proof packet is generated regardless of verdict. There is no failed run that lacks a proof packet.

---

## 4. Surgical migration from current Otto

### 4.1 Deleted

- `otto/spec_compile.py` group/contract synthesis (lines 1280–1418, ~400 lines).
- `otto/spec_compile.py::_normalize_critical_shared_contract_scope` and friends (~200 lines, no longer needed once locks are write-time).
- `otto/build.py::detect_critical_shared_contract_violations` and similar post-hoc detection paths (~600 lines).

### 4.2 Gated to T2/Discovery only

- `otto/spec_compile.py:548–700` (scope violation detection) — used only inside discovery's manifest validation.
- Existing `merge_queue.py` group-in-session FIFO logic — still runs but rescoped to cross-task merge.

### 4.3 New code

| File | Purpose | Approx LOC |
|---|---|---|
| `otto/lead.py` | The single Lead implementation. Replaces `build.py` group orchestration. | 300 |
| `otto/locks.py` | Write-time path locking, sqlite-backed per worktree, with TTL + watcher liveness probe. | 250 |
| `otto/tier_select.py` | Deterministic rule-based dispatcher. | 150 |
| `otto/discovery.py` | Read-only architecture-discovery agent (used at Lead's discretion). | 300 |
| `otto/manifest_check.py` | Mechanical glob containment validator. | 150 |
| `otto/test_agent.py` | Test-agent runner (separate Claude session, post-build). | 200 |
| `otto/state.py` | Verdict + project-state machinery. Reads/writes per-project state file. | 200 |
| `otto/queue/subtask.py` | Lead-facing tool: `otto.submit_subtask`. | 100 |
| Prompts: `lead.md`, `discovery.md`, `test-agent.md`, `compile-spec-flat.md`, `manifest-check-feedback.md` | Replace `build.md` and `compile-spec.md`. | n/a |

### 4.4 Unchanged

`otto/audit.py` (verifier), `otto/checkpoint.py`, `otto/resume.py`, `otto/budget.py`, `otto/observability.py`, `otto/branching.py`, `otto/worktree.py`, `otto/queue/runtime.py` (extended slightly), `otto/web/`, `otto/render.py`, `otto/cli_*.py` (extended for new flags).

### 4.5 Backward compatibility

`otto.yaml` files without the new defaults section: Otto fills in defaults silently, user sees no change.

Existing checkpoints / sessions: `runner.py` resume path detects schema version. Sessions without `tier` field are treated as `t2` (today's behavior). New sessions write `tier`. After one release, both schemas coexist; after two releases, old schema is read-only.

---

## 5. Implementation milestones

Each milestone independently shippable. Each closes a real failure observed in the wild. Each can be deployed without forcing the next.

### M1 — Quick win: manifest pre-flight + tier flag plumbing (this week)

**Scope.** No architectural change yet. Two surgical fixes:

1. Implement `otto/manifest_check.py` correctly. Glob containment check for spec.json: for every `shared_contracts[c]` with `critical=True`, no `groups[g != c.owner_id].owned_paths` may contain a glob intersecting `c.paths` unless explicitly listed in `c.allowed_extension_paths`. Fail compile with structured error naming the conflict.
2. Wire `--tier` flag through `cli_run.py` and `web/client/src/api.ts`. Choices `auto|t0|t1|t2|t3|ask`. Default `auto`. `auto` maps to today's behavior (`t2`). All other choices currently raise `NotImplementedError` — placeholders for M2/M4.
3. Write `tier-decision.json` per session.

Per existing memory rule (`feedback_codex_fixes_own_bugs.md`), the implementation is dispatched to Codex (it found the bug class via review).

**Outcome.** finance-true-web class can no longer reach build phase. UI accepts new flag (does nothing yet). All other behavior unchanged.

**Test.** Synthetic spec with the finance-true-web overlap pattern → `manifest_check` rejects pre-flight in <1s.

### M2 — Lead, locks, T0/T1 (1–2 weeks)

**Scope.**

1. `otto/lead.py` — single Lead implementation. Reads task intent, runs build agent, runs test agent, calls verifier, terminates with verdict.
2. `otto/locks.py` MVP — sqlite-backed, register/check/release. SDK `can_use_tool` hook. TTL = 30 min default; reset on any tool use. Watcher releases stale locks.
3. `otto/state.py` — verdict + project state machinery.
4. `otto/test_agent.py` + `test-agent.md` prompt.
5. New `lead.md` build prompt. Build agent prompt explicitly forbids touching `tests/**`.
6. `otto/tier_select.py` rules. Auto defaults map: brownfield → existing path; intent < 200 chars + no list → t0; default → t1. T2 still routes through current pipeline.

**Outcome.** finance-dashboard runs through T1 (Lead) in ~10–15 min instead of an hour. Self-attestation class closed (build/test split). Locks prevent doomed work.

**Test.**
- Bench T1 against today's pipeline + M1 manifest check on: finance-dashboard, microblog, ops-dashboard, one brownfield refactor.
- Compare wall time, cost, feature coverage, verdict distribution.
- T1 must be at least competitive on all four before T2 is built.

### M3 — Lead-emitted subtasks + queue extension (1 week, parallel with M2)

**Scope.**

1. `otto/queue/subtask.py` — Lead-facing `otto.submit_subtask` MCP tool.
2. Queue runtime accepts agent-emitted submissions, schedules them with declared deps.
3. Project-level audit pipeline: post-merge to `main`, run audit; on fail, auto-revert + file fix-task.
4. Extend MC to display agent-emitted tasks (icon distinguishing user-submitted from agent-emitted).

**Outcome.** A T1 Lead facing a task too big for one session can decompose into child tasks. The "build a browser" scenario becomes natively expressible — Lead emits ~7 child tasks, each with its own Lead.

### M4 — Discovery + T2 modular (2 weeks, after M2 benches)

**Scope.**

1. `otto/discovery.py` + `discovery.md` prompt.
2. `t2` codepath: Lead invokes discovery → manifest_check → modular build with locks from manifest.
3. Existing `merge_queue.py` rescoped to cross-task instead of intra-session.

**Outcome.** Browser-scale and multi-surface projects have a coherent architecture. Discovery thinks first; manifest is from thinking, not from intent text alone.

### M5 — Robustness layer (ongoing)

**Scope.**

1. Provider fallback (codex-out-of-credits → claude transparently).
2. Cross-provider cost accounting (summed in summary.json).
3. `intent.md` drift detection on resume.
4. Subagent budget slicing wrapper.
5. Verdict vocabulary fully implemented in MC and proof packet rendering.
6. Bounded-retry self-healing for fix-tasks (cap 3, terminal `regression_unfixable`).
7. Project state machine (`green | degraded | quarantined`).

**Outcome.** Otto is robust to provider failures, mid-run drift, integration regressions. Self-heals where possible; honestly reports where not.

---

## 6. Test strategy

### 6.1 Unit tests

Each new module has targeted unit tests. Specific fixtures for:
- `manifest_check`: the finance-true-web overlap pattern; deeply-nested glob containment; allowed_extension_paths whitelisting.
- `locks`: register, check, release, TTL expiry, conflict scenarios, crash recovery.
- `tier_select`: each rule branch; explicit override; auto-detection of brownfield.

### 6.2 Integration tests / benchmarks

Bench harness runs each milestone against fixed projects:

- finance-dashboard (the bug class)
- microblog (current eval)
- ops-dashboard (medium complexity)
- acme-expense (existing baseline)
- one brownfield refactor (Django SAML add)

Metrics: wall time, cost (claude + codex), feature coverage, verdict distribution, regression count post-merge.

T1 must match or beat today's pipeline on all five before T2 ships.

### 6.3 Adversarial / red-team

Each major milestone gets a Codex review pass per existing protocol (`feedback_codex_fixes_own_bugs.md`). Codex finds bugs; Codex fixes. Claude reviews the fix.

### 6.4 Live testing

After M2 ships, dogfood for one week on real projects (not fixtures). Capture verdict distribution across actual user tasks. Adjust retry budgets, lock TTLs, dispatcher heuristics based on telemetry.

---

## 7. Open questions

These don't block M1 but need resolution by their respective milestones.

1. **Subagent budget slicing.** CC SDK Agent tool doesn't natively expose per-call budget. Workaround: Lead checks running cost before each Agent dispatch, refuses if remaining budget < typical sub-call cost. Need real-world numbers to set the threshold. (Resolve by M5.)

2. **Recursive depth and lock semantics.** `--max-depth 3` means a sub-sub-sub-agent can exist. How do its locks interact with great-grandparent's locks? Default: each level locks against its own siblings; cousins (different subtrees) cannot conflict because their owned paths are disjoint by construction. (Resolve before M4 ships.)

3. **Mid-run intent amendment.** User's design preference: in autopilot, defer to follow-up task; in supervised, ask. Need to design the `--mode supervised` checkpoint UX. (Resolve by M5.)

4. **Test agent's resilience to flaky DOM.** If the running product is flaky (race conditions, animations), the test agent's selectors may be brittle. Mitigation: test agent has a bounded retry budget for selector resolution before declaring `unverified`. (Resolve by M5.)

5. **Build/test agent context overlap.** Both need to know what feature is being built. Solution: shared `task_intent.md` + `behavior_journeys.md` in the worktree. Build agent reads + may not write. Test agent reads + may write `behavior_journeys.md` based on observation. (Resolve in M2.)

6. **Provider model defaults under fallback.** When Otto falls back from codex to claude, what model? Sonnet? Need a model-mapping table. (Resolve in M5.)

7. **Cumulative project intent.** Across many tasks, what is "the project's intent"? Auto-accumulated `INTENT.md`? User-maintained? Inferred from first task? (Resolve in M5.)

---

## 8. Risks and mitigations

### 8.1 Lead context overflow on medium-sized tasks

A finance dashboard with 28 features may push 200K tokens. T1 may overflow.

**Mitigation.** Lead has access to `otto.submit_subtask` from M3. If lead detects context pressure, it emits child tasks and exits gracefully. No need to wait for context exhaustion — the Lead can pre-empt.

### 8.2 Best-effort hides real bugs

If everything is best-effort, real bugs get verdicts of `partial` instead of failing loudly.

**Mitigation.** Verdict vocabulary is rich and surfaced prominently. `partial` is not a success — it's an honest state that the user sees. Telemetry tracks verdict distribution; consistent `partial` on a project class is a signal to harden rather than accept.

### 8.3 Locks may stall under crash

Stale locks block writes if cleanup fails.

**Mitigation.** TTL + watcher liveness probe. Default TTL is generous (30 min); on watcher heartbeat, locks reset. If watcher itself crashes, supervisor process clears all locks for dead PIDs on next startup.

### 8.4 Dispatcher heuristics misclassify

Rule-based dispatcher will sometimes pick the wrong tier.

**Mitigation.** `--tier <explicit>` always wins. `--tier ask` opt-in. Telemetry on (intent, dispatched-tier, verdict). Tune rules based on data. Eventually, optionally: an LLM-assisted dispatcher trained on telemetry — but only after rule-based is well-understood.

### 8.5 Migration risk

Existing user projects have today's session shape.

**Mitigation.** Two-release deprecation window. Old sessions resume in legacy mode (T2 default). New sessions adopt new schema. Migration tool optional.

### 8.6 Test agent's "see the running product" needs reliable harness

The test agent depends on `agent-browser` / Playwright / curl / etc. being reliable. Today, these have flaky moments.

**Mitigation.** Existing `otto/browser_testing.py` handles retries, screenshot evidence, harness errors as `unverified` not `fail`. Test agent inherits the same robustness.

---

## 9. What this plan deliberately doesn't promise

- **Determinism.** Same intent twice will not produce bit-identical output. Lead is a stochastic process. Determinism is at the structural level (verdict + which tasks ran), not output level.
- **Strict cost ceilings per task.** Budget is a soft cap. Tasks may marginally exceed before the next Otto check-in.
- **Anti-prompt-injection across tools.** Out of scope for v5.
- **Recovery from ANY agent state.** Some agent crashes are unrecoverable; we mark them and move on.
- **Cross-project knowledge sharing.** Each project is independent. Memory across projects is not in v5.

---

## 10. Definition of done for v5

Otto v5 is shipped when:

1. M1–M5 have all landed.
2. Bench results show T1 matches or beats today's pipeline on the five reference projects.
3. Verdict distribution telemetry shows `pass` rate ≥ today's, `partial` rate accounted for honestly.
4. No regressions in brownfield path (T3 = today's existing brownfield behavior).
5. Documentation: user-facing guide for verdicts and tier selection; internal arch doc for the Lead/locks/queue model.
6. The user can confidently run `otto run "<intent>"` autopilot, walk away, and trust the resulting proof packet.
