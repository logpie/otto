"""Tests for otto.config module."""

import json
import re
from pathlib import Path

import pytest
import yaml

from otto.config import (
    ConfigError,
    DEFAULT_CONFIG,
    _normalize_intent,
    agent_provider,
    create_config,
    detect_default_branch,
    detect_test_command,
    get_max_rounds,
    get_max_turns_per_call,
    get_spec_timeout,
    git_meta_dir,
    load_config,
    resolve_intent_for_enqueue,
    resolve_project_dir,
    resolve_intent,
    resolve_certifier_mode,
    validate_certifier_mode,
)
from otto.setup_gitattributes import GitAttributesConflict


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


class TestResolveProjectDir:
    def test_resolves_repo_root_from_subdirectory(self, tmp_bare_git_repo):
        nested = tmp_bare_git_repo / "src" / "feature"
        nested.mkdir(parents=True)

        assert resolve_project_dir(nested) == tmp_bare_git_repo.resolve()

    def test_reports_missing_git_binary(self, tmp_path, monkeypatch):
        import subprocess

        def fake_run(*args, **kwargs):
            raise FileNotFoundError("git")

        monkeypatch.setattr(subprocess, "run", fake_run)
        with pytest.raises(ConfigError, match="git is not installed"):
            resolve_project_dir(tmp_path)


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

    def test_queue_section_defaults_present(self, tmp_bare_git_repo):
        """Phase 1.3: queue: section with all expected keys + correct defaults."""
        cfg = load_config(tmp_bare_git_repo / "otto.yaml")
        q = cfg["queue"]
        assert q["concurrent"] == 3
        assert q["worktree_dir"] == ".worktrees"
        assert q["on_watcher_restart"] == "resume"
        assert "intent.md" in q["bookkeeping_files"]
        assert "otto.yaml" in q["bookkeeping_files"]

    def test_queue_partial_override_preserves_other_defaults(self, tmp_bare_git_repo):
        """Deep-merge: user setting `queue.concurrent: 5` must not drop
        other queue defaults like `worktree_dir`."""
        config_path = tmp_bare_git_repo / "otto.yaml"
        config_path.write_text(yaml.dump({"queue": {"concurrent": 5}}))
        cfg = load_config(config_path)
        assert cfg["queue"]["concurrent"] == 5
        assert cfg["queue"]["worktree_dir"] == ".worktrees"   # preserved
        assert cfg["queue"]["on_watcher_restart"] == "resume" # preserved
        assert "intent.md" in cfg["queue"]["bookkeeping_files"]

    def test_queue_section_user_extra_keys_preserved(self, tmp_bare_git_repo):
        """Forward-compat: unknown queue keys round-trip cleanly."""
        config_path = tmp_bare_git_repo / "otto.yaml"
        config_path.write_text(yaml.dump({"queue": {"future_key": "x"}}))
        cfg = load_config(config_path)
        assert cfg["queue"]["future_key"] == "x"
        assert cfg["queue"]["concurrent"] == 3  # default still present

    def test_warns_when_queue_section_is_not_a_dict(self, tmp_bare_git_repo, caplog):
        config_path = tmp_bare_git_repo / "otto.yaml"
        config_path.write_text(yaml.dump({"queue": "not-a-dict"}))

        with caplog.at_level("WARNING", logger="otto.config"):
            cfg = load_config(config_path)

        assert cfg["queue"] == DEFAULT_CONFIG["queue"]
        assert "Invalid queue config" in caplog.text

    def test_warns_and_falls_back_for_invalid_queue_key_types(
        self, tmp_bare_git_repo, caplog
    ):
        config_path = tmp_bare_git_repo / "otto.yaml"
        config_path.write_text(yaml.dump({
            "queue": {
                "concurrent": "many",
                "bookkeeping_files": "intent.md",
            }
        }))

        with caplog.at_level("WARNING", logger="otto.config"):
            cfg = load_config(config_path)

        assert cfg["queue"]["concurrent"] == DEFAULT_CONFIG["queue"]["concurrent"]
        assert cfg["queue"]["bookkeeping_files"] == DEFAULT_CONFIG["queue"]["bookkeeping_files"]
        assert "Invalid queue.concurrent" in caplog.text
        assert "Invalid queue.bookkeeping_files" in caplog.text

    def test_load_config_does_not_mutate_default_queue(self, tmp_bare_git_repo):
        """Loading + mutating a config must not leak into DEFAULT_CONFIG."""
        cfg = load_config(tmp_bare_git_repo / "otto.yaml")
        cfg["queue"]["concurrent"] = 99
        # Re-load: should still see the original default
        cfg2 = load_config(tmp_bare_git_repo / "otto.yaml")
        assert cfg2["queue"]["concurrent"] == 3
        assert DEFAULT_CONFIG["queue"]["concurrent"] == 3

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

    def test_rejects_non_mapping_yaml_root(self, tmp_bare_git_repo):
        config_path = tmp_bare_git_repo / "otto.yaml"
        config_path.write_text("- not\n- a\n- mapping\n")
        with pytest.raises(ConfigError, match="expected a YAML mapping"):
            load_config(config_path)


class TestProviderHelpers:
    def test_agent_provider_defaults_to_claude(self):
        assert agent_provider({}) == "claude"

    def test_resolve_intent_for_enqueue_prefers_explicit_value(self, tmp_bare_git_repo):
        (tmp_bare_git_repo / "intent.md").write_text("from project")
        assert resolve_intent_for_enqueue(tmp_bare_git_repo, explicit="from cli") == "from cli"

    def test_default_config_exposes_max_turns(self):
        assert DEFAULT_CONFIG["max_turns_per_call"] == 200

    def test_normalize_intent_collapses_multiline_whitespace(self):
        assert _normalize_intent("a kanban board:\n  localStorage") == "a kanban board: localStorage"

    def test_resolve_certifier_mode_defaults_to_fast(self):
        assert resolve_certifier_mode({}) == "fast"

    def test_resolve_certifier_mode_uses_yaml_value(self):
        assert resolve_certifier_mode({"certifier_mode": "standard"}) == "standard"

    def test_resolve_certifier_mode_cli_overrides_yaml(self):
        assert resolve_certifier_mode({"certifier_mode": "standard"}, cli_mode="fast") == "fast"

    def test_resolve_certifier_mode_rejects_unknown_value(self):
        with pytest.raises(ValueError, match="Expected one of"):
            validate_certifier_mode("turbo")

    def test_get_spec_timeout_uses_default(self):
        assert get_spec_timeout({}) == 600

    def test_get_max_turns_per_call_uses_default(self):
        assert get_max_turns_per_call({}) == 200

    def test_get_max_turns_per_call_rejects_values_above_cap(self):
        with pytest.raises(ConfigError, match="<= 200"):
            get_max_turns_per_call({"max_turns_per_call": 201})

    def test_get_max_rounds_rejects_values_above_cap(self):
        with pytest.raises(ConfigError, match="<= 50"):
            get_max_rounds({"max_certify_rounds": 51})

    def test_readme_fallback_is_not_truncated(self, tmp_bare_git_repo):
        (tmp_bare_git_repo / "README.md").write_text("A" * 2500)
        assert len(resolve_intent(tmp_bare_git_repo) or "") == 2500

    def test_oversized_readme_is_rejected(self, tmp_bare_git_repo):
        (tmp_bare_git_repo / "README.md").write_text("A" * 9000)
        with pytest.raises(ConfigError, match="8"):
            resolve_intent(tmp_bare_git_repo)

    def test_legacy_runtime_intent_log_is_ignored_in_favor_of_readme(self, tmp_bare_git_repo):
        (tmp_bare_git_repo / "intent.md").write_text(
            "# Build Intents\n\n## 2026-04-20 12:00 (run-1)\nold runtime entry\n"
        )
        (tmp_bare_git_repo / "README.md").write_text("canonical product description")
        assert resolve_intent(tmp_bare_git_repo) == "canonical product description"

    def test_intent_read_error_falls_back_to_readme(self, tmp_bare_git_repo, monkeypatch):
        (tmp_bare_git_repo / "intent.md").write_text("canonical intent")
        (tmp_bare_git_repo / "README.md").write_text("readme fallback")

        original_read_text = Path.read_text

        def fake_read_text(path: Path, *args, **kwargs):
            if path == tmp_bare_git_repo / "intent.md":
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad byte")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake_read_text)
        assert resolve_intent(tmp_bare_git_repo) == "readme fallback"

    def test_readme_read_error_raises_config_error(self, tmp_bare_git_repo, monkeypatch):
        (tmp_bare_git_repo / "README.md").write_text("canonical readme")

        original_read_text = Path.read_text

        def fake_read_text(path: Path, *args, **kwargs):
            if path == tmp_bare_git_repo / "README.md":
                raise PermissionError("denied")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake_read_text)
        with pytest.raises(ConfigError, match="README.md"):
            resolve_intent(tmp_bare_git_repo)

    def test_readme_otto_yaml_example_loads_with_nested_agents(self, tmp_bare_git_repo):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        match = re.search(
            r"## Configuration \(`otto\.yaml`\).*?```yaml\n(.*?)\n```",
            readme,
            re.DOTALL,
        )
        assert match, "README otto.yaml example not found"

        config_path = tmp_bare_git_repo / "otto.yaml"
        config_path.write_text(match.group(1))
        cfg = load_config(config_path)

        assert cfg["agents"]["build"]["model"] == "opus"
        assert cfg["agents"]["certifier"]["model"] == "sonnet"
        assert cfg["agents"]["spec"]["model"] == "sonnet"


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

    def test_ignores_current_feature_branch_when_no_remote(self, tmp_path):
        import subprocess

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
        (repo / "README.md").write_text("hello\n")
        subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
        subprocess.run(
            ["git", "checkout", "-q", "-b", "feature/x"],
            cwd=repo,
            check=True,
        )

        assert detect_default_branch(repo) == "main"


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

    def test_skips_gitattributes_install_when_bookkeeping_opted_out(
        self, tmp_bare_git_repo, monkeypatch
    ):
        import otto.config as config_module
        import otto.setup_gitattributes as setup_gitattributes

        original_load_config = config_module.load_config
        install_calls: list[object] = []

        def fake_load_config(config_path):
            cfg = original_load_config(config_path)
            cfg["queue"]["bookkeeping_files"] = []
            return cfg

        def fake_install(project_dir):
            install_calls.append(project_dir)
            return True

        monkeypatch.setattr(config_module, "load_config", fake_load_config)
        monkeypatch.setattr(setup_gitattributes, "install", fake_install)

        create_config(tmp_bare_git_repo)

        assert install_calls == []

    def test_create_config_propagates_gitattributes_conflict(self, tmp_bare_git_repo):
        (tmp_bare_git_repo / ".gitattributes").write_text("intent.md merge=binary\n")

        with pytest.raises(GitAttributesConflict, match="intent.md"):
            create_config(tmp_bare_git_repo)


class TestSetupCommandExistingConfig:
    def test_setup_installs_gitattributes_for_existing_config(
        self, tmp_bare_git_repo, monkeypatch
    ):
        from click.testing import CliRunner

        import otto.cli_setup as cli_setup
        from otto.cli import main

        async def fake_run_setup_query(prompt, project_dir, config=None):
            return "# CLAUDE\n"

        create_config(tmp_bare_git_repo)
        gitattributes_path = tmp_bare_git_repo / ".gitattributes"
        gitattributes_path.unlink()

        monkeypatch.chdir(tmp_bare_git_repo)
        monkeypatch.setattr(cli_setup, "_run_setup_query", fake_run_setup_query)

        result = CliRunner().invoke(main, ["setup"], input="\n", catch_exceptions=False)

        assert result.exit_code == 0
        assert "intent.md merge=union" in gitattributes_path.read_text()
        assert "otto.yaml merge=ours" in gitattributes_path.read_text()

    def test_setup_propagates_gitattributes_conflict_for_existing_config(
        self, tmp_bare_git_repo, monkeypatch
    ):
        from click.testing import CliRunner

        from otto.cli import main

        create_config(tmp_bare_git_repo)
        (tmp_bare_git_repo / ".gitattributes").write_text("intent.md merge=binary\n")

        monkeypatch.chdir(tmp_bare_git_repo)

        with pytest.raises(GitAttributesConflict, match="intent.md"):
            CliRunner().invoke(main, ["setup"], catch_exceptions=False)

    def test_setup_skips_gitattributes_install_when_existing_config_opted_out(
        self, tmp_bare_git_repo, monkeypatch
    ):
        from click.testing import CliRunner

        import otto.cli_setup as cli_setup
        from otto.cli import main

        async def fake_run_setup_query(prompt, project_dir, config=None):
            return "# CLAUDE\n"

        config_path = tmp_bare_git_repo / "otto.yaml"
        config_path.write_text(yaml.safe_dump({
            "default_branch": "main",
            "queue": {"bookkeeping_files": []},
        }))
        gitattributes_path = tmp_bare_git_repo / ".gitattributes"
        if gitattributes_path.exists():
            gitattributes_path.unlink()

        monkeypatch.chdir(tmp_bare_git_repo)
        monkeypatch.setattr(cli_setup, "_run_setup_query", fake_run_setup_query)

        result = CliRunner().invoke(main, ["setup"], input="\n", catch_exceptions=False)

        assert result.exit_code == 0
        assert not gitattributes_path.exists()
