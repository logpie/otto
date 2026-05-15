# Autonomous loop — full implementation + e2e + self-healing

> **Self-healing principle.** A halt-on-drift loop is not autonomous —
> it's a tripwire. The loop classifies every drift by severity and
> attempts repair before escalating. It only escalates when repair
> genuinely can't progress, which is the only case where halting is
> more honest than continuing.
>
> The risk in self-healing is the opposite: a loop that "fixes"
> everything by lying. The discipline rules below (no spot fix, no
> overfit, no bandaid, no test deletion, no assertion weakening,
> honest verdicts) become *more* important once self-healing is
> enabled, not less. The loop fixes things while staying honest, or
> escalates.

# Autonomous loop — full implementation + e2e to "works for a real user"

## What this loop does

A single dynamic-mode `/loop` invocation that runs Otto's redesign to
completion **without manual intervention**. Each tick it:

1. Reads `progress.md` to find the current phase + next pending step.
2. Implements the next step (writes code, runs tests, updates docs).
3. Verifies via tests + honest-failure tests + (on phase boundaries)
   real-LLM Bench A.
4. On every Mth tick (M=5), runs a fresh greenfield E2E against one
   of three rotating fixture intents (webapp / cli / library) to
   confirm nothing has regressed at the user-experience level.
5. Updates `progress.md`, writes a tick summary to `loop-report.md`.
6. On any drift detected (vocabulary leak, magic-number leak,
   spot-fix pattern, scope creep, false-positive verdict, slop):
   logs to `drift-log.md`, halts, escalates.
7. On phase complete: triggers Phase Advance Gate, marks phase `[✓]`,
   moves to next phase.
8. On final phase complete + sign-off criteria from research §4.8 met:
   stops the loop and emits a "ready for user" notice.

The loop runs until done. No fixed-interval scheduling — it self-paces
based on whether the previous tick's work is still going (build
running, audit pending, etc).

## What "done" means

The loop terminates when ALL of these are true:

- `progress.md` shows every phase from A0 through C marked `[✓]`
- Sign-off criteria from `research.md §4.8` met:
  - All Phase A green (units + integration + Bench A + RUA on A4/A5)
  - Phase B green (Microfeed parity passes 2 of 3 runs)
  - Phase C complete (legacy code deleted, full test suite green)
  - Grep for retired vocabulary returns zero hits
  - A new user can run `otto run "<intent>"` against an empty
    directory, get a Proof packet they understand, share a per-Feature
    URL with a teammate, and re-audit one Feature against modified
    code — all without reading docs
  - RUA round against three diverse intents finds zero blocking issues

If the loop can't make these conditions true within budget, it halts
honestly and escalates rather than declaring premature victory.

## Self-healing — drift classification and response

| Severity | What | Loop response |
|---|---|---|
| **soft** | Minor, mechanical (vocabulary leak post-A0, missing test for new code, lint nit, missed grep pattern) | Auto-fix in same tick. Log as `info` to `drift-log.md`. Continue. |
| **hard** | Real bug, understood (failing test, scope-leak file, false-positive verdict, magic-number leak, RUA wireframe divergence, single-fixture Bench A failure) | Start a durable repair session with a typed oracle packet, budget, attempt journal, baseline scope, and composite gate. Continue only when oracle state improves and the composite gate passes. |
| **catastrophic** | Budget exhausted, no-progress oracle state, repair-fail loop, plan-vs-impl divergence requiring design-doc change, honest-failure regression, two phases of regression, wall cap | Halt loop. Write full diagnostic. Escalate to user. |

### Durable repair session model

Repairs are not per-symptom playbooks with small retry counts. The loop
creates one repair packet for the failing unit and drives the same agent
session until a typed oracle accepts the result or the budget/no-progress
gate stops it. Deterministic local actions may run first only when they
are provably idempotent, such as clearing an Otto-owned port, but they
hand off to the packet session when the oracle remains red.

| Drift kind | Repair playbook |
|---|---|
| Vocabulary leak (post-A0) | Packet includes grep output, affected paths, and the vocabulary scan command as the oracle. |
| Magic-number leak | Packet includes the scan hit, target defaults file, allowed scope, and the scan command as the oracle. |
| Failing unit/integration test | Packet includes full test command, full artifact paths, current diff/baseline, and the composite scope gate. |
| False-positive verdict | Packet targets the verdict-producing stage and requires a typed re-run of the same E2E/audit oracle before pass. |
| Scope-leak file | Composite gate blocks out-of-scope writes relative to the packet baseline unless the contract explicitly permits the shared edit. |
| Honest-failure regression | Packet preserves the injected failure and requires the expected blocked/partial verdict from the oracle. |
| RUA wireframe divergence | Packet includes screenshots/artifacts as evidence paths; verdict comes from the visual/a11y oracle, not excerpts. |
| Bench A failure | Packet classifies by stage only for routing, then reruns the named benchmark oracle with full artifacts. |

Every repair session:
1. Writes a failing test that reproduces the issue (if not already
   covered).
2. Implements the root-cause fix (NOT the symptom).
3. Runs the packet oracle and records the typed result, digest, artifacts,
   and event journal.
4. Runs the composite gate: clean oracle, dirty/conflict checks,
   baseline-relative scope, verdict consistency, and graph integrity.
5. Continues only if oracle state improves under budget; unchanged
   state writes a structured no-progress escalation.
6. Logs to `drift-log.md` as `auto-resolved` with severity downgrade
   only after the oracle and composite gate pass.

After budget exhaustion or no-progress on the same repair unit, severity
bumps to catastrophic. The loop halts with a full diagnostic.

### Anti-slop guardrails (tick-end self-audit)

These are the bright lines between "self-healing" and "lying to itself."
Any violation → catastrophic; halt; do not commit; do not update progress.md.

- **Never delete a failing test** to make it green. (Test stays; fix code.)
- **Never weaken an assertion** to make it pass. (Assertion stays; fix code.)
- **Never `try/except: pass`** to silence an error. (Error stays; fix root.)
- **Never modify research.md / plan.md / wireframes** to match wrong code.
- **Never add `# pragma: no cover`** or test markers to skip a test.
- **Never special-case a fixture project** in production code.
- **Never edit `otto_logs/` or `bench-results/`** as a "fix."
- **Never declare a phase `[✓]`** when honest-failure injection produces false-positive.
- **Never silently retry outside the repair packet budget.** Escalate honestly.

These are checked on every tick before progress.md is updated.

### Catastrophic-only escalation

The loop halts only when continuing would lie:

- Repair packet budget exhausted on the same hard drift
- Same drift recurs across 3 different repair attempts (going in circles)
- Plan-vs-impl divergence requires research.md or plan.md edits
- Honest-failure regression unfixable in one tick
- Two phases of progress lost in a row (regressing, not advancing)
- Wall cap reached (default 14 days)
- Anti-slop guardrail violated

Even on escalation, the loop writes a full diagnostic to `drift-log.md`:
what was tried, what failed, hypothesis for root cause, candidate fix
paths the user might take. Escalation is a handoff, not a black-box
failure.

## Discipline (non-negotiable per-tick rules)

These rules apply on every tick. The loop self-enforces them.

### No-spot-fix rule

When fixing a bug:
1. Reproduce with a test that fails before the fix.
2. Fix the root cause, not the symptom.
3. Confirm the test passes.
4. Grep for similar patterns elsewhere; fix all instances.
5. Grep again to confirm zero matches.

If steps 1-5 can't all be done, write a `drift-log.md` entry instead
and halt.

### No-overfit rule

Never special-case a fixture project to make a test pass. If the
generalization-breaking patch is the cheapest fix, the loop refuses
it and writes a `drift-log.md` entry instead. Generalization beats
"this E2E project now passes."

### No-bandaid rule

Fixes go in the right module, not in the calling site. No
`if filename == "p7-shortener":` checks. No silent try/except that
swallows errors. No commented-out tests. No `# TODO: fix this later`
without a tracked ticket.

### Honest-verdict rule

Every Run the loop launches must produce honest verdicts:
- A Feature that genuinely fails reports `failed` or `partial`, never
  silent `passed`.
- A Run that hits cost cap or wall cap reports `aborted` with partial
  Proof, never silent `passed`.
- A Component that didn't build reports `blocked`, not omitted.

The loop has a tick-end honest-failure check: at least once per phase,
inject a guaranteed-fail Feature, confirm Run reports `partial`, fix
mode if it doesn't.

### Vocabulary discipline

Loop runs `grep -rE '\b(slice|capability|capability_verdict|certifier|story|stories_passed|stories_tested|acceptance.check|\bAC\b)\b'` on every tick. After Phase A0 completes, any hit halts the loop with critical drift.

(During Phase A0 itself, hits are expected — they're what's being
removed. The loop recognizes A0-active mode and only halts when
hits *increase* tick-over-tick.)

### Magic-number discipline

Loop runs the same numeric-literal scan on every tick. Hits outside
`otto/defaults.py` (and `tests/`) halt with critical drift.

### E2E generalization sweep

Every Mth tick (default M=5), the loop:
1. Picks one of three rotating fixture intents:
   - webapp: "tiny webpage with hello world plus a counter button"
   - cli: "small linter that reports unused imports in a Python file"
   - library: "tiny Python library that wraps requests with retry+timeout"
2. Runs `otto run` end-to-end against an empty fixture directory.
3. Asserts: Proof packet exists, all Features have verdicts, every
   per-Feature page has ≥1 evidence ref.
4. Reads the Proof packet narratively; if the verdicts read like
   nonsense or the evidence is empty, halts with drift entry.
5. Cleans up the fixture dir afterward.

The rotation prevents overfitting to one project type. The loop must
work for *all three* before claiming progress.

### Verification cadence

- **Per tick:** unit tests scoped to current phase, vocabulary scan,
  magic-number scan, scope check vs current phase's expected files.
- **Per phase boundary:** full unit test suite, integration tests,
  honest-failure tests, RUA checklist if MC-touching, Bench A if
  bench-gated.
- **Per Mth tick:** E2E generalization sweep (above).
- **Per cycle (~10 ticks):** full Phase A bench rerun to detect
  regression.

## What the loop does NOT do

- **Does not skip Loop 2 phase advance gates.** Phase progression
  always requires the gate.
- **Does not silence failures.** Every failure goes into
  `drift-log.md` with full evidence.
- **Does not modify research.md, plan.md, wireframes** without
  explicit `drift-log.md` justification (these are user-signed-off
  designs).
- **Does not invent new vocabulary.** Sticks to the unified terms
  in research.md §2.
- **Does not commit to main.** Works only on `cc-i2p-2` branch.
- **Does not skip Codex review** (when credits return).

## Persistent files involved

| File | Role | Loop access |
|---|---|---|
| `research.md` | Design source of truth | read-only |
| `plan.md` | Implementation plan | read-only |
| `docs/otto-wireframes.md` | UI source of truth | read-only |
| `progress.md` | Phase checklist + drift counters + tick log | read-write |
| `drift-log.md` | Append-only drift incidents | append-only |
| `review.md` | Append-only post-phase reviews | append-only |
| `loop-report.md` | Append-only per-tick summary | append-only |
| `loop-config.json` | Loop run-time settings (current phase, M
  for E2E cadence, etc.) | read-write |

## The single prompt

Copy this into `/loop` (no leading interval — dynamic mode). It is
self-contained; the loop re-enters this prompt on each tick.

```
/loop AUTONOMOUS OTTO REDESIGN to completion. Work from research.md, plan.md, docs/otto-wireframes.md as design source of truth. progress.md is the live phase checklist. drift-log.md is append-only drift incidents. review.md is append-only phase reviews. loop-report.md is append-only per-tick summaries.

ON EVERY TICK:

(1) Read progress.md. Identify current phase (first phase not marked [✓]) and the next pending step within it (first item with [ ]).

(2) Implement that step. Use the Edit tool for code changes, Write for new files, Read for inspection. NO new files outside what plan.md specifies for the current phase. NO modifications to research.md / plan.md / wireframes — those are signed off by the user; if a finding requires changing them, write to drift-log.md instead and halt.

(3) Verify the step:
- Run unit tests scoped to the modified files (uv run pytest -q tests/test_<scope>.py).
- Run frontend typecheck if frontend changed (npm run web:typecheck).
- Run vocabulary scan: grep -rE '\b(slice|capability|capability_verdict|certifier|story|stories_passed|stories_tested|acceptance.check|\bAC\b)\b' otto/ tests/ docs/ --exclude=docs/otto-redesign-conversation.md --exclude=drift-log.md --exclude=review.md --exclude=docs/review-walkthrough-*.md --exclude=docs/review-plan.md. After Phase A0 is [✓], any hit = critical drift.
- Run magic-number scan: grep -rE '\b(retries|timeout|max_attempts|budget)\s*=\s*\d+' otto/ --exclude=otto/defaults.py. Any hit outside defaults.py = critical drift.
- Run scope check: git diff main --name-only — every modified file must be in the current phase's expected scope per plan.md. Out-of-scope file = critical drift.

(4) Update progress.md: mark step [✓] with verified-timestamp; update drift counters table; append one-line tick summary to "Tick log" section.

(5) Write a full tick report to loop-report.md (append): phase, step, files changed, tests run, results, evidence paths, any drift findings.

(6) Decide what's next:
- If current phase is now fully complete (all steps [✓]): trigger Phase Advance Gate (see below).
- If E2E generalization sweep is due (every 5th tick by default, tracked in loop-config.json): run sweep before continuing.
- Otherwise: schedule next tick (ScheduleWakeup with delaySeconds=270 for active work, or 1200 if waiting on a long-running build/audit).

PHASE ADVANCE GATE (when current phase fully done):

(a) Run full test suite: uv run pytest -q. Zero failures required.
(b) Run integration tests for this phase: uv run pytest -m integration -q tests/integration/. Zero failures required.
(c) Run honest-failure tests per research §4.6: inject (i) a Feature whose deterministic check is impossible to pass, confirm Run reports verdict=blocked; (ii) a Feature whose audit must fail, confirm Run reports partial after Layer 2 repair; (iii) a Guardrail violation, confirm audit flags it; (iv) a wall-cap breach, confirm Run aborts cleanly with partial Proof. Any false-positive (passed when should be partial/blocked) = critical drift.
(d) If this phase touches MC (A4 / A5): run RUA via chrome-devtools: navigate through screens 1-8 in docs/otto-wireframes.md against ≥3 fixture sessions. Capture screenshots to docs/rua-screenshots/<phase>/. Compare against wireframes. Any wireframe-divergence = drift.
(e) If this phase is bench-gated (A1c / A2 / A3 / B): run Bench A end-to-end against three fixture intents (webapp, cli, library) with OTTO_ALLOW_REAL_COST=1. All three must produce passing verdicts with non-empty per-Feature proof.
(f) Append APPROVED entry to review.md with timestamp + evidence paths.
(g) Mark phase [✓] in progress.md.
(h) Continue to next phase on next tick.

ANY GATE FAILURE: append CRITICAL entry to drift-log.md with failing criterion, evidence path, and root-cause analysis. Do NOT mark phase [✓]. Halt the loop. Escalate.

E2E GENERALIZATION SWEEP (every 5th tick, rotating fixture):

(a) Pick fixture: ticks 5,15,25... = webapp; 10,20,30... = cli; 15,25... = library. (The cadence ensures all three rotate; track the last-used in loop-config.json.)
(b) Create fresh /tmp/otto-loop-e2e-<fixture>-<timestamp>/ directory.
(c) Run otto run "<fixture intent>" against it.
(d) Assert: Proof packet exists, all Features have verdicts, every per-Feature page has ≥1 evidence ref, no blocked Features unless intentional.
(e) Read the Proof packet narratively (parse HTML for verdict + evidence count). If verdicts read as nonsense (zero evidence per Feature; verdict text empty; or "passed" with no walkthrough), halt with drift entry "false-positive verdict on <fixture>".
(f) Append result to loop-report.md.
(g) Clean up the fixture directory.

DISCIPLINE RULES (enforced every tick):

- NO SPOT FIX. When fixing a bug: write failing test first, fix root cause, run grep for similar patterns, fix all instances, grep again to confirm zero matches. Don't fix one occurrence and move on.
- NO OVERFIT. Never special-case a fixture project to make a test pass. If generalization-breaking patch is cheapest: refuse, write drift-log.md entry, halt.
- NO BANDAID. Fixes go in the right module, not in the calling site. No silent try/except. No commented-out tests.
- NO MAGIC NUMBERS outside otto/defaults.py. No exceptions.
- NO MANUAL STEPS. If a step requires the user (e.g. "manually approve spec at gate"): the loop drives the API or CLI to do it programmatically using a fixture spec; never wait for human input.
- NO WORKTREE/BRANCH CHANGES. Stays on cc-i2p-2 branch. Doesn't merge to main.
- VERIFY BEFORE CLAIMING. After every edit, immediately re-read the file and grep for the pattern just changed; confirm the change took effect across all sites.

STOP CONDITIONS:

- All phases A0-C marked [✓] in progress.md AND research §4.8 sign-off criteria met → emit "OTTO REDESIGN COMPLETE — ready for user" and stop the loop.
- Any drift-log.md CRITICAL entry can't be auto-resolved within the tick → halt and escalate (do not call ScheduleWakeup).
- Wall-clock cap: 14 days from first tick (loop-config.json:started_at). After 14 days: halt and emit a status report regardless of completion state.
- User explicitly says "stop" — halt cleanly.

NEXT TICK:

ScheduleWakeup with delaySeconds=270 for actively-progressing work (cache warm), 1200 if waiting on a long-running operation (build agent in flight, audit walkthrough pending). prompt = the entire prompt above prefixed with "/loop ".

ON FIRST TICK ONLY: if loop-config.json doesn't exist, create it with {"started_at": "<iso8601>", "tick_count": 0, "last_e2e_fixture": null, "current_phase": "A0"}. Then run the first tick's logic.
```

## Tweakable parameters

If the user wants to change loop behavior:

- **E2E sweep cadence M** — default 5. Lower = more rigorous, slower.
  Higher = faster, riskier of regression.
- **Tick wall cap** — default 14 days. Tune in `loop-config.json`.
- **Retired-vocab allowlist** — patterns the vocab scan ignores. Edit
  the prompt's `--exclude=` flags.
- **Fixture intents** — three rotating greenfield projects. Edit the
  prompt's "(a) Pick fixture" block.

## When to use this loop

Use it when:
- All design docs are signed off (research.md, plan.md, wireframes).
- All review reports are read and any blocking findings folded in.
- progress.md phase checklists are complete and accurate.
- You want Otto's redesign to ship without further human input.

Don't use it when:
- Designs are still in flux (loop will halt on the first
  research.md modification proposal).
- Cost is a real constraint (loop runs to completion regardless of
  spend).
- You want to manually steer specific implementation decisions.

## Failure-mode self-protection

The loop knows it can drift over many ticks. Mitigations:

- Every tick re-reads research.md / plan.md to refresh design context;
  doesn't trust any cached understanding.
- Every 10 ticks, re-runs the full Phase A bench to catch silent
  regressions.
- `drift-log.md` is monotonically growing; humans can audit it weekly.
- The wall-clock cap (14 days) bounds how long the loop runs.
- Honest-failure tests fire at every phase boundary; if false-positive
  verdicts creep in, they get caught at the next gate.

## Setup checklist before starting

- [ ] All five review reports read by user
- [ ] research.md / plan.md / wireframes signed off
- [ ] progress.md checklists accurate (no `[ ]` marked-`[✓]` items)
- [ ] drift-log.md and review.md initialized
- [ ] No uncommitted user-owned changes in working tree (run
      `git status`; loop will refuse to start with dirty tree)
- [ ] On branch `cc-i2p-2`
- [ ] OTTO_ALLOW_REAL_COST=1 in environment for bench runs
- [ ] User has the time to leave the loop running unattended for
      multi-day stretches
