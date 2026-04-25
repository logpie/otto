"""Smoke tests for the Phase 5 web-as-user + Phase 3.5 web-record scaffolding.

These are unit-level tests of the harness scripts themselves — they live
outside ``tests/browser/`` so they never trigger the Playwright suite or its
fixtures. They MUST pass without any real LLM activity, real subprocess
spawn, or real browser launch.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
WEB_AS_USER = SCRIPTS_DIR / "web_as_user.py"
WEB_RECORD_FIXTURE = SCRIPTS_DIR / "web_record_fixture.py"


def _run_script(script: Path, args: list[str], *, env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run a scaffolding script in a clean child process.

    We intentionally drop ``OTTO_ALLOW_REAL_COST`` from the inherited env so
    the guard tests can prove the negative case. Other tests that need it
    add it back via ``env_extra``.
    """
    env = {k: v for k, v in os.environ.items() if k != "OTTO_ALLOW_REAL_COST"}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=REPO_ROOT,
    )


# ---------------------------------------------------------------------------
# --list runs without real cost
# ---------------------------------------------------------------------------


def test_web_as_user_list_runs_without_real_cost() -> None:
    """`--list` enumerates all 14 scenarios + tier mappings, exits 0, no env needed."""
    result = _run_script(WEB_AS_USER, ["--list"])
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    stdout = result.stdout
    expected_ids = ["W1", "W2", "W3", "W4", "W5", "W6", "W7", "W8", "W9",
                    "W10", "W11", "W12a", "W12b", "W13"]
    for sid in expected_ids:
        assert sid in stdout, f"scenario id {sid!r} missing from --list output"
    # Tier mappings printed
    assert "nightly" in stdout and "weekly" in stdout
    assert "W11" in stdout and "W7" in stdout


def test_web_record_fixture_list_runs_without_real_cost() -> None:
    """`--list` enumerates R1..R14 with no env needed."""
    result = _run_script(WEB_RECORD_FIXTURE, ["--list"])
    assert result.returncode == 0, f"stderr: {result.stderr}\nstdout: {result.stdout}"
    stdout = result.stdout
    for n in range(1, 15):
        rid = f"R{n}"
        assert rid in stdout, f"recording id {rid!r} missing from --list output"


# ---------------------------------------------------------------------------
# Real-cost guard refuses without env var
# ---------------------------------------------------------------------------


def test_web_as_user_refuses_without_OTTO_ALLOW_REAL_COST() -> None:
    """Invoking a real scenario without dry-run AND without env var aborts with clear message."""
    result = _run_script(WEB_AS_USER, ["--scenario", "W1"])
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "OTTO_ALLOW_REAL_COST" in combined, (
        f"expected guard mention; got stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_web_record_fixture_refuses_without_OTTO_ALLOW_REAL_COST() -> None:
    """Recording without dry-run + without env var aborts with clear message."""
    result = _run_script(WEB_RECORD_FIXTURE, ["--recording", "R1"])
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "OTTO_ALLOW_REAL_COST" in combined, (
        f"expected guard mention; got stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Dry-run mode does not invoke browser / LLM
# ---------------------------------------------------------------------------


def test_web_as_user_dry_run_W1_does_not_spawn_browser() -> None:
    """`--dry-run --scenario W1` should not need OTTO_ALLOW_REAL_COST + complete cleanly."""
    result = _run_script(WEB_AS_USER, ["--dry-run", "--scenario", "W1"])
    assert result.returncode == 0, (
        f"dry-run W1 should succeed without real cost; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    combined = result.stdout + result.stderr
    # Sanity: dry-run announces it skipped real LLM.
    assert "dry-run" in combined.lower() or "skipped" in combined.lower()


def test_web_record_fixture_dry_run_R1_does_not_invoke_llm() -> None:
    """`--dry-run --recording R1` should not need OTTO_ALLOW_REAL_COST."""
    result = _run_script(WEB_RECORD_FIXTURE, ["--dry-run", "--recording", "R1"])
    assert result.returncode == 0, (
        f"dry-run R1 should succeed without real cost; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# Registry completeness
# ---------------------------------------------------------------------------


def test_web_as_user_scenario_registry_completeness() -> None:
    """Every W1..W13 (with W12 split into W12a + W12b) has a registry entry."""
    # Import lazily to avoid pulling in scripts/* at collection time
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    expected = {
        "W1", "W2", "W3", "W4", "W5", "W6", "W7", "W8", "W9", "W10",
        "W11", "W12a", "W12b", "W13",
    }
    actual = set(web_as_user.SCENARIOS)
    assert actual == expected, f"registry diff: missing={expected - actual}, extra={actual - expected}"
    # Also verify tier mappings reference real scenarios
    for sid in web_as_user.TIER_NIGHTLY:
        assert sid in web_as_user.SCENARIOS, f"TIER_NIGHTLY references unknown scenario {sid!r}"
    for sid in web_as_user.TIER_WEEKLY:
        assert sid in web_as_user.SCENARIOS, f"TIER_WEEKLY references unknown scenario {sid!r}"


def test_web_record_fixture_recording_registry_completeness() -> None:
    """Every R1..R14 has a registry entry."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_record_fixture  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    expected = {f"R{n}" for n in range(1, 15)}
    actual = set(web_record_fixture.RECORDINGS)
    assert actual == expected, f"registry diff: missing={expected - actual}, extra={actual - expected}"
