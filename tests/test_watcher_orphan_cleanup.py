"""Regression tests for W11-IMPORTANT-4 — orphan watcher cleanup.

Background
----------
The Mission Control web backend launches the watcher as a subprocess
(``otto queue run --no-dashboard --concurrent N``) via
``subprocess.Popen(..., start_new_session=True)`` so it survives a parent
exit. Before the fix, ``MCBackend.stop()`` only joined the uvicorn thread
and never signalled the watcher, leaving an orphan ``otto queue run``
process and any in-flight grandchild (the actual ``otto build``) running
against a now-deleted tempdir.

These tests pin the production fix:

1. ``terminate_watcher_blocking`` (helper in ``otto.mission_control.service``)
   reads supervisor metadata, signals the watcher's process group, and
   escalates ``SIGTERM`` → ``SIGKILL`` after a short grace.
2. The FastAPI lifespan in ``otto/web/app.py`` invokes the helper for
   every project ever bound to the app on shutdown.
3. The browser test harness in ``tests/browser/_helpers/server.py``
   invokes the helper a second time as belt-and-braces.

We exercise (1) directly with a real ``time.sleep`` subprocess and a
hand-written supervisor metadata file. We exercise (2) by spinning up a
``start_backend(...)`` against a real project dir, recording a fake
watcher PID, then calling ``backend.stop()``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from otto.mission_control.service import terminate_watcher_blocking
from otto.mission_control.supervisor import (
    record_watcher_launch,
    read_supervisor,
    supervisor_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git_init(project_dir: Path) -> None:
    """Initialise a minimal git project so MissionControlService accepts it."""

    project_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project_dir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "watcher-orphan@example.com"],
        cwd=project_dir,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Watcher Orphan"],
        cwd=project_dir,
        check=True,
    )
    (project_dir / "README.md").write_text("# orphan-test\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=project_dir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=project_dir, check=True)


def _spawn_sleep(duration: int = 60) -> subprocess.Popen[bytes]:
    """Spawn a process-group leader that sleeps in its own session."""

    return subprocess.Popen(
        [sys.executable, "-c", f"import time; time.sleep({duration})"],
        start_new_session=True,
    )


def _spawn_sleep_with_child(parent_duration: int = 60, child_duration: int = 60) -> subprocess.Popen[bytes]:
    """Spawn a leader that itself spawns a child, both sleeping.

    The child is started via ``subprocess.Popen`` from the parent so it
    inherits the parent's process group. Returns the parent ``Popen``;
    the child PID is discoverable via ``psutil.Process(parent.pid).children()``.
    """

    code = (
        "import subprocess, sys, time;"
        f"subprocess.Popen([sys.executable, '-c', 'import time; time.sleep({child_duration})']);"
        f"time.sleep({parent_duration})"
    )
    return subprocess.Popen([sys.executable, "-c", code], start_new_session=True)


def _record_watcher(project_dir: Path, pid: int) -> None:
    """Write supervisor metadata for ``pid`` so the helper picks it up."""

    record_watcher_launch(
        project_dir,
        watcher_pid=pid,
        argv=["otto", "queue", "run", "--no-dashboard", "--concurrent", "2"],
        log_path=project_dir / "watcher.log",
        concurrent=2,
        exit_when_empty=False,
    )


def _is_dead_or_zombie(pid: int) -> bool:
    """Return True if ``pid`` is no longer a live process.

    A process can linger as a zombie after termination until its parent
    reaps it (via ``waitpid``). For our purposes — "is the watcher still
    consuming resources" — a zombie is dead. We use ``psutil`` so we can
    detect ``STATUS_ZOMBIE`` rather than relying solely on
    ``os.kill(pid, 0)`` which returns success for zombies.
    """

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return True
    try:
        status = proc.status()
    except psutil.NoSuchProcess:
        return True
    return status == psutil.STATUS_ZOMBIE


def _wait_dead(pid: int, *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _is_dead_or_zombie(pid):
            return True
        time.sleep(0.05)
    return _is_dead_or_zombie(pid)


# ---------------------------------------------------------------------------
# Helper-level tests
# ---------------------------------------------------------------------------


def test_terminate_watcher_blocking_kills_running_watcher(tmp_path: Path) -> None:
    project_dir = tmp_path / "project"
    _git_init(project_dir)
    proc = _spawn_sleep(duration=120)
    try:
        _record_watcher(project_dir, proc.pid)
        result = terminate_watcher_blocking(project_dir, grace=3.0)
        assert result["terminated"] is True
        assert result["pid"] == proc.pid
        assert result["pgid"] is not None
        assert _wait_dead(proc.pid), f"watcher pid={proc.pid} survived terminate"
        # Stop record was written.
        meta, _err = read_supervisor(project_dir)
        assert meta is not None
        assert meta.get("stop_requested_at") is not None
        assert meta.get("stop_target_pid") == proc.pid
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_terminate_watcher_blocking_kills_grandchildren(tmp_path: Path) -> None:
    """SIGTERM on the process group must take down the watcher's children too.

    The orphan-watcher bug had an in-flight ``otto build`` grandchild
    surviving alongside the watcher. We model that with a parent that
    ``Popen``s a sleep — the fix's ``killpg`` should reap both.
    """

    project_dir = tmp_path / "project"
    _git_init(project_dir)
    proc = _spawn_sleep_with_child(parent_duration=120, child_duration=120)
    # Give the child a moment to spawn before we capture its pid.
    time.sleep(0.5)
    parent = psutil.Process(proc.pid)
    children = parent.children(recursive=True)
    assert children, "child subprocess never spawned"
    child_pids = [c.pid for c in children]
    try:
        _record_watcher(project_dir, proc.pid)
        result = terminate_watcher_blocking(project_dir, grace=3.0)
        assert result["terminated"] is True
        assert result["pgid"] is not None
        # Both leader and at least the immediate child must be dead.
        assert _wait_dead(proc.pid), f"watcher pid={proc.pid} survived terminate"
        for cpid in child_pids:
            assert _wait_dead(cpid), f"grandchild pid={cpid} survived killpg"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
        for cpid in child_pids:
            try:
                os.kill(cpid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_terminate_watcher_blocking_escalates_to_sigkill(tmp_path: Path) -> None:
    """A watcher that ignores SIGTERM should be SIGKILLed after grace."""

    project_dir = tmp_path / "project"
    _git_init(project_dir)
    code = (
        "import signal, time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "time.sleep(120)"
    )
    proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        # Wait for the SIGTERM handler to be installed before asking the
        # helper to send the signal — otherwise the SIGTERM races with
        # `signal.signal()` and may terminate immediately, masking the
        # escalation path we want to exercise.
        time.sleep(0.3)
        _record_watcher(project_dir, proc.pid)
        result = terminate_watcher_blocking(project_dir, grace=0.5)
        assert result["terminated"] is True
        assert result["escalated"] is True, "SIGKILL escalation should have fired"
        assert _wait_dead(proc.pid), f"watcher pid={proc.pid} survived even SIGKILL"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


def test_terminate_watcher_blocking_handles_dead_pid(tmp_path: Path) -> None:
    """Pre-dead PID is a no-op (no signal sent) but stop record is written."""

    project_dir = tmp_path / "project"
    _git_init(project_dir)
    proc = _spawn_sleep(duration=1)
    pid = proc.pid
    proc.wait(timeout=5)
    assert _wait_dead(pid)
    _record_watcher(project_dir, pid)
    result = terminate_watcher_blocking(project_dir, grace=1.0)
    assert result["terminated"] is False
    assert result["pid"] == pid
    meta, _err = read_supervisor(project_dir)
    assert meta is not None
    assert meta.get("stop_requested_at") is not None


def test_terminate_watcher_blocking_no_metadata_is_noop(tmp_path: Path) -> None:
    """No supervisor file → helper returns clean no-op without raising."""

    project_dir = tmp_path / "project"
    _git_init(project_dir)
    assert not supervisor_path(project_dir).exists()
    result = terminate_watcher_blocking(project_dir, grace=1.0)
    assert result == {
        "terminated": False,
        "pid": None,
        "pgid": None,
        "escalated": False,
        "error": None,
    }


def test_watcher_pid_registry_pruned_on_external_kill(tmp_path: Path) -> None:
    """If the watcher is killed externally, the next helper call notices.

    The supervisor metadata still has the PID, but the helper detects it
    is no longer alive, writes a stop record, and reports terminated=False
    so callers don't double-signal.
    """

    project_dir = tmp_path / "project"
    _git_init(project_dir)
    proc = _spawn_sleep(duration=120)
    try:
        _record_watcher(project_dir, proc.pid)
        # External kill — simulating a user `kill -9`.
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        assert _wait_dead(proc.pid)
        result = terminate_watcher_blocking(project_dir, grace=1.0)
        assert result["terminated"] is False
        assert result["pid"] == proc.pid
        meta, _err = read_supervisor(project_dir)
        assert meta is not None
        # Stop record reflects the post-mortem cleanup.
        assert meta.get("stop_requested_at") is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


# ---------------------------------------------------------------------------
# Backend-shutdown integration test
# ---------------------------------------------------------------------------


def test_backend_stop_kills_running_watcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: ``MCBackend.stop()`` must reap the watcher subprocess.

    We can't (and shouldn't) really invoke ``otto queue run`` here — the
    fixture would spawn an actual queue runner. Instead we record fake
    supervisor metadata pointing at a real ``time.sleep`` PID; the
    backend's lifespan + harness ``stop()`` should both notice and kill it.
    """

    monkeypatch.setenv("OTTO_WEB_SKIP_FRESHNESS", "1")

    # Imported lazily so the env var is in effect.
    from tests.browser._helpers.server import start_backend

    project_dir = tmp_path / "project"
    _git_init(project_dir)
    projects_root = tmp_path / "managed"
    projects_root.mkdir()

    backend = start_backend(project_dir, projects_root=projects_root)
    proc = _spawn_sleep(duration=120)
    try:
        _record_watcher(project_dir, proc.pid)
        backend.stop()
        assert _wait_dead(proc.pid), f"watcher pid={proc.pid} survived backend.stop()"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
