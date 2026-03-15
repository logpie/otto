# Otto — TODO

## Rubric & Test Generation

- [x] **"Test like a user" principle**: Generated tests now use subprocess for CLIs, public API for libraries. No more CliRunner.
- [x] **Anti-pattern rubrics**: Rubric generation now requires happy path, error handling, negative ("does NOT"), and edge case categories.
- [x] **Test generation retry**: Retries with error feedback when validation fails.
- [x] **Rubric count scaling**: Rubric count scales with task complexity (3-5 simple, 6-10 medium, 10-12 complex).
- [x] **Test quality guidelines**: No trivial tests, bundle shared setup, use parametrize, smoke tests for CLIs.
- [x] **Smarter context gathering**: AST-based symbol index + import graph selects relevant files for large projects. 86% context reduction on 153-file project (40K→6K tokens). Task prompt + rubric keywords matched against symbol names with substring matching + import graph traversal.
- [ ] **Framework-specific test patterns**: Provide framework-specific examples (pytest fixtures, jest patterns, etc.).
- [ ] **Test deduplication**: When rubric tests overlap with existing project tests, detect and skip duplicates.

## Adversarial Testing

- [x] **Anti-pattern tests**: Rubric prompt explicitly asks for "does NOT" / "must NOT" criteria.
- [x] **Adversarial testgen agent**: Full Agent SDK testgen that writes black-box TDD tests from rubric BEFORE coding agent. Mechanical isolation (temp dir, AST stubs). Two-phase validation. Tamper detection.
- [x] **Regression-style rubrics**: Rubric generation includes "existing X still works after adding Y" criteria.
- [ ] **Mutation-style checks**: Intentionally break a key line and verify the tests catch it. If they don't, tests are too weak.

## Integration Testing

- [x] **Post-run integration tests**: Integration gate generates cross-feature tests after 2+ tasks pass. Agent fixes failures.
- [ ] **Cross-task regression gate**: After full run, run entire test suite one more time as final sanity check.
- [ ] **Environment tests**: Verify project works in a clean environment — fresh install, no cached state.

## Observability

- [x] **Live agent streaming**: Agent messages stream to stdout with styled ANSI formatting.
- [x] **Agent session logs**: Coding agent logs persisted to `otto_logs/<key>/attempt-N-agent.log`.
- [x] **Testgen agent logs**: Testgen agent logs persisted to `otto_logs/<key>/testgen-agent.log`.
- [x] **Timing**: Wall-clock time per task and full run.
- [ ] **Cost tracking**: Log token usage and cost per task from `ResultMessage.total_cost_usd`. Show in `otto status`.
- [ ] **Test coverage delta**: Measure test coverage change per task. Warn if coverage didn't increase.

## UX

- [x] **`otto diff <id>`**: Show git diff for a task's commit.
- [x] **`otto show <id>`**: Show task details — prompt, rubrics, status, commit, test file, logs.
- [ ] **Parallel tasks**: Run independent tasks concurrently on separate branches.
- [ ] **Watch mode**: `otto watch` re-runs on file changes.
- [ ] **Progress bar**: Overall progress (3/5 tasks) and per-task progress.
