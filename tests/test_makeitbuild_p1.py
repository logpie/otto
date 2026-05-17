from __future__ import annotations

import subprocess
from pathlib import Path

from otto.v5_capability_inventory import inject_into_charter
from otto.v5_runner import _commit_charter_injection_if_dirty


def _git(project_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=project_dir,
        text=True,
        capture_output=True,
        check=True,
    )


def _init_repo(project_dir: Path) -> None:
    _git(project_dir, "init")
    _git(project_dir, "config", "user.email", "otto@example.invalid")
    _git(project_dir, "config", "user.name", "Otto Test")
    (project_dir / "CHARTER.md").write_text("# Charter\n\n", encoding="utf-8")
    _git(project_dir, "add", "CHARTER.md")
    _git(project_dir, "commit", "-m", "initial charter")


def test_capability_inventory_injection_is_committed_and_clean(tmp_path: Path) -> None:
    _init_repo(tmp_path)

    changed = inject_into_charter(tmp_path, "## Detected Infrastructure\n\n- npm: none\n")
    assert changed is True

    ok, detail = _commit_charter_injection_if_dirty(tmp_path)

    assert ok, detail
    assert _git(tmp_path, "status", "--short").stdout == ""
    assert "CHARTER.md" in _git(tmp_path, "show", "--name-only", "--format=", "HEAD").stdout
    assert "record detected infrastructure in CHARTER" in _git(
        tmp_path,
        "log",
        "-1",
        "--pretty=%s",
    ).stdout
