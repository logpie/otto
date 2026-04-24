"""FastAPI app for local web Mission Control."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from otto.mission_control.serializers import serialize_project
from otto.mission_control.service import (
    MissionControlService,
    MissionControlServiceError,
    filters_from_params,
)


def create_app(project_dir: Path, *, queue_compat: bool = True) -> FastAPI:
    project_dir = Path(project_dir).resolve(strict=False)
    service = MissionControlService(project_dir, queue_compat=queue_compat)
    static_dir = Path(__file__).resolve().parent / "static"
    app = FastAPI(title="Otto Mission Control", version="0.1.0")
    app.state.project_dir = project_dir
    app.state.service = service

    @app.exception_handler(MissionControlServiceError)
    async def _service_error(_request, exc: MissionControlServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"ok": False, "message": str(exc), "severity": "error"},
        )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/api/project")
    def project() -> dict[str, Any]:
        return serialize_project(project_dir)

    @app.get("/api/state")
    def state(
        active_only: bool = False,
        type_filter: str = Query("all", alias="type"),
        outcome_filter: str = Query("all", alias="outcome"),
        query: str = "",
        history_page: int = 0,
    ) -> dict[str, Any]:
        filters = filters_from_params(
            active_only=active_only,
            type_filter=type_filter,
            outcome_filter=outcome_filter,
            query=query,
            history_page=history_page,
        )
        return service.state(filters)

    @app.get("/api/runs/{run_id}")
    def run_detail(
        run_id: str,
        type_filter: str = Query("all", alias="type"),
        outcome_filter: str = Query("all", alias="outcome"),
        query: str = "",
        history_page: int = 0,
    ) -> dict[str, Any]:
        filters = filters_from_params(
            type_filter=type_filter,
            outcome_filter=outcome_filter,
            query=query,
            history_page=history_page,
        )
        return service.detail(run_id, filters)

    @app.get("/api/runs/{run_id}/logs")
    def run_logs(run_id: str, log_index: int = 0, offset: int = 0) -> dict[str, Any]:
        return service.logs(run_id, log_index=log_index, offset=offset)

    @app.get("/api/runs/{run_id}/artifacts")
    def run_artifacts(run_id: str) -> dict[str, Any]:
        return service.artifacts(run_id)

    @app.get("/api/runs/{run_id}/artifacts/{artifact_index}/content")
    def run_artifact_content(run_id: str, artifact_index: int) -> dict[str, Any]:
        return service.artifact_content(run_id, artifact_index)

    @app.post("/api/runs/{run_id}/actions/{action}")
    def run_action(run_id: str, action: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        return service.execute(
            run_id,
            action,
            selected_queue_task_ids=payload.get("selected_queue_task_ids"),
            artifact_index=payload.get("artifact_index"),
        )

    @app.post("/api/actions/merge-all")
    def merge_all() -> dict[str, Any]:
        return service.merge_all()

    @app.get("/api/watcher")
    def watcher_status() -> dict[str, Any]:
        return service.watcher_status()

    @app.get("/api/runtime")
    def runtime_status() -> dict[str, Any]:
        return service.runtime_status()

    @app.get("/api/events")
    def events(limit: int = Query(80, ge=1, le=500)) -> dict[str, Any]:
        return service.events(limit=limit)

    @app.post("/api/watcher/start")
    def watcher_start(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        return service.start_watcher(
            concurrent=payload.get("concurrent"),
            exit_when_empty=bool(payload.get("exit_when_empty", False)),
        )

    @app.post("/api/watcher/stop")
    def watcher_stop() -> dict[str, Any]:
        return service.stop_watcher()

    @app.post("/api/queue/{command}")
    def queue(command: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        return service.enqueue(command, payload)

    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app
