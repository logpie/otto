"""Tests for otto/build.py — slice dispatch + retry + scope enforcement.

Coverage:
- ready_slices: dep-aware readiness, exclusion of in-flight, exclusion of completed
- detect_scope_violations: write-scope, allow-create-anywhere, allow-shared-scaffold
- BuildBudget: per-slice retries, repair budget exhaustion
- run_build: happy path (one slice passes first try), retry-then-pass,
  retry-exhaustion-blocks, scope violation, dep-blocked downstream slice
- _build_agent_prompt: includes spec context + previous failure on retries

The build agent is mocked throughout; the LLM never runs.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from otto.build import (
    BuildAgentInput,
    BuildAgentOutput,
    BuildBudget,
    SliceStatus,
    _build_agent_prompt,
    detect_scope_violations,
    ready_slices,
    run_build,
)
from otto.spec_compile import (
    RepoTestCheck,
    Slice,
    Spec,
    StateInvariant,
    StructureDecisions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _spec(slices: list[Slice]) -> Spec:
    return Spec(
        intent="test intent",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        slices=slices,
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
# ready_slices
# ---------------------------------------------------------------------------


def test_ready_slices_returns_no_dep_slices_first() -> None:
    spec = _spec(
        [
            Slice(id="s1", title="shell", deps=[], owned_paths=[], tasks=[], checks=[]),
            Slice(id="s2", title="api", deps=["s1"], owned_paths=[], tasks=[], checks=[]),
        ]
    )
    assert [s.id for s in ready_slices(spec, completed_ids=set())] == ["s1"]


def test_ready_slices_unblocks_dependents_after_completion() -> None:
    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[]),
            Slice(id="s2", title="y", deps=["s1"], owned_paths=[], tasks=[], checks=[]),
            Slice(id="s3", title="z", deps=["s1"], owned_paths=[], tasks=[], checks=[]),
        ]
    )
    ready = ready_slices(spec, completed_ids={"s1"})
    assert sorted(s.id for s in ready) == ["s2", "s3"]


def test_ready_slices_excludes_in_progress() -> None:
    spec = _spec(
        [
            Slice(id="a", title="x", deps=[], owned_paths=[], tasks=[], checks=[]),
            Slice(id="b", title="y", deps=[], owned_paths=[], tasks=[], checks=[]),
        ]
    )
    ready = ready_slices(spec, completed_ids=set(), in_progress_ids={"a"})
    assert [s.id for s in ready] == ["b"]


# ---------------------------------------------------------------------------
# detect_scope_violations
# ---------------------------------------------------------------------------


def test_scope_violations_allows_own_paths() -> None:
    spec = _spec(
        [
            Slice(
                id="s1",
                title="shell",
                deps=[],
                owned_paths=["app/main.py", "app/components/*"],
                tasks=[],
                checks=[],
            ),
        ]
    )
    s1 = spec.slices[0]
    violations = detect_scope_violations(s1, spec, ["app/main.py", "app/components/Nav.tsx"])
    assert violations == []


def test_scope_violations_flags_modifications_to_other_slice_paths(tmp_path: Path) -> None:
    spec = _spec(
        [
            Slice(id="s1", title="shell", deps=[], owned_paths=["app/main.py"], tasks=[], checks=[]),
            Slice(id="s2", title="api", deps=["s1"], owned_paths=["app/api/*"], tasks=[], checks=[]),
        ]
    )
    s2 = spec.slices[1]
    # Make s1's file pre-exist so it's a "modification" not a "creation".
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("# existing", encoding="utf-8")
    violations = detect_scope_violations(s2, spec, ["app/main.py"], project_root=tmp_path)
    assert violations == ["app/main.py"]


def test_scope_violations_allows_create_in_other_slice_glob(tmp_path: Path) -> None:
    """Newly created files are allowed even if they match another slice's glob."""
    spec = _spec(
        [
            Slice(id="s1", title="shell", deps=[], owned_paths=["app/components/*"], tasks=[], checks=[]),
            Slice(id="s2", title="api", deps=["s1"], owned_paths=["app/api/*"], tasks=[], checks=[]),
        ]
    )
    s2 = spec.slices[1]
    # path matches s1's glob but doesn't exist on disk → newly created → allowed
    violations = detect_scope_violations(
        s2, spec, ["app/components/PostCard.tsx"], project_root=tmp_path
    )
    assert violations == []


def test_scope_violations_allows_shared_scaffold() -> None:
    """Files matching no slice's owned_paths are shared scaffold and allowed."""
    spec = _spec(
        [
            Slice(id="s1", title="shell", deps=[], owned_paths=["app/main.py"], tasks=[], checks=[]),
            Slice(id="s2", title="api", deps=["s1"], owned_paths=["app/api/*"], tasks=[], checks=[]),
        ]
    )
    s2 = spec.slices[1]
    violations = detect_scope_violations(s2, spec, ["package.json", "tsconfig.json"])
    assert violations == []


def test_scope_violations_allows_shared_scaffold_extension(tmp_path: Path) -> None:
    """Microfeed bench learning: foundational files like models.py must be
    extendable by multiple slices. shared_scaffold declares this; the rule
    must honor it."""
    spec = Spec(
        intent="webapp",
        project_kind="webapp",
        structure=StructureDecisions(payload={}),
        slices=[
            Slice(id="s1", title="shell", deps=[], owned_paths=["routes/auth.py"], tasks=[], checks=[]),
            Slice(id="s2", title="api", deps=["s1"], owned_paths=["routes/api.py"], tasks=[], checks=[]),
        ],
        shared_scaffold=["models.py", "app.py", "database.py"],
    )
    s2 = spec.slices[1]
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
        slices=[
            Slice(id="s1", title="x", deps=[], owned_paths=["routes/x.py"], tasks=[], checks=[]),
        ],
        shared_scaffold=["config/*.py", "app/__init__.py"],
    )
    s1 = spec.slices[0]
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.py").write_text("# ex\n", encoding="utf-8")
    violations = detect_scope_violations(
        s1, spec, ["config/settings.py"], project_root=tmp_path,
    )
    assert violations == []


def test_scope_violations_recursive_glob() -> None:
    spec = _spec(
        [
            Slice(id="s1", title="shell", deps=[], owned_paths=["app/components/**"], tasks=[], checks=[]),
        ]
    )
    s1 = spec.slices[0]
    # Both top-level and nested under app/components/ should match.
    violations = detect_scope_violations(s1, spec, ["app/components/Nav.tsx", "app/components/inner/Inner.tsx"])
    assert violations == []


# ---------------------------------------------------------------------------
# BuildBudget
# ---------------------------------------------------------------------------


def test_build_budget_repair_charge_and_remaining() -> None:
    budget = BuildBudget(per_slice_retries=3, total_repair_s=100)
    assert budget.remaining_repair_s() == 100
    budget.charge_repair(40)
    assert budget.remaining_repair_s() == 60
    budget.charge_repair(70)  # over-charge clamps at 0
    assert budget.remaining_repair_s() == 0


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
            Slice(
                id="s1",
                title="hello",
                deps=[],
                owned_paths=[],
                tasks=["print hello"],
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
    assert result.slice_results[0].attempts == 1


def test_run_build_dep_aware_dispatch(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()
    spec = _spec(
        [
            Slice(id="s1", title="a", deps=[], owned_paths=[], tasks=[], checks=[_no_op_passing_check()]),
            Slice(id="s2", title="b", deps=["s1"], owned_paths=[], tasks=[], checks=[_no_op_passing_check()]),
            Slice(id="s3", title="c", deps=["s2"], owned_paths=[], tasks=[], checks=[_no_op_passing_check()]),
        ]
    )

    order: list[str] = []

    async def fake_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        order.append(input_.slice.id)
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

    # Slice with a state invariant that fails until the agent has
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
            Slice(id="s1", title="x", deps=[], owned_paths=["marker.txt"], tasks=[], checks=[inv]),
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
    assert result.slice_results[0].attempts == 3


def test_run_build_blocks_after_retry_exhaustion(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    spec = _spec(
        [
            Slice(
                id="s1",
                title="hopeless",
                deps=[],
                owned_paths=[],
                tasks=[],
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
            budget=BuildBudget(per_slice_retries=2),
        )
    )
    r = result.slice_results[0]
    assert r.status == SliceStatus.BLOCKED
    assert r.attempts == 2
    assert "checks failed" in r.failure_narrative


def test_run_build_propagates_block_to_dependent_slice(tmp_path: Path) -> None:
    _init_git(tmp_path)
    session_dir = tmp_path / "_session"
    session_dir.mkdir()

    spec = _spec(
        [
            Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[_no_op_failing_check()]),
            Slice(id="s2", title="y", deps=["s1"], owned_paths=[], tasks=[], checks=[_no_op_passing_check()]),
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
            budget=BuildBudget(per_slice_retries=1),
        )
    )
    by_id = {r.slice_id: r for r in result.slice_results}
    assert by_id["s1"].status == SliceStatus.BLOCKED
    assert by_id["s2"].status == SliceStatus.BLOCKED
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
            Slice(id="s1", title="shell", deps=[], owned_paths=["app/main.py"], tasks=[], checks=[]),
            Slice(
                id="s2",
                title="naughty",
                deps=["s1"],
                owned_paths=["app/api/*"],
                tasks=[],
                checks=[_no_op_passing_check()],
            ),
        ]
    )

    # s2's agent illegally modifies s1's file.
    async def fake_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        if input_.slice.id == "s2":
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
    by_id = {r.slice_id: r for r in result.slice_results}
    # s1 has no checks → empty list → all_pass==True (vacuously)
    assert by_id["s1"].status == SliceStatus.PASSING
    assert by_id["s2"].status == SliceStatus.FAILED_SCOPE
    assert "scope violation" in by_id["s2"].failure_narrative
    assert "app/main.py" in by_id["s2"].failure_narrative


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
            Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[_no_op_passing_check()]),
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
    assert result.slice_results[0].attempts == 2


# ---------------------------------------------------------------------------
# Prompt composition
# ---------------------------------------------------------------------------


def test_build_agent_prompt_contains_required_context(tmp_path: Path) -> None:
    s = Slice(
        id="s1",
        title="Auth flow",
        deps=["base"],
        owned_paths=["app/auth/*"],
        tasks=["build login form", "wire to API"],
        checks=[_no_op_passing_check()],
    )
    spec = _spec([s])
    spec.intent = "social network MVP"
    inp = BuildAgentInput(
        spec=spec,
        slice=s,
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


def test_build_agent_prompt_includes_last_failure_on_retry(tmp_path: Path) -> None:
    s = Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[])
    spec = _spec([s])
    inp = BuildAgentInput(
        spec=spec,
        slice=s,
        project_dir=tmp_path,
        worktree=tmp_path,
        branch="x",
        attempt=2,
        last_failure_narrative="check 3 failed: missing route /api/posts",
    )
    prompt = _build_agent_prompt(inp)
    assert "Previous attempt failed" in prompt
    assert "missing route /api/posts" in prompt
    assert "fresh approach" in prompt


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
    s = Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[])
    spec = _spec([s])
    inp = BuildAgentInput(
        spec=spec, slice=s, project_dir=tmp_path, worktree=tmp_path,
        branch="x", attempt=1,
    )
    prompt = _build_agent_prompt(inp)
    assert "Project contract surface" in prompt
    assert "test_command" in prompt
    assert "tests/run_acceptance.py" in prompt
    assert "READ THESE FIRST" in prompt


def test_build_agent_prompt_surfaces_seeded_test_files(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "run_acceptance.py").write_text("# contract\n", encoding="utf-8")
    (tmp_path / "tests" / "conftest.py").write_text("# fixtures\n", encoding="utf-8")
    s = Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[])
    spec = _spec([s])
    inp = BuildAgentInput(
        spec=spec, slice=s, project_dir=tmp_path, worktree=tmp_path,
        branch="x", attempt=1,
    )
    prompt = _build_agent_prompt(inp)
    assert "tests/run_acceptance.py" in prompt
    assert "tests/conftest.py" in prompt


def test_build_agent_prompt_omits_contract_section_when_no_contract(tmp_path: Path) -> None:
    """If there's no otto.yaml test_command and no seeded test files, skip
    the contract section entirely — don't push noise into the prompt."""
    s = Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[])
    spec = _spec([s])
    inp = BuildAgentInput(
        spec=spec, slice=s, project_dir=tmp_path, worktree=tmp_path,
        branch="x", attempt=1,
    )
    prompt = _build_agent_prompt(inp)
    assert "Project contract surface" not in prompt
