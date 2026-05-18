"""Regression: otto's own .otto/ side-channel must be in the managed
.gitignore so it never dirties git_checkout_clean.

Context (v5-itracker-setupfix-224814, 2026-05-18): otto writes its
merge-conflict side-channel to `<project>/.otto/merge-conflicts/` DURING
integration (otto/v5_branching.py). The otto-managed .gitignore block
(_DEFAULT_GITIGNORE) ignored otto_logs/, .worktrees/, etc. but NOT .otto/,
so `?? .otto/` left the integrated tree dirty and blocked
`git_checkout_clean`. A repair agent DID fix it correctly in ~65s (added
.otto/ to .gitignore, committed) — so this is preventive hygiene, not the
cause of that run's terminal merge_blocked — but otto must not leak its own
runtime side-channel into the product tree in the first place
(feedback_otto_owned_leakage; same class as otto_logs/ already in the
block). is_otto_owned_path() already recognizes ".otto" — the only gap was
the managed .gitignore not listing it.
"""

from __future__ import annotations

from otto.v5_branching import _DEFAULT_GITIGNORE


def test_managed_gitignore_ignores_dototto_sidechannel() -> None:
    lines = {ln.strip() for ln in _DEFAULT_GITIGNORE.splitlines()}
    # Both the trailing-slash and no-slash variants, matching the
    # symlink-safety pattern otto uses for otto_logs/.worktrees.
    assert ".otto/" in lines, ".otto/ must be in the otto-managed .gitignore"
    assert ".otto" in lines, ".otto (no-slash, symlink-safe) must also be present"
    # Sanity: it sits with the other otto runtime artifacts, not removed
    # alongside a future edit to otto_logs.
    assert "otto_logs/" in lines


def test_dototto_owned_path_is_runner_committable_signal() -> None:
    # Defense in depth: the mechanical preflight fast-path relies on
    # is_otto_owned_path to classify .otto/ as otto-owned dirt (so a tree
    # already dirtied before a managed-gitignore refresh is still
    # mechanically resolvable rather than burning an LLM repair wall).
    from otto.setup_gitignore import is_otto_owned_path

    assert is_otto_owned_path(".otto/")
    assert is_otto_owned_path(".otto/merge-conflicts/latest.json")
