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

v3 is simpler than v2 — it removes agents and pipeline stages, not adds them.

```
User prompt → spec gen (independent PM voice, optional)
                  ↓
              spec items (the contract)
                  ↓
              pilot (orchestrator — ordering, parallelism, merge decisions)
                  ↓
              coding agent (strong, autonomous — plans, codes, self-tests)
                  ↓
              verification (existing test suite in clean worktree)
                  ↓
              pilot checks spec compliance on diff
                  ├─ confident → merge
                  ├─ not confident → retry with feedback
                  └─ stuck → escalation report
```

### What's removed from v2 (simpler, not more complex)

| Removed | Why | Savings |
|---------|-----|---------|
| Testgen as mandatory step | Coding agent writes own tests — its strongest self-correction tool | ~200 lines orchestration |
| Tamper detection | Coding agent trusted to fix test bugs, spec is the real contract | 15 lines + tamper-revert bugs |
| "Don't write tests" constraint | Artificially weakened the coding agent | Prompt complexity + workarounds |
| Holistic testgen (required) | Optional at most — coding agent handles its own tests | ~150 lines parallel loop |
| Test diagnosis agent | No adversarial tests = no test bugs to diagnose | ~50 lines |
| Pilot coding directly | Pilot orchestrates, never codes | Role boundary violation removed |
| Single-attempt mode | Agent handles own retries with full context | Pilot micromanagement removed |
| Separate review agent | Pilot does cross-task review + spec compliance — one role, not two | Removes an agent |

### What's kept / refined

**1. Spec gen (independent PM voice)**
- Formalizes user intent without implementation bias
- Extracts hard constraints, preserves them verbatim
- The contract that coding agent and pilot both reference
- Benchmarked: "spec" framing produces 2x faster, better constraint preservation than "rubric"
- Optional for simple tasks, valuable when user prompt is casual/ambiguous

**2. Coding agent (strong, autonomous)**
- Plans before coding ("can current architecture meet all requirements?")
- Writes its own tests to validate approach
- Can fix test bugs (broken imports, wrong stdlib) but not weaken assertions
- Handles retries internally with full context
- Receives spec items directly — optimizes for the spec, uses tests as feedback

**3. Pilot (orchestrator — no coding, no micromanaging)**
- Decides task ordering and parallelism
- Checks spec compliance on diff after coding agent passes
- Cross-task consistency review (no separate review agent needed)
- Escalates when stuck instead of failing silently
- Does NOT use Edit/Write/Bash — only orchestration tools

**4. Verification (simple, not layered)**
- Coding agent's own tests (written during implementation)
- Existing test suite in clean worktree (catches regressions)
- Custom verify command (user-provided, optional)
- Pilot spec compliance check (semantic, on the diff)

**5. Escalation instead of silent failure**
- When confidence is too low after max retries
- Structured report: what was built, what's met, what needs input
- Critical for 24/7 unattended — user wakes up to clarity, not mystery

**6. Testgen (optional, not default)**
- `--tdd` mode: testgen runs first, provides independent cross-check
- Default: coding agent writes own tests as part of implementation
- Both modes: pilot does spec compliance check

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
