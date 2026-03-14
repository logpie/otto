# Otto — TODO

## Rubric & Test Generation

- [x] **"Test like a user" principle**: Generated tests now use subprocess for CLIs, public API for libraries. No more CliRunner.
- [x] **Anti-pattern rubrics**: Rubric generation now requires happy path, error handling, negative ("does NOT"), and edge case categories.
- [x] **Test generation retry**: Single retry with error feedback when validation fails.
- [ ] **Smarter context gathering**: Current `_gather_project_context` reads up to 5 random files/100 lines; misses the relevant ones and bloats prompts causing timeouts. Fix: capture which files the agent read/edited during its run (from ToolUseBlock stream), pass only those to testgen. This gives the LLM exactly the files that matter instead of guessing.
- [ ] **Framework-specific test patterns**: Provide framework-specific examples (pytest fixtures with tmp_path, jest mocking patterns, etc.) for higher quality output.
- [ ] **Test deduplication**: When rubric tests overlap with existing project tests, detect and skip duplicates instead of testing the same thing twice.
- [ ] **Rubric count tuning**: 22 rubrics for one task is too many — testgen times out. Cap at 8-10 criteria, prioritize the most important ones.

## Adversarial Testing

- [x] **Anti-pattern tests**: Rubric prompt now explicitly asks for "does NOT" / "must NOT" criteria.
- [ ] **Regression-style rubrics**: When a task modifies existing code, auto-generate rubric items that verify existing behavior is preserved — not just that new behavior works. "Search still works after adding tags" type checks.
- [ ] **Security anti-patterns**: For tasks involving user input, file paths, or external data, generate tests that verify common vulnerabilities don't exist — injection, path traversal, unescaped output, etc.
- [ ] **Mutation-style checks**: After the agent implements a feature, intentionally break a key line and verify the tests catch it. If they don't, the tests are too weak.

## Integration Testing

- [ ] **Post-run integration tests**: After ALL tasks complete (not per-task), generate one final test file that exercises features working together. Run it as a final gate before the run summary. E.g., "import bookmarks from JSON, search them, favorite one, export as HTML — verify the exported HTML includes the favorited imported bookmark."
- [ ] **Cross-task regression gate**: Before marking a task as passed, run ALL existing tests (already done via tier 1). But also: after the full run completes, run the entire test suite one more time as a final sanity check — catches interactions that individual task verification missed.
- [ ] **State interaction tests**: Generate tests that exercise multi-step workflows — e.g., "add → favorite → delete → verify favorites count updated", "import → search → export → verify roundtrip."
- [ ] **Environment tests**: Verify the project works in a clean environment — fresh install, no cached state, no leftover files. Catches implicit dependencies.

## Observability

- [x] **Live agent streaming**: Agent messages stream to stdout in real time with styled formatting.
- [x] **Agent session logs**: Full conversation persisted to `otto_logs/<key>/attempt-N-agent.log`.
- [x] **Timing**: Wall-clock time shown per task and for the full run.
- [ ] **Cost tracking**: Log token usage and cost per task from `ResultMessage.total_cost_usd` and `ResultMessage.usage`. Show in `otto status` or `otto logs`.
- [ ] **Test coverage delta**: After each task, measure test coverage change. Did the new tests actually cover the new code? Warns if coverage didn't increase.
- [ ] **`--quiet` mode**: Suppress agent streaming for CI/background use. Only show task pass/fail and summary.

## UX

- [ ] **`otto diff <id>`**: Show the git diff for a specific task's commit. Shorthand for finding the right SHA.
- [ ] **`otto show <id>`**: Show task details — prompt, rubrics, status, attempts, feedback, log snippets.
- [ ] **Parallel tasks**: Run independent tasks concurrently on separate branches. Merge sequentially. Would need dependency detection or user annotation.
- [ ] **Watch mode**: `otto watch` re-runs on file changes (like `features.md` edits). Auto-imports and runs.
- [ ] **Progress bar**: Show overall progress (3/5 tasks) and per-task progress (agent working / verifying / merging).
