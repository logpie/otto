"""Tests for otto.config module."""

import json

import pytest
import yaml

from otto.config import (
    DEFAULT_CONFIG,
    _normalize_intent,
    agent_provider,
    create_config,
    detect_default_branch,
    detect_test_command,
    git_meta_dir,
    load_config,
)


class TestGitMetaDir:
    def test_returns_dot_git_for_normal_repo(self, tmp_bare_git_repo):
        result = git_meta_dir(tmp_bare_git_repo)
        assert result == tmp_bare_git_repo / ".git"

    def test_returns_common_dir_for_linked_worktree(self, tmp_bare_git_repo):
        """In a linked worktree, git_meta_dir returns the shared .git/ dir."""
        import subprocess
        wt_path = tmp_bare_git_repo / "worktrees" / "test-wt"
        subprocess.run(
            ["git", "worktree", "add", str(wt_path), "-b", "test-wt-branch"],
            cwd=tmp_bare_git_repo, capture_output=True, check=True,
        )
        result = git_meta_dir(wt_path)
        # Should point to the main repo's .git, not the worktree's .git file
        assert result == tmp_bare_git_repo / ".git"
        # Clean up
        subprocess.run(
            ["git", "worktree", "remove", str(wt_path)],
            cwd=tmp_bare_git_repo, capture_output=True,
        )


class TestLoadConfig:
    def test_loads_valid_config(self, tmp_bare_git_repo):
        config_path = tmp_bare_git_repo / "otto.yaml"
        config_path.write_text(yaml.dump({"test_command": "pytest"}))
        cfg = load_config(config_path)
        assert cfg["test_command"] == "pytest"

    def test_fills_defaults_for_missing_keys(self, tmp_bare_git_repo):
        config_path = tmp_bare_git_repo / "otto.yaml"
        config_path.write_text(yaml.dump({"test_command": "pytest"}))
        cfg = load_config(config_path)
        assert cfg["provider"] == DEFAULT_CONFIG["provider"]
        assert cfg["model"] == DEFAULT_CONFIG["model"]

    def test_returns_defaults_when_file_missing(self, tmp_bare_git_repo):
        cfg = load_config(tmp_bare_git_repo / "otto.yaml")
        # load_config fills in auto-detected project values even without a
        # yaml — default_branch comes from git.
        expected = {**DEFAULT_CONFIG, "default_branch": "main"}
        assert cfg == expected

    def test_loads_empty_file(self, tmp_bare_git_repo):
        config_path = tmp_bare_git_repo / "otto.yaml"
        config_path.write_text("")
        cfg = load_config(config_path)
        # Auto-detect adds default_branch=main (from git) and leaves
        # test_command=None for bare repos with no test framework.
        expected = {**DEFAULT_CONFIG, "default_branch": "main", "test_command": None}
        assert cfg == expected

    def test_normalizes_provider_fields(self, tmp_bare_git_repo):
        config_path = tmp_bare_git_repo / "otto.yaml"
        config_path.write_text(yaml.dump({
            "provider": "CODEX",
        }))
        cfg = load_config(config_path)
        assert cfg["provider"] == "codex"

    def test_rejects_invalid_provider(self, tmp_bare_git_repo):
        config_path = tmp_bare_git_repo / "otto.yaml"
        config_path.write_text(yaml.dump({"provider": "not-a-provider"}))
        with pytest.raises(ValueError, match="Invalid provider"):
            load_config(config_path)


class TestProviderHelpers:
    def test_agent_provider_defaults_to_claude(self):
        assert agent_provider({}) == "claude"

    def test_default_config_exposes_spec_timeout(self):
        assert DEFAULT_CONFIG["spec_timeout"] == 600

    def test_normalize_intent_collapses_multiline_whitespace(self):
        assert _normalize_intent("a kanban board:\n  localStorage") == "a kanban board: localStorage"


class TestDetectTestCommand:
    def test_detects_pytest(self, tmp_bare_git_repo):
        (tmp_bare_git_repo / "tests").mkdir()
        (tmp_bare_git_repo / "tests" / "test_example.py").write_text("def test_x(): pass\n")
        result = detect_test_command(tmp_bare_git_repo)
        assert result == "pytest"

    def test_detects_npm_test(self, tmp_bare_git_repo):
        pkg = {"scripts": {"test": "jest"}}
        (tmp_bare_git_repo / "package.json").write_text(json.dumps(pkg))
        result = detect_test_command(tmp_bare_git_repo)
        assert result == "npm test"

    def test_returns_none_when_nothing_found(self, tmp_bare_git_repo):
        result = detect_test_command(tmp_bare_git_repo)
        assert result is None

    def test_chains_multiple_frameworks(self, tmp_bare_git_repo):
        """Mixed-language project: npm test + pytest chained with &&."""
        (tmp_bare_git_repo / "tests").mkdir()
        (tmp_bare_git_repo / "tests" / "test_example.py").write_text("def test_x(): pass\n")
        pkg = {"scripts": {"test": "jest"}}
        (tmp_bare_git_repo / "package.json").write_text(json.dumps(pkg))
        result = detect_test_command(tmp_bare_git_repo)
        assert result == "npm test && pytest"

    def test_empty_tests_dir_no_pytest(self, tmp_bare_git_repo):
        """tests/ dir with no Python test files shouldn't trigger pytest."""
        (tmp_bare_git_repo / "tests").mkdir()
        (tmp_bare_git_repo / "tests" / "helper.js").write_text("module.exports = {}")
        result = detect_test_command(tmp_bare_git_repo)
        assert result is None

    @pytest.mark.parametrize("placeholder", [
        'echo "Error: no test specified" && exit 1',
        'echo "Error" && exit 1',
    ])
    def test_skips_npm_placeholder(self, tmp_bare_git_repo, placeholder):
        """npm init placeholders should not produce 'npm test'."""
        pkg = {"scripts": {"test": placeholder}}
        (tmp_bare_git_repo / "package.json").write_text(json.dumps(pkg))
        assert detect_test_command(tmp_bare_git_repo) is None

    @pytest.mark.parametrize("lockfile,expected", [
        ("pnpm-lock.yaml", "pnpm test"),
        ("yarn.lock", "yarn test"),
        ("bun.lockb", "bun test"),
    ])
    def test_detects_package_manager_from_lockfile(self, tmp_bare_git_repo, lockfile, expected):
        pkg = {"scripts": {"test": "vitest"}}
        (tmp_bare_git_repo / "package.json").write_text(json.dumps(pkg))
        (tmp_bare_git_repo / lockfile).write_text("")
        assert detect_test_command(tmp_bare_git_repo) == expected

    @pytest.mark.parametrize("config_name", ["deno.json", "deno.jsonc"])
    def test_detects_deno(self, tmp_bare_git_repo, config_name):
        (tmp_bare_git_repo / config_name).write_text("{}")
        assert detect_test_command(tmp_bare_git_repo) == "deno test"

    def test_detects_tox(self, tmp_bare_git_repo):
        (tmp_bare_git_repo / "tests").mkdir()
        (tmp_bare_git_repo / "tests" / "test_example.py").write_text("def test_x(): pass\n")
        (tmp_bare_git_repo / "tox.ini").write_text("[tox]\nenvlist = py3\n")
        result = detect_test_command(tmp_bare_git_repo)
        # tox should replace bare pytest
        assert result == "tox"
        assert "pytest" not in result

    def test_detects_nox(self, tmp_bare_git_repo):
        (tmp_bare_git_repo / "tests").mkdir()
        (tmp_bare_git_repo / "tests" / "test_example.py").write_text("def test_x(): pass\n")
        (tmp_bare_git_repo / "noxfile.py").write_text("import nox\n")
        result = detect_test_command(tmp_bare_git_repo)
        assert result == "nox"
        assert "pytest" not in result

    def test_detects_makefile_test_target(self, tmp_bare_git_repo):
        (tmp_bare_git_repo / "Makefile").write_text("test:\n\tpytest\n")
        result = detect_test_command(tmp_bare_git_repo)
        assert result == "make test"


class TestDetectDefaultBranch:
    def test_detects_main(self, tmp_path):
        """Explicitly init with 'main' so the assertion can be tight."""
        import subprocess
        repo = tmp_path / "repo"
        repo.mkdir()
        # -b main forces the branch name regardless of git's init.defaultBranch
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        assert detect_default_branch(repo) == "main"

    def test_returns_main_as_fallback(self, tmp_path):
        # Non-git directory
        assert detect_default_branch(tmp_path) == "main"


class TestCreateConfig:
    def test_creates_config_file(self, tmp_bare_git_repo):
        config_path = create_config(tmp_bare_git_repo)
        assert config_path.exists()
        cfg = yaml.safe_load(config_path.read_text())
        assert "default_branch" in cfg

    def test_create_config_mentions_spec_timeout(self, tmp_bare_git_repo):
        config_path = create_config(tmp_bare_git_repo)
        assert "# spec_timeout: 600" in config_path.read_text()

    def test_updates_git_info_exclude(self, tmp_bare_git_repo):
        create_config(tmp_bare_git_repo)
        exclude_path = tmp_bare_git_repo / ".git" / "info" / "exclude"
        content = exclude_path.read_text()
        assert "otto_logs/" in content
