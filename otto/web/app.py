"""FastAPI app for local web Mission Control."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
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


PROJECT_SLUG_RE = re.compile(r"[^a-z0-9]+")


def create_app(
    project_dir: Path,
    *,
    queue_compat: bool = True,
    project_launcher: bool = False,
    projects_root: Path | None = None,
) -> FastAPI:
    project_dir = Path(project_dir).resolve(strict=False)
    projects_root = (Path(projects_root).expanduser() if projects_root else Path.home() / "otto-projects").resolve(strict=False)
    service = None if project_launcher else MissionControlService(project_dir, queue_compat=queue_compat)
    static_dir = Path(__file__).resolve().parent / "static"
    app = FastAPI(title="Otto Mission Control", version="0.1.0")
    app.state.project_dir = None if project_launcher else project_dir
    app.state.projects_root = projects_root
    app.state.project_launcher = project_launcher
    app.state.service = service

    def _set_project(next_project_dir: Path) -> dict[str, Any]:
        resolved = Path(next_project_dir).expanduser().resolve(strict=False)
        _ensure_git_project(resolved)
        app.state.project_dir = resolved
        app.state.service = MissionControlService(resolved, queue_compat=queue_compat)
        return serialize_project(resolved)

    def _service() -> MissionControlService:
        if app.state.service is None or app.state.project_dir is None:
            raise MissionControlServiceError("No project selected. Create or open a managed project first.", status_code=409)
        return app.state.service

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
        if app.state.project_dir is None:
            return {"path": None, "name": None, "branch": None, "dirty": False, "head_sha": None}
        return serialize_project(app.state.project_dir)

    @app.get("/api/projects")
    def projects() -> dict[str, Any]:
        root = app.state.projects_root
        managed = _managed_projects(root) if app.state.project_launcher or root.exists() else []
        return {
            "launcher_enabled": bool(app.state.project_launcher),
            "projects_root": str(root),
            "current": serialize_project(app.state.project_dir) if app.state.project_dir else None,
            "projects": managed,
        }

    @app.post("/api/projects/create")
    def create_project(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        root = app.state.projects_root
        name = str(payload.get("name") or "").strip()
        path = _create_managed_project(root, name)
        return {"ok": True, "project": _set_project(path), "projects": _managed_projects(root)}

    @app.post("/api/projects/select")
    def select_project(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        root = app.state.projects_root
        raw_path = str(payload.get("path") or "").strip()
        if not raw_path:
            raise MissionControlServiceError("project path is required", status_code=400)
        path = Path(raw_path).expanduser().resolve(strict=False)
        if not _is_relative_to(path, root):
            raise MissionControlServiceError(f"Managed projects must live under {root}", status_code=403)
        return {"ok": True, "project": _set_project(path), "projects": _managed_projects(root)}

    @app.post("/api/projects/clear")
    def clear_project() -> dict[str, Any]:
        app.state.project_dir = None
        app.state.service = None
        return {"ok": True, "current": None, "projects": _managed_projects(app.state.projects_root)}

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
        return _service().state(filters)

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
        return _service().detail(run_id, filters)

    @app.get("/api/runs/{run_id}/logs")
    def run_logs(run_id: str, log_index: int = 0, offset: int = 0) -> dict[str, Any]:
        return _service().logs(run_id, log_index=log_index, offset=offset)

    @app.get("/api/runs/{run_id}/artifacts")
    def run_artifacts(run_id: str) -> dict[str, Any]:
        return _service().artifacts(run_id)

    @app.get("/api/runs/{run_id}/artifacts/{artifact_index}/content")
    def run_artifact_content(run_id: str, artifact_index: int) -> dict[str, Any]:
        return _service().artifact_content(run_id, artifact_index)

    @app.get("/api/runs/{run_id}/proof-report")
    def run_proof_report(run_id: str) -> FileResponse:
        return FileResponse(_service().proof_report_path(run_id), media_type="text/html")

    @app.get("/api/runs/{run_id}/diff")
    def run_diff(run_id: str) -> dict[str, Any]:
        return _service().diff(run_id)

    @app.post("/api/runs/{run_id}/actions/{action}")
    def run_action(run_id: str, action: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        return _service().execute(
            run_id,
            action,
            selected_queue_task_ids=payload.get("selected_queue_task_ids"),
            artifact_index=payload.get("artifact_index"),
        )

    @app.post("/api/actions/merge-all")
    def merge_all() -> dict[str, Any]:
        return _service().merge_all()

    @app.get("/api/watcher")
    def watcher_status() -> dict[str, Any]:
        return _service().watcher_status()

    @app.get("/api/runtime")
    def runtime_status() -> dict[str, Any]:
        return _service().runtime_status()

    @app.get("/api/events")
    def events(limit: int = Query(80, ge=1, le=500)) -> dict[str, Any]:
        return _service().events(limit=limit)

    @app.post("/api/watcher/start")
    def watcher_start(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        return _service().start_watcher(
            concurrent=payload.get("concurrent"),
            exit_when_empty=bool(payload.get("exit_when_empty", False)),
        )

    @app.post("/api/watcher/stop")
    def watcher_stop() -> dict[str, Any]:
        return _service().stop_watcher()

    @app.post("/api/queue/{command}")
    def queue(command: str, payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        return _service().enqueue(command, payload)

    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    return app


def _managed_projects(root: Path) -> list[dict[str, Any]]:
    root.mkdir(parents=True, exist_ok=True)
    projects: list[dict[str, Any]] = []
    for child in sorted(root.iterdir(), key=lambda path: path.name.lower()):
        if not child.is_dir() or not (child / ".git").exists():
            continue
        item = serialize_project(child)
        item["managed"] = True
        projects.append(item)
    return projects


def _create_managed_project(root: Path, name: str) -> Path:
    slug = _project_slug(name)
    if not slug:
        raise MissionControlServiceError("Project name is required.", status_code=400)
    root.mkdir(parents=True, exist_ok=True)
    path = (root / slug).resolve(strict=False)
    if not _is_relative_to(path, root):
        raise MissionControlServiceError("Project path escapes the managed projects root.", status_code=400)
    if path.exists() and not path.is_dir():
        raise MissionControlServiceError(f"Project path already exists and is not a folder: {path}", status_code=409)
    if path.exists() and any(path.iterdir()):
        raise MissionControlServiceError(f"Project already exists: {path}", status_code=409)
    path.mkdir(parents=True, exist_ok=True)
    try:
        _run_git(path, "init", "-q", "-b", "main")
        _run_git(path, "config", "user.email", "otto@example.local")
        _run_git(path, "config", "user.name", "Otto")
        (path / "README.md").write_text(f"# {name.strip()}\n\nManaged by Otto.\n", encoding="utf-8")
        (path / "otto.yaml").write_text("default_branch: main\nqueue:\n  bookkeeping_files: []\n", encoding="utf-8")
        (path / ".gitignore").write_text(
            "\n".join(["otto_logs/", ".worktrees/", ".otto-queue*.lock"]) + "\n",
            encoding="utf-8",
        )
        _run_git(path, "add", ".")
        _run_git(path, "commit", "-q", "-m", "Initial Otto project")
    except Exception as exc:
        raise MissionControlServiceError(f"Failed to create managed project: {exc}", status_code=500) from exc
    return path


def _ensure_git_project(path: Path) -> None:
    if not path.exists() or not path.is_dir():
        raise MissionControlServiceError(f"Project folder does not exist: {path}", status_code=404)
    result = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=path, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise MissionControlServiceError(f"Project is not a git repository: {path}", status_code=400)


def _project_slug(name: str) -> str:
    return PROJECT_SLUG_RE.sub("-", name.strip().lower()).strip("-")


def _run_git(cwd: Path, *args: str) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "git failed").strip())


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
