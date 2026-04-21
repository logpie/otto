"""Tests for otto.merge.conflict_agent."""

from __future__ import annotations

import subprocess
from pathlib import Path

from otto.merge.conflict_agent import (
    _files_with_markers,
    validate_post_agent,
)


def test_files_with_markers_detects_marker_lines(tmp_path: Path):
    """`_files_with_markers` scans on-disk content for conflict marker
    lines. Used by the consolidated path where markers live in committed
    files (and `git diff --check` can't see them)."""
    clean = tmp_path / "clean.py"
    clean.write_text("def f():\n    return 1\n")
    dirty = tmp_path / "dirty.py"
    dirty.write_text(
        "def f():\n"
        "<<<<<<< HEAD\n"
        "    return 1\n"
        "=======\n"
        "    return 2\n"
        ">>>>>>> branch\n"
    )
    assert _files_with_markers(tmp_path, ["clean.py"]) == []
    assert _files_with_markers(tmp_path, ["dirty.py"]) == ["dirty.py"]
    # Both — only dirty.py shows up
    assert _files_with_markers(tmp_path, ["clean.py", "dirty.py"]) == ["dirty.py"]
    # Nonexistent path — silently skipped
    assert _files_with_markers(tmp_path, ["nope.py"]) == []


def test_files_with_markers_flags_partial_start_marker(tmp_path: Path):
    partial = tmp_path / "partial_start.py"
    partial.write_text("def f():\n<<<<<<< HEAD\n    return 1\n")

    assert _files_with_markers(tmp_path, ["partial_start.py"]) == ["partial_start.py"]


def test_files_with_markers_flags_partial_start_and_separator(tmp_path: Path):
    partial = tmp_path / "partial_mid.py"
    partial.write_text(
        "def f():\n"
        "<<<<<<< HEAD\n"
        "    return 1\n"
        "=======\n"
        "    return 2\n"
    )

    assert _files_with_markers(tmp_path, ["partial_mid.py"]) == ["partial_mid.py"]


def test_files_with_markers_flags_marker_like_docstring_line_in_conflict_set(tmp_path: Path):
    """Conflict-set files fail closed on any column-zero marker line."""
    doc = tmp_path / "doc_example.py"
    doc.write_text(
        '"""\n'
        ">>>>>>> feature/demo\n"
        '"""\n'
        "def f():\n"
        "    return 'ok'\n"
    )

    assert _files_with_markers(tmp_path, ["doc_example.py"]) == ["doc_example.py"]


def test_files_with_markers_flags_large_files_via_streaming_scan(tmp_path: Path):
    huge = tmp_path / "huge.py"
    huge.write_bytes((b"x\n" * (6 * 1024 * 1024)) + b"<<<<<<< HEAD\n")

    assert huge.stat().st_size > 10 * 1024 * 1024
    assert _files_with_markers(tmp_path, ["huge.py"]) == ["huge.py"]


def test_validate_post_agent_catches_committed_markers(tmp_path: Path):
    """`validate_post_agent` catches markers that live in committed files
    (where `git diff --check` is blind) by content-scanning expected_uu_files."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    p = tmp_path / "f.py"
    p.write_text("<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> b\n")
    subprocess.run(["git", "add", "f.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "marker commit"], cwd=tmp_path, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()

    ok, err = validate_post_agent(
        project_dir=tmp_path,
        pre_diff_files=set(),
        expected_uu_files={"f.py"},
        pre_untracked_files=set(),
        pre_head=head,
    )
    assert not ok
    assert err is not None and "f.py" in err and "markers" in err


def test_validate_post_agent_passes_clean_tree(tmp_path: Path):
    """Clean tree, expected_uu_files empty, HEAD unchanged → passes."""
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "f.py").write_text("def f(): return 1\n")
    subprocess.run(["git", "add", "f.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=tmp_path, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()

    ok, err = validate_post_agent(
        project_dir=tmp_path,
        pre_diff_files=set(),
        expected_uu_files=set(),
        pre_untracked_files=set(),
        pre_head=head,
    )
    assert ok, f"clean tree should pass; got err={err}"
