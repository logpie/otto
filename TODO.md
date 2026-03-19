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

### High Priority
- [x] **Architect phase**: Design agent runs before implementation — examines all tasks + existing codebase, produces living `otto_arch/` with shared conventions, interface contracts, and `conftest.py` test helpers. Prevents convention drift across tasks.
- [x] **BUG: Parallel test contamination**: In parallel mode, sibling tasks' test files caused false failures. Fix: sibling test files are now deleted from the disposable verification worktree (not the task worktree — keeps git history clean). Uses actual file paths from pre-generation phase, framework-agnostic.
- [x] **Conflict-aware architect (v2 Phase 1)**: Architect produces `file-plan.md` predicting which files each task modifies. Runner auto-injects `depends_on` chains to prevent merge conflicts. Eliminated the $1.43/2-task-lost merge conflict problem.
- [x] **Holistic testgen (v2 Phase 2)**: Single agent call generates tests for ALL tasks with shared `conftest.py`. Consistent conventions by construction. Replaces serial per-task testgen.
- [x] **Pilot orchestrator (v2 Phase 3)**: LLM-driven execution engine replaces fixed `run_all()` pipeline. Plans before executing, adapts on failure, steers with hints. Uses MCP tools to drive testgen/coding/verify/merge. `--no-pilot` falls back to classic pipeline.
- [x] **Enhanced integration gate (v2 Phase 4)**: Cross-task consistency review before integration tests. Claude agent sees combined diff, fixes inconsistencies (duplicate code, import conflicts). Verified against test suite before committing.
- [x] **Supervisor agent**: Replaced by the pilot orchestrator (v2 Phase 3). The pilot IS the supervisor — it drives execution, reads failure output, decides retry strategy, and handles merge failures.
- [ ] **Parallel testgen with architect**: Now that architect provides `test-patterns.md` + `conftest.py`, serial testgen is less critical. Add `--parallel-testgen` flag (or auto when `otto_arch/` exists) to parallelize testgen phase too. Currently the biggest latency bottleneck.
- [ ] **Fail-fast on repeated errors**: If attempt N and N+1 fail with the same test failures (identical failing test names), stop retrying — the agent can't self-correct on a structural mismatch. Compare failure signatures across attempts, abort early if no progress. Saves $2-4 per stuck task.
- [ ] **Failure classification before retry**: Before retrying, classify the failure: (a) implementation bug (agent's code wrong — retry makes sense), (b) test bug (adversarial test is wrong — regenerate test, not code), (c) environment issue (missing dep, broken baseline — abort, don't retry). The judge agent partially does (a) vs (b) but doesn't catch (c) or "impossible test" cases. A lightweight classifier before each retry could save most wasted cost.
- [ ] **Pilot UX overhaul**: Current pilot output is a mix of useful decisions and noise. Needs:
  - Filter noise: suppress ToolSearch, save_run_state, separator lines for non-interesting tools
  - Show code diffs: when coding agent finishes, show summary diff (files changed, lines added/removed). On failure, show failing tests + relevant output.
  - Show timing per phase: elapsed time next to each phase header (architect, testgen, per-task coding, integration gate)
  - Animated spinners: show ⠋⠙⠹⠸⠼⠴ or elapsed timer during long-running phases (architect 2+ min, coding 60+ sec) so user knows it's alive
  - Keep pilot reasoning: "Task #1 passed, now executing #2" is useful. "Good, all tools loaded" is not. Distinguish decisions from filler.
  - Expand on failure only: compact single-line per task on success, multi-line with test output + diff on failure
- [ ] **Pilot speed optimization**: Current overhead is ~3-5 min per run (ToolSearch, reasoning between tools, save_run_state). For simple happy-path runs the classic pipeline is faster. Investigate: (1) pre-load MCP tools to avoid ToolSearch, (2) reduce pilot reasoning turns, (3) batch save_run_state, (4) auto-select pilot vs classic based on task count/complexity.
- [ ] **Pilot retry tracking**: Pilot-level retries (calling `run_coding_agent` multiple times with hints) are invisible in `otto status`. Each `run_coding_agent` call starts a fresh `run_task()` which resets attempts to 0. Need to: (1) accumulate cost across pilot retries, (2) track total pilot attempts in tasks.yaml, (3) show pilot retry history in `otto show`.
- [x] **Pilot display fixes**: Fixed: (1) strip `tool` key from side-channel data, (2) deduplicate tool headers, (3) stop spinner on ThinkingBlock, (4) integration gate retry on API error.
- [ ] **Run time reporting**: `otto status` only shows coding time (2m54s) but full run was 10m34s. Missing: architect, testgen, integration gate, pilot overhead. Should track per-phase timing and show total run time in `otto status` summary.

### Medium Priority
- [ ] **Rename rubric → spec throughout** — "rubric" means grading checklist (evaluative, after the fact). What we actually have is acceptance criteria / specs (prescriptive, before implementation). Rename: `rubric.py` → `spec.py`, `generate_rubric()` → `generate_spec()`, `--no-rubric` → `--no-spec`, tasks.yaml `rubric:` → `spec:`. Clear naming makes the system easier to understand.
- [ ] **Spec generation: extract hard constraints from user prompt** — user writes "< 300ms latency, hard constraint" but the rubric agent dilutes it to "cached lookups < 300ms." The agent should extract explicit user constraints first (numbers, thresholds, "must"/"never"/"hard constraint"), preserve them verbatim as top-priority criteria, then add 3-5 supporting criteria. No new flags — just smarter prompt. Keeps total to 5-8 criteria max, reduces bloat/duplication.
- [ ] **Refactor: testgen.py has 4 near-duplicate functions** — `run_testgen_agent`, `run_holistic_testgen`, `generate_tests`, `generate_integration_tests` share 80% of their code (agent setup, streaming, validation, file handling). Two real patterns: (1) pre-impl TDD from rubric, (2) post-impl integration. Consolidate into a shared `_run_testgen_core()` with mode/config params.
- [ ] **Refactor: runner.py is 2300 lines** — doing too much (task execution, architect phase, dep injection, holistic testgen loop, cross-task review, reconciliation, parallel worktree management). Split into: `runner.py` (core `run_task()` loop), `pipeline.py` (orchestration phases for `run_all()`), and move cross-task review + integration gate into their own module.
- [ ] **Refactor: pilot.py MCP script is embedded string** — 500 lines of Python inside an f-string with double-brace escaping. Extract to `otto/pilot_mcp_server.py` as a real module, pass config via env vars or temp JSON file instead of baking into the script.
- [ ] **BUG: `reset --hard` destroys user commits**: Currently uses `git reset --hard` to parent of oldest otto commit, which nukes interleaved user commits (e.g., `features.md`). Should use `git revert` on otto commits only, or at minimum only reset otto-prefixed commits while preserving user history.
- [ ] **Task history**: `otto add -f` wipes all tasks — no way to see past runs. Add `otto history` or archive old tasks before re-import so devs can review prior results/costs.
- [ ] **Review file caps**: `_MAX_STUB_FILES` and similar caps may be too conservative now that architect provides context. Check if they can be raised or removed.
- [ ] **Framework-specific test patterns**: Provide framework-specific examples (pytest fixtures, jest patterns) for higher quality generated tests.
- [ ] **Test deduplication**: When rubric tests overlap with existing project tests, detect and skip duplicates.
- [ ] **Cross-task regression gate**: After full run, run entire test suite one more time as final sanity check.
- [ ] **Test coverage delta**: Run `pytest --cov` before/after each task, warn if coverage didn't increase.
- [ ] **Retry UX**: `otto retry` without `--force` for stale tasks, cascade to blocked dependents, `--run` flag to combine retry+run. See `docs/superpowers/plans/2026-03-15-retry-ux.md`.

### Low Priority
- [ ] **Coding agent subagents**: Agent SDK supports `ClaudeAgentOptions.agents` — dict of named `AgentDefinition(description, prompt, tools, model)`. The coding agent can spawn subagents to parallelize within a task: research subagent to investigate APIs, test writer to write tests in parallel, exploration subagent to search codebase. Extends GRIND — explore multiple approaches simultaneously instead of serially.
- [ ] **`--fast` mode**: Two approaches (benchmarked: `claude -p` with tools is same speed as Agent SDK — both ~68s for rubric gen due to per-tool API round-trips):
  - **Option A (reduce tool calls)**: Pre-load context into prompt, tell agent "start writing immediately." Cuts 12 tool calls to 3-4. Expected: ~25s instead of ~68s. Keeps self-validation.
  - **Option B (true one-shot)**: `claude -p --tools "" --max-turns 1` — no tools at all, single text response. ~5s but ~70% reliability (original v1 approach, failed ~30% of the time). Only for prototyping.
- [x] **Parallel tasks**: Run independent tasks concurrently in git worktrees. `depends_on` field, topological ordering, blocked status, `--no-parallel` flag.
- [ ] **Parallel TUI**: `rich.Live` panels with per-task workzone boxes for parallel execution. Keypress toggles for compact/panel/focus modes. See `docs/superpowers/plans/2026-03-15-parallel-tui.md`.
- [ ] **Environment tests**: Verify project works in a clean environment — fresh install, no cached state.
- [ ] **Watch mode**: `otto watch` auto-reimports and reruns on file changes.
- [ ] **Progress bar**: Show overall progress (3/5 tasks) during run.
