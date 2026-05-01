from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from otto.cli import main
from otto.cli_improve import _run_improve
from tests._helpers import init_repo


def test_improve_reports_malformed_otto_yaml_cleanly(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = init_repo(tmp_path)
    (repo / "otto.yaml").write_text("default_branch: [\n", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        _run_improve(
            repo,
            intent="fix bugs",
            rounds=1,
            focus=None,
            certifier_mode="fast",
            command_label="improve bugs",
            subcommand="bugs",
        )

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "otto.yaml" in output
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


def test_improve_feature_uses_focus_when_project_readme_is_oversized(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = init_repo(tmp_path)
    (repo / "README.md").write_text("x" * 9000, encoding="utf-8")
    monkeypatch.chdir(repo)

    with patch("otto.cli_improve._run_improve") as run_improve:
        result = CliRunner().invoke(
            main,
            ["improve", "feature", "small footer polish", "--allow-dirty"],
            catch_exceptions=False,
        )

    assert result.exit_code == 0
    assert run_improve.call_args.kwargs["intent"] == "small footer polish"
    assert run_improve.call_args.kwargs["focus"] == "small footer polish"
