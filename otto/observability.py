"""Best-effort helpers for Otto observability artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


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
