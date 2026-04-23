from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import types
from pathlib import Path

from otto import paths


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "otto_as_user_nightly.py"
SCRIPTS_DIR = SCRIPT_PATH.parent

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SPEC = importlib.util.spec_from_file_location("tests._otto_as_user_nightly_script", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
OTTO_AS_USER_NIGHTLY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = OTTO_AS_USER_NIGHTLY
SPEC.loader.exec_module(OTTO_AS_USER_NIGHTLY)


def test_n9_and_n10_are_registered() -> None:
    assert "N9" in OTTO_AS_USER_NIGHTLY.SCENARIOS
    assert "N9" in OTTO_AS_USER_NIGHTLY.SCENARIO_SPECS
    assert OTTO_AS_USER_NIGHTLY.SCENARIOS["N9"].description == "mission control workflow"
    assert "N10" in OTTO_AS_USER_NIGHTLY.SCENARIOS
    assert "N10" in OTTO_AS_USER_NIGHTLY.SCENARIO_SPECS
    assert OTTO_AS_USER_NIGHTLY.SCENARIOS["N10"].description == "mission control pilot integration"


def test_n9_step_plan_mentions_cancelled_background_build() -> None:
    steps = OTTO_AS_USER_NIGHTLY.SCENARIO_SPECS["N9"].step_plan
    assert any("background, will be cancelled" in step for step in steps)
    assert any("--allow-dirty" in step for step in steps)
    assert any("harness appends cancel envelope" in step for step in steps)
    assert any(step == "otto merge --all --cleanup-on-success" for step in steps)


def test_n10_step_plan_mentions_pilot_and_cancelled_build() -> None:
    steps = OTTO_AS_USER_NIGHTLY.SCENARIO_SPECS["N10"].step_plan
    assert any("background, will be cancelled via Mission Control" in step for step in steps)
    assert any("pilot.run_test" in step for step in steps)
    assert any("presses c" in step for step in steps)
    assert any(step == "pytest tests/visible -q --tb=short" for step in steps)


def test_main_list_succeeds(capsys) -> None:
    assert OTTO_AS_USER_NIGHTLY.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "N1" in out
    assert "N2" in out
    assert "N4" in out
    assert "N8" in out
    assert "N9" in out
    assert "N10" in out


def test_main_dry_run_supports_each_core_scenario(capsys) -> None:
    for scenario_id in ("N1", "N2", "N4", "N8", "N9", "N10"):
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


def test_verify_n10_checks_cancelled_artifacts_and_uses_visible_only(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    summary_path = repo / "otto_logs" / "sessions" / "build-run" / "summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps({"status": "cancelled"}), encoding="utf-8")

    intent_path = repo / "intent.md"
    intent_path.write_text("seeded intent\n", encoding="utf-8")
    manifest_path = repo / "otto_logs" / "sessions" / "build-run" / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    primary_log_path = repo / "otto_logs" / "sessions" / "build-run" / "build" / "narrative.log"
    primary_log_path.parent.mkdir(parents=True, exist_ok=True)
    primary_log_path.write_text("cancelled\n", encoding="utf-8")

    history_path = repo / "otto_logs" / "cross-sessions" / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "history_kind": "terminal_snapshot",
                "run_id": "build-run",
                "terminal_outcome": "cancelled",
                "dedupe_key": "terminal_snapshot:build-run",
                "intent_path": str(intent_path),
                "manifest_path": str(manifest_path),
                "summary_path": str(summary_path),
                "primary_log_path": str(primary_log_path),
                "artifacts": {
                    "manifest_path": str(manifest_path),
                    "summary_path": str(summary_path),
                    "primary_log_path": str(primary_log_path),
                    "extra_log_paths": [],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    calls: list[str] = []

    def fake_run_pytest(repo_arg: Path, target: str, artifact_dir_arg: Path, attempt_index: int):
        assert repo_arg == repo
        assert artifact_dir_arg == artifact_dir
        assert attempt_index == 1
        calls.append(target)
        return subprocess.CompletedProcess(args=["pytest", target], returncode=0, stdout="", stderr="")

    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY, "_run_pytest", fake_run_pytest)

    run_result = OTTO_AS_USER_NIGHTLY.base.RunResult(
        scenario_id="N10",
        returncode=0,
        started_at="2026-04-23T00:00:00Z",
        finished_at="2026-04-23T00:01:00Z",
        duration_s=60.0,
        recording_path=str(artifact_dir / "recording.cast"),
        repo_path=str(repo),
        debug_log=str(artifact_dir / "debug.log"),
        output="",
        details={
            "build_record_latency_ms": 250,
            "tui-live-row-latency-ms": 350,
            "tui-artifact-count-before-cancel": 3,
            "tui-cancelled-latency-ms": 200,
            "tui-refresh-window-ms": 1000,
            "build-terminated-via-cancel": True,
            "summary-status": "cancelled",
            "history-terminal-snapshot-count": 1,
            "history-terminal-outcome": "cancelled",
            "history-artifacts-resolve": True,
            "live-record-state-after-cancel": "cancelled",
        },
    )

    result = OTTO_AS_USER_NIGHTLY.verify_n10(repo, run_result)

    assert result.passed is True
    assert calls == ["tests/visible"]


def test_run_n10_records_single_cancelled_terminal_snapshot(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()

    ctx = OTTO_AS_USER_NIGHTLY.base.ExecutionContext(
        scenario=OTTO_AS_USER_NIGHTLY.SCENARIOS["N10"],
        artifact_dir=artifact_dir,
        repo=repo,
        provider="claude",
        debug_log=artifact_dir / "debug.log",
        recording_path=artifact_dir / "recording.cast",
    )
    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY.base, "EXECUTION_CONTEXT", ctx)

    shared = {"status": "running"}
    build_run_id = "build-run"
    session_dir = paths.session_dir(repo, build_run_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    build_log = paths.build_dir(repo, build_run_id) / "narrative.log"
    build_log.parent.mkdir(parents=True, exist_ok=True)
    build_log.write_text("running\n", encoding="utf-8")

    class FakePopen:
        def __init__(self, argv, **kwargs):
            del kwargs
            self.argv = argv
            self.pid = 5151
            self._done = False
            self._rc = 130

        def poll(self):
            return self._rc if self._done else None

        def wait(self, timeout=None):
            del timeout
            self._done = True
            return self._rc

    proc_holder: dict[str, FakePopen] = {}

    def fake_popen(argv, **kwargs):
        proc = FakePopen(argv, **kwargs)
        proc_holder["proc"] = proc
        return proc

    class FakeRecord:
        run_id = build_run_id
        domain = "atomic"

        @property
        def status(self):
            return shared["status"]

    class FakeLiveTable:
        row_count = 1

        def get_row_at(self, index):
            assert index == 0
            return (shared["status"].upper(), "build", build_run_id, "-", "-", "-", "-")

    class FakeArtifactsTable:
        row_count = 1

    class FakeStatic:
        @property
        def content(self):
            return f"run id: {build_run_id}\nstatus: {shared['status']}\n"

    class FakePilot:
        def __init__(self, app):
            self.app = app

        async def press(self, key):
            if key == "enter":
                self.app.state.focus = "detail"
                return
            if key == "c":
                shared["status"] = "cancelled"
                proc_holder["proc"]._done = True
                summary_path = paths.session_summary(repo, build_run_id)
                summary_path.parent.mkdir(parents=True, exist_ok=True)
                summary_path.write_text(
                    json.dumps({"run_id": build_run_id, "status": "cancelled"}),
                    encoding="utf-8",
                )
                manifest_path = session_dir / "manifest.json"
                manifest_path.write_text("{}", encoding="utf-8")
                intent_path = repo / "intent.md"
                intent_path.write_text("seeded intent\n", encoding="utf-8")
                history_path = paths.history_jsonl(repo)
                history_path.parent.mkdir(parents=True, exist_ok=True)
                history_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 2,
                            "history_kind": "terminal_snapshot",
                            "dedupe_key": f"terminal_snapshot:{build_run_id}",
                            "run_id": build_run_id,
                            "domain": "atomic",
                            "run_type": "build",
                            "command": "build",
                            "status": "cancelled",
                            "terminal_outcome": "cancelled",
                            "intent_path": str(intent_path.resolve()),
                            "manifest_path": str(manifest_path.resolve()),
                            "summary_path": str(summary_path.resolve()),
                            "primary_log_path": str(build_log.resolve()),
                            "artifacts": {
                                "manifest_path": str(manifest_path.resolve()),
                                "summary_path": str(summary_path.resolve()),
                                "primary_log_path": str(build_log.resolve()),
                                "extra_log_paths": [],
                            },
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )

        async def pause(self, *_args):
            return None

    class FakeRunTest:
        def __init__(self, app):
            self.app = app

        async def __aenter__(self):
            return FakePilot(self.app)

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeMissionControlApp:
        def __init__(self, project_dir):
            assert Path(project_dir) == repo
            self.state = types.SimpleNamespace(
                focus="live",
                selection=types.SimpleNamespace(run_id=build_run_id),
                live_runs=types.SimpleNamespace(
                    refresh_interval_s=0.5,
                    items=[types.SimpleNamespace(record=FakeRecord())],
                ),
            )

        def run_test(self):
            return FakeRunTest(self)

        def query_one(self, selector, *_args):
            if selector == "#live-table":
                return FakeLiveTable()
            if selector == "#detail-artifacts":
                return FakeArtifactsTable()
            if selector == "#detail-meta":
                return FakeStatic()
            raise AssertionError(f"unexpected selector: {selector}")

    import otto.runs.registry as registry_module
    import otto.tui.mission_control as mission_control_module

    monkeypatch.setattr(OTTO_AS_USER_NIGHTLY.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(registry_module, "allocate_run_id", lambda _repo: build_run_id)
    monkeypatch.setattr(registry_module, "read_live_records", lambda _repo: [FakeRecord()])
    monkeypatch.setattr(registry_module, "load_live_record", lambda _repo, _run_id: FakeRecord())
    monkeypatch.setattr(registry_module, "garbage_collect_live_records", lambda _repo, terminal_retention_s=0: [build_run_id])
    monkeypatch.setattr(mission_control_module, "MissionControlApp", FakeMissionControlApp)

    result = OTTO_AS_USER_NIGHTLY.run_n10(repo, "claude")

    assert result.returncode == 0
    assert result.details["summary-status"] == "cancelled"
    assert result.details["history-terminal-snapshot-count"] == 1
    assert result.details["history-terminal-outcome"] == "cancelled"
    assert result.details["build-terminated-via-cancel"] is True
