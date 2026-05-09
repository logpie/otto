"""In-process MCP server holding Otto's custom tools for v5 Leads.

Tools (all in-process, no IPC):
    submit_subtask(intent, depends_on=[]) -> {task_id}
        Emit a child task to the project's queue. Returns task_id immediately.
        Lead's calling task is recorded as parent_task_id.

    begin_inline() -> "ok"
        Mark the current task as inline-build (no children). Records the
        decomposition decision in task_graph.

    verify(feature_scope_ids=[]) -> {journeys: [...], verdict, evidence}
        Run audit at this Lead's level against the running product.
        Empty feature_scope_ids = audit all journeys for this task's scope.

    checkpoint(reason) -> "ok"
        Persist state for resumability. NON-BLOCKING in autopilot.

The MCP server is created PER LEAD SESSION via ``create_otto_mcp_server``.
Each Lead's tools have access to its own task_id, project_dir, and session_dir
via closure. This means the tools "know who they are" without the Lead having
to pass identity each call.

SDK groundedness: in-process MCP works with depth-1 in-session subagents in
SDK 0.1.50 (verified via /tmp/sdk-smoke/smoke.py). We don't use depth-2
in-session subagents (verified broken in /tmp/sdk-smoke/test_depth2_v2.py);
recursion happens via the queue (verified at depth 3+ via test_queue_recursion.py).
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("otto.mcp_tools")


def _intent_hash(parent_task_id: str | None, intent: str) -> str:
    parent = parent_task_id or "root"
    return hashlib.sha256(f"{parent}::{intent}".encode("utf-8")).hexdigest()[:16]


def create_otto_mcp_server(
    *,
    task_id: str,
    project_dir: Path,
    session_dir: Path,
    integration_branch: str | None,
):
    """Build the in-process MCP server for one Lead session.

    The Lead at ``task_id`` calls these tools; each tool knows the Lead's
    identity via closure.

    Returns an SdkMcpServerConfig dict suitable for passing to
    ``ClaudeAgentOptions.mcp_servers={"otto": <result>}``.
    """
    # Imports here so this module is importable without claude_agent_sdk
    # at static-analysis time (some tests stub the SDK).
    from claude_agent_sdk import create_sdk_mcp_server, tool

    from otto.queue.task_graph import (
        add_cost,
        record_task,
        set_decomposition,
    )

    # ------------------------------------------------------------------
    # submit_subtask
    # ------------------------------------------------------------------
    @tool(
        "submit_subtask",
        (
            "Emit a child task to the project's queue. Returns task_id immediately. "
            "Use this when this Lead's intent contains MULTIPLE strategic areas. "
            "Each call produces one child task with its own Lead. The CALLING "
            "Lead's task_id is automatically recorded as parent_task_id; you do "
            "NOT pass it. If a child must run after another, pass depends_on=[task_id, ...]."
        ),
        {
            "intent": str,
            "depends_on": list[str],
        },
    )
    async def submit_subtask(args: dict[str, Any]) -> dict[str, Any]:
        intent = (args.get("intent") or "").strip()
        depends_on = list(args.get("depends_on") or [])
        if not intent:
            return _err("submit_subtask: 'intent' is required and must be non-empty.")

        # Idempotency: same (parent, intent_hash) returns the same task_id.
        # Prevents resume-after-crash duplication.
        idem_key = _intent_hash(task_id, intent)
        from otto.queue.task_graph import read_graph

        graph = read_graph(project_dir)
        for tid, entry in graph.get("tasks", {}).items():
            if (
                entry.get("parent_task_id") == task_id
                and _intent_hash(task_id, entry.get("intent") or "") == idem_key
            ):
                return _ok({"task_id": tid, "duplicate": True})

        # Create the child's task entry. The actual queue spawn is the watcher's
        # job; we just record the relationship so the watcher can pick it up.
        from otto.queue.subtask import enqueue_subtask

        try:
            child_task_id = enqueue_subtask(
                project_dir=project_dir,
                parent_task_id=task_id,
                parent_session_dir=session_dir,
                intent=intent,
                depends_on=depends_on,
                parent_integration_branch=integration_branch,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort: report to Lead
            logger.warning("submit_subtask failed: %s", exc)
            return _err(f"submit_subtask: enqueue failed: {exc}")

        record_task(
            project_dir,
            task_id=child_task_id,
            intent=intent,
            parent_task_id=task_id,
            depends_on=depends_on,
        )
        # Mark this Lead's decomposition as 'emit' once the first subtask lands.
        set_decomposition(project_dir, task_id, "emit")
        return _ok({"task_id": child_task_id, "duplicate": False})

    # ------------------------------------------------------------------
    # begin_inline
    # ------------------------------------------------------------------
    @tool(
        "begin_inline",
        (
            "Mark the current task as inline-build (no child tasks). "
            "Use this when this Lead's intent is one coherent unit of work "
            "and the Lead intends to build it in this session. Required before "
            "any Write/Edit if the Lead is not emitting subtasks."
        ),
        {},
    )
    async def begin_inline(_args: dict[str, Any]) -> dict[str, Any]:
        set_decomposition(project_dir, task_id, "inline")
        return _ok({"acknowledged": True})

    # ------------------------------------------------------------------
    # verify
    # ------------------------------------------------------------------
    @tool(
        "verify",
        (
            "Run the deterministic audit (browser/CLI/HTTP probes) against the "
            "running product. Audits behavior journeys appropriate to this "
            "Lead's scope. Returns structured pass/fail per journey plus "
            "evidence paths. This is the ONLY way to legitimately claim 'pass'."
        ),
        {
            "feature_scope_ids": list[str],
        },
    )
    async def verify(args: dict[str, Any]) -> dict[str, Any]:
        scope_ids = list(args.get("feature_scope_ids") or [])
        try:
            from otto.lead_verify import run_verify_for_lead

            result = await run_verify_for_lead(
                task_id=task_id,
                project_dir=project_dir,
                session_dir=session_dir,
                feature_scope_ids=scope_ids,
            )
            return _ok(result)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("verify failed: %s", exc)
            return _err(f"verify: {exc}")

    # ------------------------------------------------------------------
    # checkpoint
    # ------------------------------------------------------------------
    @tool(
        "checkpoint",
        (
            "Persist the current task state for resumability. Non-blocking in "
            "autopilot mode (just records). Useful before risky operations."
        ),
        {
            "reason": str,
        },
    )
    async def checkpoint(args: dict[str, Any]) -> dict[str, Any]:
        reason = str(args.get("reason") or "manual")
        # Cost: not the SDK budget cap (that's separate); just a log line.
        # Otto's checkpoint.py handles full state persistence at the runner level.
        logger.info("checkpoint(task=%s, reason=%s)", task_id, reason)
        return _ok({"acknowledged": True, "reason": reason})

    server = create_sdk_mcp_server("otto", "1.0.0", tools=[
        submit_subtask, begin_inline, verify, checkpoint
    ])
    # Note: server is a dict (McpSdkServerConfig); we cannot setattr on it.
    # The Lead runner reads cost from the SDK ResultMessage directly, not via
    # this server, so the helper isn't needed.
    return server


def _ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]
    }


def _err(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps({"error": message}, ensure_ascii=False)}],
        "isError": True,
    }
