"""Best-effort helpers for Otto observability artifacts."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger("otto.observability")


def append_text_log(path: Path, lines: str | Iterable[str]) -> None:
    """Append human-readable text to a log file.

    Best-effort: logs and swallows I/O failures so observability writes never
    break the caller's main path. The log message names the file so a silent
    write failure is still discoverable in the debug log.
    """
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
    except OSError as exc:
        logger.warning("append_text_log(%s) failed: %s", path, exc)


def write_json_file(path: Path, data: Any) -> None:
    """Write readable JSON to a file. Best-effort, logs I/O failures."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("write_json_file(%s) failed: %s", path, exc)


