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
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger("otto.lead_verify")


VerifyVerdict = Literal["pass", "partial", "unverified"]


async def run_verify_for_lead(
    *,
    task_id: str,
    project_dir: Path,
    session_dir: Path,
    feature_scope_ids: list[str],
    timeout_s: int = 600,
) -> dict[str, Any]:
    """Run real verification against the running product.

    Best-effort: any subprocess crash, missing test runner, or timeout
    becomes verdict=unverified with the reason recorded. Never crashes Otto.
    """
    started = time.monotonic()
    log_dir = session_dir / "verify"
    log_dir.mkdir(parents=True, exist_ok=True)

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

    journey_results: list[dict[str, Any]] = []
    evidence: list[str] = []

    # ---- Layer 1: native test runner (npm test / pytest / cargo test / ...) ----
    test_outcome = await _run_native_tests(
        project_dir=project_dir, log_dir=log_dir, timeout_s=timeout_s,
    )
    test_evidence_path = log_dir / "test-output.log"
    if test_outcome["log_path"]:
        evidence.append(str(test_outcome["log_path"]))

    # ---- Layer 2: browser journey runner if applicable ----
    browser_outcome: dict[str, Any] | None = None
    browser_runner = _detect_browser_runner(project_dir)
    if browser_runner is not None:
        browser_outcome = await _run_browser_journey(
            project_dir=project_dir,
            runner=browser_runner,
            log_dir=log_dir,
            timeout_s=timeout_s,
        )
        if browser_outcome.get("log_path"):
            evidence.append(str(browser_outcome["log_path"]))
        if browser_outcome.get("artifacts"):
            evidence.extend([str(p) for p in browser_outcome["artifacts"]])

    # ---- Layer 3: map outcomes to per-journey pass/fail ----
    # In v5, the test agent (Phase 2 build/test split) writes tests named after
    # journey ids. For now, we use a simpler heuristic: ALL native tests must
    # pass for journey verdicts to be `pass`. Any browser failure flags the
    # journeys it touched.
    overall_native_passed = test_outcome["status"] == "pass"
    overall_browser_passed = (
        browser_outcome is None or browser_outcome.get("status") == "pass"
    )

    for journey in journeys_in_scope:
        jid = journey.get("id") or "<unnamed>"
        # If both layers passed, journey passes. Otherwise it's flagged.
        if overall_native_passed and overall_browser_passed:
            journey_results.append({
                "id": jid,
                "passed": True,
                "detail": (
                    f"native tests passed ({test_outcome.get('summary', '?')})"
                    + (
                        f"; browser journey passed ({browser_outcome.get('summary', '?')})"
                        if browser_outcome else ""
                    )
                ),
            })
        else:
            failure_reasons: list[str] = []
            if not overall_native_passed:
                failure_reasons.append(
                    f"native tests {test_outcome.get('status', '?')}: "
                    f"{test_outcome.get('summary', '?')}"
                )
            if not overall_browser_passed and browser_outcome:
                failure_reasons.append(
                    f"browser journey {browser_outcome.get('status', '?')}: "
                    f"{browser_outcome.get('summary', '?')}"
                )
            journey_results.append({
                "id": jid,
                "passed": False,
                "detail": "; ".join(failure_reasons),
            })

    passed = sum(1 for r in journey_results if r["passed"])
    total = len(journey_results)
    if test_outcome["status"] == "no_tests" and (
        browser_outcome is None or browser_outcome.get("status") == "no_runner"
    ):
        # Nothing to verify against — honest: unverified.
        verdict: VerifyVerdict = "unverified"
        summary = "no test runner detected; cannot verify"
    elif passed == total:
        verdict = "pass"
        summary = f"{total}/{total} journeys passed"
    elif passed > 0:
        verdict = "partial"
        summary = f"{passed}/{total} journeys passed; {total - passed} failed"
    else:
        verdict = "unverified"
        summary = "no journey passed verification"

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
        with spec_path.open(encoding="utf-8") as f:
            data: dict[str, Any] = json.load(f)
        return data
    except (OSError, json.JSONDecodeError):
        return None


def _filter_journeys(
    journeys: list[dict[str, Any]],
    scope_ids: list[str],
) -> list[dict[str, Any]]:
    """Filter journeys to those in scope. Empty scope = all journeys."""
    if not scope_ids:
        return list(journeys)
    scope_set = set(scope_ids)
    return [j for j in journeys if (j.get("id") or "") in scope_set]


def _unverified(reason: str) -> dict[str, Any]:
    return {
        "verdict": "unverified",
        "journeys": [],
        "evidence": [],
        "summary": reason,
    }
