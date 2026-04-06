"""Unified certifier tiers — structural checks and probes.

Tier 1 (structural): does it build, do tests pass, does the app start?
Tier 2 (probes): do routes respond with correct shapes? (HTTP) / does --help work? (CLI)

These are fast (seconds), deterministic (no LLM), and wrap existing
preflight.py and manifest.py functionality.
"""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import tempfile
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

    # 1. Source files exist (sanity check — not critical)
    has_source = any(
        project_dir.glob(f"**/{ext}")
        for ext in ("*.py", "*.js", "*.ts", "*.rs", "*.go", "*.rb", "*.java")
    )
    if not has_source:
        findings.append(Finding(
            tier=1, severity="warning", category="build",
            description="No source files found in project",
            diagnosis="No .py, .js, .ts, .rs, .go files detected",
            fix_suggestion="Check that source code exists in the project directory",
        ))

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


def run_tier2_cli_probes(
    project_dir: Path,
    manifest: Any,
    profile: Any,
) -> TierResult:
    """Tier 2 — CLI probes. No LLM, seconds.

    Checks: CLI entrypoint exists, --help works, subcommands respond.
    Failures are warnings (not blockers) — many CLIs have non-standard help.
    """
    start = time.monotonic()
    findings: list[Finding] = []

    entrypoint = getattr(manifest, "cli_entrypoint", [])
    if not entrypoint:
        return TierResult(
            tier=2, name="probes", status=TierStatus.SKIPPED,
            skip_reason="No CLI entrypoint detected",
            duration_s=round(time.monotonic() - start, 1),
        )

    # Build env: .venv/bin on PATH + disposable HOME for safety
    env = _cli_probe_env(project_dir)

    # 1. --help check
    try:
        result = subprocess.run(
            entrypoint + ["--help"],
            cwd=str(project_dir), env=env,
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            # Capture help text for the manifest
            manifest.cli_help_text = (result.stdout or "")[:5000]
            logger.info("CLI probe: --help succeeded (%d chars)", len(manifest.cli_help_text))
        else:
            findings.append(Finding(
                tier=2, severity="warning", category="endpoint",
                description=f"CLI --help exited with code {result.returncode}",
                diagnosis=(result.stderr or result.stdout or "")[:500],
                fix_suggestion="Ensure the CLI entrypoint accepts --help",
            ))
    except subprocess.TimeoutExpired:
        findings.append(Finding(
            tier=2, severity="warning", category="endpoint",
            description="CLI --help timed out (10s)",
            diagnosis="The CLI entrypoint did not exit within 10 seconds",
            fix_suggestion="Ensure --help exits quickly",
        ))
    except FileNotFoundError:
        findings.append(Finding(
            tier=2, severity="critical", category="endpoint",
            description=f"CLI entrypoint not found: {shlex.join(entrypoint)}",
            diagnosis="The command could not be executed",
            fix_suggestion="Check that the entrypoint exists and is executable",
        ))

    # 2. Subcommand --help (discovered from adapter analysis)
    cli_commands = getattr(manifest, "cli_commands", [])
    for cmd in cli_commands[:10]:  # cap at 10 to avoid slow probes
        cmd_name = cmd.get("name", "")
        if not cmd_name:
            continue
        try:
            result = subprocess.run(
                entrypoint + [cmd_name, "--help"],
                cwd=str(project_dir), env=env,
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                findings.append(Finding(
                    tier=2, severity="warning", category="endpoint",
                    description=f"Subcommand '{cmd_name} --help' exited with code {result.returncode}",
                    diagnosis=(result.stderr or "")[:300],
                    fix_suggestion=f"Check that '{cmd_name}' is a valid subcommand",
                ))
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass  # --help probe failures are best-effort

    duration = round(time.monotonic() - start, 1)
    has_critical = any(f.severity == "critical" for f in findings)
    status = TierStatus.FAILED if has_critical else TierStatus.PASSED
    return TierResult(
        tier=2, name="probes", status=status,
        findings=findings, duration_s=duration,
    )


def _cli_probe_env(project_dir: Path) -> dict[str, str]:
    """Build an environment for CLI probes: .venv on PATH, disposable HOME."""
    env = dict(os.environ)

    # Prepend .venv/bin to PATH if it exists
    venv_bin = project_dir / ".venv" / "bin"
    if venv_bin.exists():
        env["PATH"] = str(venv_bin) + ":" + env.get("PATH", "")
        env["VIRTUAL_ENV"] = str(project_dir / ".venv")

    # Disposable HOME so CLI tools don't pollute real home or read stale config
    probe_home = tempfile.mkdtemp(prefix="otto-cli-probe-")
    env["HOME"] = probe_home
    env["XDG_CONFIG_HOME"] = os.path.join(probe_home, ".config")
    env["XDG_DATA_HOME"] = os.path.join(probe_home, ".local", "share")

    return env
