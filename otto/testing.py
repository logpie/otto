"""Otto testing — subprocess environment helpers."""

import os
import sys
from pathlib import Path

_BASE_ENV_KEYS = {
    "HOME",
    "PATH",
    "TERM",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "SHELL",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "VIRTUAL_ENV",
    "PYTHONPATH",
}
_AGENT_ENV_PREFIXES = (
    "ANTHROPIC_",
    "CLAUDE_CODE_",
    "CODEX_",
    "OPENAI_",
    "AZURE_OPENAI_",
)
_AGENT_ENV_KEYS = {
    "GIT_ASKPASS",
    "SSH_AUTH_SOCK",
}
_PROJECT_RUNTIME_ENV_KEYS = {
    "APP_ENV",
    "BROKER_URL",
    "CELERY_BROKER_URL",
    "DATABASE_URL",
    "DATABASE_URL_REPLICA",
    "DJANGO_SETTINGS_MODULE",
    "FLASK_APP",
    "FLASK_ENV",
    "HOST",
    "NODE_ENV",
    "PGDATABASE",
    "PGHOST",
    "PGPASSWORD",
    "PGPORT",
    "PGUSER",
    "PORT",
    "PYTEST_DB_URL",
    "REDIS_URL",
}
_PROJECT_RUNTIME_ENV_SUFFIXES = (
    "_API_URL",
    "_BASE_URL",
    "_BROKER_URL",
    "_DATABASE_URL",
    "_DATABASE_URI",
    "_DB_URL",
    "_REDIS_URL",
)


def _current_runtime_bins() -> set[str]:
    bins = {str(Path(sys.executable).parent)}
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        bins.add(str(Path(virtual_env) / "bin"))
    return bins


def _clean_path(value: str) -> str:
    skip = _current_runtime_bins()
    entries = [entry for entry in value.split(os.pathsep) if entry and entry not in skip]
    return os.pathsep.join(entries)


def _allowed_parent_env() -> dict[str, str]:
    """Return the subset of the parent env that child agents are allowed to inherit."""
    allowed: dict[str, str] = {}
    for key, value in os.environ.items():
        if key in _BASE_ENV_KEYS or key in _AGENT_ENV_KEYS:
            allowed[key] = value
            continue
        if any(key.startswith(prefix) for prefix in _AGENT_ENV_PREFIXES):
            allowed[key] = value
            continue
        if key in _PROJECT_RUNTIME_ENV_KEYS or any(
            key.endswith(suffix) for suffix in _PROJECT_RUNTIME_ENV_SUFFIXES
        ):
            # Provider agents need the same project runtime handles as the
            # deterministic checks they are debugging. Keep this to explicit,
            # common app/test env names instead of passing the whole shell env.
            allowed[key] = value
    return allowed


def _subprocess_env(project_dir: Path | None = None) -> dict:
    """Return an env dict with Python/tooling paths tuned for the target project.

    Child agents operate inside target worktrees. They should not inherit
    Otto's own virtualenv as their default ``python`` just because Otto itself
    was launched from that environment.
    """
    env = _allowed_parent_env()
    env["PATH"] = _clean_path(env.get("PATH", ""))
    env.pop("VIRTUAL_ENV", None)
    # Prevent git from hanging on prompts in unattended mode
    env["GIT_TERMINAL_PROMPT"] = "0"
    # CI=true disables interactive test runners (CRA/Jest watch mode)
    # and enables deterministic output in many frameworks
    env["CI"] = "true"
    # Allow Agent SDK to spawn Claude inside a Claude Code session (e.g. otto
    # invoked from Claude Code).  Without this, the nested session is rejected.
    # Agent SDK merges os.environ with user env, so we must explicitly unset it
    # (pop alone doesn't help since os.environ is read separately by the SDK).
    env.pop("CLAUDECODE", None)
    env["CLAUDECODE"] = ""
    if project_dir:
        src_dir = project_dir / "src"
        if src_dir.is_dir():
            existing = env.get("PYTHONPATH", "")
            parts = [str(src_dir)]
            if existing:
                parts.append(existing)
            env["PYTHONPATH"] = os.pathsep.join(parts)
        project_venv_bin = project_dir / ".venv" / "bin"
        if project_venv_bin.is_dir():
            existing = env.get("PATH", "")
            if str(project_venv_bin) not in existing.split(os.pathsep):
                env["PATH"] = str(project_venv_bin) + os.pathsep + existing
            env["VIRTUAL_ENV"] = str(project_venv_bin.parent)
    return env
