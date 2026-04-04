"""Certify MCP tool — expose certifier as a tool the coding agent can call.

The agent calls certify() when it believes the product is ready for user testing.
The tool snapshots the current code, runs the certifier in an isolated worktree,
and returns coarse actionable findings.

This is Variant A of the agent-driven build: the agent drives the loop.
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("otto.certifier.mcp_tool")


class CertifyTool:
    """Stateful certify() tool for a coding agent session.

    Not a real MCP server — injected as a tool definition into the agent's
    SDK options. The agent calls it like any other tool.
    """

    def __init__(
        self,
        project_dir: Path,
        intent: str,
        config: dict[str, Any],
        *,
        max_calls: int = 5,
        cache_dir: Path | None = None,
    ):
        self.project_dir = project_dir
        self.intent = intent
        self.config = config
        self.max_calls = max_calls
        self.cache_dir = cache_dir
        self.call_count = 0
        self.results: list[dict[str, Any]] = []

    def tool_definition(self) -> dict[str, Any]:
        """Tool definition for the SDK."""
        return {
            "name": "certify",
            "description": (
                "Submit your current code for product certification. "
                "Simulates real users testing your product. "
                "Takes several minutes. Returns: {status, issues, warnings}. "
                "status='passed' means done. status='failed' means fix issues. "
                "status='error' means infrastructure failed (not your code)."
            ),
            "input_schema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        }

    def __call__(self) -> dict[str, Any]:
        """Execute certification. Called when agent uses the certify() tool."""
        from otto.certifier.isolated import run_isolated_certifier
        from otto.certifier.report import CertificationOutcome

        self.call_count += 1
        if self.call_count > self.max_calls:
            return {
                "status": "error",
                "message": f"Maximum certification calls ({self.max_calls}) exceeded.",
            }

        logger.info("certify() called (call %d/%d)", self.call_count, self.max_calls)

        # Snapshot current work
        candidate_sha = self._snapshot()

        # Run certifier in isolated worktree
        start = time.monotonic()
        try:
            report = run_isolated_certifier(
                intent=self.intent,
                candidate_sha=candidate_sha,
                project_dir=self.project_dir,
                config=self.config,
                cache_dir=self.cache_dir,
                port_override=self.config.get("port_override"),
            )
        except Exception as exc:
            logger.exception("certify() tool failed")
            result = {
                "status": "error",
                "message": f"Certification infrastructure failed: {exc}",
            }
            self.results.append(result)
            return result

        duration = round(time.monotonic() - start, 1)

        # Map outcome to coarse status
        if report.outcome == CertificationOutcome.PASSED:
            status = "passed"
        elif report.outcome == CertificationOutcome.BLOCKED:
            status = "error"
        elif hasattr(CertificationOutcome, "INFRA_ERROR") and report.outcome == CertificationOutcome.INFRA_ERROR:
            status = "error"
        else:
            status = "failed"

        # Coarse output — don't expose full certifier schema
        issues = []
        for f in report.critical_findings():
            issues.append({
                "what": f.description,
                "detail": f.diagnosis,
                "suggestion": f.fix_suggestion,
            })

        warnings = []
        for f in report.break_findings():
            warnings.append(f"{f.description}")

        result = {
            "status": status,
            "issues": issues,
            "warnings": warnings,
            "duration_s": duration,
            "cost_usd": report.cost_usd,
        }
        if status == "error":
            # Include error details for non-actionable failures
            blocked_tiers = [t for t in report.tiers if t.blocked_by]
            if blocked_tiers:
                result["message"] = f"Testing infrastructure failed: {blocked_tiers[0].blocked_by}"
            elif report.findings:
                result["message"] = report.findings[0].description

        self.results.append(result)
        logger.info("certify() result: status=%s, %d issues, %d warnings, %.1fs",
                     status, len(issues), len(warnings), duration)
        return result

    def _snapshot(self) -> str:
        """Commit current state as a candidate ref."""
        subprocess.run(["git", "add", "-A"], cwd=self.project_dir, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m",
             f"otto: certify candidate {self.call_count}"],
            cwd=self.project_dir, capture_output=True,
        )
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.project_dir, capture_output=True, text=True,
        )
        return result.stdout.strip()
