"""Best-effort helpers for Otto observability artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterable


def append_text_log(path: Path, lines: str | Iterable[str]) -> None:
    """Append human-readable text to a log file without raising."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(lines, str):
            text = lines
        else:
            text = "\n".join(str(line) for line in lines)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
    except Exception:
        pass


def write_json_file(path: Path, data: Any) -> None:
    """Write readable JSON to a file without raising."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        pass


def update_json_file(path: Path, mutator: Callable[[dict[str, Any]], dict[str, Any] | None]) -> None:
    """Read-modify-write a JSON object file without raising."""
    try:
        current: dict[str, Any] = {}
        if path.exists():
            try:
                parsed = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    current = parsed
            except Exception:
                current = {}
        updated = mutator(dict(current))
        if updated is None:
            updated = current
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(updated, indent=2, sort_keys=True), encoding="utf-8")
    except Exception:
        pass
