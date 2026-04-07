"""Integration test — end-to-end otto flow with mocked agent."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
import yaml
from click.testing import CliRunner

from otto.cli import main
from otto.config import create_config, load_config
from otto.product_planner import PlannedTask, ProductPlan, _parse_planner_output
from otto.runner import run_task_v45
from otto.tasks import add_task, load_tasks


def _commit_otto_config(repo: Path) -> None:
    """Commit otto.yaml after create_config so the tree stays clean."""
    subprocess.run(
        ["git", "add", "otto.yaml"],
        cwd=repo, capture_output=True, check=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "add otto config"],
        cwd=repo, capture_output=True, check=True,
    )


def _make_fake_result(session_id="test-session"):
    """Create a fake ResultMessage-like object."""
    result = MagicMock()
    result.session_id = session_id
    result.is_error = False
    result.subtype = "success"
    return result


class TestEndToEnd:
    @patch("otto.runner.ClaudeAgentOptions")
    @patch("otto.runner.query")
    def test_task_passes_and_merges(
        self, mock_query, mock_options_cls, tmp_git_repo
    ):
        """Full flow: add task → run_task_v45 → verify → QA → merge to main."""
        create_config(tmp_git_repo)
        _commit_otto_config(tmp_git_repo)

        config = load_config(tmp_git_repo / "otto.yaml")
        config["test_command"] = "true"
        tasks_path = tmp_git_repo / "tasks.yaml"
        add_task(tasks_path, "Create hello.py that prints hello",
                 spec=["hello.py exists and prints hello"])

        async def fake_query(*, prompt, options=None):
            (tmp_git_repo / "hello.py").write_text("print('hello')\n")
            yield _make_fake_result("test-session-123")

        mock_query.side_effect = fake_query

        tasks = load_tasks(tasks_path)
        task = tasks[0]

        with patch("otto.runner.run_qa", new=AsyncMock(return_value={
            "must_passed": True,
            "verdict": {"must_passed": True, "must_items": []},
            "raw_report": "QA PASS",
            "cost_usd": 0.0,
        })):
            result = asyncio.run(run_task_v45(task, config, tmp_git_repo, tasks_path))
        success = result["success"]

        # Verify
        assert success is True
        tasks = load_tasks(tasks_path)
        assert tasks[0]["status"] == "verified"

    @patch("otto.runner.ClaudeAgentOptions")
    @patch("otto.runner.query")
    def test_task_fails_and_reverts(
        self, mock_query, mock_options_cls, tmp_git_repo
    ):
        """Task fails verify_cmd → workspace reverted, main untouched."""
        create_config(tmp_git_repo)
        _commit_otto_config(tmp_git_repo)

        config = load_config(tmp_git_repo / "otto.yaml")
        config["test_command"] = "true"
        config["max_retries"] = 0
        tasks_path = tmp_git_repo / "tasks.yaml"
        add_task(tasks_path, "Do something that fails verification",
                 verify="false", spec=["it works"])

        async def fake_query(*, prompt, options=None):
            (tmp_git_repo / "bad.py").write_text("broken\n")
            yield _make_fake_result("s1")

        mock_query.side_effect = fake_query

        tasks = load_tasks(tasks_path)
        task = tasks[0]

        result = asyncio.run(run_task_v45(task, config, tmp_git_repo, tasks_path))
        success = result["success"]

        assert success is False
        tasks = load_tasks(tasks_path)
        assert tasks[0]["status"] == "failed"
        # bad.py should NOT be on main
        assert not (tmp_git_repo / "bad.py").exists()
        # Branch should be cleaned up
        branches = subprocess.run(
            ["git", "branch"], cwd=tmp_git_repo,
            capture_output=True, text=True,
        ).stdout
        assert "otto/" not in branches

    @patch("otto.runner._snapshot_untracked", side_effect=RuntimeError("setup boom"))
    def test_setup_exception_marks_task_failed(
        self, mock_snapshot, tmp_git_repo
    ):
        """Setup failures should not leave the task stuck in running."""
        create_config(tmp_git_repo)
        _commit_otto_config(tmp_git_repo)

        config = load_config(tmp_git_repo / "otto.yaml")
        config["max_retries"] = 0
        tasks_path = tmp_git_repo / "tasks.yaml"
        add_task(tasks_path, "Task that fails during setup", spec=["it works"])

        task = load_tasks(tasks_path)[0]
        result = asyncio.run(run_task_v45(task, config, tmp_git_repo, tasks_path))
        success = result["success"]

        assert success is False
        failed_task = load_tasks(tasks_path)[0]
        assert failed_task["status"] == "failed"
        assert failed_task["error_code"] == "internal_error"
        assert "setup boom" in failed_task["error"]


class TestBuildCommand:
    def test_build_skips_product_qa_after_inner_failure(
        self,
        tmp_git_repo,
        monkeypatch,
    ):
        monkeypatch.chdir(tmp_git_repo)
        create_config(tmp_git_repo)

        tasks_path = tmp_git_repo / "tasks.yaml"

        product_spec_path = tmp_git_repo / "product-spec.md"
        product_spec_path.write_text("# Product Spec\n")
        plan = ProductPlan(
            mode="decomposed",
            tasks=[PlannedTask(prompt="Build the actual product")],
            product_spec_path=product_spec_path,
            architecture_path=None,
        )

        from otto.pipeline import BuildResult
        async def fake_build(intent, project_dir, config, **kwargs):
            return BuildResult(passed=False, build_id="test-build", error="build failed",
                               tasks_passed=0, tasks_failed=1)

        runner = CliRunner()
        with patch("otto.pipeline.build_agentic_v3", side_effect=fake_build):
            result = runner.invoke(main, ["build", "demo app", "--no-review"])

        assert result.exit_code == 1

    def test_build_exit_code_tracks_product_qa_result(self, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        create_config(tmp_git_repo)

        from otto.pipeline import BuildResult
        async def fake_build(intent, project_dir, config, **kwargs):
            return BuildResult(
                passed=False, build_id="test-build", rounds=1,
                journeys=[{"name": "happy path", "passed": False}],
                tasks_passed=1, tasks_failed=0,
            )

        runner = CliRunner()
        with patch("otto.pipeline.build_agentic_v3", side_effect=fake_build):
            result = runner.invoke(main, ["build", "demo app", "--no-review"])

        assert result.exit_code == 1
        assert "Some journeys failed" in result.output

    def test_build_sets_exit_code_when_product_qa_raises(self, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        create_config(tmp_git_repo)

        async def fake_build(intent, project_dir, config, **kwargs):
            raise RuntimeError("qa boom")

        runner = CliRunner()
        with patch("otto.pipeline.build_agentic_v3", side_effect=fake_build):
            result = runner.invoke(main, ["build", "demo app", "--no-review"])

        assert result.exit_code == 1
        assert "qa boom" in result.output

    def test_build_split_mode_runs(self, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        create_config(tmp_git_repo)

        from otto.pipeline import BuildResult
        async def fake_split(intent, project_dir, config):
            return BuildResult(passed=True, build_id="test-split", total_cost=0.5)

        runner = CliRunner()
        with patch("otto.pipeline.build_agentic_v2", side_effect=fake_split):
            result = runner.invoke(
                main,
                ["build", "demo app", "--split", "--no-review"],
            )

        assert result.exit_code == 0

    def test_resume_build_uses_checkpoint(self, tmp_git_repo, monkeypatch):
        monkeypatch.chdir(tmp_git_repo)
        create_config(tmp_git_repo)

        checkpoint_dir = tmp_git_repo / "otto_logs" / "builds" / "build-123"
        checkpoint_dir.mkdir(parents=True)
        checkpoint_path = checkpoint_dir / "checkpoint.json"
        checkpoint_path.write_text(json.dumps({
            "session_id": "s1",
            "base_sha": "abc123",
            "round": 1,
            "verification_round": 2,
            "state": "fixing",
            "certifier_outcome": "failed",
            "candidate_sha": "def456",
            "intent": "resume this build",
            "last_status": "ready_for_review",
            "last_summary": "partial build",
            "findings": [],
            "cost_so_far": 1.2,
            "agent_cost_so_far": 0.5,
            "certifier_cost_so_far": 0.7,
        }))

        from otto.pipeline import BuildResult

        async def fake_resume(checkpoint_path, project_dir, config, **kwargs):
            return BuildResult(passed=True, build_id="build-123", rounds=2, total_cost=1.2)

        runner = CliRunner()
        with patch("otto.pipeline.resume_continuous", side_effect=fake_resume):
            result = runner.invoke(main, ["resume-build", str(checkpoint_path)])

        assert result.exit_code == 0
        assert "resume this build" in result.output


class TestPipelineE2E:
    """E2E tests for the full pipeline: build → certify → fix → verify.

    Mocks only the external boundaries (orchestrator + certifier).
    Everything else runs for real: pipeline.py, verification.py, tasks.yaml, git.
    """

    @pytest.mark.asyncio
    async def test_monolithic_certify_fix_verify(self, tmp_git_repo):
        """Full cycle: build → certify (fail) → fix task → re-verify (pass)."""
        from otto.config import create_config
        from otto.pipeline import build_product
        from otto.tasks import load_tasks, update_task
        from otto.certifier.report import (
            CertificationOutcome, CertificationReport, Finding, TierResult, TierStatus,
        )

        create_config(tmp_git_repo)
        _commit_otto_config(tmp_git_repo)

        certifier_calls = []

        async def fake_run_per(config, tasks_path, project_dir):
            tasks = load_tasks(tasks_path)
            for task in tasks:
                if task.get("status") == "pending":
                    update_task(tasks_path, task["key"], status="passed")
            return 0

        def fake_certifier(intent, project_dir, config, *,
                           port_override=None, stories_path=None,
                           skip_story_ids=None):
            certifier_calls.append({"skip": set(skip_story_ids) if skip_story_ids else None})
            if len(certifier_calls) == 1:
                # Round 1: one story fails
                tier4 = TierResult(tier=4, name="journeys", status=TierStatus.FAILED,
                    findings=[Finding(
                        tier=4, severity="critical", category="journey",
                        description="Story failed: counter persists",
                        diagnosis="Counter resets on refresh",
                        fix_suggestion="Add localStorage",
                        story_id="counter-persist",
                        evidence={"steps": [
                            {"action": "refresh page", "outcome": "fail",
                             "diagnosis": "No persistence", "fix_suggestion": "Use localStorage"},
                        ]},
                    )],
                    cost_usd=0.50, duration_s=30.0)
                # Stash raw results for story tracking
                _mock_results = MagicMock()
                _mock_results.results = [
                    MagicMock(passed=False, story_id="counter-persist", story_title="counter persists",
                              persona="user", blocked_at=None, summary="", diagnosis="Counter resets",
                              fix_suggestion="Add localStorage", steps=[], break_findings=[]),
                    MagicMock(passed=True, story_id="counter-display", story_title="counter displays",
                              persona="user", blocked_at=None, summary="", diagnosis="",
                              fix_suggestion="", steps=[], break_findings=[]),
                ]
                tier4._cert_result = _mock_results
                tier4._stories_output = []
                return CertificationReport(
                    product_type="web", interaction="http",
                    tiers=[tier4],
                    findings=tier4.findings,
                    outcome=CertificationOutcome.FAILED,
                    cost_usd=0.50, duration_s=30.0,
                )
            # Round 2: all pass
            tier4 = TierResult(tier=4, name="journeys", status=TierStatus.PASSED,
                cost_usd=0.30, duration_s=20.0)
            _mock_results = MagicMock()
            _mock_results.results = [
                MagicMock(passed=True, story_id="counter-persist", story_title="counter persists",
                          persona="user", blocked_at=None, summary="", diagnosis="",
                          fix_suggestion="", steps=[], break_findings=[]),
            ]
            tier4._cert_result = _mock_results
            tier4._stories_output = []
            return CertificationReport(
                product_type="web", interaction="http",
                tiers=[tier4],
                outcome=CertificationOutcome.PASSED,
                cost_usd=0.30, duration_s=20.0,
            )

        # Mock the verify subprocess to return the full verification result.
        # The real subprocess runs the full certify→fix→re-certify loop.
        def fake_verify_subprocess(intent, project_dir, tasks_path,
                                    product_spec_path, config):
            return {
                "product_passed": True,
                "rounds": 2,
                "total_cost": 0.80,
                "journeys": [],
                "fix_tasks_created": 1,
            }

        with patch("otto.orchestrator.run_per", side_effect=fake_run_per), \
             patch("otto.pipeline._run_verify_subprocess", side_effect=fake_verify_subprocess):
            result = await build_product(
                "Build a counter app",
                tmp_git_repo,
                {"test_command": "true", "default_branch": "main"},
            )

        # Pipeline completed with fix loop
        assert result.passed is True
        assert result.rounds == 2
        assert result.total_cost > 0

        # intent.md was created as grounding
        assert (tmp_git_repo / "intent.md").exists()

    @pytest.mark.asyncio
    async def test_monolithic_build_passes_first_try(self, tmp_git_repo):
        """Happy path: build → certify (pass) → done in 1 round."""
        from otto.config import create_config
        from otto.pipeline import build_product
        from otto.tasks import load_tasks
        from otto.certifier.report import (
            CertificationOutcome, CertificationReport, TierResult, TierStatus,
        )

        create_config(tmp_git_repo)
        _commit_otto_config(tmp_git_repo)

        async def fake_run_per(config, tasks_path, project_dir):
            tasks = load_tasks(tasks_path)
            for task in tasks:
                if task.get("status") == "pending":
                    from otto.tasks import update_task
                    update_task(tasks_path, task["key"], status="passed")
            return 0

        def fake_certifier(**kwargs):
            return CertificationReport(
                product_type="web", interaction="http",
                tiers=[TierResult(tier=4, name="journeys", status=TierStatus.PASSED, cost_usd=0.30)],
                outcome=CertificationOutcome.PASSED,
                cost_usd=0.30, duration_s=15.0,
            )

        def fake_verify_subprocess(intent, project_dir, tasks_path,
                                    product_spec_path, config):
            return {
                "product_passed": True,
                "rounds": 1,
                "total_cost": 0.30,
                "journeys": [],
                "fix_tasks_created": 0,
            }

        with patch("otto.orchestrator.run_per", side_effect=fake_run_per), \
             patch("otto.pipeline._run_verify_subprocess", side_effect=fake_verify_subprocess):
            result = await build_product(
                "Build a hello world app",
                tmp_git_repo,
                {"test_command": "true", "default_branch": "main"},
            )

        assert result.passed is True
        assert result.rounds == 1

    @pytest.mark.asyncio
    async def test_infra_error_stops_without_fix_task(self, tmp_git_repo):
        """Certifier blocked (app won't start) → stop, no fix tasks."""
        from otto.config import create_config
        from otto.pipeline import build_product
        from otto.tasks import load_tasks
        from otto.certifier.report import (
            CertificationOutcome, CertificationReport, Finding, TierResult, TierStatus,
        )

        create_config(tmp_git_repo)
        _commit_otto_config(tmp_git_repo)

        async def fake_run_per(config, tasks_path, project_dir):
            tasks = load_tasks(tasks_path)
            for task in tasks:
                if task.get("status") == "pending":
                    from otto.tasks import update_task
                    update_task(tasks_path, task["key"], status="passed")
            return 0

        def fake_certifier(**kwargs):
            return CertificationReport(
                product_type="web", interaction="http",
                tiers=[
                    TierResult(tier=4, name="journeys", status=TierStatus.FAILED,
                        findings=[Finding(tier=4, severity="critical", category="journey",
                            description="App failed to start", diagnosis="port 3000 not responding")]),
                ],
                findings=[Finding(tier=4, severity="critical", category="journey",
                    description="App failed to start", diagnosis="port 3000 not responding")],
                outcome=CertificationOutcome.FAILED,
                cost_usd=0.0, duration_s=5.0,
            )

        def fake_verify_subprocess(intent, project_dir, tasks_path,
                                    product_spec_path, config):
            return {
                "product_passed": False,
                "rounds": 1,
                "total_cost": 0.0,
                "journeys": [],
                "fix_tasks_created": 0,
            }

        with patch("otto.orchestrator.run_per", side_effect=fake_run_per), \
             patch("otto.pipeline._run_verify_subprocess", side_effect=fake_verify_subprocess):
            result = await build_product(
                "Build an app",
                tmp_git_repo,
                {"test_command": "true", "default_branch": "main"},
            )

        assert result.passed is False

    @pytest.mark.asyncio
    async def test_no_progress_stops_early(self, tmp_git_repo):
        """Same failure count across rounds → stop early, don't loop forever."""
        from otto.config import create_config
        from otto.pipeline import build_product
        from otto.certifier.report import (
            CertificationOutcome, CertificationReport, Finding, TierResult, TierStatus,
        )

        create_config(tmp_git_repo)
        _commit_otto_config(tmp_git_repo)

        call_count = 0

        async def fake_run_per(config, tasks_path, project_dir):
            from otto.tasks import load_tasks, update_task
            tasks = load_tasks(tasks_path)
            for task in tasks:
                if task.get("status") == "pending":
                    update_task(tasks_path, task["key"], status="passed")
            return 0

        def fake_certifier(**kwargs):
            nonlocal call_count
            call_count += 1
            return CertificationReport(
                product_type="web", interaction="http",
                tiers=[
                    TierResult(tier=4, name="journeys", status=TierStatus.FAILED,
                        findings=[Finding(tier=4, severity="critical", category="journey",
                            description="Story failed: broken story",
                            diagnosis="Still broken", fix_suggestion="fix it",
                            story_id="broken")]),
                ],
                findings=[Finding(tier=4, severity="critical", category="journey",
                    description="Story failed: broken story",
                    diagnosis="Still broken", fix_suggestion="fix it",
                    story_id="broken")],
                outcome=CertificationOutcome.FAILED,
                cost_usd=0.50, duration_s=30.0,
            )

        def fake_verify_subprocess(intent, project_dir, tasks_path,
                                    product_spec_path, config):
            return {
                "product_passed": False,
                "rounds": 2,
                "total_cost": 1.0,
                "journeys": [],
                "fix_tasks_created": 1,
            }

        with patch("otto.orchestrator.run_per", side_effect=fake_run_per), \
             patch("otto.pipeline._run_verify_subprocess", side_effect=fake_verify_subprocess):
            result = await build_product(
                "Build something",
                tmp_git_repo,
                {"test_command": "true", "default_branch": "main"},
            )

        assert result.passed is False


class TestPlannerConfig:
    def test_decomposed_plan_requires_product_spec_file(self, tmp_path):
        raw = json.dumps({
            "mode": "decomposed",
            "tasks": [{"prompt": "Build feature A"}],
        })

        with pytest.raises(ValueError, match="product-spec.md"):
            _parse_planner_output(raw, tmp_path)


class TestUnifiedCertifierRegressions:
    @pytest.mark.asyncio
    async def test_build_product_forces_skip_qa_and_skip_spec(self, tmp_git_repo):
        from otto.pipeline import build_product

        seen_config = {}
        create_config(tmp_git_repo)
        config = load_config(tmp_git_repo / "otto.yaml")

        async def fake_run_per(config, tasks_path, project_dir):
            seen_config.update(config)
            return 0

        def fake_verify_subprocess(intent, project_dir, tasks_path,
                                    product_spec_path, config):
            return {"product_passed": True, "rounds": 1, "total_cost": 0.0, "journeys": []}

        with patch("otto.pipeline._commit_artifacts"), \
             patch("otto.orchestrator.run_per", side_effect=fake_run_per), \
             patch("otto.pipeline._run_verify_subprocess", side_effect=fake_verify_subprocess):
            result = await build_product(
                "Build a demo app",
                tmp_git_repo,
                config,
            )

        assert result.passed is True
        assert seen_config["skip_qa"] is True
        assert seen_config["skip_spec"] is True

    def test_tier4_fails_on_story_result_count_mismatch(self, tmp_git_repo):
        from otto.certifier import _run_journeys
        from otto.certifier.report import CertificationReport, TierStatus
        from otto.certifier.stories import StorySet, StoryStep, UserStory

        stories = [
            UserStory(
                id="story-1",
                persona="user",
                title="Story One",
                narrative="first flow",
                steps=[StoryStep(action="do first thing", verify="first thing works")],
            ),
            UserStory(
                id="story-2",
                persona="user",
                title="Story Two",
                narrative="second flow",
                steps=[StoryStep(action="do second thing", verify="second thing works")],
            ),
        ]
        story_set = StorySet(intent="intent", stories=stories, cost_usd=0.2)
        cert_result = SimpleNamespace(
            certified=True,
            total_cost_usd=0.3,
            results=[
                SimpleNamespace(
                    story_id="story-1",
                    story_title="Story One",
                    persona="user",
                    passed=True,
                    blocked_at=None,
                    summary="passed",
                    diagnosis="",
                    fix_suggestion="",
                    steps=[],
                    break_findings=[],
                )
            ],
        )

        with patch(
            "otto.certifier.stories.load_or_compile_stories",
            return_value=(story_set, "cache", None, 0.0),
        ), patch(
            "otto.certifier.journey_agent.verify_all_stories",
            new=AsyncMock(return_value=cert_result),
        ):
            result = _run_journeys(
                intent="intent",
                project_dir=tmp_git_repo,
                config={},
                manifest=SimpleNamespace(base_url="http://localhost:3000"),
                stories_path=None,
                skip_story_ids=None,
            )

        assert result.status == TierStatus.FAILED
        mismatch = next(
            finding
            for finding in result.findings
            if finding.category == "harness" and finding.severity == "warning"
        )
        assert mismatch.evidence["stories_requested"] == 2
        assert mismatch.evidence["results_received"] == 1
        report = CertificationReport(
            product_type="web",
            interaction="http",
            tiers=[result],
            findings=result.findings,
        )
        assert report.critical_findings() == []

    def test_tier4_uses_story_criticality_and_empty_story_status(self, tmp_git_repo):
        from otto.certifier import _run_journeys
        from otto.certifier.report import TierStatus
        from otto.certifier.stories import StorySet, StoryStep, UserStory

        story = UserStory(
            id="noncritical-story",
            persona="user",
            title="Noncritical Story",
            narrative="test flow",
            steps=[StoryStep(action="do thing", verify="thing works")],
            critical=False,
        )
        story_set = StorySet(intent="intent", stories=[story], cost_usd=0.2)
        cert_result = SimpleNamespace(
            certified=False,
            total_cost_usd=0.3,
            results=[
                SimpleNamespace(
                    story_id=story.id,
                    story_title=story.title,
                    persona=story.persona,
                    passed=False,
                    blocked_at="step 1",
                    summary="failed",
                    diagnosis="broke",
                    fix_suggestion="fix it",
                    steps=[
                        SimpleNamespace(
                            action="do thing",
                            outcome="fail",
                            diagnosis="broke",
                            fix_suggestion="fix it",
                            verification="checked",
                        )
                    ],
                    break_findings=[],
                )
            ],
        )

        with patch(
            "otto.certifier.stories.load_or_compile_stories",
            return_value=(story_set, "cache", None, 0.0),
        ), patch(
            "otto.certifier.journey_agent.verify_all_stories",
            new=AsyncMock(return_value=cert_result),
        ):
            result = _run_journeys(
                intent="intent",
                project_dir=tmp_git_repo,
                config={},
                manifest=SimpleNamespace(base_url="http://localhost:3000"),
                stories_path=None,
                skip_story_ids=None,
            )

        assert result.status == TierStatus.FAILED
        assert result.findings[0].severity == "important"

        empty_story_set = StorySet(intent="intent", stories=[], cost_usd=0.0)
        with patch(
            "otto.certifier.stories.load_or_compile_stories",
            return_value=(empty_story_set, "cache", None, 0.0),
        ):
            empty_result = _run_journeys(
                intent="intent",
                project_dir=tmp_git_repo,
                config={},
                manifest=SimpleNamespace(base_url="http://localhost:3000"),
                stories_path=None,
                skip_story_ids=None,
            )

        assert empty_result.status == TierStatus.SKIPPED
        assert empty_result.skip_reason == "no stories to test"

        with patch(
            "otto.certifier.stories.load_or_compile_stories",
            return_value=(story_set, "cache", None, 0.0),
        ):
            skipped_result = _run_journeys(
                intent="intent",
                project_dir=tmp_git_repo,
                config={},
                manifest=SimpleNamespace(base_url="http://localhost:3000"),
                stories_path=None,
                skip_story_ids={story.id},
            )

        assert skipped_result.status == TierStatus.PASSED

    def test_unified_certifier_manifest_failure_falls_back_to_minimal(self, tmp_git_repo):
        from otto.certifier import run_unified_certifier
        from otto.certifier.report import CertificationOutcome, TierStatus
        from otto.certifier.journey_agent import ProjectDiscovery

        discovery = ProjectDiscovery(
            product_type="api", interaction="http",
            base_url="http://localhost:3000", app_started=True,
        )

        # When build_manifest fails, certifier should fall back to minimal
        # manifest and still run journeys (which we mock to pass).
        from otto.certifier.stories import StorySet
        empty_stories = StorySet(intent="intent", stories=[], cost_usd=0.0)

        with patch("otto.certifier.classifier.classify", return_value=SimpleNamespace(
            product_type="web",
            interaction="http",
            framework="unknown",
            language="unknown",
        )), patch(
            "otto.certifier.adapter.analyze_project",
            return_value=SimpleNamespace(),
        ), patch(
            "otto.certifier.journey_agent.discover_project",
            return_value=discovery,
        ), patch(
            "otto.certifier.manifest.build_manifest",
            side_effect=RuntimeError("manifest boom"),
        ), patch(
            "otto.certifier.stories.load_or_compile_stories",
            return_value=(empty_stories, "cache", None, 0.0),
        ):
            report = run_unified_certifier("intent", tmp_git_repo, {})

        # With no stories, journeys tier is SKIPPED, outcome is PASSED
        assert report.outcome == CertificationOutcome.PASSED
        assert any(t.name == "journeys" for t in report.tiers)

    def test_verification_log_timestamps_every_line(self, tmp_path):
        from otto.verification import _verification_log

        _verification_log(tmp_path, "line one", "line two")

        log_path = tmp_path / "otto_logs" / "verification.log"
        lines = log_path.read_text().splitlines()
        assert len(lines) == 2
        assert all(line.startswith("[20") and "] " in line for line in lines)
