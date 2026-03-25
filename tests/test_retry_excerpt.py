"""Tests for otto.retry_excerpt — failure extraction from test output."""

from otto.retry_excerpt import build_retry_excerpt


class TestBuildRetryExcerpt:
    def test_small_output_returned_as_is(self):
        text = "FAILED test_foo.py - assert 1 == 2\n1 failed, 5 passed"
        result = build_retry_excerpt(text)
        assert result == text

    def test_empty_input(self):
        assert build_retry_excerpt("") == ""
        assert build_retry_excerpt(None) is None

    def test_strips_ansi_escapes(self):
        text = "\x1b[31mFAILED\x1b[0m test_foo.py\n1 failed"
        result = build_retry_excerpt(text)
        assert "\x1b[" not in result
        assert "FAILED" in result

    def test_preserves_failure_block(self):
        """Failure lines and their context are preserved."""
        lines = (
            ["PASS test_a.py"] * 100
            + ["FAILED test_b.py::test_thing - AssertionError: assert 1 == 2"]
            + ["    def test_thing():"]
            + [">       assert 1 == 2"]
            + ["E       assert 1 == 2"]
            + ["PASS test_c.py"] * 100
            + ["=== 1 failed, 200 passed ==="]
        )
        text = "\n".join(lines)
        result = build_retry_excerpt(text, max_chars=5000)
        assert "FAILED test_b.py" in result
        assert "assert 1 == 2" in result
        assert "1 failed, 200 passed" in result

    def test_drops_passing_test_noise(self):
        """PASS lines should be collapsed, not kept individually."""
        lines = (
            ["PASS __tests__/foo.test.tsx"] * 50
            + ["FAILED test_bar.py - assert False"]
            + ["=== 1 failed, 50 passed ==="]
        )
        text = "\n".join(lines)
        result = build_retry_excerpt(text, max_chars=3000)
        # Individual PASS lines should NOT appear
        assert "PASS __tests__/foo" not in result
        # But failure and summary must
        assert "FAILED test_bar.py" in result
        assert "1 failed, 50 passed" in result

    def test_go_ok_lines_are_treated_as_pass_noise(self):
        lines = (
            ["ok\tgithub.com/example/project/pkg/a\t0.012s"] * 40
            + [
                "--- FAIL: TestRunnerRetries (0.00s)",
                "    retry_test.go:42: expected retry excerpt to keep the diff",
                "FAIL\tgithub.com/example/project/pkg/retry\t0.021s",
            ]
            + ["ok\tgithub.com/example/project/pkg/z\t0.008s"] * 40
        )
        text = "\n".join(lines)
        result = build_retry_excerpt(text, max_chars=4000)

        assert "--- FAIL: TestRunnerRetries" in result
        assert "FAIL\tgithub.com/example/project/pkg/retry" in result
        assert "ok\tgithub.com/example/project/pkg/a" not in result
        assert "ok\tgithub.com/example/project/pkg/z" not in result

    def test_drops_console_warnings(self):
        """React act() warnings and console spam should be collapsed."""
        lines = [
            "FAILED test_app.py - assert False",
            "console.error",
            "  An update to WeatherApp inside a test was not wrapped in act(...)",
            "  When testing, code that causes React state updates...",
            "  act(() => {",
            "    /* fire events that update state */",
            "  });",
            "  This ensures that you're testing the behavior...",
            "=== 1 failed ===",
        ]
        # Repeat warnings 20 times
        text = "\n".join(lines[:1] + lines[1:8] * 20 + lines[8:])
        result = build_retry_excerpt(text, max_chars=3000)
        assert "FAILED test_app.py" in result
        assert "1 failed" in result
        # Should NOT have 20 copies of the warning
        assert result.count("An update to WeatherApp") <= 2

    def test_preserves_real_console_error_inside_failure_window(self):
        lines = [
            "FAILED test_backend.py - assert False",
            "console.error Error: Backend returned 500",
            "    at fetchWeather (/app/backend.ts:18:11)",
            "=== 1 failed ===",
        ]
        result = build_retry_excerpt("\n".join(lines), max_chars=3000)

        assert "console.error Error: Backend returned 500" in result
        assert "fetchWeather" in result
        assert "1 failed" in result

    def test_preserves_pytest_summary(self):
        lines = (
            ["test_a PASSED"] * 50
            + ["FAILED test_b - AssertionError"]
            + ["=========================== short test summary info ============================"]
            + ["FAILED test_b - AssertionError: assert 'UV Index' in content"]
            + ["======================== 1 failed, 50 passed in 14.25s ========================"]
        )
        text = "\n".join(lines)
        result = build_retry_excerpt(text, max_chars=3000)
        assert "short test summary" in result
        assert "1 failed, 50 passed" in result

    def test_preserves_jest_summary(self):
        lines = (
            ["PASS __tests__/a.test.tsx"] * 40
            + ["FAIL __tests__/b.test.tsx"]
            + ["  ● test suite failed"]
            + ["Test Suites: 1 failed, 40 passed, 41 total"]
            + ["Tests:       3 failed, 800 passed, 803 total"]
            + ["Ran all test suites."]
        )
        text = "\n".join(lines)
        result = build_retry_excerpt(text, max_chars=3000)
        assert "FAIL __tests__/b" in result
        assert "Test Suites:" in result
        assert "Ran all test suites" in result

    def test_extends_failure_window_for_large_assertion_diff(self):
        diff_lines = [f"- expected line {i}" for i in range(50)] + [f"+ actual line {i}" for i in range(50)]
        lines = [
            "FAILED test_snapshot.py::test_render - AssertionError: snapshot mismatch",
            "    def test_render():",
            ">       assert expected == actual",
            "E       AssertionError: snapshot mismatch",
            *diff_lines,
            "======================== 1 failed, 12 passed in 0.30s ========================",
        ]
        text = "\n".join(lines)
        result = build_retry_excerpt(text, max_chars=12000)

        assert "FAILED test_snapshot.py::test_render" in result
        assert "- expected line 0" in result
        assert "+ actual line 49" in result
        assert "+ actual line 24" in result
        assert "1 failed, 12 passed" in result

    def test_extends_failure_window_for_plain_unindented_failure_text(self):
        lines = (
            ["PASS __tests__/a.test.tsx"] * 40
            + ["FAIL __tests__/b.test.tsx", "  ● renders mismatch"]
            + [f"Received line {i}" for i in range(100)]
            + ["Test Suites: 1 failed, 40 passed, 41 total", "Ran all test suites."]
        )
        result = build_retry_excerpt("\n".join(lines), max_chars=12000)

        assert "FAIL __tests__/b.test.tsx" in result
        assert "Received line 0" in result
        assert "Received line 99" in result
        assert result.count("Received line ") == 100
        assert "Test Suites: 1 failed, 40 passed, 41 total" in result

    def test_hard_cap_applied(self):
        """Even with many failures, output is bounded."""
        lines = [f"FAILED test_{i}.py - assert False" for i in range(500)]
        lines.append("500 failed")
        text = "\n".join(lines)
        result = build_retry_excerpt(text, max_chars=5000)
        assert len(result) <= 6000  # some slack for the truncation message

    def test_no_failures_keeps_head_and_tail(self):
        """When no failure anchors found, keep head + tail."""
        lines = [f"line {i}: some output" for i in range(500)]
        text = "\n".join(lines)
        result = build_retry_excerpt(text, max_chars=5000)
        assert "line 0:" in result
        assert "line 499:" in result
        assert "omitted" in result

    def test_real_world_weatherapp_scenario(self):
        """Simulate the actual weatherapp 847K output."""
        # pytest section: 1 failure in 109 tests
        pytest_lines = [
            "============================= test session starts ==============================",
            "collected 109 items",
        ]
        pytest_lines += [f"tests/test_weather_app.py::test_{i} PASSED" for i in range(108)]
        pytest_lines += [
            "",
            "    def test_uv_index_panel(self):",
            "        content = _read('components', 'WeatherDetails.tsx')",
            ">       assert 'UV Index' in content",
            "E       AssertionError: assert 'UV Index' in '\"use client\";...'",
            "",
            "tests/test_weather_app.py:176: AssertionError",
            "=========================== short test summary info ============================",
            "FAILED tests/test_weather_app.py::TestWeatherDetails::test_uv_index_panel",
            "======================== 1 failed, 108 passed in 14.25s ========================",
        ]
        # jest section: 975 tests all pass + massive console warning spam
        jest_lines = []
        for i in range(43):
            jest_lines.append(f"PASS __tests__/test_{i}.test.tsx")
            # Add console warning spam (the act() warnings)
            for _ in range(10):
                jest_lines += [
                    "    console.error",
                    "      An update to WeatherApp inside a test was not wrapped in act(...)",
                    "      at node_modules/react-dom/cjs/react-dom-client.development.js:18758:19",
                    "      at runWithFiberInDEV (node_modules/react-dom/cjs/react-dom-client.development.js:874:13)",
                ]
        jest_lines += [
            "Test Suites: 43 passed, 43 total",
            "Tests:       975 passed, 975 total",
            "Ran all test suites.",
        ]

        text = "\n".join(pytest_lines + jest_lines)
        assert len(text) > 100_000  # confirm it's large

        result = build_retry_excerpt(text)
        assert len(result) <= 12_000  # bounded

        # Must preserve the actual failure info
        assert "UV Index" in result
        assert "AssertionError" in result
        assert "test_uv_index_panel" in result
        assert "1 failed, 108 passed" in result

        # Must preserve jest summary
        assert "975 passed" in result

        # Must NOT have 43 PASS lines or 430 console.error blocks
        assert result.count("PASS __tests__") <= 2
        assert result.count("console.error") <= 2
