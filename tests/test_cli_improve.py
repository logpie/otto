"""Phase C deletion smoke tests for the improve CLI surface.

The legacy ``_run_improve`` / ``_run_improve_locked`` outer loops were
deleted in Phase C (tick 63). The click subcommands (`bugs`, `feature`,
`target`) stay as the user-facing entry points but now dispatch
unconditionally to the i2p stack, with ``--legacy`` reduced to a hard
error pointing operators at ``--i2p``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from otto.cli import main
from tests._helpers import init_repo


def test_improve_legacy_flag_exits_with_phase_c_message(tmp_path: Path) -> None:
    """Passing ``--legacy`` to any improve subcommand surfaces the
    Phase C migration error and exits non-zero."""
    repo = init_repo(tmp_path)
    (repo / "intent.md").write_text("test intent\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["improve", "bugs", "--legacy", "focus"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    output = result.output + (result.stderr_bytes or b"").decode(errors="replace")
    # Click's CliRunner mixes stdout+stderr by default; check both.
    assert "Legacy improve loop has been removed" in output or \
        "Legacy improve loop has been removed" in result.output


def test_improve_feature_legacy_flag_exits_with_phase_c_message(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    (repo / "intent.md").write_text("test intent\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["improve", "feature", "--legacy", "search UX"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1


def test_improve_target_legacy_flag_exits_with_phase_c_message(
    tmp_path: Path,
) -> None:
    repo = init_repo(tmp_path)
    (repo / "intent.md").write_text("test intent\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["improve", "target", "--legacy", "latency < 100ms"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1


def test_improve_legacy_flag_does_not_invoke_orchestrate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--legacy`` must short-circuit BEFORE the i2p orchestrator is touched."""
    repo = init_repo(tmp_path)
    (repo / "intent.md").write_text("test intent\n", encoding="utf-8")

    sentinel = {"orchestrate_called": False}

    def fake_orchestrate(**_kwargs):
        sentinel["orchestrate_called"] = True
        import sys
        sys.exit(0)

    monkeypatch.setattr("otto.cli_run.orchestrate_improve", fake_orchestrate)

    result = CliRunner().invoke(
        main,
        ["improve", "bugs", "--legacy"],
        catch_exceptions=False,
    )
    assert result.exit_code == 1
    assert sentinel["orchestrate_called"] is False
