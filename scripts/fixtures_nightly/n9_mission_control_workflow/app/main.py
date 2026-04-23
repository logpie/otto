from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel


class TaskCreate(BaseModel):
    title: str


def create_app() -> FastAPI:
    app = FastAPI(title="Nightly N9")

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/tasks")
    def list_tasks() -> list[dict[str, Any]]:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="GET /tasks is not implemented yet")

    @app.post("/tasks", status_code=status.HTTP_201_CREATED)
    def create_task(payload: TaskCreate) -> dict[str, Any]:
        del payload
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="POST /tasks is not implemented yet")

    @app.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_task(task_id: int) -> Response:
        del task_id
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="DELETE /tasks/{task_id} is not implemented yet")

    return app


app = create_app()
