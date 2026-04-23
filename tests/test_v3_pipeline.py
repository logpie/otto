"""Fast E2E tests for the v3 agentic pipeline.

Mocks run_agent_query (no LLM calls). Tests the full pipeline wiring:
prompt construction → result parsing → PoW writing → checkpoint → BuildResult.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from otto.agent import AssistantMessage, TextBlock, ToolResultBlock, ToolUseBlock
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
COVERAGE_OBSERVED:
- Exercised the mocked first-experience, CRUD, search, persistence, and edge-case stories
COVERAGE_GAPS:
- Did not model additional product-specific coverage in this mocked transcript
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
COVERAGE_OBSERVED:
- Exercised the mocked CRUD, auth, isolation, and edge stories
COVERAGE_GAPS:
- Did not model additional product-specific coverage in this mocked transcript
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
COVERAGE_OBSERVED:
- Exercised the mocked CRUD, auth, and edge stories in round 1
COVERAGE_GAPS:
- Did not model additional product-specific coverage in this mocked transcript
VERDICT: FAIL
DIAGNOSIS: Missing auth on toggle endpoint

Fixed the auth bug. Re-certifying.

CERTIFY_ROUND: 2
STORIES_TESTED: 3
STORIES_PASSED: 3
STORY_RESULT: crud | PASS | Works
STORY_RESULT: auth | PASS | Auth check added
STORY_RESULT: edge | PASS | Edge cases handled
COVERAGE_OBSERVED:
- Re-exercised the mocked CRUD, auth, and edge stories in round 2
COVERAGE_GAPS:
- Did not model additional product-specific coverage in this mocked transcript
VERDICT: PASS
DIAGNOSIS: null
"""

AGENT_OUTPUT_TWO_PASS = """\
Built and tested.

CERTIFY_ROUND: 1
STORIES_TESTED: 2
STORIES_PASSED: 2
STORY_RESULT: crud | PASS | Works
STORY_RESULT: auth | PASS | Works
COVERAGE_OBSERVED:
- Exercised the mocked CRUD and auth stories in round 1
COVERAGE_GAPS:
- Did not model additional product-specific coverage in this mocked transcript
VERDICT: PASS
DIAGNOSIS: null

Re-running verification.

CERTIFY_ROUND: 2
STORIES_TESTED: 2
STORIES_PASSED: 2
STORY_RESULT: crud | PASS | Still works
STORY_RESULT: auth | PASS | Still works
COVERAGE_OBSERVED:
- Re-exercised the mocked CRUD and auth stories in round 2
COVERAGE_GAPS:
- Did not model additional product-specific coverage in this mocked transcript
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
COVERAGE_OBSERVED:
- Exercised the mocked latency probe and regression stories
COVERAGE_GAPS:
- Did not model additional product-specific coverage in this mocked transcript
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
COVERAGE_OBSERVED:
- Exercised the mocked latency probe and regression stories
COVERAGE_GAPS:
- Did not model additional product-specific coverage in this mocked transcript
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
COVERAGE_OBSERVED:
- Exercised the mocked CRUD and edge-case stories
COVERAGE_GAPS:
- Did not model additional product-specific coverage in this mocked transcript
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
COVERAGE_OBSERVED:
- Exercised the mocked latency probe and regression stories
COVERAGE_GAPS:
- Did not model additional product-specific coverage in this mocked transcript
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
        # build_id in the new layout is the unified session_id
        # (<date>-<HHMMSS>-<6hex>). Just check it's non-empty.
        assert result.build_id

        from otto import paths as _paths

        # --- Per-build session logs (Phase 6 layout) ---
        build_dir = _paths.build_dir(tmp_git_repo, result.build_id)
        # narrative.log — human-readable streamed event log. VERDICT and
        # STORY_RESULT markers are elevated as marker lines.
        narr = (build_dir / "narrative.log").read_text()
        assert "VERDICT: PASS" in narr
        assert "STORY_RESULT:" in narr
        # messages.jsonl — lossless normalized SDK event stream. Contains
        # full text blocks including agent prose like "dispatching the
        # certifier" that the narrative might compress.
        jsonl = (build_dir / "messages.jsonl").read_text()
        assert "VERDICT: PASS" in jsonl
        assert "dispatching the certifier" in jsonl

        # --- Per-build checkpoint (summary of the run) ---
        cp = json.loads((build_dir / "checkpoint.json").read_text())
        assert cp["run_id"] == result.build_id
        assert cp["build_id"] == result.build_id
        assert cp["passed"] is True
        assert cp["stories_passed"] == 5
        assert cp["stories_tested"] == 5
        assert cp["mode"] == "agentic_v3"

        summary = json.loads(_paths.session_summary(tmp_git_repo, result.build_id).read_text())
        assert summary["run_id"] == result.build_id
        assert summary["verdict"] == "passed"
        assert summary["status"] == "completed"
        assert summary["stories_passed"] == 5
        assert summary["stories_tested"] == 5
        assert summary["runtime_path"].endswith("runtime.json")
        assert summary["breakdown"]["build"]["duration_s"] >= 0
        assert summary["breakdown"].get("certify", {}).get("rounds", 0) == 0
        runtime = json.loads((_paths.session_dir(tmp_git_repo, result.build_id) / "runtime.json").read_text())
        assert runtime["otto_version"]
        assert runtime["python_version"]
        assert runtime["platform"]
        assert runtime["git_branch"]

        provenance = json.loads((_paths.session_dir(tmp_git_repo, result.build_id) / "input-provenance.json").read_text())
        assert provenance["intent"]["source"] == "cli-argument"
        assert provenance["intent"]["resolved_text"] == intent
        assert provenance["intent"]["sha256"]
        assert provenance["spec"]["source"] == "none"
        assert len(provenance["prompts"]) >= 2
        for prompt_entry in provenance["prompts"]:
            assert Path(prompt_entry["rendered_path"]).exists()
            assert prompt_entry["rendered_sha256"]

        # --- PoW (proof-of-work) ---
        certifier_dir = _paths.certify_dir(tmp_git_repo, result.build_id)
        pow_data = json.loads((certifier_dir / "proof-of-work.json").read_text())
        assert pow_data["schema_version"] == 1
        assert pow_data["outcome"] == "passed"
        assert pow_data["pipeline_mode"] == "agentic_v3"
        assert pow_data["mode"] == "agentic_v3"
        assert pow_data["certifier_mode"] == "thorough"
        assert pow_data["passed_count"] == 5
        assert pow_data["failed_count"] == 0
        assert pow_data["warn_count"] == 0
        assert len(pow_data["stories"]) == 5
        assert all("warn" not in story for story in pow_data["stories"])
        assert all("claim" in story for story in pow_data["stories"])
        assert all("observed_result" in story for story in pow_data["stories"])
        assert all("has_evidence" in story for story in pow_data["stories"])
        assert all(story["has_evidence"] is False for story in pow_data["stories"])
        assert len(pow_data["round_history"]) == 1
        assert (certifier_dir / "proof-of-work.html").exists()

        # --- Session intent snapshot ---
        assert _paths.session_intent(tmp_git_repo, result.build_id).read_text().strip() == intent
        entry = json.loads(
            _paths.history_jsonl(tmp_git_repo).read_text().strip().split("\n")[-1]
        )
        assert entry["passed"] is True
        assert entry["stories_passed"] == 5
        assert intent in entry["intent"]


@pytest.mark.asyncio
async def test_build_result_total_cost_includes_spec_cost(tmp_git_repo):
    with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
        result = await build_agentic_v3("test", tmp_git_repo, {}, spec_cost=0.25)

    assert result.total_cost == 0.75


@pytest.mark.asyncio
async def test_completed_checkpoint_total_cost_and_run_id_match_build_result(tmp_git_repo):
    with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
        result = await build_agentic_v3(
            "test",
            tmp_git_repo,
            {},
            spec_cost=0.25,
            run_id="run-123",
        )

    # run_id="run-123" is the session_id in the new layout.
    from otto import paths as _paths
    checkpoint_path = _paths.session_dir(tmp_git_repo, "run-123") / "checkpoint.json"
    checkpoint = json.loads(checkpoint_path.read_text())
    assert checkpoint["run_id"] == "run-123"
    assert checkpoint["agent_session_id"] == "test-session"
    assert checkpoint["total_cost"] == pytest.approx(0.75)
    assert result.total_cost == pytest.approx(0.75)
    assert _paths.resolve_pointer(tmp_git_repo, _paths.PAUSED_POINTER) is None


@pytest.mark.asyncio
async def test_resume_totals_include_prior_cost_and_duration(tmp_git_repo):
    from otto import paths as _paths

    with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
        result = await build_agentic_v3(
            "test",
            tmp_git_repo,
            {},
            run_id="resume-123",
            prior_total_cost=1.25,
            prior_total_duration=9.5,
        )

    summary = json.loads(_paths.session_summary(tmp_git_repo, result.build_id).read_text())
    checkpoint = json.loads(_paths.session_checkpoint(tmp_git_repo, result.build_id).read_text())
    assert result.total_cost == pytest.approx(1.75)
    assert result.total_duration >= 9.5
    assert summary["cost_usd"] == pytest.approx(1.75)
    assert summary["duration_s"] >= 9.5
    assert checkpoint["total_cost_so_far"] == pytest.approx(1.75)
    assert checkpoint["total_duration_so_far"] >= 9.5


@pytest.mark.asyncio
async def test_summary_duration_includes_spec_duration(tmp_git_repo):
    from otto import paths as _paths

    with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
        result = await build_agentic_v3(
            "test",
            tmp_git_repo,
            {},
            spec_cost=0.25,
            spec_duration=12.0,
        )

    summary = json.loads(_paths.session_summary(tmp_git_repo, result.build_id).read_text())
    assert result.total_duration >= 12.0
    assert summary["duration_s"] >= 12.0
    assert summary["breakdown"]["spec"]["duration_s"] == 12.0


@pytest.mark.asyncio
async def test_agent_mode_summary_includes_estimated_phase_costs_when_usage_is_logged(tmp_git_repo):
    assistant_messages = [
        AssistantMessage(
            content=[TextBlock(text="I'll build this product. Now dispatching the certifier.")],
            usage={"output_tokens": 40},
        ),
        AssistantMessage(
            content=[ToolUseBlock(
                name="Agent",
                id="cert-1",
                input={"prompt": "Quick smoke test\n## Verdict Format\nReturn PASS/FAIL."},
            )],
            usage={"output_tokens": 10},
        ),
        AssistantMessage(
            content=[TextBlock(text="Certifier is running.")],
            usage={"output_tokens": 25},
        ),
        AssistantMessage(
            content=[ToolResultBlock(
                tool_use_id="cert-1",
                content=(
                    "STORIES_TESTED: 5\n"
                    "STORIES_PASSED: 5\n"
                    "STORY_RESULT: smoke | PASS | core flow works\n"
                    "VERDICT: PASS\n"
                    "DIAGNOSIS: null"
                ),
            )],
            usage={"output_tokens": 15},
        ),
        AssistantMessage(
            content=[TextBlock(text=AGENT_OUTPUT_PASS)],
            usage={"output_tokens": 30},
        ),
    ]
    with patch(
        "otto.agent.run_agent_query",
        side_effect=_make_mock_query(AGENT_OUTPUT_PASS, assistant_messages=assistant_messages),
    ):
        result = await build_agentic_v3("test", tmp_git_repo, {})

    from otto import paths as _paths

    summary = json.loads(_paths.session_summary(tmp_git_repo, result.build_id).read_text())
    build_entry = summary["breakdown"]["build"]
    certify_entry = summary["breakdown"]["certify"]
    assert build_entry["cost_usd"] >= 0
    assert build_entry["estimated"] is True
    assert certify_entry["cost_usd"] >= 0
    assert certify_entry["estimated"] is True
    assert certify_entry["rounds"] == 1


@pytest.mark.asyncio
async def test_paused_build_does_not_write_summary_json(tmp_git_repo):
    async def crashing_query(*_args, **_kwargs):
        raise RuntimeError("agent crashed mid-build")

    with patch("otto.agent.run_agent_query", side_effect=crashing_query):
        result = await build_agentic_v3("test", tmp_git_repo, {})

    from otto import paths as _paths

    paused_sess = _paths.resolve_pointer(tmp_git_repo, _paths.PAUSED_POINTER)
    assert paused_sess is not None
    assert paused_sess.name == result.build_id
    assert not _paths.session_summary(tmp_git_repo, result.build_id).exists()

    checkpoint = json.loads(
        _paths.session_checkpoint(tmp_git_repo, result.build_id).read_text()
    )
    assert checkpoint["status"] == "paused"


@pytest.mark.asyncio
async def test_crash_artifact_and_checkpoint_capture_last_activity(tmp_git_repo):
    async def crashing_query(_prompt, _options, **kwargs):
        from otto.agent import AssistantMessage, TextBlock, ToolUseBlock

        on_message = kwargs.get("on_message")
        if on_message is not None:
            on_message(AssistantMessage(content=[TextBlock(text="starting work")]))
            on_message(AssistantMessage(content=[
                ToolUseBlock(name="Read", input={"file_path": "app/main.py"}, id="read-1")
            ]))
        raise RuntimeError("agent exploded")

    with patch("otto.agent.run_agent_query", side_effect=crashing_query):
        result = await build_agentic_v3("test", tmp_git_repo, {})

    from otto import paths as _paths

    session_dir = _paths.session_dir(tmp_git_repo, result.build_id)
    crash = json.loads((session_dir / "crash.json").read_text())
    checkpoint = json.loads((session_dir / "checkpoint.json").read_text())

    assert crash["exception_class"] == "AgentCallError"
    assert "Agent crashed" in crash["exception_message"]
    assert crash["phase"] == "build"
    assert crash["agent_session_id"] == ""
    assert crash["last_n_events"]

    assert checkpoint["status"] == "paused"
    assert checkpoint["last_activity"] == "reading app/main.py"
    assert checkpoint["last_tool_name"] == "Read"
    assert checkpoint["last_tool_args_summary"] == "app/main.py"
    assert checkpoint["last_operation_started_at"]


@pytest.mark.asyncio
async def test_build_flow_creates_only_active_phase_directory(tmp_git_repo):
    from otto import paths as _paths

    async def fake_run(*args, **kwargs):
        return (
            "STORIES_TESTED: 1\n"
            "STORIES_PASSED: 1\n"
            "STORY_RESULT: smoke | PASS | claim=Smoke works | observed_result=OK | surface=CLI | methodology=cli-execution | summary=Smoke passed\n"
            "COVERAGE_OBSERVED:\n"
            "- Exercised the CLI smoke path\n"
            "COVERAGE_GAPS:\n"
            "- Did not exercise malformed input coverage\n"
            "VERDICT: PASS\n"
            "DIAGNOSIS: null\n",
            0.1,
            "agent-session-1",
            {"round_timings": []},
        )

    from otto.certifier import run_agentic_certifier

    with patch("otto.agent.run_agent_with_timeout", side_effect=fake_run):
        report = await run_agentic_certifier("test", tmp_git_repo, {}, session_id="certify-run-1")

    session_dir = _paths.session_dir(tmp_git_repo, "certify-run-1")
    assert report.outcome.value == "passed"
    assert (_paths.certify_dir(tmp_git_repo, "certify-run-1")).exists()
    assert not (_paths.build_dir(tmp_git_repo, "certify-run-1")).exists()
    assert not (_paths.improve_dir(tmp_git_repo, "certify-run-1")).exists()
    assert not (session_dir / "spec").exists()


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

        from otto import paths as _paths
        build_dir = _paths.build_dir(tmp_git_repo, result.build_id)
        cp = json.loads((build_dir / "checkpoint.json").read_text())
        assert cp["passed"] is False

    @pytest.mark.asyncio
    async def test_fail_pow_shows_failures(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(AGENT_OUTPUT_FAIL)):
            result = await build_agentic_v3("test", tmp_git_repo, {})

        from otto import paths as _paths
        pow_data = json.loads(
            (_paths.certify_dir(tmp_git_repo, result.build_id) / "proof-of-work.json").read_text()
        )
        assert pow_data["outcome"] == "failed"
        failed = [s for s in pow_data["stories"] if not s["passed"]]
        assert len(failed) == 2

    @pytest.mark.asyncio
    async def test_fail_history_entry(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query", side_effect=_make_mock_query(AGENT_OUTPUT_FAIL)):
            await build_agentic_v3("test", tmp_git_repo, {})

        from otto import paths as _paths
        entry = json.loads(
            _paths.history_jsonl(tmp_git_repo).read_text().strip()
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
            result = await build_agentic_v3("test", tmp_git_repo, {})

        from otto import paths as _paths
        pow_data = json.loads(
            (_paths.certify_dir(tmp_git_repo, result.build_id) / "proof-of-work.json").read_text()
        )
        assert pow_data["outcome"] == "passed"
        assert len(pow_data["round_history"]) == 2
        assert pow_data["round_history"][0]["verdict"] == "failed"
        assert pow_data["round_history"][1]["verdict"] == "passed"

    @pytest.mark.asyncio
    async def test_strict_mode_requires_two_consecutive_passes(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
            result = await build_agentic_v3("test", tmp_git_repo, {}, strict_mode=True)

        assert result.passed is False

    @pytest.mark.asyncio
    async def test_strict_mode_passes_after_two_consecutive_passes(self, tmp_git_repo):
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(AGENT_OUTPUT_TWO_PASS)):
            result = await build_agentic_v3("test", tmp_git_repo, {}, strict_mode=True)

        assert result.passed is True

    @pytest.mark.asyncio
    async def test_strict_mode_prompt_includes_reverification_instruction(self, tmp_git_repo):
        captured_prompts = []

        async def capture_query(prompt, options, **kwargs):
            captured_prompts.append(prompt)
            return AGENT_OUTPUT_TWO_PASS, 0.50, MagicMock(session_id="s2")

        with patch("otto.agent.run_agent_query", side_effect=capture_query):
            await build_agentic_v3("test", tmp_git_repo, {}, strict_mode=True)

        assert "STRICT MODE: after the first PASS, run the certifier one more time." in captured_prompts[0]


class TestV3EdgeCases:
    """Edge cases: no markers, empty output, retry context."""

    @pytest.mark.asyncio
    async def test_no_verdict_markers_raises_malformed_output(self, tmp_git_repo):
        from otto.markers import MalformedCertifierOutputError

        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(AGENT_OUTPUT_NO_MARKERS)):
            with pytest.raises(MalformedCertifierOutputError, match="no structured output"):
                await build_agentic_v3("test", tmp_git_repo, {})

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
    async def test_each_run_writes_its_own_session_intent_snapshot(self, tmp_git_repo):
        """Runtime intent belongs to the session dir, not project-root intent.md."""
        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
            first = await build_agentic_v3("feature one", tmp_git_repo, {})

        with patch("otto.agent.run_agent_query",
                    side_effect=_make_mock_query(AGENT_OUTPUT_PASS)):
            second = await build_agentic_v3("feature two", tmp_git_repo, {})

        from otto import paths as _paths

        assert _paths.session_intent(tmp_git_repo, first.build_id).read_text().strip() == "feature one"
        assert _paths.session_intent(tmp_git_repo, second.build_id).read_text().strip() == "feature two"
        assert not (tmp_git_repo / "intent.md").exists()


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
    def test_empty_intent_rejected(self, bad_intent, tmp_git_repo, monkeypatch):
        from click.testing import CliRunner
        from otto.cli import main

        monkeypatch.chdir(tmp_git_repo)
        runner = CliRunner()
        result = runner.invoke(main, ["build", bad_intent])
        assert result.exit_code == 2
