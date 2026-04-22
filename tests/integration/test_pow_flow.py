from __future__ import annotations

import subprocess
from pathlib import Path

from otto import paths


def test_pow_cli_opens_latest_session_report(
    tmp_otto_repo: Path,
    cli_in_repo,
    monkeypatch,
) -> None:
    run_id = "2026-04-22-090000-abcdef"
    paths.ensure_session_scaffold(tmp_otto_repo, run_id, phase="certify")
    pow_html = paths.certify_dir(tmp_otto_repo, run_id) / "proof-of-work.html"
    pow_html.write_text("<html>pow</html>\n")
    paths.set_pointer(tmp_otto_repo, paths.LATEST_POINTER, run_id)

    opened: list[list[str]] = []

    def fake_open(args: list[str], **kwargs):
        opened.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("otto.cli_pow.require_git", lambda: None)
    monkeypatch.setattr("otto.cli_pow.resolve_project_dir", lambda _path: tmp_otto_repo)
    monkeypatch.setattr("otto.cli._resolve_git_worktree_context", lambda _path: None)
    monkeypatch.setattr("otto.cli._check_venv_guard", lambda **kwargs: (False, None))
    monkeypatch.setattr("otto.cli_pow.subprocess.run", fake_open)

    result = cli_in_repo(tmp_otto_repo, ["pow"])

    assert result.exit_code == 0, result.output
    assert opened
    assert opened[0][-1] == str(pow_html.resolve())
