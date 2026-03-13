"""Stub for claude-agent-sdk when not installed."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ClaudeAgentOptions:
    prompt: str = ""
    options: dict[str, Any] = field(default_factory=dict)


class AgentResult:
    session_id: str | None = None


def query(options: ClaudeAgentOptions) -> AgentResult:
    return AgentResult()
