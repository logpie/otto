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


def test_scope_violations_flags_modifications_to_peer_slice_paths(tmp_path: Path) -> None:
    """Modifying a peer slice (no dep relation) IS a violation."""
    spec = _spec(
        [
            Slice(id="s1", title="shell", deps=[], owned_paths=["app/main.py"], tasks=[], checks=[]),
            Slice(id="s2", title="api", deps=[], owned_paths=["app/api.py"], tasks=[], checks=[]),
        ]
    )
    s2 = spec.slices[1]  # peer of s1, not a dep
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


def test_scope_violations_allows_transitive_dep_modification(tmp_path: Path) -> None:
    """A slice may modify any file owned by a slice in its transitive deps.

    Microfeed bench learning: downstream feature slices need to extend
    foundation slice's files (User model in models.py, blueprint
    registration in app.py). The dep relation already declares this
    "extends" relationship — the rule should respect it.
    """
    spec = _spec(
        [
            Slice(id="shell", title="x", deps=[],
                  owned_paths=["app.py", "models.py"], tasks=[], checks=[]),
            Slice(id="auth", title="y", deps=["shell"],
                  owned_paths=["routes/auth.py"], tasks=[], checks=[]),
        ]
    )
    auth = spec.slices[1]
    # Pre-existing files owned by shell; auth (deps=[shell]) extends them.
    (tmp_path / "app.py").write_text("# shell\n", encoding="utf-8")
    (tmp_path / "models.py").write_text("# shell\n", encoding="utf-8")
    violations = detect_scope_violations(
        auth, spec, ["app.py", "models.py"], project_root=tmp_path,
    )
    assert violations == []


def test_scope_violations_blocks_peer_slice_modification(tmp_path: Path) -> None:
    """A slice may NOT modify a peer slice's owned files (no dep relation)."""
    spec = _spec(
        [
            Slice(id="shell", title="x", deps=[],
                  owned_paths=["app.py"], tasks=[], checks=[]),
            Slice(id="posts", title="p", deps=["shell"],
                  owned_paths=["routes/posts.py"], tasks=[], checks=[]),
            Slice(id="search", title="s", deps=["shell"],
                  owned_paths=["routes/search.py"], tasks=[], checks=[]),
        ]
    )
    search = spec.slices[2]
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes" / "posts.py").write_text("# posts\n", encoding="utf-8")
    # search wants to modify posts' file — should be blocked.
    violations = detect_scope_violations(
        search, spec, ["routes/posts.py"], project_root=tmp_path,
    )
    assert violations == ["routes/posts.py"]


def test_scope_violations_transitive_dep_chain(tmp_path: Path) -> None:
    """Dep transitivity: posts (deps=auth) (deps=shell) can modify shell's files."""
    spec = _spec(
        [
            Slice(id="shell", title="x", deps=[],
                  owned_paths=["app.py"], tasks=[], checks=[]),
            Slice(id="auth", title="y", deps=["shell"],
                  owned_paths=["routes/auth.py"], tasks=[], checks=[]),
            Slice(id="posts", title="p", deps=["auth"],
                  owned_paths=["routes/posts.py"], tasks=[], checks=[]),
        ]
    )
    posts = spec.slices[2]
    (tmp_path / "app.py").write_text("# shell\n", encoding="utf-8")
    violations = detect_scope_violations(
        posts, spec, ["app.py"], project_root=tmp_path,
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
            budget=BuildBudget(per_slice_retries_hard_cap=8),
        )
    )
    r = result.slice_results[0]
    assert r.status == SliceStatus.BLOCKED
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
            Slice(id="s1", title="x", deps=[], owned_paths=[], tasks=[], checks=[_no_op_failing_check()]),
            Slice(id="s2", title="y", deps=["s1"], owned_paths=[], tasks=[], checks=[_no_op_passing_check()]),
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
                title="naughty-peer",
                deps=[],  # peer of s1, NOT a dep — so it can't modify s1's files
                owned_paths=["app/api/*"],
                tasks=[],
                checks=[_no_op_passing_check()],
            ),
        ]
    )

    # s2's agent illegally modifies s1's file (peer slice, no dep).
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
    # Soft-warning model: scope crossing does NOT block the slice. Both
    # slices still PASS (each has a no-op-passing check), and the
    # warning is captured on s2's SliceResult for the proof packet.
    assert by_id["s1"].status == SliceStatus.PASSING
    assert by_id["s2"].status == SliceStatus.PASSING
    assert "app/main.py" in by_id["s2"].scope_warnings


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


def test_build_agent_prompt_writeable_paths_only(tmp_path: Path) -> None:
    """Phase 1A simplification: prompt lists ONLY the paths the slice may
    write — its own owned_paths, transitive deps, and shared_scaffold.
    Peer-owned paths are NOT enumerated (they were dead text the agent
    treated as 'fine, just a warning'). Soft-warning behavior remains
    in the runtime; the prompt no longer recites it.
    """
    s_self = Slice(id="auth", title="Auth", deps=["shell"],
                   owned_paths=["routes/auth.py"], tasks=[], checks=[])
    s_shell = Slice(id="shell", title="Shell", deps=[],
                    owned_paths=["app.py"], tasks=[], checks=[])
    s_peer = Slice(id="search", title="Search", deps=["shell"],
                   owned_paths=["routes/search.py", "templates/search.html"],
                   tasks=[], checks=[])
    spec = _spec([s_shell, s_self, s_peer])
    inp = BuildAgentInput(
        spec=spec, slice=s_self, project_dir=tmp_path, worktree=tmp_path,
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
    # Pattern C: prompt instructs not to widen scope on retry.
    assert "Re-read your slice tasks" in prompt
    assert "do NOT widen scope" in prompt.lower() or "do not widen scope" in prompt.lower()


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
    # Pattern C: contract surface is informational context, not a directive
    # to "make it pass" (whole-product test isn't the slice's job).
    assert "read these to learn API shapes" in prompt.lower() or "read for api/data shapes" in prompt.lower()


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
        path.write_text(f"work from {input_.slice.id}", encoding="utf-8")
        return BuildAgentOutput(succeeded=True, cost_usd=0.01)

    spec = _spec(
        [
            Slice(id="alpha", title="x", deps=[],
                  owned_paths=["slice-output.txt"],
                  tasks=["write slice-output.txt"],
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
    # Slice's branch exists in git.
    branch_check = subprocess.run(
        ["git", "rev-parse", "--verify", f"i2p/{session_dir.name}/alpha"],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert branch_check.returncode == 0, "slice branch should exist after build"
    # Slice branch has a commit beyond main.
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
        # Each slice writes its own file. Slice b should see slice a's file.
        out = input_.worktree / f"{input_.slice.id}.txt"
        out.write_text(f"from {input_.slice.id}", encoding="utf-8")
        # Verify dep visibility for slice b.
        if input_.slice.deps:
            dep_file = input_.worktree / f"{input_.slice.deps[0]}.txt"
            assert dep_file.exists(), f"dep file {dep_file} should be visible to slice {input_.slice.id}"
        return BuildAgentOutput(succeeded=True, cost_usd=0.01)

    spec = _spec(
        [
            Slice(id="a", title="A", deps=[], owned_paths=["a.txt"],
                  tasks=["write a"], checks=[_no_op_passing_check()]),
            Slice(id="b", title="B", deps=["a"], owned_paths=["b.txt"],
                  tasks=["write b"], checks=[_no_op_passing_check()]),
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
            Slice(id="s1", title="x", deps=[], owned_paths=["out.txt"],
                  tasks=["write out.txt"], checks=[_no_op_passing_check()]),
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
