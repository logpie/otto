# Adversarial Testgen — Design Spec

## Problem

Otto's testgen uses `claude -p` (one-shot) to generate tests. This fails ~30% of the time (prose instead of code, wrong imports, tests that don't actually work). More fundamentally, tests are generated AFTER the coding agent runs, so they tend to confirm the implementation rather than challenge it.

## Solution

Replace one-shot testgen with an **adversarial testgen agent** that writes black-box tests from the rubric BEFORE the coding agent implements the feature. This is TDD by an adversary.

## Core Principle

The testgen agent and coding agent are **adversaries**, not collaborators:
- Testgen writes tests from the SPEC (rubric), not the implementation
- Testgen has never seen the implementation — it doesn't exist yet
- If tests fail after implementation, that's a bug the coding agent must fix
- The coding agent cannot modify the test file

## New Task Execution Flow

For each task in `otto run`:

```
1. TESTGEN AGENT (QA adversary)
   Input: rubric items, project structure, public API
   Output: tests/otto_verify_<key>.py
   Access: read any file, write only tests/, run bash
   Constraint: black-box only — reads signatures/types/docstrings, NOT function bodies

2. TDD CHECK
   Run new tests against current codebase
   Expected: tests FAIL or ERROR (feature not implemented yet)
   If all pass: regenerate once with feedback, then warn loudly and skip

3. COMMIT TESTS
   git add tests/otto_verify_<key>.py
   git commit -m "otto: add rubric tests for task #N"

4. CODING AGENT (implementer)
   Input: task prompt, full codebase (including the test file)
   Output: implementation code
   Constraint: cannot modify tests/otto_verify_<key>.py

5. VERIFICATION
   Run full test suite in clean worktree
   If fail: coding agent retries (can edit any file EXCEPT test file)
   If pass: merge to main
```

## Testgen Agent Details

### What it sees
- Rubric items (the spec)
- Project file tree (`git ls-files`)
- Public API: function/class signatures, type hints, docstrings, CLI --help
- Existing test files (for framework patterns, fixtures, import style)

### What it does NOT see
- Function bodies / implementation details
- The coding agent's diff or conversation
- Internal state or private methods

### Prompt
```
You are a QA engineer writing black-box tests. You have NOT seen
the implementation — it hasn't been written yet.

SPEC (rubric):
{numbered rubric items}

Write tests that verify each spec item. You may:
- Read project files to understand the public API (function signatures,
  CLI commands, type hints, docstrings)
- Read existing tests to follow the project's test patterns
- Run your tests to verify they are syntactically valid and collected

Your tests MUST:
- Test the public interface only (CLI via subprocess, library via imports)
- Fail or error on the current codebase (the feature doesn't exist yet)
- Be independent and hermetic
- Use subprocess.run() for CLI testing, not CliRunner

Do NOT:
- Read function bodies or implementation details
- Write implementation code
- Modify any file outside tests/
```

### Agent configuration
- Uses `query()` from Agent SDK (full agent, not `claude -p`)
- `permission_mode="bypassPermissions"`
- `cwd=project_dir`
- Streams output to stdout (same as coding agent)

## TDD Check

After testgen writes tests, verify the TDD invariant:

```python
result = run test_command + " tests/otto_verify_<key>.py"

if all tests pass:
    # ⚠⚠⚠ LOUD WARNING
    # Regenerate once with feedback
    # If still all pass: warn and skip rubric tests
elif some pass, some fail:
    # OK — passing tests are regression checks, failing test new behavior
elif all fail/error:
    # Perfect — TDD invariant holds
```

Warning on all-pass is loud and unmissable:
```
⚠⚠⚠ WARNING: All rubric tests PASS before implementation — tests may be trivial
    Regenerating tests (attempt 2)...

⚠⚠⚠ WARNING: Tests still pass pre-implementation — rubric tests skipped for task #1
    The coding agent will run WITHOUT adversarial test coverage.
    Review this task's output manually.
```

## Coding Agent Changes

The coding agent prompt gains one constraint:
```
Do NOT modify tests/otto_verify_<key>.py — these are acceptance tests
you must pass, not tests you can change.
```

On retry (verification failed), the prompt says:
```
You may edit any file EXCEPT tests/otto_verify_<key>.py to make all tests pass.
```

## What Changes

| File | Change |
|------|--------|
| `otto/testgen.py` | New `run_testgen_agent()` using Agent SDK `query()` instead of `claude -p`. Keeps existing `generate_tests()` as fallback for tasks without rubrics. |
| `otto/runner.py` | Reorder: testgen agent → TDD check → commit tests → coding agent → verify. Remove concurrent testgen. Add test file protection in coding agent prompt. |
| `otto/verify.py` | No change — already runs all tests in worktree |
| `otto/rubric.py` | No change |
| `otto/cli.py` | No change |

## What Stays The Same

- Rubric generation (`otto add` flow)
- Verification in disposable worktrees
- Branch-per-task, ff-only merge
- Retry mechanics for coding agent
- Integration gate (post-run cross-feature tests)
- `generate_tests()` fallback for tasks without rubrics (still uses `claude -p`)
