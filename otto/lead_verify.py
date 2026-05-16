"""Real verifier for v5 Leads.

Plan-v5 §1.4: the verifier is the only PASS gate. The Lead's claim of
"pass" is downgraded to "unverified" if no verify call ran; even if it ran,
verdict is computed from the verifier's output, not the Lead's text.

This module:
  1. Reads the FROZEN behavior_journeys from the session's spec.json.
  2. Runs the project's native test suite (auto-detected via
     ``otto.config.detect_test_command``) against the worktree.
  3. If a browser journey runner exists at the conventional path
     (``tests/run_browser_journey.py`` or ``npm run browser``), invokes it
     against the running product.
  4. Maps test/journey results back to behavior_journey ids to produce
     per-journey pass/fail.
  5. Returns structured result the Lead reads (and Otto's render layer
     persists into the proof packet).

This is the **truth gate** — never trusts Lead self-attestation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal

from otto.journey_api_executor import run_api_journey_executor
from otto.journey_scope_policy import ExecutionScope, applicability_for
from otto.journey_verdict_sink import failed_journey_ids, resolve_journey_verdicts
from otto.observability import iso_timestamp
from otto.spec_compile_flat import StructuredSpecValidationError, load_flat_spec

logger = logging.getLogger("otto.lead_verify")


VerifyVerdict = Literal["pass", "partial", "unverified"]


async def run_verify_for_lead(
    *,
    task_id: str,
    project_dir: Path,
    session_dir: Path,
    feature_scope_ids: list[str],
    execution_scope: ExecutionScope = "leaf",
    timeout_s: int = 600,
) -> dict[str, Any]:
    """Run real verification against the running product.

    Best-effort: any subprocess crash, missing test runner, or timeout
    becomes verdict=unverified with the reason recorded. Never crashes Otto.
    """
    started = time.monotonic()
    log_dir = session_dir / "verify"
    log_dir.mkdir(parents=True, exist_ok=True)

    # If this Lead has a worktree (`<session_dir>/worktree` symlink, set up
    # by v5_runner._run_child), verify against the worktree. Otherwise verify
    # against project_dir directly. This matters because child Leads write
    # into their per-task worktree, not into project_dir.
    verify_dir = project_dir
    worktree_link = session_dir / "worktree"
    try:
        if worktree_link.is_symlink() or worktree_link.is_dir():
            target = worktree_link.resolve()
            if target.exists():
                verify_dir = target
    except OSError:
        pass

    spec = _load_flat_spec_for_session(session_dir)
    if spec is None:
        return _unverified("no flat spec at session_dir/spec/spec.json")

    journeys_in_scope = _filter_journeys(
        spec.get("behavior_journeys") or [], feature_scope_ids
    )
    if not journeys_in_scope:
        return _unverified(
            f"no behavior journeys in scope (asked for {feature_scope_ids!r}, "
            f"spec has {len(spec.get('behavior_journeys') or [])} total)"
        )

    evidence: list[str] = []

    # ---- Layer 1: native test runner (npm test / pytest / cargo test / ...) ----
    test_outcome = await _run_native_tests(
        project_dir=verify_dir, log_dir=log_dir, timeout_s=timeout_s,
    )
    if test_outcome["log_path"]:
        evidence.append(str(test_outcome["log_path"]))

    # ---- Layer 2: browser journey runner if applicable ----
    browser_outcome: dict[str, Any] | None = None
    browser_runner = _detect_browser_runner(verify_dir)
    if browser_runner is not None:
        browser_outcome = await _run_browser_journey(
            project_dir=verify_dir,
            runner=browser_runner,
            log_dir=log_dir,
            timeout_s=timeout_s,
        )
        if browser_outcome.get("log_path"):
            evidence.append(str(browser_outcome["log_path"]))
        if browser_outcome.get("artifacts"):
            evidence.extend([str(p) for p in browser_outcome["artifacts"]])

    api_journeys = [
        journey
        for journey in journeys_in_scope
        if isinstance(journey, dict)
        and str(journey.get("verification_level") or "").strip() == "api"
        and applicability_for(execution_scope, "api") == "run"
    ]
    api_executor_results: list[dict[str, Any]] = []
    if api_journeys:
        api_run = run_api_journey_executor(
            journeys=api_journeys,
            project_dir=verify_dir,
            artifact_dir=log_dir / "api-journeys",
            base_url=_api_base_url(),
            timeout_s=timeout_s,
        )
        api_executor_results = api_run.executor_results
        evidence.extend(str(path) for path in api_run.artifact_paths)
        (log_dir / "api-executor-results.json").write_text(
            json.dumps(
                {
                    "_written_at": iso_timestamp(),
                    "source": "api_executor",
                    "executor_results": api_executor_results,
                    "artifact_paths": [str(path) for path in api_run.artifact_paths],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

    journey_results = resolve_journey_verdicts(
        journeys=journeys_in_scope,
        execution_scope=execution_scope,
        executor_results=api_executor_results,
        registered_executor_levels={"ui", "api"},
    )
    api_ids = {str(journey.get("id") or "") for journey in api_journeys}
    api_results = [item for item in journey_results if str(item.get("id") or "") in api_ids]
    api_failed = failed_journey_ids(api_results)
    api_passed = sum(1 for item in api_results if item.get("passed") is True)
    native_status = str(test_outcome.get("status") or "")
    native_passed = native_status == "pass"
    native_failed = native_status in {"fail", "timeout", "error"}
    browser_failed = browser_outcome is not None and browser_outcome.get("status") not in {"pass", "no_tests"}
    browser_passed = browser_outcome is None or browser_outcome.get("status") == "pass"

    verdict: VerifyVerdict
    if browser_failed or native_failed or api_failed:
        verdict = "partial" if (native_passed or api_passed) else "unverified"
    elif api_results:
        verdict = "pass" if api_passed == len(api_results) else "unverified"
    elif native_passed and browser_passed:
        verdict = "pass"
    else:
        verdict = "unverified"

    deferred = [
        str(item.get("id"))
        for item in journey_results
        if str(item.get("status") or "").strip().lower() in {"defer", "deferred"}
    ]
    skipped = [
        str(item.get("id"))
        for item in journey_results
        if str(item.get("status") or "").strip().lower() in {"skip", "skipped"}
    ]
    summary_parts = [f"native tests {test_outcome.get('status')}"]
    if api_results:
        summary_parts.append(f"{api_passed}/{len(api_results)} api journeys passed")
    else:
        summary_parts.append("no applicable api journeys for this execution scope")
    if deferred:
        summary_parts.append(f"deferred ui journeys: {', '.join(deferred)}")
    if skipped:
        summary_parts.append(f"skipped journeys: {', '.join(skipped)}")
    if browser_failed:
        browser_status = browser_outcome.get("status") if browser_outcome is not None else "unknown"
        summary_parts.append(f"browser runner {browser_status}")
    summary = "; ".join(summary_parts)

    duration = time.monotonic() - started
    result = {
        "verdict": verdict,
        "journeys": journey_results,
        "evidence": evidence,
        "summary": summary,
        "duration_s": duration,
        "test_outcome": test_outcome,
        "browser_outcome": browser_outcome,
    }

    # Persist verify output to disk for the Lead's proof packet.
    (log_dir / "verify-result.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result


# ---------------------------------------------------------------------------
# Native test runner — auto-detect by project shape
# ---------------------------------------------------------------------------


async def _run_native_tests(
    *,
    project_dir: Path,
    log_dir: Path,
    timeout_s: int,
) -> dict[str, Any]:
    """Run the project's native test suite. Returns structured result."""
    cmd = _detect_test_command(project_dir)
    if not cmd:
        return {
            "status": "no_tests",
            "summary": "no test runner detected (package.json / pyproject / etc.)",
            "log_path": None,
            "exit_code": None,
        }

    log_path = log_dir / "test-output.log"

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(project_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={**os.environ},
        )
        try:
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "status": "timeout",
                "summary": f"test runner exceeded {timeout_s}s",
                "log_path": str(log_path),
                "exit_code": None,
            }
        text = (stdout_b or b"").decode("utf-8", errors="replace")
        log_path.write_text(text, encoding="utf-8")
        exit_code = proc.returncode
    except Exception as exc:  # noqa: BLE001
        logger.warning("test runner crashed: %s", exc)
        return {
            "status": "error",
            "summary": f"test runner spawn failed: {exc}",
            "log_path": None,
            "exit_code": None,
        }

    status = "pass" if exit_code == 0 else "fail"
    last_lines = text.strip().split("\n")[-3:] if text else []
    summary = " | ".join(last_lines)[:200] or f"exit_code={exit_code}"
    return {
        "status": status,
        "summary": summary,
        "log_path": str(log_path),
        "exit_code": exit_code,
        "command": cmd,
    }


def _detect_test_command(project_dir: Path) -> str | None:
    """Detect the project's test command. Order: npm, pytest, cargo, go."""
    if (project_dir / "package.json").is_file():
        try:
            pkg = json.loads((project_dir / "package.json").read_text(encoding="utf-8"))
            scripts = pkg.get("scripts") or {}
            if isinstance(scripts, dict):
                if "test" in scripts:
                    return "npm test --silent --if-present"
        except (OSError, json.JSONDecodeError):
            pass
    if (
        (project_dir / "pyproject.toml").is_file()
        or (project_dir / "pytest.ini").is_file()
        or (project_dir / "tests").is_dir()
    ):
        if shutil.which("pytest"):
            return "pytest -q --no-header"
    if (project_dir / "Cargo.toml").is_file():
        if shutil.which("cargo"):
            return "cargo test --quiet"
    if (project_dir / "go.mod").is_file():
        if shutil.which("go"):
            return "go test ./..."
    return None


# ---------------------------------------------------------------------------
# Browser journey runner
# ---------------------------------------------------------------------------


def _detect_browser_runner(project_dir: Path) -> str | None:
    """Detect a browser journey runner. Returns a shell command or None."""
    runner = project_dir / "tests" / "run_browser_journey.py"
    if runner.is_file():
        return f"python3 {runner}"
    # Playwright via npm:
    if (project_dir / "package.json").is_file():
        try:
            pkg = json.loads((project_dir / "package.json").read_text(encoding="utf-8"))
            scripts = pkg.get("scripts") or {}
            if isinstance(scripts, dict) and "browser" in scripts:
                return "npm run browser"
        except (OSError, json.JSONDecodeError):
            pass
    return None


async def _run_browser_journey(
    *,
    project_dir: Path,
    runner: str,
    log_dir: Path,
    timeout_s: int,
) -> dict[str, Any]:
    """Run the browser journey runner against the project."""
    log_path = log_dir / "browser-output.log"
    artifact_dir = log_dir / "browser-artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "OTTO_BROWSER_BASE_URL": os.environ.get("OTTO_BROWSER_BASE_URL", "http://127.0.0.1:5173"),
    }
    try:
        proc = await asyncio.create_subprocess_shell(
            runner,
            cwd=str(project_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        try:
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "status": "timeout",
                "summary": f"browser runner exceeded {timeout_s}s",
                "log_path": str(log_path),
                "exit_code": None,
                "artifacts": [],
            }
        text = (stdout_b or b"").decode("utf-8", errors="replace")
        log_path.write_text(text, encoding="utf-8")
        exit_code = proc.returncode
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "error",
            "summary": f"browser runner crashed: {exc}",
            "log_path": None,
            "exit_code": None,
            "artifacts": [],
        }

    status = "pass" if exit_code == 0 else "fail"
    artifacts = list(artifact_dir.glob("*.png"))
    last_lines = text.strip().split("\n")[-3:] if text else []
    summary = " | ".join(last_lines)[:200] or f"exit_code={exit_code}"
    return {
        "status": status,
        "summary": summary,
        "log_path": str(log_path),
        "exit_code": exit_code,
        "artifacts": [str(p) for p in artifacts],
        "command": runner,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_flat_spec_for_session(session_dir: Path) -> dict[str, Any] | None:
    """Load the flat spec.json this session was compiled with."""
    spec_path = session_dir / "spec" / "spec.json"
    if not spec_path.exists():
        return None
    try:
        return asdict(load_flat_spec(spec_path))
    except (OSError, json.JSONDecodeError, StructuredSpecValidationError):
        return None


def _filter_journeys(
    journeys: list[dict[str, Any]],
    scope_ids: list[str],
) -> list[dict[str, Any]]:
    """Filter journeys to those owned by this leaf scope. Empty scope = all."""
    if not scope_ids:
        return list(journeys)
    scope_set = set(scope_ids)
    filtered: list[dict[str, Any]] = []
    for journey in journeys:
        ids = {
            str(journey.get("id") or ""),
            *[str(item) for item in journey.get("feature_ids") or []],
            *[str(item) for item in journey.get("covers_primary_actions") or []],
        }
        if ids & scope_set:
            filtered.append(journey)
    return filtered


def _api_base_url() -> str | None:
    for name in ("OTTO_API_BASE_URL", "OTTO_BROWSER_BASE_URL", "API_BASE_URL"):
        value = os.environ.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _unverified(reason: str) -> dict[str, Any]:
    return {
        "verdict": "unverified",
        "journeys": [],
        "evidence": [],
        "summary": reason,
    }
