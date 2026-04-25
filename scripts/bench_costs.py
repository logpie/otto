"""Shared benchmark cost parsing helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path

_COST_RE = re.compile(r"cost \$([0-9]+(?:\.[0-9]+)?)")


def merge_cost_from_state_dir(merge_dir: Path) -> float:
    """Return merge-agent cost from merge state files without double-counting notes."""
    total = 0.0
    for state_path in merge_dir.glob("merge-*/state.json"):
        try:
            data = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        seen_notes: set[str] = set()
        outcomes = data.get("outcomes", [])
        if not isinstance(outcomes, list):
            continue
        for outcome in outcomes:
            if not isinstance(outcome, dict):
                continue
            note = str(outcome.get("note") or "")
            if not note or note in seen_notes:
                continue
            seen_notes.add(note)
            match = _COST_RE.search(note)
            if match:
                total += float(match.group(1))
    return total
