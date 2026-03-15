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
- [ ] **Mutation-style checks**: After tests pass, intentionally break a key line and verify the tests catch it. Validates that adversarial tests actually catch bugs, not just exercise code. Without this, we have no signal for test quality.
- [ ] **Cost tracking**: Log token usage and cost per task from `ResultMessage.total_cost_usd`. Show in `otto status`. Users are blind to spend right now.

### Medium Priority
- [ ] **Framework-specific test patterns**: Provide framework-specific examples (pytest fixtures, jest patterns) for higher quality generated tests.
- [ ] **Test deduplication**: When rubric tests overlap with existing project tests, detect and skip duplicates.
- [ ] **Cross-task regression gate**: After full run, run entire test suite one more time as final sanity check.
- [ ] **Test coverage delta**: Run `pytest --cov` before/after each task, warn if coverage didn't increase.

### Low Priority
- [ ] **Parallel tasks**: Run independent tasks concurrently on separate branches.
- [ ] **Environment tests**: Verify project works in a clean environment — fresh install, no cached state.
- [ ] **Watch mode**: `otto watch` auto-reimports and reruns on file changes.
- [ ] **Progress bar**: Show overall progress (3/5 tasks) during run.
