"""Tests for agent-driven build infrastructure: session, feedback, isolated certifier."""

import json
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


class TestAgentDrivenBuild:
    @pytest.mark.asyncio
    async def test_variant_b_passes_first_round(self, tmp_git_repo):
        """Agent builds, certifier passes on round 1."""
        from otto.pipeline import build_agent_driven

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
             patch("otto.pipeline._commit_artifacts"):
            result = await build_agent_driven(
                "Build a todo app", tmp_git_repo, {"default_branch": "main"},
            )

        assert result.passed is True
        assert result.rounds == 1

    @pytest.mark.asyncio
    async def test_variant_b_fix_loop(self, tmp_git_repo):
        """Agent builds, certifier fails, agent fixes, certifier passes."""
        from otto.pipeline import build_agent_driven

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
             patch("otto.pipeline._commit_artifacts"):
            result = await build_agent_driven(
                "Build a todo app", tmp_git_repo, {"default_branch": "main"},
            )

        assert result.passed is True
        assert result.rounds == 2
        assert call_count == 2  # build + fix
        assert len(certifier_calls) == 2
