# pyright: reportPrivateUsage=false
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from otto import v5_runner
from otto.lead import LeadResult
from otto.queue.subtask import enqueue_subtask
from otto.queue.task_graph import get_task, record_task, set_verdict, update_task_metadata
from otto.v5_runner import ROOT_TASK_ID


def _git(cwd: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main", check=True)
    _git(repo, "config", "user.email", "test@example.invalid", check=True)
    _git(repo, "config", "user.name", "Test User", check=True)
    (repo / ".gitignore").write_text(".worktrees/\notto_logs/\n.otto/\n", encoding="utf-8")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A", check=True)
    _git(repo, "commit", "-q", "-m", "init", check=True)


def _enqueue(
    repo: Path,
    *,
    task_id: str,
    parent_task_id: str = ROOT_TASK_ID,
    task_role: str = "feature",
    owned_paths: list[str] | None = None,
    depends_on: list[str] | None = None,
    intent: str | None = None,
    integration_branch: str = "main",
) -> None:
    generated = enqueue_subtask(
        project_dir=repo,
        parent_task_id=parent_task_id,
        parent_session_dir=repo / "otto_logs" / "sessions" / parent_task_id,
        intent=intent or task_id,
        depends_on=depends_on or [],
        owned_paths=owned_paths or [],
        task_role=task_role,
        parent_integration_branch=integration_branch,
    )
    pending = repo / "otto_logs" / "cross-sessions" / "v5_pending.jsonl"
    pending.write_text(
        pending.read_text(encoding="utf-8").replace(generated, task_id),
        encoding="utf-8",
    )
    record_task(
        repo,
        task_id=task_id,
        parent_task_id=parent_task_id,
        intent=intent or task_id,
        integration_branch=integration_branch,
        depends_on=depends_on or [],
        owned_paths=owned_paths or [],
        task_role=task_role,  # type: ignore[arg-type]
    )


class _CleanPass:
    passed = True
    issues: list[Any] = []

    def to_jsonable(self) -> dict[str, Any]:
        return {"passed": True, "issues": []}


@pytest.mark.asyncio
async def test_no_dep_feature_waits_for_foundation_and_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record_task(repo, task_id=ROOT_TASK_ID, intent="root", parent_task_id=None)
    _enqueue(
        repo,
        task_id="foundation",
        task_role="foundation",
        owned_paths=["backend/auth.py"],
        intent="Foundation scaffold",
    )
    _enqueue(
        repo,
        task_id="feature",
        task_role="feature",
        owned_paths=["backend/routers/auth.py"],
        intent="Feature without depends_on",
    )

    dispatched: list[str] = []

    async def fake_run_child(**kwargs: Any) -> LeadResult:
        task_id = str(kwargs["entry"]["task_id"])
        dispatched.append(task_id)
        assert task_id == "foundation"
        set_verdict(repo, task_id, "pass")
        return LeadResult(task_id=task_id, verdict="pass", decomposition="inline", verify_called=True)

    monkeypatch.setattr(v5_runner, "_run_child", fake_run_child)
    monkeypatch.setattr(v5_runner, "_verify_child_branches_reached_parent", lambda **_kwargs: None)

    await v5_runner._process_children(
        project_dir=repo,
        parent_task_id=ROOT_TASK_ID,
        config={},
        max_parallel=2,
        tree_budget_usd=100.0,
        child_results={},
        integration_results={},
    )

    feature = get_task(repo, "feature") or {}
    foundation = get_task(repo, "foundation") or {}
    assert dispatched == ["foundation", "foundation", "foundation"]
    assert foundation["verdict"] == "merge_blocked"
    assert foundation["merge_blocked_structured_reason"]["kind"] == (
        "foundation_contracts_missing_after_pass"
    )
    assert feature["verdict"] == "merge_blocked"
    assert feature["merge_blocked_structured_reason"]["kind"] == (
        "foundation_contracts_missing_after_pass"
    )


@pytest.mark.parametrize("foundation_verdict", ["merge_blocked", "catastrophic"])
@pytest.mark.asyncio
async def test_terminal_blocked_foundation_blocks_features_honestly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    foundation_verdict: str,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record_task(repo, task_id=ROOT_TASK_ID, intent="root", parent_task_id=None)
    _enqueue(
        repo,
        task_id="foundation",
        task_role="foundation",
        owned_paths=["backend/"],
        intent="Foundation scaffold",
    )
    _enqueue(
        repo,
        task_id="feature",
        task_role="feature",
        depends_on=["foundation"],
        owned_paths=["backend/routers/auth.py"],
        intent="Feature depends on foundation",
    )
    set_verdict(repo, "foundation", foundation_verdict)  # type: ignore[arg-type]
    if foundation_verdict == "merge_blocked":
        update_task_metadata(
            repo,
            "foundation",
            merge_blocked_reason="foundation failed",
            merge_blocked_structured_reason={
                "kind": "foundation_contract_write_blocked",
                "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
        )

    dispatched: list[str] = []
    events: list[dict[str, Any]] = []

    async def fake_run_child(**kwargs: Any) -> LeadResult:
        dispatched.append(str(kwargs["entry"]["task_id"]))
        raise AssertionError("terminal foundation must block feature dispatch")

    monkeypatch.setattr(v5_runner, "_run_child", fake_run_child)
    monkeypatch.setattr(v5_runner, "_verify_child_branches_reached_parent", lambda **_kwargs: None)

    await v5_runner._process_children(
        project_dir=repo,
        parent_task_id=ROOT_TASK_ID,
        config={},
        max_parallel=2,
        tree_budget_usd=100.0,
        child_results={},
        integration_results={},
        on_event=events.append,
    )

    task = get_task(repo, "feature") or {}
    assert dispatched == []
    assert task["verdict"] == "merge_blocked"
    assert task["merge_blocked_structured_reason"]["kind"] == "foundation_unsatisfied"


@pytest.mark.asyncio
async def test_feature_creating_foundation_path_blocks_before_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record_task(repo, task_id=ROOT_TASK_ID, intent="root", parent_task_id=None)
    record_task(
        repo,
        task_id="foundation",
        parent_task_id=ROOT_TASK_ID,
        intent="Foundation",
        task_role="foundation",
        owned_paths=["backend/auth.py"],
    )
    record_task(
        repo,
        task_id="feature",
        parent_task_id=ROOT_TASK_ID,
        intent="Feature",
        task_role="feature",
        owned_paths=["backend/features/"],
        integration_branch="main",
    )
    update_task_metadata(
        repo,
        ROOT_TASK_ID,
        foundation_contracts=[
            {"path": "backend/auth.py", "owner_task_id": "foundation", "check": "literal"}
        ],
    )
    before = _git(repo, "rev-parse", "main", check=True).stdout.strip()
    (repo / "backend").mkdir()
    (repo / "backend" / "auth.py").write_text("SECRET = True\n", encoding="utf-8")

    def fail_commit(**_kwargs: Any) -> tuple[bool, str]:
        raise AssertionError("commit_worktree must not run for a foundation-contract write")

    monkeypatch.setattr("otto.v5_branching.commit_worktree", fail_commit)

    result = LeadResult(task_id="feature", verdict="pass", decomposition="inline", verify_called=True)
    await v5_runner._merge_child_branch(
        project_dir=repo,
        child_task_id="feature",
        child_worktree=repo,
        child_session_dir=repo / "otto_logs" / "sessions" / "feature",
        parent_integration_branch="main",
        result=result,
        config={},
    )

    after = _git(repo, "rev-parse", "main", check=True).stdout.strip()
    task = get_task(repo, "feature") or {}
    assert after == before
    assert task["verdict"] == "merge_blocked"
    assert task["merge_blocked_structured_reason"]["kind"] == "foundation_contract_write_blocked"


def test_gated_child_is_not_mergeable_from_stale_in_memory_pass(tmp_path: Path) -> None:
    record_task(tmp_path, task_id="feature", intent="Feature", parent_task_id=ROOT_TASK_ID)
    set_verdict(tmp_path, "feature", "merge_blocked")
    update_task_metadata(
        tmp_path,
        "feature",
        merge_blocked_structured_reason={
            "kind": "foundation_contract_write_blocked",
            "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    )

    stale = LeadResult(task_id="feature", verdict="pass", decomposition="inline")

    assert not v5_runner._child_result_allows_upward_merge(tmp_path, "feature", stale)


def test_task_entry_pass_with_blocked_metadata_is_not_mergeable() -> None:
    entry = {
        "verdict": "pass",
        "merge_blocked_structured_reason": {
            "kind": "foundation_contract_write_blocked",
            "_written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }

    assert not v5_runner._task_entry_allows_upward_merge(entry)
