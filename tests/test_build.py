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


def test_scope_violations_flags_modifications_to_LATER_peer_slice_paths(tmp_path: Path) -> None:
    """Topological-precedence rule: an EARLIER slice cannot modify a
    LATER slice's owned files. (Direction matters: the later slice
    hasn't built yet, so its files don't exist for modification.)
    """
    spec = _spec(
        [
            Slice(id="s1", title="shell", deps=[], owned_paths=["app/main.py"], tasks=[], checks=[]),
            Slice(id="s2", title="api", deps=[], owned_paths=["app/api.py"], tasks=[], checks=[]),
        ]
    )
    s1 = spec.slices[0]  # earlier in spec order
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "api.py").write_text("# existing", encoding="utf-8")
    violations = detect_scope_violations(s1, spec, ["app/api.py"], project_root=tmp_path)
    assert violations == ["app/api.py"]


def test_scope_violations_allows_LATER_slice_modifying_EARLIER_peer(tmp_path: Path) -> None:
    """Topological-precedence: later slices can extend earlier slices'
    work even without an explicit dep declaration. This is the rule
    relaxation that solved the consistent peer-overreach failure
    (e.g. `posts` adding helpers to `routes/social.py`)."""
    spec = _spec(
        [
            Slice(id="social", title="social", deps=[], owned_paths=["routes/social.py"], tasks=[], checks=[]),
            Slice(id="posts", title="posts", deps=[], owned_paths=["routes/posts.py"], tasks=[], checks=[]),
        ]
    )
    posts = spec.slices[1]  # later in spec order
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes" / "social.py").write_text("# existing", encoding="utf-8")
    violations = detect_scope_violations(posts, spec, ["routes/social.py"], project_root=tmp_path)
    assert violations == []


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


def test_scope_violations_blocks_EARLIER_modifying_LATER_peer(tmp_path: Path) -> None:
    """The topological-precedence rule still blocks earlier slices from
    modifying later slices' files (those files haven't been built yet
    in the merge order)."""
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
    posts = spec.slices[1]  # earlier than search
    (tmp_path / "routes").mkdir()
    (tmp_path / "routes" / "search.py").write_text("# search\n", encoding="utf-8")
    # posts wants to modify search's file (search is LATER) — blocked.
    violations = detect_scope_violations(
        posts, spec, ["routes/search.py"], project_root=tmp_path,
    )
    assert violations == ["routes/search.py"]


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
            Slice(
                id="s1",
                title="naughty-earlier",
                deps=[],  # earlier in spec order
                owned_paths=["app/api/*"],
                tasks=[],
                checks=[_no_op_passing_check()],
            ),
            Slice(id="s2", title="shell", deps=[], owned_paths=["app/main.py"], tasks=[], checks=[]),
        ]
    )

    # s1's agent illegally modifies s2's file. s2 is LATER in spec order,
    # so under topological-precedence s1 cannot modify it.
    async def fake_agent(input_: BuildAgentInput) -> BuildAgentOutput:
        if input_.slice.id == "s1":
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
    # s1 ran first (earlier in spec) and tampered with s2's file → FAILED_SCOPE.
    assert by_id["s1"].status == SliceStatus.FAILED_SCOPE
    assert "scope violation" in by_id["s1"].failure_narrative
    assert "app/main.py" in by_id["s1"].failure_narrative


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


def test_build_agent_prompt_lists_peer_owned_paths_as_forbidden(tmp_path: Path) -> None:
    """Round-6 Microfeed bench learning: build agents over-reach into peer
    slices' owned files. Prompt must explicitly list the forbidden paths.
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
    # Self's owned_paths shown as MODIFY
    assert "your owned: `routes/auth.py`" in prompt
    # Dep's owned_paths shown as MODIFY (transitive dep)
    assert "dep `shell`'s: `app.py`" in prompt
    # Peer's owned_paths shown as FORBIDDEN
    assert "FORBIDDEN" in prompt
    assert "peer `search`'s: `routes/search.py`" in prompt
    assert "peer `search`'s: `templates/search.html`" in prompt
    assert "Stay in your lane" in prompt


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
