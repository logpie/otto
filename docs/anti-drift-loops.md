# Anti-drift loops — design and runbook

The Otto redesign is a 4-6 week multi-phase project. Without active
guardrails, slop accumulates faster than humans can notice. This doc
defines two loops — a fast drift sentinel and a phase-advance gate —
plus the persistent files they read and write.

## What "drift" means here

Six failure modes the loops must catch:

1. **Vocabulary leakage.** Retired words (`slice`, `capability`,
   `task`, `certifier`, `story`, `stories_passed/tested`, `AC`) sneak
   back in.
2. **Magic-number leakage.** Numeric defaults inlined outside
   `otto/defaults.py`.
3. **Scope drift.** Phase A1 work touches files that belong to A4 or
   later phases.
4. **Verification skip.** Phase exit criteria forgotten — A2 declared
   done without running audit-tagging coverage test.
5. **Plan-vs-impl divergence.** Code grows a feature `plan.md` never
   described, or omits a step `plan.md` mandated.
6. **Honest-failure regression.** False-positive runs creep back in;
   the system claims `passed` when it should claim `partial`.

## Loop architecture

### Loop 1 — Drift sentinel

**Cadence:** every 60 minutes (when actively working) or on every
commit (via post-commit hook).

**Cost:** zero LLM cost, ~30 seconds wall time. Pure tooling.

**Job:** detect drift, halt and escalate; do not fix.

**Steps each tick:**

1. Read `progress.md` to determine **current phase** (the first phase
   not marked `[✓]`).
2. Run vocabulary scan:
   ```
   grep -rE '\b(slice|capability|capability_verdict|certifier|story|stories_passed|stories_tested|acceptance.check|\bAC\b)\b' \
     otto/ tests/ docs/ \
     --exclude-dir=otto_logs \
     --exclude=docs/otto-redesign-conversation.md \
     --exclude=drift-log.md \
     --exclude=review.md
   ```
   Hits → critical drift; halt and write to `drift-log.md`.
3. Run magic-number scan:
   ```
   grep -rE '\b(retries|timeout|max_attempts|budget)\s*=\s*\d+' \
     otto/ \
     --exclude=otto/defaults.py \
     --exclude-dir=otto/prompts
   ```
   Hits → critical drift; halt and write to `drift-log.md`.
4. Run scope check: `git diff main --name-only` produces the changed
   files. Compare against the current phase's expected file scope (per
   `plan.md`'s "Files" section). Files outside scope → warning drift,
   continue.
5. Run fast unit tests scoped to current phase:
   ```
   uv run pytest -q tests/test_<phase-relevant>.py
   ```
   Failures → critical drift; halt and write to `drift-log.md`.
6. Run frontend typecheck (only if frontend files changed):
   ```
   npm run web:typecheck
   ```
7. Update `progress.md`'s "Drift counters" table with timestamps and
   counts.
8. If no critical drift: emit one-line summary to stdout and continue.
   If critical drift: emit halt notice with link to drift-log.md.

**On critical drift, the loop stops** — does not heal automatically,
because automated fixes for these would themselves be drift. The
human reads `drift-log.md`, fixes, re-runs the loop manually.

### Loop 2 — Phase advance gate

**Cadence:** explicit invocation only. Triggered when implementer
believes "Phase X is done."

**Cost:** real LLM cost (Bench A run); ~60-90 minutes wall.

**Job:** verify phase exit criteria; gate progression to next phase.

**Steps:**

1. Read `progress.md`'s phase exit criteria for the claimed-done phase.
2. Run full unit test suite: `uv run pytest -q`.
3. Run integration tests gated for current phase:
   `uv run pytest -m integration -q tests/integration/`.
4. Run honest-failure tests (research §4.6): inject failures, confirm
   honest verdicts.
5. If phase touches MC: run RUA checklist (manual prompt to human).
6. If phase is bench-gated: run Bench A (greenfield e2e) end-to-end
   with `OTTO_ALLOW_REAL_COST=1`.
7. Compare results against phase exit criteria. All criteria met:
   mark phase `[✓]` in `progress.md`, emit advance proposal. Any
   criterion fails: log to `drift-log.md` with severity=critical.
8. Append review entry to `review.md`.

## Persistent files

Five files. All checked into git.

| File | Purpose | Updated by |
|---|---|---|
| `research.md` | Design source of truth | Human (rarely) |
| `plan.md` | Plan source of truth | Human (rarely) |
| `progress.md` | Live phase checklist + drift counters | Loop 1 + Loop 2 |
| `drift-log.md` | Append-only drift incidents | Loop 1 + Loop 2 |
| `review.md` | Append-only post-phase reviews | Loop 2 + human |

Plus implicit:
- Git history — final ground truth for what changed
- `otto_logs/sessions/<id>/` — runtime session logs (not part of plan)

## Loop 1 — concrete prompt

Use this verbatim with `/loop`:

```
/loop drift sentinel — read progress.md to find current phase. Run vocabulary scan (retired words: slice, capability, capability_verdict, certifier, story, stories_passed, stories_tested, acceptance.check, AC). Run magic-number scan (numeric retry/timeout/budget literals outside otto/defaults.py). Run git-diff scope check against current phase's expected files in plan.md. Run fast unit tests for the current phase. Update progress.md drift counters with timestamps. On any critical drift (retired vocab hit, magic number leak, failing test claimed passing): append entry to drift-log.md, emit halt notice with file paths, stop the loop. On all clean: emit one-line summary, schedule next tick in 60 minutes.
```

This is dynamic-mode `/loop` (no leading interval) — it self-paces
with `ScheduleWakeup` based on whether work is actively ongoing.

A second variant for fixed-interval ticking:

```
/loop 1h drift sentinel — read progress.md to find current phase. Run vocabulary scan (retired words: slice, capability, capability_verdict, certifier, story, stories_passed, stories_tested, acceptance.check, AC). Run magic-number scan (numeric retry/timeout/budget literals outside otto/defaults.py). Run git-diff scope check against current phase's expected files in plan.md. Run fast unit tests for the current phase. Update progress.md drift counters with timestamps. On any critical drift: append entry to drift-log.md, emit halt notice with file paths. On all clean: emit one-line summary.
```

## Loop 2 — concrete prompt (manual invocation)

Not a scheduled loop — invoked by hand when phase claims done. Use:

```
Phase advance gate for phase A<N>. Read progress.md to load phase A<N> exit criteria. Run full unit test suite (uv run pytest -q). Run integration tests for A<N> (uv run pytest -m integration -q tests/integration/). Run honest-failure tests per research §4.6 (inject blocked Feature, inject audit failure, inject Guardrail violation, inject cost-cap breach). If A<N> touches MC: run RUA checklist by driving chrome-devtools through screens 1-8 against fixture sessions; capture screenshots. If A<N> is bench-gated: run Bench A with OTTO_ALLOW_REAL_COST=1 against three fixture intents. Compare results against exit criteria. All pass: mark A<N> [✓] in progress.md with verified-timestamp; append APPROVED review entry to review.md; emit advance proposal for A<N+1>. Any fail: append CRITICAL entry to drift-log.md with failing criterion and evidence; halt; emit blocking notice.
```

## Anti-patterns the loops are designed to prevent

**"Just one quick fix"** — implementer is in Phase A2, finds a bug in
Phase A1 code, "just" fixes it. → Loop 1 scope check catches files
outside A2 scope; flags as warning drift. Implementer must explicitly
ack: "this is a backport to A1, please add to drift-log.md as info."

**"Skipping verification because it's obvious"** — phase claims done
without running honest-failure tests. → Loop 2 always runs them; can't
skip.

**"Reverting to old vocabulary while debugging"** — implementer types
"slice" in code while pattern-matching old patterns. → Loop 1
vocabulary scan catches it within an hour or one commit, whichever
comes first.

**"Magic numbers are fine just for this case"** — `max_attempts=3`
inlined in build.py because "the default is 3 anyway." → Loop 1
magic-number scan catches it. Forces routing through `defaults.py`.

**"Tests pass = phase done"** — phase moved on without RUA or bench. →
Loop 2 requires every gate criterion explicitly.

## Failure mode for the loops themselves

**The loop becomes noise.** If Loop 1 fires too often or with too many
warnings, humans start ignoring it. Mitigations:
- Critical-only halts. Warnings update counters but don't halt.
- Warnings batched (one notice per tick, not one per finding).
- Allowlist for known-temporary drift (e.g. during Phase A0 itself,
  vocabulary is *being* refactored — Loop 1 must understand that).

**The loop becomes wrong.** Drift detector flags a false positive that
isn't actually drift. Mitigations:
- All loop output is logged to `drift-log.md` with severity. Humans
  audit weekly.
- "Resolved by override" is a valid resolution — append rationale to
  the drift-log entry. Future ticks suppress that exact pattern.

**The loop misses real drift.** Mitigations:
- Every Loop 2 phase gate runs full sweep — recovers any drift Loop 1
  missed.
- Quarterly meta-review: read `drift-log.md` end-to-end, check for
  patterns the loops should've caught earlier.

## Setup checklist (before starting Phase A0 work)

- [ ] `progress.md` exists with phase checklists initialized
- [ ] `drift-log.md` exists with empty body
- [ ] `review.md` exists with empty body
- [ ] Loop 1 prompt copy-pasted into a `/loop` invocation in this
      session, OR scheduled via `/schedule` for cloud cadence
- [ ] Decision recorded: session-only loop vs cloud cadence
- [ ] Optional: post-commit hook installed at `.git/hooks/post-commit`
      that triggers Loop 1 immediately on every commit
