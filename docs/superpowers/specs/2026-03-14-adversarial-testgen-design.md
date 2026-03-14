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
- The coding agent cannot modify the test file (mechanically enforced, not just prompt)

## New Task Execution Flow

For each task in `otto run`:

```
1. BUILD BLACK-BOX CONTEXT (orchestrator, not agent)
   Extract from project: file tree, public API stubs (signatures + docstrings only),
   CLI --help output, existing test samples. No function bodies.

2. TESTGEN AGENT (QA adversary)
   Input: rubric items + black-box context (curated, not raw repo access)
   Output: tests/otto_verify_<key>.py
   Access: NO repo access — works only from provided context
   Can run: bash (to verify test syntax/collection only)

3. TEST VALIDATION
   Step A: pytest --collect-only — verify tests are importable and collected
           If collection fails (syntax/import error): regenerate once with error feedback
   Step B: pytest tests/otto_verify_<key>.py — run against current codebase
           Expected: assertion failures (feature not implemented)
           If all pass: regenerate once ("tests are trivial"), then warn loudly and skip

4. COMMIT TESTS
   git add tests/otto_verify_<key>.py
   git commit -m "otto: add rubric tests for task #N"
   Record test file SHA for tamper detection

5. CODING AGENT (implementer)
   Input: task prompt, full codebase (including the test file)
   Output: implementation code
   Prompt: "Do NOT modify tests/otto_verify_<key>.py"

6. TAMPER CHECK
   Verify tests/otto_verify_<key>.py blob SHA matches the committed version
   If modified: restore from committed version, warn loudly

7. VERIFICATION
   Run full test suite in clean worktree
   If fail: coding agent retries (test file restored before each retry)
   If pass: merge to main (squash test commit + implementation into one)

8. GIT HISTORY
   Final merge: base -> single commit (tests + implementation together)
   The test commit is an intermediate artifact, squashed on merge
```

## Black-Box Context Builder

The orchestrator (not the agent) builds a sanitized view of the project. This enforces isolation mechanically — the testgen agent literally cannot see implementation details because they're not in its context.

### `build_blackbox_context(project_dir, rubric)` → str

Extracts:
- **File tree**: `git ls-files` (shows what files exist)
- **Public API stubs**: For each source file, extract only:
  - Class/function signatures (`def foo(x: int) -> str:`)
  - Docstrings
  - Type hints
  - Constants and module-level assignments
  - NOT: function bodies, logic, internal helpers
- **CLI help**: Run `python -m <package> --help` and subcommand help
- **Existing test samples**: First 30 lines of existing test files (import patterns, fixtures)
- **Framework info**: Detected test framework, conftest.py content

Implementation: AST-based extraction for Python (parse the file, walk the tree, emit only signatures + docstrings). For non-Python projects, fall back to reading only type definition files, header files, or interface files.

### Why not give the agent repo access?

Prompt-based constraints ("don't read function bodies") are unreliable — the agent may read implementation code to "understand the API better." Mechanical isolation (curated context) is the only reliable approach. The agent gets exactly what a QA engineer reviewing a spec would get: the interface contract, not the implementation.

## Testgen Agent Details

### What it receives (in prompt)
- Rubric items (the spec)
- Black-box context (from builder above)
- Framework and project structure info

### Agent configuration
- Uses `query()` from Agent SDK
- `permission_mode="bypassPermissions"`
- `cwd` set to a **temp directory** (not the project dir) — agent writes test file there
- Only tool needed: Bash (to run `pytest --collect-only` on the test file for syntax validation)
- Streams output to stdout

### Prompt
```
You are a QA engineer writing black-box tests from a specification.
You have NOT seen the implementation — it hasn't been written yet.
Your job is to write tests that will CATCH BUGS, not confirm correctness.

SPEC (acceptance criteria):
{numbered rubric items}

PROJECT CONTEXT (public interface only):
{black_box_context}

Write a complete pytest test file. For each spec item, write one or more
test functions that verify the behavior.

Your tests MUST:
- Test the public interface only (CLI via subprocess, library via imports)
- Be designed to FAIL on the current codebase (the feature doesn't exist yet)
- Be independent and hermetic (use tmp_path, no shared state)
- Use subprocess.run() for CLI testing, not CliRunner
- Include negative tests (what should NOT happen)

Write the test file to: tests/otto_verify_{key}.py
```

## Test Validation (Two-Phase)

### Phase A: Collection check
```python
result = subprocess.run(
    ["pytest", "--collect-only", test_file],
    cwd=project_dir, capture_output=True, timeout=30,
)
if result.returncode != 0:
    # Tests have syntax/import errors — broken, not failing
    # Regenerate once with the error output
```

This catches: syntax errors, bad imports, missing fixtures, test discovery issues.

### Phase B: TDD check
```python
result = subprocess.run(
    [test_command, test_file],
    cwd=project_dir, capture_output=True, timeout=timeout,
)
passed = count_passed(result)
failed = count_failed(result)
total = passed + failed

if total == 0:
    # No tests collected — broken
elif passed == total:
    # All pass → tests are trivial → regenerate once, then warn
elif failed > 0:
    # Good — tests fail as expected (TDD invariant holds)
```

### Warning output (all-pass case)
```
⚠⚠⚠ WARNING: All rubric tests PASS before implementation — tests may be trivial
    Regenerating tests (attempt 2)...

⚠⚠⚠ WARNING: Tests still pass pre-implementation — rubric tests skipped for task #1
    The coding agent will run WITHOUT adversarial test coverage.
    Review this task's output manually.
```

## Test File Protection (Mechanical)

After committing the test file, record its git blob SHA:

```python
test_sha = subprocess.run(
    ["git", "hash-object", test_file_path],
    capture_output=True, text=True,
).stdout.strip()
```

Before each verification step and before building the candidate commit, check:

```python
current_sha = subprocess.run(
    ["git", "hash-object", test_file_path],
    capture_output=True, text=True,
).stdout.strip()

if current_sha != test_sha:
    # Coding agent modified the test file — restore it
    subprocess.run(["git", "checkout", "HEAD", "--", test_file_path], cwd=project_dir)
    print("⚠ Test file was modified by coding agent — restored from committed version")
```

This is a hard check, not a prompt suggestion.

## Commit/Retry Model

### Branch history during task execution
```
main (base) → test commit → implementation attempt 1
                           → implementation attempt 2 (reset to test commit, retry)
                           → implementation attempt N
```

### On retry
- `git reset --mixed <test_commit_sha>` (preserves test file, discards implementation)
- `git clean -fd` (remove untracked agent files)
- Coding agent runs again from clean state with test file in place

### On success (merge)
- Squash test commit + implementation into one commit
- `git reset --mixed <base_sha>` then `git add -u` + stage new files + `git commit`
- Single clean commit on main: "otto: <prompt> (#id)"

### On failure (all retries exhausted)
- Clean up branch (same as current `_cleanup_task_failure`)
- Test file is NOT preserved on main (branch is deleted)

## What Changes

| File | Change |
|------|--------|
| `otto/testgen.py` | New `build_blackbox_context()` (AST-based stub extraction). New `run_testgen_agent()` using Agent SDK. New `validate_tests()` (two-phase: collect + run). Keep `generate_tests()` as fallback for no-rubric tasks. |
| `otto/runner.py` | Reorder: blackbox context → testgen agent → validate → commit tests → coding agent → tamper check → verify. Track test file SHA. Adjust reset logic to preserve test commit. Squash on merge. |
| `otto/verify.py` | No change |
| `otto/rubric.py` | No change |
| `otto/cli.py` | No change |

## What Stays The Same

- Rubric generation (`otto add` flow)
- Verification in disposable worktrees
- Branch-per-task, ff-only merge
- Integration gate (post-run cross-feature tests)
- `generate_tests()` fallback for tasks without rubrics
- Retry count from config
