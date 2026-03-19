# Verifiable Specs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Spec items are classified as verifiable or non-verifiable. Verifiable specs get mandatory automated tests that prove compliance. Non-verifiable specs get best-effort visual/behavioral validation. Otto can browse apps, take screenshots, and verify from a real user's perspective.

**Architecture:** Spec gen classifies each item. Coding agent writes verification tests for verifiable items. A new verification tier (Tier 4: behavioral) runs browser automation for web apps. Pilot uses screenshots + test results for compliance checks instead of just reading diffs.

**Tech Stack:** Playwright (browser automation), Agent SDK (existing), MCP chrome-devtools (already available in user's toolset)

---

## Context

### The Problem

Otto currently has two verification gaps:

1. **Verifiable specs aren't enforced with tests.** The coding agent is told "meet the spec" but there's no mechanism to ensure a test exists for each verifiable spec item. The agent can declare "<300ms achieved" by only testing cache hits.

2. **No visual/behavioral verification.** For web apps, otto can't check if the UI actually works — it reads code diffs and guesses. A real user would open the app, click around, and see if it works.

### The Insight

Spec items fall into two categories:

| Type | Example | How to verify |
|------|---------|---------------|
| **Verifiable** | "e2e latency <300ms" | Automated test that measures and asserts |
| **Verifiable** | "search is case-insensitive" | `assert search("HELLO") == search("hello")` |
| **Verifiable** | "python -m bookmarks works" | subprocess test, exit code 0 |
| **Non-verifiable** | "Apple Weather visual style" | Screenshot + LLM judgment |
| **Non-verifiable** | "smooth transitions" | Browser observation, subjective |
| **Non-verifiable** | "non-intrusive location" | Screenshot + LLM judgment |

Verifiable specs should have **hard test gates** — the task can't pass without a test
for each one. But some verifiable constraints may be genuinely infeasible (e.g., <300ms
on a cold external API call). The coding agent must try 3+ fundamentally different
approaches before escalating. The pilot judges whether the escalation is genuine or lazy —
preventing agents from finding easy excuses while allowing real impossibility.

Non-verifiable specs get **best-effort behavioral validation** — browse the app,
screenshot, let the LLM judge with iterative hill-climbing.

### The Architecture

```
Spec items (from spec gen)
│
├── Verifiable (has_test_strategy: true)
│   ├── Binary ("search returns results")
│   │   → Test must pass. No gradient. Retry until it works or escalate.
│   │
│   └── Gradient ("<300ms latency", "< 10MB memory")
│       → Grind toward target. Always try to hit it exactly.
│       → If infeasible after 3+ architecturally different approaches:
│         report best achieved + why target is unreachable + what was tried.
│         USER decides what's acceptable, not the agent.
│
└── Non-verifiable (has_test_strategy: false)
    → Coding agent implements best-effort
    → Tier 4 (behavioral): browse app, screenshot, LLM judges
    → Iterative refinement: screenshot → concrete feedback → retry
    → Max 3 rounds. Agent always tries to improve, never declares "good enough"
    → Pilot reports what was achieved. User decides acceptance.
```

**Anti-reward-hacking principle:** Neither the coding agent nor the pilot ever declares
"close enough" or "good enough." They always try to hit the exact target. When they
can't, they report the gap with full transparency. The user decides acceptance.
This prevents agents from finding thresholds where they can stop trying.

---

## Phase 1: Spec Classification

### What changes

Spec gen agent classifies each item as verifiable or non-verifiable, and for verifiable items, suggests a test strategy.

### tasks.yaml schema change

```yaml
spec:
  - text: "e2e latency <300ms"
    verifiable: true
    test_hint: "measure time from fetch start to render complete, assert <300ms"
  - text: "Apple Weather visual style with gradient backgrounds"
    verifiable: false
  - text: "python -m bookmarks works as entry point"
    verifiable: true
    test_hint: "subprocess run python -m bookmarks --help, assert exit code 0"
```

Backward compatible: if spec items are plain strings (old format), treat all as verifiable (current behavior).

### Files

- `otto/spec.py` — update system prompt to classify items, update output format
- `otto/tasks.py` — support new spec item format (dict with text/verifiable/test_hint)
- `otto/runner.py` — read new format, pass test_hints to coding agent
- `otto/pilot.py` — read new format for compliance check

---

## Phase 2: Verification Test Enforcement

### What changes

For verifiable spec items, the coding agent is told to write a specific test for each. Verification (Tier 1) checks that these tests exist and pass.

### Coding agent prompt addition

```
VERIFIABLE SPEC ITEMS — you MUST write a test for each:
  1. [verifiable] "e2e latency <300ms"
     Test hint: measure time from fetch start to render complete, assert <300ms
  2. [verifiable] "search is case-insensitive"
     Test hint: search with mixed case, verify same results
  3. [visual] "Apple Weather gradient backgrounds"
     (No test required — will be verified visually)
```

### Verification enforcement

After the coding agent finishes, before declaring pass, check:
- For each verifiable spec item, does a test exist that exercises it?
- The completion_check in the coding agent system prompt: "for each verifiable item, name the test that proves it"
- If a verifiable test genuinely can't pass (after 3+ approaches), the agent writes a structured escalation in task notes explaining what was tried and why

### Infeasibility handling

The tension: we want hard enforcement, but some constraints are genuinely impossible.
The solution: high bar for escalation, pilot as judge.

```
Coding agent tries approach 1 → test fails
Coding agent tries approach 2 (fundamentally different) → test still fails
Coding agent tries approach 3 (fundamentally different) → test still fails
→ Agent writes escalation: "Tried X, Y, Z. Constraint impossible because..."
→ Pilot reads escalation, judges:
  - Did agent try genuinely different approaches? (not variations of same thing)
  - Is the impossibility real? (network physics vs just hard engineering)
  - Accept with gap documented, or retry with "try harder" hint
```

The pilot's anti-gaming check: "variations of the same approach" (e.g., "tried caching,
tried better caching, tried even better caching") doesn't count as 3 approaches.
Different means architecturally different (caching vs prefetch vs edge compute vs
stale-while-revalidate).

### Files

- `otto/runner.py` — format spec items differently in prompt (verifiable vs visual)
- `otto/runner.py` — coding agent system prompt: enforce test per verifiable item, structured escalation for infeasible
- `otto/pilot.py` — pilot judges escalations: genuine vs lazy

---

## Phase 3: Behavioral Verification (Browser Automation)

### What changes

New verification tier that browses web apps, takes screenshots, and uses LLM judgment for non-verifiable specs.

### When it runs

- After Tier 1-3 pass (existing tests + custom verify)
- Only for web projects (detected by: package.json with next/react/vue/angular, or index.html)
- Only when non-verifiable spec items exist

### How it works

1. **Start the app** — `npm run dev` / `python manage.py runserver` / detected dev server command
2. **Browse key pages** — navigate to localhost, wait for load
3. **Take screenshots** — capture current state
4. **Interact** — click buttons, fill forms, navigate (based on spec items)
5. **Screenshot after interactions** — capture results
6. **LLM judges** — send screenshots + non-verifiable spec items to Claude, ask "does this meet the spec?"
7. **Report** — advisory (not hard-gate), included in pilot's compliance data

### Implementation approach: MCP chrome-devtools

The user's CC setup already has chrome-devtools MCP tools — a full browser automation
suite with zero extra dependencies:
- `navigate_page`, `take_screenshot`, `click`, `fill`, `fill_form`
- `wait_for`, `hover`, `type_text`, `press_key`
- `evaluate_script`, `get_console_message`, `list_network_requests`
- `lighthouse_audit`, `performance_start_trace`, `performance_stop_trace`
- `emulate` (device emulation), `resize_page`

The coding agent already has access via `setting_sources=["user"]`. No Playwright
install, no extra dependencies. The agent uses chrome-devtools MCP to:
1. Start the app (via Bash)
2. Open a browser page (`new_page`)
3. Navigate (`navigate_page`)
4. Interact (`click`, `fill`, `type_text`)
5. Screenshot (`take_screenshot`)
6. Even run Lighthouse audits for performance metrics

**Applies beyond web apps:**
- **Web apps**: chrome-devtools MCP (navigate, click, screenshot)
- **Electron apps**: same protocol via `electron` skill
- **CLI apps**: subprocess tests (already covered)
- **APIs**: curl/requests in tests (already covered)

### Verify command auto-detection

For web projects, auto-generate a verify script that:
1. Starts the dev server in background
2. Waits for it to be ready (poll localhost)
3. Uses chrome-devtools MCP for screenshot/interaction
4. Saves screenshots to `otto_logs/<key>/screenshots/`
5. Kills dev server

### Files

- `otto/verify.py` — add Tier 4 (behavioral verification)
- `otto/runner.py` — detect web project, add behavioral verify
- `otto/config.py` — auto-detect web dev server command

---

## Phase 4: Screenshot-Based Compliance Check

### What changes

The pilot's spec compliance check uses actual screenshots instead of just reading diffs.

### How it works

After a task passes all verification tiers:
1. Screenshots are saved to `otto_logs/<key>/screenshots/`
2. Pilot reads these screenshots (Claude is multimodal)
3. For non-verifiable spec items, pilot judges: "does this screenshot show Apple Weather-style gradient backgrounds?"
4. For verifiable items, pilot confirms tests exist in the diff

### Pilot prompt update

```
SPEC COMPLIANCE CHECK (after each task passes):
For VERIFIABLE items:
- Confirm a test exists in the diff that exercises this spec item
- If no test found, retry with feedback: "missing test for spec item #N"

For NON-VERIFIABLE items:
- Review screenshots in otto_logs/<key>/screenshots/ if available
- Judge: does the visual output match the spec description?
- If clearly wrong, retry with visual feedback
- If subjective/close enough, pass with a note
```

### Files

- `otto/pilot.py` — update compliance check prompt to reference screenshots
- `otto/pilot.py` — MCP tool to read screenshots (or just pass paths to pilot)

---

## Phase 5: Iterative Refinement for Non-Verifiable Specs

### What changes

For non-verifiable specs, the pilot gives concrete visual feedback and the coding
agent iterates. Each round must improve on something specific.

### How it works

```
Round 1: Coding agent implements
  → Screenshot → Pilot: "gradient is too dark, text hard to read"
  → Retry with specific visual feedback

Round 2: Coding agent fixes
  → Screenshot → Pilot: "gradient improved, but missing blur effect on cards"
  → Retry with specific visual feedback

Round 3: Coding agent fixes
  → Screenshot → Pilot reports to user:
    "3 rounds completed. Current state: gradient backgrounds working,
     blur effect on cards, transitions present. Screenshots attached."
    User decides if acceptable.
```

Each round must have **concrete, actionable feedback** — not "make it better" but
"the gradient is too dark, use lighter colors for day mode." The pilot acts like
a design reviewer: specific critique, not vague judgment.

### Limits

- Max 3 refinement rounds (avoid infinite loops)
- Each round must identify a specific issue to fix (not "try harder")
- After max rounds: pilot reports what was achieved with screenshots
- **User decides acceptance** — the agent never declares "good enough"

### Also applies to gradient verifiable specs

For verifiable specs with a gradient (e.g., "<300ms"), the same iterative approach
applies to the implementation:
```
Approach 1: Caching → achieved 350ms
Approach 2: Cache + parallel fetch → achieved 280ms → PASS
```
Or if target unreachable:
```
Approach 1: Caching → 350ms
Approach 2: Cache + prefetch → 320ms
Approach 3: Cache + prefetch + stale-while-revalidate → 310ms
→ Report: "Best achieved: 310ms. Target: 300ms. Gap: 10ms.
   Tried: caching, prefetch, stale-while-revalidate.
   Remaining bottleneck: geocoding API cold start (network physics)."
   User decides.
```

### Files

- `otto/pilot.py` — add visual refinement loop to spec compliance check
- `otto/runner.py` — support pilot feedback that includes screenshot references

---

## Implementation Order

| Phase | Effort | Impact | Dependencies |
|-------|--------|--------|--------------|
| 1. Spec classification | Small | High | None — backward compatible |
| 2. Test enforcement | Small | High | Phase 1 |
| 3. Browser automation | Medium | High | None (independent) |
| 4. Screenshot compliance | Small | Medium | Phase 3 |
| 5. Hill-climbing | Small | Medium | Phase 3, 4 |

Phases 1-2 can ship independently (pure spec + prompt changes, no new infrastructure).
Phase 3 is the big infrastructure piece (playwright, dev server management).
Phases 4-5 build on 3 and are mostly prompt changes.

---

## Design Decisions

1. **Spec classification in spec gen, not coding agent.** The spec gen agent has the user's original intent fresh. The coding agent sees the spec after it's written. Classification should happen at generation time.

2. **Playwright over MCP chrome-devtools for v1.** Self-contained, no external config needed. The coding agent can install playwright as a dev dependency and write tests that run in CI too. MCP chrome-devtools is better for interactive debugging but harder to automate in verification.

3. **Agents never declare "good enough."** Neither coding agent nor pilot judges acceptance. They always try to hit the exact target. When they can't, they report what was achieved + what was tried + why the gap exists. The user decides. This prevents reward hacking — no threshold where agents get to stop trying.

4. **Backward compatible spec format.** Plain string specs (old format) still work. New format is opt-in via dict items with `text`/`verifiable`/`test_hint` fields.

5. **Test hints, not test code.** The spec gen agent suggests what to test ("measure latency, assert <300ms"), not how to test it. The coding agent knows the codebase and picks the right testing approach.

---

## Risks

**Risk: Playwright adds complexity and weight to projects**
Mitigation: Only install for web projects. Use as devDependency. Coding agent manages the setup.

**Risk: Screenshot-based judging is unreliable**
Mitigation: Advisory only, not gating. Bounded rounds. Pilot can pass with a note if "close enough."

**Risk: Dev server startup is flaky**
Mitigation: Poll with timeout. If server doesn't start, skip behavioral verification (fall back to code-only). Log the failure.

**Risk: Spec classification is wrong (marks non-verifiable as verifiable)**
Mitigation: The coding agent can override — if it can't write a meaningful test for a "verifiable" item, it notes this in task notes. The pilot reads the note.

---

## Plan Review

### Round 1 — Plan Reviewer
Status: **Approved**

Recommendations (advisory):
- Phase 1: `add_task` type annotation needs updating from `list[str] | None` to `list[str | dict] | None`
- Phase 3: Dev server lifecycle (start/poll/kill) is non-trivial — use a helper class for cleanup safety
- Phase 3: Playwright install may fail (~150MB download) — skip Tier 4 on failure, don't block
- Phase 5: Hill-climbing round tracking needs explicit state management (prompt state vs run-state.json)
