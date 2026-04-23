from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def default_db_path() -> Path:
    return Path(os.environ.get("OTTO_N2_DB", "nightly_n2.db"))


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or default_db_path())
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL,
            session_token TEXT
        );
        """
    )
    conn.commit()


def seed_demo(conn: sqlite3.Connection) -> None:
    count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count:
        return
    conn.execute(
        "INSERT INTO users(email, password, session_token) VALUES (?, ?, ?)",
        ("alex@example.com", "pw-alex", None),
    )
    conn.commit()
