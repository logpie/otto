"""F13 bench: P6 with consolidated agent-mode merge.

Same scenario as P6 baseline. After improves finish, runs salvage merge.
Compares to historical baselines:
- Original P6 baseline (sequential, $2.41/13.3min for 1 conflict)
- F12 P6 (sequential, Edit-only, $19.22/49min for 2 conflicts)
- Post-revert P6 (sequential, Write allowed, $7.52/37min for 2 conflicts)
- Pre-deletion P6 (consolidated opt-in, $5.12/18min for 2 conflicts)

Run: OTTO_ALLOW_REAL_COST=1 .venv/bin/python scripts/bench_p6_consolidated.py
"""

from __future__ import annotations

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
    bench_p6_inventory_cli,
    log,
    queue_state,
)
from bench_costs import merge_cost_from_state_dir  # noqa: E402
from real_cost_guard import require_real_cost_opt_in  # noqa: E402


def main() -> int:
    require_real_cost_opt_in("P6 consolidated benchmark")
    # Env-controlled: WITH_CERT=1 runs the post-merge cert phase (exercises
    # the merge_context preamble path that replaced triage). Default is
    # --no-certify (just measures merge wall + cost).
    with_cert = os.environ.get("WITH_CERT") == "1"
    suffix = "-with-cert" if with_cert else ""
    name = f"F13-P6-consolidated{suffix}"
    log(f"Running P6 with consolidated agent-mode merge (cert={'on' if with_cert else 'off'})")
    # Run base + improves via existing P6 bench (gives us a completed set of
    # branches in a tmp dir).
    log("Phase 1+2: build base + queue 3 parallel improves (vanilla P6 bench)")
    p6_result = bench_p6_inventory_cli(name="F13-P6-phases-1-2")
    repo = Path(p6_result.repo_path)
    log(f"P6 base + improves done. repo: {repo}, cost so far ${p6_result.total_cost_usd:.2f}")

    # Identify all improve branches (the ones that have work to merge)
    state = queue_state(repo)
    failed_branches = []
    for tid, ts in state.get("tasks", {}).items():
        if tid == "base":
            continue
        if ts.get("status") in ("done", "failed"):
            # Find branch from queue.yml
            from otto.queue.schema import load_queue
            for t in load_queue(repo):
                if t.id == tid and t.branch:
                    failed_branches.append(t.branch)
                    break
    log(f"Branches to salvage-merge: {failed_branches}")

    if not failed_branches:
        log("No improve branches to merge — bench inconclusive")
        return 1

    # Consolidated agent-mode is now the only merge path — no config flip needed.

    merge_args = [str(OTTO_BIN), "merge", *failed_branches]
    if not with_cert:
        merge_args.append("--no-certify")
    cert_label = "with cert" if with_cert else "with --no-certify"
    log(f"Phase 3: consolidated merge of {len(failed_branches)} branches ({cert_label})")
    merge_t0 = time.time()
    r = subprocess.run(
        merge_args,
        cwd=repo, capture_output=True, text=True, timeout=3600,
        env={**os.environ},
    )
    merge_seconds = time.time() - merge_t0
    out = (r.stdout or "") + (r.stderr or "")
    log(f"Merge done in {merge_seconds:.0f}s, rc={r.returncode}")

    merge_cost = merge_cost_from_state_dir(repo / "otto_logs" / "merge")

    res = BenchResult(
        name=name,
        started_at=p6_result.started_at,
        finished_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        wall_seconds=p6_result.wall_seconds + merge_seconds,
        total_cost_usd=p6_result.total_cost_usd + merge_cost,
        queue_concurrency=3,
        tasks=p6_result.tasks,
        merge_outcome="success" if r.returncode == 0 else "failed",
        merge_cost_usd=merge_cost,
        merge_seconds=merge_seconds,
        cert_passed=None,
        notes=[
            "F13 consolidated agent-mode merge",
            f"Branches merged: {failed_branches}",
            f"Phase 1+2 cost (base + improves): ${p6_result.total_cost_usd:.2f}",
            f"Phase 3 cost (consolidated agent merge): ${merge_cost:.2f}",
            f"Phase 3 wall: {merge_seconds:.0f}s ({merge_seconds/60:.1f}min)",
            f"Merge log tail: {out.strip()[-300:]}",
        ],
        repo_path=str(repo),
    )
    out_path = RESULTS_DIR / f"{name}.json"
    res.write(out_path)
    log(f"Wrote {out_path}")
    print()
    print(res.short_summary())
    return 0 if r.returncode == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
