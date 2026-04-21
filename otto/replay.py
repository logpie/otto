"""Replay messages.jsonl through the current NarrativeFormatter.

Use this to regenerate narrative.log for a past session after the
formatter has been upgraded. messages.jsonl is lossless, so the
regeneration is faithful to the original run.

Entry point: `otto replay <session-id>` (registered in cli_logs.py).
Library entry: :func:`replay_session`.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from otto import paths
from otto.agent import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from otto.logstream import NarrativeFormatter


def _rebuild_block(d: dict):
    t = d.get("type")
    if t == "text":
        return TextBlock(text=d.get("text", ""))
    if t == "thinking":
        return ThinkingBlock(thinking=d.get("thinking", ""))
    if t == "tool_use":
        return ToolUseBlock(
            name=d.get("name", "") or "",
            input=d.get("input") or {},
            id=d.get("id"),
        )
    if t == "tool_result":
        return ToolResultBlock(
            content=str(d.get("content", "") or ""),
            tool_use_id=d.get("tool_use_id"),
            is_error=bool(d.get("is_error", False)),
        )
    return None


def _rebuild_message(rec: dict):
    t = rec.get("type")
    sid = rec.get("session_id", "") or ""
    if t == "assistant":
        content = [b for b in (_rebuild_block(d) for d in rec.get("blocks", [])) if b is not None]
        return AssistantMessage(content=content, session_id=sid)
    if t == "user":
        content = [b for b in (_rebuild_block(d) for d in rec.get("blocks", [])) if b is not None]
        return UserMessage(content=content, session_id=sid)
    if t == "result":
        return ResultMessage(
            subtype=rec.get("subtype", "success") or "success",
            is_error=bool(rec.get("is_error", False)),
            session_id=sid,
            result=rec.get("result"),
            total_cost_usd=rec.get("cost_usd") or rec.get("total_cost_usd"),
            usage=rec.get("usage"),
            structured_output=rec.get("structured_output"),
        )
    return None


def _infer_phase_name(path: Path) -> str:
    parts = set(path.parts)
    if "certify" in parts:
        return "CERTIFY"
    if "spec" in parts:
        return "SPEC"
    return "BUILD"


def _replay_one(jsonl_path: Path, out_path: Path) -> int:
    """Replay one messages.jsonl → narrative.log. Returns lines written."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    f = NarrativeFormatter(out_path, phase_name=_infer_phase_name(out_path))
    f.start()
    original_start = time.monotonic()
    lines_in = 0
    try:
        with jsonl_path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                lines_in += 1
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Rewind the formatter's clock so output elapsed matches original.
                elapsed = rec.get("elapsed_s") or 0.0
                f._start = original_start - float(elapsed)
                msg = _rebuild_message(rec)
                if msg is not None:
                    f.write_message(msg)
    finally:
        f.close()
    return lines_in


def replay_session(project_dir: Path, session_id: str) -> list[Path]:
    """Replay every messages.jsonl under a session into a narrative.regenerated.log
    sibling file. Returns the list of regenerated paths.

    Covers build/, certify/, spec/agent/ (or spec/agent-vN/) — anywhere a
    session logger wrote messages.jsonl.
    """
    sess = paths.session_dir(project_dir, session_id)
    if not sess.exists():
        raise FileNotFoundError(f"no such session: {sess}")

    written: list[Path] = []
    for jsonl in sorted(sess.rglob("messages.jsonl")):
        out = jsonl.with_name("narrative.regenerated.log")
        n = _replay_one(jsonl, out)
        written.append(out)
        print(f"  replayed {jsonl.relative_to(sess)} ({n} events) → {out.name}")
    return written
