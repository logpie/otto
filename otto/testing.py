"""Otto testing — subprocess environment helpers."""

import os
import sys
from pathlib import Path


def _subprocess_env(project_dir: Path | None = None) -> dict:
    """Return an env dict with Python/tooling paths tuned for the target project.

    This ensures that when otto invokes ``pytest`` (or other venv tools) via
    shell=True, the subprocess can find them even if the caller's shell did not
    activate the virtualenv.
    """
    venv_bin = str(Path(sys.executable).parent)
    env = os.environ.copy()
    existing = env.get("PATH", "")
    if venv_bin not in existing.split(os.pathsep):
        env["PATH"] = venv_bin + os.pathsep + existing
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
    return env


