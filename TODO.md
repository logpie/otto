# Otto — TODO

## Completed

- [x] "Test like a user" principle (subprocess for CLIs)
- [x] Anti-pattern rubrics (happy path, errors, negative, edge cases)
- [x] Test generation retry with error feedback
- [x] Rubric count scaling by complexity
- [x] Test quality guidelines (no trivial tests, parametrize, smoke tests)
- [x] Smart context gathering (AST symbol index + import graph, 86% reduction)
- [x] Adversarial testgen agent (black-box TDD, mechanical isolation, tamper detection)
- [x] Regression-style rubrics
- [x] Post-run integration tests with agent fix loop
- [x] Live agent streaming with styled output
- [x] Agent + testgen session logs
- [x] Timing per task and full run
- [x] `otto diff`, `otto show`, `otto retry --force` with feedback

## Remaining

### High Priority
- [x] **Mutation-style checks**: After tests pass, comments out a random implementation line and re-runs adversarial tests. Reports whether mutation was caught. Informational signal for test quality.
- [x] **Cost tracking**: Per-task cost from `ResultMessage.total_cost_usd`. Shows in `otto status`, `otto show`, task pass/fail lines, and run summary.

### High Priority (v2 — completed, superseded by v3)
- [x] **Architect phase**: Design agent runs before implementation — examines all tasks + existing codebase, produces living `otto_arch/` with shared conventions, interface contracts, and `conftest.py` test helpers. Prevents convention drift across tasks.
- [x] **BUG: Parallel test contamination**: In parallel mode, sibling tasks' test files caused false failures. Fix: sibling test files are now deleted from the disposable verification worktree (not the task worktree — keeps git history clean). Uses actual file paths from pre-generation phase, framework-agnostic.
- [x] **Conflict-aware architect (v2 Phase 1)**: Architect produces `file-plan.md` predicting which files each task modifies. Runner auto-injects `depends_on` chains to prevent merge conflicts. Eliminated the $1.43/2-task-lost merge conflict problem.
- [x] **Holistic testgen (v2 Phase 2)**: Single agent call generates tests for ALL tasks with shared `conftest.py`. Consistent conventions by construction. Replaces serial per-task testgen. *(Removed in v3 — coding agent writes own tests)*
- [x] **Pilot orchestrator (v2 Phase 3)**: LLM-driven execution engine replaces fixed `run_all()` pipeline. Plans before executing, adapts on failure, steers with hints. Uses MCP tools to drive testgen/coding/verify/merge. *(v3: simplified to 9 MCP tools, `run_all()` removed, pilot is the only path)*
- [x] **Enhanced integration gate (v2 Phase 4)**: Cross-task consistency review before integration tests. *(Removed in v3 — pilot handles cross-task concerns)*
- [x] **Supervisor agent**: Replaced by the pilot orchestrator (v2 Phase 3). The pilot IS the supervisor.
- [x] **Pilot display fixes**: Fixed: (1) strip `tool` key from side-channel data, (2) deduplicate tool headers, (3) stop spinner on ThinkingBlock, (4) integration gate retry on API error.
- [x] **Fail-fast on repeated errors**: Now handled by pilot doom-loop detection in v3. *(Pilot prompt instructs: same error 2+ times → change strategy or abort)*
- [x] **Failure classification before retry**: No longer needed — no adversarial tests to classify. Pilot analyzes verify output and decides retry strategy.
- [x] **Pilot retry tracking**: Simplified in v3 — cost flows through from task state directly.

### High Priority (current)
- [ ] **Pilot UX polish**: v3 pilot output already has noise filtering, spinners, diffs, and timing. Remaining:
  - Expand on failure only: compact single-line per task on success, multi-line with test output + diff on failure
- [ ] **Pilot speed optimization**: Overhead is ~3-5 min per run (ToolSearch, reasoning between tools). Investigate: (1) pre-load MCP tools to avoid ToolSearch, (2) reduce pilot reasoning turns, (3) batch save_run_state.
- [ ] **Pilot structured system prompt**: If pilot starts dodging responsibilities (skipping compliance checks, not doing behavioral testing), apply the same structured system_prompt treatment as spec/coding agents (XML tags, anti-examples, compliance self-check). Currently not needed — pilot follows instructions well. Monitor for: over-exploration before simple tasks, skipping behavioral testing, declaring tasks passed without checking.
- [x] **Run time reporting**: `otto status` shows per-phase timing (prepare, coding, test, qa, merge) and total elapsed time.
- [x] **Coding agent CC parity**: max_turns=200 and effort="high" (both configurable in otto.yaml), setting_sources=["project"] for CLAUDE.md, env=os.environ for API keys/PATH. See v3 design Phase 9.

### Medium Priority
- [x] **Rename rubric → spec throughout** — Done. `rubric.py` → `spec.py`, `generate_rubric()` → `generate_spec()`, `--no-rubric` → `--no-spec`, tasks.yaml `rubric:` → `spec:`.
- [x] **Spec generation: extract hard constraints** — Done. Structured system prompt with XML tags, anti-examples, compliance self-check. Spec agent no longer softens constraints.
- [ ] **Refactor: testgen.py cleanup** — only used via `--tdd` now, but still has 4 near-duplicate functions. Consolidate into a shared `_run_testgen_core()`.
- [x] **Refactor: runner.py is 2300 lines** — v3 reduced to ~1140 lines. `run_all()`, cross-task review, reconciliation, integration gate all removed. Only `run_task()` + git utilities remain.
- [ ] **Refactor: pilot.py MCP script is embedded string** — ~400 lines of Python inside an f-string with double-brace escaping. Extract to `otto/pilot_mcp_server.py` as a real module, pass config via env vars or temp JSON file instead of baking into the script.
- [x] **BUG: `reset --hard` destroys user commits**: Fixed. Replaced `reset`/`delete` with `drop` (remove from queue, code stays) and `revert` (git revert specific otto commits). No more `git reset --hard`.
- [ ] **Task history**: `otto add -f` wipes all tasks — no way to see past runs. Add `otto history` or archive old tasks before re-import so devs can review prior results/costs.
- [ ] **Retry UX**: `otto retry` without `--force` for stale tasks, cascade to blocked dependents, `--run` flag to combine retry+run. See `docs/superpowers/plans/2026-03-15-retry-ux.md`.
- [ ] **`otto retry --respec`**: Regenerate spec when retrying a task. Currently `otto retry` keeps the old spec, which may have been the problem (e.g., softened constraints from old spec agent). Also consider `otto spec <id>` to regenerate spec for any task without resetting status.

### Low Priority
- [x] **Coding agent subagents**: Added `researcher` (haiku, read-only investigation) and `explorer` (haiku, codebase search) subagents via AgentDefinition. Coding agent can spawn these to parallelize exploration within a task.
- [ ] **`--fast` mode**: Pre-load context into prompt, tell agent "start writing immediately." Cuts tool calls. Expected: ~25s instead of ~68s. Keeps self-validation.
- [x] **Parallel tasks**: Run independent tasks concurrently in git worktrees. `depends_on` field, topological ordering, blocked status. *(v3: parallel execution removed from default path — pilot runs tasks sequentially. Can be re-added as optimization.)*
- [x] **Re-add parallel execution**: `max_parallel: N` in otto.yaml. Independent tasks run in parallel worktrees, merge serially. Merge conflicts auto-retry on updated main. Pressure-tested: 8 runs, 13 tasks, 0 bugs.
- [ ] **Parallel TUI**: `rich.Live` panels with per-task workzone boxes for parallel execution. Keypress toggles for compact/panel/focus modes. See `docs/superpowers/plans/2026-03-15-parallel-tui.md`.
- [ ] **Watch mode**: `otto watch` auto-reimports and reruns on file changes.
- [ ] **Progress bar**: Show overall progress (3/5 tasks) during run.
