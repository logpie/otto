#!/usr/bin/env python3
from __future__ import annotations

import argparse
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

N9_BUILD_INTENT = (
    "Add a production-style GET /tasks endpoint with input validation, "
    "predictable error handling, and lightweight OpenAPI-style response "
    "documentation comments while keeping the implementation in-memory."
)
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
        est_cost_range="$1.7-$2.7",
        budget_s=20 * 60,
        step_plan=[
            f"otto build --provider <provider> --allow-dirty {N9_BUILD_INTENT!r} (background, will be cancelled)",
            f"otto queue build {N9_POST_INTENT!r} --as add-post",
            f"otto queue build {N9_DELETE_INTENT!r} --as add-delete",
            "otto queue run --concurrent 2 --no-dashboard --exit-when-empty",
            "<harness appends cancel envelope to standalone build>",
            "otto merge --all --cleanup-on-success",
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


def run_n9(repo: Path, provider: str) -> base.RunResult:
    from otto import paths
    from otto.runs.registry import (
        HEARTBEAT_INTERVAL_S,
        allocate_run_id,
        load_live_record,
        read_live_records,
    )

    started = base.now_iso()
    budget = ScenarioBudget(_fixture_spec("N9").budget_s)
    steps: list[dict[str, Any]] = []
    details: dict[str, Any] = {
        "steps": steps,
        "live-records-discovered-count": 0,
        "build_record_latency_ms": None,
        "queue_record_1_latency_ms": None,
        "queue_record_2_latency_ms": None,
        "cancel-ack-latency-ms": None,
        "build-terminated-via-cancel": False,
    }
    build_proc: subprocess.Popen[str] | None = None
    watcher_proc: subprocess.Popen[str] | None = None
    build_handle = None
    watcher_handle = None
    build_log_path = _background_log_path("n9-build")
    watcher_log_path = _background_log_path("n9-queue-run")
    outputs: list[str] = []
    merge_duration_s = 0.0
    build_step: dict[str, Any] | None = None
    watcher_step: dict[str, Any] | None = None
    result_returncode = 1

    try:
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
        build_step = {
            "label": "build-background",
            "argv": build_argv,
            "run_id": build_run_id,
            "rc": None,
            "duration_s": None,
        }
        steps.append(build_step)

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
        watcher_handle = watcher_log_path.open("w", encoding="utf-8")
        watcher_started = time.monotonic()
        watcher_proc = subprocess.Popen(
            watcher_argv,
            cwd=repo,
            env=base.current_ctx().env,
            stdout=watcher_handle,
            stderr=subprocess.STDOUT,
            text=True,
            preexec_fn=os.setsid,
        )
        watcher_step = {
            "label": "queue-run",
            "argv": watcher_argv,
            "rc": None,
            "duration_s": None,
        }
        steps.append(watcher_step)

        session_dir = paths.session_dir(repo, build_run_id)
        commands_dir = paths.session_commands_dir(repo, build_run_id)
        request_path = paths.session_command_requests(repo, build_run_id)
        ack_path = paths.session_command_acks(repo, build_run_id)
        build_record_deadline_s = 5.0
        queue_record_deadline_s = 30.0
        build_record_deadline = build_started + build_record_deadline_s
        queue_record_deadline = watcher_started + queue_record_deadline_s
        session_ready_latency_ms: int | None = None
        discovered_atomic = False
        discovered_queue_runs: set[str] = set()
        queue_record_order: list[str] = []

        def refresh_live_records(now: float | None = None) -> None:
            nonlocal discovered_atomic
            if now is None:
                now = time.monotonic()
            records = read_live_records(repo)
            if not discovered_atomic and any(record.run_id == build_run_id for record in records):
                discovered_atomic = True
                details["build_record_latency_ms"] = int((now - build_started) * 1000)
            for record in records:
                if record.domain != "queue" or record.run_id in discovered_queue_runs:
                    continue
                discovered_queue_runs.add(record.run_id)
                queue_record_order.append(record.run_id)
                queue_index = len(queue_record_order)
                if queue_index <= 2:
                    details[f"queue_record_{queue_index}_latency_ms"] = int((now - watcher_started) * 1000)
            details["live-records-discovered-count"] = int(discovered_atomic) + len(discovered_queue_runs)

        while time.monotonic() < build_record_deadline:
            now = time.monotonic()
            build_rc = build_proc.poll()
            if build_rc is not None and not session_dir.exists():
                details["classification_override"] = "INFRA"
                details["build-finished-before-cancel-ready"] = True
                details["build-returncode"] = build_rc
                if build_step is not None:
                    build_step["rc"] = build_rc
                    build_step["duration_s"] = round(now - build_started, 1)
                raise AssertionError("N9 standalone build exited before its cancel channel became ready")
            refresh_live_records(now)
            if session_dir.exists() and discovered_atomic:
                session_ready_latency_ms = int((now - build_started) * 1000)
                break
            time.sleep(0.05)
        else:
            raise AssertionError(
                f"N9 expected the standalone build live record within {build_record_deadline_s:.1f}s"
            )
        refresh_live_records()
        details["live-records-latency-ms"] = details["build_record_latency_ms"]
        details["session-ready-latency-ms"] = session_ready_latency_ms
        details["cancel-request-path"] = str(request_path)
        base.log_line(
            "N9 session ready after "
            f"{session_ready_latency_ms}ms; commands_dir={commands_dir} "
            f"queue live records={len(discovered_queue_runs)}"
        )

        build_record = load_live_record(repo, build_run_id)
        details["heartbeat-interval-s"] = max(
            float(build_record.timing.get("heartbeat_interval_s") or HEARTBEAT_INTERVAL_S),
            0.1,
        )
        try:
            cancel = base.append_cancel_envelope_and_wait_for_ack(
                repo,
                build_run_id,
                proc=build_proc,
                run_exited_message="N9 standalone build exited before cancel ack arrived",
                timeout_message="N9 cancel ack did not arrive",
            )
        except AssertionError as exc:
            build_rc = build_proc.poll() if build_proc is not None else None
            if build_rc is not None and "cancel ack arrived" in str(exc):
                details["classification_override"] = "INFRA"
                details["build-finished-before-cancel-ack"] = True
                details["build-returncode"] = build_rc
                if build_step is not None and build_step["rc"] is None:
                    build_step["rc"] = build_rc
                    build_step["duration_s"] = round(time.monotonic() - build_started, 1)
            raise
        details["cancel-command-appended"] = True
        details["cancel-request-path"] = cancel["request_path"]
        details["cancel-ack-path"] = cancel["ack_path"]
        details["heartbeat-interval-s"] = cancel["heartbeat_interval_s"]
        details["cancel-command-id"] = cancel["command_id"]
        details["cancel-ack-latency-ms"] = cancel["ack_latency_ms"]
        details["cancel-ack-deadline-ms"] = cancel["ack_deadline_ms"]
        base.log_line(f"N9 appended cancel envelope command_id={cancel['command_id']}")
        base.log_line(f"N9 cancel ack observed after {cancel['ack_latency_ms']}ms")

        assert build_proc is not None
        try:
            build_rc = build_proc.wait(timeout=min(30.0, budget.timeout_for(60)))
        except subprocess.TimeoutExpired as exc:
            raise AssertionError("N9 standalone build did not terminate after cancel ack") from exc
        details["build-terminated-via-cancel"] = True
        details["build-returncode"] = build_rc
        if build_step is not None:
            build_step["rc"] = build_rc
            build_step["duration_s"] = round(time.monotonic() - build_started, 1)
        steps.append(
            _run_checked_step(
                "git-checkout-main-after-cancel",
                ["git", "checkout", "main"],
                repo=repo,
            )
        )

        while len(discovered_queue_runs) < 2 and time.monotonic() < queue_record_deadline:
            refresh_live_records()
            time.sleep(0.05)
        refresh_live_records()
        if len(discovered_queue_runs) < 2:
            missing_queue_records = 2 - len(discovered_queue_runs)
            raise AssertionError(
                "N9 queue live records did not all appear within "
                f"{queue_record_deadline_s:.1f}s of watcher start "
                f"(missing {missing_queue_records})"
            )

        assert watcher_proc is not None
        watcher_rc = watcher_proc.wait(timeout=budget.timeout_for(10 * 60))
        details["watcher-returncode"] = watcher_rc
        if watcher_step is not None:
            watcher_step["rc"] = watcher_rc
            watcher_step["duration_s"] = round(time.monotonic() - watcher_started, 1)
        if watcher_rc != 0:
            raise AssertionError(f"N9 queue watcher exited with rc={watcher_rc}")
        steps.append(
            _run_checked_step(
                "git-checkout-main-before-merge",
                ["git", "checkout", "main"],
                repo=repo,
            )
        )

        merge = base.run_merge(
            repo,
            "--all",
            "--cleanup-on-success",
            timeout_s=budget.timeout_for(10 * 60),
        )
        merge_duration_s = merge.duration_s
        steps.append(_step_record("merge", merge, repo))
        outputs.append(merge.output)
        result_returncode = merge.rc
    except Exception as exc:
        details["error"] = str(exc)
        result_returncode = 1
    finally:
        if build_handle is not None:
            build_handle.flush()
            build_handle.close()
        if watcher_handle is not None:
            watcher_handle.flush()
            watcher_handle.close()
        if build_proc is not None and build_proc.poll() is None:
            details["build-cleanup-forced"] = _wait_then_terminate_process_group(build_proc, wait_s=2.0)
        if watcher_proc is not None and watcher_proc.poll() is None:
            details["watcher-cleanup-forced"] = _wait_then_terminate_process_group(watcher_proc, wait_s=15.0)
        build_output = _read_log(build_log_path)
        watcher_output = _read_log(watcher_log_path)
        if build_output:
            outputs.insert(0, build_output)
        if watcher_output:
            outputs.append(watcher_output)
    return _base_run_result(
        "N9",
        repo,
        started,
        "\n".join(part for part in outputs if part),
        float((build_step or {}).get("duration_s") or 0.0)
        + float((watcher_step or {}).get("duration_s") or 0.0)
        + merge_duration_s,
        details,
        result_returncode,
    )


def verify_n9(repo: Path, run_result: base.RunResult) -> VerifyResult:
    details = run_result.details
    if run_result.returncode != 0:
        return _build_failure(run_result, "N9 mission-control flow failed before verification")
    build_record_latency_ms = details.get("build_record_latency_ms")
    if build_record_latency_ms is None or build_record_latency_ms > 5000:
        return VerifyResult(False, "N9 standalone build live record was missing or late")
    queue_record_1_latency_ms = details.get("queue_record_1_latency_ms")
    if queue_record_1_latency_ms is None or queue_record_1_latency_ms > 30000:
        return VerifyResult(False, "N9 first queue live record was missing or late")
    queue_record_2_latency_ms = details.get("queue_record_2_latency_ms")
    if queue_record_2_latency_ms is None or queue_record_2_latency_ms > 30000:
        return VerifyResult(False, "N9 second queue live record was missing or late")
    ack_latency_ms = details.get("cancel-ack-latency-ms")
    ack_deadline_ms = details.get("cancel-ack-deadline-ms", 0)
    if ack_latency_ms is None or ack_latency_ms > ack_deadline_ms:
        return VerifyResult(False, "N9 cancel ack was missing or late")
    if details.get("build-terminated-via-cancel") is not True:
        return VerifyResult(False, "N9 standalone build did not terminate via cancel")
    visible, hidden = _visible_and_hidden(repo, run_result)
    if visible.returncode != 0:
        return VerifyResult(False, "N9 visible tests failed after merge")
    if hidden.returncode != 0:
        return VerifyResult(False, "N9 hidden mission-control workflow checks failed after merge")
    return VerifyResult(True, "N9 observed registry coherence, cancelled the standalone build, and kept merge green")


SCENARIOS: dict[str, base.Scenario] = {
    "N1": base.Scenario("N1", "N", "evolving product loop", False, 3.0, 15 * 60, False, setup_n1, run_n1, verify_n1),
    "N2": base.Scenario("N2", "N", "semantic auth merge conflict", False, 2.6, 13 * 60, False, setup_n2, run_n2, verify_n2),
    "N4": base.Scenario("N4", "N", "certifier trap with hidden invariants", False, 1.5, 8 * 60, False, setup_n4, run_n4, verify_n4),
    "N8": base.Scenario("N8", "N", "stale merge context after first graduation", False, 3.8, 20 * 60, False, setup_n8, run_n8, verify_n8),
    "N9": base.Scenario("N9", "N", "mission control workflow", False, 2.0, 15 * 60, False, setup_n9, run_n9, verify_n9),
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
