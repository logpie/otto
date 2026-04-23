from __future__ import annotations

import sqlite3

from .db import counted_execute


def authenticate(conn: sqlite3.Connection, username: str, password: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, username FROM users WHERE username = ? AND password = ?",
        (username, password),
    ).fetchone()


def list_tasks_for_user(conn: sqlite3.Connection, user_id: int, label: str | None = None) -> list[dict[str, object]]:
    del user_id
    rows = counted_execute(
        conn,
        """
        SELECT tasks.id, tasks.title, labels.name AS label, tasks.done
        FROM tasks
        LEFT JOIN labels ON labels.id = tasks.label_id
        ORDER BY tasks.id
        """,
    ).fetchall()
    tasks = [
        {
            "id": row["id"],
            "title": row["title"],
            "label": row["label"],
            "done": bool(row["done"]),
        }
        for row in rows
    ]
    if label:
        return [task for task in tasks if task["label"] == label]
    return tasks


def list_label_summaries(conn: sqlite3.Connection, user_id: int) -> list[dict[str, object]]:
    labels = counted_execute(conn, "SELECT id, name FROM labels ORDER BY name").fetchall()
    summaries: list[dict[str, object]] = []
    for label_row in labels:
        count = counted_execute(
            conn,
            "SELECT COUNT(*) FROM tasks WHERE user_id = ? AND label_id = ?",
            (user_id, label_row["id"]),
        ).fetchone()[0]
        summaries.append({"name": label_row["name"], "task_count": count})
    return summaries
