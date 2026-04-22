from __future__ import annotations

import json
from pathlib import Path

from otto import paths

from .conftest import commit_all, fast_pass_markers, assistant_text, result_message


def test_improve_bugs_cli_writes_report_and_manifest(
    tmp_otto_repo: Path,
    mock_sdk,
    cli_in_repo,
) -> None:
    (tmp_otto_repo / "intent.md").write_text("bookmark manager\n")
    commit_all(tmp_otto_repo, "add intent")

    mock_sdk.install_messages([
        assistant_text(
            "Found and fixed the focused bug set.\n\n"
            + fast_pass_markers(
                round_number=1,
                story_id="error-handling",
                summary="error handling now passes",
            )
        ),
        result_message(total_cost_usd=0.28),
    ])

    result = cli_in_repo(
        tmp_otto_repo,
        ["improve", "bugs", "error handling", "--fast"],
    )

    latest_session = paths.resolve_pointer(tmp_otto_repo, paths.LATEST_POINTER)
    assert result.exit_code == 0, result.output
    assert latest_session is not None

    report_path = latest_session / "improve" / "improvement-report.md"
    manifest_path = latest_session / "manifest.json"
    manifest = json.loads(manifest_path.read_text())

    assert report_path.exists()
    assert "error handling" in report_path.read_text()
    assert manifest["command"] == "improve"
    assert manifest["focus"] == "error handling"
    assert str(manifest["branch"]).startswith("improve/")


def test_certify_cli_reports_malformed_agent_output(
    tmp_otto_repo: Path,
    mock_sdk,
    cli_in_repo,
) -> None:
    (tmp_otto_repo / "intent.md").write_text("cli calculator\n")
    commit_all(tmp_otto_repo, "add certify intent")

    mock_sdk.install_messages([
        assistant_text("I checked the project and everything seems okay."),
        result_message(total_cost_usd=0.14),
    ])

    result = cli_in_repo(
        tmp_otto_repo,
        ["certify", "--fast"],
    )

    latest_session = paths.resolve_pointer(tmp_otto_repo, paths.LATEST_POINTER)
    assert result.exit_code == 1
    assert "Certifier produced no structured output" in result.output
    assert latest_session is not None
    assert (latest_session / "certify" / "narrative.log").exists()
