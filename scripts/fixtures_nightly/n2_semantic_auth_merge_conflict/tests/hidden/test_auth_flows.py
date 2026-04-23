from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient


def _columns(client: TestClient) -> set[str]:
    conn: sqlite3.Connection = client.app.state.conn
    rows = conn.execute("PRAGMA table_info(users)").fetchall()
    return {row[1] for row in rows}


def test_login_and_future_schema_fields_exist(client: TestClient) -> None:
    response = client.post("/login", json={"email": "alex@example.com", "password": "pw-alex"})
    assert response.status_code == 200
    assert {"password_reset_token", "remember_session_until"} <= _columns(client)


def test_password_reset_flow_exists(client: TestClient) -> None:
    response = client.post("/auth/request-password-reset", json={"email": "alex@example.com"})
    assert response.status_code == 200


def test_remember_me_sets_long_lived_session(client: TestClient) -> None:
    response = client.post(
        "/login",
        json={"email": "alex@example.com", "password": "pw-alex", "remember_me": True},
    )
    assert response.status_code == 200
    set_cookie = response.headers.get("set-cookie", "")
    assert "Max-Age=2592000" in set_cookie or "max-age=2592000" in set_cookie
