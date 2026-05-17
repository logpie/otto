"""Run #7 regression: the architect/scaffold child must receive the REAL
sibling feature task_ids through the sanctioned runtime-context channel.

Run #7 (mib7-010708, --tier modular): policy-label fix (104522af8) held
(check_route_isolation PASS even on a long descriptive policy sentence), but
the architect keyed feature_owned_paths by invented placeholders
(`PLACEHOLDER_ISSUES_FEATURE`, ...) → persist raised
`feature ownership references an unknown task_id` ×N → contract gate failed.

Root cause: lead.md told the architect to read sibling task_ids from
`otto_logs/cross-sessions/task_graph.json`, but the architect runs in an
isolated worktree with NO otto_logs/ (otto-owned, never propagated to agent
surfaces). The instruction was unsatisfiable, so the architect invented keys.

Fix: _build_decomp_runtime_context (rendered as JSON into the architect prompt
via lead.py's {decomp_runtime_context}) now carries `feature_partition_targets`
— the real {task_id,title} of every feature child — and lead.md keys
feature_owned_paths off that instead of the unreachable otto_logs path. This
pins: given a graph with feature children, the runtime context exposes their
exact task_ids (never empty, never placeholders).
"""

from __future__ import annotations

import json
from pathlib import Path

from otto.queue.task_graph import read_graph
from otto.v5_runner import _build_decomp_runtime_context


def _seed_graph(tmp: Path, tasks: dict[str, dict]) -> None:
    (tmp / "otto_logs" / "cross-sessions").mkdir(parents=True, exist_ok=True)
    graph_path = tmp / "otto_logs" / "cross-sessions" / "task_graph.json"
    graph_path.write_text(json.dumps({"tasks": tasks}), encoding="utf-8")
    # Sanity: the helper round-trips through the real reader.
    assert read_graph(tmp).get("tasks")


def test_feature_partition_targets_lists_real_feature_task_ids(tmp_path: Path) -> None:
    _seed_graph(
        tmp_path,
        {
            "root": {"task_role": "feature", "intent": "root product"},
            "v5-arch": {"task_role": "foundation", "title": "## Architect & Scaffold"},
            "v5-feat-a": {"task_role": "feature", "title": "## Feature: Issues & Teams"},
            "v5-feat-b": {"task_role": "feature", "title": "## Feature: Cycles"},
        },
    )
    ctx = _build_decomp_runtime_context(
        project_dir=tmp_path,
        config={"v5_tier": "modular", "run_budget_seconds": 3000},
        max_parallel=4,
        run_started_at=None,
    )
    targets = ctx.get("feature_partition_targets")
    assert isinstance(targets, list)
    ids = {t["task_id"] for t in targets}
    # Real feature children only — never root, never the foundation child.
    assert ids == {"v5-feat-a", "v5-feat-b"}, ids
    titles = {t["task_id"]: t["title"] for t in targets}
    assert "Issues & Teams" in titles["v5-feat-a"]
    # Every target carries a non-empty title so the architect can scope it.
    assert all(t["title"].strip() for t in targets)


def test_no_features_yields_empty_targets_not_crash(tmp_path: Path) -> None:
    _seed_graph(
        tmp_path,
        {
            "root": {"task_role": "feature", "intent": "solo"},
            "v5-arch": {"task_role": "foundation", "title": "scaffold"},
        },
    )
    ctx = _build_decomp_runtime_context(
        project_dir=tmp_path,
        config={"v5_tier": "modular"},
        max_parallel=2,
        run_started_at=None,
    )
    assert ctx.get("feature_partition_targets") == []
