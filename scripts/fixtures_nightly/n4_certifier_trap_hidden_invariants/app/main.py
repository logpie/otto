from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import db
from .auth import authenticate
from .importers import parse_csv_rows, resolve_import_tenant


def create_app(db_path: str | Path | None = None) -> FastAPI:
    conn = db.connect(db_path)
    db.init_db(conn)
    db.seed_demo(conn)
    resolved_db_path = db_path or db.default_db_path()
    conn.close()
    app = FastAPI(title="Nightly N4")
    app.state.db_path = str(resolved_db_path)

    def get_conn() -> sqlite3.Connection:
        conn = db.connect(app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.post("/login")
    def login(
        payload: dict[str, str],
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> JSONResponse:
        user = authenticate(conn, payload["username"], payload["password"])
        if user is None:
            raise HTTPException(status_code=401, detail="invalid credentials")
        response = JSONResponse({"ok": True, "tenant": user["tenant_slug"]})
        response.set_cookie("tenant_id", str(user["tenant_id"]), httponly=True)
        response.set_cookie("user_id", str(user["id"]), httponly=True)
        return response

    @app.get("/tasks")
    def tasks(
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, object]:
        tenant_id = request.cookies.get("tenant_id")
        if not tenant_id:
            raise HTTPException(status_code=401, detail="login required")
        rows = conn.execute(
            "SELECT title, import_key FROM tasks WHERE tenant_id = ? ORDER BY id",
            (int(tenant_id),),
        ).fetchall()
        return {"tasks": [dict(row) for row in rows]}

    @app.post("/tasks/import")
    def import_tasks(
        request: Request,
        payload: dict[str, str],
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict[str, int]:
        tenant_id = request.cookies.get("tenant_id")
        if not tenant_id:
            raise HTTPException(status_code=401, detail="login required")
        rows = parse_csv_rows(payload["csv"])
        import_tenant = resolve_import_tenant(conn, int(tenant_id))
        for row in rows:
            conn.execute(
                "INSERT INTO tasks(tenant_id, import_key, title, done) VALUES (?, ?, ?, 0)",
                (import_tenant, row["import_key"], row["title"]),
            )
        conn.commit()
        return {"created": len(rows)}

    return app


app = create_app(Path(tempfile.gettempdir()) / "otto-nightly-n4-import.db")
