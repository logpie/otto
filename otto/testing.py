"""Otto testing — run test suites in disposable worktrees."""

import os
import signal
import subprocess
import sys
from dataclasses import dataclass, field
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


def _verification_env(venv_bin: str | None = None) -> dict:
    """Return the subprocess env, optionally preferring a worktree venv."""
    env = _subprocess_env()
    if venv_bin:
        existing = env.get("PATH", "")
        if venv_bin not in existing.split(os.pathsep):
            env["PATH"] = venv_bin + os.pathsep + existing
    return env


def _install_deps(worktree_path: Path, timeout: int) -> str | None:
    """Auto-detect and install project dependencies in the test worktree.

    Creates an isolated venv inside the worktree for Python projects to prevent
    contaminating otto's own venv (e.g., a test project named 'python-dotenv'
    would overwrite otto's dependency if installed into otto's venv).

    Best-effort — failures are logged but don't block verification.
    """
    env = _subprocess_env()
    venv_bin: str | None = None

    # Python: create an isolated venv in the worktree for project deps.
    # This prevents `pip install -e .` from contaminating otto's venv.
    has_python_project = (
        (worktree_path / "requirements.txt").exists()
        or (worktree_path / "pyproject.toml").exists()
        or (worktree_path / "setup.py").exists()
    )
    if has_python_project:
        venv_path = worktree_path / ".venv"
        if not venv_path.exists():
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv_path)],
                cwd=worktree_path, capture_output=True, timeout=timeout,
            )
        venv_python_path = venv_path / "bin" / "python"
        if not venv_python_path.exists():
            # Venv creation failed — skip Python dep installation entirely
            # rather than falling back to sys.executable which would
            # contaminate otto's own venv.
            pass
        else:
            venv_bin = str(venv_python_path.parent)
            env = _verification_env(venv_bin)
            venv_python = str(venv_python_path)

            # Install project deps into the isolated venv
            if (worktree_path / "requirements.txt").exists():
                subprocess.run(
                    [venv_python, "-m", "pip", "install", "-q",
                     "-r", str(worktree_path / "requirements.txt")],
                    cwd=worktree_path, capture_output=True, timeout=timeout,
                    env=env,
                )

            if (worktree_path / "pyproject.toml").exists():
                subprocess.run(
                    [venv_python, "-m", "pip", "install", "-q", "-e", "."],
                    cwd=worktree_path, capture_output=True, timeout=timeout,
                    env=env,
                )
                subprocess.run(
                    [venv_python, "-m", "pip", "install", "-q", "-e", ".[dev,test]"],
                    cwd=worktree_path, capture_output=True, timeout=timeout,
                    env=env,
                )
            elif (worktree_path / "setup.py").exists():
                subprocess.run(
                    [venv_python, "-m", "pip", "install", "-q", "-e", "."],
                    cwd=worktree_path, capture_output=True, timeout=timeout,
                    env=env,
                )
                subprocess.run(
                    [venv_python, "-m", "pip", "install", "-q", "-e", ".[dev,test]"],
                    cwd=worktree_path, capture_output=True, timeout=timeout,
                    env=env,
                )

            # Also install pytest into the project venv
            subprocess.run(
                [venv_python, "-m", "pip", "install", "-q", "pytest"],
                cwd=worktree_path, capture_output=True, timeout=timeout,
                env=env,
            )

    # Node.js: package.json with node_modules missing
    if (worktree_path / "package.json").exists() and not (worktree_path / "node_modules").exists():
        # Symlink from main repo's node_modules if available (avoids full reinstall per worktree)
        main_nm = worktree_path.parent.parent / "node_modules"
        if main_nm.is_dir():
            (worktree_path / "node_modules").symlink_to(main_nm)
        else:
            # Detect package manager from lockfiles
            if (worktree_path / "pnpm-lock.yaml").exists():
                install_cmd = ["pnpm", "install", "--frozen-lockfile"]
            elif (worktree_path / "yarn.lock").exists():
                install_cmd = ["yarn", "install", "--frozen-lockfile"]
            elif (worktree_path / "bun.lockb").exists():
                install_cmd = ["bun", "install", "--frozen-lockfile"]
            else:
                # package-lock.json or no lockfile — npm is the safe default
                install_cmd = ["npm", "install", "--no-audit", "--no-fund"]
            subprocess.run(
                install_cmd,
                cwd=worktree_path, capture_output=True, timeout=timeout,
                env=env,
            )

    # Poetry: poetry.lock (Python, but uses its own installer instead of pip)
    if (worktree_path / "poetry.lock").exists():
        subprocess.run(
            ["poetry", "install", "--no-interaction"],
            cwd=worktree_path, capture_output=True, timeout=timeout,
            env=env,
        )

    # uv: uv.lock (Python, fast installer)
    if (worktree_path / "uv.lock").exists():
        subprocess.run(
            ["uv", "sync"],
            cwd=worktree_path, capture_output=True, timeout=timeout,
            env=env,
        )

    return venv_bin


@dataclass
class TierResult:
    tier: str
    passed: bool
    output: str = ""
    skipped: bool = False


@dataclass
class TestSuiteResult:
    __test__ = False  # prevent pytest from trying to collect this dataclass

    passed: bool
    tiers: list[TierResult] = field(default_factory=list)

    @property
    def failure_output(self) -> str:
        """Combined output from failed tiers, for feeding back to the agent."""
        parts = []
        for t in self.tiers:
            if not t.passed and not t.skipped:
                parts.append(f"=== {t.tier} FAILED ===\n{t.output}")
        return "\n\n".join(parts)


def _terminate_process_group(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Terminate a timed-out subprocess group and collect any remaining output."""
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        return proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return proc.communicate()


def _run_shell_command(
    command: str,
    workdir: Path,
    timeout: int,
    executable: str | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a shell command and kill its process group on timeout."""
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        executable=executable,
        start_new_session=True,
        env=env or _subprocess_env(),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        stdout, stderr = _terminate_process_group(proc)
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=(exc.output or "") + stdout,
            stderr=(exc.stderr or "") + stderr,
        ) from None
    return subprocess.CompletedProcess(
        args=command,
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
    )


def run_local_tests(
    workdir: Path,
    test_command: str | None,
    timeout: int,
    env: dict | None = None,
) -> TierResult:
    """Run existing test suite."""
    if not test_command:
        return TierResult(tier="existing_tests", passed=True, skipped=True)
    try:
        result = _run_shell_command(test_command, workdir, timeout, env=env)
        # Exit code 5 (pytest "no tests collected") or exit code 1 with
        # "No tests found" (jest) — treat as passed for greenfield projects
        output = result.stdout + result.stderr
        no_tests_found = result.returncode == 5 or (
            result.returncode == 1 and "No tests found" in output
        )
        passed = result.returncode == 0 or no_tests_found
        return TierResult(
            tier="existing_tests",
            passed=passed,
            output=result.stdout + result.stderr,
        )
    except subprocess.TimeoutExpired:
        return TierResult(
            tier="existing_tests",
            passed=False,
            output=f"Timeout after {timeout}s running: {test_command}",
        )



def run_tier3(
    workdir: Path,
    custom_test_cmd: str | None,
    timeout: int,
    env: dict | None = None,
) -> TierResult:
    """Run custom test command."""
    if not custom_test_cmd:
        return TierResult(tier="custom_test", passed=True, skipped=True)
    try:
        result = _run_shell_command(
            custom_test_cmd,
            workdir,
            timeout,
            executable="/bin/bash",
            env=env,
        )
        return TierResult(
            tier="custom_test",
            passed=result.returncode == 0,
            output=result.stdout + result.stderr,
        )
    except subprocess.TimeoutExpired:
        return TierResult(
            tier="custom_test",
            passed=False,
            output=f"Timeout after {timeout}s",
        )


