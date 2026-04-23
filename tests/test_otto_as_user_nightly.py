from __future__ import annotations

import importlib.util
import subprocess
import sys
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
    steps = OTTO_AS_USER_NIGHTLY.SCENARIO_SPECS["N9"].step_plan
    assert steps[0] == "open Mission Control against ./otto_logs/"
    assert any("--allow-dirty" in step for step in steps)
    assert any("--concurrent 2" in step for step in steps)
    assert any("heartbeat progress" in step for step in steps)
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


def test_main_dry_run_supports_each_core_scenario(capsys) -> None:
    for scenario_id in ("N1", "N2", "N4", "N8", "N9"):
        assert OTTO_AS_USER_NIGHTLY.main(["--dry-run", "--scenario", scenario_id]) == 0
        out = capsys.readouterr().out
        assert f"{scenario_id}:" in out
        assert "provider: claude" in out


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
            "queue-heartbeat-advanced": True,
            "queue-log-cycled": True,
            "cancel-ack-latency-ms": 300,
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
            "queue-heartbeat-advanced": True,
            "queue-log-cycled": True,
            "cancel-ack-latency-ms": 300,
            "queue-cancelled-latency-ms": 400,
            "editor-spawn-attempted": True,
            "merge-spawn-argv": ["otto", "merge", "--all"],
            "history-terminal-snapshot-count": 4,
            "history-terminal-outcomes": {"queue-cancelled": "cancelled"},
            "cancelled-queue-run-id": "queue-cancelled",
            "history-artifacts-resolve": True,
            "live-records-terminal-after-gc": True,
        },
    )

    result = OTTO_AS_USER_NIGHTLY.verify_n9(repo, run_result)

    assert result.passed is False
    assert result.note == "N9 merge was not launched from selected queue rows"
