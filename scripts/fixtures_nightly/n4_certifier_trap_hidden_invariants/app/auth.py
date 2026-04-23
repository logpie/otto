from __future__ import annotations

import sqlite3


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT users.id, users.username, users.tenant_id, tenants.slug AS tenant_slug
        FROM users
        JOIN tenants ON tenants.id = users.tenant_id
        WHERE username = ? AND password = ?
        """,
        (username, password),
    ).fetchone()
