from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from tests._helpers import init_repo


FAKE_OTTO = Path(__file__).resolve().parents[1] / "scripts" / "fake-otto.sh"


def test_fake_otto_no_commit_suppresses_git_commit(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    result = subprocess.run(
        [str(FAKE_OTTO), "build", "no commit please"],
        cwd=repo,
        env={**os.environ, "FAKE_OTTO_NO_COMMIT": "1"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert after == before
    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert status == ""


def test_fake_otto_manifest_escapes_json_strings(tmp_path: Path) -> None:
    repo = init_repo(tmp_path)
    intent = 'quote "and" newline\nplus slash \\'

    result = subprocess.run(
        [str(FAKE_OTTO), "build", intent],
        cwd=repo,
        env={
            **os.environ,
            "FAKE_OTTO_NO_COMMIT": "1",
            "OTTO_QUEUE_TASK_ID": "task-1",
            "OTTO_QUEUE_PROJECT_DIR": str(repo),
        },
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads((repo / "otto_logs" / "queue" / "task-1" / "manifest.json").read_text())
    assert manifest["command"] == "build"
    assert manifest["queue_task_id"] == "task-1"
    assert manifest["resolved_intent"] == intent
