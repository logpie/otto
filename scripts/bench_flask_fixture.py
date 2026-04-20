"""Build a Flask API fixture (different domain than P6 inventory CLI).

Phases:
1. otto build a tiny Flask todo REST API
2. queue 2 improves: 'tags' + 'priority' (both touch /todos endpoints + data)
3. Wait for improves
4. Save branches as bench-fixtures/flask-api-branches.bundle
5. Test F13 on it (consolidated salvage), record results

Cost: ~$5-10 estimated. Visible as background task.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OTTO_BIN = REPO_ROOT / ".venv" / "bin" / "otto"
RESULTS_DIR = REPO_ROOT / "bench-results"
FIXTURES_DIR = REPO_ROOT / "bench-fixtures"


def log(msg: str) -> None:
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def setup_repo() -> Path:
    base = Path(tempfile.mkdtemp(prefix="bench-flask-"))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.email", "f@f"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.name", "F"], cwd=base, check=True)
    (base / "README.md").write_text("# Flask todo API\n")
    subprocess.run(["git", "add", "."], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=base, check=True)
    return base


def otto_run(repo: Path, *args: str, timeout: float = 1800) -> tuple[int, str]:
    """Run otto in repo. Returns (rc, output)."""
    r = subprocess.run(
        [str(OTTO_BIN), *args],
        cwd=repo, capture_output=True, text=True, timeout=timeout,
        env={**os.environ},
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def wait_for_queue_terminal(repo: Path, timeout: float = 1800) -> dict[str, str]:
    """Poll until every queued task is in a terminal state."""
    state_path = repo / ".otto-queue-state.json"
    start = time.time()
    while True:
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text())
                tasks = state.get("tasks", {})
                statuses = {tid: ts.get("status") for tid, ts in tasks.items()}
                if statuses and all(s in ("done", "failed", "cancelled") for s in statuses.values()):
                    return statuses
            except Exception:
                pass
        if time.time() - start > timeout:
            raise TimeoutError(f"timeout after {timeout}s")
        time.sleep(20)


def main() -> int:
    log("=== Building Flask API fixture ===")
    repo = setup_repo()
    log(f"repo: {repo}")

    # Phase 1: build base via queue
    log("Phase 1: queue base build")
    rc, out = otto_run(repo, "queue", "build", "--as", "base", "--",
                       "--fast", "--no-qa",
                       "Build a Flask REST API in app.py for a tiny todo list. "
                       "Endpoints: POST /todos {text} creates a todo (returns id). "
                       "GET /todos lists all (id, text, done, created_at). "
                       "POST /todos/<id>/done marks done. "
                       "DELETE /todos/<id> deletes. "
                       "Persist as todos.json. Use stdlib only (no SQLAlchemy). "
                       "Include test_app.py with at least 4 tests covering "
                       "create + list + done + delete flow.")
    if rc != 0:
        log(f"FAILED to enqueue base: {out[:500]}")
        return 1

    log("Phase 1: starting watcher (concurrent=1 for base)")
    watcher = subprocess.Popen(
        [str(OTTO_BIN), "queue", "run", "--concurrent", "1"],
        cwd=repo,
        stdout=open(repo / ".watcher.log", "wb"),
        stderr=subprocess.STDOUT,
    )
    time.sleep(1.0)
    try:
        statuses = wait_for_queue_terminal(repo, timeout=900)
        log(f"Phase 1 done: {statuses}")
    finally:
        watcher.terminate()
        try:
            watcher.wait(timeout=10)
        except subprocess.TimeoutExpired:
            watcher.kill()
            watcher.wait()

    if statuses.get("base") != "done":
        log(f"FAILED: base did not reach done")
        return 1

    # Merge base into main
    log("Merging base into main")
    rc, out = otto_run(repo, "merge", "--all", "--no-certify", "--cleanup-on-success")
    if rc != 0:
        log(f"FAILED to merge base: {out[:500]}")
        return 1

    # Phase 2: queue 2 parallel improves
    log("Phase 2: queue 2 parallel improves")
    improves = [
        ("imp-tags", "Add tags to todos. Each todo has a 'tags' field (list of strings, default []). "
                     "POST /todos accepts a tags field in the request body. "
                     "GET /todos?tag=<x> filters by tag. "
                     "Add at least 2 tests for the tag filter."),
        ("imp-priority", "Add priority to todos. Each todo has a 'priority' field "
                          "('low'|'med'|'high', default 'med'). POST /todos accepts priority. "
                          "GET /todos sorts by priority desc by default. "
                          "Add at least 2 tests for priority ordering."),
    ]
    for tid, intent in improves:
        rc, out = otto_run(repo, "queue", "improve", "feature", "--as", tid, "--", "-n", "1", intent)
        if rc != 0:
            log(f"FAILED to enqueue {tid}: {out[:300]}")
            return 1

    log("Phase 2: starting watcher (concurrent=2)")
    watcher = subprocess.Popen(
        [str(OTTO_BIN), "queue", "run", "--concurrent", "2"],
        cwd=repo,
        stdout=open(repo / ".watcher.log", "wb"),
        stderr=subprocess.STDOUT,
    )
    time.sleep(1.0)
    try:
        statuses = wait_for_queue_terminal(repo, timeout=1800)
        log(f"Phase 2 done: {statuses}")
    finally:
        watcher.terminate()
        try:
            watcher.wait(timeout=10)
        except subprocess.TimeoutExpired:
            watcher.kill()
            watcher.wait()

    # Save fixture: pre-merge state on main, plus the 2 improve branches
    log("Saving fixture as bench-fixtures/flask-api-branches.bundle")
    pre_merge_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "update-ref", "refs/fixtures/main", pre_merge_head],
        cwd=repo, check=True,
    )
    bundle_args = ["git", "bundle", "create", str(FIXTURES_DIR / "flask-api-branches.bundle"),
                   "refs/fixtures/main"]
    for tid, _ in improves:
        bundle_args.append(f"improve/{tid}-2026-04-20")
    bundle_args.append("build/base-2026-04-20")
    subprocess.run(bundle_args, cwd=repo, check=True)
    log(f"Fixture saved")

    # Phase 3: test F13 with consolidated mode
    log("Phase 3: enable merge_mode=consolidated and run F13 salvage")
    yaml_path = repo / "otto.yaml"
    yaml_text = yaml_path.read_text() if yaml_path.exists() else "default_branch: main\n"
    if "merge_mode" not in yaml_text:
        yaml_path.write_text(yaml_text.rstrip() + "\n\nqueue:\n  merge_mode: consolidated\n")
    subprocess.run(["git", "add", "otto.yaml"], cwd=repo, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "enable consolidated merge"],
                   cwd=repo, capture_output=True)

    branches_to_merge = [f"improve/{tid}-2026-04-20" for tid, _ in improves]
    log(f"Running consolidated salvage of {branches_to_merge}")
    t0 = time.time()
    rc, out = otto_run(repo, "merge", *branches_to_merge, "--no-certify", timeout=3600)
    wall = time.time() - t0
    log(f"Salvage done in {wall:.0f}s, rc={rc}")

    cost = 0.0
    for sf in (repo / "otto_logs" / "merge").glob("merge-*/state.json"):
        try:
            d = json.loads(sf.read_text())
            for o in d.get("outcomes", []):
                note = o.get("note") or ""
                if "cost $" in note:
                    bit = note.split("cost $")[1].split(",")[0].split(")")[0]
                    cost += float(bit)
        except Exception:
            pass

    log_path = repo / "otto_logs" / "merge" / "conflict-agent-agentic.log"
    tool_counts: dict[str, int] = {}
    if log_path.exists():
        for line in log_path.read_text().splitlines():
            m = re.match(r"\[\s*\d+\.\d+s\]\s*●\s*(\S+)", line)
            if m:
                tool_counts[m.group(1)] = tool_counts.get(m.group(1), 0) + 1

    # Check final state
    markers_remain = False
    for f in repo.rglob("*"):
        if f.is_file() and ".git" not in f.parts and "otto_logs" not in f.parts:
            try:
                if any(line.startswith("<<<<<<<") for line in f.read_text().splitlines()):
                    markers_remain = True
            except Exception:
                pass

    # Try running the merged tests
    test_rc = subprocess.run(
        ["python3", "-m", "pytest", "test_app.py", "-q"],
        cwd=repo, capture_output=True, text=True, timeout=60,
    )

    res = {
        "name": "F13-flask-api",
        "scenario": "Flask todo REST API + 2 parallel improves (tags, priority). Real LLM-built code.",
        "phase_3_salvage_wall_seconds": wall,
        "phase_3_salvage_cost_usd": cost,
        "rc": rc,
        "tool_counts": tool_counts,
        "markers_remain": markers_remain,
        "merged_tests_pass": test_rc.returncode == 0,
        "merged_tests_output_tail": test_rc.stdout[-500:] if test_rc.stdout else "",
        "repo": str(repo),
        "merge_log_tail": out.strip().split("\n")[-7:],
    }
    print()
    print(json.dumps(res, indent=2))

    out_path = RESULTS_DIR / "F13-flask-api.json"
    out_path.write_text(json.dumps(res, indent=2))
    log(f"Saved {out_path}")

    return 0 if rc == 0 and not markers_remain else 1


if __name__ == "__main__":
    sys.exit(main())
