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

import hashlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from otto.journey_scope_policy import ExecutionScope, applicability_for
from otto.journey_verdict_sink import failed_journey_ids, resolve_journey_verdicts
from otto.spec_compile_flat import StructuredSpecValidationError, load_flat_spec

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
    "ui_journey_failed",
    "oracle_infra_error",
    "verification_contract_invalid",
    "verification_contract_missing",
    "real_project_dirty",
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
class CleanOracleIssue:
    """Typed, serializable issue emitted by the clean-deploy oracle."""

    kind: str
    severity: Literal["warn", "error", "block"]
    message: str
    step_id: str
    paths: list[str] = field(default_factory=list)
    ports: list[int] = field(default_factory=list)
    command_identity: str = ""
    return_code: int | None = None

    def to_jsonable(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CleanOracleStepResult:
    """One step in the clean verifier DAG."""

    id: str
    status: str
    return_code: int | None
    command_identity: str
    command: list[str] = field(default_factory=list)
    cwd: str = ""
    env: dict[str, str] = field(default_factory=dict)
    started_at: str = ""
    duration_s: float = 0.0
    artifact_paths: list[str] = field(default_factory=list)
    stdout_tail: str = ""
    stderr_tail: str = ""
    reason: str = ""

    def to_jsonable(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CleanOracleResult:
    """Serializable clean-deploy oracle result used by repair packets."""

    passed: bool
    scope: Scope
    issues: list[CleanOracleIssue] = field(default_factory=list)
    steps: list[CleanOracleStepResult] = field(default_factory=list)
    artifact_path_refs: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    digest: str = ""
    _written_at: str = ""

    @classmethod
    def from_parts(
        cls,
        *,
        passed: bool,
        scope: Scope,
        issues: list[CleanOracleIssue],
        steps: list[CleanOracleStepResult],
        artifact_path_refs: list[str],
        command: list[str],
        env: dict[str, str],
        project_dir: Path,
        temp_dir: Path | None = None,
    ) -> "CleanOracleResult":
        result = cls(
            passed=passed,
            scope=scope,
            issues=issues,
            steps=steps,
            artifact_path_refs=artifact_path_refs,
            command=command,
            env=env,
            _written_at=_iso_now(),
        )
        result.digest = _clean_oracle_digest(
            result,
            project_dir=project_dir,
            temp_dir=temp_dir,
        )
        return result

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "scope": self.scope,
            "issues": [issue.to_jsonable() for issue in self.issues],
            "steps": [step.to_jsonable() for step in self.steps],
            "artifact_path_refs": list(self.artifact_path_refs),
            "command": list(self.command),
            "env": dict(self.env),
            "digest": self.digest,
            "_written_at": self._written_at,
        }


@dataclass(frozen=True)
class CleanVerifyOracleCommand:
    """Resolved in-worktree clean verifier command for repair packets."""

    command: list[str]
    env: dict[str, str]
    verify_scope: Scope
    repair_packet_path: str = ""

    @property
    def command_identity(self) -> str:
        return _command_identity(self.command)


@dataclass
class PortCleanupResult:
    """Honest startup cleanup result for declared ports."""

    killed_ports: list[int] = field(default_factory=list)
    freed_ports: list[int] = field(default_factory=list)
    still_bound_ports: list[int] = field(default_factory=list)
    killed_pids: dict[int, list[int]] = field(default_factory=dict)
    pids_before: dict[int, list[int]] = field(default_factory=dict)
    pids_after: dict[int, list[int]] = field(default_factory=dict)
    ports_without_owned_process: list[int] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.killed_ports or self.still_bound_ports)

    def __iter__(self) -> Iterator[int]:
        return iter(self.killed_ports)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return self.killed_ports == other
        return super().__eq__(other)

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

_SERIALIZED_ENV_EXACT_ALLOWLIST = {
    "CI",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NODE_OPTIONS",
    "NO_COLOR",
    "NPM_CONFIG_CACHE",
    "PATH",
    "PYTHONPATH",
    "TMP",
    "TEMP",
    "TMPDIR",
    "TZ",
    "UV_CACHE_DIR",
    "VIRTUAL_ENV",
    "npm_config_cache",
}
_SERIALIZED_ENV_SUFFIX_ALLOWLIST = (
    "_DIR",
    "_HOST",
    "_PATH",
    "_PORT",
    "_ROOT",
)
_SECRET_ENV_NAME_PARTS = (
    "API_KEY",
    "AUTH",
    "COOKIE",
    "CREDENTIAL",
    "KEY",
    "PASSWORD",
    "SECRET",
    "TOKEN",
)


def _secret_like_env_key(key: str) -> bool:
    upper = key.upper()
    return any(part in upper for part in _SECRET_ENV_NAME_PARTS)


def _serialized_oracle_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return the small non-secret env surface safe to persist in packets."""
    source = base_env if base_env is not None else os.environ
    env: dict[str, str] = {}
    for key, value in source.items():
        if _secret_like_env_key(key):
            continue
        upper = key.upper()
        if (
            key in _SERIALIZED_ENV_EXACT_ALLOWLIST
            or upper.startswith("OTTO_")
            or upper.startswith("AGENT_BROWSER_")
            or upper.startswith("PLAYWRIGHT_")
            or upper.endswith(_SERIALIZED_ENV_SUFFIX_ALLOWLIST)
        ):
            env[str(key)] = str(value)
    return env


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


def _command_identity(command: list[str]) -> str:
    """Stable command identity for digesting; paths collapse to basenames."""
    parts: list[str] = []
    for index, item in enumerate(command):
        text = str(item)
        if "otto-clean-" in text:
            parts.append("<temp-root>")
        elif Path(text).is_absolute():
            parts.append(Path(text).name)
        else:
            parts.append(text)
    return " ".join(parts)


def _normalize_path_for_digest(path: str, *, project_dir: Path, temp_dir: Path | None) -> str:
    raw = str(path)
    try:
        p = Path(raw)
    except TypeError:
        return raw
    candidates = [project_dir.resolve(strict=False)]
    if temp_dir is not None:
        candidates.append(temp_dir.resolve(strict=False))
    if p.is_absolute():
        resolved = p.resolve(strict=False)
        for root in candidates:
            try:
                return resolved.relative_to(root).as_posix()
            except ValueError:
                continue
        return Path(resolved.name).as_posix()
    return raw.replace("\\", "/")


def _normalize_text_for_digest(
    text: str,
    *,
    project_dir: Path,
    temp_dir: Path | None,
) -> str:
    normalized = text
    roots = [project_dir.resolve(strict=False)]
    if temp_dir is not None:
        roots.append(temp_dir.resolve(strict=False))
    for root in roots:
        normalized = normalized.replace(str(root), "<root>")
    normalized = re.sub(r"/(?:private/)?tmp/otto-clean-[^ \n'\":)]+", "<temp-root>", normalized)
    normalized = re.sub(r"\b\d+(?:\.\d+)?s\b", "<duration>", normalized)
    return normalized


def _clean_oracle_digest(
    result: CleanOracleResult,
    *,
    project_dir: Path,
    temp_dir: Path | None,
) -> str:
    canonical = {
        "issues": sorted(
            (
                {
                    "kind": issue.kind,
                    "severity": issue.severity,
                    "message": _normalize_text_for_digest(
                        issue.message,
                        project_dir=project_dir,
                        temp_dir=temp_dir,
                    ),
                    "step_id": issue.step_id,
                    "paths": sorted(
                        _normalize_path_for_digest(
                            path,
                            project_dir=project_dir,
                            temp_dir=temp_dir,
                        )
                        for path in issue.paths
                    ),
                    "ports": sorted(int(port) for port in issue.ports),
                    "command_identity": issue.command_identity,
                    "return_code": issue.return_code,
                }
                for issue in result.issues
            ),
            key=lambda item: json.dumps(item, sort_keys=True),
        ),
        "steps": sorted(
            (
                {
                    "id": step.id,
                    "status": step.status,
                    "return_code": step.return_code,
                    "command_identity": step.command_identity,
                }
                for step in result.steps
            ),
            key=lambda item: str(item["id"]),
        ),
        "artifact_path_refs": sorted(
            _normalize_path_for_digest(path, project_dir=project_dir, temp_dir=temp_dir)
            for path in result.artifact_path_refs
        ),
        "command_identities": sorted({step.command_identity for step in result.steps if step.command_identity}),
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_clean_verify_oracle_command(
    *,
    worktree_path: Path,
    verify_scope: Scope,
    repair_packet_path: Path | None = None,
    spec_path: Path | None = None,
    journey_scope: ExecutionScope = "subtree_integration",
    journey_artifact_dir: Path | None = None,
) -> CleanVerifyOracleCommand:
    """Return the resolved CLI command stored in repair packets.

    Prefer the linked worktree's own venv Python so ``python -m otto.cli``
    loads Otto from the same checkout and avoids the linked-worktree venv guard.
    Tests and sparse fixtures without a venv fall back to the current
    interpreter.
    """
    worktree = Path(worktree_path)
    python = worktree / ".venv" / "bin" / "python"
    repo_python = Path(__file__).resolve().parents[1] / ".venv" / "bin" / "python"
    if python.exists():
        executable = str(python)
    elif repo_python.exists():
        executable = str(repo_python)
    else:
        executable = str(Path(sys.executable).resolve())
    command = [
        executable,
        "-m",
        "otto.cli",
        "clean-verify",
        "--json",
        "--verify-scope",
        verify_scope,
    ]
    env = _serialized_oracle_env()
    repo_root = Path(__file__).resolve().parents[1]
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(repo_root)
        if not existing_pythonpath
        else str(repo_root) + os.pathsep + existing_pythonpath
    )
    env["OTTO_CLEAN_VERIFY_WORKTREE"] = str(worktree.resolve(strict=False))
    repair_packet = ""
    if repair_packet_path is not None:
        repair_packet = str(Path(repair_packet_path).resolve(strict=False))
        command.extend(["--repair-packet", repair_packet])
        env["OTTO_REPAIR_PACKET_PATH"] = repair_packet
    if spec_path is not None:
        resolved_spec = str(Path(spec_path).resolve(strict=False))
        command.extend(["--spec-path", resolved_spec])
        env["OTTO_CLEAN_VERIFY_SPEC_PATH"] = resolved_spec
    if journey_scope != "subtree_integration":
        command.extend(["--journey-scope", journey_scope])
    env["OTTO_CLEAN_VERIFY_JOURNEY_SCOPE"] = journey_scope
    if journey_artifact_dir is not None:
        resolved_artifacts = str(Path(journey_artifact_dir).resolve(strict=False))
        command.extend(["--journey-artifact-dir", resolved_artifacts])
        env["OTTO_CLEAN_VERIFY_JOURNEY_ARTIFACT_DIR"] = resolved_artifacts
    return CleanVerifyOracleCommand(
        command=command,
        env=env,
        verify_scope=verify_scope,
        repair_packet_path=repair_packet,
    )


def _oracle_step(
    *,
    step_id: str,
    status: str,
    return_code: int | None,
    command: list[str],
    cwd: Path,
    env: dict[str, str] | None = None,
    started_at: str = "",
    duration_s: float = 0.0,
    stdout_tail: str = "",
    stderr_tail: str = "",
    reason: str = "",
) -> CleanOracleStepResult:
    return CleanOracleStepResult(
        id=step_id,
        status=status,
        return_code=return_code,
        command_identity=_command_identity(command),
        command=[str(part) for part in command],
        cwd=str(cwd),
        env=dict(env or {}),
        started_at=started_at,
        duration_s=round(duration_s, 3),
        stdout_tail=stdout_tail[-800:],
        stderr_tail=stderr_tail[-800:],
        reason=reason,
    )


def _issue_from_step(
    *,
    kind: str,
    message: str,
    step: CleanOracleStepResult,
    ports: list[int] | None = None,
    paths: list[str] | None = None,
) -> CleanOracleIssue:
    return CleanOracleIssue(
        kind=kind,
        severity="block",
        message=message,
        step_id=step.id,
        paths=list(paths or []),
        ports=list(ports or []),
        command_identity=step.command_identity,
        return_code=step.return_code,
    )


def _run_oracle_command(
    *,
    step_id: str,
    kind_failed: str,
    command: list[str],
    cwd: Path,
    timeout_s: int,
    env: dict[str, str] | None = None,
    message_prefix: str,
) -> tuple[CleanOracleStepResult, CleanOracleIssue | None]:
    started_at = _iso_now()
    started = time.monotonic()
    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - started
        stdout_tail = cast(str, exc.stdout)[-800:] if isinstance(exc.stdout, str) else ""
        stderr_tail = cast(str, exc.stderr)[-800:] if isinstance(exc.stderr, str) else ""
        step = _oracle_step(
            step_id=step_id,
            status="failed",
            return_code=None,
            command=command,
            cwd=cwd,
            env=env,
            started_at=started_at,
            duration_s=duration,
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
            reason=f"timed out after {timeout_s}s",
        )
        return step, _issue_from_step(
            kind=kind_failed,
            message=f"{message_prefix} timed out after {timeout_s}s",
            step=step,
        )
    duration = time.monotonic() - started
    status = "passed" if proc.returncode == 0 else "failed"
    step = _oracle_step(
        step_id=step_id,
        status=status,
        return_code=proc.returncode,
        command=command,
        cwd=cwd,
        env=env,
        started_at=started_at,
        duration_s=duration,
        stdout_tail=proc.stdout or "",
        stderr_tail=proc.stderr or "",
    )
    if proc.returncode == 0:
        return step, None
    tail = (proc.stderr or proc.stdout or "")[-400:]
    return step, _issue_from_step(
        kind=kind_failed,
        message=f"{message_prefix} failed: exit {proc.returncode}. Tail: {tail!r}",
        step=step,
    )


def _package_has_build(package_json: Path) -> bool:
    try:
        raw = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        try:
            return '"build"' in package_json.read_text(encoding="utf-8")
        except OSError:
            return False
    scripts = raw.get("scripts") if isinstance(raw, dict) else None
    return isinstance(scripts, dict) and "build" in scripts


def _first_failed_step_id(steps: list[CleanOracleStepResult]) -> str | None:
    for step in steps:
        if step.status == "failed":
            return step.id
    return None


def _load_clean_oracle_journeys(
    *,
    behavior_journeys: list[dict[str, Any]] | None,
    spec_path: Path | None,
) -> tuple[list[dict[str, Any]], CleanOracleIssue | None]:
    if behavior_journeys is not None:
        return [dict(journey) for journey in behavior_journeys], None
    if spec_path is None:
        return [], None
    if not spec_path.exists():
        return [], None
    try:
        spec = load_flat_spec(spec_path)
    except (OSError, json.JSONDecodeError, StructuredSpecValidationError) as exc:
        step = _oracle_step(
            step_id="ui_journeys",
            status="failed",
            return_code=1,
            command=["load_flat_spec", str(spec_path)],
            cwd=spec_path.parent,
            reason=f"{type(exc).__name__}: {exc}",
        )
        issue_kind = "verification_contract_invalid"
        message = f"behavior journey contract could not be loaded: {type(exc).__name__}: {exc}"
        return [], _issue_from_step(kind=issue_kind, message=message, step=step)
    return [dict(journey) for journey in spec.behavior_journeys], None


def _ui_journeys_for_scope(
    journeys: list[dict[str, Any]],
    journey_scope: ExecutionScope,
) -> list[dict[str, Any]]:
    if applicability_for(journey_scope, "ui") != "run":
        return []
    return [
        journey
        for journey in journeys
        if isinstance(journey, dict)
        and str(journey.get("verification_level") or "").strip() == "ui"
    ]


def _frontend_base_url(port_envs: list[tuple[str | None, int]], ports: list[int]) -> str:
    preferred_names = ("FE", "FRONTEND", "WEB", "UI", "CLIENT")
    for env_name, port in port_envs:
        name = str(env_name or "").upper()
        if any(marker in name for marker in preferred_names):
            return f"http://127.0.0.1:{port}"
    if ports:
        return f"http://127.0.0.1:{ports[0]}"
    return ""


def _ui_journey_step_from_issue(issue: CleanOracleIssue) -> CleanOracleStepResult:
    return CleanOracleStepResult(
        id=issue.step_id,
        status="failed",
        return_code=1,
        command_identity=issue.command_identity,
        command=["ui_journeys"],
        cwd="",
        reason=issue.message,
    )


def _run_ui_journeys_clean_oracle_step(
    *,
    journeys: list[dict[str, Any]],
    project: Path,
    temp_root: Path,
    base_url: str,
    artifact_dir: Path,
    timeout_s: int,
    journey_scope: ExecutionScope,
) -> tuple[CleanOracleStepResult, CleanOracleIssue | None]:
    started_at = _iso_now()
    started = time.monotonic()
    if not base_url:
        step = _oracle_step(
            step_id="ui_journeys",
            status="failed",
            return_code=1,
            command=["ui_journey_executor"],
            cwd=temp_root,
            started_at=started_at,
            duration_s=time.monotonic() - started,
            reason="ui journeys require at least one declared frontend port",
        )
        return step, _issue_from_step(
            kind="oracle_infra_error",
            message="ui journeys require at least one declared frontend port",
            step=step,
        )

    from otto.journey_ui_executor import run_ui_journey_executor

    probe = run_ui_journey_executor(
        journeys=journeys,
        base_url=base_url,
        project_dir=project,
        clean_project_dir=temp_root,
        artifact_dir=artifact_dir,
        timeout_s=timeout_s,
    )
    verdicts = resolve_journey_verdicts(
        journeys=journeys,
        execution_scope=journey_scope,
        executor_results=probe.executor_results,
        registered_executor_levels={"ui", "api"},
    )
    verdict_path = artifact_dir / "journey-verdicts.json"
    verdict_path.parent.mkdir(parents=True, exist_ok=True)
    verdict_path.write_text(
        json.dumps(
            {
                "_written_at": _iso_now(),
                "source": "journey_verdict_sink",
                "journey_scope": journey_scope,
                "base_url": base_url,
                "executor_results": probe.executor_results,
                "journey_verdicts": verdicts,
                "artifact_paths": [str(path) for path in probe.artifact_paths],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    artifacts = [str(path) for path in probe.artifact_paths]
    artifacts.append(str(verdict_path))
    failures = failed_journey_ids(verdicts)
    infra_error = probe.infra_error or ""
    passed = not failures and not infra_error
    reason = (
        f"{len(verdicts)}/{len(verdicts)} ui journeys passed"
        if passed
        else infra_error or f"ui journeys failed: {', '.join(failures)}"
    )
    step = _oracle_step(
        step_id="ui_journeys",
        status="passed" if passed else "failed",
        return_code=0 if passed else 1,
        command=["ui_journey_executor", base_url],
        cwd=temp_root,
        started_at=started_at,
        duration_s=time.monotonic() - started,
        reason=reason,
    )
    step.artifact_paths.extend(artifacts)
    if passed:
        return step, None
    issue_kind = "oracle_infra_error" if infra_error else "ui_journey_failed"
    return step, _issue_from_step(kind=issue_kind, message=reason, step=step)


def verify_from_clean_oracle(
    project_dir: Path,
    *,
    scope: Scope = "subtree",
    timeout_s: int = 120,
    port_wait_s: int = 12,
    preserve_temp: bool = False,
    logger_fn: Any = None,
    journey_scope: ExecutionScope = "subtree_integration",
    spec_path: Path | None = None,
    behavior_journeys: list[dict[str, Any]] | None = None,
    journey_artifact_dir: Path | None = None,
) -> CleanOracleResult:
    """Clean-state verifier with packet-grade step DAG evidence.

    Independent install/build/compile branches continue after sibling failures;
    dependent steps are recorded as ``skipped_due_to:<upstream-step>`` instead
    of producing synthetic cascade issues.
    """

    def log(msg: str) -> None:
        if logger_fn:
            logger_fn(msg)

    project = Path(project_dir)
    command_spec = build_clean_verify_oracle_command(
        worktree_path=project,
        verify_scope=scope,
        spec_path=spec_path,
        journey_scope=journey_scope,
        journey_artifact_dir=journey_artifact_dir,
    )
    steps: list[CleanOracleStepResult] = []
    issues: list[CleanOracleIssue] = []
    artifact_refs: list[str] = []
    declared_ports: list[int] = []
    declared_port_envs: list[tuple[str | None, int]] = []
    port_block_step_id: str | None = None
    if applicability_for(journey_scope, "ui") == "run":
        loaded_journeys, journey_contract_issue = _load_clean_oracle_journeys(
            behavior_journeys=behavior_journeys,
            spec_path=spec_path,
        )
        ui_journeys = _ui_journeys_for_scope(loaded_journeys, journey_scope)
    else:
        journey_contract_issue = None
        ui_journeys = []

    if scope in ("subtree", "full"):
        declared_port_envs = _parse_declared_port_envs(project)
        declared_ports = sorted({port for _env_name, port in declared_port_envs})
        busy = _check_ports_free(declared_ports)
        step = _oracle_step(
            step_id="precheck_ports",
            status="failed" if busy else "passed",
            return_code=1 if busy else 0,
            command=["check_ports_free", *[str(port) for port in declared_ports]],
            cwd=project,
            env={"PATH": os.environ.get("PATH", "")},
            reason=("declared ports busy" if busy else ""),
        )
        steps.append(step)
        if busy:
            port_block_step_id = step.id
            issues.append(_issue_from_step(
                kind="port_busy",
                message=(
                    f"Declared ports {busy} already bound (likely zombies "
                    "from prior runs). Cannot run clean-deploy."
                ),
                step=step,
                ports=busy,
            ))

    temp_root = Path(tempfile.mkdtemp(prefix="otto-clean-"))
    artifact_refs.append(str(temp_root))
    if journey_artifact_dir is not None:
        artifact_refs.append(str(journey_artifact_dir))
    try:
        copy_started = time.monotonic()
        copied, copy_err = _copy_project_clean(project, temp_root)
        copy_step = _oracle_step(
            step_id="copy",
            status="passed" if copied else "failed",
            return_code=0 if copied else 1,
            command=["copy_project_clean", str(project), str(temp_root)],
            cwd=project,
            env={},
            started_at=_iso_now(),
            duration_s=time.monotonic() - copy_started,
            reason=copy_err or "",
        )
        steps.append(copy_step)
        if not copied:
            issues.append(_issue_from_step(
                kind="copy_failed",
                message=copy_err or "copy to clean temp dir failed",
                step=copy_step,
            ))
            for skipped_id in ("script_valid", "scaffold", "start"):
                steps.append(_oracle_step(
                    step_id=skipped_id,
                    status="skipped_due_to:copy",
                    return_code=None,
                    command=[skipped_id],
                    cwd=temp_root,
                ))
            return CleanOracleResult.from_parts(
                passed=False,
                scope=scope,
                issues=issues,
                steps=steps,
                artifact_path_refs=artifact_refs,
                command=command_spec.command,
                env=command_spec.env,
                project_dir=project,
                temp_dir=temp_root,
            )

        script_started = time.monotonic()
        script_ok, script_kind, script_message, _script_steps = _script_valid(
            temp_root,
            timeout_s=timeout_s,
            log=log,
        )
        script_step = _oracle_step(
            step_id="script_valid",
            status="passed" if script_ok else "failed",
            return_code=0 if script_ok else 1,
            command=["script_valid", str(temp_root)],
            cwd=temp_root,
            env={"PATH": os.environ.get("PATH", "")},
            started_at=_iso_now(),
            duration_s=time.monotonic() - script_started,
            reason=script_message or "",
        )
        steps.append(script_step)
        if not script_ok:
            issues.append(_issue_from_step(
                kind=script_kind or "script_valid_failed",
                message=script_message or "script validation failed",
                step=script_step,
            ))

        package_jsons, pyprojects = _find_manifests(temp_root)
        package_jsons = sorted(package_jsons)
        pyprojects = sorted(pyprojects)
        npm_path = shutil.which("npm")
        if package_jsons and not npm_path:
            step = _oracle_step(
                step_id="npm_available",
                status="failed",
                return_code=1,
                command=["npm", "--version"],
                cwd=temp_root,
                env={"PATH": os.environ.get("PATH", "")},
                reason="npm not on PATH",
            )
            steps.append(step)
            issues.append(_issue_from_step(kind="no_npm", message="npm not on PATH", step=step))
        for pkg in package_jsons:
            rel_parent = pkg.parent.relative_to(temp_root).as_posix() or "."
            step_suffix = "root" if pkg.parent == temp_root else pkg.parent.name
            install_id = f"npm_install:{step_suffix}"
            build_id = f"npm_build:{step_suffix}"
            if not npm_path:
                steps.append(_oracle_step(
                    step_id=install_id,
                    status="skipped_due_to:npm_available",
                    return_code=None,
                    command=["npm", "install"],
                    cwd=pkg.parent,
                ))
                steps.append(_oracle_step(
                    step_id=build_id,
                    status=f"skipped_due_to:{install_id}",
                    return_code=None,
                    command=["npm", "run", "build", "--silent"],
                    cwd=pkg.parent,
                ))
                continue
            has_lockfile = (pkg.parent / "package-lock.json").exists()
            install_cmd = (
                [npm_path, "ci", "--no-audit", "--no-fund"]
                if has_lockfile
                else [npm_path, "install", "--no-audit", "--no-fund"]
            )
            log(f"verify_from_clean: {install_cmd[1]} in {rel_parent}")
            install_step, install_issue = _run_oracle_command(
                step_id=install_id,
                kind_failed="install_failed",
                command=install_cmd,
                cwd=pkg.parent,
                timeout_s=timeout_s * 3,
                env=None,
                message_prefix=f"{install_cmd[1]} in {rel_parent}",
            )
            steps.append(install_step)
            if install_issue is not None:
                issues.append(install_issue)
                steps.append(_oracle_step(
                    step_id=build_id,
                    status=f"skipped_due_to:{install_id}",
                    return_code=None,
                    command=[npm_path, "run", "build", "--silent"],
                    cwd=pkg.parent,
                ))
                continue
            if not _package_has_build(pkg):
                steps.append(_oracle_step(
                    step_id=build_id,
                    status="skipped",
                    return_code=None,
                    command=[npm_path, "run", "build", "--silent"],
                    cwd=pkg.parent,
                    reason="package has no build script",
                ))
                continue
            log(f"verify_from_clean: npm run build in {rel_parent}")
            build_step, build_issue = _run_oracle_command(
                step_id=build_id,
                kind_failed="build_failed",
                command=[npm_path, "run", "build", "--silent"],
                cwd=pkg.parent,
                timeout_s=timeout_s,
                env=None,
                message_prefix=f"npm run build in {rel_parent}",
            )
            steps.append(build_step)
            if build_issue is not None:
                issues.append(build_issue)

        python_path = shutil.which("python3") or shutil.which("python")
        if pyprojects and not python_path:
            step = _oracle_step(
                step_id="python_available",
                status="failed",
                return_code=1,
                command=["python3", "--version"],
                cwd=temp_root,
                env={"PATH": os.environ.get("PATH", "")},
                reason="python not on PATH",
            )
            steps.append(step)
            issues.append(_issue_from_step(kind="no_python", message="python not on PATH", step=step))
        for pyp in pyprojects:
            step_suffix = "root" if pyp.parent == temp_root else pyp.parent.name
            step_id = f"py_compile:{step_suffix}"
            if not python_path:
                steps.append(_oracle_step(
                    step_id=step_id,
                    status="skipped_due_to:python_available",
                    return_code=None,
                    command=["python3", "-m", "py_compile"],
                    cwd=pyp.parent,
                ))
                continue
            py_files = sorted(
                p for p in pyp.parent.rglob("*.py")
                if ".venv" not in p.parts
                and "__pycache__" not in p.parts
                and ".worktrees" not in p.parts
            )
            if not py_files:
                steps.append(_oracle_step(
                    step_id=step_id,
                    status="skipped",
                    return_code=None,
                    command=[python_path, "-m", "py_compile"],
                    cwd=pyp.parent,
                    reason="no Python files",
                ))
                continue
            rel_files = [p.relative_to(pyp.parent).as_posix() for p in py_files]
            log(f"verify_from_clean: py_compile in {pyp.parent.name} ({len(py_files)} files)")
            compile_step, compile_issue = _run_oracle_command(
                step_id=step_id,
                kind_failed="py_compile_failed",
                command=[python_path, "-m", "py_compile", *rel_files],
                cwd=pyp.parent,
                timeout_s=timeout_s,
                env=None,
                message_prefix=f"py_compile in {pyp.parent.name}",
            )
            steps.append(compile_step)
            if compile_issue is not None:
                compile_issue.paths = rel_files
                issues.append(compile_issue)

        if scope in ("subtree", "full"):
            start_sh = temp_root / "start.sh"
            upstream = port_block_step_id or _first_failed_step_id(
                [step for step in steps if step.id != "precheck_ports"]
            )
            if not start_sh.exists():
                steps.append(_oracle_step(
                    step_id="start",
                    status="skipped",
                    return_code=None,
                    command=["bash", "start.sh"],
                    cwd=temp_root,
                    reason="no start.sh",
                ))
                if ui_journeys:
                    step = _oracle_step(
                        step_id="ui_journeys",
                        status="failed",
                        return_code=1,
                        command=["ui_journey_executor"],
                        cwd=temp_root,
                        reason="ui journeys require start.sh clean deployment",
                    )
                    steps.append(step)
                    issues.append(_issue_from_step(
                        kind="oracle_infra_error",
                        message="ui journeys require start.sh clean deployment",
                        step=step,
                    ))
            elif upstream:
                steps.append(_oracle_step(
                    step_id="start",
                    status=f"skipped_due_to:{upstream}",
                    return_code=None,
                    command=["bash", "start.sh"],
                    cwd=temp_root,
                ))
            else:
                ui_step: CleanOracleStepResult | None = None
                ui_issue: CleanOracleIssue | None = None

                def run_ui_probe(listening_ports: list[int]) -> None:
                    nonlocal ui_step, ui_issue
                    if journey_contract_issue is not None:
                        ui_step = _ui_journey_step_from_issue(journey_contract_issue)
                        ui_issue = journey_contract_issue
                        return
                    if not ui_journeys:
                        return
                    try:
                        artifact_root = (
                            journey_artifact_dir
                            if journey_artifact_dir is not None
                            else temp_root / "otto_artifacts" / "journeys"
                        )
                        base_url = _frontend_base_url(declared_port_envs, listening_ports)
                        ui_step, ui_issue = _run_ui_journeys_clean_oracle_step(
                            journeys=ui_journeys,
                            project=project,
                            temp_root=temp_root,
                            base_url=base_url,
                            artifact_dir=artifact_root,
                            timeout_s=timeout_s,
                            journey_scope=journey_scope,
                        )
                    except Exception as exc:  # noqa: BLE001
                        ui_step = _oracle_step(
                            step_id="ui_journeys",
                            status="failed",
                            return_code=1,
                            command=["ui_journey_executor"],
                            cwd=temp_root,
                            reason=f"{type(exc).__name__}: {exc}",
                        )
                        ui_issue = _issue_from_step(
                            kind="oracle_infra_error",
                            message=f"ui journey executor raised: {type(exc).__name__}: {exc}",
                            step=ui_step,
                        )

                start_started = time.monotonic()
                passed, kind, message, _start_steps, listening = _subtree_verify_start_sh(
                    temp_root,
                    declared_ports=declared_ports,
                    timeout_s=timeout_s,
                    port_wait_s=port_wait_s,
                    log=log,
                    after_listening=run_ui_probe,
                )
                start_step = _oracle_step(
                    step_id="start",
                    status="passed" if passed else "failed",
                    return_code=0 if passed else 1,
                    command=["bash", "start.sh"],
                    cwd=temp_root,
                    env={"PATH": os.environ.get("PATH", "")},
                    started_at=_iso_now(),
                    duration_s=time.monotonic() - start_started,
                    reason=message or "",
                )
                steps.append(start_step)
                if not passed:
                    issues.append(_issue_from_step(
                        kind=kind or "start_failed",
                        message=message or "start.sh failed",
                        step=start_step,
                        ports=declared_ports,
                    ))
                elif listening:
                    start_step.artifact_paths.append(
                        "listening_ports:" + ",".join(str(port) for port in listening)
                    )
                if ui_step is not None:
                    steps.append(ui_step)
                    artifact_refs.extend(ui_step.artifact_paths)
                if ui_issue is not None:
                    issues.append(ui_issue)

        return CleanOracleResult.from_parts(
            passed=not issues,
            scope=scope,
            issues=issues,
            steps=steps,
            artifact_path_refs=artifact_refs,
            command=command_spec.command,
            env=command_spec.env,
            project_dir=project,
            temp_dir=temp_root,
        )
    except Exception as exc:  # noqa: BLE001
        step = _oracle_step(
            step_id="internal",
            status="failed",
            return_code=1,
            command=["verify_from_clean_oracle"],
            cwd=project,
            reason=f"{type(exc).__name__}: {exc}",
        )
        steps.append(step)
        issues.append(_issue_from_step(
            kind="internal_error",
            message=f"verify_from_clean_oracle raised: {type(exc).__name__}: {exc}",
            step=step,
        ))
        return CleanOracleResult.from_parts(
            passed=False,
            scope=scope,
            issues=issues,
            steps=steps,
            artifact_path_refs=artifact_refs,
            command=command_spec.command,
            env=command_spec.env,
            project_dir=project,
            temp_dir=temp_root,
        )
    finally:
        if not preserve_temp:
            shutil.rmtree(temp_root, ignore_errors=True)
            log("verify_from_clean: temp dir cleaned up")


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
        stdout_tail: str = cast(str, exc.stdout)[-800:] if isinstance(exc.stdout, str) else ""
        stderr_tail: str = cast(str, exc.stderr)[-800:] if isinstance(exc.stderr, str) else ""
        result.commands.append(ToolchainCommandResult(
            command=command,
            cwd=str(cwd),
            started_at=started_at,
            duration_s=round(duration_s, 3),
            returncode=None,
            status="failed",
            reason=f"timed out after {timeout_s}s",
            stdout_tail=stdout_tail,
            stderr_tail=stderr_tail,
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


def _pids_for_port(port: int) -> list[int]:
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f":{port}"],
            text=True,
            timeout=2,
            stderr=subprocess.DEVNULL,
        )
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        FileNotFoundError,
    ):
        return []
    pids: list[int] = []
    for raw in out.split():
        try:
            pids.append(int(raw))
        except ValueError:
            continue
    return pids


def _process_environ(proc: Any) -> dict[str, str]:
    try:
        env = proc.environ()
    except Exception:  # noqa: BLE001
        return {}
    return env if isinstance(env, dict) else {}


def _looks_like_field_test_path(path: str) -> bool:
    text = path.replace("\\", "/")
    return "/field-tests/" in text or text.endswith("/field-tests")


def _looks_like_local_server(cmdline: str) -> bool:
    lowered = cmdline.lower()
    markers = (
        "uvicorn",
        "http.server",
        "vite",
        "next dev",
        "npm run",
        "pnpm",
        "flask",
        "fastapi",
        "node",
    )
    return any(marker in lowered for marker in markers)


def _is_otto_owned_process(pid: int, project_dir: Path) -> bool:
    try:
        import psutil
    except ImportError:
        return False
    try:
        proc = psutil.Process(pid)
        cwd = Path(proc.cwd()).resolve()
        project = project_dir.resolve()
        if cwd == project or project in cwd.parents:
            return True
        env = _process_environ(proc)
        field_test_project = env.get("FIELD_TEST_PROJECT_DIR", "")
        if field_test_project and _looks_like_field_test_path(field_test_project):
            return True
        queue_project = env.get("OTTO_QUEUE_PROJECT_DIR", "")
        if queue_project:
            try:
                queue_root = Path(queue_project).resolve()
                if queue_root == project or queue_root in project.parents or project in queue_root.parents:
                    return True
            except OSError:
                pass
        cmdline = " ".join(proc.cmdline())
        if str(project) in cmdline and "otto" in cmdline.lower():
            return True
        if _looks_like_field_test_path(str(cwd)):
            return _looks_like_local_server(cmdline)
        if cwd.name.startswith("otto-clean-"):
            return _looks_like_local_server(cmdline)
        return False
    except (psutil.Error, OSError, RuntimeError):
        return False


def _terminate_pid(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, OSError):
        return
    time.sleep(0.2)
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OSError):
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        return


def cleanup_stale_declared_ports(
    project_dir: Path, logger_fn: Any = None
) -> PortCleanupResult:
    """Kill processes bound to project's declared ports.

    Called once at the start of a pipeline run to clean up zombies from
    prior otto sessions (dev servers, test runners that didn't shut down
    cleanly). Each "port already in use" error inside an agent's session
    burns 30-60 seconds of agent time + tokens diagnosing it; doing one
    cleanup pass up-front saves that across the whole run.

    Reads ports from CHARTER.md (best-effort). Skips silently if CHARTER
    is absent or has no port declarations — the architect hasn't pinned
    ports yet, so there's nothing zombie-able.

    Returns killed/freed/still-bound ports. The object is iterable over
    ``killed_ports`` for compatibility with older callers.
    """
    ports = _parse_declared_ports(project_dir)
    if not ports:
        return PortCleanupResult()

    def log(msg: str) -> None:
        if logger_fn:
            logger_fn(msg)

    pids_before: dict[int, list[int]] = {}
    killed_pids: dict[int, list[int]] = {}
    ports_without_owned_process: list[int] = []
    for port in ports:
        pids = _pids_for_port(port)
        pids_before[port] = pids
        if not pids:
            continue
        owned = [pid for pid in pids if _is_otto_owned_process(pid, project_dir)]
        if not owned:
            ports_without_owned_process.append(port)
            continue
        log(f"port-cleanup: port {port} bound by Otto-owned PIDs {owned}; killing")
        for pid in owned:
            _terminate_pid(pid)
        killed_pids[port] = owned
    still_bound = _check_ports_free(ports)
    pids_after = {port: _pids_for_port(port) for port in still_bound}
    killed_ports = sorted(killed_pids)
    freed_ports = sorted(port for port in killed_ports if port not in still_bound)
    if still_bound:
        log(
            "port-cleanup: declared ports still bound after cleanup: "
            + ", ".join(str(port) for port in still_bound)
        )
    return PortCleanupResult(
        killed_ports=killed_ports,
        freed_ports=freed_ports,
        still_bound_ports=sorted(still_bound),
        killed_pids=killed_pids,
        pids_before=pids_before,
        pids_after=pids_after,
        ports_without_owned_process=sorted(ports_without_owned_process),
    )


def _subtree_verify_start_sh(
    temp_root: Path,
    declared_ports: list[int],
    timeout_s: int,
    port_wait_s: int,
    log: Any,
    after_listening: Callable[[list[int]], None] | None = None,
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
        if after_listening is not None:
            after_listening(sorted(listening))
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
