from __future__ import annotations

import importlib.util
import sys
import types
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


def test_run_n9_checks_out_main_after_cancel_and_before_merge(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    session_dir = repo / ".otto" / "sessions" / "build-run"
    session_dir.mkdir(parents=True)
    commands_dir = session_dir / "commands"
    request_path = commands_dir / "requests.jsonl"
    ack_path = commands_dir / "acks.jsonl"

    ctx = OTTO_AS_USER_NIGHTLY.base.ExecutionContext(
        scenario=OTTO_AS_USER_NIGHTLY.SCENARIOS["N9"],
        artifact_dir=artifact_dir,
        repo=repo,
        provider="claude",
        debug_log=artifact_dir / "debug.log",
        recording_path=artifact_dir / "recording.cast",
    )
    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY.base, "EXECUTION_CONTEXT", ctx)

    paths_module = types.ModuleType("otto.paths")
    paths_module.session_dir = lambda _repo, _run_id: session_dir
    paths_module.session_commands_dir = lambda _repo, _run_id: commands_dir
    paths_module.session_command_requests = lambda _repo, _run_id: request_path
    paths_module.session_command_acks = lambda _repo, _run_id: ack_path

    build_record = types.SimpleNamespace(run_id="build-run", domain="build", timing={})
    queue_record_one = types.SimpleNamespace(run_id="queue-1", domain="queue")
    queue_record_two = types.SimpleNamespace(run_id="queue-2", domain="queue")
    ack_ids: list[str] = []

    registry_module = types.ModuleType("otto.runs.registry")
    registry_module.HEARTBEAT_INTERVAL_S = 1.0
    registry_module.allocate_run_id = lambda _repo: "build-run"
    registry_module.append_jsonl_row = lambda _path, payload: ack_ids.append(payload["command_id"])
    registry_module.load_command_ack_ids = lambda _path: set(ack_ids)
    registry_module.load_live_record = lambda _repo, _run_id: build_record
    registry_module.read_live_records = lambda _repo: [build_record, queue_record_one, queue_record_two]
    registry_module.utc_now_iso = lambda: "2026-04-23T00:00:00Z"

    otto_module = types.ModuleType("otto")
    otto_module.paths = paths_module
    runs_module = types.ModuleType("otto.runs")
    runs_module.registry = registry_module
    monkeypatch.setitem(sys.modules, "otto", otto_module)
    monkeypatch.setitem(sys.modules, "otto.paths", paths_module)
    monkeypatch.setitem(sys.modules, "otto.runs", runs_module)
    monkeypatch.setitem(sys.modules, "otto.runs.registry", registry_module)

    events: list[tuple[str, tuple[str, ...]]] = []

    class FakePopen:
        _pid = 1000

        def __init__(self, argv, **kwargs):
            del kwargs
            self.argv = argv
            self.pid = FakePopen._pid
            FakePopen._pid += 1
            self._done = False
            self._rc = 130 if argv[1] == "build" else 0

        def poll(self):
            return self._rc if self._done else None

        def wait(self, timeout=None):
            del timeout
            self._done = True
            return self._rc

    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(
        OTTO_AS_USER_NIGHTLY.base,
        "queue_build",
        lambda _repo, task_id, _provider, _intent: events.append((f"queue:{task_id}", ())),
    )
    monkeypatch.setattr(
        OTTO_AS_USER_NIGHTLY.base,
        "run_checked",
        lambda argv, *, cwd, env=None: (
            cwd == repo and env is None and events.append(("run_checked", tuple(argv)))
        ),
    )
    monkeypatch.setattr(
        OTTO_AS_USER_NIGHTLY.base,
        "run_merge",
        lambda _repo, *args, timeout_s: (
            events.append(("merge", tuple(args)))
            or OTTO_AS_USER_NIGHTLY.base.CommandResult(
                argv=[str(OTTO_AS_USER_NIGHTLY.base.OTTO_BIN), "merge", *args],
                rc=0,
                duration_s=0.2,
                output="merge ok",
            )
        ),
    )

    result = OTTO_AS_USER_NIGHTLY.run_n9(repo, "claude")

    assert result.returncode == 0
    assert [event for event in events if event[0] == "run_checked"] == [
        ("run_checked", ("git", "checkout", "main")),
        ("run_checked", ("git", "checkout", "main")),
    ]
    assert events.index(("run_checked", ("git", "checkout", "main"))) < events.index(
        ("merge", ("--all", "--cleanup-on-success"))
    )
    step_labels = [step["label"] for step in result.details["steps"]]
    assert "git-checkout-main-after-cancel" in step_labels
    assert "git-checkout-main-before-merge" in step_labels
    assert step_labels[-1] == "merge"
