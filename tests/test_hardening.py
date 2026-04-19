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
    """Build should time out when certifier_timeout is exceeded."""

    @pytest.mark.asyncio
    async def test_build_times_out(self, tmp_git_repo):
        import time as _time
        async def slow_query(prompt, options, **kwargs):
            await asyncio.sleep(10)
            return "never reached", 0.0, MagicMock()

        start = _time.monotonic()
        with patch("otto.agent.run_agent_query", side_effect=slow_query):
            result = await build_agentic_v3(
                "test", tmp_git_repo, {"certifier_timeout": 1}
            )
        elapsed = _time.monotonic() - start

        assert result.passed is False
        # Timeout was 1s; with orphan cleanup and report writes it may take
        # several seconds. 9s still catches a no-timeout regression (which
        # would sleep the full 10s plus overhead).
        assert elapsed < 9, f"Timeout not enforced; elapsed={elapsed:.1f}s"
        # Check the raw log mentions timeout — strict `Timed out` match,
        # not case-insensitive (AgentCallError writes exactly "Timed out").
        build_dir = tmp_git_repo / "otto_logs" / "builds" / result.build_id
        raw = (build_dir / "agent-raw.log").read_text()
        assert "Timed out" in raw, f"Timeout not reported in raw log: {raw[:200]}"


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
        # Write a custom timeout to otto.yaml
        config_path = tmp_git_repo / "otto.yaml"
        config_path.write_text("certifier_timeout: 1200\n")

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
        assert captured_config[0].get("certifier_timeout") == 1200


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
            await build_agentic_v3("test", tmp_git_repo, {})

        pow_path = tmp_git_repo / "otto_logs" / "certifier" / "proof-of-work.json"
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
        assert "FAIL (certifier omitted METRIC_MET)" in (
            tmp_git_repo / "build-journal.md"
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

        with patch("otto.agent.run_agent_query", side_effect=slow_query):
            # This should not raise UnboundLocalError
            result = await build_agentic_v3(
                "test", tmp_git_repo, {"certifier_timeout": 1}
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


# -- Test: certifier_timeout validation --

class TestTimeoutValidation:
    """certifier_timeout validation should fall back to the 900s default
    for invalid inputs (non-numeric, zero, negative)."""

    @pytest.mark.parametrize("bad_value", ["abc", 0, -5, None, "", "0"])
    def test_invalid_timeout_falls_back_to_default(self, bad_value):
        """get_timeout is the sole source of truth — unit-test it directly
        rather than spinning up the whole pipeline to assert isinstance."""
        from otto.config import get_timeout
        assert get_timeout({"certifier_timeout": bad_value}) == 900

    def test_valid_timeout_honored(self):
        from otto.config import get_timeout
        assert get_timeout({"certifier_timeout": 120}) == 120
        assert get_timeout({"certifier_timeout": "120"}) == 120


# -- Test: Certifier timeout validation --

class TestCertifierTimeoutValidation:
    """Certifier runs successfully even with invalid timeout config.
    Narrow integration test — unit behavior lives in TestTimeoutValidation."""

    @pytest.mark.asyncio
    async def test_certifier_tolerates_invalid_timeout(self, tmp_git_repo):
        from otto.certifier import run_agentic_certifier
        from otto.certifier.report import CertificationOutcome

        async def mock_query(prompt, options, **kwargs):
            return ("VERDICT: PASS\nSTORY_RESULT: x | PASS | ok\n"
                    "STORIES_TESTED: 1\nSTORIES_PASSED: 1\nDIAGNOSIS: null"), 0.1, MagicMock()

        with patch("otto.agent.run_agent_query", side_effect=mock_query):
            report = await run_agentic_certifier(
                "test", tmp_git_repo, config={"certifier_timeout": "not-a-number"}
            )
        # Stronger than "report is not None": verify the run completed and
        # parsed the mock output (the weak isinstance check used to pass even
        # on a crashed/empty report).
        assert report.outcome == CertificationOutcome.PASSED
        assert report.story_results and report.story_results[0]["story_id"] == "x"


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
            tmp_path, command="certify", certifier_mode="fast",
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
                tmp_path, command="build", certifier_mode="fast",
                stories=[{"story_id": f"s{i}", "passed": True, "summary": f"Story {i}"}],
                cost=0.1,
            )

        entries = load_history(tmp_path)
        assert len(entries) == MAX_ENTRIES


class TestResolveResume:
    """resolve_resume handles the four checkpoint states consistently."""

    def test_no_checkpoint_no_resume(self, tmp_path):
        """Clean slate: no checkpoint, user didn't ask to resume."""
        from otto.checkpoint import resolve_resume
        state = resolve_resume(tmp_path, resume=False, expected_command="build")
        assert not state.resumed
        assert state.start_round == 1
        assert state.total_cost == 0.0
        assert state.session_id == ""

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
        assert state.session_id == "sess-abc"
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


class TestCheckpointRegression:
    """Regression tests for checkpoint/resume edge cases."""

    def test_load_checkpoint_ignores_truncated_json(self, tmp_path):
        """Partial checkpoint writes should load as None, not crash."""
        from otto.checkpoint import CHECKPOINT_FILE, load_checkpoint

        checkpoint_path = tmp_path / CHECKPOINT_FILE
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
        assert checkpoint["session_id"] == "sess-resume-123"

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
        assert "[INTENT]" in r.output  # intent is optional

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
