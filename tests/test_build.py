"""Tests for otto/build.py — slice dispatch + retry + scope enforcement.

Coverage:
- ready_groups: dep-aware readiness, exclusion of in-flight, exclusion of completed
- detect_scope_violations: write-scope, allow-create-anywhere, allow-shared-scaffold
- BuildBudget: per-slice retries, repair budget exhaustion
- run_build: happy path (one slice passes first try), retry-then-pass,
  retry-exhaustion-blocks, scope violation, dep-blocked downstream slice
- _build_agent_prompt: includes spec context + previous failure on retries

The build agent is mocked throughout; the LLM never runs.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from otto.build import (
    BuildAgentInput,
    BuildAgentOutput,
    BuildBudget,
    ContractDelta,
    GroupStatus,
    _build_agent_prompt,
    _commit_group_work,
    _write_build_context_packet,
    collect_critical_shared_contract_deltas,
    default_build_agent,
    detect_critical_shared_contract_violations,
    detect_dependency_scope_extensions,
    detect_scope_violations,
    ready_groups,
    run_build,
)
from otto.spec_compile import (
    Feature,
    RepoTestCheck,
    Group,
    PytestCheck,
    SharedContract,
    Spec,
    StateInvariant,
    StructureDecisions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(groups: list[Group]) -> Spec:
    return Spec(
        intent="test intent",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=groups,
    )


def _init_git(repo: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", ".gitkeep"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init", "--no-verify"], cwd=repo, check=True)


def _no_op_passing_check() -> RepoTestCheck:
    return RepoTestCheck(command=("python", "-c", "print('ok')"), timeout_s=10)


def _no_op_failing_check() -> RepoTestCheck:
    return RepoTestCheck(command=("python", "-c", "import sys; sys.exit(1)"), timeout_s=10)


# ---------------------------------------------------------------------------
# ready_groups
# ---------------------------------------------------------------------------


def test_ready_slices_returns_no_dep_slices_first() -> None:
    spec = _spec(
        [
            Group(id="s1", name="shell", dependencies=[], owned_paths=[], feature_ids=[], checks=[]),
            Group(id="s2", name="api", dependencies=["s1"], owned_paths=[], feature_ids=[], checks=[]),
        ]
    )
    assert [s.id for s in ready_groups(spec, completed_ids=set())] == ["s1"]


def test_ready_slices_unblocks_dependents_after_completion() -> None:
    spec = _spec(
        [
            Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[]),
            Group(id="s2", name="y", dependencies=["s1"], owned_paths=[], feature_ids=[], checks=[]),
            Group(id="s3", name="z", dependencies=["s1"], owned_paths=[], feature_ids=[], checks=[]),
        ]
    )
    ready = ready_groups(spec, completed_ids={"s1"})
    assert sorted(s.id for s in ready) == ["s2", "s3"]


def test_ready_slices_excludes_in_progress() -> None:
    spec = _spec(
        [
            Group(id="a", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[]),
            Group(id="b", name="y", dependencies=[], owned_paths=[], feature_ids=[], checks=[]),
        ]
    )
    ready = ready_groups(spec, completed_ids=set(), in_progress_ids={"a"})
    assert [s.id for s in ready] == ["b"]


# ---------------------------------------------------------------------------
# detect_scope_violations
# ---------------------------------------------------------------------------


def test_scope_violations_allows_own_paths() -> None:
    spec = _spec(
        [
            Group(
                id="s1",
                name="shell",
                dependencies=[],
                owned_paths=["app/main.py", "app/components/*"],
                feature_ids=[],
                checks=[],
            ),
        ]
    )
    s1 = spec.groups[0]
    violations = detect_scope_violations(s1, spec, ["app/main.py", "app/components/Nav.tsx"])
    assert violations == []


def test_scope_violations_flags_modifications_to_peer_slice_paths(tmp_path: Path) -> None:
    """Modifying a peer slice (no dep relation) IS a violation."""
    spec = _spec(
        [
            Group(id="s1", name="shell", dependencies=[], owned_paths=["app/main.py"], feature_ids=[], checks=[]),
            Group(id="s2", name="api", dependencies=[], owned_paths=["app/api.py"], feature_ids=[], checks=[]),
        ]
    )
    s2 = spec.groups[1]  # peer of s1, not a dep
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("# existing", encoding="utf-8")
    violations = detect_scope_violations(s2, spec, ["app/main.py"], project_root=tmp_path)
    assert violations == ["app/main.py"]


def test_scope_violations_allows_create_in_other_slice_glob(tmp_path: Path) -> None:
    """Newly created files are allowed even if they match another slice's glob."""
    spec = _spec(
        [
            Group(id="s1", name="shell", dependencies=[], owned_paths=["app/components/*"], feature_ids=[], checks=[]),
            Group(id="s2", name="api", dependencies=["s1"], owned_paths=["app/api/*"], feature_ids=[], checks=[]),
        ]
    )
    s2 = spec.groups[1]
    # path matches s1's glob but doesn't exist on disk → newly created → allowed
    violations = detect_scope_violations(
        s2, spec, ["app/components/PostCard.tsx"], project_root=tmp_path
    )
    assert violations == []


def test_scope_violations_allows_shared_scaffold() -> None:
    """Files matching no slice's owned_paths are shared scaffold and allowed."""
    spec = _spec(
        [
            Group(id="s1", name="shell", dependencies=[], owned_paths=["app/main.py"], feature_ids=[], checks=[]),
            Group(id="s2", name="api", dependencies=["s1"], owned_paths=["app/api/*"], feature_ids=[], checks=[]),
        ]
    )
    s2 = spec.groups[1]
    violations = detect_scope_violations(s2, spec, ["package.json", "tsconfig.json"])
    assert violations == []


def test_scope_violations_warns_on_existing_unowned_file(tmp_path: Path) -> None:
    """Existing config files must be declared own/shared/dependency scope."""
    spec = _spec(
        [
            Group(id="s1", name="shell", dependencies=[], owned_paths=["app.py"], feature_ids=[], checks=[]),
        ]
    )
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")

    violations = detect_scope_violations(
        spec.groups[0],
        spec,
        ["pyproject.toml"],
        project_root=tmp_path,
    )

    assert violations == ["pyproject.toml"]


def test_scope_violations_ignore_common_generated_artifacts(tmp_path: Path) -> None:
    """Generated caches are not product write-scope evidence."""
    spec = _spec(
        [
            Group(id="s1", name="shell", dependencies=[], owned_paths=["app.py"], feature_ids=[], checks=[]),
        ]
    )
    cache_dir = tmp_path / "pkg" / "__pycache__"
    cache_dir.mkdir(parents=True)
    (cache_dir / "app.cpython-314.pyc").write_bytes(b"cache")

    violations = detect_scope_violations(
        spec.groups[0],
        spec,
        ["pkg/__pycache__/app.cpython-314.pyc", "tests/__pycache__/test_app.pyc"],
        project_root=tmp_path,
    )

    assert violations == []


def test_scope_violations_allows_new_unowned_file(tmp_path: Path) -> None:
    """New supporting files remain allowed as implicit shared scaffold."""
    spec = _spec(
        [
            Group(id="s1", name="shell", dependencies=[], owned_paths=["app.py"], feature_ids=[], checks=[]),
        ]
    )

    violations = detect_scope_violations(
        spec.groups[0],
        spec,
        ["README.md"],
        project_root=tmp_path,
    )

    assert violations == []


def test_scope_violations_allows_shared_scaffold_extension(tmp_path: Path) -> None:
    """Microfeed bench learning: foundational files like models.py must be
    extendable by multiple slices. shared_scaffold declares this; the rule
    must honor it."""
    spec = Spec(
        intent="webapp",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[
            Group(id="s1", name="shell", dependencies=[], owned_paths=["routes/auth.py"], feature_ids=[], checks=[]),
            Group(id="s2", name="api", dependencies=["s1"], owned_paths=["routes/api.py"], feature_ids=[], checks=[]),
        ],
        shared_scaffold=["models.py", "app.py", "database.py"],
    )
    s2 = spec.groups[1]
    # Pre-existing shared scaffold file; s2 extends it (would normally be
    # a "modify" violation if any slice owned it).
    (tmp_path / "models.py").write_text("# existing\n", encoding="utf-8")
    violations = detect_scope_violations(s2, spec, ["models.py"], project_root=tmp_path)
    assert violations == []


def test_scope_violations_shared_scaffold_globs_match(tmp_path: Path) -> None:
    """shared_scaffold may use globs (e.g. 'app/__init__.py' or 'config/*.py')."""
    spec = Spec(
        intent="webapp",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        groups=[
            Group(id="s1", name="x", dependencies=[], owned_paths=["routes/x.py"], feature_ids=[], checks=[]),
        ],
        shared_scaffold=["config/*.py", "app/__init__.py"],
    )
    s1 = spec.groups[0]
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.py").write_text("# ex\n", encoding="utf-8")
    violations = detect_scope_violations(
        s1, spec, ["config/settings.py"], project_root=tmp_path,
    )
    assert violations == []


def test_scope_violations_allows_transitive_dep_modification(tmp_path: Path) -> None:
    """A slice may modify any file owned by a slice in its transitive deps.

    Microfeed bench learning: downstream feature slices need to extend
    foundation slice's files (User model in models.py, blueprint
    registration in app.py). The dep relation already declares this
    "extends" relationship — the rule should respect it.
    """
    spec = _spec(
        [
            Group(id="shell", name="x", dependencies=[],
                  owned_paths=["app.py", "models.py"], feature_ids=[], checks=[]),
            Group(id="auth", name="y", dependencies=["shell"],
                  owned_paths=["routes/auth.py"], feature_ids=[], checks=[]),
        ]
    )
    auth = spec.groups[1]
    # Pre-existing files owned by shell; auth (dependencies=[shell]) extends them.
    (tmp_path / "app.py").write_text("# shell\n", encoding="utf-8")
    (tmp_path / "models.py").write_text("# shell\n", encoding="utf-8")
    violations = detect_scope_violations(
        auth, spec, ["app.py", "models.py"], project_root=tmp_path,
    )
    assert violations == []


def test_dep_owned_modifications_are_reported_as_extensions() -> None:
    """Allowed dep-owned edits should still be visible to operators."""
    spec = _spec(
        [
            Group(id="cli_scaffold", name="scaffold", owned_paths=["todo.py"]),
            Group(
                id="task_lifecycle",
                name="lifecycle",
                dependencies=["cli_scaffold"],
                owned_paths=["tasks.json"],
            ),
        ]
    )
    lifecycle = spec.groups[1]

    assert detect_scope_violations(lifecycle, spec, ["todo.py", "tasks.json"]) == []
    assert detect_dependency_scope_extensions(
        lifecycle, spec, ["todo.py", "tasks.json"]
    ) == ["todo.py"]


def test_scope_violations_blocks_peer_slice_modification(tmp_path: Path) -> None:
    """A slice may NOT modify a peer slice's owned files (no dep relation)."""
    spec = _spec(
        [
            Group(id="shell", name="x", dependencies=[],
                  owned_paths=["app.py"], feature_ids=[], checks=[]),
            Group(id="posts", name="p", dependencies=["shell"],
                  owned_paths=["routes/posts.py"], feature_ids=[], checks=[]),
            Group(id="search", name="s", dependencies=["shell"],
                  owned_paths=["routes/search.py"], feature_ids=[], checks=[]),
        ]
    )
    search = spec.groups[2]
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes" / "posts.py").write_text("# posts\n", encoding="utf-8")
    # search wants to modify posts' file — should be blocked.
    violations = detect_scope_violations(
        search, spec, ["routes/posts.py"], project_root=tmp_path,
    )
    assert violations == ["routes/posts.py"]


def test_scope_violations_transitive_dep_chain(tmp_path: Path) -> None:
    """Dep transitivity: posts (dependencies=auth) (dependencies=shell) can modify shell's files."""
    spec = _spec(
        [
            Group(id="shell", name="x", dependencies=[],
                  owned_paths=["app.py"], feature_ids=[], checks=[]),
            Group(id="auth", name="y", dependencies=["shell"],
                  owned_paths=["routes/auth.py"], feature_ids=[], checks=[]),
            Group(id="posts", name="p", dependencies=["auth"],
                  owned_paths=["routes/posts.py"], feature_ids=[], checks=[]),
        ]
    )
    posts = spec.groups[2]
    (tmp_path / "app.py").write_text("# shell\n", encoding="utf-8")
    violations = detect_scope_violations(
        posts, spec, ["app.py"], project_root=tmp_path,
    )
    assert violations == []


def test_scope_violations_recursive_glob() -> None:
    spec = _spec(
        [
            Group(id="s1", name="shell", dependencies=[], owned_paths=["app/components/**"], feature_ids=[], checks=[]),
        ]
    )
    s1 = spec.groups[0]
    # Both top-level and nested under app/components/ should match.
    violations = detect_scope_violations(s1, spec, ["app/components/Nav.tsx", "app/components/inner/Inner.tsx"])
    assert violations == []


# ---------------------------------------------------------------------------
# BuildBudget
# ---------------------------------------------------------------------------


def test_build_budget_repair_charge_and_remaining() -> None:
    budget = BuildBudget(total_repair_s=100)
    assert budget.remaining_repair_s() == 100
    budget.charge_repair(40)
    assert budget.remaining_repair_s() == 60
    budget.charge_repair(70)  # over-charge clamps at 0
    assert budget.remaining_repair_s() == 0


def test_build_budget_cost_charge_and_remaining() -> None:
    """v2 phase 2: total cost is a primary bound, not a count."""
    budget = BuildBudget(total_cost_usd=10.0)
    assert budget.remaining_total_cost_usd() == 10.0
    budget.charge_cost(3.0)
    assert budget.remaining_total_cost_usd() == 7.0
    budget.charge_cost(8.0)
    assert budget.remaining_total_cost_usd() == 0.0


# ---------------------------------------------------------------------------
# run_build — happy path
# ---------------------------------------------------------------------------


def _run_build_sync(*args, **kwargs):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(run_build(*args, **kwargs))


def test_run_build_single_slice_passing_first_try(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    spec = _spec(
        [
            Group(
                id="s1",
                name="hello",
                dependencies=[],
                owned_paths=[],
                feature_ids=["print hello"],
                checks=[_no_op_passing_check()],
            ),
        ]
    )

    async def fake_agent(_input: BuildAgentInput) -> BuildAgentOutput:
        return BuildAgentOutput(succeeded=True, cost_usd=0.01, wall_s=0.5, detail="done")

    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=fake_agent,
        )
    )
    assert result.all_passing
    assert result.passing_ids == ["s1"]
    assert result.group_results[0].attempts == 1


def test_run_build_dep_aware_dispatch(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = _spec(
        [
            Group(id="s1", name="a", dependencies=[], owned_paths=[], feature_ids=[], checks=[_no_op_passing_check()]),
            Group(id="s2", name="b", dependencies=["s1"], owned_paths=[], feature_ids=[], checks=[_no_op_passing_check()]),
            Group(id="s3", name="c", dependencies=["s2"], owned_paths=[], feature_ids=[], checks=[_no_op_passing_check()]),
        ]
    )

    order: list[str] = []

    async def fake_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        order.append(input_.group.id)
        return BuildAgentOutput(succeeded=True)

    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=fake_agent,
        )
    )
    assert order == ["s1", "s2", "s3"]
    assert result.all_passing


# ---------------------------------------------------------------------------
# run_build — retry path
# ---------------------------------------------------------------------------


def test_run_build_retries_on_failing_check_then_passes(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    pass_after = {"counter": 0}

    # Group with a state invariant that fails until the agent has
    # "succeeded" 2 times (so attempt 3 passes).
    inv = StateInvariant(
        description="needs counter at >= 3",
        expression="exists('marker.txt') and read_text('marker.txt').count('x') >= 3",
    )

    async def fake_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        pass_after["counter"] += 1
        # Append "x" to marker.txt each attempt; on attempt 3, total == 3
        marker = input_.worktree / "marker.txt"
        existing = marker.read_text(encoding="utf-8") if marker.exists() else ""
        marker.write_text(existing + "x", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, detail=f"attempt={input_.attempt}")

    spec = _spec(
        [
            Group(id="s1", name="x", dependencies=[], owned_paths=["marker.txt"], feature_ids=[], checks=[inv]),
        ]
    )
    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=fake_agent,
        )
    )
    assert result.all_passing
    assert result.group_results[0].attempts == 3


def test_run_build_reuses_agent_session_between_retries(tmp_path: Path) -> None:
    """A9: retry attempts should resume the provider session returned by attempt 1."""
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    seen_sessions: list[str] = []

    async def fake_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        seen_sessions.append(input_.agent_session_id)
        if input_.attempt == 1:
            return BuildAgentOutput(
                succeeded=False,
                detail="try again",
                session_id="provider-session-1",
            )
        assert input_.agent_session_id == "provider-session-1"
        return BuildAgentOutput(succeeded=True, session_id="provider-session-2")

    spec = _spec(
        [
            Group(
                id="s1",
                name="x",
                dependencies=[],
                owned_paths=[],
                feature_ids=[],
                checks=[],
            ),
        ]
    )
    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=fake_agent,
        )
    )
    assert result.all_passing
    assert seen_sessions == ["", "provider-session-1"]


def test_detect_critical_shared_contract_violations() -> None:
    foundation = Group(id="foundation", name="Foundation", owned_paths=["src/lib/store.ts"])
    feature = Group(id="transactions", name="Transactions", dependencies=["foundation"])
    spec = Spec(
        intent="finance",
        groups=[foundation, feature],
        shared_contracts=[
            SharedContract(
                id="store",
                name="Store",
                owner_id="foundation",
                paths=["src/lib/store.*"],
            )
        ],
    )

    violations = detect_critical_shared_contract_violations(
        feature,
        spec,
        ["src/lib/store.ts"],
    )

    assert violations == ["src/lib/store.ts (shared_contract=store, owner=foundation)"]


def test_detect_critical_shared_contract_allows_declared_extensions() -> None:
    foundation = Group(id="foundation", name="Foundation")
    feature = Group(id="transactions", name="Transactions", dependencies=["foundation"])
    spec = Spec(
        intent="finance",
        groups=[foundation, feature],
        shared_contracts=[
            SharedContract(
                id="browser-runner",
                name="Browser runner",
                kind="test_runner",
                owner_id="foundation",
                paths=["tests/run_browser_journey.py", "tests/browser/**"],
                allowed_extension_paths=[
                    "tests/browser/test_*.py",
                    "tests/browser/test_*.playwright.ts",
                ],
            )
        ],
    )

    violations = detect_critical_shared_contract_violations(
        feature,
        spec,
        [
            "tests/browser/test_transactions.py",
            "tests/browser/test_transactions.playwright.ts",
            "tests/run_browser_journey.py",
        ],
    )

    assert violations == [
        "tests/run_browser_journey.py (shared_contract=browser-runner, owner=foundation)"
    ]


def test_collect_critical_shared_contract_deltas_groups_paths_by_contract() -> None:
    foundation = Group(id="foundation", name="Foundation")
    feature = Group(id="transactions", name="Transactions", dependencies=["foundation"])
    spec = Spec(
        intent="finance",
        groups=[foundation, feature],
        shared_contracts=[
            SharedContract(
                id="store",
                name="Store",
                owner_id="foundation",
                paths=["src/lib/store.*"],
                invariants=["transactions survive refresh"],
                extension_policy="feature groups may call store APIs",
            )
        ],
    )

    deltas = collect_critical_shared_contract_deltas(
        feature,
        spec,
        ["src/lib/store.ts", "src/lib/store.test.ts"],
    )

    assert len(deltas) == 1
    assert deltas[0].to_dict() == {
        "group_id": "transactions",
        "contract_id": "store",
        "owner_id": "foundation",
        "paths": ["src/lib/store.ts", "src/lib/store.test.ts"],
        "invariants": ["transactions survive refresh"],
        "extension_policy": "feature groups may call store APIs",
    }


def test_collect_critical_shared_contract_deltas_does_not_overmatch_feature_subtrees() -> None:
    foundation = Group(id="foundation", name="Foundation")
    feature = Group(id="budgets", name="Budgets", dependencies=["foundation"])
    spec = Spec(
        intent="finance",
        groups=[foundation, feature],
        shared_contracts=[
            SharedContract(
                id="app-shell",
                name="App shell",
                owner_id="foundation",
                paths=["src/features/featureRegistry.*", "src/features/types.*"],
                allowed_extension_paths=["src/features/*/**"],
            )
        ],
    )

    deltas = collect_critical_shared_contract_deltas(
        feature,
        spec,
        ["src/features/featureRegistry.tsx", "src/features/budgets/panel.tsx"],
    )

    assert len(deltas) == 1
    assert deltas[0].contract_id == "app-shell"
    assert deltas[0].paths == ["src/features/featureRegistry.tsx"]


def test_run_build_records_contract_delta_without_blocking(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    (tmp_path / "src" / "lib").mkdir(parents=True)
    (tmp_path / "src" / "lib" / "store.ts").write_text(
        "export const version = 1;\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/lib/store.ts"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "store", "--no-verify"],
        cwd=tmp_path,
        check=True,
    )
    spec = _spec(
        [
            Group(id="foundation", name="Foundation", owned_paths=["src/lib/store.ts"]),
            Group(
                id="transactions",
                name="Transactions",
                dependencies=["foundation"],
                owned_paths=["src/routes/Transactions.tsx"],
                feature_ids=["transactions"],
                checks=[_no_op_passing_check()],
            ),
        ]
    )
    spec.shared_contracts = [
        SharedContract(
            id="finance-store",
            name="Finance store",
            owner_id="foundation",
            paths=["src/lib/store.*"],
            invariants=["transactions survive refresh"],
        )
    ]

    async def agent(input_: BuildAgentInput) -> BuildAgentOutput:
        if input_.group.id == "transactions":
            (input_.worktree / "src" / "lib" / "store.ts").write_text(
                "export const version = 2;\n",
                encoding="utf-8",
            )
        return BuildAgentOutput(succeeded=True)

    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=agent,
        )
    )

    tx = next(r for r in result.group_results if r.group_id == "transactions")
    assert tx.status == GroupStatus.PASSING
    assert tx.contract_deltas
    assert tx.contract_deltas[0].contract_id == "finance-store"
    assert tx.contract_deltas[0].paths == ["src/lib/store.ts"]
    events = [
        json.loads(line)
        for line in (session_dir / "spec-state.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(event["kind"] == "contract.delta" for event in events)


def test_run_build_emits_check_feedback_for_same_thread_repair(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    seen_last_failures: list[str] = []

    flag = tmp_path / "pass.flag"
    check = RepoTestCheck(
        command=(
            "python",
            "-c",
            "from pathlib import Path; import sys; sys.exit(0 if Path('pass.flag').exists() else 3)",
        ),
        timeout_s=10,
    )

    async def fixing_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        seen_last_failures.append(input_.last_failure_narrative)
        if input_.attempt == 2:
            flag.write_text("ok", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, session_id="provider-thread")

    spec = _spec([
        Group(id="s1", name="x", owned_paths=["pass.flag"], feature_ids=["feature one"], checks=[check])
    ])

    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=fixing_agent,
        )
    )

    journal = (session_dir / "spec-state.jsonl").read_text(encoding="utf-8")
    assert result.all_passing
    assert "group.check.feedback" in journal
    assert "Authoritative Otto check-runner evidence follows" in seen_last_failures[1]


def test_run_build_uses_resume_agent_session_from_prior_run(tmp_path: Path) -> None:
    """A resumed outer run should seed the first attempt with the prior thread."""
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    seen_sessions: list[str] = []

    async def fake_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        seen_sessions.append(input_.agent_session_id)
        return BuildAgentOutput(
            succeeded=True,
            detail="ok",
            session_id="provider-session-next",
        )

    spec = _spec(
        [
            Group(
                id="s1",
                name="x",
                dependencies=[],
                owned_paths=[],
                feature_ids=[],
                checks=[],
            ),
        ]
    )
    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=fake_agent,
            resume_agent_sessions={"s1": "provider-session-prior"},
        )
    )

    assert result.all_passing
    assert seen_sessions == ["provider-session-prior"]


def test_default_build_agent_passes_resume_session_to_provider(tmp_path: Path, monkeypatch) -> None:
    """A9: default_build_agent must set AgentOptions.resume from BuildAgentInput."""
    from otto.agent import AgentOptions

    group = Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[])
    spec = _spec([group])
    seen: dict[str, str] = {}

    def fake_make_options(*_args, **_kwargs) -> AgentOptions:
        return AgentOptions()

    async def fake_run_agent(_prompt, options, **_kwargs):
        seen["resume"] = options.resume or ""
        return "done", 0.03, "provider-session-2", {}

    monkeypatch.setattr("otto.agent.make_agent_options", fake_make_options)
    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_run_agent)

    output = asyncio.run(
        default_build_agent(
            BuildAgentInput(
                spec=spec,
                group=group,
                project_dir=tmp_path,
                worktree=tmp_path,
                branch="i2p/test/s1",
                attempt=2,
                agent_session_id="provider-session-1",
            )
        )
    )
    assert output.succeeded is True
    assert output.session_id == "provider-session-2"
    assert seen["resume"] == "provider-session-1"


def test_default_build_agent_preserves_provider_failure_continuity(
    tmp_path: Path, monkeypatch
) -> None:
    from otto.agent import AgentCallError, AgentOptions

    group = Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[])
    spec = _spec([group])

    def fake_make_options(*_args, **_kwargs) -> AgentOptions:
        return AgentOptions()

    async def fake_run_agent(_prompt, _options, **_kwargs):
        raise AgentCallError(
            "app-server stream ended",
            session_id="provider-session-error",
            total_cost_usd=0.07,
            crash_path=str(tmp_path / "crash.json"),
        )

    monkeypatch.setattr("otto.agent.make_agent_options", fake_make_options)
    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_run_agent)

    output = asyncio.run(
        default_build_agent(
            BuildAgentInput(
                spec=spec,
                group=group,
                project_dir=tmp_path,
                worktree=tmp_path,
                branch="i2p/test/s1",
                attempt=1,
            )
        )
    )

    assert output.succeeded is False
    assert output.session_id == "provider-session-error"
    assert output.cost_usd == 0.07
    assert "crash details:" in output.detail


def test_default_build_agent_uses_input_config_for_provider_options(
    tmp_path: Path, monkeypatch
) -> None:
    """CLI/runtime provider overrides must reach spawned build agents."""
    from otto.agent import AgentOptions

    group = Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[])
    spec = _spec([group])
    seen: dict[str, object] = {}

    def fake_make_options(_project_dir, config, *, agent_type, **_kwargs) -> AgentOptions:
        seen["config"] = dict(config)
        seen["agent_type"] = agent_type
        return AgentOptions()

    async def fake_run_agent(_prompt, options, **_kwargs):
        return "done", 0.0, "provider-session", {}

    monkeypatch.setattr("otto.agent.make_agent_options", fake_make_options)
    monkeypatch.setattr("otto.agent.run_agent_with_timeout", fake_run_agent)

    asyncio.run(
        default_build_agent(
            BuildAgentInput(
                spec=spec,
                group=group,
                project_dir=tmp_path,
                worktree=tmp_path,
                branch="i2p/test/s1",
                attempt=1,
                config={"provider": "codex", "_cli_overrides": {"provider": "codex"}},
            )
        )
    )

    assert seen["agent_type"] == "build"
    assert seen["config"] == {
        "provider": "codex",
        "_cli_overrides": {"provider": "codex"},
    }


def test_run_build_blocks_after_retry_exhaustion(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    spec = _spec(
        [
            Group(
                id="s1",
                name="hopeless",
                dependencies=[],
                owned_paths=[],
                feature_ids=[],
                checks=[_no_op_failing_check()],
            ),
        ]
    )

    async def fake_agent(_input: BuildAgentInput) -> BuildAgentOutput:
        return BuildAgentOutput(succeeded=True)

    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=fake_agent,
            budget=BuildBudget(per_slice_retries_hard_cap=8),
        )
    )
    r = result.group_results[0]
    assert r.status == GroupStatus.BLOCKED
    # v2 phase 2: bound by progress. Two consecutive identical failures
    # (after the first) trigger the no-progress bound, so attempts==2
    # by the time we exit. (Hard cap is 8 but rarely fires in practice.)
    assert r.attempts == 2
    assert "no progress" in r.failure_narrative or "checks failed" in r.failure_narrative


def test_run_build_propagates_block_to_dependent_slice(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    spec = _spec(
        [
            Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[_no_op_failing_check()]),
            Group(id="s2", name="y", dependencies=["s1"], owned_paths=[], feature_ids=[], checks=[_no_op_passing_check()]),
        ]
    )

    async def fake_agent(_input: BuildAgentInput) -> BuildAgentOutput:
        return BuildAgentOutput(succeeded=True)

    # Use a tiny cost ceiling so the slice fails fast on its first attempt.
    # The dep-block propagation logic doesn't depend on the failure
    # mechanism — only that s1 ended in BLOCKED.
    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=fake_agent,
            budget=BuildBudget(per_slice_cost_usd=0.0, total_cost_usd=0.0),
        )
    )
    by_id = {r.group_id: r for r in result.group_results}
    assert by_id["s1"].status == GroupStatus.BLOCKED
    assert by_id["s2"].status == GroupStatus.BLOCKED
    assert by_id["s2"].failure_narrative == "dep blocked"


# ---------------------------------------------------------------------------
# run_build — scope violation
# ---------------------------------------------------------------------------


def test_run_build_flags_scope_violation(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    # Pre-existing file owned by s1
    (tmp_path / "app").mkdir()
    s1_file = tmp_path / "app" / "main.py"
    s1_file.write_text("# s1\n", encoding="utf-8")
    subprocess.run(["git", "add", "app/main.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add s1 file", "--no-verify"], cwd=tmp_path, check=True)

    spec = _spec(
        [
            Group(id="s1", name="shell", dependencies=[], owned_paths=["app/main.py"], feature_ids=[], checks=[]),
            Group(
                id="s2",
                name="naughty-peer",
                dependencies=[],  # peer of s1, NOT a dep — so it can't modify s1's files
                owned_paths=["app/api/*"],
                feature_ids=[],
                checks=[_no_op_passing_check()],
            ),
        ]
    )

    # s2's agent illegally modifies s1's file (peer slice, no dep).
    async def fake_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        if input_.group.id == "s2":
            (input_.worktree / "app" / "main.py").write_text("# tampered\n", encoding="utf-8")
        return BuildAgentOutput(succeeded=True)

    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=fake_agent,
        )
    )
    by_id = {r.group_id: r for r in result.group_results}
    # Soft-warning model: scope crossing does NOT block the slice. Both
    # slices still PASS (each has a no-op-passing check), and the
    # warning is captured on s2's GroupResult for the proof packet.
    assert by_id["s1"].status == GroupStatus.PASSING
    assert by_id["s2"].status == GroupStatus.PASSING
    assert "app/main.py" in by_id["s2"].scope_warnings


def test_run_build_ignores_otto_runtime_paths_in_scope_warnings(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "otto_logs" / "sessions" / "scope-run"
    session_dir.mkdir(parents=True)

    spec = _spec(
        [
            Group(
                id="s1",
                name="feature",
                dependencies=[],
                owned_paths=["app.py"],
                feature_ids=[],
                checks=[_no_op_passing_check()],
            ),
        ]
    )

    async def fake_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        (input_.worktree / "app.py").write_text("print('ok')\n", encoding="utf-8")
        return BuildAgentOutput(succeeded=True)

    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=fake_agent,
        )
    )

    s1_result = next(r for r in result.group_results if r.group_id == "s1")
    assert s1_result.scope_warnings == []
    journal = (session_dir / "spec-state.jsonl").read_text(encoding="utf-8")
    assert '"kind": "scope.warning"' not in journal


def test_run_build_warns_on_dep_owned_extension(tmp_path: Path) -> None:
    """Downstream edits to dep-owned files pass but are surfaced."""
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    spec = _spec(
        [
            Group(
                id="cli_scaffold",
                name="CLI scaffold",
                owned_paths=["todo.py"],
                checks=[_no_op_passing_check()],
            ),
            Group(
                id="task_lifecycle",
                name="Task lifecycle",
                dependencies=["cli_scaffold"],
                owned_paths=["tasks.json"],
                checks=[_no_op_passing_check()],
            ),
        ]
    )

    async def fake_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        todo = input_.worktree / "todo.py"
        if input_.group.id == "cli_scaffold":
            todo.write_text("def main():\n    return 0\n", encoding="utf-8")
        else:
            todo.write_text(
                todo.read_text(encoding="utf-8") + "\n# lifecycle handlers\n",
                encoding="utf-8",
            )
        return BuildAgentOutput(succeeded=True)

    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=fake_agent,
        )
    )

    by_id = {r.group_id: r for r in result.group_results}
    assert by_id["task_lifecycle"].status == GroupStatus.PASSING
    assert "todo.py" in by_id["task_lifecycle"].scope_warnings
    journal = (session_dir / "spec-state.jsonl").read_text(encoding="utf-8")
    assert '"kind": "scope.warning"' in journal
    assert "todo.py" in journal


# ---------------------------------------------------------------------------
# run_build — agent crash
# ---------------------------------------------------------------------------


def test_run_build_handles_agent_crash_with_retry(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    counter = {"n": 0}

    async def crashing_agent(_input: BuildAgentInput) -> BuildAgentOutput:
        counter["n"] += 1
        if counter["n"] == 1:
            raise RuntimeError("simulated agent crash")
        return BuildAgentOutput(succeeded=True)

    spec = _spec(
        [
            Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[_no_op_passing_check()]),
        ]
    )
    result = asyncio.run(
        run_build(
            spec,
            project_dir=tmp_path,
            session_dir=session_dir,
            build_agent=crashing_agent,
        )
    )
    assert result.all_passing
    assert result.group_results[0].attempts == 2


# ---------------------------------------------------------------------------
# Prompt composition
# ---------------------------------------------------------------------------


def test_build_agent_prompt_writeable_paths_only(tmp_path: Path) -> None:
    """Phase 1A simplification: prompt lists ONLY the paths the slice may
    write — its own owned_paths, transitive deps, and shared_scaffold.
    Peer-owned paths are NOT enumerated (they were dead text the agent
    treated as 'fine, just a warning'). Soft-warning behavior remains
    in the runtime; the prompt no longer recites it.
    """
    s_self = Group(id="auth", name="Auth", dependencies=["shell"],
                   owned_paths=["routes/auth.py"], feature_ids=[], checks=[])
    s_shell = Group(id="shell", name="Shell", dependencies=[],
                    owned_paths=["app.py"], feature_ids=[], checks=[])
    s_peer = Group(id="search", name="Search", dependencies=["shell"],
                   owned_paths=["routes/search.py", "templates/search.html"],
                   feature_ids=[], checks=[])
    spec = _spec([s_shell, s_self, s_peer])
    inp = BuildAgentInput(
        spec=spec, group=s_self, project_dir=tmp_path, worktree=tmp_path,
        branch="x", attempt=1,
    )
    prompt = _build_agent_prompt(inp)
    # Writable paths are enumerated.
    assert "routes/auth.py" in prompt
    assert "app.py" in prompt
    # Hard-forbid language never appears.
    assert "FORBIDDEN" not in prompt
    # Peer's exclusive paths NOT enumerated (no longer ceremony).
    assert "routes/search.py" not in prompt
    assert "templates/search.html" not in prompt
    # The "stay in lane" guidance survives in some form.
    assert (
        "don't pre-build" in prompt
        or "build only" in prompt.lower()
        or "another slice will deliver" in prompt.lower()
        or "do not implement features that belong to other slices" in prompt.lower()
    )


def test_build_agent_prompt_allows_explicit_shared_entrypoint_edits(tmp_path: Path) -> None:
    """Brownfield apps often need a small route/bootstrap edit.

    If the compiler put an entry point in shared_scaffold, the prompt
    should not contradict itself with blanket read-only language.
    """
    group = Group(
        id="dashboard",
        name="Dashboard",
        owned_paths=["templates/dashboard.html"],
        feature_ids=["pass SLA summary to dashboard template"],
        checks=[],
    )
    spec = _spec([group])
    spec.shared_scaffold = ["app.py"]
    inp = BuildAgentInput(
        spec=spec,
        group=group,
        project_dir=tmp_path,
        worktree=tmp_path,
        branch="x",
        attempt=1,
    )

    prompt = _build_agent_prompt(inp)

    assert "**Shared scaffold (any slice may extend):**" in prompt
    assert "`app.py`" in prompt
    assert "If one is listed under **Yours** or **Shared scaffold**" in prompt
    assert "may make the smallest necessary edit" in prompt
    assert "DO NOT modify them" not in prompt


def test_build_agent_prompt_lists_shared_paths_as_writable_contracts(tmp_path: Path) -> None:
    group = Group(
        id="ledger",
        name="Ledger",
        owned_paths=["src/ledger.ts"],
        feature_ids=["add ledger view"],
        checks=[],
    )
    spec = _spec([group])
    spec.shared_paths = ["src/store/**", "playwright.config.*"]
    inp = BuildAgentInput(
        spec=spec,
        group=group,
        project_dir=tmp_path,
        worktree=tmp_path,
        branch="x",
        attempt=1,
    )

    prompt = _build_agent_prompt(inp)

    assert "**Shared paths (any slice may extend compatibly):**" in prompt
    assert "`src/store/**`" in prompt
    assert "`playwright.config.*`" in prompt
    assert "(No declared paths" not in prompt


def test_build_agent_prompt_steers_dep_owned_entrypoints_to_registration_points(
    tmp_path: Path,
) -> None:
    """Dep-owned entry points remain merge hot spots.

    The prompt should ask downstream slices to use a registration seam or
    amendment instead of freely editing a transitive dependency entry point.
    """
    shell = Group(
        id="shell",
        name="Shell",
        owned_paths=["app.py"],
        feature_ids=[],
        checks=[],
    )
    widget = Group(
        id="widget",
        name="Widget",
        dependencies=["shell"],
        owned_paths=["templates/widget.html"],
        feature_ids=["register widget route"],
        checks=[],
    )
    spec = _spec([shell, widget])
    inp = BuildAgentInput(
        spec=spec,
        group=widget,
        project_dir=tmp_path,
        worktree=tmp_path,
        branch="x",
        attempt=1,
    )

    prompt = _build_agent_prompt(inp)

    assert "`app.py` (owned by `shell`)" in prompt
    assert "appears only through **Dep-owned**" in prompt
    assert "not blanket permission to rewrite shared product contracts" in prompt
    assert "request an amendment or owner change" in prompt
    assert "prefer the dependency's registration point" in prompt
    assert "request an amendment via `.otto/amendment_request.json`" in prompt


def test_build_agent_prompt_contains_required_context(tmp_path: Path) -> None:
    s = Group(
        id="s1",
        name="Auth flow",
        dependencies=["base"],
        owned_paths=["app/auth/*"],
        feature_ids=["build login form", "wire to API"],
        checks=[_no_op_passing_check()],
    )
    spec = _spec([s])
    spec.intent = "social network MVP"
    inp = BuildAgentInput(
        spec=spec,
        group=s,
        project_dir=tmp_path,
        worktree=tmp_path,
        branch="i2p/x/s1",
        attempt=1,
    )
    prompt = _build_agent_prompt(inp)
    assert "s1" in prompt
    assert "Auth flow" in prompt
    assert "build login form" in prompt
    assert "wire to API" in prompt
    assert "app/auth/*" in prompt
    assert "social network MVP" in prompt
    assert "Previous attempt failed" not in prompt


def test_build_context_packet_keeps_full_structure_available_without_prompt_dump(
    tmp_path: Path,
) -> None:
    s = Group(
        id="reports",
        name="Reports",
        owned_paths=["app/reports/*"],
        feature_ids=["f-reports"],
        checks=[_no_op_passing_check()],
    )
    spec = _spec([s])
    spec.structure = StructureDecisions(
        payload={
            "huge_contract": "X" * 5000,
            "api_shape": {"field": "manager_sla_age_days"},
        }
    )
    spec.features = [
        Feature(
            id="f-reports",
            name="Manager SLA report",
            description="Show aged approvals",
            group_id="reports",
        )
    ]
    packet_path = tmp_path / "session" / "build" / "reports" / "attempt-01" / "context-packet.json"
    full_spec_path = tmp_path / "session" / "spec" / "spec.json"
    inp = BuildAgentInput(
        spec=spec,
        group=s,
        project_dir=tmp_path,
        worktree=tmp_path,
        branch="i2p/x/reports",
        attempt=1,
        context_packet_path=packet_path,
        full_spec_path=full_spec_path,
    )

    _write_build_context_packet(inp, packet_path)
    prompt = _build_agent_prompt(inp)
    packet = json.loads(packet_path.read_text(encoding="utf-8"))

    assert packet["full_spec_path"] == str(full_spec_path)
    assert packet["structure"]["payload"]["huge_contract"] == "X" * 5000
    assert packet["features_for_group"][0]["id"] == "f-reports"
    assert str(packet_path) in prompt
    assert str(full_spec_path) in prompt
    assert "huge_contract" in prompt
    assert "X" * 1000 not in prompt
    assert "Do not search user/Codex memory" in prompt
    assert "Do not search or read user/Codex/agent memory" in prompt


def test_build_agent_prompt_includes_last_failure_on_retry(tmp_path: Path) -> None:
    s = Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[])
    spec = _spec([s])
    inp = BuildAgentInput(
        spec=spec,
        group=s,
        project_dir=tmp_path,
        worktree=tmp_path,
        branch="x",
        attempt=2,
        last_failure_narrative="check 3 failed: missing route /api/posts",
    )
    prompt = _build_agent_prompt(inp)
    assert "Previous attempt failed" in prompt
    assert "missing route /api/posts" in prompt
    # Pattern C: prompt instructs not to widen scope on retry.
    assert "Re-read your slice tasks" in prompt
    assert "do NOT widen scope" in prompt.lower() or "do not widen scope" in prompt.lower()


def test_build_agent_prompt_has_merge_repair_framing(tmp_path: Path) -> None:
    s = Group(
        id="feed",
        name="Feed",
        dependencies=[],
        owned_paths=["src/feed.ts"],
        feature_ids=["render micro-post feed"],
        checks=[],
    )
    spec = _spec([s])
    inp = BuildAgentInput(
        spec=spec,
        group=s,
        project_dir=tmp_path,
        worktree=tmp_path,
        branch="i2p/session/feed",
        attempt=1,
        last_failure_narrative="merge conflict on slice branch i2p/session/feed",
        merge_repair=True,
        contract_deltas=(
            ContractDelta(
                group_id="feed",
                contract_id="timeline-store",
                owner_id="foundation",
                paths=["src/lib/store.ts"],
                invariants=["posts survive refresh"],
                extension_policy="feature groups may add compatible selectors",
            ),
        ),
    )

    prompt = _build_agent_prompt(inp)

    assert "Merge repair mode" in prompt
    assert "branch-winner choice" in prompt
    assert "Do not resolve by blindly choosing" in prompt
    assert "Compose both sides where compatible" in prompt
    assert "already-integrated product behavior" in prompt
    assert "incompatible product decision" in prompt
    assert "Contract deltas from this branch" in prompt
    assert "timeline-store" in prompt
    assert "posts survive refresh" in prompt
    assert "Integration failure detail" in prompt
    assert "merge conflict on slice branch i2p/session/feed" in prompt


def test_layer2_build_prompt_requires_regression_tests(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    s = Group(id="number", name="Number", owned_paths=["src/number.py"])
    spec = _spec([s])
    spec.intent = "invalid comma strings should be returned unchanged"
    spec.features = [
        Feature(
            id="intword",
            name="intword",
            group_id="number",
            description="parse comma-separated numeric strings",
        )
    ]
    inp = BuildAgentInput(
        spec=spec,
        group=s,
        project_dir=tmp_path,
        worktree=tmp_path,
        branch="",
        attempt=1,
        feature_id="intword",
        last_failure_narrative="intword('not,a,number') returned 'notanumber'",
    )

    prompt = _build_agent_prompt(inp)

    assert "Regression-test requirement" in prompt
    assert "repo-native regression test" in prompt
    assert "exact acceptance examples" in prompt
    assert "invalid/error input" in prompt
    assert "same changed path" in prompt
    assert "Do NOT change expected test values" in prompt
    assert "exactly equal to the original input" in prompt
    assert "including punctuation/separators" in prompt
    assert "docstring examples are not a substitute" in prompt


# ---------------------------------------------------------------------------
# Project contract surface in build prompt (Microfeed bench learning)
# ---------------------------------------------------------------------------


def test_build_agent_prompt_surfaces_otto_yaml_test_command(tmp_path: Path) -> None:
    """Reproduce: agent built `{follower, following}` against contract that uses
    `{follower, target}`. Fix: build prompt explicitly tells agent the
    test_command + contract files exist and to read them."""
    (tmp_path / "otto.yaml").write_text(
        "default_branch: main\ntest_command: 'python tests/run_acceptance.py'\n",
        encoding="utf-8",
    )
    s = Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[])
    spec = _spec([s])
    inp = BuildAgentInput(
        spec=spec, group=s, project_dir=tmp_path, worktree=tmp_path,
        branch="x", attempt=1,
    )
    prompt = _build_agent_prompt(inp)
    assert "Project contract surface" in prompt
    assert "test_command" in prompt
    assert "tests/run_acceptance.py" in prompt
    # Pattern C: contract surface is informational context, not a directive
    # to "make it pass" (whole-product test isn't the slice's job).
    assert "read these to learn API shapes" in prompt.lower() or "read for api/data shapes" in prompt.lower()


def test_build_agent_prompt_surfaces_seeded_test_files(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "run_acceptance.py").write_text("# contract\n", encoding="utf-8")
    (tmp_path / "tests" / "conftest.py").write_text("# fixtures\n", encoding="utf-8")
    s = Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[])
    spec = _spec([s])
    inp = BuildAgentInput(
        spec=spec, group=s, project_dir=tmp_path, worktree=tmp_path,
        branch="x", attempt=1,
    )
    prompt = _build_agent_prompt(inp)
    assert "tests/run_acceptance.py" in prompt
    assert "tests/conftest.py" in prompt


def test_build_agent_prompt_uses_target_runtime_for_pytest_checks(tmp_path: Path) -> None:
    s = Group(
        id="sla",
        name="SLA",
        checks=[PytestCheck(selector="tests/test_sla_aging.py", timeout_s=120)],
    )
    spec = _spec([s])
    inp = BuildAgentInput(
        spec=spec,
        group=s,
        project_dir=tmp_path,
        worktree=tmp_path,
        branch="x",
        attempt=1,
    )

    prompt = _build_agent_prompt(inp)

    assert "python -m pytest tests/test_sla_aging.py" in prompt
    assert "global pytest executable" in prompt
    assert "pytest selector `tests/test_sla_aging.py`" not in prompt


def test_build_agent_prompt_omits_contract_section_when_no_contract(tmp_path: Path) -> None:
    """If there's no otto.yaml test_command and no seeded test files, skip
    the contract section entirely — don't push noise into the prompt."""
    s = Group(id="s1", name="x", dependencies=[], owned_paths=[], feature_ids=[], checks=[])
    spec = _spec([s])
    inp = BuildAgentInput(
        spec=spec, group=s, project_dir=tmp_path, worktree=tmp_path,
        branch="x", attempt=1,
    )
    prompt = _build_agent_prompt(inp)
    assert "Project contract surface" not in prompt


# ---------------------------------------------------------------------------
# Pattern D — real per-slice branches in build loop
# ---------------------------------------------------------------------------


def test_run_build_creates_real_per_slice_branch(tmp_path: Path) -> None:
    """Pattern D: when run_build runs in a git repo with `base_branch`
    present, each slice gets a real git branch checked out before its
    build agent runs. The branch persists after the build (so merge_queue
    can merge it) and contains the slice's commits.
    """
    _init_git(tmp_path)
    # Ensure we're on `main` (older git default `master`).
    current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if current != "main":
        subprocess.run(["git", "branch", "-m", current, "main"], cwd=tmp_path, check=True)

    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    async def writing_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        # Write to the slice's owned path so there's a real diff.
        path = input_.worktree / "slice-output.txt"
        path.write_text(f"work from {input_.group.id}", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, cost_usd=0.01)

    spec = _spec(
        [
            Group(id="alpha", name="x", dependencies=[],
                  owned_paths=["slice-output.txt"],
                  feature_ids=["write slice-output.txt"],
                  checks=[_no_op_passing_check()]),
        ]
    )
    result = asyncio.run(
        run_build(
            spec, project_dir=tmp_path, session_dir=session_dir,
            build_agent=writing_agent, base_branch="main",
        )
    )
    assert result.all_passing
    # Group's branch exists in git.
    branch_check = subprocess.run(
        ["git", "rev-parse", "--verify", f"i2p/{session_dir.name}/alpha"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert branch_check.returncode == 0, "slice branch should exist after build"
    # Group branch has a commit beyond main.
    log = subprocess.run(
        ["git", "log", "--format=%s", f"main..i2p/{session_dir.name}/alpha"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout
    assert "i2p(alpha): build slice" in log


def test_run_build_dependent_slice_branches_off_dep_tip(tmp_path: Path) -> None:
    """Pattern D: a slice with deps branches off its dep's tip so it sees
    the dep's work. Avoids the trap where each slice starts from a stale
    base and can't import its dep.
    """
    _init_git(tmp_path)
    current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if current != "main":
        subprocess.run(["git", "branch", "-m", current, "main"], cwd=tmp_path, check=True)

    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    async def writing_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        # Each slice writes its own file. Group b should see slice a's file.
        out = input_.worktree / f"{input_.group.id}.txt"
        out.write_text(f"from {input_.group.id}", encoding="utf-8")
        # Verify dep visibility for slice b.
        if input_.group.dependencies:
            dep_file = input_.worktree / f"{input_.group.dependencies[0]}.txt"
            assert dep_file.exists(), f"dep file {dep_file} should be visible to slice {input_.group.id}"
        return BuildAgentOutput(succeeded=True, cost_usd=0.01)

    spec = _spec(
        [
            Group(id="a", name="A", dependencies=[], owned_paths=["a.txt"],
                  feature_ids=["write a"], checks=[_no_op_passing_check()]),
            Group(id="b", name="B", dependencies=["a"], owned_paths=["b.txt"],
                  feature_ids=["write b"], checks=[_no_op_passing_check()]),
        ]
    )
    result = asyncio.run(
        run_build(
            spec, project_dir=tmp_path, session_dir=session_dir,
            build_agent=writing_agent, base_branch="main",
        )
    )
    assert result.all_passing
    # Branch b should contain commits from BOTH a and b.
    log = subprocess.run(
        ["git", "log", "--format=%s", f"main..i2p/{session_dir.name}/b"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout
    assert "i2p(a): build slice" in log
    assert "i2p(b): build slice" in log


def test_run_build_surfaces_multi_dep_conflict_to_dependent_agent(
    tmp_path: Path,
) -> None:
    """A multi-dep slice must not silently fall back to one dependency.

    If two required sibling branches conflict while building the slice's
    integrated starting point, running the dependent agent on only one
    dependency can produce a false pass for an incomplete product.
    """
    _init_git(tmp_path)
    current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if current != "main":
        subprocess.run(["git", "branch", "-m", current, "main"], cwd=tmp_path, check=True)

    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    calls: list[str] = []

    async def writing_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        calls.append(input_.group.id)
        if input_.group.id == "c":
            raise AssertionError("dependent group ran without all dependency branches")
        (input_.worktree / "shared.txt").write_text(
            f"from {input_.group.id}\n", encoding="utf-8"
        )
        return BuildAgentOutput(succeeded=True, cost_usd=0.01)

    spec = _spec(
        [
            Group(id="a", name="A", dependencies=[], owned_paths=["shared.txt"],
                  feature_ids=["write a"], checks=[_no_op_passing_check()]),
            Group(id="b", name="B", dependencies=[], owned_paths=["shared.txt"],
                  feature_ids=["write b"], checks=[_no_op_passing_check()]),
            Group(id="c", name="C", dependencies=["a", "b"], owned_paths=["c.txt"],
                  feature_ids=["write c"], checks=[_no_op_passing_check()]),
        ]
    )
    spec.shared_scaffold = ["shared.txt"]

    result = asyncio.run(
        run_build(
            spec, project_dir=tmp_path, session_dir=session_dir,
            build_agent=writing_agent, base_branch="main",
        )
    )

    assert calls == ["a", "b", "c", "c", "c"]
    c_result = next(r for r in result.group_results if r.group_id == "c")
    assert c_result.status == GroupStatus.BLOCKED
    assert "dependent group ran without all dependency branches" in c_result.failure_narrative


def test_run_build_falls_back_when_not_a_git_repo(tmp_path: Path) -> None:
    """Pattern D: if project_dir isn't a git repo, branch setup is
    skipped silently and the build completes in single-worktree mode.
    Ensures Pattern D is non-breaking for non-git fixtures.
    """
    # No _init_git — tmp_path is a plain directory.
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    async def writing_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        (input_.worktree / "out.txt").write_text("ok", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, cost_usd=0.01)

    spec = _spec(
        [
            Group(id="s1", name="x", dependencies=[], owned_paths=["out.txt"],
                  feature_ids=["write out.txt"], checks=[_no_op_passing_check()]),
        ]
    )
    result = asyncio.run(
        run_build(
            spec, project_dir=tmp_path, session_dir=session_dir,
            build_agent=writing_agent,
        )
    )
    assert result.all_passing
    # File exists (build agent ran), no git artifacts.
    assert (tmp_path / "out.txt").exists()
    assert not (tmp_path / ".git").exists()


# ---------------------------------------------------------------------------
# B2/B4: commit-failure handling — branch_by_group not populated, slice BLOCKED,
#        journal records BLOCKED instead of PASSING
# ---------------------------------------------------------------------------


def test_commit_group_work_ignores_otto_build_logs_only(tmp_path: Path) -> None:
    _init_git(tmp_path)
    initial_count = int(subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip())

    log_dir = tmp_path / "_otto_build_logs" / "attempt-01"
    log_dir.mkdir(parents=True)
    (log_dir / "messages.jsonl").write_text("provider transcript\n", encoding="utf-8")

    assert _commit_group_work(tmp_path, group_id="g", branch="layer2/g")

    final_count = int(subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip())
    assert final_count == initial_count
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout
    assert "?? _otto_build_logs/" in status


def test_commit_group_work_excludes_otto_build_logs_from_product_commit(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('real change')\n", encoding="utf-8")
    log_dir = tmp_path / "_otto_build_logs" / "attempt-01"
    log_dir.mkdir(parents=True)
    (log_dir / "messages.jsonl").write_text("provider transcript\n", encoding="utf-8")

    assert _commit_group_work(tmp_path, group_id="g", branch="layer2/g")

    committed_paths = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert "src/app.py" in committed_paths
    assert not any(path.startswith("_otto_build_logs/") for path in committed_paths)
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout
    assert "?? _otto_build_logs/" in status


def test_commit_group_work_excludes_otto_runtime_evidence_from_product_commit(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('real change')\n", encoding="utf-8")
    artifact_dir = tmp_path / "otto_artifacts" / "browser"
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "shot.png").write_bytes(b"png")
    (tmp_path / "__audit_home_body__.html").write_text("<main>audit</main>", encoding="utf-8")

    assert _commit_group_work(tmp_path, group_id="g", branch="layer2/g")

    committed_paths = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert "src/app.py" in committed_paths
    assert not any(path.startswith("otto_artifacts/") for path in committed_paths)
    assert "__audit_home_body__.html" not in committed_paths


def test_commit_group_work_excludes_common_generated_artifacts(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('real change')\n", encoding="utf-8")
    cache_dir = tmp_path / "src" / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "app.cpython-314.pyc").write_bytes(b"cache")
    pytest_cache = tmp_path / ".pytest_cache"
    pytest_cache.mkdir()
    (pytest_cache / "README.md").write_text("cache\n", encoding="utf-8")
    report_dir = tmp_path / "test-results" / "playwright-report"
    report_dir.mkdir(parents=True)
    (report_dir / "index.html").write_text("<html>generated</html>\n", encoding="utf-8")

    assert _commit_group_work(tmp_path, group_id="g", branch="layer2/g")

    committed_paths = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert "src/app.py" in committed_paths
    assert not any("__pycache__" in path for path in committed_paths)
    assert not any(path.startswith(".pytest_cache/") for path in committed_paths)
    assert not any(path.startswith("test-results/") for path in committed_paths)


def test_commit_group_work_ignores_unstaged_non_product_modifications(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    (tmp_path / "otto_logs").mkdir()
    (tmp_path / "otto_logs" / "watcher.log").write_text("baseline\n", encoding="utf-8")
    subprocess.run(["git", "add", "otto_logs/watcher.log"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "track runtime path", "--no-verify"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "otto_logs" / "watcher.log").write_text("runtime\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('real change')\n", encoding="utf-8")

    assert _commit_group_work(tmp_path, group_id="g", branch="layer2/g")

    committed_paths = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    assert committed_paths == ["src/app.py"]


def test_run_build_marks_blocked_on_commit_failure(tmp_path: Path) -> None:
    """B2/B4: if _commit_group_work fails (e.g., git not configured),
    the slice is marked BLOCKED rather than PASSING. branch_by_group is
    NOT populated, so a downstream dependent slice would not branch off
    a phantom commit.
    """
    _init_git(tmp_path)
    current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if current != "main":
        subprocess.run(["git", "branch", "-m", current, "main"], cwd=tmp_path, check=True)

    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    async def writing_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        (input_.worktree / "a.txt").write_text(f"from {input_.group.id}", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, cost_usd=0.01)

    # Force commit to fail by clobbering git identity in the slice's branch.
    # Run build with the user.email config unset for *this* command — easiest
    # is to monkeypatch _commit_group_work to return False.
    import otto.build as build_mod
    orig = build_mod._commit_group_work
    build_mod._commit_group_work = lambda *a, **k: False
    try:
        spec = _spec(
            [
                Group(id="a", name="A", dependencies=[], owned_paths=["a.txt"],
                      feature_ids=["write a"], checks=[_no_op_passing_check()]),
            ]
        )
        result = asyncio.run(
            run_build(
                spec, project_dir=tmp_path, session_dir=session_dir,
                build_agent=writing_agent, base_branch="main",
            )
        )
    finally:
        build_mod._commit_group_work = orig

    assert not result.all_passing
    a_result = next(r for r in result.group_results if r.group_id == "a")
    assert a_result.status == GroupStatus.BLOCKED
    assert "failed to commit work" in a_result.failure_narrative


# ---------------------------------------------------------------------------
# B3: scope detection in single-worktree fallback (no git repo)
# ---------------------------------------------------------------------------


def test_run_build_detects_scope_violation_without_git(tmp_path: Path) -> None:
    """B3: when the worktree isn't a git repo, scope detection MUST
    still work — a slice writing into a PEER slice's owned_paths
    should produce scope_warnings via the filesystem-snapshot
    fallback path. Without the fallback, scope detection silently
    becomes a no-op when git isn't available.
    """
    # No _init_git — tmp_path is a plain dir.
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    # Pre-create peer.txt so detect_scope_violations sees it as an
    # existing file (the "newly created" allowance does not apply).
    (tmp_path / "peer.txt").write_text("baseline", encoding="utf-8")

    async def over_reaching_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        # Group s1 owns only a.txt; the agent over-reaches and writes
        # peer.txt (owned by slice s2) — this is real over-reach.
        if input_.group.id == "s1":
            (input_.worktree / "a.txt").write_text("ok", encoding="utf-8")
            (input_.worktree / "peer.txt").write_text("from s1 over-reach", encoding="utf-8")
        else:
            (input_.worktree / "peer.txt").write_text("from s2", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, cost_usd=0.01)

    spec = _spec(
        [
            Group(id="s1", name="A", dependencies=[], owned_paths=["a.txt"],
                  feature_ids=["write a.txt"], checks=[_no_op_passing_check()]),
            Group(id="s2", name="B", dependencies=[], owned_paths=["peer.txt"],
                  feature_ids=["write peer.txt"], checks=[_no_op_passing_check()]),
        ]
    )
    result = asyncio.run(
        run_build(
            spec, project_dir=tmp_path, session_dir=session_dir,
            build_agent=over_reaching_agent,
        )
    )
    s1_result = next(r for r in result.group_results if r.group_id == "s1")
    # Scope detection must catch peer.txt write even without git.
    assert any("peer.txt" in w for w in s1_result.scope_warnings), (
        f"expected scope warning for peer.txt; got {s1_result.scope_warnings}"
    )


# ---------------------------------------------------------------------------
# V9: ensure_clean_git_state aborts in-progress merges/rebases
# ---------------------------------------------------------------------------


def test_ensure_clean_git_state_aborts_inprogress_merge(tmp_path: Path) -> None:
    """V9: a worktree left in mid-merge state (MERGE_HEAD present) should
    be recovered by `_ensure_clean_git_state` so subsequent branch ops
    can proceed. P2 hit this — audit phase saw repeated 'mid-MERGE_HEAD'
    after the merge phase ended; without recovery the audit fix-loop
    silently skipped slices.
    """
    from otto.build import _ensure_clean_git_state
    _init_git(tmp_path)
    # Create a conflicting situation: two branches modifying same file.
    (tmp_path / "shared.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "base", "--no-verify"],
                   cwd=tmp_path, check=True, capture_output=True)
    # Branch A
    subprocess.run(["git", "checkout", "-b", "branchA"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "shared.txt").write_text("from A", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "A", "--no-verify"],
                   cwd=tmp_path, check=True, capture_output=True)
    # Branch B from base, conflicting change
    subprocess.run(["git", "checkout", "-b", "branchB", "main"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "shared.txt").write_text("from B", encoding="utf-8")
    subprocess.run(["git", "add", "shared.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "B", "--no-verify"],
                   cwd=tmp_path, check=True, capture_output=True)
    # Trigger a conflict — git merge --no-commit will leave MERGE_HEAD.
    subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", "branchA"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    # Verify MERGE_HEAD now exists.
    git_dir = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    merge_head = tmp_path / git_dir / "MERGE_HEAD"
    assert merge_head.exists(), "test setup: MERGE_HEAD should exist"

    # V9: ensure_clean_git_state should abort the merge.
    assert _ensure_clean_git_state(tmp_path) is True
    assert not merge_head.exists(), "MERGE_HEAD should be cleared"

    # And subsequent branch ops should now work.
    co = subprocess.run(
        ["git", "checkout", "main"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert co.returncode == 0, f"checkout should succeed; stderr: {co.stderr}"


def test_setup_slice_branch_recovers_from_mid_merge(tmp_path: Path) -> None:
    """V9: _setup_group_branch used to silently skip when MERGE_HEAD was
    present. Now it calls _ensure_clean_git_state first to recover, so
    a leftover mid-merge from a prior phase doesn't trap the build.
    """
    from otto.build import _setup_group_branch
    _init_git(tmp_path)
    # Setup conflicting state and trigger MERGE_HEAD same way as above.
    (tmp_path / "f.txt").write_text("base", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "base", "--no-verify"],
                   cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "branchA"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "f.txt").write_text("A", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "A", "--no-verify"],
                   cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "-b", "branchB", "main"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "f.txt").write_text("B", encoding="utf-8")
    subprocess.run(["git", "add", "f.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "B", "--no-verify"],
                   cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "merge", "--no-commit", "--no-ff", "branchA"],
                   cwd=tmp_path, capture_output=True, text=True, check=False)

    # V9: _setup_group_branch should now recover and succeed.
    ok = _setup_group_branch(tmp_path, branch="i2p/test/recovery", parent_ref="main")
    assert ok is True, "setup should recover from mid-merge state"
    head = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head == "i2p/test/recovery"


def test_build_prompt_forbids_git_mutations(tmp_path: Path) -> None:
    """V8: build agent prompt MUST tell the agent that git is read-only.
    Without this, agents have run `git merge other-slice-branch` to
    grab files from other slices, breaking branch isolation (P2).
    """
    s = Group(id="s1", name="x", dependencies=[], owned_paths=["x.py"],
              feature_ids=["build x.py"], checks=[_no_op_passing_check()])
    spec = _spec([s])
    inp = BuildAgentInput(
        spec=spec, group=s, project_dir=tmp_path, worktree=tmp_path,
        branch="x", attempt=1,
    )
    prompt = _build_agent_prompt(inp)
    # Must explicitly forbid git state mutations.
    assert "Git is read-only" in prompt
    assert "git merge" in prompt.lower()
    assert "git checkout" in prompt.lower() or "git rebase" in prompt.lower()
    # Must allow the read-only forms so the agent isn't crippled.
    assert "git log" in prompt.lower()


def test_run_build_dag_slice_branch_contains_all_dep_work(tmp_path: Path) -> None:
    """V12: a slice with multiple deps from sibling branches must have
    ALL deps' contributions on its branch when its build agent runs.
    Without this fix, the slice only sees `last_dep`'s ancestry — sibling
    deps are missing, agent guesses APIs, merges conflict (P5 SSG).

    Topology: a (no deps) → both b and c. d depends on both b and c.
    d's branch must contain BOTH b's and c's commits.
    """
    _init_git(tmp_path)
    current = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout.strip()
    if current != "main":
        subprocess.run(["git", "branch", "-m", current, "main"], cwd=tmp_path, check=True)

    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    async def writing_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        # Each slice writes its own owned file.
        out = input_.worktree / f"{input_.group.id}.txt"
        out.write_text(f"from {input_.group.id}", encoding="utf-8")
        # Group d MUST be able to see both b.txt and c.txt at build time.
        if input_.group.id == "d":
            assert (input_.worktree / "b.txt").exists(), "V12: b.txt missing"
            assert (input_.worktree / "c.txt").exists(), "V12: c.txt missing"
        return BuildAgentOutput(succeeded=True, cost_usd=0.01)

    spec = _spec(
        [
            Group(id="a", name="A", dependencies=[], owned_paths=["a.txt"],
                  feature_ids=["x"], checks=[_no_op_passing_check()]),
            Group(id="b", name="B", dependencies=["a"], owned_paths=["b.txt"],
                  feature_ids=["x"], checks=[_no_op_passing_check()]),
            Group(id="c", name="C", dependencies=["a"], owned_paths=["c.txt"],
                  feature_ids=["x"], checks=[_no_op_passing_check()]),
            Group(id="d", name="D", dependencies=["b", "c"], owned_paths=["d.txt"],
                  feature_ids=["x"], checks=[_no_op_passing_check()]),
        ]
    )
    result = asyncio.run(
        run_build(
            spec, project_dir=tmp_path, session_dir=session_dir,
            build_agent=writing_agent, base_branch="main",
        )
    )
    assert result.all_passing, (
        f"V12: d should see b.txt and c.txt; got: "
        f"{[(r.group_id, r.status, r.failure_narrative) for r in result.group_results]}"
    )
    # Verify d's branch contains commits from BOTH b and c.
    log = subprocess.run(
        ["git", "log", "--format=%s", f"main..i2p/{session_dir.name}/d"],
        cwd=tmp_path, capture_output=True, text=True, check=True,
    ).stdout
    assert "i2p(a): build slice" in log, f"a missing from d's branch: {log}"
    assert "i2p(b): build slice" in log, f"b missing from d's branch: {log}"
    assert "i2p(c): build slice" in log, f"c missing from d's branch: {log}"
    assert "i2p(d): build slice" in log, f"d's own commit missing: {log}"
