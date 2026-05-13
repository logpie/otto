"""Unified "verify from clean state" primitive.

Replaces the previous trio of overlapping checks:
    - check_scaffold_compiles (in-place compile, no install)
    - smoke_clean_deploy      (temp copy + start.sh)
    - architect leaf self-verify (in-session npm run build)

All three asked the same question ("does this run cleanly?") with
different state assumptions; the in-place scaffold check could pass for
the architect while failing preflight, because deps weren't installed.

This module exposes ONE primitive: ``verify_from_clean(project_dir,
scope=...)`` that callers parameterize by how far to push the check:

    scaffold  : copy to temp + install deps + run build (no services)
    subtree   : same as scaffold, plus run start.sh + probe declared ports
    full      : alias for subtree at present (reserved for future)

The function ALWAYS copies the project to a temp dir first (excluding
stateful state: ``node_modules``, ``.venv``, ``dist``, ``.git``,
``.worktrees``, ``otto_logs``, ``__pycache__``, etc.). This is the
"clean" in "verify_from_clean" — verifications are meaningless if the
state under test is whatever happens to be in the working directory.

Preflight wrappers (``check_scaffold_compiles``, ``smoke_clean_deploy``)
delegate to this primitive and map results back to their existing
``PreflightIssue`` kinds for backward compatibility.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Scope = Literal["scaffold", "subtree", "full"]

# Failure kinds emitted by the primitive. Preflight wrappers map these
# to their own external kind names for back-compat.
FailureKind = Literal[
    "copy_failed",
    "no_npm",
    "no_python",
    "install_failed",
    "install_timeout",
    "build_failed",
    "build_timeout",
    "py_compile_failed",
    "py_compile_timeout",
    "no_start_sh",
    "port_busy",
    "start_failed",
    "ports_not_listening",
    "internal_error",
]


@dataclass
class CleanVerifyResult:
    """Outcome of one ``verify_from_clean`` call."""

    passed: bool
    scope: Scope
    failure_kind: FailureKind | None = None
    failure_message: str | None = None
    steps_run: list[str] = field(default_factory=list)
    # Optional debug info — preserved temp dir path, listening ports, etc.
    temp_dir: Path | None = None
    listening_ports: list[int] = field(default_factory=list)


# Stateful dirs that should NOT be copied to the clean temp.
_STATEFUL_EXCLUDES = (
    ".venv",
    "node_modules",
    "dist",
    "build",
    ".git",
    ".worktrees",
    "otto_logs",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "target",  # rust build dir
)


def _copy_project_clean(
    project_dir: Path, temp_root: Path
) -> tuple[bool, str | None]:
    """Copy project to temp_root, excluding stateful state. Returns
    (success, error_message)."""
    rsync = shutil.which("rsync")
    if rsync:
        excludes = list(_STATEFUL_EXCLUDES) + ["*.egg-info"]
        cmd = (
            [rsync, "-a"]
            + [f"--exclude={p}" for p in excludes]
            + [f"{project_dir}/", f"{temp_root}/"]
        )
        try:
            subprocess.run(cmd, check=True, timeout=60, capture_output=True)
            return True, None
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            return False, f"rsync to temp dir failed: {exc}"
    # Fallback: shutil.copytree with ignore patterns.
    excluded = set(_STATEFUL_EXCLUDES)

    def _ignore(_d: str, names: list[str]) -> list[str]:
        return [n for n in names if n in excluded or n.endswith(".egg-info")]

    try:
        shutil.copytree(project_dir, temp_root, dirs_exist_ok=True, ignore=_ignore)
        return True, None
    except OSError as exc:
        return False, f"copytree to temp dir failed: {exc}"


def _find_manifests(temp_root: Path) -> tuple[list[Path], list[Path]]:
    """Find package.json and pyproject.toml under temp_root, skipping
    nested stateful dirs we missed in copy."""
    package_jsons = [
        p
        for p in temp_root.rglob("package.json")
        if "node_modules" not in p.parts and ".worktrees" not in p.parts
    ]
    pyprojects = [
        p
        for p in temp_root.rglob("pyproject.toml")
        if ".venv" not in p.parts and ".worktrees" not in p.parts
    ]
    return package_jsons, pyprojects


def _scaffold_verify(
    temp_root: Path, timeout_s: int, log: Any
) -> tuple[bool, FailureKind | None, str | None, list[str]]:
    """Install + build for every manifest under temp_root.

    Returns (passed, failure_kind, message, steps_run).
    """
    steps: list[str] = []
    package_jsons, pyprojects = _find_manifests(temp_root)

    npm_path = shutil.which("npm")
    if package_jsons and not npm_path:
        return False, "no_npm", "npm not on PATH", steps
    npm: str = npm_path or ""  # narrowed below by early-return above

    for pkg in package_jsons:
        manifest_text = ""
        try:
            manifest_text = pkg.read_text(encoding="utf-8")
        except OSError:
            continue
        has_build = '"build"' in manifest_text
        has_lockfile = (pkg.parent / "package-lock.json").exists()
        # Install. Prefer `npm ci` when lockfile present (deterministic);
        # fall back to `npm install` otherwise.
        install_cmd = (
            [npm, "ci", "--no-audit", "--no-fund"]
            if has_lockfile
            else [npm, "install", "--no-audit", "--no-fund"]
        )
        log(f"verify_from_clean: {install_cmd[1]} in {pkg.parent.name}")
        steps.append(f"npm_{install_cmd[1]}:{pkg.parent.name}")
        try:
            proc = subprocess.run(
                install_cmd,
                cwd=pkg.parent,
                capture_output=True,
                text=True,
                timeout=timeout_s * 3,  # install often slower than build
            )
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "")[-400:]
                return (
                    False,
                    "install_failed",
                    f"{install_cmd[1]} failed in {pkg.parent.name}: "
                    f"exit {proc.returncode}. Tail: {tail!r}",
                    steps,
                )
        except subprocess.TimeoutExpired:
            return (
                False,
                "install_timeout",
                f"{install_cmd[1]} in {pkg.parent.name} timed out after {timeout_s*3}s",
                steps,
            )

        if not has_build:
            continue

        # Build.
        log(f"verify_from_clean: npm run build in {pkg.parent.name}")
        steps.append(f"npm_build:{pkg.parent.name}")
        try:
            proc = subprocess.run(
                [npm, "run", "build", "--silent"],
                cwd=pkg.parent,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "")[-400:]
                return (
                    False,
                    "build_failed",
                    f"npm run build failed in {pkg.parent.name}: "
                    f"exit {proc.returncode}. Tail: {tail!r}",
                    steps,
                )
        except subprocess.TimeoutExpired:
            return (
                False,
                "build_timeout",
                f"npm run build in {pkg.parent.name} timed out after {timeout_s}s",
                steps,
            )

    # Python: py_compile every .py file (cheap syntactic check).
    python_path = shutil.which("python3") or shutil.which("python")
    if pyprojects and not python_path:
        return False, "no_python", "python not on PATH", steps
    python: str = python_path or ""  # narrowed by early-return above

    for pyp in pyprojects:
        py_files = [
            p
            for p in pyp.parent.rglob("*.py")
            if ".venv" not in p.parts
            and "__pycache__" not in p.parts
            and ".worktrees" not in p.parts
        ]
        if not py_files:
            continue
        log(f"verify_from_clean: py_compile in {pyp.parent.name} ({len(py_files)} files)")
        steps.append(f"py_compile:{pyp.parent.name}")
        try:
            proc = subprocess.run(
                [python, "-m", "py_compile", *map(str, py_files)],
                cwd=pyp.parent,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
            if proc.returncode != 0:
                tail = (proc.stderr or proc.stdout or "")[-400:]
                return (
                    False,
                    "py_compile_failed",
                    f"py_compile failed in {pyp.parent.name}: "
                    f"exit {proc.returncode}. Tail: {tail!r}",
                    steps,
                )
        except subprocess.TimeoutExpired:
            return (
                False,
                "py_compile_timeout",
                f"py_compile in {pyp.parent.name} timed out after {timeout_s}s",
                steps,
            )

    return True, None, None, steps


def _parse_declared_ports(project_dir: Path) -> list[int]:
    """Best-effort port extraction from CHARTER.md."""
    declared: list[int] = []
    charter = project_dir / "CHARTER.md"
    if not charter.exists():
        return declared
    try:
        text = charter.read_text(encoding="utf-8")
    except OSError:
        return declared
    for m in re.finditer(r"(?:127\.0\.0\.1|localhost):(\d{4,5})", text):
        declared.append(int(m.group(1)))
    for m in re.finditer(r"\b(?:port|Port|PORT)\s*[:=]?\s*(\d{4,5})\b", text):
        declared.append(int(m.group(1)))
    return sorted(set(declared))


def _check_ports_free(declared_ports: list[int]) -> list[int]:
    """Return any declared ports that are already bound (busy)."""
    busy: list[int] = []
    for port in declared_ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            busy.append(port)
        finally:
            s.close()
    return busy


def _subtree_verify_start_sh(
    temp_root: Path,
    declared_ports: list[int],
    timeout_s: int,
    port_wait_s: int,
    log: Any,
) -> tuple[bool, FailureKind | None, str | None, list[str], list[int]]:
    """Run start.sh in temp_root, poll for declared ports. Returns
    (passed, failure_kind, message, steps_run, listening_ports)."""
    steps: list[str] = []
    start_sh = temp_root / "start.sh"
    if not start_sh.exists():
        # Caller decides whether absent start.sh is a failure or skip.
        return True, None, None, steps, []

    bash = shutil.which("bash")
    if not bash:
        return False, "internal_error", "bash not on PATH", steps, []

    steps.append("start_sh")
    log(f"verify_from_clean: running start.sh in {temp_root}")
    proc = None
    listening: set[int] = set()
    try:
        proc = subprocess.Popen(  # noqa: S603 — our own script
            [bash, "start.sh"],
            cwd=temp_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.time() + timeout_s + port_wait_s
        start_exited_early = False
        while time.time() < deadline:
            ret = proc.poll()
            if ret is not None and ret != 0 and not start_exited_early:
                start_exited_early = True
                try:
                    out = (
                        proc.stdout.read(2000).decode("utf-8", errors="replace")
                        if proc.stdout
                        else ""
                    )
                except Exception:  # noqa: BLE001
                    out = ""
                return (
                    False,
                    "start_failed",
                    f"start.sh exited {ret} during clean-state deploy. "
                    f"Last output (truncated): {out[-800:]!r}",
                    steps,
                    sorted(listening),
                )
            for port in declared_ports:
                if port in listening:
                    continue
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(0.3)
                try:
                    s.connect(("127.0.0.1", port))
                    listening.add(port)
                    log(f"verify_from_clean: port {port} listening")
                except OSError:
                    pass
                finally:
                    s.close()
            if declared_ports and listening == set(declared_ports):
                log("verify_from_clean: all declared ports listening")
                break
            time.sleep(1.5)

        steps.append("port_probe")
        if declared_ports:
            missing = [p for p in declared_ports if p not in listening]
            if missing:
                return (
                    False,
                    "ports_not_listening",
                    f"After clean-state deploy, ports {missing} did not bind "
                    f"within {timeout_s + port_wait_s}s. Listening: "
                    f"{sorted(listening) or 'none'}.",
                    steps,
                    sorted(listening),
                )
        return True, None, None, steps, sorted(listening)
    finally:
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        # Kill anything bound to declared ports we tested.
        for port in declared_ports:
            try:
                out = subprocess.check_output(
                    ["lsof", "-ti", f":{port}"], text=True, timeout=2
                )
                for pid in out.strip().split("\n"):
                    if pid.strip().isdigit():
                        subprocess.run(
                            ["kill", "-9", pid.strip()], timeout=2, check=False
                        )
            except (
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                FileNotFoundError,
            ):
                pass


def verify_from_clean(
    project_dir: Path,
    *,
    scope: Scope = "subtree",
    timeout_s: int = 120,
    port_wait_s: int = 12,
    preserve_temp: bool = False,
    logger_fn: Any = None,
) -> CleanVerifyResult:
    """Verify the project compiles/installs/runs from clean state.

    Steps:
      1. Copy project to a temp dir, excluding stateful dirs.
      2. For each manifest: install deps via lockfile when present,
         then run build (if a build script is declared).
      3. If ``scope`` >= ``"subtree"`` and ``start.sh`` exists: run it
         in the temp dir and probe declared ports.
      4. Clean up the temp dir (unless ``preserve_temp=True``).

    Returns a ``CleanVerifyResult`` whose ``failure_kind`` is one of
    the values in ``FailureKind`` when ``passed=False``.

    Parameters:
        project_dir: project root to verify.
        scope: ``"scaffold"`` for compile-only, ``"subtree"`` to also
            run start.sh, ``"full"`` (currently an alias for subtree).
        timeout_s: per-step timeout. Install gets 3x this.
        port_wait_s: additional grace after install for ports to bind
            in subtree scope.
        preserve_temp: keep the temp dir on return (for debug).
        logger_fn: callable taking one ``str`` argument for progress logs.

    """

    def log(msg: str) -> None:
        if logger_fn:
            logger_fn(msg)

    declared_ports: list[int] = []

    # Pre-check ports for subtree scope so we don't try to run start.sh
    # if its ports are already busy (which would give us a misleading
    # "still listening" pass).
    if scope in ("subtree", "full"):
        declared_ports = _parse_declared_ports(project_dir)
        busy = _check_ports_free(declared_ports)
        if busy:
            return CleanVerifyResult(
                passed=False,
                scope=scope,
                failure_kind="port_busy",
                failure_message=(
                    f"Declared ports {busy} already bound (likely zombies "
                    f"from prior runs). Cannot run clean-deploy."
                ),
            )

    temp_root = Path(tempfile.mkdtemp(prefix="otto-clean-"))
    try:
        copied, copy_err = _copy_project_clean(project_dir, temp_root)
        if not copied:
            return CleanVerifyResult(
                passed=False,
                scope=scope,
                failure_kind="copy_failed",
                failure_message=copy_err,
            )

        # Scaffold-level verification: install + build everywhere.
        passed, kind, message, steps_a = _scaffold_verify(
            temp_root, timeout_s=timeout_s, log=log
        )
        if not passed:
            return CleanVerifyResult(
                passed=False,
                scope=scope,
                failure_kind=kind,
                failure_message=message,
                steps_run=steps_a,
                temp_dir=temp_root if preserve_temp else None,
            )

        # Subtree/full scope: run start.sh too.
        if scope in ("subtree", "full"):
            passed, kind, message, steps_b, listening = _subtree_verify_start_sh(
                temp_root,
                declared_ports=declared_ports,
                timeout_s=timeout_s,
                port_wait_s=port_wait_s,
                log=log,
            )
            if not passed:
                return CleanVerifyResult(
                    passed=False,
                    scope=scope,
                    failure_kind=kind,
                    failure_message=message,
                    steps_run=steps_a + steps_b,
                    temp_dir=temp_root if preserve_temp else None,
                    listening_ports=listening,
                )
            return CleanVerifyResult(
                passed=True,
                scope=scope,
                steps_run=steps_a + steps_b,
                temp_dir=temp_root if preserve_temp else None,
                listening_ports=listening,
            )

        # Scaffold scope: stop here.
        return CleanVerifyResult(
            passed=True,
            scope=scope,
            steps_run=steps_a,
            temp_dir=temp_root if preserve_temp else None,
        )
    except Exception as exc:  # noqa: BLE001 — last-resort
        return CleanVerifyResult(
            passed=False,
            scope=scope,
            failure_kind="internal_error",
            failure_message=f"verify_from_clean raised: {exc}",
            temp_dir=temp_root if preserve_temp else None,
        )
    finally:
        if not preserve_temp:
            shutil.rmtree(temp_root, ignore_errors=True)
            log("verify_from_clean: temp dir cleaned up")
