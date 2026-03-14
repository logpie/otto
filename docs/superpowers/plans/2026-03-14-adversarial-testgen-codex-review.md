# Codex Review Findings — Adversarial Testgen Plan

## Blockers (must fix before implementation)

1. **Isolation broken**: `run_testgen_agent()` uses `cwd=project_dir` giving agent full repo access.
   Fix: run in temp dir, pass only blackbox context, copy test file to project after generation.

2. **Commit helpers incompatible**: `build_candidate_commit()` resets to `base_sha` which erases test commit.
   Fix: pass `test_commit_sha` to build_candidate_commit, or bypass it entirely for adversarial mode.

3. **Verification may not run test file**: if `test_command=None`, rubric tests never execute.
   Fix: when rubric tests exist, require a test command or run the test file explicitly.

## Improvements (should fix)

4. **Scope to Python-only**: AST extraction, test validation, all pytest-specific. Document this explicitly.

5. **Async interface**: `run_testgen_agent` should be async, called directly from `run_task()`. 
   Logging helpers should be local or in a utility module (not imported from runner → circular dep).

6. **Regenerate flow**: Collection error and all-pass cases need real code and tests, not just comments.

7. **Test fixtures**: Use `tmp_git_repo` fixture from conftest.py, not manual `git init`.
