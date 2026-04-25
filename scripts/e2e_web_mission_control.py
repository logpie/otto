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
from otto.queue.schema import QueueTask, append_task, load_queue, write_state as write_queue_state
from otto.runs.registry import make_run_record, write_record


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


COVERAGE_MODEL: dict[str, list[dict[str, str]]] = {
    "states": [
        {"id": "project.launcher", "scenario": "project-launcher", "intent": "managed project creation and target selection"},
        {"id": "project.clean.empty", "scenario": "fresh-queue", "intent": "clean project with no work"},
        {"id": "queue.queued", "scenario": "fresh-queue", "intent": "queued task waiting for watcher"},
        {"id": "queue.command_backlog", "scenario": "command-backlog", "intent": "pending command backlog"},
        {"id": "watcher.running", "scenario": "watcher-stop-ui", "intent": "running watcher with visible stop affordance"},
        {"id": "task.ready", "scenario": "ready-land", "intent": "single task ready to land"},
        {"id": "task.bulk_ready", "scenario": "bulk-land", "intent": "multiple ready tasks"},
        {"id": "task.failed", "scenario": "multi-state", "intent": "failed task needs recovery"},
        {"id": "task.landed", "scenario": "multi-state", "intent": "landed task review packet"},
        {"id": "repo.dirty_blocked", "scenario": "dirty-blocked", "intent": "dirty working tree blocks landing"},
        {"id": "evidence.large_log", "scenario": "long-log-layout", "intent": "large log and artifact review"},
        {"id": "filters.no_match", "scenario": "control-tour", "intent": "filtered board can hide all task cards"},
    ],
    "actions": [
        {"id": "project.create", "scenario": "project-launcher", "intent": "create managed project"},
        {"id": "queue.build.submit", "scenario": "fresh-queue", "intent": "submit build job"},
        {"id": "queue.improve.submit", "scenario": "job-submit-matrix", "intent": "submit improve job with advanced options"},
        {"id": "queue.certify.submit", "scenario": "job-submit-matrix", "intent": "submit certify job with advanced options"},
        {"id": "watcher.start", "scenario": "command-backlog", "intent": "start watcher from UI"},
        {"id": "watcher.stop.cancel", "scenario": "watcher-stop-ui", "intent": "cancel stop confirmation"},
        {"id": "watcher.stop.confirm", "scenario": "watcher-stop-ui", "intent": "confirm stop watcher"},
        {"id": "run.land.selected", "scenario": "ready-land", "intent": "land one selected task"},
        {"id": "run.land.bulk.cancel", "scenario": "control-tour", "intent": "cancel bulk land confirmation"},
        {"id": "run.land.bulk.confirm", "scenario": "bulk-land", "intent": "land all ready tasks"},
        {"id": "run.cleanup.cancel", "scenario": "control-tour", "intent": "open and cancel advanced cleanup"},
        {"id": "inspector.diff", "scenario": "ready-land", "intent": "open changed-file code diff before landing"},
        {"id": "inspector.proof", "scenario": "long-log-layout", "intent": "open proof content"},
        {"id": "inspector.logs", "scenario": "long-log-layout", "intent": "open bounded logs"},
        {"id": "inspector.artifact.drilldown", "scenario": "long-log-layout", "intent": "open artifact content and return"},
        {"id": "diagnostics.open", "scenario": "multi-state", "intent": "open diagnostics view"},
        {"id": "filters.search", "scenario": "control-tour", "intent": "search and clear task filters"},
        {"id": "responsive.mobile", "scenario": "control-tour", "intent": "scenario is viewport-safe for mobile/tablet runs"},
    ],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--otto-root", type=Path, default=Path.cwd(), help="Otto source tree to test.")
    parser.add_argument(
        "--scenario",
        choices=[
            "all",
            "project-launcher",
            "fresh-queue",
            "ready-land",
            "dirty-blocked",
            "multi-state",
            "command-backlog",
            "watcher-stop-ui",
            "job-submit-matrix",
            "bulk-land",
            "long-log-layout",
            "control-tour",
        ],
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
    prepare_artifacts_dir(artifacts_dir)
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
            if ctx.artifacts_dir.exists():
                shutil.rmtree(ctx.artifacts_dir)
            ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
            print(f"[web-e2e] {scenario.name}: {scenario.description}")
            try:
                with browser_session_lock(scenario.name):
                    try:
                        try:
                            scenario.run(ctx)
                        except Exception:
                            capture_failure_evidence(ctx, scenario.name)
                            raise
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
        write_coverage_report(artifacts_dir, [item["scenario"] for item in results])
    finally:
        if args.keep:
            print(f"[web-e2e] kept projects under {run_root}")
        else:
            shutil.rmtree(run_root, ignore_errors=True)
    return 0


def scenarios() -> list[Scenario]:
    return [
        Scenario("project-launcher", "create a managed project before queueing work", scenario_project_launcher),
        Scenario("fresh-queue", "queue a first build from the web UI before the watcher starts", scenario_fresh_queue),
        Scenario("ready-land", "review and land a clean completed task", scenario_ready_land),
        Scenario("dirty-blocked", "show a clean recovery path when local changes block landing", scenario_dirty_blocked),
        Scenario("multi-state", "audit queued, failed, ready, and landed work in one board", scenario_multi_state),
        Scenario("command-backlog", "recover pending command backlog when the watcher is stopped", scenario_command_backlog),
        Scenario("watcher-stop-ui", "cancel and confirm watcher stop from the visible UI", scenario_watcher_stop_ui),
        Scenario("job-submit-matrix", "submit improve and certify jobs with advanced queue options", scenario_job_submit_matrix),
        Scenario("bulk-land", "land multiple ready tasks through the bulk action", scenario_bulk_land),
        Scenario("long-log-layout", "keep large logs in a bounded full-width inspector", scenario_long_log_layout),
        Scenario("control-tour", "click through the main controls, dialogs, inspectors, and tabs", scenario_control_tour),
    ]


def prepare_artifacts_dir(artifacts_dir: Path) -> None:
    for child in artifacts_dir.iterdir():
        if child.is_dir() and len(child.name) > 3 and child.name[:2].isdigit() and child.name[2] == "-":
            shutil.rmtree(child)
    for filename in ("summary.json", "coverage-model.json"):
        with contextlib.suppress(FileNotFoundError):
            (artifacts_dir / filename).unlink()


def write_coverage_report(artifacts_dir: Path, selected_scenarios: list[str]) -> None:
    scenario_names = {scenario.name for scenario in scenarios()}
    selected = set(selected_scenarios)
    model_errors: list[str] = []
    covered: dict[str, list[dict[str, str]]] = {}
    missing: dict[str, list[dict[str, str]]] = {}
    for group, entries in COVERAGE_MODEL.items():
        seen_ids: set[str] = set()
        covered[group] = []
        missing[group] = []
        for entry in entries:
            entry_id = entry["id"]
            owner = entry["scenario"]
            if entry_id in seen_ids:
                model_errors.append(f"duplicate {group} coverage id {entry_id}")
            seen_ids.add(entry_id)
            if owner not in scenario_names:
                model_errors.append(f"{group} coverage id {entry_id} references unknown scenario {owner}")
            if owner in selected:
                covered[group].append(entry)
            else:
                missing[group].append(entry)
    report = {
        "schema_version": 1,
        "selected_scenarios": selected_scenarios,
        "model": COVERAGE_MODEL,
        "covered": covered,
        "missing": missing,
        "model_errors": model_errors,
    }
    (artifacts_dir / "coverage-model.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if set(selected_scenarios) == scenario_names:
        missing_items = [entry for entries in missing.values() for entry in entries]
        if model_errors or missing_items:
            raise AssertionError(f"coverage model incomplete: errors={model_errors}, missing={missing_items}")


def scenario_project_launcher(ctx: ScenarioContext) -> None:
    host = init_repo(ctx.run_root / "host")
    projects_root = ctx.run_root / "managed-projects"
    start_server(ctx, host, extra_args=["--project-launcher", "--projects-root", str(projects_root)])
    open_app(ctx)

    wait_text("Project Launcher")
    wait_text("Create project")
    assert_page_contains(str(projects_root))
    assert_no_passive_refresh_status()
    browser("find", "label", "Project name", "fill", "Expense Approval Portal")
    browser("find", "role", "button", "click", "--name", "Create project")
    wait_text("Task Board")
    managed = projects_root / "expense-approval-portal"
    assert (managed / ".git").exists()
    assert (managed / "otto.yaml").exists()
    browser("find", "testid", "new-job-button", "click")
    wait_text("New queue job")
    assert_page_contains(str(managed))
    browser("find", "role", "button", "click", "--name", "Close")
    assert_page_lacks(str(host))
    browser("find", "testid", "switch-project-button", "click")
    wait_text("Project Launcher")
    wait_text("Open project")
    wait_text("expense-approval-portal")
    browser("find", "role", "button", "click", "--name", "expense-approval-portal")
    wait_text("Task Board")
    assert_page_contains("expense-approval-portal")
    screenshot(ctx, "project-launcher.png")


def scenario_fresh_queue(ctx: ScenarioContext) -> None:
    repo = init_repo(ctx.run_root / "fresh")
    start_server(ctx, repo)
    open_app(ctx)

    browser("find", "testid", "new-job-button", "click")
    wait_text("New queue job")
    assert_page_contains(str(repo))
    assert_modal_focus()
    browser("find", "label", "Intent / focus", "fill", "Build an expense approval portal for a small company.")
    browser("find", "text", "Advanced options", "click")
    browser("find", "label", "Task id", "fill", "expense-portal")
    browser("find", "role", "button", "click", "--name", "Queue job")
    wait_text("queued expense-portal")
    wait_text("Task Board")
    wait_text("1 queued task waiting")
    wait_text("Waiting for watcher")
    assert_no_passive_refresh_status()
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
    wait_text("Review evidence and land the task.")
    browser("find", "testid", "open-proof-button", "click")
    wait_text("Stories tested")
    wait_text("saved-views-create")
    wait_text("Open HTML proof report")
    assert_proof_report_link()
    browser("find", "testid", "proof-open-diff-button", "click")
    wait_text("Code diff")
    wait_snapshot_contains("saved_views.txt")
    wait_snapshot_contains("+saved-views")
    assert_diff_file_browser()
    browser("find", "testid", "close-inspector-button", "click")
    assert_inspector_closed()
    browser("find", "testid", "review-next-action-button", "click")
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
    assert packet["changes"]["diff_command"]
    assert packet["changes"]["files"] == ["saved_views.txt"]
    browser("find", "testid", "open-diff-button", "click")
    wait_text("Code diff")
    wait_snapshot_contains("saved_views.txt")
    wait_snapshot_contains("+saved-views")
    browser("find", "testid", "close-inspector-button", "click")
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
    assert "Fatal Python error" in failed_packet["failure"]["reason"]
    browser("find", "testid", "open-logs-button", "click")
    wait_text("Primary session log was not created")
    wait_text("Bad file descriptor")
    browser("find", "testid", "close-inspector-button", "click")
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


def scenario_watcher_stop_ui(ctx: ScenarioContext) -> None:
    repo = init_repo(ctx.run_root / "watcher-stop-ui")
    paths.queue_commands_path(repo).write_text(
        json.dumps({"command_id": "cmd-stop-ui", "run_id": "run-missing", "action": "retry"}) + "\n",
        encoding="utf-8",
    )
    start_server(ctx, repo)
    open_app(ctx)

    wait_text("Commands are waiting")
    browser("find", "testid", "start-watcher-button", "click")
    wait_for_api_state(
        ctx,
        lambda state: bool(state["runtime"]["supervisor"]["can_stop"]) or state["watcher"]["health"]["state"] == "running",
        "watcher can stop",
        timeout_s=20,
    )
    browser("find", "testid", "stop-watcher-button", "click")
    wait_text("Stop watcher")
    assert_modal_focus()
    browser("find", "role", "button", "click", "--name", "Cancel")
    assert_no_dialog()
    wait_for_api_state(ctx, lambda state: state["watcher"]["health"]["state"] == "running", "watcher remains running after cancel", timeout_s=10)

    browser("find", "testid", "stop-watcher-button", "click")
    wait_text("Stop watcher")
    browser("find", "role", "button", "click", "--name", "Stop watcher")
    wait_for_api_state(ctx, lambda state: state["watcher"]["health"]["state"] != "running", "watcher stopped from UI", timeout_s=20)
    browser("reload")
    wait_text("watcher stop requested")
    screenshot(ctx, "watcher-stop-ui.png")


def scenario_job_submit_matrix(ctx: ScenarioContext) -> None:
    repo = init_repo(ctx.run_root / "job-submit-matrix")
    seed_queued_task(repo, "base-task")
    start_server(ctx, repo)
    open_app(ctx)

    queue_job_from_dialog(
        command="improve",
        task_id="improve-saved-views",
        intent="Add saved dashboard views with named filters.",
        after="base-task",
        subcommand="feature",
        provider="codex",
        model="gpt-5.4",
        effort="high",
    )
    wait_text("queued improve-saved-views")

    queue_job_from_dialog(
        command="certify",
        task_id="certify-checkout",
        intent="Certify the checkout workflow against the product spec.",
        provider="claude",
        effort="medium",
        certification="standard",
    )
    wait_text("queued certify-checkout")

    queue_job_from_dialog(
        command="build",
        task_id="build-without-cert",
        intent="Add an import preview screen.",
        certification="skip",
    )
    wait_text("queued build-without-cert")

    tasks = {task.id: task for task in load_queue(repo)}
    improve = tasks["improve-saved-views"]
    certify = tasks["certify-checkout"]
    build = tasks["build-without-cert"]
    assert improve.command_argv[:3] == ["improve", "feature", "Add saved dashboard views with named filters."], improve
    assert improve.after == ["base-task"], improve
    assert improve.focus == "Add saved dashboard views with named filters.", improve
    assert improve.resumable is True
    assert_cli_args(improve.command_argv, {"--provider": "codex", "--model": "gpt-5.4", "--effort": "high"})
    assert certify.command_argv[:2] == ["certify", "Certify the checkout workflow against the product spec."], certify
    assert certify.resumable is False
    assert_cli_args(certify.command_argv, {"--provider": "claude", "--effort": "medium"}, flags=["--standard"])
    assert build.command_argv == ["build", "Add an import preview screen.", "--no-qa"], build
    state = api_json(ctx, "api/state")
    assert state["watcher"]["counts"]["queued"] == 4
    screenshot(ctx, "job-submit-matrix.png")


def scenario_bulk_land(ctx: ScenarioContext) -> None:
    repo = init_repo(ctx.run_root / "bulk-land")
    seed_ready_task(repo, task_id="saved-views", filename="saved_views.txt")
    seed_ready_task(repo, task_id="audit-log", filename="audit_log.txt")
    start_server(ctx, repo)
    open_app(ctx)

    wait_text("2 tasks ready to land")
    browser("find", "role", "button", "click", "--name", "Land all ready")
    wait_text("Land ready tasks")
    wait_text("saved-views")
    wait_text("audit-log")
    assert_modal_focus()
    browser("find", "role", "button", "click", "--name", "Land 2 tasks")
    wait_for_api_state(ctx, lambda state: state["landing"]["counts"]["merged"] >= 2, "bulk tasks landed", timeout_s=30)
    assert_git_file(repo, "main", "saved_views.txt", "saved-views")
    assert_git_file(repo, "main", "audit_log.txt", "audit-log")
    state = api_json(ctx, "api/state")
    by_task = {item["task_id"]: item for item in state["landing"]["items"]}
    assert by_task["saved-views"]["landing_state"] == "merged"
    assert by_task["audit-log"]["landing_state"] == "merged"
    browser("reload")
    wait_text("Landed")
    screenshot(ctx, "bulk-land.png")


def scenario_control_tour(ctx: ScenarioContext) -> None:
    repo = init_repo(ctx.run_root / "control-tour")
    seed_ready_task(repo, task_id="ready-dashboard", filename="ready_dashboard.txt")
    start_server(ctx, repo)
    open_app(ctx)

    wait_text("1 task ready to land")
    browser("find", "role", "button", "click", "--name", "Refresh")
    assert_no_horizontal_overflow()

    browser("find", "label", "Search", "fill", "ready-dashboard")
    wait_text("ready-dashboard")
    assert_task_card_count(1)
    browser("find", "label", "Search", "fill", "no-such-task")
    assert_task_card_count(0)
    browser("find", "role", "button", "click", "--name", "Clear filters")
    wait_text("ready-dashboard")

    browser("select", "[data-testid='filter-type-select']", "build")
    browser("select", "[data-testid='filter-outcome-select']", "success")
    browser("find", "role", "button", "click", "--name", "Clear filters")
    browser("find", "label", "Active", "click")
    browser("find", "role", "button", "click", "--name", "Clear filters")

    browser("find", "testid", "new-job-button", "click")
    wait_text("New queue job")
    assert_modal_focus()
    assert_submit_disabled("Queue job")
    browser("select", "[data-testid='job-command-select']", "improve")
    wait_text("Improve mode")
    browser("select", "[data-testid='job-improve-mode-select']", "feature")
    browser("find", "label", "Intent / focus", "fill", "Add saved dashboard views with named filters.")
    browser("find", "text", "Advanced options", "click")
    browser("find", "label", "Task id", "fill", "saved-dashboard-views")
    browser("find", "label", "After", "fill", "ready-dashboard")
    browser("select", "[data-testid='job-provider-select']", "codex")
    browser("select", "[data-testid='job-effort-select']", "high")
    browser("find", "label", "Model", "fill", "gpt-5.4")
    wait_text("Evaluation policy")
    assert_submit_enabled("Queue job")
    browser("find", "role", "button", "click", "--name", "Close")
    assert_page_lacks("New queue job")

    browser("find", "role", "button", "click", "--name", "Land all ready")
    wait_text("Land ready tasks")
    assert_modal_focus()
    browser("find", "role", "button", "click", "--name", "Cancel")
    assert_no_dialog()

    browser("find", "testid", "task-card-ready-dashboard", "click")
    wait_text("Ready for review")
    browser("find", "testid", "open-proof-button", "click")
    wait_text("Proof of work")
    assert_no_horizontal_overflow()
    if ctx.viewport_width > 980:
        browser("find", "testid", "new-job-button", "click")
        wait_text("New queue job")
        assert_modal_focus()
        browser("find", "role", "button", "click", "--name", "Close")
        assert_inspector_closed()
        browser("find", "testid", "open-proof-button", "click")
        wait_text("Proof of work")
    browser("find", "role", "tab", "click", "--name", "Artifacts")
    wait_snapshot_contains("summary file")
    browser("find", "role", "tab", "click", "--name", "Logs")
    wait_snapshot_contains("Run logs")
    browser("find", "role", "tab", "click", "--name", "Proof")
    wait_snapshot_contains("Certification checks")
    browser("find", "testid", "close-inspector-button", "click")
    assert_inspector_closed()

    browser("find", "testid", "review-next-action-button", "click")
    wait_text("Land task")
    assert_modal_focus()
    browser("find", "role", "button", "click", "--name", "Cancel")
    assert_no_dialog()

    browser("find", "text", "Advanced run actions", "click")
    wait_text("Clean run record")
    browser("find", "role", "button", "click", "--name", "Clean run record")
    wait_text("Clean run record")
    assert_modal_focus()
    browser("find", "role", "button", "click", "--name", "Cancel")
    assert_no_dialog()

    browser("find", "testid", "diagnostics-tab", "click")
    wait_text("Diagnostics Summary")
    wait_text("Live Runs")
    assert_url_contains("view=diagnostics")
    browser("reload")
    wait_text("Diagnostics Summary")
    assert_url_contains("view=diagnostics")
    browser("find", "testid", "tasks-tab", "click")
    wait_text("Task Board")
    assert_url_contains("view=tasks")
    browser("find", "testid", "diagnostics-tab", "click")
    wait_text("Diagnostics Summary")
    browser("back")
    wait_text("Task Board")
    assert_url_contains("view=tasks")
    assert_no_horizontal_overflow()
    screenshot(ctx, "control-tour.png")


def scenario_long_log_layout(ctx: ScenarioContext) -> None:
    repo = init_repo(ctx.run_root / "long-log")
    seed_long_log_run(repo)
    start_server(ctx, repo)
    open_app(ctx)

    wait_text("run-long-log")
    assert_inspector_closed()
    browser("find", "testid", "open-proof-button", "click")
    wait_text("Proof of work")
    assert_page_contains("Evidence artifacts")
    assert_page_contains("Evidence content")
    assert_page_contains("stories_tested")
    browser("find", "testid", "close-inspector-button", "click")
    assert_inspector_closed()
    browser("find", "testid", "review-more-artifacts-button", "click")
    wait_text("messages")
    assert_artifact_list_layout()
    browser("find", "role", "button", "click", "--name", "primary log log")
    wait_text("complete lines")
    assert_page_contains("Long log fixture output line")
    assert_artifact_log_theme()
    browser("find", "role", "button", "click", "--name", "Back to artifacts")
    wait_text("messages")
    browser("find", "testid", "close-inspector-button", "click")
    assert_inspector_closed()
    browser("find", "testid", "open-logs-button", "click")
    wait_text("Run logs")
    assert_long_log_layout()
    screenshot(ctx, "long-log-layout.png")
    scroll_to_run_inspector()
    screenshot(ctx, "long-log-inspector.png")
    browser("find", "testid", "close-inspector-button", "click")
    assert_inspector_closed()


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
    watcher_log = repo / "otto_logs" / "web" / "watcher.log"
    watcher_log.parent.mkdir(parents=True, exist_ok=True)
    watcher_log.write_text(
        "\n".join(
            [
                f"[{task_id}] Fatal Python error: init_sys_streams: can't initialize sys standard streams",
                f"[{task_id}] OSError: [Errno 9] Bad file descriptor",
                f"[02:00:22] reaped {task_id}: failed (exit_code=1)",
            ]
        )
        + "\n",
        encoding="utf-8",
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
    seed_proof_report(repo, task_id=task_id, run_id=f"run-{task_id}")


def seed_proof_report(repo: Path, *, task_id: str, run_id: str) -> None:
    worktree = repo / ".worktrees" / task_id
    certify_dir = paths.certify_dir(worktree, run_id)
    certify_dir.mkdir(parents=True, exist_ok=True)
    summary_path = paths.session_summary(worktree, run_id)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "done",
                "stories_tested": 2,
                "stories_passed": 2,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (certify_dir / "proof-of-work.html").write_text(
        f"<html><body><h1>Proof of work</h1><p>{task_id} report</p></body></html>",
        encoding="utf-8",
    )
    (certify_dir / "proof-of-work.json").write_text(
        json.dumps(
            {
                "stories_tested": 2,
                "stories_passed": 2,
                "stories": [
                    {
                        "story_id": f"{task_id}-create",
                        "status": "pass",
                        "claim": f"{task_id} can be created by the user.",
                        "observed_result": "The primary workflow completed with live UI events.",
                        "methodology": "live-ui-events",
                    },
                    {
                        "story_id": f"{task_id}-restore",
                        "status": "pass",
                        "claim": f"{task_id} can be reopened and verified.",
                        "observed_result": "The saved state remained visible after reload.",
                        "methodology": "live-ui-events",
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
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


def seed_long_log_run(repo: Path) -> None:
    run_id = "run-long-log"
    branch = "build/long-log-fixture"
    git(repo, "checkout", "-q", "-b", branch)
    (repo / "long_log_fixture.txt").write_text("long log fixture\n", encoding="utf-8")
    git(repo, "add", "long_log_fixture.txt")
    git(repo, "commit", "-q", "-m", "add long log fixture")
    git(repo, "checkout", "-q", "main")
    session_dir = paths.session_dir(repo, run_id)
    log_path = paths.build_dir(repo, run_id) / "narrative.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"{index:04d} [build] Long log fixture output line with enough content to exercise horizontal and vertical scanning."
        for index in range(900)
    ]
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    messages_path = log_path.with_name("messages.jsonl")
    messages_path.write_text(json.dumps({"event": "long-log", "run_id": run_id}) + "\n", encoding="utf-8")
    manifest_path = session_dir / "manifest.json"
    summary_path = session_dir / "summary.json"
    checkpoint_path = session_dir / "checkpoint.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"run_id": run_id, "command": "build", "fixture": "long-log"}, indent=2), encoding="utf-8")
    summary_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "done",
                "intent": "Exercise Mission Control with a large streaming log.",
                "stories_tested": 3,
                "stories_passed": 3,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    checkpoint_path.write_text(json.dumps({"resumable": True, "last_step": "streaming"}, indent=2), encoding="utf-8")
    intent_path = session_dir / "intent.txt"
    intent_path.write_text("Exercise Mission Control with a large streaming log.\n", encoding="utf-8")
    record = make_run_record(
        project_dir=repo,
        run_id=run_id,
        domain="atomic",
        run_type="build",
        command="build long-log-fixture",
        display_name="Long log fixture",
        status="done",
        cwd=repo,
        git={"branch": branch},
        intent={"summary": "Exercise Mission Control with a large streaming log.", "intent_path": str(intent_path)},
        artifacts={
            "manifest_path": str(manifest_path),
            "summary_path": str(summary_path),
            "checkpoint_path": str(checkpoint_path),
            "primary_log_path": str(log_path),
        },
        adapter_key="atomic.build",
        last_event="streaming long log fixture output",
    )
    write_record(repo, record)


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


def start_server(ctx: ScenarioContext, repo: Path, extra_args: list[str] | None = None) -> None:
    ctx.repo = repo
    log_path = ctx.artifacts_dir / "server.log"
    log = log_path.open("w", encoding="utf-8")
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONPATH"] = str(ctx.otto_root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    ctx.server = subprocess.Popen(
        [sys.executable, "-m", "otto.cli", "web", "--port", str(ctx.port), "--no-open", *(extra_args or [])],
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
            with urllib.request.urlopen(ctx.url + "api/projects", timeout=0.5) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"web server did not become ready: {last_error}")


def wait_for_api_state(ctx: ScenarioContext, predicate: Callable[[dict[str, object]], bool], label: str, timeout_s: float = 15) -> None:
    deadline = time.monotonic() + timeout_s
    last_state: dict[str, object] | None = None
    while time.monotonic() < deadline:
        state = api_json(ctx, "api/state")
        last_state = state
        if predicate(state):
            return
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {label}: {state_debug_summary(last_state)}")


def capture_failure_evidence(ctx: ScenarioContext, scenario_name: str) -> None:
    with contextlib.suppress(Exception):
        state = api_json(ctx, "api/state")
        (ctx.artifacts_dir / "failure-state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
    with contextlib.suppress(Exception):
        screenshot(ctx, f"{scenario_name}-failure.png")


def state_debug_summary(state: dict[str, object] | None) -> str:
    if not state:
        return "no state captured"
    landing = state.get("landing")
    live = state.get("live")
    runtime = state.get("runtime")
    summary = {
        "landing": landing.get("counts") if isinstance(landing, dict) else None,
        "live": live.get("counts") if isinstance(live, dict) else None,
        "runtime": runtime.get("summary") if isinstance(runtime, dict) else None,
    }
    if isinstance(landing, dict):
        summary["landing_items"] = [
            {
                "task_id": item.get("task_id"),
                "state": item.get("landing_state"),
                "blocked": item.get("landing_blocked"),
                "reason": item.get("landing_blocked_reason"),
            }
            for item in landing.get("items", [])
            if isinstance(item, dict)
        ]
    return json.dumps(summary, sort_keys=True)


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


def queue_job_from_dialog(
    *,
    command: str,
    task_id: str,
    intent: str,
    after: str = "",
    subcommand: str = "bugs",
    provider: str = "",
    model: str = "",
    effort: str = "",
    certification: str = "",
) -> None:
    browser("find", "testid", "new-job-button", "click")
    wait_text("New queue job")
    assert_modal_focus()
    browser("select", "[data-testid='job-command-select']", command)
    if command == "improve":
        wait_text("Improve mode")
        browser("select", "[data-testid='job-improve-mode-select']", subcommand)
    browser("find", "label", "Intent / focus", "fill", intent)
    if task_id or after or provider or effort or model or certification:
        browser("find", "text", "Advanced options", "click")
        if task_id:
            browser("find", "label", "Task id", "fill", task_id)
        if after:
            browser("find", "label", "After", "fill", after)
        if provider:
            browser("select", "[data-testid='job-provider-select']", provider)
        if effort:
            browser("select", "[data-testid='job-effort-select']", effort)
        if model:
            browser("find", "label", "Model", "fill", model)
        if certification:
            browser("select", "[data-testid='job-certification-select']", certification)
    assert_submit_enabled("Queue job")
    browser("find", "role", "button", "click", "--name", "Queue job")


def assert_cli_args(argv: list[str], values: dict[str, str], *, flags: list[str] | None = None) -> None:
    for flag, expected in values.items():
        if flag not in argv:
            raise AssertionError(f"missing {flag!r} in {argv!r}")
        index = argv.index(flag)
        actual = argv[index + 1] if index + 1 < len(argv) else None
        if actual != expected:
            raise AssertionError(f"expected {flag} {expected!r}, got {actual!r} in {argv!r}")
    for flag in flags or []:
        if flag not in argv:
            raise AssertionError(f"missing flag {flag!r} in {argv!r}")


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


def assert_page_contains(text: str) -> None:
    snapshot = browser("snapshot", timeout_s=10).stdout
    if text not in snapshot:
        raise AssertionError(f"expected page text {text!r}\n{snapshot}")


def assert_url_contains(text: str) -> None:
    url = browser("get", "url", timeout_s=10).stdout.strip()
    if text not in url:
        raise AssertionError(f"expected URL to contain {text!r}, got {url!r}")


def wait_snapshot_contains(text: str, timeout_s: float = 20) -> None:
    deadline = time.monotonic() + timeout_s
    last_snapshot = ""
    while time.monotonic() < deadline:
        last_snapshot = browser("snapshot", timeout_s=10).stdout
        if text in last_snapshot:
            return
        time.sleep(0.4)
    raise AssertionError(f"expected page text {text!r}\n{last_snapshot}")


def assert_no_passive_refresh_status() -> None:
    time.sleep(2)
    snapshot = browser("snapshot", timeout_s=10).stdout
    forbidden = [text for text in ("idle", "refreshing") if text in snapshot]
    if forbidden:
        raise AssertionError(f"unexpected passive refresh status {forbidden}\n{snapshot}")


def assert_no_horizontal_overflow() -> None:
    raw = browser_eval(
        """JSON.stringify({
          viewportWidth: window.innerWidth,
          scrollWidth: document.documentElement.scrollWidth,
          bodyScrollWidth: document.body?.scrollWidth || 0
        })"""
    )
    metrics = json.loads(raw)
    if isinstance(metrics, str):
        metrics = json.loads(metrics)
    max_scroll = max(int(metrics["scrollWidth"]), int(metrics["bodyScrollWidth"]))
    if max_scroll > int(metrics["viewportWidth"]) + 1:
        raise AssertionError(f"page has horizontal overflow: {metrics}")


def assert_task_card_count(expected: int) -> None:
    raw = browser_eval(
        """JSON.stringify({
          count: document.querySelectorAll('.task-card-main').length,
          titles: [...document.querySelectorAll('.task-card-main .task-title')].map((item) => item.textContent || '')
        })"""
    )
    metrics = json.loads(raw)
    if isinstance(metrics, str):
        metrics = json.loads(metrics)
    if int(metrics["count"]) != expected:
        raise AssertionError(f"expected {expected} task card(s): {metrics}")


def assert_submit_disabled(label: str) -> None:
    result = browser_eval(
        f"""(() => {{
          const button = [...document.querySelectorAll('button')].find((item) => item.textContent?.trim() === {json.dumps(label)});
          return button instanceof HTMLButtonElement && button.disabled;
        }})()"""
    )
    if not result.endswith("true"):
        raise AssertionError(f"expected {label!r} submit button to be disabled: {result}")


def assert_submit_enabled(label: str) -> None:
    result = browser_eval(
        f"""(() => {{
          const button = [...document.querySelectorAll('button')].find((item) => item.textContent?.trim() === {json.dumps(label)});
          return button instanceof HTMLButtonElement && !button.disabled;
        }})()"""
    )
    if not result.endswith("true"):
        raise AssertionError(f"expected {label!r} submit button to be enabled: {result}")


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


def assert_no_dialog() -> None:
    result = browser_eval("""(() => !document.querySelector('[role="dialog"][aria-modal="true"]'))()""")
    if not result.endswith("true"):
        raise AssertionError(f"expected modal dialog to be closed: {result}")


def assert_proof_report_link() -> None:
    raw = browser_eval(
        """JSON.stringify((() => {
          const link = document.querySelector('[data-testid="proof-report-link"]');
          return {
            exists: Boolean(link),
            href: link?.getAttribute('href') || '',
            label: link?.textContent || ''
          };
        })())"""
    )
    data = parse_browser_json(raw)
    if not data["exists"] or not data["href"].endswith("/proof-report"):
        raise AssertionError(f"proof report link missing or wrong: {data}")


def assert_diff_file_browser() -> None:
    raw = browser_eval(
        """JSON.stringify((() => {
          const fileList = document.querySelector('[data-testid="diff-file-list"]');
          const selected = document.querySelector('[data-testid="diff-selected-file"]');
          const diffPane = document.querySelector('.diff-pane');
          return {
            fileList: Boolean(fileList),
            selected: selected?.textContent || '',
            selectedButtons: fileList ? fileList.querySelectorAll('button.selected').length : 0,
            paneText: diffPane?.textContent?.slice(0, 500) || ''
          };
        })())"""
    )
    data = parse_browser_json(raw)
    if not data["fileList"] or data["selectedButtons"] != 1:
        raise AssertionError(f"diff file browser missing selected file: {data}")
    if "saved_views.txt" not in data["selected"] or "+saved-views" not in data["paneText"]:
        raise AssertionError(f"diff file browser did not show selected file patch: {data}")


def parse_browser_json(raw: str) -> dict[str, object]:
    data = json.loads(raw)
    if isinstance(data, str):
        data = json.loads(data)
    if not isinstance(data, dict):
        raise AssertionError(f"expected browser JSON object, got: {raw}")
    return data


def assert_long_log_layout() -> None:
    raw = browser_eval(
        """JSON.stringify((() => {
          const inspector = document.querySelector('[data-testid="run-inspector"]');
          const detail = document.querySelector('[data-testid="run-detail-panel"]');
          const log = document.querySelector('[data-testid="run-log-pane"]');
          const detailLog = detail?.querySelector('[data-testid="run-log-pane"]');
          const inspectorBox = inspector?.getBoundingClientRect();
          const detailBox = detail?.getBoundingClientRect();
          return {
            inspectorExists: Boolean(inspector),
            detailExists: Boolean(detail),
            detailHasLog: Boolean(detailLog),
            logWhiteSpace: log ? getComputedStyle(log).whiteSpace : "",
            logOverflowWrap: log ? getComputedStyle(log).overflowWrap : "",
            inspectorWidth: inspectorBox ? Math.round(inspectorBox.width) : 0,
            inspectorHeight: inspectorBox ? Math.round(inspectorBox.height) : 0,
            inspectorTop: inspectorBox ? Math.round(inspectorBox.top) : 0,
            inspectorBottom: inspectorBox ? Math.round(inspectorBox.bottom) : 0,
            detailWidth: detailBox ? Math.round(detailBox.width) : 0,
            logClientHeight: log ? Math.round(log.clientHeight) : 0,
            logScrollHeight: log ? Math.round(log.scrollHeight) : 0,
            viewportWidth: window.innerWidth,
            viewportHeight: window.innerHeight,
            bodyScrollWidth: document.documentElement.scrollWidth,
            visibleLogHead: log?.textContent?.slice(0, 180) || "",
            visibleLogTail: log?.textContent?.slice(-120) || ""
          };
        })())"""
    )
    metrics = json.loads(raw)
    if isinstance(metrics, str):
        metrics = json.loads(metrics)
    if not metrics["inspectorExists"] or not metrics["detailExists"]:
        raise AssertionError(f"missing inspector/detail: {metrics}")
    if metrics["detailHasLog"]:
        raise AssertionError(f"log pane is still cramped inside the detail panel: {metrics}")
    if metrics["logWhiteSpace"] != "pre-wrap" or metrics["logOverflowWrap"] not in {"anywhere", "break-word"}:
        raise AssertionError(f"logs should wrap by default: {metrics}")
    if metrics["viewportWidth"] > 1180 and metrics["inspectorWidth"] < metrics["viewportWidth"] * 0.78:
        raise AssertionError(f"inspector should have a wide review workspace: {metrics}")
    if metrics["viewportWidth"] <= 980 and metrics["inspectorWidth"] < metrics["viewportWidth"] - 40:
        raise AssertionError(f"inspector should use the mobile width: {metrics}")
    if 980 < metrics["viewportWidth"] <= 1180 and metrics["inspectorWidth"] < metrics["viewportWidth"] * 0.68:
        raise AssertionError(f"inspector should use most of the sidebar desktop workspace: {metrics}")
    min_height = 620 if metrics["viewportHeight"] >= 760 else max(360, int(metrics["viewportHeight"] * 0.72))
    if metrics["inspectorHeight"] < min_height:
        raise AssertionError(f"inspector should have enough workspace height to scan evidence: {metrics}")
    if metrics["viewportWidth"] > 980 and metrics["inspectorBottom"] > metrics["viewportHeight"] + 1:
        raise AssertionError(f"inspector should be visible in the desktop viewport: {metrics}")
    if metrics["viewportWidth"] > 980 and metrics["inspectorTop"] > 140:
        raise AssertionError(f"inspector starts too low to scan: {metrics}")
    min_log_height = 420 if metrics["viewportHeight"] >= 760 else 220
    if metrics["logClientHeight"] < min_log_height:
        raise AssertionError(f"log viewport is too short to scan: {metrics}")
    if metrics["logScrollHeight"] <= metrics["logClientHeight"]:
        raise AssertionError(f"long logs should scroll inside the inspector: {metrics}")
    if metrics["bodyScrollWidth"] > metrics["viewportWidth"] + 1:
        raise AssertionError(f"page has horizontal overflow: {metrics}")
    if "0899" not in metrics["visibleLogTail"]:
        raise AssertionError(f"log tail did not load latest output: {metrics}")
    first_visible_line = str(metrics["visibleLogHead"]).split("\n\n", 1)[-1].splitlines()[0]
    if not first_visible_line.startswith("0771 [build]"):
        raise AssertionError(f"log truncation starts mid-line: {metrics}")


def assert_artifact_list_layout() -> None:
    raw = browser_eval(
        """JSON.stringify((() => {
          const cards = [...document.querySelectorAll('.artifact-list button')].map((button) => {
            const box = button.getBoundingClientRect();
            return {width: Math.round(box.width), height: Math.round(box.height), text: button.textContent || ""};
          });
          return {
            count: cards.length,
            maxHeight: Math.max(0, ...cards.map((card) => card.height)),
            minWidth: Math.min(...cards.map((card) => card.width)),
            hasOverflowArtifact: cards.some((card) => card.text.includes("messages") || card.text.includes("primary log"))
          };
        })())"""
    )
    metrics = json.loads(raw)
    if isinstance(metrics, str):
        metrics = json.loads(metrics)
    if metrics["count"] < 6 or not metrics["hasOverflowArtifact"]:
        raise AssertionError(f"artifact cards missing expected items: {metrics}")
    if metrics["maxHeight"] > 120:
        raise AssertionError(f"artifact cards are stretched instead of compact: {metrics}")
    if metrics["minWidth"] < 150:
        raise AssertionError(f"artifact cards are too narrow to scan: {metrics}")


def assert_artifact_log_theme() -> None:
    raw = browser_eval(
        """JSON.stringify((() => {
          const pane = document.querySelector('.artifact-pane pre');
          const infoLine = pane?.querySelector('.log-line-info');
          const paneStyle = pane ? getComputedStyle(pane) : null;
          const lineStyle = infoLine ? getComputedStyle(infoLine) : null;
          return {
            exists: Boolean(pane),
            whiteSpace: paneStyle?.whiteSpace || "",
            overflowWrap: paneStyle?.overflowWrap || "",
            background: paneStyle?.backgroundColor || "",
            infoLine: Boolean(infoLine),
            infoColor: lineStyle?.color || "",
            textHead: pane?.textContent?.slice(0, 220) || ""
          };
        })())"""
    )
    metrics = parse_browser_json(raw)
    if not metrics["exists"]:
        raise AssertionError(f"artifact log pane missing: {metrics}")
    if metrics["whiteSpace"] != "pre-wrap" or metrics["overflowWrap"] not in {"anywhere", "break-word"}:
        raise AssertionError(f"artifact log pane is not wrapped like logs tab: {metrics}")
    if not metrics["infoLine"] or metrics["infoColor"] in {"rgb(229, 231, 235)", "rgb(255, 255, 255)", "rgb(0, 0, 0)"}:
        raise AssertionError(f"artifact log pane lacks semantic log color: {metrics}")
    if "\n0716 [build]" not in metrics["textHead"] and "\n0772 [build]" not in metrics["textHead"]:
        raise AssertionError(f"artifact log pane lost line breaks: {metrics}")


def assert_inspector_closed() -> None:
    result = browser_eval("""(() => !document.querySelector('[data-testid="run-inspector"]'))()""")
    if not result.endswith("true"):
        raise AssertionError(f"run inspector should be closed: {result}")


def scroll_to_run_inspector() -> None:
    browser_eval(
        """(() => {
          document.querySelector('[data-testid="run-inspector"]')?.scrollIntoView({block: "nearest"});
          return true;
        })()"""
    )


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
