# Otto — TODO

## Rubric & Test Generation

- [ ] **Smarter rubric generation**: Give the LLM more project context — read the actual source files referenced in the task, not just file tree. Current `_gather_project_context` reads up to 5 files/100 lines; may miss the relevant ones.
- [ ] **Rubric-to-test quality**: Generated tests sometimes have import errors or test the wrong thing. Explore: run a validation pass (import check, quick syntax run) before committing. Or use a two-step: generate test, then have a second LLM call review/fix it.
- [ ] **Framework-specific test patterns**: Current generation prompt is generic. Could provide framework-specific examples (pytest fixtures, jest mocking patterns, etc.) for higher quality output.
- [ ] **User-provided test examples**: Let users point to a "golden" test file as a style reference, beyond what `_read_existing_tests` auto-detects.
- [ ] **Rubric refinement loop**: If rubric-generated tests fail to parse or import, auto-retry with the error message fed back to the LLM (similar to how agent retries work).
- [ ] **Test deduplication**: When rubric tests overlap with existing project tests, detect and skip duplicates instead of testing the same thing twice.

## Adversarial Testing

- [ ] **Anti-pattern tests**: Generated tests should include cases that verify the code does NOT do the wrong thing — e.g. "search does not return unrelated bookmarks", "delete does not affect other records", "invalid input does not silently succeed". The rubric prompt should explicitly ask for "what should NOT happen" alongside "what should happen".
- [ ] **Boundary and abuse cases**: Generate tests for boundaries (empty input, huge input, unicode, special chars, None where unexpected) and misuse (calling methods in wrong order, concurrent access, re-entrance).
- [ ] **Regression-style rubrics**: When a task modifies existing code, auto-generate rubric items that verify existing behavior is preserved — not just that new behavior works. "Search still works after adding tags" type checks.
- [ ] **Security anti-patterns**: For tasks involving user input, file paths, or external data, generate tests that verify common vulnerabilities don't exist — injection, path traversal, unescaped output, etc.

## Integration Testing

- [ ] **Cross-task integration tests**: When multiple tasks touch the same module, generate integration tests that verify the features work together — e.g. "search works on bookmarks that have tags", "export includes favorite status".
- [ ] **End-to-end behavioral tests**: Generate tests that exercise the full stack (CLI → store → file) rather than just unit-testing individual methods. Verify the system works as a user would use it.
- [ ] **State interaction tests**: Generate tests that verify features interact correctly with shared state — e.g. "adding a bookmark, favoriting it, then exporting includes the favorite", "deleting a tagged bookmark updates tag counts".

## Observability

- [ ] **Agent session logs**: Capture the full agent conversation (messages, tool calls, file edits) to `otto_logs/<key>/attempt-N-agent.log`. Currently only verification output is logged — if a task fails, there's no way to see what the agent actually did without `claude --resume <session_id>`.
- [ ] **Cost tracking**: Log token usage and cost per task from `ResultMessage.total_cost_usd` and `ResultMessage.usage`. Show in `otto status` or `otto logs`.
- [ ] **Timing**: Log wall-clock time per attempt (agent duration, testgen duration, verify duration) for identifying bottlenecks.
