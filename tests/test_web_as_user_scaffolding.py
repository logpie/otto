"""Smoke tests for the Phase 5 web-as-user + Phase 3.5 web-record scaffolding.

These are unit-level tests of the harness scripts themselves — they live
outside ``tests/browser/`` so they never trigger the Playwright suite or its
fixtures. They MUST pass without any real LLM activity, real subprocess
spawn, or real browser launch.
"""

from __future__ import annotations

import json
import os
import signal
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


def test_browser_bundle_helper_uses_build_not_commit_gate_by_default(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """True WebTest needs a fresh bundle even when the local bundle is uncommitted."""
    from tests.browser._helpers import build_bundle

    calls: list[str] = []
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "index.js").write_text("console.log('ok')", encoding="utf-8")
    (assets / "index.css").write_text("body{}", encoding="utf-8")
    monkeypatch.delenv("OTTO_BROWSER_SKIP_BUILD", raising=False)
    monkeypatch.delenv("OTTO_BROWSER_REQUIRE_COMMITTED_BUNDLE", raising=False)
    monkeypatch.setattr(build_bundle, "STATIC_ASSETS_DIR", assets)
    monkeypatch.setattr(build_bundle, "_run_npm", lambda script: calls.append(script))
    if hasattr(build_bundle.ensure_bundle_built, "_done"):
        delattr(build_bundle.ensure_bundle_built, "_done")

    build_bundle.ensure_bundle_built()

    assert calls == ["web:build"]


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


def test_web_as_user_dry_run_accepts_custom_intent() -> None:
    """`--intent` enables varied pressure projects without hardcoding one benchmark."""
    result = _run_script(
        WEB_AS_USER,
        [
            "--dry-run",
            "--scenario",
            "W1",
            "--intent",
            "Build a micro Twitter.",
            "--build-timeout-s",
            "1800",
        ],
    )
    assert result.returncode == 0, (
        f"dry-run W1 custom intent should succeed without real cost; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_web_as_user_dry_run_accepts_group_concurrency() -> None:
    """`--group-concurrent` makes true-web concurrency explicit and replayable."""
    result = _run_script(
        WEB_AS_USER,
        [
            "--dry-run",
            "--scenario",
            "W1",
            "--group-concurrent",
            "3",
        ],
    )
    assert result.returncode == 0, (
        f"dry-run W1 group concurrency should succeed without real cost; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_web_as_user_cleanup_keeps_videos_but_prunes_trace_bundles(tmp_path: Path) -> None:
    """True-web videos are evidence; trace/HAR bundles are optional debug bloat."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    artifact_dir = tmp_path / "artifacts"
    nested = artifact_dir / "test-results" / "case"
    nested.mkdir(parents=True)
    video = nested / "video.webm"
    trace = nested / "trace.zip"
    har = nested / "network.har"
    screenshot = nested / "screen.png"
    video.write_bytes(b"video")
    trace.write_bytes(b"trace")
    har.write_bytes(b"har")
    screenshot.write_bytes(b"png")

    report = web_as_user.cleanup_heavy_browser_artifacts(artifact_dir)

    assert video.exists()
    assert screenshot.exists()
    assert not trace.exists()
    assert not har.exists()
    deleted = {entry["path"] for entry in report["deleted"]}
    assert "test-results/case/trace.zip" in deleted
    assert "test-results/case/network.har" in deleted


def test_failed_project_snapshot_preserves_browser_test_results(tmp_path: Path) -> None:
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    project = tmp_path / "project"
    result_dir = project / "test-results" / "case"
    node_modules = project / "node_modules"
    result_dir.mkdir(parents=True)
    node_modules.mkdir(parents=True)
    (result_dir / "trace.zip").write_bytes(b"trace")
    (node_modules / "large.js").write_text("ignored", encoding="utf-8")

    artifact_dir = tmp_path / "artifacts"
    report = web_as_user._preserve_failed_project_snapshot(project, artifact_dir)

    assert report["copied"] is True
    assert (artifact_dir / "project-snapshot" / "test-results" / "case" / "trace.zip").exists()
    assert not (artifact_dir / "project-snapshot" / "node_modules").exists()


def test_web_as_user_writes_bounded_meta_debug_packet(tmp_path: Path) -> None:
    """A stalled monitor gets root-cause evidence without dumping messages.jsonl."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    project = tmp_path / "project"
    artifact_dir = tmp_path / "artifacts"
    session = project / ".worktrees" / "task-a" / "otto_logs" / "sessions" / "run-1"
    (session / "audit" / "attempt-00").mkdir(parents=True)
    (session / "summary.json").write_text('{"verdict":"blocked"}', encoding="utf-8")
    (session / "audit" / "attempt-00" / "evidence-packet.json").write_text(
        json.dumps(
            {
                "merge_summary": {
                    "blocked_ids": ["foundation"],
                    "per_group": [
                        {
                            "group_id": "foundation",
                            "narrative": "merge wall budget exhausted after 1230s",
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
    (session / "spec-state.jsonl").write_text(
        '{"kind":"group.blocked","detail":"merge wall budget exhausted after 1230s"}\n',
        encoding="utf-8",
    )
    (session / "messages.jsonl").write_text("do not read this raw transcript\n", encoding="utf-8")
    ctx = web_as_user.ScenarioContext(
        scenario=web_as_user.SCENARIOS["W1"],
        project_dir=project,
        artifact_dir=artifact_dir,
        provider="codex",
        failures=web_as_user.RunFailures(),
        debug_log=artifact_dir / "debug.log",
        run_id="test-run",
    )

    packet_path = web_as_user._write_meta_debug_packet(
        ctx,
        reason="visible-progress-stall",
        state={
            "live": {
                "items": [
                    {
                        "status": "running",
                        "run_id": "run-1",
                        "queue_task_id": "task-a",
                        "last_event": "merge",
                    }
                ]
            },
            "history": {"items": []},
            "landing": {"items": []},
        },
        submitted_task_id="task-a",
        submitted_run_id="run-1",
        poll_count=42,
        progress_signature="same",
        terminal_outcome=None,
    )

    assert packet_path is not None
    packet = json.loads(packet_path.read_text(encoding="utf-8"))
    assert packet["reason"] == "visible-progress-stall"
    assert packet["audit_evidence"]["merge_summary"]["blocked_ids"] == ["foundation"]
    assert any("merge wall budget exhausted" in hint for hint in packet["hints"])
    assert "messages.jsonl" not in json.dumps(packet.get("recent_spec_events", []))


def test_configure_throwaway_project_aligns_timeout_and_queue_guard(tmp_path: Path) -> None:
    """Long true-web runs must not be preempted by Otto's queue hard-kill guard."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("# test\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True)

    web_as_user._configure_throwaway_project(
        tmp_path,
        group_concurrent=3,
        build_timeout_s=7200,
    )

    config = (tmp_path / "otto.yaml").read_text(encoding="utf-8")
    assert "run_budget_seconds: 7200" in config
    assert "queue:\n  task_timeout_s: 8640" in config
    assert "build:\n  group_concurrent: 3" in config


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
    monkeypatch.setattr(web_as_user, "_start_otto_web_in_process", lambda _project, _artifact, **_kwargs: DummyBackend())
    monkeypatch.setattr(web_as_user, "artifact_mine_pass", lambda _project, _failures: None)
    teardown_calls: list[bool] = []
    cleanup_calls: list[bool] = []

    def fake_teardown(**kwargs: object) -> dict[str, object]:
        teardown_calls.append(bool(kwargs["keep_snapshot"]))
        return {"project_process_cleanup": {}}

    monkeypatch.setattr(web_as_user, "_teardown_scenario_runtime", fake_teardown)
    monkeypatch.setattr(
        web_as_user,
        "cleanup_heavy_browser_artifacts",
        lambda _artifact_dir, *, keep=False: cleanup_calls.append(bool(keep)) or {},
    )

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
    assert teardown_calls == [True]
    assert cleanup_calls == [True]


def test_needs_product_verification_requires_product_browser_step(
    monkeypatch, tmp_path: Path
) -> None:
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

    monkeypatch.setattr(web_as_user, "DEFAULT_ARTIFACT_ROOT", tmp_path / "artifacts")
    monkeypatch.setattr(web_as_user, "_throwaway_project", fake_project)
    monkeypatch.setattr(
        web_as_user,
        "_start_otto_web_in_process",
        lambda _project, _artifact, **_kwargs: DummyBackend(),
    )
    monkeypatch.setattr(web_as_user, "artifact_mine_pass", lambda _project, _failures: None)

    scenario = web_as_user.Scenario(
        id="WX",
        description="missing product verification regression",
        tier="nightly",
        estimated_cost=0.0,
        estimated_seconds=0,
        needs_product_verification=True,
        target_recordings=[],
        run_fn=lambda _ctx: web_as_user.ScenarioRunResult("PASS", "ok", 0.0),
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
    assert "never ran the generated-product browser verification" in outcome.note


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


def test_artifact_mine_does_not_require_manifest_for_running_queue_task(tmp_path: Path) -> None:
    """Running queue tasks may not have written the completion manifest yet."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    project = tmp_path / "project"
    project.mkdir()
    (project / ".otto-queue-state.json").write_text(
        '{"tasks": {"build-micro-twitter": {"status": "running"}}}',
        encoding="utf-8",
    )
    failures = web_as_user.RunFailures()

    web_as_user.artifact_mine_pass(project, failures)

    assert failures.failures == []


def test_artifact_mine_does_not_require_manifest_for_interrupted_queue_task(
    tmp_path: Path,
) -> None:
    """Watcher-stopped tasks can be visible/resumable before a manifest exists."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    project = tmp_path / "project"
    project.mkdir()
    (project / ".otto-queue-state.json").write_text(
        '{"tasks": {"build-micro-twitter": {"status": "interrupted"}}}',
        encoding="utf-8",
    )
    failures = web_as_user.RunFailures()

    web_as_user.artifact_mine_pass(project, failures)

    assert failures.failures == []


def test_artifact_mine_flags_missing_otto_artifacts_gitignore_after_evidence(tmp_path: Path) -> None:
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
    (project / "otto_artifacts").mkdir()
    failures = web_as_user.RunFailures()

    web_as_user.artifact_mine_pass(project, failures)

    assert any("otto_artifacts" in failure for failure in failures.failures)


def test_artifact_mine_accepts_runtime_gitignore_patterns(tmp_path: Path) -> None:
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
    (project / ".gitignore").write_text("otto_logs/\notto_artifacts/\n", encoding="utf-8")
    (project / "otto_artifacts").mkdir()
    failures = web_as_user.RunFailures()

    web_as_user.artifact_mine_pass(project, failures)

    assert failures.failures == []


def test_mc_realistic_probe_actions_are_stratified_not_pure_random(tmp_path: Path) -> None:
    """Realistic mode must force varied wait-time behaviors, not sample blindly."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    scenario = web_as_user.Scenario(
        id="WX",
        description="probe action regression",
        tier="nightly",
        estimated_cost=0.0,
        estimated_seconds=0,
        needs_product_verification=False,
        target_recordings=[],
        run_fn=lambda _ctx: web_as_user.ScenarioRunResult("PASS", "ok", 0.0),
    )
    ctx = web_as_user.ScenarioContext(
        scenario=scenario,
        project_dir=tmp_path,
        artifact_dir=tmp_path,
        provider="codex",
        failures=web_as_user.RunFailures(),
        debug_log=tmp_path / "debug.log",
        run_id="probe-run",
        user_behavior="mc-realistic",
        user_seed=42,
    )

    plans = [web_as_user._mc_probe_actions(ctx, f"running-poll-{1 + idx * 12}") for idx in range(10)]
    first_actions = [plan[0] for plan in plans]

    assert first_actions == [
        "inspect-run",
        "reload",
        "project-roundtrip",
        "inspect-run",
        "background-return",
        "back-forward",
        "keyboard-probe",
        "scroll",
        "ui-refresh",
        "layout",
    ]
    assert all(len(plan) == 2 for plan in plans)
    assert "inspect-run" in web_as_user._mc_probe_actions(ctx, "terminal-state")


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


def test_web_as_user_agent_browser_probe_records_real_tool_path(
    monkeypatch, tmp_path: Path
) -> None:
    """True-web W1 must collect first-class agent-browser evidence."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(web_as_user.shutil, "which", lambda name: "/opt/bin/agent-browser")
    monkeypatch.setattr(web_as_user.subprocess, "run", fake_run)

    ctx = web_as_user.ScenarioContext(
        scenario=web_as_user.SCENARIOS["W1"],
        project_dir=tmp_path / "project",
        artifact_dir=tmp_path / "artifacts",
        provider="codex",
        failures=web_as_user.RunFailures(),
        debug_log=tmp_path / "debug.log",
        run_id="agent-browser-test",
        user_behavior="mc-realistic",
        user_seed=123,
    )

    web_as_user._agent_browser_mc_probe(
        ctx,
        phase="shell-loaded",
        url="http://127.0.0.1:9000",
        expectation="probe shell",
        log_fn=lambda _msg: None,
        failures=ctx.failures,
        hard=True,
    )

    assert ctx.failures.failures == []
    assert calls
    assert all(call[:3] == ["agent-browser", "--session", calls[0][2]] for call in calls)
    assert [call[3] for call in calls] == ["set", "open", "snapshot", "screenshot"]
    actions = (ctx.artifact_dir / "agent-browser-actions.jsonl").read_text(encoding="utf-8")
    assert '"tool": "agent-browser"' in actions


def test_web_as_user_semantic_audit_accepts_explained_seed_artifact_path() -> None:
    """Artifact/log paths may include seed if the UI also explains fixtures."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    findings = web_as_user._mc_run_detail_semantic_findings(
        """
        Stages ● Prepare fixtures done → ◐ Build groups active
        Artifacts seed/seed.log build/foundation/attempt-01/narrative.log
        """
    )

    assert findings == []


def test_web_as_user_filters_launcher_state_409_resource_noise() -> None:
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    captured = {
        "network_errors": [{"status": 409, "url": "http://127.0.0.1:9000/api/state"}],
        "console": [
            {
                "type": "error",
                "text": "Failed to load resource: the server responded with a status of 409 (Conflict)",
                "location": {"url": "http://127.0.0.1:9000/api/state"},
            },
            {"type": "warning", "text": "ordinary warning", "location": {}},
        ],
    }

    assert web_as_user._unexpected_network_errors(captured["network_errors"]) == []
    assert web_as_user._unexpected_console_errors(captured) == []


def test_web_as_user_product_roots_prefer_submitted_worktree(tmp_path: Path) -> None:
    """Generated-product verification must inspect the queue worktree, not the shell repo."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    project = tmp_path / "project"
    worktree = project / ".worktrees" / "build-micro-twitter"
    worktree.mkdir(parents=True)
    (worktree / "package.json").write_text('{"scripts":{"dev":"vite"}}\n', encoding="utf-8")
    (project / "README.md").write_text("# shell\n", encoding="utf-8")
    ctx = web_as_user.ScenarioContext(
        scenario=web_as_user.SCENARIOS["W1"],
        project_dir=project,
        artifact_dir=tmp_path / "artifacts",
        provider="codex",
        failures=web_as_user.RunFailures(),
        debug_log=tmp_path / "debug.log",
        run_id="root-test",
    )
    state = {
        "history": {
            "items": [
                {
                    "queue_task_id": "build-micro-twitter",
                    "run_id": "run-1",
                    "cwd": str(worktree),
                    "worktree": ".worktrees/build-micro-twitter",
                }
            ]
        }
    }

    roots = web_as_user._candidate_product_roots(
        ctx,
        state=state,
        submitted_task_id="build-micro-twitter",
        submitted_run_id="run-1",
    )

    assert roots[0] == worktree.resolve()


def test_web_as_user_product_roots_ignore_stale_richer_worktree(
    tmp_path: Path,
) -> None:
    """Submitted-run verification must not rank stale sibling worktrees above the target."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    project = tmp_path / "project"
    target = project / ".worktrees" / "build-micro-twitter"
    stale = project / ".worktrees" / "old-rich-project"
    target.mkdir(parents=True)
    stale.mkdir(parents=True)
    (target / "index.html").write_text("<main>target</main>", encoding="utf-8")
    (stale / "package.json").write_text('{"scripts":{"dev":"vite"}}\n', encoding="utf-8")
    (stale / "index.html").write_text("<main>stale</main>", encoding="utf-8")
    (stale / "src").mkdir()
    ctx = web_as_user.ScenarioContext(
        scenario=web_as_user.SCENARIOS["W1"],
        project_dir=project,
        artifact_dir=tmp_path / "artifacts",
        provider="codex",
        failures=web_as_user.RunFailures(),
        debug_log=tmp_path / "debug.log",
        run_id="root-test",
    )
    state = {
        "history": {
            "items": [
                {
                    "queue_task_id": "build-micro-twitter",
                    "run_id": "run-1",
                    "cwd": str(target),
                    "worktree": ".worktrees/build-micro-twitter",
                }
            ]
        }
    }

    roots = web_as_user._candidate_product_roots(
        ctx,
        state=state,
        submitted_task_id="build-micro-twitter",
        submitted_run_id="run-1",
    )

    assert target.resolve() in roots
    assert stale.resolve() not in roots


def test_web_as_user_terminal_detection_accepts_done_live_row() -> None:
    """W1 must stop polling when Mission Control keeps success in live rows."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    state = {
        "live": {
            "items": [
                {
                    "queue_task_id": "build-micro-twitter",
                    "run_id": "run-1",
                    "status": "done",
                    "terminal_outcome": "success",
                }
            ]
        },
        "history": {"items": []},
    }

    outcome, run_id = web_as_user._terminal_outcome_for_submitted_task(
        state,
        submitted_task_id="build-micro-twitter",
        submitted_run_id=None,
    )

    assert outcome == "success"
    assert run_id == "run-1"


def test_web_as_user_terminal_detection_matches_by_run_id_when_task_id_missing() -> None:
    """Mission Control rows from older surfaces may not include queue_task_id."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    state = {
        "history": {
            "items": [
                {
                    "run_id": "run-1",
                    "status": "done",
                    "terminal_outcome": "success",
                }
            ]
        }
    }

    outcome, run_id = web_as_user._terminal_outcome_for_submitted_task(
        state,
        submitted_task_id=None,
        submitted_run_id="run-1",
    )

    assert outcome == "success"
    assert run_id == "run-1"


def test_web_as_user_wait_for_terminal_accepts_live_terminal_row(
    monkeypatch,
) -> None:
    """Shared terminal waits must not spin until timeout when terminal rows remain live."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    state = {
        "live": {
            "items": [
                {
                    "domain": "queue",
                    "queue_task_id": "build-micro-twitter",
                    "run_id": "run-1",
                    "status": "failed",
                }
            ]
        },
        "history": {"items": []},
    }
    monkeypatch.setattr(web_as_user, "_state", lambda _url: state)

    outcome, run_id = web_as_user._wait_for_terminal(
        "http://127.0.0.1:9",
        timeout_s=60,
        log_fn=lambda _msg: None,
        domain_filter={"queue"},
        queue_task_id="build-micro-twitter",
    )

    assert outcome == "failure"
    assert run_id == "run-1"


def test_web_as_user_queued_work_count_ignores_terminal_live_rows() -> None:
    """Stale terminal rows in live[] are not queued/running work."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    state = {
        "watcher": {"counts": {"queued": 0, "starting": 0, "initializing": 0, "running": 0}},
        "runtime": {"queue_tasks": 0, "state_tasks": 0},
        "live": {"items": [{"status": "failed", "terminal_outcome": "failure"}]},
        "landing": {"items": [{"queue_status": "failed"}]},
    }

    assert web_as_user._queued_work_count(state) == 0


def test_web_as_user_progress_signature_changes_on_visible_progress() -> None:
    """The W1 stall guard keys off Mission Control-visible row progress."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    first = {
        "live": {
            "items": [
                {
                    "queue_task_id": "build-micro-twitter",
                    "run_id": "run-1",
                    "status": "running",
                    "last_event": "build started",
                    "stories_passed": 0,
                }
            ]
        }
    }
    second = {
        "live": {
            "items": [
                {
                    "queue_task_id": "build-micro-twitter",
                    "run_id": "run-1",
                    "status": "running",
                    "last_event": "audit started",
                    "stories_passed": 3,
                }
            ]
        }
    }

    assert web_as_user._submitted_task_progress_signature(
        first,
        submitted_task_id="build-micro-twitter",
        submitted_run_id="run-1",
    ) != web_as_user._submitted_task_progress_signature(
        second,
        submitted_task_id="build-micro-twitter",
        submitted_run_id="run-1",
    )


def test_teardown_scenario_runtime_reports_leftover_processes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Process leaks after teardown must be surfaced as evidence, not hidden."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    monkeypatch.setattr(
        web_as_user,
        "_api_post",
        lambda *_args, **_kwargs: (200, {"ok": True}),
    )
    monkeypatch.setattr(
        web_as_user,
        "_terminate_project_processes",
        lambda _project: {
            "matched": [{"pid": 123, "pgid": 123, "command": "otto queue run"}],
            "after_sigkill": [{"pid": 123, "pgid": 123, "command": "otto queue run"}],
        },
    )

    report = web_as_user._teardown_scenario_runtime(
        project_dir=tmp_path,
        artifact_dir=tmp_path / "artifacts",
        web_url="http://127.0.0.1:1",
        keep_snapshot=False,
    )

    assert report["project_process_cleanup"]["after_sigkill"][0]["pid"] == 123
    assert (tmp_path / "artifacts" / "teardown.json").exists()


def test_web_as_user_signal_handler_ignores_self_signal_before_killpg(
    monkeypatch,
) -> None:
    """The cleanup handler must not recurse when it signals its own process group."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    installed: dict[int, object] = {}
    signal_calls: list[tuple[int, object]] = []

    def fake_signal(signum: int, handler: object) -> object:
        installed[signum] = handler
        signal_calls.append((signum, handler))
        return signal.SIG_DFL

    monkeypatch.setattr(web_as_user.signal, "signal", fake_signal)
    monkeypatch.setattr(web_as_user.os, "getpgrp", lambda: 12345)
    monkeypatch.setattr(web_as_user.os, "killpg", lambda _pgid, _sig: None)

    web_as_user._install_signal_handlers()
    handler = installed[signal.SIGTERM]

    try:
        handler(signal.SIGTERM, None)  # type: ignore[misc]
    except SystemExit as exc:
        assert exc.code == 143
    else:  # pragma: no cover
        raise AssertionError("signal handler did not exit")

    assert (signal.SIGTERM, signal.SIG_IGN) in signal_calls


def test_web_as_user_concurrency_journal_requires_batch_overlap(tmp_path: Path) -> None:
    """A concurrent true-web run must prove multiple groups actually executed concurrently."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    project = tmp_path / "project"
    session = project / ".worktrees" / "build-micro-twitter" / "otto_logs" / "sessions" / "run-1"
    session.mkdir(parents=True)
    (session / "proof-packet.json").write_text(
        '{"groups":[{"group_id":"feed"},{"group_id":"composer"}]}',
        encoding="utf-8",
    )
    (session / "spec-state.jsonl").write_text(
        "\n".join(
            [
                '{"event":"group.started","group_id":"feed","ts":"2026-05-06T01:00:00Z"}',
                '{"event":"group.started","group_id":"composer","ts":"2026-05-06T01:00:01Z"}',
                '{"event":"group.execution.started","group_id":"feed","ts":"2026-05-06T01:00:02Z"}',
                '{"event":"group.execution.started","group_id":"composer","ts":"2026-05-06T01:00:03Z"}',
                '{"event":"group.execution.finished","group_id":"feed","ts":"2026-05-06T01:00:09Z"}',
                '{"event":"group.merge.eligible","group_id":"feed","ts":"2026-05-06T01:00:10Z"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = web_as_user.ScenarioContext(
        scenario=web_as_user.SCENARIOS["W1"],
        project_dir=project,
        artifact_dir=tmp_path / "artifacts",
        provider="codex",
        failures=web_as_user.RunFailures(),
        debug_log=tmp_path / "debug.log",
        run_id="concurrency-test",
        group_concurrent=2,
    )
    failures = web_as_user.RunFailures()

    web_as_user._assert_group_concurrency_observed(
        ctx,
        run_id="run-1",
        log_fn=lambda _msg: None,
        failures=failures,
    )

    assert failures.failures == []
    report = json.loads((ctx.artifact_dir / "group-concurrency.json").read_text())
    assert report["started_before_first_terminal"] == ["feed", "composer"]
    assert report["execution_started_before_first_finished"] == ["feed", "composer"]
    assert report["max_execution_overlap"] == 2
    assert report["max_execution_overlap_groups"] == ["composer", "feed"]


def test_web_as_user_concurrency_journal_rejects_started_only_false_overlap(
    tmp_path: Path,
) -> None:
    """Started events alone are not proof of real async execution overlap."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    project = tmp_path / "project"
    session = project / ".worktrees" / "build-micro-twitter" / "otto_logs" / "sessions" / "run-1"
    session.mkdir(parents=True)
    (session / "proof-packet.json").write_text(
        '{"groups":[{"group_id":"feed"},{"group_id":"composer"}]}',
        encoding="utf-8",
    )
    (session / "spec-state.jsonl").write_text(
        "\n".join(
            [
                '{"event":"group.started","group_id":"feed","ts":"2026-05-06T01:00:00Z"}',
                '{"event":"group.started","group_id":"composer","ts":"2026-05-06T01:00:01Z"}',
                '{"event":"group.execution.started","group_id":"feed","ts":"2026-05-06T01:00:02Z"}',
                '{"event":"group.execution.finished","group_id":"feed","ts":"2026-05-06T01:00:10Z"}',
                '{"event":"group.execution.started","group_id":"composer","ts":"2026-05-06T01:00:11Z"}',
                '{"event":"group.execution.finished","group_id":"composer","ts":"2026-05-06T01:00:20Z"}',
                '{"event":"group.merge.eligible","group_id":"feed","ts":"2026-05-06T01:00:21Z"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = web_as_user.ScenarioContext(
        scenario=web_as_user.SCENARIOS["W1"],
        project_dir=project,
        artifact_dir=tmp_path / "artifacts",
        provider="codex",
        failures=web_as_user.RunFailures(),
        debug_log=tmp_path / "debug.log",
        run_id="concurrency-test",
        group_concurrent=2,
    )
    failures = web_as_user.RunFailures()

    web_as_user._assert_group_concurrency_observed(
        ctx,
        run_id="run-1",
        log_fn=lambda _msg: None,
        failures=failures,
    )

    assert failures.failures
    assert "within any dependency-ready wave" in failures.failures[0]


def test_web_as_user_concurrency_allows_dependency_gated_parallel_wave(
    tmp_path: Path,
) -> None:
    """Sequential foundation followed by a parallel sibling wave is real concurrency."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    project = tmp_path / "project"
    session = project / ".worktrees" / "build-issue-tracker" / "otto_logs" / "sessions" / "run-1"
    session.mkdir(parents=True)
    (session / "proof-packet.json").write_text(
        '{"groups":[{"group_id":"foundation"},{"group_id":"projects"},{"group_id":"views"},{"group_id":"filters"}]}',
        encoding="utf-8",
    )
    (session / "spec-state.jsonl").write_text(
        "\n".join(
            [
                '{"event":"group.execution.started","group_id":"foundation","ts":"2026-05-06T01:00:00Z"}',
                '{"event":"group.execution.finished","group_id":"foundation","ts":"2026-05-06T01:05:00Z"}',
                '{"event":"group.merge.eligible","group_id":"foundation","ts":"2026-05-06T01:05:01Z"}',
                '{"event":"group.execution.started","group_id":"projects","ts":"2026-05-06T01:05:02Z"}',
                '{"event":"group.execution.started","group_id":"views","ts":"2026-05-06T01:05:03Z"}',
                '{"event":"group.execution.started","group_id":"filters","ts":"2026-05-06T01:05:04Z"}',
                '{"event":"group.execution.finished","group_id":"filters","ts":"2026-05-06T01:09:00Z"}',
                '{"event":"group.execution.finished","group_id":"projects","ts":"2026-05-06T01:10:00Z"}',
                '{"event":"group.execution.finished","group_id":"views","ts":"2026-05-06T01:11:00Z"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = web_as_user.ScenarioContext(
        scenario=web_as_user.SCENARIOS["W1"],
        project_dir=project,
        artifact_dir=tmp_path / "artifacts",
        provider="codex",
        failures=web_as_user.RunFailures(),
        debug_log=tmp_path / "debug.log",
        run_id="concurrency-test",
        group_concurrent=3,
    )
    failures = web_as_user.RunFailures()

    web_as_user._assert_group_concurrency_observed(
        ctx,
        run_id="run-1",
        log_fn=lambda _msg: None,
        failures=failures,
    )

    assert failures.failures == []
    report = json.loads((ctx.artifact_dir / "group-concurrency.json").read_text())
    assert report["execution_started_before_first_finished"] == ["foundation"]
    assert report["max_execution_overlap"] == 3
    assert report["max_execution_overlap_groups"] == ["filters", "projects", "views"]


def test_web_as_user_concurrency_uses_queue_task_session_when_run_id_missing(
    tmp_path: Path,
) -> None:
    """Timeouts can lack a submitted run id; task worktree journals still prove concurrency."""
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        import web_as_user  # type: ignore[import-not-found]
    finally:
        if str(SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(SCRIPTS_DIR))

    project = tmp_path / "project"
    session = (
        project
        / ".worktrees"
        / "build-micro-twitter"
        / "otto_logs"
        / "sessions"
        / "run-1"
    )
    (session / "spec").mkdir(parents=True)
    (session / "spec" / "spec.json").write_text(
        '{"groups":[{"id":"feed"},{"id":"composer"}]}',
        encoding="utf-8",
    )
    (session / "spec-state.jsonl").write_text(
        "\n".join(
            [
                '{"event":"group.execution.started","group_id":"feed","ts":"2026-05-06T01:00:02Z"}',
                '{"event":"group.execution.started","group_id":"composer","ts":"2026-05-06T01:00:03Z"}',
                '{"event":"group.execution.finished","group_id":"feed","ts":"2026-05-06T01:00:09Z"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    ctx = web_as_user.ScenarioContext(
        scenario=web_as_user.SCENARIOS["W1"],
        project_dir=project,
        artifact_dir=tmp_path / "artifacts",
        provider="codex",
        failures=web_as_user.RunFailures(),
        debug_log=tmp_path / "debug.log",
        run_id="concurrency-test",
        group_concurrent=2,
    )
    failures = web_as_user.RunFailures()

    web_as_user._assert_group_concurrency_observed(
        ctx,
        run_id=None,
        queue_task_id="build-micro-twitter",
        log_fn=lambda _msg: None,
        failures=failures,
    )

    assert failures.failures == []
    report = json.loads((ctx.artifact_dir / "group-concurrency.json").read_text())
    assert report["group_count"] == 2
    assert report["session_dir"] == str(session)


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
