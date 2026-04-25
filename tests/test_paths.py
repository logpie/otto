"""Tests for otto/paths.py — the log-layout choke point.

Covers the invariants Codex flagged during Plan Gate review:
  - atomic pointer writes (no stale state on fallback)
  - `paused` pointer is authoritative for resume discovery
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


class TestSessionScaffold:
    def test_scaffold_creates_session_root_only_by_default(self, project_dir):
        sid = "2026-04-20-170200-abcdef"
        paths.ensure_session_scaffold(project_dir, sid)

        sess = paths.session_dir(project_dir, sid)
        assert sess.exists()
        assert not (sess / "build").exists()
        assert not (sess / "certify").exists()
        assert not (sess / "improve").exists()

    def test_scaffold_creates_only_requested_phase(self, project_dir):
        sid = "2026-04-20-170200-abcdef"
        paths.ensure_session_scaffold(project_dir, sid, phase="certify")

        sess = paths.session_dir(project_dir, sid)
        assert sess.exists()
        assert (sess / "certify").exists()
        assert not (sess / "build").exists()
        assert not (sess / "improve").exists()


class TestQueuePaths:
    def test_queue_manifest_path_uses_queue_namespace(self, project_dir):
        assert paths.queue_manifest_path(project_dir, "add-csv-export") == (
            paths.queue_dir(project_dir) / "add-csv-export" / "manifest.json"
        )

    @pytest.mark.parametrize("bad_value", ["../escape", "foo/bar", "..", "UPPER", "foo..bar"])
    def test_queue_manifest_path_rejects_invalid_task_ids(self, project_dir, bad_value):
        with pytest.raises(ValueError, match="Invalid queue task id"):
            paths.queue_manifest_path(project_dir, bad_value)


class TestResolvePointerPausedPointer:
    def test_missing_paused_pointer_does_not_scan_sessions(self, project_dir):
        sid = "2026-04-20-170200-abcdef"
        paths.ensure_session_scaffold(project_dir, sid)
        cp = paths.session_checkpoint(project_dir, sid)
        cp.write_text(json.dumps({
            "status": "paused",
            "run_id": sid,
            "updated_at": "2026-04-20T17:02:00Z",
        }))
        assert paths.resolve_pointer(project_dir, "paused") is None

    def test_missing_paused_pointer_does_not_scan_in_progress_sessions(self, project_dir):
        sid = "2026-04-20-170300-ddeeff"
        paths.ensure_session_scaffold(project_dir, sid)
        paths.session_checkpoint(project_dir, sid).write_text(json.dumps({
            "status": "in_progress",
            "run_id": sid,
        }))
        assert paths.resolve_pointer(project_dir, "paused") is None

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
        """A leftover lock file without a live flock holder is reused in place."""
        paths.logs_dir(project_dir).mkdir(parents=True, exist_ok=True)
        lock_path = paths.logs_dir(project_dir) / paths.LOCK_FILE_NAME
        lock_path.write_text(json.dumps({
            "pid": 999999,  # unlikely to be alive
            "started_at": "2026-01-01T00:00:00",
            "command": "build",
            "nonce": "stale-nonce",
            "session_id": None,
        }))
        before = lock_path.stat()
        handle = paths.acquire_project_lock(project_dir, "build")
        try:
            after = lock_path.stat()
            record = json.loads(lock_path.read_text())
            assert record["command"] == "build"
            assert record["nonce"] == handle._nonce
            assert record["nonce"] != "stale-nonce"
            assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
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

    def test_break_lock_refuses_live_holder(self, project_dir):
        """Manual break-lock must never unlink a live Unix flock holder."""
        first = paths.acquire_project_lock(project_dir, "build")
        try:
            with pytest.raises(paths.LockBreakError):
                with paths.project_lock(project_dir, "certify", break_lock=True):
                    pass
        finally:
            first.release()

    def test_break_lock_clears_stale_holder(self, project_dir):
        """Manual break-lock may clear a stale lock record after confirming pid is dead."""
        lock_path = paths.logs_dir(project_dir) / paths.LOCK_FILE_NAME
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        stale = {
            "pid": 999999,
            "started_at": "2026-04-20T17:02:01+00:00",
            "command": "build",
            "nonce": "stale-nonce",
            "session_id": None,
        }
        lock_path.write_text(json.dumps(stale))

        prior = paths.break_project_lock(project_dir)
        assert prior == stale
        assert lock_path.exists()
        assert json.loads(lock_path.read_text() or "{}") == {}

    def test_release_does_not_unlink_replaced_lock(self, project_dir, caplog):
        """Old holders must not remove a lock record recreated on a new inode."""
        first = paths.acquire_project_lock(project_dir, "build")
        try:
            lock_path = paths.logs_dir(project_dir) / paths.LOCK_FILE_NAME
            lock_path.unlink()
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

            lock_path = paths.logs_dir(project_dir) / paths.LOCK_FILE_NAME
            lock_path.unlink()
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
    def test_flock_survives_process_exit_and_stale_file_is_reused(self, project_dir):
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
        before = lock_path.stat()

        replacement = paths.acquire_project_lock(project_dir, "parent")
        try:
            after = lock_path.stat()
            record = json.loads(lock_path.read_text())
            assert record["command"] == "parent"
            assert record["nonce"] == replacement._nonce
            assert record["nonce"] != child_nonce
            assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)
        finally:
            replacement.release()
