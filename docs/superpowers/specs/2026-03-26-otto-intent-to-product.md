# Otto: Intent-to-Product — Closing the Automation Gap

**Date**: 2026-03-26
**Status**: Design (updated with agent-native framing)
**Builds on**: v4.5 pipeline (implemented)
**Recovered**: 2026-03-28 — original was written in conversation but never committed, reconstructed from session history.

---

## The Gap

Today otto automates execution. The user does all the thinking:

```
Intent → Product Spec → Architecture → Task Decomposition → Per-Task Specs → Execution
         ↑ USER          ↑ USER        ↑ USER               ↑ OTTO           ↑ OTTO
```

For true intent-to-product, otto does everything:

```bash
otto build "bookmark manager with tags, search, and Chrome extension"
# Otto figures out EVERYTHING. One command.

otto build "bookmark manager" --review
# Shows the plan, waits for approval before executing.
```

---

## Agent-Native Design (Not Human Process Copies)

Human engineering teams use milestones, sprints, kanban, backlogs — these solve HUMAN problems (limited memory, communication overhead, burnout, capacity constraints). Agents don't have these problems.

Instead, design for AGENT strengths and weaknesses:

| Agent strength | Exploit it |
|---|---|
| Parallel execution | Run independent tasks simultaneously |
| Cheap retry | Try, fail, try again — no ego |
| Perfect file memory | Everything persists on disk |
| Tireless | Run for hours without breaks |

| Agent weakness | Compensate |
|---|---|
| Can't self-assess quality | External verification (QA, proof scripts) |
| Compound errors | **Verify before building on assumptions** |
| Hallucinate completion | Proof-of-work |
| Cost money per attempt | Circuit breaker (time + retry limits) |

**The key agent weakness that drives the design: compounding errors.** A bad architecture assumption in task 1 wastes ALL downstream work. The solution isn't milestones — it's **verification gates**.

---

## Verification Gates (Not Milestones)

Gates are checkpoints that verify assumptions before building on them. Pass the gate → proceed. Fail → fix before continuing. No downstream work is wasted on a bad foundation.

```yaml
# Product planner produces a GATED TASK GRAPH
gates:
  - name: foundation
    tasks: [1, 2, 3]
    verify: "app starts, CRUD works, data model is correct"

  - name: core_features
    depends_on: [foundation]
    tasks: [4, 5, 6]
    verify: "search works end-to-end, tags work, UI connects to API"

  - name: extensions
    depends_on: [core_features]
    tasks: [7, 8]
    verify: "Chrome extension saves bookmarks, import/export round-trips"
```

**Gates vs milestones:**

| Milestones (human) | Gates (agent) |
|---|---|
| "Deliver v1.0 to stakeholders" | "Verify foundation before building on it" |
| Incremental delivery | Assumption checkpoints |
| Time-boxed | Pass/fail (not time-boxed) |
| Stakeholder feedback | Automated verification |
| Priority triage | Gate ordering = priority |

Gate ordering IS priority. Foundation tasks are highest priority (without them nothing works). Extension tasks are lowest (core works without them).

---

## Failing Proofs = Bugs (Not a Bug Tracker)

No separate bug tracking system. Proof scripts ARE the bug tracker:

```
Gate 1 passes. Proof scripts all green.

Gate 2 tasks execute. After merging task 5 (UI):
  Re-run gate 1 proofs → search proof FAILS ✗

The failing proof IS the bug report:
  - What broke: search endpoint
  - Evidence: proof script output
  - When: after task 5 merged
  - Regression: was passing, now failing
  - Repro: re-run the proof script
```

Every gate re-runs ALL prior gate proofs (regression check). A newly failing proof is a regression bug. The proof is the report, the evidence, and the regression test — all in one artifact.

Fix flow (autonomous):
```
Prior proof fails → create fix task with the failing proof as context
  → coding agent sees: "this proof was passing, now fails after your changes"
  → fix runs through v4.5 pipeline
  → re-run the proof → passes → proceed
```

---

## Three Levels of Spec

### Product Spec (NEW — what the product does)

```markdown
# Bookmark Manager — Product Spec

## Core Features
- Save bookmarks with URL, title, optional description
- Tag system: add/remove tags, filter by tag
- Full-text search across title, URL, description, tags
- Chrome extension: save current page with one click
- Import/export (Netscape bookmark format)

## Scope
- Single-user, local data (SQLite), CLI + web interface

## Non-goals (v1)
- Mobile app, multi-user, AI auto-tagging

## Key User Journeys (for product verification)
1. Save & Search: create bookmark → add tags → search by tag → find it
2. Chrome Extension: click extension → save page → verify in web UI
3. Import Round-trip: import file → verify bookmarks → export → diff matches
```

User journeys are defined in the product spec (not a separate file). They guide gate verification.

### Architecture Spec (NEW for greenfield, ENHANCED for brownfield)

```markdown
# Architecture
Backend: Python FastAPI + SQLite (FTS5 for search)
Frontend: React SPA (Vite)
Extension: Chrome Manifest V3

## Data Model
Bookmark(id, url, title, description, tags[], created_at, updated_at)

## API Design
POST/GET/PATCH/DELETE /api/bookmarks
GET /api/search?q=term
GET /api/tags
POST /api/import, GET /api/export
```

Architecture is SHARED CONTEXT for all coding agents. Concrete decisions (FastAPI, not Django; SQLite, not Postgres) prevent agents from making conflicting choices.

### Task-Level Specs (EXISTS — [must]/[should] per task)

Produced by existing spec agent. No change.

---

## Product Planner Agent

### What it produces

One agent session → three artifacts on disk:
1. `otto_arch/product-spec.md` — features, scope, non-goals, user journeys
2. `otto_arch/architecture.md` — tech stack, data model, API, project structure
3. `tasks.yaml` — gated task graph with dependencies

### Agent configuration

```python
planner_opts = ClaudeAgentOptions(
    permission_mode="bypassPermissions",
    cwd=str(project_dir),
    setting_sources=["user", "project"],
    env=os.environ,
    # Full tools: Read, Grep, Glob (explore existing code)
    #             WebSearch, WebFetch (research technologies)
    #             Write (produce artifacts)
    # No max_turns, no custom system prompt micromanagement
)
```

### System prompt (goal-oriented)

```
You are a product planner for an autonomous coding system. Given a user's
intent, produce a complete plan that coding agents can execute without
further human input.

Produce three files:

1. product-spec.md — What we're building.
   Features, scope, non-goals. Be opinionated — plan an MVP.
   Include 3-5 key user journeys that define "product works."

2. architecture.md — How it's structured.
   Tech stack, data model, API design, project structure.
   Make concrete decisions. Don't leave choices to coding agents.
   If code already exists, respect existing choices.
   Research unfamiliar technologies before committing.

3. tasks.yaml — Gated task graph.
   Group tasks into verification gates (foundation → features → extensions).
   Each gate has a verify condition ("app starts", "search works").
   Each task: one coherent feature, implementable in one agent session.
   Dependencies: over-specify if unsure (safe > fast).
```

---

## Execution Flow

```
otto build "bookmark manager with tags, search, Chrome extension"

STEP 1: PRODUCT PLANNING
  Product planner agent explores codebase + researches technologies
  → produces product-spec.md, architecture.md, tasks.yaml
  → all persisted to disk (human-reviewable, survive crashes)
  [optional: --review flag pauses for user approval]

STEP 2: PER-GATE EXECUTION (uses existing v4.5 pipeline)
  For each gate in order:
    a. Spec gen for gate's tasks (parallel with coding, per v4.5)
    b. Execute tasks through v4.5 pipeline (bare CC → clean test → QA)
       Architecture.md included as context in every coding prompt
    c. Gate verification: run gate's verify condition + ALL prior gate proofs
    d. If gate fails → fix tasks → re-verify (smart retry, see below)
    e. If gate passes → proceed to next gate

STEP 3: PRODUCT VERIFICATION (after all gates pass)
  Product QA agent tests user journeys from product-spec.md
  → end-to-end journey testing (browser + API)
  → regression check (all gate proofs still pass)
  → product-level proof-of-work (journey scripts + screenshots)
```

---

## Gate Verification

After all tasks in a gate complete, the gate must be verified before proceeding.

Gate verification = **gate's own verify condition** + **all prior gate proofs (regression)**

```python
async def verify_gate(gate, prior_proofs, project_dir, config):
    """Verify a gate: run its condition + regression proofs."""

    # Run gate's verify condition (e.g., "app starts, CRUD works")
    gate_result = await run_gate_check(gate.verify, project_dir, config)

    # Re-run ALL prior gate proofs (regression check)
    regression_failures = []
    for proof_script in prior_proofs:
        result = subprocess.run(["bash", proof_script], capture_output=True)
        if result.returncode != 0:
            regression_failures.append((proof_script, result.stderr))

    return GateResult(
        passed=gate_result.passed and not regression_failures,
        gate_issues=gate_result.issues,
        regressions=regression_failures,
    )
```

### Smart retry on gate failure

Gate verification classifies failures (from Codex suggestion):

```python
for failure in gate_result.failures:
    if failure.type == "glue_bug":
        # Cheap: create a fix task, run through v4.5
        add_task(tasks_file, prompt=f"Fix: {failure.description}")

    elif failure.type == "architecture_mismatch":
        # Medium: update architecture + fix task
        # If one task diverged → fix that task
        # If architecture was wrong → update architecture.md, re-spec affected tasks
        add_task(tasks_file, prompt=f"Fix mismatch: {failure.description}")

    elif failure.type == "design_flaw":
        # Expensive: re-invoke product planner with failure context
        # Only re-run tasks affected by the design change
        new_plan = await product_planner(
            original_intent, project_dir, config,
            context=f"Previous attempt failed: {failure.description}. Revise."
        )

# Run fix tasks through v4.5 pipeline, then re-verify gate
```

Escalation: glue_bug (cheap) → architecture_mismatch (medium) → design_flaw (expensive). Try cheapest fix first.

Circuit breaker: max 3 gate verification rounds. If gate still fails after 3, report what's broken and stop.

---

## Product QA (After All Gates Pass)

Tests USER JOURNEYS from product-spec.md against the complete integrated product. Different from task QA (which tests individual [must] items).

```
Task QA:    "Does GET /api/search return results?"           (component)
Product QA: "Can a user save a bookmark, tag it, search,    (journey)
             and find it via the web UI?"
```

### Product QA agent

```python
product_qa_opts = ClaudeAgentOptions(
    permission_mode="bypassPermissions",
    cwd=str(project_dir),
    setting_sources=["user", "project"],
    env=os.environ,
    mcp_servers={"chrome-devtools": chrome_config},  # always has browser
    # No max_turns
)
```

Prompt receives: product-spec.md (including user journeys) + architecture.md + full codebase.

Tests each user journey end-to-end. Produces journey-based proof-of-work:

```json
{
  "product_passed": true/false,
  "journeys": [
    {
      "name": "Save & Search",
      "status": "pass",
      "steps": [
        {"action": "POST /api/bookmarks", "result": "201", "proof": "journey-1-step-1.sh"},
        {"action": "GET /api/search?q=machine", "result": "1 match", "proof": "journey-1-step-2.sh"}
      ]
    }
  ],
  "regressions": [],
  "failure_type": null
}
```

### Product QA failures → autonomous fix

Same smart retry as gate failures:
- `glue_bug` → fix task
- `architecture_mismatch` → update arch + re-spec
- `design_flaw` → re-plan affected parts

Max 3 product QA rounds.

---

## Compatibility with v4.5

The intent-to-product design builds ON TOP of v4.5. No changes to per-task execution.

### What v4.5 provides (no changes needed)

| v4.5 component | Used by intent-to-product |
|---|---|
| `run_per()` orchestrator | Executes tasks within each gate |
| `coding_loop()` | Per-task execution (bare CC → clean test → QA) |
| `run_task_v45()` | Inner task loop with retry |
| Spec agent (parallel) | Generates [must]/[should] per task |
| QA agent (tiered) | Per-task spec verification |
| `tasks.yaml` format | Tasks from product planner stored here |
| `default_plan()` / `plan()` | Batches tasks within a gate by dependencies |
| Telemetry dual-write | Events from product planning + gate verification |
| Signal handling | Ctrl+C during product build |
| PipelineContext | Cross-task learnings across gates |

### What's NEW (additive, doesn't modify v4.5)

| New component | What it does |
|---|---|
| `product_planner.py` | Intent → product-spec.md + architecture.md + tasks.yaml |
| `product_qa.py` | Journey-based verification against product spec |
| `otto build` command in cli.py | Calls product planner → then existing `run_per()` |
| Gate verification logic | Runs gate checks + regression proofs between batches |
| Architecture context injection | Adds architecture.md to coding prompts |

### How gates map to v4.5 batches

The v4.5 orchestrator already executes tasks in dependency-ordered batches. Gates ADD verification between batch groups:

```
v4.5 today:
  batch 1: [task 1] → batch 2: [task 2, 3] → batch 3: [task 4, 5, 6] → ...
  (no verification between batches)

With gates:
  GATE 1 (foundation):
    batch 1: [task 1] → batch 2: [task 2, 3]
    → GATE VERIFICATION (app starts? CRUD works?)

  GATE 2 (core_features):
    batch 3: [task 4, 5, 6]
    → GATE VERIFICATION (search? UI? + re-run gate 1 proofs)

  GATE 3 (extensions):
    batch 4: [task 7, 8]
    → GATE VERIFICATION (extension? import? + re-run all proofs)
```

The batching WITHIN a gate is exactly v4.5's `default_plan()` topological sort. Gates add verification BETWEEN batch groups. The orchestrator change is small:

```python
async def run_gated(gates, config, tasks_file, project_dir):
    """Execute a gated task graph. Each gate uses v4.5's run_per() internally."""
    prior_proofs = []

    for gate in gates:
        # Write gate's tasks to tasks.yaml
        write_gate_tasks(tasks_file, gate.tasks)

        # Execute gate's tasks using existing v4.5 pipeline
        exit_code = await run_per(config, tasks_file, project_dir)

        # Gate verification
        gate_result = await verify_gate(gate, prior_proofs, project_dir, config)

        if not gate_result.passed:
            # Smart retry (fix tasks → re-run → re-verify)
            await handle_gate_failure(gate_result, config, tasks_file, project_dir)

        # Collect proofs for regression checking in future gates
        prior_proofs.extend(gate_result.proof_scripts)

    # All gates passed — run product QA
    await run_product_qa(product_spec, project_dir, config)
```

The key insight: `run_per()` is called INSIDE each gate. The gate logic wraps it with verification and regression checks. v4.5's per-task pipeline is completely unchanged.

---

## File Artifacts

```
project_dir/
  ├── otto_arch/
  │   ├── product-spec.md      # NEW — features, scope, user journeys
  │   ├── architecture.md      # NEW — tech stack, data model, API
  │   └── file-plan.md         # EXISTS — predicted file overlaps
  ├── tasks.yaml               # EXISTS — now auto-generated with gate info
  └── otto_logs/
      ├── product-planner.log  # NEW — planner agent transcript
      ├── gate-1-foundation/   # NEW — gate verification results
      │   ├── verify.log
      │   └── proofs/
      └── product-qa/          # NEW — product-level QA
          ├── verdict.json
          ├── proof-report.md
          └── proofs/
              ├── journey-1-save-and-search.sh
              └── journey-2-import-export.sh
```

All human-readable. `proof-report.md` renders screenshots inline in VS Code/GitHub.

---

## Implementation Steps

1. **`product_planner.py`** — one query() call, produces 3 files. Simplest new component.
2. **`otto build` in cli.py** — calls planner, writes tasks, calls `run_per()`.
3. **Architecture context** — wire `architecture.md` into coding agent prompt in `runner.py`.
4. **Gate verification** — wrap `run_per()` with gate checks + regression proofs.
5. **Product QA** — new `product_qa.py`, runs after all gates, journey-based testing.
6. **Gate failure handling** — smart retry with failure triage (glue/mismatch/flaw).

Steps 1-3 are enough for a working `otto build`. Steps 4-6 add the verification and quality layers.

---

## Design Decisions Log

### Codex review (2026-03-26)

Codex reviewed the initial spec. Suggestions and dispositions:

| Suggestion | Disposition | Rationale |
|---|---|---|
| Two-pass planner (spec+arch first, then tasks) | **Skip for v1** | Don't prescribe thinking order. If one-pass empirically fails, add second pass. |
| contracts.yaml (machine-readable) | **Skip** | Architecture.md + task specs + QA = three enforcement layers already. |
| journeys.yaml (separate file) | **Adopt as section** | Added "Key User Journeys" to product-spec.md instead of separate file. |
| Batch-level checkpoints | **Adopt** | This IS verification gates — same concept, now prioritized. |
| Architecture drift detection | **Skip** | Redundant with task QA + product QA. |
| Failure triage (glue/mismatch/flaw) | **Adopt** | Added to gate verification as failure classification. |

### Agent-native framing (2026-03-26, second iteration)

Initial design used milestones, backlogs, sprint capacity from human engineering. Reframed to agent-native:
- **Milestones → verification gates** (checkpoints for assumptions, not delivery increments)
- **Bug tracker → failing proofs** (proof scripts ARE the tracking system)
- **Sprint capacity → circuit breakers** (time + retry limits, not cost ceilings)
- **Backlog prioritization → gate ordering** (dependency order = priority)

### Budget-aware execution (2026-03-26)

No max cost ceiling — costs are hard to predict and cap prematurely. Instead:
- Max time per gate (circuit breaker)
- Max retries per task (existing v4.5 mechanism)
- Max gate verification rounds (3)
- Report cost after completion for user awareness

---

## Open Questions

1. **Planner quality.** Can one agent session reliably produce good architecture + right-sized tasks for a complex product? Or will it need iteration (plan → attempt → learn → re-plan)?

2. **Gate verify conditions.** The planner writes "app starts, CRUD works" as gate verify conditions. How are these actually tested? A mini QA session? A bash script? A checklist the orchestrator runs?

3. **Brownfield complexity.** For existing codebases, the planner must understand existing architecture before adding to it. How deep should exploration go? Time limit?

4. **Multi-session products.** `otto build` for a complex product might take 30-60 minutes. Should there be a way to pause after a gate and resume later?

---

## References

- v4.5 pipeline: `docs/superpowers/specs/2026-03-24-otto-v4.5-pipeline-redesign.md`
- v5 early thinking: `docs/superpowers/specs/2026-03-21-otto-v5-early-thinking.md`
- Workflow audit: `docs/superpowers/specs/2026-03-22-otto-workflow-audit.md`
- Anthropic harness design: https://www.anthropic.com/engineering/harness-design-long-running-apps
- Research: `research-intent-to-product.md`, `ralph-orchestrator-analysis.md`
