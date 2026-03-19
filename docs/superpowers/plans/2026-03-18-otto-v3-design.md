# Otto v3 — Design Vision

## Mission

Reliability harness that makes Claude safe to run, 24/7, unattended, to turn intent into product.

## Core Insight

All verification artifacts (tests, specs, rubrics) are LLM-generated approximations of user intent. None are ground truth. Treating any of them as absolute truth leads to reward hacking (removing features to pass tests) and stuck loops (retrying against broken tests).

The user's words are the closest thing to truth, and even those are ambiguous.

## Design Principles

1. **Spec is a contract, tests are tools.** The spec formalizes user intent. Tests validate implementation. When they conflict, the spec wins.
2. **Tests are evidence, not gates.** Multiple signals contribute to confidence — tests, diff review, spec compliance, agent assessment. No single signal is authoritative.
3. **Each agent is as capable as `claude -p` within its role.** Don't artificially weaken agents then build external systems to compensate.
4. **Roles are orthogonal.** Spec gen formalizes intent. Coding agent plans and implements. Pilot orchestrates and judges. No overlap.
5. **Escalate, don't fail silently.** When confidence is low, produce a clear report instead of marking "failed."

## Current Architecture (v2)

```
User prompt → spec gen → spec items
                              ↓
                    ┌─────────┼──────────┐
                    ↓                    ↓
              testgen agent        coding agent
              (writes tests        (sees spec + code,
               from spec)          plans, implements)
                    ↓                    ↓
              test file ────→ verification (binary pass/fail)
                                         ↓
                                   pilot (orchestrates)
                                         ↓
                                   merge or fail
```

### v2 Problems

- **Tests as hard gates**: one broken test blocks everything. Coding agent burns $2-10 retrying against unfixable test bugs.
- **Testgen has implementation bias**: writes tests that are too aggressive (adversarial framing) or semantically wrong (cached-only when spec says all lookups).
- **Binary verification**: pass/fail with no nuance. A task that meets 6/7 spec items is "failed" the same as one that meets 0/7.
- **Pilot reward-hacks**: when coding agent fails, pilot previously coded directly (removing features to pass tests).
- **No escalation**: tasks either pass or fail. No middle ground of "mostly done, needs human input."
- **Coding agent was artificially weak**: "don't write tests, don't modify tests, don't plan" — then external systems compensated.

## v3 Architecture

```
User prompt → spec gen (independent PM voice)
                  ↓
              spec items (the contract)
                  ↓
              coding agent (strong, autonomous)
                  ↓ plans, codes, self-tests, iterates
              implementation + agent assessment
                  ↓
              verification (multiple signals → confidence score)
                  ├─ mechanical: test suite pass/fail
                  ├─ semantic: pilot spec compliance check
                  ├─ architectural: does the approach make sense?
                  └─ regression: existing tests still pass?
                  ↓
              pilot decision
                  ├─ high confidence → merge
                  ├─ medium confidence → retry with feedback
                  └─ low confidence → escalation report
```

### Key Changes from v2

**1. Coding agent is strong and autonomous**
- Plans before coding ("can current architecture meet all requirements?")
- Writes its own tests to validate approach
- Can fix test bugs (broken imports, wrong stdlib) but not weaken assertions
- Handles retries internally with full context
- Optimizes for the spec, uses tests as feedback
- Reports its own assessment: what it built, what it's confident about, what's uncertain

**2. Tests are weighted evidence, not binary gates**
- Test results are one signal among many
- Pilot weighs: test pass rate, spec compliance, diff quality, agent assessment
- A task with 95% tests passing and clear spec compliance can merge
- A task with 100% tests passing but spec-dodging gets rejected

**3. Testgen becomes optional**
- Default: coding agent writes its own tests as part of implementation
- `--tdd` mode: testgen runs first, provides independent cross-check
- Both modes: pilot does spec compliance check on the diff

**4. Confidence-based decisions instead of binary pass/fail**
- Per spec item: "clearly met" / "approximately met" / "not met" / "unclear"
- Overall: "high confidence" / "medium" / "low"
- High → merge automatically (safe for 24/7 unattended)
- Medium → merge with notes (flag for future review)
- Low → escalation report (needs human input)

**5. Escalation protocol**
- When confidence is too low after max retries
- System produces a structured report:
  - What was built (diff summary)
  - What spec items are met (with evidence)
  - What couldn't be resolved (with explanation)
  - Proposed options (A, B, C)
- This is the difference between "task failed" and "here's where I need help"
- Critical for 24/7 unattended operation — the user wakes up to a clear report, not a mystery failure

**6. Spec gen remains independent**
- Formalizes user intent without implementation bias
- Extracts hard constraints, preserves them verbatim
- The contract that everyone (coding agent, pilot, verification) references
- Does NOT change during implementation (user must amend)

## Progressive Verification

Not every task needs the same level of scrutiny:

| Task Complexity | Verification Level | Cost |
|----------------|-------------------|------|
| Simple (add CLI command) | Agent self-tests + mechanical verification | Low |
| Medium (new feature) | + spec compliance check | Medium |
| Complex (architectural change) | + integration tests + escalation readiness | Higher |
| Visual/GUI | Approximate (can't verify visually yet) | Report-based |

The pilot decides verification level based on task complexity and spec requirements.

## Future: Visual Verification

Some tasks produce visual output (GUI, charts, styling) that can't be verified by tests. v3 should support:
- Screenshot capture of GUI output
- LLM-based visual comparison ("does this look like Apple Weather?")
- Human-in-the-loop for visual sign-off
- Approximate verification: "the tkinter window renders without errors" (testable) vs "the gradient looks good" (visual review)

## Implementation Sequence

1. **Coding agent prompt refinement** — "optimize for spec, tests are feedback"
2. **Confidence scoring in verification** — per-spec-item assessment
3. **Escalation protocol** — structured report when confidence is low
4. **Optional testgen** — `--tdd` flag, default is agent writes own tests
5. **Visual verification** — screenshot + LLM review for GUI tasks
