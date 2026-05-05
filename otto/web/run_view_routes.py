"""FastAPI routes for the new design's RunView (research §7 + A4).

Mounts `GET /api/runs` and `GET /api/runs/<session_id>`. The latter
returns a JSON RunView produced by `otto.mission_control.run_view.build_run_view`.

Compatible with launcher mode (multiple projects) via
`project_dir_provider` — same pattern as `install_i2p_routes`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

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
        """List all session ids under the current project's otto_logs/sessions/."""
        project = _resolve_project_dir()
        sessions_dir = project / "otto_logs" / "sessions"
        if not sessions_dir.exists():
            return JSONResponse({"runs": []})
        ids = sorted(
            (entry.name for entry in sessions_dir.iterdir() if entry.is_dir()),
            reverse=True,
        )
        return JSONResponse({"runs": ids})

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
    """Resolve a session id to a path under `<project>/otto_logs/sessions/`.

    Rejects path-traversal attempts (e.g. "../etc"). Returns the
    canonical path or raises HTTP 404 if the session doesn't exist.
    """
    sessions_dir = (project_dir / "otto_logs" / "sessions").resolve()
    candidate = (sessions_dir / session_id).resolve()
    try:
        candidate.relative_to(sessions_dir)
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"session id rejected: {session_id!r}",
        ) from exc
    if not candidate.exists() or not candidate.is_dir():
        raise HTTPException(
            status_code=404,
            detail=f"session not found: {session_id!r}",
        )
    return candidate


__all__ = ["install_run_view_routes"]
