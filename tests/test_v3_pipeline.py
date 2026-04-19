"""Fast E2E tests for the v3 agentic pipeline.

Mocks run_agent_query (no LLM calls). Tests the full pipeline wiring:
prompt construction → result parsing → PoW writing → checkpoint → BuildResult.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from otto.pipeline import build_agentic_v3
from tests.conftest import make_mock_query as _make_mock_query

# `tmp_git_repo` fixture comes from tests/conftest.py.


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

AGENT_OUTPUT_TARGET_METRIC_NOT_MET = """\
Built and tested.

CERTIFY_ROUND: 1
STORIES_TESTED: 2
STORIES_PASSED: 2
STORY_RESULT: p50-latency | PASS | Latency probe completed successfully
STORY_RESULT: regression-suite | PASS | Existing behavior still passes
METRIC_VALUE: 137ms
METRIC_MET: NO
VERDICT: PASS
DIAGNOSIS: null
"""

AGENT_OUTPUT_TARGET_METRIC_MET = """\
Built and tested.

CERTIFY_ROUND: 1
STORIES_TESTED: 2
STORIES_PASSED: 2
STORY_RESULT: p50-latency | PASS | Latency probe completed successfully
STORY_RESULT: regression-suite | PASS | Existing behavior still passes
METRIC_VALUE: 82ms
METRIC_MET: YES
VERDICT: PASS
DIAGNOSIS: null
"""

AGENT_OUTPUT_NON_TARGET_METRIC_ONLY = """\
Built and tested.

CERTIFY_ROUND: 1
STORIES_TESTED: 2
STORIES_PASSED: 2
STORY_RESULT: crud | PASS | CRUD works
STORY_RESULT: edge-cases | PASS | Edge cases handled
METRIC_VALUE: 137ms
VERDICT: PASS
DIAGNOSIS: null
"""

AGENT_OUTPUT_TARGET_MISSING_METRIC_MET = """\
Built and tested.

CERTIFY_ROUND: 1
STORIES_TESTED: 2
STORIES_PASSED: 2
STORY_RESULT: p50-latency | PASS | Latency probe completed successfully
STORY_RESULT: regression-suite | PASS | Existing behavior still passes
METRIC_VALUE: 82ms
VERDICT: PASS
DIAGNOSIS: null
"""


# -- Tests --

@pytest.mark.asyncio
async def test_agent_mode_target_fails_when_metric_not_met(tmp_git_repo):
    with patch("otto.agent.run_agent_query",
               side_effect=_make_mock_query(AGENT_OUTPUT_TARGET_METRIC_NOT_MET)):
        result = await build_agentic_v3(
            'latency < 100ms', tmp_git_repo, {"_target": "latency < 100ms"},
            certifier_mode="target",
        )

    assert result.passed is False


@pytest.mark.asyncio
async def test_non_target_mode_with_metric_value_but_no_met_still_passes(tmp_git_repo):
    with patch("otto.agent.run_agent_query",
               side_effect=_make_mock_query(AGENT_OUTPUT_NON_TARGET_METRIC_ONLY)):
        result = await build_agentic_v3("test", tmp_git_repo, {})

    assert result.passed is True


@pytest.mark.asyncio
async def test_target_mode_missing_metric_met_fails(tmp_git_repo):
    with patch("otto.agent.run_agent_query",
               side_effect=_make_mock_query(AGENT_OUTPUT_TARGET_MISSING_METRIC_MET)):
        result = await build_agentic_v3(
            "latency < 100ms", tmp_git_repo, {"_target": "latency < 100ms"},
            certifier_mode="target",
        )

    assert result.passed is False


@pytest.mark.asyncio
async def test_agent_mode_target_passes_when_metric_met(tmp_git_repo):
    with patch("otto.agent.run_agent_query",
               side_effect=_make_mock_query(AGENT_OUTPUT_TARGET_METRIC_MET)):
        result = await build_agentic_v3(
            'latency < 100ms', tmp_git_repo, {"_target": "latency < 100ms"},
            certifier_mode="target",
        )

    assert result.passed is True

class TestV3PipelinePass:
    """Happy path: agent builds, certifies, all pass.

    All artifacts are asserted in one run — the previous six-tests-six-runs
    setup ran the full pipeline six times to check each file individually,
    which is pure churn since they're all side effects of a single call.
    """

    @pytest.mark.asyncio
    async def test_pipeline_writes_all_artifacts_on_pass(self, tmp_git_repo):
        intent = "bookmark manager with tags"
        with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
            result = await build_agentic_v3(
                intent, tmp_git_repo, {"test_command": "true"},
            )

        # --- BuildResult ---
        assert result.passed is True
        assert result.tasks_passed == 5
        assert result.tasks_failed == 0
        assert result.total_cost == 0.50
        assert result.build_id.startswith("build-")

        # --- Per-build logs ---
        build_dir = tmp_git_repo / "otto_logs" / "builds" / result.build_id
        log_content = (build_dir / "agent.log").read_text()
        assert "VERDICT: PASS" in log_content
        assert "STORY_RESULT:" in log_content
        # agent-raw.log should capture the full agent output verbatim —
        # check for a distinctive substring from AGENT_OUTPUT_PASS that
        # only appears in the raw mock text, not in the structured summary.
        raw_content = (build_dir / "agent-raw.log").read_text()
        assert "VERDICT: PASS" in raw_content
        assert "dispatching the certifier" in raw_content

        # --- Per-build checkpoint ---
        cp = json.loads((build_dir / "checkpoint.json").read_text())
        assert cp["passed"] is True
        assert cp["stories_passed"] == 5
        assert cp["stories_tested"] == 5
        assert cp["mode"] == "agentic_v3"

        # --- PoW (proof-of-work) ---
        certifier_dir = tmp_git_repo / "otto_logs" / "certifier"
        pow_data = json.loads((certifier_dir / "proof-of-work.json").read_text())
        assert pow_data["outcome"] == "passed"
        assert len(pow_data["stories"]) == 5
        assert (certifier_dir / "proof-of-work.html").exists()

        # --- Cumulative logs ---
        intent_md = (tmp_git_repo / "intent.md").read_text()
        assert intent in intent_md
        entry = json.loads(
            (tmp_git_repo / "otto_logs" / "run-history.jsonl").read_text().strip().split("\n")[-1]
        )
        assert entry["passed"] is True
        assert entry["stories_passed"] == 5
        assert intent in entry["intent"]


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
    async def test_cross_run_memory_injected_when_enabled(self, tmp_git_repo):
        """With memory enabled, re-running should inject cross-run memory."""
        # First build: FAIL — records memory
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(AGENT_OUTPUT_FAIL)):
            await build_agentic_v3("test", tmp_git_repo, {})

        # Second build with memory enabled
        captured_prompts = []
        async def capture_query(prompt, options, **kwargs):
            captured_prompts.append(prompt)
            return AGENT_OUTPUT_PASS, 0.50, MagicMock(session_id="s2")

        with patch("otto.agent.run_agent_query", side_effect=capture_query):
            result = await build_agentic_v3("test", tmp_git_repo, {"memory": True})

        assert result.passed is True
        assert "Previous Certification History" in captured_prompts[0]

    @pytest.mark.asyncio
    async def test_no_memory_on_first_run(self, tmp_git_repo):
        """First run should NOT have cross-run memory section."""
        captured_prompts = []
        async def capture_query(prompt, options, **kwargs):
            captured_prompts.append(prompt)
            return AGENT_OUTPUT_PASS, 0.50, MagicMock(session_id="s2")

        with patch("otto.agent.run_agent_query", side_effect=capture_query):
            await build_agentic_v3("test", tmp_git_repo, {})

        assert "Previous Certification History" not in captured_prompts[0]

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


class TestV3SkipQA:
    """--no-qa (skip_product_qa) should pass when agent completes successfully."""

    @pytest.mark.asyncio
    async def test_skip_qa_passes_without_markers(self, tmp_git_repo):
        """With skip_product_qa, build passes even without certification markers."""
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(AGENT_OUTPUT_NO_MARKERS)):
            result = await build_agentic_v3(
                "test lib", tmp_git_repo, {"skip_product_qa": True},
            )

        assert result.passed is True

    @pytest.mark.asyncio
    async def test_skip_qa_passes_with_real_output(self, tmp_git_repo):
        """With skip_product_qa, agent output with markers still passes."""
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
            result = await build_agentic_v3(
                "test app", tmp_git_repo, {"skip_product_qa": True},
            )

        assert result.passed is True

    @pytest.mark.parametrize("agent_output", [
        "BUILD TIMED OUT after 30s",
        "BUILD ERROR: something broke",
    ])
    @pytest.mark.asyncio
    async def test_skip_qa_fails_on_agent_failure(self, tmp_git_repo, agent_output):
        """With skip_product_qa, build still fails if the agent call fails
        (timeout or error). Without QA markers, success depends on clean exit."""
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(agent_output)):
            result = await build_agentic_v3(
                "test", tmp_git_repo, {"skip_product_qa": True},
            )

        assert result.passed is False


class TestEmptyIntent:
    """Empty / whitespace-only intent should be rejected at CLI level."""

    @pytest.mark.parametrize("bad_intent", ["", "   ", "\t\n"])
    def test_empty_intent_rejected(self, bad_intent):
        from click.testing import CliRunner
        from otto.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["build", bad_intent])
        assert result.exit_code == 2
