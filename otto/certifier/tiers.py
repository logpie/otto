"""Unified certifier tiers — structural checks and probes.

Tier 1 (structural): does it build, do tests pass, does the app start?
Tier 2 (probes): do routes respond with correct shapes?

These are fast (seconds), deterministic (no LLM), and wrap existing
preflight.py and manifest.py functionality.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from otto.certifier.report import Finding, TierResult, TierStatus

logger = logging.getLogger("otto.certifier.tiers")


def run_tier1_structural(
    project_dir: Path,
    profile: Any,
    test_command: str | None = None,
    app_runner: Any | None = None,
) -> TierResult:
    """Tier 1 — structural checks. No LLM, seconds.

    Checks: files exist, build succeeds, tests pass, app starts.
    Returns TierResult with app_start prerequisite status in findings.
    """
    start = time.monotonic()
    findings: list[Finding] = []
    all_passed = True

    # 1. Expected files exist
    expected_files = _detect_expected_files(project_dir)
    for name, path in expected_files:
        if path.exists():
            logger.debug("structural: %s exists", name)
        else:
            findings.append(Finding(
                tier=1, severity="critical", category="build",
                description=f"Expected file missing: {name}",
                diagnosis=f"{path} does not exist",
                fix_suggestion=f"Create {name} in the project root",
            ))
            all_passed = False

    # 2. Agent's tests pass
    if test_command:
        test_result = _run_test_command(test_command, project_dir)
        if not test_result["passed"]:
            findings.append(Finding(
                tier=1, severity="critical", category="build",
                description=f"Tests failed: {test_command}",
                diagnosis=test_result.get("stderr", "")[:500],
                fix_suggestion="Fix failing tests",
                evidence={"exit_code": test_result["exit_code"],
                          "output": test_result.get("output", "")[:1000]},
            ))
            all_passed = False

    # 3. App starts (web/API only)
    app_started = False
    if app_runner is not None:
        try:
            app_evidence = app_runner.start()
            if app_evidence.passed:
                app_started = True
                logger.info("structural: app started at %s", app_runner.base_url)
            else:
                findings.append(Finding(
                    tier=1, severity="critical", category="build",
                    description="App failed to start",
                    diagnosis=str(app_evidence.actual)[:500],
                    fix_suggestion="Check start command and dependencies",
                ))
                all_passed = False
        except Exception as exc:
            findings.append(Finding(
                tier=1, severity="critical", category="build",
                description="App failed to start",
                diagnosis=str(exc)[:500],
                fix_suggestion="Check start command and dependencies",
            ))
            all_passed = False

    duration = round(time.monotonic() - start, 1)

    # Tag findings with app_start status for downstream prerequisite checks
    status = TierStatus.PASSED if all_passed else TierStatus.FAILED
    result = TierResult(
        tier=1, name="structural", status=status,
        findings=findings, duration_s=duration,
    )
    # Stash app_started as extra data for tier 2/4 prerequisite checks
    result._app_started = app_started  # type: ignore[attr-defined]
    return result


def run_tier2_probes(
    project_dir: Path,
    manifest: Any,
    base_url: str,
    tier1: TierResult,
) -> TierResult:
    """Tier 2 — HTTP probes. No LLM, seconds.

    Prerequisite: tier 1 app_start passed.
    Checks: routes respond, correct status codes, response shapes.
    """
    # Check prerequisite: app must be running
    app_started = getattr(tier1, "_app_started", False)
    if not app_started:
        return TierResult(
            tier=2, name="probes", status=TierStatus.BLOCKED,
            blocked_by="tier_1:app_start",
        )

    start = time.monotonic()
    findings: list[Finding] = []

    # Use existing preflight check
    from otto.certifier.preflight import preflight_check
    pf = preflight_check(manifest, base_url)

    for check in pf.checks:
        if not check.passed:
            findings.append(Finding(
                tier=2, severity="important", category="endpoint",
                description=f"Probe failed: {check.name}",
                diagnosis=check.detail,
                fix_suggestion=f"Fix {check.name} to respond correctly",
            ))

    duration = round(time.monotonic() - start, 1)
    status = TierStatus.PASSED if pf.ready else TierStatus.FAILED
    return TierResult(
        tier=2, name="probes", status=status,
        findings=findings, duration_s=duration,
    )


def _detect_expected_files(project_dir: Path) -> list[tuple[str, Path]]:
    """Detect expected project files based on what exists."""
    files = []
    # Check for common project markers
    candidates = [
        ("package.json", project_dir / "package.json"),
        ("requirements.txt", project_dir / "requirements.txt"),
        ("setup.py", project_dir / "setup.py"),
        ("pyproject.toml", project_dir / "pyproject.toml"),
        ("Cargo.toml", project_dir / "Cargo.toml"),
    ]
    # Only check for files that the project type suggests should exist
    # If none of the markers exist, that itself is a finding
    found_any = False
    for name, path in candidates:
        if path.exists():
            files.append((name, path))
            found_any = True

    if not found_any:
        # Return first candidate so we report "no project file found"
        files.append((
            "package.json or requirements.txt or setup.py or pyproject.toml",
            project_dir / "package.json",
        ))

    return files


def _run_test_command(cmd: str, cwd: Path) -> dict[str, Any]:
    """Run the test command and return structured result."""
    try:
        result = subprocess.run(
            cmd, shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=300,
        )
        return {
            "passed": result.returncode == 0,
            "exit_code": result.returncode,
            "output": result.stdout[-2000:] if result.stdout else "",
            "stderr": result.stderr[-2000:] if result.stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {
            "passed": False,
            "exit_code": -1,
            "output": "",
            "stderr": "Test command timed out after 300s",
        }
    except Exception as exc:
        return {
            "passed": False,
            "exit_code": -1,
            "output": "",
            "stderr": str(exc),
        }
