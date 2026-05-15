"""In-process MCP server holding Otto's custom tools for v5 Leads.

Tools (all in-process, no IPC):
    submit_subtask(intent, depends_on=[], owned_paths=[], action_ids=[]) -> {task_id}
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
from typing import Any, cast

from otto.journey_scope_policy import ExecutionScope

logger = logging.getLogger("otto.mcp_tools")


def _intent_hash(parent_task_id: str | None, intent: str) -> str:
    parent = parent_task_id or "root"
    return hashlib.sha256(f"{parent}::{intent}".encode("utf-8")).hexdigest()[:16]


async def _run_scaffold_certification(
    *,
    project_dir: Path,
    session_dir: Path,
    build_command: str,
    summary: str,
) -> dict[str, Any]:
    """Run a build command, write a verify-result.json, return the payload.

    Module-level helper so unit tests can exercise it without reaching into
    the SDK's tool registry.
    """
    import asyncio as _asyncio
    import os as _os
    import subprocess as _sp

    cmd = (build_command or "").strip()
    if not cmd:
        return {"_err": "certify_scaffold: build_command is required."}

    # Resolve the worktree the same way verify does.
    verify_dir = project_dir
    wt_link = session_dir / "worktree"
    try:
        if wt_link.is_symlink() or wt_link.is_dir():
            target = wt_link.resolve()
            if target.exists():
                verify_dir = target
    except OSError:
        pass

    log_dir = session_dir / "verify"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "scaffold-build.log"
    result_path = log_dir / "verify-result.json"

    try:
        proc = await _asyncio.create_subprocess_shell(
            cmd,
            cwd=str(verify_dir),
            stdout=_sp.PIPE,
            stderr=_sp.STDOUT,
            env={**_os.environ},
        )
        stdout_b, _ = await _asyncio.wait_for(proc.communicate(), timeout=600)
        text = (stdout_b or b"").decode("utf-8", errors="replace")
        log_path.write_text(text, encoding="utf-8")
        exit_code = proc.returncode
    except Exception as exc:  # noqa: BLE001
        return {"_err": f"certify_scaffold: command crashed: {exc}"}

    if exit_code != 0:
        tail = "\n".join((text or "").strip().splitlines()[-5:])
        payload = {
            "verdict": "partial",
            "journeys": [],
            "evidence": [str(log_path)],
            "summary": f"scaffold build failed (exit {exit_code}): {tail[:200]}",
            "scaffold": True,
        }
    else:
        payload = {
            "verdict": "pass",
            "journeys": [],
            "evidence": [str(log_path)],
            "summary": (summary or "scaffold compiled").strip(),
            "scaffold": True,
        }
    result_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def _coerce_id_list(raw: Any) -> list[str]:
    """Coerce LLM-supplied id list to clean list[str].

    Accepts:
      - list/tuple of strings
      - JSON-encoded list string ("[]", '["a","b"]')
      - comma-joined string ("a,b")
      - single task id ("a")
      - None / falsy

    Drops empty strings and obvious JSON literals (\"[]\", \"{}\", \"null\").
    """
    if raw is None:
        return []
    items: list[str] = []
    if isinstance(raw, (list, tuple)):
        items = [str(s) for s in raw]
    elif isinstance(raw, str):
        text = raw.strip()
        if not text or text in {"[]", "{}", "null", "None"}:
            return []
        # Try JSON first.
        try:
            import json as _json
            parsed = _json.loads(text)
            if isinstance(parsed, list):
                items = [str(s) for s in parsed]
            elif isinstance(parsed, str):
                items = [parsed]
            else:
                items = []
        except Exception:  # noqa: BLE001
            items = [s.strip() for s in text.split(",")]
    else:
        return []
    # Filter out empties and obvious JSON noise.
    cleaned = [s.strip() for s in items if s and str(s).strip()]
    cleaned = [s for s in cleaned if s not in {"[]", "{}", "null", "None"}]
    return cleaned


def create_otto_mcp_server(
    *,
    task_id: str,
    project_dir: Path,
    session_dir: Path,
    integration_branch: str | None,
    execution_scope: str = "leaf",
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
            "NOT pass it. If a child must run after another, pass depends_on=[task_id, ...]. "
            "When known, pass owned_paths=[...] and action_ids=[...] so Otto can "
            "safely scope child context."
        ),
        {
            "intent": str,
            "depends_on": list[str],
            "owned_paths": list[str],
            "action_ids": list[str],
        },
    )
    async def submit_subtask(args: dict[str, Any]) -> dict[str, Any]:
        intent = (args.get("intent") or "").strip()
        # depends_on may arrive as a list, a JSON-encoded list string, a
        # comma-joined string, a single task id, or a JSON literal like "[]".
        # Coerce all shapes to a clean list of plausible task ids.
        depends_on = _coerce_id_list(args.get("depends_on"))
        owned_paths = _coerce_id_list(args.get("owned_paths"))
        action_ids = _coerce_id_list(args.get("action_ids"))
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

        # Children of THIS Lead merge into THIS Lead's own integration branch
        # (i2p/<task_id>/integration), NOT the branch this Lead itself merges
        # back into. Passing None lets enqueue_subtask compute the namespaced
        # default; passing this Lead's outer integration_branch would put
        # children on the wrong branch and break worktree isolation when an
        # integration Lead later tries to check out the same branch the
        # project_dir is already on.
        try:
            child_task_id = enqueue_subtask(
                project_dir=project_dir,
                parent_task_id=task_id,
                parent_session_dir=session_dir,
                intent=intent,
                depends_on=depends_on,
                owned_paths=owned_paths,
                action_ids=action_ids,
                parent_integration_branch=None,
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
            owned_paths=owned_paths,
            action_ids=action_ids,
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
        scope_ids = _coerce_id_list(args.get("feature_scope_ids"))
        try:
            from otto.lead_verify import run_verify_for_lead

            result = await run_verify_for_lead(
                task_id=task_id,
                project_dir=project_dir,
                session_dir=session_dir,
                feature_scope_ids=scope_ids,
                execution_scope=cast(ExecutionScope, execution_scope),
            )
            return _ok(result)
        except Exception as exc:  # noqa: BLE001 — best-effort
            logger.warning("verify failed: %s", exc)
            return _err(f"verify: {exc}")

    # ------------------------------------------------------------------
    # certify_scaffold (lightweight verify for Architect phase)
    # ------------------------------------------------------------------
    @tool(
        "certify_scaffold",
        (
            "LIGHTWEIGHT verification for scaffold-only tasks. Use this INSTEAD "
            "of mcp__otto__verify when your task only produces infrastructure "
            "(CHARTER.md + empty project shell + dependencies wired) and there "
            "is no testable behavior yet. Runs the given build_command; if it "
            "succeeds, marks the task as pass without spinning up Playwright "
            "or running behavior journeys. Saves ~10 minutes per Architect run."
        ),
        {"build_command": str, "summary": str},
    )
    async def certify_scaffold(args: dict[str, Any]) -> dict[str, Any]:
        payload = await _run_scaffold_certification(
            project_dir=project_dir,
            session_dir=session_dir,
            build_command=args.get("build_command") or "",
            summary=args.get("summary") or "scaffold compiled",
        )
        if "_err" in payload:
            return _err(payload["_err"])
        return _ok(payload)

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

    # Simplified architecture: only decomposition + checkpoint tools exposed
    # to agents. Verification is done by the agent itself running its own
    # tests via Bash and writing verdict.json. No mcp__otto__verify or
    # mcp__otto__certify_scaffold — the agent's loop IS the verification.
    server = create_sdk_mcp_server("otto", "1.0.0", tools=[
        submit_subtask, begin_inline, checkpoint
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
