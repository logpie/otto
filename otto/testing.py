"""Otto testing — run test suites in disposable worktrees."""

import os
import signal
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


def _subprocess_env() -> dict:
    """Return an env dict with the current Python's bin dir prepended to PATH.

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
        subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund"],
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
            output=f"Timeout after {timeout}s",
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


def run_test_suite(
    project_dir: Path,
    candidate_sha: str,
    test_command: str | None,
    custom_test_cmd: str | None,
    timeout: int,
    exclude_test_files: list[Path] | None = None,
) -> TestSuiteResult:
    """Run all test tiers in a disposable worktree.

    exclude_test_files: paths (relative to project root) to delete from
    the disposable worktree before running tests. Used in parallel mode
    to exclude sibling tasks' test files that can't pass yet.
    """
    tiers: list[TierResult] = []
    worktree_path = Path(tempfile.mkdtemp(prefix="otto-verify-"))

    try:
        # Create disposable worktree with detached HEAD
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_path), candidate_sha],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )

        # Remove sibling test files from disposable worktree (parallel mode)
        if exclude_test_files:
            for rel_path in exclude_test_files:
                excl = worktree_path / rel_path
                if excl.exists():
                    excl.unlink()

        # Install project dependencies in the disposable worktree.
        # Without this, projects using third-party libs fail testing.
        venv_bin = _install_deps(worktree_path, timeout)
        env = _verification_env(venv_bin)

        # Run all tests (existing + spec-generated) in one pass
        t1 = run_local_tests(worktree_path, test_command, timeout, env=env)
        tiers.append(t1)
        if not t1.passed and not t1.skipped:
            return TestSuiteResult(passed=False, tiers=tiers)

        # Custom test command (if provided)
        t3 = run_tier3(worktree_path, custom_test_cmd, timeout, env=env)
        tiers.append(t3)

        all_passed = all(t.passed for t in tiers)
        return TestSuiteResult(passed=all_passed, tiers=tiers)

    finally:
        # Always clean up the worktree
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=project_dir,
            capture_output=True,
        )
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)


def run_integration_gate(
    project_dir: Path,
    test_command: str | None,
    integration_test_file: Path | None,
    timeout: int,
) -> TestSuiteResult:
    """Run integration gate in a clean disposable worktree.

    Tests HEAD of the current branch (all tasks already merged).
    Runs the full test suite (cross-task regression) plus integration tests.
    """
    tiers: list[TierResult] = []

    head_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project_dir, capture_output=True, text=True, check=True,
    ).stdout.strip()

    worktree_path = Path(tempfile.mkdtemp(prefix="otto-integration-"))

    try:
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_path), head_sha],
            cwd=project_dir, capture_output=True, check=True,
        )

        # Copy integration test file into worktree
        if integration_test_file and integration_test_file.exists():
            dest = worktree_path / "tests" / "otto_integration.py"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(integration_test_file, dest)

        venv_bin = _install_deps(worktree_path, timeout)
        env = _verification_env(venv_bin)

        # Run full test suite (regression + integration tests in one pass)
        t1 = run_local_tests(worktree_path, test_command, timeout, env=env)
        tiers.append(t1)

        all_passed = all(t.passed for t in tiers)
        return TestSuiteResult(passed=all_passed, tiers=tiers)

    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=project_dir, capture_output=True,
        )
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
