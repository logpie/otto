"""Tests for otto.flaky — flaky test detection."""

from otto.flaky import extract_failing_tests, failures_are_baseline_only


class TestExtractFailingTests:
    def test_jest_fail_line(self):
        output = "FAIL __tests__/initialLoad.test.tsx\nPASS __tests__/other.test.tsx"
        assert extract_failing_tests(output) == {"__tests__/initialLoad.test.tsx"}

    def test_jest_verbose_checkmark(self):
        output = "  ✕ render completes under 200ms (560 ms)\n  ✓ other test (5 ms)"
        result = extract_failing_tests(output)
        assert "render completes under 200ms" in result

    def test_pytest_failed(self):
        output = "FAILED tests/test_app.py::TestWeather::test_uv_panel - assert False"
        result = extract_failing_tests(output)
        assert "tests/test_app.py::TestWeather::test_uv_panel" in result

    def test_go_fail(self):
        output = "--- FAIL: TestRetry (0.01s)\nok\tgithub.com/example/pkg\t0.01s"
        assert "TestRetry" in extract_failing_tests(output)

    def test_cargo_fail(self):
        output = "test utils::test_parse ... FAILED\ntest result: FAILED"
        assert "utils::test_parse" in extract_failing_tests(output)

    def test_no_failures(self):
        output = "Tests: 100 passed\nTest Suites: 5 passed"
        assert extract_failing_tests(output) == set()

    def test_empty(self):
        assert extract_failing_tests("") == set()

    def test_multiple_frameworks(self):
        output = (
            "FAIL __tests__/foo.test.tsx\n"
            "FAILED tests/test_bar.py::test_baz\n"
        )
        result = extract_failing_tests(output)
        assert "__tests__/foo.test.tsx" in result
        assert "tests/test_bar.py::test_baz" in result


class TestFailuresAreBaselineOnly:
    def test_no_failures(self):
        assert failures_are_baseline_only(set(), set()) is True

    def test_all_in_baseline(self):
        baseline = {"__tests__/flaky.test.tsx", "test_timing"}
        current = {"__tests__/flaky.test.tsx"}
        assert failures_are_baseline_only(baseline, current) is True

    def test_new_failure(self):
        baseline = {"__tests__/flaky.test.tsx"}
        current = {"__tests__/flaky.test.tsx", "__tests__/new.test.tsx"}
        assert failures_are_baseline_only(baseline, current) is False

    def test_only_new_failures(self):
        baseline = set()
        current = {"__tests__/broken.test.tsx"}
        assert failures_are_baseline_only(baseline, current) is False

    def test_exact_match(self):
        baseline = {"test_a", "test_b"}
        current = {"test_a", "test_b"}
        assert failures_are_baseline_only(baseline, current) is True
