"""Regression tests for v5 integration worktree branch discipline."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from otto.lead import LeadResult, _render_prompt
from otto.queue.task_graph import record_task, set_verdict
from otto.spec_compile_flat import FlatSpec
from otto.v5_branching import integration_branch_name
from otto.v5_preflight import PreflightIssue


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "t")
    (repo / ".gitignore").write_text(
        ".worktrees/\notto_logs/\nuploads/\n*.db\n*.db.bak\n"
    )
    (repo / "README.md").write_text("init\n")
    _git(repo, "add", ".gitignore", "README.md")
    _git(repo, "commit", "-q", "-m", "init")


def _seed_integration_task(repo: Path, task_id: str) -> str:
    own_integration = integration_branch_name(task_id)
    record_task(repo, task_id="root", intent="root", parent_task_id=None)
    record_task(
        repo,
        task_id=task_id,
        intent="integrate product",
        parent_task_id="root",
        integration_branch="main",
    )
    return own_integration


def test_integration_prompt_renders_smoke_preflight_payload(tmp_path: Path) -> None:
    rendered = _render_prompt(
        kind="integration",
        task_id="v5-integrate",
        intent="integrate product",
        session_dir=tmp_path / "session",
        integration_branch="i2p/integ/v5-integrate",
        child_summaries=[],
        integration_packet_path="/tmp/session/integration_packet.json",
        preflight_result={
            "check": "smoke_clean_deploy",
            "cwd": "/tmp/merged-worktree",
            "passed": False,
            "issues": [
                {
                    "kind": "clean_deploy_start_failed",
                    "severity": "block",
                    "message": "start.sh failed",
                }
            ],
        },
    )

    assert "PRE-INTEGRATION PREFLIGHT" in rendered
    assert "smoke_clean_deploy" in rendered
    assert "/tmp/merged-worktree" in rendered
    assert "clean_deploy_start_failed" in rendered
    assert "start.sh failed" in rendered
    assert "First read `/tmp/session/integration_packet.json`" in rendered


def test_lead_prompt_renders_decomp_runtime_context(tmp_path: Path) -> None:
    rendered = _render_prompt(
        kind="plan_or_inline",
        task_id="root",
        intent="build tracker",
        session_dir=tmp_path / "session",
        integration_branch=None,
        child_summaries=[],
        decomp_runtime_context={
            "max_parallel": 3,
            "run_budget_seconds": 3600,
            "cost_model_s": {"worktree_setup_s": 60},
            "queue_state": {"ready": 0, "free_slots": 3},
            "spec_profile": {"core_entities": 4},
        },
    )

    assert "DECOMP_RUNTIME_CONTEXT" in rendered
    assert '"max_parallel": 3' in rendered
    assert "FE waiting on BE is fake parallelism" in rendered
    assert "vertical capability leaves" in rendered
    assert "budget_usd" not in rendered


@pytest.mark.asyncio
async def test_nested_integration_restores_project_dir_to_parent_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A nested integration may run from project_dir, but must restore main."""
    from otto import v5_runner

    repo = tmp_path / "repo"
    _init_repo(repo)
    task_id = "v5-child"
    own_integration = integration_branch_name(task_id)
    _git(repo, "branch", own_integration, "main")
    _git(repo, "checkout", "-q", own_integration)

    record_task(repo, task_id="root", intent="root", parent_task_id=None)
    record_task(
        repo,
        task_id=task_id,
        intent="child with grandchildren",
        parent_task_id="root",
        integration_branch="main",
    )

    branches_seen: list[str] = []

    async def fake_run_lead(**kwargs: Any) -> LeadResult:
        branches_seen.append(_git(repo, "branch", "--show-current").stdout.strip())
        return LeadResult(
            task_id=kwargs["task_id"],
            verdict="pass",
            cost_usd=0.1,
            decomposition="inline",
        )

    monkeypatch.setattr(v5_runner, "run_lead", fake_run_lead)
    monkeypatch.setattr(v5_runner, "smoke_clean_deploy", lambda *a, **k: [])

    result = await v5_runner._run_integration(
        project_dir=repo,
        task_id=task_id,
        intent="integrate child subtree",
        config={"default_branch": "main"},
        child_results={},
        integration_results={},
    )

    assert result.verdict == "pass"
    assert branches_seen == [own_integration]
    assert _git(repo, "branch", "--show-current").stdout.strip() == "main"


@pytest.mark.asyncio
async def test_root_integration_starts_on_main_even_after_prior_worktree_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root integration checks out main even if child processing left another branch."""
    from otto import v5_runner

    repo = tmp_path / "repo"
    _init_repo(repo)
    stale_branch = integration_branch_name("v5-stale")
    _git(repo, "branch", stale_branch, "main")
    _git(repo, "checkout", "-q", stale_branch)

    calls: list[tuple[str, str]] = []

    async def fake_compile_flat_spec(**kwargs: Any) -> FlatSpec:
        del kwargs
        return FlatSpec(intent="build a thing", behavior_journeys=[])

    async def fake_process_children(**kwargs: Any) -> None:
        project_dir = kwargs["project_dir"]
        child_results = kwargs["child_results"]
        _git(project_dir, "checkout", "-q", stale_branch)
        record_task(
            project_dir,
            task_id="v5-child",
            intent="child",
            parent_task_id="root",
            integration_branch="main",
        )
        set_verdict(project_dir, "v5-child", "pass")
        child_results["v5-child"] = LeadResult(
            task_id="v5-child",
            verdict="pass",
            cost_usd=0.1,
            decomposition="inline",
        )

    async def fake_run_lead(**kwargs: Any) -> LeadResult:
        kind = kwargs.get("kind", "plan_or_inline")
        calls.append((kind, _git(repo, "branch", "--show-current").stdout.strip()))
        if kind == "integration":
            return LeadResult(
                task_id=kwargs["task_id"],
                verdict="pass",
                cost_usd=0.2,
                decomposition="inline",
            )
        return LeadResult(
            task_id=kwargs["task_id"],
            verdict="pending_children",
            cost_usd=0.3,
            decomposition="emit",
            emitted_subtask_ids=["v5-child"],
        )

    monkeypatch.setattr(v5_runner, "compile_flat_spec", fake_compile_flat_spec)
    monkeypatch.setattr(v5_runner, "_process_children", fake_process_children)
    monkeypatch.setattr(v5_runner, "run_lead", fake_run_lead)

    result = await v5_runner.run_v5_pipeline(
        project_dir=repo,
        intent="build a thing",
        config={"default_branch": "main"},
    )

    assert result.verdict == "pass"
    assert ("plan_or_inline", "main") in calls
    assert ("integration", "main") in calls
    assert _git(repo, "branch", "--show-current").stdout.strip() == "main"


@pytest.mark.asyncio
async def test_root_integration_receives_clean_deploy_preflight_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from otto import v5_runner

    repo = tmp_path / "repo"
    _init_repo(repo)
    lead_kwargs: list[dict[str, Any]] = []

    async def fake_compile_flat_spec(**kwargs: Any) -> FlatSpec:
        del kwargs
        return FlatSpec(intent="build a thing", behavior_journeys=[])

    async def fake_process_children(**kwargs: Any) -> None:
        project_dir = kwargs["project_dir"]
        child_results = kwargs["child_results"]
        record_task(
            project_dir,
            task_id="v5-child",
            intent="child",
            parent_task_id="root",
            integration_branch="main",
        )
        set_verdict(project_dir, "v5-child", "pass")
        child_results["v5-child"] = LeadResult(
            task_id="v5-child",
            verdict="pass",
            cost_usd=0.1,
            decomposition="inline",
        )

    async def fake_run_lead(**kwargs: Any) -> LeadResult:
        lead_kwargs.append(kwargs)
        if kwargs.get("kind") == "integration":
            return LeadResult(
                task_id=kwargs["task_id"],
                verdict="pass",
                cost_usd=0.2,
                decomposition="inline",
                verify_called=True,
                verify_result={"verdict": "pass", "summary": "ok"},
            )
        return LeadResult(
            task_id=kwargs["task_id"],
            verdict="pending_children",
            cost_usd=0.3,
            decomposition="emit",
            emitted_subtask_ids=["v5-child"],
        )

    smoke_results = [
        [
            PreflightIssue(
                kind="clean_deploy_start_failed",
                severity="block",
                message="root start failed",
            )
        ],
        [],
    ]

    def fake_smoke(path: Path, *_args: Any, **_kwargs: Any) -> list[PreflightIssue]:
        assert path == repo
        return smoke_results.pop(0)

    monkeypatch.setattr(v5_runner, "compile_flat_spec", fake_compile_flat_spec)
    monkeypatch.setattr(v5_runner, "_process_children", fake_process_children)
    monkeypatch.setattr(v5_runner, "run_lead", fake_run_lead)
    monkeypatch.setattr(v5_runner, "smoke_clean_deploy", fake_smoke)

    result = await v5_runner.run_v5_pipeline(
        project_dir=repo,
        intent="build a thing",
        config={"default_branch": "main"},
    )

    assert result.verdict == "pass"
    integration_calls = [call for call in lead_kwargs if call.get("kind") == "integration"]
    assert len(integration_calls) == 1
    payload = integration_calls[0]["preflight_result"]
    assert payload["check"] == "smoke_clean_deploy"
    assert payload["task_id"] == "root"
    assert payload["passed"] is False
    assert payload["issues"][0]["message"] == "root start failed"


@pytest.mark.asyncio
async def test_runner_commits_integration_product_files_and_excludes_runtime_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration edits are committed by the runner with runtime paths excluded."""
    from otto import v5_runner

    repo = tmp_path / "repo"
    _init_repo(repo)
    task_id = "v5-integrate"
    own_integration = integration_branch_name(task_id)
    _git(repo, "branch", own_integration, "main")
    _git(repo, "checkout", "-q", own_integration)

    record_task(repo, task_id="root", intent="root", parent_task_id=None)
    record_task(
        repo,
        task_id=task_id,
        intent="integrate product",
        parent_task_id="root",
        integration_branch="main",
    )

    async def fake_run_lead(**kwargs: Any) -> LeadResult:
        del kwargs
        (repo / "frontend" / "src").mkdir(parents=True)
        (repo / "frontend" / "src" / "App.tsx").write_text("export const ok = true;\n")
        (repo / "api").mkdir()
        (repo / "api" / "main.py").write_text("print('api')\n")
        (repo / "otto_logs" / "sessions" / "s1").mkdir(parents=True)
        (repo / "otto_logs" / "sessions" / "s1" / "noise.log").write_text("runtime\n")
        (repo / "uploads" / "images").mkdir(parents=True)
        (repo / "uploads" / "images" / "x.png").write_bytes(b"not product")
        (repo / "data.db.bak").write_bytes(b"sqlite")
        return LeadResult(
            task_id=task_id,
            verdict="pass",
            cost_usd=0.1,
            decomposition="inline",
        )

    monkeypatch.setattr(v5_runner, "run_lead", fake_run_lead)
    monkeypatch.setattr(v5_runner, "smoke_clean_deploy", lambda *a, **k: [])

    result = await v5_runner._run_integration(
        project_dir=repo,
        task_id=task_id,
        intent="integrate product",
        config={"default_branch": "main"},
        child_results={},
        integration_results={},
    )

    assert result.verdict == "pass"
    assert _git(repo, "branch", "--show-current").stdout.strip() == "main"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""

    tracked = _git(repo, "ls-tree", "-r", "--name-only", own_integration).stdout.splitlines()
    assert "frontend/src/App.tsx" in tracked
    assert "api/main.py" in tracked
    assert "otto_logs/sessions/s1/noise.log" not in tracked
    assert "uploads/images/x.png" not in tracked
    assert "data.db.bak" not in tracked

    subject = _git(repo, "log", "-1", "--pretty=%s", own_integration).stdout.strip()
    assert subject == f"integration: {task_id} runner-managed changes"


@pytest.mark.asyncio
async def test_integration_smoke_payload_uses_resolved_worktree_and_reruns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from otto import v5_runner

    repo = tmp_path / "repo"
    _init_repo(repo)
    task_id = "v5-integrate"
    own_integration = _seed_integration_task(repo, task_id)

    smoke_calls: list[Path] = []
    smoke_results = [
        [
            PreflightIssue(
                kind="clean_deploy_start_failed",
                severity="block",
                message="start.sh failed before repair",
            )
        ],
        [],
    ]
    lead_kwargs: list[dict[str, Any]] = []

    def fake_smoke(path: Path, *_args: Any, **_kwargs: Any) -> list[PreflightIssue]:
        smoke_calls.append(Path(path))
        return smoke_results.pop(0)

    async def fake_run_lead(**kwargs: Any) -> LeadResult:
        lead_kwargs.append(kwargs)
        return LeadResult(
            task_id=kwargs["task_id"],
            verdict="pass",
            cost_usd=0.1,
            decomposition="inline",
            verify_called=True,
            verify_result={"verdict": "pass", "summary": "fixed"},
        )

    monkeypatch.setattr(v5_runner, "smoke_clean_deploy", fake_smoke)
    monkeypatch.setattr(v5_runner, "run_lead", fake_run_lead)

    result = await v5_runner._run_integration(
        project_dir=repo,
        task_id=task_id,
        intent="integrate product",
        config={"default_branch": "main"},
        child_results={},
        integration_results={},
    )

    assert result.verdict == "pass"
    assert len(smoke_calls) == 2
    assert all(path != repo for path in smoke_calls)
    assert all(path.exists() for path in smoke_calls)
    assert smoke_calls[0] == smoke_calls[1]
    assert _git(smoke_calls[0], "branch", "--show-current").stdout.strip() == own_integration

    assert len(lead_kwargs) == 1
    payload = lead_kwargs[0]["preflight_result"]
    assert payload["check"] == "smoke_clean_deploy"
    assert payload["cwd"] == str(smoke_calls[0])
    assert payload["passed"] is False
    assert payload["issues"][0]["kind"] == "clean_deploy_start_failed"
    assert "before repair" in payload["issues"][0]["message"]
    packet_path = Path(lead_kwargs[0]["integration_packet_path"])
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["parent_task_id"] == task_id
    assert packet["integration_branch"] == own_integration
    assert packet["preflight_results"]["pre_agent"]["issues"][0]["kind"] == "clean_deploy_start_failed"

    assert result.verify_result is not None
    assert result.verify_result["post_integration_preflight"]["passed"] is True


@pytest.mark.asyncio
async def test_integration_repeat_smoke_failure_downgrades_merge_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from otto import v5_runner
    from otto.queue.task_graph import get_task

    repo = tmp_path / "repo"
    _init_repo(repo)
    task_id = "v5-integrate"
    _seed_integration_task(repo, task_id)

    blocking_issue = PreflightIssue(
        kind="clean_deploy_start_failed",
        severity="block",
        message="start.sh still fails",
    )
    smoke_calls: list[Path] = []

    def fake_smoke(path: Path, *_args: Any, **_kwargs: Any) -> list[PreflightIssue]:
        smoke_calls.append(Path(path))
        return [blocking_issue]

    async def fake_run_lead(**kwargs: Any) -> LeadResult:
        return LeadResult(
            task_id=kwargs["task_id"],
            verdict="pass",
            cost_usd=0.1,
            decomposition="inline",
            verify_called=True,
            verify_result={"verdict": "pass", "summary": "claimed fixed"},
        )

    events: list[dict[str, Any]] = []
    monkeypatch.setattr(v5_runner, "smoke_clean_deploy", fake_smoke)
    monkeypatch.setattr(v5_runner, "run_lead", fake_run_lead)

    result = await v5_runner._run_integration(
        project_dir=repo,
        task_id=task_id,
        intent="integrate product",
        config={"default_branch": "main"},
        child_results={},
        integration_results={},
        on_event=events.append,
    )

    assert len(smoke_calls) == 2
    assert result.verdict == "merge_blocked"
    assert "start.sh still fails" in result.failure_reason
    assert (get_task(repo, task_id) or {}).get("verdict") == "merge_blocked"
    assert result.verify_result is not None
    assert result.verify_result["verdict"] == "merge_blocked"
    assert result.verify_result["post_integration_preflight"]["passed"] is False
    assert any(e.get("event") == "integration_smoke_failed" for e in events)
