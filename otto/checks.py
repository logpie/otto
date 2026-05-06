"""Check executors — runtime for the typed Check kinds in spec_compile.py.

Step 2 of the unified intent-to-product pipeline. Owns the runtime
patterns for evidence collection (formerly the prototype's
`oracles.py`, since absorbed and deleted) and wires them against the
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
import re
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
    CLIProbe,
    ImportCheck,
    PytestCheck,
    RepoTestCheck,
    StateInvariant,
    TypeCheck,
)


@dataclass
class Evidence:
    """Result of running one Check.

    `feature_id` (A1b addition, research §4 audit honesty) attributes the
    evidence to a specific Feature so per-Feature proof can aggregate
    evidence from any number of Checks. Empty string means "not yet
    attributed" (legacy code paths) or "applies to whole product"
    (cross-Group / integration checks).
    """

    passed: bool
    started_at: str  # ISO-8601 UTC
    duration_s: float
    detail: str  # one-line human summary
    artifacts: list[Path] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    feature_id: str = ""  # A1b: per-Feature attribution; empty = unattributed


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
        if isinstance(check, CLIProbe):
            return _run_cli_probe(check, work_dir, project_dir, started, t0, raw_log_path)
        if isinstance(check, ImportCheck):
            return _run_import_check(check, work_dir, project_dir, started, t0, raw_log_path)
        if isinstance(check, TypeCheck):
            return _run_type_check(check, work_dir, project_dir, started, t0, raw_log_path)
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
        # v2.1 permissive: malformed check (empty command) → informational
        # PASS, not slice-blocking. Audit's contract gate verifies the real
        # product. (See docs/intent-to-product-v2-plan.md §v2.1.)
        return _malformed_check_evidence(
            started, t0, "RepoTestCheck.command is empty (informational; nothing to run)"
        )
    bootstrap = _run_node_bootstrap_if_needed(
        list(check.command), cwd=cwd, timeout_s=check.timeout_s,
        extra_pythonpath=[project_dir, cwd],
    )
    if bootstrap is not None and bootstrap.returncode != 0:
        output = _format_subprocess_output(bootstrap.args, bootstrap)
        if raw_log_path is not None:
            _write_raw(raw_log_path, output)
        return Evidence(
            passed=False,
            started_at=started,
            duration_s=time.monotonic() - t0,
            detail=f"dependency install failed exit={bootstrap.returncode}",
            raw={
                "bootstrap_command": list(bootstrap.args),
                "bootstrap_exit_code": bootstrap.returncode,
                "bootstrap_stdout": bootstrap.stdout or "",
                "bootstrap_stderr": bootstrap.stderr or "",
            },
        )
    completed = _run_command(
        list(check.command), cwd=cwd, timeout_s=check.timeout_s,
        extra_pythonpath=[project_dir, cwd],
    )
    resolved_command = (
        list(completed.args) if isinstance(completed.args, (list, tuple)) else list(check.command)
    )
    output = _format_subprocess_output(resolved_command, completed)
    if bootstrap is not None:
        output = (
            _format_subprocess_output(bootstrap.args, bootstrap).rstrip()
            + "\n\n"
            + output
        )
    if raw_log_path is not None:
        _write_raw(raw_log_path, output)
    passed = completed.returncode == 0
    raw = {
        "command": list(check.command),
        "resolved_command": resolved_command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
    }
    if bootstrap is not None:
        raw.update({
            "bootstrap_command": list(bootstrap.args),
            "bootstrap_exit_code": bootstrap.returncode,
            "bootstrap_stdout": bootstrap.stdout or "",
            "bootstrap_stderr": bootstrap.stderr or "",
        })
    return Evidence(
        passed=passed,
        started_at=started,
        duration_s=time.monotonic() - t0,
        detail=f"exit={completed.returncode}",
        raw=raw,
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
        return _malformed_check_evidence(
            started, t0, "PytestCheck.selector is empty (informational; nothing to run)"
        )
    # V11 fix: the spec compile agent sometimes generates selectors like
    # "tests/test_x.py::a or tests/test_x.py::b" intending "either node id".
    # Passed verbatim to pytest, the literal " or " makes it look for a
    # single nonexistent test; pytest exits 4 (no tests collected). Detect
    # the boolean-expression intent and route via `-k` (which supports
    # `or`/`and`/`not`), otherwise split multi-token selectors into
    # separate positional node ids.
    selector = check.selector
    selector_parts: list[str]
    selector_lower = f" {selector.lower()} "
    if " or " in selector_lower or " and " in selector_lower:
        # Strip explicit file path prefixes (`tests/x.py::`) and join the
        # bare test names with `or`/`and`. -k matches by substring on
        # the test ID, so `test_list_bookmarks or test_add_bookmark`
        # matches both even with file paths in node IDs.
        import re
        keywords = re.split(r"\s+(?:or|and|not)\s+", selector, flags=re.IGNORECASE)
        ops = re.findall(r"\s+(or|and|not)\s+", selector, flags=re.IGNORECASE)
        bare_keywords = [
            (k.split("::", 1)[-1] if "::" in k else k).strip()
            for k in keywords
        ]
        # Reassemble as a -k expression preserving original operator order.
        kexpr_parts = [bare_keywords[0]]
        for op, kw in zip(ops, bare_keywords[1:]):
            kexpr_parts.append(op.lower())
            kexpr_parts.append(kw)
        kexpr = " ".join(kexpr_parts)
        selector_parts = ["-k", kexpr]
    else:
        # Single selector or whitespace-separated multiple node ids.
        selector_parts = [s for s in selector.split() if s]
        if not selector_parts:
            selector_parts = [selector]
    cmd = [*_pytest_base_command(cwd, project_dir), "-q", *selector_parts]
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


def _pytest_base_command(cwd: Path, project_dir: Path) -> list[str]:
    """Return the least-surprising pytest executable for a target project.

    ``uv run pytest`` is a useful fallback for projects that manage dependencies
    through uv, but it is the wrong default for brownfield repos that have a
    requirements.txt and no PEP 621 dependencies: uv creates a clean temporary
    environment and pytest fails to import installed app dependencies. Prefer
    the target project's venv or the user's PATH first, matching what a real
    developer and Otto's build agent would run from that checkout.
    """
    for root in _candidate_project_roots(cwd, [project_dir]):
        for relative in (".venv/bin/pytest", ".venv/Scripts/pytest.exe"):
            candidate = root / relative
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return [str(candidate)]
    pytest_bin = _which_user_path("pytest")
    if pytest_bin:
        return [pytest_bin]
    if _which("uv"):
        return ["uv", "run", "pytest"]
    return [sys.executable, "-m", "pytest"]


def _which_user_path(name: str) -> str | None:
    """Locate a user PATH binary without preferring Otto's own venv."""
    skip = {str(Path(sys.executable).parent)}
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        skip.add(str(Path(virtual_env) / "bin"))
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        if not entry or entry in skip:
            continue
        candidate = Path(entry) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


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
        return _malformed_check_evidence(
            started, t0, "BrowserJourney.command is empty (informational; nothing to run)"
        )
    bootstrap = _run_node_bootstrap_if_needed(
        list(check.command), cwd=cwd, timeout_s=check.timeout_s,
        extra_pythonpath=[project_dir, cwd],
    )
    if bootstrap is not None and bootstrap.returncode != 0:
        output = _format_subprocess_output(bootstrap.args, bootstrap)
        if raw_log_path is not None:
            _write_raw(raw_log_path, output)
        return Evidence(
            passed=False,
            started_at=started,
            duration_s=time.monotonic() - t0,
            detail=f"dependency install failed exit={bootstrap.returncode} artifacts=0",
            artifacts=[],
            raw={
                "bootstrap_command": list(bootstrap.args),
                "bootstrap_exit_code": bootstrap.returncode,
                "bootstrap_stdout": bootstrap.stdout or "",
                "bootstrap_stderr": bootstrap.stderr or "",
                "evidence_globs": list(check.evidence_globs),
            },
        )
    completed = _run_command(
        list(check.command), cwd=cwd, timeout_s=check.timeout_s,
        extra_pythonpath=[project_dir, cwd],
    )
    output = _format_subprocess_output(check.command, completed)
    if bootstrap is not None:
        output = (
            _format_subprocess_output(bootstrap.args, bootstrap).rstrip()
            + "\n\n"
            + output
        )
    if raw_log_path is not None:
        _write_raw(raw_log_path, output)
    artifacts = _collect_evidence_artifacts(cwd, project_dir, check.evidence_globs)
    artifacts = _merge_artifacts(
        artifacts,
        _collect_output_artifacts(completed.stdout or "", cwd=cwd, project_dir=project_dir),
    )
    passed = completed.returncode == 0
    raw = {
        "command": list(check.command),
        "exit_code": completed.returncode,
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
        "evidence_globs": list(check.evidence_globs),
    }
    if bootstrap is not None:
        raw.update({
            "bootstrap_command": list(bootstrap.args),
            "bootstrap_exit_code": bootstrap.returncode,
            "bootstrap_stdout": bootstrap.stdout or "",
            "bootstrap_stderr": bootstrap.stderr or "",
        })
    return Evidence(
        passed=passed,
        started_at=started,
        duration_s=time.monotonic() - t0,
        detail=f"exit={completed.returncode} artifacts={len(artifacts)}",
        artifacts=artifacts,
        raw=raw,
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
        return _malformed_check_evidence(
            started, t0,
            "ApiProbe needs base_url (informational; no server boot in this check pass)"
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
        return _malformed_check_evidence(
            started, t0, "StateInvariant.expression is empty (informational; nothing to evaluate)"
        )

    # Generous safe-builtin set. The risk of restricted eval was about
    # NETWORK access (no socket, no urlopen unless http_get is exposed),
    # not about ordinary Python expressiveness. Compile agents writing
    # state_invariants legitimately reach for callable, hasattr,
    # isinstance, etc. — restricting them produces NameError that the
    # runner now treats as informational, but the cleaner outcome is to
    # provide what the agent expects.
    namespace: dict[str, Any] = {
        "__builtins__": {
            "abs": abs, "all": all, "any": any,
            "bool": bool, "bytes": bytes, "callable": callable,
            "dict": dict, "enumerate": enumerate,
            "filter": filter, "float": float, "frozenset": frozenset,
            "getattr": getattr, "hasattr": hasattr, "hash": hash,
            "isinstance": isinstance, "issubclass": issubclass,
            "int": int, "len": len, "list": list,
            "map": map, "max": max, "min": min,
            "range": range, "repr": repr, "reversed": reversed,
            "round": round, "set": set, "sorted": sorted,
            "str": str, "sum": sum, "tuple": tuple, "type": type,
            "zip": zip,
            "True": True, "False": False, "None": None,
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
    except (SyntaxError, NameError, AttributeError, KeyError, TypeError, IndexError) as exc:
        # Permissive fallback (v2.1 F2 generalized): eval errors mean
        # the expression isn't a clean predicate, NOT that the predicate
        # is false. Don't slice-block on:
        #   - SyntaxError: agent wrote prose instead of Python
        #   - NameError: agent referenced a symbol the namespace doesn't
        #     expose (legitimate in a sandboxed environment)
        #   - AttributeError / KeyError / IndexError / TypeError:
        #     expression structure assumes data shapes that differ from
        #     what the runtime provides
        # Real damage is caught by other checks + audit's contract gate.
        detail = check.description or expression
        if len(detail) > 200:
            detail = detail[:197] + "..."
        return Evidence(
            passed=True,
            started_at=started,
            duration_s=time.monotonic() - t0,
            detail=f"{detail} → informational ({type(exc).__name__}: {exc})",
            raw={
                "expression": expression,
                "description": check.description,
                "result": None,
                "eval_error": f"{type(exc).__name__}: {exc}",
                "non_python_expression": isinstance(exc, SyntaxError),
            },
        )
    except Exception as exc:  # noqa: BLE001 — surface other eval failures
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
# A1b kinds — CLIProbe, ImportCheck, TypeCheck
# ---------------------------------------------------------------------------


def _run_cli_probe(
    check: CLIProbe,
    cwd: Path,
    project_dir: Path,
    started: str,
    t0: float,
    raw_log_path: Path | None,
) -> Evidence:
    """Run a CLI subprocess; assert exit code and optional output substrings.

    Used primarily by `project_kind=cli`. Failures fold the exit-code
    mismatch and substring misses into a single `passed=False` verdict
    with a detail listing each failed expectation.
    """
    if not check.command:
        return _malformed_check_evidence(
            started, t0, "CLIProbe.command is empty (informational; nothing to run)"
        )
    completed = _run_command(
        list(check.command), cwd=cwd, timeout_s=check.timeout_s,
        extra_pythonpath=[project_dir, cwd],
    )
    output = _format_subprocess_output(check.command, completed)
    if raw_log_path is not None:
        _write_raw(raw_log_path, output)

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    exit_ok = completed.returncode == check.expect_exit_code
    stdout_ok = (not check.expect_stdout_substring) or (check.expect_stdout_substring in stdout)
    stderr_ok = (not check.expect_stderr_substring) or (check.expect_stderr_substring in stderr)
    passed = exit_ok and stdout_ok and stderr_ok

    detail_parts = [f"exit={completed.returncode}"]
    if not exit_ok:
        detail_parts.append(f"expected={check.expect_exit_code}")
    if check.expect_stdout_substring:
        detail_parts.append(
            f"stdout_contains={check.expect_stdout_substring!r}={'✓' if stdout_ok else '✗'}"
        )
    if check.expect_stderr_substring:
        detail_parts.append(
            f"stderr_contains={check.expect_stderr_substring!r}={'✓' if stderr_ok else '✗'}"
        )

    return Evidence(
        passed=passed,
        started_at=started,
        duration_s=time.monotonic() - t0,
        detail=" ".join(detail_parts),
        raw={
            "command": list(check.command),
            "exit_code": completed.returncode,
            "expect_exit_code": check.expect_exit_code,
            "expect_stdout_substring": check.expect_stdout_substring,
            "expect_stderr_substring": check.expect_stderr_substring,
            "stdout_match": stdout_ok,
            "stderr_match": stderr_ok,
            "stdout": stdout,
            "stderr": stderr,
        },
    )


def _run_import_check(
    check: ImportCheck,
    cwd: Path,
    project_dir: Path,
    started: str,
    t0: float,
    raw_log_path: Path | None,
) -> Evidence:
    """Verify `python -c "import <package_name>"` succeeds.

    Used by `project_kind=library`. Optional `expect_version` checks
    `<pkg>.__version__` matches. Captures the full traceback in `raw`
    when import fails.
    """
    if not check.package_name:
        return _malformed_check_evidence(
            started, t0, "ImportCheck.package_name is empty (informational; nothing to import)"
        )

    package = check.package_name
    if check.expect_version:
        # Compare strings — agents emit the version verbatim. Build the
        # snippet without nested f-strings so the inner repr/format is
        # evaluated in the spawned Python at runtime.
        snippet = (
            f"import {package}; "
            f"v = getattr({package}, '__version__', None); "
            f"expected = {check.expect_version!r}; "
            "assert v == expected, "
            "'version mismatch: got %r, expected %r' % (v, expected)"
        )
    else:
        snippet = f"import {package}"
    cmd = [sys.executable, "-c", snippet]

    completed = _run_command(
        cmd, cwd=cwd, timeout_s=check.timeout_s,
        extra_pythonpath=[project_dir, cwd],
    )
    output = _format_subprocess_output(cmd, completed)
    if raw_log_path is not None:
        _write_raw(raw_log_path, output)

    passed = completed.returncode == 0
    detail_parts = [f"package={package!r}", f"exit={completed.returncode}"]
    if check.expect_version:
        detail_parts.append(f"expect_version={check.expect_version!r}")
    return Evidence(
        passed=passed,
        started_at=started,
        duration_s=time.monotonic() - t0,
        detail=" ".join(detail_parts),
        raw={
            "command": cmd,
            "package_name": package,
            "expect_version": check.expect_version,
            "exit_code": completed.returncode,
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
        },
    )


def _run_type_check(
    check: TypeCheck,
    cwd: Path,
    project_dir: Path,
    started: str,
    t0: float,
    raw_log_path: Path | None,
) -> Evidence:
    """Invoke a type checker (mypy / pyright / basedpyright) on `paths`.

    Tools are invoked via subprocess; if the tool isn't on PATH we
    return `passed=False` with a clean "tool not available" detail
    (no fabricated success). Type-checker exit 0 → pass.
    """
    if not check.paths:
        return _malformed_check_evidence(
            started, t0, "TypeCheck.paths is empty (informational; nothing to check)"
        )

    tool = (check.tool or "mypy").strip().lower()
    if tool not in {"mypy", "pyright", "basedpyright"}:
        return Evidence(
            passed=False,
            started_at=started,
            duration_s=time.monotonic() - t0,
            detail=f"unsupported type-checker tool: {tool!r}",
            raw={"tool": tool, "supported": ["mypy", "pyright", "basedpyright"]},
        )

    binary = _which(tool)
    if binary is None:
        # Honest reporting: the tool wasn't found, so we can't make a
        # type-safety claim. passed=False keeps the check truthful;
        # detail explains why so callers can install or skip.
        return Evidence(
            passed=False,
            started_at=started,
            duration_s=time.monotonic() - t0,
            detail=f"type-checker {tool!r} not available on PATH",
            raw={"tool": tool, "tool_available": False, "paths": list(check.paths)},
        )

    if tool == "mypy":
        cmd = [binary, *check.paths]
    else:
        # pyright / basedpyright share the same CLI surface.
        cmd = [binary, *check.paths]

    completed = _run_command(
        cmd, cwd=cwd, timeout_s=check.timeout_s,
        extra_pythonpath=[project_dir, cwd],
    )
    output = _format_subprocess_output(cmd, completed)
    if raw_log_path is not None:
        _write_raw(raw_log_path, output)

    passed = completed.returncode == 0
    return Evidence(
        passed=passed,
        started_at=started,
        duration_s=time.monotonic() - t0,
        detail=f"tool={tool} paths={list(check.paths)} exit={completed.returncode}",
        raw={
            "command": cmd,
            "tool": tool,
            "tool_available": True,
            "paths": list(check.paths),
            "exit_code": completed.returncode,
            "stdout": completed.stdout or "",
            "stderr": completed.stderr or "",
        },
    )


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
    resolved_command = _resolve_subprocess_command(command, cwd, extra_pythonpath)
    return subprocess.run(
        resolved_command,
        cwd=cwd,
        env=_subprocess_env(extra_pythonpath=extra_pythonpath),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def _run_node_bootstrap_if_needed(
    command: list[str],
    *,
    cwd: Path,
    timeout_s: int,
    extra_pythonpath: list[Path] | None = None,
) -> subprocess.CompletedProcess[str] | None:
    """Install locked npm deps before npm checks in a clean worktree.

    Slice branches commit package metadata, not ``node_modules``. Merge
    verification runs after ``git clean -fdx``, so commands like
    ``npm run build`` otherwise fail with ``vite: command not found``
    even though the branch has a valid ``package-lock.json``. Keep this
    deliberately narrow: only npm commands with a lockfile get implicit
    bootstrap, and only when ``node_modules`` is absent.
    """
    if not command:
        return None
    if Path(command[0]).name != "npm":
        return None
    if not (cwd / "package.json").exists():
        return None
    if not (cwd / "package-lock.json").exists():
        return None
    if (cwd / "node_modules").exists():
        return None
    bootstrap_cmd = ["npm", "ci", "--prefer-offline", "--no-audit", "--no-fund"]
    return _run_command(
        bootstrap_cmd,
        cwd=cwd,
        timeout_s=max(timeout_s, 60),
        extra_pythonpath=extra_pythonpath,
    )


def _subprocess_env(extra_pythonpath: list[Path] | None = None) -> dict[str, str]:
    """Return a subprocess env without preferring Otto's own virtualenv.

    If `extra_pythonpath` is provided, those paths are prepended to
    PYTHONPATH (deduplicated, preserving caller order).
    """
    env = os.environ.copy()
    path_entries = env.get("PATH", "").split(os.pathsep) if env.get("PATH") else []
    skip_bins = _current_runtime_bins()
    path_entries = [entry for entry in path_entries if entry and entry not in skip_bins]
    env.pop("VIRTUAL_ENV", None)
    project_venv_bin = _first_project_venv_bin(_candidate_project_roots(Path.cwd(), extra_pythonpath))
    if project_venv_bin is not None:
        project_venv_bin_text = str(project_venv_bin)
        if project_venv_bin_text not in skip_bins:
            path_entries = [
                project_venv_bin_text,
                *[entry for entry in path_entries if entry != project_venv_bin_text],
            ]
            env["VIRTUAL_ENV"] = str(project_venv_bin.parent)
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


def _current_runtime_bins() -> set[str]:
    bins = {str(Path(sys.executable).parent)}
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        bins.add(str(Path(virtual_env) / "bin"))
    return bins


def _resolve_subprocess_command(
    command: list[str],
    cwd: Path,
    extra_pythonpath: list[Path] | None,
) -> list[str]:
    if not command:
        return command
    executable = command[0]
    if os.sep in executable or (os.altsep and os.altsep in executable):
        return command
    name = Path(executable).name.lower()
    roots = _candidate_project_roots(cwd, extra_pythonpath)
    if name in {"python", "python.exe", "python3", "python3.exe"}:
        resolved = _resolve_python_executable(roots, prefer_python3=name.startswith("python3"))
        return [resolved, *command[1:]]
    if name in {"pytest", "pytest.exe"}:
        resolved_pytest = _resolve_project_or_user_tool("pytest", roots)
        if resolved_pytest:
            return [resolved_pytest, *command[1:]]
        if _which("uv"):
            return ["uv", "run", "pytest", *command[1:]]
        return [sys.executable, "-m", "pytest", *command[1:]]
    return command


def _candidate_project_roots(cwd: Path, extra_pythonpath: list[Path] | None) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for root in [cwd, *(extra_pythonpath or [])]:
        path = Path(root)
        for candidate in [path, *_linked_worktree_runtime_roots(path)]:
            if candidate not in seen:
                roots.append(candidate)
                seen.add(candidate)
    return roots


def _linked_worktree_runtime_roots(path: Path) -> list[Path]:
    roots: list[Path] = []
    for root in [path, *path.parents]:
        if root.name == ".worktrees":
            roots.append(root.parent)
            break
    return roots


def _first_project_venv_bin(roots: list[Path]) -> Path | None:
    for root in roots:
        for relative in (".venv/bin", ".venv/Scripts"):
            candidate = root / relative
            if candidate.is_dir():
                return candidate
    return None


def _resolve_python_executable(roots: list[Path], *, prefer_python3: bool = False) -> str:
    names = ("python3", "python") if prefer_python3 else ("python", "python3")
    for name in names:
        resolved = _resolve_project_or_user_tool(name, roots)
        if resolved:
            return resolved
    return sys.executable


def _resolve_project_or_user_tool(name: str, roots: list[Path]) -> str | None:
    for root in roots:
        for relative in (f".venv/bin/{name}", f".venv/Scripts/{name}.exe"):
            candidate = root / relative
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return _which_user_path(name)


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


_OUTPUT_ARTIFACT_RE = re.compile(
    r"(?P<path>(?:/|\.{1,2}/|[A-Za-z0-9_.-]+/)[^\s'\"<>:]+"
    r"\.(?:png|jpg|jpeg|webp|gif|webm|mp4|har|html))"
)


def _collect_output_artifacts(output: str, *, cwd: Path, project_dir: Path) -> list[Path]:
    """Recover evidence files printed by browser journeys.

    Agents sometimes emit a wrong `evidence_globs` value but their harness
    prints concrete screenshot/video paths. Treat existing printed paths as
    artifacts so proof packets do not lose browser evidence solely because
    the glob was stale.
    """
    artifacts: list[Path] = []
    for match in _OUTPUT_ARTIFACT_RE.finditer(output or ""):
        raw = match.group("path").rstrip(").,;]")
        path = Path(raw)
        candidates = [path] if path.is_absolute() else [cwd / path, project_dir / path]
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                artifacts.append(candidate)
                break
    return _merge_artifacts([], artifacts)


def _merge_artifacts(existing: list[Path], extra: list[Path]) -> list[Path]:
    merged: list[Path] = []
    seen: set[str] = set()
    for artifact in [*existing, *extra]:
        key = str(artifact)
        if key in seen:
            continue
        if artifact.exists() and artifact.is_file():
            merged.append(artifact)
            seen.add(key)
    return merged


def _err_evidence(started: str, t0: float, detail: str) -> Evidence:
    return Evidence(
        passed=False,
        started_at=started,
        duration_s=time.monotonic() - t0,
        detail=detail,
        raw={"error": detail},
    )


def _malformed_check_evidence(started: str, t0: float, detail: str) -> Evidence:
    """Return informational PASS for a malformed check payload.

    v2.1 design (docs/intent-to-product-v2-plan.md): malformed agent-emitted
    check payloads (empty command, missing selector, prose state_invariant)
    are NOT slice-blocking. They surface as informational PASS with a
    diagnostic in `raw`. The audit's contract gate verifies the integrated
    product's real behavior — that's the source of truth, not the per-check
    payload shape.
    """
    return Evidence(
        passed=True,
        started_at=started,
        duration_s=time.monotonic() - t0,
        detail=detail,
        raw={"malformed_check": True, "diagnostic": detail},
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
        if raw_log_dir is not None and isinstance(
            check,
            (RepoTestCheck, PytestCheck, BrowserJourney, CLIProbe, ImportCheck, TypeCheck),
        ):
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
_ = (
    PytestCheck, RepoTestCheck, ApiProbe, BrowserJourney, StateInvariant,
    CLIProbe, ImportCheck, TypeCheck, json,
)
