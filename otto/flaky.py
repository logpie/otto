"""Detect pre-existing (flaky) test failures to avoid blaming the coding agent.

Extracts failing test names from test runner output. Framework-agnostic:
jest, pytest, vitest, go test, cargo test, mocha, TAP, unittest, rspec.
"""

import re


def extract_failing_tests(output: str) -> set[str]:
    """Extract a set of failing test identifiers from test runner output.

    Returns short identifiers (file::test or file > test) that can be compared
    across runs. Not perfect — heuristic matching.
    """
    if not output:
        return set()

    failures: set[str] = set()

    # Jest/Vitest: "FAIL __tests__/foo.test.tsx"
    for m in re.finditer(r"^FAIL\s+(\S+\.(?:test|spec)\.\S+)", output, re.MULTILINE):
        failures.add(m.group(1))

    # Jest verbose: "✕ test name (123 ms)"
    for m in re.finditer(r"^\s+[✕✗×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$", output, re.MULTILINE):
        failures.add(m.group(1).strip())

    # Pytest: "FAILED tests/test_foo.py::TestClass::test_method"
    for m in re.finditer(r"^FAILED\s+(\S+::\S+)", output, re.MULTILINE):
        failures.add(m.group(1))

    # Go: "--- FAIL: TestFoo (0.01s)"
    for m in re.finditer(r"^--- FAIL:\s+(\S+)", output, re.MULTILINE):
        failures.add(m.group(1))

    # Cargo: "test result: FAILED" + "test module::test_name ... FAILED"
    for m in re.finditer(r"^test\s+(\S+)\s+\.\.\.\s+FAILED", output, re.MULTILINE):
        failures.add(m.group(1))

    # Mocha: "  N failing" followed by numbered failures "  1) test name"
    if re.search(r"^\s+\d+\s+failing", output, re.MULTILINE):
        for m in re.finditer(r"^\s+\d+\)\s+(.+)$", output, re.MULTILINE):
            failures.add(m.group(1).strip())

    # TAP: "not ok 1 - description"
    for m in re.finditer(r"^not ok\s+\d+\s+(?:-\s+)?(.+)$", output, re.MULTILINE):
        failures.add(m.group(1).strip())

    # Python unittest: "FAIL: test_method (test_module.TestClass)"
    for m in re.finditer(r"^FAIL:\s+(\S+)\s+\((\S+)\)", output, re.MULTILINE):
        failures.add(f"{m.group(2)}.{m.group(1)}")

    # Vitest file-path style: "FAIL  src/foo.test.ts > suite > test name"
    for m in re.finditer(r"^\s*FAIL\s+(\S+)\s+>\s+(.+)$", output, re.MULTILINE):
        failures.add(f"{m.group(1)} > {m.group(2).strip()}")

    # RSpec: "rspec ./spec/foo_spec.rb:42 # description"
    for m in re.finditer(r"^rspec\s+(\S+:\d+)\s+#\s+(.+)$", output, re.MULTILINE):
        failures.add(m.group(1))

    return failures


def failures_are_baseline_only(
    baseline_failures: set[str],
    current_failures: set[str],
) -> bool:
    """Check if all current failures were already failing in baseline.

    Returns True if there are no NEW failures (only pre-existing ones).
    Returns False if there are any failures not in the baseline set.
    """
    if not current_failures:
        return True  # no failures at all
    if not baseline_failures:
        return False  # new failures, no baseline to compare
    new_failures = current_failures - baseline_failures
    return len(new_failures) == 0
