"""Regression tests for Step 0b recovery plumbing.

Live-run audit (2026-05-13) found the integration agent skipped Step
0b entirely despite 5 children having ``verdict=merge_blocked`` in the
task graph. Codex's diagnosis: ``_build_child_summaries`` was reading
the stale in-memory ``LeadResult.verdict`` (still ``"pass"``) instead
of the task-graph verdict (``"merge_blocked"``), and ``_render_prompt``
discarded the ``build_branch`` / ``recovery_hint`` fields even when
populated.

These tests pin the fix:

1. When task-graph verdict is ``merge_blocked`` but the in-memory
   ``LeadResult.verdict`` is stale, the summary surfaces
   ``merge_blocked``.
2. The rendered integration prompt includes ``build_branch=`` and
   ``recovery_hint:`` for blocked children.
3. ``_merge_child_branch`` mutates ``result.verdict`` after a merge
   failure (in-memory consistency with task graph).
"""

from __future__ import annotations

from pathlib import Path

from otto.lead import LeadResult, _render_prompt
from otto.queue.task_graph import record_task, set_verdict
from otto.v5_runner import _build_child_summaries


def _seed_parent_with_child(tmp_path: Path, child_id: str) -> None:
    (tmp_path / "otto_logs").mkdir(exist_ok=True)
    record_task(tmp_path, task_id="root", intent="root", parent_task_id=None)
    record_task(tmp_path, task_id=child_id,
                intent=f"Build {child_id}", parent_task_id="root")


def test_summary_prefers_graph_merge_blocked_over_stale_result(tmp_path: Path) -> None:
    """When the merge step writes merge_blocked to the graph but the
    in-memory result still says ``pass``, the summary must surface the
    merge_blocked verdict so the integration Lead sees it."""
    _seed_parent_with_child(tmp_path, "v5-foo")
    # Graph says merge_blocked.
    set_verdict(tmp_path, "v5-foo", "merge_blocked")
    # Stale in-memory result still claims pass (agent's self-declared verdict).
    stale_result = LeadResult(
        task_id="v5-foo", verdict="pass", cost_usd=1.0,
        final_text="all journeys passed", decomposition="inline",
    )
    child_results = {"v5-foo": stale_result}

    summaries = _build_child_summaries(tmp_path, "root", child_results)
    assert len(summaries) == 1
    s = summaries[0]
    assert s["verdict"] == "merge_blocked"
    assert "build_branch" in s
    assert s["build_branch"].endswith("v5-foo")
    assert "recovery_hint" in s


def test_summary_keeps_non_merge_blocked_result_verdicts(tmp_path: Path) -> None:
    """Sanity: when the graph verdict isn't merge_blocked, the
    in-memory result still wins (e.g., pass / partial / unverified)."""
    _seed_parent_with_child(tmp_path, "v5-bar")
    set_verdict(tmp_path, "v5-bar", "partial")
    result = LeadResult(task_id="v5-bar", verdict="partial", cost_usd=0.5,
                         final_text="some journeys failed", decomposition="inline")
    summaries = _build_child_summaries(tmp_path, "root", {"v5-bar": result})
    assert len(summaries) == 1
    assert summaries[0]["verdict"] == "partial"
    assert "build_branch" not in summaries[0]


def test_render_integration_prompt_includes_recovery_hint(tmp_path: Path) -> None:
    """The integration prompt's child_summaries section MUST contain the
    build branch + recovery hint for blocked children, so the agent can
    execute Step 0b's ``git merge <branch>`` instruction."""
    _seed_parent_with_child(tmp_path, "v5-blocked")
    set_verdict(tmp_path, "v5-blocked", "merge_blocked")
    stale_result = LeadResult(
        task_id="v5-blocked", verdict="pass", cost_usd=1.0,
        final_text="agent thought it passed", decomposition="inline",
    )
    summaries = _build_child_summaries(tmp_path, "root", {"v5-blocked": stale_result})

    rendered = _render_prompt(
        kind="integration",
        task_id="root",
        intent="root intent",
        session_dir=tmp_path / "session",
        integration_branch="main",
        child_summaries=summaries,
    )

    # The merge_blocked verdict must show
    assert "verdict=merge_blocked" in rendered
    # And the build branch must be present so the agent can `git merge` it
    assert "build_branch=" in rendered or "build_branch:" in rendered
    assert "v5-blocked" in rendered


def test_render_integration_prompt_no_branch_for_passing_child(tmp_path: Path) -> None:
    """Don't pollute the per-child summary with build_branch info for
    children that merged successfully — only blocked ones need it.

    The integration prompt template itself mentions ``build_branch`` /
    ``recovery_hint`` as concepts in Step 0b instructions; that's
    fine. What we're checking is that the rendered CHILD SUMMARY
    section doesn't attach those fields to passing children.
    """
    _seed_parent_with_child(tmp_path, "v5-clean")
    set_verdict(tmp_path, "v5-clean", "pass")
    result = LeadResult(task_id="v5-clean", verdict="pass", cost_usd=1.0,
                         final_text="all good", decomposition="inline")
    summaries = _build_child_summaries(tmp_path, "root", {"v5-clean": result})

    # Summary dict itself shouldn't carry the recovery fields.
    assert "build_branch" not in summaries[0]
    assert "recovery_hint" not in summaries[0]

    rendered = _render_prompt(
        kind="integration",
        task_id="root",
        intent="root intent",
        session_dir=tmp_path / "session",
        integration_branch="main",
        child_summaries=summaries,
    )
    # In the rendered prompt: the per-child line itself doesn't have
    # an indented `build_branch=` or `recovery_hint:` follower line.
    # (Those only appear when verdict==merge_blocked per the formatter.)
    lines_for_child = [
        ln for ln in rendered.splitlines() if "v5-clean" in ln
    ]
    # Should be one bullet line, no follow-on indented build_branch/recovery_hint.
    follow_lines = [
        ln for ln in rendered.splitlines()
        if ln.strip().startswith(("build_branch=", "recovery_hint:"))
    ]
    assert lines_for_child  # at least one mention
    assert follow_lines == []  # no follow-on indented lines emitted


def test_step0b_prompt_enumerates_commit_allowlist_and_runtime_excludes() -> None:
    """Step 0b recovery must scope commits instead of inviting add-all."""
    prompt = Path("otto/prompts/lead-integration.md").read_text(encoding="utf-8")

    assert "Stage only legitimate product paths" in prompt
    for allowed in [
        "frontend/",
        "api/",
        "CHARTER.md",
        "decisions.md",
        "package.json",
        "pyproject.toml",
    ]:
        assert allowed in prompt
    for excluded in [
        ".worktrees/",
        "otto_logs/",
        "uploads/",
        "*.db",
        "*.db.bak",
    ]:
        assert excluded in prompt
    assert "Never use `git add -A`" in prompt
    assert "`git diff --cached --name-only`" in prompt


def test_integration_prompt_requires_self_commit_with_integration_tag() -> None:
    """Integration prompt is a secondary guard behind runner enforcement."""
    prompt = Path("otto/prompts/lead-integration.md").read_text(encoding="utf-8")

    assert "MUST commit those edits yourself before yielding" in prompt
    assert "message tagged `integration:`" in prompt
    assert "`git status --short` before and" in prompt
    assert "`git add .`" in prompt


def test_leaf_prompt_commit_hygiene_scopes_pathspecs() -> None:
    """Leaf agents should not self-commit runtime or sibling files."""
    prompt = Path("otto/prompts/lead.md").read_text(encoding="utf-8")

    assert "Never use `git add -A`" in prompt
    assert "Stage explicit product paths only" in prompt
    assert "decisions.md" in prompt
    for excluded in [".worktrees/", "otto_logs/", "uploads/", "*.db.bak"]:
        assert excluded in prompt


def test_default_gitignore_covers_runtime_artifacts() -> None:
    """The runtime-artifacts patterns (db/sqlite/log/pid) must be in
    the default gitignore. Live run on 2026-05-13 hit
    `api/itracker.db` as a merge conflict — same shape as
    tsbuildinfo."""
    from otto.v5_branching import _DEFAULT_GITIGNORE
    assert "*.db" in _DEFAULT_GITIGNORE
    assert "*.sqlite" in _DEFAULT_GITIGNORE
    assert "*.log" in _DEFAULT_GITIGNORE
    # SQLite WAL-mode sidecars (Codex review found these missing)
    assert "*-wal" in _DEFAULT_GITIGNORE
    assert "*-shm" in _DEFAULT_GITIGNORE
    assert "*.db-journal" in _DEFAULT_GITIGNORE


def test_managed_gitignore_idempotent(tmp_path: Path) -> None:
    """Running the helper twice produces an identical file (no
    accumulating duplicate blocks)."""
    from otto.v5_branching import _apply_managed_gitignore
    gi = tmp_path / ".gitignore"
    assert _apply_managed_gitignore(gi) is True
    snapshot = gi.read_text()
    # Second call: should detect block exists + content current; no-op.
    assert _apply_managed_gitignore(gi) is False
    assert gi.read_text() == snapshot


def test_managed_gitignore_preserves_user_content(tmp_path: Path) -> None:
    """Patterns outside the managed BEGIN/END markers stay untouched
    across reapplications."""
    from otto.v5_branching import (
        _apply_managed_gitignore,
        _OTTO_GITIGNORE_BEGIN,
        _OTTO_GITIGNORE_END,
    )
    gi = tmp_path / ".gitignore"
    gi.write_text(
        f"my-user-pattern.txt\nsome-dir/\n\n"
        f"{_OTTO_GITIGNORE_BEGIN}\nold-otto-patterns\n{_OTTO_GITIGNORE_END}\n\n"
        f"more-user-patterns\n"
    )
    assert _apply_managed_gitignore(gi) is True
    new = gi.read_text()
    # User content preserved.
    assert "my-user-pattern.txt" in new
    assert "some-dir/" in new
    assert "more-user-patterns" in new
    # Old managed content replaced.
    assert "old-otto-patterns" not in new
    # New managed patterns present.
    assert "*.db" in new


def _init_git(repo: Path) -> None:
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)


def _commit(repo: Path, content: str, branch: str | None = None) -> str:
    import subprocess
    if branch:
        subprocess.run(["git", "checkout", "-q", "-b", branch], cwd=repo, check=True)
    f = repo / "f.txt"
    f.write_text(content)
    subprocess.run(["git", "add", "f.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", f"{content[:20]}"], cwd=repo, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()


def test_reconcile_recovered_children_flips_merged_to_pass(tmp_path: Path) -> None:
    """Integration recovers a merge_blocked child by `git merge`-ing its
    build branch. The reconciler detects ``build_branch`` is an ancestor
    of the integration branch and updates the verdict pass."""
    from otto.v5_runner import _reconcile_recovered_children
    from otto.v5_branching import child_branch_name
    import subprocess

    _init_git(tmp_path)
    _commit(tmp_path, "initial")
    # Build branch for child v5-foo: has its own commit
    child_tid = "v5-foo"
    child_branch = child_branch_name(child_tid)
    _commit(tmp_path, "child work", branch=child_branch)
    subprocess.run(["git", "checkout", "-q", "main"], cwd=tmp_path, check=True)
    # Simulate Step 0b: integration agent merges child_branch into main.
    subprocess.run(
        ["git", "merge", "-q", "--no-ff", "--no-edit", child_branch],
        cwd=tmp_path, check=True,
    )

    # Seed task graph: root + child marked merge_blocked.
    record_task(tmp_path, task_id="root", intent="r", parent_task_id=None)
    record_task(tmp_path, task_id=child_tid, intent="c", parent_task_id="root")
    set_verdict(tmp_path, child_tid, "merge_blocked")

    n = _reconcile_recovered_children(tmp_path, "root")
    assert n == 1

    from otto.queue.task_graph import get_task
    final = get_task(tmp_path, child_tid)
    assert final is not None
    assert final.get("verdict") == "pass"


def test_reconcile_leaves_unrecovered_children_alone(tmp_path: Path) -> None:
    """If the integration agent didn't merge the child's branch, the
    child stays merge_blocked (don't fake success)."""
    from otto.v5_runner import _reconcile_recovered_children
    from otto.v5_branching import child_branch_name

    _init_git(tmp_path)
    _commit(tmp_path, "initial")
    child_tid = "v5-orphan"
    child_branch = child_branch_name(child_tid)
    _commit(tmp_path, "child work", branch=child_branch)
    # Important: do NOT merge child_branch into main.
    import subprocess
    subprocess.run(["git", "checkout", "-q", "main"], cwd=tmp_path, check=True)

    record_task(tmp_path, task_id="root", intent="r", parent_task_id=None)
    record_task(tmp_path, task_id=child_tid, intent="c", parent_task_id="root")
    set_verdict(tmp_path, child_tid, "merge_blocked")

    n = _reconcile_recovered_children(tmp_path, "root")
    assert n == 0

    from otto.queue.task_graph import get_task
    final = get_task(tmp_path, child_tid)
    assert final is not None
    assert final.get("verdict") == "merge_blocked"  # preserved honest verdict


def test_managed_gitignore_migrates_legacy_sentinel(tmp_path: Path) -> None:
    """Files written with the old single-line sentinel format get
    upgraded to the BEGIN/END block on next apply, without losing
    pre-sentinel user content."""
    from otto.v5_branching import (
        _apply_managed_gitignore,
        _OTTO_GITIGNORE_LEGACY_SENTINEL,
        _OTTO_GITIGNORE_BEGIN,
        _OTTO_GITIGNORE_END,
    )
    gi = tmp_path / ".gitignore"
    gi.write_text(
        f"my-user-pattern\n\n{_OTTO_GITIGNORE_LEGACY_SENTINEL}\n"
        f"old-legacy-stuff\n"
    )
    assert _apply_managed_gitignore(gi) is True
    new = gi.read_text()
    assert "my-user-pattern" in new
    assert "old-legacy-stuff" not in new
    assert _OTTO_GITIGNORE_BEGIN in new
    assert _OTTO_GITIGNORE_END in new
    assert "*.db" in new  # new patterns now present
