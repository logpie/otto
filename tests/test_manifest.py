"""Tests for otto/manifest.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from otto.manifest import (
    QUEUE_PROJECT_DIR_ENV,
    QUEUE_TASK_ENV,
    current_head_sha,
    make_manifest,
    manifest_path_for,
    now_iso,
    queue_index_path_for,
    write_manifest,
)
from tests._helpers import init_repo


def test_manifest_path_always_uses_session_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fallback = tmp_path / "otto_logs" / "sessions" / "run-1"
    monkeypatch.setenv(QUEUE_TASK_ENV, "from-env")
    path = manifest_path_for(
        project_dir=tmp_path,
        fallback_dir=fallback,
        queue_task_id="explicit-task",
    )
    assert path == fallback / "manifest.json"


def test_queue_index_path_for_uses_queue_namespace(tmp_path: Path):
    assert queue_index_path_for(tmp_path, "add-csv-export") == (
        tmp_path / "otto_logs" / "queue" / "add-csv-export" / "manifest.json"
    )


def test_queue_index_path_for_returns_none_without_task_id(tmp_path: Path):
    assert queue_index_path_for(tmp_path, None) is None


@pytest.mark.parametrize("bad_value", ["../", "foo/bar", "..", "UPPER", "foo..bar"])
def test_queue_index_path_for_rejects_invalid_task_ids(tmp_path: Path, bad_value: str):
    with pytest.raises(ValueError, match=QUEUE_TASK_ENV):
        queue_index_path_for(tmp_path, bad_value)


def test_write_manifest_creates_canonical_file_only_for_atomic_run(tmp_path: Path):
    fallback = tmp_path / "otto_logs" / "sessions" / "build-1"
    manifest = make_manifest(
        command="build",
        argv=["build", "test"],
        run_id="build-1",
        branch="build/test-2026-04-19",
        checkpoint_path=fallback / "checkpoint.json",
        proof_of_work_path=fallback / "certify" / "proof-of-work.json",
        cost_usd=1.23,
        duration_s=45.6,
        started_at=now_iso(),
        head_sha="abc123",
        resolved_intent="test",
    )

    out = write_manifest(manifest, project_dir=tmp_path, fallback_dir=fallback)

    assert out == fallback / "manifest.json"
    assert out.exists()
    assert not (tmp_path / "otto_logs" / "queue").exists()
    data = json.loads(out.read_text())
    assert data["command"] == "build"
    assert data["run_id"] == "build-1"
    assert data["queue_task_id"] is None
    assert "mirror_of" not in data


def test_write_manifest_queue_mode_writes_canonical_and_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    session_dir = tmp_path / "worktree" / "otto_logs" / "sessions" / "build-1"
    anchor_repo = tmp_path / "anchor"
    monkeypatch.setenv(QUEUE_TASK_ENV, "csv-export")
    monkeypatch.setenv(QUEUE_PROJECT_DIR_ENV, str(anchor_repo))
    manifest = make_manifest(
        command="build",
        argv=["build", "test"],
        run_id="build-1",
        branch="build/test",
        checkpoint_path=session_dir / "checkpoint.json",
        proof_of_work_path=session_dir / "certify" / "proof-of-work.json",
        cost_usd=0.5,
        duration_s=1.0,
        started_at=now_iso(),
        head_sha=None,
        resolved_intent="t",
    )

    canonical = write_manifest(
        manifest,
        project_dir=tmp_path / "worktree",
        fallback_dir=session_dir,
    )

    mirror = anchor_repo / "otto_logs" / "queue" / "csv-export" / "manifest.json"
    assert canonical == session_dir / "manifest.json"
    assert canonical.exists()
    assert mirror.exists()

    canonical_data = json.loads(canonical.read_text())
    mirror_data = json.loads(mirror.read_text())
    assert canonical_data["queue_task_id"] == "csv-export"
    assert "mirror_of" not in canonical_data
    assert mirror_data["queue_task_id"] == "csv-export"
    assert mirror_data["mirror_of"] == str(canonical.resolve())
    assert {k: v for k, v in mirror_data.items() if k != "mirror_of"} == canonical_data


def test_write_manifest_atomic_via_rename_for_canonical_and_queue_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    fallback = tmp_path / "otto_logs" / "sessions" / "cert-1"
    monkeypatch.setenv(QUEUE_TASK_ENV, "queue-task")
    manifest = make_manifest(
        command="certify",
        argv=["certify"],
        run_id="cert-1",
        queue_task_id="queue-task",
        branch=None,
        checkpoint_path=None,
        proof_of_work_path=None,
        cost_usd=0,
        duration_s=0,
        started_at=now_iso(),
        head_sha=None,
        resolved_intent=None,
    )

    out = write_manifest(manifest, project_dir=tmp_path, fallback_dir=fallback)

    mirror = tmp_path / "otto_logs" / "queue" / "queue-task" / "manifest.json"
    assert list(out.parent.glob("*.tmp")) == []
    assert list(mirror.parent.glob("*.tmp")) == []


def test_current_head_sha_returns_real_sha(tmp_path: Path):
    repo = init_repo(tmp_path)
    sha = current_head_sha(repo)
    assert sha is not None
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_current_head_sha_returns_none_for_non_git(tmp_path: Path):
    assert current_head_sha(tmp_path) is None
