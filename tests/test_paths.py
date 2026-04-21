"""Tests for otto/paths.py — the log-layout choke point.

Covers the invariants Codex flagged during Plan Gate review:
  - atomic pointer writes (no stale state on fallback)
  - scan fallback when `paused` pointer missing but session checkpoint exists
  - session_id collision retry
  - project lock refuses concurrent invocations
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from otto import paths


@pytest.fixture
def project_dir(tmp_path):
    """A fresh project directory."""
    return tmp_path


class TestSessionId:
    def test_new_session_id_format(self, project_dir):
        sid = paths.new_session_id(project_dir)
        # <yyyy-mm-dd>-<HHMMSS>-<6hex>
        assert len(sid) == len("YYYY-MM-DD-HHMMSS-abcdef")
        assert paths.is_session_id(sid)

    def test_collision_retries(self, project_dir):
        """If secrets.token_hex collides, new_session_id retries until unique."""
        # Pre-create one session dir with a known id.
        paths.sessions_root(project_dir).mkdir(parents=True, exist_ok=True)
        fixed_hex = "abcdef"
        # Simulate the first 2 calls returning the same hex that collides,
        # then a new one succeeds.
        call_count = {"n": 0}

        def rigged_token_hex(n):
            call_count["n"] += 1
            # First two calls return the colliding hex; subsequent calls
            # return a unique one.
            if call_count["n"] <= 2:
                return fixed_hex
            return "123456"

        # Seed a colliding session dir
        with patch("secrets.token_hex", side_effect=rigged_token_hex):
            # First call creates a session
            sid1 = paths.new_session_id(project_dir)
            (paths.sessions_root(project_dir) / sid1).mkdir()
            # Second call should retry because sid1 dir exists, and succeed
            # with a different hex.
            sid2 = paths.new_session_id(project_dir)

        assert sid1 != sid2
        assert sid1.endswith(fixed_hex)
        assert sid2.endswith("123456")


class TestPointerAtomicity:
    def test_set_pointer_creates_symlink(self, project_dir):
        sid = "2026-04-20-170200-abcdef"
        paths.ensure_session_scaffold(project_dir, sid)
        paths.set_pointer(project_dir, "latest", sid)
        resolved = paths.resolve_pointer(project_dir, "latest")
        assert resolved is not None
        assert resolved.name == sid

    def test_set_pointer_falls_back_to_txt_on_symlink_failure(self, project_dir):
        sid = "2026-04-20-170200-abcdef"
        paths.ensure_session_scaffold(project_dir, sid)

        # Make os.symlink raise (simulate Windows without admin).
        def failing_symlink(*_args, **_kwargs):
            raise OSError("EPERM")

        with patch("os.symlink", side_effect=failing_symlink):
            paths.set_pointer(project_dir, "latest", sid)

        txt = paths.logs_dir(project_dir) / "latest.txt"
        assert txt.exists(), ".txt fallback should be written when symlink fails"
        assert txt.read_text().strip() == sid

    def test_fallback_clears_stale_symlink(self, project_dir):
        """When symlink fails mid-update, any stale symlink at the target
        path must be removed so resolve_pointer doesn't return pre-failure state."""
        sid1 = "2026-04-20-170200-abcdef"
        sid2 = "2026-04-20-170201-111111"
        paths.ensure_session_scaffold(project_dir, sid1)
        paths.ensure_session_scaffold(project_dir, sid2)

        # First write succeeds → symlink at "latest" → sid1.
        paths.set_pointer(project_dir, "latest", sid1)
        assert paths.resolve_pointer(project_dir, "latest").name == sid1

        # Second write: os.symlink fails. Must clear stale symlink and write .txt.
        def failing_symlink(*_args, **_kwargs):
            raise OSError("EPERM")

        with patch("os.symlink", side_effect=failing_symlink):
            paths.set_pointer(project_dir, "latest", sid2)

        # resolve_pointer must NOT return the old sid1 target.
        resolved = paths.resolve_pointer(project_dir, "latest")
        assert resolved is not None
        assert resolved.name == sid2


class TestResolvePointerScanFallback:
    def test_scan_finds_paused_session_without_pointer(self, project_dir):
        """If `paused` pointer is missing (e.g., crash before set_pointer),
        resolve_pointer('paused') scans sessions/ for status=paused."""
        sid = "2026-04-20-170200-abcdef"
        paths.ensure_session_scaffold(project_dir, sid)
        cp = paths.session_checkpoint(project_dir, sid)
        cp.write_text(json.dumps({
            "status": "paused",
            "run_id": sid,
            "updated_at": "2026-04-20T17:02:00Z",
        }))
        # No pointer file / symlink — only the session checkpoint exists.
        resolved = paths.resolve_pointer(project_dir, "paused")
        assert resolved is not None
        assert resolved.name == sid

    def test_scan_also_finds_in_progress(self, project_dir):
        """Hard-crashed sessions have status=in_progress (not paused).
        resolve_pointer must still find them so --resume works after SIGKILL."""
        sid = "2026-04-20-170300-ddeeff"
        paths.ensure_session_scaffold(project_dir, sid)
        paths.session_checkpoint(project_dir, sid).write_text(json.dumps({
            "status": "in_progress",
            "run_id": sid,
        }))
        resolved = paths.resolve_pointer(project_dir, "paused")
        assert resolved is not None

    def test_scan_prefers_paused_over_in_progress(self, project_dir):
        """When both exist, paused (clean pause) wins over in_progress (crash)."""
        older_in_prog = "2026-04-20-170000-111111"
        newer_paused = "2026-04-20-170200-222222"
        paths.ensure_session_scaffold(project_dir, older_in_prog)
        paths.ensure_session_scaffold(project_dir, newer_paused)
        paths.session_checkpoint(project_dir, older_in_prog).write_text(json.dumps({
            "status": "in_progress",
            "run_id": older_in_prog,
            "updated_at": "2026-04-20T17:01:00Z",
        }))
        paths.session_checkpoint(project_dir, newer_paused).write_text(json.dumps({
            "status": "paused",
            "run_id": newer_paused,
            "updated_at": "2026-04-20T17:02:00Z",
        }))
        resolved = paths.resolve_pointer(project_dir, "paused")
        assert resolved is not None
        assert resolved.name == newer_paused, "paused must win over in_progress"


class TestProjectLock:
    def test_lock_acquired_and_released(self, project_dir):
        with paths.project_lock(project_dir, "build"):
            assert (paths.logs_dir(project_dir) / paths.LOCK_FILE_NAME).exists()
        # After the context manager exits, the lock file is removed.
        assert not (paths.logs_dir(project_dir) / paths.LOCK_FILE_NAME).exists()

    def test_lock_refuses_second_live_holder(self, project_dir):
        """Second acquire should raise LockBusy when the first holder is alive."""
        first = paths.acquire_project_lock(project_dir, "build")
        try:
            # Spoof the "live" check so the second call sees the first PID
            # as alive regardless of OS behavior with the current test PID.
            with patch("otto.paths._pid_alive", return_value=True):
                with pytest.raises(paths.LockBusy):
                    paths.acquire_project_lock(project_dir, "build")
        finally:
            first.release()

    def test_stale_lock_auto_released(self, project_dir):
        """If the holder PID is gone, the lock is auto-released on next acquire."""
        # Write a stale lock manually.
        paths.logs_dir(project_dir).mkdir(parents=True, exist_ok=True)
        (paths.logs_dir(project_dir) / paths.LOCK_FILE_NAME).write_text(json.dumps({
            "pid": 999999,  # unlikely to be alive
            "started_at": "2026-01-01T00:00:00",
            "command": "build",
            "session_id": None,
        }))
        # Force _pid_alive to return False for the stale pid.
        with patch("otto.paths._pid_alive", return_value=False):
            handle = paths.acquire_project_lock(project_dir, "build")
            handle.release()

    def test_set_session_id_updates_record(self, project_dir):
        """LockHandle.set_session_id() updates the record in place."""
        with paths.project_lock(project_dir, "build") as handle:
            handle.set_session_id("2026-04-20-170200-abcdef")
            record = json.loads((paths.logs_dir(project_dir) / paths.LOCK_FILE_NAME).read_text())
            assert record["session_id"] == "2026-04-20-170200-abcdef"
