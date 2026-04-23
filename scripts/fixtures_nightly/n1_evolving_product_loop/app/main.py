from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import db
from .html import render_home, render_login
from .repository import authenticate, list_label_summaries, list_tasks_for_user


def create_app(db_path: str | Path | None = None) -> FastAPI:
    conn = db.connect(db_path)
    db.init_db(conn)
    db.seed_demo(conn)
    resolved_db_path = db_path or db.default_db_path()
    conn.close()
    app = FastAPI(title="Nightly N1")
    app.state.db_path = str(resolved_db_path)

    def get_conn() -> sqlite3.Connection:
        conn = db.connect(app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        return render_home()

    @app.post("/login")
    def login(
        payload: dict[str, str],
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> JSONResponse:
        user = authenticate(conn, payload["username"], payload["password"])
        if user is None:
            raise HTTPException(status_code=401, detail="invalid credentials")
        response = JSONResponse({"ok": True, "username": user["username"]})
        response.set_cookie("user_id", str(user["id"]), httponly=True)
        return response

    @app.get("/me", response_class=HTMLResponse)
    def me(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> str:
        user_id = request.cookies.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="login required")
        username = conn.execute(
            "SELECT username FROM users WHERE id = ?",
            (int(user_id),),
        ).fetchone()
        if username is None:
            raise HTTPException(status_code=401, detail="unknown user")
        return render_login(username["username"])

    @app.get("/tasks")
    def tasks(
        request: Request,
        label: str | None = None,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, object]:
        user_id = request.cookies.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="login required")
        db.reset_query_count()
        items = list_tasks_for_user(conn, int(user_id), label=label)
        return {"tasks": items, "active_label": label, "query_count": db.get_query_count()}

    @app.get("/labels")
    def labels(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, object]:
        user_id = request.cookies.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="login required")
        db.reset_query_count()
        items = list_label_summaries(conn, int(user_id))
        return {"labels": items, "query_count": db.get_query_count()}

    return app


app = create_app(Path(tempfile.gettempdir()) / "otto-nightly-n1-import.db")
