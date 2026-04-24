from __future__ import annotations

import importlib
import importlib.util
import json
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "otto_as_user_nightly.py"
SCRIPTS_DIR = SCRIPT_PATH.parent

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SPEC = importlib.util.spec_from_file_location("tests._otto_as_user_nightly_script", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
OTTO_AS_USER_NIGHTLY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = OTTO_AS_USER_NIGHTLY
SPEC.loader.exec_module(OTTO_AS_USER_NIGHTLY)


def test_n9_is_registered_without_n10() -> None:
    assert "N9" in OTTO_AS_USER_NIGHTLY.SCENARIOS
    assert "N9" in OTTO_AS_USER_NIGHTLY.SCENARIO_SPECS
    assert OTTO_AS_USER_NIGHTLY.SCENARIOS["N9"].description == "realistic operator session"
    assert "N10" not in OTTO_AS_USER_NIGHTLY.SCENARIOS
    assert "N10" not in OTTO_AS_USER_NIGHTLY.SCENARIO_SPECS


def test_n9_step_plan_mentions_realistic_operator_session() -> None:
    spec = OTTO_AS_USER_NIGHTLY.SCENARIO_SPECS["N9"]
    steps = spec.step_plan
    queue_cancel_idx = next(i for i, step in enumerate(steps) if "presses c while both queue tasks are still running" in step)
    other_queue_finish_idx = next(i for i, step in enumerate(steps) if "other queue task to finish naturally" in step)
    standalone_settle_idx = next(i for i, step in enumerate(steps) if "8 minutes" in step and "presses c" in step)
    merge_evidence_idx = next(i for i, step in enumerate(steps) if "merge evidence" in step and "30s" in step)

    assert spec.est_cost_range == "$2.5-$4.5"
    assert spec.budget_s == 25 * 60
    assert steps[0] == "open Mission Control against ./otto_logs/"
    assert any("--allow-dirty" in step and "GET /tasks endpoint that returns the current task list as JSON." in step for step in steps)
    assert any("--concurrent 2" in step for step in steps)
    assert any("within 5s" in step for step in steps)
    assert any("within 4s" in step for step in steps)
    assert queue_cancel_idx < other_queue_finish_idx < standalone_settle_idx < merge_evidence_idx
    assert any("presses e" in step for step in steps)
    assert any("not --all" in step for step in steps)
    assert steps[-2:] == [
        "pytest tests/visible -q --tb=short",
        "pytest tests/hidden -q --tb=short",
    ]


def test_main_list_succeeds(capsys) -> None:
    assert OTTO_AS_USER_NIGHTLY.main(["--list"]) == 0
    out = capsys.readouterr().out
    for scenario_id in ("N1", "N2", "N4", "N8", "N9"):
        assert scenario_id in out
    assert "N10" not in out


def test_all_registered_scenario_intents_exist() -> None:
    missing = [
        f"{scenario_id}: {spec.intent_path}"
        for scenario_id, spec in OTTO_AS_USER_NIGHTLY.SCENARIO_SPECS.items()
        if not spec.intent_path.exists()
    ]
    assert missing == []


def test_main_dry_run_supports_each_core_scenario(capsys) -> None:
    for scenario_id in ("N1", "N2", "N4", "N8", "N9"):
        assert OTTO_AS_USER_NIGHTLY.main(["--dry-run", "--scenario", scenario_id]) == 0
        out = capsys.readouterr().out
        assert f"{scenario_id}:" in out
        assert "provider: claude" in out


def test_debug_fast_args_respects_env(monkeypatch) -> None:
    monkeypatch.delenv("OTTO_DEBUG_FAST", raising=False)
    assert OTTO_AS_USER_NIGHTLY._debug_fast_args() == []

    monkeypatch.setenv("OTTO_DEBUG_FAST", "0")
    assert OTTO_AS_USER_NIGHTLY._debug_fast_args() == []

    monkeypatch.setenv("OTTO_DEBUG_FAST", "1")
    assert OTTO_AS_USER_NIGHTLY._debug_fast_args() == ["--fast"]


def test_run_failures_accumulates_and_returns_first() -> None:
    failures = OTTO_AS_USER_NIGHTLY.RunFailures()

    assert failures.soft_assert(True, "unused") is True
    assert failures.soft_assert(False, "first failure") is False
    failures.fail("second failure")
    failures.note("kept going")

    assert failures.first() == "first failure"
    assert failures.all() == ["first failure", "second failure"]
    assert failures.notes == ["kept going"]


def test_audit_artifacts_post_run_reports_dangling_history_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    history_dir = repo / "otto_logs" / "cross-sessions"
    history_dir.mkdir(parents=True)
    history_path = history_dir / "history.jsonl"
    history_path.write_text(
        json.dumps(
            {
                "run_id": "run-1",
                "manifest_path": str(repo / "missing-manifest.json"),
                "summary_path": str(repo / "missing-summary.json"),
                "primary_log_path": str(repo / "missing.log"),
                "terminal_outcome": "mystery",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    failures = OTTO_AS_USER_NIGHTLY.RunFailures()
    details: dict[str, object] = {"merge-spawn-argv": []}

    OTTO_AS_USER_NIGHTLY._audit_artifacts_post_run(repo, failures, details)

    assert failures.all() == [
        f"audit: history row run-1.manifest_path dangles: {repo / 'missing-manifest.json'}",
        f"audit: history row run-1.summary_path dangles: {repo / 'missing-summary.json'}",
        f"audit: history row run-1.primary_log_path dangles: {repo / 'missing.log'}",
        "audit: history row run-1 has invalid terminal_outcome='mystery'",
    ]


def test_run_n9_uses_fast_args_for_build_and_queue(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    debug_log = artifact_dir / "debug.log"
    recording_path = artifact_dir / "recording.cast"
    monkeypatch.setenv("OTTO_DEBUG_FAST", "1")

    scenario = OTTO_AS_USER_NIGHTLY.SCENARIOS["N9"]
    ctx = OTTO_AS_USER_NIGHTLY.base.ExecutionContext(
        scenario=scenario,
        artifact_dir=artifact_dir,
        repo=repo,
        provider="claude",
        debug_log=debug_log,
        recording_path=recording_path,
    )
    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY.base, "EXECUTION_CONTEXT", ctx)

    registry = importlib.import_module("otto.runs.registry")
    monkeypatch.setattr(registry, "allocate_run_id", lambda repo_arg: "build-run-id")
    monkeypatch.setattr(registry, "garbage_collect_live_records", lambda repo_arg, terminal_retention_s=0: [])
    monkeypatch.setattr(registry, "read_live_records", lambda repo_arg: [])

    queue_calls: list[dict[str, object]] = []

    def fake_queue_build(repo_arg: Path, task_id: str, provider: str, intent: str, *extra_inner: str) -> None:
        queue_calls.append(
            {
                "repo": repo_arg,
                "task_id": task_id,
                "provider": provider,
                "intent": intent,
                "extra_inner": extra_inner,
            }
        )

    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY.base, "queue_build", fake_queue_build)

    log_lines: list[str] = []
    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY.base, "log_line", lambda text: log_lines.append(text))

    @contextmanager
    def fake_capture_mission_control_action_spawns(*, stderr_paths=None):
        assert stderr_paths == {"merge": artifact_dir / "merge-stderr.log"}
        yield []

    monkeypatch.setattr(
        OTTO_AS_USER_NIGHTLY,
        "_capture_mission_control_action_spawns",
        fake_capture_mission_control_action_spawns,
    )

    async def fake_drive_n9_mission_control(**kwargs):
        kwargs["start_build_flow"]()
        kwargs["start_queue_flow"]()
        kwargs["runtime"]["merge_step"] = {
            "label": "merge-selected",
            "argv": ["otto", "merge", "add-post"],
            "rc": 0,
            "duration_s": 0.1,
        }

    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY, "_drive_n9_mission_control", fake_drive_n9_mission_control)
    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY, "_read_terminal_snapshots", lambda repo_arg: [])

    popen_calls: list[dict[str, object]] = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            self.argv = [str(part) for part in argv]
            self.kwargs = kwargs
            self.pid = 1000 + len(popen_calls)
            self._returncode = None
            popen_calls.append({"argv": self.argv, "kwargs": kwargs, "proc": self})

        def poll(self):
            return self._returncode

        def wait(self, timeout=None):
            self._returncode = 0
            return 0

    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY.subprocess, "Popen", FakePopen)

    result = OTTO_AS_USER_NIGHTLY.run_n9(repo, "claude")

    assert result.returncode == 0
    assert [call["task_id"] for call in queue_calls] == ["add-post", "add-delete"]
    assert all(call["extra_inner"] == ("--fast",) for call in queue_calls)
    assert popen_calls[0]["argv"] == [
        str(OTTO_AS_USER_NIGHTLY.base.OTTO_BIN),
        "build",
        "--provider",
        "claude",
        "--allow-dirty",
        "--fast",
        OTTO_AS_USER_NIGHTLY.N9_BUILD_INTENT,
    ]
    assert hasattr(popen_calls[0]["kwargs"]["stderr"], "write")
    assert popen_calls[0]["kwargs"]["stderr"] is not subprocess.STDOUT
    assert hasattr(popen_calls[1]["kwargs"]["stderr"], "write")
    assert popen_calls[1]["kwargs"]["stderr"] is not subprocess.STDOUT
    assert log_lines.count("[N9] OTTO_DEBUG_FAST=1 — using --fast for all otto invocations") == 1


def test_n9_merge_spawn_matcher_accepts_direct_and_module_argv() -> None:
    assert OTTO_AS_USER_NIGHTLY._argv_invokes_otto_subcommand(["otto", "merge", "add-post"], "merge") is True
    assert (
        OTTO_AS_USER_NIGHTLY._argv_invokes_otto_subcommand(
            [sys.executable, "-m", "otto.cli", "merge", "add-post"],
            "merge",
    )
    is True
    )
    assert OTTO_AS_USER_NIGHTLY._argv_invokes_otto_subcommand(["true", "intent.txt"], "merge") is False


def test_capture_mission_control_action_spawns_persists_merge_stderr(monkeypatch, tmp_path: Path) -> None:
    mission_control_actions = importlib.import_module("otto.mission_control.actions")
    calls: list[dict[str, object]] = []

    class FakeProc:
        pid = 4242
        returncode = 1

        def communicate(self):
            return ("", "merge exploded\ntraceback")

    def fake_popen(argv, *args, **kwargs):
        calls.append({"argv": [str(part) for part in argv], "kwargs": kwargs})
        return FakeProc()

    fake_subprocess = type("FakeSubprocess", (), {"Popen": staticmethod(fake_popen)})()
    monkeypatch.setattr(mission_control_actions, "subprocess", fake_subprocess)

    stderr_path = tmp_path / "merge-stderr.log"
    with OTTO_AS_USER_NIGHTLY._capture_mission_control_action_spawns(stderr_paths={"merge": stderr_path}) as spawns:
        proc = mission_control_actions.subprocess.Popen(
            ["otto", "merge", "add-post"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proc.communicate()

    assert calls[0]["argv"] == ["otto", "merge", "add-post"]
    assert calls[0]["kwargs"]["stderr"] is subprocess.PIPE
    assert spawns[0]["stderr_path"] == stderr_path
    assert stderr_path.read_text(encoding="utf-8") == "merge exploded\ntraceback\n"


def test_checkout_main_before_merge_step_uses_checked_helper(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: list[tuple[str, list[str], Path]] = []

    def fake_run_checked_step(label: str, argv: list[str], *, repo: Path, env=None):
        calls.append((label, argv, repo))
        return {"label": label, "argv": argv, "rc": 0}

    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY, "_run_checked_step", fake_run_checked_step)

    step = OTTO_AS_USER_NIGHTLY._checkout_main_before_merge_step(repo)

    assert step == {
        "label": "git-checkout-main-before-merge",
        "argv": ["git", "checkout", "main"],
        "rc": 0,
    }
    assert calls == [
        ("git-checkout-main-before-merge", ["git", "checkout", "main"], repo),
    ]


def test_record_one_scenario_honors_classification_override(monkeypatch, tmp_path: Path) -> None:
    scenario = OTTO_AS_USER_NIGHTLY.SCENARIOS["N9"]
    run_result = OTTO_AS_USER_NIGHTLY.base.RunResult(
        scenario_id="N9",
        returncode=1,
        started_at="2026-04-23T00:00:00Z",
        finished_at="2026-04-23T00:00:01Z",
        duration_s=1.0,
        recording_path=str(tmp_path / "recording.cast"),
        repo_path=str(tmp_path / "repo"),
        debug_log=str(tmp_path / "debug.log"),
        output="Mission Control never reflected the cancelled queue row",
        details={"classification_override": "INFRA"},
    )
    verify_result = OTTO_AS_USER_NIGHTLY.VerifyResult(False, "expected infra")

    def fake_run_one_attempt(*args, **kwargs):
        return tmp_path / "repo", run_result, verify_result

    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY, "DEFAULT_ARTIFACT_ROOT", tmp_path)
    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY, "run_one_attempt", fake_run_one_attempt)
    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY.base, "INFRA_RETRY_DELAY_S", 0.0)

    outcome = OTTO_AS_USER_NIGHTLY.record_one_scenario(scenario, "run-123", "claude")

    assert outcome.outcome == "INFRA"


def test_verify_n9_checks_realistic_session_and_uses_visible_hidden(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    merge_state = repo / "otto_logs" / "merge" / "merge-1" / "state.json"
    merge_state.parent.mkdir(parents=True)
    merge_state.write_text("{}", encoding="utf-8")
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    calls: list[str] = []

    def fake_run_pytest(repo_arg: Path, target: str, artifact_dir_arg: Path, attempt_index: int):
        assert repo_arg == repo
        assert artifact_dir_arg == artifact_dir
        assert attempt_index == 1
        calls.append(target)
        return subprocess.CompletedProcess(args=["pytest", target], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY, "_run_pytest", fake_run_pytest)

    run_result = OTTO_AS_USER_NIGHTLY.base.RunResult(
        scenario_id="N9",
        returncode=0,
        started_at="2026-04-23T00:00:00Z",
        finished_at="2026-04-23T00:10:00Z",
        duration_s=600.0,
        recording_path=str(artifact_dir / "recording.cast"),
        repo_path=str(repo),
        debug_log=str(artifact_dir / "debug.log"),
        output="",
        details={
            "build-live-row-latency-ms": 500,
            "build-finished-naturally": True,
            "standalone-heartbeat-advanced": True,
            "standalone-log-cycled": True,
            "queue-cancel-history-latency-ms": 300,
            "queue-cancelled-latency-ms": 400,
            "editor-spawn-attempted": True,
            "merge-spawn-argv": ["otto", "merge", "add-post"],
            "history-terminal-snapshot-count": 4,
            "history-terminal-outcomes": {
                "build-run": "success",
                "queue-success": "success",
                "queue-cancelled": "cancelled",
                "merge-run": "success",
            },
            "cancelled-queue-run-id": "queue-cancelled",
            "history-artifacts-resolve": True,
            "live-records-terminal-after-gc": True,
        },
    )

    result = OTTO_AS_USER_NIGHTLY.verify_n9(repo, run_result)

    assert result.passed is True
    assert calls == ["tests/visible", "tests/hidden"]


def test_verify_n9_rejects_merge_all(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    run_result = OTTO_AS_USER_NIGHTLY.base.RunResult(
        scenario_id="N9",
        returncode=0,
        started_at="2026-04-23T00:00:00Z",
        finished_at="2026-04-23T00:10:00Z",
        duration_s=600.0,
        recording_path=str(artifact_dir / "recording.cast"),
        repo_path=str(repo),
        debug_log=str(artifact_dir / "debug.log"),
        output="",
        details={
            "build-live-row-latency-ms": 500,
            "build-finished-naturally": True,
            "standalone-heartbeat-advanced": True,
            "standalone-log-cycled": True,
            "queue-cancel-history-latency-ms": 300,
            "queue-cancelled-latency-ms": 400,
            "editor-spawn-attempted": True,
            "merge-spawn-argv": ["otto", "merge", "--all"],
            "history-terminal-snapshot-count": 3,
            "history-terminal-outcomes": {
                "queue-cancelled": "cancelled",
                "queue-success": "success",
                "merge-run": "success",
            },
            "cancelled-queue-run-id": "queue-cancelled",
            "history-artifacts-resolve": True,
            "live-records-terminal-after-gc": True,
        },
    )

    result = OTTO_AS_USER_NIGHTLY.verify_n9(repo, run_result)

    assert result.passed is False
    assert result.note == "N9 merge was not launched from selected queue rows"


def test_verify_n9_accepts_legacy_queue_cancel_ack_field(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    merge_state = repo / "otto_logs" / "merge" / "merge-1" / "state.json"
    merge_state.parent.mkdir(parents=True)
    merge_state.write_text("{}", encoding="utf-8")
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    def fake_run_pytest(repo_arg: Path, target: str, artifact_dir_arg: Path, attempt_index: int):
        assert repo_arg == repo
        assert artifact_dir_arg == artifact_dir
        assert attempt_index == 1
        return subprocess.CompletedProcess(args=["pytest", target], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY, "_run_pytest", fake_run_pytest)

    run_result = OTTO_AS_USER_NIGHTLY.base.RunResult(
        scenario_id="N9",
        returncode=0,
        started_at="2026-04-23T00:00:00Z",
        finished_at="2026-04-23T00:10:00Z",
        duration_s=600.0,
        recording_path=str(artifact_dir / "recording.cast"),
        repo_path=str(repo),
        debug_log=str(artifact_dir / "debug.log"),
        output="",
        details={
            "build-live-row-latency-ms": 500,
            "build-finished-naturally": True,
            "standalone-heartbeat-advanced": True,
            "standalone-log-cycled": True,
            "cancel-ack-latency-ms": 300,
            "queue-cancelled-latency-ms": 400,
            "editor-spawn-attempted": True,
            "merge-spawn-argv": ["otto", "merge", "add-post"],
            "history-terminal-snapshot-count": 3,
            "history-terminal-outcomes": {
                "queue-cancelled": "cancelled",
                "queue-success": "success",
                "merge-run": "success",
            },
            "cancelled-queue-run-id": "queue-cancelled",
            "history-artifacts-resolve": True,
            "live-records-terminal-after-gc": True,
        },
    )

    result = OTTO_AS_USER_NIGHTLY.verify_n9(repo, run_result)

    assert result.passed is True


def test_verify_n9_accepts_merge_live_record_fallback(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    merge_state = repo / "otto_logs" / "merge" / "merge-1" / "state.json"
    merge_state.parent.mkdir(parents=True)
    merge_state.write_text("{}", encoding="utf-8")
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    def fake_run_pytest(repo_arg: Path, target: str, artifact_dir_arg: Path, attempt_index: int):
        assert repo_arg == repo
        assert artifact_dir_arg == artifact_dir
        assert attempt_index == 1
        return subprocess.CompletedProcess(args=["pytest", target], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY, "_run_pytest", fake_run_pytest)

    run_result = OTTO_AS_USER_NIGHTLY.base.RunResult(
        scenario_id="N9",
        returncode=0,
        started_at="2026-04-23T00:00:00Z",
        finished_at="2026-04-23T00:10:00Z",
        duration_s=600.0,
        recording_path=str(artifact_dir / "recording.cast"),
        repo_path=str(repo),
        debug_log=str(artifact_dir / "debug.log"),
        output="",
        details={
            "build-live-row-latency-ms": 500,
            "build-finished-naturally": True,
            "standalone-heartbeat-advanced": True,
            "standalone-log-cycled": True,
            "queue-cancel-history-latency-ms": 300,
            "queue-cancelled-latency-ms": 400,
            "editor-spawn-attempted": True,
            "merge-live-record-seen": True,
            "merge-history-run-id": "merge-run",
            "merge-history-terminal-outcome": "success",
            "history-terminal-snapshot-count": 4,
            "history-terminal-outcomes": {
                "queue-cancelled": "cancelled",
                "queue-success": "success",
                "build-run": "success",
                "merge-run": "success",
            },
            "cancelled-queue-run-id": "queue-cancelled",
            "history-artifacts-resolve": True,
            "live-records-terminal-after-gc": True,
        },
    )

    result = OTTO_AS_USER_NIGHTLY.verify_n9(repo, run_result)

    assert result.passed is True
