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
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


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


def test_web_as_user_evidence_selectors_cover_current_run_drawer() -> None:
    """True-web evidence checks must target the current RunDrawer controls/panels."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    assert '[data-testid="run-quick-action-logs"]' in web_as_user.MC_EVIDENCE_BUTTON_SELECTORS["logs"]
    assert '[data-testid="run-quick-action-diff"]' in web_as_user.MC_EVIDENCE_BUTTON_SELECTORS["diff"]
    assert '[data-testid="run-quick-action-artifacts"]' in web_as_user.MC_EVIDENCE_BUTTON_SELECTORS["artifacts"]
    assert '[data-testid="run-resource-panel-logs"]' in web_as_user.MC_EVIDENCE_PANEL_SELECTORS["logs"]
    assert '[data-testid="run-resource-panel-diff"]' in web_as_user.MC_EVIDENCE_PANEL_SELECTORS["diff"]
    assert '[data-testid="run-resource-panel-artifacts"]' in web_as_user.MC_EVIDENCE_PANEL_SELECTORS["artifacts"]
    assert '[data-testid="run-drawer"]' not in web_as_user.MC_EVIDENCE_PANEL_SELECTORS["diff"]
    assert '[data-testid="run-list-detail-drawer"]' not in web_as_user.MC_EVIDENCE_PANEL_SELECTORS["logs"]


def test_web_as_user_honors_scenario_returned_failure(monkeypatch, tmp_path: Path) -> None:
    """A scenario returning FAIL/INFRA must not be wrapped as PASS."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    class DummyBackend:
        port = 12345
        url = "http://127.0.0.1:12345"

        def stop(self) -> None:
            return None

    @contextmanager
    def fake_project() -> Iterator[Path]:
        project = tmp_path / "project"
        project.mkdir()
        yield project

    def returned_failure(ctx: object) -> object:
        return web_as_user.ScenarioRunResult(
            outcome="FAIL",
            note="scenario-specific failure",
            duration_s=0.0,
            failures=[],
        )

    monkeypatch.setattr(web_as_user, "DEFAULT_ARTIFACT_ROOT", tmp_path / "artifacts")
    monkeypatch.setattr(web_as_user, "_throwaway_project", fake_project)
    monkeypatch.setattr(web_as_user, "_start_otto_web_in_process", lambda _project, _artifact: DummyBackend())
    monkeypatch.setattr(web_as_user, "artifact_mine_pass", lambda _project, _failures: None)

    scenario = web_as_user.Scenario(
        id="WX",
        description="returned failure regression",
        tier="nightly",
        estimated_cost=0.0,
        estimated_seconds=0,
        needs_product_verification=False,
        target_recordings=[],
        run_fn=returned_failure,
    )

    outcome = web_as_user.run_one_scenario(
        scenario,
        run_id="test-run",
        provider="claude",
        dry_run=False,
        user_behavior="off",
        user_seed=None,
    )

    assert outcome.outcome == "FAIL"
    assert outcome.note == "scenario-specific failure"


def test_web_as_user_summary_treats_infra_as_nonzero(tmp_path: Path) -> None:
    """INFRA is not a green test run unless the caller handles it explicitly."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    exit_code = web_as_user.print_summary(
        "test-run",
        [
            web_as_user.ScenarioOutcome(
                scenario_id="WX",
                description="infra regression",
                outcome="INFRA",
                note="rate limited",
                artifact_dir=tmp_path,
                wall_duration_s=0.0,
            )
        ],
    )

    assert exit_code == 1


def test_web_as_user_semantic_audit_flags_obvious_false_confidence() -> None:
    """The true-web harness must fail contradictions selectors alone would miss."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    findings = web_as_user._mc_run_detail_semantic_findings(
        """
        Tasks 1 task on main STATUS TASK STORIES ● RUNNING build-a-micro-twitter
        Running0 files3:50
        × building build a micro twitter FEATURES 0 / 19 WALL 0s COST $0.00
        Stages ● Compile done → ○ Spec review pending → ◐ Build active →
        ● Seed done → ○ Audit pending
        Diff Base: main No changes from base.
        """
    )

    assert any("Spec review is pending" in finding for finding in findings)
    assert any("Seed is visible" in finding for finding in findings)
    assert any("WALL 0s" in finding for finding in findings)
    assert any("No changes from base" in finding for finding in findings)


def test_web_as_user_semantic_audit_accepts_fixed_stage_language() -> None:
    """Fixed Mission Control stage labels should not trip false blockers."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    findings = web_as_user._mc_run_detail_semantic_findings(
        """
        Tasks 1 task on main STATUS TASK STORIES ● RUNNING build-a-micro-twitter
        Running0 files0:12
        × building build a micro twitter FEATURES 0 / 19 WALL 12s COST $0.00
        Stages ● Compile spec done → ○ Spec review skipped →
        ● Prepare fixtures done → ◐ Build groups active → ○ Audit product pending
        """
    )

    assert findings == []


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
