"""Otto configuration — load/create otto.yaml, auto-detect project settings."""

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "test_command": None,
    "max_retries": 3,
    "model": None,
    "default_branch": "main",
    "verify_timeout": 300,
    "max_parallel": 3,
    "effort": "high",
    "orchestrator": "v4",
    # Agent settings scope — controls which CC settings are loaded per agent type
    # "user,project": loads user CLAUDE.md, skills, hooks + project settings
    # "project": loads only project settings (faster, skips user skills/hooks)
    "coding_agent_settings": "user,project",   # coding benefits from user CLAUDE.md
    "spec_agent_settings": "project",           # spec doesn't need user prefs
    "qa_agent_settings": "project",             # QA doesn't need user prefs
}


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

    # npm/node test (check first — JS projects often also have tests/ dir)
    pkg_json = project_dir / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            if "test" in pkg.get("scripts", {}):
                candidates.append("npm test")
        except (json.JSONDecodeError, KeyError):
            pass

    # pytest
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
        candidates.append("pytest")

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
        "max_retries": DEFAULT_CONFIG["max_retries"],
        "verify_timeout": DEFAULT_CONFIG["verify_timeout"],
    }
    # Only include optional keys if they differ from defaults,
    # so the generated file is clean but discoverable via comments.
    if test_command:
        config["test_command"] = test_command

    # Write config with comments showing all available options
    config_path = project_dir / "otto.yaml"
    lines = yaml.dump(config, default_flow_style=False, sort_keys=False).rstrip()
    lines += "\n\n# Other options (uncomment to customize):\n"
    lines += f"# test_command: pytest          # auto-detected if not set\n"
    lines += f"# model: null                   # override Claude model (e.g. sonnet)\n"
    lines += f"# max_turns: 200                # max agent turns per task\n"
    lines += f"# effort: high                  # agent thinking effort (low/medium/high/max)\n"
    lines += f"# max_parallel: 3               # max parallel tasks (future)\n"
    lines += f"# researcher_model: sonnet       # model for research subagent (sonnet/haiku/opus)\n"
    lines += f"# max_task_time: 900             # max seconds per task (prevents unbounded retries)\n"
    config_path.write_text(lines + "\n")

    # Update .git/info/exclude for runtime files (use git_meta_dir for linked worktrees)
    exclude_path = git_meta_dir(project_dir) / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text() if exclude_path.exists() else ""
    entries = ["tasks.yaml", ".tasks.lock", "otto_logs/", "otto.lock", "otto_arch/"]
    to_add = [e for e in entries if e not in existing]
    if to_add:
        with open(exclude_path, "a") as f:
            f.write("\n# otto runtime files\n")
            for entry in to_add:
                f.write(f"{entry}\n")

    return config_path
