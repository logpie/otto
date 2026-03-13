"""Tests for otto.config module."""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from otto.config import (
    DEFAULT_CONFIG,
    create_config,
    detect_default_branch,
    detect_test_command,
    git_meta_dir,
    load_config,
)


class TestGitMetaDir:
    def test_returns_dot_git_for_normal_repo(self, tmp_git_repo):
        result = git_meta_dir(tmp_git_repo)
        assert result == tmp_git_repo / ".git"

    def test_returns_common_dir_for_linked_worktree(self, tmp_git_repo):
        """In a linked worktree, git_meta_dir returns the shared .git/ dir."""
        import subprocess
        wt_path = tmp_git_repo / "worktrees" / "test-wt"
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), "-b", "test-wt-branch"],
            cwd=tmp_git_repo, capture_output=True, check=True,
        )
        result = git_meta_dir(wt_path)
        # Should point to the main repo's .git, not the worktree's .git file
        assert result == tmp_git_repo / ".git"
        # Clean up
        subprocess.run(
            ["git", "worktree", "remove", str(wt_path)],
            cwd=tmp_git_repo, capture_output=True,
        )


class TestLoadConfig:
    def test_loads_valid_config(self, tmp_git_repo):
        config_path = tmp_git_repo / "otto.yaml"
        config_path.write_text(yaml.dump({"test_command": "pytest", "max_retries": 5}))
        cfg = load_config(config_path)
        assert cfg["test_command"] == "pytest"
        assert cfg["max_retries"] == 5

    def test_fills_defaults_for_missing_keys(self, tmp_git_repo):
        config_path = tmp_git_repo / "otto.yaml"
        config_path.write_text(yaml.dump({"test_command": "pytest"}))
        cfg = load_config(config_path)
        assert cfg["max_retries"] == DEFAULT_CONFIG["max_retries"]
        assert cfg["model"] == DEFAULT_CONFIG["model"]
        assert cfg["verify_timeout"] == DEFAULT_CONFIG["verify_timeout"]

    def test_returns_defaults_when_file_missing(self, tmp_git_repo):
        cfg = load_config(tmp_git_repo / "otto.yaml")
        assert cfg == DEFAULT_CONFIG

    def test_loads_empty_file(self, tmp_git_repo):
        config_path = tmp_git_repo / "otto.yaml"
        config_path.write_text("")
        cfg = load_config(config_path)
        assert cfg == DEFAULT_CONFIG


class TestDetectTestCommand:
    def test_detects_pytest(self, tmp_git_repo):
        (tmp_git_repo / "tests").mkdir()
        (tmp_git_repo / "tests" / "test_example.py").write_text("def test_x(): pass\n")
        result = detect_test_command(tmp_git_repo)
        assert result == "pytest"

    def test_detects_npm_test(self, tmp_git_repo):
        pkg = {"scripts": {"test": "jest"}}
        (tmp_git_repo / "package.json").write_text(json.dumps(pkg))
        result = detect_test_command(tmp_git_repo)
        assert result == "npm test"

    def test_returns_none_when_nothing_found(self, tmp_git_repo):
        result = detect_test_command(tmp_git_repo)
        assert result is None

    def test_returns_none_when_ambiguous(self, tmp_git_repo):
        (tmp_git_repo / "tests").mkdir()
        (tmp_git_repo / "tests" / "test_example.py").write_text("def test_x(): pass\n")
        pkg = {"scripts": {"test": "jest"}}
        (tmp_git_repo / "package.json").write_text(json.dumps(pkg))
        result = detect_test_command(tmp_git_repo)
        assert result is None


class TestDetectDefaultBranch:
    def test_detects_main(self, tmp_git_repo):
        result = detect_default_branch(tmp_git_repo)
        # git init creates 'main' by default on modern git
        assert result in ("main", "master")

    def test_returns_main_as_fallback(self, tmp_path):
        # Non-git directory
        result = detect_default_branch(tmp_path)
        assert result == "main"


class TestCreateConfig:
    def test_creates_config_file(self, tmp_git_repo):
        config_path = create_config(tmp_git_repo)
        assert config_path.exists()
        cfg = yaml.safe_load(config_path.read_text())
        assert "test_command" in cfg
        assert "default_branch" in cfg

    def test_updates_git_info_exclude(self, tmp_git_repo):
        create_config(tmp_git_repo)
        exclude_path = tmp_git_repo / ".git" / "info" / "exclude"
        content = exclude_path.read_text()
        assert "tasks.yaml" in content
        assert "otto_logs/" in content
