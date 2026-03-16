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
- [ ] **Architect phase**: Design agent runs before implementation — examines all tasks + existing codebase, produces living `otto_design.md` with shared conventions, interface contracts, and `conftest.py` test helpers. Prevents convention drift across tasks. See `docs/superpowers/plans/2026-03-16-architect-phase.md`.

### Medium Priority
- [ ] **Framework-specific test patterns**: Provide framework-specific examples (pytest fixtures, jest patterns) for higher quality generated tests.
- [ ] **Test deduplication**: When rubric tests overlap with existing project tests, detect and skip duplicates.
- [ ] **Cross-task regression gate**: After full run, run entire test suite one more time as final sanity check.
- [ ] **Test coverage delta**: Run `pytest --cov` before/after each task, warn if coverage didn't increase.
- [ ] **Retry UX**: `otto retry` without `--force` for stale tasks, cascade to blocked dependents, `--run` flag to combine retry+run. See `docs/superpowers/plans/2026-03-15-retry-ux.md`.

### Low Priority
- [ ] **`--fast` mode**: Toggle `claude -p` one-shot for rubric/testgen instead of agentic. 5-10x faster and cheaper but lower quality (~70% vs ~95% reliability). Useful for prototyping and simple projects.
- [x] **Parallel tasks**: Run independent tasks concurrently in git worktrees. `depends_on` field, topological ordering, blocked status, `--no-parallel` flag.
- [ ] **Parallel TUI**: `rich.Live` panels with per-task workzone boxes for parallel execution. Keypress toggles for compact/panel/focus modes. See `docs/superpowers/plans/2026-03-15-parallel-tui.md`.
- [ ] **Environment tests**: Verify project works in a clean environment — fresh install, no cached state.
- [ ] **Watch mode**: `otto watch` auto-reimports and reruns on file changes.
- [ ] **Progress bar**: Show overall progress (3/5 tasks) during run.
