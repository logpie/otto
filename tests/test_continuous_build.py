"""Tests for continuous/agentic build infrastructure: session, feedback, isolated certifier."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

from otto.session import AgentSession, SessionCheckpoint, SessionResult
from otto.feedback import format_certifier_as_feedback, finding_fingerprints
from otto.certifier.report import (
    CertificationOutcome,
    CertificationReport,
    Finding,
    TierResult,
    TierStatus,
)


class TestSessionCheckpoint:
    def test_save_and_load(self, tmp_path):
        cp = SessionCheckpoint(
            session_id="sess-123",
            base_sha="abc1234",
            round=2,
            state="certified",
            certifier_outcome="failed",
            candidate_sha="def5678",
            intent="build a todo app",
            last_summary="Built CRUD API",
            findings=[{"description": "XSS found"}],
            cost_so_far=1.23,
        )
        path = tmp_path / "checkpoint.json"
        cp.save(path)

        loaded = SessionCheckpoint.load(path)
        assert loaded is not None
        assert loaded.session_id == "sess-123"
        assert loaded.round == 2
        assert loaded.state == "certified"
        assert loaded.certifier_outcome == "failed"
        assert loaded.findings == [{"description": "XSS found"}]

    def test_load_missing_file(self, tmp_path):
        assert SessionCheckpoint.load(tmp_path / "nonexistent.json") is None

    def test_load_corrupt_file(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("not json")
        assert SessionCheckpoint.load(path) is None


class TestSessionResult:
    def test_end_status_from_structured_output(self):
        result_msg = SimpleNamespace(
            structured_output={"status": "blocked", "summary": "stuck on auth"},
        )
        sr = SessionResult(text="", cost=0, result_msg=result_msg, session_id="s1")
        assert sr.end_status == "blocked"
        assert sr.summary == "stuck on auth"

    def test_end_status_fallback(self):
        sr = SessionResult(text="done building", cost=0, result_msg=None, session_id="s1")
        assert sr.end_status == "ready_for_review"
        assert sr.summary == "done building"


class TestAgentSession:
    @pytest.mark.asyncio
    async def test_start_captures_session_id(self, tmp_git_repo):
        result_msg = SimpleNamespace(session_id="sess-abc", total_cost_usd=0.5)

        async def fake_query(prompt, options, **kwargs):
            return "built the product", 0.5, result_msg

        with patch("otto.session.run_agent_query", side_effect=fake_query), \
             patch("otto.session.agent_provider", return_value="claude"):
            session = AgentSession(
                intent="build todo", options=MagicMock(), project_dir=tmp_git_repo,
            )
            result = await session.start("Build a todo app")

        assert session.session_id == "sess-abc"
        assert session.total_cost == 0.5
        assert result.session_id == "sess-abc"

    @pytest.mark.asyncio
    async def test_resume_with_session_id(self, tmp_git_repo):
        result_msg = SimpleNamespace(session_id="sess-abc", total_cost_usd=0.3)

        async def fake_query(prompt, options, **kwargs):
            return "fixed the bugs", 0.3, result_msg

        with patch("otto.session.run_agent_query", side_effect=fake_query), \
             patch("otto.session.agent_provider", return_value="claude"):
            session = AgentSession(
                intent="build todo", options=MagicMock(), project_dir=tmp_git_repo,
            )
            session.session_id = "sess-abc"
            session._supports_resume = True
            result = await session.resume("Fix XSS vulnerability")

        assert result.text == "fixed the bugs"
        assert session.total_cost == 0.3

    @pytest.mark.asyncio
    async def test_resume_fallback_on_failure(self, tmp_git_repo):
        call_count = 0

        async def fake_query(prompt, options, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1 and getattr(options, "resume", None):
                raise RuntimeError("resume failed")
            return "fixed via state package", 0.4, SimpleNamespace(session_id="sess-new")

        with patch("otto.session.run_agent_query", side_effect=fake_query), \
             patch("otto.session.agent_provider", return_value="claude"):
            session = AgentSession(
                intent="build todo",
                options=MagicMock(permission_mode="bypassPermissions", cwd="/tmp",
                                  model=None, system_prompt=None, mcp_servers=None,
                                  env=None, setting_sources=None, disallowed_tools=None,
                                  output_format=None),
                project_dir=tmp_git_repo,
            )
            session.session_id = "sess-abc"
            session._supports_resume = True
            result = await session.resume("Fix bugs")

        assert result.text == "fixed via state package"
        assert session.session_id == "sess-new"
        assert call_count == 2  # first failed, second succeeded

    @pytest.mark.asyncio
    async def test_resume_fallback_on_error_result(self, tmp_git_repo):
        call_count = 0

        async def fake_query(prompt, options, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1 and getattr(options, "resume", None):
                return "resume failed", 0.1, SimpleNamespace(
                    session_id="sess-abc",
                    is_error=True,
                    subtype="error",
                )
            return "fixed via state package", 0.4, SimpleNamespace(session_id="sess-new")

        with patch("otto.session.run_agent_query", side_effect=fake_query), \
             patch("otto.session.agent_provider", return_value="claude"):
            session = AgentSession(
                intent="build todo",
                options=MagicMock(permission_mode="bypassPermissions", cwd="/tmp",
                                  model=None, system_prompt=None, mcp_servers=None,
                                  env=None, setting_sources=None, disallowed_tools=None,
                                  output_format=None),
                project_dir=tmp_git_repo,
            )
            session.session_id = "sess-abc"
            session._supports_resume = True
            result = await session.resume("Fix bugs")

        assert result.text == "fixed via state package"
        assert session.session_id == "sess-new"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_start_raises_on_missing_result_message(self, tmp_git_repo):
        async def fake_query(prompt, options, **kwargs):
            return "built the product", 0.5, None

        with patch("otto.session.run_agent_query", side_effect=fake_query), \
             patch("otto.session.agent_provider", return_value="claude"):
            session = AgentSession(
                intent="build todo", options=MagicMock(), project_dir=tmp_git_repo,
            )
            with pytest.raises(RuntimeError, match="session start returned no result message"):
                await session.start("Build a todo app")

    @pytest.mark.asyncio
    async def test_resume_with_state_package_raises_on_error_result(self, tmp_git_repo):
        async def fake_query(prompt, options, **kwargs):
            return "resume failed", 0.1, SimpleNamespace(
                session_id="sess-new",
                is_error=True,
                subtype="error",
            )

        with patch("otto.session.run_agent_query", side_effect=fake_query), \
             patch("otto.session.agent_provider", return_value="openai"):
            session = AgentSession(
                intent="build todo",
                options=MagicMock(),
                project_dir=tmp_git_repo,
            )
            with pytest.raises(RuntimeError, match="state package resume returned invalid result"):
                await session.resume("Fix bugs")


class TestFeedback:
    def test_format_actionable_findings(self):
        report = CertificationReport(
            product_type="web", interaction="http",
            findings=[
                Finding(tier=4, severity="critical", category="journey",
                        description="XSS in todo creation",
                        diagnosis="HTML stored without sanitization",
                        fix_suggestion="Escape HTML entities"),
            ],
            outcome=CertificationOutcome.FAILED,
        )
        feedback = format_certifier_as_feedback(report)
        assert feedback is not None
        assert "XSS in todo creation" in feedback
        assert "HTML stored without sanitization" in feedback
        assert "Escape HTML entities" in feedback

    def test_returns_none_for_passed(self):
        report = CertificationReport(
            product_type="web", interaction="http",
            outcome=CertificationOutcome.PASSED,
        )
        assert format_certifier_as_feedback(report) is None

    def test_returns_none_for_blocked(self):
        report = CertificationReport(
            product_type="web", interaction="http",
            outcome=CertificationOutcome.BLOCKED,
        )
        assert format_certifier_as_feedback(report) is None

    def test_finding_fingerprints_stable(self):
        findings = [
            Finding(tier=4, severity="critical", category="journey",
                    description="XSS in todo creation", story_id="s1"),
            Finding(tier=4, severity="important", category="edge-case",
                    description="Missing validation", story_id="s2"),
        ]
        fp1 = finding_fingerprints(findings)
        fp2 = finding_fingerprints(findings)
        assert fp1 == fp2
        assert len(fp1) == 2

    def test_finding_fingerprints_detects_change(self):
        findings_a = [
            Finding(tier=4, severity="critical", category="journey",
                    description="XSS", story_id="s1"),
        ]
        findings_b = [
            Finding(tier=4, severity="critical", category="journey",
                    description="Missing auth", story_id="s2"),
        ]
        assert finding_fingerprints(findings_a) != finding_fingerprints(findings_b)

    def test_certify_cli_issue_fingerprints_ignore_detail_text(self):
        from otto.certifier.certify_cli import _issue_fingerprints

        issues_a = [{
            "category": "journey",
            "what": "Task creation stores raw HTML in title",
            "detail": "first diagnosis",
            "story": "story-1",
        }]
        issues_b = [{
            "category": "journey",
            "what": "Task creation stores raw HTML in title",
            "detail": "different diagnosis wording",
            "story": "story-1",
        }]

        assert _issue_fingerprints(issues_a) == _issue_fingerprints(issues_b)


class TestContinuousBuild:
    def test_snapshot_candidate_rejects_pre_existing_untracked_source_files(self, tmp_git_repo):
        from otto.pipeline import _snapshot_candidate

        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_git_repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        (tmp_git_repo / "scratch.py").write_text("print('keep me out')\n")

        with patch("otto.git_ops.build_candidate_commit") as build_mock:
            with pytest.raises(RuntimeError, match="eligible untracked files"):
                _snapshot_candidate(
                    tmp_git_repo,
                    1,
                    base_sha,
                    pre_existing_untracked={"scratch.py"},
                )

        build_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_continuous_passes_first_round(self, tmp_git_repo):
        """Agent builds, certifier passes on round 1."""
        from otto.pipeline import build_continuous

        async def fake_query(prompt, options, **kwargs):
            return "built the product", 0.3, SimpleNamespace(
                session_id="s1", structured_output={"status": "ready_for_review", "summary": "done"},
            )

        passing_report = CertificationReport(
            product_type="web", interaction="http",
            outcome=CertificationOutcome.PASSED,
            cost_usd=1.50, duration_s=300.0,
        )

        with patch("otto.session.run_agent_query", side_effect=fake_query), \
             patch("otto.session.agent_provider", return_value="claude"), \
             patch("otto.certifier.isolated.certify_with_retry", return_value=passing_report), \
             patch("otto.pipeline._snapshot_candidate", return_value="abc1234"), \
             patch("otto.pipeline._commit_artifacts"), \
             patch("otto.git_ops._snapshot_untracked", return_value=set()), \
             patch("otto.git_ops.check_clean_tree", return_value=True):
            result = await build_continuous(
                "Build a todo app", tmp_git_repo, {"default_branch": "main"},
            )

        assert result.passed is True
        assert result.rounds == 1

    @pytest.mark.asyncio
    async def test_agentic_runs_single_session(self, tmp_git_repo):
        """Agentic mode: one session, agent drives everything."""
        import json as _json
        from otto.pipeline import build_agentic

        async def fake_query(prompt, options, **kwargs):
            # Simulate: agent built product and certify passed
            # Write fake history to simulate certify CLI results
            job_root = tmp_git_repo / "otto_logs" / "certify-job"
            job_root.mkdir(parents=True, exist_ok=True)
            job_dir = job_root / "20260404-000000-000001"
            job_dir.mkdir()
            (job_root / "history.json").write_text(_json.dumps([
                {"status": "passed", "round": 1, "cost_usd": 1.50,
                 "issues_count": 0, "stories_passed": 7, "stories_total": 7}
            ]))
            (job_dir / "job.json").write_text(_json.dumps({
                "job_id": job_dir.name,
                "status": "passed",
                "round": 1,
                "cost_usd": 1.50,
            }))
            return "built and certified", 0.5, SimpleNamespace(session_id="s1")

        with patch("otto.agent.run_agent_query", side_effect=fake_query), \
             patch("otto.pipeline._commit_artifacts"):
            result = await build_agentic(
                "Build a todo app", tmp_git_repo, {"default_branch": "main"},
            )

        assert result.passed is True
        assert result.rounds == 1
        assert result.total_cost == 2.0  # 0.5 agent + 1.5 certifier

    @pytest.mark.asyncio
    async def test_agentic_waits_for_running_certify_job(self, tmp_git_repo):
        from otto.pipeline import build_agentic

        async def fake_query(prompt, options, **kwargs):
            job_root = tmp_git_repo / "otto_logs" / "certify-job"
            job_root.mkdir(parents=True, exist_ok=True)
            job_dir = job_root / "20260404-000000-000002"
            job_dir.mkdir()
            (job_dir / "job.json").write_text(json.dumps({
                "job_id": job_dir.name,
                "status": "running",
                "round": 1,
                "pid": 12345,
            }))

            async def finish_job():
                await asyncio.sleep(0.05)
                (job_dir / "job.json").write_text(json.dumps({
                    "job_id": job_dir.name,
                    "status": "passed",
                    "round": 1,
                    "cost_usd": 1.25,
                }))
                (job_root / "history.json").write_text(json.dumps([
                    {"status": "passed", "round": 1, "cost_usd": 1.25}
                ]))

            asyncio.create_task(finish_job())
            return "agent returned before certify finished", 0.4, SimpleNamespace(session_id="s1")

        with patch("otto.agent.run_agent_query", side_effect=fake_query), \
             patch("otto.pipeline._commit_artifacts"):
            result = await build_agentic(
                "Build a todo app",
                tmp_git_repo,
                {"default_branch": "main", "agentic_certify_wait_timeout_s": 2},
            )

        assert result.passed is True
        assert result.rounds == 1
        assert result.total_cost == pytest.approx(1.65)
        assert result.error == ""

    def test_certify_start_refuses_when_another_job_is_alive(self, tmp_path, monkeypatch, capsys):
        from otto.certifier import certify_cli

        project_dir = tmp_path / "project"
        project_dir.mkdir()
        intent_file = tmp_path / "intent.md"
        intent_file.write_text("Build a todo app")

        job_dir = project_dir / "otto_logs" / "certify-job" / "20260404-000000-000001"
        job_dir.mkdir(parents=True)
        (job_dir / "job.json").write_text(json.dumps({
            "job_id": job_dir.name,
            "status": "running",
            "pid": 99999,
        }))

        monkeypatch.setattr(certify_cli.sys, "argv", [
            "certify_cli.py",
            "start",
            str(project_dir),
            str(intent_file),
        ])
        monkeypatch.setattr(certify_cli.os, "kill", lambda pid, sig: None)

        certify_cli._cmd_start()

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "already_running"
        assert output["job_id"] == job_dir.name

    def test_snapshot_writes_job_error_when_commit_fails(self, tmp_path):
        from otto.certifier import certify_cli

        job_dir = tmp_path / "otto_logs" / "certify-job" / "20260404-000000-000001"
        job_dir.mkdir(parents=True)
        certify_cli._write_job(job_dir, {"job_id": job_dir.name, "status": "starting"})

        calls = [
            subprocess.CompletedProcess(["git", "add", "-A"], 0, stdout="", stderr=""),
            subprocess.CompletedProcess(["git", "commit"], 1, stdout="", stderr="commit failed"),
        ]

        with patch("otto.certifier.certify_cli.subprocess.run", side_effect=calls):
            with pytest.raises(RuntimeError, match="Failed to create certification snapshot commit"):
                certify_cli._snapshot(tmp_path, job_dir)

        state = certify_cli._read_job(job_dir)
        assert state["status"] == "error"
        assert state["phase"] == "snapshot"
        assert "commit failed" in state["stderr"]

    @pytest.mark.asyncio
    async def test_verify_all_stories_awaits_timeout_cleanup(self, tmp_path, monkeypatch):
        from otto.certifier.journey_agent import verify_all_stories
        from otto.certifier.stories import StoryStep, UserStory

        cleanup = asyncio.Event()

        async def fake_verify_story(*args, **kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                cleanup.set()

        monkeypatch.setattr("otto.certifier.journey_agent.verify_story", fake_verify_story)

        story = UserStory(
            id="story-1",
            persona="user",
            title="Times out",
            narrative="test",
            steps=[StoryStep(action="wait", verify="eventually")],
            critical=True,
        )

        result = await verify_all_stories(
            stories=[story],
            manifest=SimpleNamespace(),
            base_url="http://localhost:3000",
            project_dir=tmp_path,
            config={
                "certifier_story_timeout_base": 0.01,
                "certifier_story_timeout_per_step": 0,
                "certifier_story_timeout_break": 0,
            },
        )

        assert result.results[0].passed is False
        assert cleanup.is_set()

    @pytest.mark.asyncio
    async def test_continuous_fix_loop(self, tmp_git_repo):
        """Agent builds, certifier fails, agent fixes, certifier passes."""
        from otto.pipeline import build_continuous

        call_count = 0

        async def fake_query(prompt, options, **kwargs):
            nonlocal call_count
            call_count += 1
            return f"response {call_count}", 0.3, SimpleNamespace(
                session_id="s1", structured_output={"status": "ready_for_review", "summary": f"round {call_count}"},
            )

        certifier_calls = []

        def fake_certifier(**kwargs):
            certifier_calls.append(1)
            if len(certifier_calls) == 1:
                return CertificationReport(
                    product_type="web", interaction="http",
                    findings=[Finding(tier=4, severity="critical", category="journey",
                                      description="XSS found", diagnosis="unescaped HTML",
                                      fix_suggestion="escape it")],
                    outcome=CertificationOutcome.FAILED,
                    cost_usd=1.50, duration_s=300.0,
                )
            return CertificationReport(
                product_type="web", interaction="http",
                outcome=CertificationOutcome.PASSED,
                cost_usd=1.00, duration_s=200.0,
            )

        with patch("otto.session.run_agent_query", side_effect=fake_query), \
             patch("otto.session.agent_provider", return_value="claude"), \
             patch("otto.certifier.isolated.certify_with_retry", side_effect=fake_certifier), \
             patch("otto.pipeline._snapshot_candidate", return_value="abc1234"), \
             patch("otto.pipeline._commit_artifacts"), \
             patch("otto.git_ops._snapshot_untracked", return_value=set()), \
             patch("otto.git_ops.check_clean_tree", return_value=True):
            result = await build_continuous(
                "Build a todo app", tmp_git_repo, {"default_branch": "main"},
            )

        assert result.passed is True
        assert result.rounds == 2
        assert call_count == 2  # build + fix
        assert len(certifier_calls) == 2
        assert result.total_cost == pytest.approx(3.10)

    @pytest.mark.asyncio
    async def test_resume_from_checkpoint_continues_from_last_state(self, tmp_git_repo):
        from otto.pipeline import resume_continuous

        checkpoint_dir = tmp_git_repo / "otto_logs" / "builds" / "build-123"
        checkpoint_dir.mkdir(parents=True)
        SessionCheckpoint(
            session_id="s1",
            base_sha="base123",
            round=0,
            verification_round=1,
            state="certified",
            certifier_outcome="failed",
            candidate_sha="abc1234",
            intent="Build a todo app",
            last_status="ready_for_review",
            last_summary="built the product",
            findings=[{
                "severity": "critical",
                "category": "journey",
                "description": "XSS found",
                "diagnosis": "unescaped HTML",
                "fix_suggestion": "escape it",
                "story_id": "story-1",
            }],
            cost_so_far=1.8,
            agent_cost_so_far=0.3,
            certifier_cost_so_far=1.5,
        ).save(checkpoint_dir / "checkpoint.json")

        async def fake_query(prompt, options, **kwargs):
            return "fixed the bugs", 0.2, SimpleNamespace(
                session_id="s1",
                structured_output={"status": "ready_for_review", "summary": "fixed"},
            )

        passing_report = CertificationReport(
            product_type="web", interaction="http",
            outcome=CertificationOutcome.PASSED,
            cost_usd=1.00, duration_s=200.0,
        )

        with patch("otto.session.run_agent_query", side_effect=fake_query), \
             patch("otto.session.agent_provider", return_value="claude"), \
             patch("otto.certifier.isolated.certify_with_retry", return_value=passing_report), \
             patch("otto.pipeline._snapshot_candidate", return_value="def5678"):
            result = await resume_continuous(
                checkpoint_dir / "checkpoint.json",
                tmp_git_repo,
                {"default_branch": "main"},
            )

        assert result.passed is True
        assert result.rounds == 2
        assert result.total_cost == pytest.approx(3.0)
