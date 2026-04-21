"""Otto configuration — load/create otto.yaml, auto-detect project settings."""

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Single source of truth for all configuration defaults.
#
# Precedence, highest first:
#   1. CLI flag (e.g. `otto build --thorough`)
#   2. otto.yaml (created by `otto setup`, never auto-created)
#   3. DEFAULTS (this dict)
#
# Per-agent overrides (agents.<name>.model etc.) only via otto.yaml —
# CLI has global --model/--provider/--effort flags that apply to all
# agents in the run. If finer control is needed, edit otto.yaml.
# ---------------------------------------------------------------------------

AGENT_TYPES = ("build", "certifier", "spec", "fix")

DEFAULTS: dict[str, Any] = {
    # Project setup (auto-detected if None)
    "default_branch":         None,
    "test_command":           None,

    # Global agent defaults (fallback for every agent)
    "provider":               "claude",
    "model":                  None,      # None = provider default
    "effort":                 None,      # low | medium | high | max

    # Per-agent overrides (None = inherit global)
    "agents": {
        "build":     {"provider": None, "model": None, "effort": None},
        "certifier": {"provider": None, "model": None, "effort": None},
        "spec":      {"provider": None, "model": None, "effort": None},
        "fix":       {"provider": None, "model": None, "effort": None},
    },

    # Budgets & caps
    "run_budget_seconds":     3600,
    "spec_timeout":           600,
    "max_certify_rounds":     8,

    # Per-invocation defaults (CLI typically overrides)
    "certifier_mode":         "fast",    # fast | standard | thorough
    "skip_product_qa":        False,
    "split_mode":             False,

    # Features (opt-in)
    "memory":                 False,
}

# Kept as an alias for backward compatibility with tests and external
# callers. Prefer DEFAULTS in new code.
DEFAULT_CONFIG: dict[str, Any] = DEFAULTS

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


def agent_provider(config: dict[str, Any], agent_type: str | None = None) -> str:
    """Return the effective provider for a given agent.

    Resolution order: per-agent override > global config > built-in default.
    ``agent_type`` is one of AGENT_TYPES (build, certifier, spec, fix).
    If None, returns the global provider only.
    """
    if agent_type:
        per_agent = (config.get("agents", {}) or {}).get(agent_type, {}) or {}
        override = per_agent.get("provider")
        if override:
            return normalize_provider(
                override, default=None, key=f"agents.{agent_type}.provider",
            ) or DEFAULTS["provider"]
    return normalize_provider(
        config.get("provider"),
        default=DEFAULTS["provider"],
        key="provider",
    ) or DEFAULTS["provider"]


def agent_model(config: dict[str, Any], agent_type: str | None = None) -> str | None:
    """Return the effective model for a given agent, or None for provider default.

    Resolution: per-agent override > global config.model > None.
    """
    if agent_type:
        per_agent = (config.get("agents", {}) or {}).get(agent_type, {}) or {}
        if per_agent.get("model"):
            return str(per_agent["model"])
    return config.get("model") or None


def agent_effort(config: dict[str, Any], agent_type: str | None = None) -> str | None:
    """Return the effective effort level for a given agent.

    Resolution: per-agent override > global config.effort > None.
    """
    if agent_type:
        per_agent = (config.get("agents", {}) or {}).get(agent_type, {}) or {}
        if per_agent.get("effort"):
            return str(per_agent["effort"])
    return config.get("effort") or None



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


def _merge_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge ``raw`` over DEFAULTS, with one level of nesting for
    ``agents.<name>`` so partial per-agent overrides work correctly.
    """
    merged: dict[str, Any] = {}
    for k, v in DEFAULTS.items():
        if isinstance(v, dict) and k == "agents":
            merged[k] = {name: dict(sub) for name, sub in v.items()}
        else:
            merged[k] = v
    for k, v in raw.items():
        if k == "agents" and isinstance(v, dict):
            for name, sub in v.items():
                if name in merged["agents"] and isinstance(sub, dict):
                    merged["agents"][name].update(sub)
                else:
                    merged["agents"][name] = dict(sub) if isinstance(sub, dict) else sub
        else:
            merged[k] = v
    return merged


def load_config(config_path: Path) -> dict[str, Any]:
    """Load otto.yaml, filling missing keys with DEFAULTS.

    If otto.yaml is missing, returns DEFAULTS with auto-detected
    ``test_command`` / ``default_branch`` filled in. The yaml is NEVER
    auto-created — only ``otto setup`` writes it.
    """
    project_dir = config_path.parent
    if not config_path.exists():
        config = _merge_defaults({})
    else:
        raw = yaml.safe_load(config_path.read_text()) or {}
        config = _merge_defaults(raw if isinstance(raw, dict) else {})

    # Normalize + validate the global provider string.
    config["provider"] = agent_provider(config)

    # Auto-detect project-specific values when yaml didn't set them.
    if not config.get("test_command"):
        config["test_command"] = detect_test_command(project_dir)
    if not config.get("default_branch"):
        config["default_branch"] = detect_default_branch(project_dir)
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


_YAML_TEMPLATE = """\
# otto.yaml — project-level defaults for otto build/certify/improve.
#
# Precedence (highest first):
#   1. CLI flag (e.g. `otto build --thorough`)
#   2. otto.yaml (this file)
#   3. built-in defaults (see otto/config.py::DEFAULTS)
#
# Commented-out lines show the built-in default. Uncomment + edit to
# override. Delete the file to revert to defaults.

# ─── Project setup ───────────────────────────────────────────────────
default_branch: {default_branch}      # detected
test_command: {test_command}          # detected; set explicitly if wrong

# ─── Global agent defaults (applied to every agent) ──────────────────
provider: {provider}                  # claude | codex
# model: null                         # override provider model (e.g. sonnet, haiku, gpt-5)
# effort: null                        # low | medium | high | max (provider-specific)

# ─── Per-agent overrides (inherit global if not set) ─────────────────
# agents:
#   build:     {{provider: null, model: null, effort: null}}
#   certifier: {{provider: null, model: null, effort: null}}
#   spec:      {{provider: null, model: null, effort: null}}
#   fix:       {{provider: null, model: null, effort: null}}

# ─── Budgets & caps ──────────────────────────────────────────────────
run_budget_seconds: {run_budget_seconds}      # total wall-clock (primary knob)
# spec_timeout: {spec_timeout}                # cap on the spec-agent call only
# max_certify_rounds: {max_certify_rounds}    # max certify→fix loop iterations

# ─── Per-invocation defaults (CLI typically overrides) ───────────────
# certifier_mode: {certifier_mode}            # fast | standard | thorough
# skip_product_qa: false                      # --no-qa equivalent
# split_mode: false                           # --split equivalent

# ─── Features ────────────────────────────────────────────────────────
# memory: false                       # cross-run certifier memory (opt-in)
"""


def create_config(project_dir: Path) -> Path:
    """Write otto.yaml with every knob present at its default, plus
    auto-detected ``default_branch`` / ``test_command``. Updates
    .git/info/exclude to ignore otto_logs/.

    Only called by ``otto setup`` — never auto-invoked from build/
    certify/improve.
    """
    default_branch = detect_default_branch(project_dir) or "main"
    test_command = detect_test_command(project_dir) or "null"

    config_path = project_dir / "otto.yaml"
    config_path.write_text(_YAML_TEMPLATE.format(
        default_branch=default_branch,
        test_command=test_command,
        provider=DEFAULTS["provider"],
        run_budget_seconds=DEFAULTS["run_budget_seconds"],
        spec_timeout=DEFAULTS["spec_timeout"],
        max_certify_rounds=DEFAULTS["max_certify_rounds"],
        certifier_mode=DEFAULTS["certifier_mode"],
    ))

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
