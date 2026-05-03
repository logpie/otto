"""Check executors — runtime for the typed Check kinds in spec_compile.py.

Step 2 of the unified intent-to-product pipeline. Ports
codex-i2p/otto/oracles.py runtime patterns and rewires them against the
typed Check dataclasses defined in `otto.spec_compile`.

A check executor takes one Check, runs it, and returns Evidence:

    evidence = run_check(check, project_dir=..., cwd=..., base_url=...)

`Evidence.passed` is the binary verdict; `detail` is a one-line human
summary; `artifacts` is the list of evidence files collected (screenshots,
logs, response dumps); `raw` is the check-specific structured output for
machine consumers.

Errors during execution become `passed=False` with `detail` describing the
failure (no exception escapes `run_check`). Timeouts become `passed=False`
with detail "timed out after Ns".
"""

from __future__ import annotations

import builtins
import glob
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from otto.observability import iso_timestamp, write_text_atomic
from otto.spec_compile import (
    ApiProbe,
    BrowserJourney,
    CheckKind,
    PytestCheck,
    RepoTestCheck,
    StateInvariant,
)


@dataclass
class Evidence:
    """Result of running one Check."""

    passed: bool
    started_at: str  # ISO-8601 UTC
    duration_s: float
    detail: str  # one-line human summary
    artifacts: list[Path] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def run_check(
    check: CheckKind,
    *,
    project_dir: Path,
    cwd: Path | None = None,
    base_url: str | None = None,
    raw_log_path: Path | None = None,
) -> Evidence:
    """Execute a single check and return Evidence.

    Args:
        check: The typed CheckKind from a Spec.
        project_dir: Project root (git worktree top). Used for path resolution
            and PATH augmentation.
        cwd: Working directory for subprocess checks. Defaults to project_dir.
        base_url: Base URL for ApiProbe and StateInvariant checks that probe
            a running app (e.g., "http://localhost:5173"). Required for
            ApiProbe; optional for StateInvariant.
        raw_log_path: If provided, write raw stdout/stderr to this path for
            audit/debugging.

    Returns:
        Evidence with `passed`, `detail`, `artifacts`, and `raw` populated.
        Never raises — failures become `passed=False`.
    """
    started = iso_timestamp()
    t0 = time.monotonic()
    work_dir = Path(cwd) if cwd is not None else Path(project_dir)

    try:
        if isinstance(check, RepoTestCheck):
            return _run_repo_test(check, work_dir, project_dir, started, t0, raw_log_path)
        if isinstance(check, PytestCheck):
            return _run_pytest(check, work_dir, project_dir, started, t0, raw_log_path)
        if isinstance(check, BrowserJourney):
            return _run_browser_journey(check, work_dir, project_dir, started, t0, raw_log_path)
        if isinstance(check, ApiProbe):
            return _run_api_probe(check, base_url, started, t0)
        if isinstance(check, StateInvariant):
            return _run_state_invariant(check, project_dir, work_dir, base_url, started, t0)
    except subprocess.TimeoutExpired as exc:
        return Evidence(
            passed=False,
            started_at=started,
            duration_s=time.monotonic() - t0,
            detail=f"timed out after {getattr(check, 'timeout_s', 0)}s",
            raw={
                "timeout": True,
                "stdout": (exc.stdout or "") if isinstance(exc.stdout, str) else "",
                "stderr": (exc.stderr or "") if isinstance(exc.stderr, str) else "",
            },
        )
    except Exception as exc:
        return Evidence(
            passed=False,
            started_at=started,
            duration_s=time.monotonic() - t0,
            detail=f"{type(exc).__name__}: {exc}",
        )

    return Evidence(
        passed=False,
        started_at=started,
        duration_s=time.monotonic() - t0,
        detail=f"unsupported check kind: {type(check).__name__}",
    )


# ---------------------------------------------------------------------------
# Subprocess-driven kinds (RepoTestCheck, PytestCheck, BrowserJourney)
# ---------------------------------------------------------------------------


def _run_repo_test(
    check: RepoTestCheck,
    cwd: Path,
    project_dir: Path,
    started: str,
    t0: float,
    raw_log_path: Path | None,
) -> Evidence:
    if not check.command:
        return _err_evidence(started, t0, "RepoTestCheck.command is empty")
    completed = _run_command(
        list(check.command), cwd=cwd, timeout_s=check.timeout_s,
        extra_pythonpath=[project_dir, cwd],
    )
    output = _format_subprocess_output(check.command, completed)
    if raw_log_path is not None:
        _write_raw(raw_log_path, output)
    passed = completed.returncode == 0
    return Evidence(
        passed=passed,
        started_at=started,
        duration_s=time.monotonic() - t0,
        detail=f"exit={completed.returncode}",
        raw={
            "command": list(check.command),
            "exit_code": completed.returncode,
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
        },
    )


def _run_pytest(
    check: PytestCheck,
    cwd: Path,
    project_dir: Path,
    started: str,
    t0: float,
    raw_log_path: Path | None,
) -> Evidence:
    if not check.selector:
        return _err_evidence(started, t0, "PytestCheck.selector is empty")
    # Prefer `uv run pytest` if uv exists; fall back to interpreter.
    if _which("uv"):
        cmd = ["uv", "run", "pytest", "-q", check.selector]
    else:
        cmd = [sys.executable, "-m", "pytest", "-q", check.selector]
    # Project-root layouts (e.g. flat `app.py` + `tests/test_x.py`) need the
    # project_dir on PYTHONPATH or `from app import …` fails at collect time.
    # `pytest` itself only auto-adds rootdir if a conftest.py is present;
    # build agents don't reliably create one. Make the import path
    # predictable here.
    completed = _run_command(
        cmd, cwd=cwd, timeout_s=check.timeout_s, extra_pythonpath=[project_dir, cwd]
    )
    output = _format_subprocess_output(cmd, completed)
    if raw_log_path is not None:
        _write_raw(raw_log_path, output)
    passed = completed.returncode == 0
    return Evidence(
        passed=passed,
        started_at=started,
        duration_s=time.monotonic() - t0,
        detail=f"selector={check.selector!r} exit={completed.returncode}",
        raw={
            "command": cmd,
            "selector": check.selector,
            "exit_code": completed.returncode,
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
        },
    )


def _run_browser_journey(
    check: BrowserJourney,
    cwd: Path,
    project_dir: Path,
    started: str,
    t0: float,
    raw_log_path: Path | None,
) -> Evidence:
    """Subprocess + glob.

    The check's `command` runs a Playwright/Puppeteer harness (or any
    runner that drives a browser); we don't own the browser session. After
    the subprocess exits, we glob `evidence_globs` (relative to cwd or
    absolute) to collect screenshots/videos/HARs as artifacts.
    """
    if not check.command:
        return _err_evidence(started, t0, "BrowserJourney.command is empty")
    completed = _run_command(
        list(check.command), cwd=cwd, timeout_s=check.timeout_s,
        extra_pythonpath=[project_dir, cwd],
    )
    output = _format_subprocess_output(check.command, completed)
    if raw_log_path is not None:
        _write_raw(raw_log_path, output)
    artifacts = _collect_evidence_artifacts(cwd, project_dir, check.evidence_globs)
    passed = completed.returncode == 0
    return Evidence(
        passed=passed,
        started_at=started,
        duration_s=time.monotonic() - t0,
        detail=f"exit={completed.returncode} artifacts={len(artifacts)}",
        artifacts=artifacts,
        raw={
            "command": list(check.command),
            "exit_code": completed.returncode,
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
            "evidence_globs": list(check.evidence_globs),
        },
    )


# ---------------------------------------------------------------------------
# Network-driven kinds (ApiProbe, StateInvariant)
# ---------------------------------------------------------------------------


def _run_api_probe(
    check: ApiProbe,
    base_url: str | None,
    started: str,
    t0: float,
) -> Evidence:
    if not base_url:
        return _err_evidence(
            started, t0, "ApiProbe needs base_url; pass it to run_check(base_url=...)"
        )
    url = base_url.rstrip("/") + (check.path if check.path.startswith("/") else "/" + check.path)
    method = (check.method or "GET").upper()
    request = urllib.request.Request(url, method=method)
    status_code = 0
    response_text = ""
    try:
        with urllib.request.urlopen(request, timeout=check.timeout_s) as response:
            status_code = int(response.status)
            response_text = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
        try:
            response_text = exc.read().decode("utf-8", errors="replace")
        except Exception:
            response_text = ""
    except urllib.error.URLError as exc:
        return Evidence(
            passed=False,
            started_at=started,
            duration_s=time.monotonic() - t0,
            detail=f"connection error: {exc.reason}",
            raw={"url": url, "method": method, "error": str(exc.reason)},
        )

    body_match_ok = True
    if check.expect_body_contains:
        body_match_ok = check.expect_body_contains in response_text

    passed = status_code == check.expect_status and body_match_ok
    detail_parts = [f"{method} {url}", f"status={status_code}"]
    if check.expect_body_contains:
        detail_parts.append(
            f"body_contains={check.expect_body_contains!r}={'✓' if body_match_ok else '✗'}"
        )
    return Evidence(
        passed=passed,
        started_at=started,
        duration_s=time.monotonic() - t0,
        detail=" ".join(detail_parts),
        raw={
            "url": url,
            "method": method,
            "status_code": status_code,
            "expect_status": check.expect_status,
            "expect_body_contains": check.expect_body_contains,
            "response_text": response_text[:8192],  # cap to keep raw small
        },
    )


def _run_state_invariant(
    check: StateInvariant,
    project_dir: Path,
    cwd: Path,
    base_url: str | None,
    started: str,
    t0: float,
) -> Evidence:
    """Evaluate `check.expression` as a Python boolean expression.

    Restricted namespace exposes filesystem helpers only; no network in v1
    unless `base_url` is set, in which case `http_get(path)` is available.
    Network access is deliberately limited — state invariants should
    primarily be repo-state predicates (file existence, exclusivity).
    """
    expression = (check.expression or "").strip()
    if not expression:
        return _err_evidence(started, t0, "StateInvariant.expression is empty")

    namespace: dict[str, Any] = {
        "__builtins__": {
            "abs": abs,
            "all": all,
            "any": any,
            "bool": bool,
            "dict": dict,
            "float": float,
            "int": int,
            "len": len,
            "list": list,
            "max": max,
            "min": min,
            "set": set,
            "sorted": sorted,
            "str": str,
            "sum": sum,
            "tuple": tuple,
            "True": True,
            "False": False,
            "None": None,
        },
        "project_dir": project_dir,
        "cwd": cwd,
        "Path": Path,
        "exists": lambda p: (project_dir / p).exists() if not Path(p).is_absolute() else Path(p).exists(),
        "is_file": lambda p: (project_dir / p).is_file() if not Path(p).is_absolute() else Path(p).is_file(),
        "is_dir": lambda p: (project_dir / p).is_dir() if not Path(p).is_absolute() else Path(p).is_dir(),
        "glob_count": lambda pattern: len(list(project_dir.glob(pattern))),
        "read_text": lambda p: ((project_dir / p).read_text(encoding="utf-8") if not Path(p).is_absolute() else Path(p).read_text(encoding="utf-8")),
    }
    if base_url:
        namespace["http_get"] = lambda path: _safe_http_get(base_url, path, timeout=check.timeout_s)

    try:
        # eval in restricted namespace; expression is project-author-controlled.
        # Compile separately to surface SyntaxError as a clearer detail.
        code = builtins.compile(expression, "<state_invariant>", "eval")
        result = eval(code, namespace, {})
    except SyntaxError as exc:
        return _err_evidence(started, t0, f"SyntaxError in expression: {exc.msg}")
    except Exception as exc:  # noqa: BLE001 — surface eval failures
        return _err_evidence(started, t0, f"{type(exc).__name__}: {exc}")

    passed = bool(result)
    detail = check.description or expression
    if len(detail) > 200:
        detail = detail[:197] + "..."
    return Evidence(
        passed=passed,
        started_at=started,
        duration_s=time.monotonic() - t0,
        detail=f"{detail} → {passed}",
        raw={
            "expression": expression,
            "description": check.description,
            "result": passed,
            "raw_result_repr": repr(result),
        },
    )


def _safe_http_get(base_url: str, path: str, *, timeout: int) -> dict[str, Any]:
    url = base_url.rstrip("/") + (path if path.startswith("/") else "/" + path)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return {
                "status": int(response.status),
                "text": response.read().decode("utf-8", errors="replace"),
            }
    except urllib.error.HTTPError as exc:
        try:
            text = exc.read().decode("utf-8", errors="replace")
        except Exception:
            text = ""
        return {"status": int(exc.code), "text": text}
    except urllib.error.URLError as exc:
        return {"status": 0, "error": str(exc.reason)}


# ---------------------------------------------------------------------------
# Subprocess + evidence helpers
# ---------------------------------------------------------------------------


def _run_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_s: int,
    extra_pythonpath: list[Path] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command list with PATH + venv injection. Returns completed process.

    `extra_pythonpath` (if set) is prepended to PYTHONPATH so the spawned
    process can resolve top-level project modules (e.g. ``from app import …``
    when project layout has flat top-level files). pytest does NOT auto-add
    rootdir without a conftest.py; agents don't reliably create one.

    Raises subprocess.TimeoutExpired on timeout (caller catches in run_check).
    """
    return subprocess.run(
        command,
        cwd=cwd,
        env=_subprocess_env(extra_pythonpath=extra_pythonpath),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def _subprocess_env(extra_pythonpath: list[Path] | None = None) -> dict[str, str]:
    """Augment env with interpreter bin and active venv bin on PATH.

    If `extra_pythonpath` is provided, those paths are prepended to
    PYTHONPATH (deduplicated, preserving caller order).
    """
    env = os.environ.copy()
    path_entries = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    interpreter_bin = str(Path(sys.executable).parent)
    if interpreter_bin and interpreter_bin not in path_entries:
        path_entries.insert(0, interpreter_bin)
    virtual_env = env.get("VIRTUAL_ENV")
    if virtual_env:
        venv_bin = str(Path(virtual_env) / "bin")
        if venv_bin not in path_entries:
            path_entries.insert(0, venv_bin)
    if path_entries:
        env["PATH"] = os.pathsep.join(path_entries)
    if extra_pythonpath:
        existing = env.get("PYTHONPATH", "").split(os.pathsep) if env.get("PYTHONPATH") else []
        prepend = [str(p) for p in extra_pythonpath if str(p)]
        seen: set[str] = set()
        merged: list[str] = []
        for entry in [*prepend, *existing]:
            if entry and entry not in seen:
                merged.append(entry)
                seen.add(entry)
        env["PYTHONPATH"] = os.pathsep.join(merged)
    return env


def _format_subprocess_output(
    command: list[str] | tuple[str, ...],
    completed: subprocess.CompletedProcess[str],
) -> str:
    return (
        f"$ {' '.join(command)}\n"
        f"exit_code={completed.returncode}\n\n"
        f"STDOUT:\n{completed.stdout or ''}\n"
        f"STDERR:\n{completed.stderr or ''}"
    )


def _write_raw(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, content.rstrip() + "\n")


def _which(name: str) -> str | None:
    """Locate a binary on PATH, after augmentation."""
    env_path = _subprocess_env().get("PATH", "")
    for entry in env_path.split(os.pathsep):
        if not entry:
            continue
        candidate = Path(entry) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _collect_evidence_artifacts(
    cwd: Path,
    project_dir: Path,
    evidence_globs: tuple[str, ...] | list[str],
) -> list[Path]:
    """Resolve evidence globs to concrete file paths.

    Globs starting with `otto_logs/` are resolved against `project_dir`;
    absolute globs are passed through; everything else is resolved against
    `cwd`. Files only (no directories). Deduplicated, sorted.
    """
    artifacts: list[Path] = []
    seen: set[str] = set()
    for raw in evidence_globs:
        text = str(raw or "").strip()
        if not text:
            continue
        candidates: list[Path]
        if Path(text).is_absolute():
            candidates = [Path(p) for p in sorted(glob.glob(text))]
        elif text.startswith("otto_logs/"):
            candidates = sorted(project_dir.glob(text))
        else:
            candidates = sorted(cwd.glob(text))
        for candidate in candidates:
            resolved = str(candidate)
            if candidate.exists() and candidate.is_file() and resolved not in seen:
                artifacts.append(candidate)
                seen.add(resolved)
    return artifacts


def _err_evidence(started: str, t0: float, detail: str) -> Evidence:
    return Evidence(
        passed=False,
        started_at=started,
        duration_s=time.monotonic() - t0,
        detail=detail,
        raw={"error": detail},
    )


# ---------------------------------------------------------------------------
# Convenience: run a list of checks; collect evidence
# ---------------------------------------------------------------------------


def run_checks(
    checks: list[CheckKind],
    *,
    project_dir: Path,
    cwd: Path | None = None,
    base_url: str | None = None,
    raw_log_dir: Path | None = None,
) -> list[tuple[CheckKind, Evidence]]:
    """Run a sequence of checks; return (check, evidence) pairs.

    If `raw_log_dir` is set, writes one raw output file per subprocess-driven
    check, named `<index>-<kind>.log`.
    """
    results: list[tuple[CheckKind, Evidence]] = []
    for index, check in enumerate(checks):
        raw_log_path: Path | None = None
        if raw_log_dir is not None and isinstance(check, (RepoTestCheck, PytestCheck, BrowserJourney)):
            raw_log_path = raw_log_dir / f"{index:03d}-{type(check).__name__}.log"
        evidence = run_check(
            check,
            project_dir=project_dir,
            cwd=cwd,
            base_url=base_url,
            raw_log_path=raw_log_path,
        )
        results.append((check, evidence))
    return results


# Public API
__all__ = [
    "Evidence",
    "run_check",
    "run_checks",
]


# Suppress unused-import warning — these names are part of the typed
# CheckKind union we dispatch on; importing them pins the module-level
# dependency.
_ = (PytestCheck, RepoTestCheck, ApiProbe, BrowserJourney, StateInvariant, json)
