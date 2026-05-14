"""Tests for the install-dir sharing optimization.

The earlier symlink implementation looked only at
``project_dir/{node_modules,.venv}`` — which doesn't exist in real
projects, where install dirs live in subdirs (``frontend/node_modules``,
``api/.venv``). These tests exercise the fix:

  - ``_iter_install_dirs`` walks subdirs, skips nested noise
  - ``_link_shared_install_dirs`` creates per-subdir symlinks
  - ``_propagate_install_dirs_from_architect`` mirrors the architect's
    install dirs into ``project_dir`` so later children can symlink them
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from otto.v5_runner import (
    _iter_install_dirs,
    _link_shared_install_dirs,
    _propagate_install_dirs_from_architect,
    _write_toolchain_preflight_log,
)
from otto.v5_clean_verify import preflight_shared_toolchains


def _populate_dir(d: Path, *, files: dict[str, str] | None = None) -> None:
    """Create a directory with optional populated files (so it's non-empty)."""
    d.mkdir(parents=True, exist_ok=True)
    for name, content in (files or {}).items():
        (d / name).write_text(content)


def test_iter_install_dirs_finds_subsystem_node_modules(tmp_path: Path) -> None:
    """The real layout: frontend/node_modules, api/.venv."""
    _populate_dir(tmp_path / "frontend" / "node_modules", files={"placeholder": "x"})
    _populate_dir(tmp_path / "api" / ".venv", files={"placeholder": "x"})

    found = list(_iter_install_dirs(tmp_path))
    rels = sorted(str(rel) for _path, rel in found)
    assert "frontend/node_modules" in rels
    assert "api/.venv" in rels


def test_iter_install_dirs_skips_nested(tmp_path: Path) -> None:
    """A node_modules inside node_modules is nested noise — not shared."""
    _populate_dir(tmp_path / "frontend" / "node_modules" / "package-a" / "node_modules")
    _populate_dir(tmp_path / "frontend" / "node_modules")

    found = list(_iter_install_dirs(tmp_path))
    rels = sorted(str(rel) for _path, rel in found)
    # Top-level frontend/node_modules: yes. The nested one: no.
    assert "frontend/node_modules" in rels
    assert "frontend/node_modules/package-a/node_modules" not in rels


def test_iter_install_dirs_skips_git_worktrees(tmp_path: Path) -> None:
    """Don't share dirs that live inside another worktree."""
    _populate_dir(tmp_path / ".worktrees" / "v5-foo" / "frontend" / "node_modules")
    found = list(_iter_install_dirs(tmp_path))
    rels = [str(rel) for _path, rel in found]
    assert not any("worktrees" in r for r in rels)


def test_iter_install_dirs_empty_when_nothing_to_share(tmp_path: Path) -> None:
    _populate_dir(tmp_path / "src", files={"main.py": "x"})
    assert list(_iter_install_dirs(tmp_path)) == []


def test_link_shared_install_dirs_creates_symlinks(tmp_path: Path) -> None:
    """Real fix verification: symlinks created at subsystem-correct paths."""
    project = tmp_path / "project"
    project.mkdir()
    _populate_dir(project / "frontend" / "node_modules", files={"a.js": "x"})
    _populate_dir(project / "api" / ".venv", files={"pyvenv.cfg": "x"})

    worktree = tmp_path / "worktree"
    worktree.mkdir()

    n = _link_shared_install_dirs(project, worktree, "v5-test")
    assert n == 2

    fe_link = worktree / "frontend" / "node_modules"
    be_link = worktree / "api" / ".venv"
    assert fe_link.is_symlink()
    assert be_link.is_symlink()
    # And they point at the real source.
    assert fe_link.resolve() == (project / "frontend" / "node_modules").resolve()
    assert be_link.resolve() == (project / "api" / ".venv").resolve()
    # Symlinked content is readable through the link.
    assert (fe_link / "a.js").read_text() == "x"


def test_link_shared_install_dirs_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _populate_dir(project / "frontend" / "node_modules")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    # First call creates 1 link; second call finds it already there.
    assert _link_shared_install_dirs(project, worktree, "v5-x") == 1
    assert _link_shared_install_dirs(project, worktree, "v5-x") == 0


def test_link_shared_install_dirs_no_source_creates_nothing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    assert _link_shared_install_dirs(project, worktree, "v5-x") == 0
    # No noise files left in worktree.
    assert list(worktree.iterdir()) == []


def test_propagate_from_architect_links_into_project_dir(tmp_path: Path) -> None:
    """The main-case bug: after architect runs install in its worktree,
    project_dir has no install dirs. Propagation creates symlinks at
    the same subsystem-relative paths in project_dir."""
    project = tmp_path / "project"
    project.mkdir()
    # No install dirs in project yet.
    assert list(_iter_install_dirs(project)) == []

    architect_wt = tmp_path / ".worktrees" / "v5-arch"
    _populate_dir(
        architect_wt / "frontend" / "node_modules",
        files={"index.js": "x"},
    )
    _populate_dir(architect_wt / "api" / ".venv", files={"pyvenv.cfg": "x"})

    n = _propagate_install_dirs_from_architect(project, architect_wt)
    assert n == 2

    # Now project has symlinks pointing at architect's install dirs.
    assert (project / "frontend" / "node_modules").is_symlink()
    assert (project / "api" / ".venv").is_symlink()
    assert (project / "frontend" / "node_modules").resolve() == (
        architect_wt / "frontend" / "node_modules"
    ).resolve()


def test_propagate_then_link_e2e(tmp_path: Path) -> None:
    """End-to-end: architect worktree → project_dir → feature child worktree."""
    project = tmp_path / "project"
    project.mkdir()
    architect_wt = tmp_path / "arch_wt"
    _populate_dir(architect_wt / "frontend" / "node_modules", files={"a.js": "x"})

    # 1) Propagate from architect.
    assert _propagate_install_dirs_from_architect(project, architect_wt) == 1

    # 2) Now a feature child's worktree symlinks through project_dir.
    feat_wt = tmp_path / "feat_wt"
    feat_wt.mkdir()
    assert _link_shared_install_dirs(project, feat_wt, "v5-feat") == 1

    # Feature child sees the file via two-hop symlink chain.
    fe_link = feat_wt / "frontend" / "node_modules"
    assert fe_link.is_symlink()
    assert (fe_link / "a.js").read_text() == "x"


def test_propagate_skips_existing(tmp_path: Path) -> None:
    """If project_dir already has the install dir (rare but possible),
    don't overwrite — propagation respects existing state."""
    project = tmp_path / "project"
    _populate_dir(project / "frontend" / "node_modules", files={"existing.js": "1"})
    architect_wt = tmp_path / "arch"
    _populate_dir(architect_wt / "frontend" / "node_modules", files={"new.js": "2"})

    n = _propagate_install_dirs_from_architect(project, architect_wt)
    assert n == 0  # didn't overwrite
    # Existing dir intact, not a symlink.
    fe = project / "frontend" / "node_modules"
    assert fe.is_dir() and not fe.is_symlink()
    assert (fe / "existing.js").read_text() == "1"


def test_propagate_missing_architect_worktree_returns_zero(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    missing = tmp_path / "does_not_exist"
    assert _propagate_install_dirs_from_architect(project, missing) == 0


def test_toolchain_preflight_runs_manifests_inside_architect_worktree(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    architect_wt = tmp_path / "project" / ".worktrees" / "v5-architect"
    (architect_wt / "frontend").mkdir(parents=True)
    (architect_wt / "frontend" / "package.json").write_text(
        '{"devDependencies":{"@playwright/test":"1.0.0"}}\n',
        encoding="utf-8",
    )
    (architect_wt / "api").mkdir()
    (architect_wt / "api" / "pyproject.toml").write_text(
        "[project]\nname='api'\n",
        encoding="utf-8",
    )
    commands: list[tuple[list[str], Path]] = []

    def fake_which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in {"npm", "uv", "npx"} else None

    def fake_run(cmd: list[str], *, cwd: Path, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append((cmd, Path(cwd)))
        if cmd[1] in {"install", "ci"}:
            (Path(cwd) / "node_modules").mkdir()
        if cmd[1] == "sync":
            (Path(cwd) / ".venv").mkdir()
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr("otto.v5_clean_verify.shutil.which", fake_which)
    monkeypatch.setattr("otto.v5_clean_verify._playwright_chromium_cached", lambda: False)
    monkeypatch.setattr("otto.v5_clean_verify.subprocess.run", fake_run)

    result = preflight_shared_toolchains(architect_wt)

    assert result.passed is True
    assert result.manifest_counts == {"package_json": 1, "pyproject": 1}
    assert [cmd[:3] for cmd, _cwd in commands] == [
        ["/usr/bin/npm", "install", "--no-audit"],
        ["/usr/bin/uv", "sync"],
        ["/usr/bin/npx", "playwright", "install"],
    ]
    assert (architect_wt / "frontend" / "node_modules").is_dir()
    assert (architect_wt / "api" / ".venv").is_dir()


def test_toolchain_preflight_log_is_numbered_and_timestamped(tmp_path: Path) -> None:
    result = {
        "passed": True,
        "worktree": str(tmp_path / ".worktrees" / "v5-architect"),
        "commands": [],
        "failure_messages": [],
        "manifest_counts": {"package_json": 0, "pyproject": 0},
    }

    path = _write_toolchain_preflight_log(
        project_dir=tmp_path,
        architect_task_id="v5-architect",
        retry_count=1,
        result=result,
    )

    assert path.name == "toolchain-preflight-v5-architect-attempt-2.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["_written_at"]
    assert payload["architect_task_id"] == "v5-architect"
