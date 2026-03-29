"""Tests for QA verdict validation, parsing, and finalization.

Covers:
- _is_verdict_complete: schema validation, legacy parse trust, coverage checks
- _parse_qa_verdict_json: JSON extraction, legacy fallback, malformed input
- _finalize_qa_result: single-task injection, batch coverage matrix, attribution
- format_batch_spec: multi-task spec formatting
- determine_qa_tier: always returns 1
"""

import json

import pytest

from otto.qa import (
    _finalize_qa_result,
    _is_verdict_complete,
    _parse_qa_verdict_json,
    determine_qa_tier,
    format_batch_spec,
)


# ── _is_verdict_complete ─────────────────────────────────────────────────


class TestIsVerdictComplete:
    def test_valid_verdict_returns_true(self):
        verdict = {
            "must_passed": True,
            "must_items": [{"status": "pass", "criterion": "works"}],
        }
        assert _is_verdict_complete(verdict) is True

    def test_must_passed_missing_returns_false(self):
        verdict = {"must_items": []}
        assert _is_verdict_complete(verdict) is False

    def test_must_passed_not_bool_returns_false(self):
        verdict = {"must_passed": "yes", "must_items": []}
        assert _is_verdict_complete(verdict) is False

    def test_must_items_missing_returns_false(self):
        verdict = {"must_passed": True}
        assert _is_verdict_complete(verdict) is False

    def test_must_items_not_list_returns_false(self):
        verdict = {"must_passed": True, "must_items": "none"}
        assert _is_verdict_complete(verdict) is False

    def test_legacy_parse_pass_returns_false(self):
        """Legacy parses lack structured evidence — pass is not trusted."""
        verdict = {
            "must_passed": True,
            "must_items": [],
            "_legacy_parse": True,
        }
        assert _is_verdict_complete(verdict) is False

    def test_legacy_parse_fail_returns_true(self):
        """Legacy parses are trusted for fail verdicts."""
        verdict = {
            "must_passed": False,
            "must_items": [],
            "_legacy_parse": True,
        }
        assert _is_verdict_complete(verdict) is True

    def test_pass_with_insufficient_must_items_returns_false(self):
        verdict = {
            "must_passed": True,
            "must_items": [{"status": "pass"}],
        }
        assert _is_verdict_complete(verdict, expected_must_count=3) is False

    def test_pass_with_zero_expected_and_empty_items_returns_true(self):
        verdict = {
            "must_passed": True,
            "must_items": [],
        }
        assert _is_verdict_complete(verdict, expected_must_count=0) is True

    def test_fail_skips_coverage_check(self):
        """Failures don't need full coverage — the fail itself is the signal."""
        verdict = {
            "must_passed": False,
            "must_items": [],
        }
        assert _is_verdict_complete(verdict, expected_must_count=5) is True

    def test_pass_with_exact_expected_count(self):
        verdict = {
            "must_passed": True,
            "must_items": [{"status": "pass"}, {"status": "pass"}],
        }
        assert _is_verdict_complete(verdict, expected_must_count=2) is True

    def test_must_passed_none_returns_false(self):
        verdict = {"must_passed": None, "must_items": []}
        assert _is_verdict_complete(verdict) is False

    def test_must_passed_int_returns_false(self):
        verdict = {"must_passed": 1, "must_items": []}
        assert _is_verdict_complete(verdict) is False


# ── _parse_qa_verdict_json ───────────────────────────────────────────────


class TestParseQaVerdictJson:
    def test_extracts_json_from_markdown_fence(self):
        report = 'Some text\n```json\n{"must_passed": true, "must_items": [{"status": "pass"}]}\n```\nMore text'
        result = _parse_qa_verdict_json(report)
        assert result["must_passed"] is True
        assert len(result["must_items"]) == 1
        assert "_legacy_parse" not in result

    def test_extracts_json_from_raw_text(self):
        report = 'Here is the verdict: {"must_passed": false, "must_items": []} end'
        result = _parse_qa_verdict_json(report)
        assert result["must_passed"] is False

    def test_legacy_pass_from_text_pattern(self):
        report = "All items verified.\nQA VERDICT: PASS\nLooks good."
        result = _parse_qa_verdict_json(report)
        assert result["must_passed"] is True
        assert result["_legacy_parse"] is True
        assert result["must_items"] == []

    def test_legacy_fail_from_text_pattern(self):
        report = "Item 1 broken.\nQA VERDICT: FAIL\nNeeds fix."
        result = _parse_qa_verdict_json(report)
        assert result["must_passed"] is False
        assert result["_legacy_parse"] is True

    def test_legacy_fail_overrides_pass_pattern(self):
        """If both PASS and FAIL are present, FAIL wins."""
        report = "QA VERDICT: PASS\nActually wait, QA VERDICT: FAIL"
        result = _parse_qa_verdict_json(report)
        assert result["must_passed"] is False

    def test_empty_input_returns_legacy(self):
        result = _parse_qa_verdict_json("")
        assert result["_legacy_parse"] is True
        assert result["must_passed"] is False

    def test_malformed_json_falls_back_to_legacy(self):
        report = '```json\n{broken json\n```'
        result = _parse_qa_verdict_json(report)
        assert result["_legacy_parse"] is True

    def test_json_without_must_passed_falls_back_to_legacy(self):
        report = '```json\n{"status": "ok"}\n```'
        result = _parse_qa_verdict_json(report)
        assert result["_legacy_parse"] is True

    def test_natural_language_pass_patterns(self):
        for phrase in ["all must items passed", "all criteria pass", "ready to merge"]:
            result = _parse_qa_verdict_json(f"Checked everything. {phrase}.")
            assert result["must_passed"] is True, f"Failed for: {phrase}"
            assert result["_legacy_parse"] is True

    def test_verdict_fail_uppercase_detected(self):
        report = "VERDICT: FAIL — regression found"
        result = _parse_qa_verdict_json(report)
        assert result["must_passed"] is False


# ── _finalize_qa_result ──────────────────────────────────────────────────


class TestFinalizeQaResultSingleTask:
    """Single task (len==1): injects task_key, validates expected_must_count."""

    def _make_task(self, key="task-1", num_must=2, num_should=0):
        spec = []
        for i in range(num_must):
            spec.append({"text": f"must item {i+1}", "binding": "must"})
        for i in range(num_should):
            spec.append({"text": f"should item {i+1}", "binding": "should"})
        return {"key": key, "prompt": "do something", "spec": spec}

    def test_injects_task_key_into_must_items(self):
        task = self._make_task(key="abc123", num_must=1)
        qa_result = {
            "must_passed": True,
            "verdict": {
                "must_passed": True,
                "must_items": [{"spec_id": 1, "criterion": "works", "status": "pass"}],
            },
            "raw_report": "",
            "cost_usd": 0.1,
        }
        result = _finalize_qa_result(qa_result, [task])
        assert result["verdict"]["must_items"][0]["task_key"] == "abc123"

    def test_pass_with_insufficient_must_items_forced_fail(self):
        task = self._make_task(num_must=3)
        qa_result = {
            "must_passed": True,
            "verdict": {
                "must_passed": True,
                "must_items": [{"status": "pass"}],  # only 1 of 3
            },
            "raw_report": "",
            "cost_usd": 0.1,
        }
        result = _finalize_qa_result(qa_result, [task])
        assert result["must_passed"] is False

    def test_pass_with_full_must_items(self):
        task = self._make_task(num_must=2)
        qa_result = {
            "must_passed": True,
            "verdict": {
                "must_passed": True,
                "must_items": [
                    {"spec_id": 1, "status": "pass"},
                    {"spec_id": 2, "status": "pass"},
                ],
            },
            "raw_report": "",
            "cost_usd": 0.2,
        }
        result = _finalize_qa_result(qa_result, [task])
        assert result["must_passed"] is True

    def test_infrastructure_error_passthrough(self):
        task = self._make_task()
        qa_result = {
            "must_passed": None,
            "verdict": None,
            "raw_report": "",
            "cost_usd": 0.0,
            "infrastructure_error": True,
        }
        result = _finalize_qa_result(qa_result, [task])
        assert result["infrastructure_error"] is True
        # must_passed is None (passthrough) — not coerced to bool for single task
        assert not result["must_passed"]

    def test_failed_task_keys_from_failed_items(self):
        task = self._make_task(key="task-x", num_must=2)
        qa_result = {
            "must_passed": False,
            "verdict": {
                "must_passed": False,
                "must_items": [
                    {"spec_id": 1, "status": "pass", "task_key": "task-x"},
                    {"spec_id": 2, "status": "fail", "task_key": "task-x"},
                ],
            },
            "raw_report": "",
            "cost_usd": 0.1,
        }
        result = _finalize_qa_result(qa_result, [task])
        assert "task-x" in result["failed_task_keys"]


class TestFinalizeQaResultBatch:
    """Multi task (len>1): coverage matrix, attribution, failed_task_keys."""

    def _make_tasks(self):
        return [
            {
                "key": "t1",
                "prompt": "add feature A",
                "spec": [
                    {"text": "A works", "binding": "must"},
                    {"text": "A looks good", "binding": "should"},
                ],
            },
            {
                "key": "t2",
                "prompt": "add feature B",
                "spec": [
                    {"text": "B works", "binding": "must"},
                    {"text": "B is fast", "binding": "must"},
                ],
            },
        ]

    def test_full_coverage_passes(self):
        tasks = self._make_tasks()
        qa_result = {
            "must_passed": True,
            "verdict": {
                "must_passed": True,
                "must_items": [
                    {"task_key": "t1", "spec_id": 1, "status": "pass"},
                    {"task_key": "t2", "spec_id": 1, "status": "pass"},
                    {"task_key": "t2", "spec_id": 2, "status": "pass"},
                ],
                "integration_findings": [],
                "regressions": [],
                "test_suite_passed": True,
            },
            "raw_report": "",
            "cost_usd": 0.5,
        }
        result = _finalize_qa_result(qa_result, tasks)
        assert result["must_passed"] is True
        assert result["failed_task_keys"] == []

    def test_missing_coverage_forces_fail(self):
        tasks = self._make_tasks()
        qa_result = {
            "must_passed": True,
            "verdict": {
                "must_passed": True,
                "must_items": [
                    {"task_key": "t1", "spec_id": 1, "status": "pass"},
                    # Missing t2/spec_id=1 and t2/spec_id=2
                ],
                "integration_findings": [],
                "regressions": [],
                "test_suite_passed": True,
            },
            "raw_report": "",
            "cost_usd": 0.3,
        }
        result = _finalize_qa_result(qa_result, tasks)
        assert result["must_passed"] is False
        assert "coverage_error" in result["verdict"]
        coverage_error = result["verdict"]["coverage_error"]
        assert coverage_error["expected_count"] == 3  # 1 from t1 + 2 from t2
        assert coverage_error["actual_count"] == 1
        assert "t2" in result["failed_task_keys"]

    def test_failed_must_item_attribution(self):
        tasks = self._make_tasks()
        qa_result = {
            "must_passed": False,
            "verdict": {
                "must_passed": False,
                "must_items": [
                    {"task_key": "t1", "spec_id": 1, "status": "pass"},
                    {"task_key": "t2", "spec_id": 1, "status": "fail"},
                    {"task_key": "t2", "spec_id": 2, "status": "pass"},
                ],
                "integration_findings": [],
                "regressions": [],
                "test_suite_passed": True,
            },
            "raw_report": "",
            "cost_usd": 0.4,
        }
        result = _finalize_qa_result(qa_result, tasks)
        assert result["must_passed"] is False
        assert "t2" in result["failed_task_keys"]
        assert "t1" not in result["failed_task_keys"]

    def test_integration_failure_adds_to_failed_keys(self):
        tasks = self._make_tasks()
        qa_result = {
            "must_passed": True,
            "verdict": {
                "must_passed": True,
                "must_items": [
                    {"task_key": "t1", "spec_id": 1, "status": "pass"},
                    {"task_key": "t2", "spec_id": 1, "status": "pass"},
                    {"task_key": "t2", "spec_id": 2, "status": "pass"},
                ],
                "integration_findings": [
                    {"status": "fail", "description": "conflict", "tasks_involved": ["t1", "t2"]},
                ],
                "regressions": [],
                "test_suite_passed": True,
            },
            "raw_report": "",
            "cost_usd": 0.5,
        }
        result = _finalize_qa_result(qa_result, tasks)
        assert result["must_passed"] is False
        assert "t1" in result["failed_task_keys"]
        assert "t2" in result["failed_task_keys"]

    def test_regressions_force_fail(self):
        tasks = self._make_tasks()
        qa_result = {
            "must_passed": True,
            "verdict": {
                "must_passed": True,
                "must_items": [
                    {"task_key": "t1", "spec_id": 1, "status": "pass"},
                    {"task_key": "t2", "spec_id": 1, "status": "pass"},
                    {"task_key": "t2", "spec_id": 2, "status": "pass"},
                ],
                "integration_findings": [],
                "regressions": ["existing tests broke"],
                "test_suite_passed": True,
            },
            "raw_report": "",
            "cost_usd": 0.5,
        }
        result = _finalize_qa_result(qa_result, tasks)
        assert result["must_passed"] is False

    def test_infrastructure_error_passthrough(self):
        tasks = self._make_tasks()
        qa_result = {
            "must_passed": None,
            "verdict": None,
            "raw_report": "",
            "cost_usd": 0.0,
            "infrastructure_error": True,
        }
        result = _finalize_qa_result(qa_result, tasks)
        assert result["infrastructure_error"] is True
        assert result["must_passed"] is False


# ── format_batch_spec ────────────────────────────────────────────────────


class TestFormatBatchSpec:
    def test_single_task_formatting(self):
        tasks = [{
            "key": "k1",
            "id": "1",
            "prompt": "add button",
            "spec": [
                {"text": "button exists", "binding": "must"},
                {"text": "button is blue", "binding": "should"},
            ],
        }]
        result = format_batch_spec(tasks)
        assert "task_key: k1" in result
        assert "[must]" in result
        assert "[should]" in result
        assert "button exists" in result
        assert "Cross-Task Integration" in result

    def test_multi_task_includes_all_tasks(self):
        tasks = [
            {
                "key": "k1", "id": "1", "prompt": "feature A",
                "spec": [{"text": "A works", "binding": "must"}],
            },
            {
                "key": "k2", "id": "2", "prompt": "feature B",
                "spec": [{"text": "B works", "binding": "must"}],
            },
        ]
        result = format_batch_spec(tasks)
        assert "task_key: k1" in result
        assert "task_key: k2" in result
        assert "feature A" in result
        assert "feature B" in result

    def test_non_verifiable_gets_marker(self):
        tasks = [{
            "key": "k1", "id": "1", "prompt": "style it",
            "spec": [{"text": "looks nice", "binding": "must", "verifiable": False}],
        }]
        result = format_batch_spec(tasks)
        assert "\u25c8" in result  # ◈ marker


# ── determine_qa_tier ────────────────────────────────────────────────────


class TestDetermineQaTier:
    def test_always_returns_1(self):
        task = {"key": "t1"}
        spec = [{"text": "works", "binding": "must"}]
        diff_info = {"files": ["src/app.py"], "full_diff": ""}
        assert determine_qa_tier(task, spec, attempt=1, diff_info=diff_info) == 1

    def test_returns_1_regardless_of_inputs(self):
        assert determine_qa_tier({}, [], attempt=0, diff_info={}) == 1
        assert determine_qa_tier(
            {"key": "big"},
            [{"text": f"item {i}", "binding": "must"} for i in range(50)],
            attempt=3,
            diff_info={"files": ["a.py", "b.py", "c.py"], "full_diff": "lots of diff"},
        ) == 1

    def test_writes_log_when_log_dir_provided(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        determine_qa_tier(
            {"key": "logged"},
            [{"text": "x", "binding": "must"}],
            attempt=1,
            diff_info={"files": ["a.py"]},
            log_dir=log_dir,
        )
        log_file = log_dir / "qa-tier.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "logged" in content
        assert "tier: 1" in content
