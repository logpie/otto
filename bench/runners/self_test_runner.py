"""Self-test runner — Claude writes its own tests then implements."""

import json
import subprocess
import time
from pathlib import Path

from otto.bench import BenchRunner, TaskResult


class SelfTestRunner(BenchRunner):
    """Write tests → implement to pass tests. Same agent, no adversarial separation."""

    name = "self-test"

    async def run_task(self, repo_path: Path, spec: str, timeout: int) -> TaskResult:
        total_cost = 0.0
        total_tokens = 0

        start = time.monotonic()

        # Step 1: Write tests
        test_result = self._claude_call(
            repo_path,
            f"Write comprehensive pytest tests for the following feature. "
            f"Only write tests, do NOT implement the feature.\n\n{spec}",
            timeout=timeout,
        )
        total_cost += test_result.get("cost", 0.0)
        total_tokens += test_result.get("tokens", 0)

        if test_result.get("error"):
            return TaskResult(
                passed=False,
                cost_usd=total_cost,
                time_s=round(time.monotonic() - start, 1),
                tokens=total_tokens,
                error=test_result["error"],
            )

        # Step 2: Implement to pass tests
        impl_result = self._claude_call(
            repo_path,
            f"Implement the following feature to make all existing tests pass:\n\n{spec}",
            timeout=timeout,
        )
        total_cost += impl_result.get("cost", 0.0)
        total_tokens += impl_result.get("tokens", 0)

        elapsed = time.monotonic() - start

        return TaskResult(
            passed=True,  # validator determines actual pass/fail
            cost_usd=total_cost,
            time_s=round(elapsed, 1),
            tokens=total_tokens,
            error=impl_result.get("error", ""),
        )

    @staticmethod
    def _claude_call(repo_path: Path, prompt: str, timeout: int) -> dict:
        """Make a claude -p call and return {cost, tokens, error}."""
        try:
            result = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "json"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            cost = 0.0
            tokens = 0
            if result.stdout.strip():
                try:
                    data = json.loads(result.stdout)
                    cost = data.get("cost_usd", 0.0)
                    tokens = data.get("total_input_tokens", 0) + data.get("total_output_tokens", 0)
                except (json.JSONDecodeError, TypeError):
                    pass
            error = "" if result.returncode == 0 else result.stderr[-500:]
            return {"cost": cost, "tokens": tokens, "error": error}
        except subprocess.TimeoutExpired:
            return {"cost": 0, "tokens": 0, "error": f"Timeout after {timeout}s"}
        except FileNotFoundError:
            return {"cost": 0, "tokens": 0, "error": "claude CLI not found"}
