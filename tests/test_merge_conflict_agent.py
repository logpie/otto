"""Tests for otto.merge.conflict_agent."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from otto.merge.conflict_agent import (
    ConflictContext,
    ConflictResolutionAttempt,
    _files_with_markers,
    resolve_one_conflict,
    validate_post_agent,
)


class _StubOptions:
    def __init__(self) -> None:
        self.disallowed_tools: list[str] = []
        self.model: str | None = None


def test_conflict_agent_disallows_bash_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """`disallowed_tools` must contain 'Bash' but NOT 'Write'. Disallowing
    Write was measured 2-3× slower because Edit-only forced multi-pass
    plan→edit→verify cycles. Drift is caught post-agent by
    `validate_post_agent` instead."""
    # Minimal git repo so head_sha/changed_files don't crash
    import subprocess
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("hello\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "i"], cwd=tmp_path, check=True)

    captured: dict[str, Any] = {}

    def fake_make_options(project_dir: Path, config: dict[str, Any] | None = None, **overrides: Any) -> _StubOptions:
        return _StubOptions()

    async def fake_run_agent(prompt: str, options: _StubOptions, **kwargs: Any) -> tuple[str, float, str]:
        captured["disallowed_tools"] = list(options.disallowed_tools or [])
        return ("ok", 0.0, "session-1")

    def fake_validate(*, project_dir, pre_diff_files, expected_uu_files, pre_untracked_files, pre_head):
        return (True, None)  # pretend the agent did it right

    # Inject stubs via monkeypatch on the imports inside resolve_one_conflict
    import otto.agent as _agent
    import otto.merge.conflict_agent as _ca
    monkeypatch.setattr(_agent, "make_agent_options", fake_make_options)
    monkeypatch.setattr(_agent, "run_agent_with_timeout", fake_run_agent)
    monkeypatch.setattr(_ca, "validate_post_agent", fake_validate)

    ctx = ConflictContext(
        target="main",
        branch_being_merged="feature/x",
        branch_intents={"main": "target", "feature/x": "added X"},
        branch_stories=[],
        conflict_files=["f.txt"],
        conflict_diff="<<<<<<< HEAD\na\n=======\nb\n>>>>>>> feature/x\n",
    )
    import asyncio
    result: ConflictResolutionAttempt = asyncio.run(
        resolve_one_conflict(project_dir=tmp_path, config={}, ctx=ctx)
    )

    assert result.success is True
    assert "Bash" in captured["disallowed_tools"], (
        f"Bash must be disallowed (no shell escape); got {captured['disallowed_tools']!r}"
    )
    assert "Write" not in captured["disallowed_tools"], (
        f"Write must remain allowed (Edit-only is 2-3x slower). "
        f"Got: {captured['disallowed_tools']!r}"
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


def test_validate_post_agent_scans_committed_markers(
    tmp_path: Path,
):
    """When scan_files_for_markers=True, validator catches markers that
    `git diff --check` would miss (because they live in HEAD)."""
    # Set up a tiny git repo with a committed marker-laden file
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

    # Without the marker scan, validator passes (`git diff --check` is blind)
    ok, err = validate_post_agent(
        project_dir=tmp_path,
        pre_diff_files=set(),
        expected_uu_files={"f.py"},
        pre_untracked_files=set(),
        pre_head=head,
        scan_files_for_markers=False,
    )
    assert ok, f"baseline (no scan) should pass; got err={err}"

    # With the marker scan, validator catches the leftover markers
    ok, err = validate_post_agent(
        project_dir=tmp_path,
        pre_diff_files=set(),
        expected_uu_files={"f.py"},
        pre_untracked_files=set(),
        pre_head=head,
        scan_files_for_markers=True,
    )
    assert not ok
    assert err is not None and "f.py" in err and "markers" in err
