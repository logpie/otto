"""Aggregate bench-results/*.json into a markdown report."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "bench-results"


def fmt_seconds(s: float) -> str:
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s/60:.1f}m"
    return f"{s/3600:.2f}h"


def gen_report() -> str:
    files = sorted(RESULTS_DIR.glob("*.json"))
    if not files:
        return "No bench results found.\n"
    out: list[str] = []
    out.append("# parallel-otto Real Product Benchmarks\n")
    out.append("Real LLM runs against complex products. Each bench builds a base "
               "product, queues feature improves, and merges. See bench-results/ "
               "for raw json per bench.\n")

    # Summary table
    out.append("## Summary\n")
    out.append("| Bench | Concurrency | Tasks | Wall | Cost | Merge | Cert |")
    out.append("|---|---|---|---|---|---|---|")
    by_name: dict[str, dict] = {}
    for f in files:
        d = json.loads(f.read_text())
        by_name[d["name"]] = d
        n_tasks = len(d.get("tasks", []))
        n_done = sum(1 for t in d.get("tasks", []) if t.get("status") == "done")
        out.append(
            f"| `{d['name']}` | {d.get('queue_concurrency')} | "
            f"{n_done}/{n_tasks} done | {fmt_seconds(d.get('wall_seconds') or 0)} | "
            f"${d.get('total_cost_usd') or 0:.2f} | "
            f"{d.get('merge_outcome') or '–'}"
            + (f" (+${d.get('merge_cost_usd'):.2f}, +{fmt_seconds(d.get('merge_seconds') or 0)})"
               if d.get("merge_cost_usd") else "")
            + f" | {d.get('cert_passed') if d.get('cert_passed') is not None else '–'} |"
        )

    # Parallel speedup if both P1 and P2 present
    p1 = by_name.get("P1-todo-parallel-improves")
    p2 = by_name.get("P2-todo-sequential-baseline")
    if p1 and p2:
        out.append("\n## Parallel Speedup (P1 vs P2)\n")
        speedup = (p2["wall_seconds"] or 0) / max(1, p1["wall_seconds"] or 1)
        out.append(f"- P1 wall time (concurrent=3): {fmt_seconds(p1['wall_seconds'])}")
        out.append(f"- P2 wall time (concurrent=1): {fmt_seconds(p2['wall_seconds'])}")
        out.append(f"- **Speedup**: {speedup:.2f}× (parallel vs sequential)")
        out.append(f"- Cost difference: ${(p1.get('total_cost_usd') or 0) - (p2.get('total_cost_usd') or 0):+.2f} (parallel - sequential)")
        out.append(f"- Per-task cost in parallel: avg ${sum(t['cost_usd'] for t in p1['tasks']) / max(1, len(p1['tasks'])):.2f}")
        out.append(f"- Per-task cost in sequential: avg ${sum(t['cost_usd'] for t in p2['tasks']) / max(1, len(p2['tasks'])):.2f}")

    # Per-bench details
    out.append("\n## Per-Bench Details\n")
    for f in files:
        d = json.loads(f.read_text())
        out.append(f"### {d['name']}\n")
        out.append(f"- repo: `{d.get('repo_path')}`")
        out.append(f"- started: {d.get('started_at')} → finished: {d.get('finished_at')}")
        out.append(f"- wall: {fmt_seconds(d.get('wall_seconds') or 0)}, total cost: ${d.get('total_cost_usd') or 0:.2f}")
        out.append(f"- concurrency: {d.get('queue_concurrency')}")
        if d.get("merge_outcome"):
            out.append(f"- merge: **{d['merge_outcome']}** (+${d.get('merge_cost_usd') or 0:.2f}, +{fmt_seconds(d.get('merge_seconds') or 0)}); cert_passed={d.get('cert_passed')}")
        out.append("\n#### Per-task")
        out.append("| ID | Status | Cost | Duration | Failure |")
        out.append("|---|---|---|---|---|")
        for t in d.get("tasks", []):
            out.append(
                f"| `{t['id']}` | {t['status']} | ${t.get('cost_usd') or 0:.2f} | "
                f"{fmt_seconds(t.get('duration_s') or 0)} | "
                f"{(t.get('failure_reason') or '')[:80]} |"
            )
        if d.get("notes"):
            out.append("\n#### Notes")
            for n in d["notes"]:
                out.append(f"- {n[:300]}{'...' if len(n) > 300 else ''}")
        out.append("")

    return "\n".join(out)


def main() -> int:
    report = gen_report()
    out_path = REPO_ROOT / "bench-report.md"
    out_path.write_text(report)
    print(f"Wrote {out_path} ({len(report.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
