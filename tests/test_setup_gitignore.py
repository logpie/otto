"""Tests for otto.setup_gitignore — F4 fix."""

from __future__ import annotations

from pathlib import Path

from otto.setup_gitignore import (
    COMMON_BUILD_ARTIFACT_PATTERNS,
    COMMON_HEADER,
    OTTO_HEADER,
    OTTO_PATTERNS,
    ensure_gitignore,
)


def test_ensure_gitignore_creates_new_file(tmp_path: Path):
    changed = ensure_gitignore(tmp_path)
    assert changed is True
    text = (tmp_path / ".gitignore").read_text()
    for pat in OTTO_PATTERNS:
        assert pat in text, f"missing pattern in fresh file: {pat!r}\n{text}"
    assert OTTO_HEADER in text


def test_ensure_gitignore_adds_common_build_artifacts_F14(tmp_path: Path):
    """F14: common build artifacts (__pycache__, node_modules, etc.) must
    land in .gitignore so the merge validator doesn't bail when the agent
    runs project tests and produces artifacts."""
    ensure_gitignore(tmp_path)
    text = (tmp_path / ".gitignore").read_text()
    for pat in COMMON_BUILD_ARTIFACT_PATTERNS:
        assert pat in text, f"F14: missing common artifact {pat!r}\n{text}"
    assert COMMON_HEADER in text
    # Concrete high-signal patterns
    assert "__pycache__/" in text
    assert "node_modules/" in text
    assert ".pytest_cache/" in text


def test_ensure_gitignore_preserves_existing(tmp_path: Path):
    user_text = "# my project\nbuild/\n*.log\n"
    (tmp_path / ".gitignore").write_text(user_text)
    changed = ensure_gitignore(tmp_path)
    assert changed is True
    text = (tmp_path / ".gitignore").read_text()
    # User content kept verbatim
    assert text.startswith(user_text)
    for pat in OTTO_PATTERNS:
        assert pat in text


def test_ensure_gitignore_is_idempotent(tmp_path: Path):
    ensure_gitignore(tmp_path)
    text1 = (tmp_path / ".gitignore").read_text()
    changed = ensure_gitignore(tmp_path)
    assert changed is False
    text2 = (tmp_path / ".gitignore").read_text()
    assert text1 == text2


def test_ensure_gitignore_partial_overlap(tmp_path: Path):
    """If user already has SOME otto patterns (e.g. otto_logs/), only add the rest."""
    (tmp_path / ".gitignore").write_text("otto_logs/\n.worktrees/\n")
    changed = ensure_gitignore(tmp_path)
    assert changed is True
    text = (tmp_path / ".gitignore").read_text()
    # No duplication of existing entries
    assert text.count("otto_logs/") == 1
    assert text.count(".worktrees/") == 1
    # New entries added
    assert ".otto-queue.yml" in text
