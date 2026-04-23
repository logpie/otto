from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def default_db_path() -> Path:
    return Path(os.environ.get("OTTO_N4_DB", "nightly_n4.db"))


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or default_db_path())
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tenants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            FOREIGN KEY(tenant_id) REFERENCES tenants(id)
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id INTEGER NOT NULL,
            import_key TEXT,
            title TEXT NOT NULL,
            done INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(tenant_id) REFERENCES tenants(id)
        );
        """
    )
    conn.commit()


def seed_demo(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]
    if count:
        return
    conn.executemany("INSERT INTO tenants(slug) VALUES (?)", [("acme",), ("globex",)])
    tenant_ids = {
        row["slug"]: row["id"]
        for row in conn.execute("SELECT id, slug FROM tenants").fetchall()
    }
    conn.executemany(
        "INSERT INTO users(tenant_id, username, password) VALUES (?, ?, ?)",
        [
            (tenant_ids["acme"], "ava", "pw-ava"),
            (tenant_ids["globex"], "gary", "pw-gary"),
        ],
    )
    conn.executemany(
        "INSERT INTO tasks(tenant_id, import_key, title, done) VALUES (?, ?, ?, ?)",
        [
            (tenant_ids["acme"], "seed-1", "Initial ACME task", 0),
            (tenant_ids["globex"], "seed-2", "Globex launch review", 0),
        ],
    )
    conn.commit()
