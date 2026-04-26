"""Otto configuration — load/create otto.yaml, auto-detect project settings."""

import json
import hashlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Single source of truth for all configuration defaults.
#
# Precedence, highest first:
#   1. CLI flag (e.g. `otto build --thorough` or `--certifier-provider codex`)
#   2. otto.yaml (created by `otto setup`, never auto-created)
#   3. DEFAULTS (this dict)
#
# Per-agent overrides can come from otto.yaml or explicit phase CLI flags.
# ---------------------------------------------------------------------------

AGENT_TYPES = ("build", "certifier", "spec", "fix")
MAX_INTENT_CHARS = 8 * 1024
PROJECT_INTENT_MIN_CHARS = 50
MAX_SPEC_CHARS = 32 * 1024
MAX_CERTIFY_ROUNDS = 50

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
    "max_turns_per_call":     200,

    # Per-invocation defaults (CLI typically overrides)
    "certifier_mode":         "fast",    # fast | standard | thorough
    "skip_product_qa":        False,
    "split_mode":             True,
    "strict_mode":            False,
    "allow_dirty_repo":       False,

    # Features (opt-in)
    "memory":                 False,

    # Queue settings — used by `otto queue` runner and `otto merge`.
    # See plan-parallel.md §3.3.
    "queue": {
        "concurrent":           3,             # default --concurrent for `otto queue run`
        "worktree_dir":         ".worktrees",  # where per-task worktrees live (relative to project)
        "on_watcher_restart":   "resume",      # resume | fail
        "task_timeout_s":        4200.0,        # per-task timeout; 0/null disables
        "merge_certifier_mode":  "standard",    # fast | standard | thorough
        "bookkeeping_files": [                 # files queue tasks should NOT commit to their branches
            "intent.md",
            "otto.yaml",
        ],
    },
}

# Kept as an alias for backward compatibility with tests and external
# callers. Prefer DEFAULTS in new code.
DEFAULT_CONFIG: dict[str, Any] = DEFAULTS

SUPPORTED_PROVIDERS = {"claude", "codex"}
PUBLIC_CERTIFIER_MODES = ("fast", "standard", "thorough")
INTERNAL_CERTIFIER_MODES = ("hillclimb", "target")
SUPPORTED_CERTIFIER_MODES = PUBLIC_CERTIFIER_MODES + INTERNAL_CERTIFIER_MODES
_REPO_STATE_MARKERS: tuple[tuple[str, str], ...] = (
    ("MERGE_HEAD", "merge in progress"),
    ("rebase-merge", "rebase in progress"),
    ("rebase-apply", "rebase/apply in progress"),
    ("CHERRY_PICK_HEAD", "cherry-pick in progress"),
    ("REVERT_HEAD", "revert in progress"),
    ("BISECT_LOG", "bisect in progress"),
)


class ConfigError(ValueError):
    """User-facing configuration error."""


def _run_git(
    project_dir: Path,
    *args: str,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=check,
        )
    except OSError as exc:
        raise ConfigError(f"Failed to run git {' '.join(args)!r}: {exc}") from exc
    return result


def _normalize_intent(s: str) -> str:
    """Collapse shell-wrapped or multiline intent input to one clean line."""
    return re.sub(r"\s+", " ", s).strip()


def resolve_project_dir(start_dir: Path | None = None) -> Path:
    """Return the canonical git worktree root for a path.

    Uses ``git rev-parse --show-toplevel`` so running Otto from a subdirectory
    of the same repo still resolves to one shared runtime root.
    """
    cwd = Path.cwd() if start_dir is None else Path(start_dir)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=cwd,
        )
    except OSError as exc:
        if isinstance(exc, FileNotFoundError) or exc.errno == 2:
            raise ConfigError("git is not installed or not on PATH") from exc
        raise ConfigError(f"Failed to resolve git worktree root from {cwd}: {exc}") from exc

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise ConfigError(
            f"Not a git repository: {cwd}. "
            f"{stderr or 'Run `git init` first.'}"
        )

    root = (result.stdout or "").strip()
    if not root:
        raise ConfigError(f"Git did not return a worktree root for {cwd}")
    return Path(root).resolve()


def resolve_certifier_mode(
    config: dict[str, Any] | None,
    cli_mode: str | None = None,
    *,
    allow_internal: bool = False,
) -> str:
    """Resolve certifier mode with CLI > config > DEFAULTS precedence."""
    if cli_mode:
        return validate_certifier_mode(
            cli_mode,
            key="CLI certifier mode",
            allow_internal=True,
        )
    resolved = (config or {}).get("certifier_mode")
    if resolved:
        return validate_certifier_mode(resolved, allow_internal=allow_internal)
    return str(DEFAULTS["certifier_mode"])


def validate_certifier_mode(
    value: str | None,
    *,
    key: str = "certifier_mode",
    allow_internal: bool = True,
) -> str:
    """Normalize certifier mode strings and reject unsupported values."""
    mode = str(value or "").strip().lower()
    if not mode:
        return str(DEFAULTS["certifier_mode"])
    choices_tuple = SUPPORTED_CERTIFIER_MODES if allow_internal else PUBLIC_CERTIFIER_MODES
    if mode not in choices_tuple:
        choices = ", ".join(choices_tuple)
        raise ValueError(f"Invalid {key}: {value!r}. Expected one of: {choices}")
    return mode


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


def _cli_agent_override(config: dict[str, Any], key: str, agent_type: str | None = None) -> Any:
    overrides = (config.get("_cli_overrides") or {}) if isinstance(config, dict) else {}
    if not isinstance(overrides, dict):
        return None
    if agent_type:
        agents = overrides.get("agents")
        if isinstance(agents, dict):
            agent_overrides = agents.get(agent_type)
            if isinstance(agent_overrides, dict) and agent_overrides.get(key):
                return agent_overrides[key]
    return overrides.get(key)


def agent_provider(config: dict[str, Any], agent_type: str | None = None) -> str:
    """Return the effective provider for a given agent.

    Resolution order: phase CLI override > global CLI override > per-agent config
    > global config > built-in default.
    ``agent_type`` is one of AGENT_TYPES (build, certifier, spec, fix).
    If None, returns the global provider only.
    """
    cli_override = _cli_agent_override(config, "provider", agent_type)
    if cli_override:
        return normalize_provider(
            cli_override,
            default=DEFAULTS["provider"],
            key=f"agents.{agent_type}.provider" if agent_type else "provider",
        ) or DEFAULTS["provider"]
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

    Resolution: phase CLI override > global CLI override > per-agent override
    > global config.model > None.
    """
    cli_override = _cli_agent_override(config, "model", agent_type)
    if cli_override:
        return str(cli_override)
    if agent_type:
        per_agent = (config.get("agents", {}) or {}).get(agent_type, {}) or {}
        if per_agent.get("model"):
            return str(per_agent["model"])
    return config.get("model") or None


def agent_effort(config: dict[str, Any], agent_type: str | None = None) -> str | None:
    """Return the effective effort level for a given agent.

    Resolution: phase CLI override > global CLI override > per-agent override
    > global config.effort > None.
    """
    cli_override = _cli_agent_override(config, "effort", agent_type)
    if cli_override:
        return str(cli_override)
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


def get_spec_timeout(config: dict[str, Any]) -> int:
    """Read `spec_timeout` from config. Default 600."""
    import logging
    _logger = logging.getLogger("otto.config")
    default = int(DEFAULT_CONFIG["spec_timeout"])
    raw = config.get("spec_timeout", default)
    try:
        value = int(raw)
    except (ValueError, TypeError):
        _logger.warning("Invalid spec_timeout (%r), using default %ds", raw, default)
        return default
    if value <= 0:
        _logger.warning("spec_timeout must be positive, using default %ds", default)
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
    if value < 1:
        raise ConfigError("max_certify_rounds must be at least 1")
    if value > MAX_CERTIFY_ROUNDS:
        raise ConfigError(f"max_certify_rounds must be <= {MAX_CERTIFY_ROUNDS}")
    return value


def get_max_turns_per_call(config: dict[str, Any]) -> int:
    """Read max_turns_per_call from config with validation."""
    import logging
    _logger = logging.getLogger("otto.config")
    default = int(DEFAULT_CONFIG.get("max_turns_per_call", 200))
    try:
        value = int(config.get("max_turns_per_call", default))
    except (ValueError, TypeError):
        _logger.warning("Invalid max_turns_per_call, using default %d", default)
        return default
    if value < 1:
        raise ConfigError("max_turns_per_call must be at least 1")
    if value > default:
        raise ConfigError(f"max_turns_per_call must be <= {default}")
    return value


def validate_text_limit(text: str, *, kind: str, source: str, max_chars: int) -> str:
    """Reject oversized user-controlled text before prompt rendering."""
    if len(text) <= max_chars:
        return text
    raise ConfigError(
        f"{kind} exceeds the {max_chars}-character limit ({source}). "
        "Trim the source content and try again."
    )


def read_project_intent_md(project_dir: Path, *, min_chars: int = 1) -> str | None:
    """Read project-root intent.md when present and sufficiently informative."""
    from otto import paths

    intent_path = paths.project_intent_md(project_dir)
    if not intent_path.exists():
        return None
    try:
        intent = intent_path.read_text().strip()
    except (UnicodeDecodeError, IsADirectoryError, PermissionError, OSError) as exc:
        raise ConfigError(f"Failed to read {intent_path}: {exc}") from exc
    if not intent or len(intent) < min_chars or _looks_like_intent_log(intent):
        return None
    return validate_text_limit(
        intent,
        kind="intent",
        source=str(intent_path),
        max_chars=MAX_INTENT_CHARS,
    )


def resolve_intent(project_dir: Path) -> str | None:
    """Resolve the user-owned product description from intent.md or README.md.

    Runtime snapshots belong in `otto_logs/sessions/<id>/intent.txt`; project-root
    `intent.md` is treated as a single curated product description. Legacy
    cumulative otto-generated intent logs are ignored and we fall back to README.md.
    """
    from otto import paths

    readme_path = paths.project_readme_md(project_dir)
    try:
        intent = read_project_intent_md(project_dir)
    except ConfigError as exc:
        if readme_path.exists():
            try:
                readme = readme_path.read_text().strip()
            except (UnicodeDecodeError, IsADirectoryError, PermissionError, OSError) as readme_exc:
                intent_path = paths.project_intent_md(project_dir)
                raise ConfigError(
                    f"Failed to read {intent_path}: {exc}. "
                    f"Fallback {readme_path} also failed: {readme_exc}"
                ) from exc
            if readme:
                return validate_text_limit(
                    readme,
                    kind="intent",
                    source=str(readme_path),
                    max_chars=MAX_INTENT_CHARS,
                )
        raise
    if intent:
        return intent
    if readme_path.exists():
        try:
            intent = readme_path.read_text().strip()
        except (UnicodeDecodeError, IsADirectoryError, PermissionError, OSError) as exc:
            raise ConfigError(f"Failed to read {readme_path}: {exc}") from exc
        if intent:
            return validate_text_limit(
                intent,
                kind="intent",
                source=str(readme_path),
                max_chars=MAX_INTENT_CHARS,
            )
    return None

def resolve_intent_for_enqueue(
    project_dir: Path, *, explicit: str | None = None,
) -> str | None:
    """Resolve the intent snapshot stored in queue.yml at enqueue time.

    Explicit CLI input wins when provided and non-empty after trimming.
    Otherwise read the current project files now so queued tasks retain a
    stable intent snapshot even if intent.md/README.md changes later.
    """
    if explicit is not None:
        trimmed = explicit.strip()
        if trimmed:
            return trimmed
    return resolve_intent(project_dir)

def resolve_intent_provenance(project_dir: Path) -> dict[str, str]:
    """Return intent text plus the source/fallback metadata used to resolve it."""
    intent_path = project_dir / "intent.md"
    readme_path = project_dir / "README.md"
    fallback_reason = ""
    source = ""
    resolved_text = ""

    if intent_path.exists():
        try:
            intent = intent_path.read_text().strip()
        except (UnicodeDecodeError, IsADirectoryError, PermissionError, OSError) as exc:
            fallback_reason = f"intent.md was unreadable: {exc}"
        else:
            if intent and not _looks_like_intent_log(intent):
                return {
                    "source": "intent.md",
                    "fallback_reason": "",
                    "resolved_text": validate_text_limit(
                        intent,
                        kind="intent",
                        source=str(intent_path),
                        max_chars=MAX_INTENT_CHARS,
                    ),
                }
            fallback_reason = "intent.md was empty or looked like an Otto-generated intent log"

    if readme_path.exists():
        try:
            readme = readme_path.read_text().strip()
        except (UnicodeDecodeError, IsADirectoryError, PermissionError, OSError) as exc:
            raise ConfigError(f"Failed to read {readme_path}: {exc}") from exc
        if readme:
            source = "README.md"
            resolved_text = validate_text_limit(
                readme,
                kind="intent",
                source=str(readme_path),
                max_chars=MAX_INTENT_CHARS,
            )
    return {
        "source": source,
        "fallback_reason": fallback_reason,
        "resolved_text": resolved_text,
    }


def _looks_like_intent_log(text: str) -> bool:
    stripped = text.lstrip()
    if stripped.startswith("# Build Intents"):
        return True
    return bool(re.search(r"(?m)^## \d{4}-\d{2}-\d{2} \d{2}:\d{2} \([^)]+\)$", text))


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


def git_dir(project_dir: Path) -> Path:
    """Return the worktree-specific git dir (`git rev-parse --git-dir`)."""
    result = _run_git(project_dir, "rev-parse", "--git-dir")
    if result.returncode != 0 or not (result.stdout or "").strip():
        stderr = (result.stderr or "").strip()
        raise ConfigError(
            f"Could not resolve git dir for {project_dir}: {stderr or 'unknown git error'}"
        )
    path = Path((result.stdout or "").strip())
    if not path.is_absolute():
        path = (project_dir / path).resolve()
    return path


def repo_preflight_issues(project_dir: Path) -> dict[str, list[str]]:
    """Return dirty-vs-blocking repo-state issues before build/improve runs."""
    dirty_issues: list[str] = []
    blocking_issues: list[str] = []
    dirty_files: list[str] = []

    worktree = _run_git(project_dir, "diff", "--quiet")
    if worktree.returncode == 1:
        dirty_issues.append("working tree has unstaged changes")
    elif worktree.returncode not in (0, 1):
        stderr = (worktree.stderr or "").strip()
        blocking_issues.append(
            f"`git diff --quiet` failed: {stderr or 'unknown git error'}"
        )

    index = _run_git(project_dir, "diff", "--cached", "--quiet")
    if index.returncode == 1:
        dirty_issues.append("index has staged but uncommitted changes")
    elif index.returncode not in (0, 1):
        stderr = (index.stderr or "").strip()
        blocking_issues.append(
            f"`git diff --cached --quiet` failed: {stderr or 'unknown git error'}"
        )

    unmerged = _run_git(project_dir, "diff", "--name-only", "--diff-filter=U")
    if unmerged.returncode != 0:
        stderr = (unmerged.stderr or "").strip()
        blocking_issues.append(
            f"`git diff --name-only --diff-filter=U` failed: {stderr or 'unknown git error'}"
        )
    else:
        conflicted = [line.strip() for line in unmerged.stdout.splitlines() if line.strip()]
        if conflicted:
            blocking_issues.append(
                f"repository has unmerged paths: {', '.join(conflicted[:5])}"
            )

    try:
        current_git_dir = git_dir(project_dir)
    except ConfigError as exc:
        blocking_issues.append(str(exc))
    else:
        for name, label in _REPO_STATE_MARKERS:
            if (current_git_dir / name).exists():
                blocking_issues.append(f"repository has {label}")

    if dirty_issues:
        try:
            # Only surface paths that actually participate in the dirty-tree
            # refusal. Untracked-only state is tolerated by build/merge
            # preflight, so don't let it dominate the preview list.
            status = _run_git(project_dir, "status", "--porcelain", "--untracked-files=no")
            if status.returncode == 0:
                for line in (status.stdout or "").splitlines():
                    path = line[3:].strip() if len(line) > 3 else line.strip()
                    if " -> " in path:
                        path = path.split(" -> ", 1)[1].strip()
                    if path:
                        dirty_files.append(path)
                    if len(dirty_files) >= 20:
                        break
        except Exception:
            dirty_files = []

    return {"dirty": dirty_issues, "blocking": blocking_issues, "dirty_files": dirty_files}


def ensure_safe_repo_state(project_dir: Path, *, allow_dirty: bool = False) -> None:
    """Refuse dirty/conflicted repos unless the caller explicitly opts in."""
    issues = repo_preflight_issues(project_dir)
    blocking = list(issues["blocking"])
    dirty = [] if allow_dirty else list(issues["dirty"])
    problems = [*blocking, *dirty]
    if not problems:
        return
    dirty_files = list(issues.get("dirty_files", []) or [])
    dirty_suffix = ""
    if dirty_files:
        dirty_suffix = "\nFirst dirty files:\n" + "\n".join(f"- {path}" for path in dirty_files)
    suffix = (
        "Resolve the repo state above before running Otto."
        if allow_dirty and blocking
        else "Re-run with `--allow-dirty` only if you intentionally want Otto to proceed on a dirty tree."
    )
    raise ConfigError(
        "Refusing to run Otto in the current git state:\n"
        + "\n".join(f"- {item}" for item in problems)
        + dirty_suffix
        + f"\n{suffix}"
    )


def _merge_defaults(raw: dict[str, Any]) -> dict[str, Any]:
    """Shallow-merge ``raw`` over DEFAULTS, with one level of nesting for
    ``agents.<name>`` and ``queue`` so partial overrides work correctly
    (e.g. ``queue: {concurrent: 5}`` keeps the other queue defaults).
    """
    merged: dict[str, Any] = {}
    for k, v in DEFAULTS.items():
        if isinstance(v, dict) and k == "agents":
            merged[k] = {name: dict(sub) for name, sub in v.items()}
        elif isinstance(v, dict):
            merged[k] = dict(v)
        else:
            merged[k] = v
    for k, v in raw.items():
        if k == "agents" and isinstance(v, dict):
            for name, sub in v.items():
                if name in merged["agents"] and isinstance(sub, dict):
                    merged["agents"][name].update(sub)
                else:
                    merged["agents"][name] = dict(sub) if isinstance(sub, dict) else sub
        elif k == "queue":
            if isinstance(v, dict):
                merged["queue"] = _normalize_queue_overrides(v, merged["queue"])
            else:
                import logging
                logging.getLogger("otto.config").warning(
                    "Invalid queue config %r, using defaults", v,
                )
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
        try:
            raw = yaml.safe_load(config_path.read_text())
        except yaml.YAMLError as exc:
            raise ConfigError(f"Malformed config at {config_path}: YAML parse error: {exc}") from exc
        if raw is None:
            raw = {}
        if not isinstance(raw, dict):
            raise ConfigError(
                f"Malformed config at {config_path}: expected a YAML mapping at the document root, "
                f"found {type(raw).__name__}."
            )
        config = _merge_defaults(raw)

    # Normalize + validate the global provider string.
    config["provider"] = agent_provider(config)
    try:
        config["certifier_mode"] = resolve_certifier_mode(config)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    # Auto-detect project-specific values when yaml didn't set them.
    if not config.get("test_command"):
        config["test_command"] = detect_test_command(project_dir)
    if not config.get("default_branch"):
        config["default_branch"] = detect_default_branch(project_dir)
    return config


def _normalize_queue_overrides(
    raw_queue: dict[str, Any],
    base: dict[str, Any],
) -> dict[str, Any]:
    """Validate queue overrides against base, warning when rejecting bad values."""
    import logging

    logger = logging.getLogger("otto.config")
    queue = dict(base)
    for key, value in raw_queue.items():
        if key not in base:
            choices = ", ".join(sorted(base))
            raise ConfigError(f"Unknown queue config key: queue.{key}. Expected one of: {choices}")
        if _queue_value_is_valid(key, value):
            if key == "bookkeeping_files":
                queue[key] = list(value)
            elif key == "merge_certifier_mode":
                queue[key] = validate_certifier_mode(
                    str(value),
                    key="queue.merge_certifier_mode",
                    allow_internal=False,
                )
            else:
                queue[key] = value
            continue
        logger.warning(
            "Invalid queue.%s (%r), using default %r",
            key,
            value,
            base[key],
        )
    return queue


def _queue_value_is_valid(key: str, value: Any) -> bool:
    """Return True when a known queue key has the expected type."""
    if key == "concurrent":
        return isinstance(value, int) and not isinstance(value, bool) and value >= 1
    if key == "worktree_dir":
        return isinstance(value, str)
    if key == "on_watcher_restart":
        return isinstance(value, str) and value in {"resume", "fail"}
    if key == "merge_certifier_mode":
        try:
            validate_certifier_mode(str(value), key="queue.merge_certifier_mode", allow_internal=False)
        except ValueError:
            return False
        return True
    if key == "task_timeout_s":
        return (
            value is None
            or (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and float(value) >= 0.0
            )
        )
    if key == "bookkeeping_files":
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return True


def ensure_bookkeeping_setup(project_dir: Path, config: dict[str, Any]) -> None:
    """Install bookkeeping merge drivers when queue bookkeeping is enabled.

    Opt-out is controlled by ``queue.bookkeeping_files: []`` in the provided
    config. Conflicts must propagate; only local filesystem oddities are
    softened so later queue/merge precondition checks can catch them.
    """
    bookkeeping_files = config.get("queue", {}).get("bookkeeping_files", [])
    if not bookkeeping_files:
        return

    try:
        from otto.setup_gitattributes import install as install_gitattributes

        install_gitattributes(project_dir)
    except (FileNotFoundError, PermissionError) as exc:
        import logging

        logging.getLogger("otto.config").warning(
            ".gitattributes setup failed: %s — queue/merge will hard-fail "
            "until resolved.",
            exc,
        )


def first_touch_bookkeeping(project_dir: Path, config: dict[str, Any]) -> None:
    """First-touch helper called from `otto queue build|run` and `otto merge`.

    Idempotent. Performs the minimum needed for the queue/merge precondition
    checks to pass for users who skipped `otto setup`:

    1. Adds otto's runtime artifacts to ``.gitignore`` (queue.yml, otto_logs/,
       .worktrees/, etc.). Without this, the merge orchestrator's
       working_tree_clean check fails on every freshly-queued task.
    2. Installs the ``.gitattributes`` bookkeeping merge drivers (intent.md
       merge=union, otto.yaml merge=ours). Without this, ``otto merge``
       hard-fails its precondition.
    3. Announces and auto-commits any new/changed bookkeeping files in a
       single ``chore(otto): …`` commit so the working tree stays clean.

    Skips silently in non-git directories or if no changes are needed.
    Failures are logged but never raised — better to let the user hit the
    explicit precondition error downstream than to crash here.
    """
    import logging
    import subprocess as _sp
    import time as _time

    log = logging.getLogger("otto.config")

    def _run_git_with_index_retry(args: list[str]) -> _sp.CompletedProcess[str]:
        last_result: _sp.CompletedProcess[str] | None = None
        for attempt in range(4):
            result = _sp.run(
                ["git", *args],
                cwd=project_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode == 0:
                return result
            last_result = result
            stderr = result.stderr or ""
            if "index.lock" not in stderr or attempt == 3:
                break
            _time.sleep(0.15 * (attempt + 1))
        assert last_result is not None
        raise _sp.CalledProcessError(
            last_result.returncode,
            ["git", *args],
            output=last_result.stdout,
            stderr=last_result.stderr,
        )

    # Quick sanity: are we in a git working tree?
    try:
        check = _sp.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=project_dir, capture_output=True, text=True, check=False,
        )
        if check.returncode != 0:
            return
    except FileNotFoundError:
        return

    changed_paths: list[str] = []
    protected_paths = {".gitignore", ".gitattributes"}
    preexisting_dirty_paths: set[str] = set()

    try:
        status = _sp.run(
            ["git", "status", "--porcelain", "--", *sorted(protected_paths)],
            cwd=project_dir, capture_output=True, text=True, check=False,
        )
        for raw in (status.stdout or "").splitlines():
            if not raw:
                continue
            path_text = raw[3:] if len(raw) > 3 else raw
            for managed_path in protected_paths:
                if path_text == managed_path or path_text.endswith(f" {managed_path}"):
                    preexisting_dirty_paths.add(managed_path)
    except OSError as exc:
        log.warning("first-touch bookkeeping dirty check failed: %s", exc)
        return

    # 1. .gitignore
    if ".gitignore" in preexisting_dirty_paths:
        log.warning("skipped first-touch .gitignore setup: file already has user changes")
    else:
        try:
            from otto.setup_gitignore import ensure_gitignore
            if ensure_gitignore(project_dir, auto_commit=False):
                changed_paths.append(".gitignore")
        except Exception as exc:
            log.warning("first-touch .gitignore setup failed: %s", exc)

    # 2. .gitattributes (only if bookkeeping enabled)
    bookkeeping_files = (config.get("queue") or {}).get("bookkeeping_files") or []
    if bookkeeping_files:
        if ".gitattributes" in preexisting_dirty_paths:
            log.warning("skipped first-touch .gitattributes setup: file already has user changes")
        else:
            try:
                from otto.setup_gitattributes import GitAttributesConflict, install as install_gitattributes
                if install_gitattributes(project_dir):
                    changed_paths.append(".gitattributes")
            except GitAttributesConflict as exc:
                # Don't crash — let the downstream precondition check surface
                # the real, actionable error.
                log.warning("first-touch .gitattributes setup hit conflict: %s", exc)
            except (FileNotFoundError, PermissionError) as exc:
                log.warning("first-touch .gitattributes setup failed: %s", exc)

    if not changed_paths:
        return

    # 3. Auto-commit, only if no other staged work would get bundled in.
    try:
        diff_cached = _sp.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=project_dir, capture_output=True, text=True, check=False,
        )
        staged_other = [
            n for n in (diff_cached.stdout or "").splitlines()
            if n and n not in changed_paths
        ]
        if staged_other:
            log.warning(
                "skipped auto-commit of %s: other staged files present (%s); "
                "commit them yourself, then re-run otto",
                changed_paths, staged_other,
            )
            return
        _run_git_with_index_retry(["add", "--", *changed_paths])
        pending = _sp.run(
            ["git", "diff", "--cached", "--name-only", "--", *changed_paths],
            cwd=project_dir, capture_output=True, text=True, check=False,
        )
        if not (pending.stdout or "").strip():
            return
        msg = "chore(otto): set up runtime bookkeeping (" + ", ".join(changed_paths) + ")"
        notice = (
            "Otto is committing runtime bookkeeping files so queue/merge can run "
            f"with a clean tree: {', '.join(changed_paths)}"
        )
        log.warning(notice)
        print(notice, file=sys.stderr)
        _run_git_with_index_retry(["commit", "-q", "-m", msg, "--", *changed_paths])
        log.info("auto-committed %s: %s", changed_paths, msg)
    except _sp.CalledProcessError as exc:
        log.warning(
            "auto-commit of %s failed (rc=%d): %s",
            changed_paths, exc.returncode, (exc.stderr or b"").strip().decode(errors="replace"),
        )


def detect_test_command(project_dir: Path) -> str | None:
    """Auto-detect the project's test command.

    If multiple test frameworks detected (e.g., npm test + pytest for a React
    project with Python e2e tests), chains them with &&.
    Returns None only if no framework detected.
    """
    candidates: list[str] = []
    has_orchestrator = False

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
    if not has_py_tests:
        pytest_ini = project_dir / "pytest.ini"
        setup_cfg = project_dir / "setup.cfg"
        pyproject = project_dir / "pyproject.toml"
        has_py_tests = pytest_ini.exists()
        if not has_py_tests and setup_cfg.exists():
            has_py_tests = "[tool:pytest]" in setup_cfg.read_text()
        if not has_py_tests and pyproject.exists():
            try:
                pyproject_data = pyproject.read_text()
            except OSError:
                pyproject_data = ""
            has_py_tests = "[tool.pytest.ini_options]" in pyproject_data
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
        has_orchestrator = True

    # nox (Python test runner — similar to tox)
    if (project_dir / "noxfile.py").exists():
        candidates = [c for c in candidates if c not in ("pytest",) and not c.endswith("/pytest")]
        candidates.append("nox")
        has_orchestrator = True

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
        makefile_text = makefile.read_text()
        wraps_known_orchestrator = any(
            token in makefile_text
            for token in ("tox", "nox", "pytest", "python -m pytest", "python -m unittest")
        )
        if not wraps_known_orchestrator and not has_orchestrator:
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
            # refs/remotes/origin/main -> main
            # refs/remotes/origin/release/2026 -> release/2026
            remote_head = result.stdout.strip()
            prefix = "refs/remotes/origin/"
            if remote_head.startswith(prefix):
                branch = remote_head[len(prefix):].strip()
                if branch:
                    return branch
            fallback = remote_head.rsplit("/", 1)[-1].strip()
            if fallback:
                return fallback
    except FileNotFoundError:
        pass

    for branch_name in ("main", "master"):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{branch_name}"],
            cwd=project_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return branch_name

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
# max_turns_per_call: {max_turns_per_call}    # guardrail for agent loops

# ─── Per-invocation defaults (CLI typically overrides) ───────────────
# certifier_mode: {certifier_mode}            # fast | standard | thorough
# skip_product_qa: false                      # --no-qa equivalent
# split_mode: true                            # reliable split loop; use --agentic for one-session mode

# ─── Features ────────────────────────────────────────────────────────
# memory: false                       # cross-run certifier memory (opt-in)

# ─── Queue + merge (used by `otto queue` and `otto merge`) ───────────
# queue:
#   concurrent: 3                     # default --concurrent for `otto queue run`
#   worktree_dir: .worktrees          # where per-task worktrees live
#   on_watcher_restart: resume        # resume | fail
#   task_timeout_s: 4200.0            # per-task timeout; 0/null disables
#   merge_certifier_mode: standard    # fast | standard | thorough
#   bookkeeping_files:                # NOT committed to task branches
#     - intent.md
#     - otto.yaml
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
        max_turns_per_call=DEFAULTS["max_turns_per_call"],
        certifier_mode=DEFAULTS["certifier_mode"],
    ))

    # Update .git/info/exclude for runtime files (use git_meta_dir for linked worktrees)
    exclude_path = git_meta_dir(project_dir) / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text() if exclude_path.exists() else ""
    from otto.setup_gitignore import OTTO_PATTERNS

    entries = [*OTTO_PATTERNS, "otto.lock"]
    to_add = [e for e in entries if e not in existing]
    if to_add:
        with open(exclude_path, "a") as f:
            f.write("\n# otto runtime files\n")
            for entry in to_add:
                f.write(f"{entry}\n")

    written_config = load_config(config_path)
    ensure_bookkeeping_setup(project_dir, written_config)

    return config_path


def require_git() -> None:
    """Exit with a friendly error if not in a git repo."""
    import sys
    try:
        resolve_project_dir(Path.cwd())
    except ConfigError as exc:
        from otto.theme import error_console
        message = str(exc)
        if "git is not installed or not on PATH" in message:
            error_console.print("Error: git is not installed or not on PATH.", style="error")
        else:
            error_console.print("Error: not a git repository. Run 'git init' first.", style="error")
        sys.exit(2)


def checkpoint_fingerprint(project_dir: Path) -> dict[str, str]:
    """Capture a lightweight resume fingerprint for the current workspace."""
    try:
        git_sha = _run_git(project_dir, "rev-parse", "HEAD").stdout.strip()
    except ConfigError:
        git_sha = ""

    try:
        git_status = _run_git(
            project_dir,
            "status",
            "--porcelain",
            "--untracked-files=all",
        ).stdout
    except ConfigError:
        git_status = ""

    prompt_dir = Path(__file__).resolve().parent / "prompts"
    digest = hashlib.sha256()
    try:
        for prompt_path in sorted(prompt_dir.glob("*.md")):
            digest.update(prompt_path.name.encode("utf-8"))
            digest.update(b"\0")
            digest.update(prompt_path.read_bytes())
            digest.update(b"\0")
        prompt_hash = digest.hexdigest()
    except OSError:
        prompt_hash = ""

    return {
        "git_sha": git_sha,
        "git_status": git_status,
        "prompt_hash": prompt_hash,
    }
