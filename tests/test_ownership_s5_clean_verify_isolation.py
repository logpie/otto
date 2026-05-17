# pyright: reportPrivateUsage=false
from __future__ import annotations

from pathlib import Path
from typing import Any

from otto import cli, v5_clean_verify


class _CleanPass:
    passed = True
    issues: list[Any] = []

    def to_jsonable(self) -> dict[str, Any]:
        return {"passed": True, "issues": []}


def _invoke_clean_verify() -> None:
    callback = getattr(cli.clean_verify_command, "callback", cli.clean_verify_command)
    callback(
        json_output=True,
        verify_scope="subtree",
        repair_packet=None,
        spec_path=None,
        journey_scope=None,
        journey_artifact_dir=None,
    )


def test_clean_verify_uses_repair_worktree_env_only_with_repair_packet_context(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cwd = tmp_path / "ambient-cwd"
    repair_worktree = tmp_path / "repair-worktree"
    cwd.mkdir()
    repair_worktree.mkdir()
    captured: dict[str, Path] = {}

    def fake_verify(project_dir: Path, **_kwargs: Any) -> _CleanPass:
        captured["project_dir"] = project_dir
        return _CleanPass()

    monkeypatch.chdir(cwd)
    monkeypatch.setenv("OTTO_CLEAN_VERIFY_WORKTREE", str(repair_worktree))
    monkeypatch.setenv("OTTO_REPAIR_PACKET_PATH", str(tmp_path / "repair_packet.json"))
    monkeypatch.setattr(v5_clean_verify, "verify_from_clean_oracle", fake_verify)
    monkeypatch.setattr(
        "otto.v5_preflight_repair.append_repair_packet_oracle_event",
        lambda *_args, **_kwargs: None,
    )

    _invoke_clean_verify()

    assert captured["project_dir"] == repair_worktree


def test_clean_verify_ignores_stale_worktree_env_for_manual_invocation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cwd = tmp_path / "manual-cwd"
    stale_worktree = tmp_path / "stale-worktree"
    cwd.mkdir()
    stale_worktree.mkdir()
    captured: dict[str, Path] = {}

    def fake_verify(project_dir: Path, **_kwargs: Any) -> _CleanPass:
        captured["project_dir"] = project_dir
        return _CleanPass()

    monkeypatch.chdir(cwd)
    monkeypatch.setenv("OTTO_CLEAN_VERIFY_WORKTREE", str(stale_worktree))
    monkeypatch.delenv("OTTO_REPAIR_PACKET_PATH", raising=False)
    monkeypatch.setattr(v5_clean_verify, "verify_from_clean_oracle", fake_verify)

    _invoke_clean_verify()

    assert captured["project_dir"] == cwd

