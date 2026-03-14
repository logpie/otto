# Otto — TODO

## Rubric & Test Generation

- [ ] **Smarter rubric generation**: Give the LLM more project context — read the actual source files referenced in the task, not just file tree. Current `_gather_project_context` reads up to 5 files/100 lines; may miss the relevant ones.
- [ ] **Rubric-to-test quality**: Generated tests sometimes have import errors or test the wrong thing. Explore: run a validation pass (import check, quick syntax run) before committing. Or use a two-step: generate test, then have a second LLM call review/fix it.
- [ ] **Framework-specific test patterns**: Current generation prompt is generic. Could provide framework-specific examples (pytest fixtures, jest mocking patterns, etc.) for higher quality output.
- [ ] **User-provided test examples**: Let users point to a "golden" test file as a style reference, beyond what `_read_existing_tests` auto-detects.
- [ ] **Rubric refinement loop**: If rubric-generated tests fail to parse or import, auto-retry with the error message fed back to the LLM (similar to how agent retries work).
- [ ] **Test deduplication**: When rubric tests overlap with existing project tests, detect and skip duplicates instead of testing the same thing twice.
