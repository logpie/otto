"""Ralph runner — implement → test → fix loop (max 3 iterations)."""

import json
import subprocess
import time
from pathlib import Path

from otto.bench import BenchRunner, TaskResult


class RalphRunner(BenchRunner):
    """Implement → run tests → fix loop, up to 3 iterations."""

    name = "ralph"
    max_loops = 3

    async def run_task(self, repo_path: Path, spec: str, timeout: int) -> TaskResult:
        total_cost = 0.0
        total_tokens = 0
        retries = 0

        start = time.monotonic()

        # Detect test command
        from otto.config import detect_test_command
        test_cmd = detect_test_command(repo_path) or "pytest"

        # Step 1: Implement
        impl_result = self._claude_call(
            repo_path, f"Implement the following feature:\n\n{spec}",
            timeout=timeout,
        )
        total_cost += impl_result.get("cost", 0.0)
        total_tokens += impl_result.get("tokens", 0)

        if impl_result.get("error"):
            return TaskResult(
                passed=False,
                cost_usd=total_cost,
                time_s=round(time.monotonic() - start, 1),
                tokens=total_tokens,
                error=impl_result["error"],
            )

        # Step 2: Test → Fix loop
        for i in range(self.max_loops):
            test_result = subprocess.run(
                test_cmd, shell=True, cwd=repo_path,
                capture_output=True, text=True, timeout=timeout,
            )
            if test_result.returncode == 0:
                break

            retries += 1
            failures = (test_result.stdout + test_result.stderr)[-2000:]
            fix_result = self._claude_call(
                repo_path,
                f"The tests are failing. Fix the implementation.\n\nTest output:\n{failures}",
                timeout=timeout,
            )
            total_cost += fix_result.get("cost", 0.0)
            total_tokens += fix_result.get("tokens", 0)

        elapsed = time.monotonic() - start

        return TaskResult(
            passed=True,  # validator determines actual pass/fail
            cost_usd=total_cost,
            time_s=round(elapsed, 1),
            tokens=total_tokens,
            retries=retries,
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
