"""Bare Claude Code runner — single claude -p call, no tests, no verification."""

import json
import subprocess
import time
from pathlib import Path

from otto.bench import BenchRunner, TaskResult


class BareClaudeRunner(BenchRunner):
    """Single claude -p call. No TDD, no verification loop."""

    name = "bare-cc"

    async def run_task(self, repo_path: Path, spec: str, timeout: int) -> TaskResult:
        prompt = f"Implement the following feature in this codebase:\n\n{spec}"

        start = time.monotonic()
        try:
            result = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "json"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            elapsed = time.monotonic() - start

            cost = 0.0
            tokens = 0
            if result.returncode == 0 and result.stdout.strip():
                try:
                    data = json.loads(result.stdout)
                    cost = data.get("cost_usd", 0.0)
                    tokens = data.get("total_input_tokens", 0) + data.get("total_output_tokens", 0)
                except (json.JSONDecodeError, TypeError):
                    pass

            return TaskResult(
                passed=True,  # validator determines actual pass/fail
                cost_usd=cost,
                time_s=round(elapsed, 1),
                tokens=tokens,
                error="" if result.returncode == 0 else result.stderr[-500:],
            )
        except subprocess.TimeoutExpired:
            return TaskResult(
                passed=False,
                time_s=float(timeout),
                error=f"Timeout after {timeout}s",
            )
        except FileNotFoundError:
            return TaskResult(
                passed=False,
                error="claude CLI not found",
            )
