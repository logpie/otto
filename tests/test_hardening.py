"""Tests for hardening fixes: parsing, timeout, env, symlinks."""

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.agent import AgentOptions
from otto.config import create_config, load_config
from otto.pipeline import build_agentic_v3, BuildResult
from otto.testing import _subprocess_env


# -- Fixtures --

@pytest.fixture
def tmp_git_repo(tmp_path):
    """Create a temp git repo with otto.yaml."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=tmp_path, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=tmp_path, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=tmp_path, check=True,
    )
    create_config(tmp_path)
    subprocess.run(["git", "add", "otto.yaml"], cwd=tmp_path, capture_output=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "add config"],
        cwd=tmp_path, capture_output=True,
    )
    return tmp_path


def _make_mock_query(text, cost=0.50):
    result_msg = MagicMock()
    result_msg.session_id = "test-session"
    async def mock_query(prompt, options, **kwargs):
        return text, cost, result_msg
    return mock_query


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

    AGENT_OUTPUT_ONE_ROUND = """\
CERTIFY_ROUND: 1
STORIES_TESTED: 1
STORIES_PASSED: 1
STORY_RESULT: crud | PASS | Works
VERDICT: PASS
DIAGNOSIS: null
"""

    @pytest.mark.asyncio
    async def test_single_round(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(self.AGENT_OUTPUT_ONE_ROUND)):
            result = await build_agentic_v3("test", tmp_git_repo, {})

        assert result.rounds == 1


# -- Test: Timeout is actually enforced --

class TestTimeoutEnforcement:
    """Build should time out when certifier_timeout is exceeded."""

    @pytest.mark.asyncio
    async def test_build_times_out(self, tmp_git_repo):
        async def slow_query(prompt, options, **kwargs):
            await asyncio.sleep(10)
            return "never reached", 0.0, MagicMock()

        with patch("otto.agent.run_agent_query", side_effect=slow_query):
            result = await build_agentic_v3(
                "test", tmp_git_repo, {"certifier_timeout": 1}
            )

        assert result.passed is False
        # Check the raw log mentions timeout
        build_dir = tmp_git_repo / "otto_logs" / "builds" / result.build_id
        raw = (build_dir / "agent-raw.log").read_text()
        assert "Timed out" in raw or "TIMED OUT" in raw


# -- Test: CLAUDECODE env var --

class TestSubprocessEnv:
    """_subprocess_env should set CLAUDECODE to empty string."""

    def test_claudecode_is_empty_string(self):
        env = _subprocess_env()
        assert "CLAUDECODE" in env
        assert env["CLAUDECODE"] == ""

    def test_git_terminal_prompt_disabled(self):
        env = _subprocess_env()
        assert env["GIT_TERMINAL_PROMPT"] == "0"

    def test_ci_true(self):
        env = _subprocess_env()
        assert env["CI"] == "true"


# -- Test: AgentOptions is a proper dataclass --

class TestAgentOptions:
    """AgentOptions should be a well-formed dataclass."""

    def test_can_instantiate(self):
        opts = AgentOptions()
        assert opts.permission_mode is None
        assert opts.cwd is None

    def test_all_fields_have_defaults(self):
        opts = AgentOptions()
        # Should not raise — all fields have defaults
        assert hasattr(opts, "model")
        assert hasattr(opts, "system_prompt")
        assert hasattr(opts, "env")

    def test_fields_are_settable(self):
        opts = AgentOptions(permission_mode="bypassPermissions", cwd="/tmp")
        assert opts.permission_mode == "bypassPermissions"
        assert opts.cwd == "/tmp"


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


# -- Test: git_meta_dir handles edge cases --

class TestGitMetaDirEdgeCases:
    """git_meta_dir should handle empty/dot subprocess output gracefully."""

    def test_normal_git_dir(self, tmp_path):
        """Normal repo with .git directory."""
        from otto.config import git_meta_dir
        (tmp_path / ".git").mkdir()
        assert git_meta_dir(tmp_path) == tmp_path / ".git"

    def test_certify_passes_config(self, tmp_git_repo):
        """Config should be loaded and passed to run_agentic_certifier."""
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
            result = runner.invoke(main, ["certify", "test intent"], catch_exceptions=False)

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

        async def mock_certifier(intent, project_dir, config=None, **kwargs):
            return CertificationReport(
                outcome=CertificationOutcome.INFRA_ERROR,
                cost_usd=0.0,
                duration_s=1.0,
            )

        mock_build = AsyncMock()

        with patch("otto.cli_improve._create_improve_branch", return_value="improve/2026-04-13"), \
             patch("otto.certifier.run_agentic_certifier", side_effect=mock_certifier), \
             patch("otto.pipeline.build_agentic_v3", new=mock_build), \
             patch("pathlib.Path.cwd", return_value=tmp_git_repo):
            runner = CliRunner()
            result = runner.invoke(
                main, ["improve", "feature", "test intent", "--rounds", "1", "--split"], catch_exceptions=False
            )

        # Build agent must not be called when certifier fails with infra error
        assert mock_build.await_count == 0
        # Result should indicate failure, not success
        assert "PASSED" not in result.output

    def test_improve_reports_failure_when_fix_fails(self, tmp_git_repo):
        """When fix phase fails, result should be FAILED not PASSED."""
        from click.testing import CliRunner
        from otto.certifier.report import CertificationOutcome, CertificationReport
        from otto.cli import main
        (tmp_git_repo / "intent.md").write_text("test intent")

        async def mock_certifier(intent, project_dir, config=None, **kwargs):
            report = CertificationReport(
                outcome=CertificationOutcome.FAILED,
                cost_usd=0.0,
                duration_s=1.0,
            )
            report._story_results = [  # type: ignore[attr-defined]
                {
                    "story_id": "auth",
                    "passed": False,
                    "summary": "Login broken",
                    "evidence": "",
                }
            ]
            return report

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

        # The result should indicate failure
        assert "FAILED" in result.output or "PASSED" not in result.output


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


# -- Test: Broken node_modules symlink doesn't crash --

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
        story_results = getattr(report, "_story_results", [])
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
        story_results = getattr(report, "_story_results", [])
        auth_story = [s for s in story_results if s["story_id"] == "auth"][0]
        assert auth_story["passed"] is True


# -- Test: Makefile with binary content doesn't crash --

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

    def test_first_intent_creates_file(self, tmp_path):
        """First call should create intent.md."""
        from otto.pipeline import _append_intent

        _append_intent(tmp_path, "first intent", "build-1")
        assert (tmp_path / "intent.md").exists()
        assert "first intent" in (tmp_path / "intent.md").read_text()


# -- Test: certifier_timeout validation --

class TestTimeoutValidation:
    """certifier_timeout should handle invalid values gracefully."""

    @pytest.mark.asyncio
    async def test_non_numeric_timeout_uses_default(self, tmp_git_repo):
        """Non-numeric certifier_timeout should fall back to 900s, not crash."""
        async def fast_query(prompt, options, **kwargs):
            return "VERDICT: PASS\nSTORY_RESULT: x | PASS | ok\nSTORIES_TESTED: 1\nSTORIES_PASSED: 1\nDIAGNOSIS: null", 0.1, MagicMock()

        with patch("otto.agent.run_agent_query", side_effect=fast_query):
            # Should not raise ValueError
            result = await build_agentic_v3(
                "test", tmp_git_repo, {"certifier_timeout": "abc"}
            )
        # Build completes (not crashed)
        assert isinstance(result, BuildResult)

    @pytest.mark.asyncio
    async def test_zero_timeout_uses_default(self, tmp_git_repo):
        """Zero timeout should fall back to 900s, not cause instant timeout."""
        async def fast_query(prompt, options, **kwargs):
            return "VERDICT: PASS\nSTORY_RESULT: x | PASS | ok\nSTORIES_TESTED: 1\nSTORIES_PASSED: 1\nDIAGNOSIS: null", 0.1, MagicMock()

        with patch("otto.agent.run_agent_query", side_effect=fast_query):
            result = await build_agentic_v3(
                "test", tmp_git_repo, {"certifier_timeout": 0}
            )
        assert isinstance(result, BuildResult)

    @pytest.mark.asyncio
    async def test_negative_timeout_uses_default(self, tmp_git_repo):
        """Negative timeout should fall back to 900s."""
        async def fast_query(prompt, options, **kwargs):
            return "VERDICT: PASS\nSTORY_RESULT: x | PASS | ok\nSTORIES_TESTED: 1\nSTORIES_PASSED: 1\nDIAGNOSIS: null", 0.1, MagicMock()

        with patch("otto.agent.run_agent_query", side_effect=fast_query):
            result = await build_agentic_v3(
                "test", tmp_git_repo, {"certifier_timeout": -5}
            )
        assert isinstance(result, BuildResult)


# -- Test: Certifier timeout validation --

class TestCertifierTimeoutValidation:
    """Certifier should handle invalid timeout values."""

    @pytest.mark.asyncio
    async def test_certifier_non_numeric_timeout(self, tmp_git_repo):
        """Non-numeric timeout in certifier should not crash."""
        from otto.certifier import run_agentic_certifier

        async def mock_query(prompt, options, **kwargs):
            return "VERDICT: PASS\nSTORY_RESULT: x | PASS | ok\nSTORIES_TESTED: 1\nSTORIES_PASSED: 1\nDIAGNOSIS: null", 0.1, MagicMock()

        with patch("otto.agent.run_agent_query", side_effect=mock_query):
            report = await run_agentic_certifier(
                "test", tmp_git_repo, config={"certifier_timeout": "not-a-number"}
            )
        # Should not crash — uses default 900s
        assert report is not None


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
