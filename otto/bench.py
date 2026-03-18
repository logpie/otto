"""Otto benchmark system — measure and compare pipeline effectiveness."""

import json
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class BenchTask:
    """A benchmark task definition (loaded from YAML)."""

    name: str
    repo: str
    commit: str
    difficulty: str  # easy | medium | hard
    spec: str
    validator: str  # relative path to validator dir
    timeout: int = 300
    tags: list[str] = field(default_factory=list)


@dataclass
class TaskResult:
    """Result of running a single benchmark task."""

    passed: bool
    cost_usd: float = 0.0
    time_s: float = 0.0
    tokens: int = 0
    retries: int = 0
    mutation_score: float = 0.0
    coverage_delta: float = 0.0
    testgen_compiled: bool = False
    tdd_check: bool = False
    error: str = ""


@dataclass
class BenchRun:
    """Complete results from a benchmark run."""

    run_id: str
    label: str
    runner: str
    otto_commit: str
    tasks: dict[str, TaskResult]
    timestamp: str = ""

    @property
    def summary(self) -> dict[str, Any]:
        """Compute aggregate metrics from individual task results."""
        total = len(self.tasks)
        if total == 0:
            return {"success_rate": 0, "total_cost": 0, "total_time_s": 0}

        passed = sum(1 for r in self.tasks.values() if r.passed)
        total_cost = sum(r.cost_usd for r in self.tasks.values())
        total_time = sum(r.time_s for r in self.tasks.values())
        total_tokens = sum(r.tokens for r in self.tasks.values())

        mutation_scores = [r.mutation_score for r in self.tasks.values()
                          if r.passed and r.mutation_score > 0]
        mean_mutation = (sum(mutation_scores) / len(mutation_scores)
                         if mutation_scores else 0.0)

        success_rate = passed / total if total > 0 else 0.0
        cost_per_success = total_cost / passed if passed > 0 else float("inf")
        time_per_success = total_time / passed if passed > 0 else float("inf")
        token_efficiency = (passed / (total_tokens / 1_000_000)
                            if total_tokens > 0 else 0.0)

        return {
            "success_rate": round(success_rate, 4),
            "total_cost": round(total_cost, 2),
            "total_time_s": round(total_time, 1),
            "total_tokens": total_tokens,
            "mean_mutation_score": round(mean_mutation, 3),
            "cost_per_success": round(cost_per_success, 2),
            "time_per_success_s": round(time_per_success, 1),
            "token_efficiency": round(token_efficiency, 2),
            "passed": passed,
            "total": total,
        }


# ---------------------------------------------------------------------------
# Suite & task loading
# ---------------------------------------------------------------------------

def load_bench_task(task_path: Path) -> BenchTask:
    """Load a single benchmark task from a YAML file."""
    data = yaml.safe_load(task_path.read_text())
    return BenchTask(
        name=data["name"],
        repo=data["repo"],
        commit=data["commit"],
        difficulty=data.get("difficulty", "medium"),
        spec=data["spec"],
        validator=data["validator"],
        timeout=data.get("timeout", 300),
        tags=data.get("tags", []),
    )


def load_suite(suite_path: Path) -> list[BenchTask]:
    """Load all tasks referenced in a suite.yaml file.

    suite.yaml format:
        tasks:
          - tasks/easy/add-cli-flag.yaml
          - tasks/medium/add-search-endpoint.yaml
    """
    data = yaml.safe_load(suite_path.read_text())
    bench_dir = suite_path.parent
    tasks = []
    for entry in data.get("tasks", []):
        task_file = bench_dir / entry
        if task_file.exists():
            tasks.append(load_bench_task(task_file))
    return tasks


def filter_tasks(
    tasks: list[BenchTask],
    difficulty: str | None = None,
    tags: list[str] | None = None,
    names: list[str] | None = None,
) -> list[BenchTask]:
    """Filter tasks by difficulty, tags, or names."""
    result = tasks
    if difficulty:
        result = [t for t in result if t.difficulty == difficulty]
    if tags:
        tag_set = set(tags)
        result = [t for t in result if tag_set & set(t.tags)]
    if names:
        name_set = set(names)
        result = [t for t in result if t.name in name_set]
    return result


# ---------------------------------------------------------------------------
# Results serialization
# ---------------------------------------------------------------------------

def save_results(run: BenchRun, results_dir: Path) -> Path:
    """Save a BenchRun to JSON in results_dir. Returns the output path."""
    results_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{run.run_id}.json"
    out_path = results_dir / filename

    data = {
        "run_id": run.run_id,
        "label": run.label,
        "runner": run.runner,
        "otto_commit": run.otto_commit,
        "timestamp": run.timestamp,
        "tasks": {name: asdict(result) for name, result in run.tasks.items()},
        "summary": run.summary,
    }
    out_path.write_text(json.dumps(data, indent=2) + "\n")
    return out_path


def load_results(path: Path) -> BenchRun:
    """Load a BenchRun from a JSON file."""
    data = json.loads(path.read_text())
    tasks = {}
    for name, result_data in data.get("tasks", {}).items():
        tasks[name] = TaskResult(**{
            k: v for k, v in result_data.items()
            if k in TaskResult.__dataclass_fields__
        })
    return BenchRun(
        run_id=data["run_id"],
        label=data["label"],
        runner=data["runner"],
        otto_commit=data.get("otto_commit", ""),
        tasks=tasks,
        timestamp=data.get("timestamp", ""),
    )


def list_results(results_dir: Path) -> list[tuple[str, BenchRun]]:
    """List all saved results, newest first. Returns (filename, run) pairs."""
    if not results_dir.exists():
        return []
    runs = []
    for path in sorted(results_dir.glob("*.json"), reverse=True):
        try:
            run = load_results(path)
            runs.append((path.name, run))
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return runs


# ---------------------------------------------------------------------------
# Comparison & reporting
# ---------------------------------------------------------------------------

def compare_runs(baseline: BenchRun, current: BenchRun) -> str:
    """Generate a formatted comparison report between two runs."""
    bs = baseline.summary
    cs = current.summary

    lines = []
    lines.append(f"Otto Bench — Comparing: {baseline.label} → {current.label}")
    lines.append("━" * 60)

    # Header
    bw = max(len(baseline.label), 12)
    cw = max(len(current.label), 12)
    lines.append(f"{'':24}{baseline.label:>{bw}}  {current.label:>{cw}}  {'delta':>10}")
    lines.append("─" * 60)

    # Success rate
    b_sr = bs["success_rate"] * 100
    c_sr = cs["success_rate"] * 100
    d_sr = c_sr - b_sr
    arrow = "▲" if d_sr > 0 else "▼" if d_sr < 0 else "="
    lines.append(
        f"{'Success rate':24}{b_sr:>{bw}.1f}%  {c_sr:>{cw}.1f}%  "
        f"{arrow} {d_sr:+.1f}%"
    )

    # Cost per success
    b_cps = bs["cost_per_success"]
    c_cps = cs["cost_per_success"]
    if b_cps < float("inf") and c_cps < float("inf"):
        d_cps = c_cps - b_cps
        label = "(better)" if d_cps < 0 else "(worse)" if d_cps > 0 else ""
        arrow = "▼" if d_cps < 0 else "▲" if d_cps > 0 else "="
        lines.append(
            f"{'Cost/success':24}${b_cps:>{bw - 1}.2f}  ${c_cps:>{cw - 1}.2f}  "
            f"{arrow} ${d_cps:+.2f} {label}"
        )

    # Time per success
    b_tps = bs["time_per_success_s"]
    c_tps = cs["time_per_success_s"]
    if b_tps < float("inf") and c_tps < float("inf"):
        d_tps = c_tps - b_tps
        label = "(better)" if d_tps < 0 else "(worse)" if d_tps > 0 else ""
        arrow = "▼" if d_tps < 0 else "▲" if d_tps > 0 else "="
        b_min = b_tps / 60
        c_min = c_tps / 60
        d_min = d_tps / 60
        lines.append(
            f"{'Time/success':24}{b_min:>{bw}.1f}m  {c_min:>{cw}.1f}m  "
            f"{arrow} {d_min:+.1f}m {label}"
        )

    # Mutation score
    b_ms = bs["mean_mutation_score"]
    c_ms = cs["mean_mutation_score"]
    if b_ms > 0 or c_ms > 0:
        d_ms = c_ms - b_ms
        arrow = "▲" if d_ms > 0 else "▼" if d_ms < 0 else "="
        lines.append(
            f"{'Mutation score':24}{b_ms:>{bw}.3f}  {c_ms:>{cw}.3f}  "
            f"{arrow} {d_ms:+.3f}"
        )

    # Token efficiency
    b_te = bs["token_efficiency"]
    c_te = cs["token_efficiency"]
    if b_te > 0 or c_te > 0:
        d_te = c_te - b_te
        arrow = "▲" if d_te > 0 else "▼" if d_te < 0 else "="
        lines.append(
            f"{'Token efficiency':24}{b_te:>{bw}.1f}/M  {c_te:>{cw}.1f}/M  "
            f"{arrow} {d_te:+.1f}"
        )

    lines.append("")

    # Regressions and new passes
    regressions = []
    new_passes = []
    all_task_names = set(baseline.tasks) | set(current.tasks)
    for name in sorted(all_task_names):
        b_pass = baseline.tasks.get(name, TaskResult(passed=False)).passed
        c_pass = current.tasks.get(name, TaskResult(passed=False)).passed
        if b_pass and not c_pass:
            regressions.append(name)
        elif not b_pass and c_pass:
            new_passes.append(name)

    if regressions:
        lines.append(f"Regressions ({len(regressions)}): {', '.join(regressions)}")
    if new_passes:
        lines.append(f"New passes ({len(new_passes)}):  {', '.join(new_passes)}")
    if not regressions and not new_passes:
        lines.append("No task-level changes.")

    return "\n".join(lines)


def cross_runner_report(runs: list[BenchRun]) -> str:
    """Generate a cross-runner comparison table from multiple runs."""
    if not runs:
        return "No runs to compare."

    lines = []
    lines.append("\nCross-runner comparison:")

    # Header
    name_w = 20
    col_w = max(max(len(r.runner) for r in runs), 10)
    header = f"{'':>{name_w}}"
    for r in runs:
        header += f"  {r.runner:>{col_w}}"
    lines.append(header)
    lines.append("─" * (name_w + (col_w + 2) * len(runs)))

    # Success rate
    row = f"{'Success rate':>{name_w}}"
    for r in runs:
        sr = r.summary["success_rate"] * 100
        row += f"  {sr:>{col_w}.1f}%"
    lines.append(row)

    # Cost/success
    row = f"{'Cost/success':>{name_w}}"
    for r in runs:
        cps = r.summary["cost_per_success"]
        if cps < float("inf"):
            row += f"  ${cps:>{col_w - 1}.2f}"
        else:
            row += f"  {'N/A':>{col_w}}"
    lines.append(row)

    # Total cost
    row = f"{'Total cost':>{name_w}}"
    for r in runs:
        tc = r.summary["total_cost"]
        row += f"  ${tc:>{col_w - 1}.2f}"
    lines.append(row)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Runner base class
# ---------------------------------------------------------------------------

class BenchRunner:
    """Base class for benchmark runners."""

    name: str = "base"

    async def run_task(
        self, repo_path: Path, spec: str, timeout: int,
    ) -> TaskResult:
        """Run a task against a repo and return results.

        The repo is already checked out at the correct commit. The runner
        should implement the feature described in spec and return metrics.
        """
        raise NotImplementedError

    def cleanup(self, repo_path: Path) -> None:
        """Optional post-task cleanup."""
        pass


# ---------------------------------------------------------------------------
# Repo management
# ---------------------------------------------------------------------------

def prepare_repo(
    bench_dir: Path, task: BenchTask, work_dir: Path,
) -> Path:
    """Clone/checkout a task's repo at the specified commit into work_dir.

    Returns the path to the prepared repo.
    """
    source_repo = bench_dir / "repos" / task.repo
    repo_dest = work_dir / task.repo

    if repo_dest.exists():
        # Reset existing checkout
        subprocess.run(
            ["git", "checkout", task.commit],
            cwd=repo_dest, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "clean", "-fdx"],
            cwd=repo_dest, capture_output=True,
        )
    else:
        # Clone from local bench repo
        repo_dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", str(source_repo), str(repo_dest)],
            capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "checkout", task.commit],
            cwd=repo_dest, capture_output=True, check=True,
        )

    return repo_dest


def run_validator(
    bench_dir: Path, task: BenchTask, repo_path: Path,
) -> tuple[bool, str]:
    """Run validator tests against a repo. Returns (passed, output)."""
    validator_dir = bench_dir / task.validator
    if not validator_dir.exists():
        return False, f"Validator dir not found: {task.validator}"

    # Copy validator tests into the repo
    import shutil
    import tempfile

    # Find test files in validator dir
    test_files = list(validator_dir.glob("test_*.py"))
    if not test_files:
        test_files = list(validator_dir.glob("*.py"))
    if not test_files:
        return False, "No validator test files found"

    # Copy validator tests to a temporary location in the repo
    tests_dir = repo_path / "tests"
    tests_dir.mkdir(exist_ok=True)

    copied = []
    for tf in test_files:
        dest = tests_dir / f"_bench_validator_{tf.name}"
        shutil.copy2(tf, dest)
        copied.append(dest)

    try:
        # Run validator tests
        test_paths = " ".join(str(c.relative_to(repo_path)) for c in copied)
        result = subprocess.run(
            f"pytest {test_paths} -v",
            shell=True,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=task.timeout,
            env=_bench_env(),
        )
        return result.returncode == 0, result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return False, f"Validator timeout after {task.timeout}s"
    finally:
        # Clean up copied validators
        for c in copied:
            c.unlink(missing_ok=True)


def _bench_env() -> dict[str, str]:
    """Environment dict with Python venv bin on PATH."""
    import os
    import sys

    venv_bin = str(Path(sys.executable).parent)
    env = os.environ.copy()
    existing = env.get("PATH", "")
    if venv_bin not in existing.split(os.pathsep):
        env["PATH"] = venv_bin + os.pathsep + existing
    return env


def get_otto_commit() -> str:
    """Get current otto repo commit SHA."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent.parent,
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"


# ---------------------------------------------------------------------------
# Benchmark execution
# ---------------------------------------------------------------------------

async def run_bench(
    bench_dir: Path,
    tasks: list[BenchTask],
    runner: BenchRunner,
    label: str = "",
) -> BenchRun:
    """Execute a benchmark run: run each task through the runner, then validate.

    Returns a BenchRun with all results.
    """
    import tempfile

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    if not label:
        label = f"{runner.name}-{run_id[:8]}"

    results: dict[str, TaskResult] = {}
    work_dir = Path(tempfile.mkdtemp(prefix="otto-bench-"))

    try:
        for task in tasks:
            print(f"\n{'─' * 50}")
            print(f"  [{runner.name}] {task.name} ({task.difficulty})")
            print(f"{'─' * 50}")

            try:
                # Prepare repo at the correct commit
                repo_path = prepare_repo(bench_dir, task, work_dir)

                # Run the runner
                task_start = time.monotonic()
                result = await runner.run_task(repo_path, task.spec, task.timeout)
                elapsed = time.monotonic() - task_start

                # Override time with measured wall clock if runner didn't set it
                if result.time_s == 0:
                    result.time_s = round(elapsed, 1)

                # Run validator to determine pass/fail
                validator_passed, validator_output = run_validator(
                    bench_dir, task, repo_path,
                )
                result.passed = validator_passed

                if validator_passed:
                    print(f"  ✓ PASS  ({result.time_s:.0f}s, ${result.cost_usd:.2f})")
                else:
                    print(f"  ✗ FAIL  ({result.time_s:.0f}s, ${result.cost_usd:.2f})")
                    if not result.error:
                        # Truncate validator output for the error field
                        result.error = validator_output[-500:] if validator_output else "validator failed"

            except Exception as e:
                result = TaskResult(passed=False, error=str(e))
                print(f"  ✗ ERROR: {e}")

            results[task.name] = result

            # Clean up for next task
            try:
                runner.cleanup(repo_path)
            except Exception:
                pass

    finally:
        import shutil
        shutil.rmtree(work_dir, ignore_errors=True)

    return BenchRun(
        run_id=run_id,
        label=label,
        runner=runner.name,
        otto_commit=get_otto_commit(),
        tasks=results,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
