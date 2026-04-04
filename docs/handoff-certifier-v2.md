# Handoff: Certifier v2 — Agentic Product QA

## What Was Built

### Certifier v2 Architecture
The certifier evaluates products against their intent by simulating real users. It sits in otto's outer loop — its output creates fix tasks for the coding agent.

```
1. COMPILE: Intent → UserStory[] (LLM, shared for fair comparison)
2. ANALYZE: Code → ProductManifest (adapter + runtime probes)
3. PREFLIGHT: Quick structural check (~1s, no LLM)
4. VERIFY: Per-story journey agent (THE MAIN EVENT, no turn limit)
5. REPORT: Pass/fail + diagnosis + fix suggestions for outer loop
```

### New Files (v2)
- `otto/certifier/stories.py` — UserStory dataclass + LLM compiler from intent
- `otto/certifier/manifest.py` — ProductManifest from adapter + runtime probes
- `otto/certifier/preflight.py` — Quick structural check before journeys
- `otto/certifier/journey_agent.py` — Per-story agentic verification (the core)

### Modified Files
- `otto/certifier/__init__.py` — Added `run_certifier_v2()` alongside v1's `run_certifier_for_outer_loop()`
- `otto/outer_loop.py` — Uses `run_certifier_v2` for product verification
- `otto/product_planner.py` — Fixed planner path bug (tempfile pattern)
- `otto/certifier/baseline.py` — NEXTAUTH_URL fix in AppRunner, plus v1 binder code (keep for backward compat)
- `tests/test_orchestrator.py` — Updated mocks for v2

### v1 Files (kept for backward compat, not primary path)
- `otto/certifier/binder.py` — v1 compile→bind→execute (deprecated by v2)
- `otto/certifier/intent_compiler.py` — v1 claim compiler (replaced by stories.py)
- `otto/certifier/tier2.py` — v1 journey runner (replaced by journey_agent.py)
- `otto/certifier/baseline.py` — v1 per-claim execution (kept for AppRunner + utilities)

## Current State

### What Works
- Story compilation from intent (7 stories for task manager, 8 for recipe app)
- Product manifest from adapter + runtime route confirmation
- Preflight check (app alive, auth works, routes respond)
- Journey agent verification — **proven on task manager: 7/7 stories pass, $3.72, 18 min**
- Shared stories for fair comparison via `stories_path` parameter
- Fix task generation with diagnosis + fix suggestions for outer loop
- BREAK phase finds real quality issues (duplicate submit, no input validation, etc.)

### What's Running Right Now
A fair comparison is running in background (task ID: `bskog9lzd`):
- Bare CC task manager (port 4001) vs Otto task manager (port 4005)
- Same 7 shared stories from `bench/certifier-stress-test/shared-matrices/task-manager-stories-v2.json`
- Both running in parallel

Check result: `cat /private/tmp/claude-501/-Users-yuxuan-work-cc-autonomous/556f31f4-4b0b-4be7-900c-a36c7832c2fa/tasks/bskog9lzd.output`

### Previous Results
| Product | Builder | Stories | Cost | Time |
|---|---|---|---|---|
| Task Manager | Bare CC | 7/7 (100%) | $3.72 | 18 min |
| Task Manager | Otto (old, wrong intent) | 6/7 (86%) | $3.95 | 17 min |
| Recipe App | Bare CC | 6/8 (75%) | $6.04 | 26 min |

The otto 6/7 result was from a flawed test (wrong intent — had "priority" instead of "dueDate"). The current run uses the correct intent.

## Bugs Fixed This Session

### Critical (caused wrong results)
1. **Planner writes to wrong path** — Agent wrote `/home/user/otto_plan.json` instead of project dir. Root cause: prompt didn't specify absolute path. Fix: tempfile with absolute path (same pattern as spec agent in `otto/spec.py`).
2. **Wrong intent for otto build** — Manually wrote `priority` instead of `dueDate` when bypassing broken planner. Fix: fixed the planner so `otto build` works.
3. **NEXTAUTH_URL port mismatch** — AppRunner starts app on port 4005 but NextAuth thinks it's port 3000. Fix: set `NEXTAUTH_URL` in env when starting app.

### Certifier v1 Bugs (15+ from Codex audit)
- E-commerce bias removed (hardcoded store patterns, legacy normalizer, biased prompts)
- Self-healing changed to "try + record" model
- Schema hint → compiler feedback loop
- Response wrapper unwrapping
- Inline enum discovery from route code
- Fair comparison via `--matrix`/`--journeys`/`--plan` flags
- See `docs/certifier-hidden-inputs-audit.md` for full list of 9 hidden input surfaces

### Infrastructure
- Git HEAD guard for empty repos (`otto/orchestrator.py`)
- Plan artifacts committed before worktrees (`otto/cli.py` step 3.5)
- Per-story health check before agent runs (`otto/certifier/journey_agent.py`)
- Preflight warns about NEXTAUTH_URL mismatch

## What Needs Doing

### Immediate (validate v2)
1. **Wait for the fair comparison to complete** and verify results
2. **Run v2 on all 8 stress test projects** — only task manager and recipe app tested so far
3. **Wire `otto certify` CLI** for v2 — currently v2 is called programmatically, no CLI integration yet. Need `--stories` flag and v2 as default tier.
4. **Add `otto compare` command** — compare two certification results side by side

### Short term
5. **Add browser tools** to journey agent (chrome-devtools MCP) — currently API-only
6. **Verdict file reliability** — ~30% of stories don't write JSON verdict. ROOT CAUSE: `run_agent_query` only captures TextBlocks but agents that primarily use tools (Bash for curl) produce minimal TextBlocks → `raw_output` is empty → prose fallback fails.

   **FIX:** Replace `run_agent_query()` call in `journey_agent.py:277` with a direct `query()` loop — same pattern as QA agent in `otto/qa.py:763-861`. The QA agent's loop captures:
   - TextBlocks → `report_lines` (line 777)
   - ToolUseBlocks → `qa_actions` with command/input (lines 794-824)
   - ToolResultBlocks → output matched to tool calls (lines 840-855)
   - UserMessage ToolResultBlocks → same (lines 847-855)

   This gives the verdict parser complete text to work with. The QA agent has been battle-tested across 35+ projects with this pattern — it's reliable.
7. **Journey agent cost** — $0.50-1.00 per story is expensive. Consider using cheaper model for simple stories, opus for complex ones.

### Medium term
8. **Remove v1 code** — binder.py, old baseline per-claim execution, old tier2 runner. Only after v2 is fully validated.
9. **Otto build merge conflicts** — the latest otto build had both tasks verified but both failed to merge. Merge resolution needs work.
10. **Planner decomposition quality** — the planner added the raw intent as task #1 alongside decomposed tasks #2 and #3, causing task #1 to fail (duplicate work).

## Test Projects (all persistent)

```
bench/certifier-stress-test/
├── task-manager/          # Bare CC, port 4001
├── otto-task-manager/     # Otto (rebuilt with correct intent), port 4005
├── blog-app/              # Bare CC, port 4002
├── recipe-app/            # Bare CC, port 4003
├── url-shortener/         # Bare CC, port 4004
├── otto-recipe-app/       # Otto, port 4006
└── shared-matrices/
    ├── task-manager-stories-v2.json    # 7 shared stories (canonical intent)
    ├── shared-recipe-matrix.json       # v1 shared matrix
    └── shared-task-manager-matrix.json # v1 shared matrix
```

Start any app: `cd <dir> && NEXTAUTH_URL="http://localhost:PORT" PORT=PORT npm run dev`

## Key Design Decisions

1. **Stories, not claims** — The certifier tests user stories (multi-step flows), not individual endpoints. The inner loop already tests endpoints.
2. **Agentic, not compiled** — Journey agents interact with the app, adapting to its conventions. No more guessing field names or paths.
3. **No turn limit** — Agents work until they have evidence. Cost is secondary to accuracy.
4. **BREAK phase** — After happy path, agents try to break the product. Findings are quality signals, not certification gates.
5. **Fair comparison** — Shared stories compiled from intent (product-independent). Each product verified with same stories, same agent, same tools.

## Specs and Docs
- `docs/certifier-v2-product-qa-spec.md` — Final v2 spec with user stories, BREAK phase, browser tools
- `docs/certifier-v2-agentic-spec.md` — Earlier agentic spec (superseded by product-qa-spec)
- `docs/certifier-v2-spec.md` — Earlier semantic claims spec (superseded)
- `docs/certifier-v2-final-spec.md` — Tiered verification spec (partially incorporated)
- `docs/certifier-hidden-inputs-audit.md` — 9 hidden input surfaces that cause unfair comparison
- `docs/future-inner-loop-qa-optimization.md` — Future plan for inner loop optimization
- `bench/certifier-stress-test/findings.md` — Stress test results from v1
- `bench/certifier-stress-test/otto-vs-barecc.md` — v1 comparison results (outdated, pre-v2)

## How to Run

```bash
# Certify a product (v2, programmatic)
python -c "
from otto.certifier import run_certifier_v2
from pathlib import Path
result = run_certifier_v2(
    intent='...',
    project_dir=Path('path/to/project'),
    port_override=4001,
    stories_path=Path('path/to/shared-stories.json'),  # optional, for fair comparison
)
print(result['stories_passed'], '/', result['stories_tested'])
"

# Compile shared stories (for fair comparison)
python -c "
from otto.certifier.stories import compile_stories, save_stories
from pathlib import Path
import asyncio
stories = asyncio.run(compile_stories('your intent here'))
save_stories(stories, Path('shared-stories.json'))
"

# Run tests
.venv/bin/python -m pytest tests/ -x
```

## 609 tests pass as of this handoff.
