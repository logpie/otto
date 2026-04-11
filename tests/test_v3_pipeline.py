"""Fast E2E tests for the v3 agentic pipeline.

Mocks run_agent_query (no LLM calls). Tests the full pipeline wiring:
prompt construction → result parsing → PoW writing → checkpoint → BuildResult.
"""

import asyncio
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from otto.config import create_config
from otto.pipeline import build_agentic_v3, BuildResult


# -- Fixtures --

@pytest.fixture
def tmp_git_repo(tmp_path):
    """Create a temp git repo with otto.yaml."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
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


# -- Canned agent outputs --

AGENT_OUTPUT_PASS = """\
I'll build this product.

Built the bookmark manager with SQLite storage. All 15 tests pass. Committed.

Now dispatching the certifier.

CERTIFY_ROUND: 1
STORIES_TESTED: 5
STORIES_PASSED: 5
STORY_RESULT: first-experience | PASS | New user can add and list bookmarks
STORY_RESULT: crud-lifecycle | PASS | Create, read, delete all work
STORY_RESULT: search | PASS | Search by tag and title works
STORY_RESULT: persistence | PASS | Data survives across sessions
STORY_RESULT: edge-cases | PASS | Empty input, special chars handled
VERDICT: PASS
DIAGNOSIS: null
"""

AGENT_OUTPUT_FAIL = """\
I'll build this product.

Built the app. Tests pass. Committed. Dispatching certifier.

CERTIFY_ROUND: 1
STORIES_TESTED: 4
STORIES_PASSED: 2
STORY_RESULT: crud | PASS | CRUD works
STORY_RESULT: auth | PASS | Auth works
STORY_RESULT: isolation | FAIL | Users can see each other's data
STORY_RESULT: edge | FAIL | Empty title accepted without validation
VERDICT: FAIL
DIAGNOSIS: Data isolation broken and input validation missing
"""

AGENT_OUTPUT_FAIL_THEN_PASS = """\
Built and tested.

CERTIFY_ROUND: 1
STORIES_TESTED: 3
STORIES_PASSED: 2
STORY_RESULT: crud | PASS | Works
STORY_RESULT: auth | FAIL | Missing auth check on /toggle
STORY_RESULT: edge | PASS | Edge cases handled
VERDICT: FAIL
DIAGNOSIS: Missing auth on toggle endpoint

Fixed the auth bug. Re-certifying.

CERTIFY_ROUND: 2
STORIES_TESTED: 3
STORIES_PASSED: 3
STORY_RESULT: crud | PASS | Works
STORY_RESULT: auth | PASS | Auth check added
STORY_RESULT: edge | PASS | Edge cases handled
VERDICT: PASS
DIAGNOSIS: null
"""

AGENT_OUTPUT_NO_MARKERS = """\
I built the product. Everything looks good. Committed.
"""


# -- Mock helper --

def _make_mock_query(text, cost=0.50):
    """Create a patched run_agent_query that returns canned output."""
    result_msg = MagicMock()
    result_msg.session_id = "test-session-123"

    async def mock_query(prompt, options, **kwargs):
        return text, cost, result_msg

    return mock_query


# -- Tests --

class TestV3PipelinePass:
    """Happy path: agent builds, certifies, all pass."""

    @pytest.mark.asyncio
    async def test_basic_pass(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
            result = await build_agentic_v3(
                "bookmark manager with tags",
                tmp_git_repo,
                {"test_command": "true"},
            )

        assert result.passed is True
        assert result.tasks_passed == 5
        assert result.tasks_failed == 0
        assert result.total_cost == 0.50
        assert result.build_id.startswith("build-")

    @pytest.mark.asyncio
    async def test_creates_agent_log(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
            result = await build_agentic_v3("test", tmp_git_repo, {})

        build_dir = tmp_git_repo / "otto_logs" / "builds" / result.build_id
        assert (build_dir / "agent.log").exists()
        assert (build_dir / "agent-raw.log").exists()

        # agent.log has structured markers
        log_content = (build_dir / "agent.log").read_text()
        assert "VERDICT: PASS" in log_content
        assert "STORY_RESULT:" in log_content

        # agent-raw.log has full output
        raw_content = (build_dir / "agent-raw.log").read_text()
        assert "bookmark manager" in raw_content.lower() or "certifier" in raw_content.lower()

    @pytest.mark.asyncio
    async def test_creates_checkpoint(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
            result = await build_agentic_v3("test", tmp_git_repo, {})

        build_dir = tmp_git_repo / "otto_logs" / "builds" / result.build_id
        cp = json.loads((build_dir / "checkpoint.json").read_text())
        assert cp["passed"] is True
        assert cp["stories_passed"] == 5
        assert cp["stories_tested"] == 5
        assert cp["mode"] == "agentic_v3"

    @pytest.mark.asyncio
    async def test_creates_pow_report(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
            await build_agentic_v3("test", tmp_git_repo, {})

        certifier_dir = tmp_git_repo / "otto_logs" / "certifier"
        assert (certifier_dir / "proof-of-work.json").exists()
        assert (certifier_dir / "proof-of-work.html").exists()

        pow_data = json.loads((certifier_dir / "proof-of-work.json").read_text())
        assert pow_data["outcome"] == "passed"
        assert len(pow_data["stories"]) == 5

    @pytest.mark.asyncio
    async def test_appends_intent(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
            await build_agentic_v3("bookmark manager", tmp_git_repo, {})

        intent_md = (tmp_git_repo / "intent.md").read_text()
        assert "bookmark manager" in intent_md

    @pytest.mark.asyncio
    async def test_appends_run_history(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
            await build_agentic_v3("test app", tmp_git_repo, {})

        history = tmp_git_repo / "otto_logs" / "run-history.jsonl"
        assert history.exists()
        entry = json.loads(history.read_text().strip().split("\n")[-1])
        assert entry["passed"] is True
        assert entry["stories_passed"] == 5
        assert "test app" in entry["intent"]


class TestV3PipelineFail:
    """Agent builds, certifier finds bugs, build fails."""

    @pytest.mark.asyncio
    async def test_basic_fail(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(AGENT_OUTPUT_FAIL)):
            result = await build_agentic_v3("test", tmp_git_repo, {})

        assert result.passed is False
        assert result.tasks_passed == 2
        assert result.tasks_failed == 2

    @pytest.mark.asyncio
    async def test_fail_checkpoint(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(AGENT_OUTPUT_FAIL)):
            result = await build_agentic_v3("test", tmp_git_repo, {})

        build_dir = tmp_git_repo / "otto_logs" / "builds" / result.build_id
        cp = json.loads((build_dir / "checkpoint.json").read_text())
        assert cp["passed"] is False

    @pytest.mark.asyncio
    async def test_fail_pow_shows_failures(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(AGENT_OUTPUT_FAIL)):
            await build_agentic_v3("test", tmp_git_repo, {})

        pow_data = json.loads(
            (tmp_git_repo / "otto_logs" / "certifier" / "proof-of-work.json").read_text()
        )
        assert pow_data["outcome"] == "failed"
        failed = [s for s in pow_data["stories"] if not s["passed"]]
        assert len(failed) == 2

    @pytest.mark.asyncio
    async def test_fail_history_entry(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(AGENT_OUTPUT_FAIL)):
            await build_agentic_v3("test", tmp_git_repo, {})

        entry = json.loads(
            (tmp_git_repo / "otto_logs" / "run-history.jsonl").read_text().strip()
        )
        assert entry["passed"] is False
        assert entry["stories_passed"] == 2


class TestV3FixLoop:
    """Agent certifies, fails, fixes, re-certifies — multiple rounds."""

    @pytest.mark.asyncio
    async def test_multi_round_uses_last_verdict(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(AGENT_OUTPUT_FAIL_THEN_PASS)):
            result = await build_agentic_v3("test", tmp_git_repo, {})

        # Final verdict is PASS (round 2)
        assert result.passed is True
        assert result.tasks_passed == 3

    @pytest.mark.asyncio
    async def test_multi_round_pow_shows_rounds(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(AGENT_OUTPUT_FAIL_THEN_PASS)):
            await build_agentic_v3("test", tmp_git_repo, {})

        pow_data = json.loads(
            (tmp_git_repo / "otto_logs" / "certifier" / "proof-of-work.json").read_text()
        )
        assert pow_data["outcome"] == "passed"
        # Should have round history
        assert pow_data.get("certify_rounds", 0) >= 2


class TestV3EdgeCases:
    """Edge cases: no markers, empty output, retry context."""

    @pytest.mark.asyncio
    async def test_no_verdict_markers_fails(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(AGENT_OUTPUT_NO_MARKERS)):
            result = await build_agentic_v3("test", tmp_git_repo, {})

        # No VERDICT marker → treated as fail
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_retry_injects_previous_failure(self, tmp_git_repo):
        """After a failed build, re-running should inject failure context."""
        # First build: FAIL
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(AGENT_OUTPUT_FAIL)):
            await build_agentic_v3("test", tmp_git_repo, {})

        # Second build: capture the prompt to verify failure context
        captured_prompts = []
        async def capture_query(prompt, options, **kwargs):
            captured_prompts.append(prompt)
            return AGENT_OUTPUT_PASS, 0.50, MagicMock(session_id="s2")

        with patch("otto.agent.run_agent_query", side_effect=capture_query):
            result = await build_agentic_v3("test", tmp_git_repo, {})

        assert result.passed is True
        # The prompt should contain previous failure context
        assert "Previous Build Failed" in captured_prompts[0]
        assert "isolation" in captured_prompts[0].lower() or "FAIL" in captured_prompts[0]

    @pytest.mark.asyncio
    async def test_no_retry_context_after_pass(self, tmp_git_repo):
        """After a passing build, re-running should NOT inject failure context."""
        # First build: PASS
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
            await build_agentic_v3("test", tmp_git_repo, {})

        # Second build: check no failure context
        captured_prompts = []
        async def capture_query(prompt, options, **kwargs):
            captured_prompts.append(prompt)
            return AGENT_OUTPUT_PASS, 0.50, MagicMock(session_id="s2")

        with patch("otto.agent.run_agent_query", side_effect=capture_query):
            await build_agentic_v3("test again", tmp_git_repo, {})

        assert "Previous Build Failed" not in captured_prompts[0]

    @pytest.mark.asyncio
    async def test_intent_appends_not_overwrites(self, tmp_git_repo):
        """Multiple builds should append to intent.md, not overwrite."""
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
            await build_agentic_v3("feature one", tmp_git_repo, {})

        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
            await build_agentic_v3("feature two", tmp_git_repo, {})

        intent_md = (tmp_git_repo / "intent.md").read_text()
        assert "feature one" in intent_md
        assert "feature two" in intent_md
