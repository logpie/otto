"""Tests for otto/paths.py — the log-layout choke point.

Covers the invariants Codex flagged during Plan Gate review:
  - atomic pointer writes (no stale state on fallback)
  - scan fallback when `paused` pointer missing but session checkpoint exists
  - session_id collision retry
  - project lock refuses concurrent invocations
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from otto import paths


def _crash_lock_holder(project_dir_str: str, ready_conn, exit_conn) -> None:
    """Acquire the project lock in a child process, then exit without release."""
    handle = paths.acquire_project_lock(Path(project_dir_str), "child")
    ready_conn.send(handle._nonce)
    exit_conn.recv()
    os._exit(0)


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

    def test_stale_paused_pointer_falls_through_to_valid_session(self, project_dir):
        stale_sid = "2026-04-20-170100-aaaaaa"
        valid_sid = "2026-04-20-170200-bbbbbb"
        paths.ensure_session_scaffold(project_dir, stale_sid)
        paths.ensure_session_scaffold(project_dir, valid_sid)
        paths.set_pointer(project_dir, paths.PAUSED_POINTER, stale_sid)
        paths.session_checkpoint(project_dir, valid_sid).write_text(json.dumps({
            "status": "paused",
            "run_id": valid_sid,
            "updated_at": "2026-04-20T17:02:00Z",
        }))

        resolved = paths.resolve_pointer(project_dir, paths.PAUSED_POINTER)

        assert resolved is not None
        assert resolved.name == valid_sid


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
            with pytest.raises(paths.LockBusy):
                paths.acquire_project_lock(project_dir, "build", break_stale=False)
        finally:
            first.release()

    def test_stale_lock_auto_released(self, project_dir):
        """A leftover lock file without a live flock holder is replaced."""
        paths.logs_dir(project_dir).mkdir(parents=True, exist_ok=True)
        (paths.logs_dir(project_dir) / paths.LOCK_FILE_NAME).write_text(json.dumps({
            "pid": 999999,  # unlikely to be alive
            "started_at": "2026-01-01T00:00:00",
            "command": "build",
            "nonce": "stale-nonce",
            "session_id": None,
        }))
        handle = paths.acquire_project_lock(project_dir, "build")
        try:
            record = json.loads((paths.logs_dir(project_dir) / paths.LOCK_FILE_NAME).read_text())
            assert record["command"] == "build"
            assert record["nonce"] == handle._nonce
            assert record["nonce"] != "stale-nonce"
        finally:
            handle.release()

    def test_set_session_id_does_not_mutate_lock_record(self, project_dir, caplog):
        """LockHandle.set_session_id() is metadata-only and leaves .lock immutable."""
        caplog.set_level(logging.INFO, logger="otto.paths")
        with paths.project_lock(project_dir, "build") as handle:
            before = json.loads((paths.logs_dir(project_dir) / paths.LOCK_FILE_NAME).read_text())
            handle.set_session_id("2026-04-20-170200-abcdef")
            after = json.loads((paths.logs_dir(project_dir) / paths.LOCK_FILE_NAME).read_text())
            assert after == before
            assert after["session_id"] is None
            assert "lock now bound to session 2026-04-20-170200-abcdef" in caplog.text

    def test_break_lock_forcibly_clears_existing_holder(self, project_dir):
        """Manual break-lock should remove even a live holder before reacquiring."""
        first = paths.acquire_project_lock(project_dir, "build")
        try:
            with paths.project_lock(project_dir, "certify", break_lock=True):
                record = json.loads((paths.logs_dir(project_dir) / paths.LOCK_FILE_NAME).read_text())
                assert record["command"] == "certify"
        finally:
            first.release()

    def test_release_does_not_unlink_replaced_lock(self, project_dir, caplog):
        """Old holders must not remove a lock recreated by --break-lock."""
        first = paths.acquire_project_lock(project_dir, "build")
        try:
            prior = paths.break_project_lock(project_dir)
            assert prior["nonce"] == first._nonce

            lock_path = paths.logs_dir(project_dir) / paths.LOCK_FILE_NAME
            replacement = {
                "pid": 424242,
                "started_at": "2026-04-20T17:02:01+00:00",
                "command": "certify",
                "nonce": "replacement-nonce",
                "session_id": None,
            }
            lock_path.write_text(json.dumps(replacement))

            first.release()

            assert lock_path.exists(), "replaced lock must remain after stale holder release()"
            record = json.loads(lock_path.read_text())
            assert record == replacement
            assert "lock record no longer ours (broken?)" in caplog.text
        finally:
            first.release()

    def test_best_effort_lock_closes_fd_and_release_uses_nonce(self, project_dir, monkeypatch, caplog):
        """Best-effort locks should not keep an fd open after creation."""
        monkeypatch.setattr(paths, "fcntl", None)

        first = paths.acquire_project_lock(project_dir, "build")
        try:
            assert first._fd is None

            prior = paths.break_project_lock(project_dir)
            assert prior["nonce"] == first._nonce

            lock_path = paths.logs_dir(project_dir) / paths.LOCK_FILE_NAME
            replacement = {
                "pid": 424242,
                "started_at": "2026-04-20T17:02:01+00:00",
                "command": "certify",
                "nonce": "replacement-nonce",
                "session_id": None,
            }
            lock_path.write_text(json.dumps(replacement))

            first.release()

            assert lock_path.exists(), "replaced lock must remain after stale holder release()"
            record = json.loads(lock_path.read_text())
            assert record == replacement
            assert "lock record no longer ours (broken?)" in caplog.text
        finally:
            first.release()

    @pytest.mark.skipif(sys.platform == "win32", reason="fcntl not available on Windows")
    def test_flock_survives_process_exit_and_stale_file_is_replaced(self, project_dir):
        """Kernel flock blocks concurrent holders and is released when the holder dies."""
        ctx = multiprocessing.get_context("fork")
        parent_ready, child_ready = ctx.Pipe(duplex=False)
        child_exit, parent_exit = ctx.Pipe(duplex=False)
        proc = ctx.Process(
            target=_crash_lock_holder,
            args=(str(project_dir), child_ready, child_exit),
        )
        proc.start()
        child_nonce = parent_ready.recv()

        with pytest.raises(paths.LockBusy):
            paths.acquire_project_lock(project_dir, "parent", break_stale=False)

        parent_exit.send(True)
        proc.join(timeout=2)
        assert proc.exitcode == 0

        lock_path = paths.logs_dir(project_dir) / paths.LOCK_FILE_NAME
        assert lock_path.exists(), "crashed holder should leave behind a stale lock file"

        replacement = paths.acquire_project_lock(project_dir, "parent")
        try:
            record = json.loads(lock_path.read_text())
            assert record["command"] == "parent"
            assert record["nonce"] == replacement._nonce
            assert record["nonce"] != child_nonce
        finally:
            replacement.release()
