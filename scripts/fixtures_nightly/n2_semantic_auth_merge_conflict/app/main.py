from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from . import db
from .html import account_page, login_page
from .security import new_token


def create_app(db_path: str | Path | None = None) -> FastAPI:
    conn = db.connect(db_path)
    db.init_db(conn)
    db.seed_demo(conn)
    resolved_db_path = db_path or db.default_db_path()
    conn.close()
    app = FastAPI(title="Nightly N2")
    app.state.db_path = str(resolved_db_path)
    app.state.conn = db.connect(app.state.db_path)

    def get_conn() -> sqlite3.Connection:
        conn = db.connect(app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/", response_class=HTMLResponse)
    def home() -> str:
        return login_page()

    @app.post("/login")
    def login(
        payload: dict[str, str],
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> JSONResponse:
        row = conn.execute(
            "SELECT id, email FROM users WHERE email = ? AND password = ?",
            (payload["email"], payload["password"]),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=401, detail="invalid credentials")
        session_token = new_token("session")
        conn.execute(
            "UPDATE users SET session_token = ? WHERE id = ?",
            (session_token, row["id"]),
        )
        conn.commit()
        response = JSONResponse({"ok": True, "email": row["email"]})
        response.set_cookie("session_token", session_token, httponly=True)
        return response

    @app.get("/me", response_class=HTMLResponse)
    def me(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> str:
        token = request.cookies.get("session_token")
        if not token:
            raise HTTPException(status_code=401, detail="login required")
        row = conn.execute(
            "SELECT email FROM users WHERE session_token = ?",
            (token,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=401, detail="unknown session")
        return account_page(row["email"])

    return app


app = create_app(Path(tempfile.gettempdir()) / "otto-nightly-n2-import.db")
