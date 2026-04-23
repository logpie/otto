from __future__ import annotations

import os
import sqlite3
from pathlib import Path

QUERY_COUNT = 0


def default_db_path() -> Path:
    return Path(os.environ.get("OTTO_N1_DB", "nightly_n1.db"))


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or default_db_path())
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def counted_execute(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Cursor:
    global QUERY_COUNT
    QUERY_COUNT += 1
    return conn.execute(sql, params)


def reset_query_count() -> None:
    global QUERY_COUNT
    QUERY_COUNT = 0


def get_query_count() -> int:
    return QUERY_COUNT


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS labels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        );

        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            label_id INTEGER,
            done INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(label_id) REFERENCES labels(id)
        );
        """
    )
    conn.commit()


def seed_demo(conn: sqlite3.Connection) -> None:
    user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if user_count:
        return
    conn.executemany(
        "INSERT INTO users(username, password) VALUES (?, ?)",
        [
            ("alice", "pw-alice"),
            ("bob", "pw-bob"),
        ],
    )
    conn.executemany(
        "INSERT INTO labels(name) VALUES (?)",
        [("work",), ("home",), ("bug",)],
    )
    label_ids = {
        row["name"]: row["id"]
        for row in conn.execute("SELECT id, name FROM labels").fetchall()
    }
    user_ids = {
        row["username"]: row["id"]
        for row in conn.execute("SELECT id, username FROM users").fetchall()
    }
    conn.executemany(
        "INSERT INTO tasks(user_id, title, label_id, done) VALUES (?, ?, ?, ?)",
        [
            (user_ids["alice"], "Write release notes", label_ids["work"], 0),
            (user_ids["alice"], "Buy groceries", label_ids["home"], 0),
            (user_ids["alice"], "Fix flaky job", label_ids["bug"], 1),
            (user_ids["bob"], "Bob private roadmap", label_ids["home"], 0),
        ],
    )
    conn.commit()
