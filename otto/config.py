"""Otto configuration — load/create otto.yaml, auto-detect project settings."""

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    # Core
    "default_branch": "main",
    "max_retries": 3,
    "max_parallel": 1,              # 0 or 1 = serial (default); >1 = parallel tasks per batch
    "parallel_qa": True,            # per-task QA sessions in parallel (faster, costlier)
    "proof_of_work": False,         # audit/reporting metadata only; must not change merge-gating QA behavior
    "execution_mode": "monolithic", # monolithic = one integrated coding pass; planned = planner/batches
    "spec_generation_mode": "parallel",  # parallel = overlap with coding, before_coding = block and feed spec into attempt 1
    "test_command": None,           # auto-detected if not set
    "provider": "claude",           # coding agent provider (claude or codex)
    "planner_provider": None,       # planner-only provider override (None = provider)
    "model": None,                  # override provider model (e.g. sonnet, gpt-5)
    "planner_model": None,          # planner-only model override (None = provider default)
    "planner_effort": "medium",     # planner reasoning effort
    "fixed_plan": None,             # optional fixed execution plan for deterministic benchmarking/debugging

    # Timeouts
    "install_timeout": 120,         # seconds for dependency installation in worktrees
    "verify_timeout": 300,          # seconds for test suite in verify worktree
    "max_task_time": 3600,          # 1hr circuit breaker per task
    "qa_timeout": 3600,             # 1hr circuit breaker for QA agent

    # Harness toggles — disable phases for debugging/benchmarking
    "skip_spec": False,             # skip spec generation (no acceptance criteria)
    "skip_qa": False,               # skip QA agent (merge after tests pass)
    "skip_test": False,             # skip testing phase (merge after coding)

    # Agent CC settings scope — what settings each agent loads
    # "project": project CLAUDE.md only (default — no user skills/hooks overhead)
    # "user,project": also loads user CLAUDE.md, skills, hooks
    "coding_agent_settings": "project",
    "spec_agent_settings": "project",
    "qa_agent_settings": "project",
    "planner_agent_settings": "project",
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
    """Return the effective provider for coding/spec/QA agents."""
    return normalize_provider(
        config.get("provider"),
        default=DEFAULT_CONFIG["provider"],
        key="provider",
    ) or DEFAULT_CONFIG["provider"]


def planner_provider(config: dict[str, Any]) -> str:
    """Return the effective planner provider."""
    provider = normalize_provider(
        config.get("planner_provider"),
        default=None,
        key="planner_provider",
    )
    return provider or agent_provider(config)


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
    config["planner_provider"] = normalize_provider(
        config.get("planner_provider"),
        default=None,
        key="planner_provider",
    )
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


def discover_project_facts(project_dir: Path) -> list[str]:
    """Discover deterministic project facts for cross-run learnings.

    Returns canonical fact strings derived from files on disk. No LLM.
    Facts are refreshed each run — not accumulated.
    """
    facts: list[str] = []

    # Package manager
    if (project_dir / "package-lock.json").exists():
        facts.append("package manager: npm")
    elif (project_dir / "pnpm-lock.yaml").exists():
        facts.append("package manager: pnpm")
    elif (project_dir / "yarn.lock").exists():
        facts.append("package manager: yarn")
    elif (project_dir / "bun.lockb").exists():
        facts.append("package manager: bun")

    # Framework
    pkg_json = project_dir / "package.json"
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "next" in deps:
                facts.append("framework: Next.js")
            elif "nuxt" in deps:
                facts.append("framework: Nuxt")
            elif "react" in deps and "next" not in deps:
                facts.append("framework: React (no Next.js)")
            elif "vue" in deps:
                facts.append("framework: Vue")
            elif "svelte" in deps or "@sveltejs/kit" in deps:
                facts.append("framework: Svelte")
            # Module type
            if pkg.get("type") == "module":
                facts.append("module system: ESM (package.json type=module)")
        except (json.JSONDecodeError, OSError):
            pass

    if (project_dir / "pyproject.toml").exists():
        facts.append("python project: has pyproject.toml")

    # Test directory
    for test_dir in ("__tests__", "tests", "test", "spec"):
        if (project_dir / test_dir).is_dir():
            facts.append(f"test directory: {test_dir}/")
            break

    # Test command
    test_cmd = detect_test_command(project_dir)
    if test_cmd:
        facts.append(f"test command: {test_cmd}")

    # TypeScript
    if (project_dir / "tsconfig.json").exists():
        facts.append("language: TypeScript")

    # CSS framework
    if pkg_json.exists():
        try:
            pkg = json.loads(pkg_json.read_text())
            deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "tailwindcss" in deps:
                facts.append("styling: Tailwind CSS")
        except (json.JSONDecodeError, OSError):
            pass

    return facts


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
    lines += "\n"
    lines += "\n# Parallelism:\n"
    lines += f"# max_parallel: 1                # 0 or 1 = serial (default); >1 = parallel tasks per batch\n"
    lines += f"# parallel_qa: true              # per-task QA sessions in parallel (faster, costlier)\n"
    lines += f"# proof_of_work: false           # audit/reporting metadata only; should not affect merge-gating QA behavior\n"
    lines += f"# execution_mode: monolithic     # monolithic = one integrated coding pass; planned = planner/batches\n"
    lines += f"# spec_generation_mode: parallel # parallel or before_coding; before_coding feeds spec into attempt 1\n"
    lines += "\n# Timeouts:\n"
    lines += f"# verify_timeout: 300            # seconds for test suite in verify\n"
    lines += f"# max_task_time: 3600            # 1hr circuit breaker per task\n"
    lines += f"# qa_timeout: 3600               # 1hr circuit breaker for QA agent\n"
    lines += "\n# Provider + model:\n"
    lines += f"# provider: claude              # claude or codex\n"
    lines += f"# planner_provider: null        # override planner provider only\n"
    lines += f"# model: null                   # examples: sonnet, gpt-5.4, gpt-5.1, gpt-5-codex, gpt-5.1-codex\n"
    lines += f"#                               # other current Codex options include gpt-5.4-mini, gpt-5.4-nano,\n"
    lines += f"#                               # gpt-5.3-codex, gpt-5.2-codex, gpt-5.1-codex-max, gpt-5.1-codex-mini\n"
    lines += f"#                               # if unset, Otto uses the provider's local/default model\n"
    lines += f"# planner_model: null           # override planner model only; same model IDs as above\n"
    lines += f"# fixed_plan: null             # optional fixed execution plan (by task_ids/task_keys) for deterministic benchmarking\n"
    lines += "\n# Harness toggles (disable phases for debugging):\n"
    lines += f"# skip_spec: false               # skip spec generation\n"
    lines += f"# skip_qa: false                 # skip QA (merge after tests pass)\n"
    lines += f"# skip_test: false               # skip testing (merge after coding)\n"
    lines += "\n# Agent settings scope (project or user,project):\n"
    lines += f"# coding_agent_settings: project     # project CLAUDE.md only (default)\n"
    lines += f"# spec_agent_settings: project       # project CLAUDE.md only\n"
    lines += f"# qa_agent_settings: project         # project CLAUDE.md only\n"
    lines += f"# planner_agent_settings: project    # project CLAUDE.md only\n"
    lines += f"# Set to 'user,project' to also load ~/.claude/CLAUDE.md\n"
    lines += "\n# Product certification (otto build verifies with certifier after coding):\n"
    lines += f"# certifier_parallel_stories: 3  # stories per parallel batch (1 = sequential, 3 = default)\n"
    lines += f"# certifier_browser: null        # null = auto-detect from product type; true/false to force\n"
    lines += f"# certifier_skip_break: true     # skip adversarial break-testing phase (faster, default)\n"
    lines += f"# certifier_timeout: 900         # max seconds for entire certification subprocess\n"
    lines += f"# certifier_app_start_timeout: 90  # max seconds to wait for app to start per worker\n"
    lines += f"# certifier_interaction: null    # override product interaction type (http/cli/import/websocket)\n"
    lines += f"#                                # null = LLM discovery agent decides (recommended)\n"
    lines += f"# max_certify_rounds: 3          # max certify→fix→re-certify loops (default 3)\n"
    config_path.write_text(lines + "\n")

    # Update .git/info/exclude for runtime files (use git_meta_dir for linked worktrees)
    exclude_path = git_meta_dir(project_dir) / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text() if exclude_path.exists() else ""
    entries = ["tasks.yaml", ".tasks.lock", "otto_logs/", "otto.lock", "otto_arch/", ".otto-worktrees/"]
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
