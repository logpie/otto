"""Tests for cleanup_stale_declared_ports — the pre-run zombie killer."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from otto.v5_clean_verify import cleanup_stale_declared_ports


def _write_charter(project_dir: Path, ports: list[int]) -> None:
    """Write a minimal CHARTER.md that declares the given ports."""
    lines = ["# CHARTER"]
    for port in ports:
        lines.append(f"- backend: 127.0.0.1:{port}")
    (project_dir / "CHARTER.md").write_text("\n".join(lines) + "\n")


def _free_port() -> int:
    """Get an unused TCP port the OS hands us."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_cleanup_no_charter_returns_empty(tmp_path: Path) -> None:
    """Without CHARTER.md, there are no declared ports — cleanup is a no-op."""
    assert cleanup_stale_declared_ports(tmp_path) == []


def test_cleanup_charter_no_ports_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "CHARTER.md").write_text("# CHARTER\nNothing about ports.\n")
    assert cleanup_stale_declared_ports(tmp_path) == []


def test_cleanup_ports_already_free_returns_empty(tmp_path: Path) -> None:
    """When declared ports are free, cleanup finds nothing to kill."""
    port = _free_port()
    _write_charter(tmp_path, [port])
    assert cleanup_stale_declared_ports(tmp_path) == []


def test_cleanup_kills_stale_listener(tmp_path: Path) -> None:
    """Spawn a Python TCP listener; cleanup should kill it."""
    # Reserve a port by binding briefly to find one, then release.
    port = _free_port()

    # Spawn a subprocess that binds to that port and sleeps.
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import socket, time; "
                "s = socket.socket(socket.AF_INET, socket.SOCK_STREAM); "
                "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
                f"s.bind(('127.0.0.1', {port})); "
                "s.listen(1); time.sleep(60)"
            ),
        ]
    )
    # Give it time to bind.
    deadline = time.time() + 5.0
    bound = False
    while time.time() < deadline:
        test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            test_sock.connect(("127.0.0.1", port))
            bound = True
            test_sock.close()
            break
        except OSError:
            test_sock.close()
            time.sleep(0.1)
    if not bound:
        proc.kill()
        pytest.skip("listener didn't bind in time")

    _write_charter(tmp_path, [port])

    killed = cleanup_stale_declared_ports(tmp_path)
    assert killed == [port], f"expected [{port}] killed, got {killed}"

    # Confirm process is gone.
    deadline = time.time() + 3.0
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    if proc.poll() is None:
        proc.kill()
        pytest.fail("listener process not killed by cleanup")

    # Port should be free again.
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
    finally:
        s.close()


def test_cleanup_returns_only_killed_ports(tmp_path: Path) -> None:
    """When some declared ports have zombies and others don't, return only the killed ones."""
    free_port = _free_port()
    _write_charter(tmp_path, [free_port])
    # No process; cleanup returns [].
    assert cleanup_stale_declared_ports(tmp_path) == []
