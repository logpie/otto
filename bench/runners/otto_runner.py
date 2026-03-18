"""Otto runner — full Otto pipeline (testgen + coding + verification)."""

import asyncio
import subprocess
import time
from pathlib import Path

from otto.bench import BenchRunner, TaskResult


class OttoRunner(BenchRunner):
    """Run tasks through the full Otto pipeline."""

    name = "otto"

    async def run_task(self, repo_path: Path, spec: str, timeout: int) -> TaskResult:
        """Run a task via otto add + otto run."""
        from otto.config import create_config, load_config
        from otto.runner import run_task as otto_run_task
        from otto.tasks import load_tasks

        # Initialize otto in the repo if needed
        config_path = repo_path / "otto.yaml"
        if not config_path.exists():
            create_config(repo_path)
        config = load_config(config_path)
        config["verify_timeout"] = timeout

        tasks_file = repo_path / "tasks.yaml"

        # Create a task dict matching otto's expected format
        import uuid
        key = uuid.uuid4().hex[:12]
        task = {
            "id": 1,
            "key": key,
            "prompt": spec,
            "status": "pending",
        }

        # Generate rubric
        try:
            from otto.rubric import generate_rubric
            rubric = generate_rubric(spec, repo_path)
            if rubric:
                task["rubric"] = rubric
        except Exception:
            pass  # proceed without rubric

        start = time.monotonic()
        try:
            success = await otto_run_task(task, config, repo_path, tasks_file)
        except Exception as e:
            return TaskResult(passed=False, error=str(e))
        elapsed = time.monotonic() - start

        # Extract cost/tokens from task file
        cost = 0.0
        tokens = 0
        retries = 0
        try:
            tasks = load_tasks(tasks_file)
            if tasks:
                t = tasks[0]
                cost = t.get("cost_usd", 0.0)
                retries = t.get("attempts", 0)
        except Exception:
            pass

        return TaskResult(
            passed=success,
            cost_usd=cost,
            time_s=round(elapsed, 1),
            tokens=tokens,
            retries=retries,
        )
