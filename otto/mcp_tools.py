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
from otto.queue.task_graph import TASK_ROLES
from otto.schemas import (
    VERDICT_CATASTROPHIC,
    VERDICT_MERGE_BLOCKED,
    VERDICT_PARTIAL,
    VERDICT_PASS,
    VERDICT_UNVERIFIED,
)

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


def _coerce_task_role(raw: Any) -> str | None:
    if raw is None:
        return None
    role = str(raw).strip()
    return role if role in TASK_ROLES else "feature"


def _coerce_foundation_contracts(raw: Any) -> list[dict[str, Any]]:
    if raw is None:
        return []
    parsed = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text in {"[]", "{}", "null", "None"}:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
    if not isinstance(parsed, list):
        return []
    contracts: list[dict[str, Any]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip().lstrip("./")
        owner_task_id = str(item.get("owner_task_id") or "").strip()
        check = str(item.get("check") or "").strip()
        if not path or not owner_task_id or check not in {"literal", "semantic"}:
            continue
        contract: dict[str, Any] = {
            "path": path,
            "owner_task_id": owner_task_id,
            "check": check,
        }
        if isinstance(item.get("required_exports"), list):
            contract["required_exports"] = [str(v) for v in item["required_exports"] if str(v).strip()]
        if isinstance(item.get("behavior_probes"), list):
            contract["behavior_probes"] = [str(v) for v in item["behavior_probes"] if str(v).strip()]
        contracts.append(contract)
    return contracts


def _terminal_verdict(value: Any) -> bool:
    return value in {
        VERDICT_PASS,
        VERDICT_PARTIAL,
        VERDICT_UNVERIFIED,
        VERDICT_MERGE_BLOCKED,
        VERDICT_CATASTROPHIC,
    }


def _structured_err(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": True,
    }


def submit_subtask_for_lead(
    *,
    project_dir: Path,
    session_dir: Path,
    task_id: str,
    intent: str,
    depends_on: list[str] | None = None,
    owned_paths: list[str] | None = None,
    action_ids: list[str] | None = None,
    task_role: str | None = None,
    foundation_contracts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Record one emitted subtask, preserving corrected S0 ownership metadata."""
    intent = (intent or "").strip()
    if not intent:
        return {"error": "submit_subtask: 'intent' is required and must be non-empty."}
    if task_id != "root":
        return {
            "error": "submit_subtask: non-root Leads must build inline at this decomposition depth.",
            "kind": "inline_only_at_depth",
            "task_id": task_id,
            "decomposition": "inline_only",
        }

    role = _coerce_task_role(task_role)
    idem_key = _intent_hash(task_id, intent)
    from otto.queue.task_graph import read_graph, record_task, set_decomposition, update_task_metadata
    from otto.queue.subtask import (
        _locked_append,
        _pending_entry,
        append_pending_entry,
        v5_pending_path,
    )

    requested_owned_paths = list(owned_paths or [])
    path = v5_pending_path(project_dir)
    with _locked_append(path):
        graph = read_graph(project_dir)
        for tid, entry in graph.get("tasks", {}).items():
            if (
                isinstance(entry, dict)
                and entry.get("parent_task_id") == task_id
                and _intent_hash(task_id, entry.get("intent") or "") == idem_key
            ):
                existing_role = _coerce_task_role(entry.get("task_role")) or "feature"
                existing_owned_paths = list(entry.get("owned_paths") or [])
                role_changed = role is not None and existing_role != role
                changed = (
                    role_changed
                    or existing_owned_paths != requested_owned_paths
                )
                if changed and _terminal_verdict(entry.get("verdict")):
                    return {
                        "error": "submit_subtask: duplicate task has stale finalized ownership metadata",
                        "kind": "stale_duplicate_scope_refusal",
                        "task_id": tid,
                        "existing": {
                            "task_role": existing_role,
                            "owned_paths": existing_owned_paths,
                        },
                        "requested": {
                            "task_role": role if role is not None else existing_role,
                            "owned_paths": requested_owned_paths,
                        },
                    }
                if changed:
                    record_task(
                        project_dir,
                        task_id=tid,
                        intent=intent,
                        parent_task_id=task_id,
                        depends_on=depends_on,
                        owned_paths=requested_owned_paths,
                        action_ids=action_ids,
                        task_role=cast(Any, role),
                    )
                if foundation_contracts:
                    update_task_metadata(project_dir, task_id, foundation_contracts=foundation_contracts)
                return {"task_id": tid, "duplicate": True, "metadata_updated": changed}

        entry = _pending_entry(
            project_dir=project_dir,
            parent_task_id=task_id,
            parent_session_dir=session_dir,
            intent=intent,
            depends_on=depends_on,
            owned_paths=owned_paths,
            action_ids=action_ids,
            task_role=role or "feature",
            parent_integration_branch=None,
        )
        append_pending_entry(path, entry)
        child_task_id = str(entry["task_id"])
        record_task(
            project_dir,
            task_id=child_task_id,
            intent=intent,
            parent_task_id=task_id,
            depends_on=depends_on,
            owned_paths=owned_paths,
            action_ids=action_ids,
            task_role=cast(Any, role),
        )
        if foundation_contracts:
            update_task_metadata(project_dir, task_id, foundation_contracts=foundation_contracts)
        set_decomposition(project_dir, task_id, "emit")
        return {"task_id": child_task_id, "duplicate": False}


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
    from otto.queue.task_graph import set_decomposition

    # ------------------------------------------------------------------
    # submit_subtask
    # ------------------------------------------------------------------
    @tool(
        "submit_subtask",
        (
            "Emit a child task to the project's queue. Returns task_id immediately. "
            "Use this when this Lead's intent contains MULTIPLE strategic areas. "
            "Only the root Lead may emit subtasks; non-root Leads must build inline. "
            "Each call produces one child task with its own Lead. The CALLING "
            "Lead's task_id is automatically recorded as parent_task_id; you do "
            "NOT pass it. If a child must run after another, pass depends_on=[task_id, ...]. "
            "Do not predict feature owned_paths during root decomposition; the "
            "architect/scaffold child will author the authoritative partition "
            "after building the scaffold. Omit task_role when unchanged; use "
            "task_role='foundation' for the architect/scaffold child and an "
            "explicit task_role='feature' for ordinary feature children."
        ),
        {
            "intent": str,
            "depends_on": list[str],
            "owned_paths": list[str],
            "action_ids": list[str],
            "task_role": str | None,
            "foundation_contracts": list[dict[str, Any]],
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
        task_role = _coerce_task_role(args.get("task_role"))
        foundation_contracts = _coerce_foundation_contracts(args.get("foundation_contracts"))
        try:
            payload = submit_subtask_for_lead(
                project_dir=project_dir,
                session_dir=session_dir,
                task_id=task_id,
                intent=intent,
                depends_on=depends_on,
                owned_paths=owned_paths,
                action_ids=action_ids,
                task_role=task_role,
                foundation_contracts=foundation_contracts,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort: report to Lead
            logger.warning("submit_subtask failed: %s", exc)
            return _err(f"submit_subtask: enqueue failed: {exc}")
        if "error" in payload:
            return _structured_err(payload)
        return _ok(payload)

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
