# Test Validation Plan — Deterministic Pre-Implementation Checks

## Root Causes (from real failures)

| # | Root Cause | Example | Cost |
|---|-----------|---------|------|
| 1 | **Test logic errors** — LLM miscounts its own setup operations | Adds 4 tasks, moves 1, asserts `todo == 2` (should be 3) | $1.89 (4 retries) |
| 2 | **Test contamination** — sibling tests run against unimplemented features | Holistic testgen commits all files; verification runs all | $1.43 (2 tasks lost) |
| 3 | **Wrong API assumptions** — tests reference nonexistent fixtures/flags/methods | Tests use `--db` flag that CLI doesn't have; import from conftest that doesn't exist | 1-2 retries per task |
| 4 | **Untestable assertions** — exact string matching on implementation-dependent output | `assert output == "  ○ [todo        ] abc  My Task"` fails on formatting differences | 1 retry per task |

## Scope

**v1 is pytest/Python only.** Otto supports jest/vitest/go/cargo but all observed failures are from Python projects. CLI validation is conditional on detecting a Click/argparse CLI surface — library-only projects skip Check 1.

## Prerequisites: Harden Tier 0

Before adding new checks, fix existing validation gaps:
- `validate_generated_tests()` treats `no_tests` and `all_pass` differently but both can slip through. `no_tests` should be a hard error (test file with zero test functions is useless).
- Skip/xfail-only suites should fail validation — they test nothing.
- Runtime errors during collection that get swallowed as "0 tests" should be detected.

## Checks (all pure AST, deterministic, warning-only unless mechanically provable)

### Check 1: Subprocess command validation (catches Root Cause #3)
**Only when a Click CLI surface is detected with high confidence** (file with `@click.group()` or `@click.command()` decorators found via AST). Skip entirely if no concrete parser surface found — library-only projects, `__main__.py` with raw `sys.argv`, etc. don't get this check.

Parse `subprocess.run(["python", "-m", ...])` calls in the test:
- Module name matches actual project package → error if wrong
- Subcommand matches a registered CLI command → warning if unrecognized (may be the feature being implemented)

**Severity:** error only for provably wrong module name; warning for everything else. Unknown CLI surface → skip check entirely.

### Check 2: Assertion analysis (catches Root Causes #1, #4)
AST-parse each test function:
- **Tautological**: `assert True`, `assert 1 == 1`, `assert x == x` → error
- **No SUT call**: test function with no local calls, no helper/fixture usage, AND no subprocess/import of SUT → warning (not error — SUT call may live in fixture/helper from conftest)
- **Unreachable**: assert after unconditional `return` or `sys.exit()` → error
- **Exact multiline string match**: `assert output == "..."` where the string contains formatting/whitespace → warning (not error — may be an intentional format spec)

**NOT doing:** counting setup operations (add/move/delete) and comparing to numeric assertions. This is too fragile — helpers, loops, fixtures, parametrize make it unreliable. False positives would waste more retries than they save.

### Check 3: Import & fixture validation (catches Root Cause #3)
- `from tests.conftest import X`: check conftest.py actually defines `X` via AST → error
- `from <project>.module import Y`: check module file exists on disk → error for clearly impossible imports (nonexistent relative modules)
- Imports that depend on runtime `sys.path` manipulation or packaging layout → warning/skip (can't resolve statically)
- Fixture references: use `pytest --collect-only` output (already Tier 0) as authority — don't reimplement fixture resolution

**Severity:** error only for provably impossible imports; warning for ambiguous cases.

### Check 4: Anti-patterns (additional from Codex review)
**Errors** (mechanically provable, neuter the test suite):
- Unconditional `pytest.skip()` or `@pytest.mark.skip` (without condition) → error
- `@pytest.mark.xfail` → error (defeats TDD invariant)
- Monkeypatching `subprocess.run` in CLI-style tests (tests that use subprocess to invoke the SUT) → error

**Note:** monkeypatching in library-style tests is only a warning — patching collaborators/fixtures is legitimate setup, not test evasion. Only error when the patched symbol is also the direct call target of assertions.

**Warnings** (heuristic, may have valid uses):
- `except Exception: assert ...` — broad exception swallowing
- Non-hermetic patterns: network calls (`requests.`, `urllib.`), `time.sleep()`, `datetime.now()`, `random.`, hardcoded `/tmp/` or home paths, writes outside `tmp_path`

### Check 5: Test discovery verification
Best-effort hint — not provable without running pytest:
- Verify file follows naming conventions (`test_` prefix, in `tests/` dir) → info if not
- If `pyproject.toml`/`pytest.ini` has `testpaths` that explicitly excludes the file → warning
- Per-file `pytest --collect-only <file>` already done in Tier 0 — this is the real authority
- **Not an error gate** — Tier 0 collection check is the definitive answer

## What we're NOT doing (and why)

- **Spec-to-test coverage mapping**: Too heuristic. Keyword matching between rubric and test names is unreliable and creates false confidence. Better addressed by the existing TDD invariant (tests should fail pre-implementation).
- **Setup operation counting**: Too fragile. Helpers, fixtures, parametrize, loops make simple counting unreliable. Would create more false positives than it catches.
- **Fixture return type analysis**: `None`-returning fixtures are valid (yield fixtures, side-effect fixtures). Not worth the false positive rate.
- **Dynamic smoke tests** (running subprocess --help, instantiating fixtures): These execute arbitrary code, can hang, and introduce flaky behavior. Keep everything static.

## Integration Point

**One shared function, three callers:**

```python
def validate_test_quality(
    test_file: Path,
    project_dir: Path,
    framework: str = "pytest",
) -> list[TestWarning]:
    """Run static quality checks on a generated test file.
    Returns list of warnings with severity and actionable message."""
```

Called from:
1. `run_task()` in runner.py — after per-task testgen validation
2. `run_all()` parallel testgen loop — after holistic testgen validation
3. `run_holistic_testgen()` in pilot MCP tools — after copying tests

On errors: regenerate with specific feedback message.
On warnings: log and proceed.

## Verification

1. `python -m pytest tests/ -x -q` passes
2. Test with a known-bad test file that has tautological assertions → validator catches it
3. Test with a test file that references nonexistent CLI command → validator catches it
4. Test with a valid test file → validator passes with no errors
5. Test with a skip-only test file → Tier 0 rejects it

## Plan Review

### Round 1 — Codex
- [ISSUE] Integration point wrong — only wired into classic runner, not pilot/holistic → fixed: one shared function, three callers
- [ISSUE] Tier 0 not sound — `no_tests`/skip/xfail slip through → fixed: harden Tier 0 as prerequisite
- [ISSUE] Test contamination is orchestration not validation → accepted: contamination is handled by sibling exclusion (already fixed), not by test quality checks. Removed from this plan's scope.
- [ISSUE] Python/Click-only assumptions → fixed: scoped v1 to pytest/Python, CLI checks conditional on detected CLI surface
- [ISSUE] Test discovery bypass possible → fixed: added Check 5 (test discovery verification)
- [ISSUE] Tier 2 dynamic checks aren't static → fixed: removed all dynamic checks, everything is AST-only
- [ISSUE] Setup counting too fragile → accepted: removed, explicitly listed in "what we're NOT doing"
- [ISSUE] Fixture None-return, keyword coverage too heuristic → accepted: removed both, listed reasons
- [ISSUE] Additional anti-patterns (skip/xfail, monkeypatch SUT, non-hermetic) → fixed: added as Check 4

### Round 2 — Codex
- [ISSUE] Test discovery check not truly static for arbitrary test commands → fixed: conservative allowlist (bare pytest only), skip for wrappers
- [ISSUE] "No SUT call" not provable when SUT call is in fixture/helper → fixed: downgraded to warning, only error when truly trivial (no calls, no fixtures)
- [ISSUE] Some anti-patterns are provable, shouldn't be warning-only → fixed: split into error (unconditional skip, xfail, monkeypatch SUT) and warning (sleep, random, broad except)
- [ISSUE] Import validation needs conservative model → fixed: error only for clearly impossible imports, skip for sys.path-dependent cases
- [ISSUE] CLI surface detection must be high-confidence → fixed: only run Check 1 when Click decorators found via AST, skip entirely otherwise

### Round 3 — Codex
- [ISSUE] Discovery check overstated — pytest config controls discovery → fixed: downgraded to info/hint, Tier 0 collection is the real authority
- [ISSUE] "Monkeypatching SUT API" not provable for library tests → fixed: error only for subprocess.run patching in CLI tests; library-test patching is warning only
