"""Diff freshness contract tests (mc-audit Phase 4 cluster E).

Covers the CRITICAL diff-freshness finding plus the IMPORTANT truncation
clarity finding from ``codex-evidence-trustworthiness.md``:

  - GET ``/api/runs/{id}/diff`` enriches the response with target_sha,
    branch_sha, merge_base, fetched_at, command, limit_chars,
    full_size_chars, shown_hunks, total_hunks, errors.
  - Failures to resolve a ref do not break the response: the field is
    null and ``errors`` records what failed (so the UI can render a
    targeted warning).
  - POST ``/api/runs/{id}/actions/merge`` rejects with 409 when
    ``expected_target_sha`` doesn't match the current target HEAD; the
    happy path accepts a matching SHA. This is the safety hatch that
    prevents the "merged code differs from reviewed code" hunter found.

Run::

    uv run pytest tests/test_diff_freshness.py -v
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

from otto.queue.schema import QueueTask, append_task, write_state as write_queue_state
from otto.runs.registry import make_run_record, write_record
from otto.web.app import create_app


SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "diff@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Diff Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("# diff\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)


def _create_branch_with_change(
    repo: Path,
    branch: str,
    *,
    filename: str = "feature.txt",
    content: str = "ready\n",
) -> str:
    subprocess.run(["git", "checkout", "-q", "-b", branch], cwd=repo, check=True)
    (repo / filename).write_text(content, encoding="utf-8")
    subprocess.run(["git", "add", filename], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"add {filename}"], cwd=repo, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    return sha


def _seed_ready_task(repo: Path, *, branch: str, run_id: str) -> None:
    """Seed a queue task in done state pointing at ``branch``.

    This puts the task in landing's ready bucket so the merge action is
    legal. Mirrors the helper inlined in test_web_mission_control.py
    (kept inline here so the freshness suite can move standalone).
    """
    append_task(
        repo,
        QueueTask(
            id="ready-task",
            command_argv=["build", "ready task"],
            added_at="2026-04-24T00:00:00Z",
            resolved_intent="ready task",
            branch=branch,
            worktree=".worktrees/ready-task",
        ),
    )
    write_queue_state(
        repo,
        {
            "schema_version": 1,
            "watcher": None,
            "tasks": {
                "ready-task": {
                    "status": "done",
                    "attempt_run_id": run_id,
                    "stories_passed": 1,
                    "stories_tested": 1,
                },
            },
        },
    )


# --------------------------------------------------------------------------- #
# Diff response enrichment
# --------------------------------------------------------------------------- #


def test_diff_response_includes_shas(tmp_path: Path) -> None:
    """Live diff response carries SHAs, fetch time, hunk counts, and limits."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    target_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    branch_sha = _create_branch_with_change(repo, "build/ready-task")
    _seed_ready_task(repo, branch="build/ready-task", run_id="run-ready")

    client = TestClient(create_app(repo))
    response = client.get("/api/runs/run-ready/diff")
    assert response.status_code == 200, response.text
    diff = response.json()

    # SHA fields populated and well-formed.
    assert SHA_RE.match(diff["target_sha"]), diff["target_sha"]
    assert SHA_RE.match(diff["branch_sha"]), diff["branch_sha"]
    assert SHA_RE.match(diff["merge_base"]), diff["merge_base"]
    # The diff for a single-commit branch off main: target HEAD == merge-base.
    assert diff["target_sha"] == target_sha
    assert diff["branch_sha"] == branch_sha
    assert diff["merge_base"] == target_sha

    # Freshness/footprint metadata.
    assert diff["fetched_at"].endswith("Z"), diff["fetched_at"]
    assert diff["limit_chars"] > 0
    assert diff["full_size_chars"] > 0
    # Single-file change has at least one hunk.
    assert diff["total_hunks"] >= 1
    assert diff["shown_hunks"] == diff["total_hunks"]
    assert diff["truncated"] is False
    # Command field is populated and the diff text is fully present.
    assert diff["command"] == "git diff main...build/ready-task"
    assert "+ready" in diff["text"]
    # No errors path on the happy case.
    assert diff["errors"] == []


def test_diff_response_handles_invalid_branch(tmp_path: Path) -> None:
    """A missing branch yields ``branch_sha=null`` plus an entry in ``errors``."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    # Seed a queue task pointing at a branch that does not exist locally
    # *or* on origin. The diff endpoint must still return — only the
    # branch-side lookup should fail.
    _seed_ready_task(repo, branch="build/missing-branch", run_id="run-missing")

    client = TestClient(create_app(repo))
    response = client.get("/api/runs/run-missing/diff")
    assert response.status_code == 200, response.text
    diff = response.json()

    # Target still resolves (main exists).
    assert SHA_RE.match(diff["target_sha"]), diff["target_sha"]
    # Branch is null because the ref cannot be resolved.
    assert diff["branch_sha"] is None
    # Merge-base needs both refs; with branch missing it is also null.
    assert diff["merge_base"] is None
    # The errors list documents which lookup failed.
    assert any("build/missing-branch" in err for err in diff["errors"]), diff["errors"]


def test_diff_response_truncates_with_clarity(tmp_path: Path) -> None:
    """Oversized diffs report truncation footprint, not a bare ``truncated`` flag."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    # Build a branch whose diff text exceeds the default 240k limit.
    subprocess.run(["git", "checkout", "-q", "-b", "build/big-task"], cwd=repo, check=True)
    big = "\n".join(f"line {i:06d} added content here" for i in range(20_000))
    (repo / "huge.txt").write_text(big + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "huge.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "huge"], cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
    _seed_ready_task(repo, branch="build/big-task", run_id="run-big")

    client = TestClient(create_app(repo))
    response = client.get("/api/runs/run-big/diff")
    assert response.status_code == 200, response.text
    diff = response.json()

    assert diff["truncated"] is True
    assert diff["limit_chars"] == 240_000
    assert diff["full_size_chars"] > diff["limit_chars"]
    assert len(diff["text"]) <= diff["limit_chars"]
    assert diff["total_hunks"] >= 1
    # We never claim to show more than we actually shipped.
    assert diff["shown_hunks"] <= diff["total_hunks"]


# --------------------------------------------------------------------------- #
# Merge SHA validation (the safety hatch)
# --------------------------------------------------------------------------- #


def _stub_merge_executor(monkeypatch) -> list[dict]:
    """Replace ``execute_action`` so merge tests don't actually shell out.

    Returns a list that records each invocation so callers can assert
    "did the merge actually fire?" without touching git.
    """
    calls: list[dict] = []

    def _fake(record, action_kind, project_dir, **kwargs):
        from otto.mission_control.actions import ActionResult
        calls.append({"record": record.run_id, "action": action_kind, "kwargs": kwargs})
        return ActionResult(ok=True, message="merge requested", refresh=True)

    monkeypatch.setattr("otto.mission_control.service.execute_action", _fake)
    monkeypatch.setattr(
        "otto.mission_control.service._merge_preflight",
        lambda project_dir: {"merge_blocked": False, "merge_blockers": [], "dirty_files": []},
    )
    return calls


def test_merge_rejects_stale_target_sha(tmp_path: Path, monkeypatch) -> None:
    """Merge with an expected_target_sha that doesn't match HEAD → 409 + clear copy."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    _create_branch_with_change(repo, "build/ready-task")
    _seed_ready_task(repo, branch="build/ready-task", run_id="run-ready")

    record = make_run_record(
        project_dir=repo,
        run_id="run-ready",
        domain="queue",
        run_type="queue",
        command="build ready task",
        display_name="ready-task",
        status="done",
        cwd=repo,
        identity={"queue_task_id": "ready-task"},
        git={"branch": "build/ready-task"},
        intent={"summary": "ready"},
        adapter_key="queue.attempt",
    )
    write_record(repo, record)

    calls = _stub_merge_executor(monkeypatch)

    client = TestClient(create_app(repo))
    stale = "0" * 40  # impossible-to-match SHA
    response = client.post(
        "/api/runs/run-ready/actions/merge",
        json={"expected_target_sha": stale},
    )
    assert response.status_code == 409, response.text
    body = response.json()
    msg = body.get("message") or ""
    assert "moved" in msg or "Re-fetch" in msg, msg
    assert "main" in msg
    # Critically, the merge executor was never invoked.
    assert calls == [], calls


def test_merge_accepts_matching_target_sha(tmp_path: Path, monkeypatch) -> None:
    """Merge with the *current* target SHA passes through to the executor."""

    repo = tmp_path / "repo"
    _init_repo(repo)
    target_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    branch_sha = _create_branch_with_change(repo, "build/ready-task")
    _seed_ready_task(repo, branch="build/ready-task", run_id="run-ready")

    record = make_run_record(
        project_dir=repo,
        run_id="run-ready",
        domain="queue",
        run_type="queue",
        command="build ready task",
        display_name="ready-task",
        status="done",
        cwd=repo,
        identity={"queue_task_id": "ready-task"},
        git={"branch": "build/ready-task"},
        intent={"summary": "ready"},
        adapter_key="queue.attempt",
    )
    write_record(repo, record)

    calls = _stub_merge_executor(monkeypatch)

    client = TestClient(create_app(repo))
    response = client.post(
        "/api/runs/run-ready/actions/merge",
        json={
            "expected_target_sha": target_sha,
            "expected_branch_sha": branch_sha,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body.get("ok") is True
    assert calls and calls[0]["action"] == "m"


def test_merge_without_expected_shas_does_not_validate(tmp_path: Path, monkeypatch) -> None:
    """Operators on legacy clients (no SHA in body) still get the old behavior.

    The validation is opt-in by the client passing the SHA. This keeps
    backward-compat with any tooling that bypasses the SPA — and
    documents that the SPA *must* send the SHAs to get the protection.
    """

    repo = tmp_path / "repo"
    _init_repo(repo)
    _create_branch_with_change(repo, "build/ready-task")
    _seed_ready_task(repo, branch="build/ready-task", run_id="run-ready")

    record = make_run_record(
        project_dir=repo,
        run_id="run-ready",
        domain="queue",
        run_type="queue",
        command="build ready task",
        display_name="ready-task",
        status="done",
        cwd=repo,
        identity={"queue_task_id": "ready-task"},
        git={"branch": "build/ready-task"},
        intent={"summary": "ready"},
        adapter_key="queue.attempt",
    )
    write_record(repo, record)

    calls = _stub_merge_executor(monkeypatch)
    client = TestClient(create_app(repo))
    response = client.post("/api/runs/run-ready/actions/merge", json={})
    assert response.status_code == 200, response.text
    assert calls and calls[0]["action"] == "m"
