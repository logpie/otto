"""Tests for Playwright browser preinstall + gitignore additions.

The preinstall (run once at pipeline start) turns 22% of agent
``npx playwright install`` time-wasters into ~1s no-ops by ensuring
chromium is cached before children dispatch.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from otto.v5_runner import _ensure_playwright_browsers, _project_uses_playwright


def test_project_uses_playwright_true_when_devdep(tmp_path: Path) -> None:
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "package.json").write_text(
        '{"name": "x", "devDependencies": {"@playwright/test": "^1.40.0"}}'
    )
    assert _project_uses_playwright(tmp_path) is True


def test_project_uses_playwright_true_when_dep(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"@playwright/test": "1.40"}}'
    )
    assert _project_uses_playwright(tmp_path) is True


def test_project_uses_playwright_false_when_absent(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"react": "18"}}'
    )
    assert _project_uses_playwright(tmp_path) is False


def test_project_uses_playwright_skips_node_modules(tmp_path: Path) -> None:
    """A nested node_modules/.../package.json containing @playwright/test is
    NOT what we want to consume — only the project's own manifests."""
    (tmp_path / "node_modules" / "some-pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "some-pkg" / "package.json").write_text(
        '{"dependencies": {"@playwright/test": "1.40"}}'
    )
    (tmp_path / "package.json").write_text('{"dependencies": {"react": "18"}}')
    assert _project_uses_playwright(tmp_path) is False


def test_project_uses_playwright_no_manifest(tmp_path: Path) -> None:
    assert _project_uses_playwright(tmp_path) is False


def test_ensure_browsers_no_playwright_returns_false(tmp_path: Path) -> None:
    """Pre-install is a no-op for projects that don't use Playwright."""
    (tmp_path / "package.json").write_text('{"dependencies": {"react": "18"}}')
    assert _ensure_playwright_browsers(tmp_path) is False


def test_ensure_browsers_cache_exists_skips_install(tmp_path: Path) -> None:
    """If ``~/.cache/ms-playwright/chromium-*`` exists, skip the install."""
    (tmp_path / "package.json").write_text(
        '{"devDependencies": {"@playwright/test": "1"}}'
    )
    fake_cache = tmp_path / ".pwcache"
    (fake_cache / "chromium-1140").mkdir(parents=True)
    with patch("os.path.expanduser", return_value=str(fake_cache)):
        # subprocess.run shouldn't be called; install side-effect must be skipped.
        with patch("otto.v5_runner.subprocess.run") as mock_run:
            result = _ensure_playwright_browsers(tmp_path)
    assert result is True
    mock_run.assert_not_called()


def test_ensure_browsers_runs_install_when_cache_empty(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"devDependencies": {"@playwright/test": "1"}}'
    )
    empty_cache = tmp_path / "empty_cache"
    empty_cache.mkdir()
    with patch("os.path.expanduser", return_value=str(empty_cache)):
        with patch("shutil.which", return_value="/usr/bin/npx"):
            with patch("otto.v5_runner.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stderr = ""
                mock_run.return_value.stdout = ""
                result = _ensure_playwright_browsers(tmp_path)
    assert result is True
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert "playwright" in cmd
    assert "install" in cmd
    assert "chromium" in cmd


def test_ensure_browsers_install_failure_returns_false(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"devDependencies": {"@playwright/test": "1"}}'
    )
    with patch("os.path.expanduser", return_value=str(tmp_path / "nonexistent")):
        with patch("shutil.which", return_value="/usr/bin/npx"):
            with patch("otto.v5_runner.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 1
                mock_run.return_value.stderr = "download failed"
                mock_run.return_value.stdout = ""
                result = _ensure_playwright_browsers(tmp_path)
    assert result is False


def test_default_gitignore_includes_tsbuildinfo() -> None:
    """The default gitignore should cover *.tsbuildinfo to prevent the
    merge conflicts we've seen on those files."""
    from otto.v5_branching import _DEFAULT_GITIGNORE

    assert "*.tsbuildinfo" in _DEFAULT_GITIGNORE
