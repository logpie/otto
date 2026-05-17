# Pragmatist Review — Otto v5 Plan

**Verdict: 70% of the plan is unnecessary for v5. Ship a 2-week v5; defer the rest to v6.**

The plan reads like a refactor wishlist with the user's two real failures as window-dressing. The user wants: (1) finance-true-web doesn't burn 37 minutes, (2) finance-dash-claude doesn't ship colliding locators, (3) `--tier` flag works. Everything else is speculative. M3–M5 are v6. Even M2 is bigger than the bug warrants.

---

## 1. Smallest valuable thing for #1 pain

The 37-minute waste was caused by `spec_compile.py` emitting a structurally contradictory spec, then `detect_scope_violations` rejecting every attempt 11 times. **One pre-flight glob-containment check** kills that class in <1 second. That's it. Tens of lines.

**M1 as proposed is correct in scope but mis-padded.** The manifest_check is the value. The `--tier` flag plumbing with all-but-one choice raising `NotImplementedError` is dead weight that makes the diff look big and ships no behavior. Plumb `--tier` only when M2 actually consumes it.

**Minimum M1 (this week):** `manifest_check.py` + integrate into `spec_compile.py` post-validate path + structured rejection error + one test fixture (the finance-true-web overlap). Maybe 150 LOC including tests. Done.

---

## 2. Cut list

| Item | Cut? | Reason |
|---|---|---|
| Build-agent / test-agent split (M2) | **KEEP, but slim** | Closes finance-dash-claude self-locator class — user's #2 observed failure. But ship as one new prompt + one extra session call, ~150 LOC. Not a new module hierarchy. |
| `otto.submit_subtask` (M3) | **CUT entirely** | No user project mentioned needs it. Browser-engine speculation. The user runs finance dashboards, not Chromium. Defer to v6. |
| Discovery agent + manifest_check + T2 modular pipeline (M4) | **CUT entirely** | The manifest_check from M1 stays (gates today's pipeline). The discovery agent is speculative — user has zero evidence today's pipeline benefits from a separate read-only architect agent. Defer to v6. |
| Tier presets T0/T1/T2/T3 with separate codepaths | **CUT to flag-only** | Ship `--tier auto\|solo\|lead` as a 3-value knob that toggles knobs on the existing pipeline. Don't build separate codepaths. The user said "knobs must be exposed" — flags are knobs; new architectures are not. |
| 7-verdict vocabulary | **TRIM to 4** | `pass`, `partial`, `unverified`, `catastrophic` cover the real states. `merge_blocked` collapses into `unverified` for v5. `regression_unfixable` is M3+M4 territory — cut. `degraded` is project-state, not task-state — cut for v5. |
| `green \| degraded \| quarantined` project state | **CUT** | No user has hit this. Ship project-state in v6 when telemetry justifies it. |
| Provider fallback (M5) | **PROMOTE to M1.5** | User explicitly bitten by codex out-of-credits. Cannot be M5. Either ship a 30-LOC "if codex returns 402, retry on claude" in week 1, or document explicitly that v5 doesn't fix it. Don't bury at the end. |
| `lead.md` + `discovery.md` + `test-agent.md` + `compile-spec-flat.md` + `manifest-check-feedback.md` (5 prompts) | **TRIM to 2** | New `build.md` (replaces today's, forbids touching tests) + new `test-agent.md`. Discovery and compile-spec-flat are M4-coupled; cut. |
| `otto/state.py` | **CUT for v5** | Verdict can be a string field in `summary.json`. State machine is over-engineering before evidence. |
| `otto/locks.py` (sqlite + TTL + watcher liveness) | **TRIM dramatically** | Locks at write-time are correct; the sqlite + TTL + watcher liveness is overkill for the per-task single-Lead model where there's no fan-out yet. If M3 is cut, locks are protecting against in-session subagents only — a flat in-memory dict in the runner suffices, ~50 LOC. The full sqlite design is M3+ infrastructure. |

---

## 3. Scope-creep risks

- **Recursive subagents at depth 3+** — admitted unverified in proposal A. No user project needs this. **Cut.**
- **Discovery as separate agent with own prompt** — not a new agent; at most a phase of the Lead's prompt. **Cut as separate module.**
- **`manifest_check.py` with full glob containment** — this is real work. Globs (`tests/browser/**` vs `tests/browser/test_transactions.*`) need real path-spec containment, not string compare. Realistic: 100–200 LOC including edge cases (negation, brace expansion, doublestar). Plan says 150. Plausible if scoped tightly to what the failing spec produces; risky if comprehensive. **Keep, scope to "covers the observed bug class"**, not "full POSIX glob algebra."

---

## 4. Order for fastest user value

The plan as written closes failures in roughly the right order, but milestones are too fat:

| Failure | Closed by |
|---|---|
| finance-true-web (37-min waste) | M1 manifest_check (this week) |
| finance-dash-claude (self-locator collision) | Build/test agent split (week 2) |
| codex out-of-credits | Should be week 1, not M5 |

**"One finance dashboard run completing in <20 min on Claude"** = M1 (kills doomed retries) + slim build/test split + bypass T2 machinery. That's a 2-week deliverable, not 6.

---

## 5. Hidden coupling

- **M2 locks without M3 subtasks are nearly useless.** If the Lead doesn't fan out to child tasks, locks only protect against in-session `Agent`/`TeamCreate` subagents — a much smaller surface. The full sqlite-with-TTL lock infra is justified only when M3 ships. Cut M3, slim locks.
- **M4 discovery without M3 subtasks is also coupled** — manifest authorship presumes downstream module-Lead dispatch. If M3 is cut, M4's value collapses.
- **Verdict expansion (M5) without state.py is fine**; verdict expansion *with* project state machine is over-coupled. Decouple.

The plan's milestones are drawn so they each "independently ship" but the value of M2-M5 is actually multiplicative — each one's payoff depends on the next. **Ship M1 + slim M2; freeze the rest.**

---

## 6. The M1 implementation ask — realistic LOC

Plan says: manifest_check.py (150) + tier flag plumbing through cli_run.py + api.ts + tier-decision.json writer.

**Realistic if scoped right:**
- `manifest_check.py`: 120 LOC (including doublestar handling).
- Integration into `spec_compile.py` post-validate: 30 LOC.
- One unit test fixture (the actual finance-true-web spec.json): 50 LOC.
- `--tier` plumbing: skip entirely until M2 needs it. -100 LOC of dead code.

**Total: ~200 LOC, one PR, 2–3 days. Achievable this week.** The plan's M1 is achievable but the tier plumbing is filler.

---

## 7. What if the user ran v5 today?

- **`otto run "build a finance dashboard"`** — With M1 only: the pre-flight catches the spec contradiction, *but* `spec_compile` re-runs and may emit the same contradiction. Need a feedback loop: rejected spec → re-prompt with structured failure → cap at 2 retries → escalate. **This is missing from M1.** Without it, M1 trades 37 minutes of doomed builds for an infinite spec-recompile loop.
- **`otto run "fix typo in README"`** — Today: T2 pipeline. Plan v5: `--tier auto` routes to t0 (single agent). Better. But T0 codepath is M2 work.
- **`otto run "build a browser engine"`** — Plan v5 says M3+M4. **Aspirational.** The plan does not realistically deliver this. If asked, say so honestly: "Otto v5 doesn't claim to build browser engines. Use bare CC."

---

## 8. What to cut to ship in 2 weeks

**Ship in 2 weeks (v5-min):**
- M1 manifest_check + spec re-compile retry loop on rejection (week 1).
- Build/test agent split: one extra session call per task, both reading shared `task_intent.md`. Build agent forbidden from `tests/**`. Test agent runs after build agent commits, writes journeys based on observation. ~200 LOC + 2 prompts (week 2).
- `--tier {auto,solo,lead}` flag wired to the simplest-thing-that-works: `solo` skips spec_compile entirely, `lead` is today's pipeline + manifest_check (week 2).
- Codex 402 → claude fallback: 30 LOC retry wrapper (week 1).

**Cut from v5:** M3 entirely. M4 entirely. M5 except provider fallback. Discovery agent. Subtask emission. Project state machine. T2/T3 preset codepaths. Locks beyond in-memory. 5/7 verdicts.

---

## 9. Clean v6 follow-up

The 2-week v5 doesn't paint into a corner:
- Verdict vocabulary lives in `summary.json` strings — easy to extend in v6.
- Build/test split in v5 is one extra session — adding `submit_subtask` in v6 requires plumbing a queue MCP tool but doesn't refactor v5.
- Manifest_check in v5 is post-hoc; v6 can promote it to discovery-agent-authored without changing the v5 surface.
- Provider fallback in v5 is a wrapper; v6 can extend to per-subagent.

The v5 cuts above are all *additive deferrals*, not *blocking removals*. v6 builds on v5 cleanly.

---

## 10. The radical option (recommended)

**v5-radical: 500 LOC, 1 week, solves 80% of pain.**

1. `otto/manifest_check.py` (120 LOC) — glob containment validator covering the finance-true-web class.
2. `otto/spec_compile.py` integration — call manifest_check post-validate, on failure re-prompt the spec compiler with structured feedback, cap at 2 retries, escalate to user with explicit error if still inconsistent. (~80 LOC)
3. `otto/build.py` — split current single build agent into build-then-test pattern. Build agent's prompt gets one new line: "do not write or modify any file under `tests/`, `*.test.*`, `*.spec.*`." Test agent is a second SDK call after build commits, with a new prompt that reads the running product. (~150 LOC + 2 prompts)
4. `otto/cli_run.py` + provider wrapper — on codex 402/auth failure, fall back to claude with logged warning. (~50 LOC)
5. `--tier` flag accepts `{auto, solo, lead}` only. `solo` = skip spec_compile, single agent. `lead` = today + manifest_check. `auto` = solo if intent < 200 chars else lead. (~50 LOC)
6. Tests: one fixture per change. (~150 LOC)

**Total: ~600 LOC. Ships in a week. Closes both observed user failures, exposes the knob, fixes the credit issue, doesn't paint into corners.**

Everything else in plan-v1 is v6. The plan as written is a 6-week refactor masquerading as a bug fix. Don't ship it. Ship the bug fix.
