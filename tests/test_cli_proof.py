from __future__ import annotations

import os
import subprocess
from pathlib import Path

from click.testing import CliRunner

from otto import paths
from otto.cli import main
from otto.runs.history import append_history_snapshot


def _init_project(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@otto.local"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Otto Tester"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "commit.gpgsign", "false"], check=True)
    (path / "README.md").write_text("test project\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def _run(args: list[str], *, cwd: Path) -> tuple[int, str]:
    runner = CliRunner()
    saved_cwd = os.getcwd()
    os.chdir(cwd)
    try:
        result = runner.invoke(main, args, catch_exceptions=False)
    finally:
        os.chdir(saved_cwd)
    return result.exit_code, result.output


def test_proof_path_prefers_i2p_proof_packet(tmp_path: Path) -> None:
    _init_project(tmp_path)
    session_id = "2026-05-05-120000-abcdef"
    session_dir = paths.ensure_session_scaffold(tmp_path, session_id)
    proof_packet = session_dir / "proof-packet.html"
    proof_packet.write_text("<html>i2p</html>\n", encoding="utf-8")
    legacy_dir = paths.certify_dir(tmp_path, session_id)
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "proof-of-work.html").write_text("<html>legacy</html>\n", encoding="utf-8")

    code, out = _run(["proof", "path", session_id], cwd=tmp_path)

    assert code == 0, out
    assert str(proof_packet.resolve()) in out


def test_proof_path_falls_back_to_legacy_pow(tmp_path: Path) -> None:
    _init_project(tmp_path)
    session_id = "2026-05-05-120000-abcdef"
    paths.ensure_session_scaffold(tmp_path, session_id, phase="certify")
    legacy_dir = paths.certify_dir(tmp_path, session_id)
    legacy_pow = legacy_dir / "proof-of-work.html"
    legacy_pow.write_text("<html>legacy</html>\n", encoding="utf-8")

    code, out = _run(["proof", "path", session_id], cwd=tmp_path)

    assert code == 0, out
    assert str(legacy_pow.resolve()) in out


def test_pow_print_alias_uses_i2p_proof_packet(tmp_path: Path) -> None:
    _init_project(tmp_path)
    session_id = "2026-05-05-120000-abcdef"
    session_dir = paths.ensure_session_scaffold(tmp_path, session_id)
    proof_packet = session_dir / "proof-packet.html"
    proof_packet.write_text("<html>i2p</html>\n", encoding="utf-8")

    code, out = _run(["pow", session_id, "--print"], cwd=tmp_path)

    assert code == 0, out
    assert str(proof_packet.resolve()) in out


def test_proof_list_filters_run_history(tmp_path: Path) -> None:
    _init_project(tmp_path)
    append_history_snapshot(
        tmp_path,
        {
            "run_id": "run-a",
            "command": "run",
            "timestamp": "2026-05-05T12:00:00",
            "passed": True,
            "stories_tested": 1,
            "stories_passed": 1,
            "intent": "real run history row",
        },
    )

    code, out = _run(["proof", "list", "--command", "run"], cwd=tmp_path)

    assert code == 0, out
    assert "run" in out
    assert "real run history" in out


def test_debug_narrative_regenerates_session(tmp_path: Path, monkeypatch) -> None:
    _init_project(tmp_path)

    def fake_replay_session(project_dir: Path, session_id: str) -> list[Path]:
        assert project_dir == tmp_path
        assert session_id == "session-a"
        return [tmp_path / "narrative.regenerated.log"]

    monkeypatch.setattr("otto.replay.replay_session", fake_replay_session)

    code, out = _run(["debug", "narrative", "session-a"], cwd=tmp_path)

    assert code == 0, out
    assert "Regenerated 1 narrative file" in out


def test_top_level_help_keeps_aliases_discoverable_for_agents() -> None:
    result = CliRunner().invoke(main, ["--help"], catch_exceptions=False)

    assert result.exit_code == 0
    for visible_command in (
        "build",
        "cleanup",
        "dashboard",
        "debug",
        "history",
        "pow",
        "render",
        "replay",
        "proof",
        "run",
    ):
        assert f"  {visible_command} " in result.output
