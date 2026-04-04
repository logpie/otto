"""Agent session lifecycle — start, resume, checkpoint, fallback.

Manages a coding agent's session across build→certify→fix cycles.
Supports resume via session_id (Claude provider) with fallback to
compressed state package for providers that don't support resume.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from otto.agent import ClaudeAgentOptions, run_agent_query
from otto.config import agent_provider

logger = logging.getLogger("otto.session")


@dataclass
class SessionCheckpoint:
    """Durable checkpoint for crash recovery."""
    session_id: str | None
    base_sha: str
    round: int
    state: str                     # "building" | "certifying" | "certified" | "fixing"
    certifier_outcome: str | None  # "passed" | "failed" | "blocked" | "infra_error" | None
    candidate_sha: str | None
    intent: str
    last_summary: str = ""
    findings: list[dict[str, Any]] | None = None
    cost_so_far: float = 0.0
    created_at: str = ""

    def save(self, path: Path) -> None:
        """Write checkpoint to disk."""
        self.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, default=str))

    @classmethod
    def load(cls, path: Path) -> SessionCheckpoint | None:
        """Load checkpoint from disk. Returns None if not found or corrupt."""
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.warning("Failed to load checkpoint %s: %s", path, exc)
            return None


@dataclass
class SessionResult:
    """Result of an agent session turn."""
    text: str
    cost: float
    result_msg: Any
    session_id: str | None

    @property
    def end_status(self) -> str:
        """Parse structured output for end state. Falls back to 'ready_for_review'."""
        if self.result_msg and hasattr(self.result_msg, "structured_output"):
            so = self.result_msg.structured_output
            if isinstance(so, dict) and "status" in so:
                return so["status"]
        return "ready_for_review"

    @property
    def summary(self) -> str:
        """Extract summary from structured output or last text."""
        if self.result_msg and hasattr(self.result_msg, "structured_output"):
            so = self.result_msg.structured_output
            if isinstance(so, dict) and "summary" in so:
                return so["summary"]
        # Fallback: last 200 chars of text
        return self.text[-200:] if self.text else ""


class AgentSession:
    """Manages a coding agent session lifecycle.

    Supports:
    - Start: new session with build prompt
    - Resume: continue session with feedback (falls back to state package)
    - Checkpoint: durable state for crash recovery
    """

    def __init__(
        self,
        intent: str,
        options: ClaudeAgentOptions,
        project_dir: Path,
        config: dict[str, Any] | None = None,
        checkpoint_dir: Path | None = None,
    ):
        self.intent = intent
        self.options = options
        self.project_dir = project_dir
        self.config = config or {}
        self.session_id: str | None = None
        self.base_sha: str = ""
        self.total_cost: float = 0.0
        self.round: int = 0
        self.last_summary: str = ""
        self._checkpoint_dir = checkpoint_dir or project_dir / "otto_logs" / "session"
        self._provider = agent_provider(self.config)
        self._supports_resume = self._provider != "codex"

    async def start(self, prompt: str) -> SessionResult:
        """Start a new agent session."""
        self.base_sha = self._get_head_sha()
        self.round = 0

        logger.info("Starting agent session (provider=%s, resume_supported=%s)",
                     self._provider, self._supports_resume)

        text, cost, result_msg = await run_agent_query(prompt, self.options)
        self.session_id = getattr(result_msg, "session_id", None)
        self.total_cost += cost

        result = SessionResult(
            text=text, cost=cost, result_msg=result_msg,
            session_id=self.session_id,
        )
        self.last_summary = result.summary
        logger.info("Session started: session_id=%s, cost=$%.2f", self.session_id, cost)
        return result

    async def resume(self, feedback: str) -> SessionResult:
        """Resume session with feedback. Falls back to state package on failure."""
        self.round += 1

        if self._supports_resume and self.session_id:
            try:
                return await self._resume_with_session_id(feedback)
            except Exception as exc:
                logger.warning("Session resume failed (session_id=%s): %s. Falling back to state package.",
                               self.session_id, exc)
                # Fall through to state package

        # Fallback: new session with compressed context
        return await self._resume_with_state_package(feedback)

    async def _resume_with_session_id(self, feedback: str) -> SessionResult:
        """Resume via SDK session_id."""
        opts = ClaudeAgentOptions(
            permission_mode=self.options.permission_mode,
            cwd=self.options.cwd,
            model=self.options.model,
            resume=self.session_id,
            system_prompt=self.options.system_prompt,
            mcp_servers=self.options.mcp_servers,
            env=self.options.env,
            setting_sources=self.options.setting_sources,
            disallowed_tools=self.options.disallowed_tools,
            output_format=self.options.output_format,
        )

        text, cost, result_msg = await run_agent_query(feedback, opts)
        new_session_id = getattr(result_msg, "session_id", None)
        if new_session_id:
            self.session_id = new_session_id
        self.total_cost += cost

        result = SessionResult(
            text=text, cost=cost, result_msg=result_msg,
            session_id=self.session_id,
        )
        self.last_summary = result.summary
        logger.info("Session resumed: session_id=%s, cost=$%.2f, total=$%.2f",
                     self.session_id, cost, self.total_cost)
        return result

    async def _resume_with_state_package(self, feedback: str) -> SessionResult:
        """Start fresh session with compressed state context."""
        state_prompt = self._build_state_package(feedback)

        logger.info("Resuming with state package (session continuity lost)")
        text, cost, result_msg = await run_agent_query(state_prompt, self.options)
        self.session_id = getattr(result_msg, "session_id", None)
        self.total_cost += cost

        result = SessionResult(
            text=text, cost=cost, result_msg=result_msg,
            session_id=self.session_id,
        )
        self.last_summary = result.summary
        logger.info("State package session: session_id=%s, cost=$%.2f, total=$%.2f",
                     self.session_id, cost, self.total_cost)
        return result

    def _build_state_package(self, feedback: str) -> str:
        """Build compressed context for a fresh session when resume fails."""
        lines = [
            "You were building a product and are continuing after a review round.",
            "",
            f"Intent: {self.intent}",
            "",
        ]
        if self.last_summary:
            lines.append(f"What you built so far: {self.last_summary}")
            lines.append("")
        if self.base_sha:
            lines.append(f"Your code is committed in the project directory.")
            lines.append("")
        lines.append("Feedback from the latest review:")
        lines.append(feedback)
        lines.append("")
        lines.append("Continue fixing the issues above. Your code is in the project directory.")
        return "\n".join(lines)

    def checkpoint(
        self,
        candidate_sha: str,
        *,
        findings: list[dict[str, Any]] | None = None,
        state: str = "building",
        certifier_outcome: str | None = None,
    ) -> None:
        """Write durable checkpoint for crash recovery."""
        cp = SessionCheckpoint(
            session_id=self.session_id,
            base_sha=self.base_sha,
            round=self.round,
            state=state,
            certifier_outcome=certifier_outcome,
            candidate_sha=candidate_sha,
            intent=self.intent,
            last_summary=self.last_summary,
            findings=findings,
            cost_so_far=self.total_cost,
        )
        cp.save(self._checkpoint_dir / "checkpoint.json")
        logger.debug("Checkpoint saved: round=%d, state=%s, sha=%s",
                      self.round, state, candidate_sha[:8] if candidate_sha else "none")

    def load_checkpoint(self) -> SessionCheckpoint | None:
        """Load last checkpoint for crash recovery."""
        return SessionCheckpoint.load(self._checkpoint_dir / "checkpoint.json")

    def _get_head_sha(self) -> str:
        """Get current HEAD SHA."""
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.project_dir, capture_output=True, text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
