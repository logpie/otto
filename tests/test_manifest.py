"""Tests for otto/manifest.py — Phase 1.4 manifest contract."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from otto.manifest import (
    QUEUE_TASK_ENV,
    Manifest,
    current_head_sha,
    make_manifest,
    manifest_path_for,
    now_iso,
    write_manifest,
)


# ---------- manifest_path_for ----------


def test_path_atomic_mode_uses_fallback(tmp_path: Path):
    fallback = tmp_path / "otto_logs" / "builds" / "build-1"
    p = manifest_path_for(
        project_dir=tmp_path,
        fallback_dir=fallback,
        queue_task_id=None,
    )
    assert p == fallback / "manifest.json"


def test_path_queue_mode_uses_deterministic_path(tmp_path: Path):
    fallback = tmp_path / "otto_logs" / "builds" / "build-1"
    p = manifest_path_for(
        project_dir=tmp_path,
        fallback_dir=fallback,
        queue_task_id="add-csv-export",
    )
    assert p == tmp_path / "otto_logs" / "queue" / "add-csv-export" / "manifest.json"


def test_path_queue_mode_reads_env_var_by_default(tmp_path: Path, monkeypatch):
    fallback = tmp_path / "otto_logs" / "builds" / "x"
    monkeypatch.setenv(QUEUE_TASK_ENV, "from-env")
    p = manifest_path_for(project_dir=tmp_path, fallback_dir=fallback)
    assert p == tmp_path / "otto_logs" / "queue" / "from-env" / "manifest.json"


def test_path_queue_mode_explicit_overrides_env(tmp_path: Path, monkeypatch):
    fallback = tmp_path / "otto_logs" / "builds" / "x"
    monkeypatch.setenv(QUEUE_TASK_ENV, "from-env")
    p = manifest_path_for(
        project_dir=tmp_path,
        fallback_dir=fallback,
        queue_task_id="explicit-task",
    )
    assert p == tmp_path / "otto_logs" / "queue" / "explicit-task" / "manifest.json"


@pytest.mark.parametrize("bad_value", ["../", "foo/bar", "..", "", "UPPER", "foo..bar"])
def test_path_queue_mode_rejects_invalid_task_ids(tmp_path: Path, monkeypatch, bad_value: str):
    fallback = tmp_path / "otto_logs" / "builds" / "x"
    monkeypatch.setenv(QUEUE_TASK_ENV, bad_value)

    with pytest.raises(ValueError, match=QUEUE_TASK_ENV):
        manifest_path_for(project_dir=tmp_path, fallback_dir=fallback)


# ---------- write_manifest ----------


def test_write_manifest_creates_file_atomic_mode(tmp_path: Path):
    fallback = tmp_path / "otto_logs" / "builds" / "build-1"
    m = make_manifest(
        command="build", argv=["build", "test"], run_id="build-1",
        branch="build/test-2026-04-19",
        checkpoint_path=fallback / "checkpoint.json",
        proof_of_work_path=tmp_path / "otto_logs" / "certifier" / "proof-of-work.json",
        cost_usd=1.23, duration_s=45.6,
        started_at=now_iso(), head_sha="abc123",
        resolved_intent="test",
    )
    out = write_manifest(m, project_dir=tmp_path, fallback_dir=fallback)
    assert out == fallback / "manifest.json"
    assert out.exists()
    data = json.loads(out.read_text())
    assert data["command"] == "build"
    assert data["argv"] == ["build", "test"]
    assert data["run_id"] == "build-1"
    assert data["branch"] == "build/test-2026-04-19"
    assert data["cost_usd"] == 1.23
    assert data["queue_task_id"] is None
    assert data["exit_status"] == "success"
    assert data["schema_version"] == 1


def test_write_manifest_queue_mode_via_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(QUEUE_TASK_ENV, "csv-export")
    fallback = tmp_path / "otto_logs" / "builds" / "build-1"
    m = make_manifest(
        command="build", argv=["build", "test"], run_id="build-1",
        branch="build/test", checkpoint_path=None, proof_of_work_path=None,
        cost_usd=0, duration_s=1, started_at=now_iso(), head_sha=None, resolved_intent="t",
    )
    out = write_manifest(m, project_dir=tmp_path, fallback_dir=fallback)
    assert out == tmp_path / "otto_logs" / "queue" / "csv-export" / "manifest.json"
    data = json.loads(out.read_text())
    assert data["queue_task_id"] == "csv-export"


def test_write_manifest_failure_exit_status(tmp_path: Path):
    fallback = tmp_path / "otto_logs" / "builds" / "build-1"
    m = make_manifest(
        command="build", argv=["build", "test"], run_id="build-1",
        branch=None, checkpoint_path=None, proof_of_work_path=None,
        cost_usd=0, duration_s=0, started_at=now_iso(), head_sha=None, resolved_intent=None,
        exit_status="failure",
    )
    out = write_manifest(m, project_dir=tmp_path, fallback_dir=fallback)
    data = json.loads(out.read_text())
    assert data["exit_status"] == "failure"


def test_write_manifest_atomic_via_rename(tmp_path: Path):
    """Smoke test: writing creates no leftover .tmp file."""
    fallback = tmp_path / "otto_logs" / "x" / "y"
    m = make_manifest(
        command="certify", argv=["certify"], run_id="cert-1",
        branch=None, checkpoint_path=None, proof_of_work_path=None,
        cost_usd=0, duration_s=0, started_at=now_iso(), head_sha=None, resolved_intent=None,
    )
    out = write_manifest(m, project_dir=tmp_path, fallback_dir=fallback)
    tmp_files = list(out.parent.glob("*.tmp"))
    assert tmp_files == [], f"leftover tmp files: {tmp_files}"


# ---------- current_head_sha ----------


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=repo, check=True)
    return repo


def test_current_head_sha_returns_real_sha(tmp_path: Path):
    repo = _init_repo(tmp_path)
    sha = current_head_sha(repo)
    assert sha is not None
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_current_head_sha_returns_none_for_non_git(tmp_path: Path):
    sha = current_head_sha(tmp_path)
    assert sha is None
