"""FastAPI routes for the new design's RunView (research §7 + A4).

Mounts `GET /api/runs` and `GET /api/runs/<session_id>`. The latter
returns a JSON RunView produced by `otto.mission_control.run_view.build_run_view`.

Compatible with launcher mode (multiple projects) via
`project_dir_provider` — same pattern as `install_i2p_routes`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from otto.mission_control.actions import (
    execute_abort_group,
    execute_pause_run,
    execute_resume_run,
)
from otto.mission_control.run_view import build_run_view

logger = logging.getLogger("otto.web.run_view")


def install_run_view_routes(
    app: FastAPI,
    *,
    project_dir: Path | None = None,
    project_dir_provider: Callable[[], Path | None] | None = None,
) -> None:
    """Mount `/api/runs/*` routes onto the given FastAPI app.

    Args:
        app: FastAPI instance.
        project_dir: legacy fixed-project mode (mutually exclusive with provider).
        project_dir_provider: V19-style dynamic project resolver. Called per
            request so launcher-mode projects pick up the currently-selected
            project_dir.
    """
    if project_dir_provider is None and project_dir is None:
        raise ValueError(
            "install_run_view_routes needs project_dir or project_dir_provider"
        )

    def _resolve_project_dir() -> Path:
        if project_dir_provider is not None:
            resolved = project_dir_provider()
        else:
            resolved = project_dir
        if resolved is None:
            raise HTTPException(
                status_code=409,
                detail="No project selected.",
            )
        return resolved

    # Use /api/run-view/* to avoid collision with the legacy
    # /api/runs/{run_id}/... route surface for artifacts/logs/diff/etc.
    # Legacy /api/runs/* will be deleted in Phase C; until then, the new
    # design lives at /api/run-view/*.
    router = APIRouter(prefix="/api/run-view", tags=["run-view"])

    @router.get("")
    def list_runs() -> JSONResponse:
        """List all session ids under the current project's otto_logs/sessions/.

        Returns both the legacy ``runs: [str]`` array (kept for back-compat
        with existing callers/tests) and a richer ``sessions: [obj]`` array
        used by the new RunListLanding cards (B4). Each session object
        carries the fields the landing page renders without needing a
        per-session round trip — intent, status, counts, wall, cost,
        finished_at, and lifecycle.
        """
        project = _resolve_project_dir()
        session_dirs = _session_dirs(project)
        if not session_dirs:
            return JSONResponse({"runs": [], "sessions": []})
        ids = sorted(session_dirs.keys(), reverse=True)
        sessions = [_summarize_session(session_dirs[sid], sid) for sid in ids]
        return JSONResponse({"runs": ids, "sessions": sessions})

    @router.get("/{session_id}")
    def get_run(session_id: str) -> JSONResponse:
        """Return RunView JSON for the given session.

        404 if the session id doesn't exist or escapes the sessions dir.
        """
        project = _resolve_project_dir()
        session_dir = _resolve_session_dir(project, session_id)
        view = build_run_view(session_dir)
        return JSONResponse(view)

    # ---- A7: pause / resume / abort verbs --------------------------------
    # The run-view API reads from the spec-state.jsonl journal; these
    # endpoints append to the same journal so the read paths reflect the
    # operator action on the next poll. There's no separate command queue
    # (unlike the legacy queue/atomic cancel surfaces) because the runner
    # already polls the journal between phases for spec.review_approved /
    # group.invalidated_by_spec_edit; we ride the same poll for pause and
    # abort.

    @router.post("/{session_id}/actions/pause")
    def pause_run(session_id: str, payload: dict = Body(default_factory=dict)) -> JSONResponse:
        project = _resolve_project_dir()
        session_dir = _resolve_session_dir(project, session_id)
        note = str((payload or {}).get("note") or "").strip()
        result = execute_pause_run(session_dir, note=note)
        return JSONResponse(_action_to_json(result), status_code=200 if result.ok else 409)

    @router.post("/{session_id}/actions/resume")
    def resume_run(session_id: str, payload: dict = Body(default_factory=dict)) -> JSONResponse:
        project = _resolve_project_dir()
        session_dir = _resolve_session_dir(project, session_id)
        note = str((payload or {}).get("note") or "").strip()
        result = execute_resume_run(session_dir, note=note)
        return JSONResponse(_action_to_json(result), status_code=200 if result.ok else 409)

    @router.post("/{session_id}/groups/{group_id}/abort")
    def abort_group(
        session_id: str,
        group_id: str,
        payload: dict = Body(default_factory=dict),
    ) -> JSONResponse:
        project = _resolve_project_dir()
        session_dir = _resolve_session_dir(project, session_id)
        reason = str((payload or {}).get("reason") or "").strip()
        result = execute_abort_group(session_dir, group_id, reason=reason)
        return JSONResponse(_action_to_json(result), status_code=200 if result.ok else 409)

    app.include_router(router)


def _action_to_json(result) -> dict:
    return {
        "ok": result.ok,
        "message": result.message,
        "severity": result.severity,
    }


def _resolve_session_dir(project_dir: Path, session_id: str) -> Path:
    """Resolve a session id to a session path for the selected project.

    Queue/i2p runs execute in per-task worktrees, so their proof packets live
    under `<project>/.worktrees/<task>/otto_logs/sessions/<session>`, not the
    selected project's root `otto_logs/sessions`. Search both bounded roots
    while still rejecting path-traversal attempts.
    """
    if "/" in session_id or "\\" in session_id or session_id in {"", ".", ".."}:
        raise HTTPException(
            status_code=404,
            detail=f"session id rejected: {session_id!r}",
        )
    found = _session_dirs(project_dir).get(session_id)
    if found is None:
        raise HTTPException(
            status_code=404,
            detail=f"session not found: {session_id!r}",
        )
    return found


def _session_dirs(project_dir: Path) -> dict[str, Path]:
    """Return session-id → session-dir for project-root and queue worktrees."""
    roots = [(project_dir / "otto_logs" / "sessions").resolve()]
    worktrees = (project_dir / ".worktrees").resolve()
    if worktrees.exists() and worktrees.is_dir():
        for child in worktrees.iterdir():
            if not child.is_dir():
                continue
            roots.append((child / "otto_logs" / "sessions").resolve())

    found: dict[str, Path] = {}
    project_root = project_dir.resolve()
    for sessions_root in roots:
        try:
            sessions_root.relative_to(project_root)
        except ValueError:
            continue
        if not sessions_root.exists() or not sessions_root.is_dir():
            continue
        for entry in sessions_root.iterdir():
            if not entry.is_dir():
                continue
            # Root project sessions win ties; worktree duplicates are ignored.
            found.setdefault(entry.name, entry.resolve())
    return found


def _summarize_session(session_dir: Path, session_id: str) -> dict[str, Any]:
    """Build a compact session-summary dict for the landing-page card list (B4).

    Reads ``summary.json`` (preferred), ``proof-packet.json``,
    ``spec/spec.json``, and ``spec/lifecycle.json`` directly — avoids the
    full ``build_run_view`` cost when N sessions are listed.

    All fields are best-effort: missing files yield ``None`` rather than
    raising, so the landing page can render ``"—"`` placeholders without
    failing the whole list.
    """
    summary = _read_json(session_dir / "summary.json") or {}
    proof = _read_json(session_dir / "proof-packet.json") or {}
    spec = _read_json(session_dir / "spec" / "spec.json") or {}
    lifecycle = _read_json(session_dir / "spec" / "lifecycle.json") or {}

    intent = summary.get("intent") or proof.get("intent") or spec.get("intent")
    verdict = summary.get("verdict") or proof.get("verdict")
    status = summary.get("status") or verdict
    cost_usd = summary.get("cost_usd")
    if cost_usd is None:
        cost_usd = proof.get("cost_usd")
    wall_s = summary.get("wall_s")
    if wall_s is None:
        wall_s = proof.get("wall_s")

    features = proof.get("features") if isinstance(proof.get("features"), list) else []
    if not features and isinstance(spec.get("features"), list):
        features = spec.get("features", [])
    feature_total = len(features)
    feature_passed = sum(
        1 for f in features
        if isinstance(f, dict) and str(f.get("verdict") or "") == "passed"
    )

    findings = (
        proof.get("quality_findings")
        or proof.get("findings")
        or []
    )
    critical_findings = 0
    if isinstance(findings, list):
        for f in findings:
            sev = ""
            if isinstance(f, dict):
                sev = str(f.get("severity") or "").lower()
            elif isinstance(f, str):
                sev = ""
            if sev in ("critical", "blocking"):
                critical_findings += 1

    quality_score = proof.get("quality_score") or summary.get("quality_score")

    # R3-B10: surface the wireframe's "Built in N groups" subline. Prefer the
    # spec.json count (concrete, set at compile time); fall back to the
    # proof packet's landed_group_ids when spec.json is absent on legacy
    # sessions. Returns None when neither source exists so the frontend can
    # suppress the subline rather than rendering "0 groups".
    spec_groups = spec.get("groups") if isinstance(spec.get("groups"), list) else None
    if spec_groups is not None:
        group_count: int | None = len(spec_groups)
    else:
        landed = proof.get("landed_group_ids")
        blocked = proof.get("blocked_group_ids")
        if isinstance(landed, list) or isinstance(blocked, list):
            group_count = len(landed or []) + len(blocked or [])
        else:
            group_count = None

    return {
        "id": session_id,
        "intent": intent,
        "status": status,
        "verdict": verdict,
        "cost_usd": cost_usd,
        "wall_s": wall_s,
        "feature_total": feature_total,
        "feature_passed": feature_passed,
        "critical_findings": critical_findings,
        "quality_score": quality_score,
        "group_count": group_count,
        "finished_at": _session_finished_at(session_dir, summary, verdict),
        "lifecycle": lifecycle.get("lifecycle"),
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _session_finished_at(
    session_dir: Path,
    summary: dict[str, Any],
    verdict: Any,
) -> str | None:
    """Best-effort finished_at: summary.json field, then journal scan, then mtime.

    Pre-verdict (in-flight) sessions return ``None``.
    """
    if summary.get("finished_at"):
        return str(summary["finished_at"])
    if verdict in (None, ""):
        return None
    journal = session_dir / "spec-state.jsonl"
    if not journal.exists():
        journal = session_dir / "state.jsonl"
    if journal.exists():
        try:
            for line in reversed(journal.read_text(encoding="utf-8").splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = str(event.get("event") or event.get("kind") or "")
                if kind == "run.finished":
                    ts = event.get("ts") or event.get("timestamp")
                    if ts:
                        return str(ts)
        except OSError:
            pass
    # Fallback: mtime of proof-packet (when the run actually wrote its
    # final artifact) or summary.json.
    for candidate in (
        session_dir / "proof-packet.json",
        session_dir / "summary.json",
    ):
        if candidate.exists():
            try:
                from datetime import datetime, timezone
                ts = datetime.fromtimestamp(
                    candidate.stat().st_mtime, tz=timezone.utc
                )
                return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
            except OSError:
                continue
    return None


__all__ = ["install_run_view_routes"]
