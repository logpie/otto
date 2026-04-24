from __future__ import annotations

import json
from pathlib import Path

import pytest

from otto import paths
from otto.agent import AgentOptions, run_agent_query
from otto.markers import parse_certifier_markers

from .conftest import (
    assistant_text,
    assistant_tool_result,
    assistant_tool_use,
    fast_pass_markers,
    result_message,
    thorough_pass_markers,
)


@pytest.mark.asyncio
async def test_build_agentic_v3_dedupes_repeated_certify_round_markers(
    tmp_otto_repo: Path,
    mock_sdk,
) -> None:
    round_one = thorough_pass_markers(
        round_number=1,
        story_id="smoke",
        summary="first round passes",
    )
    round_two = thorough_pass_markers(
        round_number=2,
        story_id="smoke",
        summary="second round passes",
    )
    final_summary = (
        "Build complete. Re-reporting the final certification summary below.\n\n"
        f"{round_one}\n{round_two}"
    )
    mock_sdk.install_messages([
        assistant_tool_use("Agent", {"prompt": "Run the certifier now."}, tool_id="agent-1"),
        assistant_tool_result(f"{round_one}\n{round_two}", tool_id="agent-1"),
        assistant_text(final_summary),
        result_message(
            structured_output={"verdict": "PASS", "certify_rounds": [1, 2]},
            total_cost_usd=0.31,
        ),
    ])

    text, _cost, _result = await run_agent_query(
        "Build and certify this product.",
        AgentOptions(cwd=str(tmp_otto_repo), provider="claude"),
        capture_tool_output=False,
    )
    parsed = parse_certifier_markers(text, certifier_mode="thorough")

    assert [round_data["round"] for round_data in parsed.certify_rounds] == [1, 2]
    assert parsed.verdict_pass is True


def test_build_cli_writes_canonical_and_queue_manifest_mirror(
    tmp_otto_repo: Path,
    mock_sdk,
    cli_in_repo,
) -> None:
    mock_sdk.install_messages([
        assistant_text(
            "Built the feature and verified the happy path.\n\n"
            + fast_pass_markers(
                round_number=1,
                story_id="queue-mirror",
                summary="queue-backed build passed",
            )
        ),
        result_message(total_cost_usd=0.22),
    ])

    env = {
        "OTTO_QUEUE_TASK_ID": "build-integration",
        "OTTO_QUEUE_PROJECT_DIR": str(tmp_otto_repo),
    }
    result = cli_in_repo(
        tmp_otto_repo,
        ["build", "ship queue manifest mirror", "--fast"],
        env=env,
    )

    latest_session = paths.resolve_pointer(tmp_otto_repo, paths.LATEST_POINTER)
    assert result.exit_code == 0, result.output
    assert latest_session is not None

    canonical_manifest = latest_session / "manifest.json"
    mirror_manifest = (
        tmp_otto_repo / "otto_logs" / "queue" / "build-integration" / "manifest.json"
    )
    canonical = json.loads(canonical_manifest.read_text())
    mirror = json.loads(mirror_manifest.read_text())

    assert canonical["command"] == "build"
    assert canonical["queue_task_id"] == "build-integration"
    assert canonical["resolved_intent"] == "ship queue manifest mirror"
    assert mirror["mirror_of"] == str(canonical_manifest.resolve())
    assert mirror["run_id"] == canonical["run_id"]
