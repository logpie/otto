from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from otto.lead import run_lead


@pytest.mark.asyncio
async def test_run_lead_writes_and_appends_skipped_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir = tmp_path / "otto_logs" / "sessions" / "skip-session"

    def fake_options(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(max_turns=1, cwd="", mcp_servers={})

    async def fake_run_agent_with_timeout(*_args: Any, **kwargs: Any) -> tuple[str, float, str, dict[str, Any]]:
        log_dir = Path(kwargs["log_dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "verdict": "partial",
            "summary": "manual export remains",
            "intent_coverage": {
                "built": ["core flow"],
                "partial": [],
                "skipped": [
                    {"feature": "CSV export", "reason": "requires external storage"},
                ],
            },
        }
        (log_dir.parent / "verdict.json").write_text(
            json.dumps(payload),
            encoding="utf-8",
        )
        return "done", 0.1, "lead-thread", {}

    monkeypatch.setattr("otto.agent.make_agent_options", fake_options)
    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_run_agent_with_timeout)
    monkeypatch.setattr("otto.mcp_tools.create_otto_mcp_server", lambda **_kwargs: object())
    monkeypatch.setattr(
        "otto.v5_verification_plan.validate_lead_verdict",
        lambda **_kwargs: SimpleNamespace(final_verdict="partial", runner_checks_summary=[]),
    )

    for _ in range(2):
        result = await run_lead(
            task_id="v5-skip",
            intent="Build and report skipped items",
            project_dir=tmp_path,
            session_dir=session_dir,
            integration_branch="main",
            config={},
        )
        assert result.verdict == "partial"

    report = (session_dir / "skipped_report.md").read_text(encoding="utf-8")
    assert report.count("Manual follow-up required") == 2
    assert "task=v5-skip verdict=partial" in report
    assert "CSV export: requires external storage" in report
