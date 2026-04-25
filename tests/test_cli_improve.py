from __future__ import annotations

from pathlib import Path

import pytest

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
