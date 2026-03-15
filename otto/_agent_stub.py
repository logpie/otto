"""Stub for claude-agent-sdk when not installed."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ClaudeAgentOptions:
    permission_mode: str | None = None
    cwd: str | None = None
    model: str | None = None
    resume: str | None = None
    max_turns: int | None = None


@dataclass
class ResultMessage:
    subtype: str = "success"
    is_error: bool = False
    session_id: str = ""
    result: str | None = None
    total_cost_usd: float | None = None
    usage: dict[str, Any] | None = None


async def query(*, prompt: str, options: ClaudeAgentOptions | None = None):
    """Stub that yields a single ResultMessage."""
    yield ResultMessage()
