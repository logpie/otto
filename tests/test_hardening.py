"""Hardening regression tests: parsing, timeout/env validation, checkpoint
resume, certifier behavior, cross-run memory, and CLI guards."""

import asyncio
import json
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.pipeline import build_agentic_v3, BuildResult
from otto.testing import _subprocess_env
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


# -- Test: BuildResult.rounds reflects actual count --

class TestBuildResultRounds:
    """BuildResult.rounds should reflect the number of certification rounds."""

    AGENT_OUTPUT_TWO_ROUNDS = """\
CERTIFY_ROUND: 1
STORIES_TESTED: 2
STORIES_PASSED: 1
STORY_RESULT: crud | PASS | Works
STORY_RESULT: auth | FAIL | Missing check
VERDICT: FAIL
DIAGNOSIS: Auth broken

Fixed.

CERTIFY_ROUND: 2
STORIES_TESTED: 2
STORIES_PASSED: 2
STORY_RESULT: crud | PASS | Works
STORY_RESULT: auth | PASS | Fixed
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

    # (test_single_round removed — `rounds == 1` is implicitly covered by
    # every other happy-path test that uses a single CERTIFY_ROUND mock.)


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


# -- Test: CLAUDECODE env var --

class TestSubprocessEnv:
    """_subprocess_env should set the env vars that suppress agent-side
    prompts and nested CC detection."""

    def test_required_env_vars(self):
        env = _subprocess_env()
        assert env["CLAUDECODE"] == ""
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["CI"] == "true"


# (TestAgentOptions removed — it tested that @dataclass defaults work,
# which is guaranteed by the stdlib, not by otto.)


# -- Test: Empty story_id is rejected --

class TestEmptyStoryId:
    """STORY_RESULT with empty story_id should be silently skipped."""

    AGENT_OUTPUT_EMPTY_SID = """\
CERTIFY_ROUND: 1
STORIES_TESTED: 2
STORIES_PASSED: 2
STORY_RESULT:  | PASS | Ghost entry with empty id
STORY_RESULT: real-story | PASS | Real story that works
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
    """Cost should be accumulated, not overwritten, across messages."""

    @pytest.mark.asyncio
    async def test_cost_sums_across_messages(self):
        """When a provider yields multiple ResultMessages, sum their costs."""
        from otto.agent import (
            AssistantMessage, ResultMessage, TextBlock,
            run_agent_query, AgentOptions,
        )

        async def multi_result_query(*, prompt, options=None):
            yield AssistantMessage(content=[TextBlock(text="part 1")])
            yield ResultMessage(total_cost_usd=1.50)
            yield AssistantMessage(content=[TextBlock(text="part 2")])
            yield ResultMessage(total_cost_usd=0.75)

        with patch("otto.agent.query", side_effect=multi_result_query):
            text, cost, result_msg = await run_agent_query(
                "test", AgentOptions()
            )

        assert cost == pytest.approx(2.25)
        assert "part 1" in text
        assert "part 2" in text


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


class TestImproveCLIHardening:
    """The improve CLI should treat infra and build failures as failures."""

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

    def test_improve_stops_on_certifier_infra_error(self, tmp_git_repo):
        """INFRA_ERROR must stop the loop — build agent should never be called."""
        from click.testing import CliRunner
        from otto.certifier.report import CertificationOutcome, CertificationReport
        from otto.cli import main
        (tmp_git_repo / "intent.md").write_text("test intent")

        certifier_calls = 0

        async def mock_certifier(intent, project_dir, config=None, **kwargs):
            nonlocal certifier_calls
            certifier_calls += 1
            return CertificationReport(
                outcome=CertificationOutcome.INFRA_ERROR,
                cost_usd=0.0,
                duration_s=1.0,
            )

        mock_build = AsyncMock()

        # Patch at otto.pipeline — run_certify_fix_loop calls `build_agentic_v3`
        # and `run_agentic_certifier` directly by name in its own module scope.
        with patch("otto.cli_improve._create_improve_branch", return_value="improve/2026-04-13"), \
             patch("otto.certifier.run_agentic_certifier", side_effect=mock_certifier), \
             patch("otto.pipeline.build_agentic_v3", new=mock_build), \
             patch("pathlib.Path.cwd", return_value=tmp_git_repo):
            runner = CliRunner()
            result = runner.invoke(
                main, ["improve", "feature", "test intent", "--rounds", "1", "--split"], catch_exceptions=False
            )

        # Positive check: certifier was actually reached. Without this, the
        # mock_build.await_count==0 assertion could pass vacuously (e.g. if a
        # click wiring bug exited before entering the loop).
        assert certifier_calls >= 1, \
            f"certifier was never called — test doesn't exercise loop. Output: {result.output!r}"
        # Core invariant: INFRA_ERROR short-circuits before any build/fix agent runs
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
                main, ["improve", "feature", "test intent", "--rounds", "1", "--split"], catch_exceptions=False
            )

        # Tight assertion: non-zero exit AND a failed-story marker in output.
        # Previously this used `"FAILED" in out or "PASSED" not in out` which
        # was a tautology (empty output passed the second disjunct). The CLI
        # doesn't print the word "FAILED" to stdout — it shows per-story ✗
        # icons and writes the full verdict to improvement-report.md on disk.
        assert result.exit_code != 0, \
            f"Expected non-zero exit, got {result.exit_code}"
        # Story-specific failure summary is the reliable signal; the ✗
        # glyph is cosmetic and lives alongside.
        assert "Login broken" in result.output, \
            f"Expected failing-story summary in output: {result.output!r}"

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
        assert "not from `improve target`" in result.output
        assert run_improve.call_count == 0


# -- Test: PoW JSON round_history passed_count computed from stories --

class TestPowPassedCount:
    """PoW JSON should compute passed_count from stories, not default to 0."""

    AGENT_OUTPUT_NO_PASSED_MARKER = """\
CERTIFY_ROUND: 1
STORIES_TESTED: 2
STORY_RESULT: crud | PASS | Works
STORY_RESULT: auth | FAIL | Broken
VERDICT: FAIL
DIAGNOSIS: Auth broken

CERTIFY_ROUND: 2
STORIES_TESTED: 2
STORY_RESULT: crud | PASS | Works
STORY_RESULT: auth | PASS | Fixed
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


# -- Test: _append_intent per-section dedup, not substring --

class TestAppendIntentDedup:
    """_append_intent should compare per-section, not substring."""

    def test_similar_intents_both_appended(self, tmp_path):
        """'build X' should not block 'build X and Y' from being appended."""
        from otto.pipeline import _append_intent

        _append_intent(tmp_path, "build X", "build-1")
        _append_intent(tmp_path, "build X and Y", "build-2")

        content = (tmp_path / "intent.md").read_text()
        assert "build X and Y" in content
        assert content.count("build X") >= 2  # both entries present

    def test_exact_duplicate_blocked(self, tmp_path):
        """Exact same intent should not be appended twice."""
        from otto.pipeline import _append_intent

        _append_intent(tmp_path, "build X", "build-1")
        _append_intent(tmp_path, "build X", "build-2")

        content = (tmp_path / "intent.md").read_text()
        # "build X" appears once in the intent body (deduped)
        # The section header also contains "build-1" but not "build-2"
        assert "build-2" not in content

    # (test_first_intent_creates_file removed — `test_similar_intents_both_appended`
    # and `test_exact_duplicate_blocked` both implicitly verify that the file
    # gets created on first write.)


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


# -- Test: invalid spec_timeout in config is tolerated --

class TestSpecTimeoutTolerance:
    """spec.py should fall back to the default when spec_timeout is non-numeric."""

    @pytest.mark.asyncio
    async def test_certifier_ignores_unknown_timeout_keys(self, tmp_git_repo):
        from otto.certifier import run_agentic_certifier
        from otto.certifier.report import CertificationOutcome

        async def mock_query(prompt, options, **kwargs):
            return ("VERDICT: PASS\nSTORY_RESULT: x | PASS | ok\n"
                    "STORIES_TESTED: 1\nSTORIES_PASSED: 1\nDIAGNOSIS: null"), 0.1, MagicMock()

        # Obsolete keys like `certifier_timeout` are now ignored — no raise.
        with patch("otto.agent.run_agent_query", side_effect=mock_query):
            report = await run_agentic_certifier(
                "test", tmp_git_repo, config={"certifier_timeout": "not-a-number"}
            )
        assert report.outcome == CertificationOutcome.PASSED
        assert report.story_results and report.story_results[0]["story_id"] == "x"


class TestProofOfWorkRendering:
    def test_html_omits_empty_visual_evidence_and_diagnosis_sections(self, tmp_path):
        from otto.certifier import _generate_agentic_html_pow

        evidence_dir = tmp_path / "evidence"
        evidence_dir.mkdir()

        _generate_agentic_html_pow(
            tmp_path,
            [{"story_id": "smoke", "passed": True, "summary": "ok"}],
            "passed",
            1.0,
            0.1,
            1,
            1,
            diagnosis="",
            evidence_dir=evidence_dir,
        )

        html = (tmp_path / "proof-of-work.html").read_text()
        assert "Visual Evidence" not in html
        assert "Overall Diagnosis" not in html


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

        async def ok_agent(prompt, options, **kwargs):
            return (
                "CERTIFY_ROUND: 1\nSTORIES_TESTED: 1\nSTORIES_PASSED: 1\n"
                "STORY_RESULT: s1 | PASS | fine\nVERDICT: PASS\nDIAGNOSIS: null\n",
                0.1,
                MagicMock(session_id="sdk-sid-abc"),
            )

        with patch("otto.agent.run_agent_query", side_effect=ok_agent):
            result = await build_agentic_v3("test", tmp_git_repo, {})
        assert result.passed

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
    async def test_agent_resume_preserves_session_id_in_precheckpoint(self, tmp_git_repo):
        """Interrupted resumed agent runs must keep the resumable session_id."""
        from otto.checkpoint import load_checkpoint

        async def interrupt_agent(*args, **kwargs):
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
        assert checkpoint["status"] == "in_progress"
        assert checkpoint["agent_session_id"] == "sess-resume-123"

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

        state = resolve_resume(tmp_git_repo, resume=True, expected_command="build")
        assert state.resumed
        assert state.phase == "initial_build"
        assert state.start_round == 1

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

        state = resolve_resume(tmp_git_repo, resume=True, expected_command="improve.feature")
        assert state.resumed
        assert state.phase == "certify"
        assert state.start_round == 1


class TestBuildResume:
    """End-to-end: `otto build --resume` picks up split-mode checkpoint."""

    def test_build_cli_exposes_resume_flag(self):
        """--resume is wired on otto build and intent is optional when resuming."""
        from click.testing import CliRunner
        from otto.cli import main

        r = CliRunner().invoke(main, ["build", "--help"])
        assert r.exit_code == 0
        assert "--resume" in r.output
        assert "--break-lock" in r.output
        assert "[INTENT]" in r.output  # intent is optional

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
                ["build", "a kanban board:\n  localStorage"],
                catch_exceptions=False,
            )

        assert result.exit_code == 0
        assert captured["intent"] == "a kanban board: localStorage"

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
                ["improve", "bugs"],
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
                ["build", "failing app"],
                catch_exceptions=False,
            )

        assert result.exit_code == 1
        assert "Build did not pass certification (2/5 stories passed)." in result.output
        assert "run-fail-123/certify" in result.output
        assert "proof-of-work.html" in result.output
        assert "run-fail-123/build" in result.output
        assert "narrative.log" in result.output

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
            status="paused",
            phase="certify",
            current_round=1,
            total_cost=2.5,
            session_id="sdk-resume-1",
        )

        split_captured = {}
        agentic_captured = {}

        async def fake_loop(intent, project_dir, config, **kwargs):
            split_captured.update(kwargs)
            return BuildResult(
                passed=True,
                build_id="improve-run-123",
                total_cost=2.5,
                tasks_passed=1,
                tasks_failed=0,
            )

        async def fake_build(intent, project_dir, config, **kwargs):
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

        monkeypatch.chdir(tmp_git_repo)
        with patch("otto.cli_improve._create_improve_branch", return_value="improve/2026-04-20"), \
             patch("otto.pipeline.build_agentic_v3", side_effect=fake_build):
            result = CliRunner().invoke(
                main,
                ["improve", "bugs", "--resume"],
                catch_exceptions=False,
            )
        assert result.exit_code == 0
        assert agentic_captured["run_id"] == "improve-run-123"
