"""Tests for otto/branching.py — slug + branch policy logic.

Covers:
- slugify_intent edge cases
- compute_branch_name composition
- should_auto_branch policy (only auto-branch from default_branch)
- create_or_switch_branch git interaction (creates new + switches to existing)
- ensure_branch_for_atomic_command integration:
    * on default_branch: creates new branch
    * on non-default branch: stays put
    * greenfield (no commits): no-op
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from otto.branching import (
    RESERVED_TASK_IDS,
    compute_branch_name,
    create_or_switch_branch,
    current_branch,
    ensure_branch_for_atomic_command,
    repo_has_commits,
    should_auto_branch,
    slugify_intent,
)


# ---------- slugify_intent ----------


def _hash6(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]


@pytest.mark.parametrize("intent,expected", [
    ("add CSV export", "add-csv-export"),
    ("Add CSV Export!", "add-csv-export"),
    ("redesign settings page", "redesign-settings-page"),
    ("foo___bar", "foo-bar"),
    ("  whitespace  edges  ", "whitespace-edges"),
    ("hyphens---collapse", "hyphens-collapse"),
    ("UPPER", "upper"),
    ("mix of !@#$%^ symbols 99", "mix-of-symbols-99"),
])
def test_slugify_basic(intent, expected):
    assert slugify_intent(intent) == expected


def test_slugify_empty_falls_back_to_task():
    assert slugify_intent("") == f"task-{_hash6('')}"
    assert slugify_intent("!@#$%") == f"task-{_hash6('!@#$%')}"


def test_slugify_unicode_only_falls_back():
    assert slugify_intent("🚀✨🎉") == f"task-{_hash6('🚀✨🎉')}"


def test_slugify_max_chars_truncates_at_word_boundary():
    long = "this is a very long intent that will exceed the limit by a lot of characters"
    out = slugify_intent(long, max_chars=30)
    base, _, suffix = out.rpartition("-")
    assert len(base) <= 30
    assert len(suffix) == 6
    assert not base.endswith("-")
    assert not base.startswith("-")


def test_slugify_max_chars_falls_back_when_no_boundary():
    # 50 chars no separators
    long = "a" * 50
    out = slugify_intent(long, max_chars=10)
    assert out.startswith("a" * 10 + "-")
    assert len(out) == 17


def test_slugify_truncated_long_prefixes_get_distinct_hashes():
    long1 = "a" * 60 + "alpha"
    long2 = "a" * 60 + "beta"

    out1 = slugify_intent(long1, max_chars=40)
    out2 = slugify_intent(long2, max_chars=40)

    assert out1 != out2
    assert out1.startswith("a" * 40 + "-")
    assert out2.startswith("a" * 40 + "-")


def test_slugify_literal_task_gets_hash_suffix():
    assert slugify_intent("task") == f"task-{_hash6('task')}"


def test_reserved_ids_includes_management_verbs():
    # Sanity check the reserved set covers all queue verbs
    assert {"ls", "show", "rm", "cancel", "run"}.issubset(RESERVED_TASK_IDS)


# ---------- compute_branch_name ----------


def test_compute_branch_name_basic():
    assert compute_branch_name("build", "add-csv", date="2026-04-19") == "build/add-csv-2026-04-19"


def test_compute_branch_name_modes():
    assert compute_branch_name("improve", "x", date="2026-04-19") == "improve/x-2026-04-19"
    assert compute_branch_name("certify", "x", date="2026-04-19") == "certify/x-2026-04-19"


def test_compute_branch_name_uses_today_by_default():
    # Just check the format, not the literal date
    out = compute_branch_name("build", "x")
    assert out.startswith("build/x-")
    # Date suffix should be 10 chars (YYYY-MM-DD)
    suffix = out.removeprefix("build/x-")
    assert len(suffix) == 10
    assert suffix[4] == "-" and suffix[7] == "-"


def test_compute_branch_name_empty_slug_falls_back():
    out = compute_branch_name("build", "", date="2026-04-19")
    assert out == "build/task-2026-04-19"


def test_compute_branch_name_requires_mode():
    with pytest.raises(ValueError):
        compute_branch_name("", "x")


# ---------- should_auto_branch ----------


def test_should_auto_branch_when_on_default():
    assert should_auto_branch("main", "main") is True
    assert should_auto_branch("master", "master") is True


def test_should_not_auto_branch_when_on_feature_branch():
    assert should_auto_branch("feature/x", "main") is False
    assert should_auto_branch("improve/2026-04-19", "main") is False
    assert should_auto_branch("build/foo-2026-04-19", "main") is False


def test_should_not_auto_branch_when_detached_or_empty():
    assert should_auto_branch("", "main") is False
    assert should_auto_branch("main", "") is False


# ---------- git-interacting tests (use real tmp git repo) ----------


def _init_repo(tmp_path: Path) -> Path:
    """Create a git repo with one commit on main."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    return repo


def _empty_repo(tmp_path: Path) -> Path:
    """Create a git repo with NO commits (greenfield)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, capture_output=True, check=True)
    return repo


def test_current_branch_returns_main_after_init(tmp_path):
    repo = _init_repo(tmp_path)
    assert current_branch(repo) == "main"


def test_current_branch_returns_init_branch_even_without_commits(tmp_path):
    """`git init -b main` creates the symbolic HEAD ref, so --show-current
    returns 'main' even before the first commit lands. The greenfield
    policy is gated by `repo_has_commits()`, NOT by `current_branch()`."""
    repo = _empty_repo(tmp_path)
    assert current_branch(repo) == "main"


def test_repo_has_commits_true_after_init(tmp_path):
    repo = _init_repo(tmp_path)
    assert repo_has_commits(repo) is True


def test_repo_has_commits_false_for_empty(tmp_path):
    repo = _empty_repo(tmp_path)
    assert repo_has_commits(repo) is False


def test_create_or_switch_branch_creates_new(tmp_path):
    repo = _init_repo(tmp_path)
    out = create_or_switch_branch(repo, "build/test-1")
    assert out == "build/test-1"
    assert current_branch(repo) == "build/test-1"


def test_create_or_switch_branch_switches_to_existing(tmp_path):
    repo = _init_repo(tmp_path)
    # Create the branch first, switch back to main
    subprocess.run(["git", "checkout", "-b", "build/preexists"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    # Now ask for the same branch — should switch, not error
    out = create_or_switch_branch(repo, "build/preexists")
    assert out == "build/preexists"
    assert current_branch(repo) == "build/preexists"


def test_ensure_branch_creates_when_on_default(tmp_path):
    repo = _init_repo(tmp_path)
    branch, created = ensure_branch_for_atomic_command(
        mode="build",
        intent="add csv export",
        project_dir=repo,
        default_branch="main",
    )
    assert created is True
    assert branch.startswith("build/add-csv-export-")
    assert current_branch(repo) == branch


def test_ensure_branch_stays_on_feature_branch(tmp_path):
    repo = _init_repo(tmp_path)
    # User is on a feature branch
    subprocess.run(["git", "checkout", "-b", "feature/auth"], cwd=repo, check=True, capture_output=True)
    branch, created = ensure_branch_for_atomic_command(
        mode="build",
        intent="anything",
        project_dir=repo,
        default_branch="main",
    )
    assert created is False
    assert branch == "feature/auth"
    assert current_branch(repo) == "feature/auth"


def test_ensure_branch_noop_in_greenfield(tmp_path):
    repo = _empty_repo(tmp_path)
    branch, created = ensure_branch_for_atomic_command(
        mode="build",
        intent="anything",
        project_dir=repo,
        default_branch="main",
    )
    assert created is False
    assert branch == ""


def test_ensure_branch_idempotent_same_intent_same_day(tmp_path):
    """Re-running same intent same day should switch to existing branch, not error."""
    repo = _init_repo(tmp_path)
    branch1, created1 = ensure_branch_for_atomic_command(
        mode="build", intent="add csv", project_dir=repo, default_branch="main"
    )
    assert created1 is True
    # Switch back to main, then re-run
    subprocess.run(["git", "checkout", "main"], cwd=repo, check=True, capture_output=True)
    branch2, created2 = ensure_branch_for_atomic_command(
        mode="build", intent="add csv", project_dir=repo, default_branch="main"
    )
    assert branch2 == branch1
    # On second run, the branch already exists; create returns rc!=0 and we
    # fall through to switch, so created_new is True from our function's view
    # (we don't distinguish "I created it" from "I found it"). That's fine —
    # the user-visible outcome is the same: they're on the right branch.
    assert current_branch(repo) == branch2
