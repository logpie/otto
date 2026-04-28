from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from otto import paths
from otto.queue.schema import QueueTask, append_task, write_state as write_queue_state
from otto.runs.registry import make_run_record, write_record
from otto.web.app import create_app

from tests._helpers import init_repo


def _init_repo(repo: Path) -> None:
    repo.parent.mkdir(parents=True, exist_ok=True)
    init_repo(
        repo,
        subdir=None,
        commit_file="README.md",
        commit_content="# web\n",
        commit_msg="initial",
    )


def _app(project_dir: Path, **kwargs: Any) -> FastAPI:
    return create_app(project_dir, **kwargs)


def _client(project_dir: Path, **kwargs: Any) -> TestClient:
    return _client_for_app(_app(project_dir, **kwargs))


def _client_for_app(app: FastAPI) -> TestClient:
    return TestClient(app)


def _set_origin_head(repo: Path, branch: str) -> None:
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "update-ref", f"refs/remotes/origin/{branch}", sha], cwd=repo, check=True)
    subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}"],
        cwd=repo,
        check=True,
    )


def _create_branch_file(repo: Path, branch: str, filename: str = "feature.txt", content: str = "ready\n") -> None:
    subprocess.run(["git", "checkout", "-q", "-b", branch], cwd=repo, check=True)
    (repo / filename).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"add {filename}"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)


def _queue_task(
    task_id: str,
    *,
    command_argv: list[str] | None = None,
    added_at: str = "2026-04-24T00:00:00Z",
    resolved_intent: str | None = None,
    branch: str | None = None,
    worktree: str | None = None,
    resumable: bool = False,
) -> QueueTask:
    summary = resolved_intent or task_id.replace("-", " ")
    return QueueTask(
        id=task_id,
        command_argv=command_argv or ["build", summary],
        added_at=added_at,
        resolved_intent=summary,
        branch=branch or f"build/{task_id}",
        worktree=worktree or f".worktrees/{task_id}",
        resumable=resumable,
    )


def _append_queue_task(repo: Path, task_id: str, **kwargs: Any) -> QueueTask:
    task = _queue_task(task_id, **kwargs)
    append_task(repo, task)
    return task


def _write_empty_queue_state(repo: Path) -> None:
    write_queue_state(repo, {"schema_version": 1, "watcher": None, "tasks": {}})


def _write_run(
    repo: Path,
    *,
    run_id: str = "build-web",
    outside_artifact: str | None = None,
    branch: str = "main",
    intent_summary: str = "build the web surface",
    status: str = "running",
) -> None:
    primary_log = paths.build_dir(repo, run_id) / "narrative.log"
    primary_log.parent.mkdir(parents=True, exist_ok=True)
    primary_log.write_text("BUILD starting\nSTORY_RESULT: web PASS\n", encoding="utf-8")
    summary_path = paths.session_summary(repo, run_id)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps({"verdict": "passed"}), encoding="utf-8")
    record = make_run_record(
        project_dir=repo,
        run_id=run_id,
        domain="atomic",
        run_type="build",
        command="build",
        display_name="build web",
        status=status,
        cwd=repo,
        source={
            "argv": ["build", "web"],
            "provider": "codex",
            "model": "gpt-5.4",
            "reasoning_effort": "medium",
        },
        git={"branch": branch, "worktree": None},
        intent={"summary": intent_summary},
        artifacts={
            "summary_path": outside_artifact or str(summary_path),
            "primary_log_path": str(primary_log),
        },
        metrics={
            "cost_usd": 0.0,
            "input_tokens": 1234,
            "cached_input_tokens": 1000,
            "output_tokens": 56,
        },
        adapter_key="atomic.build",
        last_event="running tests",
    )
    write_record(repo, record)
