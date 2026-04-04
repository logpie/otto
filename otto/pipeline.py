"""Otto build pipeline — plan, build, certify, fix, verify."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BuildMode:
    """Resolved build mode — requested then finalized after planning."""
    use_planner: bool = False
    parallel: bool = False
    grounding: str = "intent"  # "intent" or "spec"

    def finalize(self, plan_mode: str) -> BuildMode:
        """Finalize mode after planner output is known."""
        return BuildMode(
            use_planner=self.use_planner,
            parallel=self.parallel and plan_mode != "single_task",
            grounding=self.grounding,
        )


@dataclass
class BuildResult:
    """Result of the entire build pipeline."""
    passed: bool
    build_id: str
    rounds: int = 1
    total_cost: float = 0.0
    journeys: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    tasks_passed: int = 0
    tasks_failed: int = 0


def resolve_build_mode(config: dict[str, Any]) -> BuildMode:
    """Resolve build mode from config + flags."""
    execution_mode = str(config.get("execution_mode", "monolithic") or "monolithic").strip().lower()
    use_planner = config.get("use_planner", execution_mode == "planned")
    parallel = execution_mode == "planned" and int(config.get("max_parallel", 1)) > 1
    grounding = "spec" if use_planner else "intent"
    return BuildMode(use_planner=use_planner, parallel=parallel, grounding=grounding)


async def build_product(
    intent: str,
    project_dir: Path,
    config: dict[str, Any],
    *,
    on_progress: Any = None,
) -> BuildResult:
    """The entire otto build pipeline: plan -> build -> certify -> fix -> verify."""
    import asyncio
    from otto.tasks import add_tasks, load_tasks

    build_id = f"build-{int(time.time())}-{os.getpid()}"
    build_dir = project_dir / "otto_logs" / "builds" / build_id
    build_dir.mkdir(parents=True, exist_ok=True)
    total_cost = 0.0

    mode = resolve_build_mode(config)
    tasks_path = project_dir / "tasks.yaml"

    # Grounding: write intent to project root for reference
    grounding_path = project_dir / "intent.md"
    if not grounding_path.exists():
        grounding_path.write_text(intent)

    # Plan (optional)
    if mode.use_planner:
        from otto.product_planner import run_product_planner
        plan = await run_product_planner(intent, project_dir, config)
        total_cost += plan.cost_usd
        mode = mode.finalize(plan.mode)

        # Certifier grounding = product-spec.md (what planner wrote)
        certifier_intent = (plan.product_spec_path or grounding_path).read_text()

        tasks = [
            {"prompt": t.prompt, "depends_on": t.depends_on if t.depends_on else None}
            for t in plan.tasks
        ]

        # Persist plan manifest (immutable sidecar)
        manifest = {
            "build_id": build_id,
            "mode": mode.grounding,
            "fingerprint": _plan_fingerprint(plan, project_dir),
            "task_count": len(tasks),
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        (build_dir / "plan-manifest.json").write_text(json.dumps(manifest, indent=2))
    else:
        # Monolithic: intent IS the task and the grounding
        certifier_intent = intent
        tasks = [{"prompt": f"Build the product described below.\n\n{intent}"}]

    # Persist artifacts before worktree creation
    add_tasks(tasks_path, tasks)

    _commit_artifacts(project_dir)

    # Build
    from otto.orchestrator import run_per
    exit_code = await run_per(config, tasks_path, project_dir)

    all_tasks = load_tasks(tasks_path) if tasks_path.exists() else []
    tasks_passed = sum(1 for t in all_tasks if t.get("status") == "passed")
    tasks_failed = sum(1 for t in all_tasks if t.get("status") in ("failed", "merge_failed"))

    if exit_code != 0 and tasks_passed == 0:
        return BuildResult(
            passed=False, build_id=build_id, error="build failed",
            total_cost=total_cost, tasks_passed=tasks_passed, tasks_failed=tasks_failed,
        )

    # Certify -> Fix -> Verify
    if not config.get("skip_product_qa"):
        from otto.verification import run_product_verification
        config.setdefault("proof_of_work", True)

        loop = asyncio.get_event_loop()
        verify_result = await loop.run_in_executor(
            None,
            lambda: _run_verification_sync(
                certifier_intent, project_dir, tasks_path, config,
            ),
        )
        total_cost += verify_result.get("total_cost", 0.0)

        return BuildResult(
            passed=verify_result.get("product_passed", False),
            build_id=build_id,
            rounds=verify_result.get("rounds", 1),
            total_cost=total_cost,
            journeys=verify_result.get("journeys", []),
            tasks_passed=tasks_passed,
            tasks_failed=tasks_failed,
        )

    return BuildResult(
        passed=True, build_id=build_id, total_cost=total_cost,
        tasks_passed=tasks_passed, tasks_failed=tasks_failed,
    )


def _run_verification_sync(
    intent: str, project_dir: Path, tasks_path: Path, config: dict[str, Any],
) -> dict[str, Any]:
    """Run verification synchronously (certifier uses asyncio.run internally)."""
    import asyncio
    from otto.verification import run_product_verification
    return asyncio.run(run_product_verification(
        product_spec_path=project_dir / "intent.md",
        project_dir=project_dir,
        tasks_path=tasks_path,
        config=config,
        intent=intent,
    ))


def _plan_fingerprint(plan: Any, project_dir: Path) -> str:
    """Compute deterministic fingerprint from parsed plan structure."""
    canonical = json.dumps({
        "tasks": [{"prompt": t.prompt, "depends_on": t.depends_on or []} for t in plan.tasks],
        "spec": (plan.product_spec_path.read_text() if plan.product_spec_path and plan.product_spec_path.exists() else ""),
        "arch": ((project_dir / "architecture.md").read_text() if (project_dir / "architecture.md").exists() else ""),
    }, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def _commit_artifacts(project_dir: Path) -> None:
    """Commit build artifacts so worktrees can see them."""
    files = ["tasks.yaml"]
    for name in ["intent.md", "product-spec.md", "architecture.md"]:
        if (project_dir / name).exists():
            files.append(name)
    subprocess.run(["git", "add"] + files, cwd=project_dir, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "otto: build artifacts"],
        cwd=project_dir, capture_output=True,
    )
