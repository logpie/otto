# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, cast

import pytest

from otto import v5_runner
from otto.lead import LeadResult
from otto.queue.subtask import v5_pending_path
from otto.queue.task_graph import (
    get_task,
    record_task,
    set_decomposition,
    set_verdict,
)
from otto.v5_branching import child_branch_name, integration_branch_name
from otto.v5_preflight_repair import OracleRepairResult, RepairPacket
from otto.v5_runner import ROOT_TASK_ID


pytestmark = pytest.mark.integration


def _git(cwd: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed in {cwd}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main", check=True)
    _git(repo, "config", "user.email", "test@example.invalid", check=True)
    _git(repo, "config", "user.name", "Test User", check=True)
    (repo / ".gitignore").write_text(".worktrees/\notto_logs/\n.otto/\n", encoding="utf-8")
    (repo / "CHARTER.md").write_text("# Tiny fixture\n", encoding="utf-8")
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A", check=True)
    _git(repo, "commit", "-q", "-m", "init", check=True)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", 0))
        except PermissionError as exc:
            pytest.skip(f"local socket bind denied by test environment: {exc}")
        return int(sock.getsockname()[1])


def _write_session_spec(session_dir: Path, journey: dict[str, Any] | None = None) -> Path:
    spec_path = session_dir / "spec" / "spec.json"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        json.dumps(
            {"routes": ["/workspaces"], "features": [], "behavior_journeys": [journey or _ui_journey()]},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return spec_path


def _enqueue_task(
    repo: Path,
    *,
    task_id: str,
    parent_task_id: str,
    intent: str,
) -> None:
    parent_session_dir = repo / "otto_logs" / "sessions" / f"session-{parent_task_id}"
    parent_session_dir.mkdir(parents=True, exist_ok=True)
    entry: dict[str, Any] = {
        "schema_version": 1,
        "task_id": task_id,
        "parent_task_id": parent_task_id,
        "parent_session_dir": str(parent_session_dir),
        "intent": intent,
        "depends_on": [],
        "owned_paths": [],
        "action_ids": [],
        "integration_branch": integration_branch_name(parent_task_id),
        "review_state": "approved",
        "enqueued_at": _now_iso(),
    }
    pending = v5_pending_path(repo)
    pending.parent.mkdir(parents=True, exist_ok=True)
    with pending.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    record_task(
        repo,
        task_id=task_id,
        intent=intent,
        parent_task_id=parent_task_id,
        integration_branch=cast(str, entry["integration_branch"]),
    )


def _lead_worktree(project_dir: Path, session_dir: Path) -> Path:
    linked = session_dir / "worktree"
    return linked.resolve() if linked.exists() else project_dir


def _ui_journey() -> dict[str, Any]:
    observable = {
        "kind": "network_and_ui_effect",
        "primary_action_id": "workspaces.create",
        "description": "Creating a workspace posts to the API and renders it.",
        "method": "POST",
        "path": "/api/workspaces",
        "status": 201,
        "selector": "[data-testid='workspace-card']",
        "text": "Acme Workspace",
        "ui_effect": "Acme Workspace appears in the workspace list.",
    }
    return {
        "id": "workspace_create_runtime",
        "role": "critical",
        "description": "Create a workspace through the UI.",
        "covers_primary_actions": ["workspaces.create"],
        "start_state": "seeded_user",
        "entry_route": "/workspaces",
        "verification_level": "ui",
        "pass_model": {
            "start_state": "seeded_user",
            "setup": [],
            "actions": [
                {
                    "id": "workspaces.create",
                    "state_changing": True,
                    "covers_primary_actions": ["workspaces.create"],
                    "selector": "[data-testid='create-workspace']",
                    "inputs": [
                        {"selector": "[data-testid='workspace-name']", "value": "Acme Workspace"},
                        {"selector": "[data-testid='workspace-slug']", "value": "acme-workspace"},
                    ],
                    "network_expectations": [
                        {"method": "POST", "path": "/api/workspaces", "status": 201}
                    ],
                    "success_observables": [observable],
                }
            ],
            "success_observables": [observable],
            "ready_policy": {"route": "/workspaces", "wait_for": "interactive", "timeout_ms": 3000},
            "settle_policy": {"after_action": "dom_or_network_effect", "timeout_ms": 3000},
            "network_expectations": [],
            "final_dom_assertions": [
                {
                    "kind": "persisted_data_visible",
                    "primary_action_id": "workspaces.create",
                    "description": "The created workspace remains visible.",
                    "selector": "[data-testid='workspace-card']",
                    "text": "Acme Workspace",
                }
            ],
        },
    }


def _write_ui_fixture(project: Path, *, mode: str, port: int) -> None:
    project.mkdir(parents=True, exist_ok=True)
    _git(project, "init", "-q", "-b", "main", check=True)
    _git(project, "config", "user.email", "test@example.invalid", check=True)
    _git(project, "config", "user.name", "Test User", check=True)
    (project / ".gitignore").write_text("otto_logs/\n", encoding="utf-8")
    (project / "CHARTER.md").write_text(
        "# UI fixture\n\n"
        "| Surface | Port | Env |\n"
        "| --- | ---: | --- |\n"
        f"| Frontend dev server | {port} | `FE_PORT` |\n",
        encoding="utf-8",
    )
    (project / "start.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"FE_PORT=\"${{FE_PORT:-{port}}}\"\n"
        "export FE_PORT\n"
        "python3 server.py\n",
        encoding="utf-8",
    )
    (project / "start.sh").chmod(0o755)
    (project / "server.py").write_text(
        f"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = {mode!r}
PORT = int(os.environ.get("FE_PORT", "{port}"))

WORKING_HTML = '''<!doctype html>
<html>
  <body>
    <main>
      <h1>Your workspaces</h1>
      <label>Workspace name <input data-testid="workspace-name" value=""></label>
      <label>Workspace slug <input data-testid="workspace-slug" value=""></label>
      <button data-testid="create-workspace">Create workspace</button>
      <section id="workspace-list"></section>
    </main>
    <script>
      document.querySelector("[data-testid='create-workspace']").addEventListener("click", async () => {{
        const name = document.querySelector("[data-testid='workspace-name']").value;
        const slug = document.querySelector("[data-testid='workspace-slug']").value;
        const res = await fetch("/api/workspaces", {{
          method: "POST",
          headers: {{"Content-Type": "application/json"}},
          body: JSON.stringify({{name, slug}})
        }});
        if (res.ok) {{
          const data = await res.json();
          const card = document.createElement("div");
          card.setAttribute("data-testid", "workspace-card");
          card.textContent = data.name;
          document.getElementById("workspace-list").appendChild(card);
        }}
      }});
    </script>
  </body>
</html>'''

DEAD_HTML = '''<!doctype html>
<html>
  <body>
    <main>
      <h1>Your workspaces</h1>
      <label>Workspace name <input data-testid="workspace-name" value=""></label>
      <label>Workspace slug <input data-testid="workspace-slug" value=""></label>
      <button data-testid="create-workspace">Create workspace</button>
      <section id="workspace-list"></section>
    </main>
  </body>
</html>'''

class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/workspaces":
            body = (WORKING_HTML if MODE == "working" else DEAD_HTML).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:
        if self.path == "/api/workspaces":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{{}}"
            payload = json.loads(raw.decode("utf-8"))
            body = json.dumps({{"name": payload.get("name"), "slug": payload.get("slug")}}).encode("utf-8")
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
""".lstrip(),
        encoding="utf-8",
    )
    _git(project, "add", "-A", check=True)
    _git(project, "commit", "-q", "-m", "fixture", check=True)


def _journey_verdict_payload(preflight_payload: dict[str, Any]) -> dict[str, Any]:
    clean_result = preflight_payload["clean_oracle_result"]
    artifact_paths: list[str] = []
    for step in clean_result.get("steps", []):
        if isinstance(step, dict) and step.get("id") == "ui_journeys":
            artifact_paths.extend(str(path) for path in step.get("artifact_paths") or [])
    verdict_paths = [Path(path) for path in artifact_paths if Path(path).name == "journey-verdicts.json"]
    assert verdict_paths, f"no journey-verdicts.json in artifact paths: {artifact_paths}"
    return json.loads(verdict_paths[0].read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_child_verify_repair_pass_reenters_when_upward_merge_gate_refuses_dirty_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    parent_branch = integration_branch_name("parent")
    child_id = "v5-child"
    child_branch = child_branch_name(child_id)
    _git(repo, "branch", parent_branch, "main", check=True)
    parent_worktree = tmp_path / "parent-integration"
    _git(repo, "worktree", "add", "-q", str(parent_worktree), parent_branch, check=True)
    (parent_worktree / "shared.txt").write_text("dirty parent gate refusal\n", encoding="utf-8")

    child_worktree = tmp_path / "child"
    _git(repo, "worktree", "add", "-q", "-b", child_branch, str(child_worktree), "main", check=True)
    child_session_dir = tmp_path / "child-session"
    spec_path = _write_session_spec(child_session_dir)

    record_task(repo, task_id="root", intent="root", parent_task_id=None)
    record_task(
        repo,
        task_id=child_id,
        intent="repair then merge",
        parent_task_id="root",
        integration_branch=parent_branch,
    )

    repair_packets: list[RepairPacket] = []

    async def fake_repair(packet: RepairPacket, **kwargs: Any) -> OracleRepairResult:
        repair_packets.append(packet)
        (child_worktree / "feature.txt").write_text("child repaired cleanly\n", encoding="utf-8")
        (child_session_dir / "verdict.json").write_text(
            json.dumps({
                "verdict": "pass",
                "summary": "child verify repair passed",
                "evidence": ["feature.txt"],
            })
            + "\n",
            encoding="utf-8",
        )
        commit_hook = kwargs.get("commit_hook")
        if commit_hook is not None:
            ok, detail = await commit_hook(packet, packet.latest_oracle_result)
            assert ok, detail
        return OracleRepairResult(
            verdict="pass",
            summary="child verify repair passed",
            cost_usd=0.01,
            agent_turns_used=1,
            oracle_invocations=1,
            packet_path=str(packet.packet_path),
            composite_gate={"passed": True},
        )

    events: list[dict[str, Any]] = []
    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_repair)

    result = await v5_runner._ensure_child_merge_ready(
        project_dir=repo,
        child_task_id=child_id,
        child_worktree=child_worktree,
        child_session_dir=child_session_dir,
        parent_integration_branch=parent_branch,
        original_intent="repair then merge",
        result=LeadResult(
            task_id=child_id,
            verdict="partial",
            cost_usd=0.2,
            verify_called=True,
            verify_result={"verdict": "partial", "summary": "needs verify repair"},
        ),
        config={"default_branch": "main"},
        max_parallel=1,
        run_started_at=None,
        spec_path=spec_path,
        on_event=events.append,
    )
    assert result.verdict == "pass"

    await v5_runner._merge_child_branch(
        project_dir=repo,
        child_task_id=child_id,
        child_worktree=child_worktree,
        child_session_dir=child_session_dir,
        parent_integration_branch=parent_branch,
        result=result,
        config={"default_branch": "main"},
        on_event=events.append,
    )

    merge_failures = [event for event in events if event.get("event") == "merge_failed"]
    task = get_task(repo, child_id) or {}
    reason = task.get("merge_blocked_reason")
    assert len(repair_packets) >= 2, (
        "upward merge gate refusal did not re-enter the child verify repair loop; "
        f"agent_turns={len(repair_packets)}, result={result.verdict!r}, "
        f"merge_detail={merge_failures[-1].get('detail') if merge_failures else None!r}, "
        f"recorded_reason={reason!r}"
    )
    reentry_packet = repair_packets[-1]
    reason_payload = json.dumps(
        {
            "latest": reentry_packet.latest_oracle_result,
            "state": reentry_packet.current_state,
            "context": reentry_packet.integration_context,
        },
        sort_keys=True,
        default=str,
    )
    assert "dirty" in reason_payload.lower() or "merge" in reason_payload.lower()


@pytest.mark.asyncio
async def test_root_ui_executor_runtime_failure_enters_preflight_repair_and_working_control_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dead_project = tmp_path / "dead-ui"
    working_project = tmp_path / "working-ui"
    _write_ui_fixture(dead_project, mode="dead", port=_free_port())
    _write_ui_fixture(working_project, mode="working", port=_free_port())
    dead_session = tmp_path / "dead-session"
    working_session = tmp_path / "working-session"
    _write_session_spec(dead_session)
    _write_session_spec(working_session)
    repair_packets: list[RepairPacket] = []

    async def fake_repair(packet: RepairPacket, **_kwargs: Any) -> OracleRepairResult:
        repair_packets.append(packet)
        return OracleRepairResult(
            verdict="merge_blocked",
            summary="ui runtime failure stayed non-pass",
            cost_usd=0.01,
            agent_turns_used=1,
            oracle_invocations=1,
            packet_path=str(packet.packet_path),
            escalation={"reason": "ui_journey_failed", "_written_at": _now_iso()},
        )

    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_repair)

    dead_payload = await v5_runner._run_integration_smoke_preflight_with_repair(
        project_dir=dead_project,
        worktree_path=dead_project,
        task_id=ROOT_TASK_ID,
        phase="pre_agent",
        session_dir=dead_session,
        config={"default_branch": "main"},
        integration_branch=None,
        journey_scope="root_integration",
    )
    dead_verdicts = _journey_verdict_payload(dead_payload)

    assert dead_payload["passed"] is False
    assert dead_payload["repair"]["terminal_state"] == "escalated"
    assert len(repair_packets) == 1
    assert repair_packets[0].repair_unit["repair_phase"] == "integration_smoke"
    assert repair_packets[0].integration_context["initial_preflight"]["issues"][0]["kind"] == (
        "ui_journey_failed"
    )
    dead_verdict = dead_verdicts["journey_verdicts"][0]
    assert dead_verdict["passed"] is False
    assert dead_verdict["source"] == "ui_executor"
    assert dead_verdict["status"] == "fail"
    assert "no observed network/DOM effect" in dead_verdict["detail"]

    working_payload = await v5_runner._run_integration_smoke_preflight_with_repair(
        project_dir=working_project,
        worktree_path=working_project,
        task_id=ROOT_TASK_ID,
        phase="pre_agent",
        session_dir=working_session,
        config={"default_branch": "main"},
        integration_branch=None,
        journey_scope="root_integration",
    )
    working_verdict = _journey_verdict_payload(working_payload)["journey_verdicts"][0]
    assert working_payload["passed"] is True
    assert "repair" not in working_payload
    assert working_verdict["passed"] is True
    assert working_verdict["source"] == "ui_executor"


@pytest.mark.asyncio
async def test_subtree_integration_pass_reenters_when_root_propagation_conflicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    record_task(repo, task_id=ROOT_TASK_ID, intent="root", parent_task_id=None)
    set_decomposition(repo, ROOT_TASK_ID, "emit")
    set_verdict(repo, ROOT_TASK_ID, "pending_children")
    _enqueue_task(
        repo,
        task_id="v5-subtree",
        parent_task_id=ROOT_TASK_ID,
        intent="Build a decomposed subtree",
    )

    class PropagationConflictLead:
        async def __call__(self, **kwargs: Any) -> LeadResult:
            task_id = str(kwargs["task_id"])
            kind = str(kwargs.get("kind") or "plan_or_inline")
            if task_id == "v5-subtree" and kind != "integration":
                _enqueue_task(
                    repo,
                    task_id="v5-grandchild",
                    parent_task_id="v5-subtree",
                    intent="Write subtree version",
                )
                set_decomposition(repo, "v5-subtree", "emit")
                set_verdict(repo, "v5-subtree", "pending_children")
                return LeadResult(
                    task_id="v5-subtree",
                    verdict="pending_children",
                    decomposition="emit",
                    emitted_subtask_ids=["v5-grandchild"],
                )
            if task_id == "v5-grandchild":
                worktree = _lead_worktree(kwargs["project_dir"], kwargs["session_dir"])
                (worktree / "shared.txt").write_text("subtree\n", encoding="utf-8")
                set_decomposition(repo, task_id, "inline")
                set_verdict(repo, task_id, "pass")
                return LeadResult(
                    task_id=task_id,
                    verdict="pass",
                    decomposition="inline",
                    verify_called=True,
                    verify_result={"verdict": "pass", "summary": "grandchild passed"},
                )
            if task_id == "v5-subtree" and kind == "integration":
                _git(repo, "checkout", "-q", "main", check=True)
                (repo / "shared.txt").write_text("root\n", encoding="utf-8")
                _git(repo, "commit", "-am", "root edits shared", check=True)
                set_verdict(repo, task_id, "pass")
                return LeadResult(
                    task_id=task_id,
                    verdict="pass",
                    decomposition="inline",
                    verify_called=True,
                    verify_result={"verdict": "pass", "summary": "subtree integration passed"},
                )
            raise AssertionError(f"unexpected lead call task_id={task_id} kind={kind}")

    async def fake_smoke_preflight(**_kwargs: Any) -> dict[str, Any]:
        return {"check": "smoke_clean_deploy", "passed": True, "issues": []}

    repair_packets: list[RepairPacket] = []

    async def fake_repair(packet: RepairPacket, **_kwargs: Any) -> OracleRepairResult:
        repair_packets.append(packet)
        return OracleRepairResult(
            verdict="pass",
            summary="propagation repair handled conflict",
            cost_usd=0.01,
            agent_turns_used=1,
            oracle_invocations=1,
            packet_path=str(packet.packet_path),
            composite_gate={"passed": True},
        )

    events: list[dict[str, Any]] = []
    monkeypatch.setattr(v5_runner, "run_lead", PropagationConflictLead())
    monkeypatch.setattr(v5_runner, "_run_integration_smoke_preflight_with_repair", fake_smoke_preflight)
    monkeypatch.setattr(v5_runner, "run_oracle_repair_agent", fake_repair)

    child_results: dict[str, LeadResult] = {}
    integration_results: dict[str, LeadResult] = {}
    await v5_runner._process_children(
        project_dir=repo,
        parent_task_id=ROOT_TASK_ID,
        config={"default_branch": "main"},
        max_parallel=1,
        tree_budget_usd=1.0,
        child_results=child_results,
        integration_results=integration_results,
        on_event=events.append,
    )

    blocked = [event for event in events if event.get("event") == "subtree_propagation_blocked"]
    task = get_task(repo, "v5-subtree") or {}
    assert len(repair_packets) >= 1, (
        "subtree propagation conflict did not enter an agentic repair loop; "
        f"repair_packets={len(repair_packets)}, propagation_events={blocked!r}, "
        f"task_verdict={task.get('verdict')!r}, recorded_reason={task.get('merge_blocked_reason')!r}"
    )
    reason_payload = json.dumps(
        {
            "latest": repair_packets[-1].latest_oracle_result,
            "state": repair_packets[-1].current_state,
            "context": repair_packets[-1].integration_context,
        },
        sort_keys=True,
        default=str,
    )
    assert "conflict" in reason_payload.lower()
