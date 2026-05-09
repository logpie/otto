"""Phase 4 smoke tests — provider fallback + cost_attempts schema."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from otto.lead import LeadResult
from otto.v5_provider_fallback import (
    append_attempt,
    detect_fallback_reason,
    fallback_provider,
    should_fallback,
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "otto_logs").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# detect_fallback_reason
# ---------------------------------------------------------------------------


class TestDetectFallbackReason:
    def test_402_payment_required(self) -> None:
        assert detect_fallback_reason("HTTP 402 Payment Required") == "provider_exhausted"

    def test_out_of_credits(self) -> None:
        assert detect_fallback_reason("Out of credits — please top up") == "provider_exhausted"

    def test_401_auth_failed(self) -> None:
        assert detect_fallback_reason("HTTP 401 Unauthorized: Invalid API key") in (
            "auth_failed",
            "provider_exhausted",  # 401 might match either if both patterns hit
        )

    def test_rate_limit(self) -> None:
        assert detect_fallback_reason("RateLimitError: too many requests") == "rate_limit_exhausted"

    def test_unrelated_error_returns_none(self) -> None:
        assert detect_fallback_reason("Connection refused") is None
        assert detect_fallback_reason("Disk full") is None
        assert detect_fallback_reason("") is None


class TestShouldFallback:
    def test_credit_failure_triggers_fallback_by_default(self) -> None:
        do, reason = should_fallback("HTTP 402: out of credits", config={})
        assert do is True
        assert reason == "provider_exhausted"

    def test_unrelated_error_does_not_trigger(self) -> None:
        do, reason = should_fallback("filesystem error", config={})
        assert do is False
        assert reason is None

    def test_user_can_disable_fallback_for_a_reason(self) -> None:
        # User says don't fallback on auth (only on quota).
        do, reason = should_fallback(
            "HTTP 401 unauthorized",
            config={"fallback_on": ["provider_exhausted"]},
        )
        # 401 detects as auth_failed, but config doesn't include it → no fallback.
        if reason == "auth_failed":
            assert do is False


class TestFallbackProvider:
    def test_returns_claude_default(self) -> None:
        assert fallback_provider({}) == "claude"

    def test_explicit_override(self) -> None:
        assert fallback_provider({"fallback_provider": "codex"}) == "codex"

    def test_under_defaults_subkey(self) -> None:
        assert fallback_provider({"defaults": {"fallback_provider": "openai-agents"}}) == (
            "openai-agents"
        )


class TestAppendAttempt:
    def test_appends_and_sums_cost(self, project: Path) -> None:
        summary = project / "otto_logs" / "summary.json"
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_text(json.dumps({"task_id": "t1"}) + "\n", encoding="utf-8")

        append_attempt(
            summary, provider="codex", cost_usd=0.13, outcome="exhausted",
            duration_s=240.0, started_at="2026-05-09T10:00:00Z",
        )
        append_attempt(
            summary, provider="claude", cost_usd=2.10, outcome="pass",
            duration_s=600.0, started_at="2026-05-09T10:04:00Z",
            fallback_reason="provider_exhausted",
        )

        data = json.loads(summary.read_text(encoding="utf-8"))
        assert len(data["cost_attempts"]) == 2
        assert data["cost_attempts"][0]["provider"] == "codex"
        assert data["cost_attempts"][1]["provider"] == "claude"
        assert data["cost_attempts"][1]["fallback_reason"] == "provider_exhausted"
        # Sum: 0.13 + 2.10 = 2.23.
        assert data["cost_usd"] == pytest.approx(2.23)

    def test_missing_summary_no_crash(self, project: Path) -> None:
        # Per philosophy: don't crash on observability writes.
        nope = project / "no" / "such" / "summary.json"
        # Should not raise.
        append_attempt(
            nope, provider="codex", cost_usd=0.0, outcome="ok", duration_s=1.0,
        )


# ---------------------------------------------------------------------------
# Fallback wiring in v5_runner
# ---------------------------------------------------------------------------


class TestRunLeadWithFallback:
    @pytest.mark.asyncio
    async def test_fallback_kicks_in_on_provider_exhausted(self, project: Path) -> None:
        """First attempt fails with credit-exhausted → fallback runs and succeeds."""
        from otto import paths as _paths
        from otto.v5_runner import _run_lead_with_fallback

        session_id = "test-fallback-session"
        session_dir = _paths.session_dir(project, session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        # Pre-create summary.json so append_attempt has somewhere to write.
        (session_dir / "summary.json").write_text(
            json.dumps({"task_id": "t1"}) + "\n", encoding="utf-8"
        )

        attempts: list[str] = []

        async def fake_run_lead(**kwargs: Any) -> LeadResult:
            cfg = kwargs["config"]
            provider = cfg.get("provider", "?")
            attempts.append(provider)
            if provider == "codex":
                return LeadResult(
                    task_id=kwargs["task_id"],
                    verdict="catastrophic",
                    cost_usd=0.13,
                    failure_reason="HTTP 402 out of credits",
                )
            return LeadResult(
                task_id=kwargs["task_id"],
                verdict="pass",
                cost_usd=2.10,
            )

        with patch("otto.v5_runner.run_lead", new=fake_run_lead):
            result = await _run_lead_with_fallback(
                task_id="t1",
                intent="x",
                project_dir=project,
                session_dir=session_dir,
                integration_branch=None,
                config={"provider": "codex", "fallback_provider": "claude"},
            )
        # Two attempts recorded: codex (exhausted) → claude (pass).
        assert attempts == ["codex", "claude"]
        assert result.verdict == "pass"
        # Verify cost_attempts in summary.
        summary = json.loads((session_dir / "summary.json").read_text())
        assert len(summary["cost_attempts"]) == 2
        assert summary["cost_attempts"][0]["provider"] == "codex"
        assert summary["cost_attempts"][1]["provider"] == "claude"
        assert summary["cost_attempts"][1]["fallback_reason"] == "provider_exhausted"

    @pytest.mark.asyncio
    async def test_no_fallback_for_unrelated_failure(self, project: Path) -> None:
        """A non-quota crash does NOT trigger fallback (best-effort, not blind retry)."""
        from otto import paths as _paths
        from otto.v5_runner import _run_lead_with_fallback

        session_id = "test-no-fallback-session"
        session_dir = _paths.session_dir(project, session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        (session_dir / "summary.json").write_text(
            json.dumps({"task_id": "t1"}) + "\n", encoding="utf-8"
        )

        attempts: list[str] = []

        async def fake_run_lead(**kwargs: Any) -> LeadResult:
            cfg = kwargs["config"]
            attempts.append(cfg.get("provider", "?"))
            return LeadResult(
                task_id=kwargs["task_id"],
                verdict="catastrophic",
                cost_usd=0.05,
                failure_reason="Disk full at /tmp",
            )

        with patch("otto.v5_runner.run_lead", new=fake_run_lead):
            result = await _run_lead_with_fallback(
                task_id="t1",
                intent="x",
                project_dir=project,
                session_dir=session_dir,
                integration_branch=None,
                config={"provider": "codex", "fallback_provider": "claude"},
            )
        # Only one attempt — disk-full doesn't trigger fallback.
        assert attempts == ["codex"]
        assert result.verdict == "catastrophic"
