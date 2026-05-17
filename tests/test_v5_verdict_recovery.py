"""Tests for verdict.json mislocation recovery.

Agents sometimes write verdict.json inside the worktree (e.g.
``worktree/frontend/verdict.json``) instead of the canonical
``<session_dir>/verdict.json``. The reader falls back to a bounded
worktree search when the canonical path is missing.
"""

from __future__ import annotations

import json
from pathlib import Path

from otto.lead import _find_misplaced_verdict, _read_agent_verdict


def _make_session(tmp_path: Path) -> tuple[Path, Path]:
    """Create a session_dir + worktree pair under tmp_path. Returns (session_dir, worktree)."""
    session = tmp_path / "session"
    session.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (session / "worktree").symlink_to(worktree)
    return session, worktree


def test_canonical_verdict_wins(tmp_path: Path) -> None:
    session, worktree = _make_session(tmp_path)
    (session / "verdict.json").write_text(
        json.dumps({"verdict": "pass", "summary": "canonical"})
    )
    # Misplaced one also exists — canonical should still win.
    (worktree / "verdict.json").write_text(
        json.dumps({"verdict": "partial", "summary": "misplaced"})
    )
    called, payload = _read_agent_verdict(session)
    assert called is True
    assert payload is not None
    assert payload["summary"] == "canonical"


def test_misplaced_worktree_root_recovered(tmp_path: Path) -> None:
    session, worktree = _make_session(tmp_path)
    (worktree / "verdict.json").write_text(
        json.dumps({"verdict": "pass", "summary": "found at worktree root"})
    )
    called, payload = _read_agent_verdict(session)
    assert called is True
    assert payload is not None
    assert payload["summary"] == "found at worktree root"
    # Canonical path now exists (copied from misplaced location).
    assert (session / "verdict.json").exists()


def test_misplaced_worktree_subsystem_recovered(tmp_path: Path) -> None:
    """The exact failure mode from the real-world FE run."""
    session, worktree = _make_session(tmp_path)
    (worktree / "frontend").mkdir()
    (worktree / "frontend" / "verdict.json").write_text(
        json.dumps({
            "verdict": "pass",
            "summary": "all 53 tests pass",
            "journeys": [{"id": "x", "passed": True}],
        })
    )
    called, payload = _read_agent_verdict(session)
    assert called is True
    assert payload is not None
    assert payload["summary"] == "all 53 tests pass"


def test_no_verdict_anywhere_returns_false(tmp_path: Path) -> None:
    session, _worktree = _make_session(tmp_path)
    called, payload = _read_agent_verdict(session)
    assert called is False
    assert payload is None


def test_find_misplaced_skips_noise_dirs(tmp_path: Path) -> None:
    session, worktree = _make_session(tmp_path)
    # node_modules contains verdict.json — must NOT be picked up.
    (worktree / "node_modules").mkdir()
    (worktree / "node_modules" / "verdict.json").write_text(
        json.dumps({"verdict": "pass", "summary": "noise"})
    )
    assert _find_misplaced_verdict(session) is None


def test_find_misplaced_returns_none_when_no_worktree(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    assert _find_misplaced_verdict(session) is None


def test_invalid_json_in_misplaced_falls_through(tmp_path: Path) -> None:
    """If misplaced verdict.json is corrupt JSON, recovery skips it gracefully."""
    session, worktree = _make_session(tmp_path)
    (worktree / "verdict.json").write_text("{ not valid json")
    called, payload = _read_agent_verdict(session)
    assert called is False
    assert payload is None
