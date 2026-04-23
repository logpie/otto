from __future__ import annotations

import importlib.util
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


def test_n9_is_registered() -> None:
    assert "N9" in OTTO_AS_USER_NIGHTLY.SCENARIOS
    assert "N9" in OTTO_AS_USER_NIGHTLY.SCENARIO_SPECS
    assert OTTO_AS_USER_NIGHTLY.SCENARIOS["N9"].description == "mission control workflow"


def test_n9_step_plan_mentions_cancelled_background_build() -> None:
    steps = OTTO_AS_USER_NIGHTLY.SCENARIO_SPECS["N9"].step_plan
    assert any("background, will be cancelled" in step for step in steps)
    assert any("--allow-dirty" in step for step in steps)
    assert any("harness appends cancel envelope" in step for step in steps)
    assert any(step == "otto merge --all --cleanup-on-success" for step in steps)


def test_main_list_succeeds(capsys) -> None:
    assert OTTO_AS_USER_NIGHTLY.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "N1" in out
    assert "N2" in out
    assert "N4" in out
    assert "N8" in out
    assert "N9" in out


def test_main_dry_run_supports_each_core_scenario(capsys) -> None:
    for scenario_id in ("N1", "N2", "N4", "N8"):
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
        output="standalone build finished before cancel ack",
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
