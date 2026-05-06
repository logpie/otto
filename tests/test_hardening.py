"""Hardening regression tests retained after Phase C.3 deletion.

The v3 pipeline (build_agentic_v3, run_certify_fix_loop) is gone;
this file keeps only cross-cutting tests that survive: marker /
parser hardening, certifier story dedup, target metrics, resume
infrastructure that does not exercise the deleted entry points.
"""

import json
import os
from unittest.mock import patch

import pytest

from otto.testing import _subprocess_env

# `tmp_git_repo` fixture comes from tests/conftest.py.


# -- Test: STORY_RESULT with pipes in summary --

class TestMarkerParsingHardening:
    def test_story_result_ids_may_contain_spaces(self):
        from otto.markers import parse_certifier_markers

        parsed = parse_certifier_markers(
            "STORIES_TESTED: 1\n"
            "STORIES_PASSED: 1\n"
            "STORY_RESULT: CLI printed the expected greeting | PASS | "
            "claim=CLI works | observed_result=stdout matched | summary=CLI passed\n"
            "VERDICT: PASS\n"
        )

        assert parsed.verdict_pass is True
        assert parsed.stories_tested == 1
        assert [story["story_id"] for story in parsed.stories] == ["CLI printed the expected greeting"]

    def test_exact_marker_tokens_only(self):
        from otto.markers import parse_certifier_markers

        parsed = parse_certifier_markers(
            "VERDICT: BYPASS\n"
            "STORY_RESULT: fake | NOTPASS | ignore me\n"
            "STORY_RESULT: real | PASS | works\n"
            "VERDICT: PASS\n"
        )

        assert parsed.verdict_seen is True
        assert [story["story_id"] for story in parsed.stories] == ["real"]

    def test_frontmatter_and_blockquotes_are_ignored(self):
        from otto.markers import parse_certifier_markers

        parsed = parse_certifier_markers(
            "---\n"
            "VERDICT: FAIL\n"
            "STORY_RESULT: fake | FAIL | hidden in frontmatter\n"
            "---\n"
            "> VERDICT: FAIL\n"
            "> STORY_RESULT: quoted | FAIL | hidden in quote\n"
            "STORY_RESULT: real | PASS | visible marker\n"
            "VERDICT: PASS\n"
        )

        assert parsed.verdict_pass is True
        assert [story["story_id"] for story in parsed.stories] == ["real"]


# -- Test: BuildResult.rounds reflects actual count --

class TestSubprocessEnv:
    """_subprocess_env should set the env vars that suppress agent-side
    prompts and nested CC detection."""

    def test_required_env_vars(self):
        env = _subprocess_env()
        assert env["CLAUDECODE"] == ""
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["CI"] == "true"

    def test_parent_env_is_allowlisted(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "sk-allowedsecret1234567890")
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "dont-pass-through")
        monkeypatch.setenv("CUSTOM_PASSWORD", "dont-pass-through")

        env = _subprocess_env()

        assert env["OPENAI_API_KEY"] == "sk-allowedsecret1234567890"
        assert env["PATH"]
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "CUSTOM_PASSWORD" not in env

    def test_project_runtime_env_is_allowlisted(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@localhost:55432/app")
        monkeypatch.setenv("DATABASE_URL_REPLICA", "postgres://user:pass@localhost:55432/app")
        monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "project.settings")
        monkeypatch.setenv("SALEOR_API_URL", "http://127.0.0.1:8000/graphql/")
        monkeypatch.setenv("CUSTOM_PASSWORD", "dont-pass-through")

        env = _subprocess_env()

        assert env["DATABASE_URL"] == "postgres://user:pass@localhost:55432/app"
        assert env["DATABASE_URL_REPLICA"] == "postgres://user:pass@localhost:55432/app"
        assert env["DJANGO_SETTINGS_MODULE"] == "project.settings"
        assert env["SALEOR_API_URL"] == "http://127.0.0.1:8000/graphql/"
        assert "CUSTOM_PASSWORD" not in env

    def test_current_otto_venv_is_not_child_agent_default(self, tmp_path, monkeypatch):
        otto_bin = tmp_path / "otto-venv" / "bin"
        user_bin = tmp_path / "user-bin"
        otto_bin.mkdir(parents=True)
        user_bin.mkdir()
        monkeypatch.setattr("otto.testing.sys.executable", str(otto_bin / "python"))
        monkeypatch.setenv("VIRTUAL_ENV", str(otto_bin.parent))
        monkeypatch.setenv("PATH", f"{otto_bin}{os.pathsep}{user_bin}")

        env = _subprocess_env(tmp_path)

        assert str(otto_bin) not in env["PATH"].split(os.pathsep)
        assert str(user_bin) in env["PATH"].split(os.pathsep)
        assert "VIRTUAL_ENV" not in env

    def test_project_venv_is_child_agent_default(self, tmp_path, monkeypatch):
        otto_bin = tmp_path / "otto-venv" / "bin"
        project_bin = tmp_path / "project" / ".venv" / "bin"
        user_bin = tmp_path / "user-bin"
        otto_bin.mkdir(parents=True)
        project_bin.mkdir(parents=True)
        user_bin.mkdir()
        monkeypatch.setattr("otto.testing.sys.executable", str(otto_bin / "python"))
        monkeypatch.setenv("VIRTUAL_ENV", str(otto_bin.parent))
        monkeypatch.setenv("PATH", f"{otto_bin}{os.pathsep}{user_bin}")

        env = _subprocess_env(project_bin.parents[1])

        path = env["PATH"].split(os.pathsep)
        assert path[0] == str(project_bin)
        assert str(otto_bin) not in path
        assert env["VIRTUAL_ENV"] == str(project_bin.parent)


# -- Test: Empty story_id is rejected --

class TestCostAccumulation:
    """Provider result costs are cumulative for a run and must not be re-summed."""

    @pytest.mark.asyncio
    async def test_cost_uses_last_cumulative_total(self):
        """When a provider yields multiple ResultMessages, take the cumulative max."""
        from otto.agent import (
            AssistantMessage, ResultMessage, TextBlock,
            run_agent_query, AgentOptions,
        )

        async def multi_result_query(*, prompt, options=None):
            yield AssistantMessage(content=[TextBlock(text="part 1")])
            yield ResultMessage(total_cost_usd=0.75)
            yield AssistantMessage(content=[TextBlock(text="part 2")])
            yield ResultMessage(total_cost_usd=1.50)

        with patch("otto.agent.query", side_effect=multi_result_query):
            text, cost, result_msg = await run_agent_query(
                "test", AgentOptions()
            )

        assert cost == pytest.approx(1.50)
        assert "part 1" in text
        assert "part 2" in text
        assert result_msg.total_cost_usd == pytest.approx(1.50)

    @pytest.mark.asyncio
    async def test_diagnosis_marker_stops_at_message_boundary(self):
        from otto.agent import (
            AssistantMessage, AgentOptions, ResultMessage, TextBlock, run_agent_query,
        )
        from otto.markers import parse_certifier_markers

        async def diagnosis_query(*, prompt, options=None):
            yield AssistantMessage(content=[TextBlock(text="VERDICT: FAIL\nDIAGNOSIS: Missing blur handler")])
            yield AssistantMessage(content=[TextBlock(text="Extra narration after the diagnosis marker.")])
            yield ResultMessage(total_cost_usd=0.25)

        with patch("otto.agent.query", side_effect=diagnosis_query):
            text, _cost, _result_msg = await run_agent_query("test", AgentOptions())

        parsed = parse_certifier_markers(text)
        assert parsed.diagnosis == "Missing blur handler"
        assert "Extra narration" not in parsed.diagnosis


# Phase C cleanup (tick 64): TestCertifyPassesConfig, TestCertifierStoryDedup,
# and the two standalone-certifier-target tests were deleted along with
# `run_agentic_certifier` (W8-A). The `--legacy` certify path now hard-errors;
# coverage of the new i2p path lives in tests/test_cli_run.py and
# tests/test_audit*.py.


def test_flat_parser_fallback_only_without_certify_round_markers():
    from otto.markers import parse_certifier_markers

    parsed = parse_certifier_markers(
        "METRIC_VALUE: stray-before-round\n"
        "METRIC_MET: NO\n"
        "CERTIFY_ROUND: 1\n"
        "METRIC_VALUE: 82ms\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: null\n"
    )

    assert len(parsed.certify_rounds) == 1
    assert parsed.metric_value == "82ms"
    assert parsed.metric_met is None
    assert parsed.stories == []
    assert parsed.verdict_pass is False


def test_parser_accepts_structured_story_result_fields():
    from otto.markers import parse_certifier_markers

    parsed = parse_certifier_markers(
        "STORY_EVIDENCE_START: smoke\n"
        "curl -i http://localhost:8000/health\n"
        "HTTP/1.1 200 OK\n"
        "STORY_EVIDENCE_END: smoke\n"
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 1\n"
        "STORY_RESULT: smoke | PASS | claim=Health endpoint responds | observed_steps=GET /health; inspect status code | observed_result=Returned 200 OK | surface=HTTP | summary=Health check passed\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: null\n"
    )

    assert len(parsed.stories) == 1
    story = parsed.stories[0]
    assert story["claim"] == "Health endpoint responds"
    assert story["observed_steps"] == ["GET /health", "inspect status code"]
    assert story["observed_result"] == "Returned 200 OK"
    assert story["surface"] == "HTTP"
    assert "200 OK" in story["evidence"]


def test_parser_extracts_prefixed_story_evidence_blocks():
    from otto.markers import parse_certifier_markers

    parsed = parse_certifier_markers(
        "▸ STORY_EVIDENCE_START: smoke\n"
        "▸ $ curl -i http://localhost:8000/health\n"
        "▸ HTTP/1.1 200 OK\n"
        "▸ STORY_EVIDENCE_END: smoke\n"
        "✦ STORIES_TESTED: 1\n"
        "✦ STORIES_PASSED: 1\n"
        "✦ STORY_RESULT: smoke | PASS | claim=Health endpoint responds | observed_steps=GET /health | observed_result=Returned 200 OK | surface=HTTP | methodology=http-request | summary=Health check passed\n"
        "✦ VERDICT: PASS\n"
        "✦ DIAGNOSIS: null\n"
    )

    assert len(parsed.stories) == 1
    assert parsed.stories[0]["evidence"] == (
        "$ curl -i http://localhost:8000/health\nHTTP/1.1 200 OK"
    )


def test_parser_extracts_timestamped_narrative_story_evidence_blocks():
    from otto.markers import parse_certifier_markers

    parsed = parse_certifier_markers(
        "[+5:39] ▸ STORY_EVIDENCE_START: csv-export\n"
        "[+5:39] ▸ $ curl -s http://localhost:8000/export.csv -o /tmp/export.csv\n"
        "[+5:39] ▸ text/csv; 5 data rows\n"
        "[+5:39] ▸ STORY_EVIDENCE_END: csv-export\n"
        "[+5:39] ✦ STORIES_TESTED: 1\n"
        "[+5:39] ✦ STORIES_PASSED: 1\n"
        "[+5:39] ✦ STORY_RESULT: csv-export | PASS | claim=CSV export works | observed_steps=GET /export.csv | observed_result=Returned text/csv | surface=HTTP | methodology=http-request | summary=CSV export passed\n"
        "[+5:39] ✦ VERDICT: PASS\n"
        "[+5:39] ✦ DIAGNOSIS: null\n"
    )

    assert len(parsed.stories) == 1
    assert parsed.stories[0]["evidence"] == (
        "$ curl -s http://localhost:8000/export.csv -o /tmp/export.csv\n"
        "text/csv; 5 data rows"
    )


def test_parser_preserves_fenced_code_inside_story_evidence():
    from otto.markers import parse_certifier_markers

    parsed = parse_certifier_markers(
        "STORY_EVIDENCE_START: smoke\n"
        "```bash\n"
        "curl -i http://localhost:8000/health\n"
        "# HTTP/1.1 200 OK\n"
        "```\n"
        "STORY_EVIDENCE_END: smoke\n"
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 1\n"
        "STORY_RESULT: smoke | PASS | summary=Health check passed\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: null\n"
    )

    assert parsed.stories[0]["evidence"] == (
        "```bash\n"
        "curl -i http://localhost:8000/health\n"
        "# HTTP/1.1 200 OK\n"
        "```"
    )


def test_parser_ignores_evidence_markers_inside_frontmatter_and_fenced_code():
    from otto.markers import parse_certifier_markers

    parsed = parse_certifier_markers(
        "---\n"
        "STORY_EVIDENCE_START: frontmatter\n"
        "secret frontmatter evidence\n"
        "STORY_EVIDENCE_END: frontmatter\n"
        "---\n"
        "```txt\n"
        "STORY_EVIDENCE_START: fenced\n"
        "secret fenced evidence\n"
        "STORY_EVIDENCE_END: fenced\n"
        "```\n"
        "STORY_EVIDENCE_START: real\n"
        "curl -i http://localhost:8000/health\n"
        "HTTP/1.1 200 OK\n"
        "STORY_EVIDENCE_END: real\n"
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 1\n"
        "STORY_RESULT: real | PASS | Health check passed\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: null\n"
    )

    assert len(parsed.stories) == 1
    assert parsed.stories[0]["evidence"] == (
        "curl -i http://localhost:8000/health\nHTTP/1.1 200 OK"
    )


def test_parser_tracks_methodology_and_defaults_implicit_round_to_one():
    from otto.markers import parse_certifier_markers

    parsed = parse_certifier_markers(
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 1\n"
        "STORY_RESULT: add-card | PASS | claim=Card create flow works | observed_steps=click + Add Card; type title; press Enter | observed_result=Card was created | surface=DOM | methodology=live-ui-events | summary=Create flow passed\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: null\n"
    )

    assert len(parsed.certify_rounds) == 1
    assert parsed.certify_rounds[0]["round"] == 1


def test_parser_ignores_mismatched_story_evidence_end_marker():
    from otto.markers import parse_certifier_markers

    parsed = parse_certifier_markers(
        "STORY_EVIDENCE_START: smoke\n"
        "curl -i http://localhost:8000/health\n"
        "STORY_EVIDENCE_END: other\n"
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 1\n"
        "STORY_RESULT: smoke | PASS | summary=Health check passed\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: null\n"
    )

    assert parsed.stories[0]["evidence"] == "curl -i http://localhost:8000/health"


def test_parser_ignores_unterminated_story_evidence_block():
    from otto.markers import parse_certifier_markers

    parsed = parse_certifier_markers(
        "STORY_EVIDENCE_START: smoke\n"
        "curl -i http://localhost:8000/health\n"
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 1\n"
        "STORY_RESULT: smoke | PASS | summary=Health check passed\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: null\n"
    )

    assert parsed.stories[0].get("evidence") is None


def test_parser_treats_malformed_story_result_segments_as_summary_text():
    from otto.markers import parse_certifier_markers

    parsed = parse_certifier_markers(
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 1\n"
        "STORY_RESULT: smoke | PASS | claim=Health endpoint responds | observed_steps GET /health | broken segment | summary=Health check passed\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: null\n"
    )

    story = parsed.stories[0]
    assert story["claim"] == (
        "Health endpoint responds | observed_steps GET /health | broken segment"
    )
    assert story["summary"] == "Health check passed"
    assert story.get("observed_steps") is None


def test_parser_non_bullet_coverage_sections_produce_empty_lists_but_keep_markers():
    from otto.markers import parse_certifier_markers

    parsed = parse_certifier_markers(
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 1\n"
        "STORY_RESULT: smoke | PASS | summary=Health check passed\n"
        "COVERAGE_OBSERVED:\n"
        "Observed the happy path without bullet formatting\n"
        "COVERAGE_GAPS:\n"
        "Skipped malformed-input coverage without bullet formatting\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: null\n",
        certifier_mode="standard",
    )

    assert parsed.coverage_observed == []
    assert parsed.coverage_gaps == []
    assert parsed.coverage_observed_emitted is True
    assert parsed.coverage_gaps_emitted is True


def test_parser_accepts_failure_evidence_field():
    from otto.markers import parse_certifier_markers

    parsed = parse_certifier_markers(
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 0\n"
        "STORY_RESULT: crud-lifecycle | FAIL | claim=Create card once | observed_result=Duplicate card rendered | "
        "surface=DOM | methodology=live-ui-events | failure_evidence=crud-lifecycle-failure.png | summary=Duplicate create bug\n"
        "VERDICT: FAIL\n"
        "DIAGNOSIS: Duplicate create bug still reproduces\n"
    )

    assert parsed.stories[0]["failure_evidence"] == "crud-lifecycle-failure.png"


def test_parser_extracts_coverage_observed_and_gaps_blocks():
    from otto.markers import parse_certifier_markers

    parsed = parse_certifier_markers(
        "STORIES_TESTED: 2\n"
        "STORIES_PASSED: 1\n"
        "STORY_RESULT: add-card | PASS | Add-card flow works\n"
        "STORY_RESULT: escape-cancel | FAIL | Escape did not cancel editing\n"
        "COVERAGE_OBSERVED:\n"
        "- Clicked Add Card, typed a title, and pressed Enter to commit\n"
        "- Pressed Escape while editing an existing card title\n"
        "COVERAGE_GAPS:\n"
        "- Did not resize the window to test responsive layout\n"
        "- Did not clear localStorage mid-session\n"
        "VERDICT: FAIL\n"
        "DIAGNOSIS: Escape cancel behavior is broken\n",
        certifier_mode="standard",
    )

    assert parsed.coverage_observed == [
        "Clicked Add Card, typed a title, and pressed Enter to commit",
        "Pressed Escape while editing an existing card title",
    ]
    assert parsed.coverage_gaps == [
        "Did not resize the window to test responsive layout",
        "Did not clear localStorage mid-session",
    ]


def test_standard_mode_missing_coverage_markers_raises_malformed_output():
    from otto.markers import MalformedCertifierOutputError, parse_certifier_markers

    with pytest.raises(
        MalformedCertifierOutputError,
        match="COVERAGE_OBSERVED/COVERAGE_GAPS",
    ):
        parse_certifier_markers(
            "STORIES_TESTED: 1\n"
            "STORIES_PASSED: 1\n"
            "STORY_RESULT: smoke | PASS | Works\n"
            "VERDICT: PASS\n"
            "DIAGNOSIS: null\n",
            certifier_mode="standard",
        )


def test_fast_mode_allows_missing_coverage_markers():
    from otto.markers import parse_certifier_markers

    parsed = parse_certifier_markers(
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 1\n"
        "STORY_RESULT: smoke | PASS | Works\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: null\n",
        certifier_mode="fast",
    )

    assert parsed.stories[0]["story_id"] == "smoke"
    assert parsed.coverage_observed == []
    assert parsed.coverage_gaps == []


def test_parser_ignores_markers_inside_code_blocks():
    from otto.markers import parse_certifier_markers

    parsed = parse_certifier_markers(
        "```text\n"
        "VERDICT: FAIL\n"
        "STORY_RESULT: fake | FAIL | should be ignored\n"
        "```\n"
        "    DIAGNOSIS: also ignored\n"
        "CERTIFY_ROUND: 1\n"
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 1\n"
        "STORY_RESULT: real | PASS | actual result\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: null\n"
    )

    assert parsed.verdict_pass is True
    assert [story["story_id"] for story in parsed.stories] == ["real"]


def test_parser_rejects_non_monotonic_round_numbers():
    from otto.markers import parse_certifier_markers

    with pytest.raises(ValueError, match="Non-monotonic"):
        parse_certifier_markers(
            "CERTIFY_ROUND: 2\n"
            "VERDICT: FAIL\n"
            "CERTIFY_ROUND: 1\n"
            "VERDICT: PASS\n"
        )


def test_parser_uses_metric_met_without_metric_value():
    from otto.markers import parse_certifier_markers

    parsed = parse_certifier_markers(
        "CERTIFY_ROUND: 1\n"
        "METRIC_MET: YES\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: null\n"
    )

    assert parsed.metric_met is True
    assert parsed.metric_value == ""


def test_parser_preserves_non_placeholder_diagnosis_prefix():
    from otto.markers import parse_certifier_markers

    parsed = parse_certifier_markers(
        "STORIES_TESTED: 1\n"
        "STORIES_PASSED: 0\n"
        "STORY_RESULT: crash | FAIL | segfault\n"
        "VERDICT: FAIL\n"
        "DIAGNOSIS: null pointer dereference\n"
    )

    assert parsed.diagnosis == "null pointer dereference"


# -- Test: result_msg init guards against UnboundLocalError on early return --

class TestRunBudget:
    """Total-run budget replaces per-call timeout as primary knob."""

    def test_default_is_3600(self):
        from otto.config import get_run_budget
        assert get_run_budget({}) == 3600

    def test_configured_value_honored(self):
        from otto.config import get_run_budget
        assert get_run_budget({"run_budget_seconds": 7200}) == 7200
        assert get_run_budget({"run_budget_seconds": "7200"}) == 7200

    @pytest.mark.parametrize("bad", ["abc", 0, -5, None, ""])
    def test_invalid_falls_back_to_3600(self, bad):
        from otto.config import get_run_budget
        assert get_run_budget({"run_budget_seconds": bad}) == 3600

    def test_remaining_decreases(self):
        import time as _time
        from otto.budget import RunBudget
        b = RunBudget(total=60.0)
        start_remaining = b.remaining()
        _time.sleep(0.05)
        assert b.remaining() < start_remaining

    def test_exhausted(self):
        from otto.budget import RunBudget
        import time as _time
        # Already-expired budget
        b = RunBudget(total=0.01, start=_time.monotonic() - 1.0)
        assert b.exhausted()

    def test_for_call_returns_remaining(self):
        import time as _time
        from otto.budget import RunBudget
        b = RunBudget(total=1000.0, start=_time.monotonic())
        assert b.for_call() == pytest.approx(1000, abs=1)


# Phase C cleanup (tick 64): TestSpecTimeoutTolerance was deleted along with
# `run_agentic_certifier` (W8-A). Marker-parsing edge-cases live in the
# parse_certifier_markers tests above; the new audit stack handles
# malformed-output detection in tests/test_audit*.py.


# Phase C cleanup (tick 64): TestProofOfWorkRendering (13 tests) was
# deleted along with `_build_pow_report_data`, `_render_pow_html`,
# `_render_pow_markdown`, `_write_pow_report`, and `_intent_excerpt`
# (W8-A). Proof-of-work rendering now lives in `otto/render.py`; tests
# for the new renderer live in tests/test_render*.py.



# -- Test: run_test_suite handles git worktree add failure --

class TestCrossRunMemory:
    """Certifier memory should record and format run history."""

    def test_record_and_load(self, tmp_path):
        """record_run writes JSONL, load_history reads it."""
        from otto.memory import load_history, record_run

        record_run(
            tmp_path,
            run_id="run-1",
            command="build",
            certifier_mode="thorough",
            stories=[
                {"story_id": "auth", "passed": True, "summary": "Auth works"},
                {"story_id": "crud", "passed": False, "summary": "Create fails"},
            ],
            cost=1.50,
        )
        entries = load_history(tmp_path)
        assert len(entries) == 1
        assert entries[0]["run_id"] == "run-1"
        assert entries[0]["command"] == "build"
        assert entries[0]["tested"] == 2
        assert entries[0]["passed"] == 1
        assert len(entries[0]["findings"]) == 2


class TestCliValidation:
    def test_build_rejects_nonpositive_budget(self, tmp_git_repo):
        from click.testing import CliRunner
        from otto.cli import main

        runner = CliRunner()
        with patch("os.getcwd", return_value=str(tmp_git_repo)):
            result = runner.invoke(main, ["build", "intent", "--budget", "0"])

        assert result.exit_code == 2
        assert "Invalid value for '--budget'" in result.output

    def test_build_rejects_rounds_above_cap(self, tmp_git_repo):
        from click.testing import CliRunner
        from otto.cli import main

        runner = CliRunner()
        with patch("os.getcwd", return_value=str(tmp_git_repo)):
            result = runner.invoke(main, ["build", "intent", "--rounds", "51"])

        assert result.exit_code == 2
        assert "Invalid value for '--rounds'" in result.output

    def test_improve_rejects_nonpositive_rounds(self, tmp_git_repo):
        from click.testing import CliRunner
        from otto.cli import main

        runner = CliRunner()
        with patch("os.getcwd", return_value=str(tmp_git_repo)):
            result = runner.invoke(main, ["improve", "bugs", "--rounds", "0"])

        assert result.exit_code == 2
        assert "Invalid value for '--rounds'" in result.output

    def test_format_for_prompt_empty(self, tmp_path):
        """No history → empty string."""
        from otto.memory import format_for_prompt
        assert format_for_prompt(tmp_path) == ""

    def test_format_for_prompt_with_history(self, tmp_path):
        """History → prompt section with findings."""
        from otto.memory import format_for_prompt, record_run

        record_run(
            tmp_path, run_id="run-2", command="certify", certifier_mode="fast",
            stories=[{"story_id": "smoke", "passed": True, "summary": "Works"}],
            cost=0.14,
        )

        result = format_for_prompt(tmp_path)
        assert "Previous Certification History" in result
        assert "smoke" in result
        assert "VERIFY" in result  # must include verification guidance

    def test_max_entries_cap(self, tmp_path):
        """Only last N entries are returned."""
        from otto.memory import MAX_ENTRIES, load_history, record_run

        for i in range(MAX_ENTRIES + 3):
            record_run(
                tmp_path, run_id=f"run-{i}", command="build", certifier_mode="fast",
                stories=[{"story_id": f"s{i}", "passed": True, "summary": f"Story {i}"}],
                cost=0.1,
            )

        entries = load_history(tmp_path)
        assert len(entries) == MAX_ENTRIES

    def test_load_history_sorts_by_timestamp_across_sources(self, tmp_path):
        """New, legacy, and archived memory entries should merge chronologically."""
        from otto.memory import load_history
        from otto import paths

        new_path = paths.certifier_memory_jsonl(tmp_path)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_text(json.dumps({
            "ts": "2026-04-20T12:00:00Z",
            "command": "build",
            "certifier_mode": "fast",
            "findings": [],
        }) + "\n")

        legacy_path = tmp_path / "otto_logs" / "certifier-memory.jsonl"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(json.dumps({
            "ts": "2026-04-20T11:00:00Z",
            "command": "legacy",
            "certifier_mode": "fast",
            "findings": [],
        }) + "\n")

        archive_dir = tmp_path / "otto_logs.pre-restructure.2026-04-19T000000Z"
        archive_dir.mkdir()
        (archive_dir / paths.LEGACY_CERTIFIER_MEMORY).write_text(json.dumps({
            "ts": "2026-04-20T10:00:00Z",
            "command": "archive",
            "certifier_mode": "fast",
            "findings": [],
        }) + "\n")

        entries = load_history(tmp_path)
        assert [entry["command"] for entry in entries] == ["archive", "legacy", "build"]


class TestHistoryOrdering:
    """History merges should sort by timestamps, not source precedence."""

    def test_load_history_entries_sorts_chronologically(self, tmp_path):
        from otto.cli_logs import _load_history_entries
        from otto import paths

        new_path = paths.history_jsonl(tmp_path)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_text(json.dumps({
            "build_id": "new-run",
            "timestamp": "2026-04-20T12:00:00Z",
            "intent": "new",
        }) + "\n")

        legacy_path = tmp_path / "otto_logs" / "run-history.jsonl"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(json.dumps({
            "build_id": "legacy-run",
            "timestamp": "2026-04-20T11:00:00Z",
            "intent": "legacy",
        }) + "\n")

        archive_dir = tmp_path / "otto_logs.pre-restructure.2026-04-19T000000Z"
        archive_dir.mkdir()
        (archive_dir / paths.LEGACY_RUN_HISTORY).write_text(json.dumps({
            "build_id": "archive-run",
            "timestamp": "2026-04-20T10:00:00Z",
            "intent": "archive",
        }) + "\n")

        entries = _load_history_entries(tmp_path)
        assert [entry["build_id"] for entry in entries] == [
            "archive-run",
            "legacy-run",
            "new-run",
        ]

    def test_load_history_entries_prefers_run_id_and_command_family(self, tmp_path):
        from otto.cli_logs import _load_history_entries
        from otto import paths

        new_path = paths.history_jsonl(tmp_path)
        new_path.parent.mkdir(parents=True, exist_ok=True)
        new_path.write_text(json.dumps({
            "run_id": "run-123",
            "command": "improve bugs",
            "timestamp": "2026-04-20T12:00:00Z",
            "intent": "new",
        }) + "\n")

        legacy_path = tmp_path / "otto_logs" / "run-history.jsonl"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text(json.dumps({
            "build_id": "run-123",
            "timestamp": "2026-04-20T11:00:00Z",
            "intent": "legacy",
        }) + "\n")

        entries = _load_history_entries(tmp_path)
        assert len(entries) == 1
        assert entries[0]["run_id"] == "run-123"
        assert entries[0]["command"] == "improve bugs"

    def test_load_history_entries_keeps_same_run_id_across_distinct_commands(self, tmp_path):
        from otto.cli_logs import _load_history_entries
        from otto import paths

        history_path = paths.history_jsonl(tmp_path)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(
            json.dumps({
                "run_id": "run-123",
                "command": "build",
                "timestamp": "2026-04-20T12:00:00Z",
                "intent": "outer build",
            })
            + "\n"
            + json.dumps({
                "run_id": "run-123",
                "command": "certify",
                "timestamp": "2026-04-20T12:01:00Z",
                "intent": "nested certify",
            })
            + "\n"
        )

        entries = _load_history_entries(tmp_path)
        assert [(entry["run_id"], entry["command"]) for entry in entries] == [
            ("run-123", "build"),
            ("run-123", "certify"),
        ]

    def test_history_command_shows_cmd_column_and_filter(self, tmp_git_repo):
        from click.testing import CliRunner
        from otto.cli import main
        from otto import paths

        history_path = paths.history_jsonl(tmp_git_repo)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(
            json.dumps({
                "run_id": "run-build",
                "build_id": "run-build",
                "command": "build",
                "passed": True,
                "stories_passed": 1,
                "stories_tested": 1,
                "cost_usd": 0.5,
                "duration_s": 10,
                "intent": "build project",
                "timestamp": "2026-04-20T12:00:00Z",
            }) + "\n" +
            json.dumps({
                "run_id": "run-certify",
                "build_id": "run-certify",
                "command": "certify",
                "passed": False,
                "stories_passed": 0,
                "stories_tested": 1,
                "cost_usd": 0.2,
                "duration_s": 5,
                "intent": "certify project",
                "timestamp": "2026-04-20T13:00:00Z",
            }) + "\n"
        )

        with patch("pathlib.Path.cwd", return_value=tmp_git_repo):
            result = CliRunner().invoke(main, ["history", "--command", "certify"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "Cmd" in result.output
        assert "certify" in result.output
        assert "build project" not in result.output

    def test_history_command_tags_merge_cert_sessions(self, tmp_git_repo):
        from click.testing import CliRunner
        from otto.cli import main
        from otto import paths

        history_path = paths.history_jsonl(tmp_git_repo)
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(
            json.dumps({
                "run_id": "run-build",
                "build_id": "run-build",
                "command": "build",
                "passed": True,
                "stories_passed": 1,
                "stories_tested": 1,
                "timestamp": "2026-04-20T12:00:00Z",
            }) + "\n" +
            json.dumps({
                "run_id": "run-cert-merge",
                "build_id": "run-cert-merge",
                "command": "certify",
                "passed": True,
                "stories_passed": 1,
                "stories_tested": 1,
                "timestamp": "2026-04-20T13:00:00Z",
            }) + "\n"
        )
        paths.session_dir(tmp_git_repo, "run-build").mkdir(parents=True, exist_ok=True)
        paths.session_summary(tmp_git_repo, "run-build").write_text(json.dumps({
            "run_id": "run-build",
            "command": "build",
        }))
        paths.session_dir(tmp_git_repo, "run-cert-merge").mkdir(parents=True, exist_ok=True)
        paths.session_summary(tmp_git_repo, "run-cert-merge").write_text(json.dumps({
            "run_id": "run-cert-merge",
            "command": "certify",
            "merged_from": ["add", "mul"],
        }))

        with patch("pathlib.Path.cwd", return_value=tmp_git_repo):
            result = CliRunner().invoke(main, ["history"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "certify [merge-cert]" in result.output
        assert "build [merge-cert]" not in result.output

    def test_pow_help_and_missing_session(self, tmp_git_repo):
        from click.testing import CliRunner
        from otto.cli import main

        with patch("pathlib.Path.cwd", return_value=tmp_git_repo):
            help_result = CliRunner().invoke(main, ["pow", "--help"], catch_exceptions=False)
            missing_result = CliRunner().invoke(main, ["pow", "missing-session", "--print"], catch_exceptions=False)

        assert help_result.exit_code == 0
        assert "Open a proof-of-work report." in help_result.output
        assert "[RUN_ID]" in help_result.output
        assert "--print" in help_result.output
        assert missing_result.exit_code == 1
        assert "session not found: missing-session" in missing_result.output


class TestResolveResume:
    """resolve_resume handles the four checkpoint states consistently."""

    def test_no_checkpoint_no_resume(self, tmp_path):
        """Clean slate: no checkpoint, user didn't ask to resume."""
        from otto.checkpoint import resolve_resume
        state = resolve_resume(tmp_path, resume=False, expected_command="build")
        assert not state.resumed
        assert state.start_round == 1
        assert state.total_cost == 0.0
        assert state.agent_session_id == ""

    def test_no_checkpoint_with_resume_flag(self, tmp_path):
        """User passed --resume but no checkpoint exists → fall back to fresh."""
        from otto.checkpoint import resolve_resume
        state = resolve_resume(tmp_path, resume=True, expected_command="build")
        assert not state.resumed
        assert state.start_round == 1

    def test_load_checkpoint_scans_active_session_if_pointer_missing(self, tmp_path):
        """A crash between checkpoint write and paused-pointer write must still be resumable."""
        import json
        from otto import paths
        from otto.checkpoint import load_checkpoint

        session_id = "2026-04-25-010203-abcdef"
        paths.ensure_session_scaffold(tmp_path, session_id)
        paths.session_checkpoint(tmp_path, session_id).write_text(json.dumps({
            "run_id": session_id,
            "command": "build",
            "status": "paused",
            "session_id": "sdk-session-1",
            "current_round": 2,
            "total_cost": 1.25,
            "updated_at": "2026-04-25T01:02:03Z",
        }))

        checkpoint = load_checkpoint(tmp_path)

        assert checkpoint is not None
        assert checkpoint["run_id"] == session_id
        assert checkpoint["agent_session_id"] == "sdk-session-1"

    def test_stale_checkpoint_cleared_when_not_resuming(self, tmp_path):
        """Checkpoint exists but user ran without --resume → it's cleared."""
        from otto.checkpoint import resolve_resume, write_checkpoint, load_checkpoint
        write_checkpoint(
            tmp_path, run_id="r1", command="improve",
            current_round=3, total_cost=2.50, status="in_progress",
        )
        state = resolve_resume(tmp_path, resume=False, expected_command="build")
        assert not state.resumed
        assert load_checkpoint(tmp_path) is None  # cleared

    def test_resume_matching_command(self, tmp_path):
        """Checkpoint matches current command → clean resume, no mismatch flag."""
        from otto.checkpoint import resolve_resume, write_checkpoint
        write_checkpoint(
            tmp_path, run_id="r1", command="build",
            session_id="sess-abc", current_round=2, total_cost=1.23,
            rounds=[{"round": 1}, {"round": 2}], status="paused",
        )
        state = resolve_resume(tmp_path, resume=True, expected_command="build")
        assert state.resumed
        assert state.start_round == 3   # current_round + 1
        assert state.total_cost == 1.23
        assert state.agent_session_id == "sess-abc"
        assert state.prior_command == "build"
        assert not state.command_mismatch
        assert len(state.rounds) == 2

    def test_resume_uses_queue_attempt_run_id_for_completed_failed_checkpoint(self, tmp_path, monkeypatch):
        """Queue resume can continue a failed task whose checkpoint was marked completed."""
        from otto.checkpoint import resolve_resume, write_checkpoint
        write_checkpoint(
            tmp_path,
            run_id="r1",
            command="improve.feature",
            current_round=3,
            total_cost=1.23,
            rounds=[{"round": 1}, {"round": 2}, {"round": 3}],
            status="completed",
        )
        monkeypatch.setenv("OTTO_RUN_ID", "r1")

        state = resolve_resume(tmp_path, resume=True, expected_command="improve.feature")

        assert state.resumed
        assert state.run_id == "r1"
        assert state.start_round == 4
        assert state.total_cost == 1.23

    def test_resume_command_mismatch(self, tmp_path):
        """Checkpoint is from `improve`, user runs `build --resume` → mismatch flag set."""
        from otto.checkpoint import resolve_resume, write_checkpoint
        write_checkpoint(
            tmp_path, run_id="r1", command="improve",
            current_round=2, total_cost=0.5, status="in_progress",
        )
        state = resolve_resume(tmp_path, resume=True, expected_command="build")
        assert state.resumed
        assert state.command_mismatch
        assert state.prior_command == "improve"

    def test_resume_rejects_command_mismatch_without_force(self, tmp_path):
        from otto.checkpoint import resolve_resume, write_checkpoint

        write_checkpoint(
            tmp_path, run_id="r1", command="improve.bugs", status="paused",
        )
        with pytest.raises(ValueError, match="not from `build`"):
            resolve_resume(
                tmp_path,
                resume=True,
                expected_command="build",
                reject_incompatible=True,
            )

    def test_resume_rejects_fingerprint_mismatch_without_force(self, tmp_path, monkeypatch):
        from otto.checkpoint import resolve_resume, write_checkpoint

        write_checkpoint(tmp_path, run_id="r1", command="build", status="paused")
        monkeypatch.setattr(
            "otto.checkpoint.checkpoint_fingerprint",
            lambda _project_dir: {"git_sha": "different", "prompt_hash": "different"},
        )
        with pytest.raises(ValueError, match="fingerprint"):
            resolve_resume(
                tmp_path,
                resume=True,
                expected_command="build",
                reject_incompatible=True,
            )

    def test_resume_rejects_dirty_worktree_fingerprint_mismatch_without_force(self, tmp_path, monkeypatch):
        from otto.checkpoint import resolve_resume, write_checkpoint

        write_checkpoint(tmp_path, run_id="r1", command="build", status="paused")
        monkeypatch.setattr(
            "otto.checkpoint.checkpoint_fingerprint",
            lambda _project_dir: {
                "git_sha": "",
                "git_status": " M otto/config.py\n",
                "prompt_hash": "",
            },
        )
        with pytest.raises(ValueError, match="git status differs"):
            resolve_resume(
                tmp_path,
                resume=True,
                expected_command="build",
                reject_incompatible=True,
            )

    def test_resume_reports_deleted_paused_session(self, tmp_path):
        from otto import paths
        from otto.checkpoint import resolve_resume

        paths.logs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        (paths.logs_dir(tmp_path) / "paused.txt").write_text("missing-run\n")

        state = resolve_resume(tmp_path, resume=True, expected_command="build")

        assert not state.resumed
        assert state.missing_paused_session_path.endswith("missing-run")

    def test_completed_checkpoint_not_resumed(self, tmp_path):
        """Completed checkpoints should be ignored even with --resume."""
        from otto.checkpoint import resolve_resume, write_checkpoint
        write_checkpoint(
            tmp_path, run_id="r1", command="build",
            current_round=5, total_cost=3.0, status="completed",
        )
        state = resolve_resume(tmp_path, resume=True, expected_command="build")
        assert not state.resumed


class TestLegacyLayoutResume:
    """Upgrade-safety: legacy otto_logs/checkpoint.json (pre-restructure
    layout) must still be loadable via resolve_resume without running any
    migration. Exercises the fallback path in checkpoint.load_checkpoint.
    """

    def test_resolve_resume_reads_legacy_paused_checkpoint(self, tmp_path):
        """Simulate an old-layout project where a build was paused with
        otto_logs/checkpoint.json at status=paused. resolve_resume must
        honor it on the first post-upgrade invocation — no sessions/ dir,
        no paused pointer."""
        import json
        from otto import paths
        from otto.checkpoint import resolve_resume

        logs = paths.logs_dir(tmp_path)
        logs.mkdir(parents=True, exist_ok=True)
        legacy = paths.legacy_checkpoint(tmp_path)
        legacy.write_text(json.dumps({
            "run_id": "legacy-run-42",
            "command": "build",
            "status": "paused",
            "phase": "build",
            "session_id": "sdk-legacy-xyz",
            "current_round": 2,
            "total_cost": 1.75,
            "rounds": [{"round": 1}, {"round": 2}],
            "intent": "legacy intent",
            "started_at": "2026-03-01T10:00:00Z",
            "updated_at": "2026-03-01T10:05:00Z",
        }))

        # Sanity: no new-layout state.
        assert not (logs / "sessions").exists()
        assert not (logs / paths.PAUSED_POINTER).exists()
        assert not (logs / f"{paths.PAUSED_POINTER}.txt").exists()

        state = resolve_resume(tmp_path, resume=True, expected_command="build")
        assert state.resumed
        assert state.prior_command == "build"
        assert state.agent_session_id == "sdk-legacy-xyz"
        assert state.run_id == "legacy-run-42"
        assert state.start_round == 3  # current_round + 1
        assert state.total_cost == 1.75
        assert state.phase == "build"
        assert state.intent == "legacy intent"

    def test_resolve_resume_legacy_completed_ignored(self, tmp_path):
        """A legacy checkpoint in status=completed must not resume."""
        import json
        from otto import paths
        from otto.checkpoint import resolve_resume

        paths.logs_dir(tmp_path).mkdir(parents=True, exist_ok=True)
        paths.legacy_checkpoint(tmp_path).write_text(json.dumps({
            "run_id": "r", "command": "build",
            "status": "completed", "current_round": 5, "total_cost": 2.0,
        }))
        state = resolve_resume(tmp_path, resume=True, expected_command="build")
        assert not state.resumed

    def test_new_layout_wins_over_legacy_when_both_present(self, tmp_path):
        """If both a new session checkpoint and legacy checkpoint.json exist,
        the new layout takes precedence (legacy is fallback only)."""
        import json
        from otto import paths
        from otto.checkpoint import resolve_resume, write_checkpoint

        # Write a new-layout paused checkpoint.
        write_checkpoint(
            tmp_path, run_id="2026-04-20-170200-abcdef", command="build",
            session_id="sdk-new", phase="build",
            current_round=4, total_cost=3.33, status="paused",
        )
        # Write a stale legacy checkpoint with different data.
        paths.legacy_checkpoint(tmp_path).write_text(json.dumps({
            "run_id": "legacy-stale", "command": "build",
            "session_id": "sdk-legacy",
            "current_round": 99, "total_cost": 99.0, "status": "paused",
        }))

        state = resolve_resume(tmp_path, resume=True, expected_command="build")
        assert state.resumed
        # New layout wins.
        assert state.agent_session_id == "sdk-new"
        assert state.run_id == "2026-04-20-170200-abcdef"
        assert state.total_cost == 3.33
