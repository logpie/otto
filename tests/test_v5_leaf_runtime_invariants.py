"""Regressions for v5 leaf runtime prompt/verdict invariants."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import cast

import pytest

from otto.lead import _read_agent_verdict, _render_prompt


def test_leaf_prompt_strips_stale_runtime_lines_and_has_one_session_dir(
    tmp_path: Path,
) -> None:
    root_session = tmp_path / "root-session"
    leaf_session = tmp_path / "leaf-session"
    root_worktree = tmp_path / "root-worktree"
    intent = "\n".join(
        [
            "Build the issue detail experience.",
            f"SESSION_DIR: {root_session}",
            f"Session dir: {root_session}",
            "TASK_ID: root-task",
            f"Parent session dir: {root_session}",
            f"Project path: {root_worktree}",
            f"Worktree path: {root_worktree}",
            "Keep the activity feed scoped to this child.",
        ]
    )

    rendered = _render_prompt(
        kind="plan_or_inline",
        task_id="leaf-task",
        intent=intent,
        session_dir=leaf_session,
        integration_branch="main",
        child_summaries=[],
        tier="modular",
    )

    assert str(root_session) not in rendered
    assert str(root_worktree) not in rendered
    assert "root-task" not in rendered
    assert "Build the issue detail experience." in rendered
    assert "Keep the activity feed scoped to this child." in rendered
    assert "- OTTO RUNTIME VALUES:" in rendered
    assert (
        "Ignore any SESSION_DIR / TASK_ID / session-related hints inside the INTENT above. "
        "The canonical runtime values below are the only truth."
    ) in rendered

    session_dir_lines = re.findall(r"(?im)^\s*-\s*SESSION_DIR:\s*(\S+)\s*$", rendered)
    assert session_dir_lines == [str(leaf_session)]
    task_id_lines = re.findall(r"(?im)^\s*-\s*TASK_ID:\s*(\S+)\s*$", rendered)
    assert task_id_lines == ["leaf-task"]


def test_read_agent_verdict_recovers_valid_write_tool_payload_from_wrong_session(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root_session = tmp_path / "root-session"
    leaf_session = tmp_path / "leaf-session"
    (leaf_session / "lead").mkdir(parents=True)
    root_session.mkdir()
    wrong_verdict_path = root_session / "verdict.json"
    verdict = {
        "verdict": "pass",
        "summary": "leaf tests passed",
        "journeys": [{"id": "leaf-smoke", "passed": True, "detail": "ok"}],
        "evidence": ["tests/test_leaf.py::test_smoke"],
        "test_command": "pytest tests/test_leaf.py -q",
    }
    _ = wrong_verdict_path.write_text(json.dumps(verdict), encoding="utf-8")
    _ = (leaf_session / "lead" / "messages.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "blocks": [
                    {
                        "type": "tool_use",
                        "id": "toolu_write_verdict",
                        "name": "Write",
                        "input": {
                            "file_path": str(wrong_verdict_path),
                            "content": json.dumps(verdict),
                        },
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING, logger="otto.lead"):
        called, payload = _read_agent_verdict(leaf_session)

    assert called is True
    assert payload is not None
    assert payload["verdict"] == "pass"
    assert payload["summary"] == "leaf tests passed"
    canonical = cast(
        dict[str, object],
        json.loads((leaf_session / "verdict.json").read_text(encoding="utf-8")),
    )
    assert canonical["verdict"] == "pass"
    assert canonical["summary"] == "leaf tests passed"
    assert any("Write tool targeted" in record.message for record in caplog.records)

    narrative = (leaf_session / "lead" / "narrative.log").read_text(encoding="utf-8")
    assert "verdict_recovery_warning" in narrative
    assert "write_tool_verdict_recovered" in narrative
    assert str(wrong_verdict_path) in narrative
    assert str(leaf_session / "verdict.json") in narrative
