from __future__ import annotations

import json
from pathlib import Path

import pytest

from otto import paths
from otto.pipeline import build_agentic_v3

from .conftest import assistant_text


@pytest.mark.asyncio
async def test_agent_mode_checkpoint_persists_intent_and_cli_resume_rejects_mismatch(
    tmp_otto_repo: Path,
    mock_sdk,
    cli_in_repo,
) -> None:
    async def interrupted_stream(*, prompt: str, options):
        yield assistant_text("Started the build and created initial files.", session_id="resume-sdk")
        raise RuntimeError("synthetic interruption after partial progress")

    mock_sdk.install(interrupted_stream)

    result = await build_agentic_v3(
        "original integration intent",
        tmp_otto_repo,
        {},
        certifier_mode="fast",
    )

    checkpoint_path = paths.session_checkpoint(tmp_otto_repo, result.build_id)
    checkpoint = json.loads(checkpoint_path.read_text())

    assert result.passed is False
    assert checkpoint["status"] == "paused"
    assert checkpoint["intent"] == "original integration intent"
    assert checkpoint["agent_session_id"] == "resume-sdk"

    resume_result = cli_in_repo(
        tmp_otto_repo,
        ["build", "different resume intent", "--resume"],
    )

    assert resume_result.exit_code == 2
    assert "Intent mismatch on resume" in resume_result.output
