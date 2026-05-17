"""Tests for cleanup_stale_declared_ports — the pre-run zombie killer."""

from __future__ import annotations

from pathlib import Path

from otto import v5_clean_verify


def _write_charter(project_dir: Path, ports: list[int]) -> None:
    """Write a minimal CHARTER.md that declares the given ports."""
    lines = ["# CHARTER"]
    for port in ports:
        lines.append(f"- backend: 127.0.0.1:{port}")
    (project_dir / "CHARTER.md").write_text("\n".join(lines) + "\n")


def test_cleanup_no_charter_returns_empty(tmp_path: Path) -> None:
    """Without CHARTER.md, there are no declared ports — cleanup is a no-op."""
    assert v5_clean_verify.cleanup_stale_declared_ports(tmp_path) == []


def test_cleanup_charter_no_ports_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "CHARTER.md").write_text("# CHARTER\nNothing about ports.\n")
    assert v5_clean_verify.cleanup_stale_declared_ports(tmp_path) == []


def test_cleanup_ports_already_free_returns_empty(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """When declared ports are free, cleanup finds nothing to kill."""
    port = 19001
    _write_charter(tmp_path, [port])
    monkeypatch.setattr(v5_clean_verify, "_pids_for_port", lambda _port: [])
    assert v5_clean_verify.cleanup_stale_declared_ports(tmp_path) == []


def test_cleanup_kills_stale_field_test_listener(tmp_path: Path, monkeypatch) -> None:
    """Cleanup kills only Otto-owned PIDs returned for declared ports."""
    port = 19001
    _write_charter(tmp_path, [port])
    killed: list[int] = []
    monkeypatch.setattr(v5_clean_verify, "_pids_for_port", lambda _port: [101, 202])
    monkeypatch.setattr(
        v5_clean_verify,
        "_is_otto_owned_process",
        lambda pid, _project_dir: pid == 101,
    )
    monkeypatch.setattr(v5_clean_verify, "_terminate_pid", lambda pid: killed.append(pid))

    assert v5_clean_verify.cleanup_stale_declared_ports(tmp_path) == [port]
    assert killed == [101]


def test_cleanup_returns_only_killed_ports(tmp_path: Path, monkeypatch) -> None:
    """When some declared ports have zombies and others don't, return only the killed ones."""
    _write_charter(tmp_path, [19001, 19002])
    killed: list[int] = []
    monkeypatch.setattr(
        v5_clean_verify,
        "_pids_for_port",
        lambda port: {19001: [], 19002: [303]}.get(port, []),
    )
    monkeypatch.setattr(v5_clean_verify, "_is_otto_owned_process", lambda *_args: True)
    monkeypatch.setattr(v5_clean_verify, "_terminate_pid", lambda pid: killed.append(pid))

    assert v5_clean_verify.cleanup_stale_declared_ports(tmp_path) == [19002]
    assert killed == [303]


def test_cleanup_leaves_unowned_listener(tmp_path: Path, monkeypatch) -> None:
    """Declared-port cleanup must not kill arbitrary user processes."""
    _write_charter(tmp_path, [19001])
    killed: list[int] = []
    monkeypatch.setattr(v5_clean_verify, "_pids_for_port", lambda _port: [404])
    monkeypatch.setattr(v5_clean_verify, "_is_otto_owned_process", lambda *_args: False)
    monkeypatch.setattr(v5_clean_verify, "_terminate_pid", lambda pid: killed.append(pid))

    assert v5_clean_verify.cleanup_stale_declared_ports(tmp_path) == []
    assert killed == []
