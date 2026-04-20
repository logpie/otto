"""Tests for otto/setup_gitattributes.py — Phase 1.6 bookkeeping merge drivers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from otto.setup_gitattributes import (
    REQUIRED_RULES,
    GitAttributesConflict,
    assert_setup,
    check_compatibility,
    install,
    is_setup,
)
from tests._helpers import init_repo


# ---------- check_compatibility ----------


def test_compat_clean_repo_is_compatible(tmp_path: Path):
    repo = init_repo(tmp_path, initial_commit=False)
    ok, conflicts = check_compatibility(repo)
    assert ok is True
    assert conflicts == []


def test_compat_existing_compatible_rules_pass(tmp_path: Path):
    repo = init_repo(tmp_path, initial_commit=False)
    (repo / ".gitattributes").write_text(
        "intent.md merge=union\n"
        "otto.yaml merge=ours\n"
    )
    ok, _ = check_compatibility(repo)
    assert ok is True


def test_compat_conflicting_rule_blocks(tmp_path: Path):
    repo = init_repo(tmp_path, initial_commit=False)
    (repo / ".gitattributes").write_text("intent.md merge=binary\n")
    ok, conflicts = check_compatibility(repo)
    assert ok is False
    assert any("intent.md" in c and "binary" in c for c in conflicts)


def test_compat_unrelated_rules_dont_interfere(tmp_path: Path):
    repo = init_repo(tmp_path, initial_commit=False)
    (repo / ".gitattributes").write_text("*.png binary\n*.jpg binary\n")
    ok, _ = check_compatibility(repo)
    assert ok is True


# ---------- install ----------


def test_install_creates_gitattributes_when_missing(tmp_path: Path):
    repo = init_repo(tmp_path, initial_commit=False)
    changed = install(repo, register_ours_driver=False)
    assert changed is True
    content = (repo / ".gitattributes").read_text()
    assert "intent.md merge=union" in content
    assert "otto.yaml merge=ours" in content


def test_install_appends_to_existing_gitattributes(tmp_path: Path):
    repo = init_repo(tmp_path, initial_commit=False)
    (repo / ".gitattributes").write_text("*.png binary\n")
    changed = install(repo, register_ours_driver=False)
    assert changed is True
    content = (repo / ".gitattributes").read_text()
    assert "*.png binary" in content
    assert "intent.md merge=union" in content


def test_install_idempotent(tmp_path: Path):
    repo = init_repo(tmp_path, initial_commit=False)
    install(repo, register_ours_driver=False)
    content_before = (repo / ".gitattributes").read_text()
    changed = install(repo, register_ours_driver=False)
    assert changed is False
    content_after = (repo / ".gitattributes").read_text()
    assert content_before == content_after


def test_install_raises_on_conflict(tmp_path: Path):
    repo = init_repo(tmp_path, initial_commit=False)
    (repo / ".gitattributes").write_text("intent.md merge=binary\n")
    with pytest.raises(GitAttributesConflict, match="intent.md"):
        install(repo, register_ours_driver=False)


def test_install_partial_existing_completes_setup(tmp_path: Path):
    """If only one of the two required rules is present, install adds the missing one."""
    repo = init_repo(tmp_path, initial_commit=False)
    (repo / ".gitattributes").write_text("intent.md merge=union\n")
    changed = install(repo, register_ours_driver=False)
    assert changed is True
    content = (repo / ".gitattributes").read_text()
    assert content.count("intent.md merge=union") == 1
    assert "otto.yaml merge=ours" in content


# ---------- is_setup / assert_setup ----------


def test_is_setup_false_when_missing(tmp_path: Path):
    repo = init_repo(tmp_path, initial_commit=False)
    assert is_setup(repo) is False


def test_is_setup_true_after_install(tmp_path: Path):
    repo = init_repo(tmp_path, initial_commit=False)
    install(repo, register_ours_driver=False)
    assert is_setup(repo) is True


def test_assert_setup_raises_when_missing(tmp_path: Path):
    repo = init_repo(tmp_path, initial_commit=False)
    with pytest.raises(GitAttributesConflict, match="Missing required"):
        assert_setup(repo)


def test_assert_setup_raises_on_conflict(tmp_path: Path):
    repo = init_repo(tmp_path, initial_commit=False)
    (repo / ".gitattributes").write_text("intent.md merge=binary\n")
    with pytest.raises(GitAttributesConflict, match="intent.md"):
        assert_setup(repo)


def test_assert_setup_passes_after_install(tmp_path: Path):
    repo = init_repo(tmp_path, initial_commit=False)
    install(repo, register_ours_driver=False)
    assert_setup(repo)


# ---------- end-to-end: union driver actually works in git merge ----------


def test_union_driver_merges_intent_md_without_conflict(tmp_path: Path):
    """E2E: setup → make two parallel branches that both modify intent.md → merge → no conflict."""
    repo = init_repo(tmp_path, initial_commit=False)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)

    install(repo)  # also registers the ours driver

    # Initial commit with .gitattributes + intent.md
    (repo / "intent.md").write_text("# Intent log\n\nbase line\n")
    subprocess.run(["git", "add", ".gitattributes", "intent.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)

    # Branch A: append
    subprocess.run(["git", "checkout", "-b", "build/a"], cwd=repo, capture_output=True, check=True)
    (repo / "intent.md").write_text("# Intent log\n\nbase line\n\nA's intent\n")
    subprocess.run(["git", "commit", "-aq", "-m", "A"], cwd=repo, check=True)

    # Branch B from main: also append (different content)
    subprocess.run(["git", "checkout", "main"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "checkout", "-b", "build/b"], cwd=repo, capture_output=True, check=True)
    (repo / "intent.md").write_text("# Intent log\n\nbase line\n\nB's intent\n")
    subprocess.run(["git", "commit", "-aq", "-m", "B"], cwd=repo, check=True)

    # Merge A first, then B — union driver should auto-merge intent.md
    subprocess.run(["git", "checkout", "main"], cwd=repo, capture_output=True, check=True)
    r1 = subprocess.run(["git", "merge", "--no-ff", "-q", "-m", "merge A", "build/a"],
                        cwd=repo, capture_output=True, text=True)
    assert r1.returncode == 0, f"first merge failed: {r1.stderr}"
    r2 = subprocess.run(["git", "merge", "--no-ff", "-q", "-m", "merge B", "build/b"],
                        cwd=repo, capture_output=True, text=True)
    assert r2.returncode == 0, f"second merge should auto-resolve via union driver: {r2.stderr}"

    final = (repo / "intent.md").read_text()
    # Both intents present (union)
    assert "A's intent" in final
    assert "B's intent" in final
    # No conflict markers
    assert "<<<<<<<" not in final
    assert ">>>>>>>" not in final


def test_required_rules_are_what_we_documented():
    """Sanity: the constant matches what we tell users in messages."""
    rules = dict(((p, a), v) for p, a, v in REQUIRED_RULES)
    assert rules.get(("intent.md", "merge")) == "union"
    assert rules.get(("otto.yaml", "merge")) == "ours"
