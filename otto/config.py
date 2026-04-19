"""Otto configuration — load/create otto.yaml, auto-detect project settings."""

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    # Core
    "provider": "claude",           # coding agent provider (claude or codex)
    "model": None,                  # override provider model (e.g. sonnet, gpt-5)

    # Wall-clock budget for the entire invocation (build, certify, or improve).
    # The single knob users should reason about. Covers all internal agent
    # calls. 1h default.
    "run_budget_seconds": 3600,

    # Per-phase cap: the spec agent is short (1-3 min in practice). This
    # bounds it without eating the full run budget.
    "spec_timeout": 600,            # cap on the spec-agent call specifically
    "max_certify_rounds": 8,        # max certification rounds in build loop
}

SUPPORTED_PROVIDERS = {"claude", "codex"}


def normalize_provider(
    value: str | None,
    *,
    default: str | None = None,
    key: str = "provider",
) -> str | None:
    """Normalize provider strings and reject unsupported values."""
    if value is None or value == "":
        return default
    provider = str(value).strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        choices = ", ".join(sorted(SUPPORTED_PROVIDERS))
        raise ValueError(f"Invalid {key}: {value!r}. Expected one of: {choices}")
    return provider


def agent_provider(config: dict[str, Any]) -> str:
    """Return the effective provider for coding agents."""
    return normalize_provider(
        config.get("provider"),
        default=DEFAULT_CONFIG["provider"],
        key="provider",
    ) or DEFAULT_CONFIG["provider"]



def get_run_budget(config: dict[str, Any]) -> int:
    """Read `run_budget_seconds` from config. Default 3600.

    This is the total wall-clock budget for the entire invocation.
    """
    import logging
    _logger = logging.getLogger("otto.config")
    default = int(DEFAULT_CONFIG["run_budget_seconds"])
    raw = config.get("run_budget_seconds", default)
    try:
        value = int(raw)
    except (ValueError, TypeError):
        _logger.warning("Invalid run_budget_seconds (%r), using default %ds", raw, default)
        return default
    if value <= 0:
        _logger.warning("run_budget_seconds must be positive, using default %ds", default)
        return default
    return value


def get_max_rounds(config: dict[str, Any]) -> int:
    """Read max_certify_rounds from config with validation."""
    import logging
    _logger = logging.getLogger("otto.config")
    default = int(DEFAULT_CONFIG.get("max_certify_rounds", 8))
    try:
        value = int(config.get("max_certify_rounds", default))
    except (ValueError, TypeError):
        _logger.warning("Invalid max_certify_rounds, using default %d", default)
        return default
    return max(1, value)


def resolve_intent(project_dir: Path) -> str | None:
    """Resolve product description from intent.md or README.md.

    Returns the intent string, or None if no intent file found or content empty.
    """
    intent_path = project_dir / "intent.md"
    readme_path = project_dir / "README.md"
    if intent_path.exists():
        intent = intent_path.read_text().strip()
        if intent:
            return intent
    if readme_path.exists():
        intent = readme_path.read_text().strip()[:2000]
        if intent:
            return intent
    return None


def git_meta_dir(project_dir: Path) -> Path:
    """Return the canonical git metadata directory (handles linked worktrees).

    In a normal repo, this returns project_dir/.git.
    In a linked worktree, .git is a file containing 'gitdir: <path>' — this
    resolves the shared .git/ dir without spawning a subprocess.
    """
    dot_git = project_dir / ".git"
    if dot_git.is_dir():
        return dot_git
    if dot_git.is_file():
        # Linked worktree: .git file contains "gitdir: <relative-path>"
        content = dot_git.read_text().strip()
        if content.startswith("gitdir:"):
            wt_gitdir = Path(content[len("gitdir:"):].strip())
            if not wt_gitdir.is_absolute():
                wt_gitdir = (project_dir / wt_gitdir).resolve()
            # Walk up from the worktree gitdir to find commondir
            # e.g. /repo/.git/worktrees/wt-name/ → commondir file points to /repo/.git
            commondir_file = wt_gitdir / "commondir"
            if commondir_file.exists():
                rel = commondir_file.read_text().strip()
                common = Path(rel) if Path(rel).is_absolute() else (wt_gitdir / rel).resolve()
                return common
            return wt_gitdir
    # Fallback: use subprocess for unusual repo layouts
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=project_dir, capture_output=True, text=True, check=True,
    )
    path = Path(result.stdout.strip())
    if not path.is_absolute():
        path = (project_dir / path).resolve()
    return path


def load_config(config_path: Path) -> dict[str, Any]:
    """Load otto.yaml, filling missing keys with defaults.

    If test_command is not explicitly set, auto-detects at load time.
    """
    if not config_path.exists():
        return dict(DEFAULT_CONFIG)
    raw = yaml.safe_load(config_path.read_text()) or {}
    config = {**DEFAULT_CONFIG, **raw}
    config["provider"] = agent_provider(config)
    # Auto-detect test_command if not explicitly configured
    if "test_command" not in raw:
        config["test_command"] = detect_test_command(config_path.parent)
    return config


def detect_test_command(project_dir: Path) -> str | None:
    """Auto-detect the project's test command.

    If multiple test frameworks detected (e.g., npm test + pytest for a React
    project with Python e2e tests), chains them with &&.
    Returns None only if no framework detected.
    """
    candidates = []

    # npm/node/deno test (check first — JS projects often also have tests/ dir)
    pkg_json = project_dir / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            test_script = pkg.get("scripts", {}).get("test", "")
            # Skip npm init placeholder: 'echo "Error: no test specified" && exit 1'
            # Also skip other common placeholder patterns
            is_placeholder = (
                not test_script
                or "no test specified" in test_script
                or test_script.strip() == 'echo "Error" && exit 1'
            )
            if not is_placeholder:
                # Use the correct package manager for running scripts
                if (project_dir / "pnpm-lock.yaml").exists():
                    candidates.append("pnpm test")
                elif (project_dir / "yarn.lock").exists():
                    candidates.append("yarn test")
                elif (project_dir / "bun.lockb").exists():
                    candidates.append("bun test")
                else:
                    candidates.append("npm test")
        except (json.JSONDecodeError, KeyError):
            pass

    # deno test
    if (project_dir / "deno.json").exists() or (project_dir / "deno.jsonc").exists():
        candidates.append("deno test")

    # pytest — prefer project venv if it exists
    tests_dir = project_dir / "tests"
    test_dir = project_dir / "test"
    has_py_tests = False
    if tests_dir.is_dir():
        has_py_tests = any(tests_dir.glob("test_*.py")) or any(tests_dir.glob("*_test.py"))
    if test_dir.is_dir() and not has_py_tests:
        has_py_tests = any(test_dir.glob("test_*.py")) or any(test_dir.glob("*_test.py"))
    # Also check root-level test files
    if not has_py_tests:
        has_py_tests = any(project_dir.glob("test_*.py"))
    if has_py_tests:
        # Check project venv first — Flask/Django projects install deps there
        venv_pytest = project_dir / ".venv" / "bin" / "pytest"
        if venv_pytest.exists():
            candidates.append(f"{venv_pytest}")
        else:
            candidates.append("pytest")

    # tox (Python test runner — prefer over bare pytest when present)
    if (project_dir / "tox.ini").exists():
        # tox already detected — don't also add pytest (tox runs it)
        candidates = [c for c in candidates if c not in ("pytest",) and not c.endswith("/pytest")]
        candidates.append("tox")

    # nox (Python test runner — similar to tox)
    if (project_dir / "noxfile.py").exists():
        candidates = [c for c in candidates if c not in ("pytest",) and not c.endswith("/pytest")]
        candidates.append("nox")

    # go test
    if (project_dir / "go.mod").exists():
        candidates.append("go test ./...")

    # cargo test
    if (project_dir / "Cargo.toml").exists():
        candidates.append("cargo test")

    # maven
    if (project_dir / "pom.xml").exists():
        candidates.append("mvn test")

    # gradle
    if (project_dir / "build.gradle").exists() or (project_dir / "build.gradle.kts").exists():
        candidates.append("gradle test")

    # ruby
    if (project_dir / "Gemfile").exists() and (project_dir / "Rakefile").exists():
        candidates.append("bundle exec rake test")

    # cmake / ctest
    if (project_dir / "CMakeLists.txt").exists():
        candidates.append("cmake --build build --target test")

    # make test (generic fallback)
    makefile = project_dir / "Makefile"
    if makefile.exists() and "test:" in makefile.read_text():
        candidates.append("make test")

    if not candidates:
        return None
    # Chain all detected commands — all must pass
    return " && ".join(candidates)


def detect_default_branch(project_dir: Path) -> str:
    """Detect the default branch name. Fallback to 'main'."""
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            # refs/remotes/origin/main → main
            return result.stdout.strip().split("/")[-1]
    except FileNotFoundError:
        pass

    # Fallback: check current branch name
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except FileNotFoundError:
        pass

    return "main"


def create_config(project_dir: Path) -> Path:
    """Create otto.yaml with auto-detected settings. Updates .git/info/exclude."""
    default_branch = detect_default_branch(project_dir)

    # Auto-detect test command
    test_command = detect_test_command(project_dir)

    config = {
        "default_branch": default_branch,
    }
    if test_command:
        config["test_command"] = test_command

    # Write config with comments showing all available options
    config_path = project_dir / "otto.yaml"
    lines = yaml.dump(config, default_flow_style=False, sort_keys=False).rstrip()
    lines += "\n"
    lines += "\n# Provider + model:\n"
    lines += "# provider: claude              # claude or codex\n"
    lines += "# model: null                   # override provider model\n"
    lines += "#                               # if unset, Otto uses the provider's local/default model\n"
    lines += "\n# Budget + certification:\n"
    lines += "# run_budget_seconds: 3600      # total wall-clock for the whole run (primary knob)\n"
    lines += "# certifier_mode: standard      # fast | standard | thorough — CLI no-flag default is fast (cheap dev loop)\n"
    lines += "# max_certify_rounds: 8         # max certify→fix attempts before giving up\n"
    lines += "# spec_timeout: 600             # cap on the spec-agent call specifically\n"
    config_path.write_text(lines + "\n")

    # Update .git/info/exclude for runtime files (use git_meta_dir for linked worktrees)
    exclude_path = git_meta_dir(project_dir) / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text() if exclude_path.exists() else ""
    entries = ["otto_logs/", "otto.lock"]
    to_add = [e for e in entries if e not in existing]
    if to_add:
        with open(exclude_path, "a") as f:
            f.write("\n# otto runtime files\n")
            for entry in to_add:
                f.write(f"{entry}\n")

    return config_path


def require_git() -> None:
    """Exit with a friendly error if not in a git repo."""
    import sys
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        capture_output=True, cwd=Path.cwd(),
    )
    if result.returncode != 0:
        from otto.theme import error_console
        error_console.print("Error: not a git repository. Run 'git init' first.", style="error")
        sys.exit(2)
