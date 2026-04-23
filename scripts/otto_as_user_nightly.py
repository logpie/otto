#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import argparse
import json
import os
import signal
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

try:
    from scripts import otto_as_user as base
except ImportError:
    import otto_as_user as base


REPO_ROOT = SCRIPT_DIR.parent
FIXTURE_ROOT = REPO_ROOT / "scripts" / "fixtures_nightly"
DEFAULT_ARTIFACT_ROOT = REPO_ROOT / "bench-results" / "as-user-nightly"
DEFAULT_SCENARIO_DELAY_S = 5.0

N1_BUILD_INTENT = "Add task labels with filtering by label."
N1_IMPROVE_BUGS_FOCUS = "Fix the bug where tasks are visible across users."
N1_TARGET = "Reduce SQL query count for GET /labels to <= 3 on the seeded dataset."

N2_RESET_INTENT = (
    "Add password reset via email token. Touch the user model, auth routes, "
    "and account/login pages."
)
N2_REMEMBER_INTENT = (
    "Add a remember me option with 30-day session expiry. Touch the user model, "
    "login route, and account/login pages. Persist the expiry in the database "
    "and set the session cookie max-age to 2592000 when remember_me is true."
)

N4_BUILD_INTENT = "Add CSV bulk import for tasks."

N9_BUILD_INTENT = "Add a GET /tasks endpoint that returns the current task list as JSON."
N9_POST_INTENT = "Add POST /tasks endpoint."
N9_DELETE_INTENT = "Add DELETE /tasks/<id> endpoint."

N8_RENAME_INTENT = (
    "Rename app/services/billing.py to app/services/payments.py and update all imports."
)
N8_LOGIC_INTENT = (
    "Update weekend billing rules in app/services/billing.py: starter adds 300 cents "
    "per seat on weekends, and enterprise has no weekend surcharge."
)
N8_TESTS_INTENT = (
    "Add regression tests that import app.services.payments.calculate_charge and cover "
    "weekend starter pricing plus enterprise no-surcharge behavior."
)


@dataclass
class VerifyResult:
    ok: bool
    detail: str
    details: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.ok

    @property
    def note(self) -> str:
        return self.detail


@dataclass
class NightlyOutcome:
    scenario: base.Scenario
    outcome: str
    run_result: base.RunResult
    verify_result: VerifyResult
    artifact_dir: Path
    attempt_count: int = 1
    wall_duration_s: float = 0.0
    retried_after_infra: bool = False


@dataclass(frozen=True)
class ScenarioSpec:
    fixture_dir: Path
    intent_path: Path
    est_cost_range: str
    budget_s: int
    step_plan: list[str]


SCENARIO_SPECS: dict[str, ScenarioSpec] = {
    "N1": ScenarioSpec(
        fixture_dir=FIXTURE_ROOT / "n1_evolving_product_loop",
        intent_path=FIXTURE_ROOT / "n1_evolving_product_loop" / "intent.md",
        est_cost_range="$2.5-$3.5",
        budget_s=22 * 60,
        step_plan=[
            f"otto build --provider <provider> {N1_BUILD_INTENT!r}",
            f"otto improve bugs --provider <provider> {N1_IMPROVE_BUGS_FOCUS!r}",
            f"otto improve target --provider <provider> {N1_TARGET!r}",
            "pytest tests/visible -q --tb=short",
            "pytest tests/hidden -q --tb=short",
        ],
    ),
    "N2": ScenarioSpec(
        fixture_dir=FIXTURE_ROOT / "n2_semantic_auth_merge_conflict",
        intent_path=FIXTURE_ROOT / "n2_semantic_auth_merge_conflict" / "intent.md",
        est_cost_range="$2.2-$3.0",
        budget_s=18 * 60,
        step_plan=[
            f"otto queue build {N2_RESET_INTENT!r} --as reset",
            f"otto queue build {N2_REMEMBER_INTENT!r} --as remember",
            "otto queue run --concurrent 2 --no-dashboard --exit-when-empty",
            "otto merge --all --cleanup-on-success",
            "pytest tests/visible -q --tb=short",
            "pytest tests/hidden -q --tb=short",
        ],
    ),
    "N4": ScenarioSpec(
        fixture_dir=FIXTURE_ROOT / "n4_certifier_trap_hidden_invariants",
        intent_path=FIXTURE_ROOT / "n4_certifier_trap_hidden_invariants" / "intent.md",
        est_cost_range="$1.2-$1.8",
        budget_s=12 * 60,
        step_plan=[
            f"otto build --provider <provider> {N4_BUILD_INTENT!r}",
            "pytest tests/visible -q --tb=short",
            "pytest tests/hidden -q --tb=short",
        ],
    ),
    "N8": ScenarioSpec(
        fixture_dir=FIXTURE_ROOT / "n8_stale_merge_context",
        intent_path=FIXTURE_ROOT / "n8_stale_merge_context" / "intent.md",
        est_cost_range="$3.2-$4.5",
        budget_s=28 * 60,
        step_plan=[
            f"otto queue build {N8_RENAME_INTENT!r} --as rename-payments",
            f"otto queue build {N8_LOGIC_INTENT!r} --as weekend-logic",
            f"otto queue build {N8_TESTS_INTENT!r} --as payments-tests",
            "otto queue run --concurrent 1 --no-dashboard --exit-when-empty",
            "otto merge --all --cleanup-on-success",
            "pytest tests/visible -q --tb=short",
            "pytest tests/hidden -q --tb=short",
        ],
    ),
    "N9": ScenarioSpec(
        fixture_dir=FIXTURE_ROOT / "n9_mission_control_workflow",
        intent_path=FIXTURE_ROOT / "n9_mission_control_workflow" / "intent.md",
        est_cost_range="$2.5-$4.5",
        budget_s=25 * 60,
        step_plan=[
            "open Mission Control against ./otto_logs/",
            f"otto build --provider <provider> --allow-dirty {N9_BUILD_INTENT!r}",
            "<pilot waits for the standalone live row within 5s>",
            f"otto queue build {N9_POST_INTENT!r} --as add-post",
            f"otto queue build {N9_DELETE_INTENT!r} --as add-delete",
            "<pilot starts the queue watcher while the standalone build is still running>",
            "otto queue run --concurrent 2 --no-dashboard --exit-when-empty",
            "<pilot waits for both queue live rows within 30s so three live records overlap>",
            "<pilot drills into the standalone Detail row, verifies heartbeat progress within 4s, and presses o to switch logs>",
            "<pilot waits up to 8 minutes for the standalone run and presses c if it is still running>",
            "<pilot cancels one running queue task via c and waits for cancel ack + cancelled status while the other queue task finishes naturally>",
            "<pilot opens History, verifies terminal snapshots, drills into the cancelled row, and presses e>",
            "<pilot selects the succeeded queue row and presses m to run otto merge <task-id> (not --all)>",
            "pytest tests/visible -q --tb=short",
            "pytest tests/hidden -q --tb=short",
        ],
    ),
}


def _fixture_spec(scenario_id: str) -> ScenarioSpec:
    return SCENARIO_SPECS[scenario_id]


def _python_bin() -> str:
    if base.PYTHON_BIN.exists():
        return str(base.PYTHON_BIN)
    return sys.executable


def _read_intent(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _safe_summary(repo: Path) -> dict[str, Any] | None:
    try:
        return base.load_summary(repo)
    except Exception:
        return None


def _copy_fixture(src: Path, dest: Path) -> None:
    shutil.copytree(
        src,
        dest,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc"),
    )
    restore = dest / "restore.sh"
    mode = restore.stat().st_mode
    restore.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _init_fixture_repo(repo: Path) -> None:
    base.run_checked(["git", "init", "-q", "-b", "main"], cwd=repo)
    base.run_checked(["git", "config", "user.email", "otto-as-user@example.com"], cwd=repo)
    base.run_checked(["git", "config", "user.name", "Otto As User"], cwd=repo)
    base.commit_all(repo, "initial fixture")


def setup_from_fixture(repo: Path, fixture_name: str) -> None:
    spec = SCENARIO_SPECS[fixture_name]
    _copy_fixture(spec.fixture_dir, repo)
    _init_fixture_repo(repo)


@contextmanager
def execution_context(
    *,
    scenario: base.Scenario,
    repo: Path,
    artifact_dir: Path,
    provider: str,
    attempt_index: int,
) -> Any:
    old_ctx = base.EXECUTION_CONTEXT
    ctx = base.ExecutionContext(
        scenario=scenario,
        artifact_dir=artifact_dir,
        repo=repo,
        provider=provider,
        debug_log=artifact_dir / base.attempt_filename("debug.log", attempt_index),
        recording_path=artifact_dir / base.attempt_filename("recording.cast", attempt_index),
    )
    base.EXECUTION_CONTEXT = ctx
    try:
        yield ctx
    finally:
        base.EXECUTION_CONTEXT = old_ctx


class ScenarioBudget:
    def __init__(self, total_s: int) -> None:
        self.total_s = total_s
        self.started = time.monotonic()

    def remaining(self) -> float:
        return self.total_s - (time.monotonic() - self.started)

    def timeout_for(self, cap_s: int) -> float:
        remaining = self.remaining()
        if remaining <= 0:
            raise TimeoutError(f"scenario budget exhausted after {self.total_s}s")
        return max(1.0, min(float(cap_s), remaining))


def _run_pytest(repo: Path, target: str, artifact_dir: Path, attempt_index: int) -> subprocess.CompletedProcess[str]:
    argv = [_python_bin(), "-m", "pytest", target, "-q", "--tb=short"]
    result = subprocess.run(argv, cwd=repo, text=True, capture_output=True)
    log_name = target.replace("/", "-").replace(".", "-")
    (artifact_dir / f"{log_name}{base.attempt_suffix(attempt_index)}.log").write_text(
        result.stdout + "\n" + result.stderr,
        encoding="utf-8",
    )
    return result


def _step_record(label: str, command: base.CommandResult, repo: Path) -> dict[str, Any]:
    summary = _safe_summary(repo)
    step: dict[str, Any] = {
        "label": label,
        "argv": command.argv,
        "rc": command.rc,
        "duration_s": command.duration_s,
    }
    if summary is not None:
        step["summary"] = {
            "run_id": summary.get("run_id"),
            "verdict": summary.get("verdict"),
        }
    return step


def _run_checked_step(
    label: str,
    argv: list[str],
    *,
    repo: Path,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    base.run_checked(argv, cwd=repo, env=env)
    return _step_record(
        label,
        base.CommandResult(
            argv=argv,
            rc=0,
            duration_s=round(time.monotonic() - started, 1),
            output="",
        ),
        repo,
    )


def _failed_run_result(
    scenario_id: str,
    repo: Path,
    output: str,
    details: dict[str, Any],
) -> base.RunResult:
    ctx = base.current_ctx()
    return base.RunResult(
        scenario_id=scenario_id,
        returncode=1,
        started_at=base.now_iso(),
        finished_at=base.now_iso(),
        duration_s=0.0,
        recording_path=str(ctx.recording_path),
        repo_path=str(repo),
        debug_log=str(ctx.debug_log),
        output=output,
        details=details,
    )


def _base_run_result(
    scenario_id: str,
    repo: Path,
    started_at: str,
    output: str,
    duration_s: float,
    details: dict[str, Any],
    returncode: int,
) -> base.RunResult:
    ctx = base.current_ctx()
    return base.RunResult(
        scenario_id=scenario_id,
        returncode=returncode,
        started_at=started_at,
        finished_at=base.now_iso(),
        duration_s=duration_s,
        recording_path=str(ctx.recording_path),
        repo_path=str(repo),
        debug_log=str(ctx.debug_log),
        output=output,
        details=details,
    )


def _attempt_index_from_run_result(run_result: base.RunResult) -> int:
    name = Path(run_result.debug_log).name
    if name == "debug.log":
        return 1
    if name == "debug-retry.log":
        return 2
    if name.startswith("debug-retry") and name.endswith(".log"):
        suffix = name.removeprefix("debug-retry").removesuffix(".log")
        if suffix.isdigit():
            return int(suffix) + 1
    return 1


def _visible_and_hidden(repo: Path, run_result: base.RunResult) -> tuple[subprocess.CompletedProcess[str], subprocess.CompletedProcess[str]]:
    artifact_dir = Path(run_result.debug_log).parent
    attempt_index = _attempt_index_from_run_result(run_result)
    visible = _run_pytest(repo, "tests/visible", artifact_dir, attempt_index)
    hidden = _run_pytest(repo, "tests/hidden", artifact_dir, attempt_index)
    return visible, hidden


def _attempt_index_from_debug_log(debug_log: Path) -> int:
    name = debug_log.name
    if name == "debug.log":
        return 1
    if name == "debug-retry.log":
        return 2
    if name.startswith("debug-retry") and name.endswith(".log"):
        suffix = name.removeprefix("debug-retry").removesuffix(".log")
        if suffix.isdigit():
            return int(suffix) + 1
    return 1


def _background_log_path(label: str) -> Path:
    ctx = base.current_ctx()
    attempt_index = _attempt_index_from_debug_log(ctx.debug_log)
    return ctx.artifact_dir / base.attempt_filename(f"{label}.log", attempt_index)


def _read_log(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _n9_phase_log_path(repo: Path) -> Path:
    from otto import paths

    return paths.cross_sessions_dir(repo) / "mission-control-pilot-phases.jsonl"


def _write_n9_phase_snapshot(app: Any, repo: Path, *, phase: str, note: str | None = None) -> None:
    path = _n9_phase_log_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    detail = app.model.detail_view(app.state)
    payload = {
        "phase": phase,
        "note": note,
        "focus": app.state.focus,
        "selection_run_id": app.state.selection.run_id,
        "selected_run_ids": sorted(app.state.selected_run_ids),
        "live_rows": [
            {
                "run_id": item.record.run_id,
                "domain": item.record.domain,
                "run_type": item.record.run_type,
                "status": item.record.status,
                "queue_task_id": item.record.identity.get("queue_task_id"),
                "merge_id": item.record.identity.get("merge_id"),
            }
            for item in app.state.live_runs.items
        ],
        "history_rows": [
            {
                "run_id": item.row.run_id,
                "domain": item.row.domain,
                "run_type": item.row.run_type,
                "status": item.row.status,
                "terminal_outcome": item.row.terminal_outcome,
                "queue_task_id": item.row.queue_task_id,
                "merge_id": item.row.merge_id,
            }
            for item in app.state.history_page.items
        ],
        "detail": {
            "run_id": detail.run_id if detail is not None else None,
            "status": detail.record.status if detail is not None else None,
            "selected_log_path": detail.selected_log_path if detail is not None else None,
            "log_paths": list(detail.log_paths) if detail is not None else [],
            "artifact_paths": [artifact.path for artifact in detail.artifacts] if detail is not None else [],
        },
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def _read_terminal_snapshots(repo: Path, run_id: str | None = None) -> list[dict[str, Any]]:
    from otto import paths

    rows = [
        row
        for row in base.read_jsonl(paths.history_jsonl(repo))
        if str(row.get("history_kind") or "terminal_snapshot") == "terminal_snapshot"
    ]
    if run_id is None:
        return rows
    return [row for row in rows if str(row.get("run_id") or "") == run_id]


def _artifact_paths_resolve(snapshot: dict[str, Any]) -> bool:
    candidate_keys = ("intent_path", "manifest_path", "summary_path", "primary_log_path")
    for key in candidate_keys:
        value = str(snapshot.get(key) or "").strip()
        if value and not Path(value).exists():
            return False
    artifacts = snapshot.get("artifacts")
    if isinstance(artifacts, dict):
        for value in artifacts.values():
            if isinstance(value, str) and value.strip() and not Path(value).exists():
                return False
            if isinstance(value, list):
                for path in value:
                    if isinstance(path, str) and path.strip() and not Path(path).exists():
                        return False
    return True


def _n9_overlap_delay_s() -> float:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return 0.0
    raw = os.environ.get("OTTO_N9_QUEUE_OVERLAP_DELAY_S", "60").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 60.0


@contextmanager
def _capture_mission_control_action_spawns() -> Any:
    import otto.tui.mission_control_actions as mission_control_actions

    real_popen = mission_control_actions.subprocess.Popen
    spawns: list[dict[str, Any]] = []

    def _wrapped_popen(argv: Any, *args: Any, **kwargs: Any) -> Any:
        proc = real_popen(argv, *args, **kwargs)
        spawns.append(
            {
                "argv": [str(part) for part in argv],
                "proc": proc,
                "pid": getattr(proc, "pid", None),
                "started_monotonic": time.monotonic(),
            }
        )
        return proc

    mission_control_actions.subprocess.Popen = _wrapped_popen
    try:
        yield spawns
    finally:
        mission_control_actions.subprocess.Popen = real_popen


def _terminate_process_group(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except OSError:
            proc.kill()
        proc.wait(timeout=5)


def _wait_then_terminate_process_group(
    proc: subprocess.Popen[str] | None,
    *,
    wait_s: float,
) -> bool:
    if proc is None or proc.poll() is not None:
        return False
    try:
        proc.wait(timeout=wait_s)
        return False
    except subprocess.TimeoutExpired:
        _terminate_process_group(proc)
        return True


def _classification_override(run_result: base.RunResult) -> str | None:
    override = run_result.details.get("classification_override")
    if override in {"INFRA", "FAIL"}:
        return override
    return None


def _build_failure(result: base.RunResult, message: str) -> VerifyResult:
    return VerifyResult(False, f"{message} (rc={result.returncode})")


def setup_n1(repo: Path, provider: str) -> None:
    del provider
    setup_from_fixture(repo, "N1")


def run_n1(repo: Path, provider: str) -> base.RunResult:
    started = base.now_iso()
    budget = ScenarioBudget(_fixture_spec("N1").budget_s)
    steps: list[dict[str, Any]] = []
    outputs: list[str] = []

    build = base.run_build(repo, provider, N1_BUILD_INTENT, timeout_s=budget.timeout_for(14 * 60))
    steps.append(_step_record("build", build, repo))
    outputs.append(build.output)
    if build.rc != 0:
        return _base_run_result("N1", repo, started, "\n".join(outputs), build.duration_s, {"steps": steps}, build.rc)

    bugs = base.run_improve(repo, provider, "bugs", N1_IMPROVE_BUGS_FOCUS, timeout_s=budget.timeout_for(10 * 60))
    steps.append(_step_record("improve-bugs", bugs, repo))
    outputs.append(bugs.output)
    if bugs.rc != 0:
        return _base_run_result("N1", repo, started, "\n".join(outputs), build.duration_s + bugs.duration_s, {"steps": steps}, bugs.rc)

    target = base.run_improve(repo, provider, "target", N1_TARGET, timeout_s=budget.timeout_for(12 * 60))
    steps.append(_step_record("improve-target", target, repo))
    outputs.append(target.output)
    return _base_run_result(
        "N1",
        repo,
        started,
        "\n".join(outputs),
        build.duration_s + bugs.duration_s + target.duration_s,
        {"steps": steps, "completed_steps": sum(1 for step in steps if step["rc"] == 0)},
        target.rc,
    )


def verify_n1(repo: Path, run_result: base.RunResult) -> VerifyResult:
    steps = run_result.details.get("steps", [])
    failed_before_target = any(
        step.get("label") in ("build", "improve-bugs") and step.get("rc") != 0
        for step in steps
    )
    if failed_before_target:
        return _build_failure(run_result, "N1 nightly chain failed before verification")
    if len(steps) != 3:
        return VerifyResult(False, "N1 expected three Otto sessions in the chain")
    visible, hidden = _visible_and_hidden(repo, run_result)
    if visible.returncode != 0:
        return VerifyResult(False, "N1 visible tests failed after nightly chain")
    if hidden.returncode != 0:
        return VerifyResult(False, "N1 hidden tests failed after nightly chain")
    return VerifyResult(True, "N1 passed visible and hidden tests after build/improve/improve target")


def setup_n2(repo: Path, provider: str) -> None:
    del provider
    setup_from_fixture(repo, "N2")


def run_n2(repo: Path, provider: str) -> base.RunResult:
    started = base.now_iso()
    budget = ScenarioBudget(_fixture_spec("N2").budget_s)
    outputs: list[str] = []
    steps: list[dict[str, Any]] = []

    base.queue_build(repo, "reset", provider, N2_RESET_INTENT)
    steps.append({"label": "queue-reset", "rc": 0, "task_id": "reset"})
    base.queue_build(repo, "remember", provider, N2_REMEMBER_INTENT)
    steps.append({"label": "queue-remember", "rc": 0, "task_id": "remember"})

    watcher = base.run_queue(
        repo,
        "run",
        "--concurrent",
        "2",
        "--no-dashboard",
        "--exit-when-empty",
        timeout_s=budget.timeout_for(15 * 60),
    )
    steps.append(_step_record("queue-run", watcher, repo))
    outputs.append(watcher.output)
    if watcher.rc != 0:
        return _base_run_result("N2", repo, started, "\n".join(outputs), watcher.duration_s, {"steps": steps}, watcher.rc)

    merge = base.run_merge(
        repo,
        "--all",
        "--cleanup-on-success",
        timeout_s=budget.timeout_for(10 * 60),
    )
    steps.append(_step_record("merge", merge, repo))
    outputs.append(merge.output)
    return _base_run_result(
        "N2",
        repo,
        started,
        "\n".join(outputs),
        watcher.duration_s + merge.duration_s,
        {"steps": steps, "queue_tasks": ["reset", "remember"]},
        merge.rc,
    )


def verify_n2(repo: Path, run_result: base.RunResult) -> VerifyResult:
    if run_result.returncode != 0:
        return _build_failure(run_result, "N2 queue/merge flow failed before verification")
    visible, hidden = _visible_and_hidden(repo, run_result)
    if visible.returncode != 0:
        return VerifyResult(False, "N2 visible login tests failed after merge")
    if hidden.returncode != 0:
        return VerifyResult(False, "N2 hidden auth-merge tests failed after merge")
    return VerifyResult(True, "N2 merged both auth features and kept login working")


def setup_n4(repo: Path, provider: str) -> None:
    del provider
    setup_from_fixture(repo, "N4")


def run_n4(repo: Path, provider: str) -> base.RunResult:
    started = base.now_iso()
    budget = ScenarioBudget(_fixture_spec("N4").budget_s)
    build = base.run_build(repo, provider, N4_BUILD_INTENT, timeout_s=budget.timeout_for(10 * 60))
    details = {"steps": [_step_record("build", build, repo)]}
    return _base_run_result(
        "N4",
        repo,
        started,
        build.output,
        build.duration_s,
        details,
        build.rc,
    )


def verify_n4(repo: Path, run_result: base.RunResult) -> VerifyResult:
    if run_result.returncode != 0:
        return _build_failure(run_result, "N4 build failed before invariant checks")
    visible, hidden = _visible_and_hidden(repo, run_result)
    if visible.returncode != 0:
        return VerifyResult(False, "N4 visible tests failed after build")
    if hidden.returncode != 0:
        return VerifyResult(False, "N4 hidden invariants failed after Otto reported success")
    return VerifyResult(True, "N4 passed happy-path and hidden invariant tests")


def setup_n8(repo: Path, provider: str) -> None:
    del provider
    setup_from_fixture(repo, "N8")


def setup_n9(repo: Path, provider: str) -> None:
    del provider
    setup_from_fixture(repo, "N9")

def run_n8(repo: Path, provider: str) -> base.RunResult:
    started = base.now_iso()
    budget = ScenarioBudget(_fixture_spec("N8").budget_s)
    outputs: list[str] = []
    steps: list[dict[str, Any]] = []

    base.queue_build(repo, "rename-payments", provider, N8_RENAME_INTENT)
    steps.append({"label": "queue-rename-payments", "rc": 0, "task_id": "rename-payments"})
    base.queue_build(repo, "weekend-logic", provider, N8_LOGIC_INTENT)
    steps.append({"label": "queue-weekend-logic", "rc": 0, "task_id": "weekend-logic"})
    base.queue_build(repo, "payments-tests", provider, N8_TESTS_INTENT)
    steps.append({"label": "queue-payments-tests", "rc": 0, "task_id": "payments-tests"})

    watcher = base.run_queue(
        repo,
        "run",
        "--concurrent",
        "1",
        "--no-dashboard",
        "--exit-when-empty",
        timeout_s=budget.timeout_for(22 * 60),
    )
    steps.append(_step_record("queue-run", watcher, repo))
    outputs.append(watcher.output)
    if watcher.rc != 0:
        return _base_run_result("N8", repo, started, "\n".join(outputs), watcher.duration_s, {"steps": steps}, watcher.rc)

    merge = base.run_merge(
        repo,
        "--all",
        "--cleanup-on-success",
        timeout_s=budget.timeout_for(12 * 60),
    )
    steps.append(_step_record("merge", merge, repo))
    outputs.append(merge.output)
    return _base_run_result(
        "N8",
        repo,
        started,
        "\n".join(outputs),
        watcher.duration_s + merge.duration_s,
        {"steps": steps, "queue_tasks": ["rename-payments", "weekend-logic", "payments-tests"]},
        merge.rc,
    )


def verify_n8(repo: Path, run_result: base.RunResult) -> VerifyResult:
    if run_result.returncode != 0:
        return _build_failure(run_result, "N8 queue/merge flow failed before verification")
    visible, hidden = _visible_and_hidden(repo, run_result)
    if visible.returncode != 0:
        return VerifyResult(False, "N8 visible tests failed after merge")
    if hidden.returncode != 0:
        return VerifyResult(False, "N8 hidden stale-context tests failed after merge")
    return VerifyResult(True, "N8 merged rename, stale branch logic, and dependent tests into final main")


async def _drive_n9_mission_control(
    *,
    repo: Path,
    build_run_id: str,
    details: dict[str, Any],
    runtime: dict[str, Any],
    start_build_flow: Any,
    start_queue_flow: Any,
    budget: ScenarioBudget,
    action_spawns: list[dict[str, Any]],
) -> None:
    from otto import paths
    from otto.runs.registry import load_command_ack_ids, read_live_records
    from otto.runs.schema import is_terminal_status
    from otto.tui.mission_control import MissionControlApp

    app = MissionControlApp(repo)
    history_path = paths.history_jsonl(repo)

    async def _wait_for(predicate: Any, *, timeout_s: float, message: str, pause_s: float = 0.1) -> Any:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await pilot.pause(pause_s)
            value = predicate()
            if value:
                return value
        raise AssertionError(message)

    def _cancelled_terminal_snapshot_for_queue_task(task_id: str, *, run_id: str | None = None) -> dict[str, Any] | None:
        for row in reversed(_read_terminal_snapshots(repo)):
            if str(row.get("queue_task_id") or "") != task_id:
                continue
            if str(row.get("terminal_outcome") or "") != "cancelled":
                continue
            if run_id is not None and str(row.get("run_id") or "") != run_id:
                continue
            return row
        return None

    async def _focus_live_run(run_id: str) -> None:
        await pilot.press("1")
        await pilot.pause(0.1)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            live_ids = [item.record.run_id for item in app.state.live_runs.items]
            if run_id not in live_ids:
                await pilot.pause(0.1)
                continue
            selected = app.state.selection.run_id
            if selected == run_id:
                return
            current_index = live_ids.index(selected) if selected in live_ids else 0
            target_index = live_ids.index(run_id)
            key = "down" if target_index >= current_index else "up"
            for _ in range(abs(target_index - current_index)):
                await pilot.press(key)
                await pilot.pause(0.05)
            if app.state.selection.run_id == run_id:
                return
        raise AssertionError(f"N9 could not focus live run {run_id}")

    async def _focus_history_run(run_id: str) -> None:
        await pilot.press("2")
        await pilot.pause(0.1)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            history_ids = [item.row.run_id for item in app.state.history_page.items]
            if run_id not in history_ids:
                await pilot.pause(0.1)
                continue
            selected = app.state.selection.run_id
            if selected == run_id:
                return
            current_index = history_ids.index(selected) if selected in history_ids else 0
            target_index = history_ids.index(run_id)
            key = "down" if target_index >= current_index else "up"
            for _ in range(abs(target_index - current_index)):
                await pilot.press(key)
                await pilot.pause(0.05)
            if app.state.selection.run_id == run_id:
                return
        raise AssertionError(f"N9 could not focus history run {run_id}")

    def _detail() -> Any:
        return app.model.detail_view(app.state)

    def _queue_items() -> dict[str, Any]:
        return {
            str(item.record.identity.get("queue_task_id")): item
            for item in app.state.live_runs.items
            if item.record.domain == "queue" and item.record.identity.get("queue_task_id")
        }

    def _history_rows() -> list[Any]:
        return list(app.state.history_page.items)

    def _merge_history_rows() -> list[Any]:
        return [item for item in _history_rows() if item.row.domain == "merge"]

    def _merge_live_records() -> list[Any]:
        return [record for record in read_live_records(repo) if record.domain == "merge"]

    async def _wait_until(predicate: Any, *, timeout_s: float, pause_s: float = 0.1) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await pilot.pause(pause_s)
            if predicate():
                return True
        return False

    async with app.run_test() as pilot:
        start_build_flow()
        build_proc = runtime["build_proc"]
        build_started = runtime["build_started"]
        if build_proc is None or build_started is None:
            raise AssertionError("N9 failed to start the standalone build from Mission Control")

        build_item = await _wait_for(
            lambda: next((item for item in app.state.live_runs.items if item.record.run_id == build_run_id), None),
            timeout_s=5.0,
            message="N9 Mission Control did not show the standalone build row within 5s",
        )
        details["build-live-row-latency-ms"] = int((time.monotonic() - build_started) * 1000)
        if build_item.record.status != "running":
            build_item = await _wait_for(
                lambda: next(
                    (
                        item
                        for item in app.state.live_runs.items
                        if item.record.run_id == build_run_id and item.record.status == "running"
                    ),
                    None,
                ),
                timeout_s=5.0,
                message="N9 Mission Control never rendered the standalone build as running",
            )

        start_queue_flow()
        watcher_started = runtime["watcher_started"]
        if watcher_started is None:
            raise AssertionError("N9 failed to start the queue watcher")
        queue_items = await _wait_for(
            lambda: _queue_items() if len(_queue_items()) >= 2 else None,
            timeout_s=30.0,
            message="N9 expected two live queue rows within 30s of starting the watcher",
        )
        for task_id, item in queue_items.items():
            details["queue-run-ids"][task_id] = item.record.run_id
            details["queue-live-latency-ms"][task_id] = int((time.monotonic() - watcher_started) * 1000)
        queue_run_ids = {item.record.run_id for item in queue_items.values()}
        concurrent_live_rows = [
            item
            for item in app.state.live_runs.items
            if item.record.run_id in queue_run_ids | {build_run_id}
        ]
        if len(concurrent_live_rows) < 3:
            raise AssertionError("N9 did not show the standalone build and both queue rows concurrently")
        if any(is_terminal_status(item.record.status) for item in concurrent_live_rows):
            raise AssertionError("N9 overlapping live rows became terminal before Mission Control inspected them")
        details["overlapping-live-run-ids"] = [item.record.run_id for item in concurrent_live_rows]

        await _focus_live_run(build_run_id)
        await pilot.press("enter")
        await _wait_for(
            lambda: app.state.focus == "detail" and (_detail() is not None and _detail().run_id == build_run_id),
            timeout_s=5.0,
            message="N9 standalone build detail view did not open",
        )
        build_detail = await _wait_for(
            lambda: _detail() if _detail() is not None and not is_terminal_status(_detail().record.status) else None,
            timeout_s=10.0,
            message="N9 standalone build detail never reached an active state",
        )
        initial_build_hb = int(build_detail.record.timing.get("heartbeat_seq") or 0)
        build_heartbeat_progress = await _wait_for(
            lambda: (
                _detail()
                if _detail() is not None and int(_detail().record.timing.get("heartbeat_seq") or 0) > initial_build_hb
                else None
            ),
            timeout_s=4.0,
            message="N9 standalone heartbeat did not advance while Detail was open",
        )
        details["standalone-heartbeat-advanced"] = True
        details["standalone-heartbeat-from"] = initial_build_hb
        details["standalone-heartbeat-to"] = int(build_heartbeat_progress.record.timing.get("heartbeat_seq") or 0)
        if len(build_detail.log_paths) < 2:
            raise AssertionError("N9 standalone detail did not expose a second log path for `o`")
        initial_build_log_path = build_detail.selected_log_path
        await pilot.press("o")
        await _wait_for(
            lambda: _detail() is not None and _detail().selected_log_path != initial_build_log_path,
            timeout_s=5.0,
            message="N9 `o` did not switch the standalone detail to a second log",
        )
        details["standalone-log-cycled"] = True
        details["standalone-log-before"] = initial_build_log_path
        details["standalone-log-after"] = _detail().selected_log_path
        _write_n9_phase_snapshot(app, repo, phase="build-running")

        build_finished_naturally = await _wait_until(
            lambda: build_proc.poll() is not None,
            timeout_s=8 * 60.0,
            pause_s=0.2,
        )
        if build_finished_naturally:
            build_finished_at = time.monotonic()
            build_rc = build_proc.poll()
            if build_rc != 0:
                raise AssertionError(f"N9 standalone build exited with rc={build_rc}")
            await _wait_for(
                lambda: next(
                    (
                        item
                        for item in app.state.live_runs.items
                        if item.record.run_id == build_run_id and item.record.status == "done"
                    ),
                    None,
                ),
                timeout_s=max(app.state.live_runs.refresh_interval_s + 1.0, 2.0),
                message="N9 Mission Control did not reflect build status=done within one refresh cycle",
            )
            details["build-finished-naturally"] = True
            details["build-live-done-latency-ms"] = int((time.monotonic() - build_finished_at) * 1000)
            _write_n9_phase_snapshot(app, repo, phase="build-done")
        else:
            request_path = paths.session_command_requests(Path(_detail().record.cwd), build_run_id)
            ack_path = paths.session_command_acks(Path(_detail().record.cwd), build_run_id)
            pre_ack_ids = set(load_command_ack_ids(ack_path))
            cancel_pressed_at = time.monotonic()
            await pilot.press("c")
            await _wait_for(
                lambda: set(load_command_ack_ids(ack_path)) - pre_ack_ids,
                timeout_s=15.0,
                message="N9 standalone cancel ack did not arrive",
            )
            details["standalone-cancel-request-path"] = str(request_path)
            details["standalone-cancel-ack-path"] = str(ack_path)
            details["standalone-cancel-ack-latency-ms"] = int((time.monotonic() - cancel_pressed_at) * 1000)
            cancelled_detail = await _wait_for(
                lambda: (
                    _detail()
                    if _detail() is not None
                    and _detail().record.run_id == build_run_id
                    and _detail().record.status == "cancelled"
                    else None
                ),
                timeout_s=max(app.state.live_runs.refresh_interval_s + 1.0, 2.0),
                message="N9 Mission Control did not refresh the standalone detail to cancelled",
            )
            details["standalone-cancelled-via-pilot"] = True
            details["standalone-cancelled-latency-ms"] = int((time.monotonic() - cancel_pressed_at) * 1000)
            details["standalone-terminal-status"] = cancelled_detail.record.status
            _write_n9_phase_snapshot(app, repo, phase="build-cancelled")
            await _wait_for(
                lambda: build_proc.poll() is not None,
                timeout_s=max(30.0, budget.timeout_for(90)),
                message="N9 standalone process did not exit after the Mission Control cancel",
            )

        await pilot.press("escape")
        await pilot.pause(0.2)

        active_queue_rows = [
            item
            for item in app.state.live_runs.items
            if item.record.domain == "queue" and not is_terminal_status(item.record.status)
        ]
        if not active_queue_rows:
            raise AssertionError("N9 queue rows settled before the standalone build finished")
        cancel_item = next(
            (item for item in active_queue_rows if str(item.record.identity.get("queue_task_id")) == "add-delete"),
            active_queue_rows[0],
        )
        cancel_task_id = str(cancel_item.record.identity.get("queue_task_id"))
        details["cancelled-queue-task-id"] = cancel_task_id
        details["cancelled-queue-run-id"] = cancel_item.record.run_id
        await _focus_live_run(cancel_item.record.run_id)
        await pilot.press("enter")
        await _wait_for(
            lambda: app.state.focus == "detail" and (_detail() is not None and _detail().run_id == cancel_item.record.run_id),
            timeout_s=5.0,
            message="N9 could not reopen queue detail for cancellation",
        )
        heartbeat_interval_s = max(float(_detail().record.timing.get("heartbeat_interval_s") or 5.0), 0.1)
        cancel_pressed_at = time.monotonic()
        await pilot.press("c")
        cancelled_snapshot = await _wait_for(
            lambda: _cancelled_terminal_snapshot_for_queue_task(
                cancel_task_id,
                run_id=cancel_item.record.run_id,
            ),
            timeout_s=max(8.0, 4.0 * heartbeat_interval_s),
            message="N9 queue cancel was not recorded in history",
        )
        details["queue-cancel-history-path"] = str(history_path)
        details["queue-cancel-history-latency-ms"] = int((time.monotonic() - cancel_pressed_at) * 1000)
        details["queue-cancel-history-run-id"] = str(cancelled_snapshot.get("run_id") or "")
        await _wait_for(
            lambda: (
                _detail() is not None
                and _detail().record.run_id == cancel_item.record.run_id
                and _detail().record.status == "cancelled"
            ),
            timeout_s=max(app.state.live_runs.refresh_interval_s + 1.0, 2.0),
            message="N9 Mission Control detail did not refresh to cancelled within one cycle",
        )
        details["queue-cancelled-latency-ms"] = int((time.monotonic() - cancel_pressed_at) * 1000)
        details["queue-refresh-window-ms"] = int(max(app.state.live_runs.refresh_interval_s, 0.5) * 1000)
        await pilot.press("escape")
        await pilot.pause(0.2)

        watcher_proc = runtime["watcher_proc"]
        await _wait_for(
            lambda: watcher_proc.poll() is not None,
            timeout_s=budget.timeout_for(18 * 60),
            message="N9 queue watcher did not settle after the cancel",
        )
        watcher_rc = watcher_proc.poll()
        if watcher_rc != 0:
            raise AssertionError(f"N9 queue watcher exited with rc={watcher_rc}")
        runtime["watcher_step"]["rc"] = watcher_rc
        runtime["watcher_step"]["duration_s"] = round(time.monotonic() - watcher_started, 1)
        details["watcher-returncode"] = watcher_rc

        await _focus_history_run(build_run_id)
        history_rows = await _wait_for(
            lambda: _history_rows() if len(_history_rows()) >= 3 else None,
            timeout_s=20.0,
            message="N9 History did not show the three terminal rows after queue settlement",
        )
        details["history-pre-merge-run-ids"] = [item.row.run_id for item in history_rows[:3]]
        terminal_outcomes = {item.row.run_id: item.row.terminal_outcome for item in history_rows}
        details["history-terminal-outcomes-pre-merge"] = terminal_outcomes
        if build_run_id not in terminal_outcomes or terminal_outcomes[build_run_id] not in {"success", "cancelled"}:
            raise AssertionError("N9 History did not record the standalone build as a terminal row")
        cancelled_history_item = next(
            (item for item in history_rows if item.row.run_id == details["cancelled-queue-run-id"]),
            None,
        )
        if cancelled_history_item is None or cancelled_history_item.row.terminal_outcome != "cancelled":
            raise AssertionError("N9 History did not record the cancelled queue row as cancelled")
        success_history_item = next(
            (
                item
                for item in history_rows
                if item.row.domain == "queue" and item.row.run_id != details["cancelled-queue-run-id"] and item.row.terminal_outcome == "success"
            ),
            None,
        )
        if success_history_item is None:
            raise AssertionError("N9 History did not record a succeeded queue row")
        details["succeeded-queue-run-id"] = success_history_item.row.run_id
        details["succeeded-queue-task-id"] = success_history_item.row.queue_task_id
        _write_n9_phase_snapshot(app, repo, phase="history-pre-merge")

        await _focus_history_run(details["cancelled-queue-run-id"])
        await pilot.press("enter")
        await _wait_for(
            lambda: app.state.focus == "detail" and (_detail() is not None and _detail().run_id == details["cancelled-queue-run-id"]),
            timeout_s=5.0,
            message="N9 cancelled history detail did not open",
        )
        await pilot.press("e")
        editor_spawn = await _wait_for(
            lambda: next(
                (
                    spawn
                    for spawn in action_spawns
                    if spawn["argv"] and Path(spawn["argv"][0]).name == "true"
                ),
                None,
            ),
            timeout_s=5.0,
            message="N9 did not attempt to spawn $EDITOR from the cancelled history row",
        )
        details["editor-spawn-attempted"] = True
        details["editor-spawn-argv"] = editor_spawn["argv"]
        _write_n9_phase_snapshot(app, repo, phase="history-cancelled-detail")

        await _focus_live_run(details["succeeded-queue-run-id"])
        await pilot.press("space")
        await _wait_for(
            lambda: details["succeeded-queue-run-id"] in app.state.selected_run_ids,
            timeout_s=5.0,
            message="N9 could not multi-select the succeeded queue row before merge",
        )
        await _wait_for(
            lambda: (
                app.state.focus == "live"
                and app.state.selection.run_id == details["succeeded-queue-run-id"]
                and (_detail() is not None and _detail().run_id == details["succeeded-queue-run-id"])
            ),
            timeout_s=5.0,
            message="N9 lost the succeeded queue row before issuing merge",
        )
        preexisting_merge_history_ids = {item.row.run_id for item in _merge_history_rows()}
        preexisting_merge_live_ids = {record.run_id for record in _merge_live_records()}
        merge_requested_at = time.monotonic()
        await pilot.press("m")
        merge_signal = await _wait_for(
            lambda: (
                next(
                    (
                        {
                            "kind": "spawn",
                            "argv": spawn["argv"],
                            "proc": spawn["proc"],
                            "run_id": None,
                        }
                        for spawn in action_spawns
                        if len(spawn["argv"]) >= 2 and spawn["argv"][1] == "merge"
                    ),
                    None,
                )
                or next(
                    (
                        {
                            "kind": "live-record",
                            "argv": None,
                            "proc": None,
                            "run_id": record.run_id,
                        }
                        for record in _merge_live_records()
                        if record.run_id not in preexisting_merge_live_ids
                    ),
                    None,
                )
                or next(
                    (
                        {
                            "kind": "history",
                            "argv": None,
                            "proc": None,
                            "run_id": item.row.run_id,
                        }
                        for item in _merge_history_rows()
                        if item.row.run_id not in preexisting_merge_history_ids
                    ),
                    None,
                )
            ),
            timeout_s=10.0,
            message="N9 did not trigger a merge from Mission Control",
        )
        merge_spawn_argv = merge_signal.get("argv") or []
        if "--all" in merge_spawn_argv:
            raise AssertionError("N9 merge action used `--all` instead of selected queue rows")
        if merge_spawn_argv:
            details["merge-spawn-argv"] = merge_spawn_argv
        details["merge-detected-via"] = merge_signal["kind"]
        if merge_signal["kind"] in {"live-record", "history"}:
            details["merge-live-record-seen"] = True
        merge_run_id = merge_signal.get("run_id")
        if merge_run_id:
            details["merge-run-id"] = merge_run_id
        merge_history_item = await _wait_for(
            lambda: next(
                (
                    item
                    for item in _merge_history_rows()
                    if item.row.run_id not in preexisting_merge_history_ids
                    and (merge_run_id is None or item.row.run_id == merge_run_id)
                    and item.row.terminal_outcome
                ),
                None,
            ),
            timeout_s=budget.timeout_for(12 * 60),
            message="N9 merge did not reach terminal history within the scenario budget",
        )
        details["merge-live-record-seen"] = True
        details["merge-history-run-id"] = merge_history_item.row.run_id
        details["merge-history-terminal-outcome"] = merge_history_item.row.terminal_outcome
        merge_step = {
            "label": "merge-selected",
            "argv": merge_spawn_argv or ["otto", "merge", str(details["succeeded-queue-task-id"])],
            "rc": 0 if merge_history_item.row.terminal_outcome == "success" else 1,
            "duration_s": round(time.monotonic() - merge_requested_at, 1),
        }
        runtime["merge_step"] = merge_step
        if merge_history_item.row.terminal_outcome != "success":
            raise AssertionError(
                f"N9 merge completed with terminal_outcome={merge_history_item.row.terminal_outcome!r}"
            )
        _write_n9_phase_snapshot(app, repo, phase="merge-complete")


def run_n9(repo: Path, provider: str) -> base.RunResult:
    from otto.runs.registry import allocate_run_id, garbage_collect_live_records, read_live_records

    started = base.now_iso()
    budget = ScenarioBudget(_fixture_spec("N9").budget_s)
    steps: list[dict[str, Any]] = []
    details: dict[str, Any] = {
        "steps": steps,
        "build-live-row-latency-ms": None,
        "build-live-done-latency-ms": None,
        "build-finished-naturally": False,
        "standalone-cancelled-via-pilot": False,
        "standalone-cancel-ack-latency-ms": None,
        "standalone-cancelled-latency-ms": None,
        "standalone-heartbeat-advanced": False,
        "standalone-log-cycled": False,
        "queue-run-ids": {},
        "queue-live-latency-ms": {},
        "cancel-ack-latency-ms": None,
        "queue-cancel-history-latency-ms": None,
        "queue-cancelled-latency-ms": None,
        "editor-spawn-attempted": False,
        "history-terminal-snapshot-count": 0,
        "history-artifacts-resolve": False,
        "live-records-terminal-after-gc": False,
        "phase-log-path": str(_n9_phase_log_path(repo)),
    }
    build_proc: subprocess.Popen[str] | None = None
    build_started: float | None = None
    build_handle = None
    watcher_handle = None
    build_log_path = _background_log_path("n9-build")
    watcher_log_path = _background_log_path("n9-queue-run")
    outputs: list[str] = []
    result_returncode = 1
    wall_started = time.monotonic()
    runtime: dict[str, Any] = {
        "build_proc": None,
        "build_started": None,
        "watcher_proc": None,
        "watcher_step": None,
        "watcher_started": None,
        "merge_step": None,
    }
    old_editor = os.environ.get("EDITOR")

    def _start_queue_flow() -> None:
        if runtime["watcher_proc"] is not None:
            return
        base.queue_build(repo, "add-post", provider, N9_POST_INTENT)
        steps.append({"label": "queue-add-post", "rc": 0, "task_id": "add-post"})
        base.queue_build(repo, "add-delete", provider, N9_DELETE_INTENT)
        steps.append({"label": "queue-add-delete", "rc": 0, "task_id": "add-delete"})
        watcher_argv = [
            str(base.OTTO_BIN),
            "queue",
            "run",
            "--concurrent",
            "2",
            "--no-dashboard",
            "--exit-when-empty",
        ]
        base.log_line(f"$ {base.shell_join(watcher_argv)}  # background")
        runtime["watcher_started"] = time.monotonic()
        nonlocal watcher_handle
        watcher_handle = watcher_log_path.open("w", encoding="utf-8")
        runtime["watcher_proc"] = subprocess.Popen(
            watcher_argv,
            cwd=repo,
            env=base.current_ctx().env,
            stdout=watcher_handle,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=os.setsid,
        )
        runtime["watcher_step"] = {
            "label": "queue-run",
            "argv": watcher_argv,
            "rc": None,
            "duration_s": None,
        }
        steps.append(runtime["watcher_step"])

    def _start_build_flow() -> None:
        nonlocal build_handle, build_proc, build_started
        if runtime["build_proc"] is not None:
            return
        base.log_line(f"$ {base.shell_join(build_argv)}  # background")
        build_env = dict(base.current_ctx().env)
        build_env["OTTO_RUN_ID"] = build_run_id
        build_handle = build_log_path.open("w", encoding="utf-8")
        build_started = time.monotonic()
        build_proc = subprocess.Popen(
            build_argv,
            cwd=repo,
            env=build_env,
            stdout=build_handle,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=os.setsid,
        )
        runtime["build_proc"] = build_proc
        runtime["build_started"] = build_started
        steps.append(build_step)

    try:
        phase_log = Path(details["phase-log-path"])
        if phase_log.exists():
            phase_log.unlink()
        build_run_id = allocate_run_id(repo)
        details["build-run-id"] = build_run_id
        build_argv = [
            str(base.OTTO_BIN),
            "build",
            "--provider",
            provider,
            "--allow-dirty",
            N9_BUILD_INTENT,
        ]
        build_step = {
            "label": "build-background",
            "argv": build_argv,
            "run_id": build_run_id,
            "rc": None,
            "duration_s": None,
        }

        os.environ["EDITOR"] = "true"
        with _capture_mission_control_action_spawns() as action_spawns:
            asyncio.run(
                _drive_n9_mission_control(
                    repo=repo,
                    build_run_id=build_run_id,
                    details=details,
                    runtime=runtime,
                    start_build_flow=_start_build_flow,
                    start_queue_flow=_start_queue_flow,
                    budget=budget,
                    action_spawns=action_spawns,
                )
            )
            details["action-spawns"] = [
                {"argv": spawn["argv"], "pid": spawn["pid"]}
                for spawn in action_spawns
            ]

        if build_proc is None or build_started is None:
            raise AssertionError("N9 standalone build never started")
        build_rc = build_proc.wait(timeout=1.0)
        build_step["rc"] = build_rc
        build_step["duration_s"] = round(time.monotonic() - build_started, 1)
        if build_rc != 0 and details.get("standalone-cancelled-via-pilot") is not True:
            raise AssertionError(f"N9 standalone build exited with rc={build_rc}")

        if runtime["watcher_proc"] is None or runtime["watcher_step"] is None:
            raise AssertionError("N9 never started the queue watcher")
        if runtime["watcher_proc"].poll() is None:
            watcher_rc = runtime["watcher_proc"].wait(timeout=budget.timeout_for(60))
            runtime["watcher_step"]["rc"] = watcher_rc
            runtime["watcher_step"]["duration_s"] = round(time.monotonic() - runtime["watcher_started"], 1)
        if runtime["watcher_step"]["rc"] != 0:
            raise AssertionError(f"N9 queue watcher exited with rc={runtime['watcher_step']['rc']}")
        if runtime["merge_step"] is None:
            raise AssertionError("N9 did not record the selected-row merge step")
        steps.append(runtime["merge_step"])

        snapshots = _read_terminal_snapshots(repo)
        details["history-terminal-snapshot-count"] = len(snapshots)
        details["history-terminal-outcomes"] = {
            str(row.get("run_id") or ""): str(row.get("terminal_outcome") or "")
            for row in snapshots
        }
        details["history-artifacts-resolve"] = all(_artifact_paths_resolve(row) for row in snapshots)

        gc_removed = garbage_collect_live_records(repo, terminal_retention_s=0)
        details["gc-removed-run-ids"] = gc_removed
        live_records = read_live_records(repo)
        details["live-records-terminal-after-gc"] = all(record.status in {"done", "failed", "cancelled", "removed"} for record in live_records)
        result_returncode = 0
    except Exception as exc:
        details["error"] = str(exc)
        result_returncode = 1
    finally:
        if old_editor is None:
            os.environ.pop("EDITOR", None)
        else:
            os.environ["EDITOR"] = old_editor
        if build_handle is not None:
            build_handle.flush()
            build_handle.close()
        if watcher_handle is not None:
            watcher_handle.flush()
            watcher_handle.close()
        if build_proc is not None and build_proc.poll() is None:
            details["build-cleanup-forced"] = _wait_then_terminate_process_group(build_proc, wait_s=2.0)
        watcher_proc = runtime["watcher_proc"]
        if watcher_proc is not None and watcher_proc.poll() is None:
            details["watcher-cleanup-forced"] = _wait_then_terminate_process_group(watcher_proc, wait_s=15.0)
        build_output = _read_log(build_log_path)
        watcher_output = _read_log(watcher_log_path)
        if build_output:
            outputs.append(build_output)
        if watcher_output:
            outputs.append(watcher_output)

    total_duration_s = round(time.monotonic() - wall_started, 1)
    return _base_run_result(
        "N9",
        repo,
        started,
        "\n".join(part for part in outputs if part),
        total_duration_s,
        details,
        result_returncode,
    )


def verify_n9(repo: Path, run_result: base.RunResult) -> VerifyResult:
    details = run_result.details
    queue_cancel_history_latency_ms = details.get("queue-cancel-history-latency-ms")
    if queue_cancel_history_latency_ms is None:
        queue_cancel_history_latency_ms = details.get("cancel-ack-latency-ms")
    if run_result.returncode != 0:
        return _build_failure(run_result, "N9 realistic Mission Control session failed before verification")
    if details.get("build-live-row-latency-ms") is None or details.get("build-live-row-latency-ms") > 5000:
        return VerifyResult(False, "N9 Mission Control did not surface the standalone build row in time")
    if details.get("build-finished-naturally") is not True and details.get("standalone-cancelled-via-pilot") is not True:
        return VerifyResult(False, "N9 standalone build neither finished nor cancelled via Mission Control")
    if details.get("standalone-cancelled-via-pilot") is True and details.get("standalone-cancel-ack-latency-ms") is None:
        return VerifyResult(False, "N9 standalone cancel never received an ack")
    if details.get("standalone-heartbeat-advanced") is not True:
        return VerifyResult(False, "N9 standalone heartbeat did not advance while Detail was open")
    if details.get("standalone-log-cycled") is not True:
        return VerifyResult(False, "N9 did not switch standalone logs with `o`")
    if queue_cancel_history_latency_ms is None:
        return VerifyResult(False, "N9 queue cancel was not confirmed via terminal history")
    if details.get("queue-cancelled-latency-ms") is None:
        return VerifyResult(False, "N9 Mission Control never refreshed the cancelled queue row")
    if details.get("editor-spawn-attempted") is not True:
        return VerifyResult(False, "N9 did not attempt to spawn $EDITOR from History")
    merge_argv = details.get("merge-spawn-argv") or []
    if "--all" in merge_argv:
        return VerifyResult(False, "N9 merge was not launched from selected queue rows")
    if int(details.get("history-terminal-snapshot-count") or 0) < 3:
        return VerifyResult(False, "N9 expected at least three terminal history snapshots after merge")
    terminal_outcomes = details.get("history-terminal-outcomes") or {}
    merge_run_id = details.get("merge-run-id") or details.get("merge-history-run-id")
    merge_history_success = bool(
        merge_run_id and terminal_outcomes.get(merge_run_id) == "success"
    ) or details.get("merge-history-terminal-outcome") == "success"
    merge_observed = bool(merge_argv) or details.get("merge-live-record-seen") is True or merge_history_success
    if not merge_observed:
        return VerifyResult(False, "N9 merge was not observed from selected queue rows")
    cancelled_run_id = details.get("cancelled-queue-run-id")
    if not cancelled_run_id or terminal_outcomes.get(cancelled_run_id) != "cancelled":
        return VerifyResult(False, "N9 cancelled queue row was not recorded as terminal_outcome=cancelled")
    if sum(1 for outcome in terminal_outcomes.values() if outcome == "cancelled") < 1:
        return VerifyResult(False, "N9 expected at least one cancelled terminal snapshot")
    if sum(1 for outcome in terminal_outcomes.values() if outcome == "success") < 2:
        return VerifyResult(False, "N9 expected at least two successful terminal snapshots")
    if details.get("history-artifacts-resolve") is not True:
        return VerifyResult(False, "N9 terminal history rows referenced missing artifacts")
    if details.get("live-records-terminal-after-gc") is not True:
        return VerifyResult(False, "N9 left non-terminal live records after cleanup")
    visible, hidden = _visible_and_hidden(repo, run_result)
    if visible.returncode != 0:
        return VerifyResult(False, "N9 visible tests failed after the realistic operator session")
    if hidden.returncode != 0:
        return VerifyResult(False, "N9 hidden realistic-session checks failed after merge")
    return VerifyResult(True, "N9 drove a realistic Mission Control operator session and kept the substrate coherent")


SCENARIOS: dict[str, base.Scenario] = {
    "N1": base.Scenario("N1", "N", "evolving product loop", False, 3.0, 15 * 60, False, setup_n1, run_n1, verify_n1),
    "N2": base.Scenario("N2", "N", "semantic auth merge conflict", False, 2.6, 13 * 60, False, setup_n2, run_n2, verify_n2),
    "N4": base.Scenario("N4", "N", "certifier trap with hidden invariants", False, 1.5, 8 * 60, False, setup_n4, run_n4, verify_n4),
    "N8": base.Scenario("N8", "N", "stale merge context after first graduation", False, 3.8, 20 * 60, False, setup_n8, run_n8, verify_n8),
    "N9": base.Scenario("N9", "N", "realistic operator session", False, 4.0, 25 * 60, False, setup_n9, run_n9, verify_n9),
    # Weekly-only scenarios (#3, #5, #6, #7) intentionally left out for now.
}


def parse_csv_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def select_scenarios(*, mode: str, scenario_csv: str | None, group_csv: str | None) -> list[base.Scenario]:
    del mode
    selected_ids = set(SCENARIOS)
    if scenario_csv:
        selected_ids = set(parse_csv_ids(scenario_csv))
    if group_csv:
        groups = {value.upper() for value in parse_csv_ids(group_csv)}
        by_group = {scenario_id for scenario_id, scenario in SCENARIOS.items() if scenario.group.upper() in groups}
        selected_ids = by_group if not scenario_csv else selected_ids & by_group
    missing = [scenario_id for scenario_id in selected_ids if scenario_id not in SCENARIOS]
    if missing:
        raise SystemExit(f"unknown scenarios: {', '.join(sorted(missing))}")
    ordered = [SCENARIOS[scenario_id] for scenario_id in SCENARIOS if scenario_id in selected_ids]
    if not ordered:
        raise SystemExit("no scenarios selected")
    return ordered


def print_scenario_list() -> None:
    print("ID  Group  Cost      Budget  Fixture")
    for scenario_id, scenario in SCENARIOS.items():
        spec = SCENARIO_SPECS[scenario_id]
        print(
            f"{scenario_id:2}  {scenario.group:5}  {spec.est_cost_range:8}  "
            f"{int(spec.budget_s / 60):>3}m    {spec.fixture_dir.relative_to(REPO_ROOT)}"
        )


def print_dry_run(scenarios: list[base.Scenario], provider: str) -> None:
    for scenario in scenarios:
        spec = _fixture_spec(scenario.name)
        print(f"{scenario.name}: {scenario.description}")
        print(f"  fixture: {spec.fixture_dir}")
        print(f"  intent: {spec.intent_path}")
        print(f"  budget: {int(spec.budget_s / 60)}m")
        print(f"  provider: {provider}")
        print("  project-intent:")
        for line in _read_intent(spec.intent_path).splitlines():
            print(f"    {line}")
        print("  plan:")
        for step in spec.step_plan:
            print(f"    - {step.replace('<provider>', provider)}")
        print()


def _write_attempt_metadata(artifact_dir: Path, scenario_id: str, repo: Path, attempt_index: int) -> None:
    base.write_json(
        artifact_dir / base.attempt_filename("attempt.json", attempt_index),
        {"scenario": scenario_id, "repo": str(repo), "attempt": attempt_index},
    )


def run_one_attempt(
    scenario: base.Scenario,
    provider: str,
    artifact_dir: Path,
    *,
    attempt_index: int,
) -> tuple[Path, base.RunResult, VerifyResult]:
    repo = Path(tempfile.mkdtemp(prefix=f"otto-nightly-{scenario.name.lower()}-", dir="/tmp"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    with execution_context(
        scenario=scenario,
        repo=repo,
        artifact_dir=artifact_dir,
        provider=provider,
        attempt_index=attempt_index,
    ):
        _write_attempt_metadata(artifact_dir, scenario.name, repo, attempt_index)
        try:
            base.log_line(f"[{scenario.name}] fixture={_fixture_spec(scenario.name).fixture_dir}")
            scenario.setup(repo, provider)
            run_result = scenario.run(repo, provider)
        except Exception as exc:
            trace = traceback.format_exc()
            with base.current_ctx().debug_log.open("a", encoding="utf-8") as handle:
                handle.write(trace)
            run_result = _failed_run_result(
                scenario.name,
                repo,
                trace,
                {"error": str(exc)},
            )
        verify_result = scenario.verify(repo, run_result)

    base.write_json(artifact_dir / base.attempt_filename("run_result.json", attempt_index), asdict(run_result))
    base.write_json(artifact_dir / base.attempt_filename("verify.json", attempt_index), asdict(verify_result))
    return repo, run_result, verify_result


def record_one_scenario(scenario: base.Scenario, run_id: str, provider: str) -> NightlyOutcome:
    artifact_dir = DEFAULT_ARTIFACT_ROOT / run_id / scenario.name
    repo, first_run_result, first_verify = run_one_attempt(scenario, provider, artifact_dir, attempt_index=1)
    if first_verify.ok:
        return NightlyOutcome(
            scenario=scenario,
            outcome="PASS",
            run_result=first_run_result,
            verify_result=first_verify,
            artifact_dir=artifact_dir,
            wall_duration_s=first_run_result.duration_s,
        )

    first_classification = base.classify_failure(
        base.latest_narrative_log(repo),
        Path(first_run_result.debug_log),
        first_run_result,
    )
    override = _classification_override(first_run_result)
    if override is not None:
        first_classification = override
    if first_classification != "INFRA":
        return NightlyOutcome(
            scenario=scenario,
            outcome="FAIL",
            run_result=first_run_result,
            verify_result=first_verify,
            artifact_dir=artifact_dir,
            wall_duration_s=first_run_result.duration_s,
        )

    print(
        f"[{scenario.name}] INFRA detected; sleeping "
        f"{base.format_seconds_for_log(base.INFRA_RETRY_DELAY_S)} and retrying",
        flush=True,
    )
    time.sleep(base.INFRA_RETRY_DELAY_S)
    retry_repo, retry_run_result, retry_verify = run_one_attempt(
        scenario,
        provider,
        artifact_dir,
        attempt_index=2,
    )
    wall = first_run_result.duration_s + base.INFRA_RETRY_DELAY_S + retry_run_result.duration_s
    if retry_verify.ok:
        retry_verify.detail = f"{retry_verify.detail} (retried after INFRA)"
        return NightlyOutcome(
            scenario=scenario,
            outcome="PASS",
            run_result=retry_run_result,
            verify_result=retry_verify,
            artifact_dir=artifact_dir,
            attempt_count=2,
            wall_duration_s=wall,
            retried_after_infra=True,
        )

    retry_classification = base.classify_failure(
        base.latest_narrative_log(retry_repo),
        Path(retry_run_result.debug_log),
        retry_run_result,
    )
    override = _classification_override(retry_run_result)
    if override is not None:
        retry_classification = override
    return NightlyOutcome(
        scenario=scenario,
        outcome=retry_classification,
        run_result=retry_run_result,
        verify_result=retry_verify,
        artifact_dir=artifact_dir,
        attempt_count=2,
        wall_duration_s=wall,
        retried_after_infra=True,
    )


def print_summary(run_id: str, provider: str, outcomes: list[NightlyOutcome]) -> None:
    print()
    print(f"otto-as-user-nightly run {run_id}")
    print(f"provider: {provider}   scenarios: {len(outcomes)}")
    print()
    print("ID  Result  Duration  Artifact")
    for item in outcomes:
        rel = item.artifact_dir.relative_to(DEFAULT_ARTIFACT_ROOT)
        duration = int(item.wall_duration_s or item.run_result.duration_s or item.scenario.estimated_seconds)
        print(f"{item.scenario.name:2}  {item.outcome:5}  {duration:>4}s     {rel}")
        print(f"    {item.verify_result.detail}")
    print()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Otto nightly as-user scenarios with hidden-test verification.")
    parser.add_argument("--mode", choices=["core", "full"], default="core")
    parser.add_argument("--provider", choices=["claude", "codex"], default="claude")
    parser.add_argument("--scenario", help="Comma-separated scenario ids, e.g. N1,N4")
    parser.add_argument("--group", help="Comma-separated group ids, e.g. N")
    parser.add_argument(
        "--scenario-delay",
        type=float,
        default=DEFAULT_SCENARIO_DELAY_S,
        help=f"Seconds to sleep between scenarios (default: {DEFAULT_SCENARIO_DELAY_S}s)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the fixture path, intent, and execution plan without invoking Otto")
    parser.add_argument("--bail-fast", action="store_true", help="Stop on first real FAIL (INFRA does not bail)")
    parser.add_argument("--list", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.list:
        print_scenario_list()
        return 0
    if args.scenario_delay < 0:
        raise SystemExit("--scenario-delay must be >= 0")

    scenarios = select_scenarios(mode=args.mode, scenario_csv=args.scenario, group_csv=args.group)
    if args.dry_run:
        print_dry_run(scenarios, args.provider)
        return 0

    run_id = base.utc_run_id()
    outcomes: list[NightlyOutcome] = []
    for index, scenario in enumerate(scenarios):
        print(f"\n=== {scenario.name} {scenario.description} ===", flush=True)
        outcome = record_one_scenario(scenario, run_id, args.provider)
        outcomes.append(outcome)
        print(f"[{scenario.name}] {outcome.outcome}: {outcome.verify_result.detail}", flush=True)
        if args.bail_fast and outcome.outcome == "FAIL":
            break
        if index < len(scenarios) - 1:
            print(
                f"[scenario-delay] sleeping {base.format_seconds_for_log(args.scenario_delay)} before next scenario",
                flush=True,
            )
            time.sleep(args.scenario_delay)
    print_summary(run_id, args.provider, outcomes)
    return 1 if any(item.outcome == "FAIL" for item in outcomes) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
