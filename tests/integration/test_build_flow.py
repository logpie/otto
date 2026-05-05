from __future__ import annotations

import json
from pathlib import Path

from otto import paths

from .conftest import (
    assistant_text,
    fast_pass_markers,
    result_message,
)


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
        ["build", "ship queue manifest mirror", "--agentic", "--fast"],
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
