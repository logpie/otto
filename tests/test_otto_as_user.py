from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "otto_as_user.py"
SCRIPTS_DIR = SCRIPT_PATH.parent

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SPEC = importlib.util.spec_from_file_location("tests._otto_as_user_script", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
OTTO_AS_USER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = OTTO_AS_USER
SPEC.loader.exec_module(OTTO_AS_USER)
CAST_UTILS = __import__("cast_utils")


def make_run_result(
    tmp_path: Path,
    *,
    duration_s: float = 5.0,
    output: str = "",
    details: dict[str, object] | None = None,
) -> object:
    return OTTO_AS_USER.RunResult(
        scenario_id="A1",
        returncode=1,
        started_at="2026-04-21T00:00:00Z",
        finished_at="2026-04-21T00:00:01Z",
        duration_s=duration_s,
        recording_path=str(tmp_path / "recording.cast"),
        repo_path=str(tmp_path),
        debug_log=str(tmp_path / "debug.log"),
        output=output,
        details=details or {},
    )


@pytest.mark.parametrize(
    ("narrative_text", "debug_text", "output", "duration_s", "details"),
    [
        ("Not logged in", "", "", 5.0, {}),
        ("", "Please run /login", "", 5.0, {}),
        ("subscription RATE LIMIT hit under load", "", "", 5.0, {}),
        ("request failed with HTTP 429 throttle window exceeded", "", "", 5.0, {"summary": {"cost_usd": 0.0}}),
        (
            "Command failed with exit code 1\nCheck stderr output for details\n",
            "",
            "",
            1.2,
            {"summary": {"cost_usd": 0.0}},
        ),
    ],
)
def test_classify_failure_detects_infra_signatures(
    tmp_path: Path,
    narrative_text: str,
    debug_text: str,
    output: str,
    duration_s: float,
    details: dict[str, object],
) -> None:
    narrative = tmp_path / "narrative.log"
    debug = tmp_path / "debug.log"
    narrative.write_text(narrative_text, encoding="utf-8")
    debug.write_text(debug_text, encoding="utf-8")

    result = make_run_result(tmp_path, duration_s=duration_s, output=output, details=details)

    assert OTTO_AS_USER.classify_failure(narrative, debug, result) == "INFRA"


def test_classify_failure_leaves_real_failures_as_fail(tmp_path: Path) -> None:
    narrative = tmp_path / "narrative.log"
    debug = tmp_path / "debug.log"
    narrative.write_text(
        "Traceback (most recent call last):\nAssertionError: expected hello.py to exist\n",
        encoding="utf-8",
    )
    debug.write_text("pytest failed with rc=1\n", encoding="utf-8")

    result = make_run_result(
        tmp_path,
        duration_s=12.0,
        details={"summary": {"cost_usd": 1.25}},
    )

    assert OTTO_AS_USER.classify_failure(narrative, debug, result) == "FAIL"


def test_classify_failure_keeps_product_rate_limit_failures_as_fail(tmp_path: Path) -> None:
    narrative = tmp_path / "narrative.log"
    debug = tmp_path / "debug.log"
    narrative.write_text(
        "Traceback (most recent call last):\nAssertionError: expected recipe rate limit banner\n",
        encoding="utf-8",
    )
    debug.write_text("pytest failed with rc=1\n", encoding="utf-8")
    result = make_run_result(
        tmp_path,
        duration_s=12.0,
        details={"summary": {"cost_usd": 1.25}},
    )

    assert OTTO_AS_USER.classify_failure(narrative, debug, result) == "FAIL"


def test_classify_failure_keeps_generic_rate_limit_exceeded_as_fail(tmp_path: Path) -> None:
    narrative = tmp_path / "narrative.log"
    debug = tmp_path / "debug.log"
    narrative.write_text(
        "Traceback (most recent call last):\nAssertionError: expected product to show rate limit exceeded\n",
        encoding="utf-8",
    )
    debug.write_text("pytest failed with rc=1\n", encoding="utf-8")
    result = make_run_result(
        tmp_path,
        duration_s=12.0,
        details={"summary": {"cost_usd": 1.25}},
    )

    assert OTTO_AS_USER.classify_failure(narrative, debug, result) == "FAIL"


def test_classify_failure_keeps_product_http_429_failures_as_fail(tmp_path: Path) -> None:
    narrative = tmp_path / "narrative.log"
    debug = tmp_path / "debug.log"
    narrative.write_text(
        "Traceback (most recent call last):\nAssertionError: product API returned HTTP 429 for guest user\n",
        encoding="utf-8",
    )
    debug.write_text("pytest failed with rc=1\n", encoding="utf-8")
    result = make_run_result(
        tmp_path,
        duration_s=12.0,
        details={"summary": {"cost_usd": 1.25}},
    )

    assert OTTO_AS_USER.classify_failure(narrative, debug, result) == "FAIL"


def test_record_one_scenario_retries_in_fresh_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    artifact_root = tmp_path / "artifacts"
    monkeypatch.setattr(OTTO_AS_USER, "DEFAULT_ARTIFACT_ROOT", artifact_root)
    monkeypatch.setattr(OTTO_AS_USER, "INFRA_RETRY_DELAY_S", 0.0)
    monkeypatch.setattr(OTTO_AS_USER.time, "sleep", lambda _seconds: None)

    repos = [tmp_path / "repo-1", tmp_path / "repo-2"]
    created_repos: list[Path] = []

    def fake_mkdtemp(*, prefix: str, dir: str) -> str:
        del prefix, dir
        repo = repos.pop(0)
        repo.mkdir()
        created_repos.append(repo)
        return str(repo)

    setup_repos: list[Path] = []

    def setup(repo: Path, provider: str) -> None:
        assert provider == "claude"
        setup_repos.append(repo)
        (repo / "seed.txt").write_text("seed", encoding="utf-8")

    def run(_repo: Path, _provider: str) -> object:
        raise AssertionError("run_one_scenario_attempt is patched")

    def verify(_repo: Path, _run_result: object) -> object:
        raise AssertionError("run_one_scenario_attempt is patched")

    scenario = OTTO_AS_USER.Scenario(
        "ZZ",
        "Z",
        "retry freshness",
        True,
        0.0,
        1,
        False,
        setup,
        run,
        verify,
    )
    attempt_repos: list[Path] = []

    def fake_attempt(
        asciinema_bin: Path,
        scenario_arg: object,
        provider: str,
        repo_path: Path,
        artifact_dir: Path,
        *,
        attempt_index: int,
    ) -> tuple[object, object, str]:
        del asciinema_bin, scenario_arg, provider, artifact_dir
        attempt_repos.append(repo_path)
        debug_log = repo_path / f"debug-{attempt_index}.log"
        if attempt_index == 1:
            debug_log.write_text("Not logged in\n", encoding="utf-8")
            return (
                OTTO_AS_USER.RunResult(
                    scenario_id="ZZ",
                    returncode=1,
                    started_at="2026-04-21T00:00:00Z",
                    finished_at="2026-04-21T00:00:01Z",
                    duration_s=1.0,
                    recording_path=str(repo_path / "recording.cast"),
                    repo_path=str(repo_path),
                    debug_log=str(debug_log),
                    output="Not logged in",
                ),
                OTTO_AS_USER.VerifyResult(False, "infra"),
                "recording.cast",
            )
        return (
            OTTO_AS_USER.RunResult(
                scenario_id="ZZ",
                returncode=0,
                started_at="2026-04-21T00:00:02Z",
                finished_at="2026-04-21T00:00:03Z",
                duration_s=1.0,
                recording_path=str(repo_path / "recording-retry.cast"),
                repo_path=str(repo_path),
                debug_log=str(debug_log),
            ),
            OTTO_AS_USER.VerifyResult(True, "passed"),
            "recording-retry.cast",
        )

    monkeypatch.setattr(OTTO_AS_USER.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(OTTO_AS_USER, "run_one_scenario_attempt", fake_attempt)

    outcome = OTTO_AS_USER.record_one_scenario(Path("asciinema"), scenario, "run-1", "claude")

    assert setup_repos == created_repos
    assert attempt_repos == created_repos
    assert outcome.outcome == "PASS"
    assert outcome.attempt_count == 2
    assert outcome.retried_after_infra is True
    assert Path(outcome.run_result.repo_path) == created_repos[1]


def test_main_list_includes_u2(capsys) -> None:
    assert OTTO_AS_USER.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "U2" in out
    assert "Mission Control basic flow" in out


def test_main_real_recording_requires_cost_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OTTO_ALLOW_REAL_COST", raising=False)
    monkeypatch.setattr(
        OTTO_AS_USER,
        "ensure_real_otto_cli",
        lambda: (_ for _ in ()).throw(AssertionError("real CLI check reached")),
    )

    assert OTTO_AS_USER.main(["--scenario", "A1"]) == 2


def test_internal_run_requires_cost_opt_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OTTO_ALLOW_REAL_COST", raising=False)
    monkeypatch.setattr(
        OTTO_AS_USER,
        "ensure_real_otto_cli",
        lambda: (_ for _ in ()).throw(AssertionError("real CLI check reached")),
    )

    assert OTTO_AS_USER.main([
        "_internal-run",
        "A1",
        str(tmp_path / "repo"),
        str(tmp_path / "artifacts"),
        "claude",
    ]) == 2


def test_queue_build_constructs_passthrough_after_inner_separator(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    captured: dict[str, object] = {}
    ctx = OTTO_AS_USER.ExecutionContext(
        scenario=OTTO_AS_USER.SCENARIOS["A1"],
        artifact_dir=artifact_dir,
        repo=repo,
        provider="claude",
        debug_log=artifact_dir / "debug.log",
        recording_path=artifact_dir / "recording.cast",
    )
    ctx.isolated_venv = artifact_dir / ".scenario-venv"
    (ctx.isolated_venv / "bin").mkdir(parents=True)
    ctx.prepended_path_entries = [ctx.isolated_venv / "bin"]
    monkeypatch.setattr(OTTO_AS_USER, "EXECUTION_CONTEXT", ctx)
    monkeypatch.setattr(OTTO_AS_USER, "maybe_warn_packaging_intent", lambda _intent: None)

    def fake_run_checked(argv: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
        captured["argv"] = argv
        captured["cwd"] = cwd
        captured["env"] = env

    monkeypatch.setattr(OTTO_AS_USER, "run_checked", fake_run_checked)

    OTTO_AS_USER.queue_build(repo, "task1", "codex", "build thing", "--fast", "--rounds", "2")

    assert captured["argv"] == [
        str(OTTO_AS_USER.OTTO_BIN),
        "queue",
        "build",
        "build thing",
        "--as",
        "task1",
        "--",
        "--provider",
        "codex",
        "--fast",
        "--rounds",
        "2",
    ]
    assert captured["cwd"] == repo
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["PATH"].split(":")[0] == str(ctx.isolated_venv / "bin")


def test_ensure_real_otto_cli_accepts_real_help(monkeypatch) -> None:
    monkeypatch.setattr(OTTO_AS_USER, "otto_shadow_preview", lambda: "Otto -- build and certify software products\n")

    OTTO_AS_USER.ensure_real_otto_cli()


def test_ensure_real_otto_cli_rejects_shadowed_help(monkeypatch) -> None:
    monkeypatch.setattr(OTTO_AS_USER, "otto_shadow_preview", lambda: "Otto -- a tiny task tracker\n")

    with pytest.raises(SystemExit, match="shadowed in venv"):
        OTTO_AS_USER.ensure_real_otto_cli()


def test_verify_u2_accepts_cancelled_terminal_snapshot(tmp_path: Path) -> None:
    from otto import paths

    repo = tmp_path / "repo"
    repo.mkdir()
    history_path = paths.history_jsonl(repo)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "history_kind": "terminal_snapshot",
                "run_id": "build-run",
                "terminal_outcome": "cancelled",
                "dedupe_key": "terminal_snapshot:build-run",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    cast_path = tmp_path / "recording.cast"
    cast_path.write_text('{"version": 2}\n[0.0, "o", "frame"]\n', encoding="utf-8")
    run_result = OTTO_AS_USER.RunResult(
        scenario_id="U2",
        returncode=0,
        started_at="2026-04-23T00:00:00Z",
        finished_at="2026-04-23T00:00:01Z",
        duration_s=5.0,
        recording_path=str(cast_path),
        repo_path=str(repo),
        debug_log=str(tmp_path / "debug.log"),
        details={"ack_latency_ms": 150, "ack_deadline_ms": 4000},
    )

    verify = OTTO_AS_USER.verify_u2(repo, run_result)

    assert verify.passed is True


def test_verify_b1_accepts_durable_history_when_queue_state_is_empty(tmp_path: Path) -> None:
    from otto import paths

    repo = tmp_path / "repo"
    repo.mkdir()
    history_path = paths.history_jsonl(repo)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "schema_version": 2,
                    "history_kind": "terminal_snapshot",
                    "run_id": f"run-{task_id}",
                    "queue_task_id": task_id,
                    "status": "done",
                    "terminal_outcome": "success",
                    "dedupe_key": f"terminal_snapshot:run-{task_id}",
                }
            )
            for task_id in ("add", "mul")
        )
        + "\n",
        encoding="utf-8",
    )
    run_result = OTTO_AS_USER.RunResult(
        scenario_id="B1",
        returncode=0,
        started_at="2026-04-23T00:00:00Z",
        finished_at="2026-04-23T00:00:01Z",
        duration_s=5.0,
        recording_path=str(tmp_path / "recording.cast"),
        repo_path=str(repo),
        debug_log=str(tmp_path / "debug.log"),
        details={"state": {"tasks": {}}, "watcher_rc": None, "notice_text": ""},
    )

    verify = OTTO_AS_USER.verify_b1(repo, run_result)

    assert verify.passed is True


def test_verify_b1_maps_success_terminal_outcome_to_done(tmp_path: Path) -> None:
    from otto import paths

    repo = tmp_path / "repo"
    repo.mkdir()
    history_path = paths.history_jsonl(repo)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "schema_version": 2,
                    "history_kind": "terminal_snapshot",
                    "run_id": f"run-{task_id}",
                    "queue_task_id": task_id,
                    "terminal_outcome": "success",
                    "dedupe_key": f"terminal_snapshot:run-{task_id}",
                }
            )
            for task_id in ("add", "mul")
        )
        + "\n",
        encoding="utf-8",
    )
    run_result = OTTO_AS_USER.RunResult(
        scenario_id="B1",
        returncode=0,
        started_at="2026-04-23T00:00:00Z",
        finished_at="2026-04-23T00:00:01Z",
        duration_s=5.0,
        recording_path=str(tmp_path / "recording.cast"),
        repo_path=str(repo),
        debug_log=str(tmp_path / "debug.log"),
        details={"state": {"tasks": {}}, "watcher_rc": None, "notice_text": ""},
    )

    verify = OTTO_AS_USER.verify_b1(repo, run_result)

    assert verify.passed is True


def test_verify_b3_accepts_mission_control_zero_row_footer(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_result = OTTO_AS_USER.RunResult(
        scenario_id="B3",
        returncode=0,
        started_at="2026-04-23T00:00:00Z",
        finished_at="2026-04-23T00:00:01Z",
        duration_s=5.0,
        recording_path=str(tmp_path / "recording.cast"),
        repo_path=str(repo),
        debug_log=str(tmp_path / "debug.log"),
        details={"screen": "focus=live | rows=0 live, 0 history\nNo selection.\n", "watcher_rc": 0},
    )

    verify = OTTO_AS_USER.verify_b3(repo, run_result)

    assert verify.passed is True


def test_mouse_capture_helpers_ignore_focus_tracking() -> None:
    raw = "\x1b[?1004h\x1b[?1000h\x1b[?1006h\x1b[?1004l\x1b[?1000l\x1b[?1006l"

    assert CAST_UTILS.mouse_enable_codes(raw) == ["\x1b[?1000h", "\x1b[?1006h"]
    assert CAST_UTILS.mouse_disable_codes(raw) == ["\x1b[?1000l", "\x1b[?1006l"]


def test_verify_b2_accepts_cancelled_terminal_history(tmp_path: Path) -> None:
    from otto import paths

    repo = tmp_path / "repo"
    repo.mkdir()
    history_path = paths.history_jsonl(repo)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "history_kind": "terminal_snapshot",
                "run_id": "run-alpha",
                "queue_task_id": "alpha",
                "status": "cancelled",
                "terminal_outcome": "cancelled",
                "dedupe_key": "terminal_snapshot:run-alpha",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    run_result = OTTO_AS_USER.RunResult(
        scenario_id="B2",
        returncode=0,
        started_at="2026-04-23T00:00:00Z",
        finished_at="2026-04-23T00:00:01Z",
        duration_s=5.0,
        recording_path=str(tmp_path / "recording.cast"),
        repo_path=str(repo),
        debug_log=str(tmp_path / "debug.log"),
        details={"state": {"tasks": {}}, "history_alpha": None},
    )

    verify = OTTO_AS_USER.verify_b2(repo, run_result)

    assert verify.passed is True


def test_verify_b2_falls_back_to_queue_terminal_status_without_stringifying_none(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_result = OTTO_AS_USER.RunResult(
        scenario_id="B2",
        returncode=0,
        started_at="2026-04-23T00:00:00Z",
        finished_at="2026-04-23T00:00:01Z",
        duration_s=5.0,
        recording_path=str(tmp_path / "recording.cast"),
        repo_path=str(repo),
        debug_log=str(tmp_path / "debug.log"),
        details={
            "state": {
                "tasks": {
                    "alpha": {
                        "status": "terminating",
                        "terminal_status": "cancelled",
                    }
                }
            },
            "history_alpha": None,
        },
    )

    verify = OTTO_AS_USER.verify_b2(repo, run_result)

    assert verify.passed is True


def test_run_d1_passes_force_for_fingerprint_resume(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        OTTO_AS_USER,
        "EXECUTION_CONTEXT",
        OTTO_AS_USER.ExecutionContext(
            scenario=OTTO_AS_USER.SCENARIOS["D1"],
            artifact_dir=tmp_path,
            repo=repo,
            provider="claude",
            debug_log=tmp_path / "debug.log",
            recording_path=tmp_path / "recording.cast",
        ),
    )

    monkeypatch.setattr(OTTO_AS_USER, "interrupt_build_after_checkpoint", lambda *_args, **_kwargs: "partial\n")

    def fake_run_build(_repo: Path, _provider: str, *args: str, timeout_s: float = 0):
        del timeout_s
        calls.append(args)
        return OTTO_AS_USER.CommandResult(argv=["otto", "build", *args], rc=0, duration_s=0.1, output="ok\n")

    monkeypatch.setattr(OTTO_AS_USER, "run_build", fake_run_build)
    monkeypatch.setattr(OTTO_AS_USER, "load_summary", lambda _repo: {"status": "done"})

    result = OTTO_AS_USER.run_d1(repo, "claude")

    assert result.returncode == 0
    assert calls == [("--resume", "--force")]


def test_run_d5_forces_fingerprint_gate_before_cross_command_check(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(
        OTTO_AS_USER,
        "EXECUTION_CONTEXT",
        OTTO_AS_USER.ExecutionContext(
            scenario=OTTO_AS_USER.SCENARIOS["D5"],
            artifact_dir=tmp_path,
            repo=repo,
            provider="claude",
            debug_log=tmp_path / "debug.log",
            recording_path=tmp_path / "recording.cast",
        ),
    )

    monkeypatch.setattr(OTTO_AS_USER, "interrupt_build_after_checkpoint", lambda *_args, **_kwargs: "partial\n")

    def fake_run_improve(_repo: Path, _provider: str, subcommand: str, *args: str, timeout_s: float = 0):
        del timeout_s
        calls.append((subcommand, args))
        output = "Checkpoint command mismatch\n" if "--force-cross-command-resume" not in args else "Checkpoint is from build\n"
        return OTTO_AS_USER.CommandResult(argv=["otto", "improve", subcommand, *args], rc=0, duration_s=0.1, output=output)

    monkeypatch.setattr(OTTO_AS_USER, "run_improve", fake_run_improve)

    result = OTTO_AS_USER.run_d5(repo, "claude")

    assert result.returncode == 0
    assert calls == [
        ("bugs", ("--resume", "--force")),
        ("bugs", ("--resume", "--force", "--force-cross-command-resume")),
    ]


def test_verify_d5_rejects_failed_forced_cross_command_resume(tmp_path: Path) -> None:
    run_result = make_run_result(
        tmp_path,
        details={
            "rejected_output": "Checkpoint command mismatch",
            "forced_output": "Checkpoint is from build",
        },
    )

    verify = OTTO_AS_USER.verify_d5(tmp_path, run_result)

    assert verify.passed is False
    assert "forced cross-command resume" in verify.note


def test_verify_d5_requires_forced_cross_command_marker(tmp_path: Path) -> None:
    run_result = OTTO_AS_USER.RunResult(
        scenario_id="D5",
        returncode=0,
        started_at="2026-04-21T00:00:00Z",
        finished_at="2026-04-21T00:00:01Z",
        duration_s=1.0,
        recording_path=str(tmp_path / "recording.cast"),
        repo_path=str(tmp_path),
        debug_log=str(tmp_path / "debug.log"),
        details={
            "rejected_output": "Checkpoint command mismatch",
            "forced_output": "build continued",
        },
    )

    verify = OTTO_AS_USER.verify_d5(tmp_path, run_result)

    assert verify.passed is False
    assert "forced cross-command resume" in verify.note


def test_verify_e4_requires_memory_reinjection_marker(tmp_path: Path) -> None:
    memory_path = tmp_path / "otto_logs" / "cross-sessions" / "certifier-memory.jsonl"
    memory_path.parent.mkdir(parents=True)
    memory_path.write_text('{"run": 1}\n{"run": 2}\n', encoding="utf-8")
    run_result = make_run_result(
        tmp_path,
        details={"memory_path": str(memory_path), "injected_marker": False},
    )

    verify = OTTO_AS_USER.verify_e4(tmp_path, run_result)

    assert verify.passed is False
    assert "prior certification history" in verify.note


def test_should_warn_packaging_intent_flags_package_builds() -> None:
    assert OTTO_AS_USER.should_warn_packaging_intent("Build a Python package with pyproject.toml and a console script.")
    assert not OTTO_AS_USER.should_warn_packaging_intent("Build a Python CLI script hello.py.")
