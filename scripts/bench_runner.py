"""Benchmark runner for parallel-otto on real complex products.

Each benchmark:
1. Sets up a tmp git repo
2. Runs the real `otto` CLI (no fake-otto stub)
3. Captures: wall time, $$ cost, success rate, cert outcome, log paths
4. Writes a structured `bench-result.json` per benchmark to bench-results/

Usage:
    .venv/bin/python scripts/bench_runner.py P1
    .venv/bin/python scripts/bench_runner.py all
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
OTTO_BIN = REPO_ROOT / ".venv" / "bin" / "otto"
RESULTS_DIR = REPO_ROOT / "bench-results"


# ------------------- helpers -------------------

def hr(label: str = "") -> None:
    pad = max(0, 76 - len(label))
    print(f"\n══════ {label} {'═' * pad}", flush=True)


def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S")
    print(f"  [{ts}] {msg}", flush=True)


def make_repo(prefix: str) -> Path:
    base = Path(tempfile.mkdtemp(prefix=prefix))
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.email", "bench@otto"], cwd=base, check=True)
    subprocess.run(["git", "config", "user.name", "Bench"], cwd=base, check=True)
    (base / "README.md").write_text("# bench scratch repo\n")
    subprocess.run(["git", "add", "."], cwd=base, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=base, check=True)
    return base


def otto_run(repo: Path, *args: str, env: dict[str, str] | None = None,
             check: bool = True, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    full_env = {**os.environ}
    if env:
        full_env.update(env)
    full_env.pop("OTTO_BIN", None)  # always real otto
    return subprocess.run(
        [str(OTTO_BIN), *args],
        cwd=repo,
        env=full_env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=check,
    )


def start_watcher(repo: Path, *, concurrent: int) -> subprocess.Popen[bytes]:
    log_file = repo / ".watcher.log"
    env = {**os.environ}
    env.pop("OTTO_BIN", None)
    proc = subprocess.Popen(
        [str(OTTO_BIN), "queue", "run", "--concurrent", str(concurrent)],
        cwd=repo, env=env,
        stdout=open(log_file, "wb"),
        stderr=subprocess.STDOUT,
    )
    time.sleep(1.0)  # let lock acquire
    return proc


def stop_watcher(proc: subprocess.Popen[bytes], timeout: float = 10.0) -> None:
    if proc.poll() is None:
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def queue_state(repo: Path) -> dict[str, Any]:
    p = repo / ".otto-queue-state.json"
    if not p.exists():
        return {"tasks": {}}
    return json.loads(p.read_text())


def all_tasks_terminal(repo: Path) -> tuple[bool, dict[str, str]]:
    """Return (all_done, {task_id: status})."""
    state = queue_state(repo)
    statuses = {tid: ts.get("status") for tid, ts in state.get("tasks", {}).items()}
    terminal = {"done", "failed", "cancelled", "removed"}
    return (all(s in terminal for s in statuses.values()) and bool(statuses), statuses)


def wait_for_all_done(repo: Path, *, timeout: float = 1800, interval: float = 30) -> dict[str, str]:
    """Poll until every queued task is terminal. Returns final {id: status}."""
    start = time.time()
    while True:
        ok_, statuses = all_tasks_terminal(repo)
        if ok_:
            return statuses
        if time.time() - start > timeout:
            raise TimeoutError(f"timeout after {timeout}s; statuses: {statuses}")
        time.sleep(interval)


# ------------------- result dataclass -------------------

@dataclass
class TaskMetric:
    id: str
    status: str
    cost_usd: float
    duration_s: float
    branch: str | None = None
    failure_reason: str | None = None


@dataclass
class BenchResult:
    name: str
    started_at: str
    finished_at: str | None = None
    wall_seconds: float = 0.0
    total_cost_usd: float = 0.0
    queue_concurrency: int = 0
    tasks: list[TaskMetric] = field(default_factory=list)
    merge_outcome: str | None = None        # "success" | "conflict" | "failed" | "skipped"
    merge_cost_usd: float = 0.0
    merge_seconds: float = 0.0
    cert_passed: bool | None = None
    notes: list[str] = field(default_factory=list)
    repo_path: str = ""

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, default=str))

    def short_summary(self) -> str:
        lines = [f"  {self.name}: wall={self.wall_seconds:.1f}s, total=${self.total_cost_usd:.2f}"]
        for t in self.tasks:
            lines.append(f"    - {t.id}: {t.status}, ${t.cost_usd:.2f}, {t.duration_s:.0f}s")
        if self.merge_outcome:
            lines.append(
                f"    merge: {self.merge_outcome}, +${self.merge_cost_usd:.2f}, "
                f"+{self.merge_seconds:.0f}s, cert_passed={self.cert_passed}"
            )
        for n in self.notes:
            lines.append(f"    note: {n}")
        return "\n".join(lines)


def collect_metrics(repo: Path, name: str, t_start: float, concurrency: int) -> BenchResult:
    state = queue_state(repo)
    tasks: list[TaskMetric] = []
    total_cost = 0.0
    for tid, ts in state.get("tasks", {}).items():
        cost = float(ts.get("cost_usd") or 0.0)
        total_cost += cost
        tasks.append(TaskMetric(
            id=tid,
            status=ts.get("status") or "unknown",
            cost_usd=cost,
            duration_s=float(ts.get("duration_s") or 0.0),
            branch=ts.get("branch"),
            failure_reason=ts.get("failure_reason"),
        ))
    return BenchResult(
        name=name,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_start)),
        finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        wall_seconds=time.time() - t_start,
        total_cost_usd=total_cost,
        queue_concurrency=concurrency,
        tasks=tasks,
        repo_path=str(repo),
    )


# ------------------- benchmarks -------------------

def bench_p1_todo_parallel_improves(name: str = "P1-todo-parallel-improves") -> BenchResult:
    """P1: Build a TODO CLI, then queue 3 parallel improves.

    Tests:
    - Real base build via queue
    - 3 parallel improves
    - Merge with likely-conflict (all touch the CLI)
    - Real post-merge cert with inline per-story pruning
    """
    hr(f"BENCH {name}")
    repo = make_repo("bench-p1-")
    log(f"repo: {repo}")
    t0 = time.time()
    try:
        # Phase 1: build the base
        log("phase 1: build base TODO CLI")
        otto_run(repo, "queue", "build", "--as", "base", "--",
                 "--fast", "--no-qa",
                 "Build a Python CLI 'todo.py' with commands: add <task>, list, done <id>, delete <id>. "
                 "Store tasks in tasks.json with id, text, done fields. Use argparse.")
        w = start_watcher(repo, concurrent=1)
        try:
            wait_for_all_done(repo, timeout=900, interval=20)
        finally:
            stop_watcher(w)
        statuses = {tid: ts.get("status") for tid, ts in queue_state(repo)["tasks"].items()}
        log(f"phase 1 done: {statuses}")
        if statuses.get("base") != "done":
            res = collect_metrics(repo, name, t0, concurrency=1)
            res.notes.append(f"base build did not reach done: {statuses}")
            res.write(RESULTS_DIR / f"{name}.json")
            return res

        # Merge base into main so improves start from it
        otto_run(repo, "merge", "--all", "--no-certify", "--cleanup-on-success")
        log("base merged into main")

        # Phase 2: 3 parallel improves (1 round each = 1 cert pass max)
        log("phase 2: queue 3 parallel improves")
        improves = [
            ("imp-priority", "Add priority field (low/med/high) to each task. New command 'priority <id> <level>'. List shows priority next to task text."),
            ("imp-duedate", "Add due_date field (ISO date string) to each task. New command 'due <id> <date>'. List sorts overdue tasks first and marks them with [OVERDUE]."),
            ("imp-tags", "Add tags field (list of strings) to each task. New command 'tag <id> <tag>'. New command 'filter --tag <name>' that lists tasks with that tag."),
        ]
        for tid, intent in improves:
            otto_run(repo, "queue", "improve", "feature", "--as", tid, "--", "-n", "1", intent)
        w = start_watcher(repo, concurrent=3)
        try:
            wait_for_all_done(repo, timeout=1800, interval=30)
        finally:
            stop_watcher(w)
        statuses = {tid: ts.get("status") for tid, ts in queue_state(repo)["tasks"].items()}
        log(f"phase 2 done: {statuses}")

        # Phase 3: merge — try --all first (only "done" tasks); if nothing
        # qualifies, fall back to merging all task branches by name (best
        # effort: improves often "fail" cert in 1 round but their commits
        # are still useful). Use --no-certify when falling back since the
        # branches haven't passed cert individually.
        log("phase 3: merge")
        merge_t0 = time.time()
        all_branches = [
            t.get("branch") for t in queue_state(repo)["tasks"].values()
            if t.get("branch")
        ]
        improve_branches = [b for b in all_branches if b and b.startswith("improve/")]
        statuses = {tid: ts.get("status") for tid, ts in queue_state(repo)["tasks"].items()}
        any_done_improve = any(
            statuses.get(tid) == "done" for tid in statuses if tid != "base"
        )
        if any_done_improve:
            merge_args = ["merge", "--all"]
        else:
            log(f"  improves did not pass cert; merging by branch name (best-effort)")
            merge_args = ["merge", *improve_branches, "--no-certify"]
        merge_r = otto_run(repo, *merge_args, check=False, timeout=600)
        merge_seconds = time.time() - merge_t0
        merge_out = (merge_r.stdout or "") + (merge_r.stderr or "")
        merge_outcome = "success" if merge_r.returncode == 0 else "failed"
        if "conflict_resolved" in merge_out.lower():
            merge_outcome = "conflict-resolved"
        log(f"merge: rc={merge_r.returncode}, outcome={merge_outcome}, seconds={merge_seconds:.0f}")

        # Pull merge cost from merge.log if present
        merge_cost = _sum_merge_agent_cost(repo)

        res = collect_metrics(repo, name, t0, concurrency=3)
        res.merge_outcome = merge_outcome
        res.merge_cost_usd = merge_cost
        res.merge_seconds = merge_seconds
        # Cert outcome from state.json
        merge_state_files = list((repo / "otto_logs" / "merge").glob("merge-*/state.json"))
        if merge_state_files:
            ms = json.loads(merge_state_files[-1].read_text())
            res.cert_passed = ms.get("cert_passed")
        res.total_cost_usd += merge_cost
        res.notes.append(f"merge_log_tail: {merge_out.strip()[-300:]}")
        res.write(RESULTS_DIR / f"{name}.json")
        return res
    except Exception as exc:
        log(f"FAILED: {exc}")
        res = collect_metrics(repo, name, t0, concurrency=3)
        res.notes.append(f"exception: {exc}")
        res.write(RESULTS_DIR / f"{name}.json")
        return res


def bench_p2_sequential_baseline(name: str = "P2-todo-sequential-baseline") -> BenchResult:
    """P2: Same 3 improves as P1 but with concurrent=1 — the sequential baseline.

    Use the same intents and a fresh base build to make the comparison fair.
    """
    hr(f"BENCH {name}")
    repo = make_repo("bench-p2-")
    log(f"repo: {repo}")
    t0 = time.time()
    try:
        # Phase 1: same base build
        log("phase 1: build base TODO CLI")
        otto_run(repo, "queue", "build", "--as", "base", "--",
                 "--fast", "--no-qa",
                 "Build a Python CLI 'todo.py' with commands: add <task>, list, done <id>, delete <id>. "
                 "Store tasks in tasks.json with id, text, done fields. Use argparse.")
        w = start_watcher(repo, concurrent=1)
        try:
            wait_for_all_done(repo, timeout=900, interval=20)
        finally:
            stop_watcher(w)
        if queue_state(repo)["tasks"]["base"]["status"] != "done":
            res = collect_metrics(repo, name, t0, concurrency=1)
            res.notes.append("base build failed")
            res.write(RESULTS_DIR / f"{name}.json")
            return res
        otto_run(repo, "merge", "--all", "--no-certify", "--cleanup-on-success")

        # Phase 2: 3 SEQUENTIAL improves (concurrent=1)
        log("phase 2: queue 3 SEQUENTIAL improves (concurrent=1)")
        improves = [
            ("imp-priority", "Add priority field (low/med/high) to each task. New command 'priority <id> <level>'. List shows priority next to task text."),
            ("imp-duedate", "Add due_date field (ISO date string) to each task. New command 'due <id> <date>'. List sorts overdue tasks first and marks them with [OVERDUE]."),
            ("imp-tags", "Add tags field (list of strings) to each task. New command 'tag <id> <tag>'. New command 'filter --tag <name>' that lists tasks with that tag."),
        ]
        for tid, intent in improves:
            otto_run(repo, "queue", "improve", "feature", "--as", tid, "--", "-n", "1", intent)
        w = start_watcher(repo, concurrent=1)  # SEQUENTIAL
        try:
            wait_for_all_done(repo, timeout=2400, interval=30)
        finally:
            stop_watcher(w)
        log(f"phase 2 done")

        # Phase 3: same merge
        merge_t0 = time.time()
        merge_r = otto_run(repo, "merge", "--all", check=False, timeout=600)
        merge_seconds = time.time() - merge_t0
        merge_outcome = "success" if merge_r.returncode == 0 else "failed"

        merge_cost = _sum_merge_agent_cost(repo)

        res = collect_metrics(repo, name, t0, concurrency=1)
        res.merge_outcome = merge_outcome
        res.merge_cost_usd = merge_cost
        res.merge_seconds = merge_seconds
        res.total_cost_usd += merge_cost
        res.write(RESULTS_DIR / f"{name}.json")
        return res
    except Exception as exc:
        log(f"FAILED: {exc}")
        res = collect_metrics(repo, name, t0, concurrency=1)
        res.notes.append(f"exception: {exc}")
        res.write(RESULTS_DIR / f"{name}.json")
        return res


# ------------------- driver -------------------

def _sum_merge_agent_cost(repo: Path) -> float:
    """Sum the conflict-agent cost across ALL merge runs in this repo.

    Reads `otto_logs/merge/merge-*/state.json` and parses BranchOutcome.note
    strings for `cost $X.YZ`. The consolidated path emits the SAME shared-
    cost note on every conflicted-branch row (one agent call, cost shared) —
    dedupe by note string within a state file before summing to avoid
    multiplying the agent cost by the number of conflicted branches.
    """
    import re
    cost_re = re.compile(r"cost \$(\d+(?:\.\d+)?)")
    total = 0.0
    for state_file in (repo / "otto_logs" / "merge").glob("merge-*/state.json"):
        try:
            d = json.loads(state_file.read_text())
        except Exception:
            continue
        seen_notes: set[str] = set()
        for o in d.get("outcomes", []):
            note = o.get("note") or ""
            if not note or note in seen_notes:
                continue
            m = cost_re.search(note)
            if m:
                total += float(m.group(1))
                seen_notes.add(note)
    return total


def bench_p3_bookmark_parallel_features(name: str = "P3-bookmark-parallel-features") -> BenchResult:
    """P3: Build a Flask bookmark API, then queue 2 parallel feature improves.

    Tests:
    - Real complex web product
    - 2 parallel improves on shared codebase
    - Higher chance of conflicts (more files / shared modules)
    - Real post-merge cert on multi-story codebase

    Uses Flask (simpler than Next.js for tmp-bench purposes; no node deps).
    """
    hr(f"BENCH {name}")
    repo = make_repo("bench-p3-")
    log(f"repo: {repo}")
    t0 = time.time()
    try:
        # Phase 1: build base bookmark API
        log("phase 1: build Flask bookmark API")
        otto_run(repo, "queue", "build", "--as", "base", "--",
                 "--fast", "--no-qa",
                 "Build a Flask app 'app.py' for bookmarks. POST /bookmarks (json: url, title, "
                 "description) creates a bookmark. GET /bookmarks lists all. GET /bookmarks/<id> "
                 "fetches one. DELETE /bookmarks/<id> deletes. Persist in bookmarks.json. "
                 "Show example curl commands in README.md.")
        w = start_watcher(repo, concurrent=1)
        try:
            wait_for_all_done(repo, timeout=900, interval=20)
        finally:
            stop_watcher(w)
        if queue_state(repo)["tasks"]["base"]["status"] != "done":
            res = collect_metrics(repo, name, t0, concurrency=2)
            res.notes.append("base build failed")
            res.write(RESULTS_DIR / f"{name}.json")
            return res
        otto_run(repo, "merge", "--all", "--no-certify", "--cleanup-on-success")

        # Phase 2: 2 parallel improves
        log("phase 2: queue 2 parallel feature improves")
        improves = [
            ("imp-tags", "Add a 'tags' field (list of strings) to bookmarks. POST/PATCH /bookmarks accept tags. New endpoint GET /bookmarks?tag=<name> filters by tag."),
            ("imp-search", "Add full-text search. New endpoint GET /search?q=<query> returns bookmarks where the query appears in title or description (case-insensitive substring match)."),
        ]
        for tid, intent in improves:
            otto_run(repo, "queue", "improve", "feature", "--as", tid, "--", "-n", "1", intent)
        w = start_watcher(repo, concurrent=2)
        try:
            wait_for_all_done(repo, timeout=1800, interval=30)
        finally:
            stop_watcher(w)
        log(f"phase 2 done: {[(tid, ts.get('status')) for tid,ts in queue_state(repo)['tasks'].items()]}")

        # Phase 3: merge
        merge_t0 = time.time()
        merge_r = otto_run(repo, "merge", "--all", check=False, timeout=600)
        merge_seconds = time.time() - merge_t0
        merge_outcome = "success" if merge_r.returncode == 0 else "failed"
        merge_out = (merge_r.stdout or "") + (merge_r.stderr or "")
        if "conflict_resolved" in merge_out.lower():
            merge_outcome = "conflict-resolved"
        log(f"merge: rc={merge_r.returncode}, outcome={merge_outcome}, seconds={merge_seconds:.0f}")

        merge_cost = _sum_merge_agent_cost(repo)

        res = collect_metrics(repo, name, t0, concurrency=2)
        res.merge_outcome = merge_outcome
        res.merge_cost_usd = merge_cost
        res.merge_seconds = merge_seconds
        res.total_cost_usd += merge_cost
        merge_state_files = list((repo / "otto_logs" / "merge").glob("merge-*/state.json"))
        if merge_state_files:
            ms = json.loads(merge_state_files[-1].read_text())
            res.cert_passed = ms.get("cert_passed")
        res.notes.append(f"merge_log_tail: {merge_out.strip()[-300:]}")
        res.write(RESULTS_DIR / f"{name}.json")
        return res
    except Exception as exc:
        log(f"FAILED: {exc}")
        res = collect_metrics(repo, name, t0, concurrency=2)
        res.notes.append(f"exception: {exc}")
        res.write(RESULTS_DIR / f"{name}.json")
        return res


def _run_complex_bench(
    *,
    name: str,
    prefix: str,
    base_intent: str,
    improves: list[tuple[str, str]],
    concurrent: int = 3,
    rounds: int = 2,
) -> BenchResult:
    """Shared driver for complex multi-feature product benchmarks.

    Designed for products with real domain logic (multi-module APIs, build
    pipelines, etc.) where improves SHOULD touch shared files and produce
    genuine merge conflicts. Uses `improve feature -n <rounds>` so the agent
    has multiple cert rounds to actually pass — at `-n 1` improves on
    complex products always 'fail' because the cert identifies more gaps
    than fit in one round.
    """
    hr(f"BENCH {name}")
    repo = make_repo(prefix)
    log(f"repo: {repo}")
    t0 = time.time()
    try:
        log("phase 1: build base")
        otto_run(repo, "queue", "build", "--as", "base", "--",
                 "--fast", "--no-qa", base_intent)
        w = start_watcher(repo, concurrent=1)
        try:
            wait_for_all_done(repo, timeout=1800, interval=20)
        finally:
            stop_watcher(w)
        if queue_state(repo)["tasks"]["base"]["status"] != "done":
            res = collect_metrics(repo, name, t0, concurrency=concurrent)
            res.notes.append("base build failed")
            res.write(RESULTS_DIR / f"{name}.json")
            return res
        otto_run(repo, "merge", "--all", "--no-certify", "--cleanup-on-success")
        log(f"base merged. queueing {len(improves)} parallel improves (concurrent={concurrent}, -n {rounds}).")

        for tid, intent in improves:
            otto_run(repo, "queue", "improve", "feature", "--as", tid, "--",
                     "-n", str(rounds), intent)
        w = start_watcher(repo, concurrent=concurrent)
        try:
            wait_for_all_done(repo, timeout=3600, interval=30)
        finally:
            stop_watcher(w)
        statuses = {tid: ts.get("status") for tid, ts in queue_state(repo)["tasks"].items()}
        log(f"phase 2 done: {statuses}")

        # Phase 3: merge — try --all first, fall back to explicit branches
        log("phase 3: merge")
        merge_t0 = time.time()
        all_branches = [t.get("branch") for t in queue_state(repo)["tasks"].values() if t.get("branch")]
        improve_branches = [b for b in all_branches if b and b.startswith("improve/")]
        any_done_improve = any(
            ts.get("status") == "done" for tid, ts in queue_state(repo)["tasks"].items()
            if tid != "base"
        )
        if any_done_improve:
            merge_args = ["merge", "--all"]
        else:
            log("  improves did not pass cert; merging by branch name (best-effort)")
            merge_args = ["merge", *improve_branches, "--no-certify"]
        merge_r = otto_run(repo, *merge_args, check=False, timeout=900)
        merge_seconds = time.time() - merge_t0
        merge_out = (merge_r.stdout or "") + (merge_r.stderr or "")
        merge_outcome = "success" if merge_r.returncode == 0 else "failed"
        if "conflict_resolved" in merge_out.lower():
            merge_outcome = "conflict-resolved"
        log(f"merge: rc={merge_r.returncode}, outcome={merge_outcome}, seconds={merge_seconds:.0f}")

        merge_cost = _sum_merge_agent_cost(repo)
        res = collect_metrics(repo, name, t0, concurrency=concurrent)
        res.merge_outcome = merge_outcome
        res.merge_cost_usd = merge_cost
        res.merge_seconds = merge_seconds
        res.total_cost_usd += merge_cost
        merge_state_files = list((repo / "otto_logs" / "merge").glob("merge-*/state.json"))
        if merge_state_files:
            ms = json.loads(sorted(merge_state_files)[-1].read_text())
            res.cert_passed = ms.get("cert_passed")
        res.notes.append(f"rounds_per_improve: {rounds}")
        res.notes.append(f"merge_log_tail: {merge_out.strip()[-300:]}")
        res.write(RESULTS_DIR / f"{name}.json")
        return res
    except Exception as exc:
        log(f"FAILED: {exc}")
        res = collect_metrics(repo, name, t0, concurrency=concurrent)
        res.notes.append(f"exception: {exc}")
        res.write(RESULTS_DIR / f"{name}.json")
        return res


def bench_p4_flask_multi_module(name: str = "P4-flask-auth-users-posts") -> BenchResult:
    """P4: Flask REST API with auth + users + posts. Improves all touch
    posts.py and the data layer — should produce real conflicts.
    """
    return _run_complex_bench(
        name=name,
        prefix="bench-p4-",
        base_intent=(
            "Build a Flask REST API in app.py with SQLite persistence "
            "(no SQLAlchemy — use the sqlite3 stdlib module) and JWT auth. "
            "Endpoints: POST /register {username, password}, POST /login -> {token}, "
            "GET/PATCH/DELETE /users/me (auth required), "
            "POST /posts {title, body} (auth required, sets author_id), "
            "GET /posts (public, list all with author username), "
            "GET /posts/<id>, PATCH /posts/<id> (author only), DELETE /posts/<id> (author only). "
            "Use PyJWT. Hash passwords with hashlib.sha256+salt. "
            "Schema: users(id, username, password_hash, salt, created_at), "
            "posts(id, author_id, title, body, created_at). "
            "README.md with curl examples for each endpoint."
        ),
        improves=[
            ("imp-comments",
             "Add comments to posts. New table comments(id, post_id, author_id, body, created_at). "
             "POST /posts/<id>/comments {body} (auth required). "
             "GET /posts/<id>/comments lists comments with author username. "
             "DELETE /comments/<id> (author only)."),
            ("imp-tags",
             "Add tags to posts. New columns: posts.tags (JSON array of strings). "
             "POST/PATCH /posts now accept a tags field. "
             "GET /posts?tag=<x> filters by tag. "
             "New endpoint GET /tags returns all unique tags with post counts."),
            ("imp-likes",
             "Add likes to posts. New table likes(post_id, user_id, created_at, PRIMARY KEY(post_id, user_id)). "
             "POST /posts/<id>/like toggles a like by the authenticated user (returns {liked: bool, count: N}). "
             "GET /posts and GET /posts/<id> include `like_count` and (when authed) `liked_by_me`."),
        ],
        concurrent=3,
        rounds=2,
    )


def bench_p5_markdown_ssg(name: str = "P5-markdown-blog-ssg") -> BenchResult:
    """P5: Markdown static-site generator (multi-file pipeline).

    Improves all touch the build pipeline + shared template/index logic.
    Realistic conflicts on the build orchestration code.
    """
    return _run_complex_bench(
        name=name,
        prefix="bench-p5-",
        base_intent=(
            "Build a Python static site generator 'blog.py' that reads .md files from "
            "posts/<slug>.md (each with YAML front-matter: title, date, tags) and "
            "generates an HTML site under dist/. "
            "Use python-markdown and jinja2 (assume installed). "
            "Outputs: dist/index.html (list of posts by date desc, with title/date/excerpt) "
            "and dist/posts/<slug>.html for each post. "
            "Templates in templates/base.html, templates/index.html, templates/post.html. "
            "Include 2 example posts in posts/ for testing. "
            "README.md explains how to run."
        ),
        improves=[
            ("imp-tag-pages",
             "Add tag pages. For each unique tag across all posts, generate "
             "dist/tags/<tag>.html listing every post with that tag (sorted by date desc). "
             "Add a tags index at dist/tags/index.html with all tags and counts. "
             "Link tags from each post page to the corresponding tag page."),
            ("imp-rss",
             "Add an RSS 2.0 feed at dist/rss.xml with the most recent 20 posts. "
             "Each entry: title, link to the post page, pubDate, description (the first 200 "
             "chars of the rendered post body, plain text). "
             "Channel info: title='My Blog', link, description, lastBuildDate."),
            ("imp-search",
             "Add full-text client-side search. Generate dist/search.json — an array of "
             "{slug, title, date, tags, plain_text_body}. "
             "Generate dist/search.html — a static page with a search input that, via "
             "vanilla JS, fetches search.json once and filters live as the user types. "
             "Result links go to the matching post page. Link from index.html."),
        ],
        concurrent=3,
        rounds=2,
    )


def bench_p6_inventory_cli(name: str = "P6-inventory-cli") -> BenchResult:
    """P6: Inventory management CLI with SQLite + multi-domain features.

    Improves add categories, suppliers, alerts — all touching the schema and
    shared CLI dispatch. Real conflicts likely on the schema migration path.
    """
    return _run_complex_bench(
        name=name,
        prefix="bench-p6-",
        base_intent=(
            "Build a Python CLI 'inv.py' for inventory management with SQLite (sqlite3 stdlib). "
            "Commands: add <name> <qty> <price> (creates an item), list (table of all items), "
            "update <id> [--name X] [--qty N] [--price P], delete <id>, "
            "stock <id> +N|-N (adjusts qty, logs the change in stock_log). "
            "Schema: items(id, name UNIQUE, qty, price, created_at), "
            "stock_log(id, item_id, delta, reason, ts). "
            "Show colored table output (use shutil.get_terminal_size for width). "
            "README.md with example session."
        ),
        improves=[
            ("imp-categories",
             "Add categories. New table categories(id, name UNIQUE) and column items.category_id. "
             "Commands: category-add <name>, category-list, category-rm <id>, "
             "list --category <name> filters items. update gains --category <name>. "
             "On 'add', --category <name> creates the category if missing."),
            ("imp-suppliers",
             "Add suppliers. New table suppliers(id, name UNIQUE, contact) and "
             "column items.supplier_id. Commands: supplier-add <name> [--contact <s>], "
             "supplier-list, supplier-rm <id>. update gains --supplier <name>. "
             "list --supplier <name> filters items."),
            ("imp-alerts",
             "Add stock alerts. New column items.min_qty (default 0). "
             "Commands: alerts (lists all items where qty <= min_qty, sorted by qty asc), "
             "set-min <id> <n> (sets min_qty). After every 'stock' command, if the new "
             "qty drops to/below min_qty, print a colored ALERT line below the success line."),
        ],
        concurrent=3,
        rounds=2,
    )


BENCHES = {
    "P1": bench_p1_todo_parallel_improves,
    "P2": bench_p2_sequential_baseline,
    "P3": bench_p3_bookmark_parallel_features,
    "P4": bench_p4_flask_multi_module,
    "P5": bench_p5_markdown_ssg,
    "P6": bench_p6_inventory_cli,
}


def main() -> int:
    args = sys.argv[1:] or ["all"]
    selected: list[str] = []
    for arg in args:
        if arg.lower() == "all":
            selected = sorted(BENCHES.keys())
        elif arg.upper() in BENCHES:
            selected.append(arg.upper())
        else:
            print(f"unknown bench: {arg!r}", file=sys.stderr)
            return 2

    results: list[BenchResult] = []
    for name in selected:
        res = BENCHES[name](name=BENCHES[name].__name__.replace("bench_", ""))
        results.append(res)
        print(res.short_summary())

    hr("OVERALL")
    for r in results:
        print(r.short_summary())
    print(f"\n  Results written to {RESULTS_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
