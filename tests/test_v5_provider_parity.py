from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from otto.lead import run_lead


@pytest.mark.asyncio
async def test_lead_recovers_verdict_from_result_record_inline_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_dir = tmp_path / "otto_logs" / "sessions" / "lead-inline"
    verdict_payload = {
        "verdict": "pass",
        "summary": "Codex emitted the verdict inline instead of writing a file.",
        "journeys": [{"id": "smoke", "passed": True}],
        "intent_coverage": {"built": ["smoke"], "partial": [], "skipped": []},
    }

    def fake_options(
        _project_dir: Path,
        _config: dict[str, Any],
        *,
        agent_type: str | None = None,
    ) -> SimpleNamespace:
        assert agent_type == "build"
        return SimpleNamespace(max_turns=1, cwd="", mcp_servers={})

    async def fake_run_agent_with_timeout(*_args: Any, **kwargs: Any) -> tuple[str, float, str, dict[str, Any]]:
        log_dir = Path(kwargs["log_dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "messages.jsonl").write_text(
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "result": "Final verdict:\n" + json.dumps(verdict_payload),
                    "session_id": "codex-thread-1",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return "inline final text", 0.25, "codex-thread-1", {}

    monkeypatch.setattr("otto.agent.make_agent_options", fake_options)
    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_run_agent_with_timeout)
    monkeypatch.setattr("otto.mcp_tools.create_otto_mcp_server", lambda **_kwargs: object())
    monkeypatch.setattr(
        "otto.v5_verification_plan.validate_lead_verdict",
        lambda **_kwargs: SimpleNamespace(final_verdict="pass", runner_checks_summary=[]),
    )

    result = await run_lead(
        task_id="v5-provider-parity",
        intent="Build a smoke feature",
        project_dir=tmp_path,
        session_dir=session_dir,
        integration_branch="main",
        config={},
    )

    assert result.verdict == "pass"
    assert result.verify_called is True
    assert result.verify_result is not None
    assert result.verify_result["summary"].startswith("Codex emitted")
    recovered = json.loads((session_dir / "verdict.json").read_text(encoding="utf-8"))
    assert recovered["verdict"] == "pass"
