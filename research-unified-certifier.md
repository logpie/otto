# Research: Unified Certifier — Replacing Per-Task QA

## Problem

Two overlapping QA systems:
1. **Per-task QA** (spec.py + qa.py): LLM generates specs from prompt, LLM agent verifies code against specs. Runs per task during build. ~$1.50/task, ~2 min.
2. **Product certifier** (certifier/): Compiles stories from intent, runs journey agents against running product. Runs after build. ~$2/run, ~10 min.

In monolithic mode (one task = the product), they test the same artifact with overlapping coverage. The bookmark manager E2E: QA cost $1.27, certifier cost $1.82 — 88% of total cost was QA, testing the same endpoints twice.

## What exists today

### Per-task QA pipeline (spec.py → qa.py)
- **spec.py**: LLM generates [must]/[should] spec items from task prompt
- **qa.py**: LLM agent runs tests, checks endpoints, writes verdict JSON
- **Proof artifacts**: proof-report.md, regression-check.sh per must item
- **Tiers**: Tier 1 (behavioral test), Tier 2 (code review only)
- **Integration**: runner.py calls spec gen + QA in the coding loop

### Product certifier pipeline (certifier/)
- **classifier.py**: Detect framework, port, start command (no LLM)
- **adapter.py**: Scan code for routes, models, auth, seeds (no LLM)
- **stories.py**: LLM compiles intent → UserStory[] (cached)
- **manifest.py**: Combine adapter + runtime probes → ProductManifest (no LLM)
- **preflight.py**: Quick HTTP checks — app alive, routes respond (no LLM)
- **journey_agent.py**: Agentic verification per story (LLM, HTTP, structured output)
- **baseline.py**: AppRunner (start/stop apps), endpoint probes
- **verification.py**: Certify → fix → re-verify loop

### What v1 certifier has that v2 doesn't
- **intent_compiler.py**: Requirement matrix (10-25 testable claims per endpoint)
- **binder.py**: Resolves abstract claims to concrete routes
- **tier2.py**: Sequential deterministic journeys (no LLM at runtime)
- **pow_report.py**: Proof-of-work report generation

### What v2 has that v1 doesn't
- **stories.py**: Product-agnostic user stories (describe WHAT, not HOW)
- **journey_agent.py**: Agentic verification (adapts to product, richer diagnosis)
- **manifest.py**: Runtime enriched product description
- **preflight.py**: Quick structural validation
- **Break testing**: Adversarial edge case testing per story

## Design constraints

1. **CLI tools, libraries, non-web apps**: Certifier must work without a running server
2. **No early exit**: Run all tiers, produce complete diagnostic
3. **Test graduation**: Good tests become persistent regression tests
4. **Cost**: Must be cheaper than current QA + certifier combined
5. **Speed**: Structural checks in seconds, full certification in minutes
6. **The coding agent writes its own tests**: Those should be the primary per-task check

## Key insight

The coding agent already writes tests. `npm test` / `pytest` runs them. That's the per-task check — fast, cheap, no LLM. The certifier then validates the PRODUCT (not the code) after merge. Per-task LLM QA is the expensive middle layer that duplicates both.

## What the unified certifier needs to cover

Currently per-task QA catches things the coding agent's tests don't:
- Spec compliance (did the agent actually build what was asked?)
- Behavioral verification (does the endpoint return the right shape?)
- Edge cases (what happens with invalid input?)

The unified certifier must absorb these. Five tiers:

| Tier | What | LLM? | Speed | When |
|------|------|------|-------|------|
| 0 | Build + agent tests | No | seconds | Per task, during build |
| 1 | Structural: files exist, app starts, expected structure | No | seconds | After merge |
| 2 | API probes: routes respond, correct status codes, right shapes | No | seconds | After merge |
| 3 | Regression: graduated tests from prior certifications | No | seconds | After merge |
| 4 | Journey agents: simulate real users, break testing | Yes | minutes | After merge |

Tier 0 replaces per-task QA. Tiers 1-4 replace the current certifier. No LLM until tier 4.

## Open questions

- How do graduated tests work? Format? Who maintains them?
- Do we keep v1 certifier (intent_compiler + baseline + tier2) or fully replace with v2?
- How does certification work for CLI tools / libraries?
- Should tiers 1-2 run before merge (as a gate) or only after?
