from __future__ import annotations

import csv
import io
import sqlite3


def parse_csv_rows(text: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def resolve_import_tenant(conn: sqlite3.Connection, current_tenant_id: int) -> int:
    row = conn.execute(
        "SELECT id FROM tenants WHERE id != ? ORDER BY id LIMIT 1",
        (current_tenant_id,),
    ).fetchone()
    if row is None:
        row = conn.execute("SELECT id FROM tenants WHERE id = ?", (current_tenant_id,)).fetchone()
    if row is None:
        raise RuntimeError("no tenants available")
    return int(row["id"])
