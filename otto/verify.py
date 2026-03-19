"""Otto verification — tiered verification in disposable worktree."""

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
    return env


@dataclass
class TierResult:
    tier: str
    passed: bool
    output: str = ""
    skipped: bool = False


@dataclass
class VerifyResult:
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
        env=_subprocess_env(),
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


def run_tier1(workdir: Path, test_command: str | None, timeout: int) -> TierResult:
    """Run existing test suite."""
    if not test_command:
        return TierResult(tier="existing_tests", passed=True, skipped=True)
    try:
        result = _run_shell_command(test_command, workdir, timeout)
        return TierResult(
            tier="existing_tests",
            passed=result.returncode == 0,
            output=result.stdout + result.stderr,
        )
    except subprocess.TimeoutExpired:
        return TierResult(
            tier="existing_tests",
            passed=False,
            output=f"Timeout after {timeout}s",
        )


def run_tier2(
    workdir: Path,
    testgen_file: Path | None,
    test_command: str | None,
    timeout: int,
) -> TierResult:
    """Run generated integration tests in the worktree.

    The test file is already in the worktree at its final path (baked into the
    candidate commit by build_candidate_commit). We just need to find and run it.
    The testgen_file path is used only to derive the filename for discovery.
    Uses test_command (the configured command) to run the test file where possible.
    """
    if not testgen_file:
        return TierResult(tier="generated_tests", passed=True, skipped=True)

    # Find the test file in the worktree (it was staged into the candidate commit)
    from otto.testgen import test_file_path, detect_test_framework
    framework = detect_test_framework(workdir) or "pytest"
    # Derive the repo-relative path from the filename
    fname = testgen_file.name
    rel_path = None
    for fw in ("pytest", "jest", "vitest", "mocha", "go", "cargo"):
        candidate = test_file_path(fw, "PLACEHOLDER")
        if candidate.suffix == Path(fname).suffix:
            rel_path = candidate.parent / fname
            break
    if rel_path is None:
        rel_path = Path("tests") / fname

    dest = workdir / rel_path
    if not dest.exists():
        # testgen was requested but file is missing from candidate commit — fail, not skip
        return TierResult(tier="generated_tests", passed=False,
                          output=f"Generated test file not found at {rel_path} in candidate commit")

    try:
        # Build test command: append the generated test file to the configured command
        # when possible, so we preserve wrappers/options (uv run pytest, python -m pytest, etc.)
        if framework in ("pytest",):
            if test_command and "pytest" in test_command:
                cmd = f"{test_command} {rel_path}"
            else:
                cmd = f"pytest {rel_path} -v"
        elif framework == "go":
            cmd = f"go test ./{rel_path.parent}/..."
        elif framework == "cargo":
            cmd = f"cargo test"
        elif framework in ("jest", "vitest", "mocha"):
            if test_command:
                cmd = f"{test_command} -- {rel_path}"
            else:
                # Use the detected runner directly (npx jest, npx vitest, npx mocha)
                cmd = f"npx {framework} {rel_path}"
        else:
            if test_command:
                cmd = f"{test_command} {rel_path}"
            else:
                cmd = f"pytest {rel_path} -v"
        result = _run_shell_command(cmd, workdir, timeout)
        return TierResult(
            tier="generated_tests",
            passed=result.returncode == 0,
            output=result.stdout + result.stderr,
        )
    except subprocess.TimeoutExpired:
        return TierResult(
            tier="generated_tests",
            passed=False,
            output=f"Timeout after {timeout}s",
        )


def run_tier3(workdir: Path, verify_cmd: str | None, timeout: int) -> TierResult:
    """Run custom verify command."""
    if not verify_cmd:
        return TierResult(tier="custom_verify", passed=True, skipped=True)
    try:
        result = _run_shell_command(
            verify_cmd,
            workdir,
            timeout,
            executable="/bin/bash",
        )
        return TierResult(
            tier="custom_verify",
            passed=result.returncode == 0,
            output=result.stdout + result.stderr,
        )
    except subprocess.TimeoutExpired:
        return TierResult(
            tier="custom_verify",
            passed=False,
            output=f"Timeout after {timeout}s",
        )


def run_verification(
    project_dir: Path,
    candidate_sha: str,
    test_command: str | None,
    verify_cmd: str | None,
    timeout: int,
    exclude_test_files: list[Path] | None = None,
) -> VerifyResult:
    """Run all verification tiers in a disposable worktree.

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

        # Run all tests (existing + spec-generated) in one pass
        t1 = run_tier1(worktree_path, test_command, timeout)
        tiers.append(t1)
        if not t1.passed and not t1.skipped:
            return VerifyResult(passed=False, tiers=tiers)

        # Custom verify command (if provided)
        t3 = run_tier3(worktree_path, verify_cmd, timeout)
        tiers.append(t3)

        all_passed = all(t.passed for t in tiers)
        return VerifyResult(passed=all_passed, tiers=tiers)

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
) -> VerifyResult:
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

        # Run full test suite (regression + integration tests in one pass)
        t1 = run_tier1(worktree_path, test_command, timeout)
        tiers.append(t1)

        all_passed = all(t.passed for t in tiers)
        return VerifyResult(passed=all_passed, tiers=tiers)

    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree_path)],
            cwd=project_dir, capture_output=True,
        )
        if worktree_path.exists():
            shutil.rmtree(worktree_path, ignore_errors=True)
