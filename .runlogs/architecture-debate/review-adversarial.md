# Adversarial review — plan-v1.md

**Verdict: Reject as-is. M1 is shippable; M2–M5 conflate too many independent concerns and have unresolved contradictions. Demand a v2 before any M2 work begins.**

The plan is meaningfully better than `unified-plan.md` (it picks lanes, names files, sequences milestones). But it papers over hard semantics with confident prose. Below: 18 specific issues, each with a fix.

---

## 1. "Best-effort everywhere; advance always" produces silent badness

§0 commit #2 collides head-on with §3.2's `quarantined` state and §1.5's auto-revert. The plan promises the user that Otto never blocks, then specifies five places where it does:

- §1.5: `cap 3` fix-tasks → mark `regression_unfixable` and *continue with other tasks*. So a regression that breaks the auth journey is silently re-classified `degraded` while later tasks land on a broken `main`. The verifier will keep flagging the same bug; subsequent tasks will get fix-tasks for *their* failures piled on top of the unrelated regression. After 5 audits fail (§3.2), the project goes `quarantined`. That's a 4-task latency before the user is even told.
- The user's "how do I know Otto silently degraded vs. advanced honestly?" question has no answer in §3 beyond "verdict is in the proof packet." Proof packets are per-task. Project-state is in `state.py` (M2). There is no "tell me right now if this project is healthy" surface.
- "Critical security work with `partial`": plan-v1 has *no* notion of task-level criticality. A `partial` for "fix CVE in auth middleware" looks identical in the queue to `partial` for "tweak landing page copy."

**Fix.** (a) Add `criticality` to task schema; `partial`/`unverified` on `criticality=critical` is a hard stop, surfaced as a top-level MC banner, not a card status. (b) `regression_unfixable` must trigger a project-state change *immediately* (1, not 5). (c) MC needs a project-health line that *summarizes* `regression_unfixable` count + `partial` count + audits-failing-on-main, refreshed in real time.

---

## 2. Lead is not "one implementation"

§1.2 + §1.9 claim there is one Lead. §4.3 lists `lead.py` at 300 LOC. §1.9 says "Tier is a set of knob defaults." But §1.8 says brownfield Leads "must read the codebase before any decomposition decision" — that is a different system prompt, different tool permissions (Edit on existing files, no greenfield scaffolding), and a different exit contract (PR shape constrained by existing structure). And §1.7 says discovery is invoked at the Lead's discretion only when intent ≥ 4 surfaces — that's a different decision tree.

Three different decision trees + three different prompt configurations dressed as "one Lead" is fragile, not resilient: a single edit to `lead.md` ships across all tiers untested. There is no per-tier prompt regression test in §6.

**Fix.** Either (a) admit there are 3 prompts (`lead-greenfield.md`, `lead-brownfield.md`, `lead-modular.md`) and ship per-prompt golden-output tests, or (b) make `lead.md` use explicit conditional sections gated by tier flags injected as system-prompt fragments, with a unit test asserting exactly which fragments are present per tier. Without one of these, a lead.md edit is a regression risk across all five reference projects.

---

## 3. "Lead picks decomposition mode" with no heuristic

§1.2's table says "Lead picks." No criterion is given. Three Leads with the same intent will pick differently because the table is descriptive ("when context-sharing matters") not operational. The whole bench plan in §6.2 ("T1 must match or beat today's pipeline") becomes statistically meaningless because run-to-run variance in mode choice swamps the signal.

**Fix.** Bake a heuristic into `lead.md` and into a deterministic pre-flight (in `tier_select.py` or new `decomp_select.py`): "if intent has ≥ N independent UI surfaces with disjoint owned_paths → emit subtasks; if subroutines share state → in-session subagents; else inline." Otherwise the plan needs ≥ 5 runs/project and report variance, not point estimates.

---

## 4. Build-test split has a tautology hole

§1.3: "Test agent receives the *running product* + behavior journeys + test suite." If the build agent emits `<a>Foo</a>` when the journey says "click the Foo button," the test agent — observing the product — writes a selector that finds `<a>Foo</a>`. Test passes. Audit (which ALSO derives journeys from intent) catches it only if it sees the same DOM.

The split closes "agent writes brittle locator that collides with itself." It does NOT close "build wrote the wrong product, test agent confirms what build did." The behavior_journey is the only ground-truth anchor — and §1.4 says audit also derives journeys "from accumulated intent." If accumulated intent is a generative re-render of intent.md, the same LLM bias appears on both sides.

**Fix.** Behavior journeys must be authored *before* build, frozen, and consumed read-only by both test agent and audit. Plan-v1 §7.5 ("shared `behavior_journeys.md` ... test agent reads + may write based on observation") is exactly the loophole: test agent rewriting journeys to match observation is the failure mode. Disallow test-agent writes to `behavior_journeys.md`. Period.

---

## 5. `otto.submit_subtask` breaks "main is consistent"

§1.2: Lead emits subtask. §1.5: per-task audit before merge. §3.1: parent task verdicts include `pass` (merged to main) + `partial` (merged to main, missing features).

If parent emits 3 subtasks with `depends_on=[]` and parent itself does inline work, parent finishes and merges to main while children are still building in their own worktrees. Children rebase against a moving main; they may merge minutes or hours later. The proof packet for parent is generated when parent merges — but the child tasks' results aren't in it. The user reads "task done, pass" and the children silently land later (or fail later as `merge_blocked`).

**Fix.** Parent task verdict must wait on transitive subtask closure. Or: subtask emission converts the parent's verdict to `partial+pending_children` until children resolve, with a follow-up proof packet. §1.5 must state: emitted subtasks are part of the parent's task graph for verdict purposes. This isn't optional; otherwise §3.3 ("no failed run lacks a proof packet") is technically true but practically false (the proof packet lies about scope).

---

## 6. Lock TTL = 30 min is wrong for real builds

§1.6 + §8.3: TTL 30 min, "reset on any tool use." A `Bash` running `npm install` or `cargo build` or `pytest` for 20 minutes on a cold build is one tool call. Tool use is registered at *call start*; if the lock TTL is checked at minute 31 from call start, the lock has expired while real work is in progress. Watcher (§1.6) clears the lock; another agent's lock_paths grabs the path; both write; merge collision.

**Fix.** Liveness signal must be process-PID heartbeat (Otto already has this — §1.6 mentions "watcher liveness probe"). TTL should be irrelevant if the PID is alive. State that explicitly: "Lock cleanup is PID-liveness-only; TTL is a backstop for cases where supervising process itself dies. Long-running tool calls are NOT lock-expiring events."

---

## 7. Cross-task merge_queue: sync or async?

§1.5 specifies the rebase loop but not whether the Lead's `task done` returns immediately. §1.7 says discovery is "the Lead's option." If Lead emits child tasks via `otto.submit_subtask(depends_on=[parent_merge])`, what does `parent_merge` mean? Today, `merge_queue.py` is per-session FIFO (1983 lines that the plan rescopes). Per-session means: tasks within one session merge in order. Cross-task means: across sessions, against project main. There is no `parent_merge` token in today's queue.

**Fix.** Define the merge primitives. Either (a) merges are sync from Lead's POV — `task_done` blocks until merged or `merge_blocked`, OR (b) merges are async — `task_done` enqueues a merge-task and Lead returns; subsequent `submit_subtask(depends_on=...)` references the merge-task id. Pick one; specify how the dependency is expressed in `subtask.py`'s API.

---

## 8. Provider fallback is hand-waved

§4.5/§5/§7.6: "fallback codex → claude on credits exhaust." Mid-session migration is impossible — the Claude SDK and Codex have different tool schemas, different message stream formats, different `session_id`s. Otto today resumes by `agent_session_id` (build.py:460,477,3702). If the Lead is mid-session on Codex and credits run out, Otto can't resume that session on Claude.

**Fix.** Document the actual fallback unit: it's *task-level*, not within-session. On credits-exhaust mid-task, the task fails to `catastrophic` (or new `provider_exhausted`); Otto re-dispatches the task fresh against the fallback provider with the original intent + any committed code. Cost accounting needs a per-provider-attempt array, not a flat sum. Today's `summary.json` schema has `cost_usd` flat — needs migration.

---

## 9. `otto.checkpoint(reason)` semantics differ by mode

§1.2: "Non-blocking in autopilot." §2.1: `--review-gate` exists only in supervised. So the same API (`otto.checkpoint`) is a no-op in autopilot and a blocking await in supervised. A Lead author cannot test in autopilot and trust the same prompt won't deadlock in supervised. There's also no timeout: if user is asleep and supervised mode is on, the Lead hangs forever.

**Fix.** Either (a) split the API: `otto.checkpoint(reason)` non-blocking always, `otto.request_review(reason, timeout)` blocking with a defined timeout that defaults to "abort-and-write-checkpoint" on expire; or (b) make `--review-gate` a runtime toggle the Lead reads, and document it in `lead.md` so the Lead actively branches behavior. Option (a) is cleaner.

---

## 10. M1 ships `--tier` that raises NotImplementedError

§5.M1: "All other choices currently raise `NotImplementedError`." Web client (§2.2) gets dropdown values that raise on submit. If a user picks `t1` post-M1, the run aborts. That's a UX regression for a milestone that "preserves all other behavior unchanged."

**Fix.** M1 ships `auto` and `t2` only. Other choices either disabled in the dropdown with a tooltip ("ships in M2") or fall back to `t2` with a warning logged. Don't ship error-on-submit code paths.

---

## 11. Migration: post-M2 deletion vs. legacy resume

§4.5 promises "two-release deprecation window." §4.1 deletes 1200 lines of `spec_compile.py`/`build.py` group orchestration in M2. §4.5 says "old sessions resume in legacy mode (T2 default)" — but legacy mode IS the deleted code. You cannot have both.

**Fix.** Either (a) gate the deletion behind one full release: M2 ships new code, M3 deletes old code, M4 onwards no legacy. Or (b) preserve the deleted code as `otto/legacy/` and route resume-of-old-checkpoint through it. Plan-v1 implies (b) but doesn't say so. Specify.

---

## 12. M2 vs. M3 contradiction on test-agent split

`unified-plan.md` §7 puts test-agent in M3. Plan-v1 §5.M2 puts test-agent in M2 (`test_agent.py`, `test-agent.md`, build prompt forbids `tests/**`). M3 is now "Lead-emitted subtasks + queue extension." This is fine technically but the plan shouldn't claim "synthesizes prior conversation" while silently moving the most-debated piece into the most-loaded milestone.

**Fix.** Acknowledge the move in §5 with a one-line rationale ("split moved to M2 because lead.md needs to know about it"). Don't smuggle scope changes.

---

## 13. Audit's `behavior_journeys` source is undefined post-spec_compile-deletion

`otto/audit.py:1956` consumes `spec.behavior_journeys`. `Spec` comes from `otto/spec_compile.py:64`. §4.1 deletes group/contract synthesis but the plan is silent on the `Spec` class itself. T1 produces "flat spec" per `compile-spec-flat.md` (§4.3), but discovery agent (M4) is what adds journeys in T2. T0/T1 — where do journeys come from?

**Fix.** Either (a) flat spec still includes `behavior_journeys` (from a renamed compile-spec-flat that drops groups but keeps features+journeys); state this. Or (b) Lead authors them at runtime — but then audit's read-only journey ground truth breaks (issue #4). Pin it down.

---

## 14. `quarantined` IS a hard block

§3.2: "5 consecutive audit failures: pause new dispatches." This contradicts §0.2 ("hard blocks only on catastrophic infra OR explicit user opt-in"). Quarantined isn't catastrophic infra — it's task quality.

**Fix.** Either drop `quarantined` and let work continue (consistent with philosophy, lets damage compound), or admit philosophy #2 has a quality safety valve and document it as a fourth state-change reason (catastrophic infra | user opt-in | quality-safety-valve | regression-loop). Don't pretend it's not a hard block.

---

## 15. Concurrent users on a project is unverified

§7 OQ#7 (in unified, also in v1 by reference): "today's queue lock applies." `otto/queue/runtime.py` (275 lines) — I checked: it has worktree-readiness markers, INTERRUPTED/RUNNING statuses, and `OTTO_QUEUE_PROJECT_DIR` anchoring, but no multi-submitter locking. `enqueue.py` writes to a shared file; concurrent enqueues will race. The plan inherits this and adds Lead-emitted subtask submissions on top, which doubles concurrent-write surface.

**Fix.** Add a sqlite or fcntl lock around queue mutations in `otto/queue/enqueue.py` and `otto/queue/subtask.py` BEFORE M3 ships. Bench: spawn 4 simultaneous `otto run` against the same project and verify queue integrity.

---

## 16. Malware-reminder injection on Read for non-Opus-4.6 — silent

Plan-v1 doesn't mention this. Build-test split DOUBLES the affected sessions: build agent reads project files (each Read tagged with system-reminder), test agent reads test fixtures + browser DOM (each Read tagged). For non-Opus-4.6 (e.g., codex-app-server in M5 fallback path), every read carries a malware nag that pollutes context.

**Fix.** Document in §6/§7 as a known hazard. Pin Lead and test-agent providers to a model that doesn't have this behavior unless `--allow-reminder-pollution` is set. M5's fallback table needs to specify which models are reminder-clean.

---

## 17. Cost realism is hand-waved

Today's finance-dashboard fail = $2.41. Plan-v1 §6.2 promises T1 is "competitive" but never publishes a cost model. Per task in v5: 1 build agent + 1 test agent + 1 verifier + (post-merge) 1 audit + (on regression) 1 fix-task with its own build+test+verifier+audit. Worst case: 4× more LLM calls per task than today. With 28 features in finance-dashboard, even with auto-decomposition into 7 subtasks, that's 28 LLM-session-equivalents.

The plan's claim of "comparable cost" needs receipts. Either (a) ship a cost model in §6 with assumptions stated and per-tier estimates, or (b) drop the comparison and let bench data answer it post-M2. Don't promise "competitive" without arithmetic.

---

## 18. "T1 beats today" is a trivially-met bar

§10.2: "Bench results show T1 matches or beats today's pipeline on the five reference projects." Today's pipeline FAILS finance-true-web (the seed of this whole replanning). T1 not failing = T1 beats. That's a vacuous bar.

**Fix.** Bar must be (a) T1 produces a working finance-dashboard product (proof: behavior journeys pass, screenshots match, manual eval), (b) T1 cost ≤ 2× today's cost on PASSING projects (microblog, ops-dashboard), (c) T1 wall-time ≤ 1.5× today's on PASSING projects, (d) zero `regression_unfixable` on the brownfield refactor. Anything weaker rewards "fail in a new way" as success.

---

## Prioritized punch list

Required before any M2 implementation work begins:

1. **Resolve issues #1, #4, #5** — best-effort safety, journey ground-truth, parent-child verdict semantics. These are correctness issues; everything else is plumbing.
2. **Resolve issue #11** — migration path for legacy checkpoints. This is binary: either the legacy code stays for one release or it doesn't. Plan must pick.
3. **Resolve issue #14** — reconcile `quarantined` with philosophy #2.
4. **Specify issues #3, #6, #7, #9** — concrete heuristics + APIs + semantics. Without these, M2 implementation will guess and bench results will be uninterpretable.

Acceptable to defer to the milestone where they bite:

5. Issues #2, #8, #15, #16, #17 — refine in their respective milestones.
6. Issue #10 — fix in M1 PR review.
7. Issues #12, #13, #18 — text edits to plan-v1 itself; not implementation work.

---

**Bottom line.** Plan-v1 is the right shape. It has more guts than unified-plan.md and more ship discipline than proposal-A. But "best-effort everywhere; advance always" is currently a slogan that masks five hard-block exceptions and three silent-degradation paths, and the Lead/subtask/merge interaction is genuinely undefined. M1 (manifest pre-flight + tier flag plumbing) is shippable as written. M2 must not start until the punch list above is resolved in a plan-v2.
