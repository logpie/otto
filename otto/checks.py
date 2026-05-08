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
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from otto.browser_testing import (
    declared_browser_evidence_missing,
    validate_agent_browser_command,
)
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
        project_dir: Project root (git worktree top). Used for PATH
            augmentation and as fallback context.
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
    browser_env = _browser_journey_env(cwd)
    preflight_error = _browser_journey_preflight(check, cwd, browser_env)
    if preflight_error:
        if raw_log_path is not None:
            _write_raw(raw_log_path, preflight_error)
        return Evidence(
            passed=False,
            started_at=started,
            duration_s=time.monotonic() - t0,
            detail="browser journey preflight failed artifacts=0",
            artifacts=[],
            raw={
                "command": list(check.command),
                "preflight_error": preflight_error,
                "evidence_globs": list(check.evidence_globs),
                "browser_env": browser_env,
            },
        )
    bootstrap = _run_node_bootstrap_if_needed(
        list(check.command), cwd=cwd, timeout_s=check.timeout_s,
        extra_pythonpath=[project_dir, cwd],
        allow_project_bootstrap=True,
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
    browser_lock_wait_s: float | None = None
    if os.environ.get("OTTO_SERIALIZE_BROWSER_JOURNEYS") == "1":
        with _browser_journey_lock() as lock_wait_s:
            browser_lock_wait_s = lock_wait_s
            completed = _run_command(
                list(check.command), cwd=cwd, timeout_s=check.timeout_s,
                extra_pythonpath=[project_dir, cwd],
                env_overrides=browser_env,
            )
    else:
        completed = _run_command(
            list(check.command), cwd=cwd, timeout_s=check.timeout_s,
            extra_pythonpath=[project_dir, cwd],
            env_overrides=browser_env,
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
    artifacts = _collect_evidence_artifacts(cwd, project_dir, check.evidence_globs)
    artifacts = _merge_artifacts(
        artifacts,
        _collect_output_artifacts(completed.stdout or "", cwd=cwd, project_dir=project_dir),
    )
    missing_declared_evidence = declared_browser_evidence_missing(
        returncode=completed.returncode,
        evidence_globs=check.evidence_globs,
        artifact_count=len(artifacts),
    )
    passed = completed.returncode == 0 and not missing_declared_evidence
    detail = f"exit={completed.returncode} artifacts={len(artifacts)}"
    if missing_declared_evidence:
        detail += " missing declared evidence"
    raw = {
        "command": list(check.command),
        "resolved_command": resolved_command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout or "",
        "stderr": completed.stderr or "",
        "evidence_globs": list(check.evidence_globs),
        "missing_declared_evidence": missing_declared_evidence,
        "browser_env": browser_env,
    }
    if browser_lock_wait_s is not None:
        raw["browser_lock_wait_s"] = browser_lock_wait_s
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
        detail=detail,
        artifacts=artifacts,
        raw=raw,
    )


def _browser_journey_preflight(
    check: BrowserJourney,
    cwd: Path,
    browser_env: Mapping[str, str],
) -> str | None:
    agent_browser_error = validate_agent_browser_command(check.command)
    if agent_browser_error:
        return agent_browser_error
    return _playwright_browser_journey_preflight(check, cwd, browser_env)


def _agent_browser_journey_preflight(command: tuple[str, ...] | list[str]) -> str | None:
    return validate_agent_browser_command(command)


def _playwright_browser_journey_preflight(
    check: BrowserJourney,
    cwd: Path,
    browser_env: Mapping[str, str],
) -> str | None:
    """Catch generated Playwright runners that cannot resolve relative routes.

    A repeated I2P failure mode is a BrowserJourney that creates tests with
    ``page.goto("/")`` or ``page.goto("/feature")`` but omits
    ``use.baseURL`` from ``playwright.config.*``. Playwright then fails before
    testing product behavior with "Cannot navigate to invalid URL", causing
    wasteful repair loops. Detect that contract violation before launching the
    browser and return a targeted repair diagnostic.
    """
    command_error = _playwright_command_entrypoint_preflight(check.command, cwd)
    if command_error:
        return command_error
    if not _browser_command_uses_playwright(check.command, cwd):
        return None
    test_paths = _playwright_browser_test_paths(cwd)
    config_paths = _playwright_config_paths(cwd)
    config_error = _playwright_config_artifact_preflight(cwd, config_paths)
    if config_error:
        return config_error
    config_text = "\n".join(_read_text(path) for path in config_paths)
    ignore_error = _playwright_test_ignore_preflight(config_paths, config_text)
    if ignore_error:
        return ignore_error
    test_text = "\n".join(_read_text(path) for path in test_paths)
    package_text = _read_text(cwd / "package.json")
    combined_runner_text = "\n".join([config_text, test_text, package_text])
    if len(test_paths) > 1 and _playwright_command_runs_overbroad_suite(check.command, cwd, test_paths):
        examples = ", ".join(str(path.relative_to(cwd)) for path in test_paths[:3])
        return (
            "Playwright BrowserJourney preflight failed: command appears to run "
            "the full browser suite instead of the intended journey. Affected "
            f"test files include: {examples}. Select one planned journey file "
            "or pass a specific test selector so repairs are targeted and "
            "deterministic."
        )
    if not test_paths:
        return None
    relative_goto_paths = [
        path
        for path in test_paths
        if _file_contains_relative_playwright_goto(path)
    ]
    if not config_paths and relative_goto_paths:
        examples = ", ".join(str(path.relative_to(cwd)) for path in relative_goto_paths[:3])
        return (
            "Playwright BrowserJourney preflight failed: browser tests use "
            f"relative `page.goto(...)` routes but no playwright config file "
            f"was found. Affected test file(s): {examples}. Commit a "
            "`playwright.config.*` with `webServer` and `use.baseURL` that "
            "honors Otto browser env values."
        )
    base_url_declared = any(_file_mentions_base_url(path) for path in [*config_paths, *test_paths])
    if relative_goto_paths and not base_url_declared:
        examples = ", ".join(str(path.relative_to(cwd)) for path in relative_goto_paths[:3])
        return (
            "Playwright BrowserJourney preflight failed: browser tests use relative "
            f"`page.goto(...)` routes but no `baseURL` was found in playwright config "
            f"or test files. Affected test file(s): {examples}. Fix by committing a "
            "`playwright.config.*` with a `webServer` command/url and `use: { baseURL: "
            "\"http://127.0.0.1:<port>\" }`, or change the journey to navigate to an "
            "absolute URL. This is runner configuration, not product evidence."
        )
    if config_paths and "webServer" not in config_text:
        return (
            "Playwright BrowserJourney preflight failed: playwright config has "
            "`baseURL`/tests but no `webServer`. The BrowserJourney must launch "
            "the product server itself so Otto can run it deterministically."
        )
    if config_paths and not _runner_mentions_browser_env(combined_runner_text):
        hardcoded_ports = _hardcoded_loopback_ports(combined_runner_text)
        if hardcoded_ports:
            port_details = ", ".join(str(port) for port in sorted(hardcoded_ports)[:5])
            occupied = sorted(port for port in hardcoded_ports if not _port_available(port))
            occupied_detail = (
                f" Occupied port(s) detected now: {', '.join(str(port) for port in occupied[:5])}."
                if occupied else ""
            )
            return (
                "Playwright BrowserJourney preflight failed: runner hard-codes "
                f"loopback port(s) {port_details} and does not reference "
                "`OTTO_BROWSER_PORT`, `OTTO_BROWSER_BASE_URL`, "
                "`PLAYWRIGHT_BASE_URL`, or `PORT`. Otto assigned "
                f"{browser_env.get('OTTO_BROWSER_BASE_URL')} for this worktree; "
                "the journey must honor that env to avoid concurrent browser "
                f"port conflicts.{occupied_detail}"
            )
    examples = ", ".join(str(path.relative_to(cwd)) for path in relative_goto_paths[:3])
    _ = examples
    return None


def _browser_command_uses_playwright(command: tuple[str, ...] | list[str], cwd: Path) -> bool:
    lowered = [part.lower() for part in command]
    if any("playwright" in part for part in lowered):
        return True
    if len(lowered) >= 3 and lowered[0] in {"npm", "pnpm", "yarn"} and lowered[1] == "run":
        script_name = lowered[2]
        package_json = cwd / "package.json"
        try:
            package_data = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        script = str(package_data.get("scripts", {}).get(script_name, "")).lower()
        return "playwright" in script
    return False


def _playwright_command_entrypoint_preflight(
    command: tuple[str, ...] | list[str],
    cwd: Path,
) -> str | None:
    """Require one canonical npm browser entrypoint for npm Playwright projects.

    Feature agents often write tiny Python BrowserJourney wrappers. If those
    wrappers call ``npx playwright test`` directly, a clean post-merge verifier
    can resolve a different Playwright binary than the project script uses and
    fail with misleading command errors such as ``unknown command 'test'``.
    Route all npm Playwright journeys through the repo's browser script so
    dependency bootstrap, runner flags, and binary resolution stay centralized.
    """
    package_json = cwd / "package.json"
    try:
        package_data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    scripts = package_data.get("scripts", {})
    browser_script = scripts.get("browser") if isinstance(scripts, dict) else None
    if not isinstance(browser_script, str) or "playwright" not in browser_script.lower():
        return None

    joined_command = " ".join(str(part) for part in command).lower()
    direct_command = _contains_direct_playwright_invocation(joined_command)
    wrapper_paths = _browser_command_python_script_paths(command, cwd)
    direct_wrappers = [
        path.relative_to(cwd)
        for path in wrapper_paths
        if _contains_direct_playwright_invocation(_read_text(path).lower())
    ]
    if direct_command or direct_wrappers:
        examples = ", ".join(str(path) for path in direct_wrappers[:3])
        example_detail = f" Affected wrapper(s): {examples}." if examples else ""
        return (
            "Playwright BrowserJourney preflight failed: npm project has a "
            "`browser` script, but the BrowserJourney bypasses it with a direct "
            "`npx playwright test`/`playwright test` invocation. Route browser "
            "journeys through `npm run browser -- <journey-file> --config "
            "playwright.config.*` so dependency bootstrap, binary resolution, "
            f"ports, and runner flags stay centralized.{example_detail}"
        )
    return None


def _contains_direct_playwright_invocation(text: str) -> bool:
    if re.search(
        r"\b(?:npx|pnpm\s+exec|pnpm\s+dlx|yarn\s+dlx)?\s*playwright\s+test\b",
        text,
    ):
        return True
    return bool(
        re.search(
            r"['\"]playwright['\"]\s*,\s*['\"]test['\"]",
            text,
        )
    )


def _browser_command_python_script_paths(
    command: tuple[str, ...] | list[str],
    cwd: Path,
) -> list[Path]:
    if not command:
        return []
    executable = Path(str(command[0])).name.lower()
    if executable not in {"python", "python3", "pytest"}:
        return []
    paths: list[Path] = []
    for raw_part in command[1:]:
        part = str(raw_part)
        if not part.endswith(".py"):
            continue
        path = Path(part)
        if not path.is_absolute():
            path = cwd / path
        if path.is_file():
            paths.append(path)
    return paths


def _playwright_config_paths(cwd: Path) -> list[Path]:
    return sorted(
        path
        for pattern in ("playwright.config.*", "playwright.*.config.*")
        for path in cwd.glob(pattern)
        if path.is_file() and not path.name.endswith(".d.ts")
    )


def _playwright_config_artifact_preflight(cwd: Path, config_paths: list[Path]) -> str | None:
    names = {path.name for path in config_paths}
    ts_configs = {name for name in names if name.endswith((".ts", ".tsx"))}
    js_configs = {name for name in names if name.endswith((".js", ".mjs", ".cjs"))}
    if ts_configs and js_configs:
        return (
            "Playwright BrowserJourney preflight failed: both TypeScript and "
            "JavaScript playwright config files exist in the product root "
            f"({', '.join(sorted(ts_configs | js_configs))}). Playwright may load "
            "a stale generated JS config and report misleading errors such as "
            "`No tests found`. Keep one source config only; if TypeScript build "
            "emitted config JS, add `noEmit: true` for the node/config tsconfig "
            "or exclude runner config from emission, then remove generated "
            "`playwright.config.js`/`.d.ts` artifacts."
        )
    generated_declarations = sorted(path.name for path in cwd.glob("playwright.config*.d.ts"))
    if ts_configs and generated_declarations:
        return (
            "Playwright BrowserJourney preflight failed: generated playwright "
            f"config declaration artifacts exist ({', '.join(generated_declarations)}). "
            "Keep runner config source-only; add `noEmit: true` for the "
            "node/config tsconfig or exclude runner config from emission, then "
            "remove generated `.d.ts` artifacts."
        )
    return None


def _playwright_test_ignore_preflight(config_paths: list[Path], config_text: str) -> str | None:
    if not config_paths or "testIgnore" not in config_text:
        return None
    bare_runtime_ignores = (
        "otto_logs/**",
        "_otto_build_logs/**",
        ".worktrees/**",
        ".otto/**",
        "otto_artifacts/**",
    )
    for pattern in bare_runtime_ignores:
        if re.search(rf"['\"]{re.escape(pattern)}['\"]", config_text):
            examples = ", ".join(path.name for path in config_paths[:3])
            return (
                "Playwright BrowserJourney preflight failed: playwright config "
                f"uses bare runtime ignore `{pattern}` in {examples}. Otto "
                "worktree paths can themselves contain `otto_logs` and "
                "`.worktrees`, so bare or recursive runtime ignores may hide the "
                "entire product checkout and surface as `No tests found`. Use "
                "absolute direct-child ignores based on `process.cwd()` or keep "
                "test discovery narrowly scoped to product tests instead."
            )
    return None


def _playwright_browser_test_paths(cwd: Path) -> list[Path]:
    candidates: list[Path] = []
    for root_name in ("tests/browser", "e2e", "playwright"):
        root = cwd / root_name
        if not root.is_dir():
            continue
        for suffix in ("*.spec.ts", "*.spec.tsx", "*.spec.js", "*.spec.jsx", "*.test.ts", "*.test.js"):
            candidates.extend(path for path in root.rglob(suffix) if path.is_file())
    return sorted(set(candidates))


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _runner_mentions_browser_env(text: str) -> bool:
    return any(
        token in text
        for token in (
            "OTTO_BROWSER_PORT",
            "OTTO_BROWSER_BASE_URL",
            "PLAYWRIGHT_BASE_URL",
            "process.env.PORT",
            "import.meta.env.PORT",
        )
    )


_LOOPBACK_PORT_RE = re.compile(r"(?:127\.0\.0\.1|localhost):(\d{2,5})")


def _hardcoded_loopback_ports(text: str) -> set[int]:
    ports: set[int] = set()
    for raw in _LOOPBACK_PORT_RE.findall(text):
        try:
            port = int(raw)
        except ValueError:
            continue
        if 0 < port < 65536:
            ports.add(port)
    return ports


def _playwright_command_runs_overbroad_suite(
    command: tuple[str, ...] | list[str],
    cwd: Path,
    test_paths: list[Path],
) -> bool:
    lowered = [str(part).lower() for part in command]
    if any(_command_part_selects_test(part, test_paths, cwd) for part in lowered):
        return False
    if len(lowered) >= 3 and lowered[0] in {"npm", "pnpm", "yarn"} and lowered[1] == "run":
        script_name = lowered[2]
        if "--" in lowered and lowered.index("--") < len(lowered) - 1:
            return False
        package_json = cwd / "package.json"
        try:
            package_data = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        script = str(package_data.get("scripts", {}).get(script_name, "")).lower()
        if any(_command_part_selects_test(script, test_paths, cwd) for _ in (0,)):
            return False
        return "playwright" in script
    if "playwright" in lowered and "test" in lowered:
        test_index = lowered.index("test")
        return not any(
            part and not part.startswith("-")
            for part in lowered[test_index + 1 :]
        )
    return False


def _command_part_selects_test(part: str, test_paths: list[Path], cwd: Path) -> bool:
    for path in test_paths:
        rel = str(path.relative_to(cwd)).lower()
        if rel in part or path.name.lower() in part:
            return True
    return False


_RELATIVE_PLAYWRIGHT_GOTO_RE = re.compile(r"\bpage\.goto\(\s*[\"']/(?!/)")


def _file_contains_relative_playwright_goto(path: Path) -> bool:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(_RELATIVE_PLAYWRIGHT_GOTO_RE.search(text))


def _file_mentions_base_url(path: Path) -> bool:
    try:
        return "baseURL" in path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def _browser_journey_env(cwd: Path) -> dict[str, str]:
    port = _allocate_browser_journey_port(cwd)
    base_url = f"http://127.0.0.1:{port}"
    socket_dir = Path(tempfile.gettempdir()) / "otto-agent-browser" / str(port)
    socket_dir.mkdir(parents=True, exist_ok=True)
    return {
        "OTTO_BROWSER_PORT": str(port),
        "OTTO_BROWSER_BASE_URL": base_url,
        "PLAYWRIGHT_BASE_URL": base_url,
        "PORT": str(port),
        "VITE_PORT": str(port),
        "HOST": "127.0.0.1",
        "AGENT_BROWSER_SOCKET_DIR": str(socket_dir),
    }


def _allocate_browser_journey_port(cwd: Path) -> int:
    """Pick a likely-free, per-worktree port for concurrent browser checks."""
    base = 20_000 + (abs(hash(str(cwd.resolve()))) % 20_000)
    for offset in range(200):
        port = base + offset
        if _port_available(port):
            return port
    return _ephemeral_port()


def _port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@contextmanager
def _browser_journey_lock() -> Any:
    """Serialize real browser journey launches across concurrent groups.

    Build agents can edit/test in parallel, but launching several Playwright or
    Chromium sessions at once on a developer Mac repeatedly causes port,
    profile, and Mach/TCC contention. A host-level file lock keeps the proof
    command real while preventing browser-launch conflicts between groups.
    """
    lock_path = Path(tempfile.gettempdir()) / "otto-browser-journey.lock"
    start = time.monotonic()
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - Windows fallback.
            yield 0.0
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield time.monotonic() - start
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


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
        "exists": lambda p: (cwd / p).exists() if not Path(p).is_absolute() else Path(p).exists(),
        "is_file": lambda p: (cwd / p).is_file() if not Path(p).is_absolute() else Path(p).is_file(),
        "is_dir": lambda p: (cwd / p).is_dir() if not Path(p).is_absolute() else Path(p).is_dir(),
        "glob_count": lambda pattern: len(list(cwd.glob(pattern))),
        "read_text": lambda p: ((cwd / p).read_text(encoding="utf-8") if not Path(p).is_absolute() else Path(p).read_text(encoding="utf-8")),
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
    env_overrides: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command list with PATH + venv injection. Returns completed process.

    `extra_pythonpath` (if set) is prepended to PYTHONPATH so the spawned
    process can resolve top-level project modules (e.g. ``from app import …``
    when project layout has flat top-level files). pytest does NOT auto-add
    rootdir without a conftest.py; agents don't reliably create one.

    Raises subprocess.TimeoutExpired on timeout (caller catches in run_check).
    """
    expanded_command = _expand_command_globs(command, cwd)
    resolved_command = _resolve_subprocess_command(expanded_command, cwd, extra_pythonpath)
    return subprocess.run(
        resolved_command,
        cwd=cwd,
        env=_subprocess_env(extra_pythonpath=extra_pythonpath, extra=env_overrides),
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )


def _expand_command_globs(command: list[str], cwd: Path) -> list[str]:
    """Expand path globs in structured subprocess commands.

    Checks run without a shell, but agents naturally test commands in a
    shell. Expanding path-like glob arguments here keeps Otto's authoritative
    check gate aligned with what `npm run test -- src/**/*.test.tsx` means in
    a terminal while still avoiding shell execution.
    """
    if not command:
        return command
    expanded: list[str] = [command[0]]
    for arg in command[1:]:
        matches = _expand_command_glob_arg(arg, cwd)
        expanded.extend(matches if matches else [arg])
    return expanded


def _expand_command_glob_arg(arg: str, cwd: Path) -> list[str]:
    if not _looks_like_path_glob(arg):
        return []
    pattern = arg if Path(arg).is_absolute() else str(cwd / arg)
    matches = sorted(glob.glob(pattern, recursive=True))
    if not matches:
        return []
    if Path(arg).is_absolute():
        return matches
    return [
        os.path.relpath(match, cwd)
        for match in matches
    ]


def _looks_like_path_glob(arg: str) -> bool:
    if not any(ch in arg for ch in "*?["):
        return False
    if arg.startswith("-") or "=" in arg:
        return False
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", arg):
        return False
    return True


def _run_node_bootstrap_if_needed(
    command: list[str],
    *,
    cwd: Path,
    timeout_s: int,
    extra_pythonpath: list[Path] | None = None,
    allow_project_bootstrap: bool = False,
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
    is_npm_command = Path(command[0]).name == "npm"
    if not is_npm_command and not allow_project_bootstrap:
        return None
    if not (cwd / "package.json").exists():
        return None
    if not (cwd / "package-lock.json").exists():
        return None
    node_modules = cwd / "node_modules"
    if node_modules.exists() and (node_modules / ".bin").exists():
        return None
    bootstrap_cmd = ["npm", "ci", "--prefer-offline", "--no-audit", "--no-fund"]
    return _run_command(
        bootstrap_cmd,
        cwd=cwd,
        timeout_s=max(timeout_s, 60),
        extra_pythonpath=extra_pythonpath,
    )


def _subprocess_env(
    extra_pythonpath: list[Path] | None = None,
    *,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
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
    if extra:
        env.update({key: value for key, value in extra.items() if value})
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
