"""Root-cause regression: a child that built+PASSED but whose branch never
reached the parent integration branch must be RE-MERGED, not silently
demoted to merge_blocked.

Observed in run #16 (and every resume of it): children_and_subtree_integration
was budget-killed AFTER feature v5-c113ef7b192e passed but BEFORE its upward
merge into main. Its per-child merge only ever runs inside _run_child for
freshly-built children, so on resume (where it's non-runnable, verdict=pass)
the merge never happens. _verify_child_branches_reached_parent detected the
unmerged branch but only `set_verdict(merge_blocked)` — silently dropping a
completed, passing feature and shipping an incomplete product (frontend
clean_deploy then failed because issues/cycles/labels code was absent).

Fix: _verify_child_branches_reached_parent now re-attempts the canonical
merge_child_into_integration before demoting; only a genuine conflict (merge
fails / branch still unreachable) becomes merge_blocked.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from otto.queue.task_graph import record_task, set_verdict
from otto.v5_runner import ROOT_TASK_ID, _verify_child_branches_reached_parent
from otto.v5_branching import child_branch_name


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(tmp: Path) -> None:
    _git(tmp, "init", "-q")
    _git(tmp, "config", "user.email", "t@t")
    _git(tmp, "config", "user.name", "t")
    # otto gitignores otto_logs/ in real runs; mirror that so the task graph
    # is never swept into a feature-branch commit and lost on checkout.
    (tmp / ".gitignore").write_text("otto_logs/\n.otto/\n")
    (tmp / "base.txt").write_text("base\n")
    _git(tmp, "add", "-A")
    _git(tmp, "commit", "-qm", "base")
    cur = _git(tmp, "rev-parse", "--abbrev-ref", "HEAD")
    if cur != "main":
        _git(tmp, "branch", "-M", "main")


def _seed_passed_child(tmp: Path, child_id: str) -> str:
    """A passed leaf child whose build branch has work NOT merged to main."""
    bb = child_branch_name(child_id)
    _git(tmp, "checkout", "-q", "-b", bb)
    (tmp / "feature.txt").write_text("the feature's real code\n")
    _git(tmp, "add", "-A")
    _git(tmp, "commit", "-qm", "feature work")
    _git(tmp, "checkout", "-q", "main")  # branch NOT merged to main
    # Seed the task graph last, on main, AFTER all branch work (otto_logs is
    # gitignored so it stays put across checkouts).
    record_task(tmp, task_id=ROOT_TASK_ID, intent="root")
    record_task(tmp, task_id=child_id, intent="feat", parent_task_id=ROOT_TASK_ID)
    set_verdict(tmp, ROOT_TASK_ID, "pending_children")
    set_verdict(tmp, child_id, "pass")
    return bb


def test_unmerged_passed_child_is_remerged_not_demoted(tmp_path: Path) -> None:
    _repo(tmp_path)
    cid = "v5-aaaaaaaaaaaa"
    bb = _seed_passed_child(tmp_path, cid)
    # Precondition: feature branch is NOT an ancestor of main, child is pass.
    anc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", bb, "main"],
        cwd=str(tmp_path), capture_output=True,
    )
    assert anc.returncode != 0, "precondition: branch must be unmerged"

    _verify_child_branches_reached_parent(
        project_dir=tmp_path, parent_task_id=ROOT_TASK_ID
    )

    # The fix must RE-MERGE it (work recovered), NOT demote to merge_blocked.
    from otto.queue.task_graph import get_task

    assert (get_task(tmp_path, cid) or {}).get("verdict") == "pass", (
        "passed child with truncated merge must be recovered, not dropped"
    )
    anc2 = subprocess.run(
        ["git", "merge-base", "--is-ancestor", bb, "main"],
        cwd=str(tmp_path), capture_output=True,
    )
    assert anc2.returncode == 0, "feature branch must now reach main"
    assert (tmp_path / "feature.txt").read_text() == "the feature's real code\n"


def test_genuine_conflict_still_becomes_merge_blocked(tmp_path: Path) -> None:
    _repo(tmp_path)
    cid = "v5-bbbbbbbbbbbb"
    bb = child_branch_name(cid)
    _git(tmp_path, "checkout", "-q", "-b", bb)
    (tmp_path / "base.txt").write_text("CHILD VERSION\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "child edits base.txt")
    _git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "base.txt").write_text("MAIN VERSION\n")  # conflicting edit
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "main edits base.txt")
    # Seed the task graph last, on main, after branch work.
    record_task(tmp_path, task_id=ROOT_TASK_ID, intent="root")
    record_task(tmp_path, task_id=cid, intent="feat", parent_task_id=ROOT_TASK_ID)
    set_verdict(tmp_path, ROOT_TASK_ID, "pending_children")
    set_verdict(tmp_path, cid, "pass")

    _verify_child_branches_reached_parent(
        project_dir=tmp_path, parent_task_id=ROOT_TASK_ID
    )

    from otto.queue.task_graph import get_task

    # A real conflict cannot be auto-merged → correctly merge_blocked
    # (anti-false-pass preserved: we never fake a merge).
    assert (get_task(tmp_path, cid) or {}).get("verdict") == "merge_blocked"
    assert _git(tmp_path, "rev-parse", "--abbrev-ref", "HEAD") == "main"
