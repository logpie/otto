# Otto v5 — Implementation Plan, Draft 2

Synthesized from plan-v1 + three reviewer reports (implementation feasibility, adversarial critic, pragmatist trim). The reviewers converged on three messages:

1. **The pragmatist is right about scope.** Plan-v1 is a 6-week refactor with the bug fix smuggled in. The user's two real failures (finance-true-web, finance-dash-claude) close in ~600 LOC over 1-2 weeks. Cut M3, M4, most of M5.
2. **The adversarial critic is right about correctness.** Even the slim version has hard correctness traps: behavior-journey tautology, parent-child verdict consistency, "best-effort everywhere" masking silent degradation. Resolve these before any code lands.
3. **The implementation reviewer is right about wire-protocol.** The plan ducked SDK hook collisions, MCP exposure, runner integration, and the deletion cascade. These shrink dramatically once the scope is cut.

This plan-v2 commits to the pragmatist's radical option as v5, addresses the adversarial's correctness issues within it, and resolves the implementation gaps.

---

## 0. North star (unchanged from v1)

Otto converts intent into a stream of well-described product states. The user reads the stream when convenient, intervenes when they want, is never required to.

Three philosophy commitments:

1. **Autonomy is the default.** Supervised mode is a rare opt-in.
2. **Best-effort everywhere; advance always.** No hard blocks outside genuine catastrophic infra or explicit user opt-in.
3. **The agent decides decomposition; Otto provides the rails.** Otto is workspace + tools + verifier + queue + merge.

What changes from v1: we honor philosophy #2 by REMOVING the silent-degradation paths the critic flagged (no `quarantined` state, no `regression_unfixable`, no project state machine in v5). The system is honestly best-effort; if the work isn't getting done, the user sees it in MC, not after Otto's auto-revert / auto-quarantine machinery has rearranged the project.

---

## 1. v5 scope: what we ship, what we don't

### What ships in v5 (1-2 weeks, ~600 LOC)

| Capability | Ships? | LOC | Closes |
|---|---|---|---|
| Manifest pre-flight check (glob-containment) on compiled spec | YES | ~150 | finance-true-web 37-min waste |
| Spec recompile retry loop on rejection (cap 2, then escalate) | YES | ~80 | "infinite recompile loop" risk on rejection |
| Build/test agent split with FROZEN behavior journeys | YES | ~200 | finance-dash-claude self-locator collision |
| Provider fallback (codex 402/auth → claude task-level re-dispatch) | YES | ~50 | codex out-of-credits user pain |
| `--tier {auto,solo,lead}` flag (3 values, no separate codepaths) | YES | ~80 | "user can specify decomposition" |
| Verdict vocabulary: `pass | partial | unverified | catastrophic` | YES | ~50 | honest reporting |

### What does NOT ship in v5 (deferred to v6+)

| Thing | Why deferred |
|---|---|
| `otto.submit_subtask` (Lead emits child tasks to queue) | No user project mentioned needs it. Browser-engine speculation. |
| Discovery agent + ARCHITECTURE.md authorship | Speculative; no observed need. |
| T2 "modular" pipeline as separate codepath | Subsumed by `--tier lead` + manifest check. |
| Cross-task merge daemon | Not in observed failures. Today's per-session merge_queue still works. |
| Sqlite/TTL/watcher locks | Without subagent fan-out, in-process tracking is enough; full lock infra is M3+ and we cut M3. |
| Project state machine (`green | degraded | quarantined`) | Adversarial #14: `quarantined` IS a hard block; contradicts philosophy. Drop the whole state machine; show health in MC instead. |
| `merge_blocked`, `regression_unfixable`, `degraded` verdicts | All three encode "Otto silently keeps going past failures." Honesty over silent advance: these get rolled into `partial` + a clear note in the proof packet. v6 can refine. |
| Recursive subagents at depth ≥ 3 | No user project needs this. |
| `otto/state.py` | Verdict is a string field on summary.json — no state machine for v5. |
| `otto.checkpoint(reason)` blocking semantics for supervised | Supervised mode is rare opt-in. v5 ships supervised as "logs more"; blocking checkpoints come later if needed. |

### What's honestly out of scope for v5

- Browser-scale projects (millions of LOC). v5 targets up to finance-dashboard complexity.
- Mid-run intent amendment.
- Multi-user concurrent submitters on the same project (defer until queue locking is hardened).
- Cross-provider subagent budget slicing.
- Project-level cumulative INTENT.md.

---

## 2. Architecture, v5

### 2.1 The pipeline

```
intent ──► spec_compile (FLAT — features + behavior_journeys, no groups)
              │
              ▼
        manifest_check  (mechanical: glob containment)
              │     ├── reject → re-prompt spec compiler with structured error (cap 2 retries)
              │     └── 2 rejections in a row → escalate, surface to user
              ▼
         BUILD AGENT  (one Claude session)
            tools: Read, Write, Edit, Bash, Glob, Grep, TodoWrite, Agent/TeamCreate
            prompt: "you write app code. NEVER touch tests/, *.test.*, *.spec.*, browser journeys."
            commits when done
              │
              ▼
         TEST AGENT  (separate Claude session)
            tools: Read, Write (only tests/), Bash, browser harness
            prompt: "you write tests. read the FROZEN behavior_journeys.md. observe the running product.
                     write selectors against actual DOM. NEVER modify behavior_journeys.md.
                     NEVER modify app code."
            commits tests
              │
              ▼
         AUDIT (existing otto/audit.py — unchanged)
            reads FROZEN behavior_journeys.md
            runs the test suite + browser journeys
            verdict: pass | partial | unverified | catastrophic
              │
              ▼
         RENDER (proof-packet.html + .json)
```

### 2.2 What changes vs. today's pipeline

- `spec_compile.py` group/contract synthesis is GATED behind `--tier lead-modular` (which we don't ship in v5; auto and lead and solo all use flat spec). No deletion in v5; just a runtime branch.
- `build.py`'s scope checking and merge_queue stay running. The manifest_check from M1 catches contradictions BEFORE the build phase, so scope checking is the backstop, not the primary defense.
- `audit.py` is unchanged.
- `merge_queue.py` is unchanged (still per-session FIFO; cross-task merging is v6).

### 2.3 Tier flag mapping (no separate codepaths)

| Flag | Behavior |
|---|---|
| `--tier auto` (default) | Heuristic: intent < 200 chars + no list markers → solo; else → lead. |
| `--tier solo` | `spec_compile` skipped entirely. Single agent gets the raw intent + project_dir, builds, commits. Audit runs against intent-derived journeys. (Trivial scripts.) |
| `--tier lead` | `spec_compile` runs in flat mode (features + journeys, no groups). manifest_check passes vacuously (no contracts). Build-then-test pattern as in §2.1. (Default for normal projects.) |

There is no `t0/t1/t2/t3` user-facing vocabulary. Three values, no preset bundles, no separate codepaths.

### 2.4 Frozen behavior journeys (closes the tautology)

Critical correctness fix from adversarial review #4. Behavior journeys are:

1. **Authored ONCE during `spec_compile`** as part of the flat spec.
2. **Written to `behavior_journeys.md`** in the session dir.
3. **Read-only for build agent and test agent.** Both prompts explicitly forbid modifying this file.
4. **The single ground truth** for what the product must do. Audit reads the same file.

The test agent's job is to write **selectors that find DOM elements satisfying the journey steps**, not to rewrite journeys to match what the build agent produced. If the build agent emits a button labeled "Submit" but the journey says "click 'Save'," the test agent's selector won't find anything → test fails → audit fails. The wrong product is caught.

This requires `compile-spec-flat.md` to author behavior journeys robustly enough that they don't need post-hoc adjustment. The prompt is explicitly required to express journeys in user-language ("click the Save button") not implementation-language ("click the element with id=save-btn"). Implementation-flexible.

### 2.5 Provider fallback (task-level re-dispatch, not mid-session)

Adversarial #8: mid-session migration is impossible. The Claude SDK and Codex have different schemas. Fallback is task-level:

- On codex returning 402 / auth-failed: the task fails to verdict `catastrophic` with `failure_reason=provider_exhausted`.
- Otto's queue runtime detects this verdict + reason, re-enqueues the same task with `--provider claude`, references the original task id as `recovered_from`.
- Cost accounting becomes a per-attempt list in summary.json: `cost_attempts: [{"provider": "codex", "cost_usd": 0.13, "outcome": "exhausted"}, {"provider": "claude", "cost_usd": 2.10, "outcome": "pass"}]`. Total is sum.
- Already-committed code (from the codex attempt's worktree, if any) is preserved and the new attempt may re-use it.

Default fallback is configured in `otto.yaml`:

```yaml
defaults:
  preferred_provider: codex-app-server
  fallback_provider: claude
  fallback_on: ["provider_exhausted", "auth_failed"]
```

If fallback also fails, verdict stays `catastrophic`. No infinite loop.

### 2.6 Verdict vocabulary (4 values, in summary.json field)

| Verdict | Meaning | What user does |
|---|---|---|
| `pass` | All checks green. Merged to `main`. | Nothing. |
| `partial` | Built and merged. Some declared features did not pass within retry budget. Honest list in proof packet. | Review proof packet. May file follow-ups. |
| `unverified` | Built and committed; verifier itself failed/timed-out. Code unverified. | Re-run audit; or accept; or file follow-up to harden tests. |
| `catastrophic` | Infrastructure failure (provider auth/credits, disk, etc.). Not Otto's fault. | Fix infra; resume. |

Verdicts are strings on `summary.json` and `history.jsonl`. No state machine. No project-level state in v5. MC renders the verdict as a colored pill; CSS additions are listed in §4.4.

### 2.7 What "best-effort everywhere; advance always" actually means in v5

After the cuts above, the philosophy is not undermined by silent-degradation paths:

- Within a task, if an agent fails (build, test, or audit), the verdict is `partial` or `unverified`. Code lands or is preserved as appropriate. Proof packet is rendered. Task ends. No retries beyond the existing per-attempt budget.
- Across tasks, today's queue accepts more tasks regardless of prior verdicts. No "quarantine" — the queue keeps running. If 5 tasks in a row land `partial`, the user sees that in MC; Otto does not auto-pause.
- The only hard blocks: (a) `catastrophic` verdict + no fallback configured, (b) explicit `--mode supervised` checkpoints (rare).

The user sees an honest stream of verdicts. They decide when to intervene.

### 2.8 What remains "broken" honestly in v5

- Long-running tasks (e.g., user submits "build a finance dashboard with 28 features in one go") may still run a single Build Agent + Test Agent for ~15-20 minutes. v5 doesn't fan out into parallel subtasks. If the lead agent's context overflows, the task ends with `partial`. The user can break work into smaller tasks.
- A `partial` proof packet's surface area depends on Otto's introspection: the audit lists which behavior journeys passed/failed, but if the journey list itself is wrong (compile-spec-flat got it wrong), the gap is invisible. v5 trusts the spec compiler to author honest journeys; v6 can add a journey-review pass.
- Cross-task merge conflicts (rare in v5 because no parallel task fan-out) fall through to today's merge_queue retry logic. Unchanged.

---

## 3. Implementation, week by week

### Week 1 — Bug fixes (~350 LOC)

**Goal:** finance-true-web class is dead. codex out-of-credits has a working fallback.

1. `otto/manifest_check.py` (~120 LOC). Glob-containment validator. Exposes one function: `validate_manifest(spec: Spec) -> list[ManifestError]`. ManifestError has `(contract_id, owner_id, conflicting_group_id, conflicting_path)`. Uses `pathspec` (already a dep) for glob matching.
2. `otto/spec_compile.py` integration (~80 LOC). After existing validation, run manifest_check. On non-empty error list:
   - Log structured failure.
   - Re-prompt spec compiler agent with the failure (`compile-spec.md` gets a new "previous attempt was rejected" suffix block).
   - Cap at 2 retries. After 2, write the failures to `manifest-rejection.json` and surface as `catastrophic` with `failure_reason=spec_compile_unsound`.
3. `otto/cli_run.py` provider fallback wrapper (~50 LOC). Catches provider-exhausted/auth-failed errors at task-result level, re-dispatches with fallback provider, accumulates `cost_attempts[]`.
4. `--tier {auto,solo,lead}` flag wired (~80 LOC). `auto` heuristic in `tier_select.py` (~40 LOC). cli_run.py + RunPayload field. Solo branch: skip spec_compile, single-shot agent. Lead branch: today's pipeline + manifest_check.
5. Tests: one fixture per change. (~100 LOC)

Deliverable: M1 PR, dispatched to Codex per `feedback_codex_fixes_own_bugs.md`. Reviewed by Claude.

### Week 2 — Build/test agent split (~250 LOC)

**Goal:** finance-dash-claude class is dead. Behavior journeys are frozen ground truth.

1. `otto/prompts/build.md` rewrite (~30 LOC of content changes). Forbid touching `tests/**`, `*.test.*`, `*.spec.*`. Move test-writing instructions to test-agent prompt.
2. `otto/prompts/test-agent.md` new (~60 LOC of content). Test agent prompt: read `behavior_journeys.md`, observe running product (browser harness or CLI invocation depending on project_kind), write selectors against actual DOM, write/extend test files. NEVER modify journeys or app code.
3. `otto/prompts/compile-spec-flat.md` (renamed from existing flat path). Explicitly require behavior_journeys to be in user-language, not implementation-language. (~40 LOC of content changes.)
4. `otto/test_agent.py` runner (~80 LOC). After build agent commits, before audit: spawn test-agent session, point at the running product, wait for return. If test agent fails → verdict `unverified`.
5. `otto/runner.py` integration. Inject test-agent phase between build and merge. (~30 LOC.)
6. `otto/audit.py` unchanged: still reads `behavior_journeys.md` (now from session dir).
7. Verdict vocabulary expansion: add `unverified` and `catastrophic` to `AuditVerdict` enum (otto/audit.py:102). (~10 LOC.)
8. Tests: integration test for build/test split with a fixture project. (~80 LOC)

Deliverable: M2 PR. Bench against finance-dash-claude scenario.

### Week 3 — Bench, polish, docs (no new features)

1. Run all five reference projects (finance-dashboard, microblog, ops-dashboard, acme-expense, brownfield SAML) with v5.
2. Compare cost, wall time, verdict distribution, feature coverage to today's pipeline. Bench script committed to repo.
3. User-facing docs: how `--tier` works, what each verdict means, when supervised mode helps.
4. Internal arch doc: how build/test split works, where to extend in v6.

Deliverable: bench report; docs landed; v5 announced.

---

## 4. Migration & deletion

### 4.1 Nothing is deleted in v5

All of today's `spec_compile.py`, `build.py`, `merge_queue.py`, and `audit.py` stay. The new code paths layer on top:

- `manifest_check.py` is a NEW post-validate hook, not a replacement.
- Build/test split modifies existing `build.md` content + adds `test_agent.py` + new prompt.
- `--tier solo` is a NEW codepath that bypasses spec_compile; `lead` uses today's pipeline.

No `_normalize_critical_shared_contract_scope` deletion, no `detect_critical_shared_contract_violations` deletion, no merge_queue rescope. v6 can remove what's no longer used.

### 4.2 Resume compatibility

Existing checkpoints lack `tier` field. `cli_run.py:resume` defaults missing `tier` to `lead` (= today's behavior). Existing summary.json without `cost_attempts[]` reads as a single-attempt run. No legacy mode branching needed.

### 4.3 Test cascade

Since nothing is deleted, no test files break. New tests are added for the new code paths.

### 4.4 MC rendering

Verdict vocabulary is 4 values: `pass | partial | unverified | catastrophic`. MC's existing pill renderer needs:
- New CSS class for `unverified` (yellow) and `catastrophic` (purple/dark).
- New badge text.
- One file: `otto/web/client/src/components/run/VerdictPill.tsx` — add 2 lines per verdict. (~10 LOC.)

Existing project-level views unchanged (no project state machine). MC dashboard already shows recent verdicts; user sees the stream.

---

## 5. Open questions & their resolution

These are honestly resolved in v5:

| Question | Resolution |
|---|---|
| Behavior journey ground truth | Frozen at compile time; build + test agents read-only. (§2.4) |
| Mid-run intent amendment | Out of scope for v5. User submits a new task. |
| Cross-task merge daemon | Not built. Today's per-session merge stays. |
| Lock semantics under crash | No new lock infra; today's worktree-readiness markers suffice. |
| Subagent budget slicing | No subagent fan-out in v5; not needed. |
| Test agent flake handling | Bounded internal retries (3) + verdict `unverified` on exhaustion. (~10 LOC in test_agent.py) |
| Recursive depth | Not applicable in v5 (no recursive subagents). |
| Provider fallback model defaults | `claude` falls back to `sonnet` (already the default per `feedback_sdk_system_prompt.md`). |
| Cumulative project intent | Not built. v5 treats each task independently. |

These are deferred openly:

| Question | Owned by |
|---|---|
| When/how to ship `otto.submit_subtask` | v6 design exercise, post-v5 telemetry |
| Project-level coherence beyond per-task audit | v6 |
| Concurrent multi-user submitter locking | v6, with proper bench |

---

## 6. Risks and mitigations

### 6.1 Spec compiler can't author honest behavior journeys

Plan-v2 trusts `compile-spec-flat.md` to write user-language journeys. If the compiler authors implementation-language journeys ("click element with class .save-btn"), the test agent's selectors will be coupled to compiler-emitted DOM expectations rather than user-visible behavior — and we're back to self-attestation.

**Mitigation:** week 2's compile-spec-flat.md rewrite has a hard rule: journeys MUST be in user-language. A unit test on the rewritten prompt validates output against a regex / lint check — no journey may include `class=`, `id=`, `data-testid=`, `getByRole('xxx', {})` syntax. Spec compiler retries if journey fails the lint.

### 6.2 Test agent context overload reading the running product

If the running product is a 30-route SPA, the test agent reading the DOM may exceed context. Mitigation: test agent prompt is explicit about scoping to ONE journey at a time, write tests incrementally, commit after each.

### 6.3 Manifest_check too strict

Glob containment may flag legitimate overlaps (e.g., shared package.json). Mitigation: `allowed_extension_paths` mechanism already exists in spec; recursive use is the escape hatch. manifest_check defaults are tuned against today's broken finance-true-web spec — it MUST reject that spec; loosening enough to let it through is a regression.

### 6.4 Provider fallback cost surprise

A user expecting codex bills suddenly sees claude charges. Mitigation: `cost_attempts[]` makes the fallback explicit in summary.json; MC renders both providers' costs; user sees the swap immediately.

### 6.5 The "v5 doesn't solve browser-engine" honesty

User explicitly mentioned IDE/browser as max-complexity examples. v5 explicitly doesn't deliver these. Mitigation: documentation states scope honestly. v6 design will tackle large-scale projects with a proper plan informed by v5 telemetry.

---

## 7. Definition of done for v5

1. Both observed user failures are closed:
   - finance-true-web pattern: rejected by manifest_check pre-flight in <1s. No 36 doomed agent attempts.
   - finance-dash-claude pattern: build agent cannot author the journey that catches itself. Test agent is independent. Strict-mode locator collisions become normal test failures with normal feedback paths.
2. codex out-of-credits → claude task-level fallback works on a real (instrumented) failing run.
3. `--tier {auto,solo,lead}` is in the CLI and MC, defaults to `auto`, all 3 values produce a valid run.
4. Verdict vocabulary `pass | partial | unverified | catastrophic` is in summary.json and rendered correctly in MC.
5. Bench on the 5 reference projects: T1 (`--tier lead`) cost ≤ today's, wall time ≤ 1.2× today's, finance-dashboard PASSES (real user-facing test passes, not just "doesn't fail like today").
6. Documentation: 1-page user guide for tiers + verdicts.
7. v6 punch list documented (subtasks, discovery, cross-task merge, etc.) with rationale for deferral.

This is a 2-3 week deliverable. ~600 LOC. No 6-week refactor.

---

## 8. What we explicitly DON'T promise in v5

- Browser-scale project support.
- Determinism across runs (same intent → same output).
- Strict cost ceilings per task (budget remains a soft cap).
- Zero regressions on `main` (best-effort everywhere).
- Recovery from arbitrary agent crash states.
- Multi-user concurrent submitters on the same project.
- Cross-project knowledge sharing.
- Mid-run intent amendment.

These are honest exclusions, surfaced in user-facing docs.

---

## 9. The one-paragraph version

Otto v5 is a 2-3 week, ~600 LOC delta on top of today's pipeline. It adds: (1) a spec-compile-time pre-flight that mechanically rejects contradictory specs, killing the finance-true-web "36 doomed attempts" failure class; (2) a build/test agent split with frozen behavior journeys, killing the finance-dash-claude self-locator-collision class; (3) a `--tier` flag with three values exposing decomposition control to the user; (4) provider-level fallback so codex out-of-credits doesn't strand a task; (5) honest 4-value verdict vocabulary. v5 does NOT deliver: dynamic decomposition, cross-task merge, browser-scale projects, project state machines, recursive subagents. Those are v6, after v5 telemetry tells us what's actually needed.
