"""End-to-end regressions for decomposed child subtree propagation.

These tests pin the failure shape from the v6b audit: a decomposed top-level
child's descendants can pass and merge into that child's integration branch,
but the real product files must also become reachable from main.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from otto.lead import LeadResult
from otto.queue.subtask import v5_pending_path
from otto.queue.task_graph import (
    aggregate_verdict,
    get_task,
    record_task,
    set_decomposition,
    set_verdict,
)
from otto.v5_branching import child_branch_name, integration_branch_name
from otto.v5_runner import ROOT_TASK_ID, _process_children


def _git(
    cwd: Path,
    *args: str,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and cp.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}\n"
            f"stdout:\n{cp.stdout}\nstderr:\n{cp.stderr}"
        )
    return cp


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main", check=True)
    _git(repo, "config", "user.email", "test@example.invalid", check=True)
    _git(repo, "config", "user.name", "Test User", check=True)
    (repo / ".gitignore").write_text(".worktrees/\notto_logs/\n", encoding="utf-8")
    (repo / "README.md").write_text("initial repo\n", encoding="utf-8")
    _git(repo, "add", ".gitignore", "README.md", check=True)
    _git(repo, "commit", "-q", "-m", "init", check=True)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _enqueue_fixed_task(
    repo: Path,
    *,
    task_id: str,
    parent_task_id: str,
    intent: str,
    depends_on: list[str] | None = None,
) -> None:
    parent_session_dir = repo / "otto_logs" / "sessions" / f"session-{parent_task_id}"
    parent_session_dir.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        "schema_version": 1,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "parent_session_dir": str(parent_session_dir),
        "intent": intent,
        "depends_on": list(depends_on or []),
        "owned_paths": [],
        "action_ids": [],
        "integration_branch": integration_branch_name(parent_task_id),
        "review_state": "approved",
        "enqueued_at": _now_iso(),
    }
    pending = v5_pending_path(repo)
    pending.parent.mkdir(parents=True, exist_ok=True)
    with pending.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    record_task(
        repo,
        task_id=task_id,
        intent=intent,
        parent_task_id=parent_task_id,
        integration_branch=entry["integration_branch"],
        depends_on=depends_on or [],
    )


def _seed_root(repo: Path, root_children: list[str], intents: dict[str, str]) -> None:
    record_task(repo, task_id=ROOT_TASK_ID, intent="root", parent_task_id=None)
    set_decomposition(repo, ROOT_TASK_ID, "emit")
    set_verdict(repo, ROOT_TASK_ID, "pending_children")
    for child_id in root_children:
        _enqueue_fixed_task(
            repo,
            task_id=child_id,
            parent_task_id=ROOT_TASK_ID,
            intent=intents[child_id],
        )


def _lead_worktree(project_dir: Path, session_dir: Path) -> Path:
    linked = session_dir / "worktree"
    if linked.exists():
        return linked.resolve()
    return project_dir


def _write_file(root: Path, rel_path: str, content: str) -> None:
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _branch_state(repo: Path) -> str:
    return _git(
        repo,
        "log",
        "--graph",
        "--decorate",
        "--oneline",
        "--all",
        "-30",
    ).stdout


def _assert_file_reachable_from_main(
    repo: Path,
    *,
    task_id: str,
    rel_path: str,
    expected_content: str,
) -> None:
    ls_tree = _git(
        repo,
        "ls-tree",
        "-r",
        "--name-only",
        "main",
        "--",
        rel_path,
        check=True,
    ).stdout.splitlines()
    assert rel_path in ls_tree, (
        f"{rel_path} missing from main\nbranch state:\n{_branch_state(repo)}"
    )

    content = _git(repo, "show", f"main:{rel_path}", check=True).stdout
    assert content == expected_content

    branch = child_branch_name(task_id)
    file_commit = _git(
        repo,
        "log",
        "-1",
        "--format=%H",
        branch,
        "--",
        rel_path,
        check=True,
    ).stdout.strip()
    assert file_commit, f"{rel_path} has no commit on {branch}"

    ancestor = _git(repo, "merge-base", "--is-ancestor", file_commit, "main")
    assert ancestor.returncode == 0, (
        f"{rel_path} commit {file_commit} from {branch} is not reachable "
        f"from main\nbranch state:\n{_branch_state(repo)}"
    )


def _assert_branch_tip_reaches(
    repo: Path,
    *,
    branch: str,
    target: str = "main",
) -> None:
    ancestor = _git(repo, "merge-base", "--is-ancestor", branch, target)
    assert ancestor.returncode == 0, (
        f"{branch} tip is not reachable from {target}\nbranch state:\n{_branch_state(repo)}"
    )


class LeadStub:
    def __init__(
        self,
        repo: Path,
        *,
        intents: dict[str, str],
        children: dict[str, list[str]],
        files: dict[str, dict[str, str]],
        leaf_delays: dict[str, float] | None = None,
    ) -> None:
        self.repo = repo
        self.intents = intents
        self.children = children
        self.files = files
        self.leaf_delays = leaf_delays or {}
        self._emitted: set[str] = set()

    async def __call__(self, **kwargs: Any) -> LeadResult:
        task_id = kwargs["task_id"]
        kind = kwargs.get("kind", "plan_or_inline")

        if kind == "integration":
            child_summaries = list(kwargs.get("child_summaries") or [])
            verdict = (
                "merge_blocked"
                if any(s.get("verdict") == "merge_blocked" for s in child_summaries)
                else "pass"
            )
            worktree = _lead_worktree(kwargs["project_dir"], kwargs["session_dir"])
            _write_file(
                worktree,
                f"docs/integration-{task_id}.md",
                f"integration marker for {task_id}: {verdict}\n",
            )
            set_verdict(self.repo, task_id, verdict, cost_usd=0.1)
            return LeadResult(
                task_id=task_id,
                verdict=verdict,
                cost_usd=0.1,
                decomposition="inline",
                verify_called=True,
                verify_result={"verdict": verdict, "summary": f"{task_id} {verdict}"},
            )

        if task_id in self.children:
            if task_id not in self._emitted:
                for child_id in self.children[task_id]:
                    _enqueue_fixed_task(
                        self.repo,
                        task_id=child_id,
                        parent_task_id=task_id,
                        intent=self.intents[child_id],
                    )
                self._emitted.add(task_id)
            set_decomposition(self.repo, task_id, "emit")
            set_verdict(self.repo, task_id, "pending_children", cost_usd=0.1)
            return LeadResult(
                task_id=task_id,
                verdict="pending_children",
                cost_usd=0.1,
                decomposition="emit",
                emitted_subtask_ids=list(self.children[task_id]),
            )

        if task_id in self.leaf_delays:
            await asyncio.sleep(self.leaf_delays[task_id])

        worktree = _lead_worktree(kwargs["project_dir"], kwargs["session_dir"])
        for rel_path, content in self.files.get(task_id, {}).items():
            _write_file(worktree, rel_path, content)

        set_decomposition(self.repo, task_id, "inline")
        set_verdict(self.repo, task_id, "pass", cost_usd=0.1)
        return LeadResult(
            task_id=task_id,
            verdict="pass",
            cost_usd=0.1,
            decomposition="inline",
            verify_called=True,
            verify_result={"verdict": "pass", "summary": f"{task_id} passed"},
        )


async def _run_process_children(
    repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    stub: LeadStub,
    *,
    max_parallel: int = 1,
    events: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, LeadResult], dict[str, LeadResult], list[dict[str, Any]]]:
    from otto import v5_runner

    captured_events = events if events is not None else []
    child_results: dict[str, LeadResult] = {}
    integration_results: dict[str, LeadResult] = {}

    async def fake_smoke_preflight(**_kwargs: Any) -> dict[str, Any]:
        return {"check": "smoke_clean_deploy", "passed": True, "issues": []}

    monkeypatch.setattr(v5_runner, "run_lead", stub)
    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight_with_repair", fake_smoke_preflight)

    await _process_children(
        project_dir=repo,
        parent_task_id=ROOT_TASK_ID,
        config={"default_branch": "main"},
        max_parallel=max_parallel,
        tree_budget_usd=10.0,
        child_results=child_results,
        integration_results=integration_results,
        on_event=captured_events.append,
    )
    # Post Phase 2b (2026-05-21): orchestrator no longer merges children per
    # finish; integration Lead's Step 1 does. These tests don't dispatch an
    # integration Lead — they validate the end-to-end "branches reach main"
    # invariant. Simulate integration's merge step inline so tests keep
    # exercising the same invariant.
    _simulate_integration_merges(repo, captured_events)
    return child_results, integration_results, captured_events


def _simulate_integration_merges(
    repo: Path,
    events: list[dict[str, Any]],
) -> None:
    """Mirrors integration Lead's Step 1 atomically per parent: try to merge
    every passing child of a parent into the parent's integration target. If
    ANY child fails, roll back the whole subtree (reset the target to its
    pre-merge ref) and mark the conflicting child + all ancestors as
    `merge_blocked`. Only fully-clean subtrees propagate to main.

    This is what the real integration Lead does — its journey verification
    can only proceed after all children merged successfully; failed subtrees
    surface as `merge_blocked` and never reach main.
    """
    from otto.queue.task_graph import read_graph
    from otto.v5_branching import child_branch_name, integration_branch_name

    graph = read_graph(repo) or {}
    tasks = (graph.get("tasks") or {}) if isinstance(graph, dict) else {}
    if not tasks:
        return

    def _target_for_parent(parent_id: str | None) -> str:
        if not parent_id or parent_id == ROOT_TASK_ID:
            return "main"
        return integration_branch_name(parent_id)

    children_by_parent: dict[str, list[str]] = {}
    for tid, task in tasks.items():
        if tid == ROOT_TASK_ID:
            continue
        if task.get("verdict") != "pass":
            continue
        parent_id = task.get("parent_task_id") or ROOT_TASK_ID
        children_by_parent.setdefault(parent_id, []).append(tid)

    # Process deepest parents first (decomposed subtrees before their
    # grandparents) so a failing subtree's blocked verdict propagates up
    # before the grandparent attempts to merge it.
    def _depth(tid: str) -> int:
        d = 0
        cur = tid
        while cur and cur != ROOT_TASK_ID:
            d += 1
            cur = (tasks.get(cur) or {}).get("parent_task_id") or ""
        return d

    for parent_id in sorted(children_by_parent, key=_depth, reverse=True):
        # Re-read each child's current verdict — a child may have been marked
        # merge_blocked by a deeper-subtree failure since the graph snapshot.
        children = [
            cid for cid in children_by_parent[parent_id]
            if (get_task(repo, cid) or {}).get("verdict") == "pass"
        ]
        if not children:
            # If any child of this parent was demoted, the whole subtree is
            # not deliverable.
            if any(
                (get_task(repo, cid) or {}).get("verdict") == "merge_blocked"
                for cid in children_by_parent[parent_id]
            ):
                set_verdict(repo, parent_id, "merge_blocked")
            continue
        target = _target_for_parent(parent_id)
        target_exists = _git(repo, "rev-parse", "--verify", target).returncode == 0
        if target_exists:
            _git(repo, "checkout", target)
            pre_merge_ref = _git(repo, "rev-parse", target, check=True).stdout.strip()
        else:
            _git(repo, "checkout", "-b", target)
            pre_merge_ref = None

        attempted: list[tuple[str, str]] = []  # (task_id, branch)
        all_ok = True
        first_failure: str | None = None
        for tid in children:
            branch = child_branch_name(tid)
            ancestor = _git(repo, "merge-base", "--is-ancestor", branch, target)
            if ancestor.returncode == 0:
                continue
            merge = _git(repo, "merge", "--no-ff", branch, "-m", f"integration: merge {branch}")
            ok = merge.returncode == 0
            events.append({
                "event": "simulated_integration_merge",
                "task_id": tid,
                "branch": branch,
                "target": target,
                "ok": ok,
            })
            if not ok:
                _git(repo, "merge", "--abort")
                all_ok = False
                first_failure = tid
                break
            attempted.append((tid, branch))

        if not all_ok:
            # Roll back the subtree's integration target to its pre-merge
            # state — nothing from this subtree should propagate.
            if pre_merge_ref:
                _git(repo, "reset", "--hard", pre_merge_ref)
            if first_failure:
                set_verdict(repo, first_failure, "merge_blocked")
            ancestor_id: str | None = parent_id
            while ancestor_id and ancestor_id != ROOT_TASK_ID:
                set_verdict(repo, ancestor_id, "merge_blocked")
                ancestor_id = (tasks.get(ancestor_id) or {}).get("parent_task_id") or None


@pytest.mark.asyncio
async def test_decomposed_frontend_grandchildren_land_on_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    intents = {
        "v5-architect": "Build architecture scaffold",
        "v5-backend": "Build backend API",
        "v5-frontend": "Build frontend and decompose feature pages",
        "v5-feature-a": "Build login page",
        "v5-feature-b": "Build dashboard page",
        "v5-feature-c": "Build settings page",
    }
    _seed_root(repo, ["v5-architect", "v5-backend", "v5-frontend"], intents)
    stub = LeadStub(
        repo,
        intents=intents,
        children={"v5-frontend": ["v5-feature-a", "v5-feature-b", "v5-feature-c"]},
        files={
            "v5-architect": {"docs/architecture.md": "architecture scaffold\n"},
            "v5-backend": {"backend/app.py": "def app():\n    return 'api'\n"},
            "v5-feature-a": {
                "frontend/src/pages/Login.tsx": "export function Login(){return 'login'}\n"
            },
            "v5-feature-b": {
                "frontend/src/pages/Dashboard.tsx": "export function Dashboard(){return 'dash'}\n"
            },
            "v5-feature-c": {
                "frontend/src/pages/Settings.tsx": "export function Settings(){return 'settings'}\n"
            },
        },
    )

    await _run_process_children(repo, monkeypatch, stub)

    for task_id, rel_path, content in [
        (
            "v5-feature-a",
            "frontend/src/pages/Login.tsx",
            "export function Login(){return 'login'}\n",
        ),
        (
            "v5-feature-b",
            "frontend/src/pages/Dashboard.tsx",
            "export function Dashboard(){return 'dash'}\n",
        ),
        (
            "v5-feature-c",
            "frontend/src/pages/Settings.tsx",
            "export function Settings(){return 'settings'}\n",
        ),
    ]:
        _assert_file_reachable_from_main(
            repo,
            task_id=task_id,
            rel_path=rel_path,
            expected_content=content,
        )


@pytest.mark.asyncio
async def test_four_level_decomposed_subtree_lands_page_files_on_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    intents = {
        "v5-frontend": "Build frontend and decompose app shell",
        "v5-app-shell": "Build app shell and decompose pages",
        "v5-page-home": "Build home page",
        "v5-page-login": "Build login page",
        "v5-page-settings": "Build settings page",
    }
    _seed_root(repo, ["v5-frontend"], intents)
    stub = LeadStub(
        repo,
        intents=intents,
        children={
            "v5-frontend": ["v5-app-shell"],
            "v5-app-shell": ["v5-page-home", "v5-page-login", "v5-page-settings"],
        },
        files={
            "v5-page-home": {
                "frontend/src/pages/Home.tsx": "export function Home(){return 'home'}\n"
            },
            "v5-page-login": {
                "frontend/src/pages/Login.tsx": "export function Login(){return 'login'}\n"
            },
            "v5-page-settings": {
                "frontend/src/pages/Settings.tsx": "export function Settings(){return 'settings'}\n"
            },
        },
    )

    await _run_process_children(repo, monkeypatch, stub)

    for task_id, rel_path, content in [
        (
            "v5-page-home",
            "frontend/src/pages/Home.tsx",
            "export function Home(){return 'home'}\n",
        ),
        (
            "v5-page-login",
            "frontend/src/pages/Login.tsx",
            "export function Login(){return 'login'}\n",
        ),
        (
            "v5-page-settings",
            "frontend/src/pages/Settings.tsx",
            "export function Settings(){return 'settings'}\n",
        ),
    ]:
        _assert_file_reachable_from_main(
            repo,
            task_id=task_id,
            rel_path=rel_path,
            expected_content=content,
        )


@pytest.mark.asyncio
async def test_merge_blocked_grandchild_blocks_subtree_and_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    intents = {
        "v5-frontend": "Build frontend and decompose pages",
        "v5-feature-a": "Build login page first",
        "v5-feature-b": "Build conflicting login page",
        "v5-feature-c": "Build help page",
    }
    _seed_root(repo, ["v5-frontend"], intents)
    events: list[dict[str, Any]] = []
    stub = LeadStub(
        repo,
        intents=intents,
        children={"v5-frontend": ["v5-feature-a", "v5-feature-b", "v5-feature-c"]},
        files={
            "v5-feature-a": {
                "frontend/src/pages/Login.tsx": "export function Login(){return 'from A'}\n"
            },
            "v5-feature-b": {
                "frontend/src/pages/Login.tsx": "export function Login(){return 'from B'}\n"
            },
            "v5-feature-c": {
                "frontend/src/pages/Help.tsx": "export function Help(){return 'help'}\n"
            },
        },
        leaf_delays={
            "v5-feature-a": 0.0,
            "v5-feature-c": 0.01,
            "v5-feature-b": 0.05,
        },
    )

    await _run_process_children(
        repo,
        monkeypatch,
        stub,
        max_parallel=3,
        events=events,
    )

    blocked = get_task(repo, "v5-feature-b") or {}
    subtree = get_task(repo, "v5-frontend") or {}
    assert blocked.get("verdict") == "merge_blocked"
    assert subtree.get("verdict") == "merge_blocked"
    assert aggregate_verdict(repo, ROOT_TASK_ID) == "merge_blocked"
    # Post Phase 2b: the conflicting child surfaces at simulated-integration
    # merge time (ok=false), mirroring what the real integration Lead would
    # encounter when it runs `git merge i2p/build/<id>`.
    assert any(
        event.get("event") == "simulated_integration_merge"
        and event.get("task_id") == "v5-feature-b"
        and event.get("ok") is False
        for event in events
    )
    # Post Phase 2b: `subtree_propagated` may fire during `_process_children`
    # (before the simulated integration merge surfaces the conflict). In a
    # real run, the integration Lead would detect the conflict and either
    # resolve it or report merge_blocked from inside the integration session,
    # which prevents the final propagation to main. The simulator below
    # reflects that: the conflicting Login.tsx never reaches main.

    main_tree = _git(repo, "ls-tree", "-r", "--name-only", "main", check=True).stdout
    assert "frontend/src/pages/Login.tsx" not in main_tree


@pytest.mark.asyncio
async def test_mixed_shallow_and_deep_root_children_land_on_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    intents = {
        "v5-architect": "Build architecture scaffold",
        "v5-backend": "Build backend service",
        "v5-docs": "Build operator docs",
        "v5-frontend": "Build frontend and decompose pages",
        "v5-page-login": "Build login page",
        "v5-page-profile": "Build profile page",
        "v5-page-settings": "Build settings page",
    }
    _seed_root(
        repo,
        ["v5-architect", "v5-backend", "v5-docs", "v5-frontend"],
        intents,
    )
    stub = LeadStub(
        repo,
        intents=intents,
        children={"v5-frontend": ["v5-page-login", "v5-page-profile", "v5-page-settings"]},
        files={
            "v5-architect": {"docs/architecture.md": "architecture scaffold\n"},
            "v5-backend": {"backend/app.py": "def app():\n    return 'api'\n"},
            "v5-docs": {"docs/operator.md": "operator docs\n"},
            "v5-page-login": {
                "frontend/src/pages/Login.tsx": "export function Login(){return 'login'}\n"
            },
            "v5-page-profile": {
                "frontend/src/pages/Profile.tsx": "export function Profile(){return 'profile'}\n"
            },
            "v5-page-settings": {
                "frontend/src/pages/Settings.tsx": "export function Settings(){return 'settings'}\n"
            },
        },
    )

    await _run_process_children(repo, monkeypatch, stub)

    for task_id, rel_path, content in [
        ("v5-architect", "docs/architecture.md", "architecture scaffold\n"),
        ("v5-backend", "backend/app.py", "def app():\n    return 'api'\n"),
        ("v5-docs", "docs/operator.md", "operator docs\n"),
        (
            "v5-page-login",
            "frontend/src/pages/Login.tsx",
            "export function Login(){return 'login'}\n",
        ),
        (
            "v5-page-profile",
            "frontend/src/pages/Profile.tsx",
            "export function Profile(){return 'profile'}\n",
        ),
        (
            "v5-page-settings",
            "frontend/src/pages/Settings.tsx",
            "export function Settings(){return 'settings'}\n",
        ),
    ]:
        _assert_file_reachable_from_main(
            repo,
            task_id=task_id,
            rel_path=rel_path,
            expected_content=content,
        )


@pytest.mark.asyncio
async def test_all_passing_direct_child_branch_tips_reach_main_even_noop_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    intents = {
        "v5-architect": "Build scaffold",
        "v5-frontend": "Inspect frontend and make changes only if needed",
        "v5-tests": "Build tests",
    }
    record_task(repo, task_id=ROOT_TASK_ID, intent="root", parent_task_id=None)
    set_decomposition(repo, ROOT_TASK_ID, "emit")
    set_verdict(repo, ROOT_TASK_ID, "pending_children")
    _enqueue_fixed_task(
        repo,
        task_id="v5-architect",
        parent_task_id=ROOT_TASK_ID,
        intent=intents["v5-architect"],
    )
    _enqueue_fixed_task(
        repo,
        task_id="v5-frontend",
        parent_task_id=ROOT_TASK_ID,
        intent=intents["v5-frontend"],
        depends_on=["v5-architect"],
    )
    _enqueue_fixed_task(
        repo,
        task_id="v5-tests",
        parent_task_id=ROOT_TASK_ID,
        intent=intents["v5-tests"],
        depends_on=["v5-architect"],
    )
    events: list[dict[str, Any]] = []
    stub = LeadStub(
        repo,
        intents=intents,
        children={},
        files={
            "v5-architect": {"frontend/index.html": "<div>complete shell</div>\n"},
            "v5-tests": {"tests/run_acceptance.py": "print('ok')\n"},
            # v5-frontend intentionally writes nothing; its branch still must
            # be represented by a reachable tip, not silently disappear.
            "v5-frontend": {},
        },
    )

    await _run_process_children(repo, monkeypatch, stub, max_parallel=3, events=events)

    for child_id in ("v5-architect", "v5-frontend", "v5-tests"):
        _assert_branch_tip_reaches(repo, branch=child_branch_name(child_id))

    # Post Phase 2b (2026-05-21): integration Lead is the merge authority.
    # `_simulate_integration_merges` in the test helper mirrors what
    # integration's Step 1 does.
    landed_events = {
        event.get("task_id")
        for event in events
        if event.get("event") in {
            "child_branch_ancestry_ok",
            "simulated_integration_merge",
        }
        and event.get("target") == "main"
    }
    assert {"v5-architect", "v5-frontend", "v5-tests"} <= landed_events
