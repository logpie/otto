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
from dataclasses import asdict, dataclass, field
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
    "script_valid_failed",
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


@dataclass
class ToolchainCommandResult:
    command: list[str]
    cwd: str
    started_at: str
    duration_s: float
    returncode: int | None
    status: Literal["passed", "failed", "skipped"]
    reason: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass
class ToolchainPreflightResult:
    passed: bool
    worktree: str
    _written_at: str
    commands: list[ToolchainCommandResult] = field(default_factory=list)
    failure_messages: list[str] = field(default_factory=list)
    manifest_counts: dict[str, int] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["commands"] = [asdict(command) for command in self.commands]
        return payload


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
    def _rel_parts(path: Path) -> tuple[str, ...]:
        try:
            return path.relative_to(temp_root).parts
        except ValueError:
            return path.parts

    package_jsons = []
    for p in temp_root.rglob("package.json"):
        rel_parts = _rel_parts(p)
        if "node_modules" in rel_parts or ".worktrees" in rel_parts:
            continue
        package_jsons.append(p)
    pyprojects = []
    for p in temp_root.rglob("pyproject.toml"):
        rel_parts = _rel_parts(p)
        if ".venv" in rel_parts or ".worktrees" in rel_parts:
            continue
        pyprojects.append(p)
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


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def preflight_shared_toolchains(
    worktree_dir: Path,
    *,
    timeout_s: int = 300,
    logger_fn: Any = None,
) -> ToolchainPreflightResult:
    """Install shared toolchains once in the architect worktree.

    This is an optimization and observability layer for v5 children. It runs
    after the architect scaffold passes clean verification so ignored install
    dirs exist in the architect worktree and can be symlink-propagated into
    child worktrees.
    """

    def log(message: str) -> None:
        if logger_fn:
            logger_fn(message)

    package_jsons, pyprojects = _find_manifests(worktree_dir)
    result = ToolchainPreflightResult(
        passed=True,
        worktree=str(worktree_dir),
        _written_at=_iso_now(),
        manifest_counts={
            "package_json": len(package_jsons),
            "pyproject": len(pyprojects),
        },
    )

    npm_path = shutil.which("npm")
    for package_json in package_jsons:
        has_lockfile = (package_json.parent / "package-lock.json").exists()
        cmd = (
            [npm_path or "npm", "ci", "--no-audit", "--no-fund"]
            if has_lockfile
            else [npm_path or "npm", "install", "--no-audit", "--no-fund"]
        )
        if not npm_path:
            _record_toolchain_skip(
                result,
                command=cmd,
                cwd=package_json.parent,
                reason="npm not on PATH",
            )
            continue
        _run_toolchain_command(result, cmd, cwd=package_json.parent, timeout_s=timeout_s, log=log)

    uv_path = shutil.which("uv")
    for pyproject in pyprojects:
        cmd = [uv_path or "uv", "sync"]
        if not uv_path:
            _record_toolchain_skip(
                result,
                command=cmd,
                cwd=pyproject.parent,
                reason="uv not on PATH",
            )
            continue
        _run_toolchain_command(result, cmd, cwd=pyproject.parent, timeout_s=timeout_s, log=log)

    playwright_cwd = _playwright_install_cwd(package_jsons)
    if playwright_cwd is not None:
        npx_path = shutil.which("npx")
        cmd = [npx_path or "npx", "playwright", "install", "chromium"]
        if _playwright_chromium_cached():
            _record_toolchain_skip(
                result,
                command=cmd,
                cwd=playwright_cwd,
                reason="chromium already cached",
                failed=False,
            )
        elif not npx_path:
            _record_toolchain_skip(
                result,
                command=cmd,
                cwd=playwright_cwd,
                reason="npx not on PATH",
            )
        else:
            _run_toolchain_command(result, cmd, cwd=playwright_cwd, timeout_s=timeout_s, log=log)

    result.passed = not result.failure_messages
    return result


def _run_toolchain_command(
    result: ToolchainPreflightResult,
    command: list[str],
    *,
    cwd: Path,
    timeout_s: int,
    log: Any,
) -> None:
    started_at = _iso_now()
    started = time.monotonic()
    log(f"toolchain-preflight: running {' '.join(command)} in {cwd}")
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        duration_s = time.monotonic() - started
        status: Literal["passed", "failed"] = "passed" if proc.returncode == 0 else "failed"
        result.commands.append(ToolchainCommandResult(
            command=command,
            cwd=str(cwd),
            started_at=started_at,
            duration_s=round(duration_s, 3),
            returncode=proc.returncode,
            status=status,
            stdout_tail=(proc.stdout or "")[-800:],
            stderr_tail=(proc.stderr or "")[-800:],
        ))
        if proc.returncode != 0:
            result.failure_messages.append(
                f"{' '.join(command)} failed in {cwd}: exit {proc.returncode}"
            )
    except subprocess.TimeoutExpired as exc:
        duration_s = time.monotonic() - started
        result.commands.append(ToolchainCommandResult(
            command=command,
            cwd=str(cwd),
            started_at=started_at,
            duration_s=round(duration_s, 3),
            returncode=None,
            status="failed",
            reason=f"timed out after {timeout_s}s",
            stdout_tail=(exc.stdout or "")[-800:] if isinstance(exc.stdout, str) else "",
            stderr_tail=(exc.stderr or "")[-800:] if isinstance(exc.stderr, str) else "",
        ))
        result.failure_messages.append(
            f"{' '.join(command)} timed out in {cwd} after {timeout_s}s"
        )


def _record_toolchain_skip(
    result: ToolchainPreflightResult,
    *,
    command: list[str],
    cwd: Path,
    reason: str,
    failed: bool = True,
) -> None:
    result.commands.append(ToolchainCommandResult(
        command=command,
        cwd=str(cwd),
        started_at=_iso_now(),
        duration_s=0.0,
        returncode=None,
        status="skipped",
        reason=reason,
    ))
    if failed:
        result.failure_messages.append(f"{' '.join(command)} skipped in {cwd}: {reason}")


def _playwright_install_cwd(package_jsons: list[Path]) -> Path | None:
    for package_json in package_jsons:
        try:
            text = package_json.read_text(encoding="utf-8")
        except OSError:
            continue
        if "@playwright/test" in text:
            return package_json.parent
    return None


def _playwright_chromium_cached() -> bool:
    cache_root = Path(os.path.expanduser("~/.cache/ms-playwright"))
    if not cache_root.exists():
        return False
    try:
        return any(entry.name.startswith("chromium-") and entry.is_dir() for entry in cache_root.iterdir())
    except OSError:
        return False


def _parse_declared_port_envs(project_dir: Path) -> list[tuple[str | None, int]]:
    """Best-effort port extraction with optional env var names."""
    declared: list[tuple[str | None, int]] = []
    charter = project_dir / "CHARTER.md"
    if charter.exists():
        try:
            text = charter.read_text(encoding="utf-8")
        except OSError:
            text = ""
        for m in re.finditer(r"(?:127\.0\.0\.1|localhost):(\d{4,5})", text):
            declared.append((None, int(m.group(1))))
        for m in re.finditer(r"\b(?:port|Port|PORT)\s*[:=]?\s*(\d{4,5})\b", text):
            declared.append((None, int(m.group(1))))
        for line in text.splitlines():
            if "|" not in line:
                continue
            env_match = re.search(r"`?([A-Z][A-Z0-9_]*PORT)`?", line)
            port_match = re.search(r"`?(\d{4,5})`?", line)
            if env_match and port_match:
                declared.append((env_match.group(1), int(port_match.group(1))))

    start_sh = project_dir / "start.sh"
    if start_sh.exists():
        try:
            script = start_sh.read_text(encoding="utf-8")
        except OSError:
            script = ""
        for m in re.finditer(
            r"\b([A-Z][A-Z0-9_]*PORT)\s*=\s*[\"']?\$\{\1:-(\d{4,5})\}",
            script,
        ):
            declared.append((m.group(1), int(m.group(2))))
        for m in re.finditer(
            r"\b([A-Z][A-Z0-9_]*PORT)\s*=\s*[\"']?(\d{4,5})\b",
            script,
        ):
            declared.append((m.group(1), int(m.group(2))))

    seen: set[tuple[str | None, int]] = set()
    unique: list[tuple[str | None, int]] = []
    for env_name, port in declared:
        item = (env_name, port)
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return sorted(unique, key=lambda item: (item[1], item[0] or ""))


def _parse_declared_ports(project_dir: Path) -> list[int]:
    """Best-effort port extraction from CHARTER.md and start.sh."""
    return sorted({port for _env, port in _parse_declared_port_envs(project_dir)})


def _root_shell_scripts(temp_root: Path) -> list[Path]:
    return sorted(p for p in temp_root.glob("*.sh") if p.is_file())


def _host_bash_major(bash: str) -> int | None:
    try:
        proc = subprocess.run(
            [bash, "--version"],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    first = (proc.stdout or proc.stderr or "").splitlines()
    if not first:
        return None
    match = re.search(r"version\s+(\d+)\.", first[0])
    return int(match.group(1)) if match else None


def _uses_bash4_case_expansion(text: str) -> bool:
    return bool(re.search(r"\$\{[^}\n]+(?:\^\^|,,)[^}\n]*\}", text))


def _bind_local_port(port: int) -> socket.socket | None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        return None


def _run_start_sh_port_conflict_probe(
    temp_root: Path,
    bash: str,
    timeout_s: int,
    log: Any,
) -> tuple[bool, str | None, list[str]]:
    steps: list[str] = []
    start_sh = temp_root / "start.sh"
    if not start_sh.exists():
        return True, None, steps
    try:
        script = start_sh.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"start.sh could not be read: {exc}", steps
    if "PORT_CONFLICT" not in script and "lsof" not in script:
        return True, None, steps

    candidates = _parse_declared_port_envs(temp_root)
    if not candidates:
        log("script_valid: port-conflict probe skipped; no declared port")
        return True, None, steps

    for env_name, port in candidates:
        listener = _bind_local_port(port)
        if listener is None:
            continue
        try:
            env = dict(os.environ)
            if env_name:
                env[env_name] = str(port)
            steps.append("script_valid:port_conflict_probe")
            log(f"script_valid: probing start.sh port-conflict branch on {port}")
            proc = subprocess.Popen(  # noqa: S603 - local generated script
                [bash, "start.sh"],
                cwd=temp_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                start_new_session=True,
            )
            try:
                out, _ = proc.communicate(timeout=max(1, timeout_s))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
                return (
                    False,
                    "start.sh did not exit through its PORT_CONFLICT branch "
                    f"within {timeout_s}s; the validator refused to let it "
                    "continue into a full service launch.",
                    steps,
                )
            output = out or ""
            lowered = output.lower()
            shell_errors = (
                "bad substitution",
                "command not found",
                "syntax error",
                "unbound variable",
                "numeric argument required",
            )
            if any(err in lowered for err in shell_errors):
                return (
                    False,
                    "start.sh PORT_CONFLICT branch hit a shell/runtime error. "
                    f"Output (truncated): {output[-800:]!r}",
                    steps,
                )
            if proc.returncode != 0 and "PORT_CONFLICT" in output:
                return True, None, steps
            return (
                False,
                "start.sh did not report PORT_CONFLICT cleanly when "
                f"declared port {port} was busy. Exit={proc.returncode}. "
                f"Output (truncated): {output[-800:]!r}",
                steps,
            )
        finally:
            listener.close()

    log("script_valid: port-conflict probe skipped; declared ports already busy")
    return True, None, steps


def _script_valid(
    temp_root: Path,
    timeout_s: int,
    log: Any,
) -> tuple[bool, FailureKind | None, str | None, list[str]]:
    """Validate root shell scripts without launching the full product."""
    steps: list[str] = []
    scripts = _root_shell_scripts(temp_root)
    if not scripts:
        return True, None, None, steps

    bash = shutil.which("bash")
    if not bash:
        return False, "script_valid_failed", "bash not on PATH", steps
    bash_major = _host_bash_major(bash)

    for script in scripts:
        rel = script.name
        steps.append(f"script_valid:shebang:{rel}")
        try:
            text = script.read_text(encoding="utf-8")
        except OSError as exc:
            return False, "script_valid_failed", f"{rel} could not be read: {exc}", steps
        first_line = text.splitlines()[0] if text.splitlines() else ""
        if not first_line.startswith("#!"):
            return False, "script_valid_failed", f"{rel} is missing a shebang", steps

        steps.append(f"script_valid:executable:{rel}")
        if not os.access(script, os.X_OK):
            return False, "script_valid_failed", f"{rel} is not executable", steps

        steps.append(f"script_valid:bash_n:{rel}")
        try:
            proc = subprocess.run(
                [bash, "-n", rel],
                cwd=temp_root,
                capture_output=True,
                text=True,
                timeout=max(3, min(timeout_s, 10)),
            )
        except subprocess.TimeoutExpired:
            return False, "script_valid_failed", f"bash -n {rel} timed out", steps
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-800:]
            return (
                False,
                "script_valid_failed",
                f"bash -n {rel} failed: exit {proc.returncode}. Tail: {tail!r}",
                steps,
            )

        if bash_major is not None and bash_major < 4 and _uses_bash4_case_expansion(text):
            return (
                False,
                "script_valid_failed",
                f"{rel} uses bash-4-only case-modification expansion "
                "(${var^^} or ${var,,}), but host bash is "
                f"{bash_major}.x.",
                steps,
            )

    passed, message, probe_steps = _run_start_sh_port_conflict_probe(
        temp_root,
        bash=bash,
        timeout_s=max(2, min(timeout_s, 5)),
        log=log,
    )
    steps.extend(probe_steps)
    if not passed:
        return False, "script_valid_failed", message, steps
    return True, None, None, steps


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


def cleanup_stale_declared_ports(
    project_dir: Path, logger_fn: Any = None
) -> list[int]:
    """Kill processes bound to project's declared ports.

    Called once at the start of a pipeline run to clean up zombies from
    prior otto sessions (dev servers, test runners that didn't shut down
    cleanly). Each "port already in use" error inside an agent's session
    burns 30-60 seconds of agent time + tokens diagnosing it; doing one
    cleanup pass up-front saves that across the whole run.

    Reads ports from CHARTER.md (best-effort). Skips silently if CHARTER
    is absent or has no port declarations — the architect hasn't pinned
    ports yet, so there's nothing zombie-able.

    Returns the list of ports we killed something on (may be empty).
    """
    ports = _parse_declared_ports(project_dir)
    if not ports:
        return []

    def log(msg: str) -> None:
        if logger_fn:
            logger_fn(msg)

    killed_on: list[int] = []
    for port in ports:
        try:
            out = subprocess.check_output(
                ["lsof", "-ti", f":{port}"],
                text=True,
                timeout=2,
                stderr=subprocess.DEVNULL,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            continue
        pids: list[int] = []
        for pid_str in out.strip().split():
            try:
                pids.append(int(pid_str.strip()))
            except ValueError:
                continue
        if not pids:
            continue
        log(f"port-cleanup: port {port} bound by PIDs {pids}; killing")
        for pid in pids:
            try:
                subprocess.run(
                    ["kill", "-9", str(pid)],
                    timeout=2,
                    check=False,
                    stderr=subprocess.DEVNULL,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        killed_on.append(port)
    return killed_on


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
      2. Validate root shell scripts without launching full services.
      3. For each manifest: install deps via lockfile when present,
         then run build (if a build script is declared).
      4. If ``scope`` >= ``"subtree"`` and ``start.sh`` exists: run it
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

        # Shift-left root shell portability before installing deps. The
        # dynamic branch probe forces only the fast PORT_CONFLICT path.
        script_ok, script_kind, script_message, script_steps = _script_valid(
            temp_root,
            timeout_s=timeout_s,
            log=log,
        )
        if not script_ok:
            return CleanVerifyResult(
                passed=False,
                scope=scope,
                failure_kind=script_kind,
                failure_message=script_message,
                steps_run=script_steps,
                temp_dir=temp_root if preserve_temp else None,
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
                steps_run=script_steps + steps_a,
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
                    steps_run=script_steps + steps_a + steps_b,
                    temp_dir=temp_root if preserve_temp else None,
                    listening_ports=listening,
                )
            return CleanVerifyResult(
                passed=True,
                scope=scope,
                steps_run=script_steps + steps_a + steps_b,
                temp_dir=temp_root if preserve_temp else None,
                listening_ports=listening,
            )

        # Scaffold scope: stop here.
        return CleanVerifyResult(
            passed=True,
            scope=scope,
            steps_run=script_steps + steps_a,
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
