#!/usr/bin/env python3
"""Agent-browser E2E scenarios for Otto Web Mission Control.

This script is intentionally outside the default pytest suite because it starts
real web servers and drives a real browser. It is the regression harness for
the user-facing Mission Control workflow.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from otto import paths
from otto.merge.state import BranchOutcome, MergeState, write_state as write_merge_state
from otto.queue.schema import QueueTask, append_task, write_state as write_queue_state


BROWSER_LOCK_DIR = Path(tempfile.gettempdir()) / "otto-agent-browser.lock"


@dataclass(slots=True)
class ScenarioContext:
    otto_root: Path
    run_root: Path
    artifacts_dir: Path
    port: int
    viewport_width: int
    viewport_height: int
    server: subprocess.Popen[str] | None = None
    repo: Path | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"


@dataclass(slots=True)
class Scenario:
    name: str
    description: str
    run: Callable[[ScenarioContext], None]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--otto-root", type=Path, default=Path.cwd(), help="Otto source tree to test.")
    parser.add_argument(
        "--scenario",
        choices=["all", "fresh-queue", "ready-land", "dirty-blocked", "multi-state", "command-backlog"],
        default="all",
    )
    parser.add_argument("--artifacts", type=Path, default=None, help="Directory for logs and screenshots.")
    parser.add_argument("--viewport", default="1440x1000", help="Browser viewport as WIDTHxHEIGHT.")
    parser.add_argument("--keep", action="store_true", help="Keep temporary projects after the run.")
    args = parser.parse_args()

    if shutil.which("agent-browser") is None:
        raise SystemExit("agent-browser is required for web Mission Control E2E")

    otto_root = args.otto_root.resolve(strict=False)
    run_root = Path(tempfile.mkdtemp(prefix="otto-web-e2e-"))
    default_artifacts = Path(tempfile.gettempdir()) / datetime.now().strftime("otto-web-e2e-%Y-%m-%d-%H%M%S")
    artifacts_dir = (args.artifacts or default_artifacts).resolve(strict=False)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    viewport_width, viewport_height = parse_viewport(args.viewport)
    print(f"[web-e2e] artifacts: {artifacts_dir}")
    selected = [scenario for scenario in scenarios() if args.scenario == "all" or scenario.name == args.scenario]
    results: list[dict[str, str]] = []
    try:
        for index, scenario in enumerate(selected, start=1):
            port = free_port()
            ctx = ScenarioContext(
                otto_root=otto_root,
                run_root=run_root / scenario.name,
                artifacts_dir=artifacts_dir / f"{index:02d}-{scenario.name}",
                port=port,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
            )
            ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
            print(f"[web-e2e] {scenario.name}: {scenario.description}")
            try:
                with browser_session_lock(scenario.name):
                    try:
                        scenario.run(ctx)
                    finally:
                        browser("close", check=False)
            except Exception as exc:
                results.append({"scenario": scenario.name, "status": "failed", "error": str(exc)})
                print(f"[web-e2e] FAIL {scenario.name}: {exc}", file=sys.stderr)
                raise
            else:
                results.append({"scenario": scenario.name, "status": "passed", "error": ""})
                print(f"[web-e2e] PASS {scenario.name}")
            finally:
                stop_server(ctx)
        (artifacts_dir / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    finally:
        if args.keep:
            print(f"[web-e2e] kept projects under {run_root}")
        else:
            shutil.rmtree(run_root, ignore_errors=True)
    return 0


def scenarios() -> list[Scenario]:
    return [
        Scenario("fresh-queue", "queue a first build from the web UI before the watcher starts", scenario_fresh_queue),
        Scenario("ready-land", "review and land a clean completed task", scenario_ready_land),
        Scenario("dirty-blocked", "show a clean recovery path when local changes block landing", scenario_dirty_blocked),
        Scenario("multi-state", "audit queued, failed, ready, and landed work in one board", scenario_multi_state),
        Scenario("command-backlog", "recover pending command backlog when the watcher is stopped", scenario_command_backlog),
    ]


def scenario_fresh_queue(ctx: ScenarioContext) -> None:
    repo = init_repo(ctx.run_root / "fresh")
    start_server(ctx, repo)
    open_app(ctx)

    browser("find", "testid", "new-job-button", "click")
    wait_text("New queue job")
    assert_modal_focus()
    browser("find", "label", "Intent / focus", "fill", "Build an expense approval portal for a small company.")
    browser("find", "label", "Task id", "fill", "expense-portal")
    browser("find", "role", "button", "click", "--name", "Queue job")
    wait_text("queued expense-portal")
    wait_text("Task Board")
    wait_text("1 queued task waiting")
    wait_text("Waiting for watcher")
    assert_page_lacks("fatal:")
    assert_page_lacks("queue manifest missing")
    assert_page_lacks("worktree missing")
    screenshot(ctx, "fresh-queue.png")


def scenario_ready_land(ctx: ScenarioContext) -> None:
    repo = init_repo(ctx.run_root / "ready")
    seed_ready_task(repo, task_id="saved-views", filename="saved_views.txt")
    start_server(ctx, repo)
    open_app(ctx)

    wait_text("Ready To Land")
    browser("find", "testid", "task-card-saved-views", "click")
    wait_text("Ready for review")
    wait_text("Safe to land into main.")
    browser("find", "role", "button", "click", "--name", "Land selected")
    browser("find", "role", "button", "click", "--name", "Land task")
    wait_text("merge saved-views")
    wait_for_api_state(ctx, lambda state: state["landing"]["counts"]["merged"] >= 1, "task landed", timeout_s=20)
    assert_git_file(repo, "main", "saved_views.txt", "saved-views")
    browser("reload")
    wait_text("Landed")
    item = landing_item(api_json(ctx, "api/state"), "saved-views")
    assert item["landing_state"] == "merged"
    assert item["run_id"] == "run-saved-views"
    browser("find", "testid", "task-card-saved-views", "click")
    wait_text("Already merged into main")
    packet = api_json(ctx, "api/runs/run-saved-views")["review_packet"]
    assert packet["readiness"]["state"] == "merged"
    assert packet["next_action"]["enabled"] is False
    assert packet["changes"]["diff_error"] is None
    assert packet["changes"]["diff_command"] is None
    assert_page_lacks("Ready for review")
    assert_page_lacks("No changed files were detected")
    screenshot(ctx, "ready-land.png")


def scenario_dirty_blocked(ctx: ScenarioContext) -> None:
    repo = init_repo(ctx.run_root / "dirty")
    seed_ready_task(repo, task_id="invoice-export", filename="invoice_export.txt")
    (repo / "README.md").write_text("# dirty\n\nlocal edit\n", encoding="utf-8")
    start_server(ctx, repo)
    open_app(ctx)

    wait_text("Cleanup required before landing")
    wait_text("Local changes block landing")
    browser("find", "testid", "task-card-invoice-export", "click")
    wait_text("Repository cleanup required before landing")
    wait_text("Review blocked")
    wait_text("README.md")
    detail = api_json(ctx, "api/runs/run-invoice-export")
    packet = detail["review_packet"]
    assert packet["headline"] == "Repository cleanup required before landing"
    assert packet["readiness"]["state"] == "blocked"
    assert packet["next_action"]["label"] == "Land blocked"
    assert packet["next_action"]["enabled"] is False
    assert_page_lacks("fatal:")
    assert_page_lacks("queue manifest missing")
    assert_page_lacks("summary missing")
    assert_page_lacks("worktree missing")
    screenshot(ctx, "dirty-blocked.png")


def scenario_multi_state(ctx: ScenarioContext) -> None:
    repo = init_repo(ctx.run_root / "multi")
    seed_queued_task(repo, "queued-search")
    seed_failed_task(repo, "failed-report")
    seed_ready_task(repo, task_id="ready-dashboard", filename="ready_dashboard.txt")
    seed_landed_task(repo, task_id="landed-settings", filename="landed_settings.txt")
    start_server(ctx, repo)
    open_app(ctx)

    wait_text("Needs Action")
    wait_text("Queued / Running")
    wait_text("Ready To Land")
    wait_text("Landed")
    state = api_json(ctx, "api/state")
    by_task = {item["task_id"]: item for item in state["landing"]["items"]}
    assert by_task["queued-search"]["queue_status"] == "queued"
    assert by_task["ready-dashboard"]["landing_state"] == "ready"
    assert by_task["landed-settings"]["landing_state"] == "merged"
    assert by_task["failed-report"]["queue_status"] == "failed"
    for task_id in ["queued-search", "failed-report", "ready-dashboard", "landed-settings"]:
        wait_text(task_id)
    browser("find", "testid", "task-card-queued-search", "click")
    wait_text("Waiting for watcher")
    queued_run = next(item for item in state["live"]["items"] if item["queue_task_id"] == "queued-search")["run_id"]
    assert api_json(ctx, f"api/runs/{queued_run}")["review_packet"]["readiness"]["state"] == "in_progress"
    browser("find", "testid", "task-card-ready-dashboard", "click")
    wait_text("Ready for review")
    ready_packet = api_json(ctx, "api/runs/run-ready-dashboard")["review_packet"]
    assert ready_packet["next_action"]["action_key"] == "m"
    assert ready_packet["next_action"]["enabled"] is True
    browser("find", "testid", "task-card-landed-settings", "click")
    wait_text("Already merged into main")
    landed_packet = api_json(ctx, "api/runs/run-landed-settings")["review_packet"]
    assert landed_packet["readiness"]["state"] == "merged"
    assert landed_packet["next_action"]["enabled"] is False
    assert landed_packet["changes"]["diff_error"] is None
    browser("find", "testid", "task-card-failed-report", "click")
    wait_text("Failed; review evidence and requeue or remove")
    failed_packet = api_json(ctx, "api/runs/run-failed-report")["review_packet"]
    assert failed_packet["readiness"]["state"] == "needs_attention"
    browser("find", "testid", "diagnostics-tab", "click")
    wait_text("Diagnostics Summary")
    wait_text("Review Packet")
    wait_text("Failed; review evidence and requeue or remove")
    wait_text("Ready to land")
    wait_text("Landed")
    wait_text("Live Runs")
    wait_text("Operator Timeline")
    assert_page_lacks("legacy queue mode")
    screenshot(ctx, "multi-state-diagnostics.png")


def scenario_command_backlog(ctx: ScenarioContext) -> None:
    repo = init_repo(ctx.run_root / "command-backlog")
    paths.queue_commands_path(repo).write_text(
        json.dumps({"command_id": "cmd-retry-1", "run_id": "run-missing", "action": "retry"}) + "\n",
        encoding="utf-8",
    )
    start_server(ctx, repo)
    open_app(ctx)

    wait_text("Commands are waiting")
    wait_text("Start watcher")
    browser("find", "testid", "diagnostics-tab", "click")
    wait_text("Command Backlog")
    wait_text("cmd-retry-1")
    assert_page_lacks("Queue the first job")
    state = api_json(ctx, "api/state")
    assert state["runtime"]["command_backlog"]["pending"] == 1
    assert state["runtime"]["command_backlog"]["items"][0]["command_id"] == "cmd-retry-1"
    assert state["runtime"]["supervisor"]["can_start"] is True
    enabled = browser_eval("(() => { const el = document.querySelector('[data-testid=\"start-watcher-button\"]'); return el instanceof HTMLButtonElement && !el.disabled; })()")
    assert enabled.endswith("true"), enabled
    browser("find", "testid", "start-watcher-button", "click")
    wait_for_api_state(ctx, lambda state: state["runtime"]["command_backlog"]["pending"] == 0, "command backlog drained", timeout_s=20)
    state = api_json(ctx, "api/state")
    assert state["runtime"]["command_backlog"]["pending"] == 0
    assert state["runtime"]["command_backlog"]["processing"] == 0
    api_post(ctx, "api/watcher/stop", {})
    wait_for_api_state(ctx, lambda state: state["watcher"]["health"]["state"] != "running", "watcher stopped", timeout_s=20)
    browser("reload")
    browser("find", "testid", "diagnostics-tab", "click")
    wait_text("No pending commands")
    screenshot(ctx, "command-backlog.png")


def init_repo(repo: Path) -> Path:
    repo.mkdir(parents=True, exist_ok=True)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "web-e2e@example.com")
    git(repo, "config", "user.name", "Web E2E")
    (repo / ".gitignore").write_text(
        "\n".join(
            [
                "otto_logs/",
                ".worktrees/",
                ".otto-queue.yml",
                ".otto-queue.yml.lock",
                ".otto-queue-state.json",
                ".otto-queue-commands.jsonl",
                ".otto-queue-commands.jsonl.lock",
                ".otto-queue-commands.processing.jsonl",
                ".otto-queue-command-acks.jsonl",
                ".otto-queue.lock",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / "otto.yaml").write_text("default_branch: main\nqueue:\n  bookkeeping_files: []\n", encoding="utf-8")
    (repo / "README.md").write_text("# web e2e\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "initial")
    return repo


def seed_queued_task(repo: Path, task_id: str) -> None:
    append_task(
        repo,
        QueueTask(
            id=task_id,
            command_argv=["build", task_id.replace("-", " ")],
            added_at=iso_now(),
            resolved_intent=f"Build {task_id.replace('-', ' ')}",
            branch=f"build/{task_id}",
            worktree=f".worktrees/{task_id}",
        ),
    )
    write_queue_state(repo, {"schema_version": 1, "watcher": None, "tasks": {}})


def seed_failed_task(repo: Path, task_id: str) -> None:
    append_task(
        repo,
        QueueTask(
            id=task_id,
            command_argv=["build", task_id.replace("-", " ")],
            added_at=iso_now(),
            resolved_intent=f"Build {task_id.replace('-', ' ')}",
            branch=f"build/{task_id}",
            worktree=f".worktrees/{task_id}",
        ),
    )
    merge_queue_state(
        repo,
        task_id,
        {
            "status": "failed",
            "attempt_run_id": f"run-{task_id}",
            "started_at": iso_now(),
            "finished_at": iso_now(),
            "failure_reason": "visible test failed",
        },
    )


def seed_ready_task(repo: Path, *, task_id: str, filename: str) -> None:
    branch = f"build/{task_id}"
    git(repo, "checkout", "-q", "-b", branch)
    (repo / filename).write_text(f"{task_id}\n", encoding="utf-8")
    git(repo, "add", filename)
    git(repo, "commit", "-q", "-m", f"add {task_id}")
    git(repo, "checkout", "-q", "main")
    append_task(
        repo,
        QueueTask(
            id=task_id,
            command_argv=["build", task_id.replace("-", " ")],
            added_at=iso_now(),
            resolved_intent=f"Build {task_id.replace('-', ' ')}",
            branch=branch,
            worktree=f".worktrees/{task_id}",
        ),
    )
    merge_queue_state(
        repo,
        task_id,
        {
            "status": "done",
            "attempt_run_id": f"run-{task_id}",
            "started_at": iso_now(),
            "finished_at": iso_now(),
            "stories_passed": 2,
            "stories_tested": 2,
        },
    )


def seed_landed_task(repo: Path, *, task_id: str, filename: str) -> None:
    seed_ready_task(repo, task_id=task_id, filename=filename)
    git(repo, "merge", "--no-ff", "-m", f"land {task_id}", f"build/{task_id}")
    merge_commit = head_sha(repo)
    write_merge_state(
        repo,
        MergeState(
            merge_id=f"merge-{task_id}",
            started_at=iso_now(),
            finished_at=iso_now(),
            target="main",
            status="done",
            terminal_outcome="success",
            branches_in_order=[f"build/{task_id}"],
            outcomes=[BranchOutcome(branch=f"build/{task_id}", status="merged", merge_commit=merge_commit)],
        ),
    )


def merge_queue_state(repo: Path, task_id: str, task_state: dict[str, object]) -> None:
    path = paths.queue_state_path(repo)
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            state = {}
    else:
        state = {}
    tasks = state.get("tasks")
    if not isinstance(tasks, dict):
        tasks = {}
    tasks[task_id] = task_state
    state["schema_version"] = 1
    state["watcher"] = state.get("watcher")
    state["tasks"] = tasks
    write_queue_state(repo, state)


def start_server(ctx: ScenarioContext, repo: Path) -> None:
    ctx.repo = repo
    log_path = ctx.artifacts_dir / "server.log"
    log = log_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(ctx.otto_root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    ctx.server = subprocess.Popen(
        [sys.executable, "-m", "otto.cli", "web", "--port", str(ctx.port), "--no-open"],
        cwd=repo,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        start_new_session=True,
    )
    wait_for_server(ctx)


def stop_server(ctx: ScenarioContext) -> None:
    if ctx.server is None:
        return
    if ctx.server.poll() is None:
        ctx.server.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            ctx.server.wait(timeout=5)
    if ctx.server.poll() is None:
        ctx.server.kill()
        ctx.server.wait(timeout=5)
    ctx.server = None


def open_app(ctx: ScenarioContext) -> None:
    browser("open", ctx.url)
    browser("set", "viewport", str(ctx.viewport_width), str(ctx.viewport_height))
    wait_text("Otto")


def parse_viewport(value: str) -> tuple[int, int]:
    parts = value.lower().split("x", 1)
    if len(parts) != 2:
        raise SystemExit("--viewport must use WIDTHxHEIGHT, for example 1440x1000")
    try:
        width = int(parts[0])
        height = int(parts[1])
    except ValueError as exc:
        raise SystemExit("--viewport must use integer WIDTHxHEIGHT") from exc
    if width < 320 or height < 480:
        raise SystemExit("--viewport is too small for Mission Control E2E")
    return width, height


def wait_for_server(ctx: ScenarioContext, timeout_s: float = 20) -> None:
    deadline = time.monotonic() + timeout_s
    last_error = ""
    while time.monotonic() < deadline:
        if ctx.server is not None and ctx.server.poll() is not None:
            raise RuntimeError(f"web server exited early with {ctx.server.returncode}")
        try:
            with urllib.request.urlopen(ctx.url + "api/state", timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"web server did not become ready: {last_error}")


def wait_for_api_state(ctx: ScenarioContext, predicate: Callable[[dict[str, object]], bool], label: str, timeout_s: float = 15) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        state = api_json(ctx, "api/state")
        if predicate(state):
            return
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {label}")


def api_json(ctx: ScenarioContext, path: str) -> dict[str, object]:
    with urllib.request.urlopen(ctx.url + path.lstrip("/"), timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def api_post(ctx: ScenarioContext, path: str, payload: dict[str, object]) -> dict[str, object]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        ctx.url + path.lstrip("/"),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def landing_item(state: dict[str, object], task_id: str) -> dict[str, object]:
    landing = state["landing"]
    assert isinstance(landing, dict)
    items = landing["items"]
    assert isinstance(items, list)
    for item in items:
        if isinstance(item, dict) and item.get("task_id") == task_id:
            return item
    raise AssertionError(f"landing item {task_id!r} not found")


def browser(*args: str, check: bool = True, timeout_s: float = 30) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["agent-browser", *args],
        text=True,
        capture_output=True,
        timeout=timeout_s,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            "agent-browser failed: "
            + " ".join(args)
            + f"\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def browser_eval(script: str) -> str:
    return browser("eval", script, timeout_s=10).stdout.strip()


@contextlib.contextmanager
def browser_session_lock(owner: str, timeout_s: float = 600, stale_after_s: float = 3600):
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            BROWSER_LOCK_DIR.mkdir()
            (BROWSER_LOCK_DIR / "owner").write_text(
                json.dumps({"owner": owner, "pid": os.getpid(), "created_at": time.time()}) + "\n",
                encoding="utf-8",
            )
            break
        except FileExistsError:
            clear_stale_browser_lock(stale_after_s)
            if time.monotonic() > deadline:
                raise TimeoutError(f"timed out waiting for browser lock {BROWSER_LOCK_DIR}")
            time.sleep(0.25)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            (BROWSER_LOCK_DIR / "owner").unlink()
        with contextlib.suppress(OSError):
            BROWSER_LOCK_DIR.rmdir()


def clear_stale_browser_lock(stale_after_s: float) -> None:
    owner_path = BROWSER_LOCK_DIR / "owner"
    owner = read_browser_lock_owner(owner_path)
    pid = owner.get("pid")
    created_at = owner.get("created_at")
    try:
        age = time.time() - float(created_at) if created_at is not None else time.time() - owner_path.stat().st_mtime
    except (OSError, TypeError, ValueError):
        age = stale_after_s + 1
    if isinstance(pid, int) and pid_alive(pid) and age <= stale_after_s:
        return
    if isinstance(pid, int) and pid_alive(pid) and age <= stale_after_s * 4:
        return
    shutil.rmtree(BROWSER_LOCK_DIR, ignore_errors=True)


def read_browser_lock_owner(owner_path: Path) -> dict[str, object]:
    try:
        raw = owner_path.read_text(encoding="utf-8").strip()
    except OSError:
        return {}
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        lines = raw.splitlines()
        pid = int(lines[1]) if len(lines) > 1 and lines[1].isdigit() else None
        return {"owner": lines[0] if lines else "", "pid": pid}
    return value if isinstance(value, dict) else {}


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_text(text: str, timeout_s: float = 20) -> None:
    browser("wait", "--text", text, timeout_s=timeout_s)


def assert_page_lacks(text: str) -> None:
    snapshot = browser("snapshot", timeout_s=10).stdout
    if text in snapshot:
        raise AssertionError(f"unexpected page text {text!r}\n{snapshot}")


def assert_modal_focus() -> None:
    result = browser_eval(
        """(() => {
          const dialog = document.querySelector('[role="dialog"][aria-modal="true"]');
          const active = document.activeElement;
          const mainHidden = document.querySelector('main')?.getAttribute('aria-hidden') === 'true';
          const sideHidden = document.querySelector('aside.sidebar')?.getAttribute('aria-hidden') === 'true';
          return Boolean(dialog && active && dialog.contains(active) && mainHidden && sideHidden);
        })()"""
    )
    if not result.endswith("true"):
        raise AssertionError(f"modal focus/background isolation failed: {result}")


def screenshot(ctx: ScenarioContext, name: str) -> None:
    path = ctx.artifacts_dir / name
    browser("screenshot", str(path), timeout_s=20)
    snapshot = browser("snapshot", timeout_s=20).stdout
    path.with_suffix(f"{path.suffix}.snapshot.txt").write_text(snapshot, encoding="utf-8")


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr or result.stdout}")
    return result.stdout.strip()


def assert_git_file(repo: Path, ref: str, path: str, expected: str) -> None:
    content = git(repo, "show", f"{ref}:{path}")
    if expected not in content:
        raise AssertionError(f"{ref}:{path} did not contain {expected!r}: {content!r}")


def head_sha(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def iso_now() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


if __name__ == "__main__":
    raise SystemExit(main())
