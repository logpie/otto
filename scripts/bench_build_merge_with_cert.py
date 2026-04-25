"""Minimal build→merge bench to validate the post-merge cert path.

Two `otto queue build` runs that produce conflicting changes on the same
file. Each build's --fast certifier registers stories in its queue manifest.
Then `otto merge --all` (with cert) exercises:
  - the merge_context preamble in the cert agent prompt
  - per-story SKIPPED / FLAG_FOR_HUMAN / PASS / FAIL verdicts
  - state.json carrying cert_passed + cert_run_id (not just None)

This is the post-deletion cert path that P6 doesn't exercise (improves
don't register stories, so P6 hits the no-stories early-exit instead).

Usage: OTTO_ALLOW_REAL_COST=1 .venv/bin/python scripts/bench_build_merge_with_cert.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from bench_runner import (  # noqa: E402
    BenchResult,
    OTTO_BIN,
    RESULTS_DIR,
    log,
    make_repo,
    otto_run,
    queue_state,
    proof_of_work_path,
    start_watcher,
    stop_watcher,
    wait_for_all_done,
)
from bench_costs import merge_cost_from_state_dir  # noqa: E402
from real_cost_guard import require_real_cost_opt_in  # noqa: E402


# Two conflicting build intents — both write `tools.py` with a CLI.
INTENT_ADD = (
    "Build a Python CLI in `tools.py` with one command: `add A B` that prints "
    "A+B (integer addition). Use argparse. Include a test in `test_tools.py` "
    "that runs `python tools.py add 2 3` and asserts output is 5."
)

INTENT_SUB = (
    "Build a Python CLI in `tools.py` with one command: `sub A B` that prints "
    "A-B (integer subtraction). Use argparse. Include a test in `test_tools.py` "
    "that runs `python tools.py sub 5 3` and asserts output is 2."
)


def _read_merge_state(repo: Path) -> dict | None:
    """Return the latest merge state.json (most recent merge dir)."""
    merges = sorted((repo / "otto_logs" / "merge").glob("merge-*/state.json"))
    if not merges:
        return None
    try:
        return json.loads(merges[-1].read_text())
    except Exception:
        return None


def main() -> int:
    require_real_cost_opt_in("build/merge benchmark")
    name = "build-merge-with-cert"
    log(f"Running {name}: 2 conflicting builds → merge with cert")
    repo = make_repo("bench-build-merge-cert-")
    log(f"repo: {repo}")
    t0 = time.time()

    try:
        # Phase 1: enqueue 2 fast builds that conflict on tools.py
        log("phase 1: enqueue 2 conflicting builds")
        otto_run(repo, "queue", "build", INTENT_ADD, "--as", "add", "--", "--fast")
        otto_run(repo, "queue", "build", INTENT_SUB, "--as", "sub", "--", "--fast")

        log("phase 2: start watcher (concurrent=2)")
        w = start_watcher(repo, concurrent=2)
        try:
            statuses = wait_for_all_done(repo, timeout=900, interval=20)
            log(f"phase 2 done: {statuses}")
        finally:
            stop_watcher(w)

        # Identify the done task IDs (NOT branches — passing task IDs lets
        # the merge orchestrator populate queue_task_lookup, which
        # collect_stories_from_branches needs to find each branch's PoW).
        state = queue_state(repo)
        done_task_ids: list[str] = []
        from otto.queue.schema import load_queue
        for t in load_queue(repo):
            tstate = state.get("tasks", {}).get(t.id, {})
            if tstate.get("status") == "done" and t.branch:
                done_task_ids.append(t.id)
        log(f"task ids to merge: {done_task_ids}")
        if len(done_task_ids) < 2:
            log(f"expected 2 done tasks, got {len(done_task_ids)} — bench inconclusive")
            return 1

        # Phase 3: merge with cert (the path we're validating). Pass task
        # IDs (not branch names) so queue_task_lookup gets populated.
        log("phase 3: otto merge <task-ids> (with cert)")
        merge_t0 = time.time()
        r = subprocess.run(
            [str(OTTO_BIN), "merge", *done_task_ids],
            cwd=repo, capture_output=True, text=True, timeout=3600,
            env={**os.environ},
        )
        merge_seconds = time.time() - merge_t0
        out = (r.stdout or "") + (r.stderr or "")
        log(f"merge done in {merge_seconds:.0f}s, rc={r.returncode}")

        merge_cost = merge_cost_from_state_dir(repo / "otto_logs" / "merge")
        merge_state = _read_merge_state(repo) or {}
        cert_passed = merge_state.get("cert_passed")
        cert_run_id = merge_state.get("cert_run_id")
        log(f"cert_passed={cert_passed}  cert_run_id={cert_run_id}")

        # Read cert proof-of-work to count verdicts (this is what we're really
        # validating: cert ran, saw stories, emitted per-story verdicts).
        verdict_counts: dict[str, int] = {}
        if cert_run_id:
            pow_file = proof_of_work_path(repo, cert_run_id)
            if pow_file.exists():
                try:
                    pow_data = json.loads(pow_file.read_text())
                    for s in pow_data.get("stories", []):
                        v = s.get("verdict") or ("PASS" if s.get("passed") else "FAIL")
                        verdict_counts[v] = verdict_counts.get(v, 0) + 1
                except Exception as exc:
                    log(f"could not read proof-of-work: {exc}")
        log(f"verdict counts: {verdict_counts or '(no cert PoW found)'}")

        # Aggregate cost across queue tasks too
        total_cost = merge_cost
        for tid, ts in state.get("tasks", {}).items():
            total_cost += float(ts.get("cost_usd") or 0.0)

        res = BenchResult(
            name=name,
            started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0)),
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            wall_seconds=time.time() - t0,
            total_cost_usd=total_cost,
            queue_concurrency=2,
            tasks=[],
            merge_outcome="success" if r.returncode == 0 else "failed",
            merge_cost_usd=merge_cost,
            merge_seconds=merge_seconds,
            cert_passed=cert_passed,
            notes=[
                f"cert_run_id: {cert_run_id}",
                f"verdict counts: {verdict_counts}",
                f"merged task ids: {done_task_ids}",
                f"merge log tail: {out.strip()[-400:]}",
            ],
            repo_path=str(repo),
        )
        out_path = RESULTS_DIR / f"{name}.json"
        res.write(out_path)
        log(f"wrote {out_path}")
        print()
        print(res.short_summary())
        return 0 if r.returncode == 0 else 1
    except Exception as exc:
        log(f"bench crashed: {exc}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
