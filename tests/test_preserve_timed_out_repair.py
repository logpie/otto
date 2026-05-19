"""Task #8 (Part B): on the escalated/timed-out integration-repair path,
the runner must PRESERVE the repair agent's work (commit the worktree it
was killed mid-committing) instead of discarding it. Linkboard e2e:
tsc-clean BookmarksPage.tsx fix killed mid `git add -A && git commit` at
the 1199s wall → discarded. Locked invariant: bugs acceptable, thrown-away
work is not. The helper imports git_status_porcelain/commit_worktree from
otto.v5_branching at call time, so patch THERE.
"""
from pathlib import Path

from otto import v5_runner as R


def test_preserves_dirty_repair_work(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "otto.v5_branching.git_status_porcelain",
        lambda p: [" M frontend/src/pages/BookmarksPage.tsx"],
    )
    monkeypatch.setattr(
        "otto.v5_branching.commit_worktree",
        lambda *, worktree_path, message: (
            calls.append(message) or (True, "committed 1 file")
        ),
    )
    ok, _ = R._preserve_timed_out_repair_work(Path("/tmp/none"))
    assert ok is True, "must commit the timed-out repair work, not discard it"
    assert calls, "commit_worktree must be invoked on dirty escalated path"


def test_clean_worktree_is_noop(monkeypatch):
    committed = []
    monkeypatch.setattr("otto.v5_branching.git_status_porcelain", lambda p: [])
    monkeypatch.setattr(
        "otto.v5_branching.commit_worktree",
        lambda *, worktree_path, message: committed.append(1) or (True, "x"),
    )
    ok, detail = R._preserve_timed_out_repair_work(Path("/tmp/none"))
    assert ok is False
    assert "clean" in detail.lower()
    assert not committed, "must NOT commit when worktree is clean"
