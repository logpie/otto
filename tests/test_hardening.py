"""Hardening regression tests for the v3 pipeline."""

import asyncio
import contextlib
import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import click
import pytest

from otto.pipeline import build_agentic_v3, BuildResult
from otto.testing import _subprocess_env
from tests._helpers import write_test_pow_report
from tests.conftest import make_mock_query as _make_mock_query
from tests.test_spec import MINIMAL_VALID

# `tmp_git_repo` fixture comes from tests/conftest.py.


# -- Test: STORY_RESULT with pipes in summary --

class TestPipeSeparatorRobustness:
    """split('|', 2) prevents summaries with pipes from breaking parsing."""

    AGENT_OUTPUT_PIPE_IN_SUMMARY = """\
CERTIFY_ROUND: 1
STORIES_TESTED: 2
STORIES_PASSED: 2
STORY_RESULT: crud | PASS | Create/Read work | including edge cases
STORY_RESULT: search | PASS | Search by tag|title returns correct results
COVERAGE_OBSERVED:
- Exercised mocked CRUD and search stories
COVERAGE_GAPS:
- Did not model deeper product-specific coverage in this mocked transcript
VERDICT: PASS
DIAGNOSIS: null
"""

    @pytest.mark.asyncio
    async def test_pipe_in_summary_preserved(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(self.AGENT_OUTPUT_PIPE_IN_SUMMARY)):
            result = await build_agentic_v3("test", tmp_git_repo, {})

        assert result.passed is True
        assert result.tasks_passed == 2
        # Summary should contain everything after the second pipe
        summaries = [j["name"] for j in result.journeys]
        assert any("including edge cases" in s for s in summaries)
        assert any("tag|title" in s for s in summaries)


# -- Test: Verdict fallback logic --

class TestVerdictFallbackScan:
    """Ensure fallback parsing takes the LAST verdict, not the first."""

    MULTIPLE_VERDICTS_FAIL_LAST = """\
STORIES_TESTED: 2
STORIES_PASSED: 1
STORY_RESULT: crud | PASS | Works
STORY_RESULT: auth | FAIL | Broken
VERDICT: PASS

Fixed code.

STORY_RESULT: crud | PASS | Still works
STORY_RESULT: auth | FAIL | Still broken
COVERAGE_OBSERVED:
- Exercised mocked CRUD and auth stories
COVERAGE_GAPS:
- Did not model deeper product-specific coverage in this mocked transcript
VERDICT: FAIL
DIAGNOSIS: Auth remains broken after fix attempt
"""

    @pytest.mark.asyncio
    async def test_last_verdict_wins_in_fallback(self, tmp_git_repo):
        """When no CERTIFY_ROUND markers, the last VERDICT should be used."""
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(self.MULTIPLE_VERDICTS_FAIL_LAST)):
            result = await build_agentic_v3("test", tmp_git_repo, {})

        # The last VERDICT is FAIL — should not pick up the earlier PASS
        assert result.passed is False


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

class TestBuildResultRounds:
    """BuildResult.rounds should reflect the number of certification rounds."""

    AGENT_OUTPUT_TWO_ROUNDS = """\
CERTIFY_ROUND: 1
STORIES_TESTED: 2
STORIES_PASSED: 1
STORY_RESULT: crud | PASS | Works
STORY_RESULT: auth | FAIL | Missing check
COVERAGE_OBSERVED:
- Exercised mocked CRUD and auth stories in round 1
COVERAGE_GAPS:
- Did not model deeper product-specific coverage in this mocked transcript
VERDICT: FAIL
DIAGNOSIS: Auth broken

Fixed.

CERTIFY_ROUND: 2
STORIES_TESTED: 2
STORIES_PASSED: 2
STORY_RESULT: crud | PASS | Works
STORY_RESULT: auth | PASS | Fixed
COVERAGE_OBSERVED:
- Exercised mocked CRUD and auth stories in round 2
COVERAGE_GAPS:
- Did not model deeper product-specific coverage in this mocked transcript
VERDICT: PASS
DIAGNOSIS: null
"""

    @pytest.mark.asyncio
    async def test_rounds_reflects_actual_count(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(self.AGENT_OUTPUT_TWO_ROUNDS)):
            result = await build_agentic_v3("test", tmp_git_repo, {})

        assert result.passed is True
        assert result.rounds == 2


# -- Test: Timeout is actually enforced --

class TestTimeoutEnforcement:
    """Build should time out when the run budget is exceeded."""

    @pytest.mark.asyncio
    async def test_build_times_out(self, tmp_git_repo):
        import time as _time
        from otto.budget import RunBudget
        async def slow_query(prompt, options, **kwargs):
            await asyncio.sleep(10)
            return "never reached", 0.0, MagicMock()

        start = _time.monotonic()
        with patch("otto.agent.run_agent_query", side_effect=slow_query):
            result = await build_agentic_v3(
                "test", tmp_git_repo, {},
                budget=RunBudget(total=1.0, start=_time.monotonic()),
            )
        elapsed = _time.monotonic() - start

        assert result.passed is False
        # Timeout was 1s; with orphan cleanup and report writes it may take
        # several seconds. 9s still catches a no-timeout regression (which
        # would sleep the full 10s plus overhead).
        assert elapsed < 9, f"Timeout not enforced; elapsed={elapsed:.1f}s"
        # Check narrative.log mentions timeout — strict `Timed out` match,
        # written by run_agent_with_timeout on asyncio.TimeoutError.
        from otto import paths
        build_dir = paths.build_dir(tmp_git_repo, result.build_id)
        narr = (build_dir / "narrative.log").read_text()
        assert "Timed out" in narr, f"Timeout not reported in narrative.log: {narr[:200]}"

    def test_resumed_budget_uses_original_session_start(self):
        from datetime import datetime, timedelta, timezone

        from otto.budget import RunBudget

        started_at = (datetime.now(timezone.utc) - timedelta(seconds=90)).isoformat()
        budget = RunBudget.start_from(
            {"run_budget_seconds": 60},
            session_started_at=started_at,
        )

        assert budget.exhausted() is True


# -- Test: CLAUDECODE env var --

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


# -- Test: Empty story_id is rejected --

class TestEmptyStoryId:
    """STORY_RESULT with empty story_id should be silently skipped."""

    AGENT_OUTPUT_EMPTY_SID = """\
CERTIFY_ROUND: 1
STORIES_TESTED: 2
STORIES_PASSED: 2
STORY_RESULT:  | PASS | Ghost entry with empty id
STORY_RESULT: real-story | PASS | Real story that works
COVERAGE_OBSERVED:
- Exercised mocked empty-id and real story entries
COVERAGE_GAPS:
- Did not model deeper product-specific coverage in this mocked transcript
VERDICT: PASS
DIAGNOSIS: null
"""

    @pytest.mark.asyncio
    async def test_empty_story_id_skipped(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(self.AGENT_OUTPUT_EMPTY_SID)):
            result = await build_agentic_v3("test", tmp_git_repo, {})

        # Only the real story should appear, ghost entry skipped
        assert len(result.journeys) == 1
        assert result.journeys[0]["story_id"] == "real-story"

    AGENT_OUTPUT_ONLY_EMPTY = """\
CERTIFY_ROUND: 1
STORIES_TESTED: 1
STORIES_PASSED: 1
STORY_RESULT: | PASS | only ghost
COVERAGE_OBSERVED:
- Exercised mocked empty story id entry
COVERAGE_GAPS:
- Did not model deeper product-specific coverage in this mocked transcript
VERDICT: PASS
DIAGNOSIS: null
"""

    @pytest.mark.asyncio
    async def test_all_empty_story_ids_means_no_pass(self, tmp_git_repo):
        """If ALL story_ids are empty, build should fail (zero real stories)."""
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(self.AGENT_OUTPUT_ONLY_EMPTY)):
            result = await build_agentic_v3("test", tmp_git_repo, {})

        # Zero stories = fail, even though VERDICT: PASS
        assert result.passed is False


# -- Test: Zero stories + VERDICT: PASS should fail --

class TestZeroStoriesVerdictPass:
    """VERDICT: PASS with no STORY_RESULT markers should NOT count as passing."""

    AGENT_OUTPUT_VERDICT_NO_STORIES = """\
CERTIFY_ROUND: 1
STORIES_TESTED: 0
STORIES_PASSED: 0
COVERAGE_OBSERVED:
- Exercised mocked zero-story certifier output
COVERAGE_GAPS:
- Did not model deeper product-specific coverage in this mocked transcript
VERDICT: PASS
DIAGNOSIS: null
"""

    @pytest.mark.asyncio
    async def test_verdict_pass_no_stories_fails(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(self.AGENT_OUTPUT_VERDICT_NO_STORIES)):
            result = await build_agentic_v3("test", tmp_git_repo, {})

        assert result.passed is False


# -- Test: Cost accumulation across multiple ResultMessages --

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


# -- Test: otto certify loads otto.yaml and passes it to the certifier --

class TestCertifyPassesConfig:
    """The `certify` CLI should load otto.yaml and forward it to
    run_agentic_certifier (not ignore user-configured timeouts etc.)."""

    def test_certify_passes_config(self, tmp_git_repo):
        # Write a custom setting to otto.yaml and verify it reaches the certifier.
        config_path = tmp_git_repo / "otto.yaml"
        config_path.write_text("spec_timeout: 1200\n")

        captured_config = []

        async def mock_certifier(intent, project_dir, config=None, **kwargs):
            captured_config.append(config)
            from otto.certifier.report import CertificationReport, CertificationOutcome
            return CertificationReport(
                outcome=CertificationOutcome.PASSED, cost_usd=0.0, duration_s=1.0,
            )

        from click.testing import CliRunner
        from otto.cli import main

        with patch("otto.certifier.run_agentic_certifier", side_effect=mock_certifier), \
             patch("os.getcwd", return_value=str(tmp_git_repo)):
            runner = CliRunner()
            runner.invoke(main, ["certify", "test intent"], catch_exceptions=False)

        assert captured_config, "run_agentic_certifier was not called"
        assert captured_config[0].get("spec_timeout") == 1200

    def test_certify_resolves_mode_with_cli_yaml_default_precedence(self, tmp_bare_git_repo):
        from click.testing import CliRunner
        from otto.cli import main
        from otto.certifier.report import CertificationOutcome, CertificationReport

        captured_modes: list[str] = []

        async def mock_certifier(intent, project_dir, config=None, mode=None, **kwargs):
            captured_modes.append(mode)
            return CertificationReport(
                outcome=CertificationOutcome.PASSED,
                cost_usd=0.0,
                duration_s=1.0,
            )

        (tmp_bare_git_repo / "intent.md").write_text("test intent")
        runner = CliRunner()

        with patch("otto.certifier.run_agentic_certifier", side_effect=mock_certifier), \
             patch("pathlib.Path.cwd", return_value=tmp_bare_git_repo):
            result = runner.invoke(main, ["certify"], catch_exceptions=False)
        assert result.exit_code == 0
        assert captured_modes.pop() == "fast"

        (tmp_bare_git_repo / "otto.yaml").write_text("certifier_mode: standard\n")
        with patch("otto.certifier.run_agentic_certifier", side_effect=mock_certifier), \
             patch("pathlib.Path.cwd", return_value=tmp_bare_git_repo):
            result = runner.invoke(main, ["certify"], catch_exceptions=False)
        assert result.exit_code == 0
        assert captured_modes.pop() == "standard"

        with patch("otto.certifier.run_agentic_certifier", side_effect=mock_certifier), \
             patch("pathlib.Path.cwd", return_value=tmp_bare_git_repo):
            result = runner.invoke(main, ["certify", "--fast"], catch_exceptions=False)
        assert result.exit_code == 0
        assert captured_modes.pop() == "fast"


class TestHistoryWrites:
    @pytest.mark.asyncio
    async def test_standalone_certify_appends_history_entry(self, tmp_git_repo):
        from otto import paths
        from otto.certifier import run_agentic_certifier

        async def mock_run(*args, **kwargs):
            return (
                "STORIES_TESTED: 1\n"
                "STORIES_PASSED: 1\n"
                "STORY_RESULT: smoke | PASS | claim=Smoke works | observed_result=OK | surface=HTTP | methodology=http-request | summary=Smoke passed\n"
                "COVERAGE_OBSERVED:\n"
                "- Exercised the smoke story over HTTP and observed an OK result\n"
                "COVERAGE_GAPS:\n"
                "- Did not exercise any deeper product-specific coverage in this mocked run\n"
                "VERDICT: PASS\n"
                "DIAGNOSIS: null\n",
                0.42,
                "agent-session-1",
                {"round_timings": []},
            )

        with patch("otto.agent.run_agent_with_timeout", side_effect=mock_run):
            await run_agentic_certifier("test intent", tmp_git_repo, {}, session_id="run-certify-1")

        entry = json.loads(paths.history_jsonl(tmp_git_repo).read_text().strip())
        assert entry["run_id"] == "run-certify-1"
        assert entry["build_id"] == "run-certify-1"
        assert entry["command"] == "certify"
        assert entry["certifier_mode"] == "standard"
        assert entry["mode"] == "standard"
        assert entry["certifier_cost_usd"] == pytest.approx(0.42)

    @pytest.mark.asyncio
    async def test_split_improve_appends_history_entry(self, tmp_git_repo):
        from otto import paths
        from otto.certifier.report import CertificationOutcome, CertificationReport
        from otto.pipeline import run_certify_fix_loop

        async def mock_certifier(*args, **kwargs):
            return CertificationReport(
                outcome=CertificationOutcome.PASSED,
                cost_usd=0.25,
                duration_s=1.0,
                story_results=[{
                    "story_id": "smoke",
                    "passed": True,
                    "summary": "Smoke passed",
                }],
            )

        with patch("otto.certifier.run_agentic_certifier", side_effect=mock_certifier):
            result = await run_certify_fix_loop(
                "test improve",
                tmp_git_repo,
                {},
                certifier_mode="thorough",
                skip_initial_build=True,
                command="improve.bugs",
                session_id="run-improve-1",
            )

        assert result.passed is True
        entry = json.loads(paths.history_jsonl(tmp_git_repo).read_text().strip())
        assert entry["run_id"] == "run-improve-1"
        assert entry["command"] == "improve bugs"
        assert entry["certifier_mode"] == "thorough"
        checkpoint = json.loads(paths.session_checkpoint(tmp_git_repo, "run-improve-1").read_text())
        assert checkpoint["prompt_mode"] == "improve"


class TestImproveCLIHardening:
    """The improve CLI should treat infra and build failures as failures."""

    def test_signal_interrupt_guard_maps_sigterm_to_keyboard_interrupt(self):
        import signal
        from otto.cli import _signal_interrupt_guard

        with _signal_interrupt_guard():
            handler = signal.getsignal(signal.SIGTERM)
            with pytest.raises(KeyboardInterrupt):
                handler(signal.SIGTERM, None)

    def test_create_improve_branch_exits_when_checkout_does_not_switch(self, tmp_path):
        """Exit if checkout claims success but branch did not actually change."""
        from otto.cli_improve import _create_improve_branch

        def cp(stdout="", returncode=0):
            return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)

        with patch("otto.cli_improve.time.strftime", return_value="2026-04-13"), \
             patch("otto.cli_improve.subprocess.run", side_effect=[
                 cp(stdout="main\n"),
                 cp(returncode=1),
                 cp(returncode=0),
                 cp(stdout="main\n"),
             ]):
            with pytest.raises(SystemExit) as exc:
                _create_improve_branch(tmp_path)

        assert exc.value.code == 1

    def test_improve_stops_on_certifier_infra_failure(self, tmp_git_repo):
        """Infra failures must stop the loop before any fix/build round runs."""
        from click.testing import CliRunner
        from otto.cli import main
        (tmp_git_repo / "intent.md").write_text("test intent")

        certifier_calls = 0

        async def mock_certifier(intent, project_dir, config=None, **kwargs):
            nonlocal certifier_calls
            certifier_calls += 1
            raise RuntimeError("socket died")

        mock_build = AsyncMock()

        # Patch at otto.pipeline — run_certify_fix_loop calls `build_agentic_v3`
        # and `run_agentic_certifier` directly by name in its own module scope.
        with patch("otto.cli_improve._create_improve_branch", return_value="improve/2026-04-13"), \
             patch("otto.certifier.run_agentic_certifier", side_effect=mock_certifier), \
            patch("otto.pipeline.build_agentic_v3", new=mock_build), \
            patch("pathlib.Path.cwd", return_value=tmp_git_repo):
            runner = CliRunner()
            result = runner.invoke(
                main, ["improve", "feature", "test intent", "--rounds", "1", "--split", "--allow-dirty"], catch_exceptions=False
            )

        # Positive check: certifier was actually reached. Without this, the
        # mock_build.await_count==0 assertion could pass vacuously (e.g. if a
        # click wiring bug exited before entering the loop).
        assert certifier_calls >= 1, \
            f"certifier was never called — test doesn't exercise loop. Output: {result.output!r}"
        # Core invariant: exhausted infra retries short-circuit before any build/fix agent runs
        assert mock_build.await_count == 0
        # Result should indicate failure, not success
        assert result.exit_code != 0
        assert "PASSED" not in result.output

    def test_improve_reports_failure_when_fix_fails(self, tmp_git_repo):
        """When fix phase fails, result should be FAILED not PASSED."""
        from click.testing import CliRunner
        from otto.certifier.report import CertificationOutcome, CertificationReport
        from otto.cli import main
        (tmp_git_repo / "intent.md").write_text("test intent")

        async def mock_certifier(intent, project_dir, config=None, **kwargs):
            return CertificationReport(
                outcome=CertificationOutcome.FAILED,
                cost_usd=0.0,
                duration_s=1.0,
                story_results=[
                    {
                        "story_id": "auth",
                        "passed": False,
                        "summary": "Login broken",
                        "evidence": "",
                    }
                ],
            )

        async def mock_build(intent, project_dir, config):
            return BuildResult(
                passed=False,
                build_id="build-1",
                total_cost=1.25,
                tasks_passed=0,
                tasks_failed=0,
            )

        with patch("otto.cli_improve._create_improve_branch", return_value="improve/2026-04-13"), \
             patch("otto.certifier.run_agentic_certifier", side_effect=mock_certifier), \
            patch("otto.pipeline.build_agentic_v3", side_effect=mock_build), \
            patch("pathlib.Path.cwd", return_value=tmp_git_repo):
            runner = CliRunner()
            result = runner.invoke(
                main, ["improve", "feature", "test intent", "--rounds", "1", "--split", "--allow-dirty"], catch_exceptions=False
            )

        # Failing-story summary is the reliable signal; the ✗ glyph is cosmetic.
        assert result.exit_code != 0, \
            f"Expected non-zero exit, got {result.exit_code}"
        assert "Login broken" in result.output, \
            f"Expected failing-story summary in output: {result.output!r}"

    def test_improve_warns_when_report_write_fails(self, tmp_git_repo):
        from click.testing import CliRunner
        from otto.cli import main

        (tmp_git_repo / "intent.md").write_text("test intent")

        async def fake_loop(*args, **kwargs):
            return BuildResult(
                passed=True,
                build_id="run-1",
                rounds=1,
                total_cost=0.5,
                total_duration=12.0,
                journeys=[{"name": "Smoke works", "passed": True}],
                tasks_passed=1,
                tasks_failed=0,
            )

        real_write_text = Path.write_text

        def fake_write_text(self, content, *args, **kwargs):
            if self.name == "improvement-report.md":
                raise OSError("disk full")
            return real_write_text(self, content, *args, **kwargs)

        with patch("otto.cli_improve._create_improve_branch", return_value="improve/2026-04-13"), \
             patch("otto.pipeline.run_certify_fix_loop", side_effect=fake_loop), \
            patch("pathlib.Path.write_text", fake_write_text), \
            patch("pathlib.Path.cwd", return_value=tmp_git_repo):
            runner = CliRunner()
            result = runner.invoke(
                main, ["improve", "feature", "--rounds", "1", "--split", "--allow-dirty"], catch_exceptions=False
            )

        assert result.exit_code == 0
        assert "could not write report file" in result.output

    @pytest.mark.asyncio
    async def test_invalid_certifier_mode_raises_instead_of_falling_back(self, tmp_git_repo):
        with pytest.raises(ValueError, match="Expected one of"):
            await build_agentic_v3("test", tmp_git_repo, {}, certifier_mode="bogus")

    @pytest.mark.asyncio
    async def test_split_mode_retry_exhaustion_raises_infra_failure(self, tmp_git_repo):
        from otto.pipeline import InfraFailureError, run_certify_fix_loop

        async def broken_certifier(*args, **kwargs):
            raise RuntimeError("socket died")

        with patch("otto.certifier.run_agentic_certifier", side_effect=broken_certifier):
            with pytest.raises(InfraFailureError, match="socket died"):
                await run_certify_fix_loop(
                    "split improve",
                    tmp_git_repo,
                    {},
                    skip_initial_build=True,
                    command="improve.feature",
                )

    @pytest.mark.asyncio
    async def test_split_mode_nested_certifier_disables_history_writes(self, tmp_git_repo):
        from otto.pipeline import run_certify_fix_loop
        from otto import paths

        async def fake_run_agent_with_timeout(*args, **kwargs):
            return (
                "STORIES_TESTED: 1\n"
                "STORIES_PASSED: 1\n"
                "STORY_RESULT: smoke | PASS | claim=Smoke works | observed_result=OK | surface=HTTP | methodology=http-request | summary=Smoke passed\n"
                "COVERAGE_OBSERVED:\n"
                "- Exercised the smoke story over HTTP and observed an OK result\n"
                "COVERAGE_GAPS:\n"
                "- Did not exercise any deeper product-specific coverage in this mocked run\n"
                "VERDICT: PASS\n"
                "DIAGNOSIS: null\n",
                0.1,
                "agent-session-1",
                {"round_timings": []},
            )

        with patch("otto.agent.run_agent_with_timeout", side_effect=fake_run_agent_with_timeout):
            result = await run_certify_fix_loop(
                "split improve",
                tmp_git_repo,
                {"max_certify_rounds": 1},
                skip_initial_build=True,
                command="improve.feature",
                session_id="run-improve-1",
            )

        assert result.passed is True
        entries = [
            json.loads(line)
            for line in paths.history_jsonl(tmp_git_repo).read_text().splitlines()
            if line.strip()
        ]
        assert len(entries) == 1
        assert entries[0]["command"] == "improve feature"

    @pytest.mark.asyncio
    async def test_fix_prompt_keeps_full_evidence(self, tmp_git_repo):
        from otto.certifier.report import CertificationOutcome, CertificationReport
        from otto.pipeline import run_certify_fix_loop

        long_evidence = "0123456789" * 80
        fail_report = CertificationReport(
            outcome=CertificationOutcome.FAILED,
            cost_usd=0.1,
            duration_s=1.0,
            story_results=[
                {
                    "story_id": "auth",
                    "passed": False,
                    "summary": "Login broken",
                    "evidence": long_evidence,
                }
            ],
        )
        pass_report = CertificationReport(
            outcome=CertificationOutcome.PASSED,
            cost_usd=0.1,
            duration_s=1.0,
            story_results=[
                {
                    "story_id": "auth",
                    "passed": True,
                    "summary": "Login fixed",
                    "evidence": "",
                }
            ],
        )
        captured = {}

        async def fake_build(intent, project_dir, config, **kwargs):
            captured["prompt"] = intent
            return BuildResult(
                passed=True,
                build_id="fix-run",
                total_cost=0.0,
                tasks_passed=1,
                tasks_failed=0,
            )

        with patch("otto.certifier.run_agentic_certifier", side_effect=[fail_report, pass_report]), \
             patch("otto.pipeline.build_agentic_v3", side_effect=fake_build):
            result = await run_certify_fix_loop(
                "split improve",
                tmp_git_repo,
                {"max_certify_rounds": 2},
                skip_initial_build=True,
                command="improve.feature",
            )

        assert result.passed is True
        assert "## Current Failures" in captured["prompt"]
        assert "### auth" in captured["prompt"]
        assert "**Symptom:** Login broken" in captured["prompt"]
        assert f"```\\n{long_evidence}\\n```".replace("\\n", "\n") in captured["prompt"]

    def test_improve_target_resume_rejects_non_target_checkpoint(
        self, tmp_git_repo, monkeypatch
    ):
        """`improve target --resume` must not resume bugs/feature checkpoints."""
        from click.testing import CliRunner
        from otto.checkpoint import write_checkpoint
        from otto.cli import main

        (tmp_git_repo / "intent.md").write_text("test intent")
        write_checkpoint(
            tmp_git_repo,
            run_id="r1",
            command="improve.bugs",
            status="in_progress",
        )

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.cli_improve._run_improve") as run_improve:
            result = CliRunner().invoke(
                main,
                ["improve", "target", "latency < 100ms", "--resume"],
                catch_exceptions=False,
            )

        assert result.exit_code == 2
        assert "Checkpoint command mismatch" in result.output
        assert "improve.bugs" in result.output
        assert "improve.target" in result.output
        assert run_improve.call_count == 0


# -- Test: PoW JSON round_history passed_count computed from stories --

class TestPowPassedCount:
    """PoW JSON should compute passed_count from stories, not default to 0."""

    AGENT_OUTPUT_NO_PASSED_MARKER = """\
CERTIFY_ROUND: 1
STORIES_TESTED: 2
STORY_RESULT: crud | PASS | Works
STORY_RESULT: auth | FAIL | Broken
COVERAGE_OBSERVED:
- Exercised mocked CRUD and auth stories in round 1
COVERAGE_GAPS:
- Did not model deeper product-specific coverage in this mocked transcript
VERDICT: FAIL
DIAGNOSIS: Auth broken

CERTIFY_ROUND: 2
STORIES_TESTED: 2
STORY_RESULT: crud | PASS | Works
STORY_RESULT: auth | PASS | Fixed
COVERAGE_OBSERVED:
- Exercised mocked CRUD and auth stories in round 2
COVERAGE_GAPS:
- Did not model deeper product-specific coverage in this mocked transcript
VERDICT: PASS
DIAGNOSIS: null
"""

    @pytest.mark.asyncio
    async def test_passed_count_computed_from_stories(self, tmp_git_repo):
        """When STORIES_PASSED marker is missing, compute from story results."""
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(self.AGENT_OUTPUT_NO_PASSED_MARKER)):
            result = await build_agentic_v3("test", tmp_git_repo, {})

        from otto import paths as _paths
        pow_path = _paths.certify_dir(tmp_git_repo, result.build_id) / "proof-of-work.json"
        pow_data = json.loads(pow_path.read_text())
        # Round history should show correct passed counts
        rounds = pow_data.get("round_history", [])
        assert len(rounds) == 2
        assert rounds[0]["passed_count"] == 1  # crud passed
        assert rounds[1]["passed_count"] == 2  # both passed


# -- Test: Certifier story deduplication --

class TestCertifierStoryDedup:
    """Certifier should deduplicate stories by story_id."""

    @pytest.mark.asyncio
    async def test_duplicate_stories_deduplicated(self, tmp_git_repo):
        """When subagents report the same story_id, keep only the last."""
        from otto.certifier import run_agentic_certifier
        from otto.certifier.report import CertificationOutcome

        # Same story reported twice (e.g. by two subagents)
        agent_output = (
            "STORIES_TESTED: 3\n"
            "STORIES_PASSED: 3\n"
            "STORY_RESULT: crud | PASS | Create works\n"
            "STORY_RESULT: crud | PASS | Create works (duplicate)\n"
            "STORY_RESULT: auth | PASS | Login works\n"
            "COVERAGE_OBSERVED:\n"
            "- Exercised duplicate and unique story markers in one run\n"
            "COVERAGE_GAPS:\n"
            "- Did not exercise additional product-specific stories in this mocked run\n"
            "VERDICT: PASS\n"
            "DIAGNOSIS: null\n"
        )

        async def mock_query(prompt, options, **kwargs):
            return agent_output, 0.10, MagicMock()

        with patch("otto.agent.run_agent_query", side_effect=mock_query):
            report = await run_agentic_certifier("test", tmp_git_repo)

        assert report.outcome == CertificationOutcome.PASSED
        story_results = report.story_results
        # Should have 2 unique stories, not 3
        assert len(story_results) == 2
        sids = [s["story_id"] for s in story_results]
        assert sorted(sids) == ["auth", "crud"]

    @pytest.mark.asyncio
    async def test_dedup_keeps_last_fail_to_pass(self, tmp_git_repo):
        """If same story_id appears with FAIL then PASS, keep PASS (last)."""
        from otto.certifier import run_agentic_certifier
        from otto.certifier.report import CertificationOutcome

        agent_output = (
            "STORIES_TESTED: 2\n"
            "STORIES_PASSED: 2\n"
            "STORY_RESULT: auth | FAIL | Initially broken\n"
            "STORY_RESULT: auth | PASS | Fixed now\n"
            "STORY_RESULT: crud | PASS | Works\n"
            "COVERAGE_OBSERVED:\n"
            "- Exercised a fail-to-pass duplicate story sequence\n"
            "COVERAGE_GAPS:\n"
            "- Did not exercise additional product-specific stories in this mocked run\n"
            "VERDICT: PASS\n"
            "DIAGNOSIS: null\n"
        )

        async def mock_query(prompt, options, **kwargs):
            return agent_output, 0.10, MagicMock()

        with patch("otto.agent.run_agent_query", side_effect=mock_query):
            report = await run_agentic_certifier("test", tmp_git_repo)

        assert report.outcome == CertificationOutcome.PASSED
        story_results = report.story_results
        auth_story = [s for s in story_results if s["story_id"] == "auth"][0]
        assert auth_story["passed"] is True


@pytest.mark.asyncio
async def test_standalone_certifier_target_fails_when_metric_not_met(tmp_git_repo):
    from otto.certifier import run_agentic_certifier
    from otto.certifier.report import CertificationOutcome

    agent_output = (
        "STORIES_TESTED: 2\n"
        "STORIES_PASSED: 2\n"
        "STORY_RESULT: p50-latency | PASS | Latency probe completed successfully\n"
        "STORY_RESULT: regression-suite | PASS | Existing behavior still passes\n"
        "METRIC_VALUE: 137ms\n"
        "METRIC_MET: NO\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: null\n"
    )

    async def mock_query(prompt, options, **kwargs):
        return agent_output, 0.10, MagicMock()

    with patch("otto.agent.run_agent_query", side_effect=mock_query):
        report = await run_agentic_certifier(
            "latency < 100ms",
            tmp_git_repo,
            config={"_target": "latency < 100ms"},
            mode="target",
            target="latency < 100ms",
        )

    assert report.outcome == CertificationOutcome.FAILED
    assert report.metric_met is False


@pytest.mark.asyncio
async def test_standalone_certifier_target_passes_when_metric_met(tmp_git_repo):
    from otto.certifier import run_agentic_certifier
    from otto.certifier.report import CertificationOutcome

    agent_output = (
        "STORIES_TESTED: 2\n"
        "STORIES_PASSED: 2\n"
        "STORY_RESULT: p50-latency | PASS | Latency probe completed successfully\n"
        "STORY_RESULT: regression-suite | PASS | Existing behavior still passes\n"
        "METRIC_VALUE: 82ms\n"
        "METRIC_MET: YES\n"
        "VERDICT: PASS\n"
        "DIAGNOSIS: null\n"
    )

    async def mock_query(prompt, options, **kwargs):
        return agent_output, 0.10, MagicMock()

    with patch("otto.agent.run_agent_query", side_effect=mock_query):
        report = await run_agentic_certifier(
            "latency < 100ms",
            tmp_git_repo,
            config={"_target": "latency < 100ms"},
            mode="target",
            target="latency < 100ms",
        )

    assert report.outcome == CertificationOutcome.PASSED
    assert report.metric_met is True


class TestTargetModeMetricGate:
    """Target-mode split loop should gate on an explicit METRIC_MET marker."""

    @pytest.mark.asyncio
    async def test_split_target_fails_fast_when_metric_met_missing(self, tmp_git_repo):
        from otto.certifier.report import CertificationOutcome, CertificationReport
        from otto.pipeline import run_certify_fix_loop

        certifier_calls = 0

        async def mock_certifier(intent, project_dir, config=None, **kwargs):
            nonlocal certifier_calls
            certifier_calls += 1
            return CertificationReport(
                outcome=CertificationOutcome.FAILED,
                cost_usd=0.10,
                duration_s=1.0,
                story_results=[
                    {
                        "story_id": "p50-latency",
                        "passed": True,
                        "summary": "Latency probe completed successfully",
                        "evidence": "",
                    },
                    {
                        "story_id": "regression-suite",
                        "passed": True,
                        "summary": "Existing behavior still passes",
                        "evidence": "",
                    },
                ],
                metric_value="150ms",
                metric_met=None,
            )

        mock_fix = AsyncMock()

        with patch("otto.certifier.run_agentic_certifier", side_effect=mock_certifier), \
             patch("otto.pipeline.build_agentic_v3", new=mock_fix):
            result = await run_certify_fix_loop(
                "latency < 100ms",
                tmp_git_repo,
                {},
                certifier_mode="target",
                target="latency < 100ms",
                skip_initial_build=True,
            )

        assert certifier_calls == 1
        assert mock_fix.await_count == 0
        assert result.passed is False
        assert result.rounds == 1
        from otto import paths as _paths
        assert "FAIL (certifier omitted METRIC_MET)" in (
            _paths.improve_dir(tmp_git_repo, result.build_id) / "build-journal.md"
        ).read_text()


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

class TestResultMsgInit:
    """result_msg should be initialized to avoid UnboundLocalError."""

    @pytest.mark.asyncio
    async def test_timeout_does_not_unbind_result_msg(self, tmp_git_repo):
        """On timeout, build should complete without UnboundLocalError."""
        async def slow_query(prompt, options, **kwargs):
            await asyncio.sleep(10)
            return "never", 0.0, MagicMock()

        import time as _time
        from otto.budget import RunBudget
        with patch("otto.agent.run_agent_query", side_effect=slow_query):
            # This should not raise UnboundLocalError
            result = await build_agentic_v3(
                "test", tmp_git_repo, {},
                budget=RunBudget(total=1.0, start=_time.monotonic()),
            )

        assert result.passed is False

    @pytest.mark.asyncio
    async def test_exception_does_not_unbind_result_msg(self, tmp_git_repo):
        """On agent crash, build should complete without UnboundLocalError."""
        async def crashing_query(prompt, options, **kwargs):
            raise RuntimeError("agent exploded")

        with patch("otto.agent.run_agent_query", side_effect=crashing_query):
            result = await build_agentic_v3("test", tmp_git_repo, {})

        assert result.passed is False


class TestSessionIdPreservedOnFailure:
    """Agent timeout/crash must preserve session_id via AgentCallError so
    --resume continues the SDK conversation instead of starting fresh."""

    @pytest.mark.asyncio
    async def test_timeout_captures_streamed_session_id(self, tmp_git_repo):
        """Timeout mid-stream: session_id must land in checkpoint via
        err.session_id, not be blanked."""
        async def streaming_query(prompt, options, *, state=None, **kwargs):
            # Simulate SDK streaming: a session_id is observed before timeout.
            if state is not None:
                state["session_id"] = "streamed-sid-abc123"
            await asyncio.sleep(10)
            return "never", 0.0, MagicMock()

        import time as _time
        from otto.budget import RunBudget
        # Budget must be >=2s so int(remaining) is positive when wait_for
        # schedules the coroutine, letting the mock set state before timeout.
        with patch("otto.agent.run_agent_query", side_effect=streaming_query):
            result = await build_agentic_v3(
                "test", tmp_git_repo, {},
                budget=RunBudget(total=3.0, start=_time.monotonic()),
            )
        assert result.passed is False

        import json
        from otto import paths as _paths
        sess = _paths.resolve_pointer(tmp_git_repo, _paths.PAUSED_POINTER)
        assert sess is not None, "expected paused pointer after failure"
        cp = json.loads((sess / "checkpoint.json").read_text())
        assert cp["agent_session_id"] == "streamed-sid-abc123", (
            "timeout must preserve streamed session_id for --resume"
        )
        assert cp["status"] == "paused", "failed run must be resumable, not completed"

    @pytest.mark.asyncio
    async def test_crash_captures_streamed_session_id(self, tmp_git_repo):
        """Agent crash mid-stream: session_id must survive."""
        async def crashing_query(prompt, options, *, state=None, **kwargs):
            if state is not None:
                state["session_id"] = "pre-crash-sid-xyz"
            raise RuntimeError("boom")

        with patch("otto.agent.run_agent_query", side_effect=crashing_query):
            result = await build_agentic_v3("test", tmp_git_repo, {})
        assert result.passed is False

        import json
        from otto import paths as _paths
        sess = _paths.resolve_pointer(tmp_git_repo, _paths.PAUSED_POINTER)
        assert sess is not None, "expected paused pointer after failure"
        cp = json.loads((sess / "checkpoint.json").read_text())
        assert cp["agent_session_id"] == "pre-crash-sid-xyz"
        assert cp["status"] == "paused"


# -- Test: runtime intent snapshots stay out of project-root intent.md --

class TestSessionIntentSnapshots:
    """Runtime intent should be recorded per session, not appended to intent.md."""

    def test_runtime_intent_writes_session_file(self, tmp_path):
        from otto import paths
        from otto.pipeline import _append_intent

        _append_intent(tmp_path, "build X", "build-1")

        assert paths.session_intent(tmp_path, "build-1").read_text().strip() == "build X"
        assert not (tmp_path / "intent.md").exists()

    def test_second_run_does_not_overwrite_first_session_snapshot(self, tmp_path):
        from otto import paths
        from otto.pipeline import _append_intent

        _append_intent(tmp_path, "build X", "build-1")
        _append_intent(tmp_path, "build X and Y", "build-2")

        assert paths.session_intent(tmp_path, "build-1").read_text().strip() == "build X"
        assert paths.session_intent(tmp_path, "build-2").read_text().strip() == "build X and Y"


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


class TestBudgetExhaustionInPipeline:
    """Integration: a pre-exhausted budget causes agent-mode timeout and
    the paused checkpoint is written with the preserved session_id."""

    @pytest.mark.asyncio
    async def test_exhausted_budget_pauses_agent_mode(self, tmp_git_repo):
        """Budget that's already expired → timeout immediately via
        asyncio.wait_for; AgentCallError path fires; checkpoint paused.

        session_id may be empty when the budget is expired at call time
        (asyncio.wait_for with timeout<=0 never starts the coroutine, so
        streaming never runs). The important invariant is status=paused.
        """
        import time as _time
        from otto.budget import RunBudget

        async def slow_query(prompt, options, *, state=None, **kwargs):
            await asyncio.sleep(10)
            return "never", 0.0, MagicMock()

        expired_budget = RunBudget(total=0.01, start=_time.monotonic() - 5.0)
        assert expired_budget.exhausted()

        with patch("otto.agent.run_agent_query", side_effect=slow_query):
            result = await build_agentic_v3(
                "test", tmp_git_repo, {}, budget=expired_budget,
            )
        assert result.passed is False

        import json
        from otto import paths as _paths
        sess = _paths.resolve_pointer(tmp_git_repo, _paths.PAUSED_POINTER)
        assert sess is not None, "expected paused pointer after failure"
        cp = json.loads((sess / "checkpoint.json").read_text())
        assert cp["status"] == "paused"

    @pytest.mark.asyncio
    async def test_mid_call_budget_timeout_preserves_session_id(self, tmp_git_repo):
        """Budget with small-but-positive remaining → stream starts, mid-call
        session_id captured, then timeout fires. Preserved in checkpoint."""
        import time as _time
        from otto.budget import RunBudget

        async def slow_query(prompt, options, *, state=None, **kwargs):
            if state is not None:
                state["session_id"] = "mid-stream-sid"
                state["total_cost_usd"] = 0.37
            await asyncio.sleep(5)
            return "never", 0.0, MagicMock()

        # Budget large enough for wait_for to actually start the coroutine
        # (int(remaining) must be > 0 after a few ms of test overhead).
        short_budget = RunBudget(total=3.0, start=_time.monotonic())

        with patch("otto.agent.run_agent_query", side_effect=slow_query):
            result = await build_agentic_v3(
                "test", tmp_git_repo, {}, budget=short_budget,
            )
        assert result.passed is False

        import json
        from otto import paths as _paths
        sess = _paths.resolve_pointer(tmp_git_repo, _paths.PAUSED_POINTER)
        assert sess is not None, "expected paused pointer after failure"
        cp = json.loads((sess / "checkpoint.json").read_text())
        assert cp["status"] == "paused"
        assert cp["agent_session_id"] == "mid-stream-sid"
        assert cp["total_cost"] == pytest.approx(0.37)


# -- Test: invalid spec_timeout in config is tolerated --

class TestSpecTimeoutTolerance:
    """spec.py should fall back to the default when spec_timeout is non-numeric."""

    @pytest.mark.asyncio
    async def test_certifier_ignores_unknown_timeout_keys(self, tmp_git_repo):
        from otto.certifier import run_agentic_certifier
        from otto.certifier.report import CertificationOutcome

        async def mock_query(prompt, options, **kwargs):
            return (
                "VERDICT: PASS\n"
                "STORY_RESULT: x | PASS | ok\n"
                "STORIES_TESTED: 1\n"
                "STORIES_PASSED: 1\n"
                "COVERAGE_OBSERVED:\n"
                "- Exercised the single mocked pass story\n"
                "COVERAGE_GAPS:\n"
                "- Did not exercise any additional product-specific flows in this mocked run\n"
                "DIAGNOSIS: null"
            ), 0.1, MagicMock()

        # Obsolete keys like `certifier_timeout` are now ignored — no raise.
        with patch("otto.agent.run_agent_query", side_effect=mock_query):
            report = await run_agentic_certifier(
                "test", tmp_git_repo, config={"certifier_timeout": "not-a-number"}
            )
        assert report.outcome == CertificationOutcome.PASSED
        assert report.story_results and report.story_results[0]["story_id"] == "x"

    @pytest.mark.asyncio
    async def test_certifier_raises_on_unstructured_output(self, tmp_git_repo):
        from otto.certifier import run_agentic_certifier
        from otto.markers import MalformedCertifierOutputError

        async def mock_query(prompt, options, **kwargs):
            return "just narration, no markers", 0.1, MagicMock()

        with patch("otto.agent.run_agent_query", side_effect=mock_query):
            with pytest.raises(MalformedCertifierOutputError, match="no structured output"):
                await run_agentic_certifier("test", tmp_git_repo, config={})


class TestProofOfWorkRendering:
    def test_html_renders_explicit_absence_notes_for_visual_evidence_and_diagnosis(self, tmp_path):
        evidence_dir = tmp_path / "evidence"
        evidence_dir.mkdir()

        write_test_pow_report(
            tmp_path,
            [{
                "story_id": "smoke",
                "passed": True,
                "summary": "ok",
                "surface": "DOM",
                "methodology": "live-ui-events",
            }],
            "passed",
            1.0,
            0.1,
            1,
            1,
            diagnosis="",
            evidence_dir=evidence_dir,
        )

        html = (tmp_path / "proof-of-work.html").read_text()
        assert "Visual Evidence" in html
        assert "Visual evidence: not collected (mode=standard)" in html
        assert "<h2>Diagnosis</h2>" not in html
        assert "<h2>Story Summary</h2>" in html
        assert "<h2>Coverage and Limitations</h2>" in html
        assert "provider-default" not in html
        assert "not recorded" not in html
        assert "not present" not in html
        assert "not used (run without --spec)" in html
        assert "$0.10" in html
        assert "certifier $0.1000" not in html

    def test_fast_mode_renders_note_when_per_run_coverage_is_missing(self, tmp_path):
        from otto.certifier import _build_pow_report_data, _render_pow_html

        options = type("Opts", (), {"provider": "", "model": None, "effort": None})()
        report = _build_pow_report_data(
            project_dir=tmp_path,
            report_dir=tmp_path,
            log_dir=tmp_path,
            run_id="run-1",
            session_id="sdk-session-1",
            pipeline_mode="agentic_certifier",
            certifier_mode="fast",
            outcome="passed",
            story_results=[{"story_id": "smoke", "passed": True, "summary": "ok"}],
            diagnosis="",
            certify_rounds=None,
            duration_s=1.0,
            certifier_cost_usd=0.1,
            total_cost_usd=0.1,
            intent="Smoke test the app.",
            options=options,
            evidence_dir=None,
            stories_tested=1,
            stories_passed=1,
            coverage_observed=[],
            coverage_gaps=[],
            coverage_emitted=False,
        )

        html = _render_pow_html(report)
        assert "Per-run coverage not emitted (fast mode)" in html

    def test_non_visual_products_suppress_visual_and_efficiency_sections(self, tmp_path):
        from otto.certifier import _build_pow_report_data, _render_pow_html, _render_pow_markdown

        options = type("Opts", (), {"provider": "", "model": None, "effort": None})()
        report = _build_pow_report_data(
            project_dir=tmp_path,
            report_dir=tmp_path,
            log_dir=tmp_path,
            run_id="run-1",
            session_id="sdk-session-1",
            pipeline_mode="agentic_certifier",
            certifier_mode="standard",
            outcome="passed",
            story_results=[{"story_id": "smoke", "passed": True, "summary": "ok", "surface": "CLI", "methodology": "cli-execution"}],
            diagnosis="",
            certify_rounds=None,
            duration_s=1.0,
            certifier_cost_usd=0.1,
            total_cost_usd=0.1,
            intent="Smoke test the CLI.",
            options=options,
            evidence_dir=tmp_path / "evidence",
            stories_tested=1,
            stories_passed=1,
            coverage_observed=["Ran the CLI happy path"],
            coverage_gaps=["Did not exercise malformed input"],
            coverage_emitted=True,
        )

        md = _render_pow_markdown(report)
        html = _render_pow_html(report)

        assert "## Visual Evidence" not in md
        assert "## Efficiency" not in md
        assert "<h2>Visual Evidence</h2>" not in html
        assert "<h2>Efficiency</h2>" not in html

    def test_old_pow_json_coverage_still_renders_with_deprecation_comment(self, tmp_path):
        from otto.certifier import _build_pow_report_data, _render_pow_html

        options = type("Opts", (), {"provider": "", "model": None, "effort": None})()
        report = _build_pow_report_data(
            project_dir=tmp_path,
            report_dir=tmp_path,
            log_dir=tmp_path,
            run_id="run-1",
            session_id="sdk-session-1",
            pipeline_mode="agentic_certifier",
            certifier_mode="standard",
            outcome="passed",
            story_results=[{"story_id": "smoke", "passed": True, "summary": "ok"}],
            diagnosis="",
            certify_rounds=None,
            duration_s=1.0,
            certifier_cost_usd=0.1,
            total_cost_usd=0.1,
            intent="Smoke test the app.",
            options=options,
            evidence_dir=None,
            stories_tested=1,
            stories_passed=1,
            coverage_observed=[],
            coverage_gaps=[],
            coverage_emitted=False,
        )
        report.pop("coverage_observed", None)
        report.pop("coverage_gaps", None)
        report["coverage"]["tested"] = ["Clicked the main CTA"]
        report["coverage"]["untested"] = ["Did not resize the window"]
        report["coverage"]["escaped_bug_classes"] = ["responsive layout regressions"]

        html = _render_pow_html(report)
        assert "Deprecated legacy coverage rendering" in html
        assert "Clicked the main CTA" in html
        assert "Did not resize the window" in html

    def test_html_surfaces_methodology_caveat_for_ui_story(self, tmp_path):
        write_test_pow_report(
            tmp_path,
            [{
                "story_id": "add-card",
                "passed": True,
                "claim": "Add-card button creates a card in the UI",
                "summary": "Card added",
                "surface": "DOM",
                "methodology": "javascript-eval",
            }],
            "passed",
            1.0,
            0.1,
            1,
            1,
            diagnosis="",
        )

        html = (tmp_path / "proof-of-work.html").read_text()
        assert "All stories verified via javascript-eval." in html
        assert "<strong>Methodology</strong>" not in html
        assert "UI event handlers were not verified" in html

    def test_html_avoids_inline_onclick_for_evidence(self, tmp_path):
        write_test_pow_report(
            tmp_path,
            [{
                "story_id": "story'quoted",
                "passed": False,
                "summary": "bad",
                "evidence": "secret-free evidence",
            }],
            "failed",
            1.0,
            0.1,
            0,
            1,
            diagnosis="",
        )

        html = (tmp_path / "proof-of-work.html").read_text()
        assert "onclick=" not in html
        assert "data-story-id=" in html
        assert "querySelectorAll('.evidence-toggle')" in html

    def test_report_caps_visible_stories_at_200(self, tmp_path):
        from otto.certifier import _build_pow_report_data

        options = type("Opts", (), {"provider": "", "model": None, "effort": None})()
        stories = [
            {"story_id": f"s{i}", "passed": True, "summary": f"story {i}"}
            for i in range(205)
        ]
        report = _build_pow_report_data(
            project_dir=tmp_path,
            report_dir=tmp_path,
            log_dir=tmp_path,
            run_id="run-1",
            session_id="sdk-1",
            pipeline_mode="agentic_certifier",
            certifier_mode="standard",
            outcome="passed",
            story_results=stories,
            diagnosis="",
            certify_rounds=[],
            duration_s=1.0,
            certifier_cost_usd=0.1,
            total_cost_usd=0.1,
            intent="intent",
            options=options,
            evidence_dir=None,
            stories_tested=205,
            stories_passed=205,
        )

        assert len(report["stories"]) == 200
        assert report["stories_hidden_count"] == 5

    def test_pow_outputs_slim_markdown_and_grouped_visual_evidence(self, tmp_path):
        from otto.certifier import _build_pow_report_data, _write_pow_report

        evidence_dir = tmp_path / "evidence"
        evidence_dir.mkdir()
        for name in (
            "recording.webm",
            "crud-lifecycle-failure.png",
            "crud-lifecycle.png",
            "drag-drop.png",
        ):
            (evidence_dir / name).write_bytes(b"test")

        (tmp_path / "narrative.log").write_text("narrative")
        (tmp_path / "messages.jsonl").write_text("{}\n")

        options = type("Opts", (), {"provider": "", "model": None, "effort": None})()
        report = _build_pow_report_data(
            project_dir=tmp_path,
            report_dir=tmp_path,
            log_dir=tmp_path,
            run_id="run-1",
            session_id="sdk-session-1",
            pipeline_mode="agentic_certifier",
            certifier_mode="standard",
            outcome="failed",
            story_results=[
                {
                    "story_id": "crud-lifecycle",
                    "passed": False,
                    "claim": "Create one card from the UI",
                    "observed_steps": ["click Add card", "type title", "press Enter"],
                    "observed_result": "A duplicate card appeared after one submit.",
                    "surface": "DOM",
                    "methodology": "live-ui-events",
                    "summary": "Duplicate create bug reproduced",
                    "evidence": "Clicked Add card once and two cards appeared.",
                    "failure_evidence": "crud-lifecycle-failure.png",
                },
                {
                    "story_id": "drag-drop",
                    "passed": True,
                    "claim": "Cards can be reordered by drag and drop",
                    "observed_result": "Card moved into the target column.",
                    "surface": "DOM",
                    "methodology": "live-ui-events",
                    "summary": "Drag and drop passed",
                    "evidence": "Dragged card A into Done and order updated.",
                },
            ],
            diagnosis="Submitting the add-card flow still creates duplicate cards.",
            certify_rounds=None,
            duration_s=12.0,
            certifier_cost_usd=0.4,
            total_cost_usd=0.4,
            intent="Verify card creation and drag-drop behavior.",
            options=options,
            evidence_dir=evidence_dir,
            stories_tested=2,
            stories_passed=1,
            coverage_observed=[
                "Clicked Add card, typed a title, and pressed Enter to submit",
                "Dragged a card from In Progress into Done",
            ],
            coverage_gaps=[
                "Did not resize the window to test responsive layout",
                "Did not clear localStorage mid-session to verify empty-state recovery",
            ],
            coverage_emitted=True,
        )
        _write_pow_report(tmp_path, report)

        md = (tmp_path / "proof-of-work.md").read_text()
        html = (tmp_path / "proof-of-work.html").read_text()

        assert "## Hero" in md
        assert "## Story Summary" in md
        assert "## Diagnosis" in md
        assert "## Story Details" in md
        assert "## Visual Evidence" in md
        assert "## Efficiency" in md
        assert "## Coverage and Limitations" in md
        assert "## Run Context" in md
        assert "## Artifacts & Metadata" in md
        assert "### What this run actually exercised" in md
        assert "### What this run did NOT cover" in md
        assert "Mode: standard —" in md

        assert html.index("<h2>Story Summary</h2>") < html.index("<h2>Diagnosis</h2>")
        assert html.index("<h2>Diagnosis</h2>") < html.index("<h2>Story Details</h2>")
        assert html.index("<h2>Story Details</h2>") < html.index("<h2>Visual Evidence</h2>")
        assert html.index("<h2>Visual Evidence</h2>") < html.index("<h2>Efficiency</h2>")
        assert html.index("<h2>Efficiency</h2>") < html.index("<h2>Coverage and Limitations</h2>")
        assert html.index("<h2>Visual Evidence</h2>") < html.index("<h2>Coverage and Limitations</h2>")
        assert html.index("<h2>Visual Evidence</h2>") < html.index("<h2>Run Context</h2>")
        assert html.index("<h2>Coverage and Limitations</h2>") < html.index("<h2>Run Context</h2>")
        assert html.index("<h2>Run Context</h2>") < html.index("<h2>Artifacts &amp; Metadata</h2>")

        assert "recording.webm" in html
        assert "crud-lifecycle-failure.png" in html
        assert "failure captured" in html
        assert "drag-drop.png" in html
        assert "All stories verified via live-ui-events." in html
        assert "Clicked Add card, typed a title, and pressed Enter to submit" in html
        assert "Did not resize the window to test responsive layout" in html
        assert "Jump to failing stories" in html
        assert "Open narrative log" not in html
        assert "agentic_certifier" not in html
        assert "Session ID" in html

        assert "coverage_observed" in report
        assert "coverage_gaps" in report
        assert "tested" not in report["coverage"]
        assert "untested" not in report["coverage"]
        assert "escaped_bug_classes" not in report["coverage"]

    def test_efficiency_section_renders_warning_for_outlier(self, tmp_path):
        from otto.certifier import _build_pow_report_data, _render_pow_html, _render_pow_markdown

        (tmp_path / "messages.jsonl").write_text("{}\n")
        options = type("Opts", (), {"provider": "", "model": None, "effort": None})()
        report = _build_pow_report_data(
            project_dir=tmp_path,
            report_dir=tmp_path,
            log_dir=tmp_path,
            run_id="run-1",
            session_id="sdk-session-1",
            pipeline_mode="agentic_certifier",
            certifier_mode="standard",
            outcome="passed",
            story_results=[
                {"story_id": "first-experience", "passed": True, "summary": "ok"},
                {"story_id": "crud-lifecycle", "passed": True, "summary": "ok"},
            ],
            diagnosis="",
            certify_rounds=None,
            duration_s=3.0,
            certifier_cost_usd=0.1,
            total_cost_usd=0.1,
            intent="Verify the main browser flows.",
            options=options,
            evidence_dir=None,
            stories_tested=2,
            stories_passed=2,
        )
        report["efficiency"] = {
            "total_browser_calls": 75,
            "distinct_sessions": 4,
            "verb_counts": {"eval": 30, "snapshot": 20, "click": 15, "type": 10},
            "calls_per_story": {"first-experience": 38, "crud-lifecycle": 37},
            "outlier": True,
            "outlier_reason": "standard-mode outlier: distinct sessions 4 > 3.",
        }

        md = _render_pow_markdown(report)
        html = _render_pow_html(report)

        assert "## Efficiency" in md
        assert "- Total browser calls: 75 across 2 stories (37.5 per story)" in md
        assert "- Distinct browser sessions: 4" in md
        assert "- Top verbs: eval (30), snapshot (20), click (15), type (10)" in md
        assert "- ⚠ Efficiency note: standard-mode outlier: distinct sessions 4 > 3." in md

        assert "<h2>Efficiency</h2>" in html
        assert "Total browser calls: 75 across 2 stories (37.5 per story)" in html
        assert "Distinct browser sessions: 4" in html
        assert "Top verbs: eval (30), snapshot (20), click (15), type (10)" in html
        assert "Efficiency note: standard-mode outlier: distinct sessions 4 &gt; 3." in html

    def test_efficiency_section_omits_warning_for_non_outlier(self, tmp_path):
        from otto.certifier import _build_pow_report_data, _render_pow_html, _render_pow_markdown

        (tmp_path / "messages.jsonl").write_text("{}\n")
        options = type("Opts", (), {"provider": "", "model": None, "effort": None})()
        report = _build_pow_report_data(
            project_dir=tmp_path,
            report_dir=tmp_path,
            log_dir=tmp_path,
            run_id="run-1",
            session_id="sdk-session-1",
            pipeline_mode="agentic_certifier",
            certifier_mode="standard",
            outcome="passed",
            story_results=[{"story_id": "smoke", "passed": True, "summary": "ok"}],
            diagnosis="",
            certify_rounds=None,
            duration_s=2.0,
            certifier_cost_usd=0.1,
            total_cost_usd=0.1,
            intent="Smoke test the browser flow.",
            options=options,
            evidence_dir=None,
            stories_tested=1,
            stories_passed=1,
        )
        report["efficiency"] = {
            "total_browser_calls": 9,
            "distinct_sessions": 1,
            "verb_counts": {"click": 4, "eval": 3, "snapshot": 2},
            "calls_per_story": {"smoke": 9},
            "outlier": False,
            "outlier_reason": "",
        }

        md = _render_pow_markdown(report)
        html = _render_pow_html(report)

        assert "## Efficiency" in md
        assert "- Total browser calls: 9 across 1 story (9.0 per story)" in md
        assert "⚠ Efficiency note" not in md

        assert "<h2>Efficiency</h2>" in html
        assert "Total browser calls: 9 across 1 story (9.0 per story)" in html
        assert "Efficiency note:" not in html

    def test_pow_report_shows_tokens_when_cost_not_reported(self, tmp_path):
        from otto.certifier import _build_pow_report_data, _render_pow_html, _render_pow_markdown

        (tmp_path / "messages.jsonl").write_text(
            json.dumps(
                {
                    "type": "phase_end",
                    "phase": "certify",
                    "usage": {
                        "input_tokens": 123456,
                        "cached_input_tokens": 120000,
                        "output_tokens": 789,
                    },
                }
            )
            + "\n"
        )
        options = type("Opts", (), {"provider": "codex", "model": "gpt-5.5", "effort": None})()
        report = _build_pow_report_data(
            project_dir=tmp_path,
            report_dir=tmp_path,
            log_dir=tmp_path,
            run_id="run-1",
            session_id="sdk-session-1",
            pipeline_mode="agentic_certifier",
            certifier_mode="standard",
            outcome="passed",
            story_results=[{"story_id": "smoke", "passed": True, "summary": "ok"}],
            diagnosis="",
            certify_rounds=None,
            duration_s=2.0,
            certifier_cost_usd=0.0,
            total_cost_usd=0.0,
            intent="Smoke test the browser flow.",
            options=options,
            evidence_dir=None,
            stories_tested=1,
            stories_passed=1,
        )

        md = _render_pow_markdown(report)
        html = _render_pow_html(report)

        assert report["token_usage"] == {
            "input_tokens": 123456,
            "cached_input_tokens": 120000,
            "output_tokens": 789,
            "total_tokens": 124245,
        }
        assert "- Cost: not reported by provider" in md
        assert "- Tokens: 123,456 input (120,000 cached), 789 output" in md
        assert "Cost: not reported by provider; Tokens:" in html

    def test_certifier_prompts_require_failure_evidence(self):
        standard = (Path(__file__).resolve().parents[1] / "otto" / "prompts" / "certifier.md").read_text()
        thorough = (Path(__file__).resolve().parents[1] / "otto" / "prompts" / "certifier-thorough.md").read_text()
        fast = (Path(__file__).resolve().parents[1] / "otto" / "prompts" / "certifier-fast.md").read_text()

        for text in (standard, thorough):
            assert re.search(r"failure_evidence=<filename", text)
            assert re.search(r"visual failure artifact.*`WARN`, not `FAIL`", text, re.DOTALL)
            assert "COVERAGE_OBSERVED:" in text
            assert "COVERAGE_GAPS:" in text
            assert "## Session topology and efficiency" in text
            assert "--session <story-id>" in text
            assert "malformed-output error" in text
        assert "COVERAGE_OBSERVED:" in fast
        assert "COVERAGE_GAPS:" in fast

    def test_intent_excerpt_strips_markdown_headings(self):
        from otto.certifier import _intent_excerpt

        excerpt = _intent_excerpt(
            "# Build Intents\n\n## 2026-04-21 09:30 (run-1)\nShip the actual feature, not the heading.\n"
        )

        assert excerpt == "Ship the actual feature, not the heading."


# -- Test: run_test_suite handles git worktree add failure --

class TestCommitArtifactsTimeout:
    """_commit_artifacts should not hang indefinitely."""

    def test_commit_artifacts_uses_timeout(self, tmp_git_repo):
        """Git commands in _commit_artifacts should have timeout."""
        from otto.pipeline import _commit_artifacts

        calls_with_timeout = []
        original_run = subprocess.run
        def patched_run(args, **kwargs):
            if args and isinstance(args, list) and args[0] == "git":
                calls_with_timeout.append(kwargs.get("timeout"))
            return original_run(args, **kwargs)

        with patch("otto.pipeline.subprocess.run", side_effect=patched_run):
            _commit_artifacts(tmp_git_repo)

        # Guard against vacuous all([]) — we must actually observe git calls.
        assert len(calls_with_timeout) > 0, \
            "_commit_artifacts made no git calls; test setup broken"
        # All git calls should have a timeout
        assert all(t is not None and t > 0 for t in calls_with_timeout), \
            f"Expected all git calls to have timeout, got: {calls_with_timeout}"


class TestCriticalWriteFailures:
    @pytest.mark.asyncio
    async def test_build_checkpoint_write_fails_closed(self, tmp_git_repo):
        from otto.observability import write_json_file as real_write_json_file

        def fake_write_json_file(path, data, *, strict=False):
            if path.name == "checkpoint.json":
                raise OSError("disk full")
            return real_write_json_file(path, data, strict=strict)

        with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(
            "STORY_RESULT: smoke | PASS | ok\n"
            "COVERAGE_OBSERVED:\n"
            "- Exercised mocked smoke story\n"
            "COVERAGE_GAPS:\n"
            "- Did not model deeper product-specific coverage in this mocked transcript\n"
            "VERDICT: PASS\n"
        )), patch("otto.observability.write_json_file", side_effect=fake_write_json_file):
            with pytest.raises(RuntimeError, match="Failed to write checkpoint"):
                await build_agentic_v3("test", tmp_git_repo, {})

    def test_session_summary_write_fails_closed(self, tmp_path):
        from otto.pipeline import _write_session_summary

        with patch("otto.observability.write_json_file", side_effect=OSError("read-only fs")):
            with pytest.raises(RuntimeError, match="Failed to write session summary"):
                _write_session_summary(
                    tmp_path,
                    "run-1",
                    verdict="passed",
                    passed=True,
                    cost=0.1,
                    duration=1.0,
                    stories_passed=1,
                    stories_tested=1,
                    rounds=1,
                )

    def test_session_summary_normalizes_breakdown_to_total_duration(self, tmp_path):
        from otto import paths as _paths
        from otto.pipeline import _write_session_summary

        _write_session_summary(
            tmp_path,
            "run-1",
            verdict="passed",
            passed=True,
            cost=2.96,
            duration=1084.127,
            stories_passed=24,
            stories_tested=24,
            rounds=2,
            breakdown={
                "build": {"duration_s": 155.255},
                "certify": {"duration_s": 769.748, "rounds": 2},
            },
        )

        summary = json.loads(_paths.session_summary(tmp_path, "run-1").read_text())
        total = sum(
            float(entry["duration_s"])
            for entry in summary["breakdown"].values()
            if isinstance(entry.get("duration_s"), int | float)
        )
        assert abs(total - 1084.127) < 0.01
        assert abs(float(summary["breakdown"]["build"]["duration_s"]) - 314.379) < 0.01

    def test_write_json_atomic_raises_and_cleans_temp_file_on_replace_failure(self, tmp_path, monkeypatch):
        from otto.observability import write_json_atomic

        target = tmp_path / "report.json"

        def fail_replace(*_args, **_kwargs):
            raise OSError("replace failed")

        monkeypatch.setattr("otto.observability.os.replace", fail_replace)

        with pytest.raises(OSError, match="replace failed"):
            write_json_atomic(target, {"ok": True})

        assert list(tmp_path.glob(".report.json.*.tmp")) == []
        assert not target.exists()

    def test_write_json_atomic_raises_on_directory_fsync_failure(self, tmp_path, monkeypatch):
        from otto.observability import write_json_atomic

        target = tmp_path / "report.json"
        real_fsync = os.fsync
        call_count = {"n": 0}

        def fake_fsync(fd):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise OSError("dir fsync failed")
            return real_fsync(fd)

        monkeypatch.setattr("otto.observability.os.fsync", fake_fsync)

        with pytest.raises(OSError, match="dir fsync failed"):
            write_json_atomic(target, {"ok": True})

        assert target.exists()
        assert list(tmp_path.glob(".report.json.*.tmp")) == []

    def test_write_json_atomic_cleans_temp_file_on_file_fsync_failure(self, tmp_path, monkeypatch):
        from otto.observability import write_json_atomic

        target = tmp_path / "report.json"

        def fail_fsync(_fd):
            raise OSError("file fsync failed")

        monkeypatch.setattr("otto.observability.os.fsync", fail_fsync)

        with pytest.raises(OSError, match="file fsync failed"):
            write_json_atomic(target, {"ok": True})

        assert list(tmp_path.glob(".report.json.*.tmp")) == []
        assert not target.exists()


# -- Test: cross-run memory --

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


class TestPhaseBuildResumeFix:
    """Regression: spec-approved → build resume must skip spec regeneration.

    Reproduces Codex Plan Gate Round 1 HIGH #4: before the fix, the build
    agent's checkpoint lacked an explicit `phase` field, so a kill-mid-build
    followed by `--resume` re-entered the spec phase and regenerated the
    spec. The fix writes `phase="build"` on every build-phase checkpoint.
    """

    @pytest.mark.asyncio
    async def test_build_checkpoint_has_phase_build(self, tmp_git_repo):
        """After a successful build, the session checkpoint should carry
        phase='build' so resume treats it as past spec_approved."""
        from otto import paths as _paths
        from otto.checkpoint import write_checkpoint as real_write_checkpoint

        seen_phases: list[tuple[str, str]] = []

        def spy_write_checkpoint(*args, **kwargs):
            seen_phases.append((kwargs.get("status", ""), kwargs.get("phase", "")))
            return real_write_checkpoint(*args, **kwargs)

        async def ok_agent(prompt, options, **kwargs):
            return (
                "CERTIFY_ROUND: 1\nSTORIES_TESTED: 1\nSTORIES_PASSED: 1\n"
                "STORY_RESULT: s1 | PASS | fine\n"
                "COVERAGE_OBSERVED:\n"
                "- Exercised the single mocked success story in this test\n"
                "COVERAGE_GAPS:\n"
                "- Did not exercise any additional mocked product-specific coverage in this test\n"
                "VERDICT: PASS\nDIAGNOSIS: null\n",
                0.1,
                MagicMock(session_id="sdk-sid-abc"),
            )

        with patch("otto.checkpoint.write_checkpoint", side_effect=spy_write_checkpoint), \
             patch("otto.agent.run_agent_query", side_effect=ok_agent):
            result = await build_agentic_v3("test", tmp_git_repo, {})
        assert result.passed
        assert ("in_progress", "build") in seen_phases

        # The session dir exists; checkpoint was marked completed and
        # removed, but the summary.json in build/ records the session.
        build_dir = _paths.build_dir(tmp_git_repo, result.build_id)
        assert build_dir.exists(), "session build dir should exist"

    @pytest.mark.asyncio
    async def test_paused_build_checkpoint_has_phase_build(self, tmp_git_repo):
        """Kill mid-build → paused checkpoint has phase='build', not 'spec'."""
        from otto import paths as _paths

        async def crashing(prompt, options, **kwargs):
            raise RuntimeError("mid-build crash")

        with patch("otto.agent.run_agent_query", side_effect=crashing):
            await build_agentic_v3("test", tmp_git_repo, {})

        sess = _paths.resolve_pointer(tmp_git_repo, _paths.PAUSED_POINTER)
        assert sess is not None, "paused pointer must be set after build crash"
        cp = json.loads((sess / "checkpoint.json").read_text())
        assert cp.get("phase") == "build", (
            f"crash-paused checkpoint must record phase='build' so --resume "
            f"does not regenerate spec; got phase={cp.get('phase')!r}"
        )


class TestCheckpointRegression:
    """Regression tests for checkpoint/resume edge cases."""

    def test_load_checkpoint_ignores_truncated_json(self, tmp_path):
        """Partial checkpoint writes should load as None, not crash.

        Writes a truncated *legacy* checkpoint (new layout requires a valid
        session_id path, so legacy exercises the same code path)."""
        from otto.checkpoint import load_checkpoint
        from otto.paths import LEGACY_CHECKPOINT, LOGS_ROOT_NAME

        checkpoint_path = tmp_path / LOGS_ROOT_NAME / LEGACY_CHECKPOINT
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_path.write_text('{"status": "in_progress", "current_round": ')
        tmp_file = checkpoint_path.with_name(checkpoint_path.name + ".tmp")
        tmp_file.write_text('{"stale": true}')

        assert load_checkpoint(tmp_path) is None
        assert tmp_file.exists()

    def test_load_checkpoint_handles_file_removed_between_checks(self, tmp_path):
        """Concurrent checkpoint deletion should be treated as no checkpoint."""
        from otto.checkpoint import load_checkpoint

        with patch("pathlib.Path.read_text", side_effect=FileNotFoundError):
            assert load_checkpoint(tmp_path) is None

    @pytest.mark.asyncio
    async def test_agent_interrupt_refreshes_paused_checkpoint_fingerprint(self, tmp_git_repo):
        """SIGTERM/KeyboardInterrupt after agent work must leave a safe resume checkpoint."""
        from otto.checkpoint import load_checkpoint, resolve_resume

        async def interrupt_agent(*args, **kwargs):
            (tmp_git_repo / "agent-work.txt").write_text("partial work\n", encoding="utf-8")
            subprocess.run(["git", "add", "agent-work.txt"], cwd=tmp_git_repo, check=True)
            subprocess.run(
                ["git", "commit", "-q", "-m", "partial agent work"],
                cwd=tmp_git_repo,
                check=True,
            )
            raise KeyboardInterrupt()

        with patch("otto.agent.run_agent_with_timeout", side_effect=interrupt_agent):
            with pytest.raises(KeyboardInterrupt):
                await build_agentic_v3(
                    "resume build",
                    tmp_git_repo,
                    {},
                    resume_session_id="sess-resume-123",
                )

        checkpoint = load_checkpoint(tmp_git_repo)
        current_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        assert checkpoint["status"] == "paused"
        assert checkpoint["agent_session_id"] == "sess-resume-123"
        assert checkpoint["git_sha"] == current_head
        state = resolve_resume(tmp_git_repo, resume=True, expected_command="build", reject_incompatible=True)
        assert state.resumed
        assert not state.fingerprint_mismatch

    @pytest.mark.asyncio
    async def test_split_resume_tracks_phase_and_last_completed_round(self, tmp_git_repo):
        """Split-mode resume must preserve initial-build state and replay interrupted rounds."""
        from otto.checkpoint import clear_checkpoint, load_checkpoint, resolve_resume
        from otto.pipeline import run_certify_fix_loop

        async def interrupt_initial_build(*args, **kwargs):
            raise KeyboardInterrupt()

        with patch("otto.pipeline.build_agentic_v3", side_effect=interrupt_initial_build):
            with pytest.raises(KeyboardInterrupt):
                await run_certify_fix_loop(
                    "split build",
                    tmp_git_repo,
                    {},
                    skip_initial_build=False,
                    command="build",
                )

        checkpoint = load_checkpoint(tmp_git_repo)
        assert checkpoint["status"] == "in_progress"
        assert checkpoint["phase"] == "initial_build"
        assert checkpoint["current_round"] == 0
        assert checkpoint["split_mode"] is True

        state = resolve_resume(tmp_git_repo, resume=True, expected_command="build")
        assert state.resumed
        assert state.phase == "initial_build"
        assert state.start_round == 1
        assert state.split_mode is True

        clear_checkpoint(tmp_git_repo)

        async def interrupt_certifier(*args, **kwargs):
            raise KeyboardInterrupt()

        with patch("otto.certifier.run_agentic_certifier", side_effect=interrupt_certifier):
            with pytest.raises(KeyboardInterrupt):
                await run_certify_fix_loop(
                    "split improve",
                    tmp_git_repo,
                    {},
                    skip_initial_build=True,
                    start_round=1,
                    command="improve.feature",
                )

        checkpoint = load_checkpoint(tmp_git_repo)
        assert checkpoint["status"] == "paused"
        assert checkpoint["phase"] == "certify"
        assert checkpoint["current_round"] == 0
        assert checkpoint["split_mode"] is True

        state = resolve_resume(tmp_git_repo, resume=True, expected_command="improve.feature")
        assert state.resumed
        assert state.phase == "certify"
        assert state.start_round == 1
        assert state.split_mode is True

    @pytest.mark.asyncio
    async def test_split_checkpoint_persists_intent_and_paused_session_id(self, tmp_git_repo):
        from otto.checkpoint import load_checkpoint
        from otto.pipeline import run_certify_fix_loop
        from otto.agent import AgentCallError

        async def paused_build(*args, **kwargs):
            raise AgentCallError("Timed out after 30s", session_id="sdk-split-123")

        with patch("otto.pipeline.build_agentic_v3", side_effect=paused_build):
            result = await run_certify_fix_loop(
                "split build intent",
                tmp_git_repo,
                {},
                skip_initial_build=False,
                command="build",
            )

        checkpoint = load_checkpoint(tmp_git_repo)
        assert result.passed is False
        assert checkpoint["intent"] == "split build intent"
        assert checkpoint["agent_session_id"] == "sdk-split-123"

    @pytest.mark.asyncio
    async def test_split_loop_persists_attempt_history_and_rich_round_state(self, tmp_git_repo):
        from otto import paths as _paths
        from otto.pipeline import run_certify_fix_loop

        reports = [
            type("Report", (), {
                "cost_usd": 0.2,
                "story_results": [{"story_id": "auth", "summary": "auth fails", "passed": False}],
                "metric_met": None,
                "metric_value": "",
                "diagnosis": "Missing auth check",
                "child_session_ids": ["child-cert-1"],
                "subagent_errors": [],
            })(),
            type("Report", (), {
                "cost_usd": 0.2,
                "story_results": [{"story_id": "auth", "summary": "auth fails", "passed": False}],
                "metric_met": None,
                "metric_value": "",
                "diagnosis": "Still missing auth check",
                "child_session_ids": ["child-cert-2"],
                "subagent_errors": [],
            })(),
        ]

        async def fake_certifier(*args, **kwargs):
            return reports.pop(0)

        async def fake_fix(*args, **kwargs):
            return BuildResult(
                passed=True,
                build_id="split-run-1",
                total_cost=0.1,
                child_session_ids=["child-fix-1"],
            )

        with patch("otto.certifier.run_agentic_certifier", side_effect=fake_certifier), \
             patch("otto.pipeline.build_agentic_v3", side_effect=fake_fix):
            result = await run_certify_fix_loop(
                "split improve",
                tmp_git_repo,
                {"max_certify_rounds": 2},
                skip_initial_build=True,
                session_id="split-run-1",
                command="improve.bugs",
            )

        assert result.passed is False

        attempt_history = json.loads(
            (_paths.improve_dir(tmp_git_repo, "split-run-1") / "attempt-history.json").read_text()
        )
        assert attempt_history == [{
            "round": 1,
            "failing_story_ids": ["auth"],
            "diagnosis": "Missing auth check",
            "fix_commit_sha": "",
            "fix_diff_stat": "(no changes)",
            "still_failing_after_fix": ["auth"],
        }]

        checkpoint = json.loads(_paths.session_checkpoint(tmp_git_repo, "split-run-1").read_text())
        assert checkpoint["child_session_ids"] == ["child-cert-1", "child-cert-2", "child-fix-1"]
        assert checkpoint["rounds"][0]["failing_story_ids"] == ["auth"]
        assert checkpoint["rounds"][0]["diagnosis"] == "Missing auth check"
        assert checkpoint["rounds"][0]["fix_diff_stat"] == "(no changes)"
        assert checkpoint["rounds"][0]["still_failing_after_fix"] == ["auth"]

    @pytest.mark.asyncio
    async def test_split_resume_reuses_paused_agent_session_id(self, tmp_git_repo):
        from otto.pipeline import run_certify_fix_loop

        async def fake_run_agent_query(prompt, options, **kwargs):
            resumed = options.resume == "sdk-split-123"
            result_msg = MagicMock()
            result_msg.session_id = options.resume or "fresh-session"
            result_msg.subtype = "success"
            result_msg.is_error = False
            result_msg.result = None
            result_msg.total_cost_usd = 0.2 if resumed else 9.0
            result_msg.usage = None
            text = (
                "STORIES_TESTED: 1\n"
                "STORIES_PASSED: 1\n"
                "STORY_RESULT: smoke | PASS | claim=Resume reused session | observed_result=OK | surface=HTTP | methodology=http-request | summary=Smoke passed\n"
                "COVERAGE_OBSERVED:\n"
                "- Exercised the smoke story over HTTP and observed an OK result\n"
                "COVERAGE_GAPS:\n"
                "- Did not exercise any deeper product-specific coverage in this mocked run\n"
                "VERDICT: PASS\n"
                "DIAGNOSIS: null\n"
            )
            return text, result_msg.total_cost_usd, result_msg

        async def passing_certifier(*args, **kwargs):
            report = MagicMock()
            report.outcome = MagicMock(value="passed")
            report.story_results = [{"story_id": "smoke", "passed": True, "summary": "ok"}]
            report.cost_usd = 0.1
            report.duration_s = 1.0
            report.metric_met = None
            report.metric_value = ""
            return report

        with patch("otto.agent.run_agent_query", side_effect=fake_run_agent_query), patch(
            "otto.certifier.run_agentic_certifier", side_effect=passing_certifier
        ):
            result = await run_certify_fix_loop(
                "split resume intent",
                tmp_git_repo,
                {},
                skip_initial_build=False,
                command="build",
                resume_session_id="sdk-split-123",
            )

        assert result.passed is True
        assert result.total_cost == pytest.approx(0.3)


class TestBuildResume:
    """End-to-end: `otto build --resume` picks up split-mode checkpoint."""

    def test_build_cli_exposes_resume_flag(self):
        """--resume is wired on otto build and intent is optional when resuming."""
        from click.testing import CliRunner
        from otto.cli import main

        r = CliRunner().invoke(main, ["build", "--help"])
        assert r.exit_code == 0
        assert "--resume" in r.output
        assert "--allow-dirty" in r.output
        assert "--break-lock" in r.output
        assert "[INTENT]" in r.output  # intent is optional

    def test_build_refuses_dirty_repo_without_allow_dirty(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.cli import main

        tracked = tmp_git_repo / "tracked.txt"
        tracked.write_text("seed\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "seed tracked"], cwd=tmp_git_repo, check=True)
        tracked.write_text("dirty edit\n")
        monkeypatch.chdir(tmp_git_repo)

        with patch("otto.pipeline.build_agentic_v3") as build_agent:
            result = CliRunner().invoke(
                main,
                ["build", "demo app", "--no-qa"],
                catch_exceptions=False,
            )

        assert result.exit_code == 2
        assert "Refusing to run Otto in the current git state" in result.output
        build_agent.assert_not_called()

    def test_build_allow_dirty_opt_in_reaches_pipeline(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.cli import main

        tracked = tmp_git_repo / "tracked.txt"
        tracked.write_text("seed\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "seed tracked"], cwd=tmp_git_repo, check=True)
        tracked.write_text("dirty edit\n")
        monkeypatch.chdir(tmp_git_repo)

        with patch(
            "otto.pipeline.build_agentic_v3",
            return_value=BuildResult(
                passed=True,
                build_id="run-1",
                total_cost=0.0,
                total_duration=1.0,
                tasks_passed=0,
                tasks_failed=0,
            ),
        ) as build_agent:
            result = CliRunner().invoke(
                main,
                ["build", "demo app", "--no-qa", "--allow-dirty"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        build_agent.assert_called_once()

    def test_improve_refuses_dirty_repo_before_branch_creation(self, tmp_git_repo):
        from otto.cli_improve import _run_improve_locked

        tracked = tmp_git_repo / "tracked.txt"
        tracked.write_text("seed\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "seed tracked"], cwd=tmp_git_repo, check=True)
        tracked.write_text("dirty edit\n")
        resume_state = SimpleNamespace(
            resumed=False,
            max_rounds=0,
            split_mode=None,
            focus="",
            certifier_mode="",
            run_id="",
            session_started_at=None,
            start_round=1,
            total_cost=0.0,
            total_duration=0.0,
            rounds=[],
            agent_session_id="",
        )

        with patch("otto.cli_improve._create_improve_branch", return_value="improve/dirty") as create_branch:
            with pytest.raises(SystemExit) as exc:
                _run_improve_locked(
                    project_dir=tmp_git_repo,
                    intent="demo app",
                    rounds=1,
                    focus=None,
                    certifier_mode="fast",
                    command_label="Improve",
                    command_id="improve.bugs",
                    subcommand="bugs",
                    target=None,
                    split=False,
                    agentic=False,
                    resume=False,
                    resume_state=resume_state,
                    run_id="improve-run-1",
                )

        assert exc.value.code == 2
        create_branch.assert_not_called()

    def test_improve_honors_allow_dirty_repo_config_before_branch_creation(self, tmp_git_repo):
        from otto.cli_improve import _run_improve_locked

        (tmp_git_repo / "otto.yaml").write_text("allow_dirty_repo: true\n", encoding="utf-8")
        tracked = tmp_git_repo / "tracked.txt"
        tracked.write_text("seed\n")
        subprocess.run(["git", "add", "tracked.txt", "otto.yaml"], cwd=tmp_git_repo, check=True)
        subprocess.run(["git", "commit", "-m", "seed tracked"], cwd=tmp_git_repo, check=True)
        tracked.write_text("dirty edit\n")
        resume_state = SimpleNamespace(
            resumed=False,
            max_rounds=0,
            split_mode=None,
            focus="",
            certifier_mode="",
            run_id="",
            session_started_at=None,
            start_round=1,
            total_cost=0.0,
            total_duration=0.0,
            rounds=[],
            agent_session_id="",
        )

        async def _fake_build_agentic_v3(*_args, **_kwargs):
            return BuildResult(
                passed=True,
                build_id="improve-run-1",
                total_cost=0.0,
                total_duration=1.0,
                tasks_passed=0,
                tasks_failed=0,
            )

        with patch("otto.cli_improve._create_improve_branch", return_value="improve/dirty") as create_branch:
            with patch("otto.pipeline.build_agentic_v3", side_effect=_fake_build_agentic_v3) as build_agent:
                with pytest.raises(SystemExit) as exc:
                    _run_improve_locked(
                        project_dir=tmp_git_repo,
                        intent="demo app",
                        rounds=1,
                        focus=None,
                        certifier_mode="fast",
                        command_label="Improve",
                        command_id="improve.bugs",
                        subcommand="bugs",
                        target=None,
                        split=False,
                        agentic=True,
                        resume=False,
                        resume_state=resume_state,
                        run_id="improve-run-1",
                    )

        assert exc.value.code == 0
        create_branch.assert_called_once()
        build_agent.assert_called_once()

    def test_certify_cli_exposes_break_lock_flag(self):
        """Standalone certify should expose the manual lock escape hatch."""
        from click.testing import CliRunner
        from otto.cli import main

        r = CliRunner().invoke(main, ["certify", "--help"])
        assert r.exit_code == 0
        assert "--break-lock" in r.output

    def test_build_cli_normalizes_multiline_intent(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.cli import main

        captured = {}

        async def fake_build(intent, project_dir, config, **kwargs):
            captured["intent"] = intent
            return BuildResult(
                passed=True,
                build_id="run-1",
                total_cost=0.0,
                tasks_passed=1,
                tasks_failed=0,
            )

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.pipeline.build_agentic_v3", side_effect=fake_build):
            result = CliRunner().invoke(
                main,
                ["build", "a kanban board:\n  localStorage", "--agentic"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert captured["intent"] == "a kanban board: localStorage"


class TestCliProjectRootResolution:
    def test_build_uses_repo_root_from_subdirectory(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.cli import main

        nested = tmp_git_repo / "src" / "nested"
        nested.mkdir(parents=True)
        captured: dict[str, Path] = {}
        monkeypatch.chdir(nested)

        def fake_build_locked(*args, **kwargs):
            captured["project_dir"] = args[-1]

        with patch("otto.paths.project_lock", return_value=contextlib.nullcontext()), patch(
            "otto.cli._build_locked", side_effect=fake_build_locked
        ):
            result = CliRunner().invoke(main, ["build", "test intent"], catch_exceptions=False)

        assert result.exit_code == 0
        assert captured["project_dir"] == tmp_git_repo.resolve()

    def test_build_in_worktree_preserves_cli_overrides_after_config_reload(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.cli import main

        (tmp_git_repo / "otto.yaml").write_text(
            "model: yaml-model\n"
            "provider: claude\n"
            "max_turns_per_call: 99\n",
            encoding="utf-8",
        )
        captured: dict[str, object] = {}
        run_id_dirs: list[Path] = []
        saved_cwd = Path.cwd()
        monkeypatch.chdir(tmp_git_repo)

        async def fake_build(intent, project_dir, config, **kwargs):
            del intent, kwargs
            captured["project_dir"] = project_dir
            captured["config"] = dict(config)
            return BuildResult(
                passed=True,
                build_id="run-1",
                total_cost=0.0,
                tasks_passed=1,
                tasks_failed=0,
            )

        def fake_new_run_id(project_dir=None):
            run_id_dirs.append(Path(project_dir))
            return "run-1"

        try:
            with patch("otto.pipeline.build_agentic_v3", side_effect=fake_build), patch(
                "otto.cli._new_run_id",
                side_effect=fake_new_run_id,
            ):
                result = CliRunner().invoke(
                    main,
                    [
                        "build",
                        "test intent",
                        "--agentic",
                        "--in-worktree",
                        "--budget",
                        "123",
                        "--max-turns",
                        "17",
                        "--model",
                        "cli-model",
                        "--provider",
                        "codex",
                        "--effort",
                        "high",
                        "--strict",
                        "--allow-dirty",
                        "--fast",
                    ],
                    catch_exceptions=False,
                )
        finally:
            os.chdir(saved_cwd)

        assert result.exit_code == 0, result.output
        config = captured["config"]
        assert Path(captured["project_dir"]).is_relative_to(tmp_git_repo / ".worktrees")
        assert run_id_dirs == [captured["project_dir"]]
        assert config["run_budget_seconds"] == 123
        assert config["max_turns_per_call"] == 17
        assert config["model"] == "cli-model"
        assert config["provider"] == "codex"
        assert config["effort"] == "high"
        assert config["strict_mode"] is True
        assert config["allow_dirty_repo"] is True
        assert config["certifier_mode"] == "fast"

    def test_improve_uses_repo_root_from_subdirectory(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.cli import main

        nested = tmp_git_repo / "src" / "nested"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)

        with patch("otto.cli_improve._require_intent", return_value="intent"), patch(
            "otto.cli_improve._run_improve"
        ) as run_improve:
            result = CliRunner().invoke(main, ["improve", "bugs"], catch_exceptions=False)

        assert result.exit_code == 0
        assert run_improve.call_args.kwargs["project_dir"] == tmp_git_repo.resolve()

    def test_history_and_replay_use_repo_root_from_subdirectory(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.cli import main

        nested = tmp_git_repo / "src" / "nested"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)

        with patch("otto.cli_logs._load_history_entries", return_value=[]) as load_history, patch(
            "otto.replay.replay_session", return_value=[]
        ) as replay_session:
            history_result = CliRunner().invoke(main, ["history"], catch_exceptions=False)
            replay_result = CliRunner().invoke(main, ["replay", "session-123"], catch_exceptions=False)

        assert history_result.exit_code == 0
        assert replay_result.exit_code == 0
        assert load_history.call_args.args[0] == tmp_git_repo.resolve()
        assert replay_session.call_args.args[0] == tmp_git_repo.resolve()

    def test_certify_cli_normalizes_multiline_intent(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.certifier.report import CertificationOutcome, CertificationReport
        from otto.cli import main

        captured = {}

        async def fake_certify(intent, project_dir, config=None, **kwargs):
            captured["intent"] = intent
            return CertificationReport(
                outcome=CertificationOutcome.PASSED,
                cost_usd=0.0,
                duration_s=1.0,
                story_results=[{"story_id": "smoke", "passed": True, "summary": "ok"}],
            )

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.certifier.run_agentic_certifier", side_effect=fake_certify):
            result = CliRunner().invoke(
                main,
                ["certify", "a kanban board:\n  localStorage"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert captured["intent"] == "a kanban board: localStorage"

    def test_improve_normalizes_resolved_intent(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.cli import main

        (tmp_git_repo / "intent.md").write_text("a kanban board:\n  localStorage")
        captured = {}

        async def fake_build(intent, project_dir, config, **kwargs):
            captured["intent"] = intent
            return BuildResult(
                passed=True,
                build_id="run-2",
                total_cost=0.0,
                tasks_passed=1,
                tasks_failed=0,
            )

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.cli_improve._create_improve_branch", return_value="improve/2026-04-21"), \
             patch("otto.pipeline.build_agentic_v3", side_effect=fake_build):
            result = CliRunner().invoke(
                main,
                ["improve", "bugs", "--agentic", "--allow-dirty"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert captured["intent"] == "a kanban board: localStorage"

    def test_build_without_intent_without_resume_errors(self, tmp_git_repo, monkeypatch):
        """Missing intent and no checkpoint → exits 2."""
        from click.testing import CliRunner
        from otto.cli import main

        monkeypatch.chdir(tmp_git_repo)
        r = CliRunner().invoke(main, ["build"])
        assert r.exit_code == 2

    def test_build_without_intent_with_stale_checkpoint_errors(
        self, tmp_git_repo, monkeypatch
    ):
        """--resume with no matching in-progress checkpoint and no intent → exits 2."""
        from click.testing import CliRunner
        from otto.cli import main

        monkeypatch.chdir(tmp_git_repo)
        # No checkpoint written, so --resume falls back to fresh — but then
        # intent is required.
        r = CliRunner().invoke(main, ["build", "--resume"])
        assert r.exit_code == 2

    def test_build_resume_rejects_cross_command_checkpoint(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.checkpoint import write_checkpoint
        from otto.cli import main

        write_checkpoint(
            tmp_git_repo,
            run_id="r1",
            command="improve.bugs",
            status="paused",
        )

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.pipeline.build_agentic_v3") as build_agent:
            result = CliRunner().invoke(main, ["build", "--resume"], catch_exceptions=False)

        assert result.exit_code == 2
        assert "Checkpoint command mismatch" in result.output
        assert "improve.bugs" in result.output
        assert build_agent.call_count == 0

    def test_build_resume_rejects_cli_intent_change(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.checkpoint import write_checkpoint
        from otto.cli import main

        write_checkpoint(
            tmp_git_repo,
            run_id="r1",
            command="build",
            status="paused",
            intent="old intent",
        )

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.pipeline.build_agentic_v3") as build_agent:
            result = CliRunner().invoke(
                main,
                ["build", "new intent", "--resume"],
                catch_exceptions=False,
            )

        assert result.exit_code == 2
        assert "Intent mismatch on resume" in result.output
        assert "checkpoint intent: 'old intent'" in result.output
        assert "CLI intent:        'new intent'" in result.output
        assert build_agent.call_count == 0

    @pytest.mark.asyncio
    async def test_build_agent_checkpoint_preserves_intent_for_resume(self, tmp_git_repo):
        from otto.agent import AgentCallError
        from otto.checkpoint import load_checkpoint

        async def fail_after_precheckpoint(*args, **kwargs):
            raise AgentCallError("Timed out after 10s", session_id="sess-123")

        with patch("otto.agent.make_agent_options", return_value=SimpleNamespace(resume=None)), \
             patch("otto.agent.run_agent_with_timeout", side_effect=fail_after_precheckpoint):
            result = await build_agentic_v3(
                "old intent",
                tmp_git_repo,
                {"skip_product_qa": True},
                run_id="run-1",
            )

        checkpoint = load_checkpoint(tmp_git_repo)
        assert result.passed is False
        assert checkpoint is not None
        assert checkpoint["status"] == "paused"
        assert checkpoint["intent"] == "old intent"

    def test_build_resume_reports_completed_last_run(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto import paths as _paths
        from otto.cli import main

        session_id = "2026-04-21-200000-abcdef"
        _paths.ensure_session_scaffold(tmp_git_repo, session_id)
        _paths.session_summary(tmp_git_repo, session_id).write_text(json.dumps({
            "run_id": session_id,
            "command": "build",
            "status": "completed",
            "verdict": "passed",
            "completed_at": "2026-04-21T20:05:00Z",
        }))

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.pipeline.build_agentic_v3") as build_agent:
            result = CliRunner().invoke(main, ["build", "--resume"], catch_exceptions=False)

        assert result.exit_code == 2
        assert f"Last run completed (session {session_id}, verdict passed)." in result.output
        assert "Nothing to resume" in result.output
        assert build_agent.call_count == 0

    def test_build_resume_reports_deleted_paused_session(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto import paths
        from otto.cli import main

        paths.logs_dir(tmp_git_repo).mkdir(parents=True, exist_ok=True)
        (paths.logs_dir(tmp_git_repo) / "paused.txt").write_text("missing-run\n")

        monkeypatch.chdir(tmp_git_repo)
        result = CliRunner().invoke(main, ["build", "--resume"])

        assert result.exit_code == 2
        assert "deleted; nothing to resume" in result.output
        assert "missing-run" in result.output

    def test_reject_spec_and_spec_file_mutex(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.cli import main

        spec_path = tmp_git_repo / "spec.md"
        spec_path.write_text(MINIMAL_VALID)

        monkeypatch.chdir(tmp_git_repo)
        result = CliRunner().invoke(
            main,
            ["build", "counter app", "--spec", "--spec-file", str(spec_path)],
        )

        assert result.exit_code == 2
        assert "--spec and --spec-file are mutually exclusive" in result.output

    def test_reject_conflicting_intent_with_spec_file(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.cli import main

        spec_path = tmp_git_repo / "spec.md"
        spec_path.write_text(MINIMAL_VALID)

        monkeypatch.chdir(tmp_git_repo)
        result = CliRunner().invoke(
            main,
            ["build", "different intent", "--spec-file", str(spec_path)],
        )

        assert result.exit_code == 2
        assert "Intent mismatch" in result.output

    def test_build_spec_rejects_yaml_skip_product_qa(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.cli import main

        (tmp_git_repo / "otto.yaml").write_text("skip_product_qa: true\n")

        monkeypatch.chdir(tmp_git_repo)
        result = CliRunner().invoke(
            main,
            ["build", "counter app", "--spec", "--allow-dirty"],
        )

        assert result.exit_code == 2
        assert "skip_product_qa is" in result.output
        assert "otto.yaml: skip_product_qa: true" in result.output

    def test_build_reports_malformed_yaml_cleanly(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.cli import main

        (tmp_git_repo / "otto.yaml").write_text("- invalid\n- root\n")

        monkeypatch.chdir(tmp_git_repo)
        result = CliRunner().invoke(main, ["build", "counter app"])

        assert result.exit_code == 2
        assert "Malformed config" in result.output
        assert "expected a YAML mapping" in result.output

    def test_build_resume_without_intent_skips_placeholder_prompt_and_append(
        self, tmp_git_repo, monkeypatch
    ):
        """Resume without INTENT should not append or leak `(resumed run)`."""
        from click.testing import CliRunner
        from otto.checkpoint import write_checkpoint
        from otto.cli import main

        intent_path = tmp_git_repo / "intent.md"
        original_intent = "# Build Intents\n\n## 2026-04-13 12:00 (build-1)\nexisting intent\n"
        intent_path.write_text(original_intent)
        write_checkpoint(
            tmp_git_repo,
            run_id="r1",
            command="build",
            session_id="sess-resume-123",
            status="in_progress",
            split_mode=False,
        )

        captured_prompts = []

        async def capture_query(prompt, options, **kwargs):
            captured_prompts.append(prompt)
            return (
                "CERTIFY_ROUND: 1\n"
                "STORIES_TESTED: 1\n"
                "STORIES_PASSED: 1\n"
                "STORY_RESULT: smoke | PASS | Works\n"
                "VERDICT: PASS\n"
                "DIAGNOSIS: null\n",
                0.10,
                MagicMock(session_id="sess-resume-123"),
            )

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.agent.run_agent_query", side_effect=capture_query):
            result = CliRunner().invoke(main, ["build", "--resume"], catch_exceptions=False)

        assert result.exit_code == 0
        # intent.md is unchanged (not re-appended)
        assert intent_path.read_text() == original_intent
        # No placeholder leaked into the resumed session's prompt
        joined_prompts = "".join(captured_prompts)
        assert "(resumed run)" not in joined_prompts
        # At least one prompt was captured; the exact contents are a
        # resumption detail that the SDK / Codex CLI may evolve, so don't
        # pin the exact shape.
        assert captured_prompts, "resume path never called the agent"

    def test_split_resume_with_empty_phase_replays_initial_build(
        self, tmp_git_repo, monkeypatch
    ):
        """Old checkpoints with phase='' must not skip the initial build."""
        from click.testing import CliRunner
        from otto.checkpoint import write_checkpoint
        from otto.cli import main

        write_checkpoint(
            tmp_git_repo,
            run_id="r1",
            command="build",
            status="in_progress",
            phase="",
        )

        captured = {}

        async def capture_loop(intent, project_dir, config, **kwargs):
            captured["skip_initial_build"] = kwargs["skip_initial_build"]
            return BuildResult(
                passed=True,
                build_id="build-1",
                total_cost=0.0,
                tasks_passed=1,
                tasks_failed=0,
            )

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.pipeline.run_certify_fix_loop", side_effect=capture_loop):
            result = CliRunner().invoke(
                main,
                ["build", "resume build", "--split", "--resume"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert captured["skip_initial_build"] is False

    def test_split_build_threads_run_id_and_spec_into_pipeline(
        self, tmp_git_repo, monkeypatch
    ):
        """Split build should preserve the spec phase session and cost."""
        from click.testing import CliRunner
        from otto.cli import main

        captured = {}

        async def fake_spec_phase(**kwargs):
            return "run-spec-123", "# Approved Spec", 1.25, 12.0

        async def fake_loop(intent, project_dir, config, **kwargs):
            captured.update(kwargs)
            return BuildResult(
                passed=True,
                build_id="run-spec-123",
                total_cost=1.25,
                tasks_passed=1,
                tasks_failed=0,
            )

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.cli._run_spec_phase", side_effect=fake_spec_phase), \
             patch("otto.pipeline.run_certify_fix_loop", side_effect=fake_loop):
            result = CliRunner().invoke(
                main,
                ["build", "spec build", "--split", "--spec", "--yes"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert captured["session_id"] == "run-spec-123"
        assert captured["spec"] == "# Approved Spec"
        assert captured["spec_cost"] == 1.25
        assert captured["spec_duration"] == 12.0

    def test_build_uses_yaml_split_mode_and_resume_preserves_it(
        self, tmp_git_repo, monkeypatch
    ):
        from click.testing import CliRunner
        from otto.checkpoint import write_checkpoint
        from otto.cli import main

        (tmp_git_repo / "otto.yaml").write_text("split_mode: true\n")
        captured = {}

        async def fake_loop(intent, project_dir, config, **kwargs):
            captured.update(kwargs)
            return BuildResult(
                passed=True,
                build_id="split-run-1",
                total_cost=0.0,
                tasks_passed=1,
                tasks_failed=0,
            )

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.pipeline.run_certify_fix_loop", side_effect=fake_loop), \
             patch("otto.pipeline.build_agentic_v3", new=AsyncMock(side_effect=AssertionError("agentic path should not run"))):
            result = CliRunner().invoke(
                main,
                ["build", "split build", "--allow-dirty"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert captured["session_id"]
        assert captured["command"] == "build"

        write_checkpoint(
            tmp_git_repo,
            run_id="split-run-1",
            command="build",
            status="paused",
            phase="certify",
            split_mode=True,
        )
        captured.clear()

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.pipeline.run_certify_fix_loop", side_effect=fake_loop), \
             patch("otto.pipeline.build_agentic_v3", new=AsyncMock(side_effect=AssertionError("agentic path should not run"))):
            result = CliRunner().invoke(
                main,
                ["build", "split build", "--resume", "--allow-dirty"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert captured["command"] == "build"

    def test_build_defaults_to_split_and_agentic_flag_overrides(
        self, tmp_git_repo, monkeypatch
    ):
        from click.testing import CliRunner
        from otto.cli import main

        async def fake_loop(intent, project_dir, config, **kwargs):
            return BuildResult(
                passed=True,
                build_id="split-default",
                total_cost=0.0,
                tasks_passed=1,
                tasks_failed=0,
            )

        async def fake_build(intent, project_dir, config, **kwargs):
            return BuildResult(
                passed=True,
                build_id="agentic-explicit",
                total_cost=0.0,
                tasks_passed=1,
                tasks_failed=0,
            )

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.pipeline.run_certify_fix_loop", side_effect=fake_loop) as split_loop, \
             patch("otto.pipeline.build_agentic_v3", new=AsyncMock(side_effect=AssertionError("agentic path should not run"))):
            result = CliRunner().invoke(
                main,
                ["build", "default split", "--allow-dirty"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        split_loop.assert_called_once()

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.pipeline.run_certify_fix_loop", new=AsyncMock(side_effect=AssertionError("split path should not run"))), \
             patch("otto.pipeline.build_agentic_v3", side_effect=fake_build) as agentic_build:
            result = CliRunner().invoke(
                main,
                ["build", "explicit agentic", "--agentic", "--allow-dirty"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        agentic_build.assert_called_once()

    def test_build_cli_certification_failure_prints_report_and_narrative(
        self, tmp_git_repo, monkeypatch
    ):
        from click.testing import CliRunner
        from otto import paths as _paths
        from otto.cli import main

        run_id = "run-fail-123"
        _paths.ensure_session_scaffold(tmp_git_repo, run_id)
        build_dir = _paths.build_dir(tmp_git_repo, run_id)
        certify_dir = _paths.certify_dir(tmp_git_repo, run_id)
        build_dir.mkdir(parents=True, exist_ok=True)
        certify_dir.mkdir(parents=True, exist_ok=True)
        (build_dir / "narrative.log").write_text("build narrative\n")
        (certify_dir / "proof-of-work.html").write_text("<html>fail</html>")

        async def fake_build(intent, project_dir, config, **kwargs):
            return BuildResult(
                passed=False,
                build_id=run_id,
                total_cost=0.5,
                tasks_passed=2,
                tasks_failed=3,
                journeys=[
                    {"name": "story 1", "passed": True},
                    {"name": "story 2", "passed": False},
                ],
            )

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.pipeline.build_agentic_v3", side_effect=fake_build):
            result = CliRunner().invoke(
                main,
                ["build", "failing app", "--agentic"],
                catch_exceptions=False,
            )

        assert result.exit_code == 1
        assert "Build did not pass certification (2/5 stories passed)." in result.output
        assert "run-fail-123/certify" in result.output
        assert "proof-of-work.html" in result.output
        assert "run-fail-123/build" in result.output
        assert "narrative.log" in result.output

    def test_build_cli_success_summary_shows_open_hint_and_spent_breakdown(
        self, tmp_bare_git_repo, monkeypatch
    ):
        from click.testing import CliRunner
        from otto import paths as _paths
        from otto.cli import main

        run_id = "run-pass-123"
        _paths.ensure_session_scaffold(tmp_bare_git_repo, run_id)
        (tmp_bare_git_repo / "index.html").write_text("<!doctype html><title>app</title>")
        _paths.certify_dir(tmp_bare_git_repo, run_id).mkdir(parents=True, exist_ok=True)
        (_paths.certify_dir(tmp_bare_git_repo, run_id) / "proof-of-work.html").write_text("<html>pass</html>")

        async def fake_build(intent, project_dir, config, **kwargs):
            return BuildResult(
                passed=True,
                build_id=run_id,
                rounds=2,
                total_cost=0.94,
                tasks_passed=2,
                tasks_failed=0,
                journeys=[
                    {"name": "Page serves over HTTP with 200 status and full content", "passed": True},
                    {"name": "Board has exactly 3 columns: To Do, In Progress, Done", "passed": True},
                ],
                breakdown={
                    "build": {"duration_s": 120.0, "cost_usd": 0.25, "estimated": True},
                    "certify": {"duration_s": 178.0, "cost_usd": 0.70, "estimated": True, "rounds": 2},
                },
            )

        monkeypatch.chdir(tmp_bare_git_repo)
        with patch("otto.pipeline.build_agentic_v3", side_effect=fake_build):
            result = CliRunner().invoke(
                main,
                ["build", "kanban board", "--agentic", "--allow-dirty"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert "Time budget" in result.output
        assert "60m" in result.output
        assert "Max build rounds" in result.output
        assert "Execution" in result.output
        assert "agentic (--agentic)" in result.output
        assert "Working on:" in result.output
        assert "Project:" in result.output
        assert "Session:" in result.output
        assert "otto_logs/sessions" in result.output
        assert "run-pass-123" in result.output
        assert "Live log:" in result.output
        assert "otto_logs/latest/build/narrative.log" in result.output
        assert "Verifying core requirements after each build." in result.output
        assert "Open it:  open index.html" in result.output
        assert "Built: kanban board" in result.output
        assert "Verification passed" in result.output
        assert "Full evidence" in result.output
        assert "otto_logs/latest/certify/proof-of-work.html" in result.output
        assert "Build Summary  ·  Run ID: run-pass-123" in result.output
        assert "Spent: 2:00 building, 2:58 verifying  (~$0.25 / ~$0.70 estimated, total $0.94)" in result.output
        assert "View report:  otto_logs/latest/certify/proof-of-work.html" in result.output
        assert "Tail live log:  otto_logs/latest/build/narrative.log" in result.output
        assert "See past runs:  otto history" in result.output

    def test_build_cli_threads_strict_and_verbose_flags(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.cli import main

        captured = {}

        async def fake_build(intent, project_dir, config, **kwargs):
            captured.update(kwargs)
            return BuildResult(
                passed=True,
                build_id="run-flags-123",
                total_cost=0.0,
                tasks_passed=0,
                tasks_failed=0,
            )

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.pipeline.build_agentic_v3", side_effect=fake_build):
            result = CliRunner().invoke(
                main,
                ["build", "flagged app", "--agentic", "--strict", "--verbose"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert captured["strict_mode"] is True
        assert captured["verbose"] is True

    def test_cli_max_turns_callbacks_reject_values_above_cap(self):
        from otto.cli import _max_turns_option as build_max_turns_option
        from otto.cli_improve import _max_turns_option as improve_max_turns_option

        with pytest.raises(click.BadParameter, match="<= 200"):
            build_max_turns_option(None, None, 201)
        with pytest.raises(click.BadParameter, match="<= 200"):
            improve_max_turns_option(None, None, 201)

    def test_build_resume_uses_checkpoint_max_rounds_when_flag_omitted(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.checkpoint import write_checkpoint
        from otto.cli import main

        write_checkpoint(
            tmp_git_repo,
            run_id="build-run-123",
            command="build",
            status="paused",
            phase="certify",
            split_mode=True,
            current_round=9,
            total_cost=1.5,
            max_rounds=20,
            intent="resume me",
            session_id="sdk-build-1",
        )

        captured = {}

        async def fake_loop(intent, project_dir, config, **kwargs):
            captured["max_certify_rounds"] = config["max_certify_rounds"]
            captured.update(kwargs)
            return BuildResult(
                passed=True,
                build_id="build-run-123",
                total_cost=1.5,
                tasks_passed=1,
                tasks_failed=0,
            )

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.pipeline.run_certify_fix_loop", side_effect=fake_loop):
            result = CliRunner().invoke(
                main,
                ["build", "--resume"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert captured["max_certify_rounds"] == 20
        assert captured["start_round"] == 10

    def test_improve_resume_threads_run_id_into_split_and_agentic(
        self, tmp_git_repo, monkeypatch
    ):
        """Improve resume should keep using the existing Otto session dir."""
        from click.testing import CliRunner
        from otto.checkpoint import write_checkpoint
        from otto.cli import main

        (tmp_git_repo / "intent.md").write_text("test intent")
        write_checkpoint(
            tmp_git_repo,
            run_id="improve-run-123",
            command="improve.bugs",
            certifier_mode="standard",
            focus="error handling",
            max_rounds=12,
            status="paused",
            phase="certify",
            current_round=1,
            total_cost=2.5,
            session_id="sdk-resume-1",
        )

        split_captured = {}
        agentic_captured = {}

        async def fake_loop(intent, project_dir, config, **kwargs):
            split_captured["config"] = config
            split_captured.update(kwargs)
            return BuildResult(
                passed=True,
                build_id="improve-run-123",
                total_cost=2.5,
                tasks_passed=1,
                tasks_failed=0,
            )

        async def fake_build(intent, project_dir, config, **kwargs):
            agentic_captured["intent"] = intent
            agentic_captured.update(kwargs)
            return BuildResult(
                passed=True,
                build_id="improve-run-123",
                total_cost=2.5,
                tasks_passed=1,
                tasks_failed=0,
            )

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.cli_improve._create_improve_branch", return_value="improve/2026-04-20"), \
             patch("otto.pipeline.run_certify_fix_loop", side_effect=fake_loop):
            result = CliRunner().invoke(
                main,
                ["improve", "bugs", "--split", "--resume"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert split_captured["session_id"] == "improve-run-123"
        assert split_captured["focus"] == "error handling"
        assert split_captured["certifier_mode"] == "standard"
        assert split_captured["config"]["max_certify_rounds"] == 12

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.cli_improve._create_improve_branch", return_value="improve/2026-04-20"), \
             patch("otto.pipeline.build_agentic_v3", side_effect=fake_build):
            result = CliRunner().invoke(
                main,
                ["improve", "bugs", "--agentic", "--resume"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert agentic_captured["run_id"] == "improve-run-123"
        assert agentic_captured["certifier_mode"] == "standard"
        assert "## Improvement Focus\nerror handling" in agentic_captured["intent"]

    def test_improve_bugs_uses_yaml_rounds_when_flag_omitted(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.cli import main

        (tmp_git_repo / "intent.md").write_text("test intent")
        (tmp_git_repo / "otto.yaml").write_text("max_certify_rounds: 6\n")
        captured = {}

        async def fake_build(intent, project_dir, config, **kwargs):
            captured["max_certify_rounds"] = config["max_certify_rounds"]
            return BuildResult(
                passed=True,
                build_id="improve-yaml-rounds",
                total_cost=0.0,
                tasks_passed=1,
                tasks_failed=0,
            )

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.cli_improve._create_improve_branch", return_value="improve/2026-04-21-abcdef"), \
             patch("otto.pipeline.build_agentic_v3", side_effect=fake_build):
            result = CliRunner().invoke(
                main,
                ["improve", "bugs", "--agentic", "--allow-dirty"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert captured["max_certify_rounds"] == 6

    def test_improve_bugs_allows_standard_override(self, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.cli import main

        (tmp_git_repo / "intent.md").write_text("test intent")
        captured = {}

        async def fake_build(intent, project_dir, config, **kwargs):
            captured["certifier_mode"] = kwargs["certifier_mode"]
            return BuildResult(
                passed=True,
                build_id="improve-standard",
                total_cost=0.0,
                tasks_passed=1,
                tasks_failed=0,
            )

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.cli_improve._create_improve_branch", return_value="improve/2026-04-21-abcdef"), \
             patch("otto.pipeline.build_agentic_v3", side_effect=fake_build):
            result = CliRunner().invoke(
                main,
                ["improve", "bugs", "--agentic", "--standard", "--allow-dirty"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert captured["certifier_mode"] == "standard"
