"""Tests for otto.merge.conflict_agent."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from unittest.mock import patch

from otto.merge import git_ops
from otto.merge.conflict_agent import (
    ConsolidatedConflictContext,
    _files_with_markers,
    resolve_all_conflicts,
    validate_post_agent,
)


def _init_repo(tmp_path: Path) -> str:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.py").write_text("def tracked():\n    return 1\n")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()


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


def test_validate_post_agent_cleans_agent_created_untracked_files(
    tmp_path: Path, caplog
):
    head = _init_repo(tmp_path)
    notes = tmp_path / "notes.txt"
    notes.write_text("notes\n")
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "report.log").write_text("report\n")

    caplog.set_level(logging.INFO, logger="otto.merge.conflict_agent")
    ok, err = validate_post_agent(
        project_dir=tmp_path,
        pre_diff_files=set(),
        expected_uu_files=set(),
        pre_untracked_files=set(),
        pre_head=head,
    )

    assert ok, f"cleanup should allow validation to pass; got err={err}"
    assert err is None
    assert not notes.exists()
    assert not output_dir.exists()
    messages = [record.getMessage() for record in caplog.records]
    assert any("notes.txt" in message for message in messages)
    assert any("output" in message for message in messages)


def test_validate_post_agent_preserves_preexisting_untracked_files(tmp_path: Path):
    head = _init_repo(tmp_path)
    existing = tmp_path / "existing.txt"
    existing.write_text("keep me\n")
    pre_untracked_files = set(git_ops.untracked_files(tmp_path))
    new_file = tmp_path / "new.txt"
    new_file.write_text("remove me\n")

    ok, err = validate_post_agent(
        project_dir=tmp_path,
        pre_diff_files=set(),
        expected_uu_files=set(),
        pre_untracked_files=pre_untracked_files,
        pre_head=head,
    )

    assert ok, f"only agent-created residue should be cleaned; got err={err}"
    assert err is None
    assert existing.exists()
    assert not new_file.exists()
    assert set(git_ops.untracked_files(tmp_path)) == {"existing.txt"}


def test_validate_post_agent_fails_when_untracked_cleanup_fails(tmp_path: Path, monkeypatch):
    head = _init_repo(tmp_path)
    victim = tmp_path / "new.txt"
    victim.write_text("remove me\n")
    real_unlink = Path.unlink

    def raising_unlink(self: Path, *args, **kwargs):
        if self == victim:
            raise PermissionError("blocked")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", raising_unlink)

    ok, err = validate_post_agent(
        project_dir=tmp_path,
        pre_diff_files=set(),
        expected_uu_files=set(),
        pre_untracked_files=set(),
        pre_head=head,
    )

    assert not ok
    assert err is not None
    assert "could not clean up agent-created files" in err
    assert "new.txt" in err
    assert victim.exists()


def test_validate_post_agent_passes_clean_tree(tmp_path: Path):
    """Clean tree, expected_uu_files empty, HEAD unchanged → passes."""
    head = _init_repo(tmp_path)

    ok, err = validate_post_agent(
        project_dir=tmp_path,
        pre_diff_files=set(),
        expected_uu_files=set(),
        pre_untracked_files=set(),
        pre_head=head,
    )
    assert ok, f"clean tree should pass; got err={err}"


def test_resolve_all_conflicts_uses_log_dir_for_agent_call(tmp_path: Path):
    head = _init_repo(tmp_path)
    ctx = ConsolidatedConflictContext(
        target="main",
        all_branches=["feat-a", "feat-b"],
        all_intents={"feat-a": "change f", "feat-b": "change f differently"},
        all_stories=[],
        conflict_files=["tracked.py"],
        conflict_diff="<<<<<<< ours\nA\n=======\nB\n>>>>>>> theirs\n",
        test_command=None,
    )
    captured: dict[str, object] = {}
    sentinel_options = object()

    async def fake_run_agent_with_timeout(prompt, options, **kwargs):
        captured["prompt"] = prompt
        captured["options"] = options
        captured["kwargs"] = kwargs
        return ("done", 1.25, "session-123", {})

    with patch("otto.agent.make_agent_options", return_value=sentinel_options), patch(
        "otto.agent.run_agent_with_timeout", side_effect=fake_run_agent_with_timeout
    ):
        attempt = asyncio.run(resolve_all_conflicts(
            project_dir=tmp_path,
            config={"provider": "claude"},
            ctx=ctx,
            pre_head=head,
            expected_uu_files=set(),
            pre_untracked_files=set(),
            pre_diff_files=set(),
            budget=None,
        ))

    assert attempt.success is True
    assert attempt.cost_usd == 1.25
    assert captured["options"] is sentinel_options
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["log_dir"] == tmp_path / "otto_logs" / "merge" / "conflict-agent-agentic"
    assert "log_path" not in kwargs
    assert kwargs["project_dir"] == tmp_path
    assert kwargs["timeout"] is None
